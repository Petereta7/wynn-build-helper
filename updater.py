"""
Updates for Wynn Build Helper, straight from its GitHub repository.

The app checks the repository's version.json when it opens. If there's a newer version it asks you first,
then downloads that version from the same repository and copies it over this folder. It never touches:

  * api_key.txt (your Claude API key)
  * wynn_app_config.json (your settings)
  * wynn_data/ (game data and the builds remembered from your scans)

Files it replaces are kept in _previous_version/ until the next update, so an update can be undone by
copying them back. It only ever downloads from the repository named below, over HTTPS.

From a terminal:  python3 updater.py          (check, and ask before installing)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- where updates come from (the only place they're ever downloaded from)
OWNER = "Petereta7"         # GitHub user name
REPO = "wynn-build-helper"
BRANCH = "main"

VERSION_FILE = os.path.join(HERE, "version.json")
# Never overwritten or removed by an update (paths relative to the app folder, "/" separated).
PROTECTED = ("api_key.txt", "wynn_app_config.json", "wynn_data/", "_previous_version/", "app.log", ".window_ready")
BACKUP_DIR = os.path.join(HERE, "_previous_version")
MAX_DOWNLOAD = 60 * 1024 * 1024


def configured() -> bool:
    return bool(OWNER) and OWNER != "OWNER" and bool(REPO)


def _ctx():
    try:
        import certifi  # optional; fixes certificate errors on some Mac Python installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, limit: int, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wynn-build-helper-updater",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is bigger than expected")
    return data


def _vtuple(v: str):
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def current_version() -> dict:
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"version": "0.0.0", "notes": ""}


def check() -> dict:
    """{"configured", "current", "latest", "available", "notes", "error"}"""
    cur = current_version().get("version", "0.0.0")
    out = {"configured": configured(), "current": cur, "latest": None, "available": False, "notes": "", "error": None}
    if not configured():
        return out
    try:
        url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/version.json?t={int(time.time())}"
        info = json.loads(_get(url, 64 * 1024).decode("utf-8"))
        out["latest"] = str(info.get("version", ""))
        out["notes"] = str(info.get("notes", ""))[:2000]
        out["available"] = _vtuple(out["latest"]) > _vtuple(cur)
    except Exception as exc:
        out["error"] = f"Couldn't check for updates ({type(exc).__name__})."
    return out


def _protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)


def install(expected_version: str | None = None, log=print) -> dict:
    """Download the newest version from the repository and copy it over this folder.
    Returns {"ok", "version", "changed", "error"}."""
    if not configured():
        return {"ok": False, "error": "Updates aren't set up for this copy."}
    try:
        log("Downloading the update...")
        url = f"https://codeload.github.com/{OWNER}/{REPO}/zip/refs/heads/{BRANCH}"
        zf = zipfile.ZipFile(io.BytesIO(_get(url, MAX_DOWNLOAD, timeout=120)))
        names = zf.namelist()
        if not names:
            raise ValueError("the download is empty")
        root = names[0].split("/")[0] + "/"
        files = {}
        for n in names:
            if not n.startswith(root) or n.endswith("/"):
                continue
            rel = n[len(root):]
            parts = rel.replace("\\", "/").split("/")
            if not rel or rel.startswith("/") or ".." in parts or ":" in rel:
                raise ValueError(f"unsafe path in the download: {rel}")
            files[rel] = n
        for must in ("version.json", "wynn_helper.py", "wynn_app.py", "app/index.html"):
            if must not in files:
                raise ValueError(f"the download is missing {must}")
        new_version = json.loads(zf.read(files["version.json"]).decode("utf-8")).get("version", "")
        if expected_version and _vtuple(new_version) < _vtuple(expected_version):
            raise ValueError("the download is older than the version announced")
        # Back up what gets replaced, then write each file next to its target and swap it in.
        if os.path.isdir(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        changed = 0
        for rel, n in sorted(files.items()):
            if _protected(rel):
                continue
            data = zf.read(n)
            dest = os.path.join(HERE, *rel.split("/"))
            if os.path.exists(dest):
                with open(dest, "rb") as fh:
                    if fh.read() == data:
                        continue
                bak = os.path.join(BACKUP_DIR, *rel.split("/"))
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(dest, bak)
            os.makedirs(os.path.dirname(dest) or HERE, exist_ok=True)
            tmp = dest + ".updating"
            with open(tmp, "wb") as fh:
                fh.write(data)
            if rel.endswith(".command"):
                os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
            changed += 1
        log(f"Updated to version {new_version} ({changed} files changed).")
        return {"ok": True, "version": new_version, "changed": changed, "error": None}
    except Exception as exc:
        return {"ok": False, "error": f"The update didn't install: {exc}. Nothing important was changed; "
                                      "you can try again later."}


def restart_app():
    """Start the app again (after an update) and let the caller exit."""
    import subprocess
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    args = [sys.executable, os.path.join(HERE, "wynn_app.py")] + [a for a in sys.argv[1:]]
    subprocess.Popen(args, cwd=HERE, **kw)


def main():
    info = check()
    if not info["configured"]:
        print("Updates aren't set up for this copy.")
        return
    if info["error"]:
        print(info["error"])
        return
    print(f"You have version {info['current']}; the newest is {info['latest']}.")
    if not info["available"]:
        print("You're up to date.")
        return
    if info["notes"]:
        print("What's new:\n" + info["notes"])
    if input("Install the update now? [y/N]: ").strip().lower() in ("y", "yes"):
        res = install(info["latest"])
        print("Done. Open the app again to use the new version." if res["ok"] else res["error"])


if __name__ == "__main__":
    main()
