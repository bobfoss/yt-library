"""Collect exact YouTube watch events from the signed-in Google My Activity page."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .network import socks5_proxy_handlers


MY_ACTIVITY_YOUTUBE_URL = "https://myactivity.google.com/product/youtube?hl=en"
MY_ACTIVITY_CONTINUATION_URL = (
    "https://myactivity.google.com/_/FootprintsMyactivityUi/data/batchexecute"
)
_CALLBACK_MARKER = "AF_initDataCallback("
_DATA_PATTERN = re.compile(r"\bdata\s*:\s*")
_CALLBACK_KEY_PATTERN = re.compile(r"\bkey\s*:\s*['\"]([^'\"]+)['\"]")
_WIZ_GLOBAL_PATTERN = re.compile(r"\bWIZ_global_data\s*=\s*")
_REQUEST_PATTERN = re.compile(r"\brequest\s*:\s*")
_MIN_ACTIVITY_TIMESTAMP_US = 946_684_800_000_000
_MAX_ACTIVITY_TIMESTAMP_US = 4_102_444_800_000_000
_COOKIE_SAVE_LOCK = threading.Lock()


class MyActivityError(RuntimeError):
    """Base class for actionable My Activity collection failures."""


@dataclass(frozen=True)
class MyActivityPage:
    events: list[MyActivityWatchEvent]
    subscription_events: list[MyActivitySubscriptionEvent]
    continuation_token: str
    activity_records: int
    record_ids: frozenset[str]


@dataclass(frozen=True)
class MyActivitySession:
    rpc_id: str
    request_template: list[Any]
    f_sid: str
    bl: str
    at: str


@dataclass(frozen=True)
class MyActivityHttpSession:
    opener: Any
    cookie_jar: http.cookiejar.MozillaCookieJar
    cookie_path: Path
    original_mtime_ns: int


@dataclass(frozen=True)
class MyActivityWatchEvent:
    event_id: str
    video_id: str
    watched_at: str
    title: str
    url: str
    source: str = "google_my_activity"

@dataclass(frozen=True)
class MyActivitySubscriptionEvent:
    event_id: str
    channel_id: str
    subscribed_at: str
    title: str
    url: str
    source: str = "google_my_activity"


def _balanced_json_value(text: str, start: int) -> tuple[str, int]:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        raise MyActivityError("My Activity callback did not contain a JSON object or array")

    opening = text[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1
    raise MyActivityError("My Activity callback contained an incomplete JSON value")


def _callback_entries(page_text: str) -> Iterator[tuple[str, Any]]:
    search_from = 0
    while True:
        callback_at = page_text.find(_CALLBACK_MARKER, search_from)
        if callback_at < 0:
            return
        header_start = callback_at + len(_CALLBACK_MARKER)
        header = page_text[header_start : header_start + 500]
        key_match = _CALLBACK_KEY_PATTERN.search(header)
        data_match = _DATA_PATTERN.search(header)
        search_from = header_start
        if not key_match or not data_match:
            continue
        value_start = header_start + data_match.end()
        try:
            raw_value, search_from = _balanced_json_value(page_text, value_start)
            yield key_match.group(1), json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise MyActivityError(
                f"My Activity callback data was not valid JSON: {exc.msg}"
            ) from exc


def _nested_lists(value: Any) -> Iterator[list[Any]]:
    stack = [value]
    while stack:
        current = stack.pop()
        if not isinstance(current, list):
            continue
        yield current
        stack.extend(reversed(current))


def _string_leaves(value: Any) -> Iterator[str]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, list):
            stack.extend(reversed(current))


def _youtube_video_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if hostname == "youtube.com" or hostname.endswith(".youtube.com"):
        return urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    return ""


def _youtube_channel_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host.endswith(("youtube.com", "youtu.be")):
        return ""
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and parts[1].startswith("UC"):
        return parts[1]
    if parts and parts[0].startswith("@"):
        return parts[0]
    if len(parts) >= 2 and parts[0] in {"c", "user"}:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _activity_timestamp(timestamp_us: int) -> str:
    seconds, microseconds = divmod(timestamp_us, 1_000_000)
    value = datetime.fromtimestamp(seconds, timezone.utc).replace(
        microsecond=microseconds
    )
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalized_text(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-16", errors="surrogatepass").decode(
            "utf-16", errors="replace"
        )
    return value


def _watch_event_from_record(record: list[Any]) -> MyActivityWatchEvent | None:
    if len(record) <= 9:
        return None
    timestamp_us = record[4]
    token = record[5]
    action = record[9]
    if (
        isinstance(timestamp_us, bool)
        or not isinstance(timestamp_us, int)
        or not (_MIN_ACTIVITY_TIMESTAMP_US <= timestamp_us <= _MAX_ACTIVITY_TIMESTAMP_US)
        or not isinstance(token, str)
        or not token
        or not isinstance(action, list)
    ):
        return None

    values = list(_string_leaves(action))
    if not any(value.casefold() == "watched" for value in values):
        return None
    url = next(
        (
            value
            for value in values
            if value.startswith(("https://", "http://")) and _youtube_video_id(value)
        ),
        "",
    )
    video_id = _youtube_video_id(url)
    if not video_id:
        return None
    title = next(
        (
            value
            for value in values
            if value != url and value.casefold() != "watched"
        ),
        "",
    )
    token_hash = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
    return MyActivityWatchEvent(
        event_id=f"my_activity:{token_hash}",
        video_id=video_id,
        watched_at=_activity_timestamp(timestamp_us),
        title=_normalized_text(title),
        url=_normalized_text(url),
    )


def _subscription_event_from_record(
    record: list[Any],
) -> MyActivitySubscriptionEvent | None:
    if len(record) <= 9:
        return None
    timestamp_us = record[4]
    token = record[5]
    action = record[9]
    if (
        isinstance(timestamp_us, bool)
        or not isinstance(timestamp_us, int)
        or not (_MIN_ACTIVITY_TIMESTAMP_US <= timestamp_us <= _MAX_ACTIVITY_TIMESTAMP_US)
        or not isinstance(token, str)
        or not token
        or not isinstance(action, list)
    ):
        return None

    values = list(_string_leaves(action))
    action_values = {
        value.casefold().rstrip(":")
        for value in values
        if not value.startswith(("https://", "http://"))
    }
    if not any(
        value == "subscribed to" or value.startswith("subscribed to ")
        for value in action_values
    ):
        return None
    url = next(
        (
            value
            for value in values
            if value.startswith(("https://", "http://")) and _youtube_channel_id(value)
        ),
        "",
    )
    channel_id = _youtube_channel_id(url)
    if not channel_id:
        return None
    title = next(
        (
            value
            for value in values
            if value != url
            and not value.casefold().rstrip(":").startswith("subscribed to")
            and not value.startswith(("https://", "http://"))
        ),
        "",
    )
    token_hash = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
    return MyActivitySubscriptionEvent(
        event_id=f"my_activity:{token_hash}",
        channel_id=channel_id,
        subscribed_at=_activity_timestamp(timestamp_us),
        title=_normalized_text(title),
        url=_normalized_text(url),
    )


def _record_id(record: Any) -> str:
    if not isinstance(record, list) or len(record) <= 5:
        return ""
    token = record[5]
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def _activity_page_from_payload(payload: Any) -> MyActivityPage:
    if (
        not isinstance(payload, list)
        or len(payload) < 2
        or payload[0] is not None
        and not isinstance(payload[0], list)
        or payload[1] is not None
        and not isinstance(payload[1], str)
    ):
        raise MyActivityError("My Activity returned an unexpected activity-page shape")

    records = payload[0] or []
    record_ids = frozenset(
        record_id for record in records if (record_id := _record_id(record))
    )
    if len(record_ids) != len(records):
        raise MyActivityError(
            "My Activity returned activity rows with missing or duplicate stable tokens"
        )

    events: dict[str, MyActivityWatchEvent] = {}
    subscription_events: dict[str, MyActivitySubscriptionEvent] = {}
    for record in records:
        if not isinstance(record, list):
            continue
        event = _watch_event_from_record(record)
        if event:
            events[event.event_id] = event
        subscription_event = _subscription_event_from_record(record)
        if subscription_event:
            subscription_events[subscription_event.event_id] = subscription_event
    return MyActivityPage(
        events=sorted(
            events.values(),
            key=lambda event: (event.watched_at, event.event_id),
            reverse=True,
        ),
        subscription_events=sorted(
            subscription_events.values(),
            key=lambda event: (event.subscribed_at, event.event_id),
            reverse=True,
        ),
        continuation_token=payload[1] or "",
        activity_records=len(records),
        record_ids=record_ids,
    )


def _activity_callback(
    page_text: str,
    *,
    require_watch_events: bool = True,
) -> tuple[str, MyActivityPage]:
    found_callback = False
    for key, payload in _callback_entries(page_text):
        found_callback = True
        try:
            page = _activity_page_from_payload(payload)
        except MyActivityError:
            continue
        if page.record_ids and (page.events or not require_watch_events):
            return key, page
    if not found_callback:
        raise MyActivityError(
            "No My Activity bootstrap payload was found; the cookies may be expired "
            "or Google may have changed the page"
        )
    if require_watch_events:
        raise MyActivityError(
            "The My Activity bootstrap payload contained no YouTube watch events"
        )
    raise MyActivityError("The My Activity bootstrap payload contained no activity rows")


def parse_my_activity_watch_events(page_text: str) -> list[MyActivityWatchEvent]:
    """Parse exact watched-event records from My Activity bootstrap callbacks."""

    _, page = _activity_callback(page_text)
    return page.events


def parse_my_activity_subscription_events(
    page_text: str,
) -> list[MyActivitySubscriptionEvent]:
    """Parse exact channel-subscription records from My Activity callbacks."""

    _, page = _activity_callback(page_text, require_watch_events=False)
    return page.subscription_events


def _data_service_request(page_text: str, data_key: str) -> tuple[str, list[Any]]:
    pattern = re.compile(
        rf"['\"]?{re.escape(data_key)}['\"]?\s*:\s*\{{\s*"
        rf"id\s*:\s*['\"]([^'\"]+)['\"]"
    )
    match = pattern.search(page_text)
    if not match:
        raise MyActivityError("My Activity did not expose its history RPC metadata")
    request_match = _REQUEST_PATTERN.search(page_text, match.end(), match.end() + 2000)
    if not request_match:
        raise MyActivityError("My Activity history RPC metadata had no request template")
    raw_request, _ = _balanced_json_value(page_text, request_match.end())
    try:
        request_template = json.loads(raw_request)
    except json.JSONDecodeError as exc:
        raise MyActivityError("My Activity history request template was invalid") from exc
    if (
        not isinstance(request_template, list)
        or len(request_template) < 2
        or request_template[1] is not None
    ):
        raise MyActivityError("My Activity history request template changed shape")
    if "youtube" not in set(_string_leaves(request_template)):
        raise MyActivityError("My Activity history request no longer targets YouTube")
    return match.group(1), request_template


def _wiz_global_data(page_text: str) -> dict[str, Any]:
    match = _WIZ_GLOBAL_PATTERN.search(page_text)
    if not match:
        raise MyActivityError("My Activity did not expose its signed-in RPC session data")
    raw_value, _ = _balanced_json_value(page_text, match.end())
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise MyActivityError("My Activity RPC session data was invalid") from exc
    if not isinstance(value, dict):
        raise MyActivityError("My Activity RPC session data changed shape")
    return value


def parse_my_activity_bootstrap(
    page_text: str,
) -> tuple[MyActivityPage, MyActivitySession]:
    """Parse the first activity page and the dynamic continuation parameters."""

    page = None
    rpc_id = ""
    request_template: list[Any] = []
    for data_key, payload in _callback_entries(page_text):
        try:
            candidate_page = _activity_page_from_payload(payload)
            candidate_rpc_id, candidate_request = _data_service_request(
                page_text,
                data_key,
            )
        except MyActivityError:
            continue
        if not candidate_page.record_ids:
            continue
        page = candidate_page
        rpc_id = candidate_rpc_id
        request_template = candidate_request
        break
    if page is None:
        if "ServiceLogin" in page_text or "accounts.google.com" in page_text:
            raise MyActivityError(
                "Google returned a signed-out My Activity shell; refresh the "
                "google.com cookie export"
            )
        raise MyActivityError(
            "My Activity did not expose a YouTube activity page with continuation metadata"
        )
    wiz_data = _wiz_global_data(page_text)
    values = {name: wiz_data.get(name) for name in ("FdrFJe", "cfb2h", "SNlM0e")}
    if not all(isinstance(value, str) and value for value in values.values()):
        raise MyActivityError("My Activity signed-in RPC session data was incomplete")
    return page, MyActivitySession(
        rpc_id=rpc_id,
        request_template=request_template,
        f_sid=values["FdrFJe"],
        bl=values["cfb2h"],
        at=values["SNlM0e"],
    )


def parse_my_activity_continuation_response(
    response_text: str,
    rpc_id: str,
) -> MyActivityPage:
    """Parse Google's framed batchexecute response for one history page."""

    text = response_text
    if text.startswith(")]}'"):
        newline_at = text.find("\n")
        text = text[newline_at + 1 :] if newline_at >= 0 else ""
    for line in text.splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in _nested_lists(frame):
            if (
                len(row) >= 3
                and row[0] == "wrb.fr"
                and row[1] == rpc_id
                and isinstance(row[2], str)
            ):
                try:
                    payload = json.loads(row[2])
                except json.JSONDecodeError as exc:
                    raise MyActivityError(
                        "My Activity continuation payload was invalid JSON"
                    ) from exc
                return _activity_page_from_payload(payload)
    raise MyActivityError("My Activity continuation response contained no history data")


