/*
 * ESP32_TX (Transmitter / Sensor side)
 * ====================================
 * TENG 信号 → ADC 采集 → ESP-NOW 发送
 *
 * 烧录到：传感器端 ESP32（穿戴 / 装在传感器附近的那块）
 * 接线：
 *   GPIO34 ← 节点 ②（信号点）
 *   GND    ← 共地点
 *   电源：USB 或电池（VIN 引脚 5V）
 *
 * 配置前必做：
 *   1. 烧 01_get_mac_address.ino 到接收端 ESP32
 *   2. 串口监视器看到 MAC 地址
 *   3. 把 MAC 填到下面 receiverMac[] 数组里
 */

#include <esp_now.h>
#include <WiFi.h>
#include <esp_wifi.h>  

// ============ 配置区 ============
// ⚠️ 必须改成你自己接收端 ESP32 的 MAC 地址
uint8_t receiverMac[] = {0x78, 0x1C, 0x3C, 0x2D, 0x64, 0x54};

const int   ADC_PIN     = 34;        // ADC 输入引脚
const int   WIFI_CHANNEL = 1;        // WiFi 频道，TX 和 RX 必须一致
const bool  USE_LR_MODE  = false;    // 长距离模式（牺牲带宽换距离）
// ===============================

typedef struct {
  uint32_t seq;
  uint32_t timestamp_us;
  uint16_t adc_value;
} __attribute__((packed)) DataPacket;

uint32_t seqCounter = 0;
uint32_t sentCount = 0;
uint32_t failCount = 0;

void onDataSent(const uint8_t* mac, esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS) {
    sentCount++;
  } else {
    failCount++;
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("=== ESP32_TX (ESP-NOW Sender) ===");

  // ADC 配置
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  // WiFi 配置
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  Serial.print("TX MAC: ");
  Serial.println(WiFi.macAddress());
  Serial.print("Target RX MAC: ");
  for (int i = 0; i < 6; i++) {
    Serial.printf("%02X", receiverMac[i]);
    if (i < 5) Serial.print(":");
  }
  Serial.println();

  // 长距离模式（可选）
  if (USE_LR_MODE) {
    esp_wifi_set_protocol(WIFI_IF_STA,
      WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_LR);
    Serial.println("[INFO] Long-range mode enabled");
  }

  // ESP-NOW 初始化
  if (esp_now_init() != ESP_OK) {
    Serial.println("[ERROR] ESP-NOW init failed");
    while (true) delay(1000);
  }

  esp_now_register_send_cb(onDataSent);

  // 添加接收端 peer
  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, receiverMac, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;

  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("[ERROR] Failed to add peer");
    while (true) delay(1000);
  }

  Serial.println("[OK] Ready. Streaming TENG data via ESP-NOW...");
  Serial.println();
}

void loop() {
  // 采样 + 打包
  DataPacket pkt;
  pkt.seq = seqCounter++;
  pkt.timestamp_us = micros();
  pkt.adc_value = analogRead(ADC_PIN);

  // 发送
  esp_now_send(receiverMac, (uint8_t*)&pkt, sizeof(pkt));

  // 不加 delay，让 ESP-NOW 自身的发送间隔决定速率
  // 实测约 1-2 kHz 单包发送速率
  // 如果想更快，看下面"批量发送"版本

  // 每 5 秒打印统计（在自己的串口，不影响接收端数据流）
  static uint32_t lastReport = 0;
  if (millis() - lastReport >= 5000) {
    lastReport = millis();
    Serial.printf("Sent: %u  Fail: %u  Rate: %.0f Hz\n",
                  sentCount, failCount, sentCount / (millis() / 1000.0));
  }
}
