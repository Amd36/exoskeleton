#include <Arduino.h>

#define BUFFER_SIZE 500
#define DATA_SIZE 1000

#define T1_INTERVAL 5000    // microseconds (5ms = 200Hz)
#define T2_INTERVAL 10000   // microseconds (10ms = 100Hz)

#define BAUD_RATE 115200

volatile int buffer[BUFFER_SIZE];
volatile int front = -1;
volatile int rear = -1;
volatile int bufferSize = 0;

int data[DATA_SIZE];
int data_index = 0;

hw_timer_t *timer1 = NULL;
hw_timer_t *timer2 = NULL;
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

volatile bool sendDataFlag = false;

// Generate random number (0-1000)
int randomInt() {
  return random(0, 1001);
}

// Timer1 ISR: enqueue random int
void IRAM_ATTR onTimer1() {
  portENTER_CRITICAL_ISR(&timerMux);
  if (bufferSize < BUFFER_SIZE) {
    if (front == -1) front = rear = 0;
    else rear = (rear + 1) % BUFFER_SIZE;

    buffer[rear] = randomInt();
    bufferSize++;
  }
  portEXIT_CRITICAL_ISR(&timerMux);
}

// Timer2 ISR: dequeue into data array, set flag to send
void IRAM_ATTR onTimer2() {
  portENTER_CRITICAL_ISR(&timerMux);
  int range = T2_INTERVAL / T1_INTERVAL;
  for (int i = 0; i < range; i++) {
    if (bufferSize > 0) {
      data[data_index] = buffer[front];
      if (front == rear) front = rear = -1;
      else front = (front + 1) % BUFFER_SIZE;
      bufferSize--;

      data_index = (data_index + 1) % DATA_SIZE;
    }
  }
  sendDataFlag = true;
  portEXIT_CRITICAL_ISR(&timerMux);
}

void setup() {
  Serial.begin(BAUD_RATE);
  delay(1000); // allow serial to stabilize
  randomSeed(analogRead(0));

  timer1 = timerBegin(0, 80, true); // prescaler 80 = 1us per tick
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
