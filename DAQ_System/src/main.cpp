#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BNO055.h>
#include <utility/imumaths.h>
#include <callbacks.h>

hw_timer_t* timer1 = nullptr;
hw_timer_t* timer2 = nullptr;

constexpr uint32_t TIMER_PRESCALER = 80; // 1 µs tick
constexpr uint64_t T1_PERIOD_US = 1000; // 1000 Hz
constexpr uint64_t T2_PERIOD_US = 2000; // 500 Hz

// FreeRTOS queue used to send simple event IDs from ISRs to a printing task.
QueueHandle_t printQueue = NULL;

// Event IDs (match callbacks.cpp)
static const uint8_t EVT_T1 = 1;
static const uint8_t EVT_T2 = 2;

// Forward declare sampling task handle so ISR can notify it (defined below)
TaskHandle_t samplingTaskHandle = NULL;

// Circular FIFO buffer: 50 rows x 17 channels (8 ADC + 9 BNO055)
constexpr size_t NUM_ROWS = 50;
constexpr size_t NUM_ADC_CH = 8; // Original ADC channels
constexpr size_t NUM_BNO055_CH = 9; // 3 acc + 3 gyro + 3 mag
constexpr size_t NUM_CH = NUM_ADC_CH + NUM_BNO055_CH; // Total: 17 channels
using sample_t = int16_t; // Use signed for compatibility with BNO055 data
static sample_t buffer[NUM_ROWS][NUM_CH];
static size_t buf_head = 0; // next write index
static size_t buf_tail = 0; // next read index
static size_t buf_count = 0; // number of rows stored

// Mutex to protect buffer access
SemaphoreHandle_t bufMutex = NULL;

// BNO055 sensor object using I2C with custom pins
Adafruit_BNO055 bno = Adafruit_BNO055(55, 0x29, &Wire);

// Sampling task: waits for notification from T1 ISR, reads ADC channels and BNO055 sensor data,
// and pushes the row into the circular FIFO.
void samplingTask(void* pvParameters) {
  for (;;) {
    // Wait indefinitely for a notification from the ISR
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);

    // Read data from both ADC channels and BNO055 sensor
    sample_t row[NUM_CH];
    
    // Read ADC channels first (channels 0-7)
    const int chPins[NUM_ADC_CH] = {34, 35, 36, 39, 32, 33, 25, 26};
    for (size_t i = 0; i < NUM_ADC_CH; ++i) {
      row[i] = static_cast<sample_t>(analogRead(chPins[i]));
    }
    
    // Read BNO055 sensor data (channels 8-16)
    sensors_event_t accelData, gyroData, magData;
    bno.getEvent(&accelData, Adafruit_BNO055::VECTOR_ACCELEROMETER);
    bno.getEvent(&gyroData, Adafruit_BNO055::VECTOR_GYROSCOPE);
    bno.getEvent(&magData, Adafruit_BNO055::VECTOR_MAGNETOMETER);
    
    // Convert to int16_t and store in buffer (multiply by 100 to preserve 2 decimal places)
    row[8] = static_cast<sample_t>(accelData.acceleration.x * 100);  // acc_x
    row[9] = static_cast<sample_t>(accelData.acceleration.y * 100);  // acc_y
    row[10] = static_cast<sample_t>(accelData.acceleration.z * 100); // acc_z
    row[11] = static_cast<sample_t>(gyroData.gyro.x * 100);          // gyro_x
    row[12] = static_cast<sample_t>(gyroData.gyro.y * 100);          // gyro_y
    row[13] = static_cast<sample_t>(gyroData.gyro.z * 100);          // gyro_z
    row[14] = static_cast<sample_t>(magData.magnetic.x * 100);       // mag_x
    row[15] = static_cast<sample_t>(magData.magnetic.y * 100);       // mag_y
    row[16] = static_cast<sample_t>(magData.magnetic.z * 100);       // mag_z

    // Push into circular buffer
    if (xSemaphoreTake(bufMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
      // Store all channels
      for (size_t c = 0; c < NUM_CH; ++c) {
        buffer[buf_head][c] = row[c];
      }
      buf_head = (buf_head + 1) % NUM_ROWS;
      if (buf_count < NUM_ROWS) {
        buf_count++;
      } else {
        // buffer full: advance tail to maintain FIFO
        buf_tail = (buf_tail + 1) % NUM_ROWS;
      }
      xSemaphoreGive(bufMutex);
    } else {
      // Failed to get mutex; drop sample
    }
  }
}

