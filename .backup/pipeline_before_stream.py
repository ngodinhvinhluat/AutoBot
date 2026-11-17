"""
Pipeline cuốn chiếu 3 tầng chạy song song cho 1 lượt trả lời:
1. Gemini `generate_content_stream` -> cắt mệnh đề ngay khi có chữ -> clause_queue.
2. Tổng hợp giọng (VieNeu/Edge) lần lượt từng mệnh đề -> audio_queue. Tầng này KHÔNG chờ gửi,
   nên mệnh đề sau được tổng hợp trong lúc loa còn đang phát mệnh đề trước.
3. Gửi PCM xuống thiết bị THEO NHỊP PHÁT (chỉ đi trước loa `send_lead` giây) để bộ đệm ESP32 không tràn.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from autobot.ai.gemini_chat import GeminiChatTurn
from autobot.ai.gemini_client import HAS_GENAI, get_active_api_key
from autobot.ai.local_fallback import local_contextual_fallback
from autobot.config.store import CFG
from autobot.conversation import history
from autobot.conversation.clauses import split_into_speakable_clauses
from autobot.core.audio_format import BYTES_PER_SECOND
from autobot.core.logger import write_log
from autobot.tts.playback import (
    TTS_EST,
    TTS_SEND_LEAD_DEFAULT,
    PacedPcmSender,
    boost_tts_pcm,
    estimate_prebuffer_seconds,
    update_tts_estimates,
)
from autobot.tts.synthesizer import synth_clause_pcm

GEMINI_TURN_TIMEOUT_SECONDS = 16.0
NOT_HEARD_REPLY = "Mình chưa nghe rõ ý bạn vừa nói, bạn nhắc lại rõ hơn một chút nhé."


async def stream_ai_and_tts_clauses(
    websocket: WebSocket,
    user_text: str,
    session_state: Dict[str, Any],
    output_format: str,
    turn_start_time: float,
    tts_enabled: bool = True,
    send_lead: float = TTS_SEND_LEAD_DEFAULT,
) -> None:
    turn_no = session_state.get("turn_count", 0) + 1
    session_state["turn_count"] = turn_no
    session_state["last_active"] = time.time()

    write_log("USER_SPEECH", f"Lượt #{turn_no} | Bạn nói: \"{user_text}\"")
    conv_entry = history.add_turn(turn_no, user_text)

    clause_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    audio_queue: asyncio.Queue[Optional[tuple[int, str, bytes, str, float]]] = asyncio.Queue()
    full_reply_parts: List[str] = []
    planned_clauses: List[str] = []  # mọi mệnh đề đã cắt từ Gemini (theo thứ tự) -> ước lượng thời gian tổng hợp còn lại

    async def tts_worker() -> None:
        """Tầng 2: lấy mệnh đề -> tổng hợp PCM -> đẩy sang tầng gửi (không chờ gửi xong)"""
        seq = 0
        while True:
            clause = await clause_queue.get()
            if clause is None:
                await audio_queue.put(None)
                break
            seq += 1
            if not tts_enabled:
                await audio_queue.put((seq, clause, b"", "", 0.0))
                continue
            t_c0 = time.perf_counter()
            pcm_bytes, engine_name = await synth_clause_pcm(clause)
            if pcm_bytes:
                await audio_queue.put((seq, clause, pcm_bytes, engine_name, time.perf_counter() - t_c0))

    async def sender() -> None:
        """Tầng 3: gửi chữ + PCM của từng mệnh đề xuống thiết bị theo nhịp phát"""
        paced = PacedPcmSender(websocket, send_lead)
        ref_peak = 0.0
        clause_idx = 0
        first_out = False
        while True:
            item = await audio_queue.get()
            if item is None:
                break
            seq, clause, pcm_bytes, engine_name, tts_dt = item
            clause_idx += 1
            full_reply_parts.append(clause)
            conv_entry["ai"] = " ".join(full_reply_parts)

            if not pcm_bytes:
                # Chế độ chỉ chữ: không tổng hợp giọng, đẩy ngay mệnh đề lên Dashboard + xuống thiết bị
                if not first_out:
                    first_out = True
                    ttft = time.perf_counter() - turn_start_time
                    conv_entry["latency"] = f"Chữ đầu {ttft:.2f}s"
                    write_log("FIRST_TEXT_OUT", f"📝 Trả lời bằng chữ sau {ttft:.2f}s: \"{clause}\"")
                else:
                    write_log("TEXT_OUT", f"📝 Mệnh đề #{clause_idx}: \"{clause}\"")
                await websocket.send_text(f"AI:{clause}")
                continue

            if output_format != "pcm":
                await websocket.send_text(f"AI:{clause}")
                await websocket.send_bytes(pcm_bytes)
                continue

            pcm_bytes, pk_in, pk_out, ref_peak = boost_tts_pcm(pcm_bytes, ref_peak)
            c_dur = len(pcm_bytes) / BYTES_PER_SECOND
            update_tts_estimates(clause, tts_dt, c_dur)
            if not first_out:
                first_out = True
                # Tổng hợp chậm sát thời gian thực -> chờ thêm một chút để cả câu trả lời phát liền mạch
                wait = min(float(CFG["tts_max_prebuffer"]), estimate_prebuffer_seconds(c_dur, planned_clauses[seq:]))
                if wait > 0.05:
                    await asyncio.sleep(wait)
                ttfa = time.perf_counter() - turn_start_time
                conv_entry["latency"] = f"TTFA {ttfa:.2f}s"
                wait_note = f", chờ thêm {wait:.2f}s cho {len(planned_clauses) - seq} mệnh đề sau" if wait > 0.05 else ""
                write_log(
                    "FIRST_AUDIO_OUT",
                    f"⚡ Bắt đầu phát Chunk #1 sau {ttfa:.2f}s ({engine_name}: {tts_dt:.2f}s, Dài: {c_dur:.2f}s{wait_note}, "
                    f"Âm lượng đỉnh {pk_in:.0f}% -> {pk_out:.0f}% (+{float(CFG['tts_boost_db']):.0f}dB nén mềm), "
                    f"gửi trước loa tối đa {send_lead:.1f}s, RTF ước lượng {TTS_EST['rtf']:.2f}): \"{clause}\"",
                )
            else:
                gap = paced.starving_seconds()
                gap_note = f" | ⚠️ loa đã chờ {gap:.2f}s (tổng hợp không kịp)" if gap > 0.05 else ""
                write_log(
                    "TTS_CHUNK_OUT",
                    f"🔊 Gửi nối tiếp Chunk #{clause_idx} ({engine_name}: {tts_dt:.2f}s, Dài: {c_dur:.2f}s{gap_note}): \"{clause}\"",
                )

            # Gửi chữ của mệnh đề ngay trước PCM để màn hình OLED trên ESP32 hiển thị đồng bộ với tiếng
            await websocket.send_text(f"AI:{clause}")
            await paced.send(pcm_bytes)

    tts_task = asyncio.create_task(tts_worker())
    sender_task = asyncio.create_task(sender())

    emitted_any = False

    async def emit(clause: str) -> None:
        nonlocal emitted_any
        emitted_any = True
        planned_clauses.append(clause)
        await clause_queue.put(clause)

    async def emit_local_fallback() -> None:
        for c in split_into_speakable_clauses(local_contextual_fallback(user_text, session_state)):
            await emit(c)

    # Tầng 1: sinh câu trả lời (Gemini nếu có API key, không thì bộ trả lời nội bộ)
    api_key = get_active_api_key()
    if HAS_GENAI and api_key:
        chat = GeminiChatTurn(api_key, session_state, user_text)
        try:
            await asyncio.wait_for(chat.stream_clauses(emit), timeout=GEMINI_TURN_TIMEOUT_SECONDS)
            if not emitted_any:
                await emit(NOT_HEARD_REPLY)
            write_log("GEMINI_STREAM_DONE", chat.describe())
        except Exception as e:
            write_log("GEMINI_ERROR", f"Lỗi stream Gemini ({type(e).__name__}: {e}) -> Dùng Local Context")
            if not emitted_any:
                await emit_local_fallback()
        chat.save_to_history()
    else:
        await emit_local_fallback()

    # Báo hiệu kết thúc hàng đợi mệnh đề và chờ tổng hợp + gửi hết các chunk PCM xuống thiết bị
    await clause_queue.put(None)
    try:
        await asyncio.gather(tts_task, sender_task)
    finally:
        for t in (tts_task, sender_task):
            if not t.done():
                t.cancel()
