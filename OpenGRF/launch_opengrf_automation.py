from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only at runtime
    winreg = None


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_PATH = SCRIPT_DIR / "opengrf_metadata.json"
AHK_SCRIPT_PATH = SCRIPT_DIR / "run_opengrf_from_json.ahk"
AHK_LOG_PATH = SCRIPT_DIR / "opengrf_automation.log"
MATLAB_LOG_PATH = SCRIPT_DIR / "matlab_launcher.log"
UNSUCCESSFUL_ANALYSES_PATH = SCRIPT_DIR / "unsuccessful_analyses.txt"

DEFAULT_OPENGRF_DIR = Path(r"C:\Users\Junayed\Documents\OpenSim\OpenGRF_v2")
DEFAULT_MATLAB_EXE = Path(r"G:\Matlab_R2025a\bin\matlab.exe")
DEFAULT_KINEMATICS_DIR = (SCRIPT_DIR / ".." / "OpenSimData" / "Kinematics").resolve()
DEFAULT_MODEL_DIR = (SCRIPT_DIR / ".." / "OpenSimData" / "Model").resolve()

DEFAULT_WATCH_TIMEOUT_SEC = 120
DEFAULT_POST_YES_WAIT_MS = 2000
DEFAULT_PENETRATION = 20
DEFAULT_ANALYSIS_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class MotJob:
    mot_id: str
    mot_path: Path
    base_dir: Path
    abbreviation: str
    start_time: float
    end_time: float


def main() -> int:
    metadata = ensure_metadata(METADATA_PATH)
    opengrf_dir = resolve_path(metadata.get("opengrf_folder"), DEFAULT_OPENGRF_DIR)
    matlab_exe = find_matlab_exe()
    jobs = build_mot_jobs(metadata)

    if not opengrf_dir.exists():
        raise FileNotFoundError(f"OpenGRF folder not found: {opengrf_dir}")

    if not AHK_SCRIPT_PATH.exists():
        raise FileNotFoundError(f"AHK script not found: {AHK_SCRIPT_PATH}")

    if not jobs:
        raise RuntimeError("No .mot files were found to process.")

    print(f"MATLAB executable: {matlab_exe}")
    print(f"MATLAB working folder: {opengrf_dir}")
    print(f"Metadata file: {METADATA_PATH}")
    print(f"MATLAB launcher log: {MATLAB_LOG_PATH}")
    clear_unsuccessful_analyses_file()

    total_jobs = len(jobs)
    unsuccessful_jobs: list[tuple[str, str]] = []
    for index, job in enumerate(jobs, start=1):
        matlab_process: subprocess.Popen[str] | None = None
        try:
            model_target, solution_target = get_target_paths(metadata, job)
            if model_target.exists() != solution_target.exists():
                raise FileExistsError(
                    f"Only one archived output exists for {job.mot_id}: "
                    f"{model_target} / {solution_target}"
                )
            if model_target.exists() and solution_target.exists():
                print(f"[{index}/{total_jobs}] Skipping {job.mot_id} because archived outputs already exist.")
                continue

            print(f"[{index}/{total_jobs}] Starting {job.mot_id} -> {job.abbreviation}")
            print(f"    Time range: {format_time_value(job.start_time)} to {format_time_value(job.end_time)}")
            append_launcher_log(f"\n=== {timestamp()} :: START {job.mot_id} ({job.abbreviation}) ===\n")

            run_started_at = time.time()
            matlab_log_offset = get_log_size(MATLAB_LOG_PATH)
            ahk_process = launch_ahk(
                AHK_SCRIPT_PATH,
                job.mot_path,
                job.start_time,
                job.end_time,
                silent=True,
            )
            wait_for_ahk_startup(run_started_at)

            matlab_command = build_matlab_command(opengrf_dir, job.mot_id)
            matlab_process = subprocess.Popen(
                [str(matlab_exe), "-sd", str(opengrf_dir), "-r", matlab_command],
                cwd=opengrf_dir,
                creationflags=get_process_group_flag(),
            )

            matlab_return_code = wait_for_matlab_outcome(
                matlab_process,
                job.mot_id,
                matlab_log_offset,
                timeout_sec=DEFAULT_ANALYSIS_TIMEOUT_SEC,
            )
            ahk_return_code = wait_for_optional_process(ahk_process, timeout_sec=15)

            if matlab_return_code != 0:
                matlab_summary = summarize_last_matlab_failure(job.mot_id)
                raise RuntimeError(
                    f"MATLAB exited with code {matlab_return_code} while processing {job.mot_id}. "
                    f"See {MATLAB_LOG_PATH}.\n{matlab_summary}"
                )

            if ahk_return_code not in (None, 0):
                raise RuntimeError(
                    f"AutoHotkey exited with code {ahk_return_code} while processing {job.mot_id}. "
                    f"See {SCRIPT_DIR / 'opengrf_automation.log'}."
                )

            archive_outputs(metadata, job, run_started_at)
            append_launcher_log(f"=== {timestamp()} :: DONE {job.mot_id} ({job.abbreviation}) ===\n")
            print(f"[{index}/{total_jobs}] Finished {job.mot_id}")
        except Exception as exc:
            terminate_matlab_session(matlab_process)
            cleanup_warning = cleanup_failed_analysis_outputs(metadata, job)
            failure_reason = str(exc)
            if cleanup_warning:
                failure_reason += " | Cleanup warning: " + cleanup_warning
            record_unsuccessful_analysis(job.mot_id, failure_reason, unsuccessful_jobs)
            append_launcher_log(f"=== {timestamp()} :: FAILED {job.mot_id} ({job.abbreviation}) ===\n")
            print(f"[{index}/{total_jobs}] Failed {job.mot_id}")
            print(f"    {failure_reason}")
            continue

    if unsuccessful_jobs:
        print(f"Completed with some failures. See {UNSUCCESSFUL_ANALYSES_PATH}")
        return 1

    print("All requested OpenGRF analyses are complete.")
    return 0


