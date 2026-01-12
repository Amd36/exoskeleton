import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from DataLogger import DataLogger

# === CONFIGURATION ===
SERIAL_PORT = 'COM5'
BAUD_RATE = 921600

NUM_CH = 17
BUFFER_LEN = 5000          # buffer in *ADC samples* (1kHz). 5000 = 5s history

UI_HZ = 10                 # refresh rate
UI_INTERVAL_MS = int(1000 / UI_HZ)

ADC_HZ = 1000
IMU_HZ = 100
SAMPLES_PER_FRAME = ADC_HZ // IMU_HZ   # 10

IMU_SCALE = 100.0          # ESP32 sends int16 scaled by 100

data_logger = DataLogger(
    port=SERIAL_PORT,
    baud_rate=BAUD_RATE,
    num_channels=NUM_CH,
    buffer_length=BUFFER_LEN,
    samples_per_event=10  # not critical; update_buffers drains queue anyway
)

def _decimated_channel(ch_idx, decim=10, max_points=None, scale=1.0):
    """
    Return (x, y) from internal buffers, but decimated by 'decim'.
    x is in frame-sample units (0..N/decim).
    """
    buf = list(data_logger.channels[ch_idx])  # deque -> list snapshot
    if len(buf) == 0:
        return [], []

    y = buf[::decim]
    if scale != 1.0:
        y = [v / scale for v in y]

    # Optional downsample further for plotting speed
    if max_points is not None and len(y) > max_points:
        step = max(1, len(y) // max_points)
        y = y[::step]
        x = list(range(0, len(y)))
    else:
        x = list(range(0, len(y)))

    return x, y

def init():
    for ln in adc_lines:
        ln.set_data([], [])

    for sensor_lines in [acc_lines, gyro_lines, mag_lines]:
        for ln in sensor_lines:
            ln.set_data([], [])

    return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

def update(_frame):
    try:
        drained = data_logger.update_buffers()
        if drained == 0:
            return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

        max_plot_points_adc = 2000
        max_plot_points_imu = 800  # smaller; IMU is slower anyway

        # ===== ADC @ 1 kHz =====
        adc_data = data_logger.get_adc_data(max_points=max_plot_points_adc)
        for c in range(min(8, len(adc_data))):
            x, y = adc_data[c]
            adc_lines[c].set_data(x, [int(v) for v in y])

        # ===== IMU @ 100 Hz (decimate by 10) =====
        # Channels: 8..10 acc, 11..13 gyro, 14..16 mag
        for i in range(3):
            x, y = _decimated_channel(8 + i, decim=SAMPLES_PER_FRAME, max_points=max_plot_points_imu, scale=IMU_SCALE)
            acc_lines[i].set_data(x, y)

        for i in range(3):
            x, y = _decimated_channel(11 + i, decim=SAMPLES_PER_FRAME, max_points=max_plot_points_imu, scale=IMU_SCALE)
            gyro_lines[i].set_data(x, y)

        for i in range(3):
            x, y = _decimated_channel(14 + i, decim=SAMPLES_PER_FRAME, max_points=max_plot_points_imu, scale=IMU_SCALE)
            mag_lines[i].set_data(x, y)

    except Exception as e:
        print("Error in update:", e)

    return tuple(adc_lines + acc_lines + gyro_lines + mag_lines)

if __name__ == "__main__":
    # === SETUP PLOT ===
    fig = plt.figure(figsize=(15, 10))
    ax_adc  = plt.subplot(2, 2, 1)
    ax_acc  = plt.subplot(2, 2, 2)
    ax_gyro = plt.subplot(2, 2, 3)
    ax_mag  = plt.subplot(2, 2, 4)

    # ADC plot
    adc_lines = []
    for ch in range(8):
        ln, = ax_adc.plot([], [], label=f'ADC{ch+1}')
        adc_lines.append(ln)
    ax_adc.set_xlim(0, BUFFER_LEN)
    ax_adc.set_ylim(0, 4095)
    ax_adc.set_title("ADC Channels (1 kHz)")
    ax_adc.set_xlabel("Sample (1 kHz buffer index)")
    ax_adc.set_ylabel("ADC Value")
    ax_adc.legend(loc='upper right')
    ax_adc.grid(True, alpha=0.3)

    # IMU plots (100 Hz frames) — x-axis in frames
    imu_frame_buf_len = BUFFER_LEN // SAMPLES_PER_FRAME

    # Accelerometer
    acc_lines = []
    for label, color in zip(['Acc X', 'Acc Y', 'Acc Z'], ['red', 'green', 'blue']):
        ln, = ax_acc.plot([], [], label=label, color=color)
        acc_lines.append(ln)
    ax_acc.set_xlim(0, imu_frame_buf_len)
    ax_acc.set_ylim(-40, 40)
    ax_acc.set_title("Accelerometer (100 Hz frames)")
    ax_acc.set_xlabel("Frame index (100 Hz)")
    ax_acc.set_ylabel("m/s²")
    ax_acc.legend()
    ax_acc.grid(True, alpha=0.3)

    # Gyroscope
    gyro_lines = []
    for label, color in zip(['Gyro X', 'Gyro Y', 'Gyro Z'], ['red', 'green', 'blue']):
        ln, = ax_gyro.plot([], [], label=label, color=color)
        gyro_lines.append(ln)
    ax_gyro.set_xlim(0, imu_frame_buf_len)
    ax_gyro.set_ylim(-20, 20)
    ax_gyro.set_title("Gyroscope (100 Hz frames)")
    ax_gyro.set_xlabel("Frame index (100 Hz)")
    ax_gyro.set_ylabel("rad/s")
    ax_gyro.legend()
    ax_gyro.grid(True, alpha=0.3)

    # Magnetometer
    mag_lines = []
    for label, color in zip(['Mag X', 'Mag Y', 'Mag Z'], ['red', 'green', 'blue']):
        ln, = ax_mag.plot([], [], label=label, color=color)
        mag_lines.append(ln)
    ax_mag.set_xlim(0, imu_frame_buf_len)
    ax_mag.set_ylim(-500, 500)
    ax_mag.set_title("Magnetometer (100 Hz frames, updated @20 Hz)")
    ax_mag.set_xlabel("Frame index (100 Hz)")
    ax_mag.set_ylabel("µT")
    ax_mag.legend()
    ax_mag.grid(True, alpha=0.3)

    plt.tight_layout()

    data_logger.start_logging()

    # UI refresh @ 10 Hz
    ani = animation.FuncAnimation(fig, update, init_func=init, blit=False, interval=UI_INTERVAL_MS)

    try:
        plt.show()
    finally:
        data_logger.stop_logging()
