"""Chẩn đoán Mic: lưu WAV gốc mỗi lượt nói và đo tốc độ nhận byte từ thiết bị (chuẩn 32000 B/s)."""
import os
import time
from datetime import datetime

from autobot.audio.dsp import raw_pcm_to_wav_bytes
from autobot.config.store import CFG
from autobot.core.audio_format import BYTES_PER_SECOND
from autobot.core.logger import write_log
from autobot.core.paths import RECORDINGS_DIR

RX_RATE_WINDOW_SECONDS = 10.0


def save_turn_wav(pcm_bytes: bytes) -> None:
    """Lưu âm thanh gốc (chưa lọc) của 1 lượt nói vào recordings/ nếu bật cài đặt debug_save_wav"""
    if not CFG["debug_save_wav"]:
        return
    try:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        wav_path = os.path.join(RECORDINGS_DIR, f"{datetime.now():%Y%m%d_%H%M%S}_raw.wav")
        with open(wav_path, "wb") as f:
            f.write(raw_pcm_to_wav_bytes(pcm_bytes))
        write_log("DEBUG_WAV", f"Đã lưu âm thanh gốc {len(pcm_bytes) / BYTES_PER_SECOND:.2f}s -> {wav_path}")
    except Exception as e:
        write_log("DEBUG_WAV", f"Lỗi lưu WAV: {e}")


class RxRateMonitor:
    """Đếm byte/gói nhận được, cứ mỗi 10s ghi log tốc độ (khi bật cài đặt debug_log_rx_rate)"""

    def __init__(self) -> None:
        self.bytes = 0
        self.chunks = 0
        self.t0 = time.perf_counter()

    def add(self, n_bytes: int) -> None:
        if not CFG["debug_log_rx_rate"]:
            return
        now_t = time.perf_counter()
        self.bytes += n_bytes
        self.chunks += 1
        win = now_t - self.t0
        if win >= RX_RATE_WINDOW_SECONDS:
            rate = self.bytes / win
            write_log(
                "RX_RATE",
                f"Nhận {rate:.0f} B/s ({rate / BYTES_PER_SECOND * 100:.0f}% so với chuẩn 32000) | "
                f"{self.chunks / win:.1f} gói/s, TB {self.bytes // max(self.chunks, 1)} B/gói",
            )
            self.bytes, self.chunks, self.t0 = 0, 0, now_t
