"""Live view window with click-to-focus for the Canon EOS 5D Mark III.

Uses libgphoto2 (via the `gphoto2` Python package) to:
  * stream live view frames from the camera
  * move the live view AF frame to the point you click (`eoszoomposition`)
  * trigger autofocus there (`autofocusdrive`)
  * magnify live view 5x / 10x around the focus point (`eoszoom`). Over USB the
    camera's 5x view is already a native-resolution crop and its 10x view is
    identical, so 10x is made here by cropping the centre of the 5x view.
  * nudge focus nearer / farther in small, medium and big steps (`manualfocusdrive`)
  * take a photo and save it to ~/Pictures/5D Mark III
  * keyboard focus steps: ; nearer, ' farther (add Cmd for medium, Ctrl for big)
  * focus peaking overlay plus a numeric sharpness score for the focus area
  * upload each photo to Amazon S3, Google Cloud Storage and/or FTP (uploads.py)
  * change ISO, aperture and shutter speed (what can be changed depends on the
    camera's mode dial: M = all three, Av = aperture, Tv = shutter speed)

Requirements on the camera:
  * Lens switch set to AF
  * Live View AF method set to "FlexiZone - Single" (Shooting menu > AF method).
    In "Face+Tracking" mode the camera picks the point itself and ignores clicks.
"""

import io
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

import gphoto2 as gp
from PIL import Image, ImageTk

import peaking
import uploads

# Canon EOS live view coordinate system for the 5D Mark III (sensor pixels).
# The zoom/AF frame position is given as the top-left corner of the 5x zoom box,
# which is 1/5 of the sensor in each direction.
COORD_W, COORD_H = 5760, 3840
ZOOM_BOX_W, ZOOM_BOX_H = COORD_W // 5, COORD_H // 5

DISPLAY_W, DISPLAY_H = 960, 640
# The 5D Mark III overwrites the first 32 bytes of most live view JPEGs (all of
# them while magnified) with other data. The rest of the image is intact, so the
# header from an earlier good frame is put back.
JPEG_HEADER_LEN = 32
AF_SECONDS = 2.0  # how long the camera is given to finish focusing
SAVE_DIR = os.path.expanduser("~/Pictures/5D Mark III")

# (gphoto2 setting name, label shown in the window)
EXPOSURE_SETTINGS = [("iso", "ISO"), ("aperture", "Aperture (f/)"), ("shutterspeed", "Shutter speed")]
MODE_NAMES = {"AV": "Av (aperture priority)", "TV": "Tv (shutter priority)", "Manual": "M (manual)",
              "P": "P (program)", "Bulb": "B (bulb)", "Auto": "Scene Intelligent Auto",
              "Custom": "C1 (custom)", "C2": "C2 (custom)", "C3": "C3 (custom)"}


