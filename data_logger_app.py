"""
data_logger_app.py
------------------
A simple live viewer and start/stop saver for the DAQ DataLogger.

The binary serial parsing stays inside DataLogger. This app only drains parsed
rows, shows them in a Tkinter window, and asks DataLogger.save_data() to write
the captured rows when saving stops.
"""

import re
import sys
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from DataLogger import DataLogger

try:
    from serial.tools import list_ports
except Exception:
    list_ports = None


DEFAULT_BAUD = 921600
BUFFER_LEN = 10000
UI_INTERVAL_MS = 100
MAX_ADC_POINTS = 10000
MAX_IMU_POINTS = 2000

BG_COLOR = "#12161d"
PANEL_COLOR = "#1b222c"
PANEL_ALT_COLOR = "#202936"
FG_COLOR = "#e6edf3"
MUTED_FG_COLOR = "#9fb0c0"
GRID_COLOR = "#3b4655"
ACCENT_COLOR = "#5cc8ff"
SAVE_COLOR = "#6ee7a8"
STOP_COLOR = "#ff6b7a"
PIEZO_COLORS = ("#5cc8ff", "#6ee7a8", "#ffd166", "#c792ea", "#ff9f7a", "#9ad0ec")
AXIS_COLORS = ("#ff6b7a", "#6ee7a8", "#5cc8ff")
IMU_LABELS = {0: "imu_thigh", 1: "imu_shank"}
ADC_GPIO_LABELS = (36, 39, 34, 35, 32, 33)


def get_available_serial_ports():
    if list_ports is None:
        return []
    return [port.device for port in list_ports.comports()]


def get_platform_default_port(available_ports=None):
    if available_ports:
        return available_ports[0]

    if sys.platform.startswith("win"):
        return "COM5"

    if sys.platform.startswith("linux"):
        by_id_dir = Path("/dev/serial/by-id")
        by_id_ports = sorted(str(port) for port in by_id_dir.glob("*")) if by_id_dir.exists() else []
        if by_id_ports:
            return by_id_ports[0]

        for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
            matches = sorted(Path("/dev").glob(Path(pattern).name))
            if matches:
                return str(matches[0])
        return "/dev/ttyUSB0"

    if sys.platform == "darwin":
        for pattern in ("/dev/tty.usbmodem*", "/dev/tty.usbserial*"):
            matches = sorted(Path("/dev").glob(Path(pattern).name))
            if matches:
                return str(matches[0])
        return "/dev/tty.usbserial"

    return ""


def safe_path_part(value, fallback):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


class DataLoggerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Exoskeleton DAQ Live Viewer")
        self.root.geometry("1500x980")
        self.root.configure(bg=BG_COLOR)

        self.logger = None
        self.saving = False
        self.save_started_at = None
        self.save_indices = []
        self.save_rows = []
        self.serial_ports = get_available_serial_ports()

        self.name_var = tk.StringVar()
        self.activity_var = tk.StringVar()
        self.session_var = tk.StringVar(value="session_001")
        self.port_var = tk.StringVar(value=get_platform_default_port(self.serial_ports))
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.status_var = tk.StringVar(value="Not connected")
        self.samples_var = tk.StringVar(value="Saved samples: 0")

        self._apply_theme()
        self._build_controls()
        self._build_plots()
        self._restart_logger(auto_connect=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(UI_INTERVAL_MS, self._update_loop)

    def _apply_theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG_COLOR, foreground=FG_COLOR, fieldbackground=PANEL_ALT_COLOR)
        style.configure("TFrame", background=BG_COLOR)
        style.configure("TLabel", background=BG_COLOR, foreground=FG_COLOR)
        style.configure(
            "TEntry",
            fieldbackground=PANEL_ALT_COLOR,
            foreground=FG_COLOR,
            insertcolor=FG_COLOR,
            bordercolor=GRID_COLOR,
            lightcolor=GRID_COLOR,
            darkcolor=GRID_COLOR,
        )
        style.configure(
            "TButton",
            background=PANEL_ALT_COLOR,
            foreground=FG_COLOR,
            bordercolor=GRID_COLOR,
            focusthickness=1,
            focuscolor=ACCENT_COLOR,
            padding=(10, 5),
        )
        style.map(
            "TButton",
            background=[("active", "#2b3748"), ("pressed", "#324256")],
            foreground=[("disabled", MUTED_FG_COLOR), ("active", FG_COLOR)],
        )
        style.configure("Start.TButton", background="#1f3b32", foreground=SAVE_COLOR)
        style.map("Start.TButton", background=[("active", "#285142"), ("pressed", "#30604e")])
        style.configure("Stop.TButton", background="#48242d", foreground=STOP_COLOR)
        style.map("Stop.TButton", background=[("active", "#63313d"), ("pressed", "#733947")])

    def _build_controls(self):
        controls = ttk.Frame(self.root, padding=8)
        controls.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(controls, text="Name").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        ttk.Entry(controls, textvariable=self.name_var, width=18).grid(row=0, column=1, padx=(0, 12))

        ttk.Label(controls, text="Activity").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        ttk.Entry(controls, textvariable=self.activity_var, width=18).grid(row=0, column=3, padx=(0, 12))

        ttk.Label(controls, text="Session ID").grid(row=0, column=4, sticky=tk.W, padx=(0, 4))
        ttk.Entry(controls, textvariable=self.session_var, width=18).grid(row=0, column=5, padx=(0, 12))

        ttk.Label(controls, text="Port").grid(row=0, column=6, sticky=tk.W, padx=(0, 4))
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, values=self.serial_ports, width=18)
        self.port_combo.grid(row=0, column=7, padx=(0, 8))
        ttk.Button(controls, text="Refresh Ports", command=self._refresh_ports).grid(row=0, column=8, padx=(0, 12))

        ttk.Label(controls, text="Baud").grid(row=0, column=9, sticky=tk.W, padx=(0, 4))
        ttk.Entry(controls, textvariable=self.baud_var, width=10).grid(row=0, column=10, padx=(0, 12))

        ttk.Button(controls, text="Reconnect", command=lambda: self._restart_logger(auto_connect=False)).grid(row=0, column=11, padx=(0, 8))
        ttk.Button(controls, text="Refresh Window", command=self._refresh_window).grid(row=0, column=12, padx=(0, 8))

        self.save_button = ttk.Button(controls, text="Start Saving", command=self._toggle_saving, style="Start.TButton")
        self.save_button.grid(row=0, column=13, padx=(0, 8))

        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=0, columnspan=8, sticky=tk.W, pady=(6, 0))
        ttk.Label(controls, textvariable=self.samples_var).grid(row=1, column=8, columnspan=6, sticky=tk.E, pady=(6, 0))

        controls.columnconfigure(14, weight=1)

    def _build_plots(self):
        self.figure = Figure(figsize=(14.8, 9.0), dpi=100, facecolor=BG_COLOR)
        grid = self.figure.add_gridspec(1, 2, width_ratios=[1.18, 1.0], wspace=0.2)
        imu_grid = grid[0, 0].subgridspec(4, 1, hspace=0.55)
        piezo_grid = grid[0, 1].subgridspec(DataLogger.ADC_CH, 1, hspace=0.72)

        self.ax_imu0_acc = self.figure.add_subplot(imu_grid[0, 0])
        self.ax_imu0_gyro = self.figure.add_subplot(imu_grid[1, 0])
        self.ax_imu1_acc = self.figure.add_subplot(imu_grid[2, 0])
        self.ax_imu1_gyro = self.figure.add_subplot(imu_grid[3, 0])

        self.piezo_axes = []
        self.piezo_lines = []
        for ch in range(DataLogger.ADC_CH):
            axis = self.figure.add_subplot(piezo_grid[ch, 0])
            line, = axis.plot([], [], color=PIEZO_COLORS[ch % len(PIEZO_COLORS)], linewidth=1.25)
            axis.set_title(f"Piezo {ch + 1}  |  GPIO {ADC_GPIO_LABELS[ch]}", loc="left", fontsize=8, pad=4, color=FG_COLOR)
            axis.set_xlabel("Sample" if ch == DataLogger.ADC_CH - 1 else "")
            axis.set_ylabel("ADC")
            axis.set_xlim(0, BUFFER_LEN)
            axis.set_ylim(0, 4095)
            self._style_axis(axis)
            if ch < DataLogger.ADC_CH - 1:
                axis.tick_params(labelbottom=False)
            self.piezo_axes.append(axis)
            self.piezo_lines.append(line)

        self.imu_lines = {}
        axis_labels = ("X", "Y", "Z")

        for imu_idx, acc_ax, gyro_ax in (
            (0, self.ax_imu0_acc, self.ax_imu0_gyro),
            (1, self.ax_imu1_acc, self.ax_imu1_gyro),
        ):
            self.imu_lines[(imu_idx, "acc")] = []
            self.imu_lines[(imu_idx, "gyro")] = []

            for axis_idx, label in enumerate(axis_labels):
                line, = acc_ax.plot([], [], color=AXIS_COLORS[axis_idx], label=f"Acc {label}", linewidth=1.25)
                self.imu_lines[(imu_idx, "acc")].append(line)

            for axis_idx, label in enumerate(axis_labels):
                line, = gyro_ax.plot([], [], color=AXIS_COLORS[axis_idx], label=f"Gyro {label}", linewidth=1.25)
                self.imu_lines[(imu_idx, "gyro")].append(line)

            imu_label = IMU_LABELS.get(imu_idx, f"imu_{imu_idx}")
            acc_ax.set_title(f"{imu_label}  |  Accelerometer", loc="left", fontsize=9, pad=5, color=FG_COLOR)
            acc_ax.set_xlabel("")
            acc_ax.set_ylabel("m/s^2")
            acc_ax.set_xlim(0, MAX_IMU_POINTS)
            acc_ax.set_ylim(-40, 40)
            self._style_axis(acc_ax)
            acc_ax.tick_params(labelbottom=False)
            acc_ax.legend(loc="upper right", fontsize=8, facecolor=PANEL_ALT_COLOR, edgecolor=GRID_COLOR, labelcolor=FG_COLOR)

            gyro_ax.set_title(f"{imu_label}  |  Gyroscope", loc="left", fontsize=9, pad=5, color=FG_COLOR)
            gyro_ax.set_xlabel("Frame" if imu_idx == DataLogger.IMU_COUNT - 1 else "")
            gyro_ax.set_ylabel("rad/s")
            gyro_ax.set_xlim(0, MAX_IMU_POINTS)
            gyro_ax.set_ylim(-20, 20)
            self._style_axis(gyro_ax)
            if imu_idx < DataLogger.IMU_COUNT - 1:
                gyro_ax.tick_params(labelbottom=False)
            gyro_ax.legend(loc="upper right", fontsize=8, facecolor=PANEL_ALT_COLOR, edgecolor=GRID_COLOR, labelcolor=FG_COLOR)

        self.figure.subplots_adjust(left=0.055, right=0.985, top=0.95, bottom=0.07)

        self.canvas = FigureCanvasTkAgg(self.figure, master=self.root)
        canvas_widget = self.canvas.get_tk_widget()
        canvas_widget.configure(bg=BG_COLOR, highlightthickness=0)
        canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.canvas.draw()

    def _style_axis(self, axis):
        axis.set_facecolor(PANEL_COLOR)
        axis.grid(True, color=GRID_COLOR, alpha=0.45, linewidth=0.7)
        title_artists = [
            axis.title,
            getattr(axis, "_left_title", None),
            getattr(axis, "_right_title", None),
        ]
        for title in [artist for artist in title_artists if artist is not None]:
            title.set_color(FG_COLOR)
            title.set_fontweight("normal")
        axis.xaxis.label.set_color(MUTED_FG_COLOR)
        axis.yaxis.label.set_color(MUTED_FG_COLOR)
        axis.xaxis.label.set_size(8)
        axis.yaxis.label.set_size(8)
        axis.tick_params(colors=MUTED_FG_COLOR, labelsize=7, pad=2)
        for spine in axis.spines.values():
            spine.set_color(GRID_COLOR)

    def _refresh_ports(self):
        self.serial_ports = get_available_serial_ports()
        self.port_combo["values"] = self.serial_ports

        if self.serial_ports and self.port_var.get().strip() not in self.serial_ports:
            self.port_var.set(self.serial_ports[0])
            self.status_var.set(f"Selected serial port: {self.serial_ports[0]}")
        elif self.serial_ports:
            self.status_var.set(f"Detected {len(self.serial_ports)} serial port(s)")
        else:
            self.status_var.set("No serial ports detected. Enter a port manually, e.g. COM5 or /dev/ttyUSB0.")

    def _restart_logger(self, auto_connect=False):
        if self.saving:
            messagebox.showwarning("Saving active", "Stop saving before reconnecting the serial port.")
            return

        port = self.port_var.get().strip()
        if auto_connect and not self.serial_ports:
            self.status_var.set("No serial ports detected. Enter a port manually, then click Reconnect.")
            return

        if not port:
            messagebox.showerror("Missing port", "Select or enter a serial port before reconnecting.")
            return

        if self.logger is not None:
            self.logger.stop_logging()

        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid baud", "Baud rate must be an integer.")
            return

        self.logger = DataLogger(
            port=port,
            baud_rate=baud,
            num_channels=DataLogger.TOTAL_CHANNELS,
            buffer_length=BUFFER_LEN,
            samples_per_event=DataLogger.ADC_BLOCK,
        )
        self.logger.start_logging()
        self.status_var.set(f"Listening on {port} at {baud} baud")

    def _toggle_saving(self):
        if self.saving:
            self._stop_saving()
        else:
            self._start_saving()

    def _start_saving(self):
        if self.logger is None:
            messagebox.showerror("No logger", "Serial logger is not running.")
            return

        name = self.name_var.get().strip()
        activity = self.activity_var.get().strip()
        session = self.session_var.get().strip()
        if not name or not activity or not session:
            messagebox.showerror("Missing information", "Please enter name, activity, and session ID before saving.")
            return

        self.save_indices = []
        self.save_rows = []
        self.save_started_at = datetime.now()
        self.saving = True
        self.save_button.config(text="Stop Saving", style="Stop.TButton")
        self.status_var.set("Saving started")
        self.samples_var.set("Saved samples: 0")

    def _stop_saving(self):
        self.saving = False
        self.save_button.config(text="Start Saving", style="Start.TButton")

        if len(self.save_rows) == 0:
            self.status_var.set("Saving stopped - no samples captured")
            messagebox.showwarning("No data", "No incoming samples were captured during this save window.")
            return

        name = safe_path_part(self.name_var.get(), "unknown_name")
        activity = safe_path_part(self.activity_var.get(), "unknown_activity")
        session = safe_path_part(self.session_var.get(), "session")
        save_dir = Path("saved_data") / name / activity / session
        timestamp = self.save_started_at.strftime("%Y%m%d_%H%M%S") if self.save_started_at else datetime.now().strftime("%Y%m%d_%H%M%S")
        filename_prefix = f"{session}_{timestamp}"

        channel_data = [
            [row[channel_idx] for row in self.save_rows]
            for channel_idx in range(DataLogger.TOTAL_CHANNELS)
        ]

        created_files = self.logger.save_data(
            filename_prefix=filename_prefix,
            file_extension=".csv",
            save_directory=str(save_dir),
            skip_initial_zeros=False,
            sample_rate=DataLogger.ADC_RATE_HZ,
            timestamp_start=0.0,
            combined=True,
            include_indices=True,
            indices_data=self.save_indices,
            channel_data=channel_data,
        )

        if created_files:
            self.status_var.set(f"Saved {len(self.save_rows)} samples to {created_files[0]}")
            messagebox.showinfo("Saved", f"Saved {len(self.save_rows)} samples to:\n{created_files[0]}")
        else:
            self.status_var.set("Save failed")
            messagebox.showerror("Save failed", "DataLogger.save_data() did not create a file.")

    def _refresh_window(self):
        self._drain_logger()
        self._update_plots(force_rescale=True)
        self.canvas.draw_idle()

    def _update_loop(self):
        self._drain_logger()
        self._update_plots()
        self.canvas.draw_idle()
        self.root.after(UI_INTERVAL_MS, self._update_loop)

    def _drain_logger(self):
        if self.logger is None:
            return

        rows = self.logger.drain_rows(update_buffers=True)
        if self.saving and rows:
            for index, row in rows:
                self.save_indices.append(index)
                self.save_rows.append(list(row))
            self.samples_var.set(f"Saved samples: {len(self.save_rows)}")

        stats = self.logger.get_reader_stats()
        if self.saving:
            suffix = f" | valid frames: {stats['valid_frames']} | queued rows: {stats['queued_rows']}"
            self.status_var.set(f"Saving... {len(self.save_rows)} samples{suffix}")

    def _update_plots(self, force_rescale=False):
        if self.logger is None:
            return

        adc_data = self.logger.get_adc_data(max_points=MAX_ADC_POINTS)
        for ch_idx, line in enumerate(self.piezo_lines):
            x, y = adc_data[ch_idx]
            line.set_data(x, y)

        for imu_idx in range(DataLogger.IMU_COUNT):
            start = DataLogger.ADC_CH + (imu_idx * DataLogger.IMU_CH_PER_SENSOR)
            for axis_idx in range(3):
                x, y = self._decimated_channel(start + axis_idx, max_points=MAX_IMU_POINTS)
                self.imu_lines[(imu_idx, "acc")][axis_idx].set_data(x, y)

                x, y = self._decimated_channel(start + 3 + axis_idx, max_points=MAX_IMU_POINTS)
                self.imu_lines[(imu_idx, "gyro")][axis_idx].set_data(x, y)

        if force_rescale:
            for axis in (self.ax_imu0_acc, self.ax_imu0_gyro, self.ax_imu1_acc, self.ax_imu1_gyro):
                axis.relim()
                axis.autoscale_view()
                axis.set_xlim(0, MAX_IMU_POINTS)
            for axis in self.piezo_axes:
                axis.set_xlim(0, BUFFER_LEN)
                axis.set_ylim(0, 4095)

    def _decimated_channel(self, channel_idx, max_points=None):
        values = list(self.logger.channels[channel_idx])[::DataLogger.ADC_BLOCK]
        if max_points is not None and len(values) > max_points:
            step = max(1, len(values) // max_points)
            values = values[::step]
        x_values = list(range(len(values)))
        return x_values, values

    def _on_close(self):
        if self.saving:
            if not messagebox.askyesno("Saving active", "Stop saving and write the captured data before closing?"):
                return
            self._stop_saving()

        if self.logger is not None:
            self.logger.stop_logging()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = DataLoggerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
