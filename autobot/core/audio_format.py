"""Định dạng âm thanh chuẩn giữa server và thiết bị: PCM 16kHz, 16-bit, Mono."""

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # 32,000 bytes/s

PCM_CHUNK_SIZE = 2048  # Kích thước gói PCM stream về Client/ESP32 (2KB)
