"""
Phát hiện giọng nói (VAD) theo năng lượng tín hiệu đã lọc 300-3400Hz, ngưỡng tự thích nghi theo nền ồn phòng.

Mỗi kết nối thiết bị có 1 VoiceActivityDetector:
- feed(chunk): đưa gói PCM từ Mic vào; trả về CapturedSpeech khi chốt xong 1 câu (chế độ vad=auto).
- flush(): lấy toàn bộ âm thanh đang đệm (khi thiết bị gửi END_OF_SPEECH ở chế độ vad=manual).
"""
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np

from autobot.audio.dsp import filter_voice_bandpass
from autobot.config.store import CFG
from autobot.core.audio_format import BYTES_PER_SECOND
from autobot.core.logger import write_log

VAD_START_CONSECUTIVE = 2        # Cần 2 gói liên tiếp (~128ms) vượt ngưỡng để chống nhiễu click chuột/gõ bàn
VAD_FLOOR_WINDOW_CHUNKS = 187    # ~6s lịch sử (gói ESP32 ~32ms)
VAD_FLOOR_PERCENTILE = 20
VAD_SMOOTH_CHUNKS = 8            # Làm mượt RMS ~250ms để 1 gói ồn lẻ không giữ câu kéo dài
PRE_ROLL_CHUNKS = 20             # ~640ms âm thanh trước lúc VAD bật để không mất phụ âm đầu câu


@dataclass
class CapturedSpeech:
    """1 câu nói đã chốt, sẵn sàng gửi nhận dạng"""
    pcm: bytes
    spoken_sec: float
    max_rms: int = 0
    max_peak: int = 0


@dataclass
class ChunkLevels:
    """Mức tín hiệu + ngưỡng VAD tại gói vừa nhận (hiển thị thanh mức mic trên Dashboard)"""
    rms: int
    peak: int
    smooth_rms: int
    floor: int
    start_thr: int
    cont_thr: int
    start_peak: int
    adaptive: bool


