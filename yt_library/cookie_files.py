"""Safe inspection and replacement of local Netscape cookie files."""

from __future__ import annotations

import http.cookiejar
import os
import tempfile
import time
from pathlib import Path
from typing import Any


MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024
COOKIE_CONFIG = {
    "youtube": ("youtube_cookies", ("youtube.com", "googlevideo.com")),
    "google": ("my_activity_cookies", ("google.com",)),
    "archivarix": ("archivarix_cookies", ("archivarix.net",)),
}


class CookieFileError(ValueError):
    """Raised when a proposed cookie file is missing or invalid."""


def _load_cookie_jar(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise CookieFileError(f"Invalid Netscape cookie file: {exc}") from exc
    return jar


def _matches_expected_domain(domain: str, expected_domains: tuple[str, ...]) -> bool:
    normalized = domain.lstrip(".").casefold()
    return any(
        normalized == expected.casefold()
        or normalized.endswith("." + expected.casefold())
        for expected in expected_domains
    )


def cookie_file_status(
    path: Path,
    expected_domains: tuple[str, ...],
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "configuredPath": str(path),
        "exists": path.is_file(),
        "valid": False,
        "cookieCount": 0,
        "matchingCookieCount": 0,
        "unexpiredMatchingCookieCount": 0,
        "expiredMatchingCookieCount": 0,
        "modifiedAt": "",
        "message": "Cookie file has not been provided.",
    }
    if not path.is_file():
        return status
    try:
        status["modifiedAt"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(path.stat().st_mtime),
        )
        jar = _load_cookie_jar(path)
    except (CookieFileError, OSError) as exc:
        status["message"] = str(exc)
        return status
    now = time.time()
    matching = [
        cookie
        for cookie in jar
        if _matches_expected_domain(cookie.domain, expected_domains)
    ]
    unexpired = [
        cookie
        for cookie in matching
        if cookie.expires is None or cookie.expires > now
    ]
    status.update(
        {
            "valid": bool(matching),
            "cookieCount": len(jar),
            "matchingCookieCount": len(matching),
            "unexpiredMatchingCookieCount": len(unexpired),
            "expiredMatchingCookieCount": len(matching) - len(unexpired),
        }
    )
    if not matching:
        status["message"] = "The file contains no cookies for the expected service."
    elif not unexpired:
        status["message"] = "All matching cookies with expiry dates have expired."
    else:
        status["message"] = "Cookie file is present and contains matching cookies."
    return status


def replace_cookie_file(
    path: Path,
    value: bytes,
    expected_domains: tuple[str, ...],
) -> dict[str, Any]:
    if not value:
        raise CookieFileError("Cookie file content is empty.")
    if len(value) > MAX_COOKIE_FILE_BYTES:
        raise CookieFileError("Cookie file is larger than 2 MiB.")
    try:
        text = value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieFileError("Cookie file must be UTF-8 text.") from exc
    if "# Netscape HTTP Cookie File" not in text[:512]:
        raise CookieFileError("Expected a Netscape HTTP Cookie File export.")

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        status = cookie_file_status(temp_path, expected_domains)
        if not status["valid"]:
            raise CookieFileError(status["message"])
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return cookie_file_status(path, expected_domains)
