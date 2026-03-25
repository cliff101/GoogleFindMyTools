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
    # Default map zoom (used on first load by tkintermapview; "Reset zoom" restores this level)
    MAP_DEFAULT_ZOOM = 15

    def __init__(self, root):
        self.root = root
        self.root.title("Google Find My Tools GUI")
        self.root.geometry("1600x1000")

        self.canonic_ids = []
        self.locations = []

        # Left panel: Devices
        self.left_frame = tk.Frame(root, width=250, bg="#f0f0f0")
        self.left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)

        self.listbox_label = tk.Label(self.left_frame, text="Devices", bg="#f0f0f0", font=("Arial", 12, "bold"))
        self.listbox_label.pack(anchor="w")

        device_list_frame = tk.Frame(self.left_frame, bg="#f0f0f0")
        device_list_frame.pack(fill=tk.X, pady=5)

        device_list_scroll = tk.Scrollbar(device_list_frame, orient=tk.VERTICAL)
        device_list_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.device_listbox = tk.Listbox(
            device_list_frame,
            height=15,
            font=("Arial", 11),
            yscrollcommand=device_list_scroll.set,
        )
        self.device_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        device_list_scroll.config(command=self.device_listbox.yview)
        self.device_listbox.bind('<<ListboxSelect>>', self.on_device_select)

        self.refresh_btn = tk.Button(self.left_frame, text="Refresh Devices", command=self.load_devices_async)
        self.refresh_btn.pack(fill=tk.X, pady=(0, 5))

        self.eik_label = tk.Label(self.left_frame, text="EIK (for GATT server):", bg="#f0f0f0", font=("Arial", 9))
        self.eik_label.pack(anchor="w", pady=(4, 0))

        self.eik_var = tk.StringVar()
        self.eik_entry = tk.Entry(self.left_frame, textvariable=self.eik_var,
                                  font=("Consolas", 9), state='readonly', readonlybackground="#fff")
        self.eik_entry.pack(fill=tk.X, pady=(0, 2))

        self.copy_eik_btn = tk.Button(self.left_frame, text="Copy EIK", command=self._copy_eik)
        self.copy_eik_btn.pack(fill=tk.X, pady=(0, 5))

        self.ak_label = tk.Label(self.left_frame, text="Account Key:", bg="#f0f0f0", font=("Arial", 9))
        self.ak_label.pack(anchor="w", pady=(5, 0))

        self.ak_var = tk.StringVar()
        self.ak_entry = tk.Entry(self.left_frame, textvariable=self.ak_var,
                                 font=("Consolas", 9), state='readonly', readonlybackground="#fff")
        self.ak_entry.pack(fill=tk.X, pady=(0, 2))

        self.copy_ak_btn = tk.Button(self.left_frame, text="Copy Account Key", command=self._copy_ak)
        self.copy_ak_btn.pack(fill=tk.X, pady=(0, 5))

        self.eid_label = tk.Label(self.left_frame, text="EID (Advertisement Key):", bg="#f0f0f0", font=("Arial", 9))
        self.eid_label.pack(anchor="w", pady=(5, 0))

        self.eid_var = tk.StringVar()
        self.eid_entry = tk.Entry(self.left_frame, textvariable=self.eid_var,
                                  font=("Consolas", 9), state='readonly', readonlybackground="#fff")
        self.eid_entry.pack(fill=tk.X, pady=(0, 2))

        self.copy_eid_btn = tk.Button(self.left_frame, text="Copy EID", command=self._copy_eid)
        self.copy_eid_btn.pack(fill=tk.X, pady=(0, 5))

        self.pair_date_label = tk.Label(self.left_frame, text="Pair Date (Unix timestamp):", bg="#f0f0f0", font=("Arial", 9))
        self.pair_date_label.pack(anchor="w", pady=(5, 0))

        self.pair_date_var = tk.StringVar()
        self.pair_date_entry = tk.Entry(self.left_frame, textvariable=self.pair_date_var,
                                        font=("Consolas", 9), state='readonly', readonlybackground="#fff")
        self.pair_date_entry.pack(fill=tk.X, pady=(0, 2))

        self.copy_pair_date_btn = tk.Button(self.left_frame, text="Copy Pair Date", command=self._copy_pair_date)
        self.copy_pair_date_btn.pack(fill=tk.X, pady=(0, 5))

        self.register_btn = tk.Button(self.left_frame, text="Register a new tracker", command=self.register_tracker_async)
        self.register_btn.pack(fill=tk.X, pady=(10, 5))

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

        self.map_toolbar = tk.Frame(self.right_frame)
        self.map_toolbar.pack(fill=tk.X, pady=(0, 4))
        self.reset_zoom_btn = tk.Button(
            self.map_toolbar,
            text="Reset zoom",
            command=self._reset_map_zoom,
        )
        self.reset_zoom_btn.pack(side=tk.RIGHT)

        # Map widget
        self.map_widget = tkintermapview.TkinterMapView(self.right_frame, corner_radius=0)
        self.map_widget.pack(fill=tk.BOTH, expand=True)

        # Initial load
        self.load_devices_async()

    def _reset_map_zoom(self):
        self.map_widget.set_zoom(
            self.MAP_DEFAULT_ZOOM,
            relative_pointer_x=0.5,
            relative_pointer_y=0.5,
        )

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
            self.root.after(0, lambda e=e: messagebox.showerror("Error", f"Failed to load devices:\n{str(e)}"))

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

    def _copy_eik(self):
        eik = self.eik_var.get()
        if eik:
            self.root.clipboard_clear()
            self.root.clipboard_append(eik)

    def _copy_ak(self):
        ak = self.ak_var.get()
        if ak:
            self.root.clipboard_clear()
            self.root.clipboard_append(ak)

    def _copy_eid(self):
        eid = self.eid_var.get()
        if eid:
            self.root.clipboard_clear()
            self.root.clipboard_append(eid)

    def _copy_pair_date(self):
        pair_date = self.pair_date_var.get()
        if pair_date:
            self.root.clipboard_clear()
            self.root.clipboard_append(pair_date)

    def _update_location_ui(self, output_str):
        self.locations = []
        self.loc_listbox.delete(0, tk.END)

        self.eik_var.set("")
        self.ak_var.set("")
        self.eid_var.set("")
        self.pair_date_var.set("")

        # Extract EIK, Account Key, EID, and Pair Date from output before trimming
        eik_match = re.search(r'\[EIK\]\s+([0-9a-fA-F]{64})', output_str)
        ak_match = re.search(r'\[AccountKey\]\s+([0-9a-fA-F]{32})', output_str)
        eid_match = re.search(r'\[EID\]\s+([0-9a-fA-F]{40})', output_str)
        pair_date_match = re.search(r'\[PairDate\]\s+(\d+)', output_str)
        if eik_match:
            self.eik_var.set(eik_match.group(1))
        if ak_match:
            self.ak_var.set(ak_match.group(1))
        if eid_match:
            self.eid_var.set(eid_match.group(1))
        if pair_date_match:
            self.pair_date_var.set(pair_date_match.group(1))

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
            
        flip_e2ee = messagebox.askyesno("Hide from FMD App?", "Do you want to hide the location in the official Google FMD app to prevent connection errors?\n\nSelecting 'No' will allow the official FMD app to decrypt the location, but it may cause the app to frequently attempt to connect to the tracker and show errors.\n\nRecommended: Yes")
            
        threading.Thread(target=self._register_tracker_thread, args=(device_name, flip_e2ee), daemon=True).start()

    def _register_tracker_thread(self, device_name, flip_e2ee):
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()
        try:
            register_esp32(device_name, flip_e2ee)
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