// Task: wait for print events and print from task context (safe for Serial). When EVT_T2 is received,
// dequeue up to 2 rows and print them as CSV: adc0,adc1,...,adc7,acc_x,acc_y,acc_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z
void printTask(void* pvParameters) {
  uint8_t evt;
  for (;;) {
    if (xQueueReceive(printQueue, &evt, portMAX_DELAY) == pdTRUE) {
      if (evt == EVT_T2) {
        // Dequeue up to 2 rows
        for (int r = 0; r < 2; ++r) {
          bool haveRow = false;
          sample_t row[NUM_CH];
          if (xSemaphoreTake(bufMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (buf_count > 0) {
              for (size_t c = 0; c < NUM_CH; ++c) row[c] = buffer[buf_tail][c];
              buf_tail = (buf_tail + 1) % NUM_ROWS;
              buf_count--;
              haveRow = true;
            }
            xSemaphoreGive(bufMutex);
          }

          if (haveRow) {
            // Print the row as CSV: adc0,adc1,...,adc7,acc_x,acc_y,acc_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z
            for (size_t c = 0; c < NUM_CH; ++c) {
              Serial.print(row[c]);
              if (c + 1 < NUM_CH) Serial.print(',');
            }
            Serial.println();
          } else {
            // No data available
            Serial.println("<no-data>");
          }
        }
      }
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  // Initialize I2C for BNO055 on pins 21 (SDA) and 22 (SCL)
  Wire.begin(21, 22);
  Wire.setClock(400000); // Set I2C frequency to 400kHz
  delay(100);
  
  // Initialize BNO055 sensor using Adafruit library
  if (!bno.begin()) {
    Serial.println("Failed to initialize BNO055 sensor");
    Serial.println("Check wiring and I2C address (0x29)");
    // Continue anyway, but sensor readings will be invalid
  } else {
    Serial.println("BNO055 initialized successfully");
    delay(1000);
    bno.setExtCrystalUse(true); // Use external crystal for better accuracy
  }

  // Create a queue that can hold up to 16 uint8_t event IDs
  printQueue = xQueueCreate(16, sizeof(uint8_t));
  if (printQueue == NULL) {
    // Failed to create queue; indicate error via Serial once and return.
    Serial.println("Failed to create printQueue");
    // We continue without queue but ISRs will check for NULL and skip.
  } else {
    // Create the print task with a small stack. Priority 1 is fine here.
    xTaskCreate(printTask, "printTask", 2048, NULL, 1, NULL);
  }

  // Create buffer mutex
  bufMutex = xSemaphoreCreateMutex();
  if (bufMutex == NULL) {
    Serial.println("Failed to create bufMutex");
  }

  // Create sampling task (it will block waiting for notifications)
  BaseType_t r = xTaskCreate(samplingTask, "samplingTask", 4096, NULL, 2, &samplingTaskHandle);
  if (r != pdPASS) {
    Serial.println("Failed to create samplingTask");
    samplingTaskHandle = NULL;
  }

  timer1 = timerBegin(0, TIMER_PRESCALER, true);
  timerAttachInterrupt(timer1, &T1_callback, true);
  timerAlarmWrite(timer1, T1_PERIOD_US, true);
  timerAlarmEnable(timer1);

  timer2 = timerBegin(1, TIMER_PRESCALER, true);
  timerAttachInterrupt(timer2, &T2_callback, true);
  timerAlarmWrite(timer2, T2_PERIOD_US, true);
  timerAlarmEnable(timer2);
}

void loop() {
  // Nothing to do in loop; printing is handled by the FreeRTOS task.
  delay(1000);
}