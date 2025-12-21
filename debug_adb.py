from adbutils import adb
import traceback

print("Testing adbutils...")
try:
    # print(f"ADB Path: {adb.adb_path()}")
    devices = adb.device_list()
    print(f"Found {len(devices)} devices")
    
    for d in devices:
        print(f"--- Device: {d.serial} ---")
        try:
            print(f"Model: {d.prop.get('ro.product.model')}")
            
            # Test IP logic
            ip = "USB"
            if "." in d.serial:
                ip = d.get_serial_no()
            else:
                try:
                    res = d.shell("ip addr show wlan0 | grep 'inet '")
                    print(f"IP Shell Result: {res}")
                except Exception as e:
                    print(f"IP Error: {e}")

            # Test Battery logic
            try:
                raw_bat = d.shell("dumpsys battery | grep level")
                print(f"Battery Raw: {raw_bat[:100]}...") # Print first 100 chars
                if raw_bat:
                    val = int(raw_bat.split(":")[1].strip())
                    print(f"Parsed Battery: {val}")
            except Exception as e:
                print(f"Battery Error: {e}")
                traceback.print_exc()

        except Exception as e:
            print(f"Error reading props: {e}")
            traceback.print_exc()

except Exception as e:
    print(f"CRITICAL ERROR: {e}")
    traceback.print_exc()
