"""Read models for the library and history web views."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import (
    archivarix_media_url,
    history_match_type_label,
    history_source_type_label,
    history_time_quality_label,
    history_time_quality_note,
    playlist_match_type_label,
    playlist_match_type_note,
    preferred_youtube_channel_reference,
    preferred_youtube_channel_url,
    wayback_video_url,
    youtube_channel_ref_from_url,
    youtube_playlist_url,
    youtube_video_url,
)




def clean_playlist_owner_name(value: str) -> str:
    value = (value or "").strip()
    return value[3:].strip() if value.lower().startswith("by ") else value


def mark_library_owner_playlists(playlists: list[dict[str, Any]]) -> None:
    explicit_channel_counts: dict[str, int] = {}
    explicit_name_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    name_counts: dict[str, int] = {}
    for playlist in playlists:
        owner_channel_id = (playlist.get("owner_channel_id") or "").strip()
        owner_name = clean_playlist_owner_name(playlist.get("owner_channel_title") or "")
        if int(playlist.get("is_library_playlist") or 0):
            if owner_channel_id:
                explicit_channel_counts[owner_channel_id] = (
                    explicit_channel_counts.get(owner_channel_id, 0) + 1
                )
            if owner_name:
                key = owner_name.casefold()
                explicit_name_counts[key] = explicit_name_counts.get(key, 0) + 1
        if (playlist.get("visibility") or "").strip():
            continue
        if owner_channel_id:
            channel_counts[owner_channel_id] = channel_counts.get(owner_channel_id, 0) + 1
        if owner_name:
            key = owner_name.casefold()
            name_counts[key] = name_counts.get(key, 0) + 1
    library_channel_id = (
        max(explicit_channel_counts, key=explicit_channel_counts.get)
        if explicit_channel_counts
        else dominant_owner_key(channel_counts)
    )
    library_owner_name = (
        max(explicit_name_counts, key=explicit_name_counts.get)
        if explicit_name_counts
        else dominant_owner_key(name_counts)
    )
    for playlist in playlists:
        owner_channel_id = (playlist.get("owner_channel_id") or "").strip()
        owner_name = clean_playlist_owner_name(playlist.get("owner_channel_title") or "")
        playlist["owner_channel_title"] = owner_name
        playlist["is_library_owner"] = int(
            bool(int(playlist.get("is_library_playlist") or 0))
            or bool(library_channel_id and owner_channel_id == library_channel_id)
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
              (SELECT COUNT(*) FROM videos) AS videos,
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
                   COALESCE(ch.aliases, '') AS owner_channel_aliases,
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
        playlist["owner_channel_reference"] = preferred_youtube_channel_reference(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
        playlist["owner_channel_url"] = preferred_youtube_channel_url(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
    mark_library_owner_playlists(rows)
    return rows


def _playlist_visibility_category(playlist: dict[str, Any]) -> str:
    visibility = str(playlist.get("visibility") or "").strip().lower()
    if visibility in {"private", "public", "unlisted"}:
        return visibility
    return "unknown"


def _playlist_ownership_category(playlist: dict[str, Any]) -> str:
    if int(playlist.get("is_library_playlist") or 0) or int(playlist.get("is_library_owner") or 0):
        return "mine"
    if str(playlist.get("owner_channel_id") or playlist.get("owner_channel_title") or "").strip():
        return "others"
    return "ownership_unknown"


def _playlist_status_category(playlist: dict[str, Any]) -> str:
    return "removed" if str(playlist.get("fetch_status") or "") == "removed" else "active"


def _playlist_list_category(playlist: dict[str, Any]) -> str:
    if _playlist_status_category(playlist) == "removed":
        return "removed"
    visibility = _playlist_visibility_category(playlist)
    if visibility != "unknown":
        return visibility
    if _playlist_ownership_category(playlist) == "others":
        return "others"
    return "unknown"


def _library_playlist_owner_identity(conn: sqlite3.Connection) -> tuple[str, str]:
    owner_rows = conn.execute(
        """
        SELECT COALESCE(p.owner_channel_id, '') AS owner_channel_id,
               CASE
                 WHEN lower(trim(COALESCE(ch.title, ''))) LIKE 'by %'
                   THEN trim(substr(trim(ch.title), 4))
                 ELSE trim(COALESCE(ch.title, ''))
               END AS owner_name,
               SUM(CASE WHEN p.is_library_playlist = 1 THEN 1 ELSE 0 END) AS explicit_count,
               SUM(CASE WHEN trim(COALESCE(p.visibility, '')) = '' THEN 1 ELSE 0 END) AS inferred_count
        FROM playlists p
        LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
        GROUP BY p.owner_channel_id, owner_name
        """
    ).fetchall()
    explicit_channel_counts: dict[str, int] = {}
    explicit_name_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    name_counts: dict[str, int] = {}
    for row in owner_rows:
        channel_id = row["owner_channel_id"]
        owner_name = row["owner_name"]
        explicit_count = int(row["explicit_count"] or 0)
        inferred_count = int(row["inferred_count"] or 0)
        if channel_id:
            explicit_channel_counts[channel_id] = (
                explicit_channel_counts.get(channel_id, 0) + explicit_count
            )
            channel_counts[channel_id] = channel_counts.get(channel_id, 0) + inferred_count
        if owner_name:
            name_key = owner_name.casefold()
            explicit_name_counts[name_key] = (
                explicit_name_counts.get(name_key, 0) + explicit_count
            )
            name_counts[name_key] = name_counts.get(name_key, 0) + inferred_count
    library_channel_id = (
        max(explicit_channel_counts, key=explicit_channel_counts.get)
        if any(explicit_channel_counts.values())
        else dominant_owner_key(channel_counts)
    )
    library_owner_name = (
        max(explicit_name_counts, key=explicit_name_counts.get)
        if any(explicit_name_counts.values())
        else dominant_owner_key(name_counts)
    )
    return library_channel_id, library_owner_name


def playlist_list_data(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    visibilities: set[str] | None = None,
    include_removed: bool = True,
    sort: str = "title",
    unavailable_only: bool = False,
    group_key: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    library_channel_id, library_owner_name = _library_playlist_owner_identity(conn)
    params: dict[str, Any] = {
        "pattern": _omni_like_pattern(query.strip()),
        "group_key": group_key.strip(),
        "unavailable_only": int(unavailable_only),
        "library_channel_id": library_channel_id,
        "library_owner_name": library_owner_name,
    }
    filtered_cte = """
        WITH playlist_rows AS (
          SELECT p.*,
                 COALESCE(s.video_count, 0) AS scanned_video_count,
                 COALESCE(s.unavailable_count, 0) AS unavailable_count,
                 s.scanned_at,
                 COALESCE(s.scan_status, '') AS scan_status,
                 CASE
                   WHEN lower(trim(COALESCE(ch.title, ''))) LIKE 'by %'
                     THEN trim(substr(trim(ch.title), 4))
                   ELSE trim(COALESCE(ch.title, ''))
                 END AS owner_channel_title,
                 COALESCE(ch.aliases, '') AS owner_channel_aliases,
                 COALESCE(ch.thumbnail_path, '') AS owner_channel_thumbnail_path,
                 COALESCE(ch.status, '') AS owner_channel_status,
                 CASE
                   WHEN p.is_library_playlist = 1 THEN 1
                   WHEN :library_channel_id <> ''
                    AND COALESCE(p.owner_channel_id, '') = :library_channel_id THEN 1
                   WHEN :library_owner_name <> ''
                    AND lower(
                      CASE
                        WHEN lower(trim(COALESCE(ch.title, ''))) LIKE 'by %'
                          THEN trim(substr(trim(ch.title), 4))
                        ELSE trim(COALESCE(ch.title, ''))
                      END
                    ) = :library_owner_name THEN 1
                   ELSE 0
                 END AS is_library_owner
          FROM playlists p
          LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
          LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
          WHERE (
              :pattern = '%%'
              OR lower(
                   p.title || ' ' || COALESCE(ch.title, '') || ' ' ||
                   COALESCE(p.owner_channel_id, '') || ' ' || p.visibility || ' ' ||
                   p.description || ' ' || p.playlist_id
                 ) LIKE :pattern ESCAPE '\\'
            )
            AND (
              :group_key = ''
              OR EXISTS (
                SELECT 1
                FROM group_playlists gp
                WHERE gp.playlist_id = p.playlist_id
                  AND (
                    gp.group_key = :group_key
                    OR gp.group_key IN (
                      SELECT group_key FROM groups WHERE parent_key = :group_key
                    )
                  )
              )
            )
            AND (:unavailable_only = 0 OR COALESCE(s.unavailable_count, 0) > 0)
        ),
        categorized AS (
          SELECT playlist_rows.*,
                 CASE
                   WHEN fetch_status = 'removed' THEN 'removed'
                   WHEN lower(trim(visibility)) IN ('private', 'public', 'unlisted')
                     THEN lower(trim(visibility))
                   WHEN is_library_owner = 0
                    AND trim(COALESCE(owner_channel_id, '') || owner_channel_title) <> ''
                     THEN 'others'
                   ELSE 'unknown'
                 END AS list_category
          FROM playlist_rows
        )
    """
    categories = ("private", "public", "unlisted", "others", "unknown", "removed")
    count_rows = conn.execute(
        filtered_cte
        + "SELECT list_category, COUNT(*) AS count FROM categorized GROUP BY list_category",
        params,
    ).fetchall()
    counts = {category: 0 for category in categories}
    counts.update({row["list_category"]: row["count"] for row in count_rows})

    category_clause = ""
    if visibilities is not None:
        selected_categories = set(visibilities)
        if include_removed:
            selected_categories.add("removed")
        selected = sorted(selected_categories & set(categories))
        if selected:
            placeholders = ", ".join(f":category_{index}" for index in range(len(selected)))
            category_clause = f"WHERE list_category IN ({placeholders})"
            params.update(
                {f"category_{index}": value for index, value in enumerate(selected)}
            )
        else:
            category_clause = "WHERE 0"
    elif not include_removed:
        category_clause = "WHERE list_category <> 'removed'"
    total = conn.execute(
        filtered_cte + f"SELECT COUNT(*) FROM categorized {category_clause}",
        params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    order_by = {
        "title_desc": "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE DESC, playlist_id",
        "newest_updated": (
            "COALESCE(updated_at, '') DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "oldest_updated": (
            "COALESCE(updated_at, ''), "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "most_videos": (
            "scanned_video_count DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "most_unavailable": (
            "unavailable_count DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
    }.get(
        sort,
        "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id",
    )
    params.update({"limit": limit, "offset": offset})
    rows = [
        dict(row)
        for row in conn.execute(
            filtered_cte
            + f"""
              SELECT * FROM categorized
              {category_clause}
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            params,
        )
    ]
    for playlist in rows:
        playlist.pop("list_category", None)
        playlist["url"] = youtube_playlist_url(playlist.get("playlist_id", ""))
        playlist["owner_channel_reference"] = preferred_youtube_channel_reference(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
        playlist["owner_channel_url"] = preferred_youtube_channel_url(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
    return {
        "results": rows,
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


def playlist_detail_data(conn: sqlite3.Connection, playlist_id: str) -> dict[str, Any] | None:
    return next((row for row in _playlist_rows(conn) if row.get("playlist_id") == playlist_id), None)


def _video_candidate_query(
    *,
    scope: str,
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
) -> tuple[str, dict[str, Any]]:
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
               MAX(COALESCE(watched_at, watch_date)) AS latest_watch_at,
               MAX(watch_progress_percent) AS watch_progress_percent
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
                 v.is_playable, v.availability,
                 COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
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
          SELECT pi.video_id, COALESCE(v.title, '') AS title,
                 COALESCE(v.title, '') AS metadata_title,
                 COALESCE(v.upload_date, '') AS metadata_upload_date,
                 COALESCE(v.updated_at, pi.updated_at) AS updated_at,
                 pi.playlist_id, p.title AS playlist_title, pi.position,
                 pi.membership_state, pi.unavailable_kind, pi.source_quality,
                 pi.match_type, pi.match_confidence, COALESCE(pi.added_at, '') AS added_at,
                 v.is_playable,
                 COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
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
    return sql, params


VIDEO_AVAILABILITY_CATEGORIES = (
    "public",
    "unlisted",
    "private",
    "members_only",
    "unavailable",
    "unknown",
)
VIDEO_COMPLETION_CATEGORIES = (
    "complete",
    "partial",
    "partial_below_minimum",
    "unknown",
    "never_watched",
)


def _bounded_partial_min_percent(value: Any) -> int:
    try:
        return max(1, min(99, int(value)))
    except (TypeError, ValueError):
        return 1


def _video_availability_category(item: dict[str, Any]) -> str:
    if not item.get("video_id"):
        return "unavailable"
    availability = str(item.get("availability") or "").strip().lower()
    if availability == "subscriber_only":
        return "members_only"
    status = str(item.get("recovered_status") or "")
    if status == "NOT_FOUND" or status.startswith("DELETED_"):
        return "unavailable"
    if availability in {"public", "unlisted"}:
        return availability
    if availability == "private":
        return (
            "private"
            if item.get("is_playable") is True or item.get("is_playable") == 1
            else "unavailable"
        )
    if availability in {
        "deleted",
        "removed",
        "unavailable",
        "needs_auth",
        "premium_only",
    }:
        return "unavailable"
    if item.get("is_playable") is True or item.get("is_playable") == 1:
        return "public"
    if item.get("is_playable") is False or item.get("is_playable") == 0:
        return "unavailable"
    return "unknown"


def _video_collection_category(item: dict[str, Any]) -> str:
    if (
        item.get("source_quality") == "takeout"
        and item.get("match_type") == "ambiguous_hidden_candidate"
    ):
        return "removed"
    return _video_availability_category(item)


def _video_completion_category(item: dict[str, Any]) -> str:
    if not item.get("video_id"):
        return "unknown"
    progress = int(item.get("watch_progress_percent") or 0)
    if progress >= 100:
        return "complete"
    if progress > 0:
        return "partial"
    if int(item.get("watch_count") or 0) > 0:
        return "unknown"
    return "never_watched"


def _video_matches_completion_filter(
    item: dict[str, Any],
    selected_filters: set[str],
    partial_min_percent: int,
) -> bool:
    return (
        _video_completion_filter_category(item, partial_min_percent)
        in selected_filters
    )


def _video_completion_filter_category(
    item: dict[str, Any],
    partial_min_percent: int,
) -> str:
    category = _video_completion_category(item)
    if (
        category == "partial"
        and int(item.get("watch_progress_percent") or 0) < partial_min_percent
    ):
        return "partial_below_minimum"
    return category


def video_collection_data(
    conn: sqlite3.Connection,
    *,
    scope: str = "playlist",
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
    include_public: bool = True,
    include_unlisted: bool = True,
    include_private: bool = True,
    include_unavailable: bool = True,
    include_members_only: bool | None = None,
    include_unknown: bool = True,
    include_removed: bool = True,
    duplicates_only: bool = False,
    completion_filters: set[str] | None = None,
    partial_min_percent: int = 1,
    sort: str = "newest_added",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    candidate_sql, params = _video_candidate_query(
        scope=scope,
        playlist_id=playlist_id,
        channel_id=channel_id,
        query=query,
    )
    selected_categories = {
        category
        for category, enabled in {
            "public": include_public,
            "unlisted": include_unlisted,
            "private": include_private,
            "unavailable": include_unavailable,
            "members_only": (
                include_unavailable
                if include_members_only is None
                else include_members_only
            ),
            "unknown": include_unknown,
            "removed": include_removed,
        }.items()
        if enabled
    }
    selected_completion_filters = (
        set(VIDEO_COMPLETION_CATEGORIES)
        if completion_filters is None
        else set(completion_filters) & set(VIDEO_COMPLETION_CATEGORIES)
    )
    partial_min_percent = _bounded_partial_min_percent(partial_min_percent)
    params["partial_min_percent"] = partial_min_percent
    collection_category_sql = """
        CASE
          WHEN source_quality = 'takeout' AND match_type = 'ambiguous_hidden_candidate'
            THEN 'removed'
          WHEN COALESCE(video_id, '') = '' THEN 'unavailable'
          WHEN lower(COALESCE(availability, '')) = 'subscriber_only' THEN 'members_only'
          WHEN lower(COALESCE(availability, '')) IN ('public', 'unlisted')
            THEN lower(availability)
          WHEN lower(COALESCE(availability, '')) = 'private' AND is_playable = 1
            THEN 'private'
          WHEN lower(COALESCE(availability, '')) IN (
            'private', 'deleted', 'removed', 'unavailable', 'needs_auth', 'premium_only'
          ) THEN 'unavailable'
          WHEN is_playable = 1 THEN 'public'
          WHEN is_playable = 0 THEN 'unavailable'
          ELSE 'unknown'
        END
    """
    completion_category_sql = """
        CASE
          WHEN COALESCE(video_id, '') = '' THEN 'unknown'
          WHEN COALESCE(watch_progress_percent, 0) >= 100 THEN 'complete'
          WHEN COALESCE(watch_progress_percent, 0) > 0
           AND COALESCE(watch_progress_percent, 0) < :partial_min_percent
            THEN 'partial_below_minimum'
          WHEN COALESCE(watch_progress_percent, 0) > 0 THEN 'partial'
          WHEN COALESCE(watch_count, 0) > 0 THEN 'unknown'
          ELSE 'never_watched'
        END
    """
    categorized_cte = f"""
        WITH raw_candidates AS MATERIALIZED (
          {candidate_sql}
        ),
        candidate_occurrences AS (
          SELECT raw_candidates.*,
                 CASE
                   WHEN COALESCE(video_id, '') = '' THEN 1
                   ELSE COUNT(*) OVER (PARTITION BY playlist_id, video_id)
                 END AS playlist_occurrence_count
          FROM raw_candidates
        ),
        categorized AS (
          SELECT candidate_occurrences.*,
                 {collection_category_sql} AS collection_category,
                 {completion_category_sql} AS completion_category,
                 CASE
                   WHEN COALESCE(video_id, '') <> '' THEN 'video:' || video_id
                   ELSE 'slot:' || playlist_id || ':' || position
                 END AS count_key
          FROM candidate_occurrences
        )
    """
    duplicate_count = 0
    if playlist_id:
        duplicate_count = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(occurrence_count), 0)
                FROM (
                  SELECT COUNT(*) AS occurrence_count
                  FROM playlist_items
                  WHERE playlist_id = ?
                    AND video_id IS NOT NULL
                  GROUP BY video_id
                  HAVING COUNT(*) > 1
                )
                """,
                (playlist_id,),
            ).fetchone()[0]
            or 0
        )
    count_rows = conn.execute(
        categorized_cte
        + """
          SELECT 'collection' AS count_type, collection_category AS category,
                 COUNT(DISTINCT count_key) AS count
          FROM categorized
          GROUP BY collection_category
          UNION ALL
          SELECT 'completion', completion_category, COUNT(DISTINCT count_key)
          FROM categorized
          GROUP BY completion_category
        """,
        params,
    ).fetchall()
    counts = {category: 0 for category in (*VIDEO_AVAILABILITY_CATEGORIES, "removed")}
    completion_counts = {category: 0 for category in VIDEO_COMPLETION_CATEGORIES}
    for row in count_rows:
        target = counts if row["count_type"] == "collection" else completion_counts
        target[row["category"]] = row["count"]

    filter_params = dict(params)
    category_placeholders = ", ".join(
        f":collection_{index}" for index in range(len(selected_categories))
    )
    filter_params.update(
        {
            f"collection_{index}": value
            for index, value in enumerate(sorted(selected_categories))
        }
    )
    completion_placeholders = ", ".join(
        f":completion_{index}" for index in range(len(selected_completion_filters))
    )
    filter_params.update(
        {
            f"completion_{index}": value
            for index, value in enumerate(sorted(selected_completion_filters))
        }
    )
    selected_clause = (
        f"collection_category IN ({category_placeholders})"
        if selected_categories
        else "0"
    )
    completion_clause = (
        f"completion_category IN ({completion_placeholders})"
        if selected_completion_filters
        else "0"
    )
    duplicate_clause = (
        "playlist_occurrence_count > 1"
        if playlist_id and duplicates_only
        else "1"
    )
    rank_partition = (
        "count_key, playlist_id, position"
        if playlist_id
        else "count_key"
    )
    page_cte = categorized_cte + f""",
        filtered AS (
          SELECT *
          FROM categorized
          WHERE {selected_clause} AND {completion_clause} AND {duplicate_clause}
        ),
        ranked AS (
          SELECT filtered.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY {rank_partition}
                   ORDER BY
                     CASE WHEN membership_state = 'current' THEN 0 ELSE 1 END,
                     completeness_score DESC
                 ) AS candidate_rank
          FROM filtered
        )
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    total = conn.execute(
        page_cte + "SELECT COUNT(*) FROM ranked WHERE candidate_rank = 1",
        filter_params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    title_sql = "COALESCE(NULLIF(metadata_title, ''), title, '') COLLATE NOCASE"
    order_by = {
        "oldest_added": (
            f"COALESCE(NULLIF(added_at, ''), metadata_upload_date, ''), {title_sql}, count_key"
        ),
        "most_watched": f"watch_count DESC, {title_sql}, count_key",
        "playlist_order": (
            "playlist_title COLLATE NOCASE, position, COALESCE(video_id, ''), count_key"
        ),
        "title": f"{title_sql}, count_key",
    }.get(
        sort,
        f"COALESCE(NULLIF(added_at, ''), metadata_upload_date, '') DESC, {title_sql} DESC, count_key",
    )
    filter_params.update({"limit": limit, "offset": offset})
    page_candidates = [
        dict(row)
        for row in conn.execute(
            page_cte
            + f"""
              SELECT *
              FROM ranked
              WHERE candidate_rank = 1
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            filter_params,
        )
    ]
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
        if scope == "playlist":
            item.update(exact_memberships.get((item.get("video_id") or "", index), {}))
            item["url"] = youtube_video_url(
                item.get("video_id") or "",
                item.get("playlist_id") or "",
            )
            item["playlist_url"] = youtube_playlist_url(item.get("playlist_id") or "")
        item.pop("completeness_score", None)
        item.pop("completion_category", None)
        item.pop("count_key", None)
        item.pop("playlist_occurrence_count", None)
        item.pop("candidate_rank", None)
        results.append(item)
    return {
        "results": results,
        "total": total,
        "counts": counts,
        "completionCounts": completion_counts,
        "duplicateCount": duplicate_count,
        "limit": limit,
        "offset": offset,
    }


def _channel_list_category(channel: dict[str, Any]) -> str:
    if _channel_status_category(channel) == "terminated":
        return "terminated"
    return _channel_subscription_category(channel)


def _channel_subscription_category(channel: dict[str, Any]) -> str:
    return "subscribed" if int(channel.get("subscribed") or 0) else "non_subscribed"


def _channel_status_category(channel: dict[str, Any]) -> str:
    return (
        "terminated"
        if str(channel.get("status") or "").lower() in {"terminated", "deleted"}
        else "active"
    )


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
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    params: dict[str, Any] = {
        "pattern": pattern,
        "subscribed_only": int(subscribed_only),
    }
    category_sql = """
        CASE
          WHEN lower(COALESCE(status, '')) IN ('terminated', 'deleted') THEN 'terminated'
          WHEN subscribed = 1 THEN 'subscribed'
          ELSE 'non_subscribed'
        END
    """
    filtered_cte = f"""
        WITH filtered AS (
          SELECT channels.*, {category_sql} AS list_category
          FROM channels
          WHERE (
              :pattern = '%%'
              OR lower(
                   title || ' ' || channel_id || ' ' || aliases || ' ' || description || ' ' ||
                   status || ' ' || status_reason
                 ) LIKE :pattern ESCAPE '\\'
            )
            AND (:subscribed_only = 0 OR subscribed = 1)
        )
    """
    count_rows = conn.execute(
        filtered_cte + "SELECT list_category, COUNT(*) AS count FROM filtered GROUP BY list_category",
        params,
    ).fetchall()
    counts = {
        key: 0 for key in ("subscribed", "non_subscribed", "terminated")
    }
    counts.update({row["list_category"]: row["count"] for row in count_rows})

    category_clause = ""
    if categories is not None:
        selected = sorted(set(categories) & set(counts))
        if selected:
            placeholders = ", ".join(f":category_{index}" for index in range(len(selected)))
            category_clause = f"WHERE list_category IN ({placeholders})"
            params.update(
                {f"category_{index}": value for index, value in enumerate(selected)}
            )
        else:
            category_clause = "WHERE 0"

    total = conn.execute(
        filtered_cte + f"SELECT COUNT(*) FROM filtered {category_clause}",
        params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    order_by = {
        "title_desc": "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE DESC, channel_id",
        "newest_updated": (
            "COALESCE(subscribed_at, first_seen_at, updated_at, fetched_at, '') DESC, "
            "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id"
        ),
        "oldest_updated": (
            "COALESCE(subscribed_at, first_seen_at, updated_at, fetched_at, ''), "
            "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id"
        ),
    }.get(
        sort,
        "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id",
    )
    params.update({"limit": limit, "offset": offset})
    rows = [
        dict(row)
        for row in conn.execute(
            filtered_cte
            + f"""
              SELECT * FROM filtered
              {category_clause}
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            params,
        )
    ]
    for row in rows:
        row.pop("list_category", None)
        row["preferred_reference"] = preferred_youtube_channel_reference(
            row.get("channel_id") or "",
            row.get("aliases") or "",
        )
        row["url"] = preferred_youtube_channel_url(
            row.get("channel_id") or "",
            row.get("aliases") or "",
        )
    return {
        "results": rows,
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


def resolve_channel_id(conn: sqlite3.Connection, channel_reference: str) -> str:
    channel_reference = (channel_reference or "").strip()
    if not channel_reference:
        return ""
    direct = conn.execute(
        "SELECT channel_id FROM channels WHERE channel_id = ?",
        (channel_reference,),
    ).fetchone()
    if direct is not None:
        return str(direct["channel_id"])
    wanted = youtube_channel_ref_from_url(channel_reference) or channel_reference
    for row in conn.execute(
        "SELECT channel_id, aliases FROM channels WHERE trim(aliases) <> ''"
    ):
        if preferred_youtube_channel_reference("", row["aliases"]).casefold() == wanted.casefold():
            return str(row["channel_id"])
    return ""


def channel_detail_data(conn: sqlite3.Connection, channel_reference: str) -> dict[str, Any] | None:
    channel_id = resolve_channel_id(conn, channel_reference)
    if not channel_id:
        return None
    row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["preferred_reference"] = preferred_youtube_channel_reference(
        channel_id,
        item.get("aliases") or "",
    )
    item["url"] = preferred_youtube_channel_url(channel_id, item.get("aliases") or "")
    return item


def video_detail_data(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    exists = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if exists is None:
        return None
    wrappers = [_omni_result("video", 0, {"video_id": video_id}, matched_description=False)]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    return wrappers[0]["item"]


def video_summaries_data(
    conn: sqlite3.Connection,
    video_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    normalized_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    wrappers = [
        _omni_result("video", 0, {"video_id": video_id}, matched_description=False)
        for video_id in normalized_ids
    ]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    return {
        "videos": [
            wrapper["item"]
            for wrapper in wrappers
            if "metadata_title" in wrapper["item"]
        ]
    }


OMNI_SEARCH_FIELDS = {"titles", "descriptions"}
OMNI_SEARCH_SORTS = {"relevance", "title", "newest", "oldest", "most_watched", "type"}
OMNI_SEARCH_KIND_ORDER = ("video", "playlist", "channel")
OMNI_SEARCH_META_FILTERS = {
    "video": VIDEO_AVAILABILITY_CATEGORIES,
    "playlist": ("private", "public", "unlisted", "unknown"),
}
OMNI_SEARCH_REACTION_FILTERS = ("none", "liked", "disliked")
OMNI_SEARCH_COMPLETION_FILTERS = VIDEO_COMPLETION_CATEGORIES
OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS = ("member", "non_member")
OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS = ("mine", "others", "ownership_unknown")
OMNI_SEARCH_PLAYLIST_STATUS_FILTERS = ("active", "removed")
OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS = ("subscribed", "non_subscribed")
OMNI_SEARCH_CHANNEL_STATUS_FILTERS = ("active", "terminated")


def _omni_like_pattern(query: str) -> str:
    escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _date_only_sort_at(value: str, timezone_name: str) -> str:
    if len(value) != 10:
        return value
    try:
        local_date = datetime.fromisoformat(value).date()
        zone = ZoneInfo(timezone_name or "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        return value
    local_end = datetime.combine(local_date, time(23, 59, 59), tzinfo=zone)
    return local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _omni_result(
    kind: str,
    score: int,
    item: dict[str, Any],
    *,
    matched_description: bool,
    display_timezone: str = "UTC",
) -> dict[str, Any]:
    sort_date_fallback = False
    if kind == "video":
        title = item.get("metadata_title") or item.get("title") or ""
        latest_watch_at = item.get("latest_watch_at") or ""
        latest_watch_sort_at = _date_only_sort_at(latest_watch_at, display_timezone)
        sort_date = (
            latest_watch_sort_at
            or item.get("added_at")
            or item.get("metadata_upload_date")
            or item.get("updated_at")
            or ""
        )
        sort_date_fallback = not bool(latest_watch_at)
        watch_count = int(item.get("watch_count") or 0)
    elif kind == "channel":
        title = item.get("title") or item.get("channel_id") or ""
        subscribed_at = item.get("subscribed_at") or ""
        first_seen_at = item.get("first_seen_at") or ""
        sort_date = subscribed_at or first_seen_at or item.get("updated_at") or item.get("fetched_at") or ""
        sort_date_fallback = not bool(subscribed_at or first_seen_at)
        watch_count = 0
    else:
        title = item.get("title") or item.get("playlist_id") or ""
        newest_video_upload_date = item.get("newest_video_upload_date") or ""
        sort_date = newest_video_upload_date
        sort_date_fallback = not bool(newest_video_upload_date)
        watch_count = 0
    return {
        "kind": kind,
        "score": score,
        "matchedDescription": matched_description,
        "item": item,
        "_title": str(title).casefold(),
        "_sort_date": str(sort_date),
        "_sort_date_fallback": sort_date_fallback,
        "_watch_count": watch_count,
        "_history_ordinal": int(item.get("latest_youtube_ordinal") or 0),
    }


def _sort_omni_results(results: list[dict[str, Any]], sort: str) -> None:
    kind_rank = {kind: rank for rank, kind in enumerate(OMNI_SEARCH_KIND_ORDER)}
    results.sort(key=lambda result: (result["_title"], kind_rank.get(result["kind"], 99)))
    if sort == "relevance":
        results.sort(key=lambda result: (result["score"], kind_rank.get(result["kind"], 99)))
    elif sort == "newest":
        results.sort(
            key=lambda result: (
                not bool(result["_history_ordinal"]),
                result["_history_ordinal"] or 0,
            )
        )
        results.sort(key=lambda result: result["_sort_date"], reverse=True)
        results.sort(key=lambda result: result["_sort_date_fallback"])
    elif sort == "oldest":
        results.sort(key=lambda result: result["_sort_date"])
    elif sort == "most_watched":
        results.sort(key=lambda result: result["_watch_count"], reverse=True)
    elif sort == "type":
        results.sort(key=lambda result: kind_rank.get(result["kind"], 99))


def _selected_omni_meta_filters(
    selected: set[str] | None,
    kind: str,
) -> set[str]:
    allowed = set(OMNI_SEARCH_META_FILTERS[kind])
    return allowed if selected is None else selected & allowed


def _assign_omni_meta_categories(
    conn: sqlite3.Connection,
    results: list[dict[str, Any]],
) -> None:
    playlist_categories = {
        row["playlist_id"]: (
            _playlist_visibility_category(row),
            _playlist_ownership_category(row),
            _playlist_status_category(row),
        )
        for row in _playlist_rows(conn)
    } if any(result["kind"] == "playlist" for result in results) else {}
    for result in results:
        item = result["item"]
        if result["kind"] == "video":
            category = _video_availability_category(item)
        elif result["kind"] == "channel":
            result["channelSubscription"] = _channel_subscription_category(item)
            result["channelStatus"] = _channel_status_category(item)
            continue
        else:
            category, ownership, status = playlist_categories.get(
                item.get("playlist_id") or "",
                (
                    _playlist_visibility_category(item),
                    _playlist_ownership_category(item),
                    _playlist_status_category(item),
                ),
            )
            result["playlistOwnership"] = ownership
            result["playlistStatus"] = status
        result["metaCategory"] = category


def _omni_meta_counts(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {
        f"{kind}s": {
            "total": 0,
            **{category: 0 for category in categories},
        }
        for kind, categories in OMNI_SEARCH_META_FILTERS.items()
    }
    counts["channels"] = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS},
        **{category: 0 for category in OMNI_SEARCH_CHANNEL_STATUS_FILTERS},
    }
    counts["playlists"].update(
        {
            **{category: 0 for category in OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS},
            **{category: 0 for category in OMNI_SEARCH_PLAYLIST_STATUS_FILTERS},
        }
    )
    for result in results:
        group = counts[f"{result['kind']}s"]
        group["total"] += 1
        if result["kind"] == "channel":
            group[result["channelSubscription"]] += 1
            group[result["channelStatus"]] += 1
        elif result["kind"] == "playlist":
            group[result["metaCategory"]] += 1
            group[result["playlistOwnership"]] += 1
            group[result["playlistStatus"]] += 1
        else:
            group[result["metaCategory"]] += 1
    return counts


def _omni_video_reaction_category(result: dict[str, Any]) -> str:
    reaction = str(result["item"].get("reaction") or "").strip().upper()
    if reaction == "L":
        return "liked"
    if reaction == "D":
        return "disliked"
    return "none"


def _omni_reaction_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_REACTION_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_reaction_category(result)] += 1
    return counts


def _omni_video_completion_category(
    result: dict[str, Any],
    partial_min_percent: int = 1,
) -> str:
    return _video_completion_filter_category(
        result["item"],
        partial_min_percent,
    )


def _omni_completion_counts(
    results: list[dict[str, Any]],
    partial_min_percent: int = 1,
) -> dict[str, int]:
    partial_min_percent = _bounded_partial_min_percent(partial_min_percent)
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_COMPLETION_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_completion_category(result, partial_min_percent)] += 1
    return counts


def _omni_video_playlist_membership_category(result: dict[str, Any]) -> str:
    item = result["item"]
    if not item.get("video_id") or int(item.get("playlist_count") or 0) > 0:
        return "member"
    return "non_member"


def _omni_playlist_membership_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_playlist_membership_category(result)] += 1
    return counts


def _playlist_links_by_video(
    conn: sqlite3.Connection,
    video_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
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
    return links_by_video


def _add_video_playlist_links(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
) -> None:
    video_ids = sorted({item.get("video_id") for item in items if item.get("video_id")})
    links_by_video = _playlist_links_by_video(conn, video_ids)
    for item in items:
        item["playlist_links"] = links_by_video.get(
            item.get("video_id") or "",
            item.get("playlist_links", []),
        )


def _add_omni_video_links(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    _add_video_playlist_links(
        conn,
        [result["item"] for result in results if result["kind"] == "video"],
    )


def _hydrate_video_identity(item: dict[str, Any], playlist_id: str = "") -> None:
    video_id = item.get("video_id") or ""
    item["url"] = youtube_video_url(video_id, playlist_id)
    item["metadata_channel_reference"] = preferred_youtube_channel_reference(
        item.get("metadata_channel_id") or "",
        item.get("metadata_channel_aliases") or "",
    )
    item["metadata_channel_url"] = preferred_youtube_channel_url(
        item.get("metadata_channel_id") or "",
        item.get("metadata_channel_aliases") or "",
    )
    item["watch_dates"] = [
        value for value in (item.pop("watch_dates_text", "") or "").split("|") if value
    ]


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
               COALESCE(ch.aliases, '') AS metadata_channel_aliases,
               v.duration_text AS metadata_duration,
               v.upload_date AS metadata_upload_date,
               v.thumbnail_path AS metadata_thumbnail_path,
               COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
               v.fetch_status AS metadata_fetch_status,
               v.reaction,
               COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
               COALESCE(hs.watch_resume_seconds, 0) AS watch_resume_seconds,
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
        hydrated_item = hydrated.get(video_id)
        if not hydrated_item:
            continue
        item = dict(hydrated_item)
        if "collection_category" in result["item"]:
            item["collection_category"] = result["item"]["collection_category"]
        _hydrate_video_identity(item, item.get("playlist_id") or "")
        item["playlist_url"] = youtube_playlist_url(item.get("playlist_id") or "")
        item["archive_url"] = wayback_video_url(video_id, item.get("archive_capture_at"))
        item["video_file_url"] = archivarix_media_url(video_id) if item.get("media_available") else ""
        item["match_label"] = playlist_match_type_label(item.get("match_type") or "")
        item["match_note"] = playlist_match_type_note(item.get("match_type") or "")
        result["item"] = item


def omni_search_data(
    conn: sqlite3.Connection,
    query: str,
    *,
    search_fields: set[str] | None = None,
    result_kinds: set[str] | None = None,
    playlist_group_key: str = "",
    video_source: str = "",
    channel_source: str = "",
    video_meta_filters: set[str] | None = None,
    video_reaction_filters: set[str] | None = None,
    video_completion_filters: set[str] | None = None,
    video_partial_min_percent: int = 1,
    video_playlist_membership_filters: set[str] | None = None,
    channel_subscription_filters: set[str] | None = None,
    channel_status_filters: set[str] | None = None,
    playlist_meta_filters: set[str] | None = None,
    playlist_ownership_filters: set[str] | None = None,
    playlist_status_filters: set[str] | None = None,
    sort: str | None = None,
    limit: int = 100,
    offset: int = 0,
    display_timezone: str = "UTC",
) -> dict[str, Any]:
    query = query.strip()
    active_result_kinds = (
        set(OMNI_SEARCH_KIND_ORDER)
        if result_kinds is None
        else set(result_kinds) & set(OMNI_SEARCH_KIND_ORDER)
    )
    playlist_group_key = playlist_group_key.strip()
    video_source = video_source if video_source in {"playlist_member", "liked"} else ""
    channel_source = channel_source if channel_source in {"subscribed", "terminated"} else ""
    active_search_fields = set(
        search_fields if search_fields is not None else OMNI_SEARCH_FIELDS
    ) & OMNI_SEARCH_FIELDS
    default_sort = "relevance" if query else "newest"
    sort = sort if sort in OMNI_SEARCH_SORTS else default_sort
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))
    pattern = _omni_like_pattern(query) if query else "%"
    params = {
        "pattern": pattern,
        "playlist_group_key": playlist_group_key,
        "video_source": video_source,
        "channel_source": channel_source,
    }
    search_titles = "titles" in active_search_fields
    search_descriptions = "descriptions" in active_search_fields
    results: list[dict[str, Any]] = []

    if "playlist" in active_result_kinds and (not query or search_titles or search_descriptions):
        playlist_title_match = """
            lower(
              p.title || ' ' || COALESCE(owner.title, '') || ' ' ||
              COALESCE(p.owner_channel_id, '') || ' ' || p.visibility || ' ' || p.playlist_id
            ) LIKE :pattern ESCAPE '\\'
        """
        playlist_description_match = "lower(p.description) LIKE :pattern ESCAPE '\\'"
        playlist_matches = []
        if query:
            if search_titles:
                playlist_matches.append(playlist_title_match)
            if search_descriptions:
                playlist_matches.append(playlist_description_match)
        else:
            playlist_matches.append("1 = 1")
        playlist_title_hit = playlist_title_match if not query or search_titles else "0"
        for row in conn.execute(
            f"""
            SELECT p.*,
                   COALESCE(ps.video_count, 0) AS scanned_video_count,
                   COALESCE(ps.unavailable_count, 0) AS unavailable_count,
                   ps.scanned_at,
                   COALESCE(ps.scan_status, '') AS scan_status,
                   COALESCE(owner.title, '') AS owner_channel_title,
                   COALESCE(owner.aliases, '') AS owner_channel_aliases,
                   COALESCE(owner.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(owner.status, '') AS owner_channel_status,
                   COALESCE(playlist_dates.newest_video_upload_date, '') AS newest_video_upload_date,
                   CASE WHEN {playlist_title_hit} THEN 1 ELSE 0 END AS title_hit
            FROM playlists p
            LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
            LEFT JOIN channels owner ON owner.channel_id = p.owner_channel_id
            LEFT JOIN (
              SELECT pi.playlist_id,
                     MAX(NULLIF(v.upload_date, '')) AS newest_video_upload_date
              FROM playlist_items pi
              JOIN videos v ON v.video_id = pi.video_id
              GROUP BY pi.playlist_id
            ) playlist_dates ON playlist_dates.playlist_id = p.playlist_id
            WHERE ({' OR '.join(f'({match})' for match in playlist_matches)})
              AND (
                :playlist_group_key = ''
                OR EXISTS (
                  SELECT 1
                  FROM group_playlists gp
                  WHERE gp.playlist_id = p.playlist_id
                    AND (
                      gp.group_key = :playlist_group_key
                      OR gp.group_key IN (
                        SELECT group_key
                        FROM groups
                        WHERE parent_key = :playlist_group_key
                      )
                    )
                )
              )
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            item["url"] = youtube_playlist_url(item.get("playlist_id") or "")
            item["owner_channel_reference"] = preferred_youtube_channel_reference(
                item.get("owner_channel_id") or "",
                item.get("owner_channel_aliases") or "",
            )
            item["owner_channel_url"] = preferred_youtube_channel_url(
                item.get("owner_channel_id") or "",
                item.get("owner_channel_aliases") or "",
            )
            results.append(_omni_result("playlist", 2 if title_hit else 5, item, matched_description=not title_hit))

    if "channel" in active_result_kinds and (not query or search_titles or search_descriptions):
        channel_title_match = """
            lower(
              ch.title || ' ' || ch.channel_id || ' ' || ch.aliases || ' ' ||
              ch.status
            ) LIKE :pattern ESCAPE '\\'
        """
        channel_description_match = "lower(ch.description || ' ' || ch.status_reason) LIKE :pattern ESCAPE '\\'"
        channel_matches = []
        if query:
            if search_titles:
                channel_matches.append(channel_title_match)
            if search_descriptions:
                channel_matches.append(channel_description_match)
        else:
            channel_matches.append("1 = 1")
        channel_title_hit = channel_title_match if not query or search_titles else "0"
        for row in conn.execute(
            f"""
            SELECT ch.*,
                   CASE WHEN {channel_title_hit} THEN 1 ELSE 0 END AS title_hit
            FROM channels ch
            WHERE ({' OR '.join(f'({match})' for match in channel_matches)})
              AND (
                :channel_source = ''
                OR (:channel_source = 'subscribed' AND ch.subscribed = 1)
                OR (
                  :channel_source = 'terminated'
                  AND lower(COALESCE(ch.status, '')) IN ('terminated', 'deleted')
                )
              )
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            item["preferred_reference"] = preferred_youtube_channel_reference(
                item.get("channel_id") or "",
                item.get("aliases") or "",
            )
            item["url"] = preferred_youtube_channel_url(
                item.get("channel_id") or "",
                item.get("aliases") or "",
            )
            results.append(_omni_result("channel", 1 if title_hit else 4, item, matched_description=not title_hit))

    if "video" in active_result_kinds and (not query or search_titles or search_descriptions):
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
        if query:
            if search_titles:
                video_matches.append(video_title_match)
            if search_descriptions:
                video_matches.append(video_description_match)
        else:
            video_matches.append("1 = 1")
        video_title_hit = video_title_match if not query or search_titles else "0"
        for row in conn.execute(
            f"""
            WITH candidate_videos AS MATERIALIZED (
              SELECT v.video_id,
                     CASE WHEN {video_title_hit} THEN 1 ELSE 0 END AS title_hit,
                     v.is_playable,
                     v.availability,
                     COALESCE(vr.archivarix_status, '') AS recovered_status
              FROM videos v
              LEFT JOIN channels ch ON ch.channel_id = v.channel_id
              LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
              WHERE ({' OR '.join(f'({match})' for match in video_matches)})
                AND (
                  :video_source = ''
                  OR (:video_source = 'liked' AND upper(COALESCE(v.reaction, '')) = 'L')
                  OR (
                    :video_source = 'playlist_member'
                    AND EXISTS (
                      SELECT 1
                      FROM playlist_items source_pi
                      WHERE source_pi.video_id = v.video_id
                    )
                  )
                )
            ),
            playlist_stats AS (
              SELECT pi.video_id,
                     MIN(COALESCE(pi.added_at, '')) AS added_at,
                     COUNT(*) AS playlist_count
              FROM playlist_items pi
              JOIN candidate_videos candidate ON candidate.video_id = pi.video_id
              GROUP BY pi.video_id
            ),
            history_stats AS (
              SELECT he.video_id,
                     COUNT(*) AS watch_count,
                     MAX(COALESCE(he.watched_at, he.watch_date)) AS latest_watch_at,
                     MAX(he.watch_progress_percent) AS watch_progress_percent
              FROM history_events he
              JOIN candidate_videos candidate ON candidate.video_id = he.video_id
              GROUP BY he.video_id
            ),
            latest_history_position AS (
              SELECT he.video_id,
                     he.youtube_ordinal,
                     ROW_NUMBER() OVER (
                       PARTITION BY he.video_id
                       ORDER BY COALESCE(he.watched_at, he.watch_date) DESC,
                                he.youtube_ordinal ASC
                     ) AS position_rank
              FROM history_events he
              JOIN candidate_videos candidate ON candidate.video_id = he.video_id
              WHERE he.youtube_ordinal IS NOT NULL
            )
            SELECT v.video_id,
                   v.title,
                   v.title AS metadata_title,
                   v.upload_date AS metadata_upload_date,
                   v.updated_at,
                   v.reaction,
                   COALESCE(ps.added_at, '') AS added_at,
                   COALESCE(ps.playlist_count, 0) AS playlist_count,
                   COALESCE(hs.watch_count, 0) AS watch_count,
                   COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                   COALESCE(lhp.youtube_ordinal, 0) AS latest_youtube_ordinal,
                   COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
                   candidate.title_hit,
                   candidate.is_playable,
                   candidate.availability,
                   candidate.recovered_status
            FROM candidate_videos candidate
            JOIN videos v ON v.video_id = candidate.video_id
            LEFT JOIN playlist_stats ps ON ps.video_id = v.video_id
            LEFT JOIN history_stats hs ON hs.video_id = v.video_id
            LEFT JOIN latest_history_position lhp
              ON lhp.video_id = v.video_id AND lhp.position_rank = 1
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            item["collection_category"] = _video_availability_category(item)
            results.append(
                _omni_result(
                    "video",
                    0 if title_hit else 3,
                    item,
                    matched_description=not title_hit,
                    display_timezone=display_timezone,
                )
            )

    if (
        "video" in active_result_kinds
        and video_source != "liked"
        and (not query or search_titles)
    ):
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
                    "reaction": "",
                    "is_playable": 0,
                    "availability": item.get("unavailable_kind") or "unavailable",
                    "playlist_count": 1,
                    "watch_count": 0,
                    "watch_dates": [],
                    "collection_category": "unavailable",
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

    _assign_omni_meta_categories(conn, results)
    meta_counts = _omni_meta_counts(results)
    reaction_counts = _omni_reaction_counts(results)
    video_partial_min_percent = _bounded_partial_min_percent(
        video_partial_min_percent
    )
    completion_counts = _omni_completion_counts(results, video_partial_min_percent)
    playlist_membership_counts = _omni_playlist_membership_counts(results)
    selected_meta_filters = {
        "video": _selected_omni_meta_filters(video_meta_filters, "video"),
        "playlist": _selected_omni_meta_filters(playlist_meta_filters, "playlist"),
    }
    selected_reaction_filters = (
        set(OMNI_SEARCH_REACTION_FILTERS)
        if video_reaction_filters is None
        else set(video_reaction_filters) & set(OMNI_SEARCH_REACTION_FILTERS)
    )
    selected_completion_filters = (
        set(OMNI_SEARCH_COMPLETION_FILTERS)
        if video_completion_filters is None
        else set(video_completion_filters) & set(OMNI_SEARCH_COMPLETION_FILTERS)
    )
    selected_playlist_membership_filters = (
        set(OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS)
        if video_playlist_membership_filters is None
        else set(video_playlist_membership_filters)
        & set(OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS)
    )
    selected_playlist_ownership_filters = (
        set(OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS)
        if playlist_ownership_filters is None
        else set(playlist_ownership_filters) & set(OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS)
    )
    selected_playlist_status_filters = (
        set(OMNI_SEARCH_PLAYLIST_STATUS_FILTERS)
        if playlist_status_filters is None
        else set(playlist_status_filters) & set(OMNI_SEARCH_PLAYLIST_STATUS_FILTERS)
    )
    selected_channel_subscription_filters = (
        set(OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS)
        if channel_subscription_filters is None
        else set(channel_subscription_filters)
        & set(OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS)
    )
    selected_channel_status_filters = (
        set(OMNI_SEARCH_CHANNEL_STATUS_FILTERS)
        if channel_status_filters is None
        else set(channel_status_filters) & set(OMNI_SEARCH_CHANNEL_STATUS_FILTERS)
    )
    results = [
        result
        for result in results
        if (
            (
                result["kind"] == "channel"
                and result["channelSubscription"]
                in selected_channel_subscription_filters
                and result["channelStatus"] in selected_channel_status_filters
            )
            or (
                result["kind"] == "playlist"
                and result["metaCategory"] in selected_meta_filters[result["kind"]]
                and result["playlistOwnership"] in selected_playlist_ownership_filters
                and result["playlistStatus"] in selected_playlist_status_filters
            )
            or (
                result["kind"] == "video"
                and result["metaCategory"] in selected_meta_filters[result["kind"]]
                and (
                    _omni_video_reaction_category(result)
                    in selected_reaction_filters
                    and _video_matches_completion_filter(
                        result["item"],
                        selected_completion_filters,
                        video_partial_min_percent,
                    )
                    and _omni_video_playlist_membership_category(result)
                    in selected_playlist_membership_filters
                )
            )
        )
    ]
    _sort_omni_results(results, sort)
    total = len(results)
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    page = results[offset : offset + limit]
    _hydrate_omni_videos(conn, page)
    _add_omni_video_links(conn, page)
    counts = {
        "videos": sum(1 for result in results if result["kind"] == "video"),
        "playlists": sum(1 for result in results if result["kind"] == "playlist"),
        "channels": sum(1 for result in results if result["kind"] == "channel"),
    }
    for result in page:
        result.pop("_title", None)
        result.pop("_sort_date", None)
        result.pop("_sort_date_fallback", None)
        result.pop("_watch_count", None)
        result.pop("_history_ordinal", None)
    return {
        "query": query,
        "searchFields": sorted(active_search_fields),
        "resultKinds": sorted(active_result_kinds),
        "playlistGroupKey": playlist_group_key,
        "videoSource": video_source,
        "channelSource": channel_source,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": total,
        "counts": counts,
        "metaCounts": meta_counts,
        "reactionCounts": reaction_counts,
        "completionCounts": completion_counts,
        "playlistMembershipCounts": playlist_membership_counts,
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
                   COALESCE(ch.aliases, '') AS metadata_channel_aliases,
                   v.duration_text AS metadata_duration,
                   v.thumbnail_path AS metadata_thumbnail_path,
                   COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
                   v.reaction,
                   v.is_playable,
                   v.availability,
                   COALESCE(he.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(he.watch_resume_seconds, 0) AS watch_resume_seconds,
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
    _add_video_playlist_links(conn, rows)
    for row in rows:
        _hydrate_video_identity(row)
        row["source_label"] = history_source_type_label(row.get("source_type") or "")
        row["time_quality_label"] = history_time_quality_label(row.get("time_quality") or "")
        row["match_label"] = history_match_type_label(row.get("match_type") or "")
        row["time_quality_note"] = history_time_quality_note(row.get("time_quality") or "")
        row["history_badges"] = [
            value
            for value in (row["time_quality_label"],)
            if value
        ]
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
