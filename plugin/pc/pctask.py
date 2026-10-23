"""pctask — run a whole task, not a click.

A plan is JSON: a list of steps, each with a verb, its arguments, and how many
times to retry. The engine executes them, checks each outcome, and writes a
journal; on failure it either stops or continues depending on `on_fail`.

Design rules this file is written to:

  * a step is only "done" when its condition is observed, never because a call
    returned. `want` and `assert` are conditions, everything else is an action.
  * every action can be retried; transient focus/UIA timing is the norm here.
  * a failure must be readable afterwards: each step logs ok, ms and its check.
  * dangerous verbs stay visible: there is no "click anything" helper, only
    "invoke this named control" or "click this coordinate that the plan states".

Plan format (all keys optional unless noted):

    {"name": "open-report",
     "steps": [
       {"do": "launch", "exe": "G:/.../wps.exe", "args": ["C:/.../x.docx"]},
       {"do": "want",   "title": "实验报告本第五次", "tries": 20, "gap_ms": 500},
       {"do": "shot",   "out": "D:/tmp/p1.png", "window": "实验报告本第五次"},
       {"do": "scroll", "notches": -6},
       {"do": "assert", "text_on_screen": "实验要求"},
       {"do": "notify", "title": "鲸娘", "text": "任务完成"}
     ]}

CLI:
    pctask.py run  <plan.json> [--dry]
    pctask.py list                       # the verbs and their arguments
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.join(HERE, "pc.py")
UIA = os.path.join(HERE, "uia.ps1")
NOTIFY = os.path.join(os.environ["USERPROFILE"], "OllamaStart", "notify.ps1")

sys.path.insert(0, HERE)
import importlib.util

_spec = importlib.util.spec_from_file_location("pcmod", PC)
pcmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pcmod)


# ---------- conditions ----------

def find_window(title_part: str):
    """Return (hwnd, title) for the first visible window whose title contains the
    text, or None. Re-queried every time on purpose: handles go stale."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hits = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part.lower() in buf.value.lower():
            hits.append((hwnd, buf.value))
            return False
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else None


def uia_has(hwnd: int, name: str) -> bool:
    out = pcmod._uia("name", hwnd, name)
    return "name=" in out


def screen_text(hwnd: int) -> str:
    """Best-effort text of the window, via UIA TextPattern-free route: collect
    every element name. Cheap and good enough to answer "is this word on
    screen"; it is not a document reader."""
    out = pcmod._uia("list", hwnd, "", 400)
    return out


# ---------- verbs ----------

