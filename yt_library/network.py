"""Network transport helpers."""

from __future__ import annotations

import http.client
import importlib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from typing import Any


@dataclass(frozen=True)
class Socks5Proxy:
    host: str
    port: int
    remote_dns: bool
    username: str | None
    password: str | None


def parse_socks5_proxy_url(value: str | None) -> Socks5Proxy | None:
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return None
    parsed = urllib.parse.urlsplit(proxy_url)
    if parsed.scheme.lower() not in {"socks5", "socks5h"}:
        raise ValueError("YouTube proxy must use socks5:// or socks5h://")
    if not parsed.hostname:
        raise ValueError("YouTube SOCKS5 proxy must include a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("YouTube SOCKS5 proxy must include a valid port") from exc
    if port is None:
        raise ValueError("YouTube SOCKS5 proxy must include a port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("YouTube SOCKS5 proxy must not include a path, query, or fragment")
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


def youtube_ytdlp_proxy_options(proxy_url: str | None) -> dict[str, str]:
    value = validated_socks5_proxy_url(proxy_url)
    return {"proxy": value} if value else {}


def _load_socks_module() -> Any:
    try:
        return importlib.import_module("socks")
    except ImportError as exc:
        raise RuntimeError(
            "SOCKS5 proxy support requires PySocks; run: python -m pip install -r requirements.txt"
        ) from exc


def _create_socks_socket(
    connection: http.client.HTTPConnection,
    socks_module: Any,
    proxy: Socks5Proxy,
) -> Any:
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
