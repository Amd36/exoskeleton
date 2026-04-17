# OpenGRF Parallel Automation

## Purpose

This folder contains a Windows automation workflow for running `Main_OpenGRF_v2` in MATLAB against `.mot` files from `../OpenSimData/Kinematics`.

The workflow is now designed for **parallel batch execution**:

- a Python launcher schedules multiple jobs at once
- each worker gets its own isolated OpenGRF sandbox
- an AutoHotkey v2 watcher drives the OpenGRF dialogs for that worker only
- worker-local MATLAB and AHK logs are preserved per job attempt
- a central run manifest tracks queued, running, succeeded, and failed jobs

## What Changed

The launcher no longer uses one shared OpenGRF folder, one shared metadata file, or one shared `Solution` / `ModelProcessed.osim` output location while jobs are running.

Instead, each worker gets:

- `runtime/worker_XX/OpenGRF_v2`
- `runtime/worker_XX/logs`
- `runtime/worker_XX/job_stage/<mot_id>/Model`
- `runtime/worker_XX/job_stage/<mot_id>/Kinematics`

Each job is staged into the worker-local `Model` and `Kinematics` folders before MATLAB starts. OpenGRF writes `ModelProcessed.osim` and `Solution` into that staged job area, and the launcher archives them back to the canonical output names after success:

- `ModelProcessed.osim` -> `ModelProcessed<ABBR>.osim`
- `Solution` -> `Solution<ABBR>`

This isolation is what makes 5-way parallel runs clean.

## Main Files

- `launch_opengrf_automation.py`
  - batch entry point
  - discovers jobs
  - prepares worker sandboxes
  - stages inputs
  - launches MATLAB and AHK per worker
  - archives outputs
  - writes `opengrf_run_manifest.json`
  - writes `unsuccessful_analyses.txt` when needed

- `run_opengrf_from_json.ahk`
  - worker-local dialog watcher
  - reads a worker-local metadata file
  - writes a worker-local AHK log
  - targets windows by MATLAB PID instead of global title-only matching

- `opengrf_metadata.json`
  - persistent user configuration
  - contains the source OpenGRF folder, source `.osim`, `.mot` entries, and runtime defaults

- `opengrf_run_manifest.json`
  - machine-readable run manifest
  - records job status, worker ID, attempt number, final outputs, log paths, and errors

- `unsuccessful_analyses.txt`
  - created only when one or more jobs fail
  - contains one failed motion ID plus the error reason per line

## Metadata Keys

If `opengrf_metadata.json` is created from scratch, it now includes these runtime defaults:

- `max_parallel_sessions`
- `worker_runtime_root`
- `analysis_timeout_sec`
- `startup_stagger_sec`
- `retry_count`
- `matlab_extra_args`

Existing metadata files still work. Missing keys fall back to defaults in the launcher.

## Batch Workflow

1. The Python launcher loads or creates `opengrf_metadata.json`.
2. It discovers one job per `.mot` file and computes `start_time` / `end_time`.
3. It prepares `runtime/worker_XX` sandboxes.
4. For each queued job, it copies the source `.osim` and current `.mot` into the worker-local stage folder.
5. It writes a worker-local metadata file for that staged job.
6. It starts MATLAB for that worker and captures the MATLAB PID.
7. It starts the worker-local AHK watcher and binds it to that MATLAB PID.
8. MATLAB waits for a ready signal, then runs `Main_OpenGRF_v2`.
9. The launcher waits for `Analysis successfully completed` in the worker-local MATLAB log.
10. On success, the staged outputs are moved to:
    - `../OpenSimData/Model/ModelProcessed<ABBR>.osim`
    - `../OpenSimData/Kinematics/Solution<ABBR>`
11. On failure, only that worker's staged outputs are cleaned up, logs are preserved, and the batch continues.

## Usage

Run the launcher from this folder:

```powershell
python .\launch_opengrf_automation.py
```

Optional overrides:

```powershell
python .\launch_opengrf_automation.py `
  --max-parallel-sessions 5 `
  --analysis-timeout-sec 3600 `
  --startup-stagger-sec 3 `
  --retry-count 1 `
  --worker-runtime-root .\runtime `
  --matlab-extra-arg -singleCompThread
```

## Output And Logs

Per-attempt logs are stored under each worker:

- `runtime/worker_XX/logs/<mot_id>__attempt_YY.matlab.log`
- `runtime/worker_XX/logs/<mot_id>__attempt_YY.ahk.log`

The repo-root legacy logs are no longer used for active worker runs:

- `matlab_launcher.log`
- `opengrf_automation.log`

The central manifest is:

- `opengrf_run_manifest.json`

## Notes For Future Agents

- Keep the workflow **process-based**, not `parpool` / `parfor` based.
- The OpenGRF `.p` files are treated as a black box.
- Do not point active parallel workers at the shared source `Model` or `Kinematics` folders.
- The scheduler owns concurrency; the AHK script is a worker helper only.
- Do not assume a clean MATLAB exit means success. The launcher must still rely on the MATLAB log success marker.
- On failure, only terminate the current worker's MATLAB PID tree.
- Preserve worker logs for failed jobs.
