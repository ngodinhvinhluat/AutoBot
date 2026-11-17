"""
Bộ tổng hợp VieNeu-TTS v3 Turbo chạy trên máy (48kHz -> 16kHz PCM).
- vieneu_synth_pcm: tổng hợp CẢ mệnh đề rồi mới trả về (infer).
- vieneu_stream_pcm: PHÁT DẦN (infer_stream) - mẩu âm thanh đầu có sau ~0.35-0.5s thay vì chờ 1-3s cho cả mệnh đề
  (đo trên i7-7700, ONNX/CPU: RTF stream ~0.67 < 1 nên phát ngay vẫn không bị đứt).
"""
import asyncio
import os
import shutil
import threading
import time
from typing import Any, Iterator, Optional

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi, resample_poly

from autobot.config.store import CFG
from autobot.core.logger import write_log

VIENEU_SAMPLE_RATE = 48000
_DOWN = 3  # 48kHz -> 16kHz

_VIENEU_TTS: Optional[Any] = None
_LOADED_PRECISION: str = ""
# Engine ONNX chỉ chạy 1 lệnh tổng hợp tại 1 thời điểm (stream của lượt trả lời vs nút "Phát thử" / câu "chưa nghe rõ")
_ENGINE_LOCK = threading.Lock()
# Chỉ 1 lần nạp model tại 1 thời điểm (khởi động / đổi fp32 <-> int8 trên Dashboard)
_LOAD_LOCK = threading.Lock()

_V3_REPO = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
_ONNX_SUBFOLDER = {"fp32": "onnx_update", "int8": "onnx_int8"}
_GRAPH_FILES = [
    "vieneu_prefill.onnx", "vieneu_decode_step.onnx", "vieneu_acoustic_cached.onnx",
    "vieneu_backbone_shared.data", "vieneu_v3_heads.npz", "config.json", "tokenizer.json",
]
LOCAL_ONNX_ROOT = os.path.join(os.path.expanduser("~"), ".cache", "vieneu_local")


def _local_onnx_dir(precision: str) -> Optional[str]:
    """
    Chép các file ONNX của model ra 1 thư mục THẬT (không symlink).
    Lý do: cache HuggingFace trên Windows lưu file dạng symlink trỏ vào kho blob; graph .onnx và file dữ liệu
    .data nằm ở 2 thư mục blob khác nhau -> onnxruntime chặn ("External data path escapes model directory").
    Bản fp32 tải trước đây là file thật nên không bị; bản int8 tải sau bị. Trả None = để thư viện tự tải như cũ.
    """
    if precision == "fp32":
        return None
    from huggingface_hub import hf_hub_download
    sub = _ONNX_SUBFOLDER[precision]
    dst = os.path.join(LOCAL_ONNX_ROOT, sub)
    os.makedirs(dst, exist_ok=True)
    for fn in _GRAPH_FILES:
        src = os.path.realpath(hf_hub_download(_V3_REPO, fn, repo_type="model", subfolder=sub))
        out = os.path.join(dst, fn)
        if not os.path.exists(out) or os.path.getsize(out) != os.path.getsize(src):
            shutil.copyfile(src, out)
    return dst


def is_loaded() -> bool:
    return _VIENEU_TTS is not None and _LOADED_PRECISION == CFG["vieneu_precision"]


def _load(precision: str) -> Any:
    from vieneu import Vieneu
    t0 = time.perf_counter()
    kwargs: dict = {"mode": "v3turbo", "precision": precision}
    onnx_dir = _local_onnx_dir(precision)
    if onnx_dir:
        kwargs["onnx_dir"] = onnx_dir
    tts = Vieneu(**kwargs)
    backend = getattr(tts, "backend", "?")
    engine = type(getattr(tts, "engine", None)).__name__
    write_log(
        "VIENEU_READY",
        f"Đã nạp sẵn VieNeu-TTS v3 Turbo ({backend}/{engine}, {precision}) vào RAM trong {time.perf_counter() - t0:.2f}s "
        f"(Giọng: {CFG['vieneu_voice']})",
    )
    return tts


