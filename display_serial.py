import matplotlib.pyplot as plt
import matplotlib.animation as animation
from DataLogger import DataLogger

# === CONFIGURATION ===
# SERIAL_PORT = '/dev/ttyUSB0'  # ubuntu
SERIAL_PORT = 'COM5'  # windows
BAUD_RATE = 115200
NUM_CH = 2
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

def init():
    for ln in lines:
        ln.set_data([], [])
    return tuple(lines)

def update(frame):
    """Update function for matplotlib animation using DataLogger."""
    try:
        # Update buffers with new data from queue
        drained = data_logger.update_buffers()
        
        if drained == 0:
            if 'lines' in globals() and lines is not None:
                return tuple(lines)
            return ()

        # Get data for all channels with downsampling for performance
        max_plot_points = 2000  # cap points shown for performance
        channel_data = data_logger.get_all_channel_data(max_points=max_plot_points)
        
        for c in range(NUM_CH):
            if c < len(channel_data):
                x, y = channel_data[c]
                lines[c].set_data(x, y)

    except Exception as e:
        print("Error in update:", e)
    
    if 'lines' in globals() and lines is not None:
        return tuple(lines)
    return ()

if __name__ == "__main__":
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

    # Start data logging
    data_logger.start_logging()

    # Animation interval controls UI refresh; keep it modest (e.g., 50 ms) while the reader handles high-rate IO.
    ani = animation.FuncAnimation(fig, update, init_func=init, blit=False, interval=50)

    try:
        plt.show()
    finally:
        # Stop data logging on exit
        data_logger.stop_logging()
