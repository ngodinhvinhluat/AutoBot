import asyncio
from collections import deque
from datetime import datetime
import io
import json
import logging
import os
import re
import time
import wave
from typing import Dict, Any, Optional, List

import numpy as np
from scipy.signal import butter, sosfilt, resample_poly, iirnotch
import speech_recognition as sr
import edge_tts
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

# Nạp biến môi trường từ file .env
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# =====================================================================
# 📝 CẤU HÌNH GHI LOG CHI TIẾT (FILE + TERMINAL + WEB DASHBOARD)
# =====================================================================
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_session.log")
SYSTEM_EVENTS: List[Dict[str, str]] = []
CONVERSATION_LOG: List[Dict[str, str]] = []


def write_log(stage: str, message: str) -> None:
    """Ghi log kèm timestamp ra Terminal, File voice_session.log và Web Dashboard"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{stage}] {message}"
    print(f"\n{line}", flush=True)
    SYSTEM_EVENTS.append({"time": ts, "stage": stage, "message": message})
    if len(SYSTEM_EVENTS) > 60:
        del SYSTEM_EVENTS[:-60]
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =====================================================================
# 🔑 CẤU HÌNH API KEY (BƯỚC 5: ĐÀM THOẠI THEO NGỮ CẢNH)
# =====================================================================
DIRECT_GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-3.5-flash-lite"

try:
    import miniaudio
    HAS_MINIAUDIO = True
except ImportError:
    HAS_MINIAUDIO = False

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

app = FastAPI(title="ESP32 Two-Way Conversational AI Server (Steps 1-5)")

# =====================================================================
# CẤU HÌNH ÂM THANH, LỌC TẠP ÂM DSP & AUTO-VAD (BƯỚC 1, 2, 3, 4)
# =====================================================================
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # 32,000 bytes/s

# Bộ lọc thông dải (Bandpass Filter 300Hz - 3400Hz): Triệt tiêu 100% nhiễu nguồn USB 50/60Hz, tiếng quạt ù và tạp âm tần số cao
VOICE_BANDPASS_SOS = butter(
    N=4,
    Wn=[300.0 / (SAMPLE_RATE / 2.0), 3400.0 / (SAMPLE_RATE / 2.0)],
    btype="bandpass",
    output="sos",
)

# Ngưỡng VAD đo trên tín hiệu ĐÃ LỌC TẠP ÂM (300Hz - 3400Hz)
VAD_START_RMS = 350              # Ngưỡng năng lượng RMS giọng nói để bắt đầu thu
VAD_START_PEAK = 1800            # Ngưỡng biên độ đỉnh giọng nói sau lọc để bắt đầu thu
VAD_START_CONSECUTIVE = 2        # Cần 2 gói liên tiếp (~128ms) vượt ngưỡng để chống nhiễu click chuột/gõ bàn
VAD_CONTINUE_RMS = 200           # Ngưỡng duy trì câu nói khi người dùng hạ giọng giữa các chữ
VAD_SILENCE_SECONDS = 1.1        # Thời gian im lặng (giây) để chốt câu. 0.75s cắt cụt khi người nói ngập ngừng giữa câu
VAD_SILENCE_SECONDS_V1 = 0.75    # Giá trị cũ, dùng để tự nâng cấp settings.json cũ
VAD_MIN_SPEECH_SECONDS = 0.40    # Thời lượng nói thực tế tối thiểu để gửi đi nhận diện

# VAD tự thích nghi theo nền ồn của phòng: ngưỡng cố định ở trên chỉ là MỨC SÀN.
# Nền ồn = phân vị 20% của RMS (đã làm mượt) trong 6s gần nhất, đo LIÊN TỤC kể cả lúc đang nói
# (giữa các chữ luôn có khoảng nghỉ). Phòng ồn (nền ~250) -> ngưỡng tự nâng -> vẫn ngắt câu được.
# Đã mô phỏng trên bản ghi thật: câu "tôi cần chiên cá" tách gọn 3.4s thay vì dính vào 49s nền ồn.
VAD_ADAPTIVE = True
VAD_START_FACTOR = 2.5           # Bắt đầu thu khi RMS gói >= 2.5 x nền ồn
VAD_CONTINUE_FACTOR = 1.6        # Coi là còn nói khi RMS làm mượt >= 1.6 x nền ồn
VAD_FLOOR_WINDOW_CHUNKS = 187    # ~6s lịch sử (gói ESP32 ~32ms)
VAD_FLOOR_PERCENTILE = 20
VAD_SMOOTH_CHUNKS = 8            # Làm mượt RMS ~250ms để 1 gói ồn lẻ không giữ câu kéo dài
VAD_MAX_SPEECH_SECONDS = 12.0    # Cắt cưỡng bức nếu 1 câu kéo dài quá lâu (phòng quá ồn)

# Khử ù điện lưới 50Hz (các hài 100..1000Hz) trước khi gửi STT.
# TẮT: thử trên bản ghi thật, bật notch làm STT tệ hơn ("cuộc sống chiên cá" thay vì "Tôi cần chiên cá").
# Ù tần thấp đã được bộ lọc thông cao 150Hz trên ESP32 xử lý.
HUM_NOTCH_ENABLED = False

# Chẩn đoán Mic: lưu WAV gốc mỗi lượt nói + log tốc độ nhận byte từ thiết bị (chuẩn 32000 B/s)
DEBUG_SAVE_TURN_WAV = True
DEBUG_LOG_RX_RATE = True
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
PCM_CHUNK_SIZE = 2048            # Kích thước gói PCM stream về Client/ESP32 (2KB)

# Phát giọng đọc AI ra loa thiết bị. Tắt (off) = chỉ nhận dạng giọng + trả lời bằng CHỮ
# (hiện trên Dashboard web và gửi "AI:..." xuống ESP32), bỏ qua TTS -> loa im, phản hồi nhanh hơn.
# Ghi đè trong .env (TTS_OUTPUT=on/off) hoặc theo từng kết nối bằng tham số URL: /ws/audio?...&tts=on
TTS_OUTPUT_DEFAULT = os.environ.get("TTS_OUTPUT", "off").strip().lower() != "off"

# Cấu hình bộ tổng hợp giọng đọc TTS (có thể ghi đè trong file .env)
TTS_ENGINE = os.environ.get("TTS_ENGINE", "vieneu").strip().lower()  # "vieneu" hoặc "edge_tts"
TTS_VOICE = os.environ.get("TTS_VOICE", "Ngọc Huyền").strip()
TTS_RATE = os.environ.get("TTS_RATE", "+0%").strip()

# Âm lượng giọng đọc gửi xuống loa: chuẩn hóa từng mệnh đề lên ~95% thang đo rồi tăng thêm
# TTS_BOOST_DB (giới hạn mềm tanh để không vỡ tiếng). 0 = chỉ chuẩn hóa; 6-9 = to hơn rõ; >12 dễ rè.
TTS_BOOST_DB = float(os.environ.get("TTS_BOOST_DB", "6"))


TTS_FADE_SAMPLES = 64  # Fade-in/out 4ms ở 2 đầu mỗi mệnh đề -> chỗ nối giữa các mệnh đề không bị "tạch"


def boost_tts_pcm(pcm_bytes: bytes, ref_peak: float = 0.0) -> tuple[bytes, float, float, float]:
    """
    Chuẩn hóa + tăng âm lượng PCM 16-bit. Trả về (pcm mới, peak gốc %, peak mới %, ref_peak dùng).
    ref_peak: đỉnh tham chiếu của CẢ LƯỢT (truyền lại giá trị trả về cho mệnh đề sau) -> mọi mệnh đề
    trong 1 câu trả lời dùng chung 1 hệ số khuếch đại, không bị to/nhỏ thất thường giữa các mệnh đề.
    """
    x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) == 0:
        return pcm_bytes, 0.0, 0.0, ref_peak
    pk = float(np.max(np.abs(x)))
    if pk < 1e-4:
        return pcm_bytes, pk * 100, pk * 100, ref_peak
    ref = max(ref_peak, pk)  # mệnh đề to hơn tham chiếu -> nâng tham chiếu (không vỡ tiếng)
    y = x / ref  # chuẩn hóa theo đỉnh của cả lượt
    g = 10 ** (float(CFG["tts_boost_db"]) / 20.0)
    if g > 1.0:
        y = np.tanh(y * g) / np.tanh(g)  # nén mềm: tiếng nhỏ to lên, đỉnh vẫn giữ <= 1.0
    y *= 0.95
    n = min(TTS_FADE_SAMPLES, len(y) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        y[:n] *= ramp
        y[-n:] *= ramp[::-1]
    out = (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)
    return out.tobytes(), pk * 100, float(np.max(np.abs(y))) * 100, ref


# Gửi PCM xuống thiết bị THEO NHỊP PHÁT: chỉ đi trước loa tối đa `lead` giây thay vì đổ cả mệnh đề
# (100-170KB) một lần -> bộ đệm nhỏ của ESP32 không bị tràn và rơi gói (nguyên nhân chính gây giật tiếng).
# Thiết bị báo dung lượng bộ đệm qua tham số URL ?buf=<bytes>; không báo -> giả định bộ đệm cũ 32KB (~1s).
TTS_SEND_LEAD_DEFAULT = 0.6      # giây, cho thiết bị không báo buf (firmware cũ / PC client)
TTS_SEND_LEAD_MAX = 3.0          # giây, đủ che giật mạng WiFi mà vẫn kết thúc lượt nhanh
TTS_SEND_LEAD_BUFFER_RATIO = 0.75  # chỉ dùng tối đa 75% bộ đệm của thiết bị


def send_lead_for_buffer(buf_bytes: int) -> float:
    if buf_bytes <= 0:
        return TTS_SEND_LEAD_DEFAULT
    return max(0.3, min(TTS_SEND_LEAD_MAX, TTS_SEND_LEAD_BUFFER_RATIO * buf_bytes / BYTES_PER_SECOND))


# Ước lượng tốc độ bộ tổng hợp (cập nhật liên tục từ số đo thật, giữ qua các lượt).
# VieNeu trên CPU (i7-7700) đo được RTF ~0.7-1.1 = gần sát thời gian thực -> mệnh đề sau có thể chưa xong khi loa phát hết.
_TTS_EST: Dict[str, float] = {"rtf": 0.85, "sec_per_char": 0.06}


def update_tts_estimates(text: str, synth_seconds: float, audio_seconds: float) -> None:
    if audio_seconds > 0.3 and text:
        _TTS_EST["rtf"] = 0.7 * _TTS_EST["rtf"] + 0.3 * (synth_seconds / audio_seconds)
        _TTS_EST["sec_per_char"] = 0.8 * _TTS_EST["sec_per_char"] + 0.2 * (audio_seconds / len(text))


def estimate_prebuffer_seconds(first_audio_seconds: float, remaining_texts: List[str]) -> float:
    """
    Cần chờ thêm bao lâu trước khi phát mệnh đề đầu để các mệnh đề sau (tổng hợp nối tiếp) luôn xong
    TRƯỚC khi loa phát hết phần đã có -> cả câu trả lời phát liền mạch, không bị ngắt quãng giữa chừng.
    """
    rtf = _TTS_EST["rtf"] * 1.1  # biên an toàn 10%
    spc = _TTS_EST["sec_per_char"]
    need, synth_acc, audio_acc = 0.0, 0.0, first_audio_seconds
    for t in remaining_texts:
        a = spc * len(t)
        synth_acc += rtf * a
        need = max(need, synth_acc - audio_acc)
        audio_acc += a
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


_VIENEU_TTS: Optional[Any] = None

def get_vieneu_tts() -> Any:
    """Tải một lần duy nhất mô hình VieNeu-TTS v3 Turbo vào RAM"""
    global _VIENEU_TTS
    if _VIENEU_TTS is None:
        try:
            from vieneu import Vieneu
            t0 = time.perf_counter()
            _VIENEU_TTS = Vieneu()
            dt = time.perf_counter() - t0
            write_log("VIENEU_READY", f"Đã nạp sẵn VieNeu-TTS v3 Turbo vào RAM trong {dt:.2f}s (Giọng mặc định: {TTS_VOICE})")
        except Exception as e:
            write_log("VIENEU_ERR", f"Không thể tải VieNeu-TTS: {e}")
            _VIENEU_TTS = None
    return _VIENEU_TTS


def vieneu_synth_pcm(text: str) -> bytes:
    """Tổng hợp tiếng Việt chất lượng cao 48kHz từ VieNeu -> Resample 16kHz PCM Raw"""
    clean = text.strip()
    if not clean:
        return b""
    engine = get_vieneu_tts()
    if engine is None:
        return b""
    try:
        wav_48k = engine.infer(text=clean, voice=CFG["vieneu_voice"])
        if len(wav_48k) == 0:
            return b""
        wav_16k = resample_poly(wav_48k, up=1, down=3)
        pcm_16k = (np.clip(wav_16k, -1.0, 1.0) * 32767).astype(np.int16)
        return pcm_16k.tobytes()
    except Exception as e:
        write_log("VIENEU_ERR", f"Lỗi sinh âm thanh VieNeu ({CFG['vieneu_voice']}): {e}")
        return b""


# Lời nhắc cũ (ép 1-2 câu -> câu trả lời qua loa, câu nào cũng hỏi lại). Giữ lại để tự nâng cấp settings.json cũ.
SYSTEM_INSTRUCTION_V1 = (
    "Bạn là một trợ lý giọng nói AI thông minh, thân thiện, đang trò chuyện trực tiếp "
    "với người dùng qua tai nghe. Hãy luôn ghi nhớ ngữ cảnh cuộc trò chuyện trước đó, "
    "trả lời bằng tiếng Việt tự nhiên, ngắn gọn, súc tích (khoảng 1 đến 2 câu), "
    "không dùng ký tự markdown như dấu sao (*) hay bảng biểu để giọng đọc TTS mượt mà nhất."
)

SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý giọng nói AI thông minh, thân thiện, đang trò chuyện trực tiếp với người dùng qua loa. "
    "Hãy ghi nhớ ngữ cảnh cuộc trò chuyện và trả lời bằng tiếng Việt tự nhiên như đang nói chuyện.\n"
    "- Đi thẳng vào nội dung, đưa thông tin cụ thể và hữu ích (thường 2 đến 4 câu). Khi người dùng nhờ kể chuyện, "
    "hướng dẫn từng bước hay giải thích thì được nói dài hơn nhưng không quá 8 câu.\n"
    "- Không chào hỏi thừa, không lặp lại câu hỏi, không kết thúc mọi câu trả lời bằng một câu hỏi lại; "
    "chỉ hỏi lại khi thật sự thiếu thông tin để trả lời.\n"
    "- Lời người dùng được nhận dạng từ giọng nói nên có thể sai chữ, thiếu chữ hoặc bị cắt cụt: hãy đoán ý hợp lý "
    "nhất dựa vào ngữ cảnh; chỉ khi hoàn toàn không hiểu mới nói ngắn gọn là chưa nghe rõ.\n"
    "- Câu trả lời được đọc thành tiếng: chỉ dùng văn xuôi, không markdown, không gạch đầu dòng, không emoji hay ký hiệu "
    "đặc biệt; viết số, đơn vị theo cách đọc tự nhiên; ngắt câu rõ ràng bằng dấu chấm và dấu phẩy."
)


# =====================================================================
# ⚙️ CÀI ĐẶT CHỈNH TRỰC TIẾP TRÊN DASHBOARD (áp dụng ngay, lưu vào settings.json)
# Giá trị mặc định lấy từ các hằng số / .env ở trên; settings.json (nếu có) ghi đè.
# =====================================================================
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def _vieneu_voice_options() -> List[List[str]]:
    """Đọc danh sách giọng VieNeu từ file preset của thư viện (không cần nạp model)"""
    try:
        import importlib.util
        spec = importlib.util.find_spec("vieneu")
        path = os.path.join(os.path.dirname(spec.origin), "assets", "voices_v3_turbo.json")
        with open(path, encoding="utf-8") as f:
            presets = json.load(f).get("presets", {})
        opts = [[name, f"{name} — {p.get('description', '')}"] for name, p in presets.items()]
    except Exception:
        opts = []
    if TTS_VOICE not in [o[0] for o in opts]:
        opts.insert(0, [TTS_VOICE, TTS_VOICE])
    return opts


_G_TTS = "🔊 Loa & giọng đọc"
_G_AI = "🤖 Trí tuệ nhân tạo (Gemini)"
_G_VAD = "🎙️ Nghe & ngắt câu (VAD / STT)"
_G_DBG = "🩺 Chẩn đoán"

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {"group": _G_TTS, "key": "tts_output", "type": "bool", "default": TTS_OUTPUT_DEFAULT,
     "label": "Phát giọng đọc ra loa",
     "help": "Tắt = chỉ trả lời bằng chữ (Dashboard + OLED), phản hồi nhanh hơn ~4s."},
    {"group": _G_TTS, "key": "tts_engine", "type": "select", "default": TTS_ENGINE if TTS_ENGINE in ("vieneu", "edge_tts") else "vieneu",
     "label": "Bộ tổng hợp giọng",
     "options": [["vieneu", "VieNeu (chạy trên máy, giọng tự nhiên)"], ["edge_tts", "Edge-TTS (Microsoft, cần Internet)"]]},
    {"group": _G_TTS, "key": "vieneu_voice", "type": "select", "default": TTS_VOICE,
     "label": "Giọng VieNeu", "options": _vieneu_voice_options()},
    {"group": _G_TTS, "key": "edge_voice", "type": "select", "default": "vi-VN-HoaiMyNeural",
     "label": "Giọng Edge-TTS",
     "options": [["vi-VN-HoaiMyNeural", "Hoài My — Nữ"], ["vi-VN-NamMinhNeural", "Nam Minh — Nam"]]},
    {"group": _G_TTS, "key": "tts_rate", "type": "select", "default": TTS_RATE if TTS_RATE in ("-20%", "-10%", "+0%", "+10%", "+20%", "+30%") else "+0%",
     "label": "Tốc độ đọc (Edge-TTS)",
     "options": [["-20%", "Chậm (-20%)"], ["-10%", "Hơi chậm (-10%)"], ["+0%", "Bình thường"], ["+10%", "Hơi nhanh (+10%)"], ["+20%", "Nhanh (+20%)"], ["+30%", "Rất nhanh (+30%)"]]},
    {"group": _G_TTS, "key": "tts_max_prebuffer", "type": "range", "default": 2.0, "min": 0, "max": 5, "step": 0.5, "unit": "s",
     "label": "Chờ tối đa trước khi phát (chống ngắt quãng)",
     "help": "VieNeu trên CPU gần sát thời gian thực: khi câu trả lời dài, server tự ước lượng và chờ thêm "
             "(không quá mức này) để phát liền mạch. 0 = phát ngay, có thể ngắt quãng giữa các câu."},
    {"group": _G_TTS, "key": "tts_boost_db", "type": "range", "default": TTS_BOOST_DB, "min": 0, "max": 15, "step": 1, "unit": "dB",
     "label": "Tăng âm lượng",
     "help": "Chuẩn hóa từng câu lên ~95% rồi tăng thêm (nén mềm). 6-9dB: to rõ; >12dB: dễ rè."},

    {"group": _G_AI, "key": "gemini_model", "type": "text", "default": GEMINI_MODEL,
     "label": "Model Gemini"},
    {"group": _G_AI, "key": "gemini_thinking", "type": "select", "default": "low",
     "label": "Mức suy nghĩ của AI",
     "options": [["minimal", "Tối thiểu (nhanh nhất, nông nhất)"], ["low", "Thấp (khuyến nghị)"],
                 ["medium", "Vừa (chậm hơn)"], ["high", "Cao (chậm nhất)"]],
     "help": "Đo thực tế với flash-lite: minimal và low đều có chữ đầu sau ~0.5-1s."},
    {"group": _G_AI, "key": "history_turns", "type": "range", "default": 20, "min": 2, "max": 60, "step": 1, "unit": "lượt",
     "label": "Số lượt hội thoại AI ghi nhớ",
     "help": "Ngữ cảnh gắn theo thiết bị: ESP32 mất kết nối rồi vào lại vẫn nhớ. Không nói gì 30 phút thì bắt đầu cuộc trò chuyện mới."},
    {"group": _G_AI, "key": "system_instruction", "type": "textarea", "default": SYSTEM_INSTRUCTION,
     "label": "Lời nhắc hệ thống (tính cách & cách trả lời)",
     "help": "Áp dụng từ lượt nói tiếp theo. Nên giữ yêu cầu trả lời ngắn để giọng đọc nhanh."},

    {"group": _G_VAD, "key": "stt_language", "type": "select", "default": "vi-VN",
     "label": "Ngôn ngữ nhận dạng giọng nói",
     "options": [["vi-VN", "Tiếng Việt"], ["en-US", "English (US)"]]},
    {"group": _G_VAD, "key": "vad_adaptive", "type": "bool", "default": VAD_ADAPTIVE,
     "label": "Tự thích nghi theo nền ồn phòng",
     "help": "Bật: ngưỡng tự nâng khi phòng ồn để vẫn ngắt được câu. Tắt: dùng ngưỡng cố định bên dưới."},
    {"group": _G_VAD, "key": "vad_start_factor", "type": "range", "default": VAD_START_FACTOR, "min": 1.5, "max": 5.0, "step": 0.1, "unit": "× nền",
     "label": "Độ to để bắt đầu thu", "help": "Cao hơn = ít bị tiếng ồn kích hoạt nhầm, nhưng phải nói to/gần hơn."},
    {"group": _G_VAD, "key": "vad_continue_factor", "type": "range", "default": VAD_CONTINUE_FACTOR, "min": 1.1, "max": 3.0, "step": 0.1, "unit": "× nền",
     "label": "Độ to coi là vẫn đang nói", "help": "Cao hơn = ngắt câu sớm hơn khi phòng ồn."},
    {"group": _G_VAD, "key": "vad_silence_seconds", "type": "range", "default": VAD_SILENCE_SECONDS, "min": 0.3, "max": 2.0, "step": 0.05, "unit": "s",
     "label": "Im lặng bao lâu thì chốt câu", "help": "Thấp = phản hồi nhanh nhưng dễ cắt ngang khi ngập ngừng."},
    {"group": _G_VAD, "key": "vad_max_speech_seconds", "type": "range", "default": VAD_MAX_SPEECH_SECONDS, "min": 4, "max": 30, "step": 1, "unit": "s",
     "label": "Độ dài tối đa 1 câu"},
    {"group": _G_VAD, "key": "vad_min_speech_seconds", "type": "range", "default": VAD_MIN_SPEECH_SECONDS, "min": 0.2, "max": 1.5, "step": 0.05, "unit": "s",
     "label": "Độ dài tối thiểu để gửi nhận dạng", "help": "Bỏ qua tiếng động quá ngắn (gõ bàn, ho...)."},
    {"group": _G_VAD, "key": "vad_start_rms", "type": "range", "default": VAD_START_RMS, "min": 100, "max": 2000, "step": 10,
     "label": "Ngưỡng bắt đầu thu (mức sàn, RMS)"},
    {"group": _G_VAD, "key": "vad_continue_rms", "type": "range", "default": VAD_CONTINUE_RMS, "min": 50, "max": 1500, "step": 10,
     "label": "Ngưỡng duy trì câu (mức sàn, RMS)"},
    {"group": _G_VAD, "key": "vad_start_peak", "type": "range", "default": VAD_START_PEAK, "min": 300, "max": 10000, "step": 100,
     "label": "Biên độ đỉnh tối thiểu để bắt đầu thu"},
    {"group": _G_VAD, "key": "hum_notch", "type": "bool", "default": HUM_NOTCH_ENABLED,
     "label": "Khử ù điện lưới 50Hz trước khi nhận dạng",
     "help": "Thử trên bản ghi thật: bật làm nhận dạng KÉM hơn. Chỉ bật để so sánh."},

    {"group": _G_DBG, "key": "debug_save_wav", "type": "bool", "default": DEBUG_SAVE_TURN_WAV,
     "label": "Lưu WAV gốc mỗi lượt nói (thư mục recordings/)"},
    {"group": _G_DBG, "key": "debug_log_rx_rate", "type": "bool", "default": DEBUG_LOG_RX_RATE,
     "label": "Ghi log tốc độ nhận âm thanh mỗi 10s"},
]
_SETTINGS_INDEX: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SETTINGS_SCHEMA}
CFG: Dict[str, Any] = {s["key"]: s["default"] for s in SETTINGS_SCHEMA}


def _coerce_setting(spec: Dict[str, Any], value: Any) -> Any:
    """Ép kiểu + kiểm tra hợp lệ 1 giá trị cài đặt; lỗi -> ValueError"""
    t = spec["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if t == "range":
        v = float(value)
        v = min(max(v, spec["min"]), spec["max"])
        return int(round(v)) if float(spec["step"]).is_integer() and float(spec["min"]).is_integer() else round(v, 3)
    if t == "select":
        allowed = [o[0] for o in spec["options"]]
        if value not in allowed:
            raise ValueError(f"'{value}' không nằm trong danh sách cho phép")
        return value
    v = str(value).strip()
    if not v:
        raise ValueError("không được để trống")
    return v


SETTINGS_VERSION = 2


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({**CFG, "_version": SETTINGS_VERSION}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log("SETTINGS", f"Lỗi lưu {SETTINGS_FILE}: {e}")


def load_settings() -> None:
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        migrated: List[str] = []
        if int(saved.get("_version", 1)) < 2:
            # v1 -> v2: giá trị cũ còn đúng bằng mặc định cũ thì chuyển sang mặc định mới (giá trị đã tự chỉnh thì giữ)
            if saved.get("system_instruction") == SYSTEM_INSTRUCTION_V1:
                saved.pop("system_instruction")
                migrated.append("system_instruction")
            if saved.get("vad_silence_seconds") == VAD_SILENCE_SECONDS_V1:
                saved.pop("vad_silence_seconds")
                migrated.append("vad_silence_seconds")
        for k, v in saved.items():
            if k in _SETTINGS_INDEX:
                try:
                    CFG[k] = _coerce_setting(_SETTINGS_INDEX[k], v)
                except Exception:
                    pass
        if int(saved.get("_version", 1)) < SETTINGS_VERSION:
            save_settings()
            write_log("SETTINGS", f"Đã nâng cấp {SETTINGS_FILE} lên v{SETTINGS_VERSION} (đổi sang mặc định mới: {migrated or 'không'})")
    except Exception as e:
        write_log("SETTINGS", f"Lỗi đọc {SETTINGS_FILE}: {e} -> dùng mặc định")


def update_settings(changes: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Áp dụng thay đổi từ Dashboard. Trả về (các giá trị đã đổi, lỗi theo key)"""
    applied: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for k, v in changes.items():
        spec = _SETTINGS_INDEX.get(k)
        if spec is None:
            errors[k] = "không có cài đặt này"
            continue
        try:
            nv = _coerce_setting(spec, v)
        except Exception as e:
            errors[k] = str(e)
            continue
        if CFG.get(k) != nv:
            CFG[k] = nv
            applied[k] = nv
    if applied:
        save_settings()
        shown = {k: (v if not isinstance(v, str) or len(v) <= 40 else v[:40] + "...") for k, v in applied.items()}
        write_log("SETTINGS", f"⚙️ Đã cập nhật: {shown}")
    return applied, errors


