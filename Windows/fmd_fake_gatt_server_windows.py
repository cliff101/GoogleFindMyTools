import sys
import os
import argparse
import hashlib
import hmac as hmac_mod
import time as time_mod
import secrets
import asyncio
import uuid
import psutil
from Cryptodome.Cipher import AES
from ecdsa import SECP160r1

from tools import ring_service

import random

import winrt.windows.foundation  # noqa: F401 — IAsyncOperation; required for async Bluetooth APIs
import winrt.windows.foundation.collections  # noqa: F401 — e.g. subscribed_clients on GATT characteristics
import winrt.windows.devices.bluetooth as bt
import winrt.windows.devices.bluetooth.genericattributeprofile as gatt
from winrt.windows.storage.streams import DataWriter, DataReader

import logging

logger = logging.getLogger(__name__)


def setup_logging():
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fmd_fake_gatt_server_windows.log")
    lg = logging.getLogger(__name__)
    lg.setLevel(logging.DEBUG)
    if lg.handlers:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    lg.propagate = False


PROTOCOL_MAJOR_VERSION = 0x01
K = 10
ROTATION_PERIOD = 1024  # 2^K seconds
ROTATE_BASE = 1024
ROTATE_JITTER_MIN = 1
ROTATE_JITTER_MAX = 204
EID_CHECK_INTERVAL = 30
WATCHDOG_RESTART_DELAY_SEC = 5

FAST_PAIR_SVC_UUID = uuid.UUID("0000FE2C-0000-1000-8000-00805F9B34FB")
MODEL_ID_CHR_UUID = uuid.UUID("FE2C1233-8366-4814-8EB0-01DE32100BEA")
KEY_BASED_PAIRING_CHR_UUID = uuid.UUID("FE2C1234-8366-4814-8EB0-01DE32100BEA")
PASSKEY_CHR_UUID = uuid.UUID("FE2C1235-8366-4814-8EB0-01DE32100BEA")
ACCOUNT_KEY_CHR_UUID = uuid.UUID("FE2C1236-8366-4814-8EB0-01DE32100BEA")
BEACON_ACTIONS_CHR_UUID = uuid.UUID("FE2C1238-8366-4814-8EB0-01DE32100BEA")
DEVICE_INFO_SVC_UUID = uuid.UUID("0000180A-0000-1000-8000-00805F9B34FB")
FIRMWARE_REVISION_CHR_UUID = uuid.UUID("00002A26-0000-1000-8000-00805F9B34FB")
EDDYSTONE_SVC_UUID = uuid.UUID("0000FEAA-0000-1000-8000-00805F9B34FB")

def _read_buffer(ibuffer):
    reader = DataReader.from_buffer(ibuffer)
    buf = bytearray(ibuffer.length)
    reader.read_bytes(buf)
    return bytes(buf)

def truncated_sha256(data):
    return hashlib.sha256(data).digest()[:8]

def compute_hmac(key, message):
    return hmac_mod.new(key, message, hashlib.sha256).digest()[:8]

def get_battery_level_bits() -> int:
    try:
        batt = psutil.sensors_battery()
        if batt is None:
            return 0x00  # Normal (if no battery sensor, report unsupported)
        if batt.power_plugged:
            return 0x01  # Normal
        pct = batt.percent
        if pct >= 20:
            return 0x01  # Normal
        elif pct >= 5:
            return 0x02  # Low
        else:
            return 0x03  # Critically low
    except Exception:
        return 0x01  # Default to normal

def _generate_eid_from_eik(identity_key: bytes, time_offset: int) -> tuple[bytes, int]:
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
    return R.x().to_bytes(20, 'big'), r

