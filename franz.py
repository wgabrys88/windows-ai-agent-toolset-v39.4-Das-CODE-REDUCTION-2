"""
franz.py — single-file UI + robust blocking annotation proxy

- Serves ONE HTML file (panel.html) that contains both the dashboard and heatmap canvas.
- Threaded HTTP server (SSE + polling + POST stable on Windows).
- Hard-blocking visual proxy: VLM is called ONLY after /annotated delivers a valid PNG.
- Implements endpoints used by the UI: /events, /health, /pause, /unpause, /render_job, /annotated,
  /allowed_tools (GET/POST), /debug/execute.

Drop this into the project root next to:
  panel.html, executor.py, vlm_client.py, config.py
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
import http.server
import socketserver
import webbrowser
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 1234
RUN_BASE = Path("panel_log")

HERE = Path(__file__).resolve().parent
PANEL_HTML = HERE / "panel.html"
EXECUTOR = HERE / "executor.py"
VLM_CLIENT = HERE / "vlm_client.py"
CONFIG_PATH = str(HERE / "config.py")

_current_run: Path | None = None

_paused = True
_pause_lock = threading.Lock()

_all_tools = ["click", "right_click", "double_click", "drag", "write", "remember", "recall"]

_render_lock = threading.Lock()
_render_job: dict | None = None
_waiting_seq: int | None = None
_render_seq = 0

_annotated_b64: str = ""
_annotated_event = threading.Event()

_sse_clients: list[Queue[str]] = []
_sse_lock = threading.Lock()


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[franz][{ts}] {msg}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_run_dir() -> Path:
    global _current_run
    RUN_BASE.mkdir(exist_ok=True)
    base = RUN_BASE / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    p = base
    i = 1
    while p.exists():
        p = RUN_BASE / f"{base.name}_{i}"
        i += 1
    p.mkdir(parents=True, exist_ok=True)
    _current_run = p

    (p / "allowed_tools.json").write_text(json.dumps(_all_tools, indent=2), encoding="utf-8")
    _log(f"Engine started → {p}")
    return p


def _read_json_body(handler: http.server.BaseHTTPRequestHandler) -> dict | list | None:
    try:
        n = int(handler.headers.get("Content-Length", "0"))
        raw = handler.rfile.read(n) if n > 0 else b""
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _send_json(handler: http.server.BaseHTTPRequestHandler, obj, code: int = 200) -> None:
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


def _send_bytes(handler: http.server.BaseHTTPRequestHandler, body: bytes, ct: str, code: int = 200) -> None:
    handler.send_response(code)
    handler.send_header("Content-Type", ct)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _broadcast(turn_obj: dict) -> None:
    msg = f"data: {json.dumps(turn_obj)}\n\n"
    with _sse_lock:
        dead: list[Queue[str]] = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except Exception:
                pass


def _actions_from_executed(executed: list[str]) -> list[dict]:
    out: list[dict] = []
    for line in executed or []:
        s = line.strip()
        if "(" not in s or not s.endswith(")"):
            continue
        name = s.split("(", 1)[0].strip()
        arg_str = s.split("(", 1)[1][:-1]
        args: list[object] = []
        if arg_str.strip():
            parts = [p.strip() for p in arg_str.split(",")]
            for p in parts:
                if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
                    args.append(p[1:-1])
                else:
                    try:
                        args.append(int(p))
                    except Exception:
                        args.append(p)
        out.append({"name": name, "args": args})
    return out


def _extract_calls_from_vlm(vlm_text: str) -> list[str]:
    calls: list[str] = []
    for line in (vlm_text or "").splitlines():
        t = line.strip()
        if t.startswith((
            "click(", "right_click(", "double_click(", "drag(",
            "write(", "remember(", "recall("
        )):
            calls.append(t)
    return calls


def _load_cfg_model() -> str:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
        cfg = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cfg)
        return getattr(cfg, "MODEL", "unknown")
    except Exception:
        return "unknown"


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/canvas"):
            if not PANEL_HTML.exists():
                return self.send_error(404)
            return _send_bytes(self, PANEL_HTML.read_bytes(), "text/html")

        if path == "/events":
            return self._sse()

        if path == "/health":
            with _pause_lock:
                paused = _paused
            return _send_json(self, {"ok": True, "paused": paused, "run_dir": str(_current_run or ""), "ts": _now_iso()})

        if path == "/render_job":
            with _render_lock:
                job = _render_job
            if not job:
                return _send_json(self, {"waiting": True})
            return _send_json(self, job)

        if path == "/allowed_tools":
            return self._get_allowed_tools()

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/annotated":
            return self._annotated()

        if path == "/pause":
            return self._pause(True)

        if path == "/unpause":
            return self._pause(False)

        if path == "/allowed_tools":
            return self._set_allowed_tools()

        if path == "/debug/execute":
            return self._debug_execute()

        self.send_error(404)

    def _pause(self, value: bool):
        global _paused
        with _pause_lock:
            _paused = value
        return _send_json(self, {"paused": _paused})

    def _get_allowed_tools(self):
        if not _current_run:
            return _send_json(self, _all_tools)
        p = _current_run / "allowed_tools.json"
        try:
            return _send_json(self, json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return _send_json(self, _all_tools)

    def _set_allowed_tools(self):
        if not _current_run:
            return _send_json(self, {"ok": False, "error": "no run dir"}, code=500)
        body = _read_json_body(self)
        if not isinstance(body, list):
            return _send_json(self, {"ok": False, "error": "expected json array"}, code=400)
        allowed = [t for t in body if isinstance(t, str) and t in _all_tools]
        (_current_run / "allowed_tools.json").write_text(json.dumps(allowed, indent=2), encoding="utf-8")
        return _send_json(self, allowed)

    def _debug_execute(self):
        if not _current_run:
            return _send_json(self, {"error": "no run dir"}, code=500)
        body = _read_json_body(self)
        raw = ""
        if isinstance(body, dict):
            raw = str(body.get("raw", ""))

        payload = {"raw": raw, "run_dir": str(_current_run), "debug": True, "config_path": CONFIG_PATH}
        try:
            r = subprocess.run(
                [sys.executable, str(EXECUTOR)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = json.loads(r.stdout) if r.stdout.strip() else {}
            return _send_json(self, {
                "executed": out.get("executed", []),
                "malformed": out.get("malformed", []),
                "error": out.get("error") or out.get("capture_error") or None,
            })
        except Exception as e:
            return _send_json(self, {"executed": [], "malformed": [], "error": str(e)}, code=500)

    def _annotated(self):
        global _annotated_b64
        body = _read_json_body(self)
        if not isinstance(body, dict):
            return _send_json(self, {"ok": False, "error": "bad json"}, code=400)

        seq = body.get("seq")
        img_b64 = body.get("image_b64", "")

        with _render_lock:
            expected = _waiting_seq

        if expected is None:
            return _send_json(self, {"ok": False, "error": "no outstanding job"}, code=409)
        if seq != expected:
            return _send_json(self, {"ok": False, "error": f"seq mismatch (got {seq}, want {expected})"}, code=409)
        if not isinstance(img_b64, str) or len(img_b64) < 100:
            return _send_json(self, {"ok": False, "error": "annotated image too small/empty"}, code=400)

        _annotated_b64 = img_b64
        _annotated_event.set()
        return _send_json(self, {"ok": True})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: Queue[str] = Queue()
        with _sse_lock:
            _sse_clients.append(q)

        try:
            self.wfile.write(f"data: {json.dumps({'type':'connected'})}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)
            return

        try:
            while True:
                try:
                    msg = q.get(timeout=10)
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)


def start_dashboard() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"Dashboard live → http://{HOST}:{PORT}")
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass
    server.serve_forever()


def _wait_until_unpaused() -> None:
    while True:
        with _pause_lock:
            if not _paused:
                return
        time.sleep(0.05)


def main() -> None:
    run_dir = _ensure_run_dir()
    threading.Thread(target=start_dashboard, daemon=True).start()

    model_name = _load_cfg_model()

    story = "hi"
    turn = 0

    _log("Engine sparked. Waiting for Resume in the panel...")
    _wait_until_unpaused()

    while True:
        with _pause_lock:
            if _paused:
                time.sleep(0.05)
                continue

        turn += 1
        t0 = time.time()
        _log(f"--- Turn {turn} ---")

        exec_payload = {"raw": story, "run_dir": str(run_dir), "debug": False, "config_path": CONFIG_PATH}
        try:
            r = subprocess.run(
                [sys.executable, str(EXECUTOR)],
                input=json.dumps(exec_payload),
                capture_output=True,
                text=True,
                timeout=120,
            )
            exec_result = json.loads(r.stdout) if r.stdout.strip() else {}
        except Exception as e:
            exec_result = {"success": False, "error": str(e), "screenshot_b64": "", "executed": []}

        raw_screenshot = exec_result.get("screenshot_b64", "") or ""
        executed = exec_result.get("executed", []) or []
        executed_actions = _actions_from_executed(executed)

        if raw_screenshot:
            (run_dir / f"turn_{turn:04d}.png").write_bytes(base64.b64decode(raw_screenshot))

        global _render_seq, _render_job, _waiting_seq, _annotated_b64
        with _render_lock:
            _render_seq += 1
            _waiting_seq = _render_seq
            _annotated_b64 = ""
            _annotated_event.clear()
            _render_job = {"seq": _render_seq, "image_b64": raw_screenshot, "actions": executed_actions}

        deadline = time.time() + 30.0
        while not _annotated_event.is_set():
            with _pause_lock:
                if _paused:
                    break
            if time.time() > deadline:
                _log("Canvas annotation timeout -> pausing (no raw fallback).")
                with _pause_lock:
                    globals()["_paused"] = True
                _broadcast({
                    "turn": turn,
                    "timestamp": _now_iso(),
                    "latency_ms": int((time.time() - t0) * 1000),
                    "request": {"story_text": story, "model": model_name},
                    "response": {"status": "error", "vlm_text": "", "error": "Canvas annotation timeout (engine paused)", "usage": {}},
                    "actions": executed_actions,
                })
                break
            _annotated_event.wait(0.25)

        with _pause_lock:
            if _paused:
                continue

        annotated_b64 = _annotated_b64
        if not annotated_b64 or len(annotated_b64) < 100:
            _log("Annotated image missing/empty -> pausing.")
            with _pause_lock:
                globals()["_paused"] = True
            continue

        vlm_payload = {"story": story, "screenshot_b64": annotated_b64, "config_path": CONFIG_PATH}
        resp_obj = {"success": False, "vlm_text": "", "usage": {}, "error": "unknown"}

        for attempt in (1, 2):
            try:
                r = subprocess.run(
                    [sys.executable, str(VLM_CLIENT)],
                    input=json.dumps(vlm_payload),
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                resp_obj = json.loads(r.stdout) if r.stdout.strip() else resp_obj
            except Exception as e:
                resp_obj = {"success": False, "vlm_text": "", "usage": {}, "error": str(e)}
            if (resp_obj.get("vlm_text") or "").strip():
                break
            _log(f"VLM returned empty (attempt {attempt})")

        vlm_text = resp_obj.get("vlm_text") or ""
        usage = resp_obj.get("usage") or {}
        vlm_error = resp_obj.get("error") if not resp_obj.get("success", True) else None

        calls = _extract_calls_from_vlm(vlm_text)
        if len(calls) < 2:
            calls = (calls + ["click(500, 500)", "click(500, 500)"])[:2]
        story = "I see the screen with previous actions marked.\n\n" + "\n".join(calls) + "\n"

        turn_obj = {
            "turn": turn,
            "timestamp": _now_iso(),
            "latency_ms": int((time.time() - t0) * 1000),
            "request": {"story_text": vlm_payload["story"], "model": model_name},
            "response": {
                "status": "ok" if (vlm_text.strip() and not vlm_error) else "error",
                "vlm_text": vlm_text,
                "error": vlm_error,
                "usage": usage,
                "finish_reason": None,
                "parse_error": None,
            },
            "actions": executed_actions,
        }

        (run_dir / "state.json").write_text(
            json.dumps({"turn": turn, "story": story, "timestamp": _now_iso()}, indent=2),
            encoding="utf-8",
        )
        with (run_dir / "turns.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(turn_obj, ensure_ascii=False) + "\n")

        _broadcast(turn_obj)
        time.sleep(1.5)


if __name__ == "__main__":
    main()