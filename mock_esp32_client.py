import asyncio
import websockets
import sounddevice as sd
import numpy as np
import miniaudio

SERVER_WS_URL = "ws://localhost:8000/ws/audio"
SAMPLE_RATE = 16000
RECORD_SECONDS = 4


async def simulate_esp32():
    async with websockets.connect(SERVER_WS_URL) as ws:
        print(f"🎤 Bắt đầu thu âm từ Mic trong {RECORD_SECONDS} giây... Hãy nói gì đó!")

        # Thu âm chuẩn 16kHz, 16-bit, Mono
        recording = sd.rec(int(RECORD_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='int16')
        sd.wait()
        print("✅ Thu âm xong. Đang giả lập ESP32 stream từng gói dữ liệu lên Server...")

        raw_bytes = recording.tobytes()
        chunk_size = 2048  # Cắt nhỏ gửi từng gói 2KB như bộ đệm của ESP32
        for i in range(0, len(raw_bytes), chunk_size):
            await ws.send(raw_bytes[i:i+chunk_size])
            await asyncio.sleep(0.02)

        # Báo hiệu đã nói xong
        await ws.send("END_OF_SPEECH")
        print("⏳ Đang chờ Server phản hồi âm thanh...")

        # Nhận luồng âm thanh TTS từ Server trả về
        tts_audio = bytearray()
        while True:
            response = await ws.recv()
            if isinstance(response, bytes):
                tts_audio.extend(response)
            elif response == "END_OF_TTS":
                break

        output_file = "server_reply.mp3"
        with open(output_file, "wb") as f:
            f.write(tts_audio)
        print(f"💾 Đã lưu phản hồi vào '{output_file}'. Đang phát trực tiếp ra Tai nghe / Loa...")

        # Giải mã MP3 và phát trực tiếp ra tai nghe
        decoded = miniaudio.decode(
            bytes(tts_audio),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=SAMPLE_RATE,
        )
        samples = np.frombuffer(decoded.samples, dtype=np.int16)
        sd.play(samples, samplerate=SAMPLE_RATE)
        sd.wait()
        print("🎧 Đã phát xong phản hồi qua tai nghe!")


if __name__ == "__main__":
    asyncio.run(simulate_esp32())
