"""Điểm vào chung để tổng hợp 1 mệnh đề thành PCM 16kHz theo bộ tổng hợp đang chọn trên Dashboard."""
import asyncio
import threading
from typing import AsyncIterator, Callable, Iterator

from autobot.config.store import CFG
from autobot.tts.edge_engine import mp3_to_pcm_bytes, text_to_speech_bytes
from autobot.tts.vieneu_engine import vieneu_stream_pcm, vieneu_synth_pcm


async def synth_clause_pcm(text: str) -> tuple[bytes, str]:
    """Tổng hợp 1 mệnh đề thành PCM 16kHz (VieNeu lỗi -> dự phòng Edge/gTTS). Trả về (pcm, tên engine)"""
    if CFG["tts_engine"] == "vieneu":
        pcm = await asyncio.to_thread(vieneu_synth_pcm, text)
        if pcm:
            return pcm, f"VieNeu ({CFG['vieneu_voice']})"
    mp3_bytes, engine_name = await text_to_speech_bytes(text)
    if not mp3_bytes:
        return b"", engine_name
    return await asyncio.to_thread(mp3_to_pcm_bytes, mp3_bytes), engine_name


async def _iterate_in_thread(gen_fn: Callable[[str, threading.Event], Iterator[bytes]], text: str) -> AsyncIterator[bytes]:
    """Chạy generator đồng bộ (nặng CPU) ở luồng riêng, chuyển từng mẩu về event loop ngay khi có"""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()

    def put(item: object) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # event loop đã đóng (tắt server)
            stop.set()

    def run() -> None:
        try:
            for item in gen_fn(text, stop):
                put(item)
        finally:
            put(done)

    loop.run_in_executor(None, run)
    try:
        while True:
            item = await q.get()
            if item is done:
                break
            yield item
    finally:
        stop.set()  # người nhận dừng giữa chừng (mất kết nối...) -> luồng tổng hợp dừng ở mẩu kế tiếp


async def stream_clause_pcm(text: str) -> AsyncIterator[tuple[bytes, str, bool]]:
    """
    Tổng hợp 1 mệnh đề, trả dần từng mẩu (pcm 16kHz, tên engine, có_phải_stream).
    VieNeu + bật "phát dần": nhiều mẩu nhỏ ngay khi tạo ra. Còn lại (Edge/gTTS, hoặc VieNeu stream lỗi
    trước khi ra mẩu nào): 1 mẩu duy nhất chứa cả mệnh đề.
    """
    if CFG["tts_engine"] == "vieneu" and CFG["tts_stream"]:
        name = f"VieNeu ({CFG['vieneu_voice']}, stream)"
        got_any = False
        async for pcm in _iterate_in_thread(vieneu_stream_pcm, text):
            got_any = True
            yield pcm, name, True
        if got_any:
            return
    pcm, name = await synth_clause_pcm(text)
    if pcm:
        yield pcm, name, False
