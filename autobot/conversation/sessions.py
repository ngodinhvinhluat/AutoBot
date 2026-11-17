"""
Ngữ cảnh hội thoại gắn theo THIẾT BỊ (không theo từng kết nối WebSocket) -> ESP32 rớt WiFi/kết nối lại vẫn nhớ.
Khóa = tham số URL ?dev=<MAC> (firmware mới) hoặc IP thiết bị. Im lặng quá lâu -> bắt đầu cuộc trò chuyện mới.
"""
import time
from typing import Any, Dict, Optional, Tuple

from autobot.core.logger import write_log

SESSION_IDLE_RESET_SECONDS = 30 * 60

DEVICE_SESSIONS: Dict[str, Dict[str, Any]] = {}

# "Thế hệ" ngữ cảnh: Dashboard bấm "Xóa ngữ cảnh" -> tăng epoch -> mọi phiên cũ tự làm mới ở lượt nói kế tiếp
_context_epoch = 0


def current_epoch() -> int:
    return _context_epoch


def request_context_reset() -> None:
    global _context_epoch
    _context_epoch += 1
    write_log("CONTEXT_RESET", "🧹 Dashboard yêu cầu xóa ngữ cảnh -> áp dụng từ lượt nói tiếp theo")


def new_session_state() -> Dict[str, Any]:
    return {"gemini_contents": [], "history": [], "turn_count": 0, "epoch": _context_epoch, "last_active": time.time()}


def get_device_session(device_key: str) -> Tuple[Dict[str, Any], bool]:
    """Trả về (session_state, có_khôi_phục_ngữ_cảnh_cũ)"""
    s = DEVICE_SESSIONS.get(device_key)
    if s is not None and time.time() - s.get("last_active", 0) <= SESSION_IDLE_RESET_SECONDS:
        return s, s.get("turn_count", 0) > 0
    s = new_session_state()
    DEVICE_SESSIONS[device_key] = s
    return s, False


def reset_session_if_stale(session_state: Dict[str, Any]) -> Optional[str]:
    """Dashboard bấm "Xóa ngữ cảnh" hoặc im lặng quá lâu -> làm mới phiên tại chỗ. Trả về lý do (None = giữ nguyên)"""
    idle = time.time() - session_state.get("last_active", time.time())
    epoch_changed = session_state.get("epoch") != _context_epoch
    if not epoch_changed and not (session_state.get("turn_count", 0) > 0 and idle > SESSION_IDLE_RESET_SECONDS):
        return None
    reason = "theo yêu cầu Dashboard" if epoch_changed else f"sau {idle / 60:.0f} phút im lặng"
    session_state.clear()
    session_state.update(new_session_state())
    write_log("CONTEXT_RESET", f"🧹 Đã xóa ngữ cảnh hội thoại {reason} -> Gemini bắt đầu cuộc trò chuyện mới")
    return reason