def get_vieneu_tts() -> Any:
    """Nạp mô hình VieNeu-TTS v3 Turbo theo độ chính xác đang chọn (nạp lại khi đổi fp32 <-> int8)"""
    global _VIENEU_TTS, _LOADED_PRECISION
    if is_loaded():
        return _VIENEU_TTS
    # Đang nạp model mới ở nền mà đã có model cũ -> dùng tạm model cũ, không bắt lượt nói chờ ~15s
    if not _LOAD_LOCK.acquire(blocking=_VIENEU_TTS is None):
        return _VIENEU_TTS
    try:
        want = CFG["vieneu_precision"]
        if _VIENEU_TTS is not None and _LOADED_PRECISION == want:
            return _VIENEU_TTS
        try:
            tts = _load(want)
        except Exception as e:
            write_log("VIENEU_ERR", f"Không thể tải VieNeu-TTS ({want}): {e}")
            return _VIENEU_TTS  # giữ model cũ (nếu có) để vẫn nói được
        with _ENGINE_LOCK:  # chờ lệnh tổng hợp đang chạy xong rồi mới đổi model
            _VIENEU_TTS, _LOADED_PRECISION = tts, want
    finally:
        _LOAD_LOCK.release()
    return _VIENEU_TTS


def preload_in_background() -> None:
    """Nạp model ở luồng nền (gọi từ trong event loop) để lượt nói kế tiếp không phải chờ ~15-25s"""
    if not is_loaded():
        asyncio.get_running_loop().run_in_executor(None, get_vieneu_tts)


def _to_pcm16(wav: np.ndarray) -> bytes:
    return (np.clip(wav, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def vieneu_synth_pcm(text: str) -> bytes:
    """Tổng hợp tiếng Việt chất lượng cao 48kHz từ VieNeu -> Resample 16kHz PCM Raw"""
    clean = text.strip()
    if not clean:
        return b""
    engine = get_vieneu_tts()
    if engine is None:
        return b""
    try:
        with _ENGINE_LOCK:
            wav_48k = engine.infer(text=clean, voice=CFG["vieneu_voice"])
        if len(wav_48k) == 0:
            return b""
        return _to_pcm16(resample_poly(wav_48k, up=1, down=_DOWN))
    except Exception as e:
        write_log("VIENEU_ERR", f"Lỗi sinh âm thanh VieNeu ({CFG['vieneu_voice']}): {e}")
        return b""


# Lọc chống chồng phổ (thông thấp 7kHz, 97 nhịp) dùng chung cho mọi luồng stream
_AA_TAPS = firwin(97, 7000, fs=VIENEU_SAMPLE_RATE).astype(np.float64)
_AA_ZI = lfilter_zi(_AA_TAPS, [1.0])


class StreamResampler48to16:
    """
    Hạ tần số 48kHz -> 16kHz cho từng mẩu stream mà KHÔNG tạo tiếng "tách" ở chỗ nối:
    giữ trạng thái bộ lọc (zi) và vị trí lấy mẫu (pha) giữa các mẩu, nên ghép các mẩu đầu ra
    giống hệt như lọc cả đoạn liền một lần.
    """

    def __init__(self) -> None:
        self.zi = _AA_ZI * 0.0
        self.phase = 0  # chỉ số mẫu (trong mẩu kế tiếp) sẽ được giữ lại

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        y, self.zi = lfilter(_AA_TAPS, [1.0], np.asarray(x, dtype=np.float64), zi=self.zi)
        out = y[self.phase :: _DOWN]
        self.phase = (self.phase - len(x)) % _DOWN
        return out.astype(np.float32)


def vieneu_stream_pcm(text: str, stop: Optional[threading.Event] = None) -> Iterator[bytes]:
    """
    Sinh PCM 16kHz 16-bit theo từng mẩu ngay khi VieNeu tạo ra (chạy trong luồng riêng, KHÔNG gọi từ event loop).
    Lỗi giữa chừng -> ghi log và dừng (phần đã phát vẫn giữ).
    """
    clean = text.strip()
    if not clean:
        return
    engine = get_vieneu_tts()
    if engine is None:
        return
    rs = StreamResampler48to16()
    try:
        with _ENGINE_LOCK:
            for sub in engine.infer_stream(text=clean, voice=CFG["vieneu_voice"]):
                if stop is not None and stop.is_set():
                    break
                out = rs.process(sub)
                if len(out):
                    yield _to_pcm16(out)
    except Exception as e:
        write_log("VIENEU_ERR", f"Lỗi stream VieNeu ({CFG['vieneu_voice']}): {e}")