class CameraWorker(threading.Thread):
    """Owns the camera. All gphoto2 calls happen on this thread."""

    def __init__(self, on_frame, on_status, on_event):
        super().__init__(daemon=True)
        self.on_frame = on_frame      # called with a PIL Image
        self.on_status = on_status    # called with a status string
        self.on_event = on_event      # called with ("settings", dict) / ("saved", [paths])
        self.zoom_state = (1, 0.5, 0.5)
        self.pending_nudges = 0
        self.nudge_lock = threading.Lock()
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.camera = None
        self.jpeg_header = None

    # ---- called from the UI thread -------------------------------------
    def focus_at(self, fx, fy):
        """fx, fy are 0..1 fractions of the live view image."""
        self.commands.put(("focus", fx, fy))

    def set_zoom(self, level, fx, fy):
        """Magnify 1x/5x/10x around the point fx, fy (0..1 of the full frame)."""
        self.commands.put(("zoom", level, fx, fy))

    def nudge_focus(self, step):
        """step is one of 'Near 1'..'Near 3' / 'Far 1'..'Far 3' (1 = smallest)."""
        # Holding a key down repeats it faster than the lens can move; don't let
        # steps pile up and keep the focus moving after the key is released.
        with self.nudge_lock:
            if self.pending_nudges >= 2:
                return
            self.pending_nudges += 1
        self.commands.put(("nudge", step))

    def take_photo(self):
        self.commands.put(("capture",))

    def request_settings(self):
        self.commands.put(("get_settings",))

    def change_setting(self, name, value):
        self.commands.put(("set_setting", name, value))

    def stop(self):
        self.stop_event.set()

    # ---- camera thread --------------------------------------------------
    def _open_camera(self):
        # macOS's own camera daemon (ptpcamerad) grabs PTP cameras as soon as they
        # appear, and relaunches itself right after being stopped, so connecting
        # is a race: attempts alternate between "could not claim" and "camera not
        # found" (while it re-attaches). Keep trying for a while.
        import camera_check
        last_err = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                cam = gp.Camera()
                cam.init()
                return cam
            except gp.GPhoto2Error as err:
                last_err = err
                if err.code in (gp.GP_ERROR_IO_USB_CLAIM, gp.GP_ERROR_IO_LOCK, gp.GP_ERROR_IO):
                    self.on_status("Releasing camera from macOS Image Capture...")
                    subprocess.run(["killall", "-9", "ptpcamerad"], capture_output=True)
                elif (err.code == gp.GP_ERROR_MODEL_NOT_FOUND
                      and camera_check.check_camera()[0] != "connected"):
                    raise RuntimeError("Camera not found. Is it switched on and plugged in?") from err
                time.sleep(0.7)
        raise RuntimeError(f"Could not connect to the camera: {last_err}. "
                           "Close other apps using it (Photos, Image Capture, another live view window).")

    def _set(self, name, value):
        # The camera answers "I/O in progress" while it is busy (e.g. focusing);
        # keep live view flowing and retry for up to ~2 seconds.
        for _ in range(40):
            try:
                widget = self.camera.get_single_config(name)
                widget.set_value(value)
                self.camera.set_single_config(name, widget)
                return
            except gp.GPhoto2Error as err:
                if err.code != gp.GP_ERROR_CAMERA_BUSY:
                    raise
                self._grab_frame()
        raise RuntimeError(f"Camera stayed busy while setting {name}")

    def _grab_frame(self):
        data = bytes(self.camera.capture_preview().get_data_and_size())
        if data[:2] == b"\xff\xd8":
            self.jpeg_header = data[:JPEG_HEADER_LEN]
        elif self.jpeg_header is not None:
            data = self.jpeg_header + data[JPEG_HEADER_LEN:]
        else:
            return  # no good frame seen yet to repair this one with
        try:
            self.on_frame(Image.open(io.BytesIO(data)).convert("RGB"))
        except OSError:
            pass

    def _release_af(self):
        if self.af_release_at is not None:
            self.af_release_at = None
            try:
                self._set("autofocusdrive", 0)
            except (gp.GPhoto2Error, RuntimeError):
                pass

    def _move_frame(self, fx, fy):
        """Move the camera's AF / magnify frame so it is centred on fx, fy."""
        x, y = frame_top_left(fx, fy)
        self._set("eoszoomposition", f"{x},{y}")
        self._grab_frame()  # give the camera a moment to move the frame

    def _zoom(self, level, fx, fy):
        self._release_af()
        if level != 1:
            self._move_frame(fx, fy)
        # 10x is a crop of the camera's 5x view (see the note at the top).
        self._set("eoszoom", "1" if level == 1 else "5")
        self.zoom_state = (level, fx, fy)
        self.on_status(f"Zoom {level}x." + (" Click to focus at that spot." if level != 1 else ""))

    def _nudge(self, step):
        self._release_af()
        try:
            self._set("manualfocusdrive", step)
            self._grab_frame()
            self._set("manualfocusdrive", "None")
            direction, size = step.split()
            self.on_status(f"Focus moved {'nearer' if direction == 'Near' else 'farther'} "
                           f"({ {'1': 'small', '2': 'medium', '3': 'big'}[size]} step).")
        except (gp.GPhoto2Error, RuntimeError) as err:
            self.on_status(f"Could not move focus: {err}. Check the lens is set to AF.")

    def _focus(self, fx, fy):
        self._release_af()  # finish any focus that is still running
        self.on_status("Focusing...")
        try:
            self._move_frame(fx, fy)
            self._set("autofocusdrive", 1)
            # Autofocus runs on the camera while live view keeps streaming.
            # Setting autofocusdrive back to 0 cancels it, so wait before releasing.
            self.af_release_at = time.monotonic() + AF_SECONDS
        except (gp.GPhoto2Error, RuntimeError) as err:
            self.on_status(f"Focus failed: {err}. Check the lens is set to AF.")

    def _capture(self):
        self._release_af()
        self.on_status("Taking photo...")
        try:
            first = self.camera.capture(gp.GP_CAPTURE_IMAGE)
            files = [first]
            # With RAW + JPEG the camera reports the second file as an event.
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                kind, data = self.camera.wait_for_event(100)
                if kind == gp.GP_EVENT_FILE_ADDED:
                    files.append(data)
            os.makedirs(SAVE_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            saved = []
            for f in files:
                ext = os.path.splitext(f.name)[1].upper() or ".JPG"
                dest = os.path.join(SAVE_DIR, f"IMG_{stamp}{ext}")
                n = 2
                while os.path.exists(dest):
                    dest = os.path.join(SAVE_DIR, f"IMG_{stamp}_{n}{ext}")
                    n += 1
                self.camera.file_get(f.folder, f.name, gp.GP_FILE_TYPE_NORMAL).save(dest)
                saved.append(dest)
            names = ", ".join(os.path.basename(p) for p in saved)
            self.on_status(f"Saved {names} to Pictures/5D Mark III")
            self.on_event(("saved", saved))
        except gp.GPhoto2Error as err:
            self.on_status(f"Could not take the photo: {err}. "
                           "If the lens is on AF, the camera may not have been able to focus.")
        # Live view restarts unmagnified after a shot; put the zoom back.
        level, fx, fy = self.zoom_state
        if level != 1:
            try:
                self._zoom(level, fx, fy)
            except (gp.GPhoto2Error, RuntimeError):
                pass

    def _read_settings(self):
        info = {}
        try:
            info["mode"] = self.camera.get_single_config("autoexposuremode").get_value()
        except gp.GPhoto2Error:
            info["mode"] = "?"
        for name, _ in EXPOSURE_SETTINGS:
            try:
                w = self.camera.get_single_config(name)
                choices = [w.get_choice(i) for i in range(w.count_choices())]
                info[name] = (w.get_value(), choices)
            except gp.GPhoto2Error:
                info[name] = ("?", [])
        self.on_event(("settings", info))

    def _change_setting(self, name, value):
        label = dict(EXPOSURE_SETTINGS).get(name, name)
        try:
            self._set(name, value)
            self.on_status(f"{label} set to {value}.")
        except (gp.GPhoto2Error, RuntimeError) as err:
            self.on_status(f"Could not set {label} to {value}: {err}")
        self._read_settings()

    def run(self):
        self.af_release_at = None
        try:
            self.on_status("Connecting to camera...")
            self.camera = self._open_camera()
            self.on_status("Live view running. Click on the picture to focus there.")
            errors = 0
            while not self.stop_event.is_set():
                try:
                    while True:
                        cmd = self.commands.get_nowait()
                        if cmd[0] == "focus":
                            self._focus(cmd[1], cmd[2])
                        elif cmd[0] == "zoom":
                            self._zoom(cmd[1], cmd[2], cmd[3])
                        elif cmd[0] == "nudge":
                            with self.nudge_lock:
                                self.pending_nudges -= 1
                            self._nudge(cmd[1])
                        elif cmd[0] == "capture":
                            self._capture()
                        elif cmd[0] == "get_settings":
                            self._read_settings()
                        elif cmd[0] == "set_setting":
                            self._change_setting(cmd[1], cmd[2])
                except queue.Empty:
                    pass
                if self.af_release_at is not None and time.monotonic() >= self.af_release_at:
                    self._release_af()
                    self.on_status("Focus done. Click anywhere to refocus.")
                try:
                    self._grab_frame()
                    errors = 0
                except gp.GPhoto2Error as err:
                    errors += 1
                    if errors > 20:
                        raise RuntimeError(f"Lost connection to the camera: {err}") from err
                    time.sleep(0.1)
        except Exception as exc:  # report to the window instead of dying silently
            self.on_status(str(exc))
        finally:
            if self.camera is not None:
                self._release_af()
                try:
                    self._set("eoszoom", "1")
                except (gp.GPhoto2Error, RuntimeError):
                    pass
                try:
                    self._set("viewfinder", 0)  # turn live view off on the camera
                except (gp.GPhoto2Error, RuntimeError):
                    pass
                try:
                    self.camera.exit()
                except gp.GPhoto2Error:
                    pass


def frame_top_left(fx, fy):
    """Camera coordinates of the 5x frame's top-left corner, centred on fx, fy."""
    x = min(max(fx * COORD_W - ZOOM_BOX_W / 2, 0), COORD_W - ZOOM_BOX_W)
    y = min(max(fy * COORD_H - ZOOM_BOX_H / 2, 0), COORD_H - ZOOM_BOX_H)
    return int(x), int(y)


def frame_center(fx, fy):
    """Where the camera actually centres the frame (0..1), after edge clamping."""
    x, y = frame_top_left(fx, fy)
    return (x + ZOOM_BOX_W / 2) / COORD_W, (y + ZOOM_BOX_H / 2) / COORD_H


class LiveViewWindow:
    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.title("5D Mark III Live View")
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.canvas = tk.Canvas(self.win, width=DISPLAY_W, height=DISPLAY_H, bg="black",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.marker = None
        self.photo = None
        self.img_size = (DISPLAY_W, DISPLAY_H)

        self.zoom = 1
        self.focus_point = (0.5, 0.5)  # last focus point, as 0..1 of the full frame

        controls = tk.Frame(self.win)
        controls.pack(fill="x", padx=8, pady=(6, 0))

        tk.Label(controls, text="Focus:").pack(side="left")
        for label, step in (("<<<", "Near 3"), ("<<", "Near 2"), ("<", "Near 1")):
            tk.Button(controls, text=label, width=3,
                      command=lambda s=step: self.worker.nudge_focus(s)).pack(side="left")
        tk.Label(controls, text="near | far").pack(side="left", padx=4)
        for label, step in ((">", "Far 1"), (">>", "Far 2"), (">>>", "Far 3")):
            tk.Button(controls, text=label, width=3,
                      command=lambda s=step: self.worker.nudge_focus(s)).pack(side="left")

        self.zoom_buttons = {}
        for level in (10, 5, 1):
            btn = tk.Button(controls, text=f"{level}x", width=3,
                            command=lambda lv=level: self.set_zoom(lv))
            btn.pack(side="right")
            self.zoom_buttons[level] = btn
        tk.Label(controls, text="Zoom:").pack(side="right")

        actions = tk.Frame(self.win)
        actions.pack(fill="x", padx=8, pady=(6, 0))
        tk.Button(actions, text="Take Photo", width=12, font=("Helvetica", 13, "bold"),
                  command=self.take_photo).pack(side="left")
        tk.Button(actions, text="ISO / Aperture / Shutter...",
                  command=self.open_settings).pack(side="left", padx=6)
        tk.Button(actions, text="Open Photos Folder",
                  command=self.open_folder).pack(side="right")
        tk.Button(actions, text="Upload Settings...",
                  command=self.open_upload_settings).pack(side="right", padx=6)

        peak_row = tk.Frame(self.win)
        peak_row.pack(fill="x", padx=8, pady=(6, 0))
        self.peaking_on = tk.BooleanVar(value=False)
        tk.Checkbutton(peak_row, text="Focus peaking (P)", variable=self.peaking_on,
                       command=self._reset_best).pack(side="left")
        tk.Label(peak_row, text="Sensitivity:").pack(side="left", padx=(8, 2))
        self.peak_sens = ttk.Combobox(peak_row, state="readonly", width=7,
                                      values=list(peaking.SENSITIVITY))
        self.peak_sens.set("Medium")
        self.peak_sens.pack(side="left")
        self.score_lbl = tk.Label(peak_row, text="Sharpness: -", font=("Menlo", 13, "bold"),
                                  width=34, anchor="w")
        self.score_lbl.pack(side="left", padx=(14, 4))
        self.score_bar = tk.Canvas(peak_row, width=140, height=14, bg="#dddddd",
                                   highlightthickness=0)
        self.score_bar.pack(side="left")
        self.score_fill = self.score_bar.create_rectangle(0, 0, 0, 14, fill="#27ae60", width=0)
        tk.Button(peak_row, text="Reset best", command=self._reset_best).pack(side="left", padx=6)

        self.status = tk.Label(self.win, text="Starting...", anchor="w")
        self.status.pack(fill="x", padx=8, pady=6)
        self.upload_status = tk.Label(self.win, text="", anchor="w", fg="#2e86de")
        self.upload_status.pack(fill="x", padx=8)
        tk.Label(self.win, fg="#888888", anchor="w", font=("Helvetica", 10), justify="left",
                 text="Click the picture to autofocus there.  Space = take photo.  P = peaking.\n"
                      "Focus keys:  ;  nearer   '  farther   (small)    "
                      "Cmd+; / Cmd+'  medium    Ctrl+; / Ctrl+'  big.\n"
                      "For pin-sharp focus: zoom 5x or 10x, then step focus until Sharpness "
                      "reaches its best (the bar fills up)."
                 ).pack(fill="x", padx=8, pady=(2, 6))
        self.win.bind("<space>", lambda e: self.take_photo())
        for keys, step in (("semicolon", "Near 1"), ("apostrophe", "Far 1"),
                           ("Command-semicolon", "Near 2"), ("Command-apostrophe", "Far 2"),
                           ("Control-semicolon", "Near 3"), ("Control-apostrophe", "Far 3")):
            self.win.bind(f"<{keys}>", lambda e, s=step: self._key_nudge(s))
        self.win.bind("<p>", lambda e: (self.peaking_on.set(not self.peaking_on.get()), self._reset_best()))
        self.upload_window = None
        self.score_avg = None
        self.score_best = 0.0
        self.score_settle_until = 0.0
        self.latest_upload_status = None
        self.uploader = uploads.Uploader(self._upload_status_from_thread)
        self.uploader.start()
        self.settings_window = None
        self._show_zoom()

        self.canvas.bind("<Button-1>", self.on_click)

        self.closed = False
        self.latest_frame = None
        self.latest_status = None
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.worker = CameraWorker(self._frame_from_worker, self._status_from_worker,
                                   self.events.put)
        self.worker.start()
        self._pump()

    # Worker callbacks run on the camera thread: just stash the data.
    def _frame_from_worker(self, img):
        with self.lock:
            self.latest_frame = img

    def _status_from_worker(self, text):
        with self.lock:
            self.latest_status = text

    def _upload_status_from_thread(self, text):
        with self.lock:
            self.latest_upload_status = text

    def _pump(self):
        """Move frames/status from the camera thread into Tk (UI thread only)."""
        with self.lock:
            img, self.latest_frame = self.latest_frame, None
            status, self.latest_status = self.latest_status, None
            up_status, self.latest_upload_status = self.latest_upload_status, None
        if up_status is not None:
            self.upload_status.config(text=up_status)
        if img is not None:
            magnified = img.size == (ZOOM_BOX_W, ZOOM_BOX_H)
            self._update_score(img, magnified)
            if self.zoom == 10 and magnified:
                w, h = img.size
                img = img.crop((w // 4, h // 4, w - w // 4, h - h // 4))
            if img.size != (DISPLAY_W, DISPLAY_H):
                img = img.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
            if self.peaking_on.get():
                # 10x is upscaled, which softens edges; lower the bar to match.
                scale = 0.6 if (self.zoom == 10 and magnified) else 1.0
                img = peaking.overlay(img, self.peak_sens.get(), scale)
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfig(self.image_item, image=self.photo)
            if self.marker:
                self.canvas.tag_raise(self.marker)
        if status is not None:
            self.status.config(text=status)
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind == "settings" and self.settings_window is not None:
                self.settings_window.show(data)
            elif kind == "saved":
                if self.uploader.enabled_destinations():
                    self.upload_status.config(text="Uploading...")
                    self.uploader.upload(data)
        if not self.closed and (self.worker.is_alive() or status is not None):
            self.win.after(30, self._pump)

    def _update_score(self, img, magnified):
        """Sharpness of the focus area. Magnified frames are native sensor pixels,
        so 5x and 10x give the same (and much more precise) score."""
        w, h = img.size
        if magnified:
            box = (w / 4, h / 4, w * 3 / 4, h * 3 / 4)  # = what 10x shows
        else:
            cx, cy = frame_center(*self.focus_point)
            bw, bh = w / 5, h / 5
            box = (cx * w - bw / 2, cy * h - bh / 2, cx * w + bw / 2, cy * h + bh / 2)
        score = peaking.sharpness(img, box)
        if time.monotonic() < self.score_settle_until:
            # The camera sends a few garbled frames while it switches zoom or
            # moves the focus frame; don't let them set a false "best".
            self.score_lbl.config(text="Sharpness  settling...", fg="#888888")
            return
        if self.score_avg and score > 3 * self.score_avg + 5:
            return  # a one-frame spike (corrupt frame), not real detail
        # Smooth out frame-to-frame noise so the number is readable.
        self.score_avg = score if self.score_avg is None else 0.6 * self.score_avg + 0.4 * score
        self.score_best = max(self.score_best, self.score_avg)
        ratio = self.score_avg / self.score_best if self.score_best else 0
        color = "#27ae60" if ratio >= 0.97 else "#f39c12" if ratio >= 0.85 else "#c0392b"
        where = "zoomed centre" if magnified else "focus box"
        self.score_lbl.config(text=f"Sharpness {self.score_avg:7.1f}  best {self.score_best:7.1f}",
                              fg=color)
        self.score_bar.coords(self.score_fill, 0, 0, 140 * ratio, 14)
        self.score_bar.itemconfig(self.score_fill, fill=color)
        self.score_where = where

    def _reset_best(self):
        self.score_avg = None
        self.score_best = 0.0
        self.score_settle_until = time.monotonic() + 1.0

    def _key_nudge(self, step):
        self.worker.nudge_focus(step)
        return "break"

    def on_click(self, event):
        w, h = self.img_size
        vx, vy = min(max(event.x / w, 0), 1), min(max(event.y / h, 0), 1)
        if self.zoom == 1:
            fx, fy = vx, vy
        else:
            # The magnified view shows 1/zoom of the frame around the frame centre;
            # turn the click into a position on the full frame. The view then
            # re-centres on that spot.
            cx, cy = frame_center(*self.focus_point)
            fx = min(max(cx + (vx - 0.5) / self.zoom, 0), 1)
            fy = min(max(cy + (vy - 0.5) / self.zoom, 0), 1)
        self.focus_point = (fx, fy)
        self._reset_best()
        self._draw_marker()
        self.worker.focus_at(fx, fy)

    def _draw_marker(self):
        """Show the AF box at 1x (it is the whole view's centre when magnified)."""
        if self.marker:
            self.canvas.delete(self.marker)
            self.marker = None
        w, h = self.img_size
        if self.zoom == 1:
            cx, cy = frame_center(*self.focus_point)
            bw, bh = w / 5, h / 5
            x0, y0 = cx * w - bw / 2, cy * h - bh / 2
        else:
            bw, bh = 40, 40  # small crosshair box in the middle of the magnified view
            x0, y0 = w / 2 - bw / 2, h / 2 - bh / 2
        self.marker = self.canvas.create_rectangle(x0, y0, x0 + bw, y0 + bh,
                                                   outline="#2ecc71", width=2)

    def set_zoom(self, level):
        if (level == 1) != (self.zoom == 1):
            self._reset_best()  # 1x and magnified scores are on different scales
        self.zoom = level
        self._show_zoom()
        if self.marker or level != 1:
            self._draw_marker()
        self.worker.set_zoom(level, *self.focus_point)

    def _show_zoom(self):
        for level, btn in self.zoom_buttons.items():
            btn.config(relief="sunken" if level == self.zoom else "raised",
                       fg="#2e86de" if level == self.zoom else "black")

    def take_photo(self):
        self.status.config(text="Taking photo...")
        self.worker.take_photo()

    def open_folder(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        subprocess.run(["open", SAVE_DIR])

    def open_upload_settings(self):
        if self.upload_window is not None and self.upload_window.win.winfo_exists():
            self.upload_window.win.lift()
        else:
            self.upload_window = uploads.UploadSettingsWindow(self.win)

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.win.winfo_exists():
            self.settings_window.win.lift()
        else:
            self.settings_window = SettingsWindow(self.win, self.worker)
        self.worker.request_settings()

    def close(self):
        self.closed = True
        if self.settings_window is not None and self.settings_window.win.winfo_exists():
            self.settings_window.win.destroy()
        self.worker.stop()
        self.worker.join(timeout=3)
        self.win.destroy()


class SettingsWindow:
    """Small window with drop-downs for ISO, aperture and shutter speed."""

    def __init__(self, master, worker):
        self.worker = worker
        self.win = tk.Toplevel(master)
        self.win.title("Exposure Settings")
        self.win.resizable(False, False)
        frame = tk.Frame(self.win, padx=14, pady=12)
        frame.pack()

        tk.Label(frame, text="Mode dial:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.mode_lbl = tk.Label(frame, text="reading...", font=("Helvetica", 13, "bold"))
        self.mode_lbl.grid(row=0, column=1, sticky="w", pady=(0, 8))

        self.boxes, self.notes = {}, {}
        for row, (name, label) in enumerate(EXPOSURE_SETTINGS, start=1):
            tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            box = ttk.Combobox(frame, state="disabled", width=10)
            box.grid(row=row, column=1, sticky="w", pady=3)
            box.bind("<<ComboboxSelected>>", lambda e, n=name: self._changed(n))
            note = tk.Label(frame, text="", fg="#888888", font=("Helvetica", 10))
            note.grid(row=row, column=2, sticky="w", padx=(8, 0))
            self.boxes[name], self.notes[name] = box, note

        tk.Label(frame, fg="#888888", font=("Helvetica", 10), justify="left",
                 text="The camera's mode dial decides what can be changed here:\n"
                      "M = all three, Av = aperture, Tv = shutter speed. ISO works in any mode."
                 ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 6))
        tk.Button(frame, text="Refresh", command=worker.request_settings).grid(
            row=5, column=0, sticky="w")
        tk.Button(frame, text="Close", command=self.win.destroy).grid(row=5, column=2, sticky="e")

    def show(self, info):
        if not self.win.winfo_exists():
            return
        mode = info.get("mode", "?")
        self.mode_lbl.config(text=MODE_NAMES.get(mode, mode))
        for name, _ in EXPOSURE_SETTINGS:
            value, choices = info.get(name, ("?", []))
            box, note = self.boxes[name], self.notes[name]
            box.config(values=choices)
            box.set(value)
            if len(choices) > 1:
                box.config(state="readonly")
                note.config(text="")
            else:
                box.config(state="disabled")
                note.config(text="set automatically in this mode")

    def _changed(self, name):
        self.worker.change_setting(name, self.boxes[name].get())
