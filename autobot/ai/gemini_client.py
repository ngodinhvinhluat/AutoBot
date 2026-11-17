"""Quản lý API key và Gemini Client dùng lại giữa các lượt (không mất thời gian bắt tay lại)."""
import os
from typing import Any, Optional

from autobot.core.env import load_env

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    genai = None
    HAS_GENAI = False

# Điền trực tiếp API key ở đây nếu không muốn dùng file .env
DIRECT_GEMINI_API_KEY = ""

_GEMINI_CLIENT: Optional[Any] = None
_GEMINI_CLIENT_KEY: str = ""


def get_active_api_key() -> str:
    """Lấy GEMINI_API_KEY từ biến trực tiếp hoặc đọc nóng từ file .env"""
    load_env()
    key = (DIRECT_GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")).strip()
    if key and key != "your_gemini_api_key_here" and len(key) >= 20:
        return key
    return ""


def get_gemini_client(api_key: str) -> Any:
    """Khởi tạo hoặc tái sử dụng Gemini Client để tối ưu tốc độ phản hồi"""
    global _GEMINI_CLIENT, _GEMINI_CLIENT_KEY
    if _GEMINI_CLIENT is None or _GEMINI_CLIENT_KEY != api_key:
        from google.genai import types
        _GEMINI_CLIENT = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=15000),
        )
        _GEMINI_CLIENT_KEY = api_key
    return _GEMINI_CLIENT
