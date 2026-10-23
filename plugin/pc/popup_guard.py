"""通用弹窗守卫：识别意外弹窗，只点「安全」按钮，其余交给人。

为什么不能只匹配标题关键词：以后会遇到的弹窗五花八门（打印机、磁盘占用、
更新提示、许可证、崩溃上报、保存确认……），标题永远列不完。

分三层处理：

1. 识别（不靠标题）
   - 标准对话框：#32770 类名、或有 owner、或有 WS_DLGMODALFRAME
   - 只要它「新出现」才算事件（启动时先拍基线，避免误点用户正在用的窗口）
2. 决策（靠按钮文案，不靠猜）
   - SAFE   取消 / 否 / 关闭 / 以后再说 / 稍后 / 不保存 / 跳过 / 退出 / 忽略
   - UNSAFE 确定 / 是 / 保存 / 覆盖 / 删除 / 允许 / 继续（这些可能毁数据，绝不自动点）
   - 只有 SAFE → 用 BM_CLICK 点它（消息点击，不抢焦点）
   - 只有 UNSAFE → 不点，记进 blocked 列表，让主鲸去问人
3. 记录
   - 每个动作写 popup_guard.log：小时:分:秒 类名 标题 按钮 结果

用法:
    python popup_guard.py --seconds 120     # 后台守 2 分钟
    python popup_guard.py --list            # 只看当前有哪些对话框（只读）
"""

import ctypes
import ctypes.wintypes as wintypes
import io

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
import os
import sys
import time

u = ctypes.windll.user32
u.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u.EnumChildWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4)]
u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

GWL_EXSTYLE = -20
WS_EX_DLGMODALFRAME = 0x0001
BM_CLICK = 0x00F5

SAFE = ("取消", "否", "关闭", "以后再说", "稍后", "不保存", "跳过", "退出",
        "忽略", "下次再说", "continue waiting" , "cancel", "no", "close", "skip", "later")
UNSAFE = ("确定", "是", "保存", "覆盖", "删除", "允许", "继续", "重试", "ok", "yes",
          "save", "delete", "allow", "retry")

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "popup_guard.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def win_text(hwnd):
    b = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, b, 512)
    return b.value


def win_class(hwnd):
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, b, 256)
    return b.value


def win_rect(hwnd):
    r = (ctypes.c_long * 4)()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r[0], r[1], r[2] - r[0], r[3] - r[1]


def pid_of(hwnd):
    pid = ctypes.c_ulong()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def top_windows():
    rows = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        if u.IsWindowVisible(hwnd):
            rows.append(int(hwnd))
        return True

    u.EnumWindows(proc(cb), 0)
    return rows


def buttons_of(hwnd):
    """[(hwnd, label)] for standard buttons inside a dialog."""
    out = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(child, _l):
        cls = win_class(child)
        label = win_text(child)
        if cls.lower() == "button" and label:
            out.append((int(child), label))
        return True

    u.EnumChildWindows(hwnd, proc(cb), 0)
    return out


def looks_like_dialog(hwnd):
    cls = win_class(hwnd)
    title = win_text(hwnd)
    ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    owner = u.GetWindow(hwnd, 4)          # GW_OWNER
    x, y, w, h = win_rect(hwnd)
    if cls == "#32770":
        return True
    if ex & WS_EX_DLGMODALFRAME:
        return True
    # 有 owner 的小窗口 + 有按钮，基本就是模态框
    if owner and 0 < w < 900 and 0 < h < 700 and buttons_of(hwnd):
        return True
    return False


def dialogs():
    out = []
    for hwnd in top_windows():
        if looks_like_dialog(hwnd):
            btns = buttons_of(hwnd)
            if btns:
                out.append({"hwnd": hwnd, "title": win_text(hwnd),
                            "class": win_class(hwnd), "pid": pid_of(hwnd),
                            "rect": win_rect(hwnd), "buttons": btns})
    return out


def pick_button(dlg):
    """Return (hwnd, label, kind) for the safest actionable button."""
    safe = [(b, l) for b, l in dlg["buttons"] if any(s in l.lower() for s in SAFE)]
    if safe:
        return safe[0][0], safe[0][1], "safe"
    unsafe = [(b, l) for b, l in dlg["buttons"] if any(s in l.lower() for s in UNSAFE)]
    if unsafe:
        return unsafe[0][0], unsafe[0][1], "unsafe"
    return None


def handle(dlg, dry=False):
    choice = pick_button(dlg)
    if choice is None:
        log(f"UNKNOWN  hwnd={dlg['hwnd']} title={dlg['title']!r} buttons={[l for _, l in dlg['buttons']]}")
        return "unknown"
    btn, label, kind = choice
    if kind == "unsafe":
        log(f"BLOCKED  {dlg['title']!r} 只找到不安全按钮 {label!r}（不自动点，交给人）")
        return "blocked"
    if dry:
        log(f"WOULD-CLOSE  {dlg['title']!r} -> {label!r}")
        return "dry"
    u.SendMessageW(btn, BM_CLICK, 0, 0)
    log(f"CLOSED   {dlg['title']!r} -> {label!r}")
    return "closed"


def main():
    args = sys.argv[1:]
    if "--list" in args:
        found = dialogs()
        if not found:
            print("  当前没有对话框")
        for d in found:
            print(f"  hwnd={d['hwnd']} pid={d['pid']} class={d['class']} title={d['title']!r}")
            print(f"     按钮: {[l for _, l in d['buttons']]}")
        return

    seconds = 120
    if "--seconds" in args:
        seconds = int(args[args.index("--seconds") + 1])

    baseline = set(top_windows())
    log(f"guard start ({seconds}s), baseline={len(baseline)} windows")
    deadline = time.time() + seconds
    stats = {"closed": 0, "blocked": 0, "unknown": 0}
    while time.time() < deadline:
        for d in dialogs():
            if d["hwnd"] in baseline:      # 开守之前就在的，不动
                continue
            result = handle(d)
            stats[result] = stats.get(result, 0) + 1
            time.sleep(0.3)
        time.sleep(0.7)
    log(f"guard end {stats}")


if __name__ == "__main__":
    main()
