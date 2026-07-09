
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS playlists (
  playlist_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT '',
  video_count_text TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS playlist_scans (
  playlist_id TEXT PRIMARY KEY REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  scanned_at INTEGER NOT NULL DEFAULT 0,
  video_count INTEGER NOT NULL DEFAULT 0,
  hidden_count INTEGER NOT NULL DEFAULT 0,
  scan_status TEXT NOT NULL DEFAULT '',
  scan_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_videos (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  is_playable INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS playlist_video_reconciled (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  display_position INTEGER NOT NULL,
  current_position INTEGER NOT NULL DEFAULT 0,
  snapshot_position INTEGER NOT NULL DEFAULT 0,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  is_playable INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  source_quality TEXT NOT NULL DEFAULT '',
  match_type TEXT NOT NULL DEFAULT '',
  match_confidence TEXT NOT NULL DEFAULT '',
  snapshot_key TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, display_position)
);

CREATE TABLE IF NOT EXISTS archivarix_candidates (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archive_url TEXT NOT NULL DEFAULT '',
  video_file_url TEXT NOT NULL DEFAULT '',
  query TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, video_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  source_path TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_playlists (
  snapshot_key TEXT NOT NULL REFERENCES snapshots(snapshot_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT '',
  video_order TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_key, playlist_id)
);

CREATE TABLE IF NOT EXISTS snapshot_videos (
  snapshot_key TEXT NOT NULL REFERENCES snapshots(snapshot_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL,
  playlist_title TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  video_id TEXT NOT NULL,
  added_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_key, playlist_id, position, video_id)
);

CREATE TABLE IF NOT EXISTS snapshot_video_recovery (
  snapshot_key TEXT NOT NULL,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archive_url TEXT NOT NULL DEFAULT '',
  video_file_url TEXT NOT NULL DEFAULT '',
  searched_at INTEGER NOT NULL DEFAULT 0,
  search_status TEXT NOT NULL DEFAULT '',
  search_error TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_key, video_id)
);

CREATE TABLE IF NOT EXISTS video_metadata (
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  reaction TEXT NOT NULL DEFAULT '',
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  yt_status TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '',
  subscribed INTEGER NOT NULL DEFAULT 0,
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  status_reason TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS youtube_history_occurrences (
  ordinal INTEGER NOT NULL,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ordinal)
);

CREATE TABLE IF NOT EXISTS takeout_history_occurrences (
  history_key TEXT NOT NULL,
  row_hash TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  watched_at_iso TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (history_key, row_hash)
);

CREATE TABLE IF NOT EXISTS history_reconciled (
  reconciled_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  best_watch_time TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  match_type TEXT NOT NULL DEFAULT '',
  time_quality TEXT NOT NULL DEFAULT '',
  youtube_history_key TEXT NOT NULL DEFAULT '',
  youtube_ordinal INTEGER NOT NULL DEFAULT 0,
  takeout_history_key TEXT NOT NULL DEFAULT '',
  takeout_row_hash TEXT NOT NULL DEFAULT '',
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  imported_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metadata_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
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
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_queue (
  queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_key TEXT NOT NULL UNIQUE,
  video_id TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel_title TEXT NOT NULL DEFAULT '',
  current_title TEXT NOT NULL DEFAULT '',
  metadata_source TEXT NOT NULL DEFAULT '',
  source_key TEXT NOT NULL DEFAULT '',
  playlist_count INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
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
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_scan_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
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
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
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
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_groups_parent_position ON groups(parent_key, position);
CREATE INDEX IF NOT EXISTS idx_group_playlists_position ON group_playlists(group_key, position);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_hidden ON playlist_videos(is_playable, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_playlist_video_reconciled_playlist ON playlist_video_reconciled(playlist_id, display_position);
CREATE INDEX IF NOT EXISTS idx_playlist_video_reconciled_video ON playlist_video_reconciled(video_id);
CREATE INDEX IF NOT EXISTS idx_archivarix_candidates_playlist ON archivarix_candidates(playlist_id, title);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_video ON snapshot_videos(snapshot_key, video_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_playlist ON snapshot_videos(snapshot_key, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_snapshot_video_recovery_status ON snapshot_video_recovery(snapshot_key, search_status);
CREATE INDEX IF NOT EXISTS idx_video_metadata_status ON video_metadata(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_video ON youtube_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_search ON youtube_history_occurrences(title, channel, ordinal);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_date ON youtube_history_occurrences(watch_date, video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_time ON takeout_history_occurrences(watched_at_iso, video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, time_quality);
CREATE INDEX IF NOT EXISTS idx_metadata_queue_order ON metadata_queue(priority, queue_id);
CREATE INDEX IF NOT EXISTS idx_metadata_queue_source ON metadata_queue(metadata_source, updated_at);
CREATE INDEX IF NOT EXISTS idx_worker_queue_order ON worker_queue(worker_type, priority, queue_id);
CREATE INDEX IF NOT EXISTS idx_worker_queue_task ON worker_queue(task_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_run ON metadata_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_run ON playlist_scan_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_run ON live_history_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_run ON placeholder_recovery_worker_log(run_id, created_at);
