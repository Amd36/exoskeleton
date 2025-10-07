"""
DataLogger.py
-------------
A class for real-time serial data acquisition and buffering.
Handles serial communication, data parsing, and thread-safe buffering.
"""

import serial
import re
import threading
import queue
from collections import deque


class DataLogger:
    def __init__(self, port, baud_rate, num_channels, buffer_length=20000, samples_per_event=2):
        """
        Initialize the DataLogger.
        
        Args:
            port (str): Serial port path (e.g., '/dev/ttyUSB0' or 'COM3')
            baud_rate (int): Serial communication baud rate
            num_channels (int): Number of data channels expected
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
        self.channels = [deque([0] * buffer_length, maxlen=buffer_length) 
                        for _ in range(num_channels)]
        
        # Reader thread control
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.serial_connection = None
        
    def parse_line(self, line):
        """
        Parse a CSV or space-separated line into a list of ints.
        
        Args:
            line (str): Raw line from serial
            
        Returns:
            list or None: List of parsed integers, or None on parse error
        """
        line = line.strip()
        if not line or line == "<no-data>":
            return None
            
        # Allow commas and/or whitespace as separators
        parts = re.split(r'[,\s]+', line)
        try:
            vals = list(map(int, parts))
        except ValueError:
            print(f"Warning: non-integer in line: {line}")
            return None
            
        if len(vals) != self.num_channels:
            print(f"Warning: expected {self.num_channels} values, got {len(vals)}: {line}")
            return None
            
        return vals
    
    def serial_reader(self, timeout=0.05):
        """
        Background thread that reads serial, parses lines, and pushes rows into row_queue.
        
        Args:
            timeout (float): Serial read timeout in seconds
        """
        try:
            self.serial_connection = serial.Serial(self.port, self.baud_rate, timeout=timeout)
            print(f"Serial connection opened: {self.port} at {self.baud_rate} baud")
        except Exception as e:
            print(f"Serial reader failed to open {self.port}: {e}")
            return

        while not self.reader_stop.is_set():
            try:
                raw = self.serial_connection.readline()
                if not raw:
                    continue
                    
                line = raw.decode('utf-8', errors='replace').strip()
                parsed = self.parse_line(line)
                if parsed is None:
                    continue
                    
                # Push parsed row into queue, drop if full to avoid blocking
                try:
                    self.row_queue.put_nowait(parsed)
                except queue.Full:
                    # Queue full: drop oldest in queue then put (best-effort)
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
        return (self.reader_thread is not None and 
                self.reader_thread.is_alive() and 
                not self.reader_stop.is_set())
    
    def save_data(self, filename_prefix="channel", file_extension=".dat", save_directory="."):
        """
        Save data from each channel to separate files.
        
        Args:
            filename_prefix (str): Prefix for the output files (default: "channel")
            file_extension (str): File extension (default: ".dat")
            save_directory (str): Directory to save files (default: current directory)
        
        Returns:
            list: List of filenames that were created
        """
        import os
        import numpy as np
        
        created_files = []
        
        # Ensure save directory exists
        if not os.path.exists(save_directory):
            os.makedirs(save_directory)
        
        for channel_idx in range(self.num_channels):
            # Create filename
            filename = f"{filename_prefix}{channel_idx + 1}{file_extension}"
            filepath = os.path.join(save_directory, filename)
            
            try:
                # Convert deque to numpy array for saving
                channel_data = np.array(list(self.channels[channel_idx]))
                
                # Save as binary file (faster) or text file based on extension
                if file_extension.lower() in ['.dat', '.bin']:
                    # Save as binary file (more efficient for large datasets)
                    channel_data.astype(np.float64).tofile(filepath)
                    print(f"Saved channel {channel_idx + 1} data to {filepath} (binary format)")
                else:
                    # Save as text file (human readable)
                    np.savetxt(filepath, channel_data, fmt='%.6f')
                    print(f"Saved channel {channel_idx + 1} data to {filepath} (text format)")
                
                created_files.append(filepath)
                
            except Exception as e:
                print(f"Error saving channel {channel_idx + 1} data: {e}")
        
        if created_files:
            print(f"Successfully saved {len(created_files)} channel files")
        
        return created_files


if __name__ == "__main__":
    print("This module defines DataLogger class for real-time serial data acquisition.")