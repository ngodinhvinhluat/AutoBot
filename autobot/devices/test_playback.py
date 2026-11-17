"""Phát thử 1 câu giọng đọc ra loa mọi thiết bị đang rảnh (nút "Phát thử ra loa" trên Dashboard)."""
from typing import Any, Dict, Tuple

from autobot.config.store import CFG
from autobot.core.audio_format import BYTES_PER_SECOND
from autobot.core.logger import write_log
from autobot.devices.registry import ACTIVE_DEVICES, STATE_LISTENING, STATE_TEST_TTS
from autobot.tts.playback import PacedPcmSender, boost_tts_pcm
from autobot.tts.synthesizer import synth_clause_pcm

DEFAULT_TEST_TEXT = "Xin chào, đây là giọng đọc thử ở mức âm lượng hiện tại."


async def play_test_tts(text: str) -> Tuple[int, Dict[str, Any]]:
    """Trả về (HTTP status, payload JSON)"""
    text = text.strip()[:200] or DEFAULT_TEST_TEXT
    if not ACTIVE_DEVICES:
        return 409, {"ok": False, "message": "Chưa có thiết bị nào kết nối"}
    targets = [d for d in list(ACTIVE_DEVICES.values()) if not d["busy"] and d["output_format"] == "pcm"]
    if not targets:
        return 409, {"ok": False, "message": "Thiết bị đang bận xử lý lượt nói, thử lại sau vài giây"}

    pcm, engine_name = await synth_clause_pcm(text)
    if not pcm:
        return 500, {"ok": False, "message": f"Không tổng hợp được giọng ({engine_name})"}
    pcm, pk_in, pk_out, _ = boost_tts_pcm(pcm)

    sent = 0
    for d in targets:
        d["busy"] = True
        d["state"] = STATE_TEST_TTS
        try:
            ws = d["ws"]
            await ws.send_text("START_OF_TTS")
            await ws.send_text(f"AI:{text}")
            await PacedPcmSender(ws, d["send_lead"]).send(pcm)
            await ws.send_text("END_OF_TTS")
            sent += 1
        except Exception as e:
            write_log("TEST_TTS", f"Lỗi gửi tới {d['addr']}: {e}")
        finally:
            d["busy"] = False
            d["state"] = STATE_LISTENING

    msg = (f"Đã phát thử trên {sent} thiết bị: {engine_name}, dài {len(pcm) / BYTES_PER_SECOND:.1f}s, "
           f"âm lượng đỉnh {pk_in:.0f}% → {pk_out:.0f}% (+{float(CFG['tts_boost_db']):.0f}dB)")
    write_log("TEST_TTS", f"🔈 {msg}")
    return 200, {"ok": sent > 0, "message": msg}
