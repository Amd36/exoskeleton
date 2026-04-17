# Preprocessing Postprocessing

This workspace contains a batch postprocessing pipeline for aligning ESP32 sensor recordings with OpenCap `.mot` files, cropping them to the relevant motion window, filtering the piezo channels, and generating review plots plus summary logs.

The current script entrypoint is `preprocessing.py`. Run that script from the workspace root.

## What The Script Does

`preprocessing.py` processes all matched `act_x/session_y` recordings in one run.

For each matched pair, it:

1. Finds the latest ESP32 CSV for the session if duplicates exist.
2. Drops invalid columns from the ESP32 file:
   - `adc0_gpio36`
   - all `imu0_*` columns
3. Uses only `imu1_*` channels from the ESP32 CSV for alignment.
4. Uses only lower-body kinematics from the MOT file for alignment:
   - `pelvis_*`
   - `hip_*`
   - `knee_*`
   - `ankle_*`
5. Builds activity envelopes from both sides and estimates the best time shift with cross-correlation.
6. Aligns the pair without changing native sample rates:
   - ESP32 CSV stays at about `500 Hz`
   - MOT stays at about `60 Hz`
7. Builds a crop window from the aligned MOT signal:
   - ignores the first `1.0 s`
   - ignores the last `0.1 s`
   - finds `30%` of the MOT peak within that valid region
   - keeps `1.0 s` before the first threshold crossing
   - keeps `1.0 s` after the last threshold crossing
8. Crops both the sensor CSV and MOT to that final window and rebases time to start at `0.0`.
9. Applies filtering to the piezo channels only:
   - low-pass at `100 Hz`
   - notch at `50 Hz`
   - notch at `100 Hz`
10. Saves processed outputs and diagnostic plots.

## Required Input Structure

Before running the script, the workspace root must contain these folders:

```text
Preprocessing/
├─ preprocessing.py
├─ <participant_name>/
│  ├─ act_1/
│  │  ├─ session_1/
│  │  │  └─ session_1_YYYYMMDD_HHMMSS_all.csv
│  │  └─ session_2/
│  ├─ act_2/
│  └─ ...
└─ OpenCapData_<session_id>_<zip_id>/
   └─ OpenCapData_<session_id>/
      └─ OpenSimData/
         └─ Kinematics/
            ├─ act_1_session_1.mot
            ├─ act_1_session_2.mot
            └─ ...
```

### Naming Rules

- The ESP32 parent folder must match the `subjectID` in:
  `OpenCapData_<session_id>_<zip_id>/OpenCapData_<session_id>/sessionMetadata.yaml`
- ESP32 files must live under `<subjectID>/act_x/session_y/`.
- ESP32 files must end with `_all.csv`.
- OpenCap MOT files must live under `OpenSimData/Kinematics/`.
- MOT filenames must match the session key as `act_x_session_y.mot`.

### Subject Folder Detection

The script does not assume the sensor-data folder is named `Junayed`.

Instead, it:

1. finds the OpenCap `OpenSimData/Kinematics/` folder
2. reads `sessionMetadata.yaml`
3. extracts `subjectID`
4. looks for the matching sensor-data folder at the workspace root

Example:

- if `sessionMetadata.yaml` contains `subjectID: junayed`
- the script expects the sensor CSVs under `junayed/act_x/session_y/`
- on case-insensitive filesystems such as Windows, `Junayed/` also works

### Duplicate Sensor Files

If a session contains more than one ESP32 CSV, the script keeps the latest file based on:

1. timestamp in the filename
2. file modified time as fallback

## Expected CSV Columns

The script expects the ESP32 CSV to contain:

- `timestamp`
- `index`
- piezo channels like `adc1_gpio39`, `adc2_gpio34`, `adc3_gpio35`, `adc4_gpio32`, `adc5_gpio33`
- IMU channels for `imu1_*`

The script will remove:

- `adc0_gpio36`
- every `imu0_*` column

## Outputs

After running, outputs are written to two places.

### 1. Processed Sensor CSVs

Saved back into each session folder:

```text
<subjectID>/act_x/session_y/<original_name>_preocessed.csv
```

Note:
- The current filename is spelled `_preocessed.csv` because that is what the script currently writes.

### 2. Processed MOT Files

Saved into the OpenCap Kinematics folder:

```text
OpenSimData/Kinematics/<original_name>_processed.mot
```

### 3. Review Artifacts

Saved into:

```text
processed_results/
```

This folder contains:

- `*_alignment.png`
  - full aligned plot before final crop
  - shows the `30%` threshold line
- `*_cropped.png`
  - final cropped plot
- `alignment_manifest.csv`
  - summary of selected inputs, shifts, crop window, outputs, and durations
- `unmatched_sessions.csv`
  - sessions that could not be processed
- `csv_selection_log.csv`
  - duplicate CSV selection decisions

## How To Run

From the workspace root:

```bash
python preprocessing.py
```

Optional help:

```bash
python preprocessing.py --help
```

## Python Dependencies

The script uses:

- `numpy`
- `pandas`
- `matplotlib`
- `scipy`

## Notes For Users And Agents

- Run the script from the workspace root so relative paths resolve correctly.
- Do not rename the `act_x/session_y` folders unless you also rename the matching MOT files.
- If a plot looks suspicious, check `alignment_manifest.csv` first for:
  - `correlation`
  - `crop_start_sec`
  - `crop_end_sec`
  - output paths
- The script does not resample the saved files to a common frequency.
- The alignment uses IMU data only on the sensor side; piezo is filtered after cropping and saved for later analysis.

## Quick Sanity Check Before Running

- the sensor-data folder named by `subjectID` exists at the workspace root
- OpenCap `OpenSimData/Kinematics/` exists
- `sessionMetadata.yaml` exists and contains `subjectID`
- session names match between CSV folders and MOT filenames
- CSV files still contain `timestamp`
- Python environment has `numpy`, `pandas`, `matplotlib`, and `scipy`
