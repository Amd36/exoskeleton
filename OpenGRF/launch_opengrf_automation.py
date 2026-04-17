from __future__ import annotations

import argparse
import json
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only at runtime
    winreg = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_METADATA_PATH = SCRIPT_DIR / "opengrf_metadata.json"
DEFAULT_AHK_SCRIPT_PATH = SCRIPT_DIR / "run_opengrf_from_json.ahk"
UNSUCCESSFUL_ANALYSES_PATH = SCRIPT_DIR / "unsuccessful_analyses.txt"
RUN_MANIFEST_PATH = SCRIPT_DIR / "opengrf_run_manifest.json"

DEFAULT_OPENGRF_DIR = Path(r"C:\Users\Junayed\Documents\OpenSim\OpenGRF_v2")
DEFAULT_MATLAB_EXE = Path(r"G:\Matlab_R2025a\bin\matlab.exe")
DEFAULT_KINEMATICS_DIR = (SCRIPT_DIR / ".." / "OpenSimData" / "Kinematics").resolve()
DEFAULT_MODEL_DIR = (SCRIPT_DIR / ".." / "OpenSimData" / "Model").resolve()

DEFAULT_WATCH_TIMEOUT_SEC = 120
DEFAULT_POST_YES_WAIT_MS = 2000
DEFAULT_PENETRATION = 20
DEFAULT_ANALYSIS_TIMEOUT_SEC = 3600
DEFAULT_MAX_PARALLEL_SESSIONS = 3
DEFAULT_STARTUP_STAGGER_SEC = 3.0
DEFAULT_RETRY_COUNT = 1
DEFAULT_MATLAB_EXTRA_ARGS = ["-singleCompThread"]
DEFAULT_RUNTIME_ROOT = (SCRIPT_DIR / "runtime").resolve()


@dataclass(frozen=True)
class MotJob:
    mot_id: str
    mot_path: Path
    base_dir: Path
    abbreviation: str
    start_time: float
    end_time: float


@dataclass(frozen=True)
class JobTargets:
    model_target: Path
    solution_target: Path


@dataclass(frozen=True)
class WorkerRuntime:
    worker_id: str
    slot_index: int
    root_dir: Path
    opengrf_dir: Path
    logs_dir: Path
    job_stage_root: Path
    automation_script_path: Path


@dataclass(frozen=True)
class StagedJob:
    job: MotJob
    worker: WorkerRuntime
    attempt: int
    stage_root: Path
    model_dir: Path
    kinematics_dir: Path
    staged_osim_path: Path
    staged_mot_path: Path
    metadata_path: Path
    ready_flag_path: Path
    ahk_log_path: Path
    matlab_log_path: Path
    targets: JobTargets


@dataclass(frozen=True)
class LauncherConfig:
    metadata_path: Path
    source_opengrf_dir: Path
    source_ahk_script: Path
    matlab_exe: Path
    source_osim_path: Path
    runtime_root: Path
    claim_root: Path
    manifest_path: Path
    max_parallel_sessions: int
    analysis_timeout_sec: float
    startup_stagger_sec: float
    retry_count: int
    matlab_extra_args: list[str]
    penetration: float
    estimate_frequency_content: bool
    watch_timeout_sec: int
    post_yes_wait_ms: int


class StartupCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, worker_id: str, job_id: str) -> None:
        print(f"[{worker_id}] Waiting for startup slot for {job_id}")
        self._lock.acquire()
        print(f"[{worker_id}] Startup slot acquired for {job_id}")

    def release(self, worker_id: str, job_id: str) -> None:
        self._lock.release()
        print(f"[{worker_id}] Startup slot released for {job_id}")


class FailureRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: list[tuple[str, str]] = []

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            if self.path.exists():
                self.path.unlink()

    def record(self, mot_id: str, reason: str) -> None:
        cleaned_reason = reason.replace("\r", " ").replace("\n", " | ").strip()
        with self._lock:
            self._entries.append((mot_id, cleaned_reason))
            with self.path.open("w", encoding="utf-8") as handle:
                for job_id, job_reason in self._entries:
                    handle.write(f"{job_id}: {job_reason}\n")

    def has_failures(self) -> bool:
        with self._lock:
            return bool(self._entries)

    def replace_all(self, entries: list[tuple[str, str]]) -> None:
        with self._lock:
            self._entries = list(entries)
            if not self._entries:
                if self.path.exists():
                    self.path.unlink()
                return

            with self.path.open("w", encoding="utf-8") as handle:
                for job_id, job_reason in self._entries:
                    handle.write(f"{job_id}: {job_reason}\n")


