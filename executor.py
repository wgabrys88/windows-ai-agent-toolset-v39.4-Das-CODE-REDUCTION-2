"""Franz executor.py — Phase 2 (standalone JSON pipeline)

Input JSON on stdin → Output JSON on stdout
Fully independent module. Calls capture.py via subprocess.
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

# ========================== Win32 low-level (exact original pattern) ==========================
_INPUT_MOUSE: Final = 0
_INPUT_KEYBOARD: Final = 1
_MOUSEEVENTF_LEFTDOWN: Final = 0x0002
_MOUSEEVENTF_LEFTUP: Final = 0x0004
_MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
_MOUSEEVENTF_RIGHTUP: Final = 0x0010
_MOUSEEVENTF_ABSOLUTE: Final = 0x8000
_MOUSEEVENTF_MOVE: Final = 0x0001
_KEYEVENTF_KEYUP: Final = 0x0002
_KEYEVENTF_UNICODE: Final = 0x0004

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]

class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]

_user32 = None
_screen_w = 1920
_screen_h = 1080
_physical = False
_executed: list[str] = []
_run_dir = ""
_crop = None
_crop_active = False

def _init_win32():
    global _user32, _screen_w, _screen_h
    if _user32: return
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    except Exception:
        pass
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint

def _send_inputs(items):
    arr = (_INPUT * len(items))(*items)
    _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT))

def _send_mouse(flags, abs_x=None, abs_y=None):
    inp = _INPUT(type=_INPUT_MOUSE)
    f, dx, dy = flags, 0, 0
    if abs_x is not None and abs_y is not None:
        dx, dy, f = abs_x, abs_y, f | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([inp])

def _to_abs(x_px, y_px):
    return (max(0, min(65535, int(x_px / max(1, _screen_w-1) * 65535))),
            max(0, min(65535, int(y_px / max(1, _screen_h-1) * 65535))))

def _smooth_move(tx, ty):
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    ddx, ddy = tx - sx, ty - sy
    for i in range(21):
        t = i / 20
        t = t * t * (3.0 - 2.0 * t)
        _send_mouse(0, *_to_abs(int(sx + ddx * t), int(sy + ddy * t)))
        time.sleep(0.01)

def _remap(v, dim):
    if _crop_active and _crop:
        span = _crop["x2"] - _crop["x1"] if dim == _screen_w else _crop["y2"] - _crop["y1"]
        origin = _crop["x1"] if dim == _screen_w else _crop["y1"]
        return origin + int((v / 1000) * span)
    return int((v / 1000) * dim)

def _phys_click(name, x, y):
    down, up, double = {
        "click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, False),
        "right_click": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, False),
        "double_click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, True),
    }[name]
    _smooth_move(_remap(x, _screen_w), _remap(y, _screen_h))
    time.sleep(0.12)
    _send_mouse(down); time.sleep(0.02); _send_mouse(up)
    if double:
        time.sleep(0.06)
        _send_mouse(down); time.sleep(0.02); _send_mouse(up)

def _phys_drag(x1, y1, x2, y2):
    _smooth_move(_remap(x1, _screen_w), _remap(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(_MOUSEEVENTF_LEFTDOWN); time.sleep(0.06)
    _smooth_move(_remap(x2, _screen_w), _remap(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(_MOUSEEVENTF_LEFTUP)

def _send_unicode(text):
    items = []
    for ch in text:
        if ch == "\r": continue
        code = 0x000D if ch == "\n" else ord(ch)
        for fl in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT(type=_INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(0, code, fl, 0, 0)
            items.append(inp)
    _send_inputs(items)

# ========================== Tools (merged) ==========================
def configure(physical: bool, run_dir: str, crop: dict | None = None):
    global _physical, _executed, _run_dir, _crop, _crop_active
    _physical = physical
    _executed = []
    _run_dir = run_dir
    if physical:
        _init_win32()
    if crop and all(k in crop for k in ("x1","y1","x2","y2")):
        _crop = crop
        _crop_active = True
    else:
        _crop_active = False

def get_results() -> list[str]:
    return list(_executed)

def click(x: int, y: int):
    ix = max(0, min(1000, int(x)))
    iy = max(0, min(1000, int(y)))
    if _physical: _phys_click("click", ix, iy)
    _executed.append(f"click({ix}, {iy})")

def right_click(x: int, y: int):
    ix = max(0, min(1000, int(x)))
    iy = max(0, min(1000, int(y)))
    if _physical: _phys_click("right_click", ix, iy)
    _executed.append(f"right_click({ix}, {iy})")

def double_click(x: int, y: int):
    ix = max(0, min(1000, int(x)))
    iy = max(0, min(1000, int(y)))
    if _physical: _phys_click("double_click", ix, iy)
    _executed.append(f"double_click({ix}, {iy})")

def drag(x1: int, y1: int, x2: int, y2: int):
    c = [max(0, min(1000, int(v))) for v in (x1, y1, x2, y2)]
    if _physical: _phys_drag(*c)
    _executed.append(f"drag({c[0]}, {c[1]}, {c[2]}, {c[3]})")

def write(text: str):
    if _physical: _send_unicode(str(text))
    _executed.append(f'write({json.dumps(str(text))})')

def remember(text: str):
    p = Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")
    items = []
    try: items = json.loads(p.read_text(encoding="utf-8"))
    except: pass
    if isinstance(items, list) and str(text) not in items:
        items.append(str(text))
        items = items[-20:]
        p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    _executed.append(f'remember({json.dumps(str(text))})')

def recall() -> str:
    p = Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(items, list) and items:
            return "\n".join(f"- {s}" for s in items[-20:])
    except: pass
    return "(no memories yet)"

# ========================== Parser + Capture call ==========================
def _extract_calls(raw: str, allowed: set[str]):
    result, malformed = [], []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned: continue
        try:
            tree = ast.parse(cleaned, mode="eval")
            if not isinstance(tree.body, ast.Call): continue
            func = tree.body.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in allowed:
                result.append(cleaned)
            elif name:
                malformed.append(f"UnknownTool: '{name}'")
        except SyntaxError:
            if any(cleaned.startswith(t) for t in ("click","right_click","double_click","drag","write","remember","recall")):
                malformed.append(f"UnrecognizedCall: '{cleaned}'")
    return result, malformed

def _call_capture(crop: dict | None, config_path: str):
    payload = {"command": "capture", "crop": crop, "config_path": config_path}
    try:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "capture.py")],
            input=json.dumps(payload), 
            capture_output=True, 
            text=True, 
            timeout=60
        )
        
        if r.stderr:
            sys.stderr.write(f"[executor] CAPTURE STDERR:\n{r.stderr}\n")
        
        if r.returncode != 0:
            return {"success": False, "error": f"capture crashed (code {r.returncode})", "screenshot_b64": "", "stderr": r.stderr}
        
        result = json.loads(r.stdout)
        # Force success if we have a valid b64 (safety net)
        if result.get("screenshot_b64") and len(result.get("screenshot_b64")) > 100:
            result["success"] = True
        return result
        
    except Exception as e:
        return {"success": False, "error": f"subprocess error: {e}", "screenshot_b64": ""}

def _load_config(config_path: str | None):
    if config_path and Path(config_path).exists():
        try:
            spec = __import__("importlib.util").util.spec_from_file_location("config", config_path)
            cfg = __import__("importlib.util").util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            return cfg
        except Exception: pass
    import config as cfg
    return cfg

# ========================== Main ==========================
def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
        cfg = _load_config(req.get("config_path"))
        raw = str(req.get("raw", ""))
        run_dir = str(req.get("run_dir", ""))
        debug = bool(req.get("debug", False))

        # allowed tools
        tools_path = Path(run_dir) / "allowed_tools.json" if run_dir else Path("allowed_tools.json")
        allowed = set()
        try:
            allowed = set(json.loads(tools_path.read_text(encoding="utf-8")))
        except Exception:
            allowed = {"click", "right_click", "double_click", "drag", "write", "remember", "recall"}

        configure(
            physical=not debug and bool(getattr(cfg, "PHYSICAL_EXECUTION", True)),
            run_dir=run_dir,
            crop=None
        )

        calls, malformed = _extract_calls(raw, allowed)
        errors = list(malformed)

        ns = {"__builtins__": {}}
        for name in ("click", "right_click", "double_click", "drag", "write", "remember", "recall"):
            ns[name] = globals()[name]

        for line in calls:
            try:
                eval(compile(line, "<agent>", "eval"), ns)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        # load crop for screenshot
        crop_path = Path(run_dir) / "crop.json" if run_dir else Path("crop.json")
        crop = None
        try:
            c = json.loads(crop_path.read_text(encoding="utf-8"))
            if isinstance(c, dict) and "x1" in c:
                crop = c
        except Exception:
            pass

        capture_result = _call_capture(crop, req.get("config_path"))

        result = {
            "success": capture_result.get("success", False),
            "executed": get_results(),
            "extracted_code": calls,
            "malformed": errors,
            "screenshot_b64": capture_result.get("screenshot_b64", ""),
            "width": capture_result.get("width", 512),
            "height": capture_result.get("height", 288),
            "capture_error": capture_result.get("error") or capture_result.get("stderr")
        }
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"[executor] FATAL: {exc}\n")
        sys.stdout.write(json.dumps({"success": False, "error": str(exc), "screenshot_b64": ""}))
        sys.stdout.flush()

if __name__ == "__main__":
    main()