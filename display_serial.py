import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import re
import threading
import queue
from collections import deque

# === CONFIGURATION ===
SERIAL_PORT = '/dev/ttyUSB0'  # adjust to your port
BAUD_RATE = 115200
NUM_CH = 2
SAMPLES_PER_EVENT = 2  # ESP prints up to 2 rows per event
BUFFER_LEN = 20000  # keep a large buffer to avoid data loss (20k samples)

# Thread-safe queue to receive parsed rows from the reader thread
row_queue = queue.Queue(maxsize=10000)

# Data buffers: one deque per channel for efficient append/pop from left
channels = [deque([0] * BUFFER_LEN, maxlen=BUFFER_LEN) for _ in range(NUM_CH)]

# Reader control
reader_thread = None
reader_stop = threading.Event()

def init():
    for ln in lines:
        ln.set_data([], [])
    return tuple(lines)

def parse_line(line):
    """Parse a CSV or space-separated line into a list of ints. Returns None on parse error."""
    # allow commas and/or whitespace as separators
    line = line.strip()
    if not line:
        return None
    if line == "<no-data>":
        return None
    parts = re.split('[,\s]+', line)
    try:
        vals = list(map(int, parts))
    except ValueError:
        print(f"Warning: non-integer in line: {line}")
        return None
    if len(vals) != NUM_CH:
        print(f"Warning: expected {NUM_CH} values, got {len(vals)}: {line}")
        return None
    return vals

def read_event():
    # Deprecated in threaded mode; kept for compatibility but we won't use it when reader thread runs.
    rows = []
    for _ in range(SAMPLES_PER_EVENT):
        try:
            rows.append(row_queue.get_nowait())
        except queue.Empty:
            break
    return rows


def serial_reader(port, baud, timeout=0.05):
    """Background thread that reads serial, parses lines, and pushes rows into row_queue."""
    try:
        s = serial.Serial(port, baud, timeout=timeout)
    except Exception as e:
        print(f"Serial reader failed to open {port}: {e}")
        return

    while not reader_stop.is_set():
        try:
            raw = s.readline()
            if not raw:
                continue
            line = raw.decode('utf-8', errors='replace').strip()
            parsed = parse_line(line)
            if parsed is None:
                continue
            # push parsed row into queue, drop if full to avoid blocking
            try:
                row_queue.put_nowait(parsed)
            except queue.Full:
                # Queue full: drop oldest in queue then put (best-effort)
                try:
                    _ = row_queue.get_nowait()
                    row_queue.put_nowait(parsed)
                except queue.Empty:
                    pass
        except Exception as e:
            print("Serial reader error:", e)
            continue
    s.close()

def update(frame):
    # Drain the queue as fast as possible and append to channel buffers
    try:
        drained = 0
        while True:
            try:
                row = row_queue.get_nowait()
            except queue.Empty:
                break
            for c in range(NUM_CH):
                channels[c].append(row[c])
            drained += 1

        if drained == 0:
            if 'lines' in globals() and lines is not None:
                return tuple(lines)
            return ()

        # For plotting we downsample if buffer is large to reduce plotting cost.
        buf_len = len(channels[0])
        max_plot_points = 2000  # cap points shown for performance
        step = max(1, buf_len // max_plot_points)
        x = range(0, buf_len, step)
        for c in range(NUM_CH):
            y = list(channels[c])[::step]
            lines[c].set_data(x, y)

    except Exception as e:
        print("Error in update:", e)
    if 'lines' in globals() and lines is not None:
        return tuple(lines)
    return ()

if __name__ == "__main__":
    # === INITIALIZE SERIAL ===
    try:
        # We create a lightweight serial here only for compatibility; the reader thread opens its own serial.
        ser = None
    except Exception:
        ser = None

    # === SETUP PLOT ===
    fig, ax = plt.subplots(figsize=(10, 6))
    lines = []
    for ch in range(NUM_CH):
        ln, = ax.plot([], [], label=f'Channel {ch+1}')
        lines.append(ln)
    ax.set_xlim(0, BUFFER_LEN)
    ax.set_ylim(0, 4095)
    ax.set_title(f"ESP32 {NUM_CH}-Channel ADC Real-Time")
    ax.set_xlabel("Sample")
    ax.set_ylabel("ADC Value")
    ax.legend(loc='upper right')

    # Start reader thread
    reader_stop.clear()
    reader_thread = threading.Thread(target=serial_reader, args=(SERIAL_PORT, BAUD_RATE), daemon=True)
    reader_thread.start()

    # Animation interval controls UI refresh; keep it modest (e.g., 50 ms) while the reader handles high-rate IO.
    ani = animation.FuncAnimation(fig, update, init_func=init, blit=False, interval=50)

    try:
        plt.show()
    finally:
        # stop reader thread on exit
        reader_stop.set()
        if reader_thread is not None:
            reader_thread.join(timeout=0.5)
