// callbacks.h
// Declarations for timer callback functions.
// Keep this header minimal: only the ISR prototypes.

#ifndef CALLBACKS_H
#define CALLBACKS_H

#include <Arduino.h>

// Timer 1 ISR (10 Hz)
extern "C" void IRAM_ATTR T1_callback();

// Timer 2 ISR (5 Hz)
extern "C" void IRAM_ATTR T2_callback();

#endif // CALLBACKS_H
