import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation

# === CONFIGURATION ===
SERIAL_PORT = '/dev/ttyUSB0'  # adjust to your port
BAUD_RATE = 115200

# === INITIALIZE ===
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# Data buffers (hold last 1000 samples for each channel)
channel1_data = [0] * 1000
channel2_data = [0] * 1000
channel3_data = [0] * 1000

# === SETUP PLOT ===
fig, ax = plt.subplots()
line1, = ax.plot([], [], label='Channel 1')
line2, = ax.plot([], [], label='Channel 2')
line3, = ax.plot([], [], label='Channel 3')
ax.set_xlim(0, 1000)
ax.set_ylim(0, 1050)
ax.set_title("ESP32 3-Channel ADC Real-Time")
ax.set_xlabel("Sample")
ax.set_ylabel("ADC Value")
ax.legend()

def init():
    line1.set_data([], [])
    line2.set_data([], [])
    line3.set_data([], [])
    return line1, line2, line3

def update(frame):
    global channel1_data, channel2_data, channel3_data
    try:
        line_in = ser.readline().decode('utf-8').strip()
        data_strs = line_in.split()
        if len(data_strs) != 3000:
            print(f"Warning: Expected 3000 values, got {len(data_strs)}")
            return line1, line2, line3
        
        flat_data = list(map(int, data_strs))
        
        # Split into channels
        ch1 = flat_data[0::3]
        ch2 = flat_data[1::3]
        ch3 = flat_data[2::3]

        # Keep only latest 1000 samples
        channel1_data = ch1[-1000:]
        channel2_data = ch2[-1000:]
        channel3_data = ch3[-1000:]

        # === WRITE TO .dat files ===
        with open("channel1.dat", "w") as f:
            f.write("\n".join(map(str, channel1_data)))
        with open("channel2.dat", "w") as f:
            f.write("\n".join(map(str, channel2_data)))
        with open("channel3.dat", "w") as f:
            f.write("\n".join(map(str, channel3_data)))

        # === UPDATE PLOT ===
        line1.set_data(range(1000), channel1_data)
        line2.set_data(range(1000), channel2_data)
        line3.set_data(range(1000), channel3_data)

    except Exception as e:
        print("Error reading line:", e)
    return line1, line2, line3

ani = animation.FuncAnimation(fig, update, init_func=init, blit=True, interval=10)
plt.show()
