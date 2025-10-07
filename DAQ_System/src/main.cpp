#include <Arduino.h>
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

// Circular FIFO buffer: 50 rows x 10 channels
constexpr size_t NUM_ROWS = 50;
constexpr size_t NUM_CH = 8;
// constexpr size_t NUM_CH = 2; 
using sample_t = uint16_t; // ADC values
static sample_t buffer[NUM_ROWS][NUM_CH];
static size_t buf_head = 0; // next write index
static size_t buf_tail = 0; // next read index
static size_t buf_count = 0; // number of rows stored

// Mutex to protect buffer access
SemaphoreHandle_t bufMutex = NULL;

// Sampling task: waits for notification from T1 ISR, reads NUM_CH ADC channels once,
// and pushes the row into the circular FIFO.
void samplingTask(void* pvParameters) {
  for (;;) {
    // Wait indefinitely for a notification from the ISR
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);

    // Read NUM_CH ADC channels (use analogRead on pins A0.. as configured)
    sample_t row[NUM_CH];
    // Map channels to pins — adjust these pin constants as needed for your hardware
    const int chPins[NUM_CH] = {34, 35, 36, 39, 32, 33, 25, 26};
    // const int chPins[NUM_CH] = {36, 39}; // --- IGNORE ---
    for (size_t i = 0; i < NUM_CH; ++i) {
      row[i] = static_cast<sample_t>(analogRead(chPins[i]));
    }

    // Push into circular buffer
    if (xSemaphoreTake(bufMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
      // Overwrite oldest if full (FIFO with overwrite) — user asked for FIFO queue; this keeps newest data
      buffer[buf_head][0] = row[0];
      for (size_t c = 0; c < NUM_CH; ++c) buffer[buf_head][c] = row[c];
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
// dequeue up to 2 rows (2 x NUM_CH) and print them.
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
            // Print the row as CSV: ch0,ch1,...ch9\n
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