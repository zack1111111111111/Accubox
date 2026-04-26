/*
 * ESP32_RX (Receiver)
 * ===================
 * 接收 ESP-NOW 数据 → USB 串口转发给电脑
 * 输出格式与单机模式完全相同，teng_daq_v1.py 不需要改
 *
 * 烧录到：连接电脑的那块 ESP32（板 A）
 * 接线：只需 USB 线连电脑
 */

#include <esp_now.h>
#include <WiFi.h>

// 必须和 TX 端的数据结构完全一致
typedef struct {
  uint32_t seq;          // 包序号
  uint32_t timestamp_us; // 发送端时间戳
  uint16_t adc_value;    // 12-bit ADC 值
} __attribute__((packed)) DataPacket;

// 统计信息
volatile uint32_t pktCount = 0;
volatile uint32_t lastSeq = 0;
volatile uint32_t lostCount = 0;
volatile bool firstPacket = true;

// ESP32 Arduino core 新旧版本的接收回调签名不一样
// 这里用一个跨版本兼容的实现
#if ESP_IDF_VERSION_MAJOR >= 5
// 新版（ESP-IDF 5.x，Arduino-ESP32 3.x）
void onDataRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
#else
// 旧版（ESP-IDF 4.x，Arduino-ESP32 2.x）
void onDataRecv(const uint8_t* mac, const uint8_t* data, int len) {
#endif
  if (len != sizeof(DataPacket)) return;

  DataPacket pkt;
  memcpy(&pkt, data, sizeof(pkt));

  // 检测丢包
  if (firstPacket) {
    firstPacket = false;
  } else {
    uint32_t expected = lastSeq + 1;
    if (pkt.seq > expected) {
      lostCount += (pkt.seq - expected);
    }
  }
  lastSeq = pkt.seq;
  pktCount++;

  // 输出 ADC 值，格式与单机模式一致 → teng_daq.py 不用改
  Serial.println(pkt.adc_value);
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("=== ESP32_RX (ESP-NOW Receiver) ===");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  Serial.print("RX MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("[ERROR] ESP-NOW init failed");
    while (true) delay(1000);
  }

  esp_now_register_recv_cb(onDataRecv);

  Serial.println("[OK] Listening for ESP-NOW packets...");
  Serial.println("(Following lines will be raw ADC values)");
  Serial.println();
}

void loop() {
  // 不打印额外信息，让串口数据流干净
  // 如果想看丢包率，取消下面注释
  /*
  static uint32_t lastReport = 0;
  if (millis() - lastReport >= 5000) {
    lastReport = millis();
    Serial.printf("# pkts=%u lost=%u\n", pktCount, lostCount);
  }
  */
  delay(10);
}
