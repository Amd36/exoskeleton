"""
DataLogger.py
-------------
A class for real-time serial data acquisition and buffering.
Now adapted for a binary packet protocol:

Packet format (little-endian, 40 bytes total):

uint16_t sync      = 0xA55A
uint16_t index     = sampleIndex & 0xFFFF
uint16_t adc[8]    = 0..4096
int16_t  imu[9]    = accel/gyro/mag
uint16_t checksum  = 16-bit sum of all previous bytes (mod 65536)
"""

import serial
import threading
import queue
from collections import deque
import os
import numpy as np
import struct


class DataLogger:
    def __init__(self, port, baud_rate, num_channels, buffer_length=20000, samples_per_event=2):
        """
        Initialize the DataLogger.

        Args:
            port (str): Serial port path (e.g., '/dev/ttyUSB0' or 'COM8')
            baud_rate (int): Serial communication baud rate
            num_channels (int): Number of data channels expected (17 for 8 ADC + 9 IMU)
            buffer_length (int): Maximum number of samples to keep in buffer
            samples_per_event (int): Expected samples per event from device
        """
        self.port = port
        self.baud_rate = baud_rate
        self.num_channels = num_channels
        self.buffer_length = buffer_length
        self.samples_per_event = samples_per_event

        # Thread-safe queue to receive parsed rows from the reader thread
        self.row_queue = queue.Queue(maxsize=10000)

        # Data buffers: one deque per channel for efficient append/pop
        self.channels = [
            deque([0] * buffer_length, maxlen=buffer_length)
            for _ in range(num_channels)
        ]

        # Reader thread control
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.serial_connection = None

        # --- Binary protocol configuration ---
        # sync word: 0xA55A (little-endian: 0x5A, 0xA5)
        self.SYNC_WORD = 0xA55A
        self.SYNC_BYTES = struct.pack("<H", self.SYNC_WORD)

        # struct format: <HH8H9hH  (little-endian)
        # sync, index, 8x uint16, 9x int16, checksum (uint16)
        self.PACKET_STRUCT = struct.Struct("<HH8H9hH")
        self.PACKET_SIZE = self.PACKET_STRUCT.size  # should be 40

        if self.num_channels != 17:
            print(
                f"Warning: num_channels={self.num_channels}, but binary protocol expects 17 (8 ADC + 9 IMU)."
            )

    # ---------- Binary parsing helpers ----------

    @staticmethod
    def _compute_checksum(packet_bytes_without_checksum: bytes) -> int:
        """
        Compute 16-bit checksum as sum of all bytes modulo 65536.
        """
        return sum(packet_bytes_without_checksum) & 0xFFFF

    def _parse_packet(self, packet_bytes):
        """
        Parse a 40-byte binary packet into a list of channel values.

        Args:
            packet_bytes (bytes): Raw packet (must be exactly PACKET_SIZE)

        Returns:
            list or None: List of floats [adc1..adc8, imu1..imu9] if valid; None on checksum error.
        """
        if len(packet_bytes) != self.PACKET_SIZE:
            return None

        # Verify checksum
        expected = self._compute_checksum(packet_bytes[:-2])
        fields = self.PACKET_STRUCT.unpack(packet_bytes)
        recv_checksum = fields[-1]

        if expected != recv_checksum:
            # checksum mismatch -> discard
            # print("Checksum mismatch in packet (expected %04X, got %04X)" % (expected, recv_checksum))
            return None

        # fields: [sync, index, adc[0..7], imu[0..8], checksum]
        sync = fields[0]
        if sync != self.SYNC_WORD:
            # Not expected sync, discard
            return None

        # index = fields[1]  # sample index if you ever need it
        adc_vals = fields[2:2 + 8]
        imu_vals = fields[2 + 8:2 + 8 + 9]

        values = list(adc_vals) + list(imu_vals)
        if len(values) != self.num_channels:
            # Should always be 17
            # print(f"Warning: expected {self.num_channels} values, got {len(values)}")
            return None

        # Convert to floats for consistency with previous CSV-based API
        return [float(v) for v in values]

    # ---------- Serial reader for binary stream ----------

    def serial_reader(self, timeout=0.05):
        """
        Background thread that reads binary serial data, parses packets,
        and pushes rows into row_queue.

        Args:
            timeout (float): Serial read timeout in seconds
        """
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

        # Optional: if your ESP32 sends a text banner like "BINARY_STREAM_START\n",
        # it will just appear as random bytes and be skipped until sync is found.
        while not self.reader_stop.is_set():
            try:
                chunk = self.serial_connection.read(1024)
                if not chunk:
                    continue

                buf.extend(chunk)

                # Try to extract as many complete packets as possible
                while True:
                    # Need at least enough for sync + full packet
                    if len(buf) < self.PACKET_SIZE:
                        break

                    # Find sync bytes in buffer
                    sync_idx = buf.find(self.SYNC_BYTES)
                    if sync_idx == -1:
                        # No sync found; keep only last 1 byte (in case it's first half of sync)
                        if len(buf) > 1:
                            del buf[:-1]
                        break

                    # If sync not at start, discard bytes before it
                    if sync_idx > 0:
                        del buf[:sync_idx]

                    if len(buf) < self.PACKET_SIZE:
                        # Wait until we have full packet
                        break

                    # Now buf[0:PACKET_SIZE] is a candidate packet
                    packet = bytes(buf[:self.PACKET_SIZE])
                    del buf[:self.PACKET_SIZE]

                    parsed = self._parse_packet(packet)
                    if parsed is None:
                        # Invalid packet -> continue searching; sync might have been a coincidence
                        continue

                    # Push parsed row into queue, drop oldest if full
                    try:
                        self.row_queue.put_nowait(parsed)
                    except queue.Full:
                        try:
                            _ = self.row_queue.get_nowait()
                            self.row_queue.put_nowait(parsed)
                        except queue.Empty:
                            pass

            except Exception as e:
                print("Serial reader error:", e)
                continue

        if self.serial_connection:
            self.serial_connection.close()
            print("Serial connection closed")

    # ---------- Control methods (unchanged) ----------

    def start_logging(self):
        """Start the data logging thread."""
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
        """Stop the data logging thread."""
        if self.reader_thread is None:
            return

        self.reader_stop.set()
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=0.5)
        print("DataLogger stopped")

    # ---------- Queue + buffer handling (mostly unchanged) ----------

    def read_event(self):
        """
        Read available rows from the queue (non-blocking).

        Returns:
            list: List of parsed data rows
        """
        rows = []
        for _ in range(self.samples_per_event):
            try:
                rows.append(self.row_queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def update_buffers(self):
        """
        Drain the queue and update channel buffers.

        Returns:
            int: Number of rows processed
        """
        drained = 0
        try:
            while True:
                try:
                    row = self.row_queue.get_nowait()
                except queue.Empty:
                    break

                if len(row) != self.num_channels:
                    # Skip malformed row
                    # print(f"Skipping malformed row of length {len(row)}")
                    continue

                for c in range(self.num_channels):
                    self.channels[c].append(row[c])
                drained += 1

        except Exception as e:
            print("Error in update_buffers:", e)

        return drained

    def get_channel_data(self, channel_index, max_points=None):
        """
        Get data from a specific channel, optionally downsampled.

        Args:
            channel_index (int): Channel index (0-based)
            max_points (int, optional): Maximum points to return (for downsampling)

        Returns:
            tuple: (x_data, y_data) for plotting
        """
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
        """
        Get data from all channels.

        Args:
            max_points (int, optional): Maximum points per channel

        Returns:
            list: List of (x_data, y_data) tuples for each channel
        """
        return [self.get_channel_data(i, max_points) for i in range(self.num_channels)]

    def get_adc_data(self, max_points=None):
        """
        Get data from ADC channels (channels 0-7).

        Args:
            max_points (int, optional): Maximum points per channel

        Returns:
            list: List of (x_data, y_data) tuples for ADC channels
        """
        adc_channels = min(8, self.num_channels)
        return [self.get_channel_data(i, max_points) for i in range(adc_channels)]

    def get_imu_data(self, max_points=None):
        """
        Get data from IMU channels (channels 8-16: acc_x,acc_y,acc_z,
        gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z).

        Args:
            max_points (int, optional): Maximum points per channel

        Returns:
            dict: Dictionary with 'accelerometer', 'gyroscope', 'magnetometer' keys,
                  each containing list of (x_data, y_data) tuples for x,y,z axes
        """
        if self.num_channels < 17:
            return {'accelerometer': [], 'gyroscope': [], 'magnetometer': []}

        imu_data = {
            'accelerometer': [self.get_channel_data(8 + i, max_points) for i in range(3)],
            'gyroscope': [self.get_channel_data(11 + i, max_points) for i in range(3)],
            'magnetometer': [self.get_channel_data(14 + i, max_points) for i in range(3)]
        }
        return imu_data

    def clear_buffers(self):
        """Clear all channel buffers."""
        for channel in self.channels:
            channel.clear()
            # Refill with zeros to maintain buffer length
            for _ in range(self.buffer_length):
                channel.append(0)

    def get_queue_size(self):
        """Get current queue size."""
        return self.row_queue.qsize()

    def is_logging(self):
        """Check if logging is active."""
        return (
            self.reader_thread is not None and
            self.reader_thread.is_alive() and
            not self.reader_stop.is_set()
        )

    # ---------- Saving (unchanged, still works with floats) ----------

    def save_data(
        self,
        filename_prefix="channel",
        file_extension=".csv",
        save_directory=".saved_data",
        skip_initial_zeros=True,
        sample_rate=1000.0,
        timestamp_start=0.0,
        combined=False
    ):
        """
        Save data from channels to CSV files with timestamps.

        By default this writes one CSV per channel containing two columns: timestamp, value.
        Optionally a single combined CSV with columns `timestamp,ch1,ch2,...` can be created via
        `combined=True`.

        Args:
            filename_prefix (str): Prefix for the output files (default: "channel")
            file_extension (str): File extension (default: ".csv")
            save_directory (str): Directory to save files (default: ".saved_data")
            skip_initial_zeros (bool): Skip leading zeros from buffer initialization (default: True)
            sample_rate (float): Sampling rate in Hz used to generate timestamps (default: 1000.0)
            timestamp_start (float): Starting timestamp in seconds for the first sample (default: 0.0)
            combined (bool): If True, produce a single combined CSV with all channels (default: False)

        Returns:
            list: List of filenames that were created
        """
        created_files = []

        # Ensure save directory exists
        if not os.path.exists(save_directory):
            os.makedirs(save_directory)

        def _trim_initial_zeros(arr):
            if not skip_initial_zeros or len(arr) == 0:
                return arr
            non_zero_indices = np.nonzero(arr)[0]
            if len(non_zero_indices) > 0:
                return arr[non_zero_indices[0]:]
            return arr

        channel_arrays = []
        for channel_idx in range(self.num_channels):
            channel_data = np.array(list(self.channels[channel_idx]))
            channel_data = _trim_initial_zeros(channel_data)
            channel_arrays.append(channel_data)

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
                data_matrix = np.column_stack([timestamps] + aligned)
                filename = f"{filename_prefix}_all{file_extension}"
                filepath = os.path.join(save_directory, filename)
                header = 'timestamp,' + ','.join(
                    [channel_map[idx + 1] for idx in range(self.num_channels)]
                )
                np.savetxt(
                    filepath, data_matrix, delimiter=',',
                    header=header, comments='', fmt='%.6f'
                )
                created_files.append(filepath)
                print(f"Saved combined data to {filepath} (text CSV, {min_len} samples)")
            except Exception as e:
                print(f"Error saving combined CSV: {e}")

            return created_files

        # Per-channel CSVs
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
                    print(
                        f"Saved channel {channel_idx + 1} data to {filepath} "
                        f"(binary format, {n} samples). Timestamps not included for binary files."
                    )
                else:
                    out_mat = np.column_stack((timestamps, channel_data))
                    np.savetxt(
                        filepath, out_mat, delimiter=',',
                        header='timestamp,value', comments='', fmt='%.6f'
                    )
                    print(
                        f"Saved channel {channel_idx + 1} data to {filepath} "
                        f"(CSV, {n} samples)"
                    )

                created_files.append(filepath)

            except Exception as e:
                print(f"Error saving channel {channel_idx + 1} data: {e}")

        if created_files:
            print(f"Successfully saved {len(created_files)} file(s)")

        return created_files


if __name__ == "__main__":
    print("This module defines DataLogger class for binary real-time serial data acquisition.")
