from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import opensim as osim


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = SCRIPT_DIR.parent / "Model"
DEFAULT_KINEMATICS_DIR = SCRIPT_DIR.parent / "Kinematics"
DEFAULT_OUTPUT_FILE = "inverse_dynamics.sto"
DEFAULT_LOWPASS_CUTOFF = 6.0
LOG_FILE_PATH = SCRIPT_DIR / "inverse_dynamics_log"


@dataclass(frozen=True)
class ExternalForceSpec:
    name: str
    applied_to_body: str
    side_suffix: str


@dataclass(frozen=True)
class RunConfig:
    model_path: Path
    coordinates_path: Path
    grf_path: Path
    session_id: str
    output_root: Path
    output_dir: Path
    grf_setup_path: Path
    id_setup_path: Path
    output_sto_path: Path
    output_file_name: str
    lowpass_cutoff: float


EXTERNAL_FORCE_SPECS = (
    ExternalForceSpec(name="left_GRF", applied_to_body="calcn_l", side_suffix="l"),
    ExternalForceSpec(name="right_GRF", applied_to_body="calcn_r", side_suffix="r"),
)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log_message("Starting inverse dynamics run.")
    configs = build_run_configs(args)

    if len(configs) == 1:
        print(f"Found 1 inverse dynamics job.")
    else:
        print(f"Found {len(configs)} inverse dynamics jobs.")

    failures: list[tuple[str, str]] = []
    skipped_sessions: list[str] = []

    for index, config in enumerate(configs, start=1):
        print(f"[{index}/{len(configs)}] {config.session_id}")
        log_message(f"[{index}/{len(configs)}] Starting session {config.session_id}.")

        try:
            status = run_job(config, overwrite=args.overwrite)
        except Exception as exc:
            failures.append((config.session_id, str(exc)))
            log_message(f"[{index}/{len(configs)}] FAILED {config.session_id}: {exc}")
            print(f"    Failed: {exc}")
            continue

        if status == "skipped":
            skipped_sessions.append(config.session_id)
            log_message(f"[{index}/{len(configs)}] Skipped session {config.session_id}.")
            print(f"    Skipped existing result: {config.output_sto_path}")
            continue

        log_message(f"[{index}/{len(configs)}] Finished session {config.session_id}.")
        print(f"    Result: {config.output_sto_path}")

    if skipped_sessions:
        print(f"Skipped {len(skipped_sessions)} session(s) with existing results.")

    if failures:
        print("Some sessions failed. See inverse_dynamics_log for details.")
        return 1

    print("All requested inverse dynamics jobs completed.")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate OpenSim external loads and inverse dynamics setup XML files, "
            "then run the Inverse Dynamics tool."
        )
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Path to the OpenSim model (.osim).",
    )
    parser.add_argument(
        "--coordinates",
        "--mot",
        dest="coordinates",
        type=Path,
        help="Path to the coordinate motion file (.mot).",
    )
    parser.add_argument(
        "--grf",
        type=Path,
        help="Path to the predicted GRF motion file (.mot).",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Optional output label such as A2S3. Inferred automatically if omitted.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR,
        help="Root folder for inverse dynamics outputs. Defaults to OpenSimData/Inverse_Dynamics.",
    )
    parser.add_argument(
        "--output-file",
        default=DEFAULT_OUTPUT_FILE,
        help="Name of the inverse dynamics results file written in the session output directory.",
    )
    parser.add_argument(
        "--lowpass-cutoff",
        type=float,
        default=DEFAULT_LOWPASS_CUTOFF,
        help="Low-pass cutoff frequency for the coordinates file. Use a negative value to disable filtering.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing inverse dynamics results instead of skipping those sessions.",
    )
    return parser.parse_args(argv)


def build_run_configs(args: argparse.Namespace) -> list[RunConfig]:
    provided_paths = [args.model is not None, args.coordinates is not None, args.grf is not None]
    if any(provided_paths) and not all(provided_paths):
        raise ValueError(
            "Provide --model, --coordinates, and --grf together for a single run, "
            "or omit all three to run every discoverable session."
        )

    if all(provided_paths):
        return [build_single_run_config(args)]

    return discover_run_configs(
        output_root=args.output_root.resolve(),
        output_file=args.output_file,
        lowpass_cutoff=args.lowpass_cutoff,
    )


