# AutoBot

Trợ lý giọng nói AI đàm thoại hai chiều bằng tiếng Việt. Thiết bị **ESP32-S3** (mic + loa I2S) hoặc một client trên PC stream âm thanh lên **server FastAPI**. Server nhận diện giọng nói, hỏi **Google Gemini**, rồi đọc câu trả lời bằng TTS và stream ngược về thiết bị theo từng đoạn nhỏ để giảm độ trễ.

```
ESP32-S3 / PC client                         Server (server.py)
┌──────────────┐  PCM 16kHz/16-bit/mono   ┌─────────────────────────────────────────────┐
│ Mic (INMP441)│ ───── WebSocket ───────► │ VAD ─► STT (Google) ─► Gemini ─► TTS        │
│ Loa(MAX98357)│ ◄──── PCM chunk 2KB ──── │                          (VieNeu / Edge-TTS)│
└──────────────┘      /ws/audio           └─────────────────────────────────────────────┘
                                            Dashboard web: http://localhost:8000/
```

## Tính năng

- Đàm thoại rảnh tay: tự phát hiện bắt đầu/kết thúc câu nói (Auto-VAD, ngưỡng thích nghi theo tiếng ồn nền).
- Nhiều lượt hội thoại, giữ ngữ cảnh theo từng thiết bị (`history_turns` lượt gần nhất).
- Streaming theo mệnh đề: Gemini trả lời đến đâu, TTS đọc và gửi về thiết bị đến đó.
- Hai engine TTS: **VieNeu-TTS** chạy cục bộ (48kHz, nhiều giọng) hoặc **Edge-TTS** của Microsoft.
- Không có `GEMINI_API_KEY` vẫn chạy được ở chế độ rule-based nội bộ.
- Dashboard web để xem hội thoại và log realtime, chỉnh cài đặt, xóa ngữ cảnh, thử TTS.

## Yêu cầu

