/*
 * Get MAC Address
 * ===============
 * 烧到接收端 ESP32，打开串口监视器（115200），
 * 记下打印的 MAC 地址，填到发送端代码里。
 */

#include <WiFi.h>

void setup() {
  Serial.begin(115200);
  delay(500);

  WiFi.mode(WIFI_STA);
  delay(200);

  Serial.println();
  Serial.println("=================================");
  Serial.print("ESP32 MAC Address: ");
  Serial.println(WiFi.macAddress());
  Serial.println("=================================");
  Serial.println("Copy this MAC into ESP32_TX code:");
  Serial.println("uint8_t receiverMac[] = { 0x.., 0x.., 0x.., 0x.., 0x.., 0x.. };");
}

void loop() {
  // 每 5 秒重复一次，方便错过的人
  delay(5000);
  Serial.print("MAC: ");
  Serial.println(WiFi.macAddress());
}
