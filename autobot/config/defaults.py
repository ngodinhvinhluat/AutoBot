"""
Giá trị MẶC ĐỊNH của các cài đặt (lấy từ hằng số / file .env). settings.json (chỉnh trên Dashboard) ghi đè lên.
Các hằng số nội bộ không chỉnh trên Dashboard nằm ngay trong module dùng nó (vad.py, playback.py...).
"""
import os

from autobot.core.env import load_env

load_env()

# --- AI (Gemini) ---
GEMINI_MODEL = "gemini-3.5-flash-lite"

# --- VAD: ngưỡng đo trên tín hiệu ĐÃ LỌC TẠP ÂM (300Hz - 3400Hz) ---
VAD_START_RMS = 350              # Ngưỡng năng lượng RMS giọng nói để bắt đầu thu
VAD_START_PEAK = 1800            # Ngưỡng biên độ đỉnh giọng nói sau lọc để bắt đầu thu
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
VAD_MAX_SPEECH_SECONDS = 12.0    # Cắt cưỡng bức nếu 1 câu kéo dài quá lâu (phòng quá ồn)

# Khử ù điện lưới 50Hz (các hài 100..1000Hz) trước khi gửi STT.
# TẮT: thử trên bản ghi thật, bật notch làm STT tệ hơn ("cuộc sống chiên cá" thay vì "Tôi cần chiên cá").
# Ù tần thấp đã được bộ lọc thông cao 150Hz trên ESP32 xử lý.
HUM_NOTCH_ENABLED = False

# --- Chẩn đoán Mic: lưu WAV gốc mỗi lượt nói + log tốc độ nhận byte từ thiết bị (chuẩn 32000 B/s) ---
DEBUG_SAVE_TURN_WAV = True
DEBUG_LOG_RX_RATE = True

# --- TTS ---
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