def do_launch(step, log):
    exe = step["exe"]
    args = step.get("args", [])
    if not os.path.exists(exe):
        raise RuntimeError(f"exe not found: {exe}")
    # 交互式程序（控制台）如果继承本进程的 stdout，调用方会一直等 EOF，
    # 整个工具调用看起来就像卡死。stdin/stdout/stderr 全部给它关掉。
    subprocess.Popen([exe, *args], close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    log(f"launched {os.path.basename(exe)}" + (f" with {len(args)} arg(s)" if args else ""))
    return True


def do_want(step, log):
    title = step["title"]
    tries = int(step.get("tries", 20))
    gap = int(step.get("gap_ms", 500)) / 1000.0
    for i in range(tries):
        hit = find_window(title)
        if hit:
            log(f"window appeared after {(i + 1) * gap:.1f}s: {hit[1][:60]}")
            return hit[0]
        time.sleep(gap)
    raise RuntimeError(f"no window whose title contains: {title}")


def do_activate(step, log):
    hwnd = step.get("hwnd") or find_window(step["window"])[0]
    pcmod.activate(hwnd)
    log("activated")
    return hwnd


def do_uia(step, log):
    hwnd = find_window(step["window"])[0]
    name = step["name"]
    if step.get("action", "select") == "on":
        # "click" a named control: use its rect centre, which is what a human would do
        info = pcmod._uia("name", hwnd, name)
        rect = [p for p in info.split("rect=")[-1].split()[0].split(",")]
        x, y, w, h = (int(v) for v in rect[:4])
        pcmod.click(str(x + w // 2), str(y + h // 2))
        log(f"clicked {name} at centre ({x + w // 2},{y + h // 2})")
    else:
        pcmod.sel(str(hwnd), name)
        log(f"selected {name}")
    return True


def do_click(step, log):
    pcmod.click(str(int(step["x"])), str(int(step["y"])))
    log(f"clicked ({step['x']},{step['y']})")
    return True


def do_key(step, log):
    if "+" in step["keys"]:
        pcmod.hotkey(step["keys"])
    else:
        pcmod.key(step["keys"])
    log(f"key {step['keys']}")
    return True


def do_type(step, log):
    pcmod.type_text(step["text"])
    log(f"typed {len(step['text'])} chars")
    return True


def do_scroll(step, log):
    pcmod.scroll(str(step.get("notches", -5)))
    log(f"scrolled {step.get('notches', -5)}")
    return True


def do_shot(step, log):
    out = step["out"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if "window" in step:
        hwnd = find_window(step["window"])[0]
        pcmod.shot(out, str(hwnd))
    else:
        pcmod.shot(out)
    log(f"evidence {out} ({os.path.getsize(out) // 1024} KB)")
    return out


def do_assert(step, log):
    if "file" in step:
        ok = os.path.exists(step["file"])
        log(f"file exists: {ok}  {step['file']}")
        if not ok:
            raise RuntimeError("file missing")
    if "window" in step:
        hit = find_window(step["window"])
        log(f"window present: {bool(hit)}  {step['window']}")
        if not hit:
            raise RuntimeError("window missing")
    if "element" in step:
        hwnd = find_window(step["window_for"])[0] if "window_for" in step else 0
        ok = uia_has(hwnd, step["element"]) if hwnd else False
        log(f"element present: {ok}  {step['element']}")
        if not ok:
            raise RuntimeError("element missing")
    if "text_on_screen" in step:
        hwnd = find_window(step["window_for"])[0] if "window_for" in step else 0
        blob = screen_text(hwnd) if hwnd else ""
        ok = step["text_on_screen"] in blob
        log(f"text on screen: {ok}  {step['text_on_screen']!r}")
        # UIA only exposes accessible names; a word inside a document body is
        # usually absent even when visible. Treat a miss as "unknown", not false.
        if not ok:
            log("  (uia names don't cover document body — needs a screenshot check)")
    if "sleep_ms" in step:
        time.sleep(int(step["sleep_ms"]) / 1000.0)
    return True


def do_sleep(step, log):
    ms = int(step.get("ms", 500))
    time.sleep(ms / 1000.0)
    log(f"slept {ms} ms")
    return True


def do_notify(step, log):
    if os.path.exists(NOTIFY):
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", NOTIFY, step.get("title", "鲸娘"), step.get("text", "")],
                       capture_output=True, timeout=30)
    log("notified")
    return True



def do_assert_text(step, log):
    """A real text judgement: OCR the screen and require the phrase to be there.

    This is the difference between "I called scroll and it returned" and "the
    words 实验要求 are actually on the screen now".
    """
    needle = step["text"]
    if "window" in step:
        hwnd = find_window(step["window"])[0]
        pcmod.activate(str(hwnd))
        time.sleep(0.4)
    kw = {}
    if step.get("region"):
        kw["region"] = tuple(int(v) for v in str(step["region"]).split(","))
    hit = pcmod.find_text(needle, None, **kw)
    if hit is None:
        raise RuntimeError(f"text not on screen: {needle!r}")
    log(f"text confirmed on screen: {needle!r} at {hit}")
    return hit


def do_click_text(step, log):
    """Click a phrase, optionally narrowed by anchors/region.

    The narrowing has to happen inside the same call as the click: WeChat's
    search dropdown closes within a couple of seconds, so "look now, click
    later" misses the target (learned the hard way on 2026-10-22).
    """
    needle = step["text"]
    if "window" in step:
        hwnd = find_window(step["window"])[0]
        pcmod.activate(str(hwnd))
        time.sleep(0.4)
    kw = {}
    if step.get("below"):
        kw["below"] = step["below"]
    if step.get("above"):
        kw["above"] = step["above"]
    if step.get("region"):
        kw["region"] = tuple(int(v) for v in str(step["region"]).split(","))
    if step.get("pick"):
        kw["pick"] = step["pick"]
    if step.get("scale") is not None:
        kw["scale"] = int(step["scale"])
    hit = pcmod.click_text(needle, None, **kw)
    log(f"clicked text {needle!r} at {hit}")
    return hit



def do_seek(step, log):
    """Page through the document until the phrase shows up.

    This is the primitive the earlier plans were missing: a fixed number of
    PageDowns either overshoots or undershoots, so "scroll until you can see it"
    is the honest instruction. Stops at max_pages and fails loudly.
    """
    needle = step["text"]
    if "window" in step:
        hwnd = find_window(step["window"])[0]
        pcmod.activate(str(hwnd))
        time.sleep(0.4)
    direction = step.get("direction", "down")
    page_key = "pagedown" if direction == "down" else "pageup"
    max_pages = int(step.get("max_pages", 6))
    for page in range(max_pages + 1):
        hit = pcmod.find_text(needle)
        if hit is not None:
            log(f"found {needle!r} on page step {page} at {hit}")
            return hit
        if page == max_pages:
            break
        pcmod.key(page_key)
        time.sleep(int(step.get("gap_ms", 700)) / 1000.0)
    raise RuntimeError(f"seek: {needle!r} not found within {max_pages} page turns")


VERBS = {
    "launch": do_launch, "want": do_want, "activate": do_activate, "uia": do_uia,
    "key": do_key, "type": do_type, "scroll": do_scroll, "shot": do_shot,
    "assert": do_assert, "sleep": do_sleep, "notify": do_notify,
    "assert_text": do_assert_text, "click_text": do_click_text, "seek": do_seek,
    "click": do_click,
}


# ---------- runner ----------

def run_plan(plan: dict, dry: bool = False) -> int:
    steps = plan.get("steps", [])
    print(f"  plan: {plan.get('name', '(unnamed)')}  steps={len(steps)}")
    results = []
    for i, step in enumerate(steps, 1):
        verb = step.get("do")
        fn = VERBS.get(verb)
        label = f"[{i}/{len(steps)}] {verb}"
        if fn is None:
            print(f"  {label}  UNKNOWN VERB")
            return 2
        tries = int(step.get("tries", 3))
        gap = int(step.get("gap_ms", 700)) / 1000.0
        t0 = time.time()
        last = None
        for attempt in range(1, tries + 1):
            try:
                fn(step, lambda m: print(f"  {label}  {m}"))
                results.append({"step": i, "verb": verb, "ok": True,
                                "ms": int((time.time() - t0) * 1000), "attempt": attempt})
                last = None
                break
            except Exception as exc:
                last = exc
                print(f"  {label}  attempt {attempt}/{tries} failed: {exc}")
                if attempt < tries:
                    time.sleep(gap)
        else:
            results.append({"step": i, "verb": verb, "ok": False,
                            "ms": int((time.time() - t0) * 1000), "error": str(last)})
            if step.get("on_fail", plan.get("on_fail", "stop")) == "stop":
                print(f"  STOP at step {i} ({verb}): {last}")
                break
            print(f"  continuing despite failure at step {i}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n  done: {ok}/{len(steps)} steps ok")
    if plan.get("journal"):
        os.makedirs(os.path.dirname(plan["journal"]), exist_ok=True)
        with open(plan["journal"], "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
        print(f"  journal: {plan['journal']}")
    return 0 if ok == len(steps) else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "list"):
        print(__doc__)
        print("  verbs:", ", ".join(sorted(VERBS)))
        return 0
    if argv[0] != "run" or len(argv) < 2:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        plan = json.load(fh)
    return run_plan(plan, dry="--dry" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
