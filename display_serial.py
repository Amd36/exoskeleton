import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from DataLogger import DataLogger
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as urlparse

# === CONFIGURATION ===
SERIAL_PORT = '/dev/ttyUSB0'  # ubuntu
# SERIAL_PORT = 'COM5'  # windows
BAUD_RATE = 115200
# NUM_CH = 9  # When receiving only IMU data: 3 acc + 3 gyro + 3 mag
NUM_CH = 17  # When ESP32 sends 8 ADC + 9 IMU
SAMPLES_PER_EVENT = 2  # ESP prints up to 2 rows per event
BUFFER_LEN = 20000  # keep a large buffer to avoid data loss (20k samples)

# Initialize DataLogger
data_logger = DataLogger(
    port=SERIAL_PORT,
    baud_rate=BAUD_RATE,
    num_channels=NUM_CH,
    buffer_length=BUFFER_LEN,
    samples_per_event=SAMPLES_PER_EVENT
)

# Saving control
_save_held = False
_save_thread = None
_save_interval = 1.0 / 250.0  # seconds between save attempts while key held (250 Hz)
_save_dir_prefix = "saved_data"

def _ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def _saver_loop():
    """Background loop that saves data while _save_held is True."""
    global _save_held
    # Make a session directory once per hold so we append to the same files
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(_save_dir_prefix, session_ts)
    _ensure_dir(session_dir)

    # Prepare per-channel file handles opened in append-binary mode
    file_handles = []
    try:
        for ch_idx in range(data_logger.num_channels):
            fname = os.path.join(session_dir, f"channel_{ch_idx+1}.dat")
            # Ensure file exists
            fh = open(fname, 'ab')
            file_handles.append(fh)

        # Track last written lengths per channel to only append new samples
        last_lens = [len(ch) for ch in data_logger.channels]

        while _save_held:
            try:
                for ch_idx, ch in enumerate(data_logger.channels):
                    cur_len = len(ch)
                    last_len = last_lens[ch_idx]
                    if cur_len == last_len:
                        continue

                    # If buffer grew normally, write the new slice
                    if cur_len > last_len:
                        new_vals = list(ch)[last_len:cur_len]
                    else:
                        # Buffer wrapped or was cleared; write the entire buffer
                        new_vals = list(ch)

                    if new_vals:
                        arr = np.array(new_vals, dtype=np.float64)
                        try:
                            arr.tofile(file_handles[ch_idx])
                        except Exception as e:
                            print(f"Error appending to channel {ch_idx+1}:", e)

                    last_lens[ch_idx] = cur_len

                time.sleep(_save_interval)
            except Exception as e:
                print("Error in saver loop iteration:", e)
                time.sleep(_save_interval)
    finally:
        # Close all file handles
        for fh in file_handles:
            try:
                fh.close()
            except Exception:
                pass


# Control helpers for starting/stopping the saver from outside (HTTP/FIFO/etc)
def start_saving():
    global _save_held, _save_thread
    if _save_held:
        return False
    _save_held = True
    _save_thread = threading.Thread(target=_saver_loop, daemon=True)
    _save_thread.start()
    print("Saver started via control")
    return True

def stop_saving():
    global _save_held
    if not _save_held:
        return False
    _save_held = False
    print("Saver stop requested via control")
    return True

def save_once():
    # One-shot save using DataLogger.save_data into a timestamped directory
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    session_dir = os.path.join(_save_dir_prefix, ts)
    _ensure_dir(session_dir)
    try:
        data_logger.save_data(filename_prefix="channel_", file_extension=".dat", save_directory=session_dir)
        return True
    except Exception as e:
        print("Error in save_once:", e)
        return False


class SimpleCtrlHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse.urlparse(self.path)
        path = parsed.path
        response = b""
        code = 200
        if path == "/save/start":
            ok = start_saving()
            response = b"started" if ok else b"already"
        elif path == "/save/stop":
            ok = stop_saving()
            response = b"stopped" if ok else b"not-running"
        elif path == "/save/once":
            ok = save_once()
            response = b"saved" if ok else b"error"
        else:
            code = 404
            response = b"not-found"

        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def run_control_http_server(host='127.0.0.1', port=8000):
    try:
        server = HTTPServer((host, port), SimpleCtrlHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"Control HTTP server running on http://{host}:{port}")
        return server
    except Exception as e:
        print("Failed to start control HTTP server:", e)
        return None

def on_key_press(event):
    pass

def on_key_release(event):
    pass

def init():
    # Initialize ADC lines
    for ln in adc_lines:
        ln.set_data([], [])
    
    # Initialize IMU lines
    for sensor_lines in [acc_lines, gyro_lines, mag_lines]:
        for ln in sensor_lines:
            ln.set_data([], [])
    
    return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

def update(frame):
    """Update function for matplotlib animation using DataLogger."""
    try:
        # Update buffers with new data from queue
        drained = data_logger.update_buffers()
        
        if drained == 0:
            return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

                # Get data with downsampling for performance
        max_plot_points = 2000  # cap points shown for performance
        
        # Check if we have ADC data (channels 0-7) or only IMU data
        if NUM_CH >= 17:
            # Full system: 8 ADC + 9 IMU
            # Update ADC plots (convert floats to integers if needed)
            adc_data = data_logger.get_adc_data(max_points=max_plot_points)
            for c in range(min(8, len(adc_data))):
                x, y = adc_data[c]
                # Ensure ADC values are displayed as integers (in case they come as floats)
                y_int = [int(val) for val in y]
                adc_lines[c].set_data(x, y_int)
        
        # Update IMU plots (works for both 9-channel and 17-channel modes)
        imu_data = data_logger.get_imu_data(max_points=max_plot_points)
        
        # For 9-channel mode, map channels directly to IMU sensors
        if NUM_CH == 9:
            # Direct mapping: channels 0-2=acc, 3-5=gyro, 6-8=mag
            all_data = data_logger.get_all_channel_data(max_points=max_plot_points)
            if len(all_data) >= 9:
                # Accelerometer (channels 0-2)
                for i in range(3):
                    if i < len(acc_lines):
                        x, y = all_data[i]
                        acc_lines[i].set_data(x, y)
                
                # Gyroscope (channels 3-5)
                for i in range(3):
                    if i < len(gyro_lines):
                        x, y = all_data[3 + i]
                        gyro_lines[i].set_data(x, y)
                
                # Magnetometer (channels 6-8)
                for i in range(3):
                    if i < len(mag_lines):
                        x, y = all_data[6 + i]
                        mag_lines[i].set_data(x, y)
        else:
            # 17-channel mode: use the dedicated IMU methods
            # Update accelerometer
            if 'accelerometer' in imu_data:
                for i, (x, y) in enumerate(imu_data['accelerometer']):
                    if i < len(acc_lines):
                        # Data is already in real units (m/s²)
                        acc_lines[i].set_data(x, y)
            
            # Update gyroscope
            if 'gyroscope' in imu_data:
                for i, (x, y) in enumerate(imu_data['gyroscope']):
                    if i < len(gyro_lines):
                        # Data is already in real units (rad/s)
                        gyro_lines[i].set_data(x, y)
            
            # Update magnetometer
            if 'magnetometer' in imu_data:
                for i, (x, y) in enumerate(imu_data['magnetometer']):
                    if i < len(mag_lines):
                        # Data is already in real units (µT)
                        mag_lines[i].set_data(x, y)

    except Exception as e:
        print("Error in update:", e)
    
    return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

