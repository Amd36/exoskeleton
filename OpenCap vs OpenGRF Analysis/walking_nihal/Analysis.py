"""
GRF Raw Plot Generator
======================

Description
-----------
This script reads two .mot files:

1. OpenGRF file
   Expected columns:
   - time
   - ground_force_calcn_l_vx
   - ground_force_calcn_l_vy
   - ground_force_calcn_l_vz
   - ground_force_calcn_r_vx
   - ground_force_calcn_r_vy
   - ground_force_calcn_r_vz

2. OpenCap file
   Expected columns:
   - time
   - ground_force_left_vx
   - ground_force_left_vy
   - ground_force_left_vz
   - ground_force_right_vx
   - ground_force_right_vy
   - ground_force_right_vz

The program:
- parses both .mot files,
- extracts data within a user-specified time window,
- generates six raw comparison plots with NO interpolation or resampling,
- saves the plots as PNG files.

Generated plots
---------------
1. left_vx_raw_no_interpolation.png
2. left_vy_raw_no_interpolation.png
3. left_vz_raw_no_interpolation.png
4. right_vx_raw_no_interpolation.png
5. right_vy_raw_no_interpolation.png
6. right_vz_raw_no_interpolation.png

How to use
----------
Run from terminal or command prompt like this:

python grf_plotter.py --opengrf Predicted_GRF_run.mot --opencap GRF_resultant_run_1.mot --start 3.3 --end 4.3 --outdir run_plots

Arguments
---------
--opengrf   Path to the OpenGRF .mot file
--opencap   Path to the OpenCap .mot file
--start     Start time in seconds
--end       End time in seconds
--outdir    Folder where the six plots will be saved

Example
-------
python grf_plotter.py --opengrf Predicted_GRF_walking.mot --opencap GRF_resultant_walking_walking_setup.mot --start 1.7 --end 4.5 --outdir walking_plots
"""

import argparse
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


def read_mot_file(file_path: Path) -> pd.DataFrame:
    """
    Read an OpenSim/OpenCap .mot file by locating the header line that starts with 'time'.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("time"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(f"Could not find a header line starting with 'time' in {file_path}")

    df = pd.read_csv(
        file_path,
        sep=r"\s+",
        engine="python",
        skiprows=header_idx,
    )

    # Remove accidental unnamed columns
    df = df.loc[:, ~df.columns.str.contains(r"^Unnamed")]
    return df


def validate_columns(df: pd.DataFrame, required_cols: list[str], source_name: str) -> None:
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required columns in {source_name}: {missing}\n"
            f"Available columns are:\n{list(df.columns)}"
        )


def filter_time_window(df: pd.DataFrame, start_time: float, end_time: float) -> pd.DataFrame:
    filtered = df[(df["time"] >= start_time) & (df["time"] <= end_time)].copy()
    if filtered.empty:
        raise ValueError(
            f"No data found in time window {start_time} to {end_time} s."
        )
    return filtered


def make_plot(
    opengrf_df: pd.DataFrame,
    opencap_df: pd.DataFrame,
    opengrf_col: str,
    opencap_col: str,
    label: str,
    out_path: Path,
) -> None:
    plt.figure(figsize=(10.5, 5.2))
    plt.plot(opengrf_df["time"], opengrf_df[opengrf_col], label=f"OpenGRF {label}")
    plt.plot(opencap_df["time"], opencap_df[opencap_col], label=f"OpenCap {label}")

    plt.xlabel("Time (s)")
    plt.ylabel(label)
    plt.title(f"{label} comparison")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close()


def generate_six_plots(
    opengrf_path: Path,
    opencap_path: Path,
    start_time: float,
    end_time: float,
    outdir: Path,
) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

    opengrf_df = read_mot_file(opengrf_path)
    opencap_df = read_mot_file(opencap_path)

    opengrf_required = [
        "time",
        "ground_force_calcn_l_vx",
        "ground_force_calcn_l_vy",
        "ground_force_calcn_l_vz",
        "ground_force_calcn_r_vx",
        "ground_force_calcn_r_vy",
        "ground_force_calcn_r_vz",
    ]

    opencap_required = [
        "time",
        "ground_force_left_vx",
        "ground_force_left_vy",
        "ground_force_left_vz",
        "ground_force_right_vx",
        "ground_force_right_vy",
        "ground_force_right_vz",
    ]

    validate_columns(opengrf_df, opengrf_required, "OpenGRF file")
    validate_columns(opencap_df, opencap_required, "OpenCap file")

    opengrf_w = filter_time_window(opengrf_df, start_time, end_time)
    opencap_w = filter_time_window(opencap_df, start_time, end_time)

    plot_pairs = [
        ("left_vx", "ground_force_calcn_l_vx", "ground_force_left_vx"),
        ("left_vy", "ground_force_calcn_l_vy", "ground_force_left_vy"),
        ("left_vz", "ground_force_calcn_l_vz", "ground_force_left_vz"),
        ("right_vx", "ground_force_calcn_r_vx", "ground_force_right_vx"),
        ("right_vy", "ground_force_calcn_r_vy", "ground_force_right_vy"),
        ("right_vz", "ground_force_calcn_r_vz", "ground_force_right_vz"),
    ]

    for label, opengrf_col, opencap_col in plot_pairs:
        out_path = outdir / f"{label}_raw_no_interpolation.png"
        make_plot(
            opengrf_df=opengrf_w,
            opencap_df=opencap_w,
            opengrf_col=opengrf_col,
            opencap_col=opencap_col,
            label=label,
            out_path=out_path,
        )

    print("Done.")
    print(f"OpenGRF samples in window: {len(opengrf_w)}")
    print(f"OpenCap samples in window: {len(opencap_w)}")
    print(f"Saved plots to: {outdir.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate six raw GRF comparison plots from OpenGRF and OpenCap .mot files."
    )
    parser.add_argument("--opengrf", required=True, type=Path, help="Path to OpenGRF .mot file")
    parser.add_argument("--opencap", required=True, type=Path, help="Path to OpenCap .mot file")
    parser.add_argument("--start", required=True, type=float, help="Start time in seconds")
    parser.add_argument("--end", required=True, type=float, help="End time in seconds")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("grf_plots"),
        help="Output directory to save plots",
    )

    args = parser.parse_args()

    if args.end <= args.start:
        raise ValueError("End time must be greater than start time.")

    generate_six_plots(
        opengrf_path=args.opengrf,
        opencap_path=args.opencap,
        start_time=args.start,
        end_time=args.end,
        outdir=args.outdir,
    )


if __name__ == "__main__":
    main()