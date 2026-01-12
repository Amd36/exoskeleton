#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BNO055.h>
#include <math.h>

// ===================== Config =====================
constexpr uint32_t SERIAL_BAUD = 921600;
constexpr uint32_t I2C_HZ      = 400000;

constexpr uint32_t ADC_HZ      = 1000;   // ADC sampling rate (piezo)
constexpr uint32_t IMU_HZ      = 100;    // accel+gyro sampling rate
constexpr uint32_t MAG_HZ      = 20;     // magnetometer update rate (cached)

static_assert((IMU_HZ % MAG_HZ) == 0, "IMU_HZ must be divisible by MAG_HZ");

constexpr uint8_t  ADC_BLOCK   = 10;     // 10 ADC samples per frame (10ms)
constexpr size_t   ADC_CH      = 8;
constexpr size_t   IMU_CH      = 9;      // acc(3), gyro(3), mag(3)

constexpr uint16_t SYNC_WORD   = 0xA55A;
constexpr uint8_t  PKT_VER     = 1;
constexpr uint8_t  PKT_TYPE_FRAME = 1;

// ADC pins (ESP32)
static const int ADC_PINS[ADC_CH] = {36, 39, 34, 35, 32, 33, 25, 26};

// Queue depth: 256 frames = 2.56s cushion at 100Hz
constexpr size_t TX_QUEUE_LEN = 256;

using sample_t = int16_t;

// ===================== IMU =====================
Adafruit_BNO055 bno(55, 0x29, &Wire);

// ===================== Packet =====================
#pragma pack(push, 1)
struct FramePacket {
  uint16_t sync;             // 0xA55A
  uint8_t  version;          // 1
  uint8_t  type;             // 1 = FramePacket
  uint16_t frame_seq;        // increments per frame (100 Hz)
  uint32_t t_us;             // micros() at start of this frame
  uint32_t adc_base_idx;     // index of first ADC sample in this frame (1kHz counter)

  uint16_t adc[ADC_BLOCK][ADC_CH]; // 10x8 ADC samples

  int16_t  imu[IMU_CH];      // acc/gyro/mag scaled by 100 (cached)

  uint16_t crc16;            // CRC16-CCITT over all bytes except this field
};
#pragma pack(pop)

static_assert(sizeof(FramePacket) == 194, "FramePacket size must be 194 bytes");

