from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CURRENT_RUN_FILES = (
    "service.stdout.log",
    "service.stderr.log",
    "service-run.json",
    "yt-library.out.log",
    "yt-library.err.log",
)
DEFAULT_ARCHIVE_RUNS = 20
DEFAULT_ARCHIVE_BYTES = 250 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _archive_name(manifest: dict[str, Any]) -> str:
    raw_started_at = str(manifest.get("startedAt") or utc_now())
    timestamp = "".join(character for character in raw_started_at if character.isdigit())[:14]
    if len(timestamp) != 14:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    run_id = str(manifest.get("runId") or uuid.uuid4().hex).replace("-", "")[:8]
    return f"{timestamp}Z-{run_id}"


def _directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def prune_archives(
    log_directory: Path,
    *,
    keep_runs: int = DEFAULT_ARCHIVE_RUNS,
    keep_bytes: int = DEFAULT_ARCHIVE_BYTES,
) -> tuple[str, ...]:
    archive_directory = (log_directory / "archive").resolve()
    if not archive_directory.is_dir():
        return ()
    entries = sorted(
        (entry for entry in archive_directory.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
        reverse=True,
    )
    retained_bytes = 0
    removed: list[str] = []
    for index, entry in enumerate(entries):
        entry_size = _directory_size(entry)
        within_count = index < max(0, keep_runs)
        retain = within_count and (
            index == 0 or retained_bytes + entry_size <= max(0, keep_bytes)
        )
        if retain:
            retained_bytes += entry_size
            continue
        resolved = entry.resolve()
        if resolved.parent != archive_directory:
            raise RuntimeError(f"Refusing to prune log path outside archive: {resolved}")
        shutil.rmtree(resolved)
        removed.append(entry.name)
    return tuple(removed)


def archive_current_run(
    log_directory: Path,
    *,
    reason: str,
    keep_runs: int = DEFAULT_ARCHIVE_RUNS,
    keep_bytes: int = DEFAULT_ARCHIVE_BYTES,
) -> Path | None:
    log_directory.mkdir(parents=True, exist_ok=True)
    source_paths = [
        log_directory / filename
        for filename in CURRENT_RUN_FILES
        if (log_directory / filename).is_file()
    ]
    if not source_paths:
        prune_archives(log_directory, keep_runs=keep_runs, keep_bytes=keep_bytes)
        return None

    manifest_path = log_directory / "service-run.json"
    manifest = _read_json(manifest_path)
    manifest.update(
        {
            "archiveReason": str(reason or "next-run"),
            "archivedAt": utc_now(),
        }
    )
    if not manifest.get("runId"):
        manifest["runId"] = uuid.uuid4().hex
    if not manifest.get("startedAt"):
        oldest_timestamp = min(path.stat().st_mtime for path in source_paths)
        manifest["startedAt"] = datetime.fromtimestamp(
            oldest_timestamp,
            timezone.utc,
        ).isoformat().replace("+00:00", "Z")

    archive_root = log_directory / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / _archive_name(manifest)
    if archive_path.exists():
        archive_path = archive_root / f"{archive_path.name}-{uuid.uuid4().hex[:6]}"
    archive_path.mkdir()
    _atomic_write_json(archive_path / "service-run.json", manifest)

    for source_path in source_paths:
        if source_path.name == "service-run.json":
            source_path.unlink(missing_ok=True)
            continue
        source_path.replace(archive_path / source_path.name)

    prune_archives(log_directory, keep_runs=keep_runs, keep_bytes=keep_bytes)
    return archive_path


def prepare_run(
    log_directory: Path,
    *,
    mode: str,
    host_pid: int = 0,
    archive_reason: str = "next-run",
) -> dict[str, Any]:
    archive_current_run(log_directory, reason=archive_reason)
    manifest = {
        "runId": uuid.uuid4().hex,
        "mode": str(mode or "direct"),
        "startedAt": utc_now(),
        "healthyAt": "",
        "stoppedAt": "",
        "hostPid": max(0, int(host_pid or 0)),
        "launcherPid": 0,
        "servicePid": 0,
        "exitCode": None,
        "stopReason": "",
    }
    _atomic_write_json(log_directory / "service-run.json", manifest)
    return manifest


def update_manifest(log_directory: Path, **values: Any) -> dict[str, Any]:
    path = log_directory / "service-run.json"
    manifest = _read_json(path)
    if not manifest:
        manifest = prepare_run(log_directory, mode=str(values.pop("mode", "unknown")))
    manifest.update(values)
    _atomic_write_json(path, manifest)
    return manifest


def queue_intent(log_directory: Path) -> dict[str, Any]:
    return _read_json(log_directory / "service-queue-intent.json")


def write_queue_intent(
    log_directory: Path,
    should_run: bool,
    *,
    source: str,
) -> dict[str, Any]:
    state = {
        "queueShouldRun": bool(should_run),
        "updatedAt": utc_now(),
        "source": str(source or "unknown"),
    }
    _atomic_write_json(log_directory / "service-queue-intent.json", state)
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage YT Library service run state")
    parser.add_argument("--log-directory", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--mode", default="direct")
    prepare.add_argument("--host-pid", type=int, default=0)
    prepare.add_argument("--archive-reason", default="next-run")

    update = subparsers.add_parser("update")
    update.add_argument("--launcher-pid", type=int)
    update.add_argument("--service-pid", type=int)
    update.add_argument("--healthy", action="store_true")
    update.add_argument("--stopped", action="store_true")
    update.add_argument("--exit-code", type=int)
    update.add_argument("--stop-reason")

    intent = subparsers.add_parser("queue-intent")
    intent.add_argument("value", choices=("running", "stopped"))
    intent.add_argument("--source", default="controller")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log_directory = args.log_directory.resolve()
    if args.action == "prepare":
        result = prepare_run(
            log_directory,
            mode=args.mode,
            host_pid=args.host_pid,
            archive_reason=args.archive_reason,
        )
    elif args.action == "update":
        values: dict[str, Any] = {}
        if args.launcher_pid is not None:
            values["launcherPid"] = args.launcher_pid
        if args.service_pid is not None:
            values["servicePid"] = args.service_pid
        if args.healthy:
            values["healthyAt"] = utc_now()
        if args.stopped:
            values["stoppedAt"] = utc_now()
        if args.exit_code is not None:
            values["exitCode"] = args.exit_code
        if args.stop_reason is not None:
            values["stopReason"] = args.stop_reason
        result = update_manifest(log_directory, **values)
    else:
        result = write_queue_intent(
            log_directory,
            args.value == "running",
            source=args.source,
        )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
