/*
 * ======================================================================================
 * HỆ THỐNG ĐÀM THOẠI GIỌNG NÓI HAI CHIỀU THEO NGỮ CẢNH TRÊN ESP32 (BƯỚC 3, 4, 5)
 * Phiên bản tối ưu Độ trễ (Streaming Chunk TTS + FreeRTOS RingBuffer + Lọc DC/High-Pass)
 * ======================================================================================
 * - Bước 3: Tự động phát hiện tiếng nói & ngắt câu rảnh tay (Auto-VAD) hoặc nhấn nút BOOT.
 *           Cơ chế Half-Duplex khóa Mic khi đang phát AI và chỉ mở lại sau khi RingBuffer
 *           đã phát hết gói âm thanh cuối cùng (tránh vọng âm từ Loa vào Mic).
 * - Bước 4: Nhận các gói PCM Chunk (2KB, 16kHz, 16-bit, Mono) cuốn chiếu từ Server,
 *           đẩy vào FreeRTOS RingBuffer (512KB trên PSRAM ~16s; không có PSRAM thì 32KB) và phát
 *           ra I2S (MAX98357A) trên tiến trình riêng. Dung lượng bộ đệm được báo cho Server qua URL
 *           (?buf=...) để Server gửi theo nhịp phát, không đổ dồn làm tràn bộ đệm (nguyên nhân giật tiếng).
 *           Cần bật PSRAM khi nạp: Tools > PSRAM > "OPI PSRAM" (ESP32-S3 N16R8).
 * - Bước 5: Đàm thoại nhiều lượt giữ nguyên ngữ cảnh cuộc trò chuyện trên Gemini AI Server.
 * ======================================================================================
 */

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/ringbuf.h>
#include <esp_heap_caps.h>
#include <Wire.h>
#include <U8g2lib.h>

// ================= 1. CẤU HÌNH WIFI & SERVER =================
const char* ssid = "NOIS-FLOOR-4";
const char* password = "BrightCoff@e2026";
// USE_NGROK = true : đi qua Internet bằng domain ngrok (wss, cổng 443) - ESP32 không cần chung mạng LAN với máy chạy server.
//                    Trên máy chạy server.py: ngrok http --url=https://unexpired-dugout-bush.ngrok-free.dev 8000
// USE_NGROK = false: kết nối thẳng IP LAN (ws, cổng 8000)
#define USE_NGROK true
#if USE_NGROK
const char* ws_host = "unexpired-dugout-bush.ngrok-free.dev";
const uint16_t ws_port = 443;
#else
const char* ws_host = "172.16.30.40"; // Thay bằng IPv4 máy tính chạy server.py (VD: 172.16.30.40 hoặc 172.17.201.232)
const uint16_t ws_port = 8000;
#endif

// Kết nối ở chế độ PCM + Auto-VAD (đàm thoại rảnh tay + nhận Streaming Chunk TTS)
const char* ws_path = "/ws/audio?format=pcm&vad=auto";
// Lúc kết nối tự nối thêm &buf=<dung lượng bộ đệm loa>&dev=<MAC> (Server giữ ngữ cảnh hội thoại theo thiết bị)
char wsFullPath[128];

// ================= 2. CHẾ ĐỘ PHẦN CỨNG =================
// Đặt false khi đã lắp Mic I2S (INMP441) và Mạch Loa/Tai nghe I2S (MAX98357A / PCM5102)
// Đặt true khi chỉ có bo mạch ESP32 trần (test bằng chân ADC 34 & xuất tai nghe qua DAC GPIO 25)
#define USE_MOCK_HARDWARE false

// Chân nút nhấn đàm thoại tùy chọn (Nút BOOT có sẵn trên mọi mạch ESP32)
#define PTT_BUTTON_PIN 0

// ================= 3. CẤU HÌNH I2S MIC (INMP441 - I2S_NUM_0) - theo sơ đồ ESP32-S3 N16R8 =================
// INMP441: L/R -> GND (kênh trái), VDD -> 3V3
#define I2S_MIC_WS   4
#define I2S_MIC_SCK  5
#define I2S_MIC_SD   6
#define I2S_MIC_PORT I2S_NUM_0

// ================= 4. CẤU HÌNH I2S LOA (MAX98357A - I2S_NUM_1) - theo sơ đồ ESP32-S3 N16R8 =================
#define I2S_SPK_DIN  7
#define I2S_SPK_BCLK 15
#define I2S_SPK_LRC  16
#define I2S_SPK_PORT I2S_NUM_1

// ================= 4b. MÀN HÌNH OLED SSD1306 128x64 (I2C) =================
#define OLED_SDA 41
#define OLED_SCL 42
#define VOICE_LEVEL_THRESHOLD 2500      // Biên độ đỉnh (sau lọc) coi là "có tiếng nói" - chỉnh theo phòng thực tế

