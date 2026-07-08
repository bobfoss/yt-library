"""Read models for the library and history web views."""

from __future__ import annotations

import sqlite3
from typing import Any

from .core import (
    history_match_type_label,
    history_source_type_label,
    history_time_quality_label,
    history_time_quality_note,
    playlist_match_type_label,
    playlist_match_type_note,
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
                   COALESCE(s.hidden_count, 0) AS hidden_count,
                   COALESCE(s.scanned_at, 0) AS scanned_at,
                   COALESCE(s.scan_status, '') AS scan_status
            FROM playlists p
            LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
            ORDER BY p.title COLLATE NOCASE
            """
        )
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
    hidden_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT v.*, p.title AS playlist_title, p.url AS playlist_url
            FROM playlist_videos v
            JOIN playlists p ON p.playlist_id = v.playlist_id
            WHERE v.is_playable = 0
            ORDER BY p.title COLLATE NOCASE, v.position
            """
        )
    ]
    channels = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM channels
            WHERE channel_id <> ''
            ORDER BY title COLLATE NOCASE, channel_id
            """
        )
    ]
    playlist_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT v.*, p.title AS playlist_title, p.url AS playlist_url,
                   v.display_position AS position,
                   COALESCE(NULLIF(NULLIF(vm.title, '- YouTube'), 'YouTube'), r.title, '') AS metadata_title,
                   COALESCE(NULLIF(vm.description, ''), r.description, '') AS metadata_description,
                   COALESCE(NULLIF(rc.title, ''), NULLIF(vc.title, ''), NULLIF(vmc.title, ''), v.channel, '') AS metadata_channel,
                   COALESCE(NULLIF(rc.url, ''), NULLIF(vc.url, ''), NULLIF(vmc.url, ''), CASE WHEN r.channel_id <> '' THEN 'https://www.youtube.com/channel/' || r.channel_id ELSE '' END, '') AS metadata_channel_url,
                   COALESCE(NULLIF(vm.duration_text, ''), r.duration_text, '') AS metadata_duration,
                   COALESCE(NULLIF(vm.upload_date, ''), r.upload_date, '') AS metadata_upload_date,
                   COALESCE(NULLIF(vm.thumbnail_path, ''), r.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(NULLIF(rc.thumbnail_path, ''), NULLIF(vc.thumbnail_path, ''), NULLIF(vmc.thumbnail_path, ''), '') AS metadata_channel_thumbnail_path,
                   COALESCE(NULLIF(vm.fetch_status, ''), r.search_status, '') AS metadata_fetch_status,
                   COALESCE(vm.reaction, '') AS reaction,
                   COALESCE(NULLIF(vm.watch_progress_percent, 0), latest_history.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(NULLIF(vm.watch_resume_seconds, 0), latest_history.watch_resume_seconds, 0) AS watch_resume_seconds,
                   COALESCE(r.status, '') AS recovered_status
            FROM playlist_video_reconciled v
            JOIN playlists p ON p.playlist_id = v.playlist_id
            LEFT JOIN video_metadata vm ON vm.video_id = v.video_id
            LEFT JOIN snapshot_video_recovery r
              ON r.snapshot_key = v.snapshot_key AND r.video_id = v.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels rc ON rc.channel_id = r.channel_id
            LEFT JOIN channels vc ON vc.channel_id = v.channel_id
            LEFT JOIN (
                SELECT hr.video_id,
                       MAX(hr.watch_progress_percent) AS watch_progress_percent,
                       MAX(hr.watch_resume_seconds) AS watch_resume_seconds
                FROM history_reconciled hr
                WHERE hr.video_id <> ''
                GROUP BY hr.video_id
            ) latest_history ON latest_history.video_id = v.video_id
            ORDER BY p.title COLLATE NOCASE, v.display_position
            """
        )
    ]
    for video in playlist_videos:
        video["match_label"] = playlist_match_type_label(video.get("match_type", ""))
        video["match_note"] = playlist_match_type_note(video.get("match_type", ""))
    archivarix_candidates = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.*, p.title AS playlist_title, p.url AS playlist_url
            FROM archivarix_candidates a
            JOIN playlists p ON p.playlist_id = a.playlist_id
            ORDER BY p.title COLLATE NOCASE, a.upload_date DESC, a.title COLLATE NOCASE
            """
        )
    ]
    snapshots = [dict(row) for row in conn.execute("SELECT * FROM snapshots ORDER BY imported_at DESC")]
    snapshot_playlists = [
        dict(row)
        for row in conn.execute(
            """
            SELECT sp.*,
                   COUNT(sv.video_id) AS video_count,
                   p.title AS current_title,
                   COALESCE(ps.video_count, 0) AS current_video_count,
                   COALESCE(ps.hidden_count, 0) AS current_hidden_count
            FROM snapshot_playlists sp
            LEFT JOIN snapshot_videos sv
              ON sv.snapshot_key = sp.snapshot_key AND sv.playlist_id = sp.playlist_id
            LEFT JOIN playlists p ON p.playlist_id = sp.playlist_id
            LEFT JOIN playlist_scans ps ON ps.playlist_id = sp.playlist_id
            GROUP BY sp.snapshot_key, sp.playlist_id
            ORDER BY sp.title COLLATE NOCASE
            """
        )
    ]
    snapshot_missing = [
        dict(row)
        for row in conn.execute(
            """
            SELECT sv.snapshot_key,
                   sv.playlist_id,
                   sv.playlist_title,
                   sv.position,
                   sv.video_id,
                   sv.added_at,
                   p.title AS current_title,
                   p.url AS playlist_url,
                   COALESCE(r.title, '') AS recovered_title,
                   COALESCE(r.description, '') AS recovered_description,
                   COALESCE(NULLIF(rc.title, ''), '') AS recovered_channel,
                   COALESCE(NULLIF(rc.url, ''), CASE WHEN r.channel_id <> '' THEN 'https://www.youtube.com/channel/' || r.channel_id ELSE '' END, '') AS recovered_channel_url,
                   COALESCE(NULLIF(rc.thumbnail_path, ''), '') AS recovered_channel_thumbnail_path,
                   COALESCE(r.status, '') AS recovered_status,
                   COALESCE(r.duration_text, '') AS recovered_duration,
                   COALESCE(r.upload_date, '') AS recovered_upload_date,
                   COALESCE(r.thumbnail_path, '') AS recovered_thumbnail_path,
                   COALESCE(r.search_status, '') AS recovery_search_status
            FROM snapshot_videos sv
            JOIN playlists p ON p.playlist_id = sv.playlist_id
            LEFT JOIN playlist_videos pv
              ON pv.playlist_id = sv.playlist_id
             AND pv.video_id = sv.video_id
             AND pv.is_playable = 1
            LEFT JOIN snapshot_video_recovery r
              ON r.snapshot_key = sv.snapshot_key
             AND r.video_id = sv.video_id
            LEFT JOIN channels rc ON rc.channel_id = r.channel_id
            WHERE pv.video_id IS NULL
            ORDER BY sv.playlist_title COLLATE NOCASE, sv.position
            """
        )
    ]
    snapshot_likely_hidden = [
        row
        for row in snapshot_missing
        if conn.execute(
            "SELECT hidden_count FROM playlist_scans WHERE playlist_id = ? AND hidden_count > 0",
            (row["playlist_id"],),
        ).fetchone()
        and (row["recovered_status"] or "").upper() != "LIVE"
    ]
    history_summary = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_reconciled) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_watch_videos
            """
        ).fetchone()
    )
    return {
        "groups": groups,
        "playlists": playlists,
        "memberships": memberships,
        "playlistVideos": playlist_videos,
        "channels": channels,
        "hiddenVideos": hidden_videos,
        "archivarixCandidates": archivarix_candidates,
        "snapshots": snapshots,
        "snapshotPlaylists": snapshot_playlists,
        "snapshotMissing": snapshot_missing,
        "snapshotLikelyHidden": snapshot_likely_hidden,
        "historySummary": history_summary,
    }


