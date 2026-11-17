/*
 * ======================================================================================
 * BƯỚC 2: FIRMWARE KIỂM THỬ SÓNG ÂM & LỌC NHIỄU TRÊN ESP32 (esp32_audio_test.ino)
 * ======================================================================================
 * - Truyền luồng Raw PCM (16kHz, 16-bit, Mono) theo gói 1024 mẫu (2KB) lên Server.
 * - Tích hợp bộ lọc High-Pass khử nhiễu DC và tắt WiFi Sleep để giảm độ trễ mạng.
 * ======================================================================================
 */

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>

// 1. CẤU HÌNH WIFI & SERVER
const char* ssid = "TEN_WIFI_CUA_BAN";
const char* password = "MAT_KHAU_WIFI";
const char* ws_host = "172.16.30.40"; // Thay bằng IP máy tính chạy server.py (VD: 172.16.30.40 hoặc 172.17.201.232)
const uint16_t ws_port = 8000;
const char* ws_path = "/ws/audio?format=pcm&vad=auto";

// 2. CHẾ ĐỘ KIỂM THỬ (Đặt true khi chưa có Mic thật, đặt false khi đã cắm Mic I2S INMP441)
#define USE_MOCK_ADC true
#define ADC_PIN 34

// 3. CẤU HÌNH CHÂN I2S (Cho INMP441)
#define I2S_WS 15
#define I2S_SD 32
#define I2S_SCK 14
#define I2S_PORT I2S_NUM_0
#define CHUNK_SAMPLES 1024

WebSocketsClient webSocket;
int16_t audioBuffer[CHUNK_SAMPLES];
int32_t i2sRaw32[CHUNK_SAMPLES];
bool isConnected = false;

float hpPrevX = 0.0f;
float hpPrevY = 0.0f;

inline int16_t filterSampleHighPass(int16_t x) {
  float xf = (float)x;
  float yf = xf - hpPrevX + 0.96f * hpPrevY;
  hpPrevX = xf;
  hpPrevY = yf;
  int32_t out = (int32_t)(yf * 2.0f);
  if (out > 32767) out = 32767;
  if (out < -32768) out = -32768;
  return (int16_t)out;
}

void setupI2S() {
  const i2s_config_t i2s_config = {
    .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 6,
    .dma_buf_len = 512,
    .use_apll = false
  };
  const i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD
  };
  i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_PORT, &pin_config);
}

void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {
  switch(type) {
    case WStype_DISCONNECTED:
      Serial.println("[WSc] Đã mất kết nối tới Server!");
      isConnected = false;
      break;
    case WStype_CONNECTED:
      Serial.println("[WSc] Đã kết nối Server! Bắt đầu truyền luồng sóng âm...");
      isConnected = true;
      break;
    case WStype_TEXT:
      Serial.printf("[WSc] Server nhắn: %s\n", payload);
      break;
    case WStype_BIN:
      Serial.printf("[WSc] Nhận được Chunk âm thanh phản hồi: %u bytes\n", length);
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Tắt WiFi Power Save để giảm độ trễ
  WiFi.begin(ssid, password);
  Serial.print("Đang kết nối WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n✅ Đã kết nối WiFi! IP ESP32: " + WiFi.localIP().toString());

  if (!USE_MOCK_ADC) {
    setupI2S();
  } else {
    pinMode(ADC_PIN, INPUT);
  }

  webSocket.begin(ws_host, ws_port, ws_path);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);
}

void loop() {
  webSocket.loop();

  if (isConnected) {
    if (USE_MOCK_ADC) {
      for (int i = 0; i < CHUNK_SAMPLES; i++) {
        int raw_adc = analogRead(ADC_PIN);
        int16_t sample16 = (int16_t)((raw_adc - 2048) * 8);
        audioBuffer[i] = filterSampleHighPass(sample16);
        delayMicroseconds(62);
      }
      webSocket.sendBIN((uint8_t*)audioBuffer, sizeof(audioBuffer));
    } else {
      size_t bytesIn = 0;
      esp_err_t err = i2s_read(I2S_PORT, &i2sRaw32, sizeof(i2sRaw32), &bytesIn, pdMS_TO_TICKS(20));
      if (err == ESP_OK && bytesIn > 0) {
        size_t samplesRead = bytesIn / sizeof(int32_t);
        for (size_t i = 0; i < samplesRead; i++) {
          int16_t raw16 = (int16_t)(i2sRaw32[i] >> 14);
          audioBuffer[i] = filterSampleHighPass(raw16);
        }
        webSocket.sendBIN((uint8_t*)audioBuffer, samplesRead * sizeof(int16_t));
      }
    }
  }
}
