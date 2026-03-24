#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import datetime
import hashlib

from FMDNCrypto.eid_generator import ROTATION_PERIOD
from FMDNCrypto.foreign_tracker_cryptor import decrypt
from KeyBackup.cloud_key_decryptor import decrypt_eik, decrypt_aes_gcm
from NovaApi.ExecuteAction.LocateTracker.decrypted_location import WrappedLocation
from ProtoDecoders import DeviceUpdate_pb2
from ProtoDecoders import Common_pb2
from ProtoDecoders.DeviceUpdate_pb2 import DeviceRegistration
from ProtoDecoders.decoder import parse_device_update_protobuf
from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id
from SpotApi.CreateBleDevice.util import flip_bits
from SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import get_eid_info
from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key


def create_google_maps_link(latitude, longitude):
    try:  
        latitude = float(latitude)
        longitude = float(longitude)
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid latitude or longitude values.")
    except ValueError as e:
        return f"Error: {e}" #more descriptive error message for the user
    base_url = "https://www.google.com/maps/search/?api=1"
    query_params = f"query={latitude},{longitude}"  

    return f"{base_url}&{query_params}"

# Indicates if the device is a custom microcontroller
def is_mcu_tracker(device_registration: DeviceRegistration) -> bool:
    return device_registration.fastPairModelId == mcu_fast_pair_model_id


def is_static_eid_device(device_registration: DeviceRegistration) -> bool:
    """Return True if the device was registered with flip_e2ee=True (static EID, ESP32-style).

    The EIK is stored with its bits flipped in that case, so normal decryption fails and
    un-flipping is required — the same heuristic used by retrieve_identity_key.
    """
    if not is_mcu_tracker(device_registration):
        return False
    encrypted_user_secrets = device_registration.encryptedUserSecrets
    owner_key = get_owner_key()
    try:
        decrypt_eik(owner_key, encrypted_user_secrets.encryptedIdentityKey)
        return False  # decrypted without flipping → flip_e2ee=False
    except Exception:
        pass
    try:
        flipped = flip_bits(encrypted_user_secrets.encryptedIdentityKey, True)
        decrypt_eik(owner_key, flipped)
        return True   # only decrypts after un-flipping → flip_e2ee=True
    except Exception:
        return False  # unknown, assume rotating


def retrieve_identity_key(device_registration: DeviceRegistration) -> bytes:
    is_mcu = is_mcu_tracker(device_registration)
    encrypted_user_secrets = device_registration.encryptedUserSecrets
    owner_key = get_owner_key()

    # Try decrypting the key exactly as it is received
    try:
        identity_key = decrypt_eik(owner_key, encrypted_user_secrets.encryptedIdentityKey)
        return identity_key
    except Exception:
        pass

    # If it failed, and it's an MCU tracker, it might have flipped bits. Try un-flipping them.
    if is_mcu:
        try:
            flipped_encrypted_identity_key = flip_bits(encrypted_user_secrets.encryptedIdentityKey, True)
            identity_key = decrypt_eik(owner_key, flipped_encrypted_identity_key)
            return identity_key
        except Exception:
            pass

    # If all attempts fail, proceed to error handling
    try:
        # We just call it one more time to trigger the original exception to catch and handle
        decrypt_eik(owner_key, encrypted_user_secrets.encryptedIdentityKey)
    except Exception as e:

        e2eeData = get_eid_info()
        current_owner_key_version = e2eeData.encryptedOwnerKeyAndMetadata.ownerKeyVersion

        print("")
        print("-" * 40)
        print("Attention:")
        print("-" * 40)

        if encrypted_user_secrets.ownerKeyVersion < current_owner_key_version:
            print(f"Failed to decrypt E2EE data. This tracker was encrypted with owner key version {encrypted_user_secrets.ownerKeyVersion}, but the current owner key version is {current_owner_key_version}.\nThis happens if you reset your end-to-end-encrypted data in the past.\nThe tracker cannot be decrypted anymore, and it is recommended to remove it in the Find My Device app.")
            exit(1)
        else:
            print(f"Failed to decrypt identity key encrypted with owner key version {encrypted_user_secrets.ownerKeyVersion}, current owner key version is {current_owner_key_version}.\nThis may happen if you reset your end-to-end-encrypted data. To resolve this issue, open the folder 'Auth' and delete the file 'secrets.json'.")
            exit(1)