def _my_activity_opener(
    cookie_path: Path,
    proxy_url: str = "",
) -> MyActivityHttpSession:
    if not cookie_path.is_file():
        raise MyActivityError(f"My Activity cookie file not found: {cookie_path}")
    cookie_jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=False)
    except (http.cookiejar.LoadError, OSError) as exc:
        raise MyActivityError(
            f"Could not load Netscape-format My Activity cookies from {cookie_path}: {exc}"
        ) from exc
    if not any(
        (cookie.domain or "").lstrip(".").endswith("google.com")
        for cookie in cookie_jar
    ):
        raise MyActivityError(
            "The My Activity cookie export contains no google.com cookies; "
            "a youtube.com-only export cannot authenticate this page"
        )
    return MyActivityHttpSession(
        opener=urllib.request.build_opener(
            *socks5_proxy_handlers(proxy_url),
            urllib.request.HTTPCookieProcessor(cookie_jar)
        ),
        cookie_jar=cookie_jar,
        cookie_path=cookie_path,
        original_mtime_ns=cookie_path.stat().st_mtime_ns,
    )


def _save_refreshed_cookies(session: MyActivityHttpSession) -> bool:
    """Persist server-issued cookie rotation without overwriting a newer Admin upload."""

    with _COOKIE_SAVE_LOCK:
        try:
            if session.cookie_path.stat().st_mtime_ns != session.original_mtime_ns:
                return False
        except OSError:
            return False
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{session.cookie_path.name}.",
            suffix=".tmp",
            dir=session.cookie_path.parent,
            delete=False,
        )
        temp_path = Path(handle.name)
        handle.close()
        try:
            session.cookie_jar.save(
                str(temp_path),
                ignore_discard=True,
                ignore_expires=False,
            )
            os.replace(temp_path, session.cookie_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return True


def _request_headers() -> dict[str, str]:
    return {
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
    }


def _fetch_my_activity_page(opener: Any, *, timeout: float) -> str:
    request = urllib.request.Request(
        MY_ACTIVITY_YOUTUBE_URL,
        headers=_request_headers(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            page_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except OSError as exc:
        raise MyActivityError(f"Could not fetch Google My Activity: {exc}") from exc
    if "accounts.google.com" in urllib.parse.urlparse(final_url).netloc:
        raise MyActivityError(
            "Google redirected the My Activity request to sign-in; refresh the cookie export"
        )
    return page_bytes.decode(charset, errors="replace")


def fetch_my_activity_page(
    cookie_path: Path,
    *,
    timeout: float = 30.0,
    proxy_url: str = "",
) -> str:
    """Fetch the English YouTube My Activity page using a Netscape cookie export."""

    return _fetch_my_activity_page(
        _my_activity_opener(cookie_path, proxy_url).opener,
        timeout=timeout,
    )


def _continuation_request(
    session: MyActivitySession,
    continuation_token: str,
) -> urllib.request.Request:
    request_arguments = deepcopy(session.request_template)
    request_arguments[1] = continuation_token
    rpc_envelope = [[[
        session.rpc_id,
        json.dumps(request_arguments, ensure_ascii=False, separators=(",", ":")),
        None,
        "generic",
    ]]]
    query = urllib.parse.urlencode(
        {
            "rpcids": session.rpc_id,
            "source-path": "/product/youtube",
            "f.sid": session.f_sid,
            "bl": session.bl,
            "hl": "en",
            "_reqid": str(int(time.time() * 1000) % 1_000_000),
            "rt": "c",
        }
    )
    body = urllib.parse.urlencode(
        {
            "f.req": json.dumps(rpc_envelope, ensure_ascii=False, separators=(",", ":")),
            "at": session.at,
        }
    ).encode("utf-8")
    return urllib.request.Request(
        f"{MY_ACTIVITY_CONTINUATION_URL}?{query}",
        data=body,
        method="POST",
        headers={
            **_request_headers(),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://myactivity.google.com",
            "Referer": MY_ACTIVITY_YOUTUBE_URL,
            "X-Same-Domain": "1",
        },
    )


def _fetch_my_activity_continuation(
    opener: Any,
    session: MyActivitySession,
    continuation_token: str,
    *,
    timeout: float,
) -> MyActivityPage:
    request = _continuation_request(session, continuation_token)
    try:
        with opener.open(request, timeout=timeout) as response:
            response_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise MyActivityError(
            f"Google My Activity continuation request failed with HTTP {exc.code}"
        ) from exc
    except OSError as exc:
        raise MyActivityError(
            f"Could not fetch a Google My Activity continuation page: {exc}"
        ) from exc
    return parse_my_activity_continuation_response(
        response_bytes.decode(charset, errors="replace"),
        session.rpc_id,
    )


def _follow_my_activity_continuations(
    first_page: MyActivityPage,
    fetch_next: Callable[[str], MyActivityPage],
    *,
    max_pages: int,
) -> list[MyActivityPage]:
    if max_pages < 1:
        raise MyActivityError("--max-pages must be at least 1")
    pages = [first_page]
    seen_record_ids = set(first_page.record_ids)
    seen_tokens: set[str] = set()
    token = first_page.continuation_token
    while token and len(pages) < max_pages:
        if token in seen_tokens:
            raise MyActivityError(
                "My Activity repeated a continuation token; collection stopped"
            )
        seen_tokens.add(token)
        page = fetch_next(token)
        if page.continuation_token in seen_tokens:
            raise MyActivityError(
                "My Activity returned a continuation loop; collection stopped"
            )
        if page.activity_records and not page.record_ids - seen_record_ids:
            raise MyActivityError(
                "My Activity continuation returned no new activity records; "
                "collection stopped"
            )
        if page.continuation_token and not page.activity_records:
            raise MyActivityError(
                "My Activity returned an empty non-terminal continuation page"
            )
        pages.append(page)
        seen_record_ids.update(page.record_ids)
        token = page.continuation_token
    return pages


def fetch_my_activity_pages(
    cookie_path: Path,
    *,
    max_pages: int = 1,
    timeout: float = 30.0,
    proxy_url: str = "",
) -> list[MyActivityPage]:
    """Fetch a bounded sequence of signed-in My Activity history pages."""

    http_session = _my_activity_opener(cookie_path, proxy_url)
    page_text = _fetch_my_activity_page(http_session.opener, timeout=timeout)
    first_page, session = parse_my_activity_bootstrap(page_text)
    pages = _follow_my_activity_continuations(
        first_page,
        lambda token: _fetch_my_activity_continuation(
            http_session.opener,
            session,
            token,
            timeout=timeout,
        ),
        max_pages=max_pages,
    )
    _save_refreshed_cookies(http_session)
    return pages


def collect_my_activity(args: Any) -> dict[str, Any]:
    max_pages = int(getattr(args, "max_pages", 1))
    if max_pages < 1:
        raise SystemExit("--max-pages must be at least 1")
    html_path = Path(args.html) if getattr(args, "html", "") else None
    if html_path:
        if max_pages != 1:
            raise SystemExit("--html supports only --max-pages 1")
        if not html_path.is_file():
            raise SystemExit(f"My Activity HTML file not found: {html_path}")
        page_text = html_path.read_text(encoding="utf-8", errors="replace")
        try:
            _, page = _activity_callback(page_text, require_watch_events=False)
        except (MyActivityError, OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        pages = [page]
    else:
        try:
            from .config import configured_proxy

            pages = fetch_my_activity_pages(
                Path(args.cookies),
                max_pages=max_pages,
                proxy_url=configured_proxy(getattr(args, "config_data", {})),
            )
        except (MyActivityError, OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    watch_events_by_id = {
            event.event_id: event
            for page in pages
            for event in page.events
        }
    subscription_events_by_id = {
        event.event_id: event
        for page in pages
        for event in page.subscription_events
    }
    watch_events = sorted(
        watch_events_by_id.values(),
        key=lambda event: (event.watched_at, event.event_id),
        reverse=True,
    )
    subscription_events = sorted(
        subscription_events_by_id.values(),
        key=lambda event: (event.subscribed_at, event.event_id),
        reverse=True,
    )
    if not watch_events and not subscription_events:
        raise SystemExit(
            "The fetched My Activity pages contained no relevant YouTube events"
        )

    from .config import effective_display_timezone
    from .core import connect, migrate_database, save_my_activity_events

    db_path = Path(args.db)
    migrate_database(db_path)
    conn = connect(db_path)
    try:
        with conn:
            stats = save_my_activity_events(
                conn,
                watch_events,
                subscription_events,
                effective_display_timezone(getattr(args, "config_data", {})),
            )
    finally:
        conn.close()

    overlap = (
        "first collection"
        if stats["first_collection"]
        else f"{stats['overlap_events']} overlapping events"
    )
    print(
        f"Collected {stats['watch_inserted']} new exact watch events and "
        f"{stats['subscription_inserted']} new subscription events in {db_path}; "
        f"{len(pages)} page(s) contained {sum(page.activity_records for page in pages)} "
        f"activity records, {stats['watch_events']} watched events, and "
        f"{stats['subscription_events']} subscription events "
        f"({overlap})."
    )
    continuation_remaining = bool(pages[-1].continuation_token)
    stats.update(
        {
            "pages_fetched": len(pages),
            "activity_records": sum(page.activity_records for page in pages),
            "continuation_remaining": continuation_remaining,
            "coverage_gap": bool(
                not stats["first_collection"] and not stats["overlap_events"]
            ),
        }
    )
    if stats["coverage_gap"]:
        print(
            "Warning: this collection did not overlap stored My Activity events; "
            "a collection gap may exist. Received events were preserved."
        )
    if continuation_remaining:
        print(
            "Older activity remains available. Increase --max-pages for a deeper "
            "bounded backfill."
        )
    else:
        print("Reached the end of the available My Activity history.")
    return stats
