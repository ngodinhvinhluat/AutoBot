"""Lịch sử hội thoại hiển thị trên Dashboard (chỉ để xem, không phải ngữ cảnh gửi AI)."""
from datetime import datetime
from typing import Dict, List

CONVERSATION_LOG: List[Dict[str, str]] = []


def add_turn(turn_no: int, user_text: str) -> Dict[str, str]:
    """Tạo bản ghi ở trạng thái đang phản hồi; pipeline cập nhật trực tiếp các trường "ai" và "latency" """
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "turn": str(turn_no),
        "user": user_text,
        "ai": "⏳ Đang phản hồi...",
        "latency": "...",
    }
    CONVERSATION_LOG.append(entry)
    return entry


def recent(n: int = 20) -> List[Dict[str, str]]:
    return CONVERSATION_LOG[-n:]


def clear() -> None:
    CONVERSATION_LOG.clear()
