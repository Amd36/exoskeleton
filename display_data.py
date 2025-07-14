import matplotlib.pyplot as plt
import matplotlib.animation as animation

fig, ax = plt.subplots()
line, = ax.plot([], [], lw=2)

def init():
    ax.set_xlim(0, 1000)   # or DATA_SIZE
    ax.set_ylim(0, 1000)   # adjust based on your expected random range
    return line,

def update(frame):
    try:
        with open("data_log.dat", "r") as f:
            content = f.read()
            data = list(map(int, content.strip().split()))
            line.set_data(range(len(data)), data)
    except Exception as e:
        print("Reading failed:", e)
    return line,

ani = animation.FuncAnimation(fig, update, frames=None,
                              init_func=init, blit=True, interval=10)

plt.show()
