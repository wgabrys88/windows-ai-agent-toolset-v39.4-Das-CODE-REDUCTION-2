"""vlm_client.py — FINAL (forces LM Studio port 1235)"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

def _load_config(config_path: str | None = None):
    if config_path and Path(config_path).exists():
        try:
            spec = __import__("importlib.util").util.spec_from_file_location("config", config_path)
            cfg = __import__("importlib.util").util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            return cfg
        except Exception:
            pass
    import config as cfg
    return cfg

def call_vlm(story: str, screenshot_b64: str, config_path: str | None = None) -> dict:
    cfg = _load_config(config_path)

    SYSTEM_PROMPT = """You are a Windows computer control agent. You can ONLY use these functions:

click(x, y)

drag(x1, y1, x2, y2)


Rules:
- Use ONLY the functions above.
- Respond with exactly two parts:
  PART 1 -- Short report (2-4 sentences)
  PART 2 -- Actions (only function calls, one per line)
- Always give at least two actions.
- Coordinates 0-1000.
"""

    payload = {
        "model": getattr(cfg, "MODEL", "huihui-qwen3-vl-2b-instruct-abliterated"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": story.strip()},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}
                ]
            }
        ],
        "temperature": getattr(cfg, "TEMPERATURE", 0.7),
        "top_p": getattr(cfg, "TOP_P", 0.9),
        "max_tokens": getattr(cfg, "MAX_TOKENS", 1000),
    }

    # Force LM Studio port 1235
    api_url = "http://127.0.0.1:1235/v1/chat/completions"

    try:
        req = urllib.request.Request(
            api_url,
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
            content = data["choices"][0]["message"]["content"]
            return {
                "success": True,
                "vlm_text": content,
                "usage": data.get("usage", {}),
                "error": None
            }
    except Exception as e:
        return {"success": False, "vlm_text": "", "error": str(e)}

def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
        result = call_vlm(
            story=req.get("story", ""),
            screenshot_b64=req.get("screenshot_b64", ""),
            config_path=req.get("config_path")
        )
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({"success": False, "vlm_text": "", "error": str(e)}))

if __name__ == "__main__":
    main()