"""pc — a small, auditable remote-hands tool for this machine.

One file, standard library plus Pillow, no plugin, no daemon. Every verb is
explicit so nothing happens by accident:

    pc.py windows                 list visible windows (handle, pid, title, rect)
    pc.py shot [--window N] [--out P]      screenshot to a PNG
    pc.py move X Y                move the pointer
    pc.py click X Y [--double] [--right]
    pc.py drag X1 Y1 X2 Y2        press, move, release
    pc.py scroll N                wheel at the pointer (positive = up)
    pc.py type "text"             type text (clipboard paste, so Chinese works)
    pc.py key enter|esc|tab|...   one key
    pc.py hotkey ctrl+s           key combination

Design rules, learned the hard way on this box:
  * every action prints what it did, so the log is the audit trail
  * screenshot first, act second — the caller is expected to look before clicking
  * no window-title guessing: use `windows` and pass the handle explicitly
"""

from __future__ import annotations

import ctypes
import io
import os
import sys
import tempfile
import time

# 被插件当子进程调用时，stdout 会退回系统默认（GBK），中文变乱码、遇到
# U+200B 之类的字符还会直接抛 UnicodeEncodeError。这里强制成 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _enable_dpi_awareness():
    """让进程变成 per-monitor DPI aware。

    ★ 为什么必须有这一段：不做 DPI 感知时，GetWindowRect 返回的是"逻辑像素"，
    而 ImageGrab 截出来的是"物理像素"、SetCursorPos 收的也是物理像素 ——
    在缩放不是 100% 的机器上，截图里的坐标拿去点就会偏。我们自己这台是 100%，
    所以一直没暴露；别人 125%/150% 一装就会"点了没反应"。
    """
    try:
        # -4 = PER_MONITOR_AWARE_V2（Win10 1703+）
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


DPI_MODE = _enable_dpi_awareness()
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF = {
    "left_down": 0x0002, "left_up": 0x0004,
    "right_down": 0x0008, "right_up": 0x0010,
    "middle_down": 0x0020, "middle_up": 0x0040,
    "wheel": 0x0800,
}
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {
    "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "win": 0x5B,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{str(d): ord(str(d)) for d in range(10)},
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(f"SendInput sent {sent}/{n}")


def _mouse(flags, dx=0, dy=0, data=0):
    _send(INPUT(type=INPUT_MOUSE,
                mi=MOUSEINPUT(dx, dy, data, flags, 0, None)))


def _key(vk, up=False):
    _send(INPUT(type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)))


# ---------- verbs ----------

