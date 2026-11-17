"""
WebSocket /ws/audio - kênh 2 chiều với thiết bị (ESP32 / PC client).

Tham số URL:
  format=pcm|mp3   định dạng âm thanh trả về (ESP32 dùng pcm)
  vad=auto|manual  auto: server tự ngắt câu; manual: thiết bị gửi "END_OF_SPEECH"
  tts=on|off       ép bật/tắt giọng đọc cho kết nối này (bỏ trống = theo Dashboard)
  buf=<bytes>      dung lượng bộ đệm loa của thiết bị (quyết định gửi trước bao nhiêu giây)
  dev=<id>         mã thiết bị (MAC) để giữ ngữ cảnh khi kết nối lại
Thiết bị nhận: "START_OF_TTS", "USER:<chữ>", "AI:<chữ>", PCM/MP3 bytes, "END_OF_TTS".
"""
import asyncio
import contextlib
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from autobot.audio.diagnostics import RxRateMonitor
from autobot.audio.vad import CapturedSpeech, VoiceActivityDetector
from autobot.conversation.sessions import get_device_session
from autobot.conversation.turn import TurnContext, process_turn
from autobot.core.logger import write_log
from autobot.devices import registry
from autobot.tts.playback import send_lead_for_buffer

router = APIRouter()


@router.websocket("/ws/audio")
async def audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = websocket.query_params
    output_format = q.get("format", "mp3").lower()
    vad_mode = q.get("vad", "manual").lower()
    tts_param = q.get("tts", "").lower()
    tts_forced = tts_param if tts_param in ("on", "off") else ""
    try:
        device_buf = int(q.get("buf", "0"))
    except ValueError:
        device_buf = 0
    send_lead = send_lead_for_buffer(device_buf)

    client_addr = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    device_key = q.get("dev", "").strip() or (websocket.client.host if websocket.client else "?")

    dev_id = id(websocket)
    dev = registry.register(dev_id, websocket, client_addr, tts_forced, output_format, send_lead)
    session_state, restored = get_device_session(device_key)

    tts_info = "Giọng đọc TTS: BẬT" if registry.device_tts_enabled(dev) else "Giọng đọc TTS: TẮT - chỉ trả lời bằng chữ"
    buf_info = f"{device_buf // 1024}KB" if device_buf else "không báo (giả định 32KB)"
    ctx_info = f"Khôi phục ngữ cảnh {session_state['turn_count']} lượt trước" if restored else "Cuộc trò chuyện mới"
    write_log(
        "WS_CONNECT",
        f"Thiết bị đã kết nối {client_addr} [id: {device_key}] (Output: {output_format.upper()} | VAD: {vad_mode.upper()} | "
        f"{tts_info} | Bộ đệm loa: {buf_info} -> gửi trước tối đa {send_lead:.1f}s | {ctx_info})",
    )

    vad = VoiceActivityDetector(auto=(vad_mode == "auto"))
    rx_monitor = RxRateMonitor()

    async def respond(speech: CapturedSpeech) -> None:
        """Bọc lượt xử lý: đánh dấu thiết bị bận trên Dashboard (không cho Phát thử chen vào)"""
        dev["busy"] = True
        dev["state"] = registry.STATE_PROCESSING
        try:
            ctx = TurnContext(
                websocket=websocket, session_state=session_state, output_format=output_format,
                vad_mode=vad_mode, send_lead=send_lead, tts_enabled=registry.device_tts_enabled(dev),
            )
            await process_turn(ctx, speech)
        finally:
            dev["busy"] = False
            dev["state"] = registry.STATE_LISTENING

    # Lượt xử lý (STT -> AI -> giọng đọc) chạy NỀN, vòng nhận bên dưới vẫn đọc socket liên tục.
    # Trước đây vòng nhận bị chặn suốt lượt: thiết bị còn gửi mic thì hàng đợi đầy, server không trả lời được
    # ping -> rớt kết nối sau ~20s (lượt dài hơn từ khi gửi PCM theo nhịp phát). Mic gửi lên trong lúc AI đang
    # trả lời bị bỏ qua (half-duplex), tránh tiếng AI dội vào mic bị nhận nhầm thành câu nói mới.
    turn_task: Optional[asyncio.Task] = None

    def busy() -> bool:
        return turn_task is not None and not turn_task.done()

    def on_turn_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None and not isinstance(t.exception(), WebSocketDisconnect):
            write_log("TURN_ERROR", f"Lỗi xử lý lượt nói: {type(t.exception()).__name__}: {t.exception()}")

    def start_turn(speech: CapturedSpeech) -> None:
        nonlocal turn_task
        turn_task = asyncio.create_task(respond(speech))
        turn_task.add_done_callback(on_turn_done)

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                write_log("WS_DISCONNECT", "Thiết bị đã ngắt kết nối.")
                break

            # 1. Luồng byte âm thanh từ Mic
            if message.get("bytes"):
                chunk = message["bytes"]
                rx_monitor.add(len(chunk))
                if busy():
                    continue
                speech = vad.feed(chunk)
                lv = vad.levels
                if lv is not None:
                    dev.update(floor=lv.floor, start_thr=lv.start_thr, cont_thr=lv.cont_thr, level=lv.smooth_rms)
                if not dev["busy"]:
                    dev["state"] = registry.STATE_RECORDING if vad.is_speaking else registry.STATE_LISTENING
                if speech is not None:
                    start_turn(speech)

            # 2. Tín hiệu điều khiển dạng text
            elif message.get("text"):
                if message["text"] == "END_OF_SPEECH" and not busy():
                    start_turn(vad.flush())

    except WebSocketDisconnect:
        write_log("WS_DISCONNECT", "Thiết bị đã ngắt kết nối.")
    finally:
        if busy():
            turn_task.cancel()
            with contextlib.suppress(BaseException):
                await turn_task
        registry.unregister(dev_id)
