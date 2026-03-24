import winsdk.windows.devices.bluetooth.advertisement as adv
from winsdk.windows.storage.streams import DataWriter
import time

def main():
    print("Creating publisher...")
    publisher = adv.BluetoothLEAdvertisementPublisher()
    
    # We want to emulate the Google Find My Device (Eddystone) payload
    # Service Data Type: 0x16
    writer = DataWriter()
    
    # Payload format:
    # [16-bit UUID: AA FE (Little Endian for 0xFEAA)]
    # [Frame Type: 0x40 (FHN)]
    # [EID: 20 bytes]
    
    # Mock 20-byte EID for testing
    mock_eid = [
        0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 
        0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14
    ]
    
    payload = [0xAA, 0xFE, 0x40] + mock_eid
    writer.write_bytes(bytes(payload))
    
    # Create the advertisement data section
    section = adv.BluetoothLEAdvertisementDataSection()
    section.data_type = 0x16  # 0x16 = Service Data
    section.data = writer.detach_buffer()
    
    publisher.advertisement.data_sections.append(section)
    
    print("Starting advertisement with mock EID payload...")
    publisher.start()
    
    time.sleep(0.5)
    print(f"Publisher status: {publisher.status.name}")
    
    if publisher.status.name == "ABORTED":
        print("Error: The advertisement was aborted. It might be due to payload restrictions or lack of Bluetooth permissions.")
        return

    print("Advertising for 10 seconds...")
    try:
        for i in range(10):
            print(f"Tick {i+1}...")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
        
    print("Stopping advertisement...")
    publisher.stop()
    print("Done. Your laptop is perfectly safe!")

if __name__ == "__main__":
    main()
