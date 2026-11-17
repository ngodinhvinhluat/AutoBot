"""Xử lý trọn 1 lượt nói: lưu WAV chẩn đoán -> DSP + STT -> (làm mới ngữ cảnh nếu cần) -> pipeline AI + TTS."""
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import WebSocket

from autobot.audio.diagnostics import save_turn_wav
from autobot.audio.dsp import pcm_to_wav_bytes
from autobot.audio.vad import CapturedSpeech
from autobot.conversation.pipeline import stream_ai_and_tts_clauses
from autobot.conversation.sessions import reset_session_if_stale
from autobot.core.audio_format import BYTES_PER_SECOND
from autobot.core.logger import write_log
from autobot.stt.google_stt import speech_to_text
from autobot.tts.playback import PacedPcmSender, boost_tts_pcm
from autobot.tts.synthesizer import synth_clause_pcm

MIN_TURN_SECONDS = 0.35
NOT_HEARD_SHORT = "Mình chưa nghe rõ, bạn nói lại nhé."


@dataclass
class TurnContext:
    """Thông tin kết nối cần cho 1 lượt nói"""
    websocket: WebSocket
    session_state: Dict[str, Any]
    output_format: str   # "pcm" (ESP32) hoặc "mp3"
    vad_mode: str        # "auto" hoặc "manual"
    send_lead: float
    tts_enabled: bool


async def process_turn(ctx: TurnContext, speech: CapturedSpeech) -> None:
    """Thực thi chuỗi Bandpass DSP + AGC -> STT -> Streaming Gemini + Clause TTS -> Phát cuốn chiếu ra loa"""
    ws = ctx.websocket
    if len(speech.pcm) < int(BYTES_PER_SECOND * MIN_TURN_SECONDS):
        return

    save_turn_wav(speech.pcm)
    turn_start_time = time.perf_counter()

    # Báo hiệu Client chuẩn bị nhận luồng âm thanh (và tạm khóa thu Mic tránh vọng âm)
    await ws.send_text("START_OF_TTS")

    t_stt = time.perf_counter()
    wav_bytes = await asyncio.to_thread(pcm_to_wav_bytes, speech.pcm)
    user_text = await asyncio.to_thread(speech_to_text, wav_bytes)
    stt_elapsed = time.perf_counter() - t_stt

    if not user_text:
        await _handle_no_text(ctx, speech, stt_elapsed)
        return

    write_log("STT_DONE", f"Nhận diện giọng nói trong {stt_elapsed:.2f}s -> \"{user_text}\"")
    # Gửi câu đã nhận diện xuống thiết bị (ESP32 hiển thị lên OLED "Bạn: ...")
    await ws.send_text(f"USER:{user_text}")

    reset_session_if_stale(ctx.session_state)

    # Chạy luồng cuốn chiếu: Gemini Stream tới đâu -> cắt Chunk mệnh đề -> TTS & gửi PCM về loa tới đó
    await stream_ai_and_tts_clauses(
        websocket=ws,
        user_text=user_text,
        session_state=ctx.session_state,
        output_format=ctx.output_format,
        turn_start_time=turn_start_time,
        tts_enabled=ctx.tts_enabled,
        send_lead=ctx.send_lead,
    )
    await ws.send_text("END_OF_TTS")


async def _handle_no_text(ctx: TurnContext, speech: CapturedSpeech, stt_elapsed: float) -> None:
    """STT không ra chữ: chế độ auto coi là tạp âm và bỏ qua; chế độ manual báo người dùng nói lại"""
    ws = ctx.websocket
    if ctx.vad_mode == "auto":
        write_log(
            "NOISE_IGNORED",
            f"Bỏ qua tạp âm không có chữ (Thời lượng: {speech.spoken_sec:.2f}s | MaxRMS: {speech.max_rms} | "
            f"MaxPeak: {speech.max_peak} | STT: {stt_elapsed:.2f}s)",
        )
        await ws.send_text("END_OF_TTS")
        return

    if not ctx.tts_enabled:
        await ws.send_text(f"AI:{NOT_HEARD_SHORT}")
        await ws.send_text("END_OF_TTS")
        return
    pcm_response, _ = await synth_clause_pcm(NOT_HEARD_SHORT)
    if ctx.output_format == "pcm":
        pcm_response, _, _, _ = boost_tts_pcm(pcm_response)
        await PacedPcmSender(ws, ctx.send_lead).send(pcm_response)
    else:
        await ws.send_bytes(pcm_response)
    await ws.send_text("END_OF_TTS")
