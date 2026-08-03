"""Shared pacing for outbound YouTube and Archivarix requests."""

from __future__ import annotations

import random
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from .config import configured_dispatch_mode, configured_request_delay_range


class RequestPacer:
    """Coordinate randomized request spacing across concurrent workers."""

    def __init__(
        self,
        minimum_delay: float = 0.0,
        maximum_delay: float = 0.0,
        *,
        monotonic=time.monotonic,
        sleep=time.sleep,
        uniform=random.uniform,
    ) -> None:
        self.minimum_delay = max(0.0, minimum_delay)
        self.maximum_delay = max(self.minimum_delay, maximum_delay)
        self._monotonic = monotonic
        self._sleep = sleep
        self._uniform = uniform
        self._lock = threading.Lock()
        self._next_request_at: float | None = None

    def wait(self) -> None:
        if self.maximum_delay <= 0.0:
            return
        with self._lock:
            now = self._monotonic()
            if self._next_request_at is not None and now < self._next_request_at:
                self._sleep(self._next_request_at - now)
                now = self._monotonic()
            self._next_request_at = now + self._uniform(
                self.minimum_delay,
                self.maximum_delay,
            )


_YOUTUBE_REQUEST_HOSTS = (
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "ytimg.com",
    "googlevideo.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
)
_ARCHIVARIX_REQUEST_HOSTS = (
    "archivarix.net",
    "web.archive.org",
)
_request_pacer = RequestPacer()


def configure_request_pacing(config: dict[str, Any]) -> None:
    global _request_pacer
    throttle_requests = configured_dispatch_mode(config) == "throttle"
    request_range = configured_request_delay_range(config)
    if not throttle_requests:
        request_range = (0.0, 0.0)
    _request_pacer = RequestPacer(*request_range)


def pace_outbound_request() -> None:
    """Apply the configured global request gate to a non-urllib client."""

    _request_pacer.wait()


def request_url_matches_hosts(url: str, hosts: tuple[str, ...]) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == host or hostname.endswith(f".{host}") for host in hosts)


def is_youtube_request_url(url: str) -> bool:
    return request_url_matches_hosts(url, _YOUTUBE_REQUEST_HOSTS)


def is_archivarix_request_url(url: str) -> bool:
    return request_url_matches_hosts(url, _ARCHIVARIX_REQUEST_HOSTS)


def open_with_request_pacing(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    if is_youtube_request_url(request.full_url) or is_archivarix_request_url(
        request.full_url
    ):
        _request_pacer.wait()
    return opener.open(request, timeout=timeout)


def request_paced_youtube_dl(
    yt_dlp_module: Any,
    options: dict[str, Any],
) -> Any:
    class RequestPacedYoutubeDL(yt_dlp_module.YoutubeDL):
        def urlopen(self, req: Any) -> Any:
            pace_outbound_request()
            return super().urlopen(req)

    return RequestPacedYoutubeDL(options)
