# Inverse Dynamics Runner

This folder contains `run_inverse_dynamics.py`, a batch runner for OpenSim inverse dynamics. It creates the XML setup files OpenSim needs, validates the expected inputs, and runs `opensim.InverseDynamicsTool` for one session or for every discoverable session in the dataset.

This README is written for both:

- users who want to run the script safely, and
- agents or developers who need to understand the control flow and file conventions before automating it further.

## What the script does

For each session, the script:

1. finds or receives a model file, a coordinate motion file, and a predicted GRF file,
2. validates that those files exist and have the expected extensions,
3. checks that the GRF file contains the required left and right foot force, point, and torque columns,
4. reads the start and end time from the coordinate `.mot` file,
5. writes `GRF_setup.xml` beside the GRF file,
6. writes `ID_setup.xml` inside the session output folder,
7. runs OpenSim inverse dynamics, and
8. expects an output `.sto` file such as `inverse_dynamics.sto`.

The script logs progress and failures to `inverse_dynamics_log` in this folder.

## Dependencies

### Python

- Python 3.10 or newer is recommended.
- The script uses only Python standard library modules plus `opensim`.

Imported standard-library modules:

- `argparse`
- `re`
- `sys`
- `dataclasses`
- `datetime`
- `pathlib`
- `typing`

### OpenSim

- The Python bindings for OpenSim must be installed and importable as `opensim`.
- The script constructs `osim.ExternalLoads`, `osim.ExternalForce`, and `osim.InverseDynamicsTool`.
- The XML generated in this dataset uses OpenSim document version `40600`, so the environment should be compatible with the same OpenSim 4.x family. That version number is inferred from the generated XML files already present in this folder.

### Model assets

- The chosen `.osim` model must load successfully in OpenSim.
- If the model references a `Geometry/` directory or other relative assets, keep that folder structure intact.
- The script does not validate geometry assets directly, but OpenSim may fail at runtime if the model cannot resolve them.

## Expected file layout

When you run the script with no explicit input paths, it auto-discovers sessions from sibling folders:

```text
OpenSimData/
  Model/
    ModelProcessedA1S3.osim
    ModelProcessedA1S4.osim
    ...
  Kinematics/
    act_1_session_3.mot
    act_1_session_4.mot
    ...
    SolutionA1S3/
      Predicted_GRF_act_1_session_3.mot
    SolutionA1S4/
      Predicted_GRF_act_1_session_4.mot
  Inverse_Dynamics/
    run_inverse_dynamics.py
```

## Discovery rules

The batch mode works by intersecting three inferred session IDs:

- models: `ModelProcessed<session>.osim`
- coordinates: `act_*_session_*.mot`
- GRF folders and files: `Solution<session>/Predicted_GRF_*.mot`

Examples:

- `ModelProcessedA2S3.osim` -> session `A2S3`
- `act_2_session_3.mot` -> session `A2S3`
- `SolutionA2S3/Predicted_GRF_act_2_session_3.mot` -> session `A2S3`

Only sessions that have all three inputs are processed.

## Required inputs before running

Make sure all of these are true before you start:

- the OpenSim Python package imports successfully,
- each session has one `.osim` model, one coordinate `.mot`, and one predicted GRF `.mot`,
- the coordinate file contains a valid OpenSim-style header followed by `endheader`,
- the coordinate file contains time values in the first column after the header,
- the GRF file contains `endheader` and a header row whose first label is `time`,
- the GRF file includes the left and right foot force columns listed below,
- the model body names match the script assumptions: `calcn_l` and `calcn_r`,
- you have write access to the output folders.

## Required GRF columns

The script requires these columns for both left and right feet:

- `ground_force_calcn_l_vx`
- `ground_force_calcn_l_vy`
- `ground_force_calcn_l_vz`
- `ground_force_calcn_l_px`
- `ground_force_calcn_l_py`
- `ground_force_calcn_l_pz`
- `ground_torque_calcn_l_vx`
- `ground_torque_calcn_l_vy`
- `ground_torque_calcn_l_vz`
- `ground_force_calcn_r_vx`
- `ground_force_calcn_r_vy`
- `ground_force_calcn_r_vz`
- `ground_force_calcn_r_px`
- `ground_force_calcn_r_py`
- `ground_force_calcn_r_pz`
- `ground_torque_calcn_r_vx`
- `ground_torque_calcn_r_vy`
- `ground_torque_calcn_r_vz`

Extra columns can exist. They are ignored by this script.

## Outputs

For a session like `A2S3`, the script writes:

- `OpenSimData/Kinematics/SolutionA2S3/GRF_setup.xml`
- `OpenSimData/Inverse_Dynamics/A2S3/ID_setup.xml`
- `OpenSimData/Inverse_Dynamics/A2S3/inverse_dynamics.sto`

It also appends progress logs to:

- `OpenSimData/Inverse_Dynamics/inverse_dynamics_log`

## Command-line usage

Run from this folder or from the project root with a Python environment that can import `opensim`.

### Batch mode

Process every discoverable session:

```bash
python OpenSimData/Inverse_Dynamics/run_inverse_dynamics.py
```

### Single-session mode

Provide all three explicit inputs together:

```bash
python OpenSimData/Inverse_Dynamics/run_inverse_dynamics.py --model OpenSimData/Model/ModelProcessedA2S3.osim --coordinates OpenSimData/Kinematics/act_2_session_3.mot --grf OpenSimData/Kinematics/SolutionA2S3/Predicted_GRF_act_2_session_3.mot --session-id A2S3
```

### Useful options

- `--output-root`
  Sets the root folder for inverse dynamics outputs. Default: `OpenSimData/Inverse_Dynamics`.
