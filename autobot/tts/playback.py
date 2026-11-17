"""Chuẩn bị & gửi PCM giọng đọc xuống loa thiết bị: tăng âm lượng mềm + gửi theo nhịp phát (chống tràn bộ đệm ESP32)."""
import asyncio
import time
from typing import Any, Dict, List

import numpy as np

from autobot.config.store import CFG
from autobot.core.audio_format import BYTES_PER_SECOND, PCM_CHUNK_SIZE

TTS_FADE_SAMPLES = 64  # Fade-in/out 4ms ở 2 đầu mỗi mệnh đề -> chỗ nối giữa các mệnh đề không bị "tạch"

# Gửi PCM xuống thiết bị THEO NHỊP PHÁT: chỉ đi trước loa tối đa `lead` giây thay vì đổ cả mệnh đề
# (100-170KB) một lần -> bộ đệm nhỏ của ESP32 không bị tràn và rơi gói (nguyên nhân chính gây giật tiếng).
# Thiết bị báo dung lượng bộ đệm qua tham số URL ?buf=<bytes>; không báo -> giả định bộ đệm cũ 32KB (~1s).
TTS_SEND_LEAD_DEFAULT = 0.6        # giây, cho thiết bị không báo buf (firmware cũ / PC client)
TTS_SEND_LEAD_MAX = 3.0            # giây, đủ che giật mạng WiFi mà vẫn kết thúc lượt nhanh
TTS_SEND_LEAD_BUFFER_RATIO = 0.75  # chỉ dùng tối đa 75% bộ đệm của thiết bị


