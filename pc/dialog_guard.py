"""Close nuisance dialogs that pop up while we work.

WPS/LibreOffice keep raising 「正在等待打印机连接…」 during document work and the
dialog steals focus (and the user's attention). This watcher polls for windows
whose title matches known nuisances and closes them with WM_CLOSE, logging each
window it closes.

Usage:  python dialog_guard.py [seconds]
"""

import ctypes
import ctypes.wintypes as wintypes
import sys
import time

u = ctypes.windll.user32
u.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]

WM_CLOSE = 0x0010
NEEDLES = ("打印机连接", "正在等待打印机", "等待打印机", "打印机")

LOG = r"C:\Users\35629\OllamaStart\pc\dialog_guard.log"


def visible_windows():
    rows = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        if u.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                rows.append((int(hwnd), buf.value))
        return True

    u.EnumWindows(proc(cb), 0)
    return rows


def log(line):
    stamp = time.strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {line}\n")


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    deadline = time.time() + seconds
    log(f"guard start ({seconds}s)")
    closed = 0
    while time.time() < deadline:
        for hwnd, title in visible_windows():
            if any(n in title for n in NEEDLES):
                u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                closed += 1
                log(f"closed hwnd={hwnd} {title!r}")
                time.sleep(0.4)
        time.sleep(0.7)
    log(f"guard end, closed {closed}")


if __name__ == "__main__":
    main()