def build_single_run_config(args: argparse.Namespace) -> RunConfig:
    model_path = args.model.resolve()
    coordinates_path = args.coordinates.resolve()
    grf_path = args.grf.resolve()

    session_id = infer_session_id(
        explicit_session_id=args.session_id,
        coordinates_path=coordinates_path,
        grf_path=grf_path,
        model_path=model_path,
    )
    output_root = args.output_root.resolve()
    output_dir = output_root / session_id
    grf_setup_path = grf_path.parent / "GRF_setup.xml"
    id_setup_path = output_dir / "ID_setup.xml"
    output_sto_path = output_dir / args.output_file

    return RunConfig(
        model_path=model_path,
        coordinates_path=coordinates_path,
        grf_path=grf_path,
        session_id=session_id,
        output_root=output_root,
        output_dir=output_dir,
        grf_setup_path=grf_setup_path,
        id_setup_path=id_setup_path,
        output_sto_path=output_sto_path,
        output_file_name=args.output_file,
        lowpass_cutoff=args.lowpass_cutoff,
    )


def discover_run_configs(
    output_root: Path,
    output_file: str,
    lowpass_cutoff: float,
) -> list[RunConfig]:
    model_paths = {
        match.group(1): path.resolve()
        for path in sorted(DEFAULT_MODEL_DIR.glob("ModelProcessed*.osim"))
        if (match := re.fullmatch(r"ModelProcessed([A-Za-z0-9]+)\.osim", path.name))
    }
    coordinate_paths = {
        abbreviate_motion_id(path.stem): path.resolve()
        for path in sorted(DEFAULT_KINEMATICS_DIR.glob("act_*_session_*.mot"))
    }
    grf_paths = {
        match.group(1): path.resolve()
        for solution_dir in sorted(DEFAULT_KINEMATICS_DIR.glob("Solution*"))
        for path in sorted(solution_dir.glob("Predicted_GRF_*.mot"))
        if (match := re.fullmatch(r"Solution([A-Za-z0-9]+)", solution_dir.name))
    }

    session_ids = sorted(set(model_paths) & set(coordinate_paths) & set(grf_paths))
    if not session_ids:
        raise RuntimeError(
            "Could not find any complete session triplets across Model and Kinematics."
        )

    return [
        RunConfig(
            model_path=model_paths[session_id],
            coordinates_path=coordinate_paths[session_id],
            grf_path=grf_paths[session_id],
            session_id=session_id,
            output_root=output_root,
            output_dir=output_root / session_id,
            grf_setup_path=grf_paths[session_id].parent / "GRF_setup.xml",
            id_setup_path=(output_root / session_id) / "ID_setup.xml",
            output_sto_path=(output_root / session_id) / output_file,
            output_file_name=output_file,
            lowpass_cutoff=lowpass_cutoff,
        )
        for session_id in session_ids
    ]


def validate_inputs(config: RunConfig) -> None:
    required_files = (
        config.model_path,
        config.coordinates_path,
        config.grf_path,
    )
    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required input files:\n" + "\n".join(missing))

    if config.model_path.suffix.lower() != ".osim":
        raise ValueError(f"Model file must be an .osim file: {config.model_path}")

    for path in (config.coordinates_path, config.grf_path):
        if path.suffix.lower() != ".mot":
            raise ValueError(f"Expected a .mot file: {path}")


def run_job(config: RunConfig, overwrite: bool) -> str:
    validate_inputs(config)
    validate_grf_columns(config.grf_path)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.grf_setup_path.parent.mkdir(parents=True, exist_ok=True)

    if config.output_sto_path.exists() and not overwrite:
        return "skipped"

    start_time, end_time = extract_time_range(config.coordinates_path)

    write_grf_setup(config)
    write_id_setup(config, start_time, end_time)
    run_inverse_dynamics(config)

    print(f"    GRF setup: {config.grf_setup_path}")
    print(f"    ID setup: {config.id_setup_path}")
    return "completed"


