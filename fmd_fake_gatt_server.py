#!/usr/bin/env python3

"""
FHN (Find Hub Network) GATT Server for Raspberry Pi

Implements the Beacon Actions characteristic (FE2C1238-8366-4814-8EB0-01DE32100BEA)
per the Find Hub Network Accessory Specification v1.3.

Requires the Ephemeral Identity Key (EIK) to compute valid HMAC authentication
for ring responses and other operations.

Usage:
  python fmd_fake_gatt_server.py --eik <64-char-hex-EIK> [--account-key <32-char-hex>]

  The EIK is the 32-byte key generated during device registration.
  You can retrieve it from the Google backend using this project's tools.

Requirements:
  sudo apt-get install python3-dbus python3-gi
"""

import sys
import argparse
import hashlib
import hmac as hmac_mod
import time as time_mod
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import secrets
from Cryptodome.Cipher import AES
from ecdsa import SECP160r1

BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'

FAST_PAIR_SVC_UUID = 'FE2C'
MODEL_ID_CHR_UUID = 'FE2C1233-8366-4814-8EB0-01DE32100BEA'
KEY_BASED_PAIRING_CHR_UUID = 'FE2C1234-8366-4814-8EB0-01DE32100BEA'
PASSKEY_CHR_UUID = 'FE2C1235-8366-4814-8EB0-01DE32100BEA'
ACCOUNT_KEY_CHR_UUID = 'FE2C1236-8366-4814-8EB0-01DE32100BEA'
BEACON_ACTIONS_CHR_UUID = 'FE2C1238-8366-4814-8EB0-01DE32100BEA'
DEVICE_INFO_SVC_UUID = '180A'
FIRMWARE_REVISION_CHR_UUID = '2A26'

PROTOCOL_MAJOR_VERSION = 0x01
K = 10
ROTATION_PERIOD = 1024  # 2^K seconds


def truncated_sha256(data):
    return hashlib.sha256(data).digest()[:8]


def compute_hmac(key, message):
    return hmac_mod.new(key, message, hashlib.sha256).digest()[:8]


def _generate_eid_from_eik(identity_key: bytes, time_offset: int) -> bytes:
    """Compute EID from EIK and time offset (seconds since pair_date, K low bits cleared)."""
    mask = ~((1 << K) - 1)
    time_offset &= mask
    ts_bytes = time_offset.to_bytes(4, byteorder='big')

    data = bytearray(32)
    data[0:11] = b'\xFF' * 11
    data[11] = K
    data[12:16] = ts_bytes
    data[16:27] = b'\x00' * 11
    data[27] = K
    data[28:32] = ts_bytes

    cipher = AES.new(identity_key, AES.MODE_ECB)
    r_dash = cipher.encrypt(bytes(data))

    r_dash_int = int.from_bytes(r_dash, byteorder='big', signed=False)
    curve = SECP160r1
    r = r_dash_int % curve.order
    R = r * curve.generator
    return R.x().to_bytes(20, 'big')


