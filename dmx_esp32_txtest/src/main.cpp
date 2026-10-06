// DMX transmit test rig (phase 1 of the rework).
//
// Sends self-verifying DMX packets out of GPIO14, the wire that already goes to
// Pi pin 29, at 3.3 V TTL (idle high, BREAK low), so the Pi UART sees what a
// console would send. Packet contents must match tools/dmx_testpattern.py:
//
//   slot 1     pattern id
//   slots 2-5  counter, u32 big-endian (one counter across all patterns)
//   slots 6..  fill(pattern, counter, j)
//
// USB serial (115200) commands, one per line:
//   p<n>  pin pattern n      c  cycle all patterns, 10 s each      ?  status now
// Status line, once a second:  S <pattern> <next_counter> <pin|cycle> fps=<n> fails=<n>

#include <Arduino.h>
#include <esp_dmx.h>

#define ONBOARD_LED 2

enum { FULL, SHORT, STARTCODES, ESCAPES, VARLEN, NUM_PATTERNS };
static const char *NAMES[NUM_PATTERNS] = {"full512", "short24", "startcodes", "escapes", "varlen"};
static const uint8_t ESC_VALUES[4] = {0xFF, 0x11, 0x13, 0x00};
static const int HEADER_SLOTS = 5;
static const uint32_t CYCLE_MS = 10000;

const dmx_port_t dmxPort = 1;   // UART1; nothing else on this board uses it
const int tx_pin = 14;

static uint8_t pkt[DMX_PACKET_SIZE_MAX];
static uint32_t counter = 0;
static int pattern = FULL;
static bool cycling = true;
static uint32_t pattern_since = 0;
static uint32_t fails = 0;
static uint32_t frames_this_second = 0, fps = 0;
static uint32_t last_status = 0;
static char cmd[16];
static int cmd_len = 0;

// Fills pkt (start code + slots) for (pattern, counter); returns its size in bytes.
static size_t build_packet(int p, uint32_t c) {
  size_t n;
  if (p == SHORT) {
    n = 24;
  } else if (p == VARLEN) {
    n = HEADER_SLOTS + ((uint64_t)c * 37) % 508;  // 5..512
  } else {
    n = 512;
  }
  static const uint8_t SCS[3] = {0x00, 0x17, 0xCF};
  pkt[0] = (p == STARTCODES) ? SCS[c % 3] : 0x00;

  uint8_t *slots = &pkt[1];
  slots[0] = p;
  slots[1] = c >> 24;
  slots[2] = c >> 16;
  slots[3] = c >> 8;
  slots[4] = c;
  for (size_t j = HEADER_SLOTS; j < n; j++) {
    if (p == ESCAPES) {
      slots[j] = ESC_VALUES[(c + j) % 4];
    } else if (p == STARTCODES) {
      slots[j] = (uint8_t)((uint64_t)c * 7 + j);
    } else {
      slots[j] = (uint8_t)(c + j);
    }
  }
  return n + 1;
}

static void print_status() {
  Serial.printf("S %d %u %s fps=%u fails=%u\n", pattern, counter, cycling ? "cycle" : "pin",
                fps, fails);
}

static void handle_command(const char *line) {
  if (line[0] == 'p' && line[1] >= '0' && line[1] < '0' + NUM_PATTERNS) {
    pattern = line[1] - '0';
    cycling = false;
    pattern_since = millis();
    Serial.printf("pattern %s (pinned)\n", NAMES[pattern]);
  } else if (line[0] == 'c') {
    cycling = true;
    pattern_since = millis();
    Serial.printf("cycling, starting with %s\n", NAMES[pattern]);
  } else if (line[0] == '?') {
    // fall through to the status below
  } else if (line[0]) {
    Serial.printf("unknown command: %s\n", line);
  }
  print_status();
}

static void poll_serial() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      cmd[cmd_len] = 0;
      if (cmd_len) handle_command(cmd);
      cmd_len = 0;
    } else if (cmd_len < (int)sizeof(cmd) - 1) {
      cmd[cmd_len++] = ch;
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(ONBOARD_LED, OUTPUT);

  dmx_config_t config = DMX_CONFIG_DEFAULT;
  if (!dmx_driver_install(dmxPort, &config, DMX_INTR_FLAGS_DEFAULT)) {
    Serial.println("dmx_driver_install failed");
  }
  dmx_set_pin(dmxPort, tx_pin, DMX_PIN_NO_CHANGE, DMX_PIN_NO_CHANGE);
  dmx_set_break_len(dmxPort, DMX_BREAK_LEN_MIN_US);  // 92 us: tightest a console may send
  dmx_set_mab_len(dmxPort, DMX_MAB_LEN_MIN_US);      // 12 us

  Serial.printf("dmx_esp32_txtest ready: TX on GPIO%d, break %u us, MAB %u us\n", tx_pin,
                dmx_get_break_len(dmxPort), dmx_get_mab_len(dmxPort));
  pattern_since = last_status = millis();
}

void loop() {
  uint32_t now = millis();

  if (cycling && now - pattern_since >= CYCLE_MS) {
    pattern = (pattern + 1) % NUM_PATTERNS;
    pattern_since = now;
    Serial.printf("pattern %s\n", NAMES[pattern]);
  }

  // Back-to-back packets: wait for the previous one to finish, then send.
  dmx_wait_sent(dmxPort, DMX_TIMEOUT_TICK);
  size_t size = build_packet(pattern, counter);
  dmx_write(dmxPort, pkt, size);
  if (dmx_send(dmxPort, size) == size) {
    counter++;
    frames_this_second++;
  } else {
    fails++;  // counter not advanced, so the probe won't count it as lost
  }

  poll_serial();

  if (now - last_status >= 1000) {
    last_status = now;
    fps = frames_this_second;
    frames_this_second = 0;
    digitalWrite(ONBOARD_LED, !digitalRead(ONBOARD_LED));
    print_status();
  }
}
