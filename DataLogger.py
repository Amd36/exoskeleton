"""
DataLogger.py
-------------
A class for real-time serial data acquisition and buffering.

UPDATED for new binary FRAME protocol (Option A):

FramePacket (little-endian, 194 bytes total):

uint16_t sync        = 0xA55A
uint8_t  version     = 1
uint8_t  type        = 1  (FramePacket)
uint16_t frame_seq   = increments @ 100 Hz
uint32_t t_us        = micros() at start of frame
uint32_t adc_base_idx= index of first ADC sample in this frame (1 kHz counter)

uint16_t adc[10][8]  = 10 samples (1 ms apart) x 8 channels
int16_t  imu[9]      = accel/gyro/mag (scaled x100), cached; mag updates @20Hz

uint16_t crc16       = CRC16-CCITT (poly 0x1021, init 0xFFFF) over all bytes except crc16

Behavior:
- Each received frame expands into 10 "rows" pushed to the queue.
- Each row is: (adc_sample_index, [adc1..adc8, imu1..imu9])  -> 17 values like before.
"""

import serial
import threading
import queue
from collections import deque
import os
import numpy as np
import struct
import time


class DataLogger:
    def __init__(self, port, baud_rate, num_channels, buffer_length=20000, samples_per_event=2):
        self.port = port
        self.baud_rate = baud_rate
        self.num_channels = num_channels
        self.buffer_length = buffer_length
        self.samples_per_event = samples_per_event

        self.row_queue = queue.Queue(maxsize=10000)

        self.channels = [
            deque([0] * buffer_length, maxlen=buffer_length)
            for _ in range(num_channels)
        ]

        # Now stores ADC sample indices (32-bit-ish, Python int)
        self.indices = deque([0] * buffer_length, maxlen=buffer_length)

        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.serial_connection = None

        # --- Binary protocol configuration ---
        self.SYNC_WORD = 0xA55A
        self.SYNC_BYTES = struct.pack("<H", self.SYNC_WORD)

        self.PKT_VER = 1
        self.PKT_TYPE_FRAME = 1
        self.ADC_BLOCK = 10
        self.ADC_CH = 8
        self.IMU_CH = 9

        # Struct:
        # < H B B H I I 80H 9h H
        # sync, ver, type, frame_seq, t_us, adc_base_idx, adc[80], imu[9], crc16
        self.PACKET_STRUCT = struct.Struct("<HBBHII80H9hH")
        self.PACKET_SIZE = self.PACKET_STRUCT.size  # 194

        if self.num_channels != 17:
            print(
                f"Warning: num_channels={self.num_channels}, but protocol expands to 17 values per ADC sample "
                f"(8 ADC + 9 IMU)."
            )

    # ---------- CRC16 (must match ESP32) ----------

    @staticmethod
    def _crc16_ccitt(data: bytes) -> int:
        """
        CRC16-CCITT (poly=0x1021, init=0xFFFF), same as ESP32 code.
        """
        crc = 0xFFFF
        for b in data:
            crc ^= (b << 8) & 0xFFFF
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        return crc

    def _parse_frame_packet(self, packet_bytes: bytes):
        """
        Parse a 194-byte FramePacket and EXPAND into 10 rows.

        Returns:
            list[(index:int, values:list[float])] or None
        """
        if len(packet_bytes) != self.PACKET_SIZE:
            return None

        # Quick sync check before unpack
        if packet_bytes[0:2] != self.SYNC_BYTES:
            return None

        # Verify CRC16
        recv_crc = struct.unpack_from("<H", packet_bytes, self.PACKET_SIZE - 2)[0]
        calc_crc = self._crc16_ccitt(packet_bytes[:-2])
        if recv_crc != calc_crc:
            return None

        fields = self.PACKET_STRUCT.unpack(packet_bytes)

        sync = fields[0]
        ver  = fields[1]
        typ  = fields[2]

        if sync != self.SYNC_WORD or ver != self.PKT_VER or typ != self.PKT_TYPE_FRAME:
            return None

        # fields layout:
        # 0:sync, 1:ver, 2:type, 3:frame_seq, 4:t_us, 5:adc_base_idx,
        # 6..(6+79): adc flat (80 vals),
        # next 9: imu,
        # last: crc
        frame_seq   = fields[3]
        t_us        = fields[4]
        adc_base_idx = fields[5]

        adc_flat = fields[6:6 + (self.ADC_BLOCK * self.ADC_CH)]  # 80 uint16
        imu_vals = fields[6 + (self.ADC_BLOCK * self.ADC_CH): 6 + (self.ADC_BLOCK * self.ADC_CH) + self.IMU_CH]

        # Expand into 10 rows: each row keeps 8 ADC + same IMU cache
        rows = []
        for i in range(self.ADC_BLOCK):
            base = i * self.ADC_CH
            adc_vals = adc_flat[base:base + self.ADC_CH]
            values = list(adc_vals) + list(imu_vals)

            if len(values) != self.num_channels:
                # If user configured different num_channels, refuse malformed
                if self.num_channels != 17:
                    # allow truncate/extend? safer to discard
                    return None
                return None

            sample_index = int(adc_base_idx + i)  # 1kHz timeline index
            # Convert to floats for compatibility with existing plotting/saving code
            rows.append((sample_index, [float(v) for v in values]))

        return rows

    # ---------- Serial reader ----------

    def serial_reader(self, timeout=0.05):
        try:
            self.serial_connection = serial.Serial(
                self.port,
                self.baud_rate,
                timeout=timeout
            )
            print(f"Serial connection opened: {self.port} at {self.baud_rate} baud")
        except Exception as e:
            print(f"Serial reader failed to open {self.port}: {e}")
            return

        buf = bytearray()

        while not self.reader_stop.is_set():
            try:
                chunk = self.serial_connection.read(4096)
                if not chunk:
                    continue

                buf.extend(chunk)

                while True:
                    if len(buf) < self.PACKET_SIZE:
                        break

                    sync_idx = buf.find(self.SYNC_BYTES)
                    if sync_idx == -1:
                        # keep last 1 byte in case it's half sync
                        if len(buf) > 1:
                            del buf[:-1]
                        break

                    if sync_idx > 0:
                        del buf[:sync_idx]

                    if len(buf) < self.PACKET_SIZE:
                        break

                    packet = bytes(buf[:self.PACKET_SIZE])
                    del buf[:self.PACKET_SIZE]

                    rows = self._parse_frame_packet(packet)
                    if rows is None:
                        continue

                    # push 10 expanded rows
                    for row in rows:
                        try:
                            self.row_queue.put_nowait(row)
                        except queue.Full:
                            # drop oldest, then try again
                            try:
                                _ = self.row_queue.get_nowait()
                                self.row_queue.put_nowait(row)
                            except queue.Empty:
                                pass

            except Exception as e:
                print("Serial reader error:", e)
                continue

        if self.serial_connection:
            self.serial_connection.close()
            print("Serial connection closed")

    # ---------- Control methods ----------

    def start_logging(self):
        if self.reader_thread is not None and self.reader_thread.is_alive():
            print("DataLogger already running")
            return

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(
            target=self.serial_reader,
            daemon=True
        )
        self.reader_thread.start()
        print("DataLogger started")

    def stop_logging(self):
        if self.reader_thread is None:
            return

        self.reader_stop.set()
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=0.5)
        print("DataLogger stopped")

    # ---------- Queue + buffer handling ----------

    def read_event(self):
        rows = []
        for _ in range(self.samples_per_event):
            try:
                rows.append(self.row_queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def update_buffers(self):
        drained = 0
        try:
            while True:
                try:
                    item = self.row_queue.get_nowait()
                except queue.Empty:
                    break

                if not isinstance(item, tuple) or len(item) != 2:
                    continue

                index, row = item

                if len(row) != self.num_channels:
                    continue

                self.indices.append(index)
                for c in range(self.num_channels):
                    self.channels[c].append(row[c])
                drained += 1

        except Exception as e:
            print("Error in update_buffers:", e)

        return drained

    def get_channel_data(self, channel_index, max_points=None):
        if channel_index >= self.num_channels:
            raise ValueError(f"Channel index {channel_index} out of range")

        buf_len = len(self.channels[channel_index])
        if max_points is None or buf_len <= max_points:
            step = 1
        else:
            step = max(1, buf_len // max_points)

        x = list(range(0, buf_len, step))
        y = list(self.channels[channel_index])[::step]
        return x, y

    def get_all_channel_data(self, max_points=None):
        return [self.get_channel_data(i, max_points) for i in range(self.num_channels)]

    def get_adc_data(self, max_points=None):
        adc_channels = min(8, self.num_channels)
        return [self.get_channel_data(i, max_points) for i in range(adc_channels)]

    def get_imu_data(self, max_points=None):
        if self.num_channels < 17:
            return {'accelerometer': [], 'gyroscope': [], 'magnetometer': []}

        imu_data = {
            'accelerometer': [self.get_channel_data(8 + i, max_points) for i in range(3)],
            'gyroscope':     [self.get_channel_data(11 + i, max_points) for i in range(3)],
            'magnetometer':  [self.get_channel_data(14 + i, max_points) for i in range(3)]
        }
        return imu_data

    def clear_buffers(self):
        for channel in self.channels:
            channel.clear()
            for _ in range(self.buffer_length):
                channel.append(0)

        self.indices.clear()
        for _ in range(self.buffer_length):
            self.indices.append(0)

    def get_queue_size(self):
        return self.row_queue.qsize()

    def is_logging(self):
        return (
            self.reader_thread is not None and
            self.reader_thread.is_alive() and
            not self.reader_stop.is_set()
        )

    def read_exact_packets(self, target_packets, timeout=0.05, max_runtime_s=30.0):
        """
        NOTE (updated meaning):
        - target_packets = number of *frames* to capture (each frame expands to 10 samples).
        - returned 'data' length will be target_packets * 10 (unless timeout).
        """
        try:
            ser = serial.Serial(self.port, self.baud_rate, timeout=timeout)
        except Exception as e:
            print(f"Failed to open serial port {self.port}: {e}")
            return {'indices': [], 'data': [], 'stats': {}}

        buf = bytearray()
        indices = []
        data = []
        bad_crc = 0
        total_bytes = 0
        frames = 0

        t0 = time.time()
        try:
            while frames < target_packets:
                if (time.time() - t0) > max_runtime_s:
                    print(f"Timeout: captured {frames}/{target_packets} frames in {max_runtime_s}s")
                    break

                chunk = ser.read(4096)
                if not chunk:
                    continue
                total_bytes += len(chunk)
                buf.extend(chunk)

                while True:
                    if len(buf) < self.PACKET_SIZE:
                        break

                    si = buf.find(self.SYNC_BYTES)
                    if si == -1:
                        if len(buf) > 1:
                            del buf[:-1]
                        break

                    if si > 0:
                        del buf[:si]

                    if len(buf) < self.PACKET_SIZE:
                        break

                    pkt = bytes(buf[:self.PACKET_SIZE])
                    del buf[:self.PACKET_SIZE]

                    rows = self._parse_frame_packet(pkt)
                    if rows is None:
                        bad_crc += 1
                        continue

                    frames += 1
                    for idx, values in rows:
                        indices.append(idx)
                        data.append(values)

                    if frames >= target_packets:
                        break

        finally:
            ser.close()

        stats = {
            "valid_frames": frames,
            "expanded_samples": len(data),
            "bad_crc_frames": bad_crc,
            "bytes_read": total_bytes,
            "elapsed_s": time.time() - t0
        }
        return {'indices': indices, 'data': data, 'stats': stats}

    def detect_gaps(self, indices=None):
        """
        Detect missing ADC sample indices (no wrap handling by default, since indices are 32-bit+).
        """
        if indices is None:
            indices = list(self.indices)

        if len(indices) < 2:
            return {'missing_count': 0, 'gaps': [], 'is_continuous': True}

        missing = 0
        gaps = []

        for a, b in zip(indices[:-1], indices[1:]):
            exp = a + 1
            if b == exp:
                continue
            dist = b - exp
            if dist > 0:
                missing += dist
                gaps.append((a, b, dist))

        return {
            'missing_count': missing,
            'gaps': gaps,
            'is_continuous': missing == 0
        }

    # ---------- Saving (unchanged) ----------

    def save_data(
        self,
        filename_prefix="channel",
        file_extension=".csv",
        save_directory=".saved_data",
        skip_initial_zeros=True,
        sample_rate=1000.0,
        timestamp_start=0.0,
        combined=False,
        include_indices=False,
        indices_data=None,
        channel_data=None
    ):
        created_files = []

        if not os.path.exists(save_directory):
            os.makedirs(save_directory)

        def _trim_initial_zeros(arr):
            if not skip_initial_zeros or len(arr) == 0:
                return arr
            non_zero_indices = np.nonzero(arr)[0]
            if len(non_zero_indices) > 0:
                return arr[non_zero_indices[0]:]
            return arr

        if channel_data is None:
            channel_arrays = []
            for channel_idx in range(self.num_channels):
                ch_data = np.array(list(self.channels[channel_idx]))
                ch_data = _trim_initial_zeros(ch_data)
                channel_arrays.append(ch_data)
        else:
            channel_arrays = [np.array(ch) for ch in channel_data]

        if indices_data is None:
            index_array = np.array(list(self.indices))
            index_array = _trim_initial_zeros(index_array) if skip_initial_zeros else index_array
        else:
            index_array = np.array(indices_data)

        channel_map = {
            1: 'piezo1', 2: 'piezo2', 3: 'piezo3', 4: 'piezo4',
            5: 'piezo5', 6: 'piezo6', 7: 'fsr1', 8: 'fsr2',
            9: 'acc_x', 10: 'acc_y', 11: 'acc_z',
            12: 'gyro_x', 13: 'gyro_y', 14: 'gyro_z',
            15: 'mag_x', 16: 'mag_y', 17: 'mag_z'
        }

        if combined:
            if len(channel_arrays) == 0:
                return created_files

            lengths = [len(a) for a in channel_arrays]
            min_len = min(lengths)
            if min_len == 0:
                print("Warning: one or more channels have no data for combined output")

            aligned = [
                a[-min_len:] if len(a) >= min_len
                else np.pad(a, (min_len - len(a), 0), 'constant')
                for a in channel_arrays
            ]
            if sample_rate is None or sample_rate <= 0:
                timestamps = np.arange(min_len) + timestamp_start
            else:
                timestamps = (np.arange(min_len) / float(sample_rate)) + float(timestamp_start)

            try:
                columns = [timestamps]
                header_parts = ['timestamp']

                if include_indices and len(index_array) > 0:
                    aligned_indices = index_array[-min_len:] if len(index_array) >= min_len else np.pad(
                        index_array, (min_len - len(index_array), 0), 'constant'
                    )
                    columns.append(aligned_indices)
                    header_parts.append('index')

                columns.extend(aligned)
                header_parts.extend([channel_map[idx + 1] for idx in range(self.num_channels)])

                data_matrix = np.column_stack(columns)
                filename = f"{filename_prefix}_all{file_extension}"
                filepath = os.path.join(save_directory, filename)
                header = ','.join(header_parts)

                np.savetxt(filepath, data_matrix, delimiter=',', header=header, comments='', fmt='%.6f')
                created_files.append(filepath)
                print(f"Saved combined data to {filepath} (CSV, {min_len} samples)")
            except Exception as e:
                print(f"Error saving combined CSV: {e}")

            return created_files

        for channel_idx, channel_data in enumerate(channel_arrays):
            if channel_data is None or len(channel_data) == 0:
                print(f"Skipping channel {channel_idx + 1}: no data to save")
                continue

            n = len(channel_data)
            if sample_rate is None or sample_rate <= 0:
                timestamps = np.arange(n) + timestamp_start
            else:
                timestamps = (np.arange(n) / float(sample_rate)) + float(timestamp_start)

            filename = f"{filename_prefix}{channel_map[channel_idx + 1]}{file_extension}"
            filepath = os.path.join(save_directory, filename)

            try:
                if file_extension.lower() in ['.dat', '.bin']:
                    channel_data.astype(np.float64).tofile(filepath)
                    print(f"Saved channel {channel_idx + 1} data to {filepath} (binary, {n} samples)")
                else:
                    out_mat = np.column_stack((timestamps, channel_data))
                    np.savetxt(filepath, out_mat, delimiter=',', header='timestamp,value', comments='', fmt='%.6f')
                    print(f"Saved channel {channel_idx + 1} data to {filepath} (CSV, {n} samples)")

                created_files.append(filepath)

            except Exception as e:
                print(f"Error saving channel {channel_idx + 1} data: {e}")

        if created_files:
            print(f"Successfully saved {len(created_files)} file(s)")

        return created_files


if __name__ == "__main__":
    print("This module defines DataLogger class for binary real-time serial data acquisition.")
