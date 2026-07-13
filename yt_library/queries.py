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
