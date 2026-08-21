"""User-authored notes and normalized tags for canonical library entities."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .time_utils import utc_now


MAX_NOTE_LENGTH = 100_000
MAX_TAG_LENGTH = 80
MAX_TAGS_PER_ENTITY = 50


@dataclass(frozen=True)
class EntityAnnotationConfig:
    table: str
    id_column: str
    tag_table: str


ENTITY_ANNOTATIONS = {
    "video": EntityAnnotationConfig("videos", "video_id", "video_tags"),
    "clip": EntityAnnotationConfig("clips", "clip_id", "clip_tags"),
    "playlist": EntityAnnotationConfig("playlists", "playlist_id", "playlist_tags"),
    "channel": EntityAnnotationConfig("channels", "channel_id", "channel_tags"),
}


def annotation_config(entity_kind: str) -> EntityAnnotationConfig:
    try:
        return ENTITY_ANNOTATIONS[entity_kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported annotation entity kind: {entity_kind}") from exc


def normalize_tag_name(value: str) -> tuple[str, str]:
    display_name = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    if not display_name:
        raise ValueError("Tags cannot be empty")
    if len(display_name) > MAX_TAG_LENGTH:
        raise ValueError(f"Tags must be {MAX_TAG_LENGTH} characters or fewer")
    return display_name, display_name.casefold()


def normalize_tags(values: Sequence[str]) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        display_name, normalized_name = normalize_tag_name(value)
        if normalized_name in seen:
            continue
        seen.add(normalized_name)
        normalized.append((display_name, normalized_name))
    if len(normalized) > MAX_TAGS_PER_ENTITY:
        raise ValueError(f"An entity can have at most {MAX_TAGS_PER_ENTITY} tags")
    return normalized


def annotation_for_entity(
    conn: sqlite3.Connection,
    entity_kind: str,
    entity_id: str,
) -> dict[str, Any] | None:
    config = annotation_config(entity_kind)
    row = conn.execute(
        f"SELECT note FROM {config.table} WHERE {config.id_column} = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    tags = [
        str(tag["name"])
        for tag in conn.execute(
            f"""
            SELECT t.name
            FROM {config.tag_table} et
            JOIN tags t ON t.tag_id = et.tag_id
            WHERE et.{config.id_column} = ?
            ORDER BY t.normalized_name
            """,
            (entity_id,),
        )
    ]
    return {"note": str(row["note"] or ""), "tags": tags}


def attach_annotations(
    conn: sqlite3.Connection,
    entity_kind: str,
    items: Sequence[dict[str, Any]],
) -> None:
    if not items:
        return
    config = annotation_config(entity_kind)
    entity_ids = sorted(
        {
            str(item.get(config.id_column) or "")
            for item in items
            if item.get(config.id_column)
        }
    )
    tags_by_id: dict[str, list[str]] = {entity_id: [] for entity_id in entity_ids}
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        for row in conn.execute(
            f"""
            SELECT et.{config.id_column} AS entity_id, t.name
            FROM {config.tag_table} et
            JOIN tags t ON t.tag_id = et.tag_id
            WHERE et.{config.id_column} IN ({placeholders})
            ORDER BY et.{config.id_column}, t.normalized_name
            """,
            entity_ids,
        ):
            tags_by_id[str(row["entity_id"])].append(str(row["name"]))
    for item in items:
        entity_id = str(item.get(config.id_column) or "")
        item["note"] = str(item.get("note") or "")
        item["tags"] = tags_by_id.get(entity_id, [])


def save_entity_annotation(
    conn: sqlite3.Connection,
    entity_kind: str,
    entity_id: str,
    note: str,
    tags: Sequence[str],
) -> dict[str, Any]:
    config = annotation_config(entity_kind)
    clean_note = str(note or "").strip()
    if len(clean_note) > MAX_NOTE_LENGTH:
        raise ValueError(f"Notes must be {MAX_NOTE_LENGTH} characters or fewer")
    normalized_tags = normalize_tags(tags)
    now = utc_now()
    with conn:
        updated = conn.execute(
            f"UPDATE {config.table} SET note = ? WHERE {config.id_column} = ?",
            (clean_note, entity_id),
        )
        if updated.rowcount == 0:
            raise KeyError(entity_id)
        conn.execute(
            f"DELETE FROM {config.tag_table} WHERE {config.id_column} = ?",
            (entity_id,),
        )
        for display_name, normalized_name in normalized_tags:
            conn.execute(
                """
                INSERT INTO tags(name, normalized_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(normalized_name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (display_name, normalized_name, now, now),
            )
            conn.execute(
                f"""
                INSERT INTO {config.tag_table}({config.id_column}, tag_id)
                SELECT ?, tag_id FROM tags WHERE normalized_name = ?
                """,
                (entity_id, normalized_name),
            )
        conn.execute(
            """
            DELETE FROM tags
            WHERE NOT EXISTS (SELECT 1 FROM video_tags WHERE video_tags.tag_id = tags.tag_id)
              AND NOT EXISTS (SELECT 1 FROM clip_tags WHERE clip_tags.tag_id = tags.tag_id)
              AND NOT EXISTS (SELECT 1 FROM playlist_tags WHERE playlist_tags.tag_id = tags.tag_id)
              AND NOT EXISTS (SELECT 1 FROM channel_tags WHERE channel_tags.tag_id = tags.tag_id)
            """
        )
    return {"note": clean_note, "tags": [name for name, _ in normalized_tags]}


def tag_suggestions(
    conn: sqlite3.Connection,
    query: str = "",
    *,
    limit: int = 25,
) -> list[str]:
    normalized_query = unicodedata.normalize("NFKC", query or "").casefold().strip()
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM tags
            WHERE normalized_name LIKE ? ESCAPE '\\'
            ORDER BY normalized_name
            LIMIT ?
            """,
            (f"%{_like_pattern(normalized_query)}%", max(1, min(100, int(limit)))),
        )
    ]


def annotation_search_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    search_notes: bool,
    search_tags: bool,
    entity_kinds: Collection[str],
) -> dict[str, set[str]]:
    kinds = {kind for kind in entity_kinds if kind in ENTITY_ANNOTATIONS}
    matches = {kind: set() for kind in kinds}
    if not query.strip() or not kinds:
        return matches
    if search_notes:
        fts_query = _fts_query(query)
        if fts_query:
            placeholders = ",".join("?" for _ in kinds)
            for row in conn.execute(
                f"""
                SELECT entity_kind, entity_id
                FROM entity_note_fts
                WHERE entity_note_fts MATCH ?
                  AND entity_kind IN ({placeholders})
                """,
                (fts_query, *sorted(kinds)),
            ):
                matches[str(row["entity_kind"])].add(str(row["entity_id"]))
    if search_tags:
        pattern = f"%{_like_pattern(unicodedata.normalize('NFKC', query).casefold())}%"
        for kind in kinds:
            config = ENTITY_ANNOTATIONS[kind]
            for row in conn.execute(
                f"""
                SELECT et.{config.id_column} AS entity_id
                FROM {config.tag_table} et
                JOIN tags t ON t.tag_id = et.tag_id
                WHERE t.normalized_name LIKE ? ESCAPE '\\'
                """,
                (pattern,),
            ):
                matches[kind].add(str(row["entity_id"]))
    return matches


def annotation_presence(item: Mapping[str, Any]) -> str:
    return "with_note" if str(item.get("note") or "").strip() else "without_note"


def _fts_query(value: str) -> str:
    tokens = re.findall(r"\w+", unicodedata.normalize("NFKC", value), flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens)


def _like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