def boost_tts_pcm(
    pcm_bytes: bytes,
    ref_peak: float = 0.0,
    *,
    fixed_ref: bool = False,
    fade_in: bool = True,
    fade_out: bool = True,
) -> tuple[bytes, float, float, float]:
    """
    Chuẩn hóa + tăng âm lượng PCM 16-bit. Trả về (pcm mới, peak gốc %, peak mới %, ref_peak dùng).
    ref_peak: đỉnh tham chiếu của CẢ LƯỢT (truyền lại giá trị trả về cho mệnh đề sau) -> mọi mệnh đề
    trong 1 câu trả lời dùng chung 1 hệ số khuếch đại, không bị to/nhỏ thất thường giữa các mệnh đề.
    fixed_ref=True (phát dần từng mẩu): KHÔNG nâng tham chiếu theo mẩu, vì đổi hệ số giữa chừng mệnh đề
    nghe rõ tiếng to/nhỏ giật cục; phần vượt tham chiếu do nén mềm tanh giữ lại (luôn bật nén, tối thiểu 0dB).
    """
    x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) == 0:
        return pcm_bytes, 0.0, 0.0, ref_peak
    pk = float(np.max(np.abs(x)))
    if pk < 1e-4 and not fixed_ref:
        return pcm_bytes, pk * 100, pk * 100, ref_peak
    ref = max(ref_peak, 1e-3) if fixed_ref else max(ref_peak, pk)  # mệnh đề to hơn tham chiếu -> nâng tham chiếu
    y = x / ref  # chuẩn hóa theo đỉnh của cả lượt
    g = 10 ** (float(CFG["tts_boost_db"]) / 20.0)
    if fixed_ref:
        g = max(g, 1.0)
    if g > 1.0 or fixed_ref:
        y = np.tanh(y * g) / np.tanh(g)  # nén mềm: tiếng nhỏ to lên, đỉnh vẫn giữ <= ~1.0
    y *= 0.95
    n = min(TTS_FADE_SAMPLES, len(y) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        if fade_in:
            y[:n] *= ramp
        if fade_out:
            y[-n:] *= ramp[::-1]
    out = (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)
    return out.tobytes(), pk * 100, float(np.max(np.abs(y))) * 100, ref


def send_lead_for_buffer(buf_bytes: int) -> float:
    """Số giây được phép gửi trước loa, tính từ dung lượng bộ đệm phát thiết bị báo lên"""
    if buf_bytes <= 0:
        return TTS_SEND_LEAD_DEFAULT
    return max(0.3, min(TTS_SEND_LEAD_MAX, TTS_SEND_LEAD_BUFFER_RATIO * buf_bytes / BYTES_PER_SECOND))


# Ước lượng tốc độ bộ tổng hợp (cập nhật liên tục từ số đo thật, giữ qua các lượt).
# VieNeu trên CPU (i7-7700) đo được RTF ~0.7-1.1 = gần sát thời gian thực -> mệnh đề sau có thể chưa xong khi loa phát hết.
# - rtf: tổng hợp CẢ mệnh đề (infer / Edge) = thời gian tổng hợp / thời lượng âm thanh.
# - stream_rtf, stream_startup: phát dần (VieNeu infer_stream) = tốc độ sau mẩu đầu, và độ trễ tới mẩu đầu.
#   Đo trên máy rảnh: stream_rtf ~0.67, mẩu đầu ~0.35-0.5s.
# - stream_peak: đỉnh âm lượng gốc điển hình của giọng VieNeu (~0.5-0.65) -> mức chuẩn hóa cố định khi phát dần.
TTS_EST: Dict[str, float] = {"rtf": 0.85, "sec_per_char": 0.06, "stream_rtf": 0.75, "stream_startup": 0.45, "stream_peak": 0.6}


def update_tts_estimates(text: str, synth_seconds: float, audio_seconds: float) -> None:
    """Cập nhật sau mỗi mệnh đề tổng hợp CẢ KHỐI"""
    if audio_seconds > 0.3 and text:
        # Chặn mẫu đo bất thường (vd. lần đầu tính cả ~12s nạp model VieNeu -> RTF 7) để ước lượng không bị lệch
        rtf = min(synth_seconds / audio_seconds, 2.0)
        TTS_EST["rtf"] = 0.7 * TTS_EST["rtf"] + 0.3 * rtf
        TTS_EST["sec_per_char"] = 0.8 * TTS_EST["sec_per_char"] + 0.2 * (audio_seconds / len(text))


def update_stream_estimates(text: str, startup: float, synth_seconds: float, audio_seconds: float, peak: float) -> None:
    """Cập nhật sau mỗi mệnh đề PHÁT DẦN (startup = độ trễ tới mẩu đầu, peak = đỉnh gốc 0..1)"""
    if audio_seconds > 0.3 and text:
        rtf = min(max(synth_seconds - startup, 0.0) / audio_seconds, 2.0)
        TTS_EST["stream_rtf"] = 0.7 * TTS_EST["stream_rtf"] + 0.3 * rtf
        TTS_EST["stream_startup"] = 0.7 * TTS_EST["stream_startup"] + 0.3 * min(startup, 2.0)
        TTS_EST["sec_per_char"] = 0.8 * TTS_EST["sec_per_char"] + 0.2 * (audio_seconds / len(text))
    if 0.05 < peak <= 1.0:
        TTS_EST["stream_peak"] = 0.8 * TTS_EST["stream_peak"] + 0.2 * peak


def estimate_prebuffer_seconds(
    ready_audio_seconds: float,
    remaining_texts: List[str],
    streaming: bool = False,
    current_remaining_text: str = "",
) -> float:
    """
    Cần chờ thêm bao lâu trước khi phát mệnh đề đầu để các mệnh đề sau (tổng hợp nối tiếp) luôn xong
    TRƯỚC khi loa phát hết phần đã có -> cả câu trả lời phát liền mạch, không bị ngắt quãng giữa chừng.
    ready_audio_seconds: âm thanh đã sẵn sàng phát (cả mệnh đề đầu, hoặc mẩu đầu khi phát dần).
    streaming: mỗi mệnh đề sau ra mẩu đầu sau `stream_startup` rồi chạy với tốc độ `stream_rtf`
    (phần còn lại của mệnh đề hiện tại `current_remaining_text` chạy tiếp ngay, không có độ trễ khởi động).
    """
    spc = TTS_EST["sec_per_char"]
    # Mỗi đoạn: (độ trễ trước khi có mẩu đầu, tốc độ tổng hợp trong lúc phát, thời lượng âm thanh)
    segments: List[tuple[float, float, float]] = []
    if streaming:
        r, s0 = TTS_EST["stream_rtf"] * 1.1, TTS_EST["stream_startup"] * 1.1  # biên an toàn 10%
        if current_remaining_text:
            segments.append((0.0, r, max(0.0, spc * len(current_remaining_text) - ready_audio_seconds)))
        segments += [(s0, r, spc * len(t)) for t in remaining_texts]
    else:
        r = TTS_EST["rtf"] * 1.1
        segments += [(r * spc * len(t), 0.0, spc * len(t)) for t in remaining_texts]

    need, synth_t, audio_t = 0.0, 0.0, ready_audio_seconds
    for startup, rate, dur in segments:
        synth_t += startup                      # mẩu đầu của đoạn này có lúc synth_t, loa cần nó lúc audio_t
        need = max(need, synth_t - audio_t)
        synth_t += rate * dur                   # phần còn lại ra dần; tốc độ < 1 -> chỉ cần kiểm tra 2 đầu đoạn
        audio_t += dur
        need = max(need, synth_t - audio_t)
    return need


class PacedPcmSender:
    """Ước lượng đồng hồ phát của thiết bị (phát đúng thời gian thực) và chỉ gửi trước tối đa `lead` giây"""

    def __init__(self, websocket: Any, lead: float):
        self.ws = websocket
        self.lead = lead
        self.play_end = 0.0  # thời điểm (perf_counter) thiết bị phát xong phần đã gửi

    def starving_seconds(self) -> float:
        """Loa đã cạn dữ liệu bao lâu (> 0 nghĩa là đang có khoảng lặng/giật do tổng hợp không kịp)"""
        if self.play_end <= 0:
            return 0.0
        return max(0.0, time.perf_counter() - self.play_end)

    async def send(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), PCM_CHUNK_SIZE):
            part = pcm[i : i + PCM_CHUNK_SIZE]
            now = time.perf_counter()
            ahead = self.play_end - now
            if ahead > self.lead:
                await asyncio.sleep(ahead - self.lead)
                now = time.perf_counter()
            self.play_end = max(self.play_end, now) + len(part) / BYTES_PER_SECOND
            await self.ws.send_bytes(part)
