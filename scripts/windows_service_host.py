from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .service_runtime import (
        prepare_run,
        queue_intent,
        update_manifest,
        utc_now,
        write_queue_intent,
    )
except ImportError:
    from service_runtime import (
        prepare_run,
        queue_intent,
        update_manifest,
        utc_now,
        write_queue_intent,
    )


DEFAULT_SERVICE_NAME = "YTLibraryManager"
SERVICE_ENVIRONMENT_KEY = "YT_LIBRARY_WINDOWS_SERVICE"
SERVICE_RESTART_EXIT_CODE = 75
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
SERVICE_WIN32_OWN_PROCESS = 0x00000010
NO_ERROR = 0
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


class ServiceStatus(ctypes.Structure):
    _fields_ = (
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    )


WindowsCallback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


ServiceMainFunction = WindowsCallback(
    None,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPWSTR),
)
HandlerFunction = WindowsCallback(
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPVOID,
)


class ServiceTableEntry(ctypes.Structure):
    _fields_ = (
        ("lpServiceName", wintypes.LPWSTR),
        ("lpServiceProc", ServiceMainFunction),
    )


@dataclass(frozen=True)
class HostConfiguration:
    repo_root: Path
    service_name: str
    restart_delay_seconds: float = 5.0
    max_restart_delay_seconds: float = 60.0
    stable_run_seconds: float = 300.0

    @property
    def log_directory(self) -> Path:
        return self.repo_root / ".codex" / "service-logs"

    @property
    def venv_python(self) -> Path:
        return self.repo_root / ".venv" / "Scripts" / "python.exe"

    @property
    def manager_script(self) -> Path:
        return self.repo_root / "yt_library_manager.py"

    @property
    def child_command(self) -> tuple[str, ...]:
        return (str(self.venv_python), str(self.manager_script))


def _config_value(config: dict[str, Any], key: str, default: Any) -> Any:
    value = config.get(key, default)
    return default if value is None or value == "" else value


