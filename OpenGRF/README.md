# OpenGRF Automation

## Purpose

This folder contains a Windows automation workflow for running `Main_OpenGRF_v2` in MATLAB against a batch of `.mot` files from `../OpenSimData/Kinematics`.

The workflow combines:

- a Python launcher that orchestrates batch execution
- an AutoHotkey v2 script that handles the OpenGRF file dialogs and input popups
- log files that confirm what happened during each run

## Key Features

- Creates `opengrf_metadata.json` only if it does not already exist
- Discovers all `.mot` files in `../OpenSimData/Kinematics`
- Extracts `start_time` and `end_time` from each `.mot` file automatically
- Uses a fixed penetration value from metadata
- Launches MATLAB in the OpenGRF folder and runs `Main_OpenGRF_v2`
- Waits for AutoHotkey startup before launching MATLAB
- Waits for `Analysis successfully completed` in `matlab_launcher.log` before moving to the next `.mot`
- Attempts to stop the current MATLAB session if the analysis errors or times out
- Renames outputs after a successful run:
  - `ModelProcessed.osim` -> `ModelProcessed<ABBR>.osim`
  - `Solution` -> `Solution<ABBR>`
- Skips jobs whose archived outputs already exist
- Continues the batch if one analysis fails
- Records failed motion IDs and error reasons in `unsuccessful_analyses.txt`
- Removes failed-run leftovers before continuing:
  - `../OpenSimData/Kinematics/Solution`
  - `../OpenSimData/Model/ModelProcessed.osim`

## Main Files

- `launch_opengrf_automation.py`
  - Batch orchestrator
  - Reads metadata
  - Finds `.mot` jobs
  - Extracts time ranges
  - Starts AHK and MATLAB
  - Waits for success via the MATLAB log
  - Attempts MATLAB shutdown on failure or timeout
  - Renames outputs
  - Writes `unsuccessful_analyses.txt` if needed

- `run_opengrf_from_json.ahk`
  - Watches for the OpenGRF dialogs
  - Selects the `.osim` and `.mot` files
  - Fills `start_time`, `end_time`, and `penetration`
  - Confirms the frequency popup

- `opengrf_metadata.json`
  - Persistent configuration
  - Holds the OpenGRF folder, source `.osim`, penetration, and the list of `.mot` entries
  - Is not overwritten once it already exists

- `opengrf_automation.log`
  - AutoHotkey-side log

- `matlab_launcher.log`
  - MATLAB-side log
  - Used as the source of truth for completed analyses

- `unsuccessful_analyses.txt`
  - Created only when one or more jobs fail
  - Contains one failed motion ID plus the error reason per line

## Batch Workflow

1. The Python launcher loads or creates `opengrf_metadata.json`.
2. It builds one job per `.mot` file.
3. For each job, it extracts the time range from the `.mot`.
4. It starts the AHK watcher with:
   - current `.mot` path
   - extracted `start_time`
   - extracted `end_time`
5. It launches MATLAB and runs `Main_OpenGRF_v2`.
6. It waits until `matlab_launcher.log` contains `Analysis successfully completed` for the current run.
7. It renames the generated outputs using the motion abbreviation, for example:
   - `act_1_session_1` -> `A1S1`
   - `ModelProcessed.osim` -> `ModelProcessedA1S1.osim`
   - `Solution` -> `SolutionA1S1`
8. If a job fails, the launcher tries to stop the MATLAB session, removes `Solution` and `ModelProcessed.osim`, logs the failure, adds the motion ID plus the error reason to `unsuccessful_analyses.txt`, and continues with the next job.

## Usage

Run the batch launcher from this folder:

```powershell
python .\launch_opengrf_automation.py
```

## Notes For Future Agents

- The Python script is the batch entry point.
- The AHK script is a worker, not the scheduler.
- Do not assume the MATLAB process exiting means the analysis is finished.
  The launcher must rely on `matlab_launcher.log` success markers.
- If a run fails or times out, stop MATLAB and clean leftover outputs before the next job starts.
- `start_time` and `end_time` should come from the `.mot` files, not hardcoded defaults.
- If a run fails, check `matlab_launcher.log` first, then `opengrf_automation.log`.
- Partial or inconsistent archived outputs are treated as an error for that job.
- The batch should continue past individual failures and capture both the IDs and reasons in `unsuccessful_analyses.txt`.
