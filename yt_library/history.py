"""History evidence identity helpers."""


def history_source_type_for_identity(
    my_activity_event_id: str | None,
    takeout_history_key: str | None,
    youtube_ordinal: int | None,
) -> str:
    has_my_activity = bool(my_activity_event_id)
    has_takeout = bool(takeout_history_key)
    has_youtube = youtube_ordinal is not None
    if has_my_activity and has_takeout and has_youtube:
        return "takeout_my_activity_youtube"
    if has_my_activity and has_takeout:
        return "takeout_my_activity"
    if has_my_activity and has_youtube:
        return "my_activity_youtube"
    if has_takeout and has_youtube:
        return "takeout_youtube"
    if has_my_activity:
        return "my_activity"
    if has_takeout:
        return "takeout"
    return "youtube"


def history_match_type_for_identity(
    my_activity_event_id: str | None,
    takeout_history_key: str | None,
    youtube_ordinal: int | None,
) -> str:
    has_my_activity = bool(my_activity_event_id)
    has_takeout = bool(takeout_history_key)
    has_youtube = youtube_ordinal is not None
    if has_my_activity and has_takeout and has_youtube:
        return "video_id_time_date"
    if has_my_activity and has_takeout:
        return "video_id_time"
    if has_youtube and (has_my_activity or has_takeout):
        return "video_id_date"
    if has_my_activity:
        return "my_activity_only"
    if has_takeout:
        return "takeout_only"
    return "youtube_only"