def service_base_url(repo_root: Path) -> str:
    config_path = repo_root / "yt_library.config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    host = str(_config_value(config, "host", "127.0.0.1"))
    port = int(_config_value(config, "port", 8765))
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _request_json(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        method=method,
        data=b"" if method == "POST" else None,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


class WindowsServiceHost:
    def __init__(self, config: HostConfiguration) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.stop_reason = ""
        self.status_handle: int | None = None
        self._checkpoint = 0
        self._service_main_callback = ServiceMainFunction(self._service_main)
        self._handler_callback = HandlerFunction(self._control_handler)
        self._host_log_lock = threading.Lock()

    def _host_log(self, level: str, message: str) -> None:
        self.config.log_directory.mkdir(parents=True, exist_ok=True)
        path = self.config.log_directory / "service-host.log"
        with self._host_log_lock:
            if path.is_file() and path.stat().st_size >= 512 * 1024:
                previous = path.with_name("service-host.previous.log")
                previous.unlink(missing_ok=True)
                path.replace(previous)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} [{level}] {message}\n")

    def _report_status(
        self,
        state: int,
        *,
        exit_code: int = 0,
        wait_hint_ms: int = 0,
    ) -> None:
        if not self.status_handle:
            return
        pending = state in {SERVICE_START_PENDING, SERVICE_STOP_PENDING}
        self._checkpoint = self._checkpoint + 1 if pending else 0
        accepted = 0 if pending else SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        status = ServiceStatus(
            SERVICE_WIN32_OWN_PROCESS,
            state,
            accepted,
            max(0, int(exit_code)),
            0,
            self._checkpoint,
            max(0, int(wait_hint_ms)),
        )
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.SetServiceStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ServiceStatus),
        ]
        advapi32.SetServiceStatus.restype = wintypes.BOOL
        if not advapi32.SetServiceStatus(self.status_handle, ctypes.byref(status)):
            self._host_log("ERROR", f"SetServiceStatus failed: {ctypes.get_last_error()}")

    def _control_handler(
        self,
        control: int,
        _event_type: int,
        _event_data: int,
        _context: int,
    ) -> int:
        if control in {SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN}:
            self.stop_reason = (
                "system-shutdown" if control == SERVICE_CONTROL_SHUTDOWN else "service-stop"
            )
            self._report_status(SERVICE_STOP_PENDING, wait_hint_ms=90_000)
            self.stop_event.set()
        return NO_ERROR

    def _service_main(
        self,
        _argument_count: int,
        _arguments: ctypes.POINTER(wintypes.LPWSTR),
    ) -> None:
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.RegisterServiceCtrlHandlerExW.argtypes = [
            wintypes.LPCWSTR,
            HandlerFunction,
            wintypes.LPVOID,
        ]
        advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        self.status_handle = advapi32.RegisterServiceCtrlHandlerExW(
            self.config.service_name,
            self._handler_callback,
            None,
        )
        if not self.status_handle:
            self._host_log(
                "ERROR",
                f"RegisterServiceCtrlHandlerExW failed: {ctypes.get_last_error()}",
            )
            return
        self._report_status(SERVICE_START_PENDING, wait_hint_ms=30_000)
        try:
            self._run_supervisor()
        except BaseException as exc:
            self._host_log("ERROR", f"Service host failed: {type(exc).__name__}: {exc}")
            os._exit(1)

    def _wait_for_health(
        self,
        process: subprocess.Popen[bytes],
        *,
        desired_queue_running: bool,
    ) -> bool:
        deadline = time.monotonic() + 120.0
        base_url = service_base_url(self.config.repo_root)
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if process.poll() is not None:
                return False
            status = _request_json(f"{base_url}/api/admin/runtime/status")
            if status is not None:
                service = status.get("service") if isinstance(status.get("service"), dict) else {}
                update_manifest(
                    self.config.log_directory,
                    servicePid=max(0, int(service.get("pid") or 0)),
                    healthyAt=utc_now(),
                )
                if desired_queue_running and not bool(status.get("workerQueueRunning")):
                    response = _request_json(
                        f"{base_url}/api/admin/queue/start",
                        method="POST",
                        timeout=15.0,
                    )
                    dispatcher = (
                        response.get("dispatcher")
                        if isinstance(response, dict) and isinstance(response.get("dispatcher"), dict)
                        else {}
                    )
                    if dispatcher.get("blocked"):
                        self._host_log(
                            "WARN",
                            f"Queue auto-resume is blocked: {dispatcher.get('message') or 'unknown reason'}",
                        )
                return True
            self.stop_event.wait(0.5)
        return False

    def _remember_runtime_state(self) -> None:
        base_url = service_base_url(self.config.repo_root)
        status = _request_json(f"{base_url}/api/admin/runtime/status", timeout=1.0)
        if status is None or bool(status.get("workerQueueStopping")):
            return
        running = bool(status.get("workerQueueRunning"))
        current = queue_intent(self.config.log_directory)
        proxy_block = (
            status.get("proxyBlock")
            if isinstance(status.get("proxyBlock"), dict)
            else {}
        )
        if (
            not running
            and bool(current.get("queueShouldRun"))
            and bool(proxy_block.get("blocked"))
        ):
            return
        if bool(current.get("queueShouldRun")) != running:
            write_queue_intent(
                self.config.log_directory,
                running,
                source="windows-service-monitor",
            )

    def _stop_child(
        self,
        process: subprocess.Popen[bytes],
        *,
        preserve_queue_intent: bool,
    ) -> None:
        base_url = service_base_url(self.config.repo_root)
        current = queue_intent(self.config.log_directory)
        should_resume = bool(current.get("queueShouldRun"))
        status = _request_json(f"{base_url}/api/admin/runtime/status", timeout=2.0)
        if status is not None:
            should_resume = bool(status.get("workerQueueRunning")) or should_resume
            if bool(status.get("workerQueueRunning")) or bool(status.get("workerQueueStopping")):
                _request_json(
                    f"{base_url}/api/admin/queue/stop",
                    method="POST",
                    timeout=15.0,
                )
        write_queue_intent(
            self.config.log_directory,
            should_resume if preserve_queue_intent else False,
            source=self.stop_reason or "windows-service-stop",
        )
        if process.poll() is not None:
            return
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._host_log("ERROR", f"Child launcher PID {process.pid} did not exit after taskkill")

    def _run_child_once(self) -> tuple[int, float]:
        if not self.config.venv_python.is_file():
            raise FileNotFoundError(f"Project venv Python was not found: {self.config.venv_python}")
        if not self.config.manager_script.is_file():
            raise FileNotFoundError(f"YT Library manager was not found: {self.config.manager_script}")

        started_at = time.monotonic()
        desired_queue_running = bool(
            queue_intent(self.config.log_directory).get("queueShouldRun")
        )
        manifest = prepare_run(
            self.config.log_directory,
            mode="windows-service",
            host_pid=os.getpid(),
            archive_reason="service-child-replacement",
        )
        stdout_path = self.config.log_directory / "service.stdout.log"
        stderr_path = self.config.log_directory / "service.stderr.log"
        environment = dict(os.environ)
        environment[SERVICE_ENVIRONMENT_KEY] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
            "ab", buffering=0
        ) as stderr:
            process = subprocess.Popen(
                self.config.child_command,
                cwd=str(self.config.repo_root),
                env=environment,
                creationflags=creation_flags,
                stdout=stdout,
                stderr=stderr,
            )
            update_manifest(
                self.config.log_directory,
                launcherPid=process.pid,
                hostPid=os.getpid(),
            )
            self._host_log(
                "INFO",
                f"Started child launcher PID {process.pid}; run={manifest['runId']}",
            )
            self._report_status(SERVICE_RUNNING)
            healthy = self._wait_for_health(
                process,
                desired_queue_running=desired_queue_running,
            )
            if not healthy and process.poll() is None and not self.stop_event.is_set():
                self._host_log("ERROR", f"Child launcher PID {process.pid} did not become healthy")
                self._stop_child(process, preserve_queue_intent=True)

            while process.poll() is None and not self.stop_event.wait(0.5):
                self._remember_runtime_state()

            if self.stop_event.is_set():
                self._stop_child(
                    process,
                    preserve_queue_intent=self.stop_reason == "system-shutdown",
                )
            exit_code = process.poll()
            if exit_code is None:
                exit_code = process.wait(timeout=5)
        update_manifest(
            self.config.log_directory,
            stoppedAt=utc_now(),
            exitCode=int(exit_code),
            stopReason=self.stop_reason or (
                "application-restart"
                if exit_code == SERVICE_RESTART_EXIT_CODE
                else "unexpected-child-exit"
            ),
        )
        return int(exit_code), time.monotonic() - started_at

    def _run_supervisor(self) -> None:
        self.config.log_directory.mkdir(parents=True, exist_ok=True)
        self._host_log(
            "INFO",
            f"Windows service host started; service={self.config.service_name}; pid={os.getpid()}",
        )
        consecutive_failures = 0
        while not self.stop_event.is_set():
            exit_code, run_seconds = self._run_child_once()
            if self.stop_event.is_set():
                break
            if exit_code == SERVICE_RESTART_EXIT_CODE:
                self._host_log("INFO", "Application requested a service-managed restart")
                consecutive_failures = 0
                continue
            if run_seconds >= self.config.stable_run_seconds:
                consecutive_failures = 0
            consecutive_failures += 1
            delay_seconds = min(
                self.config.restart_delay_seconds
                * (2 ** min(consecutive_failures - 1, 4)),
                self.config.max_restart_delay_seconds,
            )
            self._host_log("ERROR", f"Child exited unexpectedly with code {exit_code}")
            self._host_log(
                "INFO",
                f"Retrying child in {delay_seconds:.0f} seconds; failure={consecutive_failures}",
            )
            if self.stop_event.wait(delay_seconds):
                break
        self._report_status(SERVICE_STOPPED)
        self._host_log("INFO", f"Windows service host stopped; reason={self.stop_reason or 'stop'}")

    def run_dispatcher(self) -> None:
        if os.name != "nt":
            raise RuntimeError("The Windows service host can only run on Windows")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ServiceTableEntry)]
        advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        table = (ServiceTableEntry * 2)()
        table[0] = ServiceTableEntry(self.config.service_name, self._service_main_callback)
        table[1] = ServiceTableEntry(None, ServiceMainFunction())
        if not advapi32.StartServiceCtrlDispatcherW(table):
            error = ctypes.get_last_error()
            if error == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
                raise RuntimeError("This host must be started by Windows Service Control Manager")
            raise ctypes.WinError(error)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host YT Library as a Windows service")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = HostConfiguration(
        repo_root=args.repo_root.resolve(),
        service_name=str(args.service_name),
    )
    WindowsServiceHost(config).run_dispatcher()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
