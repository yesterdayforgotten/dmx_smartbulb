// WiFi airtime stand-in for load tests (see platformio.ini).
//
// UART0 at 115200, once a second:
//   S rx=<pkts/s> tx=<pkts/s> fail=<total> rssi=<dBm> ch=<channel> ip=<ip> reply=<bytes>

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#include "secrets.h"   // WIFI_SSID, WIFI_PASSWORD

#define PORT 9999
#define REPLY_LEN 181  // a KL135 transition_light_state reply
#define GOT_IP BIT0

static EventGroupHandle_t events;
static volatile uint32_t rx_count, tx_count, tx_fail;
static char ip_str[16] = "-";

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    wifi_event_sta_disconnected_t *d = data;
    // 201 no AP found, 15 handshake timeout (wrong password?), 2/202 auth problems.
    printf("disconnected, reason %d; retrying\n", d->reason);
    xEventGroupClearBits(events, GOT_IP);
    esp_wifi_connect();
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    ip_event_got_ip_t *e = data;
    snprintf(ip_str, sizeof(ip_str), IPSTR, IP2STR(&e->ip_info.ip));
    wifi_ap_record_t ap;
    esp_wifi_sta_get_ap_info(&ap);
    printf("joined: ip %s rssi %d ch %d\n", ip_str, ap.rssi, ap.primary);
    xEventGroupSetBits(events, GOT_IP);
  }
}

static void wifi_start(void) {
  ESP_ERROR_CHECK(esp_netif_init());
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  esp_netif_create_default_wifi_sta();
  wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&init));
  ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
  ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
  wifi_config_t cfg = {0};
  strlcpy((char *)cfg.sta.ssid, WIFI_SSID, sizeof(cfg.sta.ssid));
  strlcpy((char *)cfg.sta.password, WIFI_PASSWORD, sizeof(cfg.sta.password));
  cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
  ESP_ERROR_CHECK(esp_wifi_start());
  ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
  printf("joining %s\n", WIFI_SSID);
}

static void udp_task(void *arg) {
  static uint8_t buf[1500];
  static uint8_t reply[REPLY_LEN];
  for (int i = 0; i < REPLY_LEN; i++) reply[i] = (uint8_t)(0xAB ^ i);  // looks like XOR-framed data
  xEventGroupWaitBits(events, GOT_IP, pdFALSE, pdTRUE, portMAX_DELAY);
  int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  int rcvbuf = 64 * 1024;
  setsockopt(s, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
  struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
  bind(s, (struct sockaddr *)&addr, sizeof(addr));
  printf("listening on udp %d\n", PORT);
  while (1) {
    struct sockaddr_in from;
    socklen_t flen = sizeof(from);
    int n = recvfrom(s, buf, sizeof(buf), 0, (struct sockaddr *)&from, &flen);
    if (n <= 0) continue;
    rx_count++;
    if (sendto(s, reply, REPLY_LEN, 0, (struct sockaddr *)&from, flen) == REPLY_LEN) tx_count++;
    else tx_fail++;
  }
}

void app_main(void) {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    nvs_flash_erase();
    nvs_flash_init();
  }
  esp_log_level_set("wifi", ESP_LOG_WARN);
  events = xEventGroupCreate();
  wifi_start();
  xTaskCreatePinnedToCore(udp_task, "udp", 4096, NULL, 18, NULL, 1);
  while (1) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    uint32_t rx = rx_count, tx = tx_count;
    rx_count = tx_count = 0;
    wifi_ap_record_t ap = {0};
    esp_wifi_sta_get_ap_info(&ap);
    printf("S rx=%lu tx=%lu fail=%lu rssi=%d ch=%d ip=%s reply=%d\n", (unsigned long)rx, (unsigned long)tx,
           (unsigned long)tx_fail, ap.rssi, ap.primary, ip_str, REPLY_LEN);
  }
}
