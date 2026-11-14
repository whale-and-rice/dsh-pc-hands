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
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
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
    "hwheel": 0x1000,
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


def shot(out="shot.png", window=None, region=None):
    """Screenshot the screen, one window, or just a rectangle.

    region=(x0, y0, x1, y1) in screen pixels. Cropping here instead of in PIL
    afterwards saves a round trip and keeps the OCR call small (full screen
    takes ~4 s with RapidOCR, a small region well under 1 s).
    """
    from PIL import ImageGrab
    box = None
    if region:
        box = tuple(int(v) for v in region)
    elif window and str(window).isdigit():
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
    where = f"  region={tuple(box)}" if region else (f"  window={window}" if window else "  (full screen)")
    print(f"  saved {out}  {img.size[0]}x{img.size[1]}{where}")
    return out


def move(x, y):
    user32.SetCursorPos(int(x), int(y))
    print(f"  pointer -> {int(x)},{int(y)}")


# ---------------------------------------------------------------------------
# 急停：一个旗标文件，任何会循环或连发的动作都看它一眼
# 2026-11-11 加。为什么不是"杀进程"：一个 pc_task 可能带十几步，杀外层进程
# 清不干净 Python 子进程正在跑的循环；旗标能让它在下一步之前自己停。
# ---------------------------------------------------------------------------
CANCEL_FLAG = os.path.join(tempfile.gettempdir(), "dsh-pc-cancel.flag")
CANCEL_HIT = {"hit": False}       # 本次调用里是否真的被急停打断过

# 最近一次 type 的结果状态，给 CLI 输出机器可读结果用
LAST_TYPE = {"status": "unknown"}

RESULT_MARK = "##RESULT##"

# ---------------------------------------------------------------------------
# 临时文件唯一化（2026-11-11）
# 以前固定叫 dsh-ocr-crop.png / _pc_wait.png 这种。两个动作叠在一起时，
# 后一个会把前一个截的图覆盖掉 —— 等于拿别人的图做判断，是实打实的误判。
# 现在每个临时文件都带 pid + 自增序号 + 时间戳。
# ---------------------------------------------------------------------------
_TMP_SEQ = {"n": 0}
_TMP_FILES = set()
_TMP_SWEPT = {"done": False}
_TMP_MAX_AGE = 6 * 3600      # 超过 6 小时的残留才清，别动别人正在用的