class VoiceActivityDetector:
    def __init__(self, auto: bool) -> None:
        self.auto = auto
        self.is_speaking = False
        self.levels: Optional[ChunkLevels] = None

        self._buffer = bytearray()
        self._pre_roll: Deque[bytes] = deque(maxlen=PRE_ROLL_CHUNKS)
        self._consecutive = 0
        self._speech_sec = 0.0
        self._silence_sec = 0.0
        self._max_rms = 0
        self._max_peak = 0
        self._smooth_sq: Deque[float] = deque(maxlen=VAD_SMOOTH_CHUNKS)      # RMS^2 các gói gần nhất (làm mượt)
        self._floor_hist: Deque[int] = deque(maxlen=VAD_FLOOR_WINDOW_CHUNKS)  # Lịch sử RMS làm mượt ~6s để đo nền ồn

    # ------------------------------------------------------------------
    def feed(self, chunk: bytes) -> Optional[CapturedSpeech]:
        raw_samples = np.frombuffer(chunk, dtype=np.int16)
        if len(raw_samples) == 0:
            return None

        lv = self._measure(raw_samples)
        self.levels = lv
        self._print_meter(lv)

        if not self.auto:
            self._buffer.extend(chunk)
            return None
        return self._step(chunk, lv)

    def flush(self) -> CapturedSpeech:
        self.is_speaking = False
        captured = bytes(self._buffer)
        self._buffer.clear()
        return CapturedSpeech(captured, len(captured) / BYTES_PER_SECOND)

    # ------------------------------------------------------------------
    def _measure(self, raw_samples: np.ndarray) -> ChunkLevels:
        """Lọc thông dải + đo RMS/Peak, cập nhật nền ồn (phân vị thấp của RMS làm mượt) và tính ngưỡng"""
        voice_samples = filter_voice_bandpass(raw_samples)
        peak = int(np.max(np.abs(voice_samples)))
        rms = int(np.sqrt(np.mean(voice_samples.astype(np.float32) ** 2)))

        self._smooth_sq.append(float(rms) ** 2)
        smooth_rms = int(np.sqrt(np.mean(self._smooth_sq)))
        self._floor_hist.append(smooth_rms)
        floor_v = float(np.percentile(self._floor_hist, VAD_FLOOR_PERCENTILE)) if len(self._floor_hist) >= 10 else 0.0

        adaptive = bool(CFG["vad_adaptive"])
        if adaptive:
            start_rms = max(int(CFG["vad_start_rms"]), int(floor_v * float(CFG["vad_start_factor"])))
            continue_rms = max(int(CFG["vad_continue_rms"]), int(floor_v * float(CFG["vad_continue_factor"])))
        else:
            start_rms = int(CFG["vad_start_rms"])
            continue_rms = int(CFG["vad_continue_rms"])
        return ChunkLevels(rms, peak, smooth_rms, int(floor_v), start_rms, continue_rms, int(CFG["vad_start_peak"]), adaptive)

    def _print_meter(self, lv: ChunkLevels) -> None:
        """Vẽ thanh đo sóng âm đã lọc tạp âm trên cùng 1 dòng Terminal"""
        bars = int((lv.peak / 32768.0) * 40)
        meter = "█" * bars + "-" * (40 - bars)
        has_voice_energy = (lv.smooth_rms >= lv.cont_thr) if self.is_speaking else (lv.rms >= lv.start_thr and lv.peak >= lv.start_peak)
        status = " 🗣️ [GIỌNG NÓI]" if has_voice_energy else ""
        print(
            f"\rLọc âm: |{meter}| RMS:{lv.rms:4d} Peak:{lv.peak:5d} Nền:{lv.floor:4d} Ngưỡng:{lv.start_thr}/{lv.cont_thr}{status:<16}",
            end="", flush=True,
        )

    def _step(self, chunk: bytes, lv: ChunkLevels) -> Optional[CapturedSpeech]:
        """Máy trạng thái Auto-VAD: chờ giọng -> đang thu -> chốt câu khi im lặng đủ lâu / quá dài"""
        chunk_duration = len(chunk) / BYTES_PER_SECOND

        if not self.is_speaking:
            self._pre_roll.append(chunk)
            if lv.rms >= lv.start_thr and lv.peak >= lv.start_peak:
                self._consecutive += 1
                if self._consecutive >= VAD_START_CONSECUTIVE:
                    self.is_speaking = True
                    self._speech_sec = chunk_duration * self._consecutive
                    self._silence_sec = 0.0
                    self._max_rms = lv.rms
                    self._max_peak = lv.peak
                    self._consecutive = 0
                    self._buffer.clear()
                    for pre_chunk in self._pre_roll:
                        self._buffer.extend(pre_chunk)
                    self._pre_roll.clear()
                    write_log(
                        "VAD_START",
                        f"🎙️ Bắt đầu thu giọng nói (RMS: {lv.rms}, Peak: {lv.peak} | Nền ồn: {lv.floor} -> ngưỡng bắt đầu {lv.start_thr} / duy trì {lv.cont_thr})",
                    )
            else:
                self._consecutive = 0
            return None

        self._buffer.extend(chunk)
        self._max_rms = max(self._max_rms, lv.rms)
        self._max_peak = max(self._max_peak, lv.peak)

        if (lv.smooth_rms if lv.adaptive else lv.rms) >= lv.cont_thr:
            self._speech_sec += chunk_duration
            self._silence_sec = 0.0
        else:
            self._silence_sec += chunk_duration

        max_len = float(CFG["vad_max_speech_seconds"])
        too_long = len(self._buffer) / BYTES_PER_SECOND >= max_len
        if too_long:
            write_log("VAD_MAX_LEN", f"✂️ Câu dài quá {max_len:.0f}s -> cắt cưỡng bức (Nền ồn: {lv.floor})")
        if self._silence_sec < float(CFG["vad_silence_seconds"]) and not too_long:
            return None

        # Chốt câu
        self.is_speaking = False
        total_spoken, turn_rms, turn_peak = self._speech_sec, self._max_rms, self._max_peak
        self._speech_sec = self._silence_sec = 0.0
        self._max_rms = self._max_peak = 0
        captured = bytes(self._buffer)
        self._buffer.clear()
        if total_spoken < float(CFG["vad_min_speech_seconds"]):
            return None
        write_log(
            "VAD_END",
            f"⏹️ Ngắt câu tự động (Thời lượng nói: {total_spoken:.2f}s | MaxRMS: {turn_rms} | MaxPeak: {turn_peak})",
        )
        return CapturedSpeech(captured, total_spoken, turn_rms, turn_peak)