- Python 3.10 trở lên (đã chạy thử trên 3.14).
- Gemini API key từ [Google AI Studio](https://aistudio.google.com/) (không bắt buộc).
- Có Internet: STT dùng Google Web Speech API, Gemini và Edge-TTS đều gọi ra ngoài.
- Chỉ khi dùng ESP32: board ESP32-S3 (N16R8, có PSRAM), mic INMP441, loa MAX98357A, Arduino IDE.

## Cài đặt

```powershell
git clone git@github.com:ngodinhvinhluat/AutoBot.git
cd AutoBot

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
```

Mở `.env` và điền API key (xem phần [Cấu hình](#cấu-hình)).

> Lần đầu dùng giọng VieNeu, `vieneu` sẽ tải model nên khởi động hơi lâu. Nếu không cần giọng cục bộ, đặt `TTS_ENGINE=edge_tts`.

## Chạy server

```powershell
python server.py
```

Hoặc dùng uvicorn trực tiếp:

```powershell
uvicorn server:app --host 0.0.0.0 --port 8000
```

Mặc định server nghe ở cổng `8000`. Muốn đổi cổng hoặc chạy thêm một instance: `$env:PORT=4500; python server.py`.

Mở Dashboard tại <http://localhost:8000/>.

## Kết nối thiết bị

### Cách 1: Client trên PC (dễ nhất, không cần phần cứng)

Dùng mic và loa của máy tính, trong terminal khác:

```powershell
python live_pc_client.py
```

Client mở `ws://localhost:8000/ws/audio?format=pcm&vad=auto`, thu mic, gửi lên server và phát lại giọng trả lời. Nó ghi log vào `voice_session.log`.

### Cách 2: ESP32-S3

1. Mở [esp32_full_duplex/esp32_full_duplex.ino](esp32_full_duplex/esp32_full_duplex.ino) trong Arduino IDE.
2. Cài thư viện: `WebSocketsClient` (arduinoWebSockets) và `U8g2`.
3. Sửa phần cấu hình ở đầu file:
   - `ssid` / `password`: WiFi của bạn.
   - `USE_NGROK`: `true` để đi qua Internet bằng domain ngrok (`wss`, cổng 443), `false` để nối thẳng IP LAN của máy chạy server (`ws`, cổng 8000).
   - `ws_host`: domain ngrok hoặc IPv4 của máy chạy server.
4. Chọn board ESP32-S3, bật **Tools → PSRAM → OPI PSRAM**, rồi nạp.

Sơ đồ chân mặc định:

| Thành phần | Chân |
|---|---|
| Mic INMP441 (WS / SCK / SD) | GPIO 4 / 5 / 6 |
| Loa MAX98357A (DIN / BCLK / LRC) | GPIO 7 / 15 / 16 |
| OLED (SDA / SCL) | GPIO 41 / 42 |
| Nút nhấn để nói (PTT) | GPIO 0 (nút BOOT) |

ESP32 hoạt động half-duplex: khóa mic khi đang phát tiếng AI để tránh vọng âm từ loa vào mic. Server gửi dữ liệu theo nhịp phát dựa vào dung lượng bộ đệm loa mà thiết bị báo qua tham số `buf`.

### Cách 3: Đưa server ra Internet bằng ngrok

Cần khi ESP32 không cùng mạng LAN với máy chạy server. Xem [NGROK_SUBSITE_GUIDE.md](NGROK_SUBSITE_GUIDE.md). Tóm tắt:

```powershell
ngrok start --all --config "$env:LOCALAPPDATA\ngrok\ngrok.yml" --config ngrok.yml
```

[ngrok.yml](ngrok.yml) chia một domain thành hai site theo đường dẫn: `/` đến cổng 8000 và `/b/` đến cổng 4500. Hãy đổi domain trong file cho đúng domain ngrok của bạn.

## Cấu hình

### `.env` (bí mật, không commit)

| Biến | Ý nghĩa |
|---|---|
| `GEMINI_API_KEY` | API key Gemini. Để trống thì server chạy bằng bộ xử lý rule-based. |
| `TTS_ENGINE` | `vieneu` (cục bộ) hoặc `edge_tts` (Microsoft Cloud) |
| `TTS_VOICE` | Giọng VieNeu: Ngọc Huyền, Thùy Dung, Trúc Ly, Mai Anh |
| `TTS_RATE` | Tốc độ đọc cho Edge-TTS, ví dụ `+0%` |

`.env` được đọc nóng mỗi lần cần key, nên đổi key không phải khởi động lại server.

### `settings.json` (cài đặt runtime)

Chỉnh được trực tiếp trên Dashboard hoặc qua `POST /api/settings`. Một số khóa chính:

| Khóa | Ý nghĩa |
|---|---|
| `tts_output`, `tts_engine`, `tts_stream` | Bật/tắt giọng đọc, chọn engine, bật streaming |
| `vieneu_voice`, `vieneu_precision` | Giọng và độ chính xác model VieNeu |
| `edge_voice`, `tts_rate`, `tts_boost_db` | Giọng Edge-TTS, tốc độ, tăng âm lượng |
| `gemini_model`, `gemini_thinking` | Model Gemini và mức suy luận |
| `history_turns` | Số lượt hội thoại giữ làm ngữ cảnh |
| `system_instruction` | Prompt hệ thống (tính cách, độ dài, định dạng câu trả lời) |
| `stt_language` | Ngôn ngữ nhận diện, mặc định `vi-VN` |
| `vad_*` | Tham số phát hiện tiếng nói (ngưỡng, thời gian im lặng, độ dài tối đa) |
| `debug_save_wav` | Lưu âm thanh từng lượt vào `recordings/` để chẩn đoán |

Nếu chưa có `settings.json`, server dùng giá trị mặc định trong [autobot/config/defaults.py](autobot/config/defaults.py).

## API

| Route | Mô tả |
|---|---|
| `GET /` | Dashboard |
| `GET /api/state` | Trạng thái realtime: chế độ AI, hội thoại, sự kiện, thiết bị đang kết nối |
| `GET` / `POST /api/settings` | Đọc / cập nhật cài đặt |
| `POST /api/action/{name}` | `reset_context`, `clear_history`, `reset_settings`, `test_tts` |
| `WS /ws/audio` | Kênh âm thanh hai chiều với thiết bị |

Tham số query của `/ws/audio`:

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `format` | `pcm` / `mp3` (mặc định `mp3`) | Định dạng âm thanh server gửi về |
| `vad` | `auto` / `manual` (mặc định `manual`) | `auto`: server tự ngắt câu. `manual`: thiết bị gửi `END_OF_SPEECH` khi nói xong |
| `tts` | `on` / `off` | Ép bật/tắt giọng đọc cho kết nối này |
| `buf` | số byte | Dung lượng bộ đệm loa của thiết bị, để server gửi theo nhịp phát |
| `dev` | chuỗi | Định danh thiết bị (ESP32 dùng MAC) để giữ ngữ cảnh hội thoại |

## Kiểm thử

Cần server đang chạy ở `localhost:8000`.

```powershell
# Giả lập ESP32: thu mic 4 giây, gửi lên, nhận và phát lại giọng trả lời
python mock_esp32_client.py

# Test end-to-end toàn pipeline
python test_pipeline_e2e.py
```

## Cấu trúc thư mục

```
server.py                 Điểm khởi chạy
autobot/
  main.py                 Tạo app FastAPI, warm-up model, chạy uvicorn
  api/                    Dashboard, settings, WebSocket /ws/audio
  audio/                  VAD, DSP (lọc DC/high-pass), chẩn đoán âm thanh
  stt/                    Nhận diện giọng nói (Google)
  ai/                     Gemini client, chat streaming, fallback rule-based
  tts/                    VieNeu, Edge-TTS, synthesizer, phát lại
  conversation/           Pipeline hội thoại, lịch sử, phiên theo thiết bị, tách mệnh đề
  config/                 Giá trị mặc định, schema, lưu/đọc settings.json
  devices/                Registry thiết bị đang kết nối
  core/                   Đường dẫn, logger, đọc .env, định dạng âm thanh
  web/                    Giao diện Dashboard (HTML/CSS/JS)
esp32_full_duplex/        Firmware ESP32-S3 đầy đủ (mic + loa + OLED)
esp32_audio_test/         Sketch thử âm thanh đơn giản
live_pc_client.py         Client rảnh tay chạy trên PC
mock_esp32_client.py      Giả lập ESP32 bằng mic PC
test_pipeline_e2e.py      Test end-to-end
.backup/                  Bản mã cũ trước khi tách module
```

## Xử lý sự cố

- **Dashboard báo "Chưa điền GEMINI_API_KEY"**: kiểm tra `.env` có đúng tên biến và key dài từ 20 ký tự trở lên.
- **Khởi động chậm 15-25 giây**: server đang nạp model VieNeu. Tắt `tts_output` hoặc dùng `edge_tts` để khởi động nhanh hơn.
- **Không nghe ra chữ / hay cắt câu**: chỉnh các tham số `vad_*` trong `settings.json`. Bật `debug_save_wav` để nghe lại âm thanh server nhận được trong `recordings/`.
- **Tiếng ESP32 bị giật**: kiểm tra đã bật OPI PSRAM khi nạp firmware (không có PSRAM thì bộ đệm chỉ còn khoảng 1 giây).
- **Mic ESP32 chỉ thu nhiễu 50Hz**: kiểm tra dây SD của INMP441 và chân L/R, không để thả nổi.