load_settings()

# Thiết bị đang kết nối (trạng thái hiển thị trên Dashboard) + "thế hệ" ngữ cảnh hội thoại
ACTIVE_DEVICES: Dict[int, Dict[str, Any]] = {}
CONTEXT_EPOCH = 0

# Ngữ cảnh hội thoại gắn theo THIẾT BỊ (không theo từng kết nối WebSocket) -> ESP32 rớt WiFi/kết nối lại vẫn nhớ.
# Khóa = tham số URL ?dev=<MAC> (firmware mới) hoặc IP thiết bị. Im lặng quá lâu -> bắt đầu cuộc trò chuyện mới.
DEVICE_SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_IDLE_RESET_SECONDS = 30 * 60


def new_session_state() -> Dict[str, Any]:
    return {"gemini_contents": [], "history": [], "turn_count": 0, "epoch": CONTEXT_EPOCH, "last_active": time.time()}


def get_device_session(device_key: str) -> tuple[Dict[str, Any], bool]:
    """Trả về (session_state, có_khôi_phục_ngữ_cảnh_cũ)"""
    s = DEVICE_SESSIONS.get(device_key)
    if s is not None and time.time() - s.get("last_active", 0) <= SESSION_IDLE_RESET_SECONDS:
        return s, s.get("turn_count", 0) > 0
    s = new_session_state()
    DEVICE_SESSIONS[device_key] = s
    return s, False

