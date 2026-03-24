import asyncio
import uuid
import winrt.windows.devices.bluetooth as bt
import winrt.windows.devices.bluetooth.genericattributeprofile as gatt
from winrt.windows.storage.streams import DataWriter

# Fast Pair Service
FAST_PAIR_SVC_UUID = uuid.UUID("0000FE2C-0000-1000-8000-00805F9B34FB")
# Beacon Actions Characteristic
BEACON_ACTIONS_CHR_UUID = uuid.UUID("FE2C1238-8366-4814-8EB0-01DE32100BEA")

async def main():
    print("Testing FMD GATT Server on Windows...")
    
    try:
        # 1. Create a GATT service provider for the Fast Pair Service
        print(f"Creating GATT Service Provider for UUID {FAST_PAIR_SVC_UUID}...")
        provider_result = await gatt.GattServiceProvider.create_async(FAST_PAIR_SVC_UUID)
        
        if provider_result.error != bt.BluetoothError.SUCCESS:
            print(f"Failed to create service provider. Error: {provider_result.error}")
            return
            
        service_provider = provider_result.service_provider
        print("Service Provider created successfully.")
        
        # 2. Add Beacon Actions characteristic
        print("Adding Beacon Actions characteristic...")
        char_params = gatt.GattLocalCharacteristicParameters()
        # Same properties as the Raspberry Pi script
        char_params.characteristic_properties = (
            gatt.GattCharacteristicProperties.READ | 
            gatt.GattCharacteristicProperties.WRITE | 
            gatt.GattCharacteristicProperties.NOTIFY
        )
        char_params.user_description = "Beacon Actions"
        
        char_result = await service_provider.service.create_characteristic_async(
            BEACON_ACTIONS_CHR_UUID, 
            char_params
        )
        
        if char_result.error != bt.BluetoothError.SUCCESS:
            print(f"Failed to create characteristic. Error: {char_result.error}")
            return
            
        characteristic = char_result.characteristic
        print("Characteristic added successfully.")

        # 3. Define event handlers
        def on_read_requested(sender, args):
            print("Read request received! (Normally returns protocol version + nonce)")
            deferral = args.get_deferral()
            
            async def handle_request_async():
                try:
                    request = await args.get_request_async()
                    writer = DataWriter()
                    # Example payload from RPi: [PROTOCOL_MAJOR_VERSION(0x01)] + [8 bytes nonce]
                    writer.write_bytes(bytes([0x01, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]))
                    request.respond_with_value(writer.detach_buffer())
                    print("Responded to read request.")
                except Exception as e:
                    print(f"Error handling read request: {e}")
                finally:
                    deferral.complete()
            
            asyncio.create_task(handle_request_async())
            
        def on_write_requested(sender, args):
            print("Write request received! (Normally receives ring/auth commands)")
            deferral = args.get_deferral()
            
            async def handle_request_async():
                try:
                    request = await args.get_request_async()
                    # Do something with request.value
                    print("Handled write request")
                    request.respond()
                except Exception as e:
                    print(f"Error handling write request: {e}")
                finally:
                    deferral.complete()
            
            asyncio.create_task(handle_request_async())

        def on_subscribed_clients_changed(sender, args):
            print(f"Subscribed clients changed! Active clients: {len(characteristic.subscribed_clients)}")
        
        characteristic.add_read_requested(on_read_requested)
        characteristic.add_write_requested(on_write_requested)
        characteristic.add_subscribed_clients_changed(on_subscribed_clients_changed)
        
        # 4. Start advertising the service
        adv_params = gatt.GattServiceProviderAdvertisingParameters()
        adv_params.is_discoverable = True
        adv_params.is_connectable = True
        
        print("Starting advertising...")
        # PyWinRT exposes the parameterized WinRT overload as start_advertising_with_parameters
        # (start_advertising() is the no-arg overload only).
        service_provider.start_advertising_with_parameters(adv_params)
        print(f"Advertising status: {service_provider.advertisement_status}")
        
        print("FMD GATT Server is running. Advertising for 15 seconds...")
        for i in range(15):
            print(f"Tick {i+1}...")
            await asyncio.sleep(1)
            
        print("Stopping advertising...")
        service_provider.stop_advertising()
        print("Done. Your laptop is safe.")
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())