"""
Pipeline cuốn chiếu 3 tầng chạy song song cho 1 lượt trả lời:
1. Gemini `generate_content_stream` -> cắt mệnh đề ngay khi có chữ -> clause_queue.
2. Tổng hợp giọng (VieNeu/Edge) lần lượt từng mệnh đề -> audio_queue. Tầng này KHÔNG chờ gửi,
   nên mệnh đề sau được tổng hợp trong lúc loa còn đang phát mệnh đề trước.
   VieNeu v3 Turbo phát dần: mỗi mệnh đề đưa sang tầng 3 ngay khi có MẨU ĐẦU (~0.4s), các mẩu sau chảy tiếp
   qua hàng đợi riêng của mệnh đề đó.
3. Gửi PCM xuống thiết bị THEO NHỊP PHÁT (chỉ đi trước loa `send_lead` giây) để bộ đệm ESP32 không tràn.
"""
import asyncio
import time
from dataclasses import dataclass, field
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
    update_stream_estimates,
    update_tts_estimates,
)
from autobot.tts.synthesizer import stream_clause_pcm

GEMINI_TURN_TIMEOUT_SECONDS = 16.0
NOT_HEARD_REPLY = "Mình chưa nghe rõ ý bạn vừa nói, bạn nhắc lại rõ hơn một chút nhé."


