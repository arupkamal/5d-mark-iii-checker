"""Upload saved photos to Amazon S3, Google Cloud Storage and/or an FTP server.

Settings live in ~/Library/Application Support/5D Mark III Checker/uploads.json.
Secrets (S3 secret key, FTP password) are kept in the macOS Keychain, not in that file.
"""

import ftplib
import json
import os
import posixpath
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import keyring

APP_NAME = "5D Mark III Checker"
CONFIG_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
CONFIG_PATH = os.path.join(CONFIG_DIR, "uploads.json")
KEYCHAIN_SERVICE = APP_NAME

DEFAULTS = {
    "s3": {"enabled": False, "bucket": "", "prefix": "", "region": "",
           "access_key_id": "", "endpoint_url": ""},
    "gcs": {"enabled": False, "bucket": "", "prefix": "", "credentials_file": ""},
    "ftp": {"enabled": False, "host": "", "port": 21, "username": "",
            "remote_dir": "", "tls": True},
}
SECRETS = {"s3": "s3_secret_access_key", "ftp": "ftp_password"}
LABELS = {"s3": "Amazon S3", "gcs": "Google Cloud Storage", "ftp": "FTP"}


# ---- settings -------------------------------------------------------------
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        for dest in cfg:
            cfg[dest].update(saved.get(dest, {}))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_secret(dest):
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, SECRETS[dest]) or ""
    except Exception:
        return ""


def set_secret(dest, value):
    if value:
        keyring.set_password(KEYCHAIN_SERVICE, SECRETS[dest], value)
    else:
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, SECRETS[dest])
        except Exception:
            pass


def _remote_name(prefix, path):
    name = os.path.basename(path)
    prefix = prefix.strip("/")
    return f"{prefix}/{name}" if prefix else name


# ---- Amazon S3 ------------------------------------------------------------
def _s3_client(c):
    import boto3
    kwargs = {}
    if c.get("region"):
        kwargs["region_name"] = c["region"]
    if c.get("endpoint_url"):
        kwargs["endpoint_url"] = c["endpoint_url"]
    if c.get("access_key_id"):
        # Otherwise boto3 uses ~/.aws/credentials or environment variables.
        kwargs["aws_access_key_id"] = c["access_key_id"]
        kwargs["aws_secret_access_key"] = get_secret("s3")
    return boto3.client("s3", **kwargs)


def upload_s3(c, path):
    key = _remote_name(c["prefix"], path)
    _s3_client(c).upload_file(path, c["bucket"], key)
    return f"s3://{c['bucket']}/{key}"


def test_s3(c):
    _s3_client(c).head_bucket(Bucket=c["bucket"])
    return f"Connected to bucket {c['bucket']}."


# ---- Google Cloud Storage -------------------------------------------------
def _gcs_bucket(c):
    from google.cloud import storage
    if c.get("credentials_file"):
        client = storage.Client.from_service_account_json(os.path.expanduser(c["credentials_file"]))
    else:
        client = storage.Client()  # gcloud application-default credentials
    return client.bucket(c["bucket"])


def upload_gcs(c, path):
    key = _remote_name(c["prefix"], path)
    _gcs_bucket(c).blob(key).upload_from_filename(path)
    return f"gs://{c['bucket']}/{key}"


def test_gcs(c):
    bucket = _gcs_bucket(c)
    list(bucket.list_blobs(max_results=1))
    return f"Connected to bucket {c['bucket']}."


# ---- FTP ------------------------------------------------------------------
def _ftp_connect(c):
    if c.get("tls"):
        # Verify the server's certificate (ftplib doesn't by default).
        import ssl
        import certifi
        ftp = ftplib.FTP_TLS(context=ssl.create_default_context(cafile=certifi.where()))
    else:
        ftp = ftplib.FTP()
    ftp.connect(c["host"], int(c.get("port") or 21), timeout=30)
    ftp.login(c.get("username") or "anonymous", get_secret("ftp"))
    if c.get("tls"):
        ftp.prot_p()  # encrypt the file transfer too, not just the login
    return ftp


