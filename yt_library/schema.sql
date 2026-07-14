PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '',
  subscribed INTEGER NOT NULL DEFAULT 0 CHECK (subscribed IN (0, 1)),
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  status_reason TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at TEXT,
  metadata_source TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS playlists (
  playlist_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  owner_channel_id TEXT REFERENCES channels(channel_id),
  visibility TEXT NOT NULL DEFAULT '',
  video_count INTEGER NOT NULL DEFAULT 0,
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS groups (
  group_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_key TEXT REFERENCES groups(group_key) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  icon TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_playlists (
  group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (group_key, playlist_id)
);

CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  channel_id TEXT REFERENCES channels(channel_id),
  duration_text TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  reaction TEXT NOT NULL DEFAULT '',
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  is_playable INTEGER CHECK (is_playable IN (0, 1)),
  availability TEXT NOT NULL DEFAULT 'unknown',
  metadata_source TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at TEXT,
  last_seen_available_at TEXT,
  last_checked_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS playlist_scans (
  playlist_id TEXT PRIMARY KEY REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  scanned_at TEXT NOT NULL,
  video_count INTEGER NOT NULL DEFAULT 0,
  unavailable_count INTEGER NOT NULL DEFAULT 0,
  scan_status TEXT NOT NULL DEFAULT '',
  scan_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  video_id TEXT REFERENCES videos(video_id),
  membership_state TEXT NOT NULL DEFAULT 'current'
    CHECK (membership_state IN ('current', 'retained_unavailable', 'unresolved_unavailable')),
  unavailable_kind TEXT NOT NULL DEFAULT '',
  source_quality TEXT NOT NULL DEFAULT 'youtube',
  match_type TEXT NOT NULL DEFAULT '',
  match_confidence TEXT NOT NULL DEFAULT '',
  added_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  PRIMARY KEY (playlist_id, position),
  CHECK (video_id IS NOT NULL OR membership_state = 'unresolved_unavailable')
);

CREATE TABLE IF NOT EXISTS video_recovery (
  video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
  archivarix_status TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  archive_capture_at TEXT,
  media_available INTEGER CHECK (media_available IN (0, 1)),
  searched_at TEXT,
  search_status TEXT NOT NULL DEFAULT '',
  search_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS history_events (
  event_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(video_id),
  watched_at TEXT,
  watch_date TEXT,
  time_precision TEXT NOT NULL CHECK (time_precision IN ('exact', 'date_only', 'unknown')),
  source_type TEXT NOT NULL DEFAULT '',
  match_type TEXT NOT NULL DEFAULT '',
  youtube_ordinal INTEGER,
  takeout_history_key TEXT,
  takeout_row_key TEXT,
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT,
  imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS metadata_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  stale_days INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS worker_queue (
  queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_key TEXT NOT NULL UNIQUE,
  worker_type TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  channel_title TEXT NOT NULL DEFAULT '',
  current_title TEXT NOT NULL DEFAULT '',
  source_key TEXT NOT NULL DEFAULT '',
  playlist_count INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_queue_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  queue_id INTEGER NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('upsert', 'remove')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_service_blocks (
  service TEXT PRIMARY KEY,
  reason_code TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  blocked_at TEXT NOT NULL,
  retry_after TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  queue_id INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS worker_queue_event_insert
AFTER INSERT ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (NEW.queue_id, 'upsert', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_event_update
AFTER UPDATE ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (NEW.queue_id, 'upsert', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_event_delete
AFTER DELETE ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (OLD.queue_id, 'remove', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_events_prune
AFTER INSERT ON worker_queue_events
WHEN NEW.event_id % 1000 = 0
BEGIN
  DELETE FROM worker_queue_events WHERE event_id < NEW.event_id - 100000;
END;

CREATE TABLE IF NOT EXISTS playlist_scan_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  stale_days INTEGER NOT NULL DEFAULT 0,
  last_playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_scan_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 1,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  queue_id INTEGER NOT NULL DEFAULT 0,
  video_id TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  recovery_status TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_groups_parent_position ON groups(parent_key, position);
CREATE INDEX IF NOT EXISTS idx_group_playlists_position ON group_playlists(group_key, position);
CREATE INDEX IF NOT EXISTS idx_channels_title ON channels(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_channels_fetch ON channels(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_videos_title ON videos(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_fetch ON videos(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_videos_availability ON videos(is_playable, availability);
CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id);
CREATE INDEX IF NOT EXISTS idx_playlist_items_state ON playlist_items(membership_state, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_video_recovery_status ON video_recovery(search_status, searched_at);
CREATE INDEX IF NOT EXISTS idx_history_events_video ON history_events(video_id);
CREATE INDEX IF NOT EXISTS idx_history_events_date ON history_events(watch_date, youtube_ordinal);
CREATE INDEX IF NOT EXISTS idx_history_events_time ON history_events(watched_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_events_takeout
  ON history_events(takeout_history_key, takeout_row_key)
  WHERE takeout_history_key IS NOT NULL AND takeout_row_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worker_queue_order ON worker_queue(worker_type, priority, queue_id);
CREATE INDEX IF NOT EXISTS idx_worker_queue_task ON worker_queue(task_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_run ON metadata_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_run ON playlist_scan_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_run ON live_history_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_run ON placeholder_recovery_worker_log(run_id, created_at);
