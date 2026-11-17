import asyncio
from datetime import datetime
import os
from typing import Optional
import websockets
import sounddevice as sd
import numpy as np
from scipy.signal import butter, sosfilt

SERVER_WS_URL = "ws://localhost:8000/ws/audio?format=pcm&vad=auto"
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1024  # ~64ms mỗi gói
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_session.log")

# Bộ lọc thông dải 300Hz - 3400Hz hiển thị đúng năng lượng giọng người trên Terminal
VOICE_BANDPASS_SOS = butter(
    N=4,
    Wn=[300.0 / (SAMPLE_RATE / 2.0), 3400.0 / (SAMPLE_RATE / 2.0)],
    btype="bandpass",
    output="sos",
)


def log_client_event(stage: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{stage}] {msg}"
    print(f"\n{line}", flush=True)
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def print_audio_devices():
    try:
        in_dev = sd.query_devices(kind="input")
        out_dev = sd.query_devices(kind="output")
        print(f"🎙️  Thiết bị Thu (Mic)     : {in_dev['name']}")
        print(f"🎧  Thiết bị Phát (Tai nghe): {out_dev['name']}")
    except Exception as e:
        print(f"[!] Không đọc được thông tin thiết bị âm thanh: {e}")


async def run_live_duplex_client():
    print("=======================================================================")
    print("🎧 ĐÀM THOẠI RẢNH TAY GEMINI AI - STREAMING CHUNK TTS (BƯỚC 3 - 5)")
    print("=======================================================================")
    print_audio_devices()
    print(f"📝 File ghi log: {LOG_FILE_PATH}")
    print("-----------------------------------------------------------------------")

    is_playing_tts = False
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
    speaker_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def mic_callback(indata, frames, time_info, status):
        if not is_playing_tts:
            loop.call_soon_threadsafe(audio_queue.put_nowait, bytes(indata))

    # Mở sẵn luồng phát âm thanh liên tục (RawOutputStream) ra Tai nghe để phát từng Chunk ngay lập tức
    speaker_stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    speaker_stream.start()

    async with websockets.connect(SERVER_WS_URL, ping_interval=None, ping_timeout=None) as ws:
        log_client_event("CLIENT_READY", "Đã kết nối Server (Streaming Chunk Playback). Sẵn sàng thu âm từ Mic.")

        mic_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=CHUNK_SAMPLES,
            dtype="int16",
            channels=1,
            callback=mic_callback,
        )
        mic_stream.start()

        async def send_mic_task():
            while True:
                pcm_chunk = await audio_queue.get()
                if not is_playing_tts:
                    raw = np.frombuffer(pcm_chunk, dtype=np.int16)
                    filtered = sosfilt(VOICE_BANDPASS_SOS, raw.astype(np.float32))
                    peak = int(np.max(np.abs(filtered))) if len(filtered) > 0 else 0
                    rms = int(np.sqrt(np.mean(filtered ** 2))) if len(filtered) > 0 else 0
                    bars = int(min(peak / 32768.0, 1.0) * 25)
                    meter = "█" * bars + "-" * (25 - bars)
                    tag = "🗣️ Đang thu giọng nói..." if (rms >= 350 and peak >= 1800) else "👂 Đang lắng nghe (Đã lọc ồn)"
                    print(f"\rMic: |{meter}| RMS:{rms:4d} Peak:{peak:5d} {tag}", end="", flush=True)
                    await ws.send(pcm_chunk)

        async def speaker_player_task():
            """
            Nhận từng gói PCM 2KB từ hàng đợi và ghi thẳng ra tai nghe ngay khi gói đầu tiên vừa tới!
            Không cần chờ tải xong toàn bộ câu trả lời.
            """
            nonlocal is_playing_tts
            turn_bytes_played = 0
            started_turn_log = False

            while True:
                item = await speaker_queue.get()
                if item is None:
                    # Đã phát hết toàn bộ các chunk của lượt trả lời hiện tại
                    if turn_bytes_played > 0:
                        dur = turn_bytes_played / (SAMPLE_RATE * 2)
                        await asyncio.sleep(0.12)
                        log_client_event("MIC_OPEN", f"Đã phát xong toàn bộ luồng ({dur:.2f}s). Mở lại Mic chờ bạn nói tiếp.")
                    turn_bytes_played = 0
                    started_turn_log = False
                    while not audio_queue.empty():
                        audio_queue.get_nowait()
                    is_playing_tts = False
                else:
                    if not started_turn_log:
                        started_turn_log = True
                        log_client_event("HEADPHONE_STREAM_START", "🎧 Tai nghe bắt đầu phát ngay gói âm thanh đầu tiên (Streaming)...")
                    turn_bytes_played += len(item)
                    await asyncio.to_thread(speaker_stream.write, item)

        async def receive_server_task():
            nonlocal is_playing_tts

            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    # Đẩy ngay gói byte PCM vừa nhận được vào luồng phát tai nghe!
                    await speaker_queue.put(msg)
                elif isinstance(msg, str):
                    if msg == "START_OF_TTS":
                        is_playing_tts = True
                        while not audio_queue.empty():
                            audio_queue.get_nowait()
                    elif msg == "END_OF_TTS":
                        # Đánh dấu kết thúc lượt để speaker_player_task mở lại Mic sau khi phát xong gói cuối
                        await speaker_queue.put(None)

        try:
            await asyncio.gather(send_mic_task(), speaker_player_task(), receive_server_task())
        finally:
            mic_stream.stop()
            mic_stream.close()
            speaker_stream.stop()
            speaker_stream.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_live_duplex_client())
    except KeyboardInterrupt:
        print("\n👋 Đã kết thúc phiên đàm thoại.")