class ManifestTracker:
    def __init__(self, path: Path, config: LauncherConfig, jobs: list[MotJob]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = {
            "generated_at": iso_timestamp(),
            "updated_at": iso_timestamp(),
            "metadata_path": str(config.metadata_path),
            "runtime_root": str(config.runtime_root),
            "source_opengrf_dir": str(config.source_opengrf_dir),
            "max_parallel_sessions": config.max_parallel_sessions,
            "analysis_timeout_sec": config.analysis_timeout_sec,
            "startup_stagger_sec": config.startup_stagger_sec,
            "retry_count": config.retry_count,
            "matlab_extra_args": config.matlab_extra_args,
            "jobs": [
                {
                    "mot_id": job.mot_id,
                    "abbreviation": job.abbreviation,
                    "status": "discovered",
                    "worker_id": "",
                    "started_at": "",
                    "ended_at": "",
                    "attempt": 0,
                    "final_model_path": "",
                    "final_solution_path": "",
                    "matlab_log_path": "",
                    "ahk_log_path": "",
                    "error": "",
                    "already_present": False,
                }
                for job in jobs
            ],
            "summary": {
                "discovered": len(jobs),
                "queued": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
            },
        }
        self._write_locked()

    def mark_queued(self, job: MotJob, targets: JobTargets) -> None:
        with self._lock:
            entry = self._get_entry_locked(job.mot_id)
            entry.update(
                {
                    "status": "queued",
                    "worker_id": "",
                    "started_at": "",
                    "ended_at": "",
                    "attempt": 0,
                    "final_model_path": str(targets.model_target),
                    "final_solution_path": str(targets.solution_target),
                    "matlab_log_path": "",
                    "ahk_log_path": "",
                    "error": "",
                    "already_present": False,
                }
            )
            self._write_locked()

    def mark_running(self, staged_job: StagedJob) -> None:
        with self._lock:
            entry = self._get_entry_locked(staged_job.job.mot_id)
            entry.update(
                {
                    "status": "running",
                    "worker_id": staged_job.worker.worker_id,
                    "started_at": iso_timestamp(),
                    "ended_at": "",
                    "attempt": staged_job.attempt,
                    "final_model_path": str(staged_job.targets.model_target),
                    "final_solution_path": str(staged_job.targets.solution_target),
                    "matlab_log_path": str(staged_job.matlab_log_path),
                    "ahk_log_path": str(staged_job.ahk_log_path),
                    "error": "",
                    "already_present": False,
                }
            )
            self._write_locked()

    def mark_succeeded(
        self,
        job: MotJob,
        targets: JobTargets,
        *,
        worker_id: str = "",
        attempt: int = 0,
        matlab_log_path: Path | None = None,
        ahk_log_path: Path | None = None,
        already_present: bool = False,
    ) -> None:
        with self._lock:
            entry = self._get_entry_locked(job.mot_id)
            started_at = entry.get("started_at") or iso_timestamp()
            entry.update(
                {
                    "status": "succeeded",
                    "worker_id": worker_id,
                    "started_at": started_at,
                    "ended_at": iso_timestamp(),
                    "attempt": attempt,
                    "final_model_path": str(targets.model_target),
                    "final_solution_path": str(targets.solution_target),
                    "matlab_log_path": str(matlab_log_path) if matlab_log_path else entry.get("matlab_log_path", ""),
                    "ahk_log_path": str(ahk_log_path) if ahk_log_path else entry.get("ahk_log_path", ""),
                    "error": "",
                    "already_present": already_present,
                }
            )
            self._write_locked()

    def mark_failed(
        self,
        job: MotJob,
        targets: JobTargets,
        *,
        worker_id: str,
        attempt: int,
        error: str,
        matlab_log_path: Path | None = None,
        ahk_log_path: Path | None = None,
    ) -> None:
        with self._lock:
            entry = self._get_entry_locked(job.mot_id)
            started_at = entry.get("started_at") or iso_timestamp()
            entry.update(
                {
                    "status": "failed",
                    "worker_id": worker_id,
                    "started_at": started_at,
                    "ended_at": iso_timestamp(),
                    "attempt": attempt,
                    "final_model_path": str(targets.model_target),
                    "final_solution_path": str(targets.solution_target),
                    "matlab_log_path": str(matlab_log_path) if matlab_log_path else entry.get("matlab_log_path", ""),
                    "ahk_log_path": str(ahk_log_path) if ahk_log_path else entry.get("ahk_log_path", ""),
                    "error": error,
                    "already_present": False,
                }
            )
            self._write_locked()

    def write_run_summary(self) -> None:
        with self._lock:
            self._write_locked()

    def get_entry_copy(self, mot_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._get_entry_locked(mot_id))

    def list_entries_copy(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._data["jobs"]]

    def _get_entry_locked(self, mot_id: str) -> dict[str, Any]:
        for entry in self._data["jobs"]:
            if entry["mot_id"] == mot_id:
                return entry
        raise KeyError(f"Manifest entry not found for {mot_id}")

    def _write_locked(self) -> None:
        summary = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
        for entry in self._data["jobs"]:
            status = entry["status"]
            if status in summary:
                summary[status] += 1

        self._data["updated_at"] = iso_timestamp()
        self._data["summary"] = {
            "discovered": len(self._data["jobs"]),
            **summary,
        }

        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)
            handle.write("\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch OpenGRF in isolated parallel MATLAB workers.")
    parser.add_argument(
        "--metadata-path",
        default=str(DEFAULT_METADATA_PATH),
        help="Path to opengrf_metadata.json",
    )
    parser.add_argument("--max-parallel-sessions", type=int, help="Maximum concurrent MATLAB sessions.")
    parser.add_argument("--worker-runtime-root", help="Root directory for worker sandboxes.")
    parser.add_argument("--analysis-timeout-sec", type=float, help="Timeout per job.")
    parser.add_argument("--startup-stagger-sec", type=float, help="Delay between worker start-ups.")
    parser.add_argument("--retry-count", type=int, help="Retry count for failed jobs.")
    parser.add_argument(
        "--matlab-extra-arg",
        action="append",
        help="Extra MATLAB argument to append. Repeat for multiple values.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    metadata_path = resolve_path(args.metadata_path, DEFAULT_METADATA_PATH)
    metadata = ensure_metadata(metadata_path)
    config = build_launcher_config(metadata_path, metadata, args)
    jobs = discover_jobs(metadata)

    validate_environment(config, jobs)

    print(f"MATLAB executable: {config.matlab_exe}")
    print(f"OpenGRF source folder: {config.source_opengrf_dir}")
    print(f"Worker runtime root: {config.runtime_root}")
    print(f"Run manifest: {config.manifest_path}")

    failure_registry = FailureRegistry(UNSUCCESSFUL_ANALYSES_PATH)
    failure_registry.clear()
    manifest = ManifestTracker(config.manifest_path, config, jobs)

    workers = prepare_worker_runtimes(config)
    queued_jobs: Queue[MotJob] = Queue()
    startup_coordinator = StartupCoordinator()

    for job in jobs:
        targets = get_target_paths(config.source_osim_path, job)
        if targets.model_target.exists() != targets.solution_target.exists():
            reason = (
                f"Only one archived output exists for {job.mot_id}: "
                f"{targets.model_target} / {targets.solution_target}"
            )
            manifest.mark_failed(
                job,
                targets,
                worker_id="precheck",
                attempt=0,
                error=reason,
            )
            failure_registry.record(job.mot_id, reason)
            print(f"[skip] {job.mot_id}: {reason}")
            continue

        if targets.model_target.exists() and targets.solution_target.exists():
            manifest.mark_succeeded(job, targets, already_present=True)
            print(f"[skip] {job.mot_id}: archived outputs already exist.")
            continue

        manifest.mark_queued(job, targets)
        queued_jobs.put(job)

    if queued_jobs.empty():
        manifest.write_run_summary()
        if failure_registry.has_failures():
            print(f"Completed with some failures. See {UNSUCCESSFUL_ANALYSES_PATH}")
            return 1
        print("All requested OpenGRF analyses are already complete.")
        return 0

    worker_count = min(config.max_parallel_sessions, queued_jobs.qsize(), len(workers))
    threads: list[threading.Thread] = []
    for worker in workers[:worker_count]:
        thread = threading.Thread(
            target=worker_loop,
            args=(worker, config, manifest, failure_registry, queued_jobs, startup_coordinator),
            name=worker.worker_id,
            daemon=False,
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    reconciled_jobs = reconcile_archived_outputs(
        manifest,
        failure_registry,
        jobs,
        config.source_osim_path,
    )
    if reconciled_jobs:
        joined_jobs = ", ".join(reconciled_jobs)
        print(f"Reconciled archived outputs as successful: {joined_jobs}")

    manifest.write_run_summary()
    if failure_registry.has_failures():
        print(f"Completed with some failures. See {UNSUCCESSFUL_ANALYSES_PATH}")
        return 1

    print("All requested OpenGRF analyses are complete.")
    return 0


def build_launcher_config(
    metadata_path: Path,
    metadata: dict[str, Any],
    args: argparse.Namespace,
) -> LauncherConfig:
    source_opengrf_dir = resolve_path(metadata.get("opengrf_folder"), DEFAULT_OPENGRF_DIR)
    source_osim_value = metadata.get("osim")
    if source_osim_value is None or str(source_osim_value).strip() == "":
        source_osim_path = choose_source_osim(DEFAULT_MODEL_DIR)
    else:
        source_osim_path = resolve_path(source_osim_value, DEFAULT_MODEL_DIR / "placeholder.osim")
    runtime_root = resolve_path(
        args.worker_runtime_root or metadata.get("worker_runtime_root"),
        DEFAULT_RUNTIME_ROOT,
    )
    max_parallel_sessions = int(
        pick_setting(
            args.max_parallel_sessions,
            metadata.get("max_parallel_sessions"),
            DEFAULT_MAX_PARALLEL_SESSIONS,
        )
    )
    analysis_timeout_sec = float(
        pick_setting(
            args.analysis_timeout_sec,
            metadata.get("analysis_timeout_sec"),
            DEFAULT_ANALYSIS_TIMEOUT_SEC,
        )
    )
    startup_stagger_sec = float(
        pick_setting(
            args.startup_stagger_sec,
            metadata.get("startup_stagger_sec"),
            DEFAULT_STARTUP_STAGGER_SEC,
        )
    )
    retry_count = int(pick_setting(args.retry_count, metadata.get("retry_count"), DEFAULT_RETRY_COUNT))
    matlab_extra_args = normalize_matlab_extra_args(
        args.matlab_extra_arg,
        metadata.get("matlab_extra_args"),
        DEFAULT_MATLAB_EXTRA_ARGS,
    )

    if max_parallel_sessions < 1:
        raise ValueError("max_parallel_sessions must be at least 1")
    if analysis_timeout_sec <= 0:
        raise ValueError("analysis_timeout_sec must be positive")
    if startup_stagger_sec < 0:
        raise ValueError("startup_stagger_sec cannot be negative")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative")

    return LauncherConfig(
        metadata_path=metadata_path,
        source_opengrf_dir=source_opengrf_dir,
        source_ahk_script=DEFAULT_AHK_SCRIPT_PATH,
        matlab_exe=find_matlab_exe(),
        source_osim_path=source_osim_path,
        runtime_root=runtime_root,
        claim_root=runtime_root / "_window_claims",
        manifest_path=RUN_MANIFEST_PATH,
        max_parallel_sessions=max_parallel_sessions,
        analysis_timeout_sec=analysis_timeout_sec,
        startup_stagger_sec=startup_stagger_sec,
        retry_count=retry_count,
        matlab_extra_args=matlab_extra_args,
        penetration=float(metadata.get("penetration", DEFAULT_PENETRATION)),
        estimate_frequency_content=bool(metadata.get("estimate_frequency_content", True)),
        watch_timeout_sec=max(int(metadata.get("watch_timeout_sec", DEFAULT_WATCH_TIMEOUT_SEC)), 10),
        post_yes_wait_ms=max(int(metadata.get("post_yes_wait_ms", DEFAULT_POST_YES_WAIT_MS)), 500),
    )


def pick_setting(cli_value: Any, metadata_value: Any, default_value: Any) -> Any:
    if cli_value is not None:
        return cli_value
    if metadata_value is not None:
        return metadata_value
    return default_value


def normalize_matlab_extra_args(
    cli_args: list[str] | None,
    metadata_value: Any,
    default_value: list[str],
) -> list[str]:
    if cli_args:
        return [str(value) for value in cli_args if str(value).strip()]

    if isinstance(metadata_value, list):
        cleaned = [str(value) for value in metadata_value if str(value).strip()]
        if cleaned:
            return cleaned

    return list(default_value)


def validate_environment(config: LauncherConfig, jobs: list[MotJob]) -> None:
    if not config.source_opengrf_dir.exists():
        raise FileNotFoundError(f"OpenGRF source folder not found: {config.source_opengrf_dir}")
    if not config.source_ahk_script.exists():
        raise FileNotFoundError(f"AHK script not found: {config.source_ahk_script}")
    if not config.source_osim_path.exists():
        raise FileNotFoundError(f"Source .osim file not found: {config.source_osim_path}")
    if not jobs:
        raise RuntimeError("No .mot files were found to process.")
    if config.claim_root.exists():
        remove_path_with_retries(config.claim_root, is_dir=True)
    config.claim_root.mkdir(parents=True, exist_ok=True)


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
        "max_parallel_sessions": DEFAULT_MAX_PARALLEL_SESSIONS,
        "worker_runtime_root": "./runtime",
        "analysis_timeout_sec": DEFAULT_ANALYSIS_TIMEOUT_SEC,
        "startup_stagger_sec": DEFAULT_STARTUP_STAGGER_SEC,
        "retry_count": DEFAULT_RETRY_COUNT,
        "matlab_extra_args": DEFAULT_MATLAB_EXTRA_ARGS,
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


def discover_jobs(metadata: dict[str, Any]) -> list[MotJob]:
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


def prepare_worker_runtimes(config: LauncherConfig) -> list[WorkerRuntime]:
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    workers: list[WorkerRuntime] = []
    for slot_index in range(1, config.max_parallel_sessions + 1):
        worker_id = f"worker_{slot_index:02d}"
        root_dir = config.runtime_root / worker_id
        opengrf_dir = root_dir / "OpenGRF_v2"
        logs_dir = root_dir / "logs"
        job_stage_root = root_dir / "job_stage"
        automation_script_path = root_dir / "run_opengrf_from_json.ahk"

        if root_dir.exists():
            remove_path_with_retries(root_dir, is_dir=True)

        logs_dir.mkdir(parents=True, exist_ok=True)
        job_stage_root.mkdir(parents=True, exist_ok=True)
        reset_worker_runtime_paths(config, opengrf_dir, automation_script_path)

        workers.append(
            WorkerRuntime(
                worker_id=worker_id,
                slot_index=slot_index,
                root_dir=root_dir,
                opengrf_dir=opengrf_dir,
                logs_dir=logs_dir,
                job_stage_root=job_stage_root,
                automation_script_path=automation_script_path,
            )
        )

    return workers


def reset_worker_runtime(config: LauncherConfig, worker: WorkerRuntime) -> None:
    refresh_worker_runtime_paths(config, worker.opengrf_dir, worker.automation_script_path)


def reset_worker_runtime_paths(
    config: LauncherConfig,
    opengrf_dir: Path,
    automation_script_path: Path,
) -> None:
    if opengrf_dir.exists():
        remove_path_with_retries(opengrf_dir, is_dir=True)
    if automation_script_path.exists():
        remove_path_with_retries(automation_script_path, is_dir=False)

    shutil.copytree(config.source_opengrf_dir, opengrf_dir)
    shutil.copy2(config.source_ahk_script, automation_script_path)


def refresh_worker_runtime_paths(
    config: LauncherConfig,
    opengrf_dir: Path,
    automation_script_path: Path,
) -> None:
    opengrf_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.source_opengrf_dir, opengrf_dir, dirs_exist_ok=True)
    shutil.copy2(config.source_ahk_script, automation_script_path)


def worker_loop(
    worker: WorkerRuntime,
    config: LauncherConfig,
    manifest: ManifestTracker,
    failure_registry: FailureRegistry,
    queued_jobs: Queue[MotJob],
    startup_coordinator: StartupCoordinator,
) -> None:
    if worker.slot_index > 1 and config.startup_stagger_sec > 0:
        time.sleep(config.startup_stagger_sec * (worker.slot_index - 1))

    while True:
        try:
            job = queued_jobs.get_nowait()
        except Empty:
            return

        try:
            run_job_with_retries(worker, config, manifest, failure_registry, job, startup_coordinator)
        except Exception as exc:
            failure_reason = f"Unhandled worker error: {exc}"
            manifest.mark_failed(
                job,
                get_target_paths(config.source_osim_path, job),
                worker_id=worker.worker_id,
                attempt=0,
                error=failure_reason,
            )
            failure_registry.record(job.mot_id, failure_reason)
            print(f"[{worker.worker_id}] Failed {job.mot_id}")
            print(f"    {failure_reason}")
        finally:
            queued_jobs.task_done()


def run_job_with_retries(
    worker: WorkerRuntime,
    config: LauncherConfig,
    manifest: ManifestTracker,
    failure_registry: FailureRegistry,
    job: MotJob,
    startup_coordinator: StartupCoordinator,
) -> None:
    targets = get_target_paths(config.source_osim_path, job)
    attempts = config.retry_count + 1
    for attempt in range(1, attempts + 1):
        staged_job: StagedJob | None = None
        try:
            print(f"[{worker.worker_id}] Starting {job.mot_id} (attempt {attempt}/{attempts})")
            staged_job = prepare_job_stage(config, worker, job, attempt)
            manifest.mark_running(staged_job)

            run_started_at = run_worker_job(config, staged_job, startup_coordinator)
            archive_job_outputs(staged_job, run_started_at)
            cleanup_warning = ""
            try:
                cleanup_completed_job_stage(staged_job)
            except OSError as exc:
                cleanup_warning = f"could not remove completed stage {staged_job.stage_root}: {exc}"
            manifest.mark_succeeded(
                job,
                staged_job.targets,
                worker_id=worker.worker_id,
                attempt=attempt,
                matlab_log_path=staged_job.matlab_log_path,
                ahk_log_path=staged_job.ahk_log_path,
            )
            if cleanup_warning:
                print(f"[{worker.worker_id}] Cleanup warning for {job.mot_id}")
                print(f"    {cleanup_warning}")
            print(f"[{worker.worker_id}] Finished {job.mot_id}")
            return
        except Exception as exc:
            failure_reason = str(exc)
            cleanup_warning = ""
            if staged_job is not None:
                cleanup_warning = cleanup_worker_state(staged_job)
                if archived_outputs_exist(staged_job.targets):
                    manifest.mark_succeeded(
                        job,
                        staged_job.targets,
                        worker_id=worker.worker_id,
                        attempt=attempt,
                        matlab_log_path=staged_job.matlab_log_path,
                        ahk_log_path=staged_job.ahk_log_path,
                    )
                    if cleanup_warning:
                        print(f"[{worker.worker_id}] Cleanup warning for {job.mot_id}")
                        print(f"    {cleanup_warning}")
                    print(
                        f"[{worker.worker_id}] Finished {job.mot_id} "
                        "(archived outputs detected after launcher warning)"
                    )
                    return
            if cleanup_warning:
                failure_reason += " | Cleanup warning: " + cleanup_warning

            if attempt < attempts and should_retry_failure(failure_reason):
                print(f"[{worker.worker_id}] Retrying {job.mot_id}: {failure_reason}")
                try:
                    reset_worker_runtime(config, worker)
                except Exception as reset_exc:
                    print(
                        f"[{worker.worker_id}] Worker runtime reset failed for {job.mot_id}; "
                        "keeping the existing sandbox for retry."
                    )
                    print(f"    {reset_exc}")
                continue

            manifest.mark_failed(
                job,
                targets,
                worker_id=worker.worker_id,
                attempt=attempt,
                error=failure_reason,
                matlab_log_path=staged_job.matlab_log_path if staged_job else None,
                ahk_log_path=staged_job.ahk_log_path if staged_job else None,
            )
            failure_registry.record(job.mot_id, failure_reason)
            print(f"[{worker.worker_id}] Failed {job.mot_id}")
            print(f"    {failure_reason}")
            return


def should_retry_failure(failure_reason: str) -> bool:
    text = failure_reason.lower()

    non_retry_markers = [
        "matlab analysis did not complete successfully",
        "matlab exited with code",
        "error in main_opengrf_v2",
        "error in load_sto",
        "error in load_mot",
        "error in bkanalysis",
        "index exceeds array bounds",
        "java exception occurred",
    ]
    for marker in non_retry_markers:
        if marker in text:
            return False

    retry_markers = [
        "autohotkey exited with code",
        "autohotkey did not initialize cleanly",
        "autohotkey did not finish within",
        "timed out waiting for 'choose",
        "timed out waiting for 'input data'",
        "failed to find the frequency popup",
        "failed to close file dialog",
        "readytimeout",
    ]
    return any(marker in text for marker in retry_markers)


def prepare_job_stage(
    config: LauncherConfig,
    worker: WorkerRuntime,
    job: MotJob,
    attempt: int,
) -> StagedJob:
    stage_root = worker.job_stage_root / job.mot_id / f"attempt_{attempt:02d}"
    if stage_root.exists():
        remove_path_with_retries(stage_root, is_dir=True)

    model_dir = stage_root / "Model"
    kinematics_dir = stage_root / "Kinematics"
    model_dir.mkdir(parents=True, exist_ok=True)
    kinematics_dir.mkdir(parents=True, exist_ok=True)

    staged_osim_path = model_dir / config.source_osim_path.name
    staged_mot_path = kinematics_dir / job.mot_path.name
    shutil.copy2(config.source_osim_path, staged_osim_path)
    shutil.copy2(job.mot_path, staged_mot_path)

    metadata_path = stage_root / "opengrf_metadata.json"
    ready_flag_path = stage_root / "matlab_ready.flag"
    attempt_suffix = f"{job.mot_id}__attempt_{attempt:02d}"
    ahk_log_path = worker.logs_dir / f"{attempt_suffix}.ahk.log"
    matlab_log_path = worker.logs_dir / f"{attempt_suffix}.matlab.log"

    if ready_flag_path.exists():
        ready_flag_path.unlink()
    for log_path in [ahk_log_path, matlab_log_path]:
        if log_path.exists():
            log_path.unlink()

    metadata = {
        "opengrf_folder": str(worker.opengrf_dir).replace("\\", "/"),
        "osim": str(staged_osim_path).replace("\\", "/"),
        "mot": str(staged_mot_path).replace("\\", "/"),
        "start_time": job.start_time,
        "end_time": job.end_time,
        "penetration": config.penetration,
        "estimate_frequency_content": config.estimate_frequency_content,
        "watch_timeout_sec": config.watch_timeout_sec,
        "post_yes_wait_ms": config.post_yes_wait_ms,
    }
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)
        handle.write("\n")

    return StagedJob(
        job=job,
        worker=worker,
        attempt=attempt,
        stage_root=stage_root,
        model_dir=model_dir,
        kinematics_dir=kinematics_dir,
        staged_osim_path=staged_osim_path,
        staged_mot_path=staged_mot_path,
        metadata_path=metadata_path,
        ready_flag_path=ready_flag_path,
        ahk_log_path=ahk_log_path,
        matlab_log_path=matlab_log_path,
        targets=get_target_paths(config.source_osim_path, job),
    )


