"""
# FRANZ — Single-HTML Visual-Memory Proxy Architecture (README)

This repo is a **single-process** Windows desktop control loop with a **browser-based visual proxy**
that guarantees the VLM sees an **annotated screenshot** (heat + markers), not a raw one.

Core idea:
- `executor.py` performs actions + captures raw screenshot
- Browser canvas overlays what was executed (heat/markers)
- `franz.py` **blocks** until the browser POSTs the annotated screenshot
- `vlm_client.py` plans the next actions from that annotated image
- UI is **one file** (`panel.html`) with 3 panes and a draggable center cross to resize.


==========================================================================================
ASCII ARCHITECTURE DIAGRAM (wide)
==========================================================================================

   ┌────────────────────────────────────────────────────────────────────────────────────┐
   │                                     USER (Human)                                   │
   │  - Watches single UI page (panel.html)                                              │
   │  - Pause/Resume engine                                                              │
   │  - Toggle allowed tools                                                             │
   │  - Run Debug executor (safe / no physical moves)                                    │
   └────────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │  HTTP POST: /pause /unpause /debug/execute /allowed_tools
                                         │
   ┌─────────────────────────────────────┴──────────────────────────────────────────────┐
   │                                  SINGLE PAGE UI                                    │
   │                                      panel.html                                    │
   │                                                                                     │
   │  ┌───────────────────────────┐     ┌───────────────────────────────────────────┐   │
   │  │ Left Pane                  │     │ Right-Top Pane                            │   │
   │  │ VLM text                   │     │ System                                   │   │
   │  │  - Auto-flip: Input→Output │     │  - Executed action badges                 │   │
   │  │  - Debug mode textarea     │     │  - latency/model/tokens/errors            │   │
   │  └───────────────────────────┘     └───────────────────────────────────────────┘   │
   │                                                                                     │
   │  ┌───────────────────────────────────────────────────────────────────────────────┐ │
   │  │ Right-Bottom Pane: Canvas Proxy                                                │ │
   │  │  - polls GET /render_job                                                       │ │
   │  │  - draws screenshot                                                            │ │
   │  │  - overlays heatmap + markers from executed actions                            │ │
   │  │  - POST /annotated {seq, image_b64} (with retry)                               │ │
   │  └───────────────────────────────────────────────────────────────────────────────┘ │
   │                                                                                     │
   │  Resize: drag the center "cross" divider (no iframe, single DOM).                  │
   └────────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │  SSE: /events  (full turn objects)
                                         │
   ┌─────────────────────────────────────┴──────────────────────────────────────────────┐
   │                                 franz.py (ENGINE)                                  │
   │                                                                                     │
   │  ┌───────────────────────────────┐        ┌─────────────────────────────────────┐  │
   │  │ ThreadingHTTPServer            │        │ Engine Loop                         │  │
   │  │ - GET / (panel.html)           │        │ - waits until unpaused              │  │
   │  │ - GET /events (SSE)            │        │ - executor step                      │  │
   │  │ - GET /health                  │        │ - publish /render_job               │  │
   │  │ - GET /render_job              │        │ - BLOCK for /annotated              │  │
   │  │ - POST /annotated              │        │ - call VLM on annotated image       │  │
   │  │ - POST /pause /unpause         │        │ - persist + broadcast turn          │  │
   │  │ - GET/POST /allowed_tools      │        │                                     │  │
   │  │ - POST /debug/execute          │        │                                     │  │
   │  └───────────────────────────────┘        └─────────────────────────────────────┘  │
   │                                                                                     │
   │  Visual Proxy Guarantee:                                                            │
   │   - Engine publishes render_job(seq, raw_screenshot, executed_actions)              │
   │   - Engine BLOCKS until annotated_b64 arrives for that seq                          │
   │   - NO raw fallback to VLM; on timeout engine pauses with visible error             │
   └─────────────────────────────────────┬──────────────────────────────────────────────┘
                                         │  subprocess.run(JSON via stdin)
                                         │
         ┌───────────────────────────────┴───────────────────────────────┐
         │                                                               │
         ▼                                                               ▼
┌───────────────────────────────┐                           ┌───────────────────────────┐
│ executor.py                    │                           │ vlm_client.py             │
│ - parses tool calls (AST)      │                           │ - calls LM Studio OpenAI-  │
│ - enforces allowed_tools.json  │                           │   compatible endpoint      │
│ - executes physically (optional│                           │ - sends story + image      │
│   via config.PHYSICAL_EXECUTION│                           │ - returns vlm_text + usage │
│ - captures screenshot via      │                           └───────────────────────────┘
│   capture.py subprocess        │
│ - returns: executed[], b64     │
└───────────────────────────────┘
            │
            ▼
      capture.py
      - Win32 capture → crop → resize → PNG encode → base64


==========================================================================================
DATA FLOW (per turn)
==========================================================================================

(1) story_text (from prior VLM output)
    │
    ▼
executor.py
    - executes tool calls (if physical enabled)
    - captures raw screenshot b64
    - returns executed calls list
    │
    ▼
franz.py
    - sets /render_job = { seq, image_b64: raw_screenshot, actions: executed_actions }
    - clears annotated event
    │
    ▼
panel.html Canvas
    - GET /render_job (poll)
    - draw screenshot
    - overlay heatmap + markers
    - toBlob → base64 png
    - POST /annotated { seq, image_b64: annotated_png_b64 } with retry
    │
    ▼
franz.py
    - blocks until annotated event
    - calls VLM using annotated_png_b64
    │
    ▼
vlm_client.py
    - returns vlm_text (+ usage)
    │
    ▼
franz.py
    - extracts tool calls for next story_text (ensures >=2 actions)
    - persists run: state.json, turns.jsonl, turn_XXXX.png
    - broadcasts turn object via SSE /events
    │
    ▼
panel.html
    - updates panes
    - auto-flip Input → Output on new turn (when auto-advance enabled)


==========================================================================================
ENDPOINT CONTRACTS (UI ↔ franz.py)
==========================================================================================

GET  /                 -> panel.html (single combined UI + canvas)
GET  /events            -> SSE stream of full "turn objects"
GET  /health            -> { ok, paused, run_dir, ts }
POST /pause             -> { paused:true }
POST /unpause           -> { paused:false }
GET  /render_job        -> { waiting:true } OR { seq, image_b64, actions }
POST /annotated         -> { ok:true }  (requires {seq, image_b64} non-empty)
GET  /allowed_tools     -> JSON array of allowed tools
POST /allowed_tools     -> updates allowlist
POST /debug/execute     -> runs executor in debug mode (no physical moves)


==========================================================================================
SYSTEM REVIEW (from a "whole system" point of view)
==========================================================================================

This section answers: "Are all files correctly implemented?"

✅ Correct / robust (assuming you use the new combined `panel.html` + updated `franz.py`)
--------------------------------------------------------------------------------------
1) Concurrency stability on Windows:
   - Using ThreadingHTTPServer prevents SSE + polling + POST from starving each other.
   - This directly addresses the WinError 10053 / reconnect loop class of failures.

2) Visual proxy guarantee:
   - Engine blocks on an event set by /annotated and refuses raw fallback.
   - If annotation fails, engine pauses and surfaces a clear error in the turn stream.

3) UI simplification:
   - One HTML file (no iframe, no second page) reduces state/race complexity.
   - 3 panes + draggable center cross preserved.

4) Debug + Tools:
   - Debug runs executor via /debug/execute and reports executed/malformed.
   - Tools overlay updates allowed_tools.json via /allowed_tools.

5) Turn broadcasting:
   - SSE payload includes request/response/actions/latency so UI can populate panes.

⚠️ Known mismatches / gaps (worth fixing depending on your goals)
-----------------------------------------------------------------
A) vlm_client.py tool set mismatch:
   - vlm_client.py system prompt currently lists ONLY:
       click, right_click, double_click, drag, write
   - But executor + UI allow:
       remember, recall  (optional)
   Options:
   - Simplest: remove remember/recall from UI/tools list and from franz extraction
   - Or: add remember/recall to the vlm_client.py SYSTEM_PROMPT and keep them enabled

B) capture.py ignores config_path:
   - executor passes config_path to capture.py, but capture.py imports config.py directly.
   - If you need per-run/per-config switching, update capture.py to load config from path.

C) Crop support is incomplete in executor remapping:
   - executor supports crop for screenshot capture (crop.json), but it calls `configure(..., crop=None)`
     so coordinate remapping does NOT adapt to crop.
   - If you reintroduce region-select later, executor should pass crop into configure() too.

D) Render-job cleanup:
   - If franz.py does not clear _render_job / _waiting_seq after a successful annotate, the UI
     still behaves correctly due to seq gating, but clearing is cleaner.
   Recommended:
   - After annotation is accepted: clear _render_job and set _waiting_seq=None after VLM grabs it.

E) VLM empty response handling:
   - franz.py retries once if vlm_text is empty, then falls back to two clicks.
   - This is an action fallback (not an image fallback). If you want strict behavior, you can
     instead pause when VLM output is empty or tool-less.

✅ Quick “definition of done” checklist
---------------------------------------
- Engine pauses if canvas fails (no raw fallback): YES (new franz)
- UI is single HTML, 3 panes, resizable via cross: YES (combined panel.html)
- Debug and Tools remain: YES
- SSE updates panes reliably: YES
- Remaining optional fixes: A/B/C/D/E above


==========================================================================================
FILES & RESPONSIBILITIES
==========================================================================================

- franz.py
  - HTTP server + engine loop + synchronization gate for annotation
  - persists run artifacts in panel_log/run_*/
  - broadcasts turns to UI via SSE

- panel.html
  - 3-pane UI + drag-to-resize cross
  - SSE client + auto-flip Input→Output
  - debug + tools overlay
  - canvas proxy (poll /render_job, post /annotated with retry)

- executor.py
  - parses tool calls, enforces allowed tools, executes, captures screenshot

- capture.py
  - Win32 screen capture, crop, resize, png encode

- vlm_client.py
  - sends (story + annotated screenshot) to VLM endpoint and returns vlm_text


==========================================================================================
RUNNING (conceptual)
==========================================================================================

1) Start franz.py
2) Browser opens panel.html
3) Press Resume
4) Watch turns stream + canvas overlays

"""