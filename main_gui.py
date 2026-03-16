import tkinter as tk
from tkinter import messagebox, simpledialog
import tkintermapview
import threading
import sys
import io
import re

# Import existing functionality from the GoogleFindMyTools
from NovaApi.ListDevices.nbe_list_devices import request_device_list
from ProtoDecoders.decoder import parse_device_list_protobuf, get_canonic_ids
from SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import refresh_custom_trackers
from NovaApi.ExecuteAction.LocateTracker.location_request import get_location_data_for_device
from SpotApi.CreateBleDevice.create_ble_device import register_esp32

class FindMyGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Google Find My Tools GUI")
        self.root.geometry("1200x600")

        self.canonic_ids = []
        self.locations = []

        # Left panel: Devices
        self.left_frame = tk.Frame(root, width=250, bg="#f0f0f0")
        self.left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)

        self.listbox_label = tk.Label(self.left_frame, text="Devices", bg="#f0f0f0", font=("Arial", 12, "bold"))
        self.listbox_label.pack(anchor="w")

        self.device_listbox = tk.Listbox(self.left_frame, height=20, font=("Arial", 11))
        self.device_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        self.device_listbox.bind('<<ListboxSelect>>', self.on_device_select)

        self.refresh_btn = tk.Button(self.left_frame, text="Refresh Devices", command=self.load_devices_async)
        self.refresh_btn.pack(fill=tk.X, pady=5)

        self.register_btn = tk.Button(self.left_frame, text="Register a new tracker", command=self.register_tracker_async)
        self.register_btn.pack(side=tk.BOTTOM, fill=tk.X, pady=10)

        # Middle panel: Locations
        self.middle_frame = tk.Frame(root, width=250, bg="#e0e0e0")
        self.middle_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)

        self.loc_listbox_label = tk.Label(self.middle_frame, text="Locations", bg="#e0e0e0", font=("Arial", 12, "bold"))
        self.loc_listbox_label.pack(anchor="w")

        self.loc_listbox = tk.Listbox(self.middle_frame, height=20, font=("Arial", 11))
        self.loc_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        self.loc_listbox.bind('<<ListboxSelect>>', self.on_location_select)

        # Right panel: Info and Map
        self.right_frame = tk.Frame(root)
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.info_text = tk.Text(self.right_frame, height=12, state=tk.DISABLED, font=("Consolas", 10))
        self.info_text.pack(fill=tk.X, pady=(0, 10))

        # Map widget
        self.map_widget = tkintermapview.TkinterMapView(self.right_frame, corner_radius=0)
        self.map_widget.pack(fill=tk.BOTH, expand=True)

        # Initial load
        self.load_devices_async()

    def load_devices_async(self):
        self.device_listbox.delete(0, tk.END)
        self.device_listbox.insert(tk.END, "Loading devices...")
        threading.Thread(target=self._load_devices_thread, daemon=True).start()

    def _load_devices_thread(self):
        try:
            result_hex = request_device_list()
            device_list = parse_device_list_protobuf(result_hex)
            refresh_custom_trackers(device_list)
            self.canonic_ids = get_canonic_ids(device_list)

            self.root.after(0, self._update_device_list)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to load devices:\n{str(e)}"))

    def _update_device_list(self):
        self.device_listbox.delete(0, tk.END)
        for idx, (device_name, canonic_id) in enumerate(self.canonic_ids):
            self.device_listbox.insert(tk.END, f"{device_name}")

    def on_device_select(self, event):
        selection = self.device_listbox.curselection()
        if not selection:
            return

        idx = selection[0]
        # In case "Loading devices..." is selected
        if idx >= len(self.canonic_ids):
            return

        device_name, canonic_id = self.canonic_ids[idx]

        self.loc_listbox.delete(0, tk.END)
        self.locations = []
        
        self.info_text.config(state=tk.NORMAL)
        self.info_text.delete(1.0, tk.END)
        self.info_text.insert(tk.END, f"Requesting location for: {device_name}...\nThis may take a few seconds.")
        self.info_text.config(state=tk.DISABLED)

        threading.Thread(target=self._get_location_thread, args=(canonic_id, device_name), daemon=True).start()

    def _get_location_thread(self, canonic_id, device_name):
        # Redirect stdout to capture print statements
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()
        try:
            get_location_data_for_device(canonic_id, device_name)
        except Exception as e:
            print(f"Error fetching location: {e}")
        finally:
            sys.stdout = old_stdout

        output_str = captured_output.getvalue()
        self.root.after(0, lambda: self._update_location_ui(output_str))

    def _update_location_ui(self, output_str):
        self.locations = []
        self.loc_listbox.delete(0, tk.END)
        
        # Clean up output: Only show the Decrypted Locations part if present
        if "[DecryptLocations]" in output_str:
            clean_str = output_str[output_str.find("[DecryptLocations]"):]
        else:
            clean_str = output_str

        # Parse the string into location blocks
        blocks = clean_str.split("-" * 40)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            
            if "Latitude:" in block or "Semantic Location:" in block or "Time:" in block:
                loc_data = {}
                for line in block.split('\n'):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        loc_data[key.strip()] = val.strip()
                
                # Check if it has Time
                if "Time" in loc_data:
                    self.locations.append((block, loc_data))
                    
        # Populate Locations listbox
        if not self.locations:
            self.loc_listbox.insert(tk.END, "No locations found")
            
            self.info_text.config(state=tk.NORMAL)
            self.info_text.delete(1.0, tk.END)
            self.info_text.insert(tk.END, clean_str)
            self.info_text.config(state=tk.DISABLED)
        else:
            for i, (raw_text, loc_data) in enumerate(self.locations):
                time_str = loc_data.get('Time', 'Unknown')
                status_str = loc_data.get('Status', '?')
                self.loc_listbox.insert(tk.END, f"{time_str} (Status: {status_str})")
                
            # Auto-select the first location
            self.loc_listbox.selection_set(0)
            self.on_location_select(None)

    def on_location_select(self, event):
        selection = self.loc_listbox.curselection()
        if not selection:
            return
            
        idx = selection[0]
        if idx >= len(self.locations):
            return
            
        raw_text, loc_data = self.locations[idx]

        self.info_text.config(state=tk.NORMAL)
        self.info_text.delete(1.0, tk.END)
        self.info_text.insert(tk.END, raw_text)
        self.info_text.config(state=tk.DISABLED)

        try:
            lat = float(loc_data.get("Latitude", 0))
            lon = float(loc_data.get("Longitude", 0))
            if lat != 0 and lon != 0:
                self.map_widget.set_position(lat, lon)
                self.map_widget.set_zoom(15)
                self.map_widget.delete_all_marker()
                self.map_widget.set_marker(lat, lon, text="Device Location")
            else:
                self.map_widget.delete_all_marker()
        except ValueError:
            self.map_widget.delete_all_marker()

    def register_tracker_async(self):
        device_name = simpledialog.askstring("Register Device", "Enter device name:", initialvalue="GoogleFindMyTools µC")
        if device_name is None:
            return # User cancelled
        if device_name.strip() == "":
            device_name = "GoogleFindMyTools µC"
            
        threading.Thread(target=self._register_tracker_thread, args=(device_name,), daemon=True).start()

    def _register_tracker_thread(self, device_name):
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()
        try:
            register_esp32(device_name)
        except Exception as e:
            print(f"Error registering tracker: {e}")
        finally:
            sys.stdout = old_stdout

        res = captured_output.getvalue()
        self.root.after(0, lambda: self._show_register_result(res))
        
    def _show_register_result(self, res):
        top = tk.Toplevel(self.root)
        top.title("Register Tracker Result")
        top.geometry("700x400")
        
        text = tk.Text(top, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, res)
        text.config(state=tk.DISABLED)
        
        btn = tk.Button(top, text="Close", command=top.destroy)
        btn.pack(pady=10)

if __name__ == "__main__":
    root = tk.Tk()
    app = FindMyGUI(root)
    root.mainloop()