def run_worker_job(
    config: LauncherConfig,
    staged_job: StagedJob,
    startup_coordinator: StartupCoordinator,
) -> float:
    matlab_process: subprocess.Popen[str] | None = None
    ahk_process: subprocess.Popen[str] | None = None
    run_started_at = time.time()
    startup_slot_acquired = False

    try:
        startup_coordinator.acquire(staged_job.worker.worker_id, staged_job.job.mot_id)
        startup_slot_acquired = True

        matlab_command = build_matlab_command(
            staged_job.worker.opengrf_dir,
            staged_job.job.mot_id,
            staged_job.matlab_log_path,
            staged_job.ready_flag_path,
        )
        command = [
            str(config.matlab_exe),
            "-wait",
            *config.matlab_extra_args,
            "-sd",
            str(staged_job.worker.opengrf_dir),
            "-r",
            matlab_command,
        ]
        matlab_process = subprocess.Popen(
            command,
            cwd=staged_job.worker.opengrf_dir,
            creationflags=get_process_group_flag(),
        )

        ahk_process = launch_ahk(
            staged_job.worker.automation_script_path,
            staged_job.metadata_path,
            staged_job.ahk_log_path,
            claim_root=config.claim_root,
            matlab_pid=matlab_process.pid,
            worker_id=staged_job.worker.worker_id,
            silent=True,
        )
        wait_for_ahk_startup(staged_job.ahk_log_path, run_started_at, ahk_process)
        staged_job.ready_flag_path.touch()

        ahk_completion_timeout = max(
            float(config.watch_timeout_sec) + (config.post_yes_wait_ms / 1000.0) + 15.0,
            45.0,
        )
        ahk_return_code = wait_for_required_process(
            ahk_process,
            timeout_sec=ahk_completion_timeout,
            process_name="AutoHotkey",
        )
        startup_coordinator.release(staged_job.worker.worker_id, staged_job.job.mot_id)
        startup_slot_acquired = False

        if ahk_return_code != 0:
            raise RuntimeError(
                f"AutoHotkey exited with code {ahk_return_code} while processing {staged_job.job.mot_id}. "
                f"See {staged_job.ahk_log_path}."
            )

        matlab_return_code = wait_for_matlab_outcome(
            matlab_process,
            staged_job.job.mot_id,
            staged_job.matlab_log_path,
            log_offset=0,
            timeout_sec=config.analysis_timeout_sec,
        )
        if matlab_return_code != 0:
            raise RuntimeError(
                f"MATLAB exited with code {matlab_return_code} while processing {staged_job.job.mot_id}. "
                f"See {staged_job.matlab_log_path}.\n"
                f"{summarize_last_matlab_failure(staged_job.matlab_log_path, staged_job.job.mot_id)}"
            )

        return run_started_at
    except Exception:
        terminate_process_tree(ahk_process)
        terminate_matlab_session(matlab_process)
        raise
    finally:
        if startup_slot_acquired:
            startup_coordinator.release(staged_job.worker.worker_id, staged_job.job.mot_id)
        if staged_job.ready_flag_path.exists():
            staged_job.ready_flag_path.unlink()


