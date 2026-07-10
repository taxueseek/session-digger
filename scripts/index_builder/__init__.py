"""index_builder — session-digger index construction package."""
from index_builder._schema import DB_DIR, DB_PATH, FTS_TOKENIZER, init_db, SCHEMA_SESSIONS, RICH_COLUMNS, QUERY_COLUMNS
from index_builder._builder import (
    build_index, scan_sessions, detect_topic_boundaries,
    _file_fingerprint, _compute_rich_stats, _dispatch_session_stats,
    _dispatch_extract_messages, _generate_session_id,
)
