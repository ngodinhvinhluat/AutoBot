"""Danh sách giọng đọc VieNeu (đọc từ file preset của thư viện, không cần nạp model)."""
import importlib.util
import json
import os
from typing import List


def vieneu_voice_options(default_voice: str) -> List[List[str]]:
    try:
        spec = importlib.util.find_spec("vieneu")
        path = os.path.join(os.path.dirname(spec.origin), "assets", "voices_v3_turbo.json")
        with open(path, encoding="utf-8") as f:
            presets = json.load(f).get("presets", {})
        opts = [[name, f"{name} — {p.get('description', '')}"] for name, p in presets.items()]
    except Exception:
        opts = []
    if default_voice not in [o[0] for o in opts]:
        opts.insert(0, [default_voice, default_voice])
    return opts
