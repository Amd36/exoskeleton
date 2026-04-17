from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import matplotlib
import numpy as np
import pandas as pd
from scipy import signal

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


CSV_DROP_COLUMNS = {
    "adc0_gpio36",
    "imu0_acc_x",
    "imu0_acc_y",
    "imu0_acc_z",
    "imu0_gyro_x",
    "imu0_gyro_y",
    "imu0_gyro_z",
    "imu0_mag_x",
    "imu0_mag_y",
    "imu0_mag_z",
}
CSV_IMU_ALIGNMENT_PREFIX = "imu1_"
CSV_FILENAME_RE = re.compile(r"_(\d{8})_(\d{6})_all\.csv$", re.IGNORECASE)
MOT_ALIGNMENT_PREFIXES = ("pelvis_", "hip_", "knee_", "ankle_")
LOW_CONFIDENCE_CORRELATION = 0.55
MOT_CROP_IGNORE_SEC = 1.0
MOT_CROP_IGNORE_TAIL_SEC = 0.1
MOT_CROP_THRESHOLD_RATIO = 0.30
MOT_CROP_CONTEXT_SEC = 1.0
PIEZO_PREFIX = "adc"
PIEZO_LOWPASS_HZ = 100.0
PIEZO_NOTCH_HZ = (50.0, 100.0)
PIEZO_NOTCH_Q = 30.0


@dataclass(frozen=True)
class SessionPair:
    key: str
    act: str
    session: str
    csv_path: Path
    mot_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align ESP32 IMU CSV files to OpenCap MOT files and generate plots."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Workspace root containing the subjectID folder and OpenCapData_*/.",
    )
    parser.add_argument(
        "--max-shift-sec",
        type=float,
        default=3.0,
        help="Maximum absolute MOT time shift to search during alignment.",
    )
    parser.add_argument(
        "--context-sec",
        type=float,
        default=1.0,
        help="Maximum unmatched context kept at each edge after alignment.",
    )
    parser.add_argument(
        "--analysis-hz",
        type=float,
        default=120.0,
        help="Shared analysis frequency used only for lag estimation.",
    )
    parser.add_argument(
        "--smooth-sec",
        type=float,
        default=0.25,
        help="Smoothing window length in seconds for the activity envelopes.",
    )
    return parser.parse_args()


def find_kinematics_root(root: Path) -> Path:
    matches = [
        path
        for path in root.rglob("Kinematics")
        if path.is_dir() and path.parent.name == "OpenSimData"
    ]
    if not matches:
        raise FileNotFoundError("Could not find OpenSimData/Kinematics under the workspace root.")
    return sorted(matches)[0]


def find_session_metadata_path(kinematics_root: Path) -> Path:
    metadata_path = kinematics_root.parent.parent / "sessionMetadata.yaml"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Could not find sessionMetadata.yaml at {metadata_path}")
    return metadata_path


def read_subject_id(metadata_path: Path) -> str:
    for line in metadata_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("subjectID:"):
            subject_id = stripped.split(":", 1)[1].strip()
            if subject_id:
                return subject_id
            break
    raise ValueError(f"Could not read subjectID from {metadata_path}")


def find_sensor_root(root: Path, subject_id: str) -> Path:
    direct_path = root / subject_id
    if direct_path.exists():
        return direct_path

    subject_id_lower = subject_id.lower()
    for child in root.iterdir():
        if child.is_dir() and child.name.lower() == subject_id_lower:
            return child

    raise FileNotFoundError(
        f"Could not find the sensor-data folder for subjectID '{subject_id}' under {root}"
    )


def parse_csv_capture_datetime(path: Path) -> tuple[datetime, float]:
    match = CSV_FILENAME_RE.search(path.name)
    if match:
        stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    else:
        stamp = datetime.min
    return stamp, path.stat().st_mtime


def discover_latest_csvs(sensor_root: Path) -> tuple[dict[str, Path], list[dict[str, str]]]:
    grouped: dict[str, list[Path]] = {}
    for csv_path in sensor_root.rglob("*_all.csv"):
        if len(csv_path.parts) < len(sensor_root.parts) + 2:
            continue
        try:
            act = csv_path.parent.parent.name
            session = csv_path.parent.name
        except IndexError:
            continue
        key = f"{act}/{session}"
        grouped.setdefault(key, []).append(csv_path)

    latest: dict[str, Path] = {}
    duplicate_rows: list[dict[str, str]] = []
    for key, paths in grouped.items():
        selected = sorted(paths, key=parse_csv_capture_datetime)[-1]
        latest[key] = selected
        for path in sorted(paths, key=parse_csv_capture_datetime):
            duplicate_rows.append(
                {
                    "session_key": key,
                    "csv_path": str(path),
                    "selected_for_processing": str(path == selected),
                }
            )
    return latest, duplicate_rows