def history_search_data(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    query = query.strip()
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    like = f"%{query.lower()}%"
    if query:
        watch_where = """
            WHERE lower(
              hr.title || ' ' ||
              hr.channel || ' ' ||
              hr.video_id || ' ' ||
              COALESCE(vm.title, '') || ' ' ||
              COALESCE(vmc.title, '') || ' ' ||
              COALESCE(hc.title, '') || ' ' ||
              COALESCE(vm.upload_date, '') || ' ' ||
              COALESCE(vm.description, '')
            ) LIKE ?
        """
        watch_params: list[Any] = [like]
    else:
        watch_where = ""
        watch_params = []
    filtered_watch_rows = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM history_reconciled hr
            LEFT JOIN video_metadata vm ON vm.video_id = hr.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels hc ON hc.channel_id = hr.channel_id
            {watch_where}
            """,
            watch_params,
        ).fetchone()["count"]
    )
    watch_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT hr.reconciled_id,
                   COALESCE(NULLIF(hr.takeout_history_key, ''), hr.youtube_history_key) AS history_key,
                   hr.youtube_ordinal AS position,
                   'Watched' AS action,
                   hr.video_id,
                   hr.title,
                   hr.url,
                   hr.channel,
                   hr.best_watch_time AS watched_at,
                   hr.watch_date,
                   hr.source_type,
                   hr.match_type,
                   hr.time_quality,
                   hr.youtube_history_key,
                   hr.youtube_ordinal,
                   hr.takeout_history_key,
                   hr.takeout_row_hash,
                   hr.imported_at,
                   COALESCE(vm.title, '') AS metadata_title,
                   COALESCE(vm.description, '') AS metadata_description,
                   COALESCE(NULLIF(vmc.title, ''), NULLIF(hc.title, ''), hr.channel, '') AS metadata_channel,
                   COALESCE(NULLIF(vmc.url, ''), NULLIF(hc.url, ''), '') AS metadata_channel_url,
                   COALESCE(vm.duration_text, '') AS metadata_duration,
                   COALESCE(vm.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(NULLIF(vmc.thumbnail_path, ''), NULLIF(hc.thumbnail_path, ''), '') AS metadata_channel_thumbnail_path,
                   COALESCE(vm.reaction, '') AS reaction,
                   COALESCE(NULLIF(hr.watch_progress_percent, 0), vm.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(NULLIF(hr.watch_resume_seconds, 0), vm.watch_resume_seconds, 0) AS watch_resume_seconds,
                   COALESCE(vm.fetch_status, '') AS metadata_fetch_status
            FROM history_reconciled hr
            LEFT JOIN video_metadata vm ON vm.video_id = hr.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels hc ON hc.channel_id = hr.channel_id
            {watch_where}
            ORDER BY CASE WHEN hr.best_watch_time = '' THEN 1 ELSE 0 END,
                     hr.best_watch_time DESC,
                     hr.imported_at DESC,
                     position
            LIMIT ? OFFSET ?
            """,
            [*watch_params, limit, offset],
        )
    ]
    for row in watch_rows:
        source_label = history_source_type_label(row.get("source_type", ""))
        time_label = history_time_quality_label(row.get("time_quality", ""))
        match_label = history_match_type_label(row.get("match_type", ""))
        row["source_label"] = source_label
        row["time_quality_label"] = time_label
        row["match_label"] = match_label
        row["time_quality_note"] = history_time_quality_note(row.get("time_quality", ""))
        labels = [label for label in (source_label, time_label, match_label) if label]
        row["history_badges"] = labels
    total = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_reconciled) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_watch_videos
            """
        ).fetchone()
    )
    return {
        "query": query,
        "limit": limit,
        "offset": offset,
        "watch": watch_rows,
        "totals": {**total, "filtered_watch_rows": filtered_watch_rows},
    }
