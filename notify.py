"""
"Build ready" popup: a small white box in the top-left corner of the screen that fades in, stays a few
seconds and fades out. It stays on top of other windows (games in exclusive fullscreen can still cover it)
and doesn't take the keyboard away from what you're doing. Click it to close it early.

It runs as its own little process, so it works the same whether the app is in its own window or a browser.
If this Python has no Tk (tkinter), the popup is simply skipped.
"""
from __future__ import annotations

import os
import subprocess
import sys

POPUP_CODE = r'''
import sys, tkinter as tk
title, body = sys.argv[1], sys.argv[2]
root = tk.Tk()
root.withdraw()
root.overrideredirect(True)
try:
    root.attributes("-topmost", True)
except Exception:
    pass
if sys.platform.startswith("linux"):
    try:
        root.attributes("-type", "notification")
    except Exception:
        pass
frame = tk.Frame(root, bg="#ffffff", highlightthickness=1, highlightbackground="#d0d0d0", padx=16, pady=12)
frame.pack()
tk.Label(frame, text=title, bg="#ffffff", fg="#111111", font=("Segoe UI", 11, "bold"), anchor="w",
         justify="left").pack(anchor="w")
if body:
    tk.Label(frame, text=body, bg="#ffffff", fg="#444444", font=("Segoe UI", 10), anchor="w", justify="left",
             wraplength=300).pack(anchor="w", pady=(2, 0))
root.update_idletasks()
root.geometry("+16+16")
if sys.platform == "win32":
    # Show without taking focus from the game or app you're in, keep off the taskbar, stay on top.
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x8, 0x80, 0x08000000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception:
        pass
try:
    root.attributes("-alpha", 0.0)
except Exception:
    pass
root.deiconify()
root.lift()
try:
    root.attributes("-topmost", True)
except Exception:
    pass

def close(_e=None):
    root.destroy()

for w in [root, frame] + list(frame.winfo_children()):
    w.bind("<Button-1>", close)

def fade(alpha, step, then):
    alpha = max(0.0, min(1.0, alpha + step))
    try:
        root.attributes("-alpha", alpha)
    except Exception:
        then()
        return
    if (step > 0 and alpha < 1.0) or (step < 0 and alpha > 0.0):
        root.after(20, fade, alpha, step, then)
    else:
        then()

def keep_on_top():
    try:
        root.attributes("-topmost", True)
        root.lift()
    except Exception:
        pass
    root.after(500, keep_on_top)

keep_on_top()
fade(0.0, 0.08, lambda: root.after(int(sys.argv[3]), lambda: fade(1.0, -0.06, close)))
root.after(int(sys.argv[3]) + 4000, close)    # safety net
root.mainloop()
'''


def _python_for_popup() -> str:
    exe = sys.executable or "python3"
    if sys.platform == "win32":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def show_popup(title: str, body: str = "", seconds: float = 5.0) -> bool:
    """Show the popup without waiting for it. Returns False if it couldn't be started."""
    if os.environ.get("WYNN_HELPER_NO_POPUP"):
        return False
    try:
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW: no console flash
        else:
            kw["start_new_session"] = True
        subprocess.Popen([_python_for_popup(), "-c", POPUP_CODE, title, body, str(int(seconds * 1000))], **kw)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    show_popup("Wynn Build Helper", "Your build is ready.")
