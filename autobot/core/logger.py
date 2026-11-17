"""Ghi log chi tiết ra Terminal + file voice_session.log + bộ nhớ đệm hiển thị trên Web Dashboard."""
import logging
from datetime import datetime
from typing import Dict, List

from autobot.core.paths import LOG_FILE_PATH

MAX_EVENTS = 60
SYSTEM_EVENTS: List[Dict[str, str]] = []


def write_log(stage: str, message: str) -> None:
    """Ghi log kèm timestamp ra Terminal, File voice_session.log và Web Dashboard"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{stage}] {message}"
    print(f"\n{line}", flush=True)
    SYSTEM_EVENTS.append({"time": ts, "stage": stage, "message": message})
    if len(SYSTEM_EVENTS) > MAX_EVENTS:
        del SYSTEM_EVENTS[:-MAX_EVENTS]
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class EndpointFilter(logging.Filter):
    """Ẩn log polling GET /api/state của trình duyệt để không làm rối cửa sổ Terminal"""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/state" not in record.getMessage()
