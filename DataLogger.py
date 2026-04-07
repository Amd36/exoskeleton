"""
DataLogger.py
-------------
A class for real-time serial data acquisition and buffering.

Updated for the DAQ_System binary FRAME protocol:

FramePacket (little-endian, 112 bytes total):

uint16_t sync        = 0xA55A
uint8_t  version     = 1
uint8_t  type        = 1  (FramePacket)
uint16_t frame_seq   = increments @ 100 Hz
uint32_t t_us        = micros() at start of frame
uint32_t adc_base_idx= index of first ADC sample in this frame (500 Hz counter)

uint16_t adc[5][6]   = 5 samples (2 ms apart) x 6 channels
int16_t  imu[18]     = two BNO055 IMUs; acc/gyro/mag for each, scaled x100

uint16_t crc16       = CRC16-CCITT (poly 0x1021, init 0xFFFF) over all bytes except crc16

Behavior:
- Each received frame expands into 5 "rows" pushed to the queue.
- Each row is: (adc_sample_index, [adc1..adc6, imu0_9axis, imu1_9axis]).
- ADC values are raw 12-bit counts. IMU values are converted to physical units by dividing by 100.
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
    SYNC_WORD = 0xA55A
    SYNC_BYTES = struct.pack("<H", SYNC_WORD)
    PKT_VER = 1
    PKT_TYPE_FRAME = 1

    ADC_RATE_HZ = 500.0
    FRAME_RATE_HZ = 100.0
    ADC_BLOCK = 5
    ADC_CH = 6
    IMU_COUNT = 2
    IMU_CH_PER_SENSOR = 9
    IMU_CH = IMU_COUNT * IMU_CH_PER_SENSOR
    IMU_SCALE = 100.0
    TOTAL_CHANNELS = ADC_CH + IMU_CH

    ADC_CHANNEL_NAMES = (
        "adc0_gpio36",
        "adc1_gpio39",
        "adc2_gpio34",
        "adc3_gpio35",
        "adc4_gpio32",
        "adc5_gpio33",
    )
    IMU_AXIS_NAMES = ("acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z", "mag_x", "mag_y", "mag_z")
    CHANNEL_NAMES = list(ADC_CHANNEL_NAMES)
    for _imu_idx in range(IMU_COUNT):
        for _axis in IMU_AXIS_NAMES:
            CHANNEL_NAMES.append(f"imu{_imu_idx}_{_axis}")
    del _imu_idx, _axis
    PACKET_STRUCT = struct.Struct("<HBBHII30H18hH")
    PACKET_SIZE = PACKET_STRUCT.size

    def __init__(self, port, baud_rate, num_channels, buffer_length=20000, samples_per_event=2):
        self.port = port
        self.baud_rate = baud_rate
        if num_channels != self.TOTAL_CHANNELS:
            print(
                f"Warning: num_channels={num_channels}, but current DAQ protocol expands to "
                f"{self.TOTAL_CHANNELS} values per ADC sample ({self.ADC_CH} ADC + {self.IMU_CH} IMU). "
                f"Using {self.TOTAL_CHANNELS} channels."
            )
        self.num_channels = self.TOTAL_CHANNELS
        self.buffer_length = buffer_length
        self.samples_per_event = samples_per_event

        self.row_queue = queue.Queue(maxsize=10000)

        self.channels = [
            deque([0] * buffer_length, maxlen=buffer_length)
            for _ in range(self.num_channels)
        ]

        # Now stores ADC sample indices (32-bit-ish, Python int)
        self.indices = deque([0] * buffer_length, maxlen=buffer_length)

        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.serial_connection = None
        self.invalid_packets = 0
        self.valid_frames = 0

        if self.PACKET_SIZE != 112:
            raise RuntimeError(f"Unexpected packet size: {self.PACKET_SIZE} bytes")

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
        Parse one 112-byte FramePacket and expand it into 5 ADC-sample rows.

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
        # 6..35: adc flat (30 uint16 = 5 rows x 6 channels),
        # 36..53: imu flat (18 int16 = 2 IMUs x 9 channels),
        # 54: crc16
        frame_seq = fields[3]
        t_us = fields[4]
        adc_base_idx = fields[5]

        adc_count = self.ADC_BLOCK * self.ADC_CH
        adc_flat = fields[6:6 + adc_count]
        imu_raw = fields[6 + adc_count: 6 + adc_count + self.IMU_CH]
        imu_vals = [raw / self.IMU_SCALE for raw in imu_raw]

        # Expand into 5 rows: each ADC sample keeps the frame's latest cached IMU sample.
        rows = []
        for i in range(self.ADC_BLOCK):
            base = i * self.ADC_CH
            adc_vals = adc_flat[base:base + self.ADC_CH]
            values = list(adc_vals) + list(imu_vals)

            if len(values) != self.num_channels:
                return None

            sample_index = int(adc_base_idx + i)
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
                    rows = self._parse_frame_packet(packet)
                    if rows is None:
                        self.invalid_packets += 1
                        del buf[:1]
                        continue

                    del buf[:self.PACKET_SIZE]
                    self.valid_frames += 1

                    # push expanded rows
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

    def drain_rows(self, max_rows=None, update_buffers=True):
        """
        Drain parsed rows from the serial-reader queue.

        This keeps parsing centralized in DataLogger while letting UI callers
        capture the exact rows that were added during a save window.
        """
        rows = []

        try:
            while max_rows is None or len(rows) < max_rows:
                try:
                    item = self.row_queue.get_nowait()
                except queue.Empty:
                    break

                if not isinstance(item, tuple) or len(item) != 2:
                    continue

                index, row = item

                if len(row) != self.num_channels:
                    continue

                if update_buffers:
                    self.indices.append(index)
                    for c in range(self.num_channels):
                        self.channels[c].append(row[c])

                rows.append((index, row))

        except Exception as e:
            print("Error in drain_rows:", e)

        return rows

    def read_event(self):
        rows = []
        for _ in range(self.samples_per_event):
            try:
                rows.append(self.row_queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def update_buffers(self):
        return len(self.drain_rows(update_buffers=True))

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
        adc_channels = min(self.ADC_CH, self.num_channels)
        return [self.get_channel_data(i, max_points) for i in range(adc_channels)]

    def get_channel_names(self):
        return list(self.CHANNEL_NAMES[:self.num_channels])

    def _get_single_imu_data(self, imu_index, max_points=None):
        if not 0 <= imu_index < self.IMU_COUNT:
            raise ValueError(f"imu_index must be 0..{self.IMU_COUNT - 1}")

        start = self.ADC_CH + (imu_index * self.IMU_CH_PER_SENSOR)
        return {
            'accelerometer': [self.get_channel_data(start + i, max_points) for i in range(3)],
            'gyroscope': [self.get_channel_data(start + 3 + i, max_points) for i in range(3)],
            'magnetometer': [self.get_channel_data(start + 6 + i, max_points) for i in range(3)]
        }

    def get_imu_data(self, max_points=None, imu_index=None):
        """
        Return IMU plot data grouped by sensor.

        If imu_index is None, returns {'imu0': {...}, 'imu1': {...}} and also includes
        legacy top-level accelerometer/gyroscope/magnetometer aliases for IMU0.
        """
        if self.num_channels < self.TOTAL_CHANNELS:
            return {'imu0': {}, 'imu1': {}, 'accelerometer': [], 'gyroscope': [], 'magnetometer': []}

        if imu_index is not None:
            return self._get_single_imu_data(imu_index, max_points)

        imu_data = {
            f'imu{i}': self._get_single_imu_data(i, max_points)
            for i in range(self.IMU_COUNT)
        }

        # Backward-compatible aliases for callers that only plotted one IMU before.
        imu_data['accelerometer'] = imu_data['imu0']['accelerometer']
        imu_data['gyroscope'] = imu_data['imu0']['gyroscope']
        imu_data['magnetometer'] = imu_data['imu0']['magnetometer']
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

    def get_reader_stats(self):
        return {
            "valid_frames": self.valid_frames,
            "invalid_packets": self.invalid_packets,
            "queued_rows": self.get_queue_size()
        }

    def is_logging(self):
        return (
            self.reader_thread is not None and
            self.reader_thread.is_alive() and
            not self.reader_stop.is_set()
        )

    def read_exact_packets(self, target_packets, timeout=0.05, max_runtime_s=30.0):
        """
        Capture target_packets wire frames.

        Each current DAQ frame expands to 5 ADC samples, so returned 'data' length
        should be target_packets * 5 unless capture times out.
        """
        try:
            ser = serial.Serial(self.port, self.baud_rate, timeout=timeout)
        except Exception as e:
            print(f"Failed to open serial port {self.port}: {e}")
            return {'indices': [], 'data': [], 'stats': {}}

        buf = bytearray()
        indices = []
        data = []
        invalid_packets = 0
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
                    rows = self._parse_frame_packet(pkt)
                    if rows is None:
                        invalid_packets += 1
                        del buf[:1]
                        continue

                    del buf[:self.PACKET_SIZE]
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
            "invalid_packets": invalid_packets,
            "bad_crc_frames": invalid_packets,
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

    # ---------- Saving ----------

    def save_data(
        self,
        filename_prefix="channel",
        file_extension=".csv",
        save_directory=".saved_data",
        skip_initial_zeros=True,
        sample_rate=ADC_RATE_HZ,
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
            idx + 1: name
            for idx, name in enumerate(self.get_channel_names())
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
                header_parts.extend([
                    channel_map.get(idx + 1, f"channel_{idx + 1}")
                    for idx in range(self.num_channels)
                ])

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

            channel_name = channel_map.get(channel_idx + 1, f"channel_{channel_idx + 1}")
            filename = f"{filename_prefix}{channel_name}{file_extension}"
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
