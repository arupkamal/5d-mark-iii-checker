# 5D Mark III Checker

A macOS desktop app for tethered shooting with a Canon EOS 5D Mark III over USB.

- **Connection check**: shows whether the camera is plugged in and switched on (`--cli` and `--watch` modes for the terminal).
- **Live view** at up to ~20 fps, with **click-to-focus**: click anywhere on the picture to autofocus there.
- **Zoom** 1x / 5x / 10x around the focus point.
- **Manual focus steps**: small, medium and big, with buttons or keys (`;` nearer, `'` farther; add `Cmd` for medium, `Ctrl` for big).
- **Focus peaking** (red edge highlight) and a numeric **sharpness score** with a "best so far" bar for pin-sharp manual focus.
- **Take photo** (button or space bar): full-resolution files saved to `~/Pictures/5D Mark III`.
- **ISO / aperture / shutter speed** controls (what can be changed depends on the camera's mode dial).
- **Uploads** of every photo to Amazon S3, Google Cloud Storage and/or FTP/FTPS.

## Setup

```bash
brew install libgphoto2 pkgconf
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

Double-click `Start Camera App.command`, or:

```bash
.venv/bin/python camera_check.py           # app window
python3 camera_check.py --cli              # one-off connection check (no extra packages needed)
python3 camera_check.py --watch            # print when the camera connects / disconnects
```

## Camera settings

- Lens switch on **AF** (needed for click-to-focus and the focus step keys).
- Live View AF method: **FlexiZone – Single** (in Face+Tracking mode the camera picks the point itself).
- Consider setting **Auto power off** to *Disable*, otherwise the camera drops off USB after a minute.

## Notes

- macOS's camera service (`ptpcamerad`) grabs the camera as soon as it is connected. The app stops it so it can talk to the camera; macOS restarts it automatically.
- Over USB the camera's 10x live view is identical to 5x, so 10x is a crop of the native-resolution 5x view.
- The camera sends most live view frames with a corrupted 32-byte JPEG header; the app repairs them using the header of a good frame.
- Upload settings are stored in `~/Library/Application Support/5D Mark III Checker/uploads.json`; the S3 secret key and FTP password are stored in the macOS Keychain.

## Technical documentation

See [docs/TECHNICAL.md](docs/TECHNICAL.md) for the architecture, threading model, camera protocol details and quirks, the focus peaking and sharpness algorithms, the upload subsystem, testing, troubleshooting and how to extend the app.

## Files

| File | What it does |
|---|---|
| `camera_check.py` | Main window and USB connection check |
| `liveview.py` | Live view, focus, zoom, capture and exposure settings (via libgphoto2) |
| `peaking.py` | Focus peaking overlay and sharpness score |
| `uploads.py` | S3 / Google Cloud Storage / FTP uploads and their settings window |