class FHNKeys:
    """Derives all FHN keys from the Ephemeral Identity Key."""

    def __init__(self, eik_hex=None, account_key_hex=None, eid_hex=None, pair_date=None):
        self.eik = None
        self.eid = None
        self.ring_key = None
        self.recovery_key = None
        self.utp_key = None
        self.account_key = None
        self.pair_date = pair_date  # Unix timestamp at registration

        if eik_hex:
            self.eik = bytes.fromhex(eik_hex)
            assert len(self.eik) == 32, "EIK must be 32 bytes"
            self.recovery_key = truncated_sha256(self.eik + b'\x01')
            self.ring_key = truncated_sha256(self.eik + b'\x02')
            self.utp_key = truncated_sha256(self.eik + b'\x03')
            print(f"[Keys] EIK loaded ({self.eik[:4].hex()}...)")
            print(f"[Keys] Ring key:     {self.ring_key.hex()}")
            print(f"[Keys] Recovery key: {self.recovery_key.hex()}")
            print(f"[Keys] UTP key:      {self.utp_key.hex()}")
        else:
            print("[Keys] WARNING: No EIK provided — ring authentication will fail!")

        if account_key_hex:
            self.account_key = bytes.fromhex(account_key_hex)
            assert len(self.account_key) == 16, "Account key must be 16 bytes"
            print(f"[Keys] Account key loaded ({self.account_key[:4].hex()}...)")
        else:
            print("[Keys] WARNING: No account key — provisioning operations (remove device) will fail!")

        if eid_hex:
            self.eid = bytes.fromhex(eid_hex)
            assert len(self.eid) == 20, "EID must be 20 bytes"
            print(f"[Keys] Static EID loaded ({self.eid[:4].hex()}...)")

        if pair_date:
            print(f"[Keys] Pair date: {pair_date} — EID rotation enabled")
        else:
            print("[Keys] No pair date — EID rotation disabled (static EID or offset=0)")

    def get_current_eid(self) -> bytes:
        """Return the EID for the current 1024-second window.
        If pair_date is set, compute dynamically from EIK + current time offset.
        Otherwise fall back to the static EID or offset-0 EID."""
        if self.eik is None:
            return self.eid or (b'\x00' * 20)

        if self.pair_date is not None:
            current_time = int(time_mod.time())
            offset = max(0, current_time - self.pair_date)
            aligned_offset = (offset // ROTATION_PERIOD) * ROTATION_PERIOD
        else:
            aligned_offset = 0  # static: same as original behaviour

        return _generate_eid_from_eik(self.eik, aligned_offset)

    def get_clock_value(self) -> int:
        """Return clock value as seconds since pair_date (matching fmd_tracker.sh).
        Falls back to current Unix time if pair_date is not set.
        If the system clock is behind pair_date (e.g. after offline reboot), clamps to 0."""
        current_time = int(time_mod.time())
        if self.pair_date is not None:
            offset = current_time - self.pair_date
            if offset < 0:
                print(f"[Keys] WARNING: system clock is behind pair_date by {-offset}s "
                      "(offline reboot?). Clamping clock offset to 0.")
                return 0
            return offset
        return current_time


# ---------------------------------------------------------------------------
# BlueZ D-Bus GATT scaffolding
# ---------------------------------------------------------------------------

class Application(dbus.service.Object):
    def __init__(self, bus, keys):
        self.path = '/'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)
        self.add_service(FastPairService(bus, 0, keys))
        self.add_service(DeviceInfoService(bus, 1))

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.get_characteristics():
                response[chrc.get_path()] = chrc.get_properties()
                for desc in chrc.get_descriptors():
                    response[desc.get_path()] = desc.get_properties()
        return response


