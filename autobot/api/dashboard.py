"""Trang Dashboard (HTML tĩnh trong autobot/web) + API trạng thái realtime cho trang poll mỗi 700ms."""
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from autobot.ai.gemini_client import get_active_api_key
from autobot.config.store import CFG
from autobot.conversation import history
from autobot.core.logger import SYSTEM_EVENTS
from autobot.core.paths import LOG_FILE_PATH, WEB_DIR
from autobot.devices import registry

router = APIRouter()


@router.get("/")
async def dashboard():
    return FileResponse(os.path.join(WEB_DIR, "index.html"), media_type="text/html")


@router.get("/api/state")
async def get_state():
    has_key = bool(get_active_api_key())
    return JSONResponse({
        "ai_mode": f"Google Gemini ({CFG['gemini_model']} - Streaming Clause TTS)" if has_key else "Local Context Memory (Chưa điền GEMINI_API_KEY)",
        "has_api_key": has_key,
        "log_file": LOG_FILE_PATH,
        "logs": history.recent(20),
        "events": SYSTEM_EVENTS[-30:],
        "devices": registry.snapshot(),
    })
