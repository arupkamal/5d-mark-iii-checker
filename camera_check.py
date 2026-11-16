#!/usr/bin/env python3
"""Canon EOS 5D Mark III connection checker for macOS.

Looks at the Mac's USB device tree (via `ioreg`) for the camera's USB IDs:
    Vendor ID  0x04A9  (Canon)
    Product ID 0x323A  (EOS 5D Mark III)

Usage:
    python3 camera_check.py          # window that updates every 2 seconds
    python3 camera_check.py --cli    # print status once (exit code 0 = connected)
    python3 camera_check.py --watch  # print a line whenever the status changes

No third-party packages needed.
"""

import plistlib
import subprocess
import sys
import time

CANON_VENDOR_ID = 0x04A9
EOS_5D_MK3_PRODUCT_ID = 0x323A
POLL_SECONDS = 2


def usb_devices():
    """Yield a dict for every USB device currently attached."""
    out = subprocess.run(
        ["ioreg", "-a", "-p", "IOUSB", "-l"],
        capture_output=True, check=False,
    ).stdout
    if not out.strip():
        return
    root = plistlib.loads(out)
    stack = root if isinstance(root, list) else [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if "idVendor" in node:
            yield node
        stack.extend(node.get("IORegistryEntryChildren", []))


def check_camera():
    """Return (status, message). status is 'connected', 'other_canon' or 'missing'."""
    other_canon = []
    for dev in usb_devices():
        if dev.get("idVendor") != CANON_VENDOR_ID:
            continue
        name = dev.get("USB Product Name") or dev.get("kUSBProductString") or "Canon device"
        if dev.get("idProduct") == EOS_5D_MK3_PRODUCT_ID:
            serial = dev.get("USB Serial Number") or dev.get("kUSBSerialNumberString")
            msg = f"{name} is connected"
            if serial:
                msg += f" (serial {serial})"
            return "connected", msg
        other_canon.append(f"{name} (product ID 0x{dev.get('idProduct', 0):04X})")
    if other_canon:
        return "other_canon", "A Canon device is connected, but not a 5D Mark III: " + ", ".join(other_canon)
    return "missing", "Canon EOS 5D Mark III not detected"


HINT = "Make sure the camera is switched on and the USB cable is plugged into the Mac mini."


def run_cli():
    status, msg = check_camera()
    print(msg)
    if status != "connected":
        print(HINT)
    return 0 if status == "connected" else 1


def run_watch():
    last = None
    print(f"Watching for the Canon EOS 5D Mark III (Ctrl+C to stop)...")
    try:
        while True:
            status, msg = check_camera()
            if status != last:
                print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
                last = status
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print()
    return 0


def run_gui():
    import tkinter as tk

    colors = {"connected": "#2ecc71", "other_canon": "#f39c12", "missing": "#e74c3c"}
    titles = {"connected": "Connected", "other_canon": "Different Canon device", "missing": "Not connected"}

    root = tk.Tk()
    root.title("5D Mark III Checker")
    root.geometry("440x280")
    root.resizable(False, False)

    tk.Label(root, text="Canon EOS 5D Mark III", font=("Helvetica", 18, "bold")).pack(pady=(18, 8))

    canvas = tk.Canvas(root, width=60, height=60, highlightthickness=0)
    dot = canvas.create_oval(6, 6, 54, 54, fill="#999999", outline="")
    canvas.pack()

    title_lbl = tk.Label(root, text="Checking...", font=("Helvetica", 16))
    title_lbl.pack(pady=(8, 2))
    detail_lbl = tk.Label(root, text="", font=("Helvetica", 11), wraplength=400, justify="center")
    detail_lbl.pack()
    time_lbl = tk.Label(root, text="", font=("Helvetica", 10), fg="#888888")
    time_lbl.pack(side="bottom", pady=8)

    live = {"window": None}

    def open_live_view():
        if live["window"] is not None and live["window"].win.winfo_exists():
            live["window"].win.lift()
            return
        try:
            import liveview
        except ImportError as exc:
            detail_lbl.config(text=f"Live view needs the gphoto2 and Pillow packages ({exc}).\n"
                                   "Start the app with 'Start Camera App.command'.")
            return
        live["window"] = liveview.LiveViewWindow(root)

    live_btn = tk.Button(root, text="Open Live View", state="disabled", command=open_live_view)
    live_btn.pack(pady=(10, 0))

    def refresh():
        try:
            status, msg = check_camera()
        except Exception as exc:  # keep the window alive if ioreg misbehaves
            status, msg = "missing", f"Error reading USB devices: {exc}"
        live_open = live["window"] is not None and live["window"].win.winfo_exists()
        live_btn.config(state="normal" if status == "connected" or live_open else "disabled")
        canvas.itemconfig(dot, fill=colors[status])
        title_lbl.config(text=titles[status])
        detail_lbl.config(text=msg if status == "connected" else f"{msg}\n{HINT}")
        time_lbl.config(text=f"Last checked {time.strftime('%H:%M:%S')} · refreshes every {POLL_SECONDS}s")
        root.after(POLL_SECONDS * 1000, refresh)

    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("This app only runs on macOS.")
    if "--cli" in sys.argv:
        sys.exit(run_cli())
    if "--watch" in sys.argv:
        sys.exit(run_watch())
    sys.exit(run_gui())