class Service(dbus.service.Object):
    PATH_BASE = '/org/bluez/example/service'

    def __init__(self, bus, index, uuid, primary):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            'org.bluez.GattService1': {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array(
                    [c.get_path() for c in self.characteristics],
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristics(self):
        return self.characteristics


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.descriptors = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            'org.bluez.GattCharacteristic1': {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
                'Descriptors': dbus.Array(
                    [d.get_path() for d in self.descriptors],
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.signal('org.freedesktop.DBus.Properties',
                         signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class Descriptor(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, characteristic):
        self.path = characteristic.path + '/desc' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.chrc = characteristic
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            'org.bluez.GattDescriptor1': {
                'Characteristic': self.chrc.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)


# ---------------------------------------------------------------------------
# Fast Pair service and characteristics
# ---------------------------------------------------------------------------

class FastPairService(Service):
    def __init__(self, bus, index, keys):
        Service.__init__(self, bus, index, FAST_PAIR_SVC_UUID, True)
        self.add_characteristic(ModelIdCharacteristic(bus, 0, self))
        self.add_characteristic(KeyBasedPairingCharacteristic(bus, 1, self))
        self.add_characteristic(PasskeyCharacteristic(bus, 2, self))
        self.add_characteristic(AccountKeyCharacteristic(bus, 3, self))
        self.add_characteristic(BeaconActionsCharacteristic(bus, 4, self, keys))


class ModelIdCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, MODEL_ID_CHR_UUID,
                                ['read'], service)

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        print("[ModelID] Read")
        return [dbus.Byte(0x00), dbus.Byte(0x00), dbus.Byte(0x00)]


class KeyBasedPairingCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, KEY_BASED_PAIRING_CHR_UUID,
                                ['write', 'notify'], service)
        self.notifying = False

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='aya{sv}', out_signature='')
    def WriteValue(self, value, options):
        print(f"[KeyPairing] Write: {bytes(value).hex()}")
        if self.notifying:
            self.PropertiesChanged('org.bluez.GattCharacteristic1',
                                   {'Value': dbus.ByteArray([0x01])}, [])

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StopNotify(self):
        self.notifying = False


class PasskeyCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, PASSKEY_CHR_UUID,
                                ['write', 'notify'], service)
        self.notifying = False

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='aya{sv}', out_signature='')
    def WriteValue(self, value, options):
        print(f"[Passkey] Write: {bytes(value).hex()}")
        if self.notifying:
            self.PropertiesChanged('org.bluez.GattCharacteristic1',
                                   {'Value': dbus.ByteArray([0x02])}, [])

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StopNotify(self):
        self.notifying = False


class AccountKeyCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, ACCOUNT_KEY_CHR_UUID,
                                ['write'], service)

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='aya{sv}', out_signature='')
    def WriteValue(self, value, options):
        print(f"[AccountKey] Write: {bytes(value).hex()}")


# ---------------------------------------------------------------------------
# Beacon Actions — the core FHN characteristic
# ---------------------------------------------------------------------------