def _ftp_cd(ftp, remote_dir, create):
    if not remote_dir:
        return
    if remote_dir.startswith("/"):
        ftp.cwd("/")
    for part in [p for p in remote_dir.split("/") if p]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            if not create:
                raise
            ftp.mkd(part)
            ftp.cwd(part)


def upload_ftp(c, path):
    ftp = _ftp_connect(c)
    try:
        _ftp_cd(ftp, c.get("remote_dir", ""), create=True)
        name = os.path.basename(path)
        with open(path, "rb") as f:
            ftp.storbinary(f"STOR {name}", f)
        return f"ftp://{c['host']}{posixpath.join('/', c.get('remote_dir', '').strip('/'), name)}"
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def test_ftp(c):
    ftp = _ftp_connect(c)
    try:
        _ftp_cd(ftp, c.get("remote_dir", ""), create=False)
        return f"Logged in to {c['host']}" + (f", folder {c['remote_dir']} exists." if c.get("remote_dir") else ".")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


UPLOADERS = {"s3": upload_s3, "gcs": upload_gcs, "ftp": upload_ftp}
TESTERS = {"s3": test_s3, "gcs": test_gcs, "ftp": test_ftp}


def check_required(dest, c):
    if dest in ("s3", "gcs") and not c.get("bucket"):
        return "Enter a bucket name."
    if dest == "ftp" and not c.get("host"):
        return "Enter the FTP server address."
    return None


class Uploader(threading.Thread):
    """Uploads photos in the background so live view keeps running."""

    def __init__(self, on_status):
        super().__init__(daemon=True)
        self.on_status = on_status
        self.jobs = queue.Queue()

    def enabled_destinations(self):
        cfg = load_config()
        return [d for d in UPLOADERS if cfg[d]["enabled"]]

    def upload(self, paths):
        if self.enabled_destinations():
            self.jobs.put(list(paths))

    def run(self):
        while True:
            paths = self.jobs.get()
            cfg = load_config()  # pick up changes made since the photo was taken
            results = []
            for dest in [d for d in UPLOADERS if cfg[d]["enabled"]]:
                problem = check_required(dest, cfg[dest])
                if problem:
                    results.append(f"{LABELS[dest]} failed ({problem})")
                    continue
                self.on_status(f"Uploading to {LABELS[dest]}...")
                try:
                    for p in paths:
                        UPLOADERS[dest](cfg[dest], p)
                    results.append(f"{LABELS[dest]} OK")
                except Exception as err:
                    results.append(f"{LABELS[dest]} failed ({short_error(err)})")
            names = ", ".join(os.path.basename(p) for p in paths)
            self.on_status(f"Upload of {names}: " + "; ".join(results))


def short_error(err):
    text = str(err).strip() or type(err).__name__
    return text if len(text) < 160 else text[:157] + "..."