if __name__ == "__main__":
    # === SETUP PLOT ===
    if NUM_CH >= 17:
        # Full system with ADC + IMU: 2x2 layout
        fig = plt.figure(figsize=(15, 10))
        ax_adc = plt.subplot(2, 2, 1)
        ax_acc = plt.subplot(2, 2, 2)
        ax_gyro = plt.subplot(2, 2, 3)
        ax_mag = plt.subplot(2, 2, 4)
        
        # Setup ADC plot (channels 0-7)
        adc_lines = []
        for ch in range(8):
            ln, = ax_adc.plot([], [], label=f'ADC{ch+1}')
            adc_lines.append(ln)
        ax_adc.set_xlim(0, BUFFER_LEN)
        ax_adc.set_ylim(0, 4095)
        ax_adc.set_title("ADC Channels (0-4095)")
        ax_adc.set_xlabel("Sample")
        ax_adc.set_ylabel("ADC Value")
        ax_adc.legend(loc='upper right')
        ax_adc.grid(True, alpha=0.3)
    else:
        # IMU only: 1x3 layout
        fig = plt.figure(figsize=(15, 5))
        ax_acc = plt.subplot(1, 3, 1)
        ax_gyro = plt.subplot(1, 3, 2)
        ax_mag = plt.subplot(1, 3, 3)
        adc_lines = []  # Empty list for compatibility
    
    # Setup Accelerometer plot (m/s²)
    acc_lines = []
    acc_labels = ['Acc X', 'Acc Y', 'Acc Z']
    acc_colors = ['red', 'green', 'blue']
    for i, (label, color) in enumerate(zip(acc_labels, acc_colors)):
        ln, = ax_acc.plot([], [], label=label, color=color)
        acc_lines.append(ln)
    ax_acc.set_xlim(0, BUFFER_LEN)
    ax_acc.set_ylim(-2000, 2000)  # Typical accelerometer range
    ax_acc.set_title("Accelerometer (m/s²)")
    ax_acc.set_xlabel("Sample")
    ax_acc.set_ylabel("Acceleration (m/s²)")
    ax_acc.legend()
    ax_acc.grid(True, alpha=0.3)
    
    # Setup Gyroscope plot (rad/s)
    gyro_lines = []
    gyro_labels = ['Gyro X', 'Gyro Y', 'Gyro Z']
    gyro_colors = ['red', 'green', 'blue']
    for i, (label, color) in enumerate(zip(gyro_labels, gyro_colors)):
        ln, = ax_gyro.plot([], [], label=label, color=color)
        gyro_lines.append(ln)
    ax_gyro.set_xlim(0, BUFFER_LEN)
    ax_gyro.set_ylim(-1000, 1000)  # Typical gyroscope range
    ax_gyro.set_title("Gyroscope (rad/s)")
    ax_gyro.set_xlabel("Sample")
    ax_gyro.set_ylabel("Angular Velocity (rad/s)")
    ax_gyro.legend()
    ax_gyro.grid(True, alpha=0.3)
    
    # Setup Magnetometer plot (µT)
    mag_lines = []
    mag_labels = ['Mag X', 'Mag Y', 'Mag Z']
    mag_colors = ['red', 'green', 'blue']
    for i, (label, color) in enumerate(zip(mag_labels, mag_colors)):
        ln, = ax_mag.plot([], [], label=label, color=color)
        mag_lines.append(ln)
    ax_mag.set_xlim(0, BUFFER_LEN)
    ax_mag.set_ylim(-10000, 10000)  # Typical magnetometer range
    ax_mag.set_title("Magnetometer (µT)")
    ax_mag.set_xlabel("Sample")
    ax_mag.set_ylabel("Magnetic Field (µT)")
    ax_mag.legend()
    ax_mag.grid(True, alpha=0.3)
    
    # Adjust layout to prevent overlap
    plt.tight_layout()

    # Note: keyboard control removed from matplotlib key handlers.
    # Use the local HTTP control endpoints instead (see README below).

    # Start data logging
    data_logger.start_logging()

    # Start local control HTTP server (127.0.0.1:8000)
    control_server = run_control_http_server(host='127.0.0.1', port=8000)

    # Animation interval controls UI refresh; keep it modest (e.g., 100 ms) for multiple subplots
    ani = animation.FuncAnimation(fig, update, init_func=init, blit=False, interval=100)

    try:
        plt.show()
    finally:
        # Stop data logging on exit
        data_logger.stop_logging()
