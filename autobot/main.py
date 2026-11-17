"""Khởi tạo ứng dụng FastAPI (gắn các router) và chạy server."""
import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from autobot.ai.gemini_client import get_active_api_key, get_gemini_client
from autobot.api import dashboard, settings, ws_audio
from autobot.config.store import CFG, load_settings
from autobot.core.logger import EndpointFilter, write_log
from autobot.core.paths import LOG_FILE_PATH, SETTINGS_FILE, WEB_DIR
from autobot.tts.vieneu_engine import get_vieneu_tts

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8000"))  # Chạy thêm instance: set PORT=4500 rồi python server.py


def create_app() -> FastAPI:
    load_settings()
    app = FastAPI(title="ESP32 Two-Way Conversational AI Server")
    app.include_router(dashboard.router)
    app.include_router(settings.router)
    app.include_router(ws_audio.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()


def _warm_up() -> str:
    """Khởi tạo sẵn Gemini Client và (nếu bật loa) model VieNeu để lượt nói đầu tiên không phải chờ. Trả về API key"""
    key = get_active_api_key()
    if key:
        try:
            get_gemini_client(key)
        except Exception:
            pass
    # Chỉ nạp sẵn VieNeu khi bật giọng đọc (tắt thì khởi động nhanh hơn ~15-25s; vẫn nạp muộn nếu kết nối nào xin tts=on)
    if CFG["tts_engine"] == "vieneu" and CFG["tts_output"]:
        try:
            get_vieneu_tts()
        except Exception:
            pass
    return key


def run() -> None:
    import uvicorn

    logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
    key = _warm_up()

    tts_info = f"VieNeu ({CFG['vieneu_voice']} 48kHz)" if CFG["tts_engine"] == "vieneu" else f"Edge-TTS ({CFG['edge_voice']})"
    if not CFG["tts_output"]:
        tts_info = f"TẮT - chỉ trả lời bằng chữ (bật lại trên Dashboard | engine: {tts_info})"
    api_status = f"ĐÃ KẾT NỐI GEMINI API ({CFG['gemini_model']} - Streaming Clause TTS)" if key else "CHƯA ĐIỀN GEMINI_API_KEY"
    write_log(
        "SERVER_START",
        f"Khởi động Server Đàm thoại AI | Trạng thái: {api_status} | TTS: {tts_info} | "
        f"Cài đặt: {SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else 'mặc định (chưa có settings.json)'} | Log file: {LOG_FILE_PATH}",
    )
    uvicorn.run(app, host=HOST, port=PORT)