# ---- settings window ------------------------------------------------------
class UploadSettingsWindow:
    FIELDS = {
        "s3": [("bucket", "Bucket"), ("prefix", "Folder / prefix (optional)"),
               ("region", "Region (e.g. us-east-1)"), ("access_key_id", "Access key ID"),
               ("secret", "Secret access key"), ("endpoint_url", "Endpoint URL (optional)")],
        "gcs": [("bucket", "Bucket"), ("prefix", "Folder / prefix (optional)"),
                ("credentials_file", "Service account key file")],
        "ftp": [("host", "Server"), ("port", "Port"), ("username", "Username"),
                ("secret", "Password"), ("remote_dir", "Remote folder (optional)")],
    }
    HINTS = {
        "s3": "Leave the access key empty to use ~/.aws/credentials (aws configure).",
        "gcs": "Leave the key file empty to use gcloud application-default credentials.",
        "ftp": "Use FTPS (TLS) unless your server only supports plain FTP: plain FTP "
               "sends your password unencrypted.",
    }

    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.title("Upload Photos To")
        self.win.resizable(False, False)
        self.cfg = load_config()
        self.vars = {}
        notebook = ttk.Notebook(self.win)
        notebook.pack(padx=10, pady=10)
        for dest in ("s3", "gcs", "ftp"):
            notebook.add(self._build_tab(notebook, dest), text=LABELS[dest])
        bottom = tk.Frame(self.win)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(bottom, fg="#888888", font=("Helvetica", 10),
                 text="Photos are always saved to Pictures/5D Mark III too.").pack(side="left")
        tk.Button(bottom, text="Save", width=8, command=self.save_and_close).pack(side="right")
        tk.Button(bottom, text="Cancel", width=8, command=self.win.destroy).pack(side="right", padx=4)

    def _build_tab(self, parent, dest):
        c = self.cfg[dest]
        frame = tk.Frame(parent, padx=12, pady=10)
        v = self.vars[dest] = {}
        v["enabled"] = tk.BooleanVar(value=c["enabled"])
        tk.Checkbutton(frame, text=f"Upload every photo to {LABELS[dest]}",
                       variable=v["enabled"]).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row = 1
        for key, label in self.FIELDS[dest]:
            tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            value = get_secret(dest) if key == "secret" else str(c.get(key, ""))
            v[key] = tk.StringVar(value=value)
            entry = tk.Entry(frame, textvariable=v[key], width=34, show="•" if key == "secret" else "")
            entry.grid(row=row, column=1, sticky="w", pady=2)
            if key == "credentials_file":
                tk.Button(frame, text="Choose...",
                          command=lambda var=v[key]: self._choose_file(var)).grid(row=row, column=2, padx=4)
            row += 1
        if dest == "ftp":
            v["tls"] = tk.BooleanVar(value=c.get("tls", True))
            tk.Checkbutton(frame, text="Use FTPS (encrypted)", variable=v["tls"]).grid(
                row=row, column=1, sticky="w")
            row += 1
        tk.Label(frame, text=self.HINTS[dest], fg="#888888", font=("Helvetica", 10),
                 wraplength=420, justify="left").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 4))
        row += 1
        result = tk.Label(frame, text="", wraplength=420, justify="left")
        tk.Button(frame, text="Test connection",
                  command=lambda: self._test(dest, result)).grid(row=row, column=0, sticky="w", pady=(4, 0))
        result.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return frame

    def _choose_file(self, var):
        path = filedialog.askopenfilename(parent=self.win, title="Service account key (JSON)",
                                          filetypes=[("JSON key", "*.json"), ("All files", "*")])
        if path:
            var.set(path)

    def _collect(self):
        for dest, v in self.vars.items():
            for key, var in v.items():
                if key == "secret":
                    continue
                val = var.get()
                if key == "port":
                    try:
                        val = int(val)
                    except ValueError:
                        val = 21
                self.cfg[dest][key] = val.strip() if isinstance(val, str) else val
        return self.cfg

    def _store(self):
        cfg = self._collect()
        save_config(cfg)
        for dest in SECRETS:
            set_secret(dest, self.vars[dest]["secret"].get())
        return cfg

    def _test(self, dest, label):
        cfg = self._store()  # test exactly what will be used
        problem = check_required(dest, cfg[dest])
        if problem:
            label.config(text=problem, fg="#c0392b")
            return
        label.config(text="Testing...", fg="black")
        result = {}

        def work():  # runs in a background thread: no Tk calls here
            try:
                result["msg"], result["color"] = TESTERS[dest](cfg[dest]), "#27ae60"
            except Exception as err:
                result["msg"], result["color"] = f"Failed: {short_error(err)}", "#c0392b"

        def check():
            if not label.winfo_exists():
                return
            if "msg" in result:
                label.config(text=result["msg"], fg=result["color"])
            else:
                self.win.after(100, check)

        threading.Thread(target=work, daemon=True).start()
        check()

    def save_and_close(self):
        self._store()
        self.win.destroy()
