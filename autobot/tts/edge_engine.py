"""Bộ tổng hợp trực tuyến: Edge-TTS (Microsoft), lỗi thì dự phòng Google-TTS. Kết quả MP3 -> giải mã PCM 16kHz."""
import asyncio
import io

import edge_tts

from autobot.config.store import CFG
from autobot.core.audio_format import CHANNELS, SAMPLE_RATE
from autobot.core.logger import write_log

try:
    import miniaudio
    HAS_MINIAUDIO = True
except ImportError:
    HAS_MINIAUDIO = False

# Tự động tắt thử Edge-TTS nếu bị lỗi lệch giờ hệ thống để không tốn 2s chờ mỗi câu
_EDGE_TTS_WORKING: bool = True


async def _edge_synth(text: str, voice: str, rate: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    mp3_io = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_io.write(chunk["data"])
    return mp3_io.getvalue()


async def text_to_speech_bytes(text: str) -> tuple[bytes, str]:
    """
    Chuyển đổi 1 cụm câu ngắn thành MP3 bytes.
    Nếu Edge-TTS bị lỗi (do lệch giờ hệ thống), tự động nhớ trạng thái và gọi thẳng Google-TTS (không tốn 2s chờ timeout).
    """
    clean_text = text.strip()
    if not clean_text:
        return b"", "empty"

    edge_voice = CFG["edge_voice"]
    edge_rate = CFG["tts_rate"]
    if _EDGE_TTS_WORKING:
        try:
            data = await _edge_synth(clean_text, edge_voice, edge_rate)
            if data:
                return data, f"Edge-TTS ({edge_voice} {edge_rate})"
        except Exception as e:
            if edge_rate != "+0%":
                try:
                    data = await _edge_synth(clean_text, edge_voice, "+0%")
                    if data:
                        return data, f"Edge-TTS ({edge_voice})"
                except Exception:
                    pass
            write_log("TTS_WARN", f"Edge-TTS tạm lỗi: {e}, chuyển dự phòng Google-TTS")

    from gtts import gTTS

    def _gtts_synth() -> bytes:
        buf = io.BytesIO()
        gTTS(text=clean_text, lang="vi").write_to_fp(buf)
        return buf.getvalue()

    data = await asyncio.to_thread(_gtts_synth)
    return data, "Google-TTS"


def mp3_to_pcm_bytes(mp3_bytes: bytes) -> bytes:
    """Giải mã MP3 sang Raw PCM (16kHz, 16-bit, Mono)"""
    if not mp3_bytes:
        return b""
    if not HAS_MINIAUDIO:
        raise RuntimeError("Chưa cài đặt thư viện 'miniaudio' để chuyển đổi MP3 sang PCM.")
    decoded = miniaudio.decode(
        mp3_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=CHANNELS,
        sample_rate=SAMPLE_RATE,
    )
    return bytes(decoded.samples)
