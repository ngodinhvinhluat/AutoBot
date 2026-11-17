import asyncio
import io
import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd
import websockets

SERVER_URL_STEP5 = "ws://localhost:8000/ws/audio?format=pcm&vad=auto"
SAMPLE_RATE = 16000


async def generate_spoken_pcm(text: str) -> bytes:
    """Tạo giọng nói tiếng Việt giả lập người dùng nói vào Mic (16kHz, 16-bit, Mono)"""
    mp3_bytes = b""
    try:
        comm = edge_tts.Communicate(text, "vi-VN-HoaiMyNeural")
        mp3_buf = io.BytesIO()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                mp3_buf.write(chunk["data"])
        mp3_bytes = mp3_buf.getvalue()
    except Exception:
        pass

    if not mp3_bytes:
        from gtts import gTTS
        buf = io.BytesIO()
        await asyncio.to_thread(lambda: gTTS(text=text, lang="vi").write_to_fp(buf))
        mp3_bytes = buf.getvalue()

    decoded = miniaudio.decode(
        mp3_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SAMPLE_RATE,
    )
    return bytes(decoded.samples)


async def send_utterance_and_play_reply(ws, turn_idx: int, user_sentence: str):
    print(f"\n--- LƯỢT {turn_idx}: Giả lập người dùng nói: '{user_sentence}' ---")
    user_pcm = await generate_spoken_pcm(user_sentence)

    chunk_size = 2048
    for i in range(0, len(user_pcm), chunk_size):
        await ws.send(user_pcm[i : i + chunk_size])
        await asyncio.sleep(0.005)

    # Gửi 1.2 giây khoảng lặng để Auto-VAD tự động chốt câu nói
    silence_chunk = bytes(chunk_size)
    num_silence_chunks = int((SAMPLE_RATE * 2 * 1.2) / chunk_size) + 2
    for _ in range(num_silence_chunks):
        await ws.send(silence_chunk)
        await asyncio.sleep(0.01)

    pcm_reply = bytearray()
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
        if isinstance(msg, bytes):
            pcm_reply.extend(msg)
        elif msg == "END_OF_TTS":
            break

    duration_sec = len(pcm_reply) / (SAMPLE_RATE * 2)
    print(f"🎧 [Tai nghe] Nhận {len(pcm_reply)} bytes PCM (~{duration_sec:.2f}s) -> Đang phát ra Tai nghe của bạn...")
    audio_np = np.frombuffer(pcm_reply, dtype=np.int16)
    await asyncio.to_thread(sd.play, audio_np, SAMPLE_RATE)
    await asyncio.to_thread(sd.wait)
    print(f"✅ Đã phát xong phản hồi Lượt {turn_idx} qua Tai nghe!")


async def test_contextual_conversation():
    print("=======================================================================")
    print("🧪 MÔ PHỎNG ĐÀM THOẠI 2 LƯỢT THEO NGỮ CẢNH & PHÁT RA TAI NGHE (BƯỚC 3-5)")
    print("=======================================================================")
    out_dev = sd.query_devices(kind="output")
    print(f"🎧 Thiết bị Tai nghe/Loa đang phát: {out_dev['name']}")

    async with websockets.connect(SERVER_URL_STEP5) as ws:
        # Lượt 1: Giới thiệu tên
        await send_utterance_and_play_reply(ws, 1, "Xin chào, mình tên là Luật")
        # Lượt 2: Kiểm tra trí nhớ ngữ cảnh của AI trong cùng phiên kết nối
        await send_utterance_and_play_reply(ws, 2, "Bạn có nhớ mình tên là gì không")


if __name__ == "__main__":
    asyncio.run(test_contextual_conversation())