# Giữ kết nối Persistent Client Pool tới Gemini để không mất thời gian bắt tay lại mỗi lượt
_GEMINI_CLIENT: Optional[Any] = None
_GEMINI_CLIENT_KEY: str = ""
_EDGE_TTS_WORKING: bool = True  # Tự động tắt thử Edge-TTS nếu bị lỗi lệch giờ hệ thống để không tốn 2s chờ mỗi câu


def get_active_api_key() -> str:
    """Lấy GEMINI_API_KEY từ biến trực tiếp hoặc đọc nóng từ file .env"""
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass
    key = (DIRECT_GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")).strip()
    if key and key != "your_gemini_api_key_here" and len(key) >= 20:
        return key
    return ""


def get_gemini_client(api_key: str) -> Any:
    """Khởi tạo hoặc tái sử dụng Gemini Client để tối ưu tốc độ phản hồi"""
    global _GEMINI_CLIENT, _GEMINI_CLIENT_KEY
    if _GEMINI_CLIENT is None or _GEMINI_CLIENT_KEY != api_key:
        from google.genai import types
        _GEMINI_CLIENT = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=15000),
        )
        _GEMINI_CLIENT_KEY = api_key
    return _GEMINI_CLIENT


# =====================================================================
# CÁC HÀM LỌC ÂM DSP & XỬ LÝ ÂM THANH (BƯỚC 1, 3, 4)
# =====================================================================
def filter_voice_bandpass(samples: np.ndarray) -> np.ndarray:
    """Lọc thông dải 300Hz - 3400Hz (dải tần giọng nói con người)"""
    if len(samples) == 0:
        return samples
    filtered = sosfilt(VOICE_BANDPASS_SOS, samples.astype(np.float32))
    return np.clip(filtered, -32767, 32767).astype(np.int16)


def _build_hum_notch_sos() -> np.ndarray:
    """Chuỗi bộ lọc rãnh (notch) tại các hài điện lưới 100..1000Hz, mỗi rãnh rộng ~4Hz (không ăn vào giọng nói)"""
    sections = []
    for f0 in range(100, 1001, 50):
        b, a = iirnotch(f0, Q=f0 / 4.0, fs=SAMPLE_RATE)
        sections.append(np.concatenate([b, a]))
    return np.array(sections)


HUM_NOTCH_SOS = _build_hum_notch_sos()


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


def pcm_to_wav_bytes(pcm_data: bytes) -> bytes:
    """Đóng gói Raw PCM bytes (đã qua bộ lọc DSP + AGC) thành định dạng WAV trên RAM"""
    cleaned_pcm = clean_and_normalize_pcm(pcm_data)
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(cleaned_pcm)
    return wav_io.getvalue()


def speech_to_text(wav_bytes: bytes) -> str:
    """Chuyển đổi WAV bytes thành văn bản tiếng Việt"""
    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
        audio_data = recognizer.record(source)
        try:
            return recognizer.recognize_google(audio_data, language=CFG["stt_language"])
        except sr.UnknownValueError:
            return ""
        except Exception as e:
            write_log("STT_ERROR", f"Lỗi nhận diện giọng nói: {e}")
            return ""