def archive_job_outputs(staged_job: StagedJob, run_started_at: float) -> None:
    model_source = staged_job.model_dir / "ModelProcessed.osim"
    solution_source = staged_job.kinematics_dir / "Solution"
    wait_for_fresh_path(model_source, run_started_at, is_dir=False)
    wait_for_fresh_path(solution_source, run_started_at, is_dir=True)

    if staged_job.targets.model_target.exists():
        raise FileExistsError(f"Target model already exists: {staged_job.targets.model_target}")
    if staged_job.targets.solution_target.exists():
        raise FileExistsError(f"Target solution folder already exists: {staged_job.targets.solution_target}")

    staged_job.targets.model_target.parent.mkdir(parents=True, exist_ok=True)
    staged_job.targets.solution_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(model_source), str(staged_job.targets.model_target))
    shutil.move(str(solution_source), str(staged_job.targets.solution_target))


def cleanup_worker_state(staged_job: StagedJob) -> str:
    issues: list[str] = []
    paths = [
        (staged_job.kinematics_dir / "Solution", True),
        (staged_job.model_dir / "ModelProcessed.osim", False),
        (staged_job.ready_flag_path, False),
    ]

    for path, is_dir in paths:
        try:
            remove_path_with_retries(path, is_dir=is_dir)
        except OSError as exc:
            issues.append(f"could not remove {path}: {exc}")

    return " | ".join(issues)