// ================= 5. THÔNG SỐ ÂM THANH & BỘ ĐỆM STREAMING =================
#define SAMPLE_RATE       16000
#define CHUNK_SAMPLES     1024          // 1024 mẫu 16-bit = 2048 bytes (~64ms mỗi gói, đồng bộ với PC Client)
#define RINGBUF_SIZE_PSRAM    (512 * 1024)  // Bộ đệm loa trên PSRAM: ~16 giây âm thanh
#define RINGBUF_SIZE_INTERNAL (32 * 1024)   // Dự phòng khi không có PSRAM: ~1 giây âm thanh
#define MIC_GAIN_FACTOR   2             // Hệ số khuếch đại giọng thu từ INMP441
#define ADC_PIN           1             // Chỉ dùng ở chế độ mock (ESP32-S3 không có GPIO34)

WebSocketsClient webSocket;
RingbufHandle_t spkRingBuf = NULL;
size_t spkRingBufSize = 0;

int16_t micPcmBuffer[CHUNK_SAMPLES];
// Đọc STEREO khung 32-bit (2 khe L/R xen kẽ) rồi chỉ lấy khe có Mic thật -> chuyển về 16-bit sạch
int32_t i2sRaw32Buffer[CHUNK_SAMPLES * 2];
uint8_t micSlot = 0;                   // Khe (0/1) có dữ liệu INMP441 thật - tự dò lúc khởi động

volatile bool isConnected = false;
volatile bool isPlayingTTS = false;       // Khóa thu Mic khi đang chờ hoặc đang phát giọng AI
volatile bool endOfTtsReceived = false;   // Báo hiệu Server đã gửi xong gói PCM cuối cùng của lượt
volatile uint32_t turnRxBytes = 0;        // Số byte PCM đã nhận VÀ đưa được vào bộ đệm loa trong lượt
volatile uint32_t turnDroppedBytes = 0;   // Số byte PCM phải bỏ vì bộ đệm đầy (bình thường phải = 0)
volatile unsigned long ttsStartMillis = 0;
volatile bool firstChunkLogged = false;

bool lastButtonState = HIGH;

// ================= TRẠNG THÁI HIỂN THỊ OLED =================
U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE, OLED_SCL, OLED_SDA);

enum BotState : uint8_t { ST_BOOT, ST_WIFI, ST_WS_CONNECTING, ST_LISTENING, ST_THINKING, ST_SPEAKING };
volatile BotState botState = ST_BOOT;
volatile unsigned long stateSinceMs = 0;
volatile uint16_t micPeakLevel = 0;      // Biên độ đỉnh của gói Mic gần nhất (0..32767)
volatile unsigned long lastVoiceMs = 0;  // Lần cuối nghe thấy tiếng vượt ngưỡng
volatile unsigned long pttSentMs = 0;    // Lần cuối nhấn/nhả nút BOOT gửi END_OF_SPEECH
volatile uint32_t turnCount = 0;         // Số lượt AI đã trả lời xong
volatile uint32_t lastTtfaMs = 0;        // Độ trễ từ START_OF_TTS tới gói âm thanh đầu tiên
char ipStr[16] = "-";
volatile uint8_t wifiProblem = 0;        // 0: chưa rõ, 1: không thấy mạng, 2: thấy mạng nhưng không vào được
volatile uint32_t turnPlayedBytes = 0;   // Số byte PCM đã thực sự phát ra loa trong lượt hiện tại

inline void setState(BotState s) {
  botState = s;
  stateSinceMs = millis();
}

// ================= NỘI DUNG HỘI THOẠI (Server gửi "USER:..." / "AI:...") =================
// Mỗi mệnh đề AI được gắn vị trí byte PCM bắt đầu của nó -> OLED chỉ hiện chữ khi loa phát tới đó.
#define TEXT_MAX      192
#define AI_CLAUSE_MAX 8
portMUX_TYPE textMux = portMUX_INITIALIZER_UNLOCKED;
char userText[TEXT_MAX] = "";
char aiClauseText[AI_CLAUSE_MAX][TEXT_MAX];
uint32_t aiClauseStart[AI_CLAUSE_MAX];
uint32_t aiClauseCount = 0;              // Tổng số mệnh đề đã nhận trong lượt (vị trí ghi = count % MAX)

// Copy chuỗi UTF-8 có cắt an toàn (không cắt giữa ký tự nhiều byte)
void copyUtf8(char* dst, const char* src, size_t srcLen, size_t dstSize) {
  size_t n = min(srcLen, dstSize - 1);
  while (n > 0 && n < srcLen && ((uint8_t)src[n] & 0xC0) == 0x80) n--;
  memcpy(dst, src, n);
  dst[n] = '\0';
}

void resetTurnText() {
  portENTER_CRITICAL(&textMux);
  userText[0] = '\0';
  aiClauseCount = 0;
  portEXIT_CRITICAL(&textMux);
}

void setUserText(const char* s, size_t len) {
  char tmp[TEXT_MAX];
  copyUtf8(tmp, s, len, sizeof(tmp));
  portENTER_CRITICAL(&textMux);
  memcpy(userText, tmp, sizeof(tmp));
  portEXIT_CRITICAL(&textMux);
}

void addAiClause(const char* s, size_t len, uint32_t startByte) {
  char tmp[TEXT_MAX];
  copyUtf8(tmp, s, len, sizeof(tmp));
  portENTER_CRITICAL(&textMux);
  uint32_t idx = aiClauseCount % AI_CLAUSE_MAX;
  memcpy(aiClauseText[idx], tmp, sizeof(tmp));
  aiClauseStart[idx] = startByte;
  aiClauseCount++;
  portEXIT_CRITICAL(&textMux);
}