def windows():
    rows = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if not title.value.strip():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rows.append((hwnd, pid.value, r.left, r.top, r.right - r.left, r.bottom - r.top, title.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    rows.sort(key=lambda x: (x[1], x[6]))
    print(f"  {len(rows)} 个可见窗口")
    print("  hwnd        pid    x     y     w     h    title")
    for hwnd, pid, x, y, w, h, title in rows:
        print(f"  {hwnd:<11} {pid:<6} {x:<5} {y:<5} {w:<5} {h:<5} {title[:56]}")
    return rows


def shot(out="shot.png", window=None):
    from PIL import ImageGrab
    box = None
    if window and str(window).isdigit():
        hwnd = int(window)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.4)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        box = (r.left, r.top, r.right, r.bottom)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(out)
    print(f"  saved {out}  {img.size[0]}x{img.size[1]}" + (f"  window={window}" if window else "  (full screen)"))
    return out


def move(x, y):
    user32.SetCursorPos(int(x), int(y))
    print(f"  pointer -> {int(x)},{int(y)}")


def click(x, y, double=False, right=False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    down, up = ("right_down", "right_up") if right else ("left_down", "left_up")
    _mouse(MOUSEEVENTF[down])
    _mouse(MOUSEEVENTF[up])
    if double:
        time.sleep(0.06)
        _mouse(MOUSEEVENTF[down])
        _mouse(MOUSEEVENTF[up])
    print(f"  {'right ' if right else ''}{'double-' if double else ''}click at {int(x)},{int(y)}")



def _proc_name(pid):
    """Executable name for a pid, so two look-alike pets can be told apart."""
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        kernel32.CloseHandle(h)


def _window_info(hwnd):
    from ctypes import wintypes
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {
        "hwnd": int(hwnd), "title": title.value, "pid": int(pid.value),
        "exe": _proc_name(pid.value),
        "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
    }


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW(int(hwnd), buf, 256)
    return buf.value


def _grab_screen(hwnd, path):
    """Grab the window rect off the screen. Requires the window to be visible."""
    from PIL import ImageGrab
    info = _window_info(int(hwnd))
    x, y, w, h = info["rect"]
    if w < 8 or h < 8:
        return None
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
    return path


def _printwindow_raw(hwnd, path):
    out = _printwindow_impl(hwnd, path)
    return out


def _grab_window(hwnd, path):
    """Capture a window's own content even when it is covered by another app.

    Screen-grabbing the window rect only returns whatever is on top; PowerShell/
    console windows sit behind DSH most of the time, so OCR read DSH instead of
    the console. PrintWindow renders the window itself.
    """
    from PIL import Image
    info = _window_info(int(hwnd))
    w, h = info["rect"][2], info["rect"][3]
    if w < 8 or h < 8:
        return None
    hdc = user32.GetWindowDC(int(hwnd))
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    PW_RENDERFULLCONTENT = 0x2
    user32.PrintWindow(int(hwnd), mem, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    img.save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(int(hwnd), hdc)
    return path


def _grab_window_safe(hwnd, path, timeout=4.0):
    """Grab a window without ever hanging the caller.

    PrintWindow blocks forever on console (conhost) windows — that is exactly how
    the 00:12 hang happened. Rules here:
      * console windows: bring to front, grab from the screen;
      * everything else: PrintWindow inside a daemon thread with a timeout, and
        fall back to the screen grab if it does not answer in time.
    """
    cls = _class_name(hwnd)
    if cls.startswith("ConsoleWindowClass") or cls.startswith("CASCADIA"):
        try:
            activate(str(int(hwnd)))
        except Exception:
            pass
        time.sleep(0.12)
        return _grab_screen(hwnd, path)

    import threading
    box = {}

    def work():
        try:
            box["ok"] = _printwindow_impl(hwnd, path)
        except Exception as exc:      # keep the failure, never crash the caller
            box["err"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "err" in box or not box.get("ok"):
        try:
            activate(str(int(hwnd)))
        except Exception:
            pass
        time.sleep(0.12)
        return _grab_screen(hwnd, path)
    return box["ok"]


gdi32 = ctypes.windll.gdi32
kernel32.AttachConsole.argtypes = [ctypes.c_ulong]
kernel32.FreeConsole.argtypes = []
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                 ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def _api_prototypes():
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [ctypes.c_void_p]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


GA_ROOT = 2
user32.WindowFromPoint.restype = ctypes.c_void_p
user32.WindowFromPoint.argtypes = [POINT]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def whatis(x, y):
    """Which window owns this pixel? Answers "这块是哪个程序画的" without a screenshot."""
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    root = user32.GetAncestor(hwnd, GA_ROOT)
    info = _window_info(root or hwnd)
    print(f"  pixel {x},{y} -> hwnd={info['hwnd']} {info['exe']} pid={info['pid']}")
    print(f"    title={info['title']!r} rect={info['rect']}")
    if root and root != hwnd:
        child = _window_info(hwnd)
        print(f"    (child hwnd={child['hwnd']} title={child['title']!r})")
    return info


def _resolve_window(needle):
    """Find a top-level window by title substring -> hwnd (int).

    pc.py already has find(), but it prints matches and returns rows; this is the
    quiet version the window-moving verbs need.
    """
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):   # windows() prints a table; keep the output clean
        rows = windows()
    hits = [r for r in rows if str(needle).lower() in str(r[6]).lower()]
    if not hits:
        raise SystemExit(f"  no window title contains: {needle}")
    return hits[0][0]


def movewin(needle, x=None, y=None):
    """Move a window by title while KEEPING its size.

    Passing 0 width/height without SWP_NOSIZE is how a window gets shrunk to
    2x2 (done once by accident on the desktop pet), so size always comes from
    the live rect here.
    """
    hwnd = _resolve_window(needle)
    info = _window_info(hwnd)
    w, h = info["rect"][2], info["rect"][3]
    tx = info["rect"][0] if x is None else int(x)
    ty = info["rect"][1] if y is None else int(y)
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(hwnd, None, tx, ty, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    time.sleep(0.25)
    now = _window_info(hwnd)
    print(f"  movewin {needle!r}: ({info['rect'][0]},{info['rect'][1]}) -> "
          f"({now['rect'][0]},{now['rect'][1]})  size {now['rect'][2]}x{now['rect'][3]}")
    return now


def dragwin(needle, x2, y2, hold_ms=600, steps=40, gap_ms=15, grab=None):
    """Long-press drag a window to (x2,y2), grabbing its body rather than its edge.

    grab = "dx,dy" pixel offsets inside the window; default is the middle of the
    lower half, which is where a desktop pet's body usually sits.
    """
    hwnd = _resolve_window(needle)
    info = _window_info(hwnd)
    wx, wy, ww, wh = info["rect"]
    if grab:
        dx, dy = [int(v) for v in str(grab).split(",")]
    else:
        dx, dy = ww // 2, int(wh * 0.72)
    drag(wx + dx, wy + dy, int(x2), int(y2), steps=steps, hold_ms=hold_ms, gap_ms=gap_ms)
    time.sleep(0.3)
    now = _window_info(hwnd)
    print(f"  dragwin {needle!r}: now at ({now['rect'][0]},{now['rect'][1]}) "
          f"size {now['rect'][2]}x{now['rect'][3]}")
    return now


def _cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def wintext(hwnd, find=""):
    """OCR just one window's client area and print its lines.

    Full-screen OCR costs ~4 s; a console window crop costs well under 1 s, which
    is the difference between a 9-round loop taking 10 s instead of 110 s.
    """
    info = _window_info(int(hwnd))
    x, y, w, h = info["rect"]
    if w < 20 or h < 20:
        print(f"  wintext: window too small ({w}x{h})")
        return None
    shot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_win.png")
    _grab_window_safe(int(hwnd), shot_path)
    data = ocr_best(shot_path)
    lines = data.get("lines", [])
    for ln in lines:
        print("   " + ln)
    if find:
        hits = [wd for wd in data.get("words", []) if find in "".join(wd["t"].split())]
        if hits:
            cx = x + hits[0]["x"] + hits[0]["w"] // 2
            cy = y + hits[0]["y"] + hits[0]["h"] // 2
            print(f"  wintext: found {find!r} at ({cx},{cy})")
            return (cx, cy)
        print(f"  wintext: not found {find!r}")
        return None
    return lines



def conread(hwnd):
    """Read a console window's text buffer through the console API.

    OCR on a console misses short left-aligned lines ("太小" sits right against the
    window edge and gets clipped), so read the buffer instead: attach to the
    console that owns the window, dump the screen buffer as characters.
    """
    info = _window_info(int(hwnd))
    pid = info["pid"]
    if not pid:
        return ""
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(int(pid)):
        return ""
    try:
        handle = kernel32.CreateFileW("CONOUT$", 0x80000000 | 0x40000000, 3, None, 3, 0, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return ""
        csbi = (ctypes.c_char * 22)()
        kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(csbi))
        width = ctypes.cast(csbi, ctypes.POINTER(ctypes.c_int16))[1]
        height = ctypes.cast(csbi, ctypes.POINTER(ctypes.c_int16))[0] + 1
        if width <= 0 or width > 400:
            width = 120
        total = width * height
        buf = ctypes.create_unicode_buffer(total + 1)
        read = ctypes.c_ulong(0)
        coord = ctypes.c_long(0)
        kernel32.ReadConsoleOutputCharacterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                                         ctypes.c_ulong, ctypes.c_long,
                                                         ctypes.POINTER(ctypes.c_ulong)]
        if not kernel32.ReadConsoleOutputCharacterW(handle, buf, total, coord, ctypes.byref(read)):
            return ""
        text = buf[:read.value]
        kernel32.CloseHandle(handle)
        lines = [text[i:i + width].rstrip() for i in range(0, len(text), width)]
        return "\n".join(ln for ln in lines if ln.strip())
    finally:
        kernel32.FreeConsole()


def popups(list_only=True, seconds=0):
    """意外弹窗通用守卫（委托给 popup_guard.py）。

    list_only=True  → 只列当前对话框（只读）
    seconds>0      → 后台守这么久，安全按钮自动点，不安全的留给人
    """
    import subprocess as _sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "popup_guard.py")
    cmd = [sys.executable, script]
    if seconds > 0:
        cmd += ["--seconds", str(int(seconds))]
        out = _sp.run(cmd, capture_output=True, text=True, encoding="utf-8")
        print(out.stdout.strip())
    else:
        out = _sp.run(cmd + ["--list"], capture_output=True, text=True, encoding="utf-8")
        print(out.stdout.strip())


def watch(hwnd, until, timeout=60.0, interval=0.6, click_hit=False):
    """Poll one window's text until any of `until` shows up, in ONE process.

    Why this exists: every pc.py run pays the OCR model load (~2-3 s), so a
    caller loop of 9 rounds paid it 9 times and took 110 s without saying
    anything. Here the model loads once and the whole wait fits in one call.

    Matching is done on whitespace-stripped text because the engine happily
    splits Chinese words across lines ("大了" arriving as "大" + "了").
    """
    needles = [w for w in (until.split(",") if isinstance(until, str) else until) if w]
    started = time.time()
    last_note = 0.0
    while True:
        elapsed = time.time() - started
        if elapsed > timeout:
            print(f"  watch: timeout after {elapsed:.1f}s, none of {needles} appeared")
            return None
        info = _window_info(int(hwnd))
        x, y, w, h = info["rect"]
        if w >= 20 and h >= 20:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_watch.png")
            _grab_window_safe(int(hwnd), path)
            data = ocr_best(path)
            flat = "".join("".join(wd["t"].split()) for wd in data.get("words", []))
            for needle in needles:
                if "".join(needle.split()) in flat:
                    print(f"  watch: {needle!r} after {elapsed:.1f}s")
                    if click_hit:
                        for wd in data.get("words", []):
                            if "".join(needle.split()) in "".join(wd["t"].split()):
                                click(x + wd["x"] + wd["w"] // 2, y + wd["y"] + wd["h"] // 2)
                                break
                    return needle
            if elapsed - last_note >= 3:
                last_note = elapsed
                tail = " / ".join((data.get("lines") or [])[-3:])[:90]
                print(f"  watch: {elapsed:.0f}s ... {tail}")
        time.sleep(interval)


def closewin(needle):
    """Close a window by title substring via WM_CLOSE.

    Needed for Dev-C++'s run console: after the program exits the window stays on
    top at "press any key", swallows later F11 keystrokes, and it is not listed by
    windows() (that only reports titled top-level windows of the caller's view).
    """
    from ctypes import wintypes
    WM_CLOSE = 0x0010
    hits = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if needle.lower() in buf.value.lower():
            hits.append((hwnd, buf.value))
        return True

    user32.EnumWindows(proc(cb), 0)
    for hwnd, title in hits:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        print(f"  closewin: {title!r}")
    if not hits:
        print(f"  closewin: nothing matched {needle!r}")
    return hits


def hover(x, y, steps=5, settle_ms=250, wait_ms=1800):
    """Slide onto a point instead of teleporting, then hold still.

    Hover-revealed panels (the DSH pet's 喂食 panel, tooltips, hover toolbars)
    are driven by mouseenter/mousemove: a single SetCursorPos jump often only
    triggers a stray bubble or nothing at all. Entering in several small steps
    and waiting ~1.5-2 s is what actually makes the panel render.
    """
    x0, y0 = _cursor_pos()
    for i in range(1, steps + 1):
        user32.SetCursorPos(int(x0 + (x - x0) * i / steps), int(y0 + (y - y0) * i / steps))
        time.sleep(settle_ms / 1000.0)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(wait_ms / 1000.0)
    print(f"  hover {x},{y} (entered in {steps} steps, held {wait_ms}ms)")


def hoverclick(hx, hy, text, wait_ms=1800, dry=False, pick="first"):
    """Hover to reveal a panel, then click a label inside it by text.

    Encoding the two lessons from 2026-10-22 in one verb: hover must approach
    slowly (else no panel), and the button must be found by OCR text rather than
    by guessed coordinates (my guessed coordinate missed by ~8 px and the click
    silently did nothing).
    """
    hover(hx, hy, wait_ms=wait_ms)
    # Only look around the hover point: a full-screen search happily matches the
    # same words in chat history far away (happened on the first run).
    region = (max(0, int(hx) - 340), max(0, int(hy) - 60),
              min(3840, int(hx) + 340), min(2160, int(hy) + 420))
    hit = find_text(text, region=region, pick=pick)
    if hit is None:
        print(f"  hoverclick: panel text {text!r} not found after hover")
        return None
    if dry:
        print(f"  hoverclick: DRY RUN would click {text!r} at {hit}")
        return hit
    click(hit[0], hit[1])
    time.sleep(0.6)
    after = find_text(text)
    print(f"  hoverclick: clicked {text!r} at {hit}; label {'still there' if after else 'gone'}")
    return hit


def drag(x1, y1, x2, y2, steps=18, hold_ms=0, gap_ms=12):
    """Press, optionally HOLD, then move in small steps and release.

    hold_ms matters for desktop pets: they start their own drag only after the
    button has been down for a moment, otherwise a press-move-release looks like
    a click and they do not follow. gap_ms keeps the movements slow enough for
    the app to receive them as a drag rather than a jump.
    """
    user32.SetCursorPos(int(x1), int(y1))
    time.sleep(0.12)
    _mouse(MOUSEEVENTF["left_down"])
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    for i in range(1, steps + 1):
        user32.SetCursorPos(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
        time.sleep(gap_ms / 1000.0)
    time.sleep(0.08)
    _mouse(MOUSEEVENTF["left_up"])
    print(f"  dragged {x1},{y1} -> {x2},{y2} (hold {hold_ms}ms, {steps} steps)")


def scroll(n):
    step = 120
    for _ in range(abs(int(n))):
        _mouse(MOUSEEVENTF["wheel"], data=step if int(n) > 0 else -step)
    print(f"  scrolled {n}")


def _set_clipboard(text: str) -> None:
    """Put Unicode text on the clipboard through the Win32 API.

    Going through `powershell Set-Clipboard` mangled Chinese into mojibake: the
    bytes crossed the pipe as UTF-8 but the console read them as the ANSI code
    page. Talking to the clipboard directly skips every encoding in between.
    """
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    # Without these prototypes ctypes assumes a 32-bit int return, the 64-bit
    # HGLOBAL gets truncated, and GlobalLock hands back NULL.
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
    user32.OpenClipboard(None)
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        user32.CloseClipboard()


def type_text(text):
    """Clipboard paste: the only reliable way to send Chinese into any app."""
    _set_clipboard(text)
    time.sleep(0.12)
    _key(VK["ctrl"]); _key(VK["v"]); _key(VK["v"], up=True); _key(VK["ctrl"], up=True)
    print(f"  typed {len(text)} chars (pasted)")


def key(name):
    vk = VK.get(name.lower())
    if vk is None and len(name) == 1:
        vk = ord(name.upper())
    if vk is None:
        raise SystemExit(f"unknown key: {name}")
    _key(vk); _key(vk, up=True)
    print(f"  key {name}")


def hotkey(combo):
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    vks = [VK[p] for p in parts]
    for vk in vks:
        _key(vk)
    for vk in reversed(vks):
        _key(vk, up=True)
    print(f"  hotkey {combo}")



def foreground_hwnd():
    return user32.GetForegroundWindow()


def activate(window):
    """Bring a window to the front for real.

    A plain SetForegroundWindow is silently ignored when another process owns
    the foreground (Windows' foreground lock) — the keys then go to whatever was
    already there, which is how one test typed into the browser instead. The
    documented way around it is to attach to the foreground thread's input queue
    while the switch happens.
    """
    hwnd = int(window)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)

    fg = user32.GetForegroundWindow()
    if fg != hwnd:
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        my_thread = kernel32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(fg_thread, my_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, my_thread, False)

    time.sleep(0.35)
    ok = user32.GetForegroundWindow() == hwnd
    print(f"  activated hwnd {hwnd}  foreground={ok}")
    if not ok:
        print("  WARNING: 前台没抢到，按键可能落到别的窗口")


def find(title_part):
    rows = windows()
    hits = [r for r in rows if title_part.lower() in r[6].lower()]
    for r in hits:
        print(f"  MATCH hwnd={r[0]} pid={r[1]} title={r[6]}")
    if not hits:
        print(f"  no window title contains: {title_part}")
    return hits



_api_prototypes()

UIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uia.ps1")


def _uia(*args):
    """Call the PowerShell UI Automation helper and hand back its stdout."""
    import subprocess
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", UIA, *[str(a) for a in args]],
                         capture_output=True, text=True, timeout=60)
    return (res.stdout or "") + (res.stderr or "")


def sel(hwnd, name):
    """Select a control by its accessible name; print where it is.

    This is the whole point of the UIA layer: no screenshot, no eyeballed
    coordinates, and it works when the tile has moved since the last look.
    """
    out = _uia("select", hwnd, name).strip()
    if "selected:" not in out:
        raise SystemExit(f"  UIA select failed: {out[:200]}")
    print("  " + out.splitlines()[0])
    return out


def seq(script):
    """Run several verbs in one process.

    The slow part of the first version was the round trip: screenshot -> think ->
    click, once per action, each hop costing seconds. A script runs them back to
    back; the caller screenshots once at the end to verify.
    """
    for step in [s.strip() for s in script.split(";") if s.strip()]:
        parts = step.split(None, 1)
        verb, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        argv = rest.split() if rest else []
        if verb == "sel":
            sel(*argv[:2])
        elif verb == "click":
            click(argv[0], argv[1], "--double" in argv, "--right" in argv)
        elif verb == "move":
            move(argv[0], argv[1])
        elif verb == "type":
            type_text(rest)
        elif verb == "key":
            key(argv[0])
        elif verb == "hotkey":
            hotkey(argv[0])
        elif verb == "sleep":
            time.sleep(float(argv[0]) / 1000.0)
        else:
            raise SystemExit(f"  unknown seq step: {verb}")
        time.sleep(0.12)
    print("  seq done")



OCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")


def ocr(image, find=""):
    """Run Windows OCR and return the parsed JSON.

    Windows OCR splits Chinese into single characters, often with spaces, so
    callers must compare whitespace-stripped text — see find_text().
    """
    import json as _json
    import subprocess
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "dsh-ocr-result.json")
    try:
        os.remove(tmp)
    except OSError:
        pass
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", OCR, image, find, tmp],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    if os.path.exists(tmp):
        try:
            return _json.loads(io.open(tmp, encoding="utf-8").read())
        except _json.JSONDecodeError as exc:
            raise SystemExit(f"  ocr json broken: {exc}")
    out = (res.stdout or "").strip()
    start = out.find("{")
    if start < 0:
        raise SystemExit(f"  ocr failed: {out[:300]}")
    return _json.loads(out[start:])


def _load_rapid():
    import importlib.util as _iu
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_rapid.py")
    spec = _iu.spec_from_file_location("ocr_rapid", path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ocr_best(image, region=None):
    """RapidOCR when available (reads UI text Windows OCR silently drops), else
    the Windows engine. Same return shape either way."""
    try:
        rapid = _load_rapid()
        if rapid.available():
            data = rapid.run(image, region)
            if data.get("ok"):
                return data
    except Exception:
        pass
    if region:
        return _ocr_region(image, region, 2)
    return ocr(image)


def _ocr_region(image, region, scale=2):
    """OCR a cropped, upscaled slice and map the boxes back to screen pixels.

    Small UI text (a contact row, a menu item) is exactly where Windows OCR
    fails, and exactly where a wrong click hurts. Upscaling a tight crop fixes
    the accuracy and also removes the surrounding noise that confuses picking.
    """
    from PIL import Image
    x0, y0, x1, y1 = region
    im = Image.open(image).convert("RGB").crop((x0, y0, x1, y1))
    if scale != 1:
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    tmp = os.path.join(tempfile.gettempdir(), "dsh-ocr-crop.png")
    im.save(tmp)
    data = ocr(tmp)
    if data.get("ok"):
        for wd in data.get("words", []):
            wd["x"] = int(wd["x"] / scale) + x0
            wd["y"] = int(wd["y"] / scale) + y0
            wd["w"] = int(wd["w"] / scale)
            wd["h"] = int(wd["h"] / scale)
    return data


def _ocr_multi(image, region, scales=(1, 2, 4)):
    """OCR the same crop at several scales and merge the words.

    Windows OCR on small crops is flaky in a way that looks random: the same
    dialogue row returns 0 words at 1x, 3 words at 2x, 10 at 4x. Rather than
    guess a "good" scale, try a few and keep every word any scale found. Cheap
    (each pass ~0.3 s) and it removes a whole class of silent misses.
    """
    merged = {"ok": False, "lines": [], "words": [], "hit": None}
    for sc in scales:
        data = _ocr_region(image, region, sc)
        if not data.get("ok"):
            continue
        merged["ok"] = True
        merged["lines"].extend(data.get("lines", []))
        merged["words"].extend(data.get("words", []))
    return merged


def _ocr_matches(data, needle):
    """Every occurrence of needle on the image, as (x0,y0,x1,y1) in order."""
    flat, boxes = "", []
    for wd in data.get("words", []):
        for ch in wd["t"]:
            if ch.strip() == "":
                continue
            flat += ch
            boxes.append((ch, wd["x"], wd["y"], wd["w"], wd["h"]))
    target = "".join(needle.split())
    out, start = [], 0
    while True:
        idx = flat.find(target, start)
        if idx < 0:
            return out
        span = boxes[idx: idx + len(target)]
        out.append((min(b[1] for b in span), min(b[2] for b in span),
                    max(b[1] + b[3] for b in span), max(b[2] + b[4] for b in span)))
        start = idx + max(1, len(target))


def find_text(needle, image=None, region=None, below=None, above=None, pick="first", scale=1):
    """Locate a phrase, with the disambiguation the WeChat search case needed.

    The same words often appear several times on screen (a contact, a group that
    contains that contact, a chat-history row). Instead of guessing coordinates,
    the caller names an anchor: `below="联系人"` / `above="群聊"` / a pixel
    region. Every candidate is printed so a wrong choice is visible in the log.

    region: (x0, y0, x1, y1) in screen pixels.
    below/above: another phrase used as a boundary anchor.
    """
    if image is None:
        image = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ocr_full.png")
        shot(image)
    if scale == 0:
        data = _ocr_multi(image, region) if region else ocr_best(image)
    elif region and scale != 1:
        data = _ocr_region(image, region, scale)
    else:
        data = ocr_best(image, region)
    if not data.get("ok"):
        raise SystemExit(f"  ocr error: {data.get('error')}")

    seen, uniq = set(), []
    for wd in data.get("words", []):
        key = (wd["t"], wd["x"] // 8, wd["y"] // 8)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(wd)
    data["words"] = uniq

    # Prefer rows whose text IS the phrase over rows that merely contain it:
    # WeChat's "包含：许海宁" line sits directly under the group row, so a
    # substring match there sends the click to the group (this actually happened).
    target = "".join(needle.split())
    exact, loose = [], []
    for wd in data.get("words", []):
        t = "".join(wd.get("t", "").split())
        if target not in t:
            continue
        box = (wd["x"], wd["y"], wd["x"] + wd["w"], wd["y"] + wd["h"])
        (exact if t == target else loose).append(box)
    if not exact and not loose:
        flat = _ocr_matches(data, needle)
        loose = flat
    cands = exact if exact else loose
    if exact:
        print(f"  find_text: exact-line matches={len(exact)} (ignoring {len(loose)} looser)")
    if not cands:
        print(f"  find_text: not found: {needle}")
        return None

    def anchor_box(text):
        boxes = _ocr_matches(data, text)
        if not boxes:
            raise SystemExit(f"  find_text: anchor not found: {text!r}")
        return boxes[0]

    limits = {}
    if below:
        limits["below"] = anchor_box(below)
    if above:
        limits["above"] = anchor_box(above)

    kept = []
    for box in cands:
        x0, y0, x1, y1 = box
        cy = (y0 + y1) // 2
        ok = True
        if region:
            rx0, ry0, rx1, ry1 = region
            ok = ok and rx0 <= x0 and x1 <= rx1 and ry0 <= y0 and y1 <= ry1
        if "below" in limits:
            ok = ok and cy > limits["below"][3]
        if "above" in limits:
            ok = ok and cy < limits["above"][1]
        kept.append((ok, box))

    print(f"  find_text: {needle!r} candidates={len(cands)} kept={sum(1 for k, _ in kept if k)}")
    for idx, (ok, box) in enumerate(kept):
        flag = "->" if ok else "  "
        print(f"    {flag} #{idx} box={box}")
    chosen = [box for ok, box in kept if ok]
    if not chosen:
        print("  find_text: no candidate satisfied the constraints")
        return None
    if pick == "last":
        box = chosen[-1]
    elif pick in ("top", "bottom"):
        # Search panels list the exact hits first and looser matches (a group
        # that merely contains the name) after them, so "topmost" is a real
        # policy, not a guess: it needs no header text at all.
        ordered = sorted(chosen, key=lambda b: (b[1] + b[3]) // 2)
        box = ordered[0] if pick == "top" else ordered[-1]
    else:
        box = chosen[0]
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    print(f"  find_text: chose {box} centre=({cx},{cy})")
    # 兼容旧格式（插件里用 `at (x,y)` 解析坐标，别为了换个更好看的措辞弄断它）
    print(f"  find_text: {needle!r} at ({cx},{cy})")
    return cx, cy


def scan_click(needle, verify, scan_region, verify_region, step=22, dwell_ms=700,
               x=None):
    """Click down a column until the app's own title bar agrees.

    Some UIs (WeChat is one) draw list rows in a way Windows OCR cannot read, so
    "find the row by its text" is impossible — but the window title after a click
    IS readable. So: click each row position in turn, read the title, stop as
    soon as it matches. Every click is verified; a wrong one only opens another
    chat, and the loop keeps its bounds tight and aborts on no match.
    """
    x0, y0, x1, y1 = scan_region
    cx = x if x is not None else (x0 + x1) // 2
    tried = []
    y = y0
    while y <= y1:
        click(cx, y)
        time.sleep(dwell_ms / 1000.0)
        shot(os.path.join(tempfile.gettempdir(), "scan-verify.png"))
        data = _ocr_region(os.path.join(tempfile.gettempdir(), "scan-verify.png"),
                           verify_region, 2)
        blob = "".join("".join(w["t"].split()) for w in data.get("words", []))
        ok = "".join(verify.split()) in blob
        tried.append((y, ok))
        print(f"  scan_click y={y} verify={'HIT' if ok else 'miss'} ({blob[:40]!r})")
        if ok:
            print(f"  scan_click: clicked y={y} after {len(tried)} tries")
            return y
        y = y + step
    print(f"  scan_click: no match in {len(tried)} tries")
    return None


def click_text(needle, image=None, **kw):
    hit = find_text(needle, image, **kw)
    if hit is None:
        raise SystemExit(f"  click_text: {needle!r} not on screen (or filtered out)")
    click(hit[0], hit[1])
    return hit




def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    flags = [a for a in args if a.startswith("--")]
    pos = [a for a in args if not a.startswith("--")]
    if cmd == "windows":
        windows()
    elif cmd == "shot":
        window = None
        out = "shot.png"
        for i, a in enumerate(args):
            if a == "--window":
                window = args[i + 1]
            elif a == "--out":
                out = args[i + 1]
        shot(out, window)
    elif cmd == "move":
        move(pos[0], pos[1])
    elif cmd == "click":
        click(pos[0], pos[1], "--double" in flags, "--right" in flags)
    elif cmd == "whatis":
        whatis(args[0], args[1])
    elif cmd == "movewin":
        # movewin <标题子串> [x] [y]
        num = [a for a in args[1:] if not a.startswith("--")]
        movewin(args[0], *[int(v) for v in num[:2]] if len(num) >= 2 else ([int(num[0])] if num else []))
    elif cmd == "watch":
        # watch <hwnd> --until 文字[,文字] [--timeout 秒] [--interval 秒] [--click]
        until, timeout, interval, do_click = "", 60.0, 0.6, False
        i = 1
        while i < len(args):
            if args[i] == "--until":
                until = args[i + 1]; i += 2
            elif args[i] == "--timeout":
                timeout = float(args[i + 1]); i += 2
            elif args[i] == "--interval":
                interval = float(args[i + 1]); i += 2
            elif args[i] == "--click":
                do_click = True; i += 1
            else:
                i += 1
        watch(int(args[0]), until, timeout=timeout, interval=interval, click_hit=do_click)
    elif cmd == "popups":
        popups(list_only=True)
    elif cmd == "guard":
        secs = 120
        if "--seconds" in args:
            secs = int(args[args.index("--seconds") + 1])
        popups(list_only=False, seconds=secs)
    elif cmd == "conread":
        txt = conread(int(args[0]))
        for ln in (txt.splitlines()[-12:] if "--tail" in args else txt.splitlines()):
            print("   " + ln)
    elif cmd == "wintext":
        # wintext <hwnd> [--find 文字]
        find = ""
        if "--find" in args:
            find = args[args.index("--find") + 1]
        wintext(int(args[0]), find)
    elif cmd == "closewin":
        closewin(args[0])
    elif cmd == "hover":
        wait = 1800
        nums, i = [], 0
        while i < len(args):
            if args[i] == "--wait":
                wait = int(args[i + 1]); i += 2
            elif args[i] == "--steps":
                i += 2
            else:
                nums.append(int(float(args[i]))); i += 1
        hover(nums[0], nums[1], wait_ms=wait)
    elif cmd == "hoverclick":
        wait, dry, i, rest = 1800, False, 0, []
        while i < len(args):
            if args[i] == "--wait":
                wait = int(args[i + 1]); i += 2
            elif args[i] == "--dry":
                dry = True; i += 1
            else:
                rest.append(args[i]); i += 1
        hoverclick(int(rest[0]), int(rest[1]), rest[2], wait_ms=wait, dry=dry)
    elif cmd == "dragwin":
        # dragwin <标题子串> x2 y2 [--hold ms] [--grab dx,dy]
        hold, grab = 600, None
        rest, i = [], 0
        while i < len(args):
            if args[i] == "--hold":
                hold = int(args[i + 1]); i += 2
            elif args[i] == "--grab":
                grab = args[i + 1]; i += 2
            else:
                rest.append(args[i]); i += 1
        dragwin(rest[0], int(rest[1]), int(rest[2]), hold_ms=hold, grab=grab)
    elif cmd == "drag":
        # drag x1 y1 x2 y2 [--hold ms] [--steps n] [--gap ms]
        nums, hold, steps, gap = [], 0, 18, 12
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--hold":
                hold = int(args[i + 1]); i += 2
            elif a == "--steps":
                steps = int(args[i + 1]); i += 2
            elif a == "--gap":
                gap = int(args[i + 1]); i += 2
            else:
                nums.append(int(float(a))); i += 1
        drag(*nums[:4], steps=steps, hold_ms=hold, gap_ms=gap)
    elif cmd == "scroll":
        if "--px" in args:
            scroll_px(int(args[args.index("--px") + 1]))
        else:
            scroll(pos[0])
    elif cmd == "scrollto":
        # scrollto <文字> [--timeout 秒] [--region x0,y0,x1,y1] [--step 像素]
        rg, to, st = None, 25.0, -90
        for i, a in enumerate(args):
            if a == "--region":
                rg = tuple(int(v) for v in args[i + 1].split(","))
            elif a == "--timeout":
                to = float(args[i + 1])
            elif a == "--step":
                st = int(args[i + 1])
        scroll_to(pos[0], timeout=to, region=rg, step=st)
    elif cmd == "waittext":
        rg, to = None, 20.0
        for i, a in enumerate(args):
            if a == "--region":
                rg = tuple(int(v) for v in args[i + 1].split(","))
            elif a == "--timeout":
                to = float(args[i + 1])
        wait_text(pos[0], timeout=to, region=rg)
    elif cmd == "clicklabel":
        rg = None
        if "--region" in args:
            rg = tuple(int(v) for v in args[args.index("--region") + 1].split(","))
        click_label(pos[0], region=rg)
    elif cmd == "clipget":
        clip_get()
    elif cmd == "clipset":
        clip_set(args[0] if args else "")
    elif cmd == "doctor":
        doctor()
    elif cmd == "type":
        if "--secret" in flags:
            type_secret(pos[0] if pos else "")
        else:
            type_text(args[0] if args and not args[0].startswith("--") else "")
    elif cmd == "key":
        key(pos[0])
    elif cmd == "hotkey":
        hotkey(pos[0])
    elif cmd == "activate":
        activate(pos[0])
    elif cmd == "find":
        find(pos[0])
    elif cmd == "sel":
        sel(pos[0], args[1] if len(args) > 1 else "")
    elif cmd == "seq":
        seq(args[0] if args else "")
    elif cmd == "ocr":
        image = None
        find = ""
        for i, a in enumerate(args):
            if a == "--image":
                image = args[i + 1]
            elif a == "--find":
                find = args[i + 1]
        if image is None:
            image = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ocr_full.png")
            shot(image)
        data = ocr_best(image)
        print(f"  ok={data.get('ok')} engine={data.get('engine', 'windows')} lines={len(data.get('lines', []))}")
        for line in data.get("lines", [])[:25]:
            print("   " + line)
    elif cmd == "scanclick":
        # scanclick <x> <y0>-<y1> --verify <text> --vregion x0,y0,x1,y1 [--step N]
        x = int(args[0])
        y0, y1 = (int(v) for v in args[1].split("-"))
        opts = {"verify": "", "vregion": None, "step": 22}
        i = 2
        while i < len(args):
            if args[i] in ("--verify", "--vregion", "--step"):
                opt_name = args[i][2:]
                opts[opt_name] = args[i + 1]
                i = i + 2
            else:
                i = i + 1
        vregion = tuple(int(v) for v in opts["vregion"].split(","))
        scan_click("", opts["verify"], (x, y0, x, y1), vregion,
                   step=int(opts["step"]), x=x)
    elif cmd in ("findtext", "clicktext"):
        opts = {"region": None, "below": None, "above": None, "pick": "first"}
        rest = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--below", "--above", "--pick"):
                opts[a[2:]] = args[i + 1]
                i = i + 2
            elif a == "--region":
                opts["region"] = tuple(int(v) for v in args[i + 1].split(","))
                i = i + 2
            elif a == "--scale":
                opts["scale"] = int(args[i + 1])
                i = i + 2
            else:
                rest.append(a)
                i = i + 1
        fn = find_text if cmd == "findtext" else click_text
        fn(rest[0], None, **opts)
    else:
        raise SystemExit(f"unknown verb: {cmd}")


def clip_get():
    """读剪贴板里的纯文本（CF_UNICODETEXT）。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.GetClipboardData.restype = wintypes.HANDLE
    u.GetClipboardData.argtypes = [wintypes.UINT]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [wintypes.HANDLE]
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not u.OpenClipboard(None):
        print("clipboard busy")
        return
    try:
        h = u.GetClipboardData(13)          # CF_UNICODETEXT
        if not h:
            print("")
            return
        ptr = k.GlobalLock(h)
        if not ptr:
            print("")
            return
        try:
            txt = ctypes.wstring_at(ptr)
        finally:
            k.GlobalUnlock(h)
        print(txt)
    finally:
        u.CloseClipboard()


def clip_set(text):
    """只写剪贴板（不粘贴）。中文走 CF_UNICODETEXT。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [wintypes.HANDLE]
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.OpenClipboard.argtypes = [wintypes.HWND]
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    h = k.GlobalAlloc(0x0002, size)      # GMEM_MOVEABLE
    if not h:
        print("alloc failed")
        return
    ptr = k.GlobalLock(h)
    ctypes.memmove(ptr, data, size)
    k.GlobalUnlock(h)
    if not u.OpenClipboard(None):
        print("clipboard busy")
        return
    try:
        u.EmptyClipboard()
        u.SetClipboardData(13, h)        # CF_UNICODETEXT
        print("clipboard set (%d chars)" % len(text))
    finally:
        u.CloseClipboard()


def _run_ps(cmd, timeout=25):
    """跑一小段 PowerShell，返回 stdout（失败返回空串）。"""
    import subprocess
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()
    except Exception as e:
        return "ERR:" + str(e)


def doctor():
    """体检：别人装完之后先跑这个，一行一个问题，看得懂就能自己修。"""
    import platform
    rows = []

    def ok(name, detail):
        rows.append(("OK  ", name, detail))

    def bad(name, detail):
        rows.append(("FAIL", name, detail))

    def warn(name, detail):
        rows.append(("WARN", name, detail))

    # 系统
    ok("System", "%s %s" % (platform.system(), platform.release()))
    ok("Python", "%s  %s" % (platform.python_version(), sys.executable))
    ok("ScriptDir", os.path.dirname(os.path.abspath(__file__)))

    # DPI / 屏幕
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        dpi = 0
    scale = ("%d%%" % round(dpi / 96 * 100)) if dpi else "?"
    if dpi and abs(dpi - 96) > 1:
        warn("DPI", "mode=%s  system_dpi=%d  scale=%s  <- 不是 100%%，截图坐标与点击坐标"
                    "容易差一点点；v0.6.0 起已做 DPI 感知，若仍点偏请回报" % (DPI_MODE, dpi, scale))
    else:
        ok("DPI", "mode=%s  system_dpi=%d  scale=%s" % (DPI_MODE, dpi, scale))
    try:
        w, h = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        vx, vy = int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
        vw, vh = int(user32.GetSystemMetrics(78)), int(user32.GetSystemMetrics(79))
        ok("Screen", "primary=%dx%d  virtual=(%d,%d) %dx%d" % (w, h, vx, vy, vw, vh))
    except Exception as e:
        bad("Screen", str(e))

    # 截图
    try:
        from PIL import Image, ImageGrab
        img = ImageGrab.grab(all_screens=True)
        if img.size[0] > 0:
            ok("Screenshot", "Pillow %s  grab=%dx%d" % (getattr(Image, "__version__", "?"), img.width, img.height))
        else:
            bad("Screenshot", "截图为空")
    except Exception as e:
        bad("Screenshot", "Pillow 不可用: %s（pip install pillow）" % e)

    # OCR
    rapid = "无"
    try:
        import rapidocr_onnxruntime  # noqa: F401
        rapid = "有"
    except Exception:
        rapid = "无"
    ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")
    langs = _run_ps("(Get-WinUserLanguageList).LanguageTag -join ','", timeout=20) or ""
    ok("OCR", "rapidocr=%s  ocr.ps1=%s" % (rapid, "有" if os.path.exists(ps1) else "无"))
    if langs:
        warn("OCRLang", "系统语言=%s（Windows OCR 只认已安装的）" % langs[:60])

    # 剪贴板
    try:
        import ctypes as _c
        u = _c.windll.user32
        u.OpenClipboard.argtypes = [_c.c_void_p]
        opened = bool(u.OpenClipboard(None))
        if opened:
            u.CloseClipboard()
            ok("Clipboard", "可读")
        else:
            warn("Clipboard", "被别的程序占用")
    except Exception as e:
        warn("Clipboard", str(e))

    # 通知脚本 / UIA
    home = os.path.expanduser("~")
    notify = os.path.join(home, "OllamaStart", "notify.ps1")
    ok("Notify", notify if os.path.exists(notify) else "没有 notify.ps1（弹提示功能会跳过）")

    # 目录可写
    d = os.path.dirname(os.path.abspath(__file__))
    try:
        t = os.path.join(d, ".write-test")
        with open(t, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(t)
        ok("Writable", d)
    except Exception as e:
        bad("Writable", "%s（%s）" % (d, e))

    print("  pc-doctor")
    for flag, name, detail in rows:
        print("  %-4s %-11s %s" % (flag, name, detail))
    fails = sum(1 for r in rows if r[0] == "FAIL")
    warns = sum(1 for r in rows if r[0] == "WARN")
    print("  ----  %d FAIL  %d WARN  ----" % (fails, warns))
    return fails


# ---------------- v0.7.0：今天踩出来的五个坑 ----------------
def _norm(s):
    """去掉所有空白 + 转小写再比。

    OCR 会把中文拆成单字（'质 数' 要能匹配 '质数'），而英文按钮的大小写也常和
    调用方写的不一致（'Generate new token' vs 'Generate New Token'）。
    """
    return "".join(str(s).split()).lower()


def screen_text(region=None):
    """截全屏 → OCR。不打印（轮询里用，打印会把日志刷爆）。"""
    from PIL import ImageGrab
    tmp = os.path.join(tempfile.gettempdir(), "_pc_wait.png")
    ImageGrab.grab(all_screens=True).save(tmp)
    data = ocr_best(tmp, region) or {}
    return data.get("lines") or [], data.get("words") or []


def wait_text(text, timeout=20.0, region=None, interval=0.7):
    """等到屏幕（或某块区域）出现这段文字为止。

    以前只能 sleep 固定秒数：页面慢就等不够、快就白等。这是本轮最常缺的东西。
    """
    needle = _norm(text)
    t0 = time.time()
    while True:
        try:
            lines, _words = screen_text(region)
        except Exception:
            lines = []
        if needle and needle in _norm("".join(lines)):
            print(f"  found '{text}' after {time.time() - t0:.1f}s")
            return True
        if time.time() - t0 >= timeout:
            print(f"  timeout after {timeout}s: '{text}' not seen")
            return False
        time.sleep(interval)


def scroll_px(px):
    """精细滚动：一个 wheel 事件，delta 任意。120 = 传统一格。

    Chrome/Edge 支持 sub-notch（40 左右很顺），老程序会忽略小数，那就退化成整格。
    """
    _mouse(MOUSEEVENTF["wheel"], data=int(px))
    print(f"  scrolled {int(px)} (fine)")


def scroll_to(text, timeout=25.0, region=None, step=-90, pause=0.35):
    """往下滚，直到看见这段文字为止（相当于人按住滚轮找东西）。"""
    needle = _norm(text)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            lines, _words = screen_text(region)
        except Exception:
            lines = []
        if needle and needle in _norm("".join(lines)):
            print(f"  reached '{text}' in {time.time() - t0:.1f}s")
            return True
        scroll_px(step)
        time.sleep(pause)
    print(f"  timeout after {timeout}s: '{text}' never showed up")
    return False


def type_secret(text):
    """输入敏感内容（密码/令牌），粘完立刻把剪贴板清空。"""
    type_text(text)
    time.sleep(0.15)
    _set_clipboard("")
    print("  secret typed, clipboard cleared")


def click_label(text, region=None, timeout=4.0):
    """按文字点：先找这段字，点它的中心。

    自定义样式的 checkbox/radio 用坐标点不动（真实的 input 被藏起来了），
    但点它旁边的文字（label）一定能切换 —— 这招今天救了好几次。
    """
    needle = _norm(text)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            _lines, words = screen_text(region)
        except Exception:
            words = []
        for w in words:
            if needle in _norm(w.get("t", "")):
                x = int(w["x"] + w.get("w", 0) / 2)
                y = int(w["y"] + w.get("h", 0) / 2)
                click(x, y)
                print(f"  clicked label '{text}' at ({x},{y})")
                return True
        time.sleep(0.4)
    print(f"  label not found: '{text}'")
    return False



if __name__ == "__main__":
    main(sys.argv[1:])