def _sweep_stale_tmp():
    """开机首次用临时文件时扫一遍：只清「dsh- 前缀 + 超龄」的残留。

    绝不在这里泛删 %TEMP% 里的 *.png —— 那是共享目录，别人的文件不能动。
    """
    if _TMP_SWEPT["done"]:
        return
    _TMP_SWEPT["done"] = True
    try:
        base = tempfile.gettempdir()
        now = time.time()
        removed = 0
        for name in os.listdir(base):
            if not name.startswith("dsh-"):
                continue
            path = os.path.join(base, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > _TMP_MAX_AGE:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        if removed:
            print("  tmp: 清掉 %d 个超龄残留（>6h，只动 dsh- 前缀）" % removed)
    except OSError:
        pass


def _cleanup_tmp():
    """进程退出时删掉自己这次创建的所有临时文件。"""
    for path in list(_TMP_FILES):
        try:
            os.remove(path)
        except OSError:
            pass
    _TMP_FILES.clear()


import atexit as _atexit
_atexit.register(_cleanup_tmp)


def _tmp(name, suffix=""):
    _sweep_stale_tmp()
    _TMP_SEQ["n"] += 1
    stamp = "%d-%d-%d" % (os.getpid(), _TMP_SEQ["n"], int(time.time() * 1000) % 100000)
    path = os.path.join(tempfile.gettempdir(), "dsh-%s-%s%s" % (name, stamp, suffix))
    _TMP_FILES.add(path)
    return path


_OCR_ENGINE_TAG = {"v": None}


def _ocr_engine_tag():
    """缓存键必须带上"拿什么引擎识别的"：引擎换了，同图结果可能不同。"""
    if _OCR_ENGINE_TAG["v"] is not None:
        return _OCR_ENGINE_TAG["v"]
    tag = "win"
    try:
        rapid = _load_rapid()
        if rapid.available():
            ver = getattr(rapid, "VERSION", "") or getattr(rapid, "__version__", "") or "unknown"
            tag = "rapid:%s" % ver
    except Exception:
        tag = "win"
    _OCR_ENGINE_TAG["v"] = tag
    return tag


# ---------------------------------------------------------------------------
# OCR 结果缓存（2026-11-11）
# 键是【图片内容的 sha256】：像素完全一样，识别结果必然一样，跳过重复识别不会
# 改变任何判断。绝对不按"区域+时间"做键 —— 那种会读到旧帧、把没有的字说成有。
# 命中时返回深拷贝：_ocr_region 会就地改词框坐标，缓存里必须是原样。
# 急停：环境变量 DSH_OCR_CACHE=0 可以整段关掉。
# ---------------------------------------------------------------------------
_OCR_CACHE = {}
_OCR_CACHE_MAX = 32


def _ocr_cache_on():
    return os.environ.get("DSH_OCR_CACHE", "1") != "0"


def _image_key(path):
    try:
        import hashlib
        with open(path, "rb") as f:
            raw = f.read()
        h = hashlib.sha256()
        h.update(_ocr_engine_tag().encode("utf-8"))
        h.update(raw)
        return h.hexdigest()
    except OSError:
        return None


def _crop_key(im):
    """按图像像素做缓存键：像素一样，识别结果必然一样。"""
    try:
        import hashlib
        h = hashlib.sha256()
        h.update(_ocr_engine_tag().encode("utf-8"))
        h.update(("%dx%dx%s" % (im.width, im.height, im.mode)).encode("ascii"))
        h.update(im.tobytes())
        return h.hexdigest()
    except Exception:
        return None


def _deep_copy(data):
    try:
        return _json.loads(_json.dumps(data, ensure_ascii=False))
    except Exception:
        return data



def emit_result(status, ok=None, **fields):
    """统一的机器可读结果：单行 JSON，前缀 ##RESULT##。

    为什么要它：以前插件靠正则猜输出，于是"OCR 跑挂了""超时了""真的没找到"
    在插件眼里全是"没找到"。status 词表固定，ok 三态必须分得开：
      ok=True   做成了
      ok=False  失败了（not_found / timeout / cancelled / error）
      ok=None   不知道（unverified）—— 既不是成功也不是失败
    """
    payload = {"status": status, "ok": ok}
    payload.update(fields)
    try:
        print(RESULT_MARK + " " + _json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def cancel_requested():
    return os.path.exists(CANCEL_FLAG)


def set_cancel(on=True):
    """写/清急停旗标。返回 True 表示当前状态符合期望。"""
    try:
        if on:
            with open(CANCEL_FLAG, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            return True
        if os.path.exists(CANCEL_FLAG):
            os.remove(CANCEL_FLAG)
        return True
    except OSError as exc:
        print(f"  cancel flag error: {exc}")
        return False


def _abort_if_cancelled(where=""):
    """动作开始前/循环每一轮调用。返回 True 表示已收到急停，调用方应停止。"""
    if cancel_requested():
        CANCEL_HIT["hit"] = True
        tail = f"（{where}）" if where else ""
        print(f"  CANCELLED: 收到急停{tail}，后续动作不再执行")
        return True
    return False


def click(x, y, double=False, right=False, hold_ms=55, steps=4, activate_first=False,
          auto_front=True):
    """A click that actually looks like a human one.

    Three things the old version got wrong, all of which made Qt/self-drawn
    controls (TIA, a lot of installers) swallow the press:
      1. SetCursorPos jumps in one go, so the window never receives a
         WM_MOUSEMOVE and stays "not hovered" -- it ignores the button.
      2. press and release were issued with zero delay; anything under ~20 ms
         reads as a twitch and is dropped.
      3. nothing checked that the target was in front, so the click could land
         on whatever window happened to be active.

    So: bring the target forward (what a real click does anyway), walk the
    pointer in small steps, and hold the button briefly.
    """
    # 2026-11-11：CLI 传进来的坐标是字符串。以前轨迹那段因为取起点失败被整段跳过，
    # 所以 x - sx 这个减法从没执行过；轨迹修好之后它立刻炸了（字符串减整数）。
    # 坐标在入口统一转数字，别再依赖调用方。
    x = float(x)
    y = float(y)
    if _abort_if_cancelled("click"):
        return False
    if activate_first:
        try:
            activate(str(int(activate_first)))
        except Exception:
            pass
    elif auto_front:
        try:
            root = _window_at(x, y)
            fg = user32.GetForegroundWindow()
            # 关键：鼠标下面的窗口如果和前台窗口属于同一个程序（菜单、下拉、弹层
            # 都是这样），绝不能去激活它——一激活弹出菜单就消失，这一次点击就落空。
            if root and int(root) != int(fg) and not _same_process(root, fg):
                activate(str(int(root)))
                time.sleep(0.12)
        except Exception:
            pass
    # walk in, so the target sees real mouse moves
    # 2026-11-11：原来写的是
    #     sx, sy = user32.GetCursorPos(byref(POINT())) or (None, None)
    # GetCursorPos 返回的是 BOOL（成功=1），把它当坐标元组解包必然抛异常，
    # 异常被吞掉后 steps 被改成 1 —— 于是"慢慢划过去"永远退化成瞬移。
    # 菜单、Qt 控件、悬停面板都靠中间移动事件才会亮起，所以这不是观感问题。
    start = _cursor_pos()
    if start is None:
        steps = 1
        sx, sy = int(x), int(y)
    else:
        sx, sy = start
    for i in range(1, max(1, steps) + 1):
        user32.SetCursorPos(int(sx + (x - sx) * i / steps), int(sy + (y - sy) * i / steps))
        time.sleep(0.015)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    down, up = ("right_down", "right_up") if right else ("left_down", "left_up")
    _mouse(MOUSEEVENTF[down])
    time.sleep(max(0.02, hold_ms / 1000.0))
    _mouse(MOUSEEVENTF[up])
    if double:
        time.sleep(0.07)
        _mouse(MOUSEEVENTF[down])
        time.sleep(max(0.02, hold_ms / 1000.0))
        _mouse(MOUSEEVENTF[up])
    print(f"  {'right ' if right else ''}{'double-' if double else ''}click at {int(x)},{int(y)} (hold {hold_ms}ms)")



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


def _wait_until(pred, timeout=1.0, interval=0.05, what=""):
    """轮询到 pred() 为真为止，返回 True；超时返回 False。

    为什么要它：很多地方原来是 `time.sleep(0.x)` 固定等一个"应该已经好了"的状态。
    等够是常态，等不够就偶发失败，等太久就是白等。改成"条件满足就走、满足不了才吃满超时"。
    """
    deadline = time.time() + timeout
    while True:
        try:
            if pred():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _rect_xy(hwnd):
    """只读窗口左上角，给轮询谓词用。

    不能用 _window_info 当谓词：它连 exe 名一起查，轮询里反复调很浪费；
    另外它的 rect 是 tuple，跟 list 比较永远不相等 —— 这个坑本轮踩过，
    谓词恒为 False，固定 sleep 变成了"必定等到超时"，反而更慢。
    """
    from ctypes import wintypes
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (int(r.left), int(r.top))


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
    # 2026-11-10：以前这里调 _printwindow_impl()，那个函数从来不存在 ——
    # NameError 被调用方的 except 吞掉，于是"后台直接抓窗口"这条路一直在静默回退。
    return _grab_window(hwnd, path)


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
            box["ok"] = _grab_window(hwnd, path)
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
user32.ScreenToClient.restype = ctypes.c_int
user32.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = ctypes.c_int
user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINT)]
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
    # 原来是固定 sleep(0.25)：位置通常几毫秒就到位了，白等。改成等它真到。
    _wait_until(lambda: _rect_xy(hwnd) == (tx, ty),
                timeout=0.6, interval=0.02, what="movewin")
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
    # 原来是固定 sleep(0.3)：改成等窗口位置真的动了（拖不动的情况也能早发现）
    _wait_until(lambda: _rect_xy(hwnd) != (wx, wy),
                timeout=0.8, interval=0.02, what="dragwin")
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
    shot_path = _tmp("win", ".png")
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
        # 2026-11-10：CONSOLE_SCREEN_BUFFER_INFO 的 dwSize 是 COORD —— [0] 是列宽，[1] 是行数。
        # 以前读反了，窄控制台（80 列）会被当成几百列去切行，conread 的尾部对不上。
        width = ctypes.cast(csbi, ctypes.POINTER(ctypes.c_int16))[0]
        height = ctypes.cast(csbi, ctypes.POINTER(ctypes.c_int16))[1] + 1
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
            path = _tmp("watch", ".png")
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


def drag(x1, y1, x2, y2, steps=18, hold_ms=120, gap_ms=12):
    """Press, optionally HOLD, then move in small steps and release.

    hold_ms matters for two kinds of target:
      * desktop pets -- they start their own drag only after the button has been
        down for a moment, otherwise press-move-release looks like a click;
      * scrollbar thumbs -- a 10 px wide thumb is easy to miss, and the control
        wants a real "grab" before it follows.
    gap_ms keeps the movements slow enough for the app to receive them as a drag
    rather than a jump. A tiny hover nudge before pressing makes Qt/self-drawn
    widgets accept the press at all (same reason as click()).
    """
    # hover nudge: land near the start first, so the window sees a mouse move
    user32.SetCursorPos(int(x1) - 2, int(y1) - 2)
    time.sleep(0.03)
    user32.SetCursorPos(int(x1), int(y1))
    time.sleep(0.10)
    _mouse(MOUSEEVENTF["left_down"])
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    for i in range(1, steps + 1):
        user32.SetCursorPos(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
        time.sleep(gap_ms / 1000.0)
    time.sleep(0.08)
    _mouse(MOUSEEVENTF["left_up"])
    print(f"  dragged {x1},{y1} -> {x2},{y2} (hold {hold_ms}ms, {steps} steps)")


def hscroll(n):
    """Horizontal wheel -- the reliable way to see what is off to the right.

    Dragging a scrollbar thumb means finding a ~10 px target and grabbing it;
    MOUSEEVENTF_HWHEEL does the same job with one event. Positive = right.
    """
    step = 120
    for _ in range(abs(int(n))):
        if _abort_if_cancelled("hscroll"):
            return False
        _mouse(MOUSEEVENTF["hwheel"], data=step if int(n) > 0 else -step)
        time.sleep(0.04)
    print(f"  hscrolled {n}")


def _patch_gray(region):
    """40x40 grayscale thumbnail of a region, as a flat list of 0..255 ints."""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=tuple(int(v) for v in region), all_screens=True)
    img = img.convert("L").resize((40, 40))
    return list(img.tobytes())


def _patch_diff(a, b):
    """Mean absolute difference, 0.0 (identical) .. 1.0 (black vs white)."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def _around(x, y, pad=90):
    """A square around the click point, clamped to the virtual screen."""
    vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    return (max(vx, int(x) - pad), max(vy, int(y) - pad),
            min(vx + vw, int(x) + pad), min(vy + vh, int(y) + pad))


def _same_process(a, b):
    """True when both window handles belong to one process."""
    try:
        from ctypes import wintypes
        pa, pb = wintypes.DWORD(), wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(int(a)), ctypes.byref(pa))
        user32.GetWindowThreadProcessId(wintypes.HWND(int(b)), ctypes.byref(pb))
        return pa.value != 0 and pa.value == pb.value
    except Exception:
        return False

def _foreground_pid():
    from ctypes import wintypes
    fg = user32.GetForegroundWindow()
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(p))
    return p.value


def _big_window_of(pid):
    """Largest visible top-level window of a process: (hwnd, rect)."""
    from ctypes import wintypes
    best = [0, None]
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[0]:
                best[0] = area
                best[1] = (int(hwnd), (r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return best[1] if best[1] else (0, None)


def _force_foreground(hwnd):
    """SetForegroundWindow that also works when another process owns the foreground."""
    from ctypes import wintypes
    h = wintypes.HWND(int(hwnd))
    if int(user32.GetForegroundWindow()) == int(hwnd):
        return True
    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    tf = user32.GetWindowThreadProcessId(fg, None)
    tt = user32.GetWindowThreadProcessId(h, None)
    user32.AttachThreadInput(cur, tf, True)
    user32.AttachThreadInput(cur, tt, True)
    user32.BringWindowToTop(h)
    user32.SetForegroundWindow(h)
    user32.SetFocus(h)
    user32.AttachThreadInput(cur, tt, False)
    user32.AttachThreadInput(cur, tf, False)
    time.sleep(0.35)
    return int(user32.GetForegroundWindow()) == int(hwnd)


def _jump_click(x, y, hold_ms=95):
    """Teleport then click: no movement trail (a trail opens sibling submenus)."""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.22)
    _mouse(MOUSEEVENTF["left_down"])
    time.sleep(max(0.03, hold_ms / 1000.0))
    _mouse(MOUSEEVENTF["left_up"])


def _titled_windows(pid):
    """Titles of visible top-level windows of a pid (for change detection)."""
    from ctypes import wintypes
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, b, n + 1)
            if b.value:
                out.append(b.value)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out

def menu(menu_title, item, expect=None, pid=None, tries=3, hold_ms=95):
    """Click a menu title then its item, reliably, and prove something happened.

    Why this exists (2026-11-01): a plain click on a TIA menu item sometimes just
    closes the dropdown without running it. Three causes were found and all of
    them are handled here:
      1. the click auto-activated the popup window -> the menu was cancelled;
         so nothing here ever changes focus while a menu is open;
      2. walking the pointer to the item swept over a sibling that owns a
         submenu -> the submenu stole the click; so the pointer JUMPS, no trail;
      3. the action needs a selected target in the tree first -> we verify by
         waiting for a new top-level window whose title contains `expect`.
    """
    from ctypes import wintypes
    if pid is None:
        pid = _foreground_pid()
    hwnd, _rect = _big_window_of(pid)
    if not hwnd:
        print(f"  menu: pid {pid} 没有可见主窗口")
        return False

    for attempt in range(1, int(tries) + 1):
        if not _force_foreground(hwnd):
            print(f"  menu: 第 {attempt} 次抢前台失败")
        t = find_text(menu_title, region=None)
        if t is None:
            print(f"  menu: 找不到菜单「{menu_title}」")
            return False
        _jump_click(t[0], t[1], hold_ms)
        time.sleep(0.85)

        it = find_text(item)
        if it is None:
            print(f"  menu: 第 {attempt} 次菜单没开或找不到「{item}」")
            continue
        before = set(_titled_windows(pid))
        _jump_click(it[0], it[1], hold_ms)

        if expect:
            # 原来先固定等 2.2 秒再轮询：窗口一般 0.3 秒就出来了，白等近两秒。
            # 现在直接等「新窗口出现」，出现就立刻返回。
            deadline = time.time() + 3.0
            hit = []
            while True:
                hit = [w for w in (set(_titled_windows(pid)) - before) if expect in w]
                if hit:
                    print(f"  menu: {menu_title} -> {item} 生效，弹出「{hit[0]}」")
                    return True
                if time.time() >= deadline:
                    break
                time.sleep(0.15)
            print(f"  menu: 第 {attempt} 次点了「{item}」但没等到「{expect}」")
        else:
            # 没有可等的目标，只留一点让菜单收起来的物理间隔
            time.sleep(0.12)
            print(f"  menu: {menu_title} -> {item} 已点击")
            return True
    return False

def _is_menu_popup(hwnd):
    """True when hwnd looks like a pop-up menu (dropping a menu must not be focused)."""
    try:
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 64)
        if buf.value == "#32768":
            return True
        GWL_STYLE = -16
        WS_POPUP = 0x80000000
        style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE) & 0xFFFFFFFF
        GW_OWNER = 4
        owner = user32.GetWindow(wintypes.HWND(hwnd), GW_OWNER)
        fg = user32.GetForegroundWindow()
        if (style & WS_POPUP) and owner and int(owner) == int(fg):
            return True
    except Exception:
        pass
    return False

def _window_at(x, y):
    """Root window under a screen point, or None.

    2026-10-25: this used to pass `wintypes.POINT`, but WindowFromPoint was
    declared with this module's own POINT struct, so ctypes raised
    "expected POINT instance instead of POINT". click() swallowed it inside its
    try/except -- which silently disabled the auto-front step, and broke
    clickcheck's third strategy. Both POINTs must be the same class.
    """
    h = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not h:
        return None
    root = user32.GetAncestor(h, 2)   # GA_ROOT
    return root or h


def _wm_click(x, y):
    """Post WM_LBUTTONDOWN/UP straight to the window under the point.

    Last resort for controls that ignore SendInput entirely: convert the screen
    point to client coordinates and let the message loop do the work.
    """
    hwnd = _window_at(x, y)
    if not hwnd:
        raise OSError("no window under the point")
    pt = POINT(int(x), int(y))
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0201, 1, lp)      # WM_LBUTTONDOWN, MK_LBUTTON
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x0202, 0, lp)      # WM_LBUTTONUP
    print(f"  posted WM_LBUTTON to {hwnd} at client {pt.x},{pt.y}")


def clickcheck(x, y, region=None, tries=3):
    """Click, then LOOK AT THE SCREEN to see whether anything changed.

    The old click() could not tell a successful press from a swallowed one: it
    printed "click at 300,500" whether or not TIA noticed. So: measure the area,
    click, measure again, compare.

    Calibration matters, because part of this desktop is always moving (the pet,
    a clock, a video). So first take two shots 250 ms apart to learn the local
    noise floor, and only call it "changed" when the difference clearly exceeds
    that floor. Otherwise an animating pet would report every click as success
    -- which is exactly the false confidence we are trying to remove.

    Escalates through: normal click -> longer hold -> front the window first ->
    posted WM_LBUTTON message. Returns True on a visible change.
    """
    region = tuple(region) if region else _around(x, y)
    try:
        a = _patch_gray(region)
        time.sleep(0.25)
        before = _patch_gray(region)
    except Exception as e:
        # Locked screen / display asleep: ImageGrab fails and clicks do nothing.
        # Say so instead of reporting a fake success.
        print(f"  clickcheck UNVERIFIED: cannot grab the screen ({e}) -- is it locked?")
        return None
    noise = _patch_diff(a, before)
    threshold = max(0.012, noise * 3.0 + 0.004)
    strategies = [
        ("normal click", lambda: click(x, y)),
        ("long hold 220ms", lambda: click(x, y, hold_ms=220, steps=6)),
        ("front + click", lambda: click(x, y, activate_first=_window_at(x, y))),
        ("WM_LBUTTON message", lambda: _wm_click(x, y)),
    ]
    tried = []
    for name, act in strategies[:max(1, min(4, tries))]:
        try:
            act()
        except Exception as e:
            tried.append(f"{name}: error {e}")
            continue
        time.sleep(0.38)
        try:
            after = _patch_gray(region)
        except Exception:
            print(f"  clickcheck UNVERIFIED after {name} (screen grab failed mid-way)")
            return None
        d = _patch_diff(before, after)
        changed = d > threshold
        tried.append(f"{name}: diff {d:.4f} vs thr {threshold:.4f} -> {'changed' if changed else 'same'}")
        if changed:
            print(f"  clickcheck OK via {name} (diff {d:.4f}, noise {noise:.4f})")
            return True
        before = after
    print(f"  clickcheck NO CHANGE at {x},{y} -- {'; '.join(tried)}")
    return False



def scroll(n):
    """按「格」滚：n 格，一格 120 个滚轮增量。负数是向下。"""
    step = 120
    for _ in range(abs(int(n))):
        if _abort_if_cancelled("scroll"):
            return False
        _mouse(MOUSEEVENTF["wheel"], data=step if int(n) > 0 else -step)
    print(f"  scrolled {n}")


def scroll_delta(delta):
    """按「滚轮增量」滚：120 = 一格，负数向下。

    2026-11-11 修的坑：插件把「一格 120」的 `amount` 原样当格数传进 scroll()，
    于是 amount=-1200 变成了 1200 格 × 120 = 144000 增量，多滚 100 倍。
    现在分成两个入口，各管各的语义，谁也别猜谁。
    """
    d = int(delta)
    if d == 0:
        print("  scrolled 0")
        return
    step = 120 if d > 0 else -120
    notches = max(1, int(round(abs(d) / 120.0)))
    for _ in range(notches):
        if _abort_if_cancelled("scroll_delta"):
            return False
        _mouse(MOUSEEVENTF["wheel"], data=step)
    print(f"  scrolled delta {d} ({notches} notch)")


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


# ================= P0：焦点守卫 / 指纹日志 / 剪贴板卫生 =================
# 2026-10-29 加的。动机（真实事故）：叫醒 agent 去 TIA 里 Ctrl+A/Ctrl+V，
# 结果焦点其实在 DSH 聊天窗口，代码粘进了输入框，而 agent 事后截图看到的是
# TIA，以为成功了 —— 误报成功比不做贵得多。所以：动作前先证明"焦点在这"，
# 动作后留指纹，没法证明就报错中止。
import json as _json

_FP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fingerprint.jsonl")

def _fg_info():
    """当前前台窗口的指纹：hwnd / pid / 标题 / 类名 / owner。"""
    try:
        h = int(user32.GetForegroundWindow())
    except Exception:
        return {"hwnd": 0, "pid": 0, "title": "", "cls": "", "owner": 0}
    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    except Exception:
        pass
    def _text(hn):
        if not hn:
            return ""
        n = 256
        buf = ctypes.create_unicode_buffer(n)
        user32.GetWindowTextW(hn, buf, n)
        return buf.value or ""
    def _class(hn):
        if not hn:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hn, buf, 256)
        return buf.value or ""
    try:
        owner = int(user32.GetWindow(h, 4))  # GW_OWNER
    except Exception:
        owner = 0
    return {
        "hwnd": h,
        "pid": int(pid.value),
        "title": _text(h),
        "cls": _class(h),
        "owner": owner,
        "owner_title": _text(owner) if owner else "",
    }

def _fingerprint():
    info = _fg_info()
    try:
        clip_len = len(clip_get_str() or "")
    except Exception:
        clip_len = -1
    info["clip"] = clip_len
    return info

def _fp(action, before, after, **extra):
    rec = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "before": before,
        "after": after,
    }
    rec.update(extra)
    try:
        with open(_FP_LOG, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _match_focus(spec, info):
    """spec 可以是 hwnd 数字，也可以是标题子串（不区分大小写）。"""
    if spec is None:
        return True
    s = str(spec).strip()
    if s.isdigit():
        return info.get("hwnd") == int(s) or info.get("pid") == int(s)
    return s.lower() in (info.get("title") or "").lower()

def focus_guard(spec, timeout=1.2, quiet=False):
    """动作前守卫：确认前台是 spec 指定的窗口，不是就抢一次再复核。

    返回 True 表示"可以动手"；False 表示"抢不回来，必须中止"。
    不会在失败时继续执行动作 —— 这是这套工具最重要的纪律。
    """
    if spec is None:
        return True
    info = _fg_info()
    if _match_focus(spec, info):
        if not quiet:
            print(f"  focus ok: [{info['pid']}] {info['title'][:60]}")
        return True
    # 抢前台（activate 内部已处理 AttachThreadInput 那套）
    target = None
    spec_s = str(spec).strip()
    if spec_s.isdigit():
        target = int(spec_s)
    else:
        try:
            target = _resolve_window(spec_s)
        except SystemExit:
            target = None
    if target:
        try:
            activate(str(target))
        except Exception as e:
            if not quiet:
                print(f"  focus: activate failed: {e}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info2 = _fg_info()
        if _match_focus(spec, info2):
            if not quiet:
                print(f"  focus armed: [{info2['pid']}] {info2['title'][:60]}")
            return True
        time.sleep(0.12)
    bad = _fg_info()
    print(f"  FOCUS_FAILED expect={spec!r} actual=[{bad['pid']}] {bad['title'][:60]!r} "
          f"owner={bad.get('owner_title','')[:40]!r}")
    return False

_EXPECT = {"v": None}


def _expect_of(argv):
    """从命令行里取 --expect 的值（窗口标题子串或 hwnd）。"""
    if _EXPECT["v"] is not None:
        return _EXPECT["v"]
    for i, a in enumerate(argv):
        if a == "--expect" and i + 1 < len(argv):
            return argv[i + 1]
    return None

def clip_get_str():
    import ctypes as _c
    from ctypes import wintypes as _w
    u = _c.windll.user32
    k = _c.windll.kernel32
    u.OpenClipboard.argtypes = [_w.HWND]
    u.GetClipboardData.restype = _w.HANDLE
    u.GetClipboardData.argtypes = [_w.UINT]
    k.GlobalLock.restype = _c.c_void_p
    k.GlobalLock.argtypes = [_w.HANDLE]
    k.GlobalUnlock.argtypes = [_w.HANDLE]
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(h)
        if not p:
            return ""
        try:
            return _c.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()

# =======================================================================

def _read_focus_text_via_copy(hwnd):
    """把焦点里的文字真正读回来：Ctrl+A → Ctrl+C → 读剪贴板。

    这是**最通用**的验证手段：不依赖 UIA（Electron 不暴露）、不依赖 OCR（中文
    会读错字），只要那个输入框支持全选复制就行——记事本、浏览器、Electron、
    WPS 都支持。代价是会动一下选区，所以读完把光标按 End 收回到末尾。
    返回 (ok, text)：ok=False 表示剪贴板里没拿到东西（比如焦点不在文本框里）。
    """
    bak = clip_get_str()
    got = ""
    try:
        _set_clipboard("")          # 先清空：否则“什么都没复制”会读到上一次的内容
        time.sleep(0.05)
        _key(VK["ctrl"]); _key(VK["a"]); _key(VK["a"], up=True); _key(VK["ctrl"], up=True)
        time.sleep(0.12)
        _key(VK["ctrl"]); _key(VK["c"]); _key(VK["c"], up=True); _key(VK["ctrl"], up=True)
        time.sleep(0.30)
        got = clip_get_str() or ""
    finally:
        try:                          # 全选状态收回到末尾（不影响内容，只是别留个选区）
            _key(VK["end"]); _key(VK["end"], up=True)
        except Exception:
            pass
        try:
            _set_clipboard(bak if bak else "")
        except Exception:
            pass
    return bool(got.strip()), got


def _plain_text(s):
    """去掉空白和标点，只留字——OCR、剪贴板、输入文本三边比较时都用这个。"""
    import re as _re
    return _re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")


def _grab_for_compare(hwnd, tag):
    """给打字前后取两张图，用来做像素差分。"""
    import tempfile
    tmp = _tmp("type-" + str(tag), ".png")
    try:
        os.remove(tmp)
    except OSError:
        pass
    for grab in (_grab_screen, _grab_window):
        try:
            grab(hwnd, tmp)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
                return tmp
        except Exception:
            continue
    return None


def _pixels_changed(p1, p2):
    """两张图差多少（0~1）。用来回答“这次粘贴到底有没有在屏幕上留下痕迹”。

    OCR 读中文会错字（把「-」读成「一」、「2026」读成「202」），所以不能拿
    OCR 当唯一证据；像素差分对语言无感，只回答“变了没有”。
    """
    if not p1 or not p2:
        return None
    try:
        from PIL import Image, ImageChops
        a = Image.open(p1).convert("L")
        b = Image.open(p2).convert("L")
        if a.size != b.size:
            return 1.0
        diff = ImageChops.difference(a, b)
        hist = diff.histogram()
        total = a.size[0] * a.size[1]
        changed = sum(hist[8:])
        return changed / float(total)
    except Exception:
        return None


def _window_text_has(needle, hwnd):
    """打字自校验：把窗口截下来 OCR，看这段字到底进没进去。

    返回 True（找到了）/ False（图里能认字，但没有这段）/ None（验不了）。

    2026-11-11 第一次写这版时踩了坑：PrintWindow 抓 Tk/Electron 窗口拿到
    空白图，OCR 一个字都认不出，于是被判成“没进去”——**截图失败冒充了证据**。
    所以现在：先按屏幕坐标抓（前台窗口所见即所得），抓不到再用 PrintWindow；
    而且只要 OCR 认出的字少于 3 个，一律返回 None（不知道），绝不返回 False。
    """
    import tempfile
    tmp = _tmp("type-verify", ".png")
    try:
        os.remove(tmp)
    except OSError:
        pass
    got = False
    for grab in (_grab_screen, _grab_window):
        try:
            grab(hwnd, tmp)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
                got = True
                break
        except Exception:
            got = False
    if not got:
        return None
    try:
        data = ocr(tmp)
    except SystemExit:
        return None
    words = len(data.get("words", []) or [])
    if words < 3:
        return None                     # 图是空的/花的，没资格当证据
    # 两边都去掉空白和标点再比：OCR 会把「测试-2026」的连字符读出来，
    # 而调用方传进来的 probe 已经去了标点，不去标点就永远比不中（2026-11-11 踩的坑）。
    import re as _re
    def _plain(s):
        return _re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")
    flat = _plain("".join(w["t"] for w in data.get("words", [])))
    target = _plain(needle)
    if not target:
        return None
    return target in flat


def type_text(text, expect=None, verify=False, retries=0, secret=False):
    """剪贴板粘贴（中文唯一可靠路径）。

    2026-11-11 改（Kimi 窗口打不进字那次）：粘贴之后**自己验一遍**。
    以前只报 "typed N chars"，字掉进虚空也照样说成功 —— 那是最坏的失败方式。
    现在 verify=True 时截窗口做 OCR，三种结果分开报：verified / NOT_CONFIRMED /
    UNVERIFIED（验不了 ≠ 失败）。
    默认**不重试**：第一次写这版时重试导致同一个窗被输入三遍，重复比漏输更糟。
    剪贴板复原也挪到校验之后 —— Electron 的粘贴是异步的，0.15 秒就复原会让它粘到空。
    """
    if _abort_if_cancelled("type"):
        LAST_TYPE["status"] = "cancelled"
        return False
    before = _fingerprint()
    if not focus_guard(expect, quiet=True):
        print("  abort: 焦点不在目标窗口，拒绝粘贴（避免把内容粘进别处）")
        LAST_TYPE["status"] = "error"
        return False
    bak = clip_get_str()
    hwnd = before.get("hwnd")
    probe = "".join(text.split())[:12] or text[:12]
    shot_before = _grab_for_compare(hwnd, "before") if (verify and hwnd) else None

    def _paste_once():
        _set_clipboard(text)
        time.sleep(0.12)
        if (clip_get_str() or "") != text:
            _set_clipboard(text)
            time.sleep(0.10)
        _key(VK["ctrl"]); _key(VK["v"]); _key(VK["v"], up=True); _key(VK["ctrl"], up=True)
        time.sleep(0.45)

    _paste_once()
    after = _fingerprint()
    same = after.get("hwnd") == before.get("hwnd")

    verified, ver_src = None, ""
    if verify and hwnd and not secret:
        # ① 最通用也最准：Ctrl+A/Ctrl+C 把焦点里的字复制回来比。
        #    不依赖 UIA（Electron 不暴露）、不依赖 OCR（中文会读错字），
        #    记事本/浏览器/Electron/WPS 都吃这一套。
        ok_copy, got = _read_focus_text_via_copy(hwnd)
        if ok_copy:
            # 2026-11-11 改：以前只比前 20 个归一化字，第 21 个字被截也照样算过，
            # 却还宣称"内容就是刚打的字"。现在整段都必须在框里（可含原有前缀）。
            want = _plain_text(text)
            have = _plain_text(got)
            verified = bool(want) and want in have
            ver_src = "copy"
            if verified:
                print("  读回确认：整段文字都在输入框里")
            else:
                miss = 0
                while miss < len(want) and (want[:len(want) - miss] not in have):
                    miss += 1
                print(f"  读回比对不一致（框里现在 {len(have)} 字，缺了末尾约 {miss} 字）")
        # ② 复制读不到（不是文本框/被禁用）→ 退回 OCR，再退回像素
        if verified is None:
            for i in range(3):
                verified = _window_text_has(probe, hwnd)
                if verified is not False:
                    ver_src = "ocr"
                    break
                time.sleep(0.4)
        if verified is False and ver_src != "copy":
            changed = _pixels_changed(shot_before, _grab_for_compare(hwnd, "after"))
            if changed is not None:
                if changed >= 0.002:
                    verified, ver_src = "pixel", "pixel"
                    print(f"  OCR 没认准（中文 OCR 易错字），但画面变了 {changed:.2%}，判为“像已输入”")
                else:
                    print(f"  OCR 没认准，画面也几乎没变（{changed:.4%}）——判为没进去")

    # 复原剪贴板（原来是空就清空，绝不留代码在剪贴板里）——放在校验之后
    try:
        _set_clipboard(bak if bak else "")
    except Exception:
        pass
    _fp("type", before, after,
        chars=len(text), focus_kept=same,
        # 敏感内容绝不落明文：指纹里只留长度（2026-11-11 之前前 40 字会写进日志）
        head="" if secret else text[:40], verified=verified)
    if not same:
        print("  WARN: 粘贴期间前台窗口变了，内容可能落到别处")
        LAST_TYPE["status"] = "error"
        return False
    if verified is False:
        print(f"  TYPE_NOT_CONFIRMED: 窗口里认得出字，但找不到「{probe}」——字没进去（焦点多半不在输入框里）")
        LAST_TYPE["status"] = "not_confirmed"
        return False
    if verify and verified is None:
        print("  TYPE_UNVERIFIED: 截图是空的或 OCR 认不出字，没法确认（既不是成功也不是失败）")
    tag = {True: ", verified", "pixel": ", 像已输入（像素变化，字未被 OCR 认准）"}.get(verified, "")
    print(f"  typed {len(text)} chars (pasted, clipboard restored{tag})")
    LAST_TYPE["status"] = ("success" if verified in (True, "pixel") else ("unverified" if verify else "success"))
    return True


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
                         capture_output=True, text=True, timeout=60,
                         encoding="utf-8", errors="replace")
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
    # 2026-11-11：只读一次字节。
    # 以前是"先哈希文件、再让 ocr.ps1 自己重读一遍" —— 中间文件若被替换，
    # 就成了「哈希算的是 A、识别的是 B」。现在用同一份字节做哈希和识别。
    import hashlib
    raw = None
    try:
        with open(image, "rb") as f:
            raw = f.read()
    except OSError:
        raw = None
    key = None
    if raw is not None and _ocr_cache_on():
        h = hashlib.sha256()
        h.update(_ocr_engine_tag().encode("utf-8"))
        h.update(raw)
        key = h.hexdigest()
    if key and key in _OCR_CACHE:
        return _deep_copy(_OCR_CACHE[key])
    if raw is not None:
        image_copy = _tmp("ocr-src", ".png")
        try:
            with open(image_copy, "wb") as f:
                f.write(raw)
            image = image_copy
        except OSError:
            pass
    tmp = _tmp("ocr-result", ".json")
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
            with io.open(tmp, encoding="utf-8") as fh:
                data = _json.loads(fh.read())
        except _json.JSONDecodeError as exc:
            raise SystemExit(f"  ocr json broken: {exc}")
        if key:
            if len(_OCR_CACHE) >= _OCR_CACHE_MAX:
                _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
            _OCR_CACHE[key] = _deep_copy(data)
        return data
    out = (res.stdout or "").strip()
    start = out.find("{")
    if start < 0:
        raise SystemExit(f"  ocr failed: {out[:300]}")
    return _json.loads(out[start:])


_RAPID_MODULE = None


def _load_rapid():
    """每次 importlib.exec_module 都会丢掉模块里的引擎；worker 必须复用它。"""
    global _RAPID_MODULE
    if _RAPID_MODULE is not None:
        return _RAPID_MODULE
    import importlib.util as _iu
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_rapid.py")
    spec = _iu.spec_from_file_location("ocr_rapid", path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _RAPID_MODULE = mod
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
    # 2026-11-11：按【这块区域自己的像素】做缓存键。
    # 原来缓存键是整屏，桌面上的时钟和桌宠一直在动，永远命中不了；
    # 现在只要「要识别的那块」没变就直接复用，和整屏上别处在动无关。
    key = _crop_key(im) if _ocr_cache_on() else None
    if key and key in _OCR_CACHE:
        data = _deep_copy(_OCR_CACHE[key])
    else:
        tmp = _tmp("ocr-crop", ".png")
        im.save(tmp)
        data = ocr(tmp)
        if key and data.get("ok"):
            if len(_OCR_CACHE) >= _OCR_CACHE_MAX:
                _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
            _OCR_CACHE[key] = _deep_copy(data)
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
        image = _tmp("ocr-full", ".png")
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
        _scan_png = _tmp("scan-verify", ".png")
        shot(_scan_png)
        data = _ocr_region(_scan_png,
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


# ---------------------------------------------------------------- 界面变化判定
# 思路抄自别人（dsh-cua 的 ④验证）：两张截图缩到 48x48 灰度，逐格比亮度。
# 目的只有一个 —— 别再拿"我点了"当"它变了"。这里给的是可核算的数。
GRID = 48
CELL_DIFF = 10          # 单格亮度差超过这个值，算这一格变了
CHANGED_FRAC = 0.02     # 变了的格子占比超过 2%，才算界面有反应


def _gray_sig(path):
    from PIL import Image
    im = Image.open(path).convert("L").resize((GRID, GRID), Image.BILINEAR)
    return list(im.tobytes())


def changed_score(before, after):
    a, b = _gray_sig(before), _gray_sig(after)
    n = len(a)
    hit = sum(1 for x, y in zip(a, b) if abs(x - y) >= CELL_DIFF)
    diff = sum(abs(x - y) for x, y in zip(a, b)) / float(n)
    return hit / float(n), diff


def cmd_changed(args):
    """pc.py changed --before A.png --after B.png [--min 0.02]
       pc.py changed --baseline B.png [--window N] [--min 0.02]

    第二种是"滚动基线"：拍现在，和 B 比，然后把 B 覆盖成现在，方便连续调用。
    输出里 changed=1/0 是第一判据，frac 是变了多少格，mean 是平均亮度差。
    退出码：0 = 界面变了；2 = 没变。
    """
    before = after = baseline = window = None
    min_frac = CHANGED_FRAC
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--before" and i + 1 < len(args):
            before = args[i + 1]; i += 2
        elif a == "--after" and i + 1 < len(args):
            after = args[i + 1]; i += 2
        elif a == "--baseline" and i + 1 < len(args):
            baseline = args[i + 1]; i += 2
        elif a == "--window" and i + 1 < len(args):
            window = args[i + 1]; i += 2
        elif a == "--min" and i + 1 < len(args):
            min_frac = float(args[i + 1]); i += 2
        else:
            i += 1

    if baseline:
        import tempfile
        cur = _tmp("changed-now", ".png")
        shot(cur, window)
        if not os.path.exists(baseline):
            import shutil
            shutil.copyfile(cur, baseline)
            print(f"  changed=0 frac=0.0000 mean=0.00 baseline=NEW {baseline}")
            return 0
        frac, diff = changed_score(baseline, cur)
        import shutil
        shutil.copyfile(cur, baseline)       # 滚动：这次的结果成为下次的基线
        is_changed = frac >= min_frac
        print(f"  changed={1 if is_changed else 0} frac={frac:.4f} mean={diff:.2f} "
              f"min={min_frac:.3f} baseline={baseline}")
        return 0 if is_changed else 2

    if not before or not after:
        raise SystemExit("  changed: need --before A --after B  (or --baseline B)")
    frac, diff = changed_score(before, after)
    is_changed = frac >= min_frac
    print(f"  changed={1 if is_changed else 0} frac={frac:.4f} mean={diff:.2f} min={min_frac:.3f}")
    return 0 if is_changed else 2




def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    # P0：--expect <值> 是守卫参数，不参与位置参数解析
    _clean, _ex = [], None
    _i = 0
    while _i < len(args):
        if args[_i] == "--expect" and _i + 1 < len(args):
            _ex = args[_i + 1]
            _i += 2
            continue
        _clean.append(args[_i])
        _i += 1
    args = _clean
    _EXPECT["v"] = _ex
    flags = [a for a in args if a.startswith("--")]
    pos = [a for a in args if not a.startswith("--")]
    if cmd == "focus":
        info = _fingerprint()
        print("  hwnd=%s pid=%s clip=%s" % (info["hwnd"], info["pid"], info["clip"]))
        print("  title=%r" % (info["title"],))
        print("  class=%r owner=%r" % (info["cls"], info["owner_title"]))
        if len(args) > 1:
            ok = focus_guard(args[1], quiet=False)
            sys.exit(0 if ok else 4)
    elif cmd == "windows":
        windows()
    elif cmd == "shot":
        window, out, region, do_ocr = None, "shot.png", None, False
        i = 0
        while i < len(args):
            if args[i] == "--window":
                window = args[i + 1]; i += 2
            elif args[i] == "--out":
                out = args[i + 1]; i += 2
            elif args[i] == "--region":
                region = [int(float(v)) for v in args[i + 1].split(",")]; i += 2
            elif args[i] == "--ocr":
                do_ocr = True; i += 1
            else:
                i += 1
        # 裸路径当 --out 用：以前 `shot D:\x.png` 会静默存成 shot.png，找图时白等一场
        if pos and out == "shot.png" and any(ch in pos[0] for ch in (".png", ".jpg", "\\", "/")):
            out = pos[0]
        shot(out, window, region=region)
        if do_ocr:
            print("  --- ocr ---")
            blob = region_text(region, image=out) if region else "".join(
                "".join(w["t"].split()) for w in (ocr_best(out).get("words") or []))
            print("  " + (blob[:1200] if blob else "(no text)"))
    elif cmd == "changed":
        # 界面到底变没变：48x48 灰度差分，给数不给感觉
        sys.exit(cmd_changed(args))
    elif cmd == "click" and "--near" in args:
        # click --near 锚文字 [--dir down|up|left|right] [--dy 30] [--dx 0]
        #       [--pick first|last|top|bottom] [--region x0,y0,x1,y1]
        #       [--verify 文字] [--vregion x0,y0,x1,y1] [--vtimeout 3] [--timeout 5]
        d = {"dir": "down", "dy": 30, "dx": 0, "pick": "first", "region": None,
             "verify": None, "vregion": None, "vtimeout": 3.0, "timeout": 0.0}
        anchor, i = None, 0
        while i < len(args):
            a = args[i]
            if a in ("--dir", "--pick", "--verify"):
                d[a[2:]] = args[i + 1]; i += 2
            elif a in ("--dy", "--dx"):
                d[a[2:]] = int(float(args[i + 1])); i += 2
            elif a in ("--vtimeout", "--timeout"):
                d[a[2:]] = float(args[i + 1]); i += 2
            elif a in ("--region", "--vregion"):
                d[a[2:]] = [int(float(v)) for v in args[i + 1].split(",")]; i += 2
            elif a == "--near":
                anchor = args[i + 1]; i += 2
            else:
                i += 1
        near_click(anchor, dirn=d["dir"], dx=d["dx"], dy=d["dy"], pick=d["pick"],
                   region=d["region"], timeout=d["timeout"], verify=d["verify"],
                   vregion=d["vregion"], vtimeout=d["vtimeout"])
    elif cmd == "click" and "--verify" in args:
        # click x y --verify 文字 [--vregion ...] [--vtimeout 3] [--double]
        verify, vregion, vtimeout = "", None, 3.0
        nums, i = [], 0
        while i < len(args):
            if args[i] == "--verify":
                verify = args[i + 1]; i += 2
            elif args[i] == "--vregion":
                vregion = [int(float(v)) for v in args[i + 1].split(",")]; i += 2
            elif args[i] == "--vtimeout":
                vtimeout = float(args[i + 1]); i += 2
            elif args[i].startswith("--"):
                i += 1
            else:
                nums.append(int(float(args[i]))); i += 1
        click_verify(nums[0], nums[1], verify, vregion=vregion, vtimeout=vtimeout,
                     double="--double" in flags)
    elif cmd == "clickcheck":
        # clickcheck x y [--region x0,y0,x1,y1] [--tries n]
        nums, rg, tr = [], None, 3
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--region" and i + 1 < len(args):
                rg = tuple(int(v) for v in args[i + 1].split(",")); i += 2
            elif a == "--tries" and i + 1 < len(args):
                tr = int(args[i + 1]); i += 2
            else:
                try:
                    nums.append(int(float(a)))
                except Exception:
                    pass
                i += 1
        if len(nums) < 2:
            print("  usage: clickcheck x y [--region x0,y0,x1,y1] [--tries n]")
            sys.exit(2)
        res = clickcheck(nums[0], nums[1], rg, tr)
        if res is None:
            sys.exit(3)      # could not verify (locked screen)
        if res is False:
            sys.exit(2)      # clicked but nothing changed
    elif cmd == "move":
        # 2026-11-10：以前这行 elif 被粘在上一行的行尾注释里，
        # 于是 move 动词整个失踪、而这一句 move() 对 clickcheck 无条件执行（每次校验都把指针挪走）。
        move(pos[0], pos[1])
    elif cmd == "click":
        if not focus_guard(_expect_of(args), quiet=True):
            print("  abort: focus mismatch, refuse to act")
            sys.exit(4)
        before = _fingerprint()
        click(pos[0], pos[1], "--double" in flags, "--right" in flags)
        time.sleep(0.08)
        _fp("click", before, _fingerprint(), x=pos[0], y=pos[1])
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
        needle = ""
        if "--find" in args:
            needle = args[args.index("--find") + 1]
        wintext(int(args[0]), needle)
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
        nums, hold, steps, gap = [], 120, 18, 12
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
        elif "--delta" in args:
            scroll_delta(int(args[args.index("--delta") + 1]))
        else:
            scroll(pos[0])
    elif cmd == "hscroll":
        # hscroll N  (positive = right). Move the pointer into the content区 first.
        px = 0
        for a in args:
            try:
                px = int(float(a))
            except Exception:
                pass
        if "--at" in args:
            k = args.index("--at")
            try:
                x, y = args[k + 1].split(",")[:2]
                user32.SetCursorPos(int(x), int(y))
                time.sleep(0.05)
            except Exception:
                pass
        hscroll(px)
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
    elif cmd == "waitstable":
        # waitstable [--window 标题|hwnd] [--region x0,y0,x1,y1] [--stable 500] [--timeout 8]
        win, rg, stable_ms, to = None, None, 500.0, 8.0
        for i, a in enumerate(args):
            if a == "--window":
                win = args[i + 1]
            elif a == "--region":
                rg = args[i + 1]
            elif a == "--stable":
                stable_ms = float(args[i + 1])
            elif a == "--timeout":
                to = float(args[i + 1])
        res = wait_stable(window=win, region=rg, stable_ms=stable_ms, timeout=to)
        emit_result(
            "success" if res is True else ("cancelled" if CANCEL_HIT["hit"] else
                                           ("timeout" if res is False else "unverified")),
            True if res is True else (False if res is False else None),
            window=win, region=rg, stable_ms=stable_ms)
        if res is False:
            sys.exit(6 if CANCEL_HIT["hit"] else 3)   # 被叫停优先报急停
        if res is None:
            sys.exit(6 if CANCEL_HIT["hit"] else 5)
    elif cmd == "waittext":
        rg, to, gone = None, 20.0, False
        for i, a in enumerate(args):
            if a == "--region":
                rg = tuple(int(v) for v in args[i + 1].split(","))
            elif a == "--timeout":
                to = float(args[i + 1])
            elif a == "--gone":
                gone = True
        if gone:
            ok = wait_gone(pos[0], timeout=to, region=rg)
        else:
            ok = wait_text(pos[0], timeout=to, region=rg)
        emit_result("success" if ok else ("cancelled" if CANCEL_HIT["hit"] else "timeout"),
                    bool(ok), gone=gone, text=pos[0])
        if not ok:
            raise SystemExit(3)
    elif cmd == "clicklabel":
        rg = None
        if "--region" in args:
            rg = tuple(int(v) for v in args[args.index("--region") + 1].split(","))
        click_label(pos[0], region=rg)
    elif cmd == "clipget":
        clip_get()
    elif cmd == "clipset":
        clip_set(args[0] if args else "")
    elif cmd == "cancel":
        # cancel           → 立刻急停（写旗标，正在跑的循环下一步就停）
        # cancel --clear   → 解除急停
        if "--clear" in flags or "--off" in flags:
            ok = set_cancel(False)
            print("  cancel cleared" if ok else "  cancel clear FAILED")
        else:
            ok = set_cancel(True)
            print("  cancel requested（正在跑的动作会在下一步停下）" if ok else "  cancel FAILED")
        if not ok:
            sys.exit(4)
    elif cmd == "doctor":
        doctor()
    elif cmd == "type":
        exp = _expect_of(args)
        if "--secret" in flags:
            ok = type_secret(pos[0] if pos else "", expect=exp)
            emit_result("success" if ok else "error", bool(ok), secret=True)
            if not ok:
                sys.exit(4)
        else:
            txt = args[0] if args and not args[0].startswith("--") else ""
            ok = type_text(txt, expect=exp, verify=("--verify" in flags))
            emit_result(LAST_TYPE["status"], ok, chars=len(txt))
            if not ok:
                sys.exit(4)
    elif cmd == "key":
        if not focus_guard(_expect_of(args), quiet=True):
            print("  abort: focus mismatch, refuse to act")
            sys.exit(4)
        before = _fingerprint()
        key(pos[0])
        time.sleep(0.08)
        _fp("key", before, _fingerprint(), k=pos[0])
    elif cmd == "uia_text":
        needle, limit, i = None, 400, 0
        while i < len(argv):
            if argv[i] == "--window" and i + 1 < len(argv):
                needle = argv[i + 1]; i += 2
            elif argv[i] == "--max" and i + 1 < len(argv):
                limit = int(argv[i + 1]); i += 2
            else:
                i += 1
        if not needle:
            raise SystemExit('  usage: pc.py uia_text --window "标题" [--max 400]')
        _hwnd, lines = uia_text(needle, max_lines=limit)
        for line in lines:
            print(line)
        if not lines:
            print("  uia: 这个窗口没暴露可读控件，退回 OCR")
            emit_result("unverified", None, window=needle, count=0, lines=[])
            sys.exit(5)
        emit_result("success", True, window=needle, count=len(lines), lines=lines)

    elif cmd == "see":
        needle, out, region, i = None, None, None, 0
        while i < len(argv):
            if argv[i] == "--window" and i + 1 < len(argv):
                needle = argv[i + 1]; i += 2
            elif argv[i] == "--out" and i + 1 < len(argv):
                out = argv[i + 1]; i += 2
            elif argv[i] == "--region" and i + 1 < len(argv):
                region = [int(float(v)) for v in argv[i + 1].split(",")]; i += 2
            else:
                i += 1
        info = see_window(needle=needle, out=out, region=region)
        if info["source"] == "ocr":
            print(info["text"])
        emit_result("success", True, source=info["source"], window=needle,
                    shot=info["shot"], count=len(info["lines"]),
                    text=info["text"], lines=info["lines"])

    elif cmd == "hotkey":
        if not focus_guard(_expect_of(args), quiet=True):
            print("  abort: focus mismatch, refuse to act")
            sys.exit(4)
        before = _fingerprint()
        hotkey(pos[0])
        time.sleep(0.08)
        _fp("hotkey", before, _fingerprint(), k=pos[0])
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
        needle = ""
        for i, a in enumerate(args):
            if a == "--image":
                image = args[i + 1]
            elif a == "--find":
                needle = args[i + 1]
        if image is None:
            image = _tmp("ocr-full", ".png")
            shot(image)
        data = ocr_best(image)
        print(f"  ok={data.get('ok')} engine={data.get('engine', 'windows')} lines={len(data.get('lines', []))}")
        for line in data.get("lines", [])[:25]:
            print("   " + line)
    elif cmd == "menu":
        title, item = argv[1], argv[2]
        expect = None
        pid = None
        rest = argv[3:]
        i = 0
        while i < len(rest):
            if rest[i] == "--expect":
                expect = rest[i + 1]
                i += 2
            elif rest[i] == "--pid":
                pid = int(rest[i + 1])
                i += 2
            else:
                i += 1
        ok = menu(title, item, expect=expect, pid=pid)
        print("  menu:", "OK" if ok else "FAILED")

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
            elif a == "--window":
                # 2026-11-11 补：以前 --window 会被当成普通参数塞进 rest 里悄悄丢掉，
                # 于是"只在某个窗口里找"其实整屏都扫了（又慢又容易撞到别处的同名文字）。
                opts["window"] = args[i + 1]
                i = i + 2
            elif a == "--scale":
                opts["scale"] = int(args[i + 1])
                i = i + 2
            else:
                rest.append(a)
                i = i + 1
        fn = find_text if cmd == "findtext" else click_text
        _win = opts.pop("window", None)
        if _win:
            _w = _window_info(_resolve_window(_win))
            _x, _y, _ww, _wh = _w["rect"]
            opts["region"] = (_x, _y, _x + _ww, _y + _wh)
            print(f"  scope: 只在窗口「{_w['title'][:40]}」里找 ({_x},{_y}) {_ww}x{_wh}")
        try:
            res = fn(rest[0], None, **opts)
        except SystemExit as exc:
            # 引擎层面的问题（OCR 跑不起来、锚点找不到）不能算"没找到"
            emit_result("error", False, kind="engine", detail=str(exc)[:200])
            raise
        if cmd == "findtext":
            if res:
                emit_result("success", True, text=rest[0], point=[res[0], res[1]])
            else:
                emit_result("not_found", False, text=rest[0])
        else:
            emit_result("success" if res else "not_found", bool(res), text=rest[0])
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


def uia_text(needle, max_lines=400):
    """Read a window's strings through UI Automation instead of OCR.

    Why: OCR costs ~0.8 s a screen and mangles Chinese; UIA hands back exact
    strings (measured on the DSH window: 301 lines in 0.55 s). Not every window
    exposes its tree — Explorer returned 0 lines — so callers keep an OCR fallback.
    """
    hwnd = _resolve_window(needle)
    out = _uia("text", hwnd, "", int(max_lines))
    lines = []
    for line in out.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if s.startswith("TOTAL ") or s.startswith("ERROR "):
            continue
        lines.append(s)
    return hwnd, lines


def see_window(needle=None, out=None, region=None, min_uia_lines=12, max_lines=400):
    """Screenshot + text: UIA first when a window is named, OCR as fallback.

    截图始终拍 —— 文字可以来自 UIA，但"当时屏幕长什么样"只有图能作证。
    """
    path = out or _tmp("see", ".png")
    shot(path, window=needle, region=region)
    print(f"  saved {path}")
    source, lines, uia_err = "ocr", [], None

    if needle:
        try:
            _h, uia_lines = uia_text(needle, max_lines=max_lines)
        except SystemExit as exc:
            uia_lines = []
            uia_err = str(exc)[:200]
        if len(uia_lines) >= int(min_uia_lines):
            source, lines = "uia", uia_lines
            print(f"  UIA: {len(lines)} 行（跳过 OCR）")
        elif uia_lines:
            print(f"  UIA 只给了 {len(uia_lines)} 行，退回 OCR")

    if source == "ocr":
        data = ocr_best(path) or {}
        lines = [str(l) for l in (data.get("lines") or []) if str(l).strip()]

    text = "\n".join(lines)
    return {"source": source, "lines": lines, "text": text,
            "shot": path, "uia_error": uia_err}


def screen_text(region=None):
    """截全屏 → OCR。不打印（轮询里用，打印会把日志刷爆）。"""
    from PIL import ImageGrab
    tmp = _tmp("wait", ".png")
    ImageGrab.grab(all_screens=True).save(tmp)
    data = ocr_best(tmp, region) or {}
    return data.get("lines") or [], data.get("words") or []


def region_text(region, image=None, scale=1):
    """OCR just one rectangle of the screen and return it as one flat string.

    Used by --verify and by --gone: the judgement is "does this region say X",
    so there is no reason to pay for a full-screen OCR (4 s vs under 1 s).

    If `image` is given it is assumed to be ALREADY cropped to that region
    (i.e. it came from shot(..., region=...)), so it is OCR'd as a whole.
    """
    p = image or _tmp("region-text", ".png")
    if image is None:
        shot(p, region=region)
    # 小区域里的字太小，OCR 会吞字（实测「外部源文件」被读成「外部源件」）——
    # 宽 < 700 时先放大两倍再识别，这一步比换引擎更有效。
    try:
        from PIL import Image
        im = Image.open(p)
        if im.size[0] < 700:
            big = p.replace(".png", "_2x.png")
            im.resize((im.size[0] * 2, im.size[1] * 2), Image.LANCZOS).save(big)
            p = big
    except Exception:
        pass
    data = ocr_best(p)
    if not data.get("ok"):
        return ""
    return "".join("".join(w.get("t", "").split()) for w in data.get("words", []))


def near_click(anchor, dirn="down", dx=0, dy=30, pick="first", region=None,
               timeout=0.0, verify=None, vregion=None, vtimeout=3.0):
    """Click a fixed offset from a piece of text, instead of a guessed pixel.

    TIA taught this the hard way: the menu item "从源生成块" is 40 px below the
    row it belongs to, and every re-render moved it by a few pixels. Anchoring on
    the text and offsetting is stable; hard-coded coordinates are not.

    dirn: down / up / left / right. dy/dx are the offsets in pixels.
    """
    t0 = time.time()
    while True:
        hit = find_text(anchor, region=region, pick=pick)
        if hit:
            break
        if time.time() - t0 >= timeout:
            raise SystemExit(f"  near_click: anchor {anchor!r} not found (timeout {timeout}s)")
        time.sleep(0.8)
    ax, ay = hit
    if dirn == "down":
        x, y = ax + dx, ay + dy
    elif dirn == "up":
        x, y = ax + dx, ay - dy
    elif dirn == "left":
        x, y = ax - dx, ay + dy
    else:
        x, y = ax + dx, ay + dy
    print(f"  near_click: anchor {anchor!r} at ({ax},{ay}) -> click ({x},{y}) dir={dirn}")
    click(x, y)
    if verify:
        vreg = vregion or (x - 220, y - 60, x + 220, y + 90)
        ok = wait_text(verify, timeout=vtimeout, region=vreg)
        if not ok:
            raise SystemExit(f"  near_click: clicked but {verify!r} never appeared near ({x},{y})")
    return x, y


def click_verify(x, y, verify, vregion=None, vtimeout=3.0, double=False):
    """Click, then read a small region to prove the click did something."""
    click(x, y, double=double)
    vreg = vregion or (int(x) - 220, int(y) - 80, int(x) + 260, int(y) + 110)
    ok = wait_text(verify, timeout=vtimeout, region=vreg)
    if not ok:
        raise SystemExit(f"  click_verify: after clicking ({x},{y}) the text {verify!r} is not in {vreg}")
    print(f"  click_verify: ({x},{y}) confirmed by {verify!r}")
    return True


def _hwnd_by_title(title_part):
    """按标题子串找窗口句柄；多个命中只认第一个，并在日志里说明。"""
    part = (title_part or "").lower()
    hits = [r for r in windows() if part in (r[6] or "").lower()]
    if not hits:
        print(f"  no window title contains: {title_part}")
        return None
    if len(hits) > 1:
        print(f"  note: {len(hits)} windows match {title_part!r}，用第一个 hwnd={hits[0][0]}")
    return int(hits[0][0])


def _snap_image(window=None, region=None):
    """截一张图（内存里的 PIL 对象），给「等界面稳定」用。

    window 可以是 hwnd 数字，也可以是标题子串；都没有就截前台窗口。
    region 优先：给了 region 就只截那一块（少受桌宠、时钟、视频干扰）。
    """
    from PIL import ImageGrab
    try:
        if region:
            # region 既可能是 "x0,y0,x1,y1" 字符串，也可能是列表/元组；
            # 直接 for v in 字符串 会把逗号也当坐标（'0,600,500,1040' → int(',')）。
            if isinstance(region, str):
                region = [v for v in region.replace(" ", "").split(",") if v]
            x0, y0, x1, y1 = [int(float(v)) for v in region]
            return ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
        hwnd = None
        if window:
            s = str(window).strip()
            if s.isdigit():
                hwnd = int(s)
            else:
                hwnd = _hwnd_by_title(s)
                if hwnd is None:
                    # 说好了等某个窗口，却找不到它 —— 绝不能偷偷去截前台窗口，
                    # 那会把“量错了对象”伪装成“界面稳定了”。
                    print(f"  UNVERIFIED: 找不到窗口 {window!r}，不拿别的窗口顶替")
                    return None
        if hwnd is None:
            hwnd = int(user32.GetForegroundWindow())
        x, y, w, h = _window_info(hwnd)["rect"]
        if w < 8 or h < 8:
            return None
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    except Exception:
        return None


def _image_diff_ratio(a, b):
    """两张图的差异比例（0~1）。对语言无感，只回答“画面动了多少”。"""
    if a is None or b is None:
        return None
    try:
        from PIL import ImageChops
        ga, gb = a.convert("L"), b.convert("L")
        if ga.size != gb.size:
            return 1.0
        diff = ImageChops.difference(ga, gb)
        hist = diff.histogram()
        total = ga.size[0] * ga.size[1]
        return sum(hist[8:]) / float(total)
    except Exception:
        return None


def _snap_window_only(hwnd):
    """只截窗口自己的内容（PrintWindow），不含压在它上面的桌宠/弹窗。

    屏幕截图会被别的窗口污染：桌宠是动画的，量“这个窗口稳不稳”会被它带偏。
    PrintWindow 渲染的是窗口本身，正好用来把两者分开。
    """
    import tempfile
    from PIL import Image
    tmp = _tmp("stab", ".png")
    try:
        os.remove(tmp)
    except OSError:
        pass
    try:
        _grab_window(int(hwnd), tmp)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            return Image.open(tmp).copy()
    except Exception:
        pass
    return None


def wait_stable(window=None, region=None, stable_ms=500, timeout=8.0,
                threshold=0.004, poll=0.18):
    """等目标区域**停止变化**：连续 stable_ms 内画面差异都低于 threshold 才算稳。

    为什么要它：点完按钮界面还在动画，立刻截图/OCR 读到的是旧帧，于是
    “点了没反应”“字没进去”这类误判全来了。稳定后再判断，判据才可信。

    两路同时看：
      - 屏幕截图（所见即所得，但会被桌宠、弹窗这类覆盖物带偏）
      - 窗口自绘（PrintWindow，只含窗口自己，但个别 Electron 会返回旧帧）
    任一路稳住就判稳，并说清是哪一路 —— 免得把“桌宠在动”误判成“窗口没稳”。

    返回：True=稳了；False=超时还在动；None=截不到图（不知道，不是失败）。
    """
    need = max(0.1, stable_ms / 1000.0)
    hwnd = None
    if window:
        s = str(window).strip()
        hwnd = int(s) if s.isdigit() else _hwnd_by_title(s)
        if hwnd is None:
            print(f"  UNVERIFIED: 找不到窗口 {window!r}，不拿别的窗口顶替")
            return None
    elif not region:
        hwnd = int(user32.GetForegroundWindow())

    t0 = time.time()
    ps = _snap_image(window, region)
    pw = _snap_window_only(hwnd) if hwnd else None
    if ps is None and pw is None:
        print("  UNVERIFIED: 截不到图，无法判断界面是否稳定")
        return None

    settled = {"screen": None, "window": None}
    last = {}
    while True:
        if _abort_if_cancelled("wait_stable"):
            return False
        time.sleep(poll)
        cs = _snap_image(window, region)
        cw = _snap_window_only(hwnd) if hwnd else None
        now = time.time()

        for key, prev, cur in (("screen", ps, cs), ("window", pw, cw)):
            if prev is None or cur is None:
                continue
            ratio = _image_diff_ratio(prev, cur)
            if ratio is None:
                continue
            last[key] = ratio
            if ratio <= threshold:
                if settled[key] is None:
                    settled[key] = now
            else:
                settled[key] = None
        ps, pw = cs, cw

        for key in ("screen", "window"):
            if settled[key] and now - settled[key] >= need:
                detail = "，".join(f"{k} {v:.2%}" for k, v in last.items())
                if key == "window" and settled["screen"] is None:
                    print(f"  stable(窗口自身): 盖在它上面的东西还在动（{detail}），"
                          f"但窗口内容 {need:.2f}s 没变 → 判稳")
                else:
                    print(f"  stable: 画面 {need:.2f}s 没变（{detail}），"
                          f"用了 {now - t0:.1f}s")
                return True

        if now - t0 >= timeout:
            detail = "，".join(f"{k} {v:.2%}" for k, v in last.items()) or "无数据"
            print(f"  UNSTABLE: {timeout}s 内没稳下来（{detail}）")
            return False


def _seen(text, region):
    """Is `text` on screen right now? With a region, crop+upscale first."""
    needle = _norm(text)
    if not needle:
        return False
    if region:
        blob = _norm(region_text(region))
    else:
        try:
            lines, _words = screen_text(None)
        except Exception:
            lines = []
        blob = _norm("".join(lines))
    return needle in blob


def wait_gone(text, timeout=30.0, region=None, interval=0.7):
    """Wait until a phrase disappears (busy cursors, "请稍候", progress text)."""
    t0 = time.time()
    while True:
        if _abort_if_cancelled("wait_gone"):
            return False
        if not _seen(text, region):
            print(f"  gone: {text!r} disappeared after {time.time() - t0:.1f}s")
            return True
        if time.time() - t0 >= timeout:
            print(f"  timeout after {timeout}s: {text!r} still there")
            return False
        time.sleep(interval)


def wait_text(text, timeout=20.0, region=None, interval=0.7):
    """等到屏幕（或某块区域）出现这段文字为止。

    以前只能 sleep 固定秒数：页面慢就等不够、快就白等。这是本轮最常缺的东西。
    """
    needle = _norm(text)
    t0 = time.time()
    while True:
        # 2026-11-11 补：以前只给 wait_gone 挂了急停，wait_text 漏了 —— 急停按下去
        # 它照样等到超时（实测等满 20 秒），等于没停。
        if _abort_if_cancelled("wait_text"):
            return False
        if _seen(text, region):
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
        if _abort_if_cancelled("scroll_to"):
            return False
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


def type_secret(text, expect=None):
    """输入敏感内容（密码/令牌）：粘完立刻清空剪贴板，且**绝不落明文**。

    2026-11-11 修的坑：以前这里直接 type_text(text)，既不传焦点守卫，也不看
    返回值 —— 字没打进去照样打印 "secret typed"，调用方以为成功了。
    而且 type_text 会把前 40 字（密码本体）写进 fingerprint.jsonl。
    现在：守卫照走、结果照报、指纹只记长度。
    """
    ok = type_text(text, expect=expect, verify=False, secret=True)
    time.sleep(0.12)
    _set_clipboard("")          # 不管成功与否都清掉
    if not ok:
        print("  SECRET_NOT_TYPED: 焦点或粘贴失败，密码没有输进去（剪贴板已清）")
        return False
    print("  secret typed, clipboard cleared")
    return True


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



class _WorkerTee:
    """原日志实时写 stderr，同时留一份给响应 text；stdout 只走 JSON Lines。"""

    def __init__(self, stream):
        self.stream = stream
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        self.stream.write(text)
        self.stream.flush()
        return len(text)

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return False

    def text(self):
        return "".join(self.parts)


def _worker_request(req):
    """在一个请求里复用 CLI 分派；SystemExit 只结束请求，不结束 worker。"""
    import contextlib
    import traceback

    request_id = req.get("id")
    tool = req["tool"]
    args = req["args"]
    # 这些是单次 CLI 的状态，不能漏到下一请求；OCR/临时文件状态则刻意保留。
    CANCEL_HIT["hit"] = False
    LAST_TYPE["status"] = "unknown"
    _EXPECT["v"] = None

    tee = _WorkerTee(sys.stderr)
    code = 0
    with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
        try:
            main([tool, *args])
        except SystemExit as exc:
            if exc.code is None:
                code = 0
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                code = 1
                print(str(exc.code))       # 与 CLI 的 SystemExit("消息") 一致
        except Exception:
            code = 1
            traceback.print_exc()
        else:
            if CANCEL_HIT["hit"]:
                code = 6

    text = tee.text()
    data = None
    for line in reversed(text.splitlines()):
        if not line.startswith(RESULT_MARK):
            continue
        try:
            value = _json.loads(line[len(RESULT_MARK):].strip())
            if isinstance(value, dict):
                data = value
                break
        except Exception:
            pass

    if data and "status" in data:
        status = data["status"]
        ok = data.get("ok", status == "success")
    elif code == 0:
        status, ok = "success", True
    elif code == 6:
        status, ok = "cancelled", False
    elif code == 5:
        status, ok = "unverified", None
    elif code == 3:
        status, ok = "timeout", False
    else:
        status, ok = "error", False
    # code 是附加字段：插件需要保留 CLI 的 2/3/4/5/6 退出码语义。
    return {"id": request_id, "ok": ok, "status": status,
            "text": text, "data": data, "code": code}


def worker_loop():
    """新增模式：pc.py --worker。普通 pc.py <verb> ... 完全不走这里。"""
    for raw in sys.stdin:
        request_id = None
        try:
            req = _json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            request_id = req.get("id")
            if not isinstance(req.get("tool"), str) or not req["tool"]:
                raise ValueError("tool must be a non-empty string")
            if not isinstance(req.get("args"), list) or not all(isinstance(a, str) for a in req["args"]):
                raise ValueError("args must be an array of strings")
            response = _worker_request(req)
        except Exception as exc:
            print("worker request error: %s" % exc, file=sys.stderr, flush=True)
            response = {"id": request_id, "ok": False, "status": "error",
                        "text": "  worker request error: %s\n" % exc,
                        "data": None, "code": 1}
        sys.stdout.write(_json.dumps(response, ensure_ascii=False,
                                     separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker_loop()
    else:
        main(sys.argv[1:])
        # 急停打断过的调用用专门退出码，别让上层把"被叫停"当成"正常做完"
        if CANCEL_HIT["hit"]:
            sys.exit(6)
