"""Read models for the library and history web views."""

from __future__ import annotations

import sqlite3
from typing import Any

from .core import (
    archivarix_media_url,
    history_match_type_label,
    history_source_type_label,
    history_time_quality_label,
    history_time_quality_note,
    playlist_match_type_label,
    playlist_match_type_note,
    wayback_video_url,
    youtube_channel_url,
    youtube_playlist_url,
    youtube_video_url,
)


def fetch_app_data(conn: sqlite3.Connection) -> dict[str, Any]:
    groups = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM groups ORDER BY COALESCE(parent_key, ''), position, name"
        )
    ]
    playlists = [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.*,
                   COALESCE(s.video_count, 0) AS scanned_video_count,
                   COALESCE(s.unavailable_count, 0) AS unavailable_count,
                   s.scanned_at,
                   COALESCE(s.scan_status, '') AS scan_status,
                   COALESCE(ch.title, '') AS owner_channel_title,
                   COALESCE(ch.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(ch.status, '') AS owner_channel_status
            FROM playlists p
            LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
            LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
            ORDER BY p.title COLLATE NOCASE
            """
        )
    ]
    for playlist in playlists:
        playlist["url"] = youtube_playlist_url(playlist.get("playlist_id", ""))
        playlist["owner_channel_url"] = youtube_channel_url(playlist.get("owner_channel_id", ""))
    mark_library_owner_playlists(playlists)
    memberships = [
        dict(row)
        for row in conn.execute(
            """
            SELECT gp.group_key, gp.playlist_id, gp.position
            FROM group_playlists gp
            JOIN playlists p ON p.playlist_id = gp.playlist_id
            ORDER BY gp.group_key, gp.position, p.title COLLATE NOCASE
            """
        )
    ]
    channels = [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY title COLLATE NOCASE")]
    for channel in channels:
        channel["url"] = youtube_channel_url(channel.get("channel_id", ""))

    playlist_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT pi.*,
                   pi.position AS display_position,
                   CASE WHEN pi.membership_state = 'current' THEN pi.position ELSE 0 END AS current_position,
                   p.title AS playlist_title,
                   v.title,
                   COALESCE(v.channel_id, '') AS channel_id,
                   COALESCE(ch.title, '') AS channel,
                   v.duration_text,
                   COALESCE(v.is_playable, 0) AS is_playable,
                   CASE WHEN pi.video_id IS NULL THEN pi.unavailable_kind ELSE v.availability END AS availability,
                   v.title AS metadata_title,
                   v.description AS metadata_description,
                   COALESCE(v.channel_id, '') AS metadata_channel_id,
                   COALESCE(ch.title, '') AS metadata_channel,
                   v.duration_text AS metadata_duration,
                   v.upload_date AS metadata_upload_date,
                   v.thumbnail_path AS metadata_thumbnail_path,
                   COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
                   v.fetch_status AS metadata_fetch_status,
                   v.reaction,
                   COALESCE(NULLIF(v.watch_progress_percent, 0), hw.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(NULLIF(v.watch_resume_seconds, 0), hw.watch_resume_seconds, 0) AS watch_resume_seconds,
                   COALESCE(hw.watch_count, 0) AS watch_count,
                   COALESCE(hw.watch_dates, '') AS watch_dates_text,
                   COALESCE(vr.archivarix_status, '') AS recovered_status,
                   vr.archive_capture_at,
                   vr.media_available
            FROM playlist_items pi
            JOIN playlists p ON p.playlist_id = pi.playlist_id
            LEFT JOIN videos v ON v.video_id = pi.video_id
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
            LEFT JOIN (
                SELECT video_id,
                       MAX(watch_progress_percent) AS watch_progress_percent,
                       MAX(watch_resume_seconds) AS watch_resume_seconds,
                       COUNT(*) AS watch_count,
                       GROUP_CONCAT(COALESCE(watch_date, substr(watched_at, 1, 10)), '|') AS watch_dates
                FROM history_events
                GROUP BY video_id
            ) hw ON hw.video_id = pi.video_id
            ORDER BY p.title COLLATE NOCASE, pi.position
            """
        )
    ]
    playlist_links_by_video: dict[str, list[dict[str, Any]]] = {}
    seen_links: set[tuple[str, str]] = set()
    for video in playlist_videos:
        video_id = video.get("video_id") or ""
        playlist_id = video.get("playlist_id") or ""
        video["url"] = youtube_video_url(video_id, playlist_id)
        video["playlist_url"] = youtube_playlist_url(playlist_id)
        video["metadata_channel_url"] = youtube_channel_url(video.get("metadata_channel_id") or "")
        video["archive_url"] = wayback_video_url(video_id, video.get("archive_capture_at"))
        video["video_file_url"] = archivarix_media_url(video_id) if video.get("media_available") else ""
        video["match_label"] = playlist_match_type_label(video.get("match_type") or "")
        video["match_note"] = playlist_match_type_note(video.get("match_type") or "")
        video["watch_dates"] = [value for value in (video.pop("watch_dates_text", "") or "").split("|") if value]
        if not video_id or not playlist_id or (video_id, playlist_id) in seen_links:
            continue
        seen_links.add((video_id, playlist_id))
        playlist_links_by_video.setdefault(video_id, []).append(
            {
                "playlist_id": playlist_id,
                "title": video.get("playlist_title") or playlist_id,
                "removed": video.get("membership_state") == "retained_unavailable",
            }
        )
    for video in playlist_videos:
        video["playlist_links"] = playlist_links_by_video.get(video.get("video_id") or "", [])

    standalone_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT '' AS playlist_id,
                   0 AS position,
                   v.video_id,
                   '' AS membership_state,
                   '' AS unavailable_kind,
                   '' AS source_quality,
                   '' AS match_type,
                   '' AS match_confidence,
                   0 AS display_position,
                   0 AS current_position,
                   '' AS playlist_title,
                   v.title,
                   COALESCE(v.channel_id, '') AS channel_id,
                   COALESCE(ch.title, '') AS channel,
                   v.duration_text,
                   COALESCE(v.is_playable, 0) AS is_playable,
                   v.availability,
                   v.title AS metadata_title,
                   v.description AS metadata_description,
                   COALESCE(v.channel_id, '') AS metadata_channel_id,
                   COALESCE(ch.title, '') AS metadata_channel,
                   v.duration_text AS metadata_duration,
                   v.upload_date AS metadata_upload_date,
                   v.thumbnail_path AS metadata_thumbnail_path,
                   COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
                   v.fetch_status AS metadata_fetch_status,
                   v.reaction,
                   COALESCE(NULLIF(v.watch_progress_percent, 0), hw.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(NULLIF(v.watch_resume_seconds, 0), hw.watch_resume_seconds, 0) AS watch_resume_seconds,
                   COALESCE(hw.watch_count, 0) AS watch_count,
                   COALESCE(hw.watch_dates, '') AS watch_dates_text,
                   COALESCE(vr.archivarix_status, '') AS recovered_status,
                   vr.archive_capture_at,
                   vr.media_available
            FROM videos v
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
            LEFT JOIN (
                SELECT video_id,
                       MAX(watch_progress_percent) AS watch_progress_percent,
                       MAX(watch_resume_seconds) AS watch_resume_seconds,
                       COUNT(*) AS watch_count,
                       GROUP_CONCAT(COALESCE(watch_date, substr(watched_at, 1, 10)), '|') AS watch_dates
                FROM history_events
                GROUP BY video_id
            ) hw ON hw.video_id = v.video_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM playlist_items pi
                WHERE pi.video_id = v.video_id
            )
            ORDER BY v.title COLLATE NOCASE, v.video_id
            """
        )
    ]
    for video in standalone_videos:
        video_id = video.get("video_id") or ""
        video["url"] = youtube_video_url(video_id)
        video["playlist_url"] = ""
        video["metadata_channel_url"] = youtube_channel_url(video.get("metadata_channel_id") or "")
        video["archive_url"] = wayback_video_url(video_id, video.get("archive_capture_at"))
        video["video_file_url"] = archivarix_media_url(video_id) if video.get("media_available") else ""
        video["match_label"] = ""
        video["match_note"] = ""
        video["watch_dates"] = [value for value in (video.pop("watch_dates_text", "") or "").split("|") if value]
        video["playlist_links"] = []

    unavailable_videos = [
        video
        for video in playlist_videos
        if video.get("membership_state") != "current" or not video.get("is_playable")
    ]
    totals = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_events) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_events) AS distinct_watch_videos
            """
        ).fetchone()
    )
    return {
        "groups": groups,
        "playlists": playlists,
        "memberships": memberships,
        "playlistVideos": playlist_videos,
        "standaloneVideos": standalone_videos,
        "channels": channels,
        "unavailableVideos": unavailable_videos,
        "historySummary": totals,
    }