// ===================== CRC16-CCITT =====================
// Polynomial 0x1021, init 0xFFFF (common CCITT-FALSE style)
static inline uint16_t crc16_ccitt(const uint8_t* data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

// ===================== Shared IMU Cache =====================
static sample_t imuCache[IMU_CH] = {0};
static portMUX_TYPE imuMux = portMUX_INITIALIZER_UNLOCKED;

// ===================== TX Queue =====================
static QueueHandle_t txQueue = nullptr;
static volatile uint32_t droppedTxPackets = 0;

// Global counters
static volatile uint32_t adcSampleIndex = 0; // increments at 1kHz
static uint16_t frameSeq = 0;

// ---------------- IMU task @ 100Hz ----------------
// Only task that touches Wire/BNO055.
void imuTask(void* pv) {
  const TickType_t period = pdMS_TO_TICKS(1000 / IMU_HZ); // 10ms
  TickType_t lastWake = xTaskGetTickCount();

  const uint32_t magEvery = IMU_HZ / MAG_HZ; // 5
  uint32_t imuTick = 0;

  sensors_event_t a, g, m;

  // cached mag
  static sample_t mx = 0, my = 0, mz = 0;

  for (;;) {
    vTaskDelayUntil(&lastWake, period);
    imuTick++;

    bno.getEvent(&a, Adafruit_BNO055::VECTOR_ACCELEROMETER);
    bno.getEvent(&g, Adafruit_BNO055::VECTOR_GYROSCOPE);

    sample_t ax = (sample_t)lroundf(a.acceleration.x * 100.0f);
    sample_t ay = (sample_t)lroundf(a.acceleration.y * 100.0f);
    sample_t az = (sample_t)lroundf(a.acceleration.z * 100.0f);

    sample_t gx = (sample_t)lroundf(g.gyro.x * 100.0f);
    sample_t gy = (sample_t)lroundf(g.gyro.y * 100.0f);
    sample_t gz = (sample_t)lroundf(g.gyro.z * 100.0f);

    if ((imuTick % magEvery) == 0) {
      bno.getEvent(&m, Adafruit_BNO055::VECTOR_MAGNETOMETER);
      mx = (sample_t)lroundf(m.magnetic.x * 100.0f);
      my = (sample_t)lroundf(m.magnetic.y * 100.0f);
      mz = (sample_t)lroundf(m.magnetic.z * 100.0f);
    }

    portENTER_CRITICAL(&imuMux);
    imuCache[0] = ax; imuCache[1] = ay; imuCache[2] = az;
    imuCache[3] = gx; imuCache[4] = gy; imuCache[5] = gz;
    imuCache[6] = mx; imuCache[7] = my; imuCache[8] = mz;
    portEXIT_CRITICAL(&imuMux);
  }
}

// --------------- ADC task @ 1kHz (builds frames @100Hz) ---------------
// Reads ADC every 1ms; every 10 samples => builds a FramePacket and enqueues it.
void adcTask(void* pv) {
  const TickType_t period = pdMS_TO_TICKS(1000 / ADC_HZ); // 1ms
  TickType_t lastWake = xTaskGetTickCount();

  uint16_t adcBlock[ADC_BLOCK][ADC_CH];
  uint8_t  blockPos = 0;

  uint32_t frameStartUs = 0;
  uint32_t frameBaseIdx = 0;

  for (;;) {
    vTaskDelayUntil(&lastWake, period);

    // start of a new 10ms frame
    if (blockPos == 0) {
      frameStartUs = (uint32_t)micros();
      frameBaseIdx = adcSampleIndex; // index of the first sample in this frame
    }

    // Read 8 ADC channels
    for (size_t ch = 0; ch < ADC_CH; ch++) {
      adcBlock[blockPos][ch] = (uint16_t)analogRead(ADC_PINS[ch]);
    }

    adcSampleIndex++;
    blockPos++;

    // If we collected 10 samples -> build and queue a packet
    if (blockPos >= ADC_BLOCK) {
      blockPos = 0;

      FramePacket p{};
      p.sync = SYNC_WORD;
      p.version = PKT_VER;
      p.type = PKT_TYPE_FRAME;
      p.frame_seq = frameSeq++;
      p.t_us = frameStartUs;
      p.adc_base_idx = frameBaseIdx;

      // copy ADC block
      for (uint8_t i = 0; i < ADC_BLOCK; i++) {
        for (size_t ch = 0; ch < ADC_CH; ch++) {
          p.adc[i][ch] = adcBlock[i][ch];
        }
      }

      // copy cached IMU
      portENTER_CRITICAL(&imuMux);
      for (size_t i = 0; i < IMU_CH; i++) p.imu[i] = imuCache[i];
      portEXIT_CRITICAL(&imuMux);

      // CRC over everything except crc16 field
      p.crc16 = crc16_ccitt((const uint8_t*)&p, sizeof(FramePacket) - sizeof(p.crc16));

      // enqueue (don’t block; drop if full)
      if (xQueueSend(txQueue, &p, 0) != pdTRUE) {
        droppedTxPackets++;
      }
    }
  }
}

// ---------------- TX task ----------------
// Only place Serial.write happens.
void txTask(void* pv) {
  FramePacket p;
  for (;;) {
    if (xQueueReceive(txQueue, &p, portMAX_DELAY) == pdTRUE) {
      Serial.write((const uint8_t*)&p, sizeof(FramePacket));
    }
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(100);
  Serial.setTxBufferSize(8192);

  // ADC settings
  analogReadResolution(12); // 0..4095
  // If your input range needs it:
  // analogSetAttenuation(ADC_11db);

  // I2C + IMU
  Wire.begin(21, 22);
  Wire.setClock(I2C_HZ);
  delay(50);

  bool ok = bno.begin();
  delay(20);
  if (ok) bno.setExtCrystalUse(true);

  txQueue = xQueueCreate(TX_QUEUE_LEN, sizeof(FramePacket));

  // Core pinning / priorities:
  // ADC task (1kHz) gets highest prio to reduce jitter.
  xTaskCreatePinnedToCore(adcTask, "adcTask", 4096, NULL, 4, NULL, 1);
  xTaskCreatePinnedToCore(imuTask, "imuTask", 4096, NULL, 3, NULL, 0);
  xTaskCreatePinnedToCore(txTask,  "txTask",  4096, NULL, 2, NULL, 0);
}

void loop() {}
