"""Network transport helpers."""

from __future__ import annotations

import http.client
import importlib
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from typing import Any


class ProxyUnavailableError(RuntimeError):
    """Raised when the configured SOCKS proxy cannot accept requests."""


@dataclass(frozen=True)
class Socks5Proxy:
    host: str
    port: int
    remote_dns: bool
    username: str | None
    password: str | None


def _proxy_endpoint(proxy: Socks5Proxy) -> str:
    return f"{proxy.host}:{proxy.port}"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    chain: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for nested in (
            getattr(current, "reason", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
        for value in current.args:
            if isinstance(value, BaseException):
                pending.append(value)
    return chain


def _explicit_proxy_failure(exc: BaseException) -> bool:
    for current in _exception_chain(exc):
        if isinstance(current, ProxyUnavailableError):
            return True
        if isinstance(
            current,
            (
                ConnectionAbortedError,
                ConnectionRefusedError,
                ConnectionResetError,
                BrokenPipeError,
            ),
        ):
            return True
        class_name = type(current).__name__
        module_name = type(current).__module__
        if class_name in {"ProxyConnectionError", "SOCKS5AuthError"}:
            return True
        if any(
            base.__name__ == "ProxyError" and base.__module__ == "socks"
            for base in type(current).__mro__
        ):
            return True
        if class_name == "ProxyError" and module_name.startswith("yt_dlp."):
            return True
        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "error connecting to socks5 proxy",
                "failed to connect to socks5 proxy",
                "unable to connect to socks5 proxy",
                "socks5 proxy authentication failed",
                "socks5 authentication failed",
            )
        ):
            return True
    return False


def _safe_proxy_error_detail(exc: BaseException) -> str:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return re.sub(
        r"(?i)(socks5h?://)[^@\s/]+@",
        r"\1",
        detail,
    )


def proxy_unavailable_error(
    exc: BaseException,
    proxy_url: str | None,
) -> ProxyUnavailableError | None:
    proxy = parse_socks5_proxy_url(proxy_url)
    if proxy is None or not _explicit_proxy_failure(exc):
        return None
    if isinstance(exc, ProxyUnavailableError):
        return ProxyUnavailableError(_safe_proxy_error_detail(exc))
    detail = _safe_proxy_error_detail(exc)
    return ProxyUnavailableError(
        f"SOCKS5 proxy {_proxy_endpoint(proxy)} is unavailable: {detail}"
    )


def parse_socks5_proxy_url(value: str | None) -> Socks5Proxy | None:
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return None
    parsed = urllib.parse.urlsplit(proxy_url)
    if parsed.scheme.lower() not in {"socks5", "socks5h"}:
        raise ValueError("Proxy must use socks5:// or socks5h://")
    if not parsed.hostname:
        raise ValueError("SOCKS5 proxy must include a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("SOCKS5 proxy must include a valid port") from exc
    if port is None:
        raise ValueError("SOCKS5 proxy must include a port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("SOCKS5 proxy must not include a path, query, or fragment")
    return Socks5Proxy(
        host=parsed.hostname,
        port=port,
        remote_dns=parsed.scheme.lower() == "socks5h",
        username=urllib.parse.unquote(parsed.username) if parsed.username is not None else None,
        password=urllib.parse.unquote(parsed.password) if parsed.password is not None else None,
    )


def validated_socks5_proxy_url(value: str | None) -> str:
    proxy_url = str(value or "").strip()
    parse_socks5_proxy_url(proxy_url)
    return proxy_url


def ytdlp_proxy_options(proxy_url: str | None) -> dict[str, str]:
    value = validated_socks5_proxy_url(proxy_url)
    return {"proxy": value} if value else {}


def probe_socks5_proxy(
    proxy_url: str | None,
    *,
    timeout: float = 5.0,
    target_host: str = "www.youtube.com",
    target_port: int = 443,
) -> tuple[bool, str]:
    proxy = parse_socks5_proxy_url(proxy_url)
    if proxy is None:
        return True, ""
    sock = None
    try:
        socks_module = _load_socks_module()
        sock = socks_module.create_connection(
            (target_host, target_port),
            timeout=max(0.1, float(timeout)),
            proxy_type=socks_module.SOCKS5,
            proxy_addr=proxy.host,
            proxy_port=proxy.port,
            proxy_rdns=proxy.remote_dns,
            proxy_username=proxy.username,
            proxy_password=proxy.password,
        )
    except Exception as exc:
        proxy_error = proxy_unavailable_error(exc, proxy_url)
        if proxy_error:
            return False, str(proxy_error)
        return False, (
            f"SOCKS5 proxy {_proxy_endpoint(proxy)} is unavailable: "
            f"{_safe_proxy_error_detail(exc)}"
        )
    finally:
        if sock is not None:
            sock.close()
    return True, ""


def _load_socks_module() -> Any:
    try:
        return importlib.import_module("socks")
    except ImportError as exc:
        raise ProxyUnavailableError(
            "SOCKS5 proxy support requires PySocks; run: python -m pip install -r requirements.txt"
        ) from exc


def _create_socks_socket(
    connection: http.client.HTTPConnection,
    socks_module: Any,
    proxy: Socks5Proxy,
) -> Any:
    try:
        return socks_module.create_connection(
            (connection.host, connection.port),
            timeout=connection.timeout,
            source_address=connection.source_address,
            proxy_type=socks_module.SOCKS5,
            proxy_addr=proxy.host,
            proxy_port=proxy.port,
            proxy_rdns=proxy.remote_dns,
            proxy_username=proxy.username,
            proxy_password=proxy.password,
        )
    except Exception as exc:
        if _explicit_proxy_failure(exc):
            detail = _safe_proxy_error_detail(exc)
            raise ProxyUnavailableError(
                f"SOCKS5 proxy {_proxy_endpoint(proxy)} is unavailable: {detail}"
            ) from exc
        raise


class _Socks5HTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        *,
        socks_module: Any,
        proxy: Socks5Proxy,
        **kwargs: Any,
    ) -> None:
        self._socks_module = socks_module
        self._proxy = proxy
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        self.sock = _create_socks_socket(self, self._socks_module, self._proxy)
        if self._tunnel_host:
            self._tunnel()


class _Socks5HTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        socks_module: Any,
        proxy: Socks5Proxy,
        **kwargs: Any,
    ) -> None:
        self._socks_module = socks_module
        self._proxy = proxy
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        server_hostname = self._tunnel_host or self.host
        self.sock = _create_socks_socket(self, self._socks_module, self._proxy)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _Socks5HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, socks_module: Any, proxy: Socks5Proxy) -> None:
        super().__init__()
        self._connection = partial(
            _Socks5HTTPConnection,
            socks_module=socks_module,
            proxy=proxy,
        )

    def http_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(self._connection, request)


class _Socks5HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, socks_module: Any, proxy: Socks5Proxy) -> None:
        super().__init__()
        self._connection = partial(
            _Socks5HTTPSConnection,
            socks_module=socks_module,
            proxy=proxy,
        )

    def https_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(
            self._connection,
            request,
            context=self._context,
        )


def socks5_proxy_handlers(proxy_url: str | None) -> list[urllib.request.BaseHandler]:
    proxy = parse_socks5_proxy_url(proxy_url)
    if proxy is None:
        return []
    socks_module = _load_socks_module()
    return [
        urllib.request.ProxyHandler({}),
        _Socks5HTTPHandler(socks_module, proxy),
        _Socks5HTTPSHandler(socks_module, proxy),
    ]