def cleanup_completed_job_stage(staged_job: StagedJob) -> None:
    if staged_job.stage_root.exists():
        remove_path_with_retries(staged_job.stage_root, is_dir=True)


def get_target_paths(source_osim_path: Path, job: MotJob) -> JobTargets:
    model_dir = source_osim_path.parent
    return JobTargets(
        model_target=model_dir / f"ModelProcessed{job.abbreviation}.osim",
        solution_target=job.base_dir / f"Solution{job.abbreviation}",
    )


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


def archived_outputs_exist(targets: JobTargets) -> bool:
    return targets.model_target.exists() and targets.solution_target.exists()


def reconcile_archived_outputs(
    manifest: ManifestTracker,
    failure_registry: FailureRegistry,
    jobs: list[MotJob],
    source_osim_path: Path,
) -> list[str]:
    reconciled: list[str] = []

    for job in jobs:
        targets = get_target_paths(source_osim_path, job)
        if not archived_outputs_exist(targets):
            continue

        entry = manifest.get_entry_copy(job.mot_id)
        if entry.get("status") == "succeeded":
            continue

        matlab_log_path = Path(entry["matlab_log_path"]) if entry.get("matlab_log_path") else None
        ahk_log_path = Path(entry["ahk_log_path"]) if entry.get("ahk_log_path") else None
        manifest.mark_succeeded(
            job,
            targets,
            worker_id=str(entry.get("worker_id", "")),
            attempt=int(entry.get("attempt") or 0),
            matlab_log_path=matlab_log_path,
            ahk_log_path=ahk_log_path,
            already_present=bool(entry.get("already_present", False)),
        )
        reconciled.append(job.mot_id)

    synced_failures: list[tuple[str, str]] = []
    for entry in manifest.list_entries_copy():
        if entry.get("status") != "failed":
            continue
        synced_failures.append((entry["mot_id"], str(entry.get("error", ""))))
    failure_registry.replace_all(synced_failures)
    return reconciled


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


