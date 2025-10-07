#include <Arduino.h>
#include "callbacks.h"

// The queue handle is defined in main.cpp; declare it here so ISRs can queue events.
extern QueueHandle_t printQueue;

// The sampling task handle is declared in main.cpp; T1 ISR will notify it.
extern TaskHandle_t samplingTaskHandle;

// Event ID for T2 (printing request)
static const uint8_t EVT_T2 = 2;

// Timer 1 ISR: notify the sampling task to perform ADC reads in task context.
extern "C" void IRAM_ATTR T1_callback() {
  if (samplingTaskHandle != NULL) {
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    vTaskNotifyGiveFromISR(samplingTaskHandle, &xHigherPriorityTaskWoken);
    portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
  }
}

// Timer 2 ISR: enqueue a print request id. Use the FromISR variant.
extern "C" void IRAM_ATTR T2_callback() {
  if (printQueue != NULL) {
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    xQueueSendFromISR(printQueue, &EVT_T2, &xHigherPriorityTaskWoken);
    portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
  }
}