def ensure_metadata(path: Path) -> dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    metadata = create_default_metadata()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)
        handle.write("\n")

    print(f"Created default metadata file: {path}")
    return metadata


def create_default_metadata() -> dict[str, Any]:
    if not DEFAULT_KINEMATICS_DIR.exists():
        raise FileNotFoundError(f"Kinematics directory not found: {DEFAULT_KINEMATICS_DIR}")

    if not DEFAULT_MODEL_DIR.exists():
        raise FileNotFoundError(f"Model directory not found: {DEFAULT_MODEL_DIR}")

    osim_path = choose_source_osim(DEFAULT_MODEL_DIR)
    mot_files = sorted(DEFAULT_KINEMATICS_DIR.glob("*.mot"))
    if not mot_files:
        raise RuntimeError(f"No .mot files found in {DEFAULT_KINEMATICS_DIR}")

    return {
        "opengrf_folder": str(DEFAULT_OPENGRF_DIR).replace("\\", "/"),
        "osim": str(osim_path).replace("\\", "/"),
        "mot_entries": [
            {
                "base_dir": "../OpenSimData/Kinematics",
                "mot_id": mot_file.stem,
            }
            for mot_file in mot_files
        ],
        "penetration": DEFAULT_PENETRATION,
        "estimate_frequency_content": True,
        "watch_timeout_sec": DEFAULT_WATCH_TIMEOUT_SEC,
        "post_yes_wait_ms": DEFAULT_POST_YES_WAIT_MS,
    }


def choose_source_osim(model_dir: Path) -> Path:
    preferred = model_dir / "LaiUhlrich2022_scaled.osim"
    if preferred.exists():
        return preferred

    candidates = sorted(
        path
        for path in model_dir.glob("*.osim")
        if not path.stem.startswith("ModelProcessed")
    )
    if candidates:
        return candidates[0]

    raise FileNotFoundError(f"No source .osim file found in {model_dir}")


