"""Danh sách cài đặt chỉnh trực tiếp trên Dashboard (nhóm, kiểu, giới hạn, nhãn tiếng Việt)."""
from typing import Any, Dict, List

from autobot.config import defaults as d
from autobot.config.prompts import SYSTEM_INSTRUCTION
from autobot.tts.voices import vieneu_voice_options

G_TTS = "🔊 Loa & giọng đọc"
G_AI = "🤖 Trí tuệ nhân tạo (Gemini)"
G_VAD = "🎙️ Nghe & ngắt câu (VAD / STT)"
G_DBG = "🩺 Chẩn đoán"

_TTS_RATES = ("-20%", "-10%", "+0%", "+10%", "+20%", "+30%")

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {"group": G_TTS, "key": "tts_output", "type": "bool", "default": d.TTS_OUTPUT_DEFAULT,
     "label": "Phát giọng đọc ra loa",
     "help": "Tắt = chỉ trả lời bằng chữ (Dashboard + OLED), phản hồi nhanh hơn ~4s."},
    {"group": G_TTS, "key": "tts_engine", "type": "select", "default": d.TTS_ENGINE if d.TTS_ENGINE in ("vieneu", "edge_tts") else "vieneu",
     "label": "Bộ tổng hợp giọng",
     "options": [["vieneu", "VieNeu (chạy trên máy, giọng tự nhiên)"], ["edge_tts", "Edge-TTS (Microsoft, cần Internet)"]]},
    {"group": G_TTS, "key": "tts_stream", "type": "bool", "default": True,
     "label": "Phát dần khi đang tổng hợp (VieNeu v3 Turbo stream)",
     "help": "Bật: loa cất tiếng sau ~0.4s tổng hợp thay vì chờ xong cả câu (1-3s). Tắt: tổng hợp trọn từng câu rồi mới phát."},
    {"group": G_TTS, "key": "vieneu_voice", "type": "select", "default": d.TTS_VOICE,
     "label": "Giọng VieNeu", "options": vieneu_voice_options(d.TTS_VOICE)},
    {"group": G_TTS, "key": "vieneu_precision", "type": "select", "default": "fp32",
     "label": "Độ chính xác model VieNeu (CPU)",
     "options": [["fp32", "fp32 — mặc định của thư viện"], ["int8", "int8 — nhanh ~1.7x (đo trên i7-7700)"]],
     "help": "Đổi là nạp lại model ở nền (~15s), lượt đang nói vẫn dùng model cũ. Thư viện cảnh báo int8 có thể méo "
             "trên CPU không có VNNI (i7-7700 không có) — đo thử chưa thấy méo, hãy nghe so sánh."},
    {"group": G_TTS, "key": "edge_voice", "type": "select", "default": "vi-VN-HoaiMyNeural",
     "label": "Giọng Edge-TTS",
     "options": [["vi-VN-HoaiMyNeural", "Hoài My — Nữ"], ["vi-VN-NamMinhNeural", "Nam Minh — Nam"]]},
    {"group": G_TTS, "key": "tts_rate", "type": "select", "default": d.TTS_RATE if d.TTS_RATE in _TTS_RATES else "+0%",
     "label": "Tốc độ đọc (Edge-TTS)",
     "options": [["-20%", "Chậm (-20%)"], ["-10%", "Hơi chậm (-10%)"], ["+0%", "Bình thường"], ["+10%", "Hơi nhanh (+10%)"], ["+20%", "Nhanh (+20%)"], ["+30%", "Rất nhanh (+30%)"]]},
    {"group": G_TTS, "key": "tts_max_prebuffer", "type": "range", "default": 2.0, "min": 0, "max": 5, "step": 0.5, "unit": "s",
     "label": "Chờ tối đa trước khi phát (chống ngắt quãng)",
     "help": "VieNeu trên CPU gần sát thời gian thực: khi câu trả lời dài, server tự ước lượng và chờ thêm "
             "(không quá mức này) để phát liền mạch. 0 = phát ngay, có thể ngắt quãng giữa các câu."},
    {"group": G_TTS, "key": "tts_boost_db", "type": "range", "default": d.TTS_BOOST_DB, "min": 0, "max": 15, "step": 1, "unit": "dB",
     "label": "Tăng âm lượng",
     "help": "Chuẩn hóa lên ~95% rồi tăng thêm (nén mềm). 6-9dB: to rõ; >12dB: dễ rè. "
             "Khi phát dần: chuẩn hóa theo đỉnh điển hình của giọng (tự học qua các câu)."},

    {"group": G_AI, "key": "gemini_model", "type": "text", "default": d.GEMINI_MODEL,
     "label": "Model Gemini"},
    {"group": G_AI, "key": "gemini_thinking", "type": "select", "default": "low",
     "label": "Mức suy nghĩ của AI",
     "options": [["minimal", "Tối thiểu (nhanh nhất, nông nhất)"], ["low", "Thấp (khuyến nghị)"],
                 ["medium", "Vừa (chậm hơn)"], ["high", "Cao (chậm nhất)"]],
     "help": "Đo thực tế với flash-lite: minimal và low đều có chữ đầu sau ~0.5-1s."},
    {"group": G_AI, "key": "history_turns", "type": "range", "default": 20, "min": 2, "max": 60, "step": 1, "unit": "lượt",
     "label": "Số lượt hội thoại AI ghi nhớ",
     "help": "Ngữ cảnh gắn theo thiết bị: ESP32 mất kết nối rồi vào lại vẫn nhớ. Không nói gì 30 phút thì bắt đầu cuộc trò chuyện mới."},
    {"group": G_AI, "key": "system_instruction", "type": "textarea", "default": SYSTEM_INSTRUCTION,
     "label": "Lời nhắc hệ thống (tính cách & cách trả lời)",
     "help": "Áp dụng từ lượt nói tiếp theo. Nên giữ yêu cầu trả lời ngắn để giọng đọc nhanh."},

    {"group": G_VAD, "key": "stt_language", "type": "select", "default": "vi-VN",
     "label": "Ngôn ngữ nhận dạng giọng nói",
     "options": [["vi-VN", "Tiếng Việt"], ["en-US", "English (US)"]]},
    {"group": G_VAD, "key": "vad_adaptive", "type": "bool", "default": d.VAD_ADAPTIVE,
     "label": "Tự thích nghi theo nền ồn phòng",
     "help": "Bật: ngưỡng tự nâng khi phòng ồn để vẫn ngắt được câu. Tắt: dùng ngưỡng cố định bên dưới."},
    {"group": G_VAD, "key": "vad_start_factor", "type": "range", "default": d.VAD_START_FACTOR, "min": 1.5, "max": 5.0, "step": 0.1, "unit": "× nền",
     "label": "Độ to để bắt đầu thu", "help": "Cao hơn = ít bị tiếng ồn kích hoạt nhầm, nhưng phải nói to/gần hơn."},
    {"group": G_VAD, "key": "vad_continue_factor", "type": "range", "default": d.VAD_CONTINUE_FACTOR, "min": 1.1, "max": 3.0, "step": 0.1, "unit": "× nền",
     "label": "Độ to coi là vẫn đang nói", "help": "Cao hơn = ngắt câu sớm hơn khi phòng ồn."},
    {"group": G_VAD, "key": "vad_silence_seconds", "type": "range", "default": d.VAD_SILENCE_SECONDS, "min": 0.3, "max": 2.0, "step": 0.05, "unit": "s",
     "label": "Im lặng bao lâu thì chốt câu", "help": "Thấp = phản hồi nhanh nhưng dễ cắt ngang khi ngập ngừng."},
    {"group": G_VAD, "key": "vad_max_speech_seconds", "type": "range", "default": d.VAD_MAX_SPEECH_SECONDS, "min": 4, "max": 30, "step": 1, "unit": "s",
     "label": "Độ dài tối đa 1 câu"},
    {"group": G_VAD, "key": "vad_min_speech_seconds", "type": "range", "default": d.VAD_MIN_SPEECH_SECONDS, "min": 0.2, "max": 1.5, "step": 0.05, "unit": "s",
     "label": "Độ dài tối thiểu để gửi nhận dạng", "help": "Bỏ qua tiếng động quá ngắn (gõ bàn, ho...)."},
    {"group": G_VAD, "key": "vad_start_rms", "type": "range", "default": d.VAD_START_RMS, "min": 100, "max": 2000, "step": 10,
     "label": "Ngưỡng bắt đầu thu (mức sàn, RMS)"},
    {"group": G_VAD, "key": "vad_continue_rms", "type": "range", "default": d.VAD_CONTINUE_RMS, "min": 50, "max": 1500, "step": 10,
     "label": "Ngưỡng duy trì câu (mức sàn, RMS)"},
    {"group": G_VAD, "key": "vad_start_peak", "type": "range", "default": d.VAD_START_PEAK, "min": 300, "max": 10000, "step": 100,
     "label": "Biên độ đỉnh tối thiểu để bắt đầu thu"},
    {"group": G_VAD, "key": "hum_notch", "type": "bool", "default": d.HUM_NOTCH_ENABLED,
     "label": "Khử ù điện lưới 50Hz trước khi nhận dạng",
     "help": "Thử trên bản ghi thật: bật làm nhận dạng KÉM hơn. Chỉ bật để so sánh."},

    {"group": G_DBG, "key": "debug_save_wav", "type": "bool", "default": d.DEBUG_SAVE_TURN_WAV,
     "label": "Lưu WAV gốc mỗi lượt nói (thư mục recordings/)"},
    {"group": G_DBG, "key": "debug_log_rx_rate", "type": "bool", "default": d.DEBUG_LOG_RX_RATE,
     "label": "Ghi log tốc độ nhận âm thanh mỗi 10s"},
]

SETTINGS_INDEX: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SETTINGS_SCHEMA}
