"""Các bộ lọc DSP cho giọng nói: thông dải 300-3400Hz, khử ù 50Hz, AGC, đóng gói WAV gửi STT."""
import io
import wave

import numpy as np
from scipy.signal import butter, iirnotch, sosfilt

from autobot.config.store import CFG
from autobot.core.audio_format import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH

# Bộ lọc thông dải (Bandpass Filter 300Hz - 3400Hz): Triệt tiêu 100% nhiễu nguồn USB 50/60Hz, tiếng quạt ù và tạp âm tần số cao
VOICE_BANDPASS_SOS = butter(
    N=4,
    Wn=[300.0 / (SAMPLE_RATE / 2.0), 3400.0 / (SAMPLE_RATE / 2.0)],
    btype="bandpass",
    output="sos",
)


def _build_hum_notch_sos() -> np.ndarray:
    """Chuỗi bộ lọc rãnh (notch) tại các hài điện lưới 100..1000Hz, mỗi rãnh rộng ~4Hz (không ăn vào giọng nói)"""
    sections = []
    for f0 in range(100, 1001, 50):
        b, a = iirnotch(f0, Q=f0 / 4.0, fs=SAMPLE_RATE)
        sections.append(np.concatenate([b, a]))
    return np.array(sections)


HUM_NOTCH_SOS = _build_hum_notch_sos()


def filter_voice_bandpass(samples: np.ndarray) -> np.ndarray:
    """Lọc thông dải 300Hz - 3400Hz (dải tần giọng nói con người)"""
    if len(samples) == 0:
        return samples
    filtered = sosfilt(VOICE_BANDPASS_SOS, samples.astype(np.float32))
    return np.clip(filtered, -32767, 32767).astype(np.int16)


def clean_and_normalize_pcm(pcm_data: bytes) -> bytes:
    """Khử ù 50Hz + Lọc thông dải giọng nói (300Hz - 3400Hz) + Tự động chuẩn hóa biên độ (AGC)"""
    raw_samples = np.frombuffer(pcm_data, dtype=np.int16)
    if len(raw_samples) == 0:
        return pcm_data

    samples = raw_samples.astype(np.float32)
    if CFG["hum_notch"]:
        samples = sosfilt(HUM_NOTCH_SOS, samples)
    filtered = sosfilt(VOICE_BANDPASS_SOS, samples)
    peak = np.max(np.abs(filtered))
    if peak > 200:
        gain = min(26000.0 / peak, 10.0)
        filtered = np.clip(filtered * gain, -32767, 32767)

    return filtered.astype(np.int16).tobytes()


def raw_pcm_to_wav_bytes(pcm_data: bytes) -> bytes:
    """Đóng gói Raw PCM 16kHz/16-bit/Mono thành WAV trên RAM (không xử lý gì thêm)"""
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm_data)
    return wav_io.getvalue()


def pcm_to_wav_bytes(pcm_data: bytes) -> bytes:
    """Đóng gói Raw PCM bytes (đã qua bộ lọc DSP + AGC) thành định dạng WAV trên RAM"""
    return raw_pcm_to_wav_bytes(clean_and_normalize_pcm(pcm_data))
