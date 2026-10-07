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
// The UART is driven directly rather than through esp_dmx, so that line timing
// can be varied: the BREAK is made by inverting the idle TX line, and slots
// can be written one at a time with gaps between them.
//
// Patterns 0-4 use the tightest legal timing: 250 kbaud, 92 us BREAK, 12 us
// MAB, slots and packets back to back. Pattern 5 ("timing") picks a new line
// timing for every packet: baud 245k-255k (DMX allows +-2%), BREAK 92 us-10 ms,
// MAB 12 us-1 ms, gaps between slots, and idle time between packets.
//
// USB serial (115200) commands, one per line:
//   p<n>     pin pattern n           c   cycle all patterns, 10 s each
//   b<baud>  force this baud for every pattern (b0 = pattern default)
//   ?        status now
//   l / h    stop sending and hold the line low / high (for a meter); any
//            other command (e.g. p0) resumes
// Status line, once a second:
//   S <pattern> <next_counter> <pin|cycle> baud=<forced or 0> fps=<n>

#include <Arduino.h>
#include <driver/uart.h>
#include <esp_rom_sys.h>
#include <hal/uart_ll.h>

#define ONBOARD_LED 2

enum { FULL, SHORT, STARTCODES, ESCAPES, VARLEN, TIMING, NUM_PATTERNS };
static const char *NAMES[NUM_PATTERNS] = {"full512", "short24", "startcodes",
                                          "escapes", "varlen",  "timing"};
static const uint8_t ESC_VALUES[4] = {0xFF, 0x11, 0x13, 0x00};
static const int HEADER_SLOTS = 5;
static const uint32_t CYCLE_MS = 10000;
static const uint32_t BAUDS[5] = {245000, 247500, 250000, 252500, 255000};

static const uart_port_t PORT = UART_NUM_1;  // nothing else on this board uses it
static uart_dev_t *const hw = &UART1;
static const int TX_PIN = 14;

enum { GAP_NONE, GAP_SMALL, GAP_CONSTANT, GAP_SPARSE };
static const char *GAP_NAMES[4] = {"none", "small", "constant", "sparse"};

struct Timing {
  uint32_t baud, break_us, mab_us, idle_us;
  uint8_t gap_mode;
  uint16_t gap_us;
};

static uint8_t pkt[513];
static uint32_t counter = 0;
static int pattern = FULL;
static bool cycling = true;
static uint32_t forced_baud = 0;
static uint32_t cur_baud = 0;
static uint32_t pattern_since = 0;
static uint32_t frames_this_second = 0, fps = 0;
static uint32_t last_status = 0;
static Timing last_timing;
static char hold = 0;  // 'l' or 'h' while holding the line for a meter
static char cmd[16];
static int cmd_len = 0;

static inline uint32_t xorshift(uint32_t &s) {
  s ^= s << 13;
  s ^= s >> 17;
  s ^= s << 5;
  return s;
}

// Fills pkt (start code + slots) for (pattern, counter); returns its size in bytes.
static size_t build_packet(int p, uint32_t c) {
  size_t n;
  if (p == SHORT) {
    n = 24;
  } else if (p == VARLEN) {
    n = HEADER_SLOTS + ((uint64_t)c * 37) % 508;  // 5..512
  } else if (p == TIMING) {
    n = 24 + ((uint64_t)c * 97) % 489;  // 24..512
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
    } else if (p == TIMING) {
      slots[j] = (uint8_t)((uint64_t)c * 3 + j);
    } else {
      slots[j] = (uint8_t)(c + j);
    }
  }
  return n + 1;
}

static Timing pick_timing(int p, uint32_t c) {
  Timing t = {250000, 92, 12, 0, GAP_NONE, 0};
  if (p == TIMING) {
    uint32_t s = c * 2654435761u + 1;
    xorshift(s);
    t.baud = BAUDS[xorshift(s) % 5];
    t.break_us = (xorshift(s) % 16 == 0) ? 1000 + xorshift(s) % 9001 : 92 + xorshift(s) % 909;
    t.mab_us = (xorshift(s) % 16 == 0) ? 100 + xorshift(s) % 901 : 12 + xorshift(s) % 89;
    t.gap_mode = xorshift(s) % 4;
    t.gap_us = 1 + xorshift(s) % 40;
    t.idle_us = xorshift(s) % 2001;
  }
  if (forced_baud) t.baud = forced_baud;
  return t;
}

static inline void wait_tx_idle() {
  while (!uart_ll_is_tx_idle(hw)) {
  }
}

