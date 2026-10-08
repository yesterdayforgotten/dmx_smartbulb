// WiFi airtime stand-in for load tests (see platformio.ini).
//
// USB serial (115200), once a second:
//   S rx=<pkts/s> tx=<pkts/s> rssi=<dBm> ch=<channel> ip=<ip> reply=<bytes>
// Commands: r<N> sets the reply size in bytes (0 = don't reply).

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "secrets.h"   // WIFI_SSID, WIFI_PASSWORD

static const uint16_t PORT = 9999;
static const size_t MAX_REPLY = 1400;

static WiFiUDP udp;
static uint8_t rxbuf[1500];
static uint8_t reply[MAX_REPLY];
static size_t reply_len = 181;     // a KL135 transition_light_state reply
static uint32_t rx_count = 0, tx_count = 0, tx_fail = 0;
static uint32_t last_report = 0;
static char cmd[16];
static int cmd_len = 0;

static void connect_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // mains-powered bulbs don't doze; neither should we
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("joining %s", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("joined: ip %s rssi %d ch %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                  WiFi.channel());
    udp.begin(PORT);
  } else {
    Serial.println("couldn't join the WiFi; retrying");
  }
}

static void poll_serial() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      cmd[cmd_len] = 0;
      if (cmd[0] == 'r') {
        size_t n = strtoul(cmd + 1, NULL, 10);
        reply_len = n > MAX_REPLY ? MAX_REPLY : n;
        Serial.printf("reply size %u bytes\n", (unsigned)reply_len);
      }
      cmd_len = 0;
    } else if (cmd_len < (int)sizeof(cmd) - 1) {
      cmd[cmd_len++] = ch;
    }
  }
}

void setup() {
  Serial.begin(115200);
  for (size_t i = 0; i < MAX_REPLY; i++) reply[i] = (uint8_t)(0xAB ^ i);   // looks like XOR-framed data
  connect_wifi();
  last_report = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connect_wifi();
    return;
  }
  // Drain everything waiting, replying to each packet.
  int n;
  while ((n = udp.parsePacket()) > 0) {
    udp.read(rxbuf, sizeof(rxbuf));
    rx_count++;
    if (reply_len) {
      udp.beginPacket(udp.remoteIP(), udp.remotePort());
      udp.write(reply, reply_len);
      if (udp.endPacket()) tx_count++; else tx_fail++;
    }
  }
  poll_serial();
  uint32_t now = millis();
  if (now - last_report >= 1000) {
    float s = (now - last_report) / 1000.0f;
    Serial.printf("S rx=%.0f tx=%.0f fail=%u rssi=%d ch=%d ip=%s reply=%u\n", rx_count / s, tx_count / s,
                  tx_fail, WiFi.RSSI(), WiFi.channel(), WiFi.localIP().toString().c_str(), (unsigned)reply_len);
    rx_count = tx_count = 0;
    last_report = now;
  }
  delay(1);
}