def clean_playlist_owner_name(value: str) -> str:
    value = (value or "").strip()
    return value[3:].strip() if value.lower().startswith("by ") else value


def mark_library_owner_playlists(playlists: list[dict[str, Any]]) -> None:
    channel_counts: dict[str, int] = {}
    name_counts: dict[str, int] = {}
    for playlist in playlists:
        if (playlist.get("visibility") or "").strip():
            continue
        owner_channel_id = (playlist.get("owner_channel_id") or "").strip()
        owner_name = clean_playlist_owner_name(playlist.get("owner_channel_title") or "")
        if owner_channel_id:
            channel_counts[owner_channel_id] = channel_counts.get(owner_channel_id, 0) + 1
        if owner_name:
            key = owner_name.casefold()
            name_counts[key] = name_counts.get(key, 0) + 1
    library_channel_id = dominant_owner_key(channel_counts)
    library_owner_name = dominant_owner_key(name_counts)
    for playlist in playlists:
        owner_channel_id = (playlist.get("owner_channel_id") or "").strip()
        owner_name = clean_playlist_owner_name(playlist.get("owner_channel_title") or "")
        playlist["owner_channel_title"] = owner_name
        playlist["is_library_owner"] = int(
            bool(library_channel_id and owner_channel_id == library_channel_id)
            or bool(library_owner_name and owner_name.casefold() == library_owner_name)
        )


def dominant_owner_key(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    top_key, top_count = ordered[0]
    next_count = ordered[1][1] if len(ordered) > 1 else 0
    return top_key if top_count >= 5 and top_count >= max(2, next_count * 3) else ""


def library_bootstrap_data(conn: sqlite3.Connection) -> dict[str, Any]:
    groups = [
        dict(row)
        for row in conn.execute("SELECT * FROM groups ORDER BY COALESCE(parent_key, ''), position, name")
    ]
    memberships = [
        dict(row)
        for row in conn.execute(
            """
            SELECT gp.group_key, gp.playlist_id, gp.position
            FROM group_playlists gp
            JOIN playlists p ON p.playlist_id = gp.playlist_id
            ORDER BY gp.group_key, gp.position, p.title COLLATE NOCASE
            """
        )
    ]
    counts = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM playlists) AS playlists,
              (SELECT COUNT(*) FROM playlists p
                 JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
                WHERE ps.unavailable_count > 0) AS unavailable_playlists,
              (SELECT COUNT(DISTINCT video_id) FROM playlist_items WHERE video_id IS NOT NULL)
                + (SELECT COUNT(*) FROM playlist_items WHERE video_id IS NULL) AS playlist_videos,
              (SELECT COUNT(*) FROM videos WHERE upper(reaction) = 'L') AS liked_videos,
              (SELECT COUNT(*) FROM history_events) AS history,
              (SELECT COUNT(*) FROM channels) AS channels,
              (SELECT COUNT(*) FROM channels WHERE subscribed = 1) AS subscribed_channels,
              (SELECT COUNT(*) FROM channels WHERE lower(status) IN ('terminated', 'deleted')) AS terminated_channels
            """
        ).fetchone()
    )
    return {"groups": groups, "memberships": memberships, "counts": counts}


def _playlist_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.*,
                   COALESCE(s.video_count, 0) AS scanned_video_count,
                   COALESCE(s.unavailable_count, 0) AS unavailable_count,
                   s.scanned_at,
                   COALESCE(s.scan_status, '') AS scan_status,
                   COALESCE(ch.title, '') AS owner_channel_title,
                   COALESCE(ch.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(ch.status, '') AS owner_channel_status
            FROM playlists p
            LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
            LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
            ORDER BY p.title COLLATE NOCASE
            """
        )
    ]
    for playlist in rows:
        playlist["url"] = youtube_playlist_url(playlist.get("playlist_id", ""))
        playlist["owner_channel_url"] = youtube_channel_url(playlist.get("owner_channel_id", ""))
    mark_library_owner_playlists(rows)
    return rows


