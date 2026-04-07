# DAQ System (ESP32 + Dual BNO055)

This project streams synchronized ADC and IMU data from an ESP32 to a host computer over UART as fixed-size binary packets.

## Overview

The firmware runs three FreeRTOS tasks:

1. ADC task at 500 Hz: reads 6 ADC channels every 2 ms.
2. IMU task at 100 Hz: reads two BNO055 sensors on the same I2C bus (addresses `0x28` and `0x29`), updates accel+gyro each cycle, updates magnetometer every 5th cycle (20 Hz), and caches the latest values.
3. TX task: sends completed packets over serial with `Serial.write`.

Every 5 ADC samples (10 ms total), one packet is assembled and queued for transmission. Packet output rate stays at 100 Hz.

## Serial Transport

- Physical link: UART (USB serial through ESP32 USB-UART bridge)
- Baud rate: 921600
- Framing style: fixed-size binary packet with sync word + version/type + CRC
- Packet period: 10 ms (100 packets/s)
- Packet size: 112 bytes

Estimated payload throughput:

- `112 bytes/packet x 100 packets/s = 11200 bytes/s`
- Payload-only bit rate: about `89.6 kbps`

This remains well below 921600 bps.

## Binary Packet Protocol

The sender serializes a packed C struct directly to UART (`#pragma pack(push, 1)`).
On ESP32, multi-byte values are little-endian.

- Packet struct: `FramePacket`
- Total size: `112` bytes

### Field layout (byte offsets)

- `0..1`: `sync` (`uint16`) = `0xA55A`
- `2`: `version` (`uint8`) = `1`
- `3`: `type` (`uint8`) = `1` (frame)
- `4..5`: `frame_seq` (`uint16`), increments per packet (100 Hz)
- `6..9`: `t_us` (`uint32`), `micros()` value at packet start
- `10..13`: `adc_base_idx` (`uint32`), index of the first ADC sample in the packet (500 Hz counter)
- `14..73`: ADC block, `5 x 6 x uint16 = 60` bytes
- `74..109`: IMU block, `18 x int16 = 36` bytes
- `110..111`: `crc16` (`uint16`) over bytes `0..109`

## ADC Data Section

ADC payload shape:

- `adc[row][channel]`
- rows: `0..4` (5 time samples per packet)
- channels: `0..5` (6 channels)

Channel-to-pin mapping:

- `ch0 -> GPIO36`
- `ch1 -> GPIO39`
- `ch2 -> GPIO34`
- `ch3 -> GPIO35`
- `ch4 -> GPIO32`
- `ch5 -> GPIO33`

ADC resolution is 12-bit (`0..4095`).

## IMU Data Section (Two BNO055)

Both IMUs share the same I2C bus (`Wire`) and are read in the same IMU task:

- IMU0: address `0x28`
- IMU1: address `0x29`

IMU payload order is fixed:

- `imu[0..8]`: IMU0 = `ax ay az gx gy gz mx my mz`
- `imu[9..17]`: IMU1 = `ax ay az gx gy gz mx my mz`

Each component is stored as `int16` after multiplying by `100` in firmware.

Host conversion:

- `physical_value = raw / 100.0`

Units (from Adafruit BNO055 events):

- Acceleration: m/s^2
- Gyroscope: rad/s
- Magnetometer: microtesla

## CRC Definition

CRC implementation is CRC16-CCITT:

- Polynomial: `0x1021`
- Initial value: `0xFFFF`
- Bit order: MSB-first processing per byte
- Final XOR: none
- Coverage: all packet bytes except the `crc16` field

On host, compute CRC over the first `110` bytes and compare to bytes `110..111`.

## Receiver Parsing Strategy

Recommended robust parser loop:

1. Read incoming UART bytes continuously.
2. Scan for sync word `0xA55A` in little-endian byte order (`5A A5`).
3. Once sync is found, gather a full `112`-byte candidate packet.
4. Validate `version` and `type`.
5. Validate CRC.
6. If valid, decode and publish the packet.
7. If invalid, shift by one byte and re-sync.

Using sync + fixed length + CRC allows clean recovery from misalignment and detection of corrupted packets.

## Timing Semantics

- `frame_seq` wraps every `65536` packets.
- At 100 Hz, wrap time is about `655.36 s` (about `10.9 min`).
- `adc_base_idx` should increase by `5` per packet under normal operation.
- `t_us` marks the first ADC sample instant for that packet.

These fields help detect drops and reconstruct timing on the host side.

## Build

PlatformIO environment in `platformio.ini`:

- board: `esp32doit-devkit-v1`
- framework: `arduino`

Protocol implementation source: `src/main.cpp`