// Lấy mệnh đề AI đang được phát (theo số byte đã phát) + tiến độ đọc mệnh đề đó (0..1)
bool getCurrentAiClause(char* out, float* progress) {
  uint32_t played = turnPlayedBytes;
  uint32_t received = turnRxBytes;
  bool found = false;
  uint32_t start = 0, end = 0;
  portENTER_CRITICAL(&textMux);
  uint32_t count = aiClauseCount;
  uint32_t first = count > AI_CLAUSE_MAX ? count - AI_CLAUSE_MAX : 0;
  for (uint32_t k = count; k > first; k--) {
    uint32_t idx = (k - 1) % AI_CLAUSE_MAX;
    if (aiClauseStart[idx] <= played || k - 1 == first) {
      memcpy(out, aiClauseText[idx], TEXT_MAX);
      start = aiClauseStart[idx];
      end = (k < count) ? aiClauseStart[k % AI_CLAUSE_MAX] : received;
      found = true;
      break;
    }
  }
  portEXIT_CRITICAL(&textMux);
  if (!found) return false;
  *progress = (end > start) ? constrain((float)(played - min(played, start)) / (float)(end - start), 0.0f, 1.0f) : 0.0f;
  return true;
}

// Bộ lọc thông cao Butterworth bậc 2 @150Hz (fs=16kHz): -19dB@50Hz, -8dB@100Hz, -0.3dB@300Hz
// -> loại DC lệch của INMP441 + tiếng ù điện lưới/ầm thấp (chiếm ~80% năng lượng thu được) mà không ăn vào giọng nói
const float HP_B0 = 0.959203150f, HP_B1 = -1.918406299f, HP_B2 = 0.959203150f;
const float HP_A1 = -1.916741223f, HP_A2 = 0.920071375f;
float hpX1 = 0.0f, hpX2 = 0.0f, hpY1 = 0.0f, hpY2 = 0.0f;

// =====================================================================
// BỘ LỌC KHỬ NHIỄU NỀN DC & TẦN SỐ THẤP TRÊN ESP32
// =====================================================================
// Nhận mẫu dạng số thực (thang 16-bit, còn giữ phần lẻ của 24-bit) -> lọc -> nhân gain -> làm tròn 1 lần cuối
inline int16_t filterSampleHighPass(float xf) {
  // Biquad Direct Form I: y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
  float yf = HP_B0 * xf + HP_B1 * hpX1 + HP_B2 * hpX2 - HP_A1 * hpY1 - HP_A2 * hpY2;
  hpX2 = hpX1; hpX1 = xf;
  hpY2 = hpY1; hpY1 = yf;
  int32_t out = (int32_t)lroundf(yf * MIC_GAIN_FACTOR);
  if (out > 32767) out = 32767;
  if (out < -32768) out = -32768;
  return (int16_t)out;
}

// =====================================================================
// KHỞI TẠO I2S MICROPHONE (INMP441) & I2S LOA/TAI NGHE (MAX98357A)
// =====================================================================
void setupI2SMic() {
  const i2s_config_t i2s_config = {
    .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT, // INMP441 xuất dữ liệu chuẩn 24-bit trong khung 32-bit
    // Đọc cả 2 khe: ONLY_LEFT của driver I2S cũ có thể đọc nhầm khe trống (SD thả nổi -> chỉ thu nhiễu 50Hz)
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 6,
    .dma_buf_len = 512,
    .use_apll = false
  };
  const i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_MIC_SCK,
    .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_MIC_SD
  };
  i2s_driver_install(I2S_MIC_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_MIC_PORT, &pin_config);
}

// Tự dò khe nào có Mic thật: Mic 24-bit luôn có nhiễu nền nên mẫu liền kề hầu như luôn khác nhau,
// còn khe trống (SD thả nổi) cho giá trị đứng yên rất lâu rồi mới nhảy bậc.
void detectMicSlot() {
  uint32_t total[2] = {0, 0}, same[2] = {0, 0};
  int32_t prev[2] = {0, 0};
  double sumAbs[2] = {0, 0};
  for (int r = 0; r < 12; r++) {                    // ~0.75s dữ liệu
    size_t bytesIn = 0;
    i2s_read(I2S_MIC_PORT, i2sRaw32Buffer, sizeof(i2sRaw32Buffer), &bytesIn, pdMS_TO_TICKS(200));
    size_t frames = bytesIn / (2 * sizeof(int32_t));
    if (r < 2) continue;                            // Bỏ phần đầu khi Mic vừa khởi động
    for (size_t i = 0; i < frames; i++) {
      for (int s = 0; s < 2; s++) {
        int32_t v = i2sRaw32Buffer[2 * i + s] >> 8;
        if (total[s] > 0 && v == prev[s]) same[s]++;
        sumAbs[s] += abs(v);
        prev[s] = v;
        total[s]++;
      }
    }
  }
  float sameRatio[2];
  for (int s = 0; s < 2; s++) {
    sameRatio[s] = total[s] ? (float)same[s] / total[s] : 1.0f;
    Serial.printf("[MIC] Khe %d: %lu mẫu | mẫu liền kề trùng nhau %.1f%% | |giá trị| TB %.0f\n",
                  s, (unsigned long)total[s], sameRatio[s] * 100.0f, total[s] ? sumAbs[s] / total[s] : 0.0);
  }
  micSlot = (sameRatio[1] < sameRatio[0]) ? 1 : 0;
  Serial.printf("[MIC] => Dùng khe %d làm dữ liệu INMP441%s\n", micSlot,
                (sameRatio[micSlot] > 0.5f) ? "  (CẢNH BÁO: cả 2 khe đều giống khe trống - kiểm tra dây SD/WS/SCK & nguồn Mic!)" : "");
}