@dataclass
class ClauseAudio:
    """Âm thanh của 1 mệnh đề đi từ tầng tổng hợp sang tầng gửi"""
    seq: int
    text: str
    engine: str = ""
    streamed: bool = False
    first_latency: float = 0.0   # tổng hợp tới mẩu đầu (s)
    synth_seconds: float = 0.0   # tổng thời gian tổng hợp cả mệnh đề (s), có khi mẩu cuối đã ra
    # Mẩu PCM 16kHz chưa tăng âm lượng; None = hết mệnh đề. chunks=None -> chế độ chỉ chữ
    chunks: Optional[asyncio.Queue] = field(default_factory=asyncio.Queue)


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
    audio_queue: asyncio.Queue[Optional[ClauseAudio]] = asyncio.Queue()
    full_reply_parts: List[str] = []
    planned_clauses: List[str] = []  # mọi mệnh đề đã cắt từ Gemini (theo thứ tự) -> ước lượng thời gian tổng hợp còn lại

    async def tts_worker() -> None:
        """
        Tầng 2: lấy mệnh đề -> tổng hợp -> đẩy sang tầng gửi (không chờ gửi xong).
        Mệnh đề được chuyển sang tầng 3 ngay khi có mẩu âm thanh ĐẦU TIÊN; các mẩu sau đi tiếp qua item.chunks.
        """
        seq = 0
        while True:
            clause = await clause_queue.get()
            if clause is None:
                await audio_queue.put(None)
                break
            seq += 1
            if not tts_enabled:
                await audio_queue.put(ClauseAudio(seq, clause, chunks=None))
                continue
            item = ClauseAudio(seq, clause)
            t_c0 = time.perf_counter()
            got_any = False
            try:
                async for pcm, engine_name, streamed in stream_clause_pcm(clause):
                    if not got_any:
                        got_any = True
                        item.engine, item.streamed = engine_name, streamed
                        item.first_latency = time.perf_counter() - t_c0
                        await audio_queue.put(item)
                    item.chunks.put_nowait(pcm)
            finally:
                item.synth_seconds = time.perf_counter() - t_c0
                if got_any:
                    item.chunks.put_nowait(None)

    async def sender() -> None:
        """Tầng 3: gửi chữ + PCM của từng mệnh đề xuống thiết bị theo nhịp phát"""
        paced = PacedPcmSender(websocket, send_lead)
        ref_peak = 0.0
        stream_ref = TTS_EST["stream_peak"]  # mức chuẩn hóa cố định cho cả lượt khi phát dần
        clause_idx = 0
        first_out = False
        while True:
            item = await audio_queue.get()
            if item is None:
                break
            clause = item.text
            clause_idx += 1
            full_reply_parts.append(clause)
            conv_entry["ai"] = " ".join(full_reply_parts)

            if item.chunks is None:
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
                # Client không phát theo nhịp (PC...): gom đủ mệnh đề rồi gửi 1 lần như trước
                parts: List[bytes] = []
                while (pcm := await item.chunks.get()) is not None:
                    parts.append(pcm)
                await websocket.send_text(f"AI:{clause}")
                await websocket.send_bytes(b"".join(parts))
                continue

            pcm = await item.chunks.get()  # mẩu đầu luôn có (mệnh đề chỉ được đưa sang khi đã có mẩu đầu)
            if item.streamed:
                out, pk_in, pk_out, _ = boost_tts_pcm(pcm, stream_ref, fixed_ref=True, fade_in=True, fade_out=False)
            else:
                out, pk_in, pk_out, ref_peak = boost_tts_pcm(pcm, ref_peak)
            first_dur = len(out) / BYTES_PER_SECOND
            gap = paced.starving_seconds()

            if not first_out:
                first_out = True
                # Tổng hợp gần sát thời gian thực -> chờ thêm một chút để cả câu trả lời phát liền mạch
                est = estimate_prebuffer_seconds(
                    first_dur, planned_clauses[item.seq:], streaming=item.streamed,
                    current_remaining_text=clause if item.streamed else "",
                )
                wait = min(float(CFG["tts_max_prebuffer"]), est)
                if wait > 0.05:
                    await asyncio.sleep(wait)
                ttfa = time.perf_counter() - turn_start_time
                conv_entry["latency"] = f"TTFA {ttfa:.2f}s"
                wait_note = f", chờ thêm {wait:.2f}s cho {len(planned_clauses) - item.seq} mệnh đề sau" if wait > 0.05 else ""
                if item.streamed:
                    rtf_note = f"mẩu đầu sau {item.first_latency:.2f}s, dài {first_dur:.2f}s, RTF stream ước lượng {TTS_EST['stream_rtf']:.2f}"
                    vol_note = f"chuẩn hóa theo đỉnh {stream_ref * 100:.0f}%"
                else:
                    rtf_note = f"tổng hợp {item.first_latency:.2f}s, dài {first_dur:.2f}s, RTF ước lượng {TTS_EST['rtf']:.2f}"
                    vol_note = f"đỉnh {pk_in:.0f}% -> {pk_out:.0f}%"
                write_log(
                    "FIRST_AUDIO_OUT",
                    f"⚡ Bắt đầu phát sau {ttfa:.2f}s ({item.engine}: {rtf_note}{wait_note}, "
                    f"âm lượng {vol_note} +{float(CFG['tts_boost_db']):.0f}dB nén mềm, "
                    f"gửi trước loa tối đa {send_lead:.1f}s): \"{clause}\"",
                )

            # Gửi chữ của mệnh đề ngay trước PCM để màn hình OLED trên ESP32 hiển thị đồng bộ với tiếng
            await websocket.send_text(f"AI:{clause}")
            await paced.send(out)
            c_dur, max_gap, peak_in = first_dur, gap, pk_in

            # Các mẩu tiếp theo của mệnh đề (phát dần) - gửi ngay khi tổng hợp xong
            while (pcm := await item.chunks.get()) is not None:
                max_gap = max(max_gap, paced.starving_seconds())
                out, pk_in, _, _ = boost_tts_pcm(pcm, stream_ref, fixed_ref=True, fade_in=False, fade_out=False)
                peak_in = max(peak_in, pk_in)
                c_dur += len(out) / BYTES_PER_SECOND
                await paced.send(out)

            if item.streamed:
                update_stream_estimates(clause, item.first_latency, item.synth_seconds, c_dur, peak_in / 100)
                speed = f"mẩu đầu {item.first_latency:.2f}s, xong {item.synth_seconds:.2f}s"
            else:
                update_tts_estimates(clause, item.synth_seconds, c_dur)
                speed = f"{item.synth_seconds:.2f}s"
            gap_note = f" | ⚠️ loa đã chờ {max_gap:.2f}s (tổng hợp không kịp)" if max_gap > 0.05 else ""
            write_log(
                "TTS_CHUNK_OUT",
                f"🔊 Mệnh đề #{clause_idx} ({item.engine}: {speed}, Dài: {c_dur:.2f}s{gap_note}): \"{clause}\"",
            )

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