def build_mot_jobs(metadata: dict[str, Any]) -> list[MotJob]:
    entries = metadata.get("mot_entries")
    if isinstance(entries, list) and entries:
        jobs: list[MotJob] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            base_dir = resolve_path(entry.get("base_dir"), DEFAULT_KINEMATICS_DIR)
            mot_id = str(entry.get("mot_id", "")).strip()
            if mot_id == "":
                continue
            mot_path = base_dir / f"{mot_id}.mot"
            start_time, end_time = extract_time_range(mot_path)
            jobs.append(
                MotJob(
                    mot_id=mot_id,
                    mot_path=mot_path,
                    base_dir=base_dir,
                    abbreviation=abbreviate_mot_id(mot_id),
                    start_time=start_time,
                    end_time=end_time,
                )
            )
        validate_jobs(jobs)
        return jobs

    base_dir = resolve_path(metadata.get("mot_base_dir"), DEFAULT_KINEMATICS_DIR)
    jobs = []
    for mot_file in sorted(base_dir.glob("*.mot")):
        start_time, end_time = extract_time_range(mot_file)
        jobs.append(
            MotJob(
                mot_id=mot_file.stem,
                mot_path=mot_file,
                base_dir=base_dir,
                abbreviation=abbreviate_mot_id(mot_file.stem),
                start_time=start_time,
                end_time=end_time,
            )
        )
    validate_jobs(jobs)
    return jobs


def validate_jobs(jobs: list[MotJob]) -> None:
    if not jobs:
        raise RuntimeError("No mot jobs were found in the metadata or kinematics directory.")

    missing = [str(job.mot_path) for job in jobs if not job.mot_path.exists()]
    if missing:
        raise FileNotFoundError("Missing .mot files:\n" + "\n".join(missing))


def get_target_paths(metadata: dict[str, Any], job: MotJob) -> tuple[Path, Path]:
    model_dir = resolve_model_dir(metadata)
    model_target = model_dir / f"ModelProcessed{job.abbreviation}.osim"
    solution_target = job.base_dir / f"Solution{job.abbreviation}"
    return model_target, solution_target


def resolve_model_dir(metadata: dict[str, Any]) -> Path:
    osim_value = metadata.get("osim")
    if osim_value:
        return resolve_path(osim_value, DEFAULT_MODEL_DIR / "placeholder.osim").parent
    return DEFAULT_MODEL_DIR


def archive_outputs(metadata: dict[str, Any], job: MotJob, run_started_at: float) -> None:
    model_dir = resolve_model_dir(metadata)
    model_source = model_dir / "ModelProcessed.osim"
    solution_source = job.base_dir / "Solution"
    model_target, solution_target = get_target_paths(metadata, job)

    wait_for_fresh_path(model_source, run_started_at, is_dir=False)
    wait_for_fresh_path(solution_source, run_started_at, is_dir=True)

    if model_target.exists():
        raise FileExistsError(f"Target model already exists: {model_target}")
    if solution_target.exists():
        raise FileExistsError(f"Target solution folder already exists: {solution_target}")

    model_source.rename(model_target)
    solution_source.rename(solution_target)


def cleanup_failed_analysis_outputs(metadata: dict[str, Any], job: MotJob) -> str:
    model_dir = resolve_model_dir(metadata)
    model_source = model_dir / "ModelProcessed.osim"
    solution_source = job.base_dir / "Solution"
    issues: list[str] = []

    try:
        remove_path_with_retries(solution_source, is_dir=True)
    except OSError as exc:
        issues.append(f"could not remove {solution_source}: {exc}")

    try:
        remove_path_with_retries(model_source, is_dir=False)
    except OSError as exc:
        issues.append(f"could not remove {model_source}: {exc}")

    return " | ".join(issues)