def infer_session_id(
    explicit_session_id: str,
    coordinates_path: Path,
    grf_path: Path,
    model_path: Path,
) -> str:
    if explicit_session_id.strip():
        return explicit_session_id.strip()

    solution_match = re.fullmatch(r"Solution([A-Za-z0-9]+)", grf_path.parent.name)
    if solution_match:
        return solution_match.group(1)

    model_match = re.fullmatch(r"ModelProcessed([A-Za-z0-9]+)", model_path.stem)
    if model_match:
        return model_match.group(1)

    return abbreviate_motion_id(coordinates_path.stem)


def abbreviate_motion_id(motion_id: str) -> str:
    tokens = [token for token in motion_id.split("_") if token]
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

    abbreviation = "".join(parts)
    if abbreviation == "":
        raise ValueError(f"Could not infer a session id from motion file name: {motion_id}")
    return abbreviation


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
        raise ValueError(f"Could not extract a time range from {mot_path}")

    return first_time, last_time


def extract_column_labels(mot_path: Path) -> list[str]:
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

            labels = line.split()
            if labels and labels[0].lower() == "time":
                return labels

    raise ValueError(f"Could not read column labels from {mot_path}")


def validate_grf_columns(grf_path: Path) -> None:
    labels = set(extract_column_labels(grf_path))
    missing: list[str] = []

    for spec in EXTERNAL_FORCE_SPECS:
        missing.extend(required_vector_columns(labels, f"ground_force_calcn_{spec.side_suffix}_v"))
        missing.extend(required_vector_columns(labels, f"ground_force_calcn_{spec.side_suffix}_p"))
        missing.extend(required_vector_columns(labels, f"ground_torque_calcn_{spec.side_suffix}_v"))

    if missing:
        raise ValueError(
            "The predicted GRF file is missing required columns:\n" + "\n".join(sorted(missing))
        )


def required_vector_columns(labels: set[str], prefix: str) -> list[str]:
    expected = [f"{prefix}{axis}" for axis in ("x", "y", "z")]
    return [column for column in expected if column not in labels]


def write_grf_setup(config: RunConfig) -> None:
    external_loads = osim.ExternalLoads()
    external_loads.setName("externalloads")
    external_loads.setDataFileName(as_opensim_path(config.grf_path))

    data_source_name = build_grf_data_source_name(config.coordinates_path)
    for spec in EXTERNAL_FORCE_SPECS:
        force = osim.ExternalForce()
        force.setName(spec.name)
        force.setAppliedToBodyName(spec.applied_to_body)
        force.setForceExpressedInBodyName("ground")
        force.setPointExpressedInBodyName("ground")
        force.setForceIdentifier(f"ground_force_calcn_{spec.side_suffix}_v")
        force.setPointIdentifier(f"ground_force_calcn_{spec.side_suffix}_p")
        force.setTorqueIdentifier(f"ground_torque_calcn_{spec.side_suffix}_v")
        force.set_data_source_name(data_source_name)
        external_loads.cloneAndAppend(force)

    external_loads.printToXML(str(config.grf_setup_path))


def build_grf_data_source_name(coordinates_path: Path) -> str:
    return f"GRF{coordinates_path.name}"


def write_id_setup(config: RunConfig, start_time: float, end_time: float) -> None:
    tool = osim.InverseDynamicsTool()
    tool.setName("ModelProcessed")
    tool.setResultsDir(as_opensim_path(config.output_dir))
    tool.setModelFileName(as_opensim_path(config.model_path))
    tool.setStartTime(start_time)
    tool.setEndTime(end_time)

    excluded_forces = osim.ArrayStr()
    excluded_forces.append("Muscles")
    tool.setExcludedForces(excluded_forces)

    tool.setExternalLoadsFileName(as_opensim_path(config.grf_setup_path))
    tool.setCoordinatesFileName(as_opensim_path(config.coordinates_path))
    tool.setLowpassCutoffFrequency(config.lowpass_cutoff)
    tool.setOutputGenForceFileName(config.output_file_name)
    tool.printToXML(str(config.id_setup_path))


def run_inverse_dynamics(config: RunConfig) -> None:
    tool = osim.InverseDynamicsTool(str(config.id_setup_path))
    if not tool.run():
        raise RuntimeError(f"OpenSim InverseDynamicsTool reported failure for {config.session_id}")

    if not config.output_sto_path.exists():
        raise FileNotFoundError(
            f"Inverse dynamics run finished without producing {config.output_sto_path}"
        )


def as_opensim_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def log_message(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Inverse dynamics script failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