void setupI2SSpeaker() {
  const i2s_config_t spk_config = {
    .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    // Phát STEREO (mẫu mono nhân đôi ra 2 khe): MAX98357A mặc định trộn (L+R)/2 -> nếu chỉ có kênh trái
    // thì loa mất một nửa âm lượng (-6dB). Nhân đôi đảm bảo đủ âm lượng với mọi cấu hình chân SD.
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 512,
    .use_apll = false,
    .tx_desc_auto_clear = true
  };
  const i2s_pin_config_t spk_pins = {
    .bck_io_num = I2S_SPK_BCLK,
    .ws_io_num = I2S_SPK_LRC,
    .data_out_num = I2S_SPK_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(I2S_SPK_PORT, &spk_config, 0, NULL);
  i2s_set_pin(I2S_SPK_PORT, &spk_pins);
  i2s_zero_dma_buffer(I2S_SPK_PORT);
}

// =====================================================================
// MÀN HÌNH OLED: HIỂN THỊ TRẠNG THÁI (TASK RIÊNG TRÊN CORE 0, ~10 FPS)
// =====================================================================
bool setupOled() {
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.setClock(400000);
  uint8_t addr = 0;
  for (uint8_t a : {0x3C, 0x3D}) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { addr = a; break; }
  }
  if (addr == 0) {
    Serial.println("[OLED] Không tìm thấy màn hình SSD1306 (0x3C/0x3D) - bỏ qua hiển thị.");
    return false;
  }
  oled.setI2CAddress(addr << 1);
  oled.setBusClock(400000);
  oled.begin();
  Serial.printf("[OLED] Đã tìm thấy màn hình tại 0x%02X\n", addr);
  return true;
}

// Bố cục 128x64:  [0..10] thanh tiêu đề: địa chỉ Server + vạch sóng WiFi (font nhỏ)
//                 [12..59] 3 dòng tiếng Việt có dấu (Unifont 16px)
//                 [61..63] vạch mỏng: mức âm Mic / tiến độ đọc / đang xử lý
#define FONT_VI    u8g2_font_unifont_t_vietnamese2
#define LINE_Y(i)  (12 + (i) * 16)
#define WRAP_MAX_LINES 16

// Ngắt dòng chuỗi UTF-8 theo độ rộng pixel (ưu tiên ngắt ở dấu cách)
int wrapUtf8(const char* text, int maxW, uint16_t* ls, uint16_t* ll) {
  char tmp[TEXT_MAX + 8];
  size_t len = strlen(text), pos = 0;
  int n = 0;
  while (pos < len && n < WRAP_MAX_LINES) {
    while (pos < len && text[pos] == ' ') pos++;
    if (pos >= len) break;
    size_t best = 0, i = pos;
    while (i <= len) {                                   // Thêm từng từ khi còn vừa dòng
      size_t j = i;
      while (j < len && text[j] != ' ') j++;
      memcpy(tmp, text + pos, j - pos); tmp[j - pos] = '\0';
      if (oled.getUTF8Width(tmp) > maxW) break;
      best = j - pos;
      if (j >= len) break;
      i = j + 1;
    }
    if (best == 0) {                                     // Một từ dài hơn cả dòng -> cắt theo ký tự
      size_t j = pos;
      while (j < len) {
        size_t k = j + 1;
        while (k < len && ((uint8_t)text[k] & 0xC0) == 0x80) k++;
        memcpy(tmp, text + pos, k - pos); tmp[k - pos] = '\0';
        if (oled.getUTF8Width(tmp) > maxW && j > pos) break;
        j = k;
      }
      best = j - pos;
    }
    ls[n] = pos; ll[n] = best; n++;
    pos += best;
  }
  return n;
}

// Vẽ đoạn văn bản đã ngắt dòng vào các dòng [row0 .. row0+rows-1], bắt đầu từ dòng 'top'
void drawWrapped(const char* text, int row0, int rows, int top, const uint16_t* ls, const uint16_t* ll, int n) {
  char tmp[TEXT_MAX + 8];
  for (int r = 0; r < rows && top + r < n; r++) {
    memcpy(tmp, text + ls[top + r], ll[top + r]);
    tmp[ll[top + r]] = '\0';
    oled.drawUTF8(0, LINE_Y(row0 + r), tmp);
  }
}

void drawWifiBars(int x, int yBottom) {
  int level = 0;
  if (WiFi.status() == WL_CONNECTED) {
    int rssi = WiFi.RSSI();
    level = rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : rssi > -85 ? 1 : 0;
  }
  for (int b = 0; b < 4; b++) {
    int h = 2 + b * 2;
    if (b < level) oled.drawBox(x + b * 3, yBottom - h, 2, h);
    else oled.drawPixel(x + b * 3, yBottom - 1);
  }
}

