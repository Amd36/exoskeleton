#include <Arduino.h>

#define BUFFER_SIZE 500 * 3  // each entry holds a single channel sample
#define DATA_SIZE 1000 * 3   // storing flattened triplets

#define T1_INTERVAL 5000    // microseconds (5ms = 200Hz)
#define T2_INTERVAL 10000   // microseconds (10ms = 100Hz)

#define BAUD_RATE 115200
#define ADC1_PIN 34
#define ADC2_PIN 35
#define ADC3_PIN 36

volatile int buffer[BUFFER_SIZE];
volatile int front = -1;
volatile int rear = -1;
volatile int bufferSize = 0;

int data[DATA_SIZE] = {0};

hw_timer_t *timer1 = NULL;
hw_timer_t *timer2 = NULL;
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

volatile bool sendDataFlag = false;

void IRAM_ATTR onTimer1() {
  portENTER_CRITICAL_ISR(&timerMux);

  if (bufferSize <= BUFFER_SIZE - 3) {
    int val1 = map(analogRead(ADC1_PIN), 0, 4095, 0, 1000);
    int val2 = map(analogRead(ADC2_PIN), 0, 4095, 0, 1000);
    int val3 = map(analogRead(ADC3_PIN), 0, 4095, 0, 1000);

    for (int i = 0; i < 3; ++i) {
      if (front == -1) front = rear = 0;
      else rear = (rear + 1) % BUFFER_SIZE;

      if (i == 0) buffer[rear] = val1;
      else if (i == 1) buffer[rear] = val2;
      else buffer[rear] = val3;

      bufferSize++;
    }
  }

  portEXIT_CRITICAL_ISR(&timerMux);
}

void IRAM_ATTR onTimer2() {
  portENTER_CRITICAL_ISR(&timerMux);

  int range = (T2_INTERVAL / T1_INTERVAL) * 3; // dequeue 6 samples (2 triplets)
  for (int i = 0; i < range; ++i) {
    if (bufferSize > 0) {
      int value = buffer[front];
      if (front == rear) front = rear = -1;
      else front = (front + 1) % BUFFER_SIZE;
      bufferSize--;

      // shift left
      for (int j = 0; j < (DATA_SIZE - 1); ++j) {
        data[j] = data[j + 1];
      }
      data[DATA_SIZE - 1] = value;

      sendDataFlag = true;
    }
  }

  portEXIT_CRITICAL_ISR(&timerMux);
}

void setup() {
  Serial.begin(BAUD_RATE);
  delay(1000);
  pinMode(ADC1_PIN, INPUT);
  pinMode(ADC2_PIN, INPUT);
  pinMode(ADC3_PIN, INPUT);
  randomSeed(analogRead(0));

  timer1 = timerBegin(0, 80, true);
  timerAttachInterrupt(timer1, &onTimer1, true);
  timerAlarmWrite(timer1, T1_INTERVAL, true);
  timerAlarmEnable(timer1);

  timer2 = timerBegin(1, 80, true);
  timerAttachInterrupt(timer2, &onTimer2, true);
  timerAlarmWrite(timer2, T2_INTERVAL, true);
  timerAlarmEnable(timer2);
}

void loop() {
  if (sendDataFlag) {
    portENTER_CRITICAL(&timerMux);
    sendDataFlag = false;
    portEXIT_CRITICAL(&timerMux);

    for (int i = 0; i < DATA_SIZE; i++) {
      Serial.print(data[i]);
      Serial.print(" ");
    }
    Serial.println();
  }
}