def remove_path_with_retries(path: Path, is_dir: bool) -> None:
    if not path.exists():
        return

    last_error: OSError | None = None
    for _ in range(10):
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)

    if last_error is not None:
        raise last_error


def wait_for_fresh_path(path: Path, run_started_at: float, is_dir: bool) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if path.exists():
            modified = get_latest_mtime(path)
            if modified >= run_started_at - 1.0:
                return
        time.sleep(0.5)

    kind = "directory" if is_dir else "file"
    raise FileNotFoundError(f"Expected fresh output {kind} was not produced: {path}")


def wait_for_ahk_startup(run_started_at: float) -> None:
    deadline = time.time() + 8
    while time.time() < deadline:
        if AHK_LOG_PATH.exists() and AHK_LOG_PATH.stat().st_mtime >= run_started_at - 1.0:
            return
        time.sleep(0.25)

    raise RuntimeError(
        "AutoHotkey did not initialize cleanly. "
        f"Check {AHK_LOG_PATH} and any visible AutoHotkey error dialog."
    )


def get_latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    if not path.is_dir():
        return latest

    for child in path.rglob("*"):
        try:
            child_mtime = child.stat().st_mtime
        except FileNotFoundError:
            continue
        if child_mtime > latest:
            latest = child_mtime

    return latest


def find_matlab_exe() -> Path:
    matlab_on_path = shutil.which("matlab")
    if matlab_on_path:
        return Path(matlab_on_path)

    if DEFAULT_MATLAB_EXE.exists():
        return DEFAULT_MATLAB_EXE

    raise FileNotFoundError(
        "Could not find matlab.exe. Update DEFAULT_MATLAB_EXE or add MATLAB to PATH."
    )


def launch_ahk(
    script_path: Path,
    mot_path: Path,
    start_time: float,
    end_time: float,
    silent: bool,
) -> subprocess.Popen[str] | None:
    command = [str(script_path)]
    if silent:
        command.append("--silent")
    command.extend(
        [
            str(mot_path),
            format_time_value(start_time),
            format_time_value(end_time),
        ]
    )

    ahk_candidates = [
        shutil.which("AutoHotkey64.exe"),
        shutil.which("AutoHotkey.exe"),
        Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"),
        Path(r"C:\Program Files\AutoHotkey\AutoHotkey64.exe"),
        Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey.exe"),
        Path(r"C:\Program Files\AutoHotkey\AutoHotkey.exe"),
    ]

    for candidate in ahk_candidates:
        if not candidate:
            continue

        candidate_path = Path(candidate)
        if candidate_path.exists():
            return subprocess.Popen(
                [str(candidate_path), *command],
                cwd=script_path.parent,
            )

    associated_command = get_registered_ahk_command()
    if associated_command:
        return subprocess.Popen(
            [*associated_command, *command],
            cwd=script_path.parent,
        )

    raise FileNotFoundError(
        "Could not locate an AutoHotkey executable or launcher command for batch execution."
    )


def wait_for_optional_process(
    process: subprocess.Popen[str] | None,
    timeout_sec: float,
) -> int | None:
    if process is None:
        return None

    try:
        return process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None


def build_matlab_command(opengrf_dir: Path, mot_id: str) -> str:
    matlab_dir = matlab_escape(opengrf_dir)
    matlab_log = matlab_escape(MATLAB_LOG_PATH)
    safe_mot_id = mot_id.replace("'", "''")
    return (
        "try, "
        "diary off; "
        f"diary('{matlab_log}'); diary on; "
        f"cd('{matlab_dir}'); "
        "drawnow; pause(2); "
        f"disp('Launching Main_OpenGRF_v2 for {safe_mot_id}...'); "
        "Main_OpenGRF_v2; "
        f"disp('Finished Main_OpenGRF_v2 for {safe_mot_id}.'); "
        "diary off; "
        "exit(0); "
        "catch ME, "
        "disp(getReport(ME, 'extended')); "
        "diary off; "
        "exit(1); "
        "end"
    )