async def text_to_speech_bytes(text: str) -> tuple[bytes, str]:
    """
    Chuyển đổi 1 cụm câu ngắn thành MP3 bytes.
    Nếu Edge-TTS bị lỗi (do lệch giờ hệ thống), tự động nhớ trạng thái và gọi thẳng Google-TTS (không tốn 2s chờ timeout).
    """
    global _EDGE_TTS_WORKING
    clean_text = text.strip()
    if not clean_text:
        return b"", "empty"

    edge_voice = CFG["edge_voice"]
    edge_rate = CFG["tts_rate"]
    if _EDGE_TTS_WORKING:
        try:
            communicate = edge_tts.Communicate(clean_text, voice=edge_voice, rate=edge_rate)
            mp3_io = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_io.write(chunk["data"])
            data = mp3_io.getvalue()
            if data:
                return data, f"Edge-TTS ({edge_voice} {edge_rate})"
        except Exception as e:
            if edge_rate != "+0%":
                try:
                    communicate = edge_tts.Communicate(clean_text, voice=edge_voice, rate="+0%")
                    mp3_io = io.BytesIO()
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            mp3_io.write(chunk["data"])
                    data = mp3_io.getvalue()
                    if data:
                        return data, f"Edge-TTS ({edge_voice})"
                except Exception:
                    pass
            write_log("TTS_WARN", f"Edge-TTS tạm lỗi: {e}, chuyển dự phòng Google-TTS")

    from gtts import gTTS

    def _gtts_synth() -> bytes:
        buf = io.BytesIO()
        gTTS(text=clean_text, lang="vi").write_to_fp(buf)
        return buf.getvalue()

    data = await asyncio.to_thread(_gtts_synth)
    return data, "Google-TTS"


def mp3_to_pcm_bytes(mp3_bytes: bytes) -> bytes:
    """[BƯỚC 4] Giải mã MP3 sang Raw PCM (16kHz, 16-bit, Mono)"""
    if not mp3_bytes:
        return b""
    if not HAS_MINIAUDIO:
        raise RuntimeError("Chưa cài đặt thư viện 'miniaudio' để chuyển đổi MP3 sang PCM.")
    decoded = miniaudio.decode(
        mp3_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=CHANNELS,
        sample_rate=SAMPLE_RATE,
    )
    return bytes(decoded.samples)


async def synth_clause_pcm(text: str) -> tuple[bytes, str]:
    """Tổng hợp 1 mệnh đề thành PCM 16kHz theo bộ tổng hợp đang chọn (VieNeu lỗi -> dự phòng Edge/gTTS)"""
    if CFG["tts_engine"] == "vieneu":
        pcm = await asyncio.to_thread(vieneu_synth_pcm, text)
        if pcm:
            return pcm, f"VieNeu ({CFG['vieneu_voice']})"
    mp3_bytes, engine_name = await text_to_speech_bytes(text)
    if not mp3_bytes:
        return b"", engine_name
    return await asyncio.to_thread(mp3_to_pcm_bytes, mp3_bytes), engine_name


# =====================================================================
# CHIA NHỎ CÂU (CLAUSE CHUNKING) & STREAMING AI -> STREAMING TTS (GIAI ĐOẠN 4 & 5)
# =====================================================================
CLAUSE_SPLIT_PATTERN = re.compile(r"([.!?;\n]+|,\s+)")


def split_into_speakable_clauses(text: str, min_chars: int = 16) -> List[str]:
    """Tách văn bản thành các mệnh đề ngắn theo dấu câu để đọc cuốn chiếu từng phần"""
    parts = CLAUSE_SPLIT_PATTERN.split(text)
    clauses: List[str] = []
    current = ""
    for part in parts:
        current += part
        if CLAUSE_SPLIT_PATTERN.fullmatch(part) and len(current.strip()) >= min_chars:
            clauses.append(current.strip())
            current = ""
    if current.strip():
        if clauses and len(current.strip()) < 8:
            clauses[-1] = f"{clauses[-1]} {current.strip()}"
        else:
            clauses.append(current.strip())
    return [c for c in clauses if c]


def extract_ready_clause(buffer: str, first: bool = False, max_chars: int = 0) -> tuple[Optional[str], str]:
    """
    Kiểm tra bộ đệm văn bản đang stream từ Gemini:
    - Gặp dấu kết câu (. ! ? ; \\n) và độ dài >= 10 ký tự -> tách ngay để đọc luôn.
    - Gặp dấu phẩy (, ) và độ dài >= 22 ký tự -> tách ngay mệnh đề đầu để đọc trước.
    - first=True (mệnh đề ĐẦU của câu trả lời): ngưỡng thấp hơn (6 / 12 ký tự) để mệnh đề đầu ngắn,
      VieNeu tổng hợp nhanh -> loa cất tiếng sớm; các mệnh đề sau tổng hợp song song trong lúc loa đang phát.
    - max_chars > 0: câu dài hơn mức này thì ưu tiên cắt ở dấu phẩy (gần max_chars nhất) để mệnh đề kế tiếp
      tổng hợp xong trước khi loa phát hết phần đã có (tránh khoảng lặng giữa câu). Không có dấu phẩy thì giữ cả câu.
    """
    min_strong, min_comma = (6, 12) if first else (10, 22)
    # Dấu ngắt câu mạnh đầu tiên đạt ngưỡng độ dài
    strong_end = next((m.end() for m in re.finditer(r"[.!?;\n]+\s*", buffer) if m.end() >= min_strong), None)
    if strong_end is not None and (not max_chars or strong_end <= max_chars):
        return buffer[:strong_end].strip(), buffer[strong_end:]

    # Câu chưa kết thúc (hoặc quá dài) -> cắt ở dấu phẩy
    commas = [m.end() for m in re.finditer(r",\s+", buffer)
              if m.end() >= min_comma and (strong_end is None or m.end() < strong_end)]
    if max_chars:
        within = [c for c in commas if c <= max_chars]
        cut = within[-1] if within else (commas[0] if commas else None)
    else:
        cut = commas[0] if commas else None
    if cut is not None:
        return buffer[:cut].strip(), buffer[cut:]
    if strong_end is not None:
        return buffer[:strong_end].strip(), buffer[strong_end:]
    return None, buffer


def local_contextual_fallback(user_text: str, session_state: Dict[str, Any]) -> str:
    """Bộ hội thoại theo ngữ cảnh nội bộ (chỉ dùng khi không có kết nối Gemini API)"""
    history: List[Dict[str, str]] = session_state.setdefault("history", [])
    user_name: Optional[str] = session_state.get("user_name")
    text_lower = user_text.lower()

    if ("tên gì" in text_lower or "tên là gì" in text_lower) and (
        "tôi" in text_lower or "mình" in text_lower or "nhớ" in text_lower or "anh" in text_lower
    ):
        if user_name:
            reply = f"Bạn vừa giới thiệu tên bạn là {user_name} mà, mình vẫn nhớ rất rõ!"
        else:
            reply = "Bạn chưa giới thiệu tên cho mình biết. Bạn tên là gì nhỉ?"
        history.append({"user": user_text, "ai": reply})
        return reply

    for marker in ["tên tôi là ", "tôi tên là ", "mình tên là ", "anh tên là ", "em tên là "]:
        if marker in text_lower and "gì" not in text_lower and "không" not in text_lower:
            idx = text_lower.find(marker) + len(marker)
            extracted = user_text[idx:].strip().split()[0].strip(",.!?")
            if extracted:
                user_name = extracted.capitalize()
                session_state["user_name"] = user_name
                reply = f"Chào {user_name} nhé! Rất vui được làm quen với {user_name}. Hôm nay bạn muốn trò chuyện về chủ đề gì?"
                history.append({"user": user_text, "ai": reply})
                return reply

    if "làm được gì" in text_lower or "có thể làm gì" in text_lower or "bạn là ai" in text_lower:
        prefix = f"{user_name} ơi, " if user_name else ""
        reply = (
            f"{prefix}mình là trợ lý giọng nói hai chiều của bạn. Mình có thể lắng nghe bạn nói qua Mic, "
            f"ghi nhớ tên và nội dung cuộc trò chuyện của chúng mình, rồi trả lời lại qua tai nghe."
        )
    elif "xin chào" in text_lower or "chào bạn" in text_lower or "alo" in text_lower or "nghe rõ" in text_lower:
        greeting_name = f" {user_name}" if user_name else ""
        reply = f"Chào{greeting_name}! Mình nghe bạn rất rõ qua tai nghe rồi, bạn nói tiếp đi nhé."
    else:
        turn_count = len(history) + 1
        prefix = f"{user_name} ơi, " if user_name else ""
        reply = f"{prefix}mình đã nghe rõ câu thứ {turn_count} của bạn: {user_text}."

    history.append({"user": user_text, "ai": reply})
    return reply


