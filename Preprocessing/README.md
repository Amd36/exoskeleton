# Preprocessing Pipeline

This project aligns ESP32 sensor recordings with OpenCap MOT files, crops them to the relevant motion window, filters the piezo channels, and saves review plots plus summary logs.

The entrypoint is `preprocessing.py`, and it should be run from the top-level workspace directory.

## Runtime Parent Directory

Run the script from the top-level `OpenCapData_<session_id>_<zip_id>/` directory.

Expected structure before the first run:

```text
OpenCapData_<session_id>_<zip_id>/
|-- preprocessing.py
|-- <subjectID>/
|   |-- act_1/
|   |   |-- session_1/
|   |   |   `-- session_1_YYYYMMDD_HHMMSS_all.csv
|   |   `-- session_2/
|   |-- act_2/
|   `-- ...
`-- OpenCapData_<session_id>/
    |-- sessionMetadata.yaml
    `-- OpenSimData/
        `-- Kinematics/
            |-- act_1_session_1.mot
            |-- act_1_session_2.mot
            `-- ...
```

After processing, the workspace will additionally contain:

```text
OpenCapData_<session_id>_<zip_id>/
|-- processed_results/
`-- OpenCapData_<session_id>/
    `-- OpenSimData/
        `-- Kinematics/
            |-- act_1_session_1_processed.mot
            |-- act_1_session_2_processed.mot
            |-- original_data/
            |   |-- act_1_session_1.mot
            |   |-- act_1_session_2.mot
            |   `-- ...
            `-- ...
```

## How The Script Finds The Sensor Folder

The script does not hardcode a folder like `Junayed/`.

Instead, it:

1. finds `OpenCapData_<session_id>/OpenSimData/Kinematics/`
2. reads `OpenCapData_<session_id>/sessionMetadata.yaml`
3. extracts `subjectID`
4. looks for a folder with that name in the top-level workspace directory

Example:

- if `sessionMetadata.yaml` contains `subjectID: junayed`
- the sensor CSVs must be under `junayed/act_x/session_y/`
- on Windows, `Junayed/` also works because path matching is case-insensitive

## Input Naming Rules

- Sensor CSVs must live under `<subjectID>/act_x/session_y/`
- Sensor CSV filenames must end with `_all.csv`
- Raw MOT inputs must be present directly under:
  `OpenCapData_<session_id>/OpenSimData/Kinematics/`
- Each MOT filename must match the session folder naming pattern:
  `act_x_session_y.mot`

## What The Script Does

For each matched `act_x/session_y` pair, the script:

1. chooses the latest sensor CSV if duplicates exist
2. removes:
   - `adc0_gpio36`
   - all `imu0_*` columns
3. uses only `imu1_*` channels for alignment on the sensor side
4. uses only `pelvis_*`, `hip_*`, `knee_*`, and `ankle_*` MOT columns for alignment
5. estimates a time shift with cross-correlation
6. aligns the two recordings without resampling the saved outputs
7. crops the aligned MOT-guided window by:
   - ignoring the first `1.0 s`
   - ignoring the last `0.1 s`
   - finding `30%` of the valid-region MOT peak
   - keeping `1.0 s` before the first threshold crossing
   - keeping `1.0 s` after the last threshold crossing
8. crops the sensor CSV to the same final window and rebases both outputs to start at `0.0`
9. filters the piezo channels only:
   - low-pass `100 Hz`
   - notch `50 Hz`
   - notch `100 Hz`
10. writes processed files and review plots
11. moves every original `.mot` file from `Kinematics/` into
    `OpenCapData_<session_id>/OpenSimData/Kinematics/original_data/`

## Outputs

### Processed Sensor CSVs

Saved back into each session folder:

```text
<subjectID>/act_x/session_y/<original_name>_preocessed.csv
```

Note:

- the current filename is intentionally `_preocessed.csv` because that is what the script currently writes

### Processed MOT Files

Saved into:

```text
OpenCapData_<session_id>/OpenSimData/Kinematics/<original_name>_processed.mot
```

### Original MOT Archive

Saved into:

```text
OpenCapData_<session_id>/OpenSimData/Kinematics/original_data/
```

These are the raw `.mot` files moved out of `Kinematics/` after the script finishes.

### Review Artifacts

Saved into:

```text
processed_results/
```

This folder contains:

- `*_alignment.png`
- `*_cropped.png`
- `alignment_manifest.csv`
- `unmatched_sessions.csv`
- `csv_selection_log.csv`

## How To Run

From inside the top-level workspace directory:

```bash
python preprocessing.py
```

Optional help:

```bash
python preprocessing.py --help
```

## Python Dependencies

- `numpy`
- `pandas`
- `matplotlib`
- `scipy`

## Quick Sanity Check

Before running:

- `preprocessing.py` exists in the top-level workspace directory
- the subject folder named by `subjectID` exists in that same directory
- `OpenCapData_<session_id>/sessionMetadata.yaml` exists and contains `subjectID`
- `OpenCapData_<session_id>/OpenSimData/Kinematics/` exists
- the raw `.mot` files are already inside `OpenCapData_<session_id>/OpenSimData/Kinematics/`
- session names match between sensor folders and MOT filenames
- sensor CSVs still contain `timestamp`