def wait_for_ahk_startup(
    log_path: Path,
    run_started_at: float,
    process: subprocess.Popen[str] | None,
) -> None:
    deadline = time.time() + 12
    while time.time() < deadline:
        if log_path.exists() and log_path.stat().st_mtime >= run_started_at - 1.0:
            return

        if process is not None:
            return_code = process.poll()
            if return_code is not None and return_code != 0:
                raise RuntimeError(
                    f"AutoHotkey exited before initializing. See {log_path}."
                )
        time.sleep(0.25)

    raise RuntimeError(f"AutoHotkey did not initialize cleanly. Check {log_path}.")


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
    metadata_path: Path,
    log_path: Path,
    *,
    claim_root: Path,
    matlab_pid: int,
    worker_id: str,
    silent: bool,
) -> subprocess.Popen[str] | None:
    command = [str(script_path)]
    if silent:
        command.append("--silent")
    command.extend(
        [
            "--metadata-path",
            str(metadata_path),
            "--log-path",
            str(log_path),
            "--claim-root",
            str(claim_root),
            "--matlab-pid",
            str(matlab_pid),
            "--worker-id",
            worker_id,
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


def wait_for_required_process(
    process: subprocess.Popen[str] | None,
    *,
    timeout_sec: float,
    process_name: str,
) -> int:
    if process is None:
        raise RuntimeError(f"{process_name} process was not started.")

    try:
        return process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{process_name} did not finish within {timeout_sec:.1f} seconds."
        ) from exc


def build_matlab_command(
    opengrf_dir: Path,
    mot_id: str,
    matlab_log_path: Path,
    ready_flag_path: Path,
) -> str:
    matlab_dir = matlab_escape(opengrf_dir)
    matlab_log = matlab_escape(matlab_log_path)
    ready_flag = matlab_escape(ready_flag_path)
    safe_mot_id = mot_id.replace("'", "''")
    return (
        "try, "
        "diary off; "
        f"diary('{matlab_log}'); diary on; "
        f"cd('{matlab_dir}'); "
        f"disp('Waiting for worker-ready signal for {safe_mot_id}...'); "
        "readyDeadline = tic; "
        f"while ~isfile('{ready_flag}'), "
        "pause(0.25); drawnow; "
        "if toc(readyDeadline) > 300, "
        f"error('OpenGRFLauncher:ReadyTimeout', 'Timed out waiting for worker-ready signal for {safe_mot_id}.'); "
        "end; "
        "end; "
        f"delete('{ready_flag}'); "
        "drawnow; pause(0.5); "
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


def summarize_last_matlab_failure(matlab_log_path: Path, mot_id: str) -> str:
    if not matlab_log_path.exists():
        return "MATLAB log file was not found."

    lines = matlab_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
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
    matlab_log_path: Path,
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
        "OpenGRFLauncher:ReadyTimeout",
    ]

    while time.time() < deadline:
        log_text = read_log_from_offset(matlab_log_path, log_offset)
        if success_marker in log_text:
            return 0

        if contains_any_marker(log_text, failure_markers):
            raise RuntimeError(
                f"MATLAB analysis did not complete successfully for {mot_id}. "
                f"See {matlab_log_path}.\n{summarize_log_excerpt(log_text)}"
            )

        return_code = process.poll()
        if return_code is not None:
            final_log_text = read_log_from_offset(matlab_log_path, log_offset)
            if success_marker in final_log_text:
                return 0
            if return_code != 0:
                raise RuntimeError(
                    f"MATLAB exited with code {return_code} while processing {mot_id}. "
                    f"See {matlab_log_path}.\n{summarize_log_excerpt(final_log_text)}"
                )

        time.sleep(1.0)

    final_log_text = read_log_from_offset(matlab_log_path, log_offset)
    if success_marker in final_log_text:
        return 0
    if contains_any_marker(final_log_text, failure_markers):
        raise RuntimeError(
            f"MATLAB analysis did not complete successfully for {mot_id}. "
            f"See {matlab_log_path}.\n{summarize_log_excerpt(final_log_text)}"
        )

    raise RuntimeError(
        f"Timed out waiting for MATLAB analysis completion for {mot_id}. "
        f"See {matlab_log_path}.\n"
        f"{summarize_log_excerpt(final_log_text)}"
    )


def terminate_matlab_session(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return

    send_ctrl_c_to_matlab(process)
    time.sleep(1.0)

    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, ValueError, OSError):
            pass
        time.sleep(1.0)

    terminate_process_tree(process)


def send_ctrl_c_to_matlab(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    powershell_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$pidHint = {process.pid}; "
        "if ($ws.AppActivate($pidHint)) { "
        "Start-Sleep -Milliseconds 300; "
        "$ws.SendKeys('^c'); "
        "Start-Sleep -Milliseconds 800; "
        "$ws.SendKeys('^c'); "
        "}"
    )

    run_background_command(["powershell", "-NoProfile", "-Command", powershell_script])


def terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    run_background_command(["taskkill", "/PID", str(process.pid), "/T", "/F"])

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


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


def read_log_from_offset(path: Path, offset: int) -> str:
    if not path.exists():
        return ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        return handle.read()


def contains_any_marker(text: str, markers: list[str]) -> bool:
    return any(marker in text for marker in markers)


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


def iso_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Launcher failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