def discover_pairs(root: Path) -> tuple[list[SessionPair], list[dict[str, str]], list[dict[str, str]]]:
    kinematics_root = find_kinematics_root(root)
    metadata_path = find_session_metadata_path(kinematics_root)
    subject_id = read_subject_id(metadata_path)
    sensor_root = find_sensor_root(root, subject_id)
    latest_csvs, duplicate_rows = discover_latest_csvs(sensor_root)

    pairs: list[SessionPair] = []
    unmatched_rows: list[dict[str, str]] = []

    for key, csv_path in sorted(latest_csvs.items()):
        act, session = key.split("/")
        mot_path = kinematics_root / f"{act}_{session}.mot"
        if not mot_path.exists():
            unmatched_rows.append(
                {
                    "session_key": key,
                    "csv_path": str(csv_path),
                    "reason": "matching_mot_not_found",
                }
            )
            continue

        pairs.append(
            SessionPair(
                key=key,
                act=act,
                session=session,
                csv_path=csv_path,
                mot_path=mot_path,
            )
        )

    return pairs, unmatched_rows, duplicate_rows


def load_and_clean_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    drop_columns = [column for column in CSV_DROP_COLUMNS if column in df.columns]
    df = df.drop(columns=drop_columns)
    if "timestamp" not in df.columns:
        raise ValueError(f"{path} is missing the timestamp column.")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).reset_index(drop=True)
    for column in df.columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def load_mot(path: Path) -> tuple[list[str], pd.DataFrame]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        endheader_index = next(index for index, line in enumerate(lines) if line.strip() == "endheader")
    except StopIteration as exc:
        raise ValueError(f"{path} does not contain an endheader marker.") from exc

    preamble_lines = lines[: endheader_index + 1]
    df = pd.read_csv(
        path,
        sep=r"\s+",
        skiprows=endheader_index + 1,
        engine="python",
    )
    if "time" not in df.columns:
        raise ValueError(f"{path} is missing the time column.")
    for column in df.columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["time"]).reset_index(drop=True)
    return preamble_lines, df


def select_mot_alignment_columns(df: pd.DataFrame) -> list[str]:
    return [
        column
        for column in df.columns
        if column != "time" and column.startswith(MOT_ALIGNMENT_PREFIXES)
    ]


def estimate_sample_period(times: np.ndarray) -> float:
    diffs = np.diff(times)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if diffs.size == 0:
        return 0.0
    return float(np.median(diffs))


def select_piezo_columns(df: pd.DataFrame) -> list[str]:
    return [
        column
        for column in df.columns
        if column.startswith(PIEZO_PREFIX) and column != "adc0_gpio36"
    ]


def build_activity_envelope(matrix: np.ndarray) -> np.ndarray:
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        return np.zeros(0, dtype=float)
    if matrix.shape[0] == 1:
        return np.zeros(1, dtype=float)

    diffs = np.diff(matrix, axis=0, prepend=matrix[[0], :])
    center = np.median(diffs, axis=0)
    mad = np.median(np.abs(diffs - center), axis=0)
    scale = 1.4826 * mad
    std = np.std(diffs, axis=0)
    scale = np.where(scale < 1e-8, std, scale)
    scale = np.where(scale < 1e-8, 1.0, scale)
    normalized = diffs / scale
    return np.sqrt(np.mean(normalized**2, axis=1))


def smooth_and_standardize(values: np.ndarray, window_samples: int) -> np.ndarray:
    if values.size == 0:
        return values
    smoothed = values
    if window_samples > 1:
        kernel = np.ones(window_samples, dtype=float) / float(window_samples)
        smoothed = np.convolve(values, kernel, mode="same")
    smoothed = smoothed - np.mean(smoothed)
    std = np.std(smoothed)
    if std < 1e-8:
        return np.zeros_like(smoothed)
    return smoothed / std