async def stream_ai_and_tts_clauses(
    websocket: WebSocket,
    user_text: str,
    session_state: Dict[str, Any],
    output_format: str,
    turn_start_time: float,
    tts_enabled: bool = True,
    send_lead: float = TTS_SEND_LEAD_DEFAULT,
) -> None:
    """
    [GIAI ĐOẠN 4 & 5] Pipeline cuốn chiếu 3 tầng chạy song song:
    1. Gemini `generate_content_stream` -> cắt mệnh đề ngay khi có chữ -> clause_queue.
    2. Tổng hợp giọng (VieNeu/Edge) lần lượt từng mệnh đề -> audio_queue. Tầng này KHÔNG chờ gửi,
       nên mệnh đề sau được tổng hợp trong lúc loa còn đang phát mệnh đề trước.
    3. Gửi PCM xuống thiết bị THEO NHỊP PHÁT (chỉ đi trước loa `send_lead` giây) để bộ đệm ESP32 không tràn.
    Ngữ cảnh hội thoại tự giữ trong session_state["gemini_contents"] (gắn theo thiết bị).
    """
    turn_no = session_state.get("turn_count", 0) + 1
    session_state["turn_count"] = turn_no
    session_state["last_active"] = time.time()

    write_log("USER_SPEECH", f"Lượt #{turn_no} | Bạn nói: \"{user_text}\"")

    # Tạo bản ghi hội thoại trên Dashboard ở trạng thái đang phát trực tiếp
    conv_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "turn": str(turn_no),
        "user": user_text,
        "ai": "⏳ Đang phản hồi...",
        "latency": "...",
    }
    CONVERSATION_LOG.append(conv_entry)

    clause_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    audio_queue: asyncio.Queue[Optional[tuple[int, str, bytes, str, float]]] = asyncio.Queue()
    full_reply_parts: List[str] = []
    planned_clauses: List[str] = []  # mọi mệnh đề đã cắt từ Gemini (theo thứ tự) -> ước lượng thời gian tổng hợp còn lại

    async def tts_worker() -> None:
        """Tầng 2: lấy mệnh đề -> tổng hợp PCM -> đẩy sang tầng gửi (không chờ gửi xong)"""
        seq = 0
        while True:
            clause = await clause_queue.get()
            if clause is None:
                await audio_queue.put(None)
                break
            seq += 1
            if not tts_enabled:
                await audio_queue.put((seq, clause, b"", "", 0.0))
                continue
            t_c0 = time.perf_counter()
            pcm_bytes, engine_name = await synth_clause_pcm(clause)
            if pcm_bytes:
                await audio_queue.put((seq, clause, pcm_bytes, engine_name, time.perf_counter() - t_c0))

    async def sender() -> None:
        """Tầng 3: gửi chữ + PCM của từng mệnh đề xuống thiết bị theo nhịp phát"""
        paced = PacedPcmSender(websocket, send_lead)
        ref_peak = 0.0
        clause_idx = 0
        first_out = False
        while True:
            item = await audio_queue.get()
            if item is None:
                break
            seq, clause, pcm_bytes, engine_name, tts_dt = item
            clause_idx += 1
            full_reply_parts.append(clause)
            conv_entry["ai"] = " ".join(full_reply_parts)

            if not pcm_bytes:
                # Chế độ chỉ chữ: không tổng hợp giọng, đẩy ngay mệnh đề lên Dashboard + xuống thiết bị
                if not first_out:
                    first_out = True
                    ttft = time.perf_counter() - turn_start_time
                    conv_entry["latency"] = f"Chữ đầu {ttft:.2f}s"
                    write_log("FIRST_TEXT_OUT", f"📝 Trả lời bằng chữ sau {ttft:.2f}s: \"{clause}\"")
                else:
                    write_log("TEXT_OUT", f"📝 Mệnh đề #{clause_idx}: \"{clause}\"")
                await websocket.send_text(f"AI:{clause}")
                continue

            if output_format != "pcm":
                await websocket.send_text(f"AI:{clause}")
                await websocket.send_bytes(pcm_bytes)
                continue

            pcm_bytes, pk_in, pk_out, ref_peak = boost_tts_pcm(pcm_bytes, ref_peak)
            c_dur = len(pcm_bytes) / BYTES_PER_SECOND
            update_tts_estimates(clause, tts_dt, c_dur)
            if not first_out:
                first_out = True
                # Tổng hợp chậm sát thời gian thực -> chờ thêm một chút để cả câu trả lời phát liền mạch
                wait = min(float(CFG["tts_max_prebuffer"]), estimate_prebuffer_seconds(c_dur, planned_clauses[seq:]))
                if wait > 0.05:
                    await asyncio.sleep(wait)
                ttfa = time.perf_counter() - turn_start_time
                conv_entry["latency"] = f"TTFA {ttfa:.2f}s"
                wait_note = f", chờ thêm {wait:.2f}s cho {len(planned_clauses) - seq} mệnh đề sau" if wait > 0.05 else ""
                write_log(
                    "FIRST_AUDIO_OUT",
                    f"⚡ Bắt đầu phát Chunk #1 sau {ttfa:.2f}s ({engine_name}: {tts_dt:.2f}s, Dài: {c_dur:.2f}s{wait_note}, "
                    f"Âm lượng đỉnh {pk_in:.0f}% -> {pk_out:.0f}% (+{float(CFG['tts_boost_db']):.0f}dB nén mềm), "
                    f"gửi trước loa tối đa {send_lead:.1f}s, RTF ước lượng {_TTS_EST['rtf']:.2f}): \"{clause}\"",
                )
            else:
                gap = paced.starving_seconds()
                gap_note = f" | ⚠️ loa đã chờ {gap:.2f}s (tổng hợp không kịp)" if gap > 0.05 else ""
                write_log(
                    "TTS_CHUNK_OUT",
                    f"🔊 Gửi nối tiếp Chunk #{clause_idx} ({engine_name}: {tts_dt:.2f}s, Dài: {c_dur:.2f}s{gap_note}): \"{clause}\"",
                )

            # Gửi chữ của mệnh đề ngay trước PCM để màn hình OLED trên ESP32 hiển thị đồng bộ với tiếng
            await websocket.send_text(f"AI:{clause}")
            await paced.send(pcm_bytes)

    tts_task = asyncio.create_task(tts_worker())
    sender_task = asyncio.create_task(sender())

    api_key = get_active_api_key()
    emitted_any = False

    async def emit(clause: str) -> None:
        nonlocal emitted_any
        emitted_any = True
        planned_clauses.append(clause)
        await clause_queue.put(clause)

    if HAS_GENAI and api_key:
        from google.genai import types

        history: List[Any] = session_state.setdefault("gemini_contents", [])
        user_content = types.Content(role="user", parts=[types.Part(text=user_text)])
        reply_texts: List[str] = []
        stats: Dict[str, Any] = {"first_text": None, "chunks": 0}
        t_ai0 = time.perf_counter()

        async def _produce_gemini_stream() -> None:
            client = get_gemini_client(api_key)
            config = types.GenerateContentConfig(
                system_instruction=CFG["system_instruction"],
                thinking_config=types.ThinkingConfig(thinking_level=CFG["gemini_thinking"]),
            )
            stream = await client.aio.models.generate_content_stream(
                model=CFG["gemini_model"], contents=[*history, user_content], config=config,
            )
            buffer = ""
            async for chunk in stream:
                piece = ""
                for cand in chunk.candidates or []:
                    for part in (cand.content.parts if cand.content else None) or []:
                        if part.text and not part.thought:
                            piece += part.text
                if not piece:
                    continue
                stats["chunks"] += 1
                if stats["first_text"] is None:
                    stats["first_text"] = time.perf_counter() - t_ai0
                buffer += piece
                while True:
                    # Mệnh đề sau dài tối đa ~bằng tổng phần đã nói (tối thiểu 40 ký tự): VieNeu tổng hợp nhanh hơn
                    # thời gian thực một chút (~0.7-0.8x), nên mệnh đề kế tiếp kịp xong trước khi loa phát hết.
                    spoken_chars = sum(len(t) for t in reply_texts)
                    clause, buffer = extract_ready_clause(
                        buffer, first=not emitted_any, max_chars=max(40, spoken_chars) if emitted_any else 0,
                    )
                    if not clause:
                        break
                    reply_texts.append(clause)
                    await emit(clause)
            if buffer.strip():
                reply_texts.append(buffer.strip())
                await emit(buffer.strip())

        try:
            await asyncio.wait_for(_produce_gemini_stream(), timeout=16.0)
            if not emitted_any:
                await emit("Mình chưa nghe rõ ý bạn vừa nói, bạn nhắc lại rõ hơn một chút nhé.")
            ft = stats["first_text"]
            write_log(
                "GEMINI_STREAM_DONE",
                f"{CFG['gemini_model']} (suy nghĩ: {CFG['gemini_thinking']}): chữ đầu sau "
                f"{(f'{ft:.2f}s' if ft is not None else '-')}, xong sau {time.perf_counter() - t_ai0:.2f}s, "
                f"{stats['chunks']} mẩu | Ngữ cảnh: {len(history) // 2} lượt trước",
            )
        except Exception as e:
            write_log("GEMINI_ERROR", f"Lỗi stream Gemini ({type(e).__name__}: {e}) -> Dùng Local Context")
            if not emitted_any:
                fallback_text = local_contextual_fallback(user_text, session_state)
                for c in split_into_speakable_clauses(fallback_text):
                    await emit(c)

        # Lưu lượt này vào ngữ cảnh (kể cả khi bị cắt giữa chừng, giữ phần AI đã nói) và cắt bớt lượt cũ
        if reply_texts:
            history.append(user_content)
            history.append(types.Content(role="model", parts=[types.Part(text=" ".join(reply_texts))]))
            max_items = int(CFG["history_turns"]) * 2
            if len(history) > max_items:
                del history[: len(history) - max_items]
    else:
        fallback_text = local_contextual_fallback(user_text, session_state)
        for c in split_into_speakable_clauses(fallback_text):
            await emit(c)

    # Báo hiệu kết thúc hàng đợi mệnh đề và chờ tổng hợp + gửi hết các chunk PCM xuống thiết bị
    await clause_queue.put(None)
    try:
        await asyncio.gather(tts_task, sender_task)
    finally:
        for t in (tts_task, sender_task):
            if not t.done():
                t.cancel()


# =====================================================================
# GIAO DIỆN WEB THEO DÕI LỊCH SỬ & LOG CHI TIẾT (HTTP GET /)
# =====================================================================
@app.get("/api/state")
async def get_state():
    has_key = bool(get_active_api_key())
    devices = []
    for d in list(ACTIVE_DEVICES.values()):
        info = {k: v for k, v in d.items() if k != "ws"}
        info["tts"] = (d["tts_forced"] == "on") if d["tts_forced"] else bool(CFG["tts_output"])
        devices.append(info)
    return JSONResponse({
        "ai_mode": f"Google Gemini ({CFG['gemini_model']} - Streaming Clause TTS)" if has_key else "Local Context Memory (Chưa điền GEMINI_API_KEY)",
        "has_api_key": has_key,
        "log_file": LOG_FILE_PATH,
        "logs": CONVERSATION_LOG[-20:],
        "events": SYSTEM_EVENTS[-30:],
        "devices": devices,
    })


# =====================================================================
# ⚙️ API CÀI ĐẶT & THAO TÁC NHANH CHO DASHBOARD
# =====================================================================
@app.get("/api/settings")
async def get_settings():
    return JSONResponse({"schema": SETTINGS_SCHEMA, "values": CFG, "file": SETTINGS_FILE})


@app.post("/api/settings")
async def post_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"errors": {"_": "Dữ liệu gửi lên phải là JSON {khóa: giá trị}"}, "values": CFG}, status_code=400)
    applied, errors = update_settings(body)
    # Vừa bật loa / chuyển sang VieNeu mà model chưa nạp -> nạp nền để lượt nói kế tiếp không phải chờ ~15-25s
    if ("tts_output" in applied or "tts_engine" in applied) and CFG["tts_output"] and CFG["tts_engine"] == "vieneu" and _VIENEU_TTS is None:
        asyncio.get_running_loop().run_in_executor(None, get_vieneu_tts)
    return JSONResponse({"applied": applied, "errors": errors, "values": CFG}, status_code=400 if errors else 200)


