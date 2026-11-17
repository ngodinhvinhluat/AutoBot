"""API cài đặt (đọc/ghi CFG) và các thao tác nhanh trên Dashboard."""
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from autobot.config.schema import SETTINGS_SCHEMA
from autobot.config.store import CFG, reset_settings, update_settings
from autobot.conversation import history
from autobot.conversation.sessions import request_context_reset
from autobot.core.paths import SETTINGS_FILE
from autobot.devices.test_playback import play_test_tts
from autobot.tts import vieneu_engine

router = APIRouter(prefix="/api")


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


@router.get("/settings")
async def get_settings():
    return JSONResponse({"schema": SETTINGS_SCHEMA, "values": CFG, "file": SETTINGS_FILE})


@router.post("/settings")
async def post_settings(request: Request):
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"errors": {"_": "Dữ liệu gửi lên phải là JSON {khóa: giá trị}"}, "values": CFG}, status_code=400)
    applied, errors = update_settings(body)
    # Vừa bật loa / chuyển sang VieNeu mà model chưa nạp -> nạp nền để lượt nói kế tiếp không phải chờ ~15-25s
    # Đổi fp32 <-> int8 -> nạp lại model ở nền (lượt đang nói vẫn dùng model cũ tới khi nạp xong)
    if ("tts_output" in applied or "tts_engine" in applied or "vieneu_precision" in applied) \
            and CFG["tts_output"] and CFG["tts_engine"] == "vieneu":
        vieneu_engine.preload_in_background()
    return JSONResponse({"applied": applied, "errors": errors, "values": CFG}, status_code=400 if errors else 200)


@router.post("/action/{name}")
async def run_action(name: str, request: Request):
    body = await _json_body(request)
    params: Dict[str, Any] = body if isinstance(body, dict) else {}

    if name == "reset_context":
        request_context_reset()
        return JSONResponse({"ok": True, "message": "Đã xóa ngữ cảnh: AI sẽ bắt đầu cuộc trò chuyện mới từ lượt kế tiếp"})

    if name == "clear_history":
        history.clear()
        return JSONResponse({"ok": True, "message": "Đã xóa lịch sử hội thoại trên Dashboard"})

    if name == "reset_settings":
        reset_settings()
        return JSONResponse({"ok": True, "message": "Đã khôi phục cài đặt mặc định", "values": CFG})

    if name == "test_tts":
        status, payload = await play_test_tts(str(params.get("text") or ""))
        return JSONResponse(payload, status_code=status)

    return JSONResponse({"ok": False, "message": f"Không có thao tác '{name}'"}, status_code=404)