def summarize_last_matlab_failure(mot_id: str) -> str:
    if not MATLAB_LOG_PATH.exists():
        return "MATLAB log file was not found."

    lines = MATLAB_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    marker = f"Launching Main_OpenGRF_v2 for {mot_id}..."
    start_index = -1

    for index, line in enumerate(lines):
        if marker in line:
            start_index = index

    relevant_lines = lines[start_index:] if start_index >= 0 else lines
    non_empty = [line.strip() for line in relevant_lines if line.strip()]
    if not non_empty:
        return "MATLAB log did not contain any readable error details."

    tail = non_empty[-8:]
    return "Last MATLAB log lines:\n" + "\n".join(tail)


def wait_for_matlab_outcome(
    process: subprocess.Popen[str],
    mot_id: str,
    log_offset: int,
    timeout_sec: float,
) -> int:
    deadline = time.time() + timeout_sec
    success_marker = "Analysis successfully completed"
    failure_markers = [
        "Error using ",
        "Error in Main_OpenGRF_v2",
        "Java exception occurred:",
        "Output argument ",
        "Failed to write to the Xml file",
    ]
    print("    Waiting for MATLAB log confirmation of completed analysis...")

    while time.time() < deadline:
        log_text = read_log_from_offset(MATLAB_LOG_PATH, log_offset)
        if success_marker in log_text:
            return 0

        for marker in failure_markers:
            if marker in log_text:
                raise RuntimeError(
                    f"MATLAB analysis did not complete successfully for {mot_id}. "
                    f"See {MATLAB_LOG_PATH}.\n{summarize_log_excerpt(log_text)}"
                )

        return_code = process.poll()
        if return_code is not None and return_code != 0:
            final_log_text = read_log_from_offset(MATLAB_LOG_PATH, log_offset)
            raise RuntimeError(
                f"MATLAB exited with code {return_code} while processing {mot_id}. "
                f"See {MATLAB_LOG_PATH}.\n{summarize_log_excerpt(final_log_text)}"
            )

        time.sleep(1.0)

    raise RuntimeError(
        f"Timed out waiting for MATLAB analysis completion for {mot_id}. "
        f"See {MATLAB_LOG_PATH}.\n{summarize_log_excerpt(read_log_from_offset(MATLAB_LOG_PATH, log_offset))}"
    )


def terminate_matlab_session(process: subprocess.Popen[str] | None) -> None:
    send_ctrl_c_to_matlab(process)
    time.sleep(2.0)

    if process is not None and process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, ValueError, OSError):
            pass
        time.sleep(1.0)

    kill_process_by_pid(process)
    kill_process_by_image("MATLAB.exe")
    kill_process_by_image("MATLABWindow.exe")
    wait_for_no_matlab_processes(timeout_sec=20)


def send_ctrl_c_to_matlab(process: subprocess.Popen[str] | None) -> None:
    target_pid = str(process.pid) if process is not None else ""
    powershell_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$pidHint = '{target_pid}'; "
        "if ($ws.AppActivate('MATLAB')) { "
        "Start-Sleep -Milliseconds 300; "
        "$ws.SendKeys('^c'); "
        "Start-Sleep -Milliseconds 800; "
        "$ws.SendKeys('^c'); "
        "}"
    )

    run_background_command(["powershell", "-NoProfile", "-Command", powershell_script])