@app.post("/api/action/{name}")
async def run_action(name: str, request: Request):
    global CONTEXT_EPOCH
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    if name == "reset_context":
        CONTEXT_EPOCH += 1
        write_log("CONTEXT_RESET", "🧹 Dashboard yêu cầu xóa ngữ cảnh -> áp dụng từ lượt nói tiếp theo")
        return JSONResponse({"ok": True, "message": "Đã xóa ngữ cảnh: AI sẽ bắt đầu cuộc trò chuyện mới từ lượt kế tiếp"})

    if name == "clear_history":
        CONVERSATION_LOG.clear()
        return JSONResponse({"ok": True, "message": "Đã xóa lịch sử hội thoại trên Dashboard"})

    if name == "reset_settings":
        for s in SETTINGS_SCHEMA:
            CFG[s["key"]] = s["default"]
        save_settings()
        write_log("SETTINGS", "↺ Đã khôi phục toàn bộ cài đặt mặc định")
        return JSONResponse({"ok": True, "message": "Đã khôi phục cài đặt mặc định", "values": CFG})

    if name == "test_tts":
        text = str(body.get("text") or "").strip()[:200] or "Xin chào, đây là giọng đọc thử ở mức âm lượng hiện tại."
        if not ACTIVE_DEVICES:
            return JSONResponse({"ok": False, "message": "Chưa có thiết bị nào kết nối"}, status_code=409)
        targets = [d for d in list(ACTIVE_DEVICES.values()) if not d["busy"] and d["output_format"] == "pcm"]
        if not targets:
            return JSONResponse({"ok": False, "message": "Thiết bị đang bận xử lý lượt nói, thử lại sau vài giây"}, status_code=409)
        pcm, engine_name = await synth_clause_pcm(text)
        if not pcm:
            return JSONResponse({"ok": False, "message": f"Không tổng hợp được giọng ({engine_name})"}, status_code=500)
        pcm, pk_in, pk_out, _ = boost_tts_pcm(pcm)
        sent = 0
        for d in targets:
            d["busy"] = True
            d["state"] = "Đang phát thử giọng"
            try:
                ws = d["ws"]
                await ws.send_text("START_OF_TTS")
                await ws.send_text(f"AI:{text}")
                await PacedPcmSender(ws, d["send_lead"]).send(pcm)
                await ws.send_text("END_OF_TTS")
                sent += 1
            except Exception as e:
                write_log("TEST_TTS", f"Lỗi gửi tới {d['addr']}: {e}")
            finally:
                d["busy"] = False
                d["state"] = "Đang nghe"
        msg = (f"Đã phát thử trên {sent} thiết bị: {engine_name}, dài {len(pcm) / BYTES_PER_SECOND:.1f}s, "
               f"âm lượng đỉnh {pk_in:.0f}% → {pk_out:.0f}% (+{float(CFG['tts_boost_db']):.0f}dB)")
        write_log("TEST_TTS", f"🔈 {msg}")
        return JSONResponse({"ok": sent > 0, "message": msg})

    return JSONResponse({"ok": False, "message": f"Không có thao tác '{name}'"}, status_code=404)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AutoBot - Bảng điều khiển</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --muted:#94a3b8; --accent:#38bdf8; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: var(--bg); color: #f8fafc; margin: 0; padding: 24px; }
    .container { max-width: 1100px; margin: 0 auto; }
    h1 { color: var(--accent); margin: 0 0 14px; font-size: 24px; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-bottom: 20px; }
    .card h3 { margin: 0 0 12px; color: #e2e8f0; font-size: 16px; }
    .status { line-height: 1.6; }
    .muted { color: var(--muted); } .small { font-size: 12.5px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
    .log-box { max-height: 520px; overflow-y: auto; }
    .msg { padding: 10px 0; border-bottom: 1px solid var(--line); line-height: 1.5; font-size: 14px; }
    .msg:last-child { border-bottom: none; }
    .evt { font-family: Consolas, monospace; font-size: 12.5px; padding: 6px 0; border-bottom: 1px dashed var(--line); line-height: 1.4; }
    .badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: bold; background: var(--line); color: var(--accent); }
    .badge.on { color: var(--ok); } .badge.off { color: var(--warn); }

    /* Thiết bị */
    .dev { padding: 8px 0 12px; border-bottom: 1px solid var(--line); }
    .dev:last-child { border-bottom: none; }
    .devhead { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
    .meter { position: relative; height: 14px; background: #0b1220; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; margin-bottom: 6px; }
    .meter .fill { height: 100%; background: linear-gradient(90deg, #22c55e, #4ade80); transition: width .25s; }
    .meter .mk { position: absolute; top: 0; bottom: 0; width: 2px; }
    .mk.floor { background: var(--muted); } .mk.cont { background: var(--warn); } .mk.start { background: var(--bad); }
    .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 10px; vertical-align: -1px; }

    /* Cài đặt */
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
    .actions input[type=text] { flex: 1 1 260px; }
    button { background: #0ea5e9; color: #fff; border: 0; border-radius: 8px; padding: 8px 12px; font-size: 13.5px; cursor: pointer; }
    button.secondary { background: var(--line); color: #e2e8f0; }
    button:hover { filter: brightness(1.12); } button:disabled { opacity: .5; cursor: wait; }
    details.group { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 12px; background: #172033; }
    details.group > summary { cursor: pointer; padding: 10px 14px; font-weight: 600; color: #e2e8f0; }
    .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px 22px; padding: 4px 14px 14px; }
    .field.wide { grid-column: 1 / -1; }
    .lbl { font-size: 13.5px; margin-bottom: 6px; color: #e2e8f0; }
    .help { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.4; }
    input[type=text], select, textarea { width: 100%; background: #0b1220; color: #f8fafc; border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; font: inherit; font-size: 13.5px; }
    textarea { resize: vertical; line-height: 1.45; }
    .range { display: flex; align-items: center; gap: 10px; }
    .range input { flex: 1; accent-color: var(--accent); }
    .range b { min-width: 74px; text-align: right; color: var(--accent); font-size: 13px; }
    .switch { position: relative; display: inline-block; width: 46px; height: 24px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .switch span { position: absolute; inset: 0; background: var(--line); border-radius: 24px; transition: .2s; cursor: pointer; }
    .switch span:before { content: ""; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .2s; }
    .switch input:checked + span { background: #22c55e; }
    .switch input:checked + span:before { transform: translateX(22px); }
    #toast { position: fixed; right: 20px; bottom: 20px; padding: 10px 14px; border-radius: 8px; background: #166534; color: #fff; font-size: 13.5px; opacity: 0; transition: opacity .25s; pointer-events: none; max-width: 520px; }
    #toast.err { background: #991b1b; } #toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="container">
    <h1>🎧 AutoBot — Bảng điều khiển đàm thoại giọng nói</h1>

    <div class="card status">
      <div><strong>Chế độ AI:</strong> <span id="mode" style="color:var(--ok)">Đang tải...</span></div>
      <div class="small muted">File log: <code id="logfile" style="color:var(--warn)">voice_session.log</code> · Cài đặt lưu tại: <code id="cfgfile" style="color:var(--warn)">settings.json</code></div>
    </div>

    <div class="card">
      <h3>📟 Thiết bị đang kết nối</h3>
      <div id="devices" class="muted">Đang tải...</div>
      <div class="small muted legend" style="margin-top:6px">Thanh mức mic:<i style="background:#4ade80"></i>mức hiện tại<i style="background:var(--muted)"></i>nền ồn<i style="background:var(--warn)"></i>ngưỡng duy trì câu<i style="background:var(--bad)"></i>ngưỡng bắt đầu thu</div>
    </div>

    <div class="card">
      <h3>⚙️ Cài đặt <span class="small muted">(áp dụng ngay, tự lưu)</span></h3>
      <div class="actions">
        <input type="text" id="testText" placeholder="Câu đọc thử (để trống = câu mặc định)"/>
        <button id="btnTest" onclick="testTts()">🔈 Phát thử ra loa</button>
        <button class="secondary" onclick="action('reset_context')">🧹 Xóa ngữ cảnh AI</button>
        <button class="secondary" onclick="action('clear_history')">🗑️ Xóa lịch sử</button>
        <button class="secondary" onclick="if (confirm('Khôi phục toàn bộ cài đặt về mặc định?')) action('reset_settings')">↺ Khôi phục mặc định</button>
      </div>
      <div id="settings" class="muted">Đang tải cài đặt...</div>
    </div>

    <div class="grid">
      <div class="card log-box">
        <h3>💬 Lịch sử hội thoại</h3>
        <div id="logs" class="muted">Hãy nói vào Mic để bắt đầu trò chuyện...</div>
      </div>
      <div class="card log-box">
        <h3>📋 System Log trực tiếp</h3>
        <div id="events" class="muted">Đang chờ sự kiện âm thanh...</div>
      </div>
    </div>
  </div>
  <div id="toast"></div>

  <script>
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let SCHEMA = [], VALUES = {};
    let toastTimer = null;

    function toast(msg, isErr) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.className = 'show' + (isErr ? ' err' : '');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { t.className = isErr ? 'err' : ''; }, isErr ? 6000 : 2500);
    }

    function fmt(spec, v) {
      if (spec.type !== 'range') return v;
      const n = Number(v);
      const shown = Number.isInteger(spec.step) ? n.toFixed(0) : n.toFixed(2).replace(/0$/, '');
      return shown + (spec.unit ? ' ' + spec.unit : '');
    }

    function field(s) {
      const v = VALUES[s.key];
      const help = s.help ? `<div class="help">${esc(s.help)}</div>` : '';
      let ctl = '';
      if (s.type === 'bool') {
        ctl = `<label class="switch"><input type="checkbox" data-key="${s.key}" ${v ? 'checked' : ''}><span></span></label>`;
      } else if (s.type === 'range') {
        ctl = `<div class="range"><input type="range" data-key="${s.key}" min="${s.min}" max="${s.max}" step="${s.step}" value="${v}"><b id="v_${s.key}">${esc(fmt(s, v))}</b></div>`;
      } else if (s.type === 'select') {
        ctl = `<select data-key="${s.key}">${s.options.map(o => `<option value="${esc(o[0])}" ${o[0] === v ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`;
      } else if (s.type === 'textarea') {
        ctl = `<textarea data-key="${s.key}" rows="4">${esc(v)}</textarea>`;
      } else {
        ctl = `<input type="text" data-key="${s.key}" value="${esc(v)}">`;
      }
      return `<div class="field${s.type === 'textarea' ? ' wide' : ''}"><div class="lbl">${esc(s.label)}</div>${ctl}${help}</div>`;
    }

    function renderSettings() {
      const groups = {};
      SCHEMA.forEach(s => { (groups[s.group] = groups[s.group] || []).push(s); });
      document.getElementById('settings').innerHTML = Object.entries(groups).map(([g, items]) =>
        `<details open class="group"><summary>${esc(g)}</summary><div class="fields">${items.map(field).join('')}</div></details>`
      ).join('');
      document.getElementById('settings').classList.remove('muted');
      document.querySelectorAll('#settings [data-key]').forEach(el => {
        const key = el.dataset.key;
        const spec = SCHEMA.find(s => s.key === key);
        if (spec.type === 'range') {
          el.addEventListener('input', () => { document.getElementById('v_' + key).textContent = fmt(spec, el.value); });
          el.addEventListener('change', () => save(key, Number(el.value)));
        } else if (spec.type === 'bool') {
          el.addEventListener('change', () => save(key, el.checked));
        } else {
          el.addEventListener('change', () => save(key, el.value));
        }
      });
    }

    async function loadSettings() {
      const res = await fetch('/api/settings');
      const data = await res.json();
      SCHEMA = data.schema; VALUES = data.values;
      document.getElementById('cfgfile').textContent = data.file;
      renderSettings();
    }

    async function save(key, value) {
      try {
        const res = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [key]: value }) });
        const data = await res.json();
        VALUES = data.values;
        const errs = Object.values(data.errors || {});
        if (errs.length) { toast('Không lưu được: ' + errs.join(', '), true); renderSettings(); }
        else {
          const spec = SCHEMA.find(s => s.key === key);
          toast('Đã lưu: ' + spec.label + (spec.type === 'textarea' ? '' : ' = ' + (spec.type === 'bool' ? (value ? 'BẬT' : 'TẮT') : fmt(spec, VALUES[key]))));
        }
      } catch (e) { toast('Lỗi kết nối server: ' + e, true); }
    }

    async function action(name, body) {
      try {
        const res = await fetch('/api/action/' + name, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
        const data = await res.json();
        toast(data.message || (data.ok ? 'Xong' : 'Lỗi'), !data.ok);
        if (data.values) { VALUES = data.values; renderSettings(); }
        if (name === 'clear_history') document.getElementById('logs').innerHTML = '<span class="muted">Đã xóa lịch sử.</span>';
        return data;
      } catch (e) { toast('Lỗi kết nối server: ' + e, true); }
    }

    async function testTts() {
      const btn = document.getElementById('btnTest');
      btn.disabled = true; btn.textContent = '⏳ Đang tổng hợp...';
      await action('test_tts', { text: document.getElementById('testText').value });
      btn.disabled = false; btn.textContent = '🔈 Phát thử ra loa';
    }

    function renderDevices(devs) {
      if (!devs.length) return '<span class="muted">Chưa có thiết bị nào kết nối (ESP32 tự kết nối lại mỗi 3 giây).</span>';
      return devs.map(d => {
        const scale = Math.max(d.level, d.start_thr * 1.4, 1);
        const p = x => Math.min(100, x / scale * 100).toFixed(1);
        return `<div class="dev">
          <div class="devhead"><b>${esc(d.addr)}</b>
            <span class="badge">${esc(d.state)}</span>
            <span class="badge ${d.tts ? 'on' : 'off'}">Loa ${d.tts ? 'BẬT' : 'TẮT'}${d.tts_forced ? ' (theo URL)' : ''}</span>
            <span class="small muted">kết nối từ ${esc(d.since)}</span></div>
          <div class="meter"><div class="fill" style="width:${p(d.level)}%"></div>
            <i class="mk floor" style="left:${p(d.floor)}%"></i><i class="mk cont" style="left:${p(d.cont_thr)}%"></i><i class="mk start" style="left:${p(d.start_thr)}%"></i></div>
          <div class="small muted">Mức mic ${d.level} · Nền ồn ${d.floor} · Ngưỡng duy trì ${d.cont_thr} · Ngưỡng bắt đầu ${d.start_thr}</div>
        </div>`;
      }).join('');
    }

    async function refresh() {
      try {
        const res = await fetch('/api/state');
        const data = await res.json();
        document.getElementById('mode').textContent = data.ai_mode;
        document.getElementById('logfile').textContent = data.log_file;
        document.getElementById('devices').innerHTML = renderDevices(data.devices || []);
        if (data.logs.length > 0) {
          document.getElementById('logs').innerHTML = data.logs.slice().reverse().map(l =>
            `<div class="msg">
              <div class="small muted">[${esc(l.time)}] Lượt #${esc(l.turn || '1')} (${esc(l.latency)})</div>
              <div>🧑 <b>Bạn:</b> ${esc(l.user)}</div>
              <div style="color:var(--accent);margin-top:4px">🤖 <b>AI:</b> ${esc(l.ai)}</div>
            </div>`
          ).join('');
        }
        if (data.events.length > 0) {
          document.getElementById('events').innerHTML = data.events.slice().reverse().map(e =>
            `<div class="evt"><span class="muted">[${esc(e.time)}]</span> <span class="badge">${esc(e.stage)}</span> ${esc(e.message)}</div>`
          ).join('');
        }
      } catch (err) {}
    }

    loadSettings().catch(e => toast('Không tải được cài đặt: ' + e, true));
    refresh();
    setInterval(refresh, 700);
  </script>
</body>
</html>"""


# =====================================================================
# WEBSOCKET ENDPOINT (TÍCH HỢP ĐẦY ĐỦ BƯỚC 1 -> BƯỚC 5)
# =====================================================================
@app.websocket("/ws/audio")
async def audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    output_format = websocket.query_params.get("format", "mp3").lower()
    vad_mode = websocket.query_params.get("vad", "manual").lower()
    tts_param = websocket.query_params.get("tts", "").lower()

    def current_tts() -> bool:
        """Bật/tắt loa: tham số URL (tts=on/off) ưu tiên, không có thì theo cài đặt Dashboard (đổi được giữa chừng)"""
        return (tts_param != "off") if tts_param in ("on", "off") else bool(CFG["tts_output"])

    client_addr = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    # Dung lượng bộ đệm phát của thiết bị (bytes) -> quyết định gửi trước loa bao nhiêu giây
    try:
        device_buf = int(websocket.query_params.get("buf", "0"))
    except ValueError:
        device_buf = 0
    send_lead = send_lead_for_buffer(device_buf)
    device_key = websocket.query_params.get("dev", "").strip() or (websocket.client.host if websocket.client else "?")

    dev_id = id(websocket)
    dev: Dict[str, Any] = {
        "ws": websocket, "addr": client_addr, "since": datetime.now().strftime("%H:%M:%S"),
        "state": "Đang nghe", "busy": False, "floor": 0, "start_thr": 0, "cont_thr": 0, "level": 0,
        "tts_forced": tts_param if tts_param in ("on", "off") else "", "output_format": output_format,
        "send_lead": send_lead,
    }
    ACTIVE_DEVICES[dev_id] = dev

    session_state, restored = get_device_session(device_key)
    tts_info = "Giọng đọc TTS: BẬT" if current_tts() else "Giọng đọc TTS: TẮT - chỉ trả lời bằng chữ"
    buf_info = f"{device_buf // 1024}KB" if device_buf else "không báo (giả định 32KB)"
    ctx_info = f"Khôi phục ngữ cảnh {session_state['turn_count']} lượt trước" if restored else "Cuộc trò chuyện mới"
    write_log(
        "WS_CONNECT",
        f"Thiết bị đã kết nối {client_addr} [id: {device_key}] (Output: {output_format.upper()} | VAD: {vad_mode.upper()} | "
        f"{tts_info} | Bộ đệm loa: {buf_info} -> gửi trước tối đa {send_lead:.1f}s | {ctx_info})",
    )

    audio_buffer = bytearray()
    # ~640ms âm thanh trước lúc VAD bật (gói ESP32 ~32ms) để không mất phụ âm đầu câu
    pre_roll_chunks: deque[bytes] = deque(maxlen=20)

    is_speaking = False
    consecutive_voice_chunks = 0
    speech_seconds = 0.0
    silence_seconds = 0.0
    max_rms_in_turn = 0
    max_peak_in_turn = 0
    rx_stats: Dict[str, Any] = {"bytes": 0, "chunks": 0, "t0": time.perf_counter()}
    smooth_sq: deque[float] = deque(maxlen=VAD_SMOOTH_CHUNKS)      # RMS^2 các gói gần nhất (làm mượt)
    floor_hist: deque[int] = deque(maxlen=VAD_FLOOR_WINDOW_CHUNKS)  # Lịch sử RMS làm mượt ~6s để đo nền ồn

    async def process_and_respond(pcm_bytes: bytes, spoken_sec: float, turn_max_rms: int, turn_max_peak: int):
        """Bọc lượt xử lý: cập nhật trạng thái thiết bị trên Dashboard (đang bận -> không cho Phát thử chen vào)"""
        dev["busy"] = True
        dev["state"] = "Đang xử lý (STT → AI → giọng đọc)"
        try:
            await _process_turn(pcm_bytes, spoken_sec, turn_max_rms, turn_max_peak)
        finally:
            dev["busy"] = False
            dev["state"] = "Đang nghe"

    async def _process_turn(pcm_bytes: bytes, spoken_sec: float, turn_max_rms: int, turn_max_peak: int):
        """Thực thi chuỗi Bandpass DSP + AGC -> STT -> Streaming Gemini + Clause TTS -> Phát cuốn chiếu ra Tai nghe"""
        if len(pcm_bytes) < int(BYTES_PER_SECOND * 0.35):
            return
        tts_enabled = current_tts()

        if CFG["debug_save_wav"]:
            try:
                os.makedirs(RECORDINGS_DIR, exist_ok=True)
                wav_path = os.path.join(RECORDINGS_DIR, f"{datetime.now():%Y%m%d_%H%M%S}_raw.wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(SAMPLE_WIDTH)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(pcm_bytes)
                write_log("DEBUG_WAV", f"Đã lưu âm thanh gốc {len(pcm_bytes) / BYTES_PER_SECOND:.2f}s -> {wav_path}")
            except Exception as e:
                write_log("DEBUG_WAV", f"Lỗi lưu WAV: {e}")

        turn_start_time = time.perf_counter()

        # Báo hiệu Client chuẩn bị nhận luồng âm thanh (và tạm khóa thu Mic tránh vọng âm)
        await websocket.send_text("START_OF_TTS")

        t_stt = time.perf_counter()
        wav_bytes = await asyncio.to_thread(pcm_to_wav_bytes, pcm_bytes)
        user_text = await asyncio.to_thread(speech_to_text, wav_bytes)
        stt_elapsed = time.perf_counter() - t_stt

        if not user_text:
            if vad_mode == "auto":
                write_log(
                    "NOISE_IGNORED",
                    f"Bỏ qua tạp âm không có chữ (Thời lượng: {spoken_sec:.2f}s | MaxRMS: {turn_max_rms} | MaxPeak: {turn_max_peak} | STT: {stt_elapsed:.2f}s)",
                )
                await websocket.send_text("END_OF_TTS")
                return
            # Chế độ manual (Bước 1)
            if not tts_enabled:
                await websocket.send_text("AI:Mình chưa nghe rõ, bạn nói lại nhé.")
                await websocket.send_text("END_OF_TTS")
                return
            pcm_response, _ = await synth_clause_pcm("Mình chưa nghe rõ, bạn nói lại nhé.")
            if output_format == "pcm":
                pcm_response, _, _, _ = boost_tts_pcm(pcm_response)
                await PacedPcmSender(websocket, send_lead).send(pcm_response)
            else:
                await websocket.send_bytes(pcm_response)
            await websocket.send_text("END_OF_TTS")
            return

        write_log("STT_DONE", f"Nhận diện giọng nói trong {stt_elapsed:.2f}s -> \"{user_text}\"")
        # Gửi câu đã nhận diện xuống thiết bị (ESP32 hiển thị lên OLED "Bạn: ...")
        await websocket.send_text(f"USER:{user_text}")

        # Dashboard bấm "Xóa ngữ cảnh" hoặc im lặng quá lâu -> bắt đầu hội thoại mới (Gemini quên các lượt trước)
        idle = time.time() - session_state.get("last_active", time.time())
        if session_state.get("epoch") != CONTEXT_EPOCH or (session_state.get("turn_count", 0) > 0 and idle > SESSION_IDLE_RESET_SECONDS):
            reason = "theo yêu cầu Dashboard" if session_state.get("epoch") != CONTEXT_EPOCH else f"sau {idle / 60:.0f} phút im lặng"
            session_state.clear()
            session_state.update(new_session_state())
            write_log("CONTEXT_RESET", f"🧹 Đã xóa ngữ cảnh hội thoại {reason} -> Gemini bắt đầu cuộc trò chuyện mới")

        # Chạy luồng cuốn chiếu: Gemini Stream tới đâu -> cắt Chunk mệnh đề -> TTS & gửi PCM về tai nghe tới đó
        await stream_ai_and_tts_clauses(
            websocket=websocket,
            user_text=user_text,
            session_state=session_state,
            output_format=output_format,
            turn_start_time=turn_start_time,
            tts_enabled=tts_enabled,
            send_lead=send_lead,
        )

        await websocket.send_text("END_OF_TTS")

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                write_log("WS_DISCONNECT", "Thiết bị đã ngắt kết nối.")
                break

            # 1. Nhận luồng Byte âm thanh (từ Mic PC hoặc ESP32)
            if "bytes" in message and message["bytes"]:
                chunk = message["bytes"]
                chunk_duration = len(chunk) / BYTES_PER_SECOND

                if CFG["debug_log_rx_rate"]:
                    now_t = time.perf_counter()
                    rx_stats["bytes"] += len(chunk)
                    rx_stats["chunks"] += 1
                    win = now_t - rx_stats["t0"]
                    if win >= 10.0:
                        rate = rx_stats["bytes"] / win
                        write_log(
                            "RX_RATE",
                            f"Nhận {rate:.0f} B/s ({rate / BYTES_PER_SECOND * 100:.0f}% so với chuẩn 32000) | "
                            f"{rx_stats['chunks'] / win:.1f} gói/s, TB {rx_stats['bytes'] // max(rx_stats['chunks'], 1)} B/gói",
                        )
                        rx_stats.update(bytes=0, chunks=0, t0=now_t)

                raw_samples = np.frombuffer(chunk, dtype=np.int16)
                if len(raw_samples) == 0:
                    continue

                # Đưa qua bộ lọc thông dải 300Hz - 3400Hz
                voice_samples = filter_voice_bandpass(raw_samples)
                peak = int(np.max(np.abs(voice_samples)))
                rms = int(np.sqrt(np.mean(voice_samples.astype(np.float32) ** 2)))

                # --- Ngưỡng VAD tự thích nghi theo nền ồn (đo liên tục: phân vị thấp của RMS làm mượt) ---
                smooth_sq.append(float(rms) ** 2)
                smooth_rms = int(np.sqrt(np.mean(smooth_sq)))
                floor_hist.append(smooth_rms)
                floor_v = float(np.percentile(floor_hist, VAD_FLOOR_PERCENTILE)) if len(floor_hist) >= 10 else 0.0
                vad_adaptive = bool(CFG["vad_adaptive"])
                start_peak = int(CFG["vad_start_peak"])
                start_rms = max(int(CFG["vad_start_rms"]), int(floor_v * float(CFG["vad_start_factor"]))) if vad_adaptive else int(CFG["vad_start_rms"])
                continue_rms = max(int(CFG["vad_continue_rms"]), int(floor_v * float(CFG["vad_continue_factor"]))) if vad_adaptive else int(CFG["vad_continue_rms"])
                dev.update(floor=int(floor_v), start_thr=start_rms, cont_thr=continue_rms, level=smooth_rms)
                if not dev["busy"]:
                    dev["state"] = "Đang thu giọng nói" if is_speaking else "Đang nghe"

                # --- BƯỚC 2: Vẽ thanh đo sóng âm đã lọc tạp âm ---
                bars = int((peak / 32768.0) * 40)
                meter = "█" * bars + "-" * (40 - bars)
                has_voice_energy = (smooth_rms >= continue_rms) if is_speaking else (rms >= start_rms and peak >= start_peak)
                status = " 🗣️ [GIỌNG NÓI]" if has_voice_energy else ""
                print(
                    f"\rLọc âm: |{meter}| RMS:{rms:4d} Peak:{peak:5d} Nền:{int(floor_v):4d} Ngưỡng:{start_rms}/{continue_rms}{status:<16}",
                    end="", flush=True,
                )

                # --- BƯỚC 3: Auto-VAD dựa trên năng lượng giọng nói đã lọc ---
                if vad_mode == "auto":
                    if not is_speaking:
                        pre_roll_chunks.append(chunk)
                        if rms >= start_rms and peak >= start_peak:
                            consecutive_voice_chunks += 1
                            if consecutive_voice_chunks >= VAD_START_CONSECUTIVE:
                                is_speaking = True
                                speech_seconds = chunk_duration * consecutive_voice_chunks
                                silence_seconds = 0.0
                                max_rms_in_turn = rms
                                max_peak_in_turn = peak
                                consecutive_voice_chunks = 0
                                audio_buffer.clear()
                                for pre_chunk in pre_roll_chunks:
                                    audio_buffer.extend(pre_chunk)
                                pre_roll_chunks.clear()
                                write_log(
                                    "VAD_START",
                                    f"🎙️ Bắt đầu thu giọng nói (RMS: {rms}, Peak: {peak} | Nền ồn: {int(floor_v)} -> ngưỡng bắt đầu {start_rms} / duy trì {continue_rms})",
                                )
                        else:
                            consecutive_voice_chunks = 0
                    else:
                        audio_buffer.extend(chunk)
                        if rms > max_rms_in_turn:
                            max_rms_in_turn = rms
                        if peak > max_peak_in_turn:
                            max_peak_in_turn = peak

                        if (smooth_rms if vad_adaptive else rms) >= continue_rms:
                            speech_seconds += chunk_duration
                            silence_seconds = 0.0
                        else:
                            silence_seconds += chunk_duration

                        max_len = float(CFG["vad_max_speech_seconds"])
                        too_long = len(audio_buffer) / BYTES_PER_SECOND >= max_len
                        if too_long:
                            write_log("VAD_MAX_LEN", f"✂️ Câu dài quá {max_len:.0f}s -> cắt cưỡng bức (Nền ồn: {int(floor_v)})")
                        if silence_seconds >= float(CFG["vad_silence_seconds"]) or too_long:
                                is_speaking = False
                                total_spoken = speech_seconds
                                turn_rms = max_rms_in_turn
                                turn_peak = max_peak_in_turn
                                speech_seconds = 0.0
                                silence_seconds = 0.0
                                max_rms_in_turn = 0
                                max_peak_in_turn = 0
                                if total_spoken >= float(CFG["vad_min_speech_seconds"]):
                                    write_log(
                                        "VAD_END",
                                        f"⏹️ Ngắt câu tự động (Thời lượng nói: {total_spoken:.2f}s | MaxRMS: {turn_rms} | MaxPeak: {turn_peak})",
                                    )
                                    captured = bytes(audio_buffer)
                                    audio_buffer.clear()
                                    await process_and_respond(captured, total_spoken, turn_rms, turn_peak)
                                else:
                                    audio_buffer.clear()
                else:
                    audio_buffer.extend(chunk)

            # 2. Nhận tín hiệu điều khiển dạng Text
            elif "text" in message and message["text"]:
                cmd = message["text"]
                if cmd == "END_OF_SPEECH":
                    is_speaking = False
                    captured = bytes(audio_buffer)
                    audio_buffer.clear()
                    await process_and_respond(captured, len(captured) / BYTES_PER_SECOND, 0, 0)

    except WebSocketDisconnect:
        write_log("WS_DISCONNECT", "Thiết bị đã ngắt kết nối.")
    finally:
        ACTIVE_DEVICES.pop(dev_id, None)


class EndpointFilter(logging.Filter):
    """Ẩn log polling GET /api/state của trình duyệt để không làm rối cửa sổ Terminal"""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/state" not in record.getMessage()


if __name__ == "__main__":
    import uvicorn

    logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

    key = get_active_api_key()
    if key:
        try:
            get_gemini_client(key)
        except Exception:
            pass

    # Chỉ nạp sẵn VieNeu khi bật giọng đọc (tắt thì khởi động nhanh hơn ~15-25s; vẫn nạp muộn nếu kết nối nào xin tts=on)
    if CFG["tts_engine"] == "vieneu" and CFG["tts_output"]:
        try:
            get_vieneu_tts()
        except Exception:
            pass

    tts_info = f"VieNeu ({CFG['vieneu_voice']} 48kHz)" if CFG["tts_engine"] == "vieneu" else f"Edge-TTS ({CFG['edge_voice']})"
    if not CFG["tts_output"]:
        tts_info = f"TẮT - chỉ trả lời bằng chữ (bật lại trên Dashboard | engine: {tts_info})"
    api_status = f"ĐÃ KẾT NỐI GEMINI API ({CFG['gemini_model']} - Streaming Clause TTS)" if key else "CHƯA ĐIỀN GEMINI_API_KEY"
    write_log(
        "SERVER_START",
        f"Khởi động Server Đàm thoại AI | Trạng thái: {api_status} | TTS: {tts_info} | "
        f"Cài đặt: {SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else 'mặc định (chưa có settings.json)'} | Log file: {LOG_FILE_PATH}",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)