def _playlist_visibility_category(playlist: dict[str, Any]) -> str:
    visibility = str(playlist.get("visibility") or "").strip().lower()
    if visibility in {"private", "public", "unlisted"}:
        return visibility
    if str(playlist.get("owner_channel_id") or "").strip() and not int(playlist.get("is_library_owner") or 0):
        return "others"
    return "unknown"


def playlist_list_data(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    visibilities: set[str] | None = None,
    sort: str = "title",
    unavailable_only: bool = False,
    group_key: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    rows = _playlist_rows(conn)
    query = query.strip().casefold()
    if query:
        rows = [
            row
            for row in rows
            if query
            in " ".join(
                str(row.get(key) or "")
                for key in (
                    "title",
                    "owner_channel_title",
                    "owner_channel_id",
                    "visibility",
                    "description",
                    "playlist_id",
                )
            ).casefold()
        ]
    if group_key:
        group_ids = {
            row["playlist_id"]
            for row in conn.execute(
                """
                SELECT gp.playlist_id
                FROM group_playlists gp
                WHERE gp.group_key = ?
                   OR gp.group_key IN (SELECT group_key FROM groups WHERE parent_key = ?)
                """,
                (group_key, group_key),
            )
        }
        rows = [row for row in rows if row.get("playlist_id") in group_ids]
    if unavailable_only:
        rows = [row for row in rows if int(row.get("unavailable_count") or 0) > 0]
    counts = {
        category: sum(1 for row in rows if _playlist_visibility_category(row) == category)
        for category in ("private", "public", "unlisted", "others", "unknown")
    }
    if visibilities is not None:
        rows = [row for row in rows if _playlist_visibility_category(row) in visibilities]
    if sort == "title_desc":
        rows.sort(key=lambda row: str(row.get("title") or row.get("playlist_id") or "").casefold(), reverse=True)
    elif sort == "newest_updated":
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    elif sort == "oldest_updated":
        rows.sort(key=lambda row: str(row.get("updated_at") or ""))
    elif sort == "most_videos":
        rows.sort(
            key=lambda row: (-int(row.get("scanned_video_count") or 0), str(row.get("title") or "").casefold())
        )
    elif sort == "most_unavailable":
        rows.sort(
            key=lambda row: (-int(row.get("unavailable_count") or 0), str(row.get("title") or "").casefold())
        )
    else:
        rows.sort(key=lambda row: str(row.get("title") or row.get("playlist_id") or "").casefold())
    limit = max(1, min(int(limit), 500))
    total = len(rows)
    offset = max(0, int(offset))
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    return {
        "results": rows[offset : offset + limit],
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


def playlist_detail_data(conn: sqlite3.Connection, playlist_id: str) -> dict[str, Any] | None:
    return next((row for row in _playlist_rows(conn) if row.get("playlist_id") == playlist_id), None)


def _video_candidate_rows(
    conn: sqlite3.Connection,
    *,
    scope: str,
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": f"%{_omni_like_pattern(query.strip())[1:-1]}%"}
    query_clause = """
      AND (
        :query = '%%'
        OR lower(COALESCE(v.title, '') || ' ' || COALESCE(v.description, '') || ' ' ||
                 COALESCE(ch.title, '') || ' ' || COALESCE(v.video_id, '')) LIKE :query ESCAPE '\\'
      )
    """
    history_cte = """
      WITH history_stats AS (
        SELECT video_id,
               COUNT(*) AS watch_count,
               MAX(COALESCE(watched_at, watch_date)) AS latest_watch_at
        FROM history_events
        GROUP BY video_id
      )
    """
    if scope == "liked":
        sql = history_cte + f"""
          SELECT v.video_id, v.title, v.title AS metadata_title, v.upload_date AS metadata_upload_date,
                 v.updated_at, '' AS playlist_id, '' AS playlist_title, 0 AS position,
                 '' AS membership_state, '' AS unavailable_kind, '' AS source_quality,
                 '' AS match_type, '' AS match_confidence, '' AS added_at,
                 COALESCE(v.is_playable, 0) AS is_playable, v.availability,
                 COALESCE(hs.watch_count, 0) AS watch_count,
                 COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                 100 AS completeness_score
          FROM videos v
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN history_stats hs ON hs.video_id = v.video_id
          WHERE upper(v.reaction) = 'L'
          {query_clause}
        """
    else:
        where = []
        if playlist_id:
            where.append("pi.playlist_id = :playlist_id")
            params["playlist_id"] = playlist_id
        if channel_id:
            where.append("v.channel_id = :channel_id")
            params["channel_id"] = channel_id
        sql = history_cte + f"""
          SELECT pi.video_id, COALESCE(v.title, 'Unavailable video') AS title,
                 COALESCE(v.title, 'Unavailable video') AS metadata_title,
                 COALESCE(v.upload_date, '') AS metadata_upload_date,
                 COALESCE(v.updated_at, pi.updated_at) AS updated_at,
                 pi.playlist_id, p.title AS playlist_title, pi.position,
                 pi.membership_state, pi.unavailable_kind, pi.source_quality,
                 pi.match_type, pi.match_confidence, COALESCE(pi.added_at, '') AS added_at,
                 COALESCE(v.is_playable, 0) AS is_playable,
                 CASE WHEN pi.video_id IS NULL THEN pi.unavailable_kind ELSE v.availability END AS availability,
                 COALESCE(hs.watch_count, 0) AS watch_count,
                 COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                 (CASE WHEN v.thumbnail_path != '' THEN 12 ELSE 0 END
                   + CASE WHEN v.title != '' THEN 8 ELSE 0 END
                   + CASE WHEN ch.title != '' THEN 5 ELSE 0 END
                   + CASE WHEN v.description != '' THEN 4 ELSE 0 END
                   + CASE WHEN COALESCE(v.is_playable, 0) = 1 THEN 2 ELSE 0 END) AS completeness_score
          FROM playlist_items pi
          JOIN playlists p ON p.playlist_id = pi.playlist_id
          LEFT JOIN videos v ON v.video_id = pi.video_id
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN history_stats hs ON hs.video_id = pi.video_id
          WHERE {' AND '.join(where) if where else '1 = 1'}
          {query_clause}
        """
    rows = [dict(row) for row in conn.execute(sql, params)]
    if playlist_id:
        return rows
    deduplicated: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        video_id = row.get("video_id") or ""
        if not video_id:
            unresolved.append(row)
            continue
        current = deduplicated.get(video_id)
        if current is None or int(row.get("completeness_score") or 0) > int(current.get("completeness_score") or 0):
            deduplicated[video_id] = row
    return [*deduplicated.values(), *unresolved]


def _video_is_unavailable(item: dict[str, Any]) -> bool:
    if not item.get("video_id") or not int(item.get("is_playable") or 0):
        return True
    status = str(item.get("recovered_status") or "")
    return status == "NOT_FOUND" or status.startswith("DELETED_")


def video_collection_data(
    conn: sqlite3.Connection,
    *,
    scope: str = "playlist",
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
    include_videos: bool = True,
    include_unavailable: bool = True,
    sort: str = "newest_added",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    candidates = _video_candidate_rows(
        conn,
        scope=scope,
        playlist_id=playlist_id,
        channel_id=channel_id,
        query=query,
    )
    available_count = sum(1 for item in candidates if not _video_is_unavailable(item))
    unavailable_count = len(candidates) - available_count
    candidates = [
        item
        for item in candidates
        if (include_unavailable if _video_is_unavailable(item) else include_videos)
    ]
    title_key = lambda item: str(item.get("metadata_title") or item.get("title") or item.get("video_id") or "").casefold()
    if sort == "oldest_added":
        candidates.sort(key=lambda item: (str(item.get("added_at") or item.get("metadata_upload_date") or ""), title_key(item)))
    elif sort == "most_watched":
        candidates.sort(key=lambda item: (-int(item.get("watch_count") or 0), title_key(item)))
    elif sort == "playlist_order":
        candidates.sort(
            key=lambda item: (
                str(item.get("playlist_title") or "").casefold(),
                int(item.get("position") or 0),
                str(item.get("video_id") or ""),
            )
        )
    elif sort == "title":
        candidates.sort(key=title_key)
    else:
        candidates.sort(
            key=lambda item: (str(item.get("added_at") or item.get("metadata_upload_date") or ""), title_key(item)),
            reverse=True,
        )
    limit = max(1, min(int(limit), 500))
    total = len(candidates)
    offset = max(0, int(offset))
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    page_candidates = candidates[offset : offset + limit]
    exact_memberships = {
        (item.get("video_id") or "", index): {
            key: item.get(key)
            for key in (
                "playlist_id",
                "playlist_title",
                "position",
                "membership_state",
                "unavailable_kind",
                "source_quality",
                "match_type",
                "match_confidence",
                "added_at",
                "availability",
            )
        }
        for index, item in enumerate(page_candidates)
    }
    wrappers = [_omni_result("video", 0, dict(item), matched_description=False) for item in page_candidates]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    results = []
    for index, wrapper in enumerate(wrappers):
        item = wrapper["item"]
        if playlist_id:
            item.update(exact_memberships.get((item.get("video_id") or "", index), {}))
            item["url"] = youtube_video_url(item.get("video_id") or "", playlist_id)
            item["playlist_url"] = youtube_playlist_url(playlist_id)
        item.pop("completeness_score", None)
        results.append(item)
    return {
        "results": results,
        "total": total,
        "counts": {"videos": available_count, "unavailable": unavailable_count},
        "limit": limit,
        "offset": offset,
    }


def channel_list_data(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    categories: set[str] | None = None,
    subscribed_only: bool = False,
    sort: str = "title",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    pattern = _omni_like_pattern(query.strip())
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM channels
            WHERE :pattern = '%%'
               OR lower(title || ' ' || channel_id || ' ' || aliases || ' ' || description || ' ' ||
                        status || ' ' || status_reason) LIKE :pattern ESCAPE '\\'
            """,
            {"pattern": pattern},
        )
    ]
    for row in rows:
        row["url"] = youtube_channel_url(row.get("channel_id") or "")
    if subscribed_only:
        rows = [row for row in rows if int(row.get("subscribed") or 0) == 1]
    def category(row: dict[str, Any]) -> str:
        if str(row.get("status") or "").lower() in {"terminated", "deleted"}:
            return "terminated"
        return "subscribed" if int(row.get("subscribed") or 0) else "non_subscribed"
    counts = {key: sum(1 for row in rows if category(row) == key) for key in ("subscribed", "non_subscribed", "terminated")}
    if categories is not None:
        rows = [row for row in rows if category(row) in categories]
    if sort == "title_desc":
        rows.sort(key=lambda row: str(row.get("title") or row.get("channel_id") or "").casefold(), reverse=True)
    elif sort == "newest_updated":
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("fetched_at") or ""), reverse=True)
    elif sort == "oldest_updated":
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("fetched_at") or ""))
    else:
        rows.sort(key=lambda row: str(row.get("title") or row.get("channel_id") or "").casefold())
    limit = max(1, min(int(limit), 500))
    total = len(rows)
    offset = max(0, int(offset))
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    return {"results": rows[offset : offset + limit], "total": total, "counts": counts, "limit": limit, "offset": offset}


def channel_detail_data(conn: sqlite3.Connection, channel_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["url"] = youtube_channel_url(channel_id)
    return item


def video_detail_data(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    exists = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if exists is None:
        return None
    wrappers = [_omni_result("video", 0, {"video_id": video_id}, matched_description=False)]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    return wrappers[0]["item"]


OMNI_SEARCH_FILTERS = {
    "videos",
    "descriptions",
    "playlist_videos",
    "history_videos",
    "unavailable_videos",
    "channels_subscribed",
    "channels_unsubscribed",
    "playlists",
}
OMNI_SEARCH_SORTS = {"relevance", "title", "newest", "oldest", "most_watched", "type"}


def _omni_like_pattern(query: str) -> str:
    escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _omni_result(kind: str, score: int, item: dict[str, Any], *, matched_description: bool) -> dict[str, Any]:
    if kind == "video":
        title = item.get("metadata_title") or item.get("title") or item.get("video_id") or "Unavailable video"
        sort_date = (
            item.get("added_at")
            or item.get("latest_watch_at")
            or item.get("metadata_upload_date")
            or item.get("updated_at")
            or ""
        )
        watch_count = int(item.get("watch_count") or 0)
    elif kind == "channel":
        title = item.get("title") or item.get("channel_id") or ""
        sort_date = item.get("updated_at") or item.get("fetched_at") or ""
        watch_count = 0
    else:
        title = item.get("title") or item.get("playlist_id") or ""
        sort_date = item.get("updated_at") or ""
        watch_count = 0
    return {
        "kind": kind,
        "score": score,
        "matchedDescription": matched_description,
        "item": item,
        "_title": str(title).casefold(),
        "_sort_date": str(sort_date),
        "_watch_count": watch_count,
    }


def _sort_omni_results(results: list[dict[str, Any]], sort: str) -> None:
    kind_rank = {"video": 0, "channel": 1, "playlist": 2}
    results.sort(key=lambda result: (result["_title"], kind_rank.get(result["kind"], 99)))
    if sort == "relevance":
        results.sort(key=lambda result: (result["score"], kind_rank.get(result["kind"], 99)))
    elif sort == "newest":
        results.sort(key=lambda result: result["_sort_date"], reverse=True)
    elif sort == "oldest":
        results.sort(key=lambda result: result["_sort_date"])
    elif sort == "most_watched":
        results.sort(key=lambda result: result["_watch_count"], reverse=True)
    elif sort == "type":
        results.sort(key=lambda result: kind_rank.get(result["kind"], 99))


def _add_omni_video_links(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    video_ids = sorted(
        {
            result["item"].get("video_id")
            for result in results
            if result["kind"] == "video" and result["item"].get("video_id")
        }
    )
    links_by_video: dict[str, list[dict[str, Any]]] = {}
    if video_ids:
        placeholders = ",".join("?" for _ in video_ids)
        for row in conn.execute(
            f"""
            SELECT DISTINCT pi.video_id, pi.playlist_id, p.title, pi.membership_state
            FROM playlist_items pi
            JOIN playlists p ON p.playlist_id = pi.playlist_id
            WHERE pi.video_id IN ({placeholders})
            ORDER BY p.title COLLATE NOCASE
            """,
            video_ids,
        ):
            links_by_video.setdefault(row["video_id"], []).append(
                {
                    "playlist_id": row["playlist_id"],
                    "title": row["title"] or row["playlist_id"],
                    "removed": row["membership_state"] == "retained_unavailable",
                }
            )
    for result in results:
        if result["kind"] != "video":
            continue
        item = result["item"]
        item["playlist_links"] = links_by_video.get(item.get("video_id") or "", item.get("playlist_links", []))


def _hydrate_omni_videos(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    video_ids = sorted(
        {
            result["item"].get("video_id")
            for result in results
            if result["kind"] == "video" and result["item"].get("video_id")
        }
    )
    if not video_ids:
        return
    placeholders = ",".join("?" for _ in video_ids)
    rows = conn.execute(
        f"""
        WITH playlist_choice AS (
          SELECT pi.*,
                 p.title AS playlist_title,
                 ROW_NUMBER() OVER (
                   PARTITION BY pi.video_id
                   ORDER BY CASE WHEN pi.membership_state = 'current' THEN 0 ELSE 1 END,
                            p.title COLLATE NOCASE,
                            pi.position
                 ) AS choice_rank
          FROM playlist_items pi
          JOIN playlists p ON p.playlist_id = pi.playlist_id
          WHERE pi.video_id IN ({placeholders})
        ),
        history_stats AS (
          SELECT video_id,
                 COUNT(*) AS watch_count,
                 GROUP_CONCAT(COALESCE(watch_date, substr(watched_at, 1, 10)), '|') AS watch_dates,
                 MAX(COALESCE(watched_at, watch_date)) AS latest_watch_at,
                 MAX(watch_progress_percent) AS watch_progress_percent,
                 MAX(watch_resume_seconds) AS watch_resume_seconds
          FROM history_events
          WHERE video_id IN ({placeholders})
          GROUP BY video_id
        )
        SELECT COALESCE(pc.playlist_id, '') AS playlist_id,
               COALESCE(pc.position, 0) AS position,
               COALESCE(pc.membership_state, '') AS membership_state,
               COALESCE(pc.unavailable_kind, '') AS unavailable_kind,
               COALESCE(pc.source_quality, '') AS source_quality,
               COALESCE(pc.match_type, '') AS match_type,
               COALESCE(pc.match_confidence, '') AS match_confidence,
               pc.added_at,
               COALESCE(pc.playlist_title, '') AS playlist_title,
               v.video_id,
               v.title,
               COALESCE(v.channel_id, '') AS channel_id,
               COALESCE(ch.title, '') AS channel,
               v.duration_text,
               COALESCE(v.is_playable, 0) AS is_playable,
               v.availability,
               v.title AS metadata_title,
               v.description AS metadata_description,
               COALESCE(v.channel_id, '') AS metadata_channel_id,
               COALESCE(ch.title, '') AS metadata_channel,
               v.duration_text AS metadata_duration,
               v.upload_date AS metadata_upload_date,
               v.thumbnail_path AS metadata_thumbnail_path,
               COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
               v.fetch_status AS metadata_fetch_status,
               v.reaction,
               COALESCE(NULLIF(v.watch_progress_percent, 0), hs.watch_progress_percent, 0) AS watch_progress_percent,
               COALESCE(NULLIF(v.watch_resume_seconds, 0), hs.watch_resume_seconds, 0) AS watch_resume_seconds,
               COALESCE(hs.watch_count, 0) AS watch_count,
               COALESCE(hs.watch_dates, '') AS watch_dates_text,
               COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
               COALESCE(vr.archivarix_status, '') AS recovered_status,
               vr.archive_capture_at,
               vr.media_available,
               v.updated_at
        FROM videos v
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
        LEFT JOIN playlist_choice pc ON pc.video_id = v.video_id AND pc.choice_rank = 1
        LEFT JOIN history_stats hs ON hs.video_id = v.video_id
        WHERE v.video_id IN ({placeholders})
        """,
        [*video_ids, *video_ids, *video_ids],
    ).fetchall()
    hydrated = {row["video_id"]: dict(row) for row in rows}
    for result in results:
        if result["kind"] != "video":
            continue
        video_id = result["item"].get("video_id") or ""
        item = hydrated.get(video_id)
        if not item:
            continue
        item["url"] = youtube_video_url(video_id, item.get("playlist_id") or "")
        item["playlist_url"] = youtube_playlist_url(item.get("playlist_id") or "")
        item["metadata_channel_url"] = youtube_channel_url(item.get("metadata_channel_id") or "")
        item["archive_url"] = wayback_video_url(video_id, item.get("archive_capture_at"))
        item["video_file_url"] = archivarix_media_url(video_id) if item.get("media_available") else ""
        item["match_label"] = playlist_match_type_label(item.get("match_type") or "")
        item["match_note"] = playlist_match_type_note(item.get("match_type") or "")
        item["watch_dates"] = [
            value for value in (item.pop("watch_dates_text", "") or "").split("|") if value
        ]
        result["item"] = item


def omni_search_data(
    conn: sqlite3.Connection,
    query: str,
    *,
    filters: set[str] | None = None,
    include_unavailable: bool = True,
    sort: str = "relevance",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    query = query.strip()
    active_filters = set(filters if filters is not None else OMNI_SEARCH_FILTERS) & OMNI_SEARCH_FILTERS
    sort = sort if sort in OMNI_SEARCH_SORTS else "relevance"
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))
    if not query:
        return {
            "query": "",
            "filters": sorted(active_filters),
            "sort": sort,
            "limit": limit,
            "offset": 0,
            "total": 0,
            "counts": {"videos": 0, "channels": 0, "playlists": 0},
            "results": [],
        }

    pattern = _omni_like_pattern(query)
    params = {"pattern": pattern}
    search_titles = "videos" in active_filters
    search_descriptions = "descriptions" in active_filters
    results: list[dict[str, Any]] = []

    if "playlists" in active_filters and (search_titles or search_descriptions):
        playlist_title_match = """
            lower(
              p.title || ' ' || COALESCE(owner.title, '') || ' ' ||
              COALESCE(p.owner_channel_id, '') || ' ' || p.visibility || ' ' || p.playlist_id
            ) LIKE :pattern ESCAPE '\\'
        """
        playlist_description_match = "lower(p.description) LIKE :pattern ESCAPE '\\'"
        playlist_matches = []
        if search_titles:
            playlist_matches.append(playlist_title_match)
        if search_descriptions:
            playlist_matches.append(playlist_description_match)
        for row in conn.execute(
            f"""
            SELECT p.*,
                   COALESCE(ps.video_count, 0) AS scanned_video_count,
                   COALESCE(ps.unavailable_count, 0) AS unavailable_count,
                   ps.scanned_at,
                   COALESCE(ps.scan_status, '') AS scan_status,
                   COALESCE(owner.title, '') AS owner_channel_title,
                   COALESCE(owner.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(owner.status, '') AS owner_channel_status,
                   CASE WHEN {playlist_title_match} THEN 1 ELSE 0 END AS title_hit
            FROM playlists p
            LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
            LEFT JOIN channels owner ON owner.channel_id = p.owner_channel_id
            WHERE {' OR '.join(f'({match})' for match in playlist_matches)}
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            item["url"] = youtube_playlist_url(item.get("playlist_id") or "")
            item["owner_channel_url"] = youtube_channel_url(item.get("owner_channel_id") or "")
            results.append(_omni_result("playlist", 2 if title_hit else 5, item, matched_description=not title_hit))

    subscribed_filters = active_filters & {"channels_subscribed", "channels_unsubscribed"}
    if subscribed_filters and (search_titles or search_descriptions):
        channel_title_match = """
            lower(
              ch.title || ' ' || ch.channel_id || ' ' || ch.aliases || ' ' ||
              ch.status
            ) LIKE :pattern ESCAPE '\\'
        """
        channel_description_match = "lower(ch.description || ' ' || ch.status_reason) LIKE :pattern ESCAPE '\\'"
        channel_matches = []
        if search_titles:
            channel_matches.append(channel_title_match)
        if search_descriptions:
            channel_matches.append(channel_description_match)
        subscription_conditions = []
        if "channels_subscribed" in subscribed_filters:
            subscription_conditions.append("ch.subscribed = 1")
        if "channels_unsubscribed" in subscribed_filters:
            subscription_conditions.append("ch.subscribed = 0")
        for row in conn.execute(
            f"""
            SELECT ch.*,
                   CASE WHEN {channel_title_match} THEN 1 ELSE 0 END AS title_hit
            FROM channels ch
            WHERE ({' OR '.join(subscription_conditions)})
              AND ({' OR '.join(f'({match})' for match in channel_matches)})
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            item["url"] = youtube_channel_url(item.get("channel_id") or "")
            results.append(_omni_result("channel", 1 if title_hit else 4, item, matched_description=not title_hit))

    search_playlist_videos = "playlist_videos" in active_filters
    search_history_videos = "history_videos" in active_filters
    allow_unavailable = include_unavailable and "unavailable_videos" in active_filters
    if (search_playlist_videos or search_history_videos) and (search_titles or search_descriptions):
        video_title_match = """
            (
              lower(
                v.title || ' ' || COALESCE(ch.title, '') || ' ' || v.video_id || ' ' ||
                v.reaction || ' ' || v.availability
              ) LIKE :pattern ESCAPE '\\'
              OR EXISTS (
                SELECT 1
                FROM playlist_items search_pi
                JOIN playlists search_p ON search_p.playlist_id = search_pi.playlist_id
                WHERE search_pi.video_id = v.video_id
                  AND lower(search_p.title) LIKE :pattern ESCAPE '\\'
              )
            )
        """
        video_description_match = "lower(v.description) LIKE :pattern ESCAPE '\\'"
        video_matches = []
        if search_titles:
            video_matches.append(video_title_match)
        if search_descriptions:
            video_matches.append(video_description_match)
        source_conditions = []
        if search_playlist_videos:
            playlist_availability = "1 = 1" if allow_unavailable else "COALESCE(v.is_playable, 0) = 1"
            source_conditions.append(
                "(EXISTS (SELECT 1 FROM playlist_items source_pi WHERE source_pi.video_id = v.video_id) "
                f"AND ({playlist_availability}))"
            )
        if search_history_videos:
            source_conditions.append("EXISTS (SELECT 1 FROM history_events source_he WHERE source_he.video_id = v.video_id)")
        for row in conn.execute(
            f"""
            WITH candidate_videos AS MATERIALIZED (
              SELECT v.video_id,
                     CASE WHEN {video_title_match} THEN 1 ELSE 0 END AS title_hit
              FROM videos v
              LEFT JOIN channels ch ON ch.channel_id = v.channel_id
              WHERE ({' OR '.join(source_conditions)})
                AND ({' OR '.join(f'({match})' for match in video_matches)})
            ),
            playlist_stats AS (
              SELECT pi.video_id,
                     MIN(COALESCE(pi.added_at, '')) AS added_at
              FROM playlist_items pi
              JOIN candidate_videos candidate ON candidate.video_id = pi.video_id
              GROUP BY pi.video_id
            ),
            history_stats AS (
              SELECT he.video_id,
                     COUNT(*) AS watch_count,
                     MAX(COALESCE(he.watched_at, he.watch_date)) AS latest_watch_at
              FROM history_events he
              JOIN candidate_videos candidate ON candidate.video_id = he.video_id
              GROUP BY he.video_id
            )
            SELECT v.video_id,
                   v.title,
                   v.title AS metadata_title,
                   v.upload_date AS metadata_upload_date,
                   v.updated_at,
                   COALESCE(ps.added_at, '') AS added_at,
                   COALESCE(hs.watch_count, 0) AS watch_count,
                   COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                   candidate.title_hit
            FROM candidate_videos candidate
            JOIN videos v ON v.video_id = candidate.video_id
            LEFT JOIN playlist_stats ps ON ps.video_id = v.video_id
            LEFT JOIN history_stats hs ON hs.video_id = v.video_id
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            results.append(_omni_result("video", 0 if title_hit else 3, item, matched_description=not title_hit))

    if search_playlist_videos and allow_unavailable and search_titles:
        for row in conn.execute(
            """
            SELECT pi.playlist_id,
                   pi.position,
                   pi.membership_state,
                   pi.unavailable_kind,
                   pi.source_quality,
                   pi.match_type,
                   pi.match_confidence,
                   pi.added_at,
                   pi.updated_at,
                   p.title AS playlist_title
            FROM playlist_items pi
            JOIN playlists p ON p.playlist_id = pi.playlist_id
            WHERE pi.video_id IS NULL
              AND lower(
                p.title || ' ' || p.playlist_id || ' ' || pi.unavailable_kind || ' unavailable video'
              ) LIKE :pattern ESCAPE '\\'
            """,
            params,
        ):
            item = dict(row)
            item.update(
                {
                    "video_id": "",
                    "title": "Unavailable video",
                    "metadata_title": "Unavailable video",
                    "metadata_description": "",
                    "metadata_thumbnail_path": "",
                    "metadata_channel_thumbnail_path": "",
                    "metadata_channel": "",
                    "metadata_channel_id": "",
                    "metadata_duration": "",
                    "is_playable": 0,
                    "availability": item.get("unavailable_kind") or "unavailable",
                    "watch_count": 0,
                    "watch_dates": [],
                    "playlist_url": youtube_playlist_url(item.get("playlist_id") or ""),
                    "playlist_links": [
                        {
                            "playlist_id": item.get("playlist_id") or "",
                            "title": item.get("playlist_title") or item.get("playlist_id") or "",
                            "removed": False,
                        }
                    ],
                }
            )
            item["match_label"] = playlist_match_type_label(item.get("match_type") or "")
            item["match_note"] = playlist_match_type_note(item.get("match_type") or "")
            results.append(_omni_result("video", 0, item, matched_description=False))

    _sort_omni_results(results, sort)
    total = len(results)
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    page = results[offset : offset + limit]
    _hydrate_omni_videos(conn, page)
    _add_omni_video_links(conn, page)
    counts = {
        "videos": sum(1 for result in results if result["kind"] == "video"),
        "channels": sum(1 for result in results if result["kind"] == "channel"),
        "playlists": sum(1 for result in results if result["kind"] == "playlist"),
    }
    for result in page:
        result.pop("_title", None)
        result.pop("_sort_date", None)
        result.pop("_watch_count", None)
    return {
        "query": query,
        "filters": sorted(active_filters),
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": total,
        "counts": counts,
        "results": page,
    }


def history_search_data(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 200,
    offset: int = 0,
    channel_id: str = "",
) -> dict[str, Any]:
    query = query.strip()
    channel_id = channel_id.strip()
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    conditions: list[str] = []
    params: list[Any] = []
    if query:
        conditions.append(
            "lower(v.title || ' ' || COALESCE(ch.title, '') || ' ' || v.video_id || ' ' || v.description || ' ' || v.upload_date) LIKE ?"
        )
        params.append(f"%{query.lower()}%")
    if channel_id:
        conditions.append("v.channel_id = ?")
        params.append(channel_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    filtered = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM history_events he
        JOIN videos v ON v.video_id = he.video_id
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        {where}
        """,
        params,
    ).fetchone()["count"]
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT he.event_id AS reconciled_id,
                   COALESCE(he.takeout_history_key, 'youtube') AS history_key,
                   COALESCE(he.youtube_ordinal, 0) AS position,
                   'Watched' AS action,
                   he.video_id,
                   v.title,
                   COALESCE(ch.title, '') AS channel,
                   he.watched_at,
                   he.watch_date,
                   he.source_type,
                   he.match_type,
                   he.time_precision AS time_quality,
                   he.youtube_ordinal,
                   COALESCE(he.takeout_history_key, '') AS takeout_history_key,
                   COALESCE(he.takeout_row_key, '') AS takeout_row_hash,
                   he.imported_at,
                   v.title AS metadata_title,
                   v.description AS metadata_description,
                   COALESCE(v.channel_id, '') AS metadata_channel_id,
                   COALESCE(ch.title, '') AS metadata_channel,
                   v.duration_text AS metadata_duration,
                   v.thumbnail_path AS metadata_thumbnail_path,
                   COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
                   v.reaction,
                   COALESCE(NULLIF(he.watch_progress_percent, 0), v.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(NULLIF(he.watch_resume_seconds, 0), v.watch_resume_seconds, 0) AS watch_resume_seconds,
                   counts.watch_count,
                   counts.watch_dates AS watch_dates_text,
                   v.fetch_status AS metadata_fetch_status
            FROM history_events he
            JOIN videos v ON v.video_id = he.video_id
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            JOIN (
              SELECT video_id, COUNT(*) AS watch_count,
                     GROUP_CONCAT(COALESCE(watch_date, substr(watched_at, 1, 10)), '|') AS watch_dates
              FROM history_events GROUP BY video_id
            ) counts ON counts.video_id = he.video_id
            {where}
            ORDER BY COALESCE(he.watched_at, he.watch_date || 'T23:59:59Z') DESC,
                     CASE WHEN he.youtube_ordinal IS NULL THEN 1 ELSE 0 END,
                     he.youtube_ordinal
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
    ]
    video_ids = sorted({row["video_id"] for row in rows})
    playlist_links: dict[str, list[dict[str, Any]]] = {}
    if video_ids:
        placeholders = ",".join("?" for _ in video_ids)
        for link in conn.execute(
            f"""
            SELECT DISTINCT pi.video_id, pi.playlist_id, p.title, pi.membership_state
            FROM playlist_items pi JOIN playlists p ON p.playlist_id = pi.playlist_id
            WHERE pi.video_id IN ({placeholders})
            ORDER BY p.title COLLATE NOCASE
            """,
            video_ids,
        ):
            playlist_links.setdefault(link["video_id"], []).append(
                {
                    "playlist_id": link["playlist_id"],
                    "title": link["title"] or link["playlist_id"],
                    "removed": link["membership_state"] == "retained_unavailable",
                }
            )
    for row in rows:
        row["url"] = youtube_video_url(row["video_id"])
        row["metadata_channel_url"] = youtube_channel_url(row.get("metadata_channel_id") or "")
        row["source_label"] = history_source_type_label(row.get("source_type") or "")
        row["time_quality_label"] = history_time_quality_label(row.get("time_quality") or "")
        row["match_label"] = history_match_type_label(row.get("match_type") or "")
        row["time_quality_note"] = history_time_quality_note(row.get("time_quality") or "")
        row["history_badges"] = [
            value
            for value in (row["time_quality_label"],)
            if value
        ]
        row["watch_dates"] = [value for value in (row.pop("watch_dates_text", "") or "").split("|") if value]
        row["playlist_links"] = playlist_links.get(row["video_id"], [])
    totals = dict(
        conn.execute(
            """
            SELECT COUNT(*) AS watch_rows, COUNT(DISTINCT video_id) AS distinct_watch_videos
            FROM history_events
            """
        ).fetchone()
    )
    return {
        "query": query,
        "channel_id": channel_id,
        "limit": limit,
        "offset": offset,
        "watch": rows,
        "totals": {**totals, "filtered_watch_rows": int(filtered or 0)},
    }


def history_activity_data(
    conn: sqlite3.Connection,
    start_date: str = "",
    end_date: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    channel_id = channel_id.strip()
    conditions = ["COALESCE(he.watch_date, substr(he.watched_at, 1, 10)) IS NOT NULL"]
    params: list[Any] = []
    if channel_id:
        conditions.append("v.channel_id = ?")
        params.append(channel_id)
    where = " AND ".join(conditions)
    daily_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT COALESCE(he.watch_date, substr(he.watched_at, 1, 10)) AS watch_date,
                   COUNT(*) AS watch_count
            FROM history_events he
            JOIN videos v ON v.video_id = he.video_id
            WHERE {where}
            GROUP BY COALESCE(he.watch_date, substr(he.watched_at, 1, 10))
            ORDER BY watch_date DESC
            """,
            params,
        )
    ]
    offset = 0
    activity: list[dict[str, Any]] = []
    for row in daily_rows:
        watch_date = row["watch_date"]
        watch_count = int(row["watch_count"] or 0)
        if (not start_date or watch_date >= start_date) and (not end_date or watch_date <= end_date):
            activity.append(
                {
                    "watch_date": watch_date,
                    "watch_count": watch_count,
                    "offset": offset,
                }
            )
        offset += watch_count
    return {"start_date": start_date, "end_date": end_date, "channel_id": channel_id, "activity": activity}
