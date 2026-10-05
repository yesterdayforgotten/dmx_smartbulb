#include <Arduino.h>
#include <esp_log.h>
#include <esp_dmx.h>
#include <string>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#define ONBOARD_LED 2

static const char* TAG = "Main";

const dmx_port_t dmxPort = 2;
const int tx_pin = 17;
const int rx_pin = 16;
const int rts_pin = 21;

HardwareSerial txSerial(1);
#define TXS_TX_PIN    14
#define TXS_BAUD      921600
const char header[] = "DMX#";

/* Now we want somewhere to store our DMX data. Since a single packet of DMX
data can be up to 513 bytes long, we want our array to be at least that long.
This library knows that the max DMX packet size is 513, so we can fill in the
array size with `DMX_PACKET_SIZE`. */
byte data[DMX_PACKET_SIZE_MAX];
byte data2[DMX_PACKET_SIZE_MAX];

SemaphoreHandle_t sem_data  = NULL;

TaskHandle_t SerialTxHandle;

bool dmxIsConnected = false;
unsigned long lastUpdate = millis();

void SerialTx(void * pvParameters) {
  while (1) {
    // Copy the DMX data to a side buffer so we get consistent updates across all bulbs even if the DMX data changes in the middle of updating
    // Need to use a semaphore to ensure DMX data isn't being change while it is being copied
    while (!xSemaphoreTake(sem_data, 0)) {
      ESP_LOGI(TAG, "[bulb] Waiting for semaphore");
    }
    memcpy(data2, data, sizeof(data));
    xSemaphoreGive(sem_data);

    txSerial.write(header, 4);
    txSerial.write(&data[1], DMX_PACKET_SIZE_MAX-1);

    delay(20);
  }
}

void setup() {
  Serial.begin(115200);
  Serial.println("Started");

  uint32_t Freq = getCpuFrequencyMhz();
  Serial.printf("CPU Freq = %d MHz\n",Freq);
  Freq = getXtalFrequencyMhz();
  Serial.printf("XTAL Freq = %d MHz\n",Freq);
  Freq = getApbFrequency();
  Serial.printf("APB Freq = %d MHz\n",Freq);

  txSerial.begin(TXS_BAUD, SERIAL_8N1, 0, TXS_TX_PIN);

  //xTaskCreate( Blink, "Blink", 2048, NULL, 1, &BlinkHandle );

  pinMode(ONBOARD_LED, OUTPUT);

  //blink_period = 2000;

  sem_data = xSemaphoreCreateMutex();
  if (sem_data == NULL) {
      ESP_LOGE(TAG, "Error creating sem_data semaphore");
  }

  int personality_count = 1;
  dmx_config_t config = DMX_CONFIG_DEFAULT;
  dmx_driver_install(dmxPort, &config, DMX_INTR_FLAGS_DEFAULT);
  dmx_set_pin(dmxPort, tx_pin, rx_pin, rts_pin);

  xTaskCreatePinnedToCore(SerialTx, "SerialTx", 4096, NULL, 3, &SerialTxHandle, 0);

  //blink_period = 10000;
}

void loop() {
  /* We need a place to store information about the DMX packets we receive. We
    will use a dmx_packet_t to store that packet information.  */
  dmx_packet_t packet;

  /* And now we wait! The DMX standard defines the amount of time until DMX
    officially times out. That amount of time is converted into ESP32 clock
    ticks using the constant `DMX_TIMEOUT_TICK`. If it takes longer than that
    amount of time to receive data, this if statement will evaluate to false. */
  if (dmx_receive(dmxPort, &packet, DMX_TIMEOUT_TICK)) {
    /* If this code gets called, it means we've received DMX data! */

    /* Get the current time since boot in milliseconds so that we can find out
      how long it has been since we last updated data and printed to the Serial
      Monitor. */
    unsigned long now = millis();

    /* We should check to make sure that there weren't any DMX errors. */
    if (!packet.err) {
      /* If this is the first DMX data we've received, lets log it! */
      if (!dmxIsConnected) {
        Serial.println("DMX is connected!");
        dmxIsConnected = true;
      }

      /* Don't forget we need to actually read the DMX data into our buffer so
        that we can print it out. */
      while (!xSemaphoreTake(sem_data, 0)) {
        Serial.println("[loop] Waiting for semaphore");
      }
      dmx_read(dmxPort, data, packet.size);
      xSemaphoreGive(sem_data);

      if (now - lastUpdate > 1000) {
        /* Print the received start code - it's usually 0. */
        // Serial.printf("Start code is 0x%02X c1: %3d, c2: %3d, c3: %3d, c4: %3d, c5: %3d, c6: %3d, c7: %3d, c8: %3d\n", data[0],
        //               data[1], data[2], data[3], data[4], data[5], data[6], data[7], data[8]);
        //Serial.printf("DMX Core: %d\n", xPortGetCoreID());
        lastUpdate = now;
      }
    } else {
      /* Oops! A DMX error occurred! Don't worry, this can happen when you first
        connect or disconnect your DMX devices. If you are consistently getting
        DMX errors, then something may have gone wrong with your code or
        something is seriously wrong with your DMX transmitter. */
      Serial.println("A DMX error occurred.");
    }
  } else if (dmxIsConnected) {
    /* If DMX times out after having been connected, it likely means that the
      DMX cable was unplugged. When that happens in this example sketch, we'll
      uninstall the DMX driver. */
    Serial.println("DMX was disconnected.");
    dmxIsConnected = false;
    //dmx_driver_delete(dmxPort);

    /* Stop the program. */
    //while (true) yield();
  }
}