class BeaconActionsCharacteristic(Characteristic):
    def __init__(self, bus, index, service, keys):
        Characteristic.__init__(self, bus, index, BEACON_ACTIONS_CHR_UUID,
                                ['read', 'write', 'notify'], service)
        self.notifying = False
        self.last_nonce = None
        self.keys = keys

        self.ringing = False
        self.ringing_components = 0x00
        self.ringing_timeout = 0

    # ---- Read: returns protocol version + fresh nonce ----

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        nonce = secrets.token_bytes(8)
        self.last_nonce = nonce
        print(f"[BeaconActions] Read → nonce={nonce.hex()}")
        return dbus.ByteArray(bytes([PROTOCOL_MAJOR_VERSION]) + nonce)

    # ---- Write: dispatch by Data ID ----

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='aya{sv}', out_signature='')
    def WriteValue(self, value, options):
        data = bytes(value)
        if len(data) < 2:
            print("[BeaconActions] Write too short, ignoring")
            return

        data_id = data[0]
        data_len = data[1]
        req_auth = data[2:10] if len(data) >= 10 else b'\x00' * 8
        req_additional = data[10:] if len(data) > 10 else b''

        nonce = self.last_nonce
        self.last_nonce = None  # invalidate per spec

        print(f"[BeaconActions] Write data_id=0x{data_id:02x} len={data_len} "
              f"auth={req_auth.hex()} additional={req_additional.hex()}")

        if not self.notifying:
            print("[BeaconActions] WARNING: notifications not enabled!")
            return

        if nonce is None:
            print("[BeaconActions] WARNING: no valid nonce (stale or double-write)")
            return

        handlers = {
            0x00: self._handle_read_beacon_params,
            0x01: self._handle_read_provisioning_state,
            0x02: self._handle_set_eik,
            0x03: self._handle_clear_eik,
            0x04: self._handle_read_eik,
            0x05: self._handle_ring,
            0x06: self._handle_read_ringing_state,
            0x07: self._handle_activate_utp,
            0x08: self._handle_deactivate_utp,
        }

        handler = handlers.get(data_id)
        if handler:
            handler(nonce, req_auth, req_additional)
        else:
            print(f"[BeaconActions] Unknown data_id 0x{data_id:02x}")

    # ---- Notification helpers ----

    def _send_notification(self, response, delay_ms=0):
        """Send a BLE notification via PropertiesChanged."""
        def _do_send():
            print(f"[BeaconActions] >>> Notify: {response.hex()}")
            self.PropertiesChanged('org.bluez.GattCharacteristic1',
                                   {'Value': dbus.ByteArray(response)}, [])
            return False

        if delay_ms > 0:
            GLib.timeout_add(delay_ms, _do_send)
        else:
            _do_send()

    def _build_response(self, data_id, auth, additional_data=b''):
        data_length = len(auth) + len(additional_data)
        return bytes([data_id, data_length]) + auth + additional_data

    def _response_hmac(self, key, nonce, data_id, additional_data=b''):
        """Compute the response authentication HMAC per spec Table 6."""
        data_length = 8 + len(additional_data)
        message = (
            bytes([PROTOCOL_MAJOR_VERSION]) +
            nonce +
            bytes([data_id, data_length]) +
            additional_data +
            b'\x01'
        )
        return compute_hmac(key, message)

    # ---- 0x00: Read beacon parameters ----

    def _handle_read_beacon_params(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x00 Read Beacon Parameters")
        if not self.keys.account_key:
            print("  No account key — sending dummy response")
            auth = bytes(8)
            additional = bytes(16)
        else:
            # Beacon parameters: 8 bytes data + 8 bytes zero padding, AES-ECB encrypted
            clock_val = self.keys.get_clock_value()
            params = bytearray(16)
            params[0] = 0x00        # calibrated power (0 dBm)
            params[1:5] = clock_val.to_bytes(4, 'big')
            params[5] = 0x00        # SECP160R1
            params[6] = 0x01        # 1 ringing component
            params[7] = 0x00        # no volume selection
            cipher = AES.new(self.keys.account_key, AES.MODE_ECB)
            additional = cipher.encrypt(bytes(params))
            auth = self._response_hmac(self.keys.account_key, nonce, 0x00, additional)

        resp = self._build_response(0x00, auth, additional)
        self._send_notification(resp)

    # ---- 0x01: Read provisioning state ----

    def _handle_read_provisioning_state(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x01 Read Provisioning State")
        if not self.keys.account_key:
            print("  ERROR: No account key — cannot authenticate provisioning state!")
            print("  Restart with --account-key to enable device removal.")
            return

        # Validate incoming HMAC
        expected = compute_hmac(self.keys.account_key,
                                bytes([PROTOCOL_MAJOR_VERSION]) + nonce +
                                bytes([0x01, 0x08]))
        if req_auth == expected:
            print("  Incoming HMAC verified OK")
            owner_match = True
        else:
            print(f"  Incoming HMAC mismatch (not the owner account key)")
            owner_match = False

        # Bit 1 (0x01): EIK is set. Bit 2 (0x02): owner account key matched.
        state = 0x00
        if self.keys.eik:
            state |= 0x01
        if owner_match:
            state |= 0x02

        eid = self.keys.get_current_eid()
        additional = bytes([state]) + eid
        auth = self._response_hmac(self.keys.account_key, nonce, 0x01, additional)
        resp = self._build_response(0x01, auth, additional)
        self._send_notification(resp)

    # ---- 0x02: Set EIK ----

    def _handle_set_eik(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x02 Set EIK")
        key = self.keys.account_key or bytes(16)
        auth = self._response_hmac(key, nonce, 0x02)
        resp = self._build_response(0x02, auth)
        self._send_notification(resp)

    # ---- 0x03: Clear EIK ----

    def _handle_clear_eik(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x03 Clear EIK")
        if not self.keys.account_key:
            print("  ERROR: No account key — cannot authenticate clear EIK!")
            return

        # Verify the hashed EIK from the request: SHA256(EIK || nonce)[:8]
        if self.keys.eik and nonce and len(req_additional) >= 8:
            expected_eik_hash = hashlib.sha256(self.keys.eik + nonce).digest()[:8]
            if req_additional[:8] == expected_eik_hash:
                print("  EIK hash verified OK — clearing EIK")
            else:
                print(f"  EIK hash mismatch")

        auth = self._response_hmac(self.keys.account_key, nonce, 0x03)
        resp = self._build_response(0x03, auth)
        self._send_notification(resp)

    # ---- 0x04: Read EIK with user consent ----

    def _handle_read_eik(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x04 Read EIK (recovery)")
        if not self.keys.eik or not self.keys.account_key or not self.keys.recovery_key:
            print("  Missing keys for EIK recovery")
            auth = bytes(8)
            additional = bytes(32)
        else:
            cipher = AES.new(self.keys.account_key, AES.MODE_ECB)
            additional = cipher.encrypt(self.keys.eik[:16]) + cipher.encrypt(self.keys.eik[16:])
            auth = self._response_hmac(self.keys.recovery_key, nonce, 0x04, additional)
        resp = self._build_response(0x04, auth, additional)
        self._send_notification(resp)

    # ---- 0x05: Ring ----

    def _handle_ring(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x05 RING")

        # Parse the ring request additional data
        ring_op = req_additional[0] if len(req_additional) > 0 else 0xFF
        timeout_hi = req_additional[1] if len(req_additional) > 1 else 0x00
        timeout_lo = req_additional[2] if len(req_additional) > 2 else 0x3C
        volume = req_additional[3] if len(req_additional) > 3 else 0x00
        timeout_val = (timeout_hi << 8) | timeout_lo

        if ring_op == 0x00:
            print(f"  Stop ringing")
            self.ringing = False
            self.ringing_components = 0x00
            self.ringing_timeout = 0
            ringing_state = 0x04  # Stopped (GATT request)
        else:
            print(f"  Ring components=0x{ring_op:02x} timeout={timeout_val} deciseconds volume=0x{volume:02x}")
            self.ringing = True
            self.ringing_components = ring_op
            self.ringing_timeout = timeout_val
            ringing_state = 0x00  # Started

        # Validate incoming HMAC from the app (informational)
        if self.keys.ring_key:
            expected_req_hmac_msg = (
                bytes([PROTOCOL_MAJOR_VERSION]) +
                nonce +
                bytes([0x05, 8 + len(req_additional)]) +
                req_additional
            )
            expected_req_auth = compute_hmac(self.keys.ring_key, expected_req_hmac_msg)
            if req_auth == expected_req_auth:
                print(f"  Incoming HMAC verified OK")
            else:
                print(f"  Incoming HMAC MISMATCH (expected={expected_req_auth.hex()} got={req_auth.hex()})")

        # Build response: Table 6, data ID 0x05
        # Additional: [ringing_state, ringing_components, timeout_hi, timeout_lo]
        additional = bytes([ringing_state, self.ringing_components,
                            (self.ringing_timeout >> 8) & 0xFF,
                            self.ringing_timeout & 0xFF])

        if self.keys.ring_key:
            auth = self._response_hmac(self.keys.ring_key, nonce, 0x05, additional)
            print(f"  Response HMAC: {auth.hex()}")
        else:
            auth = bytes(8)
            print("  No ring key — dummy auth")

        resp = self._build_response(0x05, auth, additional)
        # Ring notification is sent after the write completes (spec exception for 0x05)
        self._send_notification(resp, delay_ms=100)

    # ---- 0x06: Read ringing state ----

    def _handle_read_ringing_state(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x06 Read Ringing State")
        additional = bytes([self.ringing_components,
                            (self.ringing_timeout >> 8) & 0xFF,
                            self.ringing_timeout & 0xFF])

        if self.keys.ring_key:
            auth = self._response_hmac(self.keys.ring_key, nonce, 0x06, additional)
        else:
            auth = bytes(8)

        resp = self._build_response(0x06, auth, additional)
        self._send_notification(resp)

    # ---- 0x07: Activate unwanted tracking protection ----

    def _handle_activate_utp(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x07 Activate UTP")
        if self.keys.utp_key:
            auth = self._response_hmac(self.keys.utp_key, nonce, 0x07)
        else:
            auth = bytes(8)
        resp = self._build_response(0x07, auth)
        self._send_notification(resp)

    # ---- 0x08: Deactivate unwanted tracking protection ----

    def _handle_deactivate_utp(self, nonce, req_auth, req_additional):
        print("[BeaconActions] 0x08 Deactivate UTP")
        if self.keys.utp_key:
            auth = self._response_hmac(self.keys.utp_key, nonce, 0x08)
        else:
            auth = bytes(8)
        resp = self._build_response(0x08, auth)
        self._send_notification(resp)

    # ---- Notify control ----

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StartNotify(self):
        print("[BeaconActions] Notifications enabled")
        self.notifying = True

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='', out_signature='')
    def StopNotify(self):
        print("[BeaconActions] Notifications disabled")
        self.notifying = False


# ---------------------------------------------------------------------------
# Device Information Service
# ---------------------------------------------------------------------------

class DeviceInfoService(Service):
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, DEVICE_INFO_SVC_UUID, True)
        self.add_characteristic(FirmwareRevisionCharacteristic(bus, 0, self))


class FirmwareRevisionCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, FIRMWARE_REVISION_CHR_UUID,
                                ['read'], service)

    @dbus.service.method('org.bluez.GattCharacteristic1',
                         in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        print("[FirmwareRev] Read")
        return [dbus.Byte(ord(c)) for c in "1.0.0"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def register_app_cb():
    print("GATT application registered successfully")


def register_app_error_cb(error):
    print("Failed to register application: " + str(error))
    mainloop.quit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='FHN GATT Server for Raspberry Pi')
    parser.add_argument('--eik', type=str, required=True,
                        help='Ephemeral Identity Key as 64-char hex string (32 bytes)')
    parser.add_argument('--account-key', type=str, default=None,
                        help='Account Key as 32-char hex string (16 bytes)')
    parser.add_argument('--eid', type=str, default=None,
                        help='Current EID (Advertisement Key) as 40-char hex string (20 bytes) — '
                             'only used as fallback when --pair-date is not set')
    parser.add_argument('--pair-date', type=int, default=None,
                        help='Unix timestamp at registration (enables EID rotation and correct clock)')
    parser.add_argument('--adapter', type=str, default='hci0',
                        help='Bluetooth adapter (default: hci0)')
    args = parser.parse_args()

    keys = FHNKeys(eik_hex=args.eik, account_key_hex=args.account_key,
                   eid_hex=args.eid, pair_date=args.pair_date)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    try:
        adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME,
                                     '/org/bluez/' + args.adapter)
        adapter_props = dbus.Interface(adapter_obj,
                                       "org.freedesktop.DBus.Properties")
        adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))
    except Exception as e:
        print(f"Error accessing Bluetooth adapter {args.adapter}: {e}")
        sys.exit(1)

    service_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, '/org/bluez/' + args.adapter),
        GATT_MANAGER_IFACE)

    app = Application(bus, keys)

    print("Registering FHN GATT service...")
    mainloop = GLib.MainLoop()

    service_manager.RegisterApplication(app.get_path(), {},
                                        reply_handler=register_app_cb,
                                        error_handler=register_app_error_cb)

    print("GATT Server running. Press Ctrl+C to exit.")
    try:
        mainloop.run()
    except KeyboardInterrupt:
        print("\nExiting...")