def correlation_score(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = np.linalg.norm(left)
    right_norm = np.linalg.norm(right)
    if left_norm < 1e-8 or right_norm < 1e-8:
        return -1.0
    return float(np.dot(left, right) / (left_norm * right_norm))


def estimate_mot_shift(
    csv_times: np.ndarray,
    csv_envelope: np.ndarray,
    mot_times: np.ndarray,
    mot_envelope: np.ndarray,
    analysis_hz: float,
    smooth_sec: float,
    max_shift_sec: float,
) -> tuple[float, float]:
    analysis_dt = 1.0 / analysis_hz
    max_time = max(float(csv_times[-1]), float(mot_times[-1]))
    grid = np.arange(0.0, max_time + (analysis_dt * 0.5), analysis_dt)

    csv_interp = np.interp(grid, csv_times, csv_envelope, left=0.0, right=0.0)
    mot_interp = np.interp(grid, mot_times, mot_envelope, left=0.0, right=0.0)
    window_samples = max(1, int(round(smooth_sec / analysis_dt)))
    csv_signal = smooth_and_standardize(csv_interp, window_samples)
    mot_signal = smooth_and_standardize(mot_interp, window_samples)

    max_lag = int(round(max_shift_sec / analysis_dt))
    best_lag = 0
    best_score = -1.0

    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            left = csv_signal[-lag:]
            right = mot_signal[: lag if lag != 0 else None]
        elif lag > 0:
            left = csv_signal[:-lag]
            right = mot_signal[lag:]
        else:
            left = csv_signal
            right = mot_signal

        if left.size < max(10, window_samples * 4):
            continue

        score = correlation_score(left, right)
        if score > best_score:
            best_score = score
            best_lag = lag

    return best_lag * analysis_dt, best_score


def compute_window(
    csv_end: float,
    mot_end: float,
    mot_shift_sec: float,
    context_sec: float,
) -> tuple[float, float]:
    csv_start = 0.0
    mot_start = -mot_shift_sec
    mot_finish = mot_end - mot_shift_sec

    overlap_start = max(csv_start, mot_start)
    overlap_end = min(csv_end, mot_finish)
    if overlap_end <= overlap_start:
        raise ValueError("No overlapping interval remains after alignment.")

    union_start = min(csv_start, mot_start)
    union_end = max(csv_end, mot_finish)
    left_extra = overlap_start - union_start
    right_extra = union_end - overlap_end

    window_start = overlap_start - min(context_sec, left_extra)
    window_end = overlap_end + min(context_sec, right_extra)
    return window_start, window_end


def create_zero_frame(columns: Iterable[str], times: np.ndarray, time_column: str) -> pd.DataFrame:
    frame = pd.DataFrame(0.0, index=np.arange(times.size), columns=list(columns))
    frame[time_column] = times
    return frame


def make_pad_times_before(first_time: float, dt: float) -> np.ndarray:
    if first_time <= 1e-9:
        return np.array([], dtype=float)
    if dt <= 0:
        return np.array([0.0], dtype=float)
    times = [0.0]
    candidate = dt
    while candidate < first_time - (dt * 0.5):
        times.append(candidate)
        candidate += dt
    return np.array(times, dtype=float)


def make_pad_times_after(last_time: float, target_duration: float, dt: float) -> np.ndarray:
    if target_duration - last_time <= 1e-9:
        return np.array([], dtype=float)
    if dt <= 0:
        return np.array([], dtype=float)
    start = last_time + dt
    if start > target_duration + (dt * 0.5):
        return np.array([], dtype=float)
    return np.arange(start, target_duration + (dt * 0.5), dt, dtype=float)


def trim_and_pad_dataframe(
    df: pd.DataFrame,
    aligned_times: np.ndarray,
    time_column: str,
    window_start: float,
    window_end: float,
    dt: float,
) -> tuple[pd.DataFrame, float, float]:
    mask = (aligned_times >= window_start - 1e-9) & (aligned_times <= window_end + 1e-9)
    trimmed = df.loc[mask].copy().reset_index(drop=True)
    duration = max(0.0, window_end - window_start)

    if not trimmed.empty:
        trimmed_times = aligned_times[mask] - window_start
        trimmed[time_column] = trimmed_times
        first_time = float(trimmed[time_column].iloc[0])
        last_time = float(trimmed[time_column].iloc[-1])
    else:
        first_time = math.inf
        last_time = -math.inf

    start_pad_times = (
        np.arange(0.0, duration + (dt * 0.5), dt, dtype=float)
        if trimmed.empty
        else make_pad_times_before(first_time, dt)
    )
    end_pad_times = (
        np.array([], dtype=float)
        if trimmed.empty
        else make_pad_times_after(last_time, duration, dt)
    )

    frames: list[pd.DataFrame] = []
    if start_pad_times.size:
        frames.append(create_zero_frame(df.columns, start_pad_times, time_column))
    if not trimmed.empty:
        frames.append(trimmed)
    if end_pad_times.size:
        frames.append(create_zero_frame(df.columns, end_pad_times, time_column))

    if not frames:
        frames.append(create_zero_frame(df.columns, np.array([0.0], dtype=float), time_column))

    aligned = pd.concat(frames, ignore_index=True)
    aligned[time_column] = pd.to_numeric(aligned[time_column], errors="coerce").fillna(0.0)
    aligned = aligned.sort_values(time_column, kind="mergesort").drop_duplicates(
        subset=[time_column], keep="first"
    )
    aligned = aligned.reset_index(drop=True)

    start_padding_sec = float(aligned[time_column].iloc[0])
    actual_first_data_time = 0.0 if trimmed.empty else first_time
    start_padding_sec = actual_first_data_time
    end_padding_sec = 0.0 if trimmed.empty else max(0.0, duration - last_time)
    return aligned, start_padding_sec, end_padding_sec


def update_mot_preamble(preamble_lines: list[str], n_rows: int) -> list[str]:
    updated = []
    for line in preamble_lines:
        if line.startswith("nRows="):
            updated.append(f"nRows={n_rows}")
        else:
            updated.append(line)
    return updated


def write_mot(path: Path, preamble_lines: list[str], df: pd.DataFrame) -> None:
    updated_preamble = update_mot_preamble(preamble_lines, len(df))
    lines = updated_preamble + ["\t".join(df.columns)]
    for row in df.itertuples(index=False, name=None):
        lines.append("\t".join(f"{float(value):.8f}" for value in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def session_plot_stem(pair: SessionPair) -> str:
    return f"{pair.act}_{pair.session}"


def confidence_label(low_confidence: bool) -> str:
    return "not_ok" if low_confidence else "ok"


def determine_crop_window(
    mot_times: np.ndarray,
    mot_envelope: np.ndarray,
    ignore_sec: float,
    ignore_tail_sec: float,
    threshold_ratio: float,
    context_sec: float,
) -> tuple[float, float, float, float]:
    if mot_times.size == 0 or mot_envelope.size == 0:
        raise ValueError("Cannot determine a crop window from empty MOT data.")

    upper_time = float(mot_times[-1]) - max(0.0, ignore_tail_sec)
    valid_mask = (mot_times >= ignore_sec) & (mot_times <= upper_time + 1e-9)
    if not np.any(valid_mask):
        peak_index = int(np.argmax(mot_envelope))
        peak_value = float(mot_envelope[peak_index])
        threshold_value = peak_value * threshold_ratio
        peak_time = float(mot_times[peak_index])
        crop_start = max(0.0, peak_time - context_sec)
        crop_end = min(float(mot_times[-1]), peak_time + context_sec)
        return crop_start, crop_end, threshold_value, peak_value

    candidate_times = mot_times[valid_mask]
    candidate_envelope = mot_envelope[valid_mask]
    peak_index_local = int(np.argmax(candidate_envelope))
    peak_value = float(candidate_envelope[peak_index_local])
    threshold_value = peak_value * threshold_ratio

    above_threshold_mask = candidate_envelope >= threshold_value
    if np.any(above_threshold_mask):
        active_times = candidate_times[above_threshold_mask]
        crop_start = max(0.0, float(active_times[0]) - context_sec)
        crop_end = min(float(mot_times[-1]), float(active_times[-1]) + context_sec)
    else:
        peak_time = float(candidate_times[peak_index_local])
        crop_start = max(0.0, peak_time - context_sec)
        crop_end = min(float(mot_times[-1]), peak_time + context_sec)

    if crop_end <= crop_start:
        crop_end = min(float(mot_times[-1]), crop_start + context_sec)
    return crop_start, crop_end, threshold_value, peak_value


def crop_dataframe(df: pd.DataFrame, time_column: str, start_sec: float, end_sec: float) -> pd.DataFrame:
    cropped = df.loc[
        (df[time_column] >= start_sec - 1e-9) & (df[time_column] <= end_sec + 1e-9)
    ].copy()
    if cropped.empty:
        closest_index = int((df[time_column] - start_sec).abs().idxmin())
        cropped = df.iloc[[closest_index]].copy()
    first_retained_time = float(cropped[time_column].iloc[0])
    cropped[time_column] = cropped[time_column] - first_retained_time
    return cropped.reset_index(drop=True)


def apply_piezo_filters(df: pd.DataFrame, sampling_hz: float) -> pd.DataFrame:
    filtered = df.copy()
    piezo_columns = select_piezo_columns(filtered)
    if not piezo_columns or sampling_hz <= 0:
        return filtered

    nyquist = sampling_hz * 0.5
    lowpass_cutoff = min(PIEZO_LOWPASS_HZ, nyquist * 0.95)
    if lowpass_cutoff <= 0 or len(filtered) < 16:
        return filtered

    sos = signal.butter(4, lowpass_cutoff, btype="lowpass", fs=sampling_hz, output="sos")
    max_notch_freq = nyquist * 0.98

    for column in piezo_columns:
        values = filtered[column].to_numpy(dtype=float)
        filtered_values = signal.sosfiltfilt(sos, values)
        for notch_hz in PIEZO_NOTCH_HZ:
            if notch_hz >= max_notch_freq:
                continue
            b, a = signal.iirnotch(notch_hz, PIEZO_NOTCH_Q, fs=sampling_hz)
            filtered_values = signal.filtfilt(b, a, filtered_values)
        filtered[column] = filtered_values

    return filtered


def save_alignment_plot(
    path: Path,
    session_stem: str,
    correlation: float,
    csv_times: np.ndarray,
    csv_envelope: np.ndarray,
    mot_times: np.ndarray,
    mot_envelope: np.ndarray,
    threshold_value: float,
    crop_start_sec: float,
    crop_end_sec: float,
    low_confidence: bool,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    axes[0].plot(csv_times, csv_envelope, color="#0f766e", linewidth=1.2)
    axes[0].set_ylabel("IMU Activity")
    axes[0].grid(True, alpha=0.25)

    axes[1].plot(mot_times, mot_envelope, color="#b45309", linewidth=1.2)
    axes[1].axhline(
        threshold_value,
        color="#7c3aed",
        linestyle="--",
        linewidth=1.2,
        label=f"{MOT_CROP_THRESHOLD_RATIO * 100:.0f}% peak",
    )
    axes[1].axvline(crop_start_sec, color="#7c3aed", linestyle=":", linewidth=1.0)
    axes[1].axvline(crop_end_sec, color="#7c3aed", linestyle=":", linewidth=1.0)
    axes[1].set_ylabel("MOT Activity")
    axes[1].set_xlabel("Time (s)")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(loc="upper right")

    status = confidence_label(low_confidence)
    fig.suptitle(
        f"{session_stem} | corr = {correlation:.3f} | {status}",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def save_cropped_plot(
    path: Path,
    session_stem: str,
    csv_times: np.ndarray,
    csv_envelope: np.ndarray,
    mot_times: np.ndarray,
    mot_envelope: np.ndarray,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    axes[0].plot(csv_times, csv_envelope, color="#0f766e", linewidth=1.2)
    axes[0].set_ylabel("IMU Activity")
    axes[0].grid(True, alpha=0.25)

    axes[1].plot(mot_times, mot_envelope, color="#b45309", linewidth=1.2)
    axes[1].set_ylabel("MOT Activity")
    axes[1].set_xlabel("Time (s)")
    axes[1].grid(True, alpha=0.25)

    fig.suptitle(f"cropped {session_stem}", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def to_native_path(path: Path) -> str:
    return str(path)


def dataframe_from_rows(rows: list[dict[str, object]], sort_columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=sort_columns)
    available_sort_columns = [column for column in sort_columns if column in df.columns]
    if available_sort_columns:
        df = df.sort_values(by=available_sort_columns, na_position="last")
    return df.reset_index(drop=True)


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    postprocessing_root = root / "processed_results"
    postprocessing_root.mkdir(exist_ok=True)

    pairs, unmatched_rows, duplicate_rows = discover_pairs(root)
    manifest_rows: list[dict[str, object]] = []

    for pair in pairs:
        csv_df = load_and_clean_csv(pair.csv_path)
        mot_preamble, mot_df = load_mot(pair.mot_path)

        imu_columns = [column for column in csv_df.columns if column.startswith(CSV_IMU_ALIGNMENT_PREFIX)]
        if not imu_columns:
            unmatched_rows.append(
                {
                    "session_key": pair.key,
                    "csv_path": str(pair.csv_path),
                    "reason": "imu1_columns_not_found",
                }
            )
            continue

        csv_times = csv_df["timestamp"].to_numpy(dtype=float)
        mot_times = mot_df["time"].to_numpy(dtype=float)
        csv_dt = estimate_sample_period(csv_times)
        mot_dt = estimate_sample_period(mot_times)
        if csv_dt <= 0 or mot_dt <= 0:
            unmatched_rows.append(
                {
                    "session_key": pair.key,
                    "csv_path": str(pair.csv_path),
                    "reason": "invalid_timebase",
                }
            )
            continue

        csv_envelope = build_activity_envelope(csv_df[imu_columns].to_numpy(dtype=float))
        mot_columns = select_mot_alignment_columns(mot_df)
        if not mot_columns:
            unmatched_rows.append(
                {
                    "session_key": pair.key,
                    "csv_path": str(pair.csv_path),
                    "reason": "mot_alignment_columns_not_found",
                }
            )
            continue
        mot_envelope = build_activity_envelope(mot_df[mot_columns].to_numpy(dtype=float))
        mot_shift_sec, correlation = estimate_mot_shift(
            csv_times=csv_times,
            csv_envelope=csv_envelope,
            mot_times=mot_times,
            mot_envelope=mot_envelope,
            analysis_hz=args.analysis_hz,
            smooth_sec=args.smooth_sec,
            max_shift_sec=args.max_shift_sec,
        )

        try:
            window_start, window_end = compute_window(
                csv_end=float(csv_times[-1]),
                mot_end=float(mot_times[-1]),
                mot_shift_sec=mot_shift_sec,
                context_sec=args.context_sec,
            )
        except ValueError:
            unmatched_rows.append(
                {
                    "session_key": pair.key,
                    "csv_path": str(pair.csv_path),
                    "reason": "alignment_produced_no_overlap",
                }
            )
            continue

        aligned_csv_df, csv_pad_start, csv_pad_end = trim_and_pad_dataframe(
            df=csv_df,
            aligned_times=csv_times,
            time_column="timestamp",
            window_start=window_start,
            window_end=window_end,
            dt=csv_dt,
        )
        shifted_mot_times = mot_times - mot_shift_sec
        aligned_mot_df, mot_pad_start, mot_pad_end = trim_and_pad_dataframe(
            df=mot_df,
            aligned_times=shifted_mot_times,
            time_column="time",
            window_start=window_start,
            window_end=window_end,
            dt=mot_dt,
        )

        aligned_csv_envelope = build_activity_envelope(
            aligned_csv_df[imu_columns].to_numpy(dtype=float)
        )
        aligned_mot_envelope = build_activity_envelope(
            aligned_mot_df[mot_columns].to_numpy(dtype=float)
        )
        crop_start_sec, crop_end_sec, threshold_value, peak_value = determine_crop_window(
            mot_times=aligned_mot_df["time"].to_numpy(dtype=float),
            mot_envelope=aligned_mot_envelope,
            ignore_sec=MOT_CROP_IGNORE_SEC,
            ignore_tail_sec=MOT_CROP_IGNORE_TAIL_SEC,
            threshold_ratio=MOT_CROP_THRESHOLD_RATIO,
            context_sec=MOT_CROP_CONTEXT_SEC,
        )

        cropped_csv_df = crop_dataframe(
            aligned_csv_df,
            time_column="timestamp",
            start_sec=crop_start_sec,
            end_sec=crop_end_sec,
        )
        cropped_mot_df = crop_dataframe(
            aligned_mot_df,
            time_column="time",
            start_sec=crop_start_sec,
            end_sec=crop_end_sec,
        )
        processed_csv_df = apply_piezo_filters(cropped_csv_df, sampling_hz=1.0 / csv_dt)
        cropped_csv_envelope = build_activity_envelope(
            processed_csv_df[imu_columns].to_numpy(dtype=float)
        )
        cropped_mot_envelope = build_activity_envelope(
            cropped_mot_df[mot_columns].to_numpy(dtype=float)
        )

        csv_output_path = pair.csv_path.with_name(f"{pair.csv_path.stem}_preocessed.csv")
        mot_output_path = pair.mot_path.with_name(f"{pair.mot_path.stem}_processed.mot")
        processed_csv_df.to_csv(csv_output_path, index=False, float_format="%.6f")
        write_mot(mot_output_path, mot_preamble, cropped_mot_df)

        plot_stem = session_plot_stem(pair)
        alignment_plot_path = postprocessing_root / f"{plot_stem}_alignment.png"
        cropped_plot_path = postprocessing_root / f"{plot_stem}_cropped.png"
        low_confidence = correlation < LOW_CONFIDENCE_CORRELATION
        save_alignment_plot(
            path=alignment_plot_path,
            session_stem=plot_stem,
            correlation=correlation,
            csv_times=aligned_csv_df["timestamp"].to_numpy(dtype=float),
            csv_envelope=aligned_csv_envelope,
            mot_times=aligned_mot_df["time"].to_numpy(dtype=float),
            mot_envelope=aligned_mot_envelope,
            threshold_value=threshold_value,
            crop_start_sec=crop_start_sec,
            crop_end_sec=crop_end_sec,
            low_confidence=low_confidence,
        )
        save_cropped_plot(
            path=cropped_plot_path,
            session_stem=plot_stem,
            csv_times=processed_csv_df["timestamp"].to_numpy(dtype=float),
            csv_envelope=cropped_csv_envelope,
            mot_times=cropped_mot_df["time"].to_numpy(dtype=float),
            mot_envelope=cropped_mot_envelope,
        )

        manifest_rows.append(
            {
                "session_key": pair.key,
                "selected_csv_input": to_native_path(pair.csv_path),
                "mot_input": to_native_path(pair.mot_path),
                "csv_output": to_native_path(csv_output_path),
                "mot_output": to_native_path(mot_output_path),
                "alignment_plot_output": to_native_path(alignment_plot_path),
                "cropped_plot_output": to_native_path(cropped_plot_path),
                "mot_shift_sec": round(mot_shift_sec, 6),
                "correlation": round(correlation, 6),
                "low_confidence": low_confidence,
                "window_start_sec": round(window_start, 6),
                "window_end_sec": round(window_end, 6),
                "crop_start_sec": round(crop_start_sec, 6),
                "crop_end_sec": round(crop_end_sec, 6),
                "mot_peak_value_after_1s": round(peak_value, 6),
                "mot_threshold_30pct": round(threshold_value, 6),
                "csv_output_duration_sec": round(float(processed_csv_df["timestamp"].iloc[-1]), 6),
                "mot_output_duration_sec": round(float(cropped_mot_df["time"].iloc[-1]), 6),
                "csv_pad_start_sec": round(csv_pad_start, 6),
                "csv_pad_end_sec": round(csv_pad_end, 6),
                "mot_pad_start_sec": round(mot_pad_start, 6),
                "mot_pad_end_sec": round(mot_pad_end, 6),
                "csv_rows": int(len(processed_csv_df)),
                "mot_rows": int(len(cropped_mot_df)),
            }
        )

    manifest_df = dataframe_from_rows(manifest_rows, ["session_key"])
    manifest_path = postprocessing_root / "alignment_manifest.csv"
    manifest_df.to_csv(manifest_path, index=False)

    unmatched_df = dataframe_from_rows(unmatched_rows, ["session_key", "reason"])
    unmatched_path = postprocessing_root / "unmatched_sessions.csv"
    unmatched_df.to_csv(unmatched_path, index=False)

    duplicate_df = dataframe_from_rows(duplicate_rows, ["session_key", "csv_path"])
    duplicate_path = postprocessing_root / "csv_selection_log.csv"
    duplicate_df.to_csv(duplicate_path, index=False)

    print(f"Processed matched sessions: {len(manifest_df)}")
    print(f"Unmatched or skipped sessions: {len(unmatched_df)}")
    print(f"Manifest: {manifest_path}")
    print(f"Unmatched log: {unmatched_path}")
    print(f"CSV selection log: {duplicate_path}")


if __name__ == "__main__":
    main()
