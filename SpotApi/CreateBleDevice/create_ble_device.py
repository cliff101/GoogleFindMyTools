#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import secrets
import time

from FMDNCrypto.key_derivation import FMDNOwnerOperations
from FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid
from KeyBackup.cloud_key_decryptor import encrypt_aes_gcm
from ProtoDecoders.DeviceUpdate_pb2 import DeviceComponentInformation, SpotDeviceType, RegisterBleDeviceRequest, PublicKeyIdList
from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id, max_truncated_eid_seconds_server
from SpotApi.CreateBleDevice.util import flip_bits
from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
from SpotApi.spot_request import spot_request


def register_esp32(device_name="GoogleFindMyTools µC", flip_e2ee=True):

    owner_key = get_owner_key()

    eik = secrets.token_bytes(32)
    pair_date = int(time.time())
    if flip_e2ee:
        # Static EID mode (ESP32 / no GATT server): EID never changes, always offset 0.
        eid = generate_eid(eik, 0)
    else:
        # Rotating EID mode (Raspberry Pi): compute from current offset like fmd_tracker.sh.
        current_offset = (int(time.time()) - pair_date) // ROTATION_PERIOD * ROTATION_PERIOD
        eid = generate_eid(eik, current_offset)

    register_request = RegisterBleDeviceRequest()
    register_request.fastPairModelId = mcu_fast_pair_model_id

    # Description
    register_request.description.userDefinedName = device_name
    register_request.description.deviceType = SpotDeviceType.DEVICE_TYPE_BEACON

    # Device Components Information
    component_information = DeviceComponentInformation()
    component_information.imageUrl = "https://docs.espressif.com/projects/esp-idf/en/v4.3/esp32/_images/esp32-DevKitM-1-isometric.png"
    register_request.description.deviceComponentsInformation.append(component_information)

    # Capabilities
    register_request.capabilities.isAdvertising = True
    register_request.capabilities.trackableComponents = 1
    register_request.capabilities.capableComponents = 1

    # E2EE Registration
    register_request.e2eePublicKeyRegistration.rotationExponent = 10
    register_request.e2eePublicKeyRegistration.pairingDate = pair_date

    # Encrypted User Secrets
    # Flip bits so Android devices cannot decrypt the key (if flip_e2ee is True)
    register_request.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedIdentityKey = flip_bits(encrypt_aes_gcm(owner_key, eik), flip_e2ee)

    import hashlib
    # Real keys instead of random garbage so Android can properly decrypt them
    account_key = secrets.token_bytes(16)
    public_address = hashlib.sha256(account_key).digest()

    register_request.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedAccountKey = encrypt_aes_gcm(owner_key, account_key)
    register_request.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedSha256AccountKeyPublicAddress = encrypt_aes_gcm(owner_key, public_address)

    register_request.e2eePublicKeyRegistration.encryptedUserSecrets.ownerKeyVersion = 1
    register_request.e2eePublicKeyRegistration.encryptedUserSecrets.creationDate.seconds = pair_date

    # announce advertisements
    for i in range(int(max_truncated_eid_seconds_server / ROTATION_PERIOD)):
        time_offset = i * ROTATION_PERIOD
        if flip_e2ee:
            # Static EID mode: same initial EID for every window (device never rotates).
            window_eid = eid
        else:
            # Rotating EID mode: each window gets its own unique EID.
            window_eid = generate_eid(eik, time_offset)

        pub_key_id = PublicKeyIdList.PublicKeyIdInfo()
        pub_key_id.publicKeyId.truncatedEid = window_eid[:10]
        pub_key_id.timestamp.seconds = pair_date + time_offset
        register_request.e2eePublicKeyRegistration.publicKeyIdList.publicKeyIdInfo.append(pub_key_id)

    # General
    register_request.manufacturerName = "GoogleFindMyTools"
    register_request.modelName = "µC"

    ownerKeys = FMDNOwnerOperations()
    ownerKeys.generate_keys(identity_key=eik)

    register_request.ringKey = ownerKeys.ringing_key
    register_request.recoveryKey = ownerKeys.recovery_key
    register_request.unwantedTrackingKey = ownerKeys.tracking_key

    bytes_data = register_request.SerializeToString()
    spot_request("CreateBleDevice", bytes_data)

    print("Registered device successfully. Copy the keys below. They will not be shown again.")
    print("Afterward, go to the folder 'GoogleFindMyTools/ESP32Firmware' or 'GoogleFindMyTools/ZephyrFirmware' and follow the instructions in the README.md file.")

    pair_date_str = str(pair_date)
    pair_date_padding_left = (78 - len(pair_date_str)) // 2
    pair_date_padding_right = 78 - len(pair_date_str) - pair_date_padding_left

    eid_label = "Advertisement Key (static)" if flip_e2ee else "Advertisement Key (initial, rotates)"
    eid_label_padding_left = (78 - len(eid_label)) // 2
    eid_label_padding_right = 78 - len(eid_label) - eid_label_padding_left
    print("+" + "-" * 78 + "+")
    print("|" + " " * 19 + eid.hex() + " " * 19 + "|")
    print("|" + " " * eid_label_padding_left + eid_label + " " * eid_label_padding_right + "|")
    print("+" + "-" * 78 + "+")
    print("|" + " " * 7 + eik.hex() + " " * 7 + "|")
    print("|" + " " * 21 + "Ephemeral Identity Key (EIK)" + " " * 29 + "|")
    print("+" + "-" * 78 + "+")
    print("|" + " " * 23 + account_key.hex() + " " * 23 + "|")
    print("|" + " " * 33 + "Account Key" + " " * 34 + "|")
    print("+" + "-" * 78 + "+")
    print("|" + " " * pair_date_padding_left + pair_date_str + " " * pair_date_padding_right + "|")
    print("|" + " " * 33 + "Pair Date" + " " * 36 + "|")
    print("+" + "-" * 78 + "+")