void oledTask(void* parameter) {
  const char spinner[] = "|/-\\";
  uint8_t spin = 0;
  float vu = 0.0f;                 // Mức VU đã làm mượt (0..1)
  char line[64];
  char text[TEXT_MAX + 16];
  uint16_t ls[WRAP_MAX_LINES], ll[WRAP_MAX_LINES];

  while (true) {
    unsigned long now = millis();
    BotState st = botState;
    unsigned long inStateMs = now - stateSinceMs;
    bool voiceNow = (st == ST_LISTENING) && (now - lastVoiceMs < 400);
    spin = (spin + 1) & 3;

    // VU meter theo thang dB (-60dB .. 0dB), giữ đỉnh và tụt dần
    float norm = 0.0f;
    if (st == ST_LISTENING && micPeakLevel > 0) {
      float db = 20.0f * log10f((float)micPeakLevel / 32767.0f);
      norm = constrain((db + 60.0f) / 60.0f, 0.0f, 1.0f);
    }
    vu = max(norm, vu * 0.75f);

    oled.clearBuffer();

    // ---------- Thanh tiêu đề: Server đang kết nối + sóng WiFi ----------
    // Nền sáng = đã kết nối Server; viền rỗng = chưa/mất kết nối
    oled.setFont(u8g2_font_6x10_tf);
#if USE_NGROK
    snprintf(line, sizeof(line), "ngrok wss");      // domain quá dài cho thanh tiêu đề 128px
#else
    snprintf(line, sizeof(line), "%s:%u", ws_host, ws_port);
#endif
    if (isConnected) {
      oled.drawBox(0, 0, 128, 11);
      oled.setDrawColor(0);
    } else {
      oled.drawFrame(0, 0, 128, 11);
    }
    oled.drawStr(2, 9, line);
    drawWifiBars(116, 10);
    oled.setDrawColor(1);

    // ---------- 3 dòng tiếng Việt ----------
    oled.setFont(FONT_VI);
    oled.setFontPosTop();
    float barFrac = -1.0f;         // <0: không vẽ vạch tiến độ

    switch (st) {
      case ST_BOOT:
        oled.drawUTF8(0, LINE_Y(0), "Khởi động...");
        break;

      case ST_WIFI:
        snprintf(line, sizeof(line), "Kết nối WiFi %c", spinner[spin]);
        oled.drawUTF8(0, LINE_Y(0), line);
        oled.drawUTF8(0, LINE_Y(1), ssid);
        if (wifiProblem == 1) oled.drawUTF8(0, LINE_Y(2), "Không thấy mạng!");
        else if (wifiProblem == 2) oled.drawUTF8(0, LINE_Y(2), "Sai mật khẩu?");
        break;

      case ST_WS_CONNECTING:
        snprintf(line, sizeof(line), "Chờ server %c", spinner[spin]);
        oled.drawUTF8(0, LINE_Y(0), line);
        snprintf(line, sizeof(line), "IP %s", ipStr);
        oled.drawUTF8(0, LINE_Y(1), line);
        oled.drawUTF8(0, LINE_Y(2), "Thử lại mỗi 3s");
        break;

      case ST_LISTENING:
        oled.drawUTF8(0, LINE_Y(0), voiceNow ? "Bạn đang nói..." : "Đang nghe...");
        snprintf(line, sizeof(line), "IP %s", ipStr);
        oled.drawUTF8(0, LINE_Y(1), line);
        if (now - pttSentMs < 1500) {
          oled.drawUTF8(0, LINE_Y(2), "Đã gửi hết câu");
        } else if (turnCount == 0) {
          oled.drawUTF8(0, LINE_Y(2), "Mời bạn nói!");
        } else {
          snprintf(line, sizeof(line), "Lượt %lu|TTFA%.1fs", (unsigned long)turnCount, lastTtfaMs / 1000.0f);
          oled.drawUTF8(0, LINE_Y(2), line);
        }
        // Vạch VU: biết ngay Mic có thu được tiếng hay không
        oled.drawBox(0, 61, (int)(128 * vu), 3);
        break;

      case ST_THINKING: {
        portENTER_CRITICAL(&textMux);
        bool hasUser = userText[0] != '\0';
        snprintf(text, sizeof(text), "Bạn: %s", userText);
        portEXIT_CRITICAL(&textMux);

        snprintf(line, sizeof(line), "%s %.1fs", hasUser ? "Đang nghĩ" : "Nhận dạng", inStateMs / 1000.0f);
        oled.drawUTF8(0, LINE_Y(0), line);
        if (hasUser) {
          // Câu bạn nói: 2 dòng, dài hơn thì tự lật trang mỗi 1.5s
          int n = wrapUtf8(text, 128, ls, ll);
          int top = (n > 2) ? (int)((inStateMs / 1500) % (unsigned long)(n - 1)) : 0;
          drawWrapped(text, 1, 2, top, ls, ll, n);
        }
        // Vạch chạy qua lại: đang xử lý
        int x = (int)((now / 8) % 216);
        if (x > 108) x = 216 - x;
        oled.drawBox(x, 61, 20, 3);
        break;
      }

      case ST_SPEAKING: {
        float progress = 0.0f;
        char clause[TEXT_MAX];
        if (getCurrentAiClause(clause, &progress)) {
          // Chữ AI chạy theo đúng tiến độ âm thanh đã phát ra loa
          snprintf(text, sizeof(text), "AI: %s", clause);
          int n = wrapUtf8(text, 128, ls, ll);
          int cur = (int)(progress * n);
          int top = constrain(cur - 1, 0, max(0, n - 3));
          drawWrapped(text, 0, 3, top, ls, ll, n);
          barFrac = progress;
        } else {
          // Server cũ chưa gửi chữ "AI:..." -> chỉ hiện thông số
          oled.drawUTF8(0, LINE_Y(0), "AI đang nói...");
          snprintf(line, sizeof(line), "TTFA %lums", (unsigned long)lastTtfaMs);
          oled.drawUTF8(0, LINE_Y(1), line);
          snprintf(line, sizeof(line), "Đã nhận %.1fs", (float)turnRxBytes / (SAMPLE_RATE * 2.0f));
          oled.drawUTF8(0, LINE_Y(2), line);
        }
        break;
      }
    }

    if (barFrac >= 0.0f) oled.drawBox(0, 61, (int)(128 * barFrac), 3);
    oled.setFontPosBaseline();

    oled.sendBuffer();
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

// =====================================================================
// TASK PHÁT ÂM THANH STREAMING CHUNK (CHẠY SONG SONG TRÊN FREERTOS)
// =====================================================================
void speakerPlayTask(void* parameter) {
  while (true) {
    size_t itemSize = 0;
    uint8_t* item = (uint8_t*)xRingbufferReceiveUpTo(
      spkRingBuf,
      &itemSize,
      pdMS_TO_TICKS(20),
      2048
    );

    if (item != NULL && itemSize > 0) {
      if (!USE_MOCK_HARDWARE) {
        // Nhân đôi mẫu mono -> khung stereo (L = R) để MAX98357A phát đủ âm lượng
        static int16_t stereoBuf[2048];                 // 1024 mẫu mono (2048 byte) -> 2048 mẫu stereo
        const int16_t* mono = (const int16_t*)item;
        size_t n = itemSize / 2;
        for (size_t i = 0; i < n; i++) {
          stereoBuf[2 * i] = mono[i];
          stereoBuf[2 * i + 1] = mono[i];
        }
        size_t bytesWritten = 0;
        i2s_write(I2S_SPK_PORT, stereoBuf, n * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
        turnPlayedBytes += itemSize;
      } else {
        turnPlayedBytes += itemSize;
#if SOC_DAC_SUPPORTED   // ESP32-S3 không có DAC -> bỏ qua nhánh mock
        int16_t* samples = (int16_t*)item;
        size_t sampleCount = itemSize / 2;
        for (size_t i = 0; i < sampleCount; i++) {
          uint8_t dacVal = (uint8_t)(((int32_t)samples[i] + 32768) >> 8);
          dacWrite(25, dacVal);
          delayMicroseconds(62);
        }
#endif
      }
      vRingbufferReturnItem(spkRingBuf, (void*)item);
    } else {
      // Khi RingBuffer đã cạn sạch VÀ Server đã báo END_OF_TTS -> Mở lại Mic
      if (isPlayingTTS && endOfTtsReceived) {
        if (!USE_MOCK_HARDWARE) {
          i2s_zero_dma_buffer(I2S_SPK_PORT);
        }
        vTaskDelay(pdMS_TO_TICKS(150)); // Chờ 150ms triệt tiêu âm vang phòng trước khi mở lại Mic

        // Xóa dữ liệu tồn đọng trong DMA của Mic trong lúc Loa phát
        if (!USE_MOCK_HARDWARE) {
          size_t dummyBytes = 0;
          i2s_read(I2S_MIC_PORT, &i2sRaw32Buffer, sizeof(i2sRaw32Buffer), &dummyBytes, pdMS_TO_TICKS(5));
        }

        float durationSec = (float)turnRxBytes / (SAMPLE_RATE * 2.0f);
        Serial.printf("[MIC_OPEN] Đã phát xong toàn bộ luồng (%.2fs - %u bytes, bỏ %u bytes do đầy bộ đệm). Mở lại Mic chờ bạn nói tiếp.\n",
                      durationSec, (unsigned)turnRxBytes, (unsigned)turnDroppedBytes);
        turnRxBytes = 0;
        turnDroppedBytes = 0;
        endOfTtsReceived = false;
        isPlayingTTS = false;
        turnCount++;
        micPeakLevel = 0;
        setState(ST_LISTENING);
      }
    }
  }
}

// =====================================================================
// XỬ LÝ SỰ KIỆN WEBSOCKET (NHẬN STREAMING CHUNK TỪ SERVER)
// =====================================================================
void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      Serial.println("[WS_DISCONNECT] Đã mất kết nối tới Server!");
      isConnected = false;
      isPlayingTTS = false;
      endOfTtsReceived = false;
      setState(ST_WS_CONNECTING);
      break;

    case WStype_CONNECTED:
      Serial.printf("[WS_CONNECT] Đã kết nối Server (%s:%u%s) | Sẵn sàng đàm thoại Streaming!\n", ws_host, ws_port, wsFullPath);
      isConnected = true;
      isPlayingTTS = false;
      endOfTtsReceived = false;
      setState(ST_LISTENING);
      break;

    case WStype_TEXT: {
      // Nội dung hội thoại để hiển thị OLED: "USER:<câu bạn nói>" / "AI:<mệnh đề sắp phát>"
      if (length > 5 && memcmp(payload, "USER:", 5) == 0) {
        setUserText((const char*)payload + 5, length - 5);
        Serial.printf("[USER] %.*s\n", (int)(length - 5), (const char*)payload + 5);
        break;
      }
      if (length > 3 && memcmp(payload, "AI:", 3) == 0) {
        addAiClause((const char*)payload + 3, length - 3, turnRxBytes);
        Serial.printf("[AI] %.*s\n", (int)(length - 3), (const char*)payload + 3);
        break;
      }

      String msg = String((char*)payload);
      if (msg == "START_OF_TTS") {
        isPlayingTTS = true;
        endOfTtsReceived = false;
        turnRxBytes = 0;
        turnDroppedBytes = 0;
        turnPlayedBytes = 0;
        resetTurnText();
        firstChunkLogged = false;
        ttsStartMillis = millis();
        micPeakLevel = 0;
        setState(ST_THINKING);
        Serial.println("\n[START_OF_TTS] Tạm khóa Mic -> Đang xử lý và chờ Chunk âm thanh đầu tiên...");
      } else if (msg == "END_OF_TTS") {
        endOfTtsReceived = true;
      }
      break;
    }

    case WStype_BIN: {
      // Nhận gói PCM 2KB từ Server -> Đẩy ngay vào RingBuffer để phát cuốn chiếu
      if (!firstChunkLogged) {
        firstChunkLogged = true;
        unsigned long ttfaMs = millis() - ttsStartMillis;
        lastTtfaMs = ttfaMs;
        setState(ST_SPEAKING);
        Serial.printf("[HEADPHONE_STREAM_START] 🎧 Bắt đầu phát ngay gói Chunk #1 sau %lu ms!\n", ttfaMs);
      }
      if (spkRingBuf != NULL) {
        // Server gửi theo nhịp phát nên bộ đệm hầu như không bao giờ đầy. Nếu đầy thì CHỜ loa phát bớt
        // (tối đa 1s, TCP tự giữ nhịp) thay vì bỏ gói như trước (chờ 50ms rồi bỏ -> mất ~1/2 số gói -> giật tiếng).
        if (xRingbufferSend(spkRingBuf, payload, length, pdMS_TO_TICKS(1000)) == pdTRUE) {
          turnRxBytes += length;
        } else {
          turnDroppedBytes += length;
        }
      }
      break;
    }

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PTT_BUTTON_PIN, INPUT_PULLUP);

  // Khởi tạo RingBuffer 32KB kiểu BYTEBUFFER phục vụ Streaming Chunk TTS
  // Ưu tiên PSRAM (bộ đệm lớn), không có thì dùng RAM nội 32KB
  if (psramFound()) {
    spkRingBuf = xRingbufferCreateWithCaps(RINGBUF_SIZE_PSRAM, RINGBUF_TYPE_BYTEBUF, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (spkRingBuf != NULL) spkRingBufSize = RINGBUF_SIZE_PSRAM;
  }
  if (spkRingBuf == NULL) {
    spkRingBuf = xRingbufferCreate(RINGBUF_SIZE_INTERNAL, RINGBUF_TYPE_BYTEBUF);
    spkRingBufSize = (spkRingBuf != NULL) ? RINGBUF_SIZE_INTERNAL : 0;
    Serial.println("[SPK] Khong dung duoc PSRAM -> bo dem loa chi 32KB (~1s). Bat Tools > PSRAM > OPI PSRAM roi nap lai.");
  }
  Serial.printf("[SPK] Bo dem loa: %u KB (~%.1fs am thanh)\n", (unsigned)(spkRingBufSize / 1024),
                spkRingBufSize / (SAMPLE_RATE * 2.0f));

  // Khởi động OLED trước để hiển thị cả quá trình kết nối WiFi/Server
  stateSinceMs = millis();
  if (setupOled()) {
    xTaskCreatePinnedToCore(oledTask, "OledTask", 4096, NULL, 1, NULL, 0);
  }

  setState(ST_WIFI);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Tắt WiFi Modem Sleep để giảm tối đa độ trễ gói tin WebSocket!
  WiFi.begin(ssid, password);
  Serial.print("Đang kết nối WiFi");
  unsigned long wifiT0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - wifiT0 > 15000) {
      // Quá 15s chưa vào được mạng -> tự quét để biết nguyên nhân (không thấy mạng / sai mật khẩu)
      Serial.printf("\n[WIFI] Chưa kết nối được (status=%d). Đang quét mạng 2.4GHz...\n", WiFi.status());
      WiFi.disconnect();
      delay(100);
      int n = WiFi.scanNetworks();
      bool found = false;
      for (int i = 0; i < n; i++) {
        bool match = (WiFi.SSID(i) == ssid);
        found |= match;
        Serial.printf("  %-32s ch%-2d %4ddBm%s\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                      match ? "  <== mạng đang cấu hình" : "");
      }
      WiFi.scanDelete();
      wifiProblem = found ? 2 : 1;
      Serial.println(found ? "[WIFI] Có thấy mạng -> có thể sai mật khẩu hoặc router chặn thiết bị."
                           : "[WIFI] KHÔNG thấy mạng -> sai tên, mạng chỉ phát 5GHz, hoặc sóng quá yếu.");
      WiFi.begin(ssid, password);
      wifiT0 = millis();
    }
  }
  wifiProblem = 0;
  Serial.printf("\n✅ Đã kết nối WiFi! IP ESP32: %s | RSSI: %d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  strlcpy(ipStr, WiFi.localIP().toString().c_str(), sizeof(ipStr));
  setState(ST_WS_CONNECTING);

  if (!USE_MOCK_HARDWARE) {
    setupI2SMic();
    detectMicSlot();
    setupI2SSpeaker();
  } else {
    pinMode(ADC_PIN, INPUT);
  }

  // Khởi chạy Task phát âm thanh trên Core 1
  xTaskCreatePinnedToCore(
    speakerPlayTask,
    "SpeakerPlayTask",
    4096,
    NULL,
    2,
    NULL,
    1
  );

  // Báo dung lượng bộ đệm loa + mã thiết bị cho Server
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  snprintf(wsFullPath, sizeof(wsFullPath), "%s&buf=%u&dev=esp32-%s", ws_path, (unsigned)spkRingBufSize, mac.c_str());
#if USE_NGROK
  Serial.printf("[WS] wss://%s:%u%s\n", ws_host, ws_port, wsFullPath);
  webSocket.beginSSL(ws_host, ws_port, wsFullPath);  // TLS không kiểm tra CA (đủ cho thử nghiệm)
#else
  Serial.printf("[WS] ws://%s:%u%s\n", ws_host, ws_port, wsFullPath);
  webSocket.begin(ws_host, ws_port, wsFullPath);
#endif
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);
}

void loop() {
  webSocket.loop();
  if (!isConnected) return;

  // Hỗ trợ nhấn nút BOOT (GPIO 0) nếu muốn ngắt câu thủ công tức thì
  bool currentButtonState = digitalRead(PTT_BUTTON_PIN);
  if (lastButtonState == LOW && currentButtonState == HIGH) {
    Serial.println("\n[PTT] Đã nhả nút BOOT -> Gửi tín hiệu END_OF_SPEECH!");
    webSocket.sendTXT("END_OF_SPEECH");
    pttSentMs = millis();
  }
  lastButtonState = currentButtonState;

  // Thu âm từ Mic, lọc nhiễu DC/High-Pass và Stream từng gói 2KB lên Server
  if (!isPlayingTTS) {
    if (USE_MOCK_HARDWARE) {
      for (int i = 0; i < CHUNK_SAMPLES; i++) {
        int raw_adc = analogRead(ADC_PIN);
        int16_t sample16 = (int16_t)((raw_adc - 2048) * 8);
        micPcmBuffer[i] = filterSampleHighPass((float)sample16);
        delayMicroseconds(62);
      }
      webSocket.sendBIN((uint8_t*)micPcmBuffer, sizeof(micPcmBuffer));
    } else {
      size_t bytesIn = 0;
      // Đọc không chặn lâu (timeout 20ms) để webSocket.loop() luôn phản hồi tức thì
      esp_err_t err = i2s_read(I2S_MIC_PORT, &i2sRaw32Buffer, sizeof(i2sRaw32Buffer), &bytesIn, pdMS_TO_TICKS(20));
      if (err == ESP_OK && bytesIn > 0) {
        size_t samplesRead = bytesIn / (2 * sizeof(int32_t));   // Số khung stereo = số mẫu Mono
        int32_t peak = 0;
        for (size_t i = 0; i < samplesRead; i++) {
          // Dịch khung 32-bit của INMP441 sang 16-bit PCM và lọc nhiễu tần số thấp
          // INMP441: mẫu 24-bit nằm ở bit cao của khung 32-bit. Giữ nguyên 24-bit (>>8) rồi chia 256 bằng số thực
          // -> âm lượng tương đương >>16 (đủ nhỏ để VAD của Server ngắt câu được, không méo) nhưng KHÔNG mất
          // 8 bit thấp: các âm nhỏ/phụ âm (s, t, x, ch...) không bị làm tròn về 0 trước khi lọc -> STT nhận rõ hơn.
          float sample16 = (float)(i2sRaw32Buffer[2 * i + micSlot] >> 8) / 256.0f;
          micPcmBuffer[i] = filterSampleHighPass(sample16);
          int32_t a = abs((int32_t)micPcmBuffer[i]);
          if (a > peak) peak = a;
        }
        micPeakLevel = (uint16_t)min(peak, (int32_t)32767);
        if (peak > VOICE_LEVEL_THRESHOLD) lastVoiceMs = millis();
        webSocket.sendBIN((uint8_t*)micPcmBuffer, samplesRead * sizeof(int16_t));
      }
    }
  }
}