def _beacon_time_candidates(
    is_mcu: bool,
    device_time_offset: int,
    pair_date: int | None,
    report_time_seconds: int,
) -> list[int]:
    """Candidate beacon time counters for EID-based AES-EAX decryption (see calculate_r)."""
    primary = 0 if is_mcu else device_time_offset
    candidates: list[int] = [primary]
    if is_mcu:
        candidates.append(device_time_offset)
    else:
        candidates.append(0)
    if pair_date and report_time_seconds >= pair_date:
        rel = report_time_seconds - pair_date
        aligned = (rel // ROTATION_PERIOD) * ROTATION_PERIOD
        if aligned >= 0:
            candidates.extend(
                (
                    aligned,
                    max(0, aligned - ROTATION_PERIOD),
                    aligned + ROTATION_PERIOD,
                )
            )
    for delta in (-3, -2, -1, 1, 2, 3):
        candidates.append((primary + delta * ROTATION_PERIOD) & 0xFFFFFFFF)
    seen: set[int] = set()
    out: list[int] = []
    for x in candidates:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _try_decrypt_network_location(
    identity_key: bytes,
    encrypted_location: bytes,
    public_key_random: bytes,
    is_mcu: bool,
    device_time_offset: int,
    pair_date: int | None,
    report_time_seconds: int,
) -> bytes:
    """Decrypt crowdsourced/network report; retry alternate beacon times if MAC fails."""
    last_err: Exception | None = None
    for beacon_time in _beacon_time_candidates(
        is_mcu, device_time_offset, pair_date, report_time_seconds
    ):
        try:
            return decrypt(
                identity_key,
                encrypted_location,
                public_key_random,
                beacon_time,
            )
        except ValueError as e:
            last_err = e
            if "MAC check failed" not in str(e):
                raise
            continue
    assert last_err is not None
    raise last_err


def decrypt_location_response_locations(device_update_protobuf):

    device_registration = device_update_protobuf.deviceMetadata.information.deviceRegistration

    pair_date = None
    try:
        pd = device_registration.pairDate
        if pd:
            pair_date = int(pd)
    except Exception:
        pass

    identity_key = retrieve_identity_key(device_registration)
    locations_proto = device_update_protobuf.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    is_mcu = is_mcu_tracker(device_registration)

    # At All Areas Reports or Own Reports
    recent_location = locations_proto.recentLocation
    recent_location_time = locations_proto.recentLocationTimestamp

    # High Traffic Reports
    network_locations = list(locations_proto.networkLocations)
    network_locations_time = list(locations_proto.networkLocationTimestamps)

    if locations_proto.HasField("recentLocation"):
        network_locations.append(recent_location)
        network_locations_time.append(recent_location_time)

    location_time_array = []
    for loc, time in zip(network_locations, network_locations_time):

        if loc.status == Common_pb2.Status.SEMANTIC:
            print("Semantic Location Report")

            wrapped_location = WrappedLocation(
                decrypted_location=b'',
                time=int(time.seconds),
                accuracy=0,
                status=loc.status,
                is_own_report=True,
                name=loc.semanticLocation.locationName
            )
            location_time_array.append(wrapped_location)
        else:

            encrypted_location = loc.geoLocation.encryptedReport.encryptedLocation
            public_key_random = loc.geoLocation.encryptedReport.publicKeyRandom

            if public_key_random == b"":  # Own Report
                identity_key_hash = hashlib.sha256(identity_key).digest()
                decrypted_location = decrypt_aes_gcm(identity_key_hash, encrypted_location)
            else:
                device_time_offset_raw = loc.geoLocation.deviceTimeOffset
                report_ts = int(time.seconds)
                decrypted_location = _try_decrypt_network_location(
                    identity_key,
                    encrypted_location,
                    public_key_random,
                    is_mcu,
                    device_time_offset_raw,
                    pair_date,
                    report_ts,
                )

            wrapped_location = WrappedLocation(
                decrypted_location=decrypted_location,
                time=int(time.seconds),
                accuracy=loc.geoLocation.accuracy,
                status=loc.status,
                is_own_report=loc.geoLocation.encryptedReport.isOwnReport,
                name=""
            )
            location_time_array.append(wrapped_location)

    print("-" * 40)
    print(f"[EIK] {identity_key.hex()}")
    try:
        owner_key = get_owner_key()
        ak = decrypt_aes_gcm(owner_key, device_registration.encryptedUserSecrets.encryptedAccountKey)
        print(f"[AccountKey] {ak.hex()}")
    except Exception:
        pass
    try:
        from FMDNCrypto.eid_generator import generate_eid
        eid = generate_eid(identity_key, 0)
        print(f"[EID] {eid.hex()}")
    except Exception:
        pass
    try:
        pair_date = device_registration.pairDate
        if pair_date:
            print(f"[PairDate] {pair_date}")
    except Exception:
        pass
    print("-" * 40)
    print("[DecryptLocations] Decrypted Locations:")

    if not location_time_array:
        print("No locations found.")
        return

    for loc in location_time_array:

        if loc.status == Common_pb2.Status.SEMANTIC:
            print(f"Semantic Location: {loc.name}")

        else:
            proto_loc = DeviceUpdate_pb2.Location()
            proto_loc.ParseFromString(loc.decrypted_location)

            latitude = proto_loc.latitude / 1e7
            longitude = proto_loc.longitude / 1e7
            altitude = proto_loc.altitude

            print(f"Latitude: {latitude}")
            print(f"Longitude: {longitude}")
            print(f"Altitude: {altitude}")
            print(f"Google Maps Link: {create_google_maps_link(latitude, longitude)}")
            
        try:
            status_str = Common_pb2.Status.Name(loc.status)
        except ValueError:
            status_str = str(loc.status)
            
        print(f"Time: {datetime.datetime.fromtimestamp(loc.time).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Status: {status_str}")
        print(f"Is Own Report: {loc.is_own_report}")
        print("-" * 40)

    pass


if __name__ == '__main__':
    res = parse_device_update_protobuf("")
    decrypt_location_response_locations(res)