"""Danh sách thiết bị đang kết nối WebSocket + trạng thái realtime (mức mic, ngưỡng VAD, bận/rảnh)."""
from datetime import datetime
from typing import Any, Dict, List

from autobot.config.store import CFG

STATE_LISTENING = "Đang nghe"
STATE_RECORDING = "Đang thu giọng nói"
STATE_PROCESSING = "Đang xử lý (STT → AI → giọng đọc)"
STATE_TEST_TTS = "Đang phát thử giọng"

ACTIVE_DEVICES: Dict[int, Dict[str, Any]] = {}


def register(dev_id: int, websocket: Any, addr: str, tts_forced: str, output_format: str, send_lead: float) -> Dict[str, Any]:
    dev: Dict[str, Any] = {
        "ws": websocket, "addr": addr, "since": datetime.now().strftime("%H:%M:%S"),
        "state": STATE_LISTENING, "busy": False, "floor": 0, "start_thr": 0, "cont_thr": 0, "level": 0,
        "tts_forced": tts_forced, "output_format": output_format, "send_lead": send_lead,
    }
    ACTIVE_DEVICES[dev_id] = dev
    return dev


def unregister(dev_id: int) -> None:
    ACTIVE_DEVICES.pop(dev_id, None)


def device_tts_enabled(dev: Dict[str, Any]) -> bool:
    """Bật/tắt loa: tham số URL (tts=on/off) ưu tiên, không có thì theo cài đặt Dashboard (đổi được giữa chừng)"""
    return (dev["tts_forced"] == "on") if dev["tts_forced"] else bool(CFG["tts_output"])


def snapshot() -> List[Dict[str, Any]]:
    """Dữ liệu thiết bị cho Dashboard (bỏ đối tượng WebSocket)"""
    out = []
    for d in list(ACTIVE_DEVICES.values()):
        info = {k: v for k, v in d.items() if k != "ws"}
        info["tts"] = device_tts_enabled(d)
        out.append(info)
    return out