def kill_process_by_pid(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    run_background_command(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"]
    )

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def kill_process_by_image(image_name: str) -> None:
    run_background_command(
        ["taskkill", "/IM", image_name, "/T", "/F"]
    )


def wait_for_no_matlab_processes(timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not matlab_processes_running():
            return
        time.sleep(0.5)


def matlab_processes_running() -> bool:
    for image_name in ["MATLAB.exe", "MATLABWindow.exe"]:
        result = run_background_command(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if image_name.lower() in output.lower():
            return True
    return False


def run_background_command(
    command: list[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
    )


def get_process_group_flag() -> int:
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def get_log_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size


def read_log_from_offset(path: Path, offset: int) -> str:
    if not path.exists():
        return ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        return handle.read()


def summarize_log_excerpt(log_text: str) -> str:
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    if not lines:
        return "No new MATLAB log lines were captured."
    tail = lines[-10:]
    return "Recent MATLAB log lines:\n" + "\n".join(tail)


def get_registered_ahk_command() -> list[str] | None:
    if winreg is None:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            r"AutoHotkeyScript\Shell\Open\Command",
        ) as key:
            command, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None

    tokens = [strip_wrapping_quotes(token) for token in shlex.split(command, posix=False)]
    usable_tokens: list[str] = []

    for token in tokens:
        if token in {"%1", "%L", "%*"}:
            continue
        usable_tokens.append(token)

    if not usable_tokens:
        return None

    exe_path = Path(usable_tokens[0])
    if not exe_path.exists():
        return None

    for extra_token in usable_tokens[1:]:
        if extra_token.endswith(".ahk") and not Path(extra_token).exists():
            return None

    return usable_tokens


def strip_wrapping_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def extract_time_range(mot_path: Path) -> tuple[float, float]:
    first_time: float | None = None
    last_time: float | None = None
    in_data_block = False

    with mot_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line == "":
                continue

            if not in_data_block:
                if line.lower() == "endheader":
                    in_data_block = True
                continue

            first_token = line.split()[0]
            try:
                current_time = float(first_token)
            except ValueError:
                continue

            if first_time is None:
                first_time = current_time
            last_time = current_time

    if first_time is None or last_time is None:
        raise ValueError(f"Could not extract time range from .mot file: {mot_path}")

    return first_time, last_time


def format_time_value(value: float) -> str:
    formatted = f"{value:.8f}".rstrip("0").rstrip(".")
    return formatted if formatted != "" else "0"


def resolve_path(raw_value: Any, default: Path) -> Path:
    if raw_value is None or str(raw_value).strip() == "":
        return default.resolve()

    candidate = Path(str(raw_value))
    if not candidate.is_absolute():
        candidate = (SCRIPT_DIR / candidate).resolve()

    return candidate


def matlab_escape(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def abbreviate_mot_id(mot_id: str) -> str:
    tokens = [token for token in mot_id.split("_") if token]
    parts: list[str] = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        letters = "".join(char for char in token if char.isalpha())
        digits = "".join(char for char in token if char.isdigit())

        if letters:
            part = letters[0].upper()
            if digits:
                part += digits
            elif index + 1 < len(tokens) and tokens[index + 1].isdigit():
                part += tokens[index + 1]
                index += 1
            parts.append(part)
        elif digits:
            parts.append(digits)

        index += 1

    abbreviated = "".join(parts)
    if abbreviated == "":
        raise ValueError(f"Could not abbreviate mot id: {mot_id}")

    return abbreviated


def append_launcher_log(message: str) -> None:
    with MATLAB_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message)


def clear_unsuccessful_analyses_file() -> None:
    if UNSUCCESSFUL_ANALYSES_PATH.exists():
        UNSUCCESSFUL_ANALYSES_PATH.unlink()


def record_unsuccessful_analysis(
    mot_id: str,
    reason: str,
    unsuccessful_jobs: list[tuple[str, str]],
) -> None:
    cleaned_reason = reason.replace("\r", " ").replace("\n", " | ").strip()
    unsuccessful_jobs.append((mot_id, cleaned_reason))
    with UNSUCCESSFUL_ANALYSES_PATH.open("w", encoding="utf-8") as handle:
        for job_id, job_reason in unsuccessful_jobs:
            handle.write(f"{job_id}: {job_reason}\n")


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Launcher failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