- `--output-file`
  Sets the output `.sto` filename inside each session output folder. Default: `inverse_dynamics.sto`.
- `--lowpass-cutoff`
  Sets the coordinate low-pass cutoff frequency passed to OpenSim. Default: `6.0`.
  Use a negative value to disable filtering.
- `--overwrite`
  Re-runs sessions even if the output `.sto` file already exists.

## Important behavior and assumptions

### Filtering

- The low-pass filter is applied only to the coordinates file through `InverseDynamicsTool.setLowpassCutoffFrequency`.
- A negative cutoff disables filtering.

### Time range

- The script extracts the first and last numeric time values from the coordinate `.mot`.
- It does not read the time range from the GRF file.
- If the coordinate and GRF time windows do not align well enough for OpenSim, the OpenSim run may fail even though the script's own validation passes.

### Forces excluded

- The inverse dynamics tool excludes `"Muscles"` when computing generalized forces.

### External force mapping

The script hardcodes two external loads:

- `left_GRF` applied to `calcn_l`
- `right_GRF` applied to `calcn_r`

Both loads are expressed in `ground`, and their identifiers are built from the GRF column prefixes:

- left: `ground_force_calcn_l_v`, `ground_force_calcn_l_p`, `ground_torque_calcn_l_v`
- right: `ground_force_calcn_r_v`, `ground_force_calcn_r_p`, `ground_torque_calcn_r_v`

If your model uses different foot body names or different GRF column names, the script must be edited before it will work correctly.

## Implementation walkthrough

This section maps the main functions so another agent can reason about the script quickly.

### Entry point

- `main()`
  Parses CLI arguments, builds run configurations, loops through each session, runs jobs, records failures, and returns exit code `0` or `1`.

### Configuration building

- `parse_args()`
  Defines batch mode and single-run mode arguments.
- `build_run_configs()`
  Enforces the rule that `--model`, `--coordinates`, and `--grf` must either all be provided or all be omitted.
- `build_single_run_config()`
  Creates one `RunConfig` from explicit paths.
- `discover_run_configs()`
  Auto-discovers complete session triplets from the default `Model/` and `Kinematics/` folders.

### Session ID inference

- `infer_session_id()`
  Uses `--session-id` first, then `Solution<session>`, then `ModelProcessed<session>`, then a fallback abbreviation from the coordinate filename.
- `abbreviate_motion_id()`
  Converts names such as `act_2_session_3` into `A2S3`.

### Validation and parsing

- `validate_inputs()`
  Ensures the required files exist and checks `.osim` and `.mot` extensions.
- `extract_time_range()`
  Scans the coordinate `.mot` after `endheader` and records the first and last numeric time values.
- `extract_column_labels()`
  Reads the first label row after `endheader` whose first column is `time`.
- `validate_grf_columns()`
  Confirms all required foot GRF vector components exist.
- `required_vector_columns()`
  Expands prefixes into `x`, `y`, and `z` column names.

### OpenSim XML generation

- `write_grf_setup()`
  Creates `GRF_setup.xml` with two `ExternalForce` definitions.
- `build_grf_data_source_name()`
  Builds the OpenSim storage name as `GRF<coordinates_filename>`.
- `write_id_setup()`
  Creates `ID_setup.xml` and points it at the model, coordinates file, external loads file, time range, and output filename.

### Execution

- `run_job()`
  Runs validation, creates output folders, skips existing results unless `--overwrite` is set, writes XML, and launches inverse dynamics.
- `run_inverse_dynamics()`
  Loads `ID_setup.xml` into `osim.InverseDynamicsTool`, calls `run()`, and verifies that the expected output `.sto` exists.

### Utilities

- `as_opensim_path()`
  Normalizes Windows paths to forward-slash paths for OpenSim XML.
- `log_message()`
  Appends timestamped messages to `inverse_dynamics_log`.

## Pre-run checklist

Use this short checklist before launching a batch:

- confirm `python` points to the environment where `opensim` is installed,
- confirm the model opens successfully in OpenSim or in a small Python import test,
- confirm each target session has a matching model, coordinates file, and GRF file,
- confirm the GRF file includes the required `calcn_l` and `calcn_r` columns,
- confirm the coordinate file has a readable time range,
- decide whether you want to keep previous results or pass `--overwrite`,
- decide whether the default `6.0 Hz` coordinate filter is appropriate for your data.

## Troubleshooting

- `Provide --model, --coordinates, and --grf together`
  You mixed batch mode and single-run mode arguments. Provide all three explicit paths or none.
- `Could not find any complete session triplets`
  Auto-discovery did not find matching session IDs across `Model/` and `Kinematics/`.
- `Missing required input files`
  One or more supplied paths does not exist.
- `The predicted GRF file is missing required columns`
  The GRF file does not contain the required foot force, point, or torque labels.
- `Could not extract a time range`
  The coordinate `.mot` file did not provide numeric time rows after `endheader`.
- `OpenSim InverseDynamicsTool reported failure`
  OpenSim failed during execution. Common causes are model incompatibility, bad external-load mapping, missing geometry, or mismatched time/data ranges.
- output file missing after a reported run
  The OpenSim tool returned without creating the expected `.sto`; treat that run as failed.

## Agent notes

If you automate or modify this script, preserve these key invariants:

- batch discovery depends on session ID agreement across three naming schemes,
- the script writes `GRF_setup.xml` next to the GRF file, not in the output directory,
- the script uses the coordinate file as the source of truth for the inverse-dynamics time range,
- the foot body names and GRF column prefixes are hardcoded,
- skipping is based only on whether the expected output `.sto` already exists,
- OpenSim path strings are normalized to forward slashes before XML is written.