static void send_packet(const uint8_t *buf, size_t n, const Timing &t, uint32_t seed) {
  wait_tx_idle();
  if (t.baud != cur_baud) {
    uart_ll_set_baudrate(hw, t.baud);
    cur_baud = t.baud;
  }

  // BREAK: invert the idle (high) line to low, then release it for the MAB.
  uart_set_line_inverse(PORT, UART_SIGNAL_TXD_INV);
  esp_rom_delay_us(t.break_us);
  uart_set_line_inverse(PORT, UART_SIGNAL_INV_DISABLE);
  esp_rom_delay_us(t.mab_us);

  if (t.gap_mode == GAP_NONE) {
    // Back to back: keep the 128-byte FIFO topped up, but never past 100 bytes.
    // The FIFO count register lags a write slightly, so filling to the brim
    // overflows it and drops bytes.
    const uint32_t FILL = 100;
    size_t i = 0;
    while (i < n) {
      uint32_t used = 128 - uart_ll_get_txfifo_len(hw);
      if (used < FILL) {
        uint32_t k = min((uint32_t)(n - i), FILL - used);
        uart_ll_write_txfifo(hw, buf + i, k);
        i += k;
        esp_rom_delay_us(2);  // let the count register catch up
      }
    }
  } else {
    uint32_t s = seed | 1;
    for (size_t i = 0; i < n; i++) {
      uart_ll_write_txfifo(hw, buf + i, 1);
      esp_rom_delay_us(1);  // let the FIFO count register catch up
      wait_tx_idle();
      uint32_t gap = 0;
      if (t.gap_mode == GAP_SMALL) {
        gap = xorshift(s) % 9;
      } else if (t.gap_mode == GAP_CONSTANT) {
        gap = t.gap_us;
      } else if (xorshift(s) % 32 == 0) {  // GAP_SPARSE
        gap = 50 + xorshift(s) % 451;
      }
      if (gap) esp_rom_delay_us(gap);
    }
  }
  wait_tx_idle();
  if (t.idle_us) esp_rom_delay_us(t.idle_us);
}

static void print_status() {
  Serial.printf("S %d %u %s baud=%u fps=%u\n", pattern, counter, cycling ? "cycle" : "pin",
                forced_baud, fps);
}

static void handle_command(const char *line) {
  if (hold) {
    hold = 0;
    uart_set_line_inverse(PORT, UART_SIGNAL_INV_DISABLE);
  }
  if (line[0] == 'p' && line[1] >= '0' && line[1] < '0' + NUM_PATTERNS) {
    pattern = line[1] - '0';
    cycling = false;
    pattern_since = millis();
    Serial.printf("pattern %s (pinned)\n", NAMES[pattern]);
  } else if (line[0] == 'c') {
    cycling = true;
    pattern_since = millis();
    Serial.printf("cycling, starting with %s\n", NAMES[pattern]);
  } else if (line[0] == 'b') {
    uint32_t b = strtoul(line + 1, NULL, 10);
    if (b == 0 || (b >= 200000 && b <= 300000)) {
      forced_baud = b;
      Serial.printf("baud %s %u\n", b ? "forced to" : "back to pattern default", b);
    } else {
      Serial.printf("baud out of range: %s\n", line + 1);
    }
  } else if (line[0] == 'l' || line[0] == 'h') {
    wait_tx_idle();
    hold = line[0];
    uart_set_line_inverse(PORT, hold == 'l' ? UART_SIGNAL_TXD_INV : UART_SIGNAL_INV_DISABLE);
    Serial.printf("holding the line %s; send any command to resume\n", hold == 'l' ? "LOW" : "HIGH");
    return;
  } else if (line[0] == '?') {
    const Timing &t = last_timing;
    Serial.printf("last packet: baud %u break %u MAB %u gaps %s/%u idle %u\n", t.baud,
                  t.break_us, t.mab_us, GAP_NAMES[t.gap_mode], t.gap_us, t.idle_us);
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

  uart_config_t cfg = {};
  cfg.baud_rate = 250000;
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_2;
  cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
  cfg.source_clk = UART_SCLK_APB;
  uart_param_config(PORT, &cfg);
  uart_set_pin(PORT, TX_PIN, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
  hw->idle_conf.tx_idle_num = 0;  // no idle bits between bytes written back to back
  cur_baud = 250000;

  Serial.printf("dmx_esp32_txtest ready: TX on GPIO%d, actual baud at 250k setting %u\n",
                TX_PIN, uart_ll_get_baudrate(hw));
  pattern_since = last_status = millis();
}

void loop() {
  uint32_t now = millis();

  if (cycling && now - pattern_since >= CYCLE_MS) {
    pattern = (pattern + 1) % NUM_PATTERNS;
    pattern_since = now;
    Serial.printf("pattern %s\n", NAMES[pattern]);
  }

  if (hold) {
    poll_serial();
    delay(10);
    return;
  }

  size_t size = build_packet(pattern, counter);
  last_timing = pick_timing(pattern, counter);
  send_packet(pkt, size, last_timing, counter * 40503u);
  counter++;
  frames_this_second++;

  poll_serial();

  if (now - last_status >= 1000) {
    last_status = now;
    fps = frames_this_second;
    frames_this_second = 0;
    digitalWrite(ONBOARD_LED, !digitalRead(ONBOARD_LED));
    print_status();
  }
}