class FHNKeys:
    def __init__(self, eik_hex=None, account_key_hex=None, pair_date=None):
        self.eik = None
        self.ring_key = None
        self.recovery_key = None
        self.utp_key = None
        self.account_key = None
        self.pair_date = pair_date

        if eik_hex:
            self.eik = bytes.fromhex(eik_hex)
            assert len(self.eik) == 32, "EIK must be 32 bytes"
            self.recovery_key = truncated_sha256(self.eik + b'\x01')
            self.ring_key = truncated_sha256(self.eik + b'\x02')
            self.utp_key = truncated_sha256(self.eik + b'\x03')
            logger.log(logging.INFO, f"[Keys] EIK loaded ({self.eik[:4].hex()}...)")
            logger.log(logging.INFO, f"[Keys] Ring key:     {self.ring_key.hex()}")
            logger.log(logging.INFO, f"[Keys] Recovery key: {self.recovery_key.hex()}")
            logger.log(logging.INFO, f"[Keys] UTP key:      {self.utp_key.hex()}")
        else:
            logger.log(logging.WARNING, "[Keys] WARNING: No EIK provided - ring authentication will fail!")
        
        if account_key_hex:
            self.account_key = bytes.fromhex(account_key_hex)
            assert len(self.account_key) == 16, "Account key must be 16 bytes"
            logger.log(logging.INFO, f"[Keys] Account key loaded ({self.account_key[:4].hex()}...)")
        else:
            logger.log(logging.WARNING, "[Keys] WARNING: No account key - provisioning operations will fail!")

        if pair_date:
            logger.log(logging.INFO, f"[Keys] Pair date: {pair_date} - EID rotation enabled")
        else:
            logger.log(logging.INFO, "[Keys] No pair date - EID rotation disabled")

    def get_current_eid(self) -> tuple[bytes, int]:
        if self.eik is None:
            return b'\x00' * 20, 0

        if self.pair_date is not None:
            current_time = int(time_mod.time())
            offset = max(0, current_time - self.pair_date)
            aligned_offset = (offset // ROTATION_PERIOD) * ROTATION_PERIOD
        else:
            aligned_offset = 0

        return _generate_eid_from_eik(self.eik, aligned_offset)

    def get_clock_value(self) -> int:
        current_time = int(time_mod.time())
        if self.pair_date is not None:
            offset = current_time - self.pair_date
            if offset < 0:
                logger.log(logging.WARNING, f"[Keys] WARNING: system clock is behind pair_date by {-offset}s. Clamping clock offset to 0.")
                return 0
            return offset
        return current_time

class FHNGattServer:
    def __init__(self, keys):
        self.keys = keys
        self.service_provider = None
        self.device_info_provider = None
        self.last_nonce = None
        self.ringing = False
        self.ringing_components = 0x00
        self.ringing_timeout = 0
        self.beacon_actions_char = None
        self.kb_pairing_char = None
        self.passkey_char = None
        self.eid_provider = None
        self.utp_mode = 0

    async def start(self):
        self._loop = asyncio.get_running_loop()

        logger.log(logging.INFO, "[GATT] Requesting default Bluetooth adapter...")
        try:
            adapter = await bt.BluetoothAdapter.get_default_async()
        except Exception as e:
            logger.log(logging.ERROR, f"[GATT] BluetoothAdapter.get_default_async failed: {e!r}", exc_info=True)
            return False
        if adapter is None:
            logger.log(logging.ERROR, "[GATT] ERROR: No Bluetooth adapter found!")
            return False
        logger.log(logging.INFO, f"[Adapter] Peripheral role supported: {adapter.is_peripheral_role_supported}")
        logger.log(logging.INFO, f"[Adapter] Central role supported:    {adapter.is_central_role_supported}")
        if not adapter.is_peripheral_role_supported:
            logger.log(logging.ERROR, "[GATT] ERROR: Bluetooth adapter does not support peripheral (GATT server) role!")
            logger.log(logging.ERROR, "[GATT] You need an adapter that supports BLE peripheral mode (e.g. a compatible USB dongle).")
            return False

        logger.log(logging.INFO, f"[GATT] Creating Fast Pair Service ({FAST_PAIR_SVC_UUID})...")
        provider_result = await gatt.GattServiceProvider.create_async(FAST_PAIR_SVC_UUID)
        if provider_result.error != bt.BluetoothError.SUCCESS:
            logger.log(logging.ERROR, f"[GATT] Failed to create service provider: {provider_result.error}")
            return False
        self.service_provider = provider_result.service_provider

        def on_fp_status_changed(sender, args):
            logger.log(logging.INFO, f"[GATT] Fast Pair advertisement status -> {sender.advertisement_status.name}")
        self.service_provider.add_advertisement_status_changed(on_fp_status_changed)

        await self._add_model_id_char(self.service_provider.service)
        self.kb_pairing_char = await self._add_key_based_pairing_char(self.service_provider.service)
        self.passkey_char = await self._add_passkey_char(self.service_provider.service)
        await self._add_account_key_char(self.service_provider.service)
        self.beacon_actions_char = await self._add_beacon_actions_char(self.service_provider.service)

        adv_params = gatt.GattServiceProviderAdvertisingParameters()
        adv_params.is_connectable = True
        adv_params.is_discoverable = True

        try:
            self.service_provider.start_advertising_with_parameters(adv_params)
            await asyncio.sleep(0.5)
            status = self.service_provider.advertisement_status
            logger.log(logging.INFO, f"[GATT] Fast Pair Service advertising status: {status.name}")
            if status.name == "ABORTED":
                logger.log(logging.WARNING, "[GATT] WARNING: Advertising aborted! The adapter may not fully support GATT server.")
        except Exception as e:
            logger.log(logging.ERROR, f"[GATT] Failed to start advertising: {e}")
            return False

        di_provider_result = await gatt.GattServiceProvider.create_async(DEVICE_INFO_SVC_UUID)
        if di_provider_result.error == bt.BluetoothError.SUCCESS:
            self.device_info_provider = di_provider_result.service_provider
            await self._add_firmware_revision_char(self.device_info_provider.service)
            try:
                di_adv_params = gatt.GattServiceProviderAdvertisingParameters()
                di_adv_params.is_connectable = True
                di_adv_params.is_discoverable = True
                self.device_info_provider.start_advertising_with_parameters(di_adv_params)
                logger.log(logging.INFO, f"[GATT] Device Info Service advertising status: {self.device_info_provider.advertisement_status.name}")
            except Exception as e:
                logger.log(logging.ERROR, f"[GATT] Failed to start Device Info advertising: {e}")

        return True

    async def start_eid_advertising(self):
        eid_bytes, r = self.keys.get_current_eid()

        # Compute hashed flags byte
        # Bits 0-4: Reserved (0)
        # Bits 5-6: Battery level
        # Bit 7: UTP (0)
        battery_level = get_battery_level_bits()
        flags = (battery_level << 1) | self.utp_mode
        
        r_bytes = r.to_bytes(20, 'big')
        sha_r = hashlib.sha256(r_bytes).digest()
        hashed_flags = flags ^ sha_r[-1]

        if self.eid_provider is None:
            result = await gatt.GattServiceProvider.create_async(EDDYSTONE_SVC_UUID)
            if result.error != bt.BluetoothError.SUCCESS:
                logger.log(logging.ERROR, f"[EID] Failed to create Eddystone service provider: {result.error}")
                return
            self.eid_provider = result.service_provider
        else:
            self.eid_provider.stop_advertising()

        adv_params = gatt.GattServiceProviderAdvertisingParameters()
        adv_params.is_connectable = True
        adv_params.is_discoverable = True

        writer = DataWriter()
        # Frame type 0x40 (160-bit curve, no UTP) or 0x41 (with UTP) + 20-byte EID + 1-byte hashed flags
        frame_type = 0x41 if self.utp_mode else 0x40
        writer.write_bytes(bytes([frame_type]) + eid_bytes + bytes([hashed_flags]))
        adv_params.service_data = writer.detach_buffer()

        self.eid_provider.start_advertising_with_parameters(adv_params)
        await asyncio.sleep(0.3)
        logger.log(logging.INFO, f"[EID] Connectable EID: {eid_bytes.hex().upper()} (status: {self.eid_provider.advertisement_status.name})")

    def stop(self):
        if self.eid_provider:
            self.eid_provider.stop_advertising()
        if self.service_provider:
            self.service_provider.stop_advertising()
        if self.device_info_provider:
            self.device_info_provider.stop_advertising()

    async def _add_model_id_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = gatt.GattCharacteristicProperties.READ
        result = await service.create_characteristic_async(MODEL_ID_CHR_UUID, params)
        char = result.characteristic

        def on_read(sender, args):
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    writer = DataWriter()
                    writer.write_bytes(bytes([0x00, 0x00, 0x00]))
                    request.respond_with_value(writer.detach_buffer())
                    logger.log(logging.INFO, "[ModelID] Read")
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling ModelID read: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)
        char.add_read_requested(on_read)
        return char

    async def _add_key_based_pairing_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = (gatt.GattCharacteristicProperties.WRITE | gatt.GattCharacteristicProperties.NOTIFY)
        result = await service.create_characteristic_async(KEY_BASED_PAIRING_CHR_UUID, params)
        char = result.characteristic

        def on_write(sender, args):
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    data = _read_buffer(request.value)
                    logger.log(logging.INFO, f"[KeyPairing] Write: {data.hex()}")
                    request.respond()
                    
                    if len(char.subscribed_clients) > 0:
                        writer = DataWriter()
                        writer.write_bytes(bytes([0x01]))
                        await char.notify_value_async(writer.detach_buffer())
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling KeyPairing write: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)
        char.add_write_requested(on_write)
        return char

    async def _add_passkey_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = (gatt.GattCharacteristicProperties.WRITE | gatt.GattCharacteristicProperties.NOTIFY)
        result = await service.create_characteristic_async(PASSKEY_CHR_UUID, params)
        char = result.characteristic

        def on_write(sender, args):
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    data = _read_buffer(request.value)
                    logger.log(logging.INFO, f"[Passkey] Write: {data.hex()}")
                    request.respond()
                    
                    if len(char.subscribed_clients) > 0:
                        writer = DataWriter()
                        writer.write_bytes(bytes([0x02]))
                        await char.notify_value_async(writer.detach_buffer())
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling Passkey write: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)
        char.add_write_requested(on_write)
        return char

    async def _add_account_key_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = gatt.GattCharacteristicProperties.WRITE
        result = await service.create_characteristic_async(ACCOUNT_KEY_CHR_UUID, params)
        char = result.characteristic

        def on_write(sender, args):
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    data = _read_buffer(request.value)
                    logger.log(logging.INFO, f"[AccountKey] Write: {data.hex()}")
                    request.respond()
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling AccountKey write: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)
        char.add_write_requested(on_write)
        return char

    async def _add_beacon_actions_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = (gatt.GattCharacteristicProperties.READ | 
                                            gatt.GattCharacteristicProperties.WRITE | 
                                            gatt.GattCharacteristicProperties.NOTIFY)
        result = await service.create_characteristic_async(BEACON_ACTIONS_CHR_UUID, params)
        char = result.characteristic

        def on_read(sender, args):
            logger.log(logging.INFO, "[BeaconActions] Read request received!")
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    nonce = secrets.token_bytes(8)
                    self.last_nonce = nonce
                    logger.log(logging.INFO, f"[BeaconActions] Read -> nonce={nonce.hex()}")
                    writer = DataWriter()
                    writer.write_bytes(bytes([PROTOCOL_MAJOR_VERSION]) + nonce)
                    request.respond_with_value(writer.detach_buffer())
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling BeaconActions read: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)

        def on_write(sender, args):
            logger.log(logging.INFO, "[BeaconActions] Write request received!")
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    data = _read_buffer(request.value)
                    request.respond()
                    
                    if len(data) < 2:
                        logger.log(logging.INFO, "[BeaconActions] Write too short, ignoring")
                        return

                    data_id = data[0]
                    data_len = data[1]
                    req_auth = data[2:10] if len(data) >= 10 else b'\x00' * 8
                    req_additional = data[10:] if len(data) > 10 else b''

                    nonce = self.last_nonce
                    self.last_nonce = None

                    logger.log(logging.INFO, f"[BeaconActions] Write data_id=0x{data_id:02x} len={data_len} "
                          f"auth={req_auth.hex()} additional={req_additional.hex()}")

                    if len(char.subscribed_clients) == 0:
                        logger.log(logging.WARNING, "[BeaconActions] WARNING: notifications not enabled!")
                        return

                    if nonce is None:
                        logger.log(logging.WARNING, "[BeaconActions] WARNING: no valid nonce (stale or double-write)")
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
                        await handler(nonce, req_auth, req_additional)
                    else:
                        logger.log(logging.INFO, f"[BeaconActions] Unknown data_id 0x{data_id:02x}")

                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling BeaconActions write: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)

        char.add_read_requested(on_read)
        char.add_write_requested(on_write)
        
        def on_clients_changed(sender, args):
            if len(char.subscribed_clients) > 0:
                logger.log(logging.INFO, f"[BeaconActions] Notifications enabled by client (count: {len(char.subscribed_clients)})")
            else:
                logger.log(logging.INFO, "[BeaconActions] Notifications disabled by client")
                
        char.add_subscribed_clients_changed(on_clients_changed)
        return char
    
    async def _add_firmware_revision_char(self, service):
        params = gatt.GattLocalCharacteristicParameters()
        params.characteristic_properties = gatt.GattCharacteristicProperties.READ
        result = await service.create_characteristic_async(FIRMWARE_REVISION_CHR_UUID, params)
        char = result.characteristic

        def on_read(sender, args):
            deferral = args.get_deferral()
            async def handle():
                try:
                    request = await args.get_request_async()
                    writer = DataWriter()
                    writer.write_bytes("1.0.0".encode('utf-8'))
                    request.respond_with_value(writer.detach_buffer())
                    logger.log(logging.INFO, "[FirmwareRev] Read")
                except Exception as e:
                    logger.log(logging.ERROR, f"Error handling FirmwareRev read: {e}")
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(handle(), self._loop)
        char.add_read_requested(on_read)
        return char

    async def _send_notification(self, response, delay_ms=0):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        logger.log(logging.INFO, f"[BeaconActions] >>> Notify: {response.hex()}")
        if self.beacon_actions_char and len(self.beacon_actions_char.subscribed_clients) > 0:
            writer = DataWriter()
            writer.write_bytes(response)
            await self.beacon_actions_char.notify_value_async(writer.detach_buffer())

    def _build_response(self, data_id, auth, additional_data=b''):
        data_length = len(auth) + len(additional_data)
        return bytes([data_id, data_length]) + auth + additional_data

    def _response_hmac(self, key, nonce, data_id, additional_data=b''):
        data_length = 8 + len(additional_data)
        message = (
            bytes([PROTOCOL_MAJOR_VERSION]) +
            nonce +
            bytes([data_id, data_length]) +
            additional_data +
            b'\x01'
        )
        return compute_hmac(key, message)

    async def _handle_read_beacon_params(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x00 Read Beacon Parameters")
        if not self.keys.account_key:
            logger.log(logging.INFO, "  No account key - sending dummy response")
            auth = bytes(8)
            additional = bytes(16)
        else:
            clock_val = self.keys.get_clock_value()
            params = bytearray(16)
            params[0] = 0x00
            params[1:5] = clock_val.to_bytes(4, 'big')
            params[5] = 0x00
            params[6] = 0x01
            params[7] = 0x00
            cipher = AES.new(self.keys.account_key, AES.MODE_ECB)
            additional = cipher.encrypt(bytes(params))
            auth = self._response_hmac(self.keys.account_key, nonce, 0x00, additional)

        resp = self._build_response(0x00, auth, additional)
        await self._send_notification(resp)

    async def _handle_read_provisioning_state(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x01 Read Provisioning State")
        if not self.keys.account_key:
            logger.log(logging.ERROR, "  ERROR: No account key - cannot authenticate provisioning state!")
            return

        expected = compute_hmac(self.keys.account_key,
                                bytes([PROTOCOL_MAJOR_VERSION]) + nonce +
                                bytes([0x01, 0x08]))
        if req_auth == expected:
            logger.log(logging.INFO, "  Incoming HMAC verified OK")
            owner_match = True
        else:
            logger.log(logging.INFO, "  Incoming HMAC mismatch (not the owner account key)")
            owner_match = False

        state = 0x00
        if self.keys.eik:
            state |= 0x01
        if owner_match:
            state |= 0x02

        eid, _ = self.keys.get_current_eid()
        additional = bytes([state]) + eid
        auth = self._response_hmac(self.keys.account_key, nonce, 0x01, additional)
        resp = self._build_response(0x01, auth, additional)
        await self._send_notification(resp)

    async def _handle_set_eik(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x02 Set EIK")
        key = self.keys.account_key or bytes(16)
        auth = self._response_hmac(key, nonce, 0x02)
        resp = self._build_response(0x02, auth)
        await self._send_notification(resp)

    async def _handle_clear_eik(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x03 Clear EIK")
        if not self.keys.account_key:
            logger.log(logging.ERROR, "  ERROR: No account key - cannot authenticate clear EIK!")
            return

        if self.keys.eik and nonce and len(req_additional) >= 8:
            expected_eik_hash = hashlib.sha256(self.keys.eik + nonce).digest()[:8]
            if req_additional[:8] == expected_eik_hash:
                logger.log(logging.INFO, "  EIK hash verified OK - clearing EIK")
            else:
                logger.log(logging.INFO, "  EIK hash mismatch")

        auth = self._response_hmac(self.keys.account_key, nonce, 0x03)
        resp = self._build_response(0x03, auth)
        await self._send_notification(resp)

    async def _handle_read_eik(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x04 Read EIK (recovery)")
        if not self.keys.eik or not self.keys.account_key or not self.keys.recovery_key:
            logger.log(logging.INFO, "  Missing keys for EIK recovery")
            auth = bytes(8)
            additional = bytes(32)
        else:
            cipher = AES.new(self.keys.account_key, AES.MODE_ECB)
            additional = cipher.encrypt(self.keys.eik[:16]) + cipher.encrypt(self.keys.eik[16:])
            auth = self._response_hmac(self.keys.recovery_key, nonce, 0x04, additional)
        resp = self._build_response(0x04, auth, additional)
        await self._send_notification(resp)

    async def _handle_ring(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x05 RING")
        ring_op = req_additional[0] if len(req_additional) > 0 else 0xFF
        timeout_hi = req_additional[1] if len(req_additional) > 1 else 0x00
        timeout_lo = req_additional[2] if len(req_additional) > 2 else 0x3C
        volume = req_additional[3] if len(req_additional) > 3 else 0x00
        timeout_val = (timeout_hi << 8) | timeout_lo

        if ring_op == 0x00:
            logger.log(logging.INFO, "  Stop ringing")
            self.ringing = False
            self.ringing_components = 0x00
            self.ringing_timeout = 0
            ringing_state = 0x04
            ring_service.stop_beep()
        else:
            logger.log(logging.INFO, f"  Ring components=0x{ring_op:02x} timeout={timeout_val} deciseconds volume=0x{volume:02x}")
            self.ringing = True
            self.ringing_components = ring_op
            self.ringing_timeout = timeout_val
            ringing_state = 0x00
            ring_service.start_beep()

        if self.keys.ring_key:
            expected_req_hmac_msg = (
                bytes([PROTOCOL_MAJOR_VERSION]) +
                nonce +
                bytes([0x05, 8 + len(req_additional)]) +
                req_additional
            )
            expected_req_auth = compute_hmac(self.keys.ring_key, expected_req_hmac_msg)
            if req_auth == expected_req_auth:
                logger.log(logging.INFO, "  Incoming HMAC verified OK")
            else:
                logger.log(logging.WARNING, f"  Incoming HMAC MISMATCH (expected={expected_req_auth.hex()} got={req_auth.hex()})")

        additional = bytes([ringing_state, self.ringing_components,
                            (self.ringing_timeout >> 8) & 0xFF,
                            self.ringing_timeout & 0xFF])

        if self.keys.ring_key:
            auth = self._response_hmac(self.keys.ring_key, nonce, 0x05, additional)
            logger.log(logging.INFO, f"  Response HMAC: {auth.hex()}")
        else:
            auth = bytes(8)
            logger.log(logging.INFO, "  No ring key - dummy auth")

        resp = self._build_response(0x05, auth, additional)
        await self._send_notification(resp, delay_ms=100)

    async def _handle_read_ringing_state(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x06 Read Ringing State")
        additional = bytes([self.ringing_components,
                            (self.ringing_timeout >> 8) & 0xFF,
                            self.ringing_timeout & 0xFF])

        if self.keys.ring_key:
            auth = self._response_hmac(self.keys.ring_key, nonce, 0x06, additional)
        else:
            auth = bytes(8)

        resp = self._build_response(0x06, auth, additional)
        await self._send_notification(resp)

    async def _handle_activate_utp(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x07 Activate UTP")
        self.utp_mode = 1
        if self.keys.utp_key:
            auth = self._response_hmac(self.keys.utp_key, nonce, 0x07)
        else:
            auth = bytes(8)
        resp = self._build_response(0x07, auth)
        await self._send_notification(resp)
        asyncio.create_task(self.start_eid_advertising())

    async def _handle_deactivate_utp(self, nonce, req_auth, req_additional):
        logger.log(logging.INFO, "[BeaconActions] 0x08 Deactivate UTP")
        self.utp_mode = 0
        if self.keys.utp_key:
            auth = self._response_hmac(self.keys.utp_key, nonce, 0x08)
        else:
            auth = bytes(8)
        resp = self._build_response(0x08, auth)
        await self._send_notification(resp)
        asyncio.create_task(self.start_eid_advertising())

async def main():
    setup_logging()
    parser = argparse.ArgumentParser(description='FHN GATT Server for Windows')
    parser.add_argument('--eik', type=str, default=os.environ.get("FMD_EIK", None),
                        help='Ephemeral Identity Key as 64-char hex string (32 bytes)')
    parser.add_argument('--account-key', type=str, default=os.environ.get("FMD_ACCOUNT_KEY", None),
                        help='Account Key as 32-char hex string (16 bytes)')
    parser.add_argument('--pair-date', type=int, default=os.environ.get("FMD_PAIR_DATE", None),
                        help='Unix timestamp at registration (enables EID rotation)')
    args = parser.parse_args()

    if not args.eik:
        logger.log(logging.ERROR, "Error: --eik or FMD_EIK environment variable is required.")
        sys.exit(1)

    keys = FHNKeys(eik_hex=args.eik, account_key_hex=args.account_key,
                   pair_date=args.pair_date)

    server = FHNGattServer(keys)
    if not await server.start():
        logger.log(logging.ERROR, "\nGATT server failed to start. Exiting.")
        sys.exit(1)

    await server.start_eid_advertising()

    logger.log(logging.INFO, "GATT Server + EID Tracker running. Press Ctrl+C to exit.")
    try:
        jitter = random.randint(ROTATE_JITTER_MIN, ROTATE_JITTER_MAX)
        next_rotate = ROTATE_BASE + jitter
        elapsed = 0
        while True:
            await asyncio.sleep(EID_CHECK_INTERVAL)
            elapsed += EID_CHECK_INTERVAL
            if elapsed >= next_rotate:
                logger.log(logging.INFO, "[EID] Rotating EID...")
                await server.start_eid_advertising()
                jitter = random.randint(ROTATE_JITTER_MIN, ROTATE_JITTER_MAX)
                next_rotate = ROTATE_BASE + jitter
                elapsed = 0
            elif (server.eid_provider and
                  server.eid_provider.advertisement_status.name == "ABORTED"):
                logger.log(logging.WARNING, "[EID] Advertising aborted, restarting...")
                await server.start_eid_advertising()
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        logger.log(logging.INFO, "\nExiting...")
        server.stop()

def _flush_logs():
    lg = logging.getLogger(__name__)
    for h in lg.handlers:
        try:
            h.flush()
        except Exception:
            pass


def run_with_watchdog():
    setup_logging()
    lg = logging.getLogger(__name__)
    while True:
        try:
            asyncio.run(main())
            return
        except KeyboardInterrupt:
            return
        except SystemExit as e:
            code = e.code
            if code is None or code == 0:
                return
            sys.exit(code if isinstance(code, int) else 1)
        except Exception as e:
            lg.exception(
                "Watchdog: %s: %r | restarting in %s s",
                type(e).__name__,
                e,
                WATCHDOG_RESTART_DELAY_SEC,
            )
            _flush_logs()
            time_mod.sleep(WATCHDOG_RESTART_DELAY_SEC)


if __name__ == '__main__':
    try:
        run_with_watchdog()
    finally:
        _flush_logs()