"""Database module for tracking sent news items and preventing duplicates.

Uses SQLite with SHA-256 URL hashing and thread-safe connection management.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

try:
    from config import DB_PATH
except (ImportError, ModuleNotFoundError):
    DB_PATH = os.getenv("DB_PATH", "news_history.db")

logger = logging.getLogger(__name__)

# Thread lock for synchronizing write operations
_db_lock = threading.Lock()


def hash_url(url: str) -> str:
    """Compute the SHA-256 hexadecimal hash of a URL.

    Args:
        url: The web URL of the news item.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


@contextmanager
def get_connection(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for obtaining a configured SQLite database connection.

    Ensures proper connection closing, timeout handling, and WAL mode.

    Args:
        db_path: Path to the SQLite database file.

    Yields:
        sqlite3.Connection: Active database connection.
    """
    # Ensure directory exists if db_path contains directory components
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # WAL mode improves concurrency for read/write workloads
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize the SQLite database schema and indices.

    Creates the 'sent_news' table if it does not already exist, along with
    relevant performance indices.

    Args:
        db_path: Path to the SQLite database file.
    """
    with _db_lock:
        with get_connection(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sent_news (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url_hash TEXT UNIQUE NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        category TEXT NOT NULL,
                        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sent_news_url_hash
                    ON sent_news(url_hash);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sent_news_sent_at
                    ON sent_news(sent_at);
                    """
                )
    logger.info("Database initialized successfully at '%s'.", db_path)


def is_sent(url: str, db_path: str = DB_PATH) -> bool:
    """Check whether a news item has already been sent based on its URL hash.

    Args:
        url: The URL to check.
        db_path: Path to the SQLite database file.

    Returns:
        True if the URL has already been recorded as sent, False otherwise.
    """
    url_digest = hash_url(url)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT 1 FROM sent_news WHERE url_hash = ? LIMIT 1;",
            (url_digest,),
        )
        row = cursor.fetchone()
        return row is not None


def _normalize_title(title: str) -> str:
    """Normalize a title for fuzzy duplicate comparison.

    Strips punctuation, lowercases, and removes common filler words
    so that 'Plex patches critical flaw!' matches 'Plex Patches Critical Flaw'.
    """
    import re
    t = title.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    # Remove very short words and common filler
    stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'to', 'of', 'in', 'on', 'for', 'and', 'or', 'its', 'has', 'have', 'been', 'with', 'that', 'this'}
    words = [w for w in t.split() if w not in stop and len(w) > 1]
    return ' '.join(words)


def is_title_duplicate(title: str, db_path: str = DB_PATH) -> bool:
    """Check if a similar title was already sent (catches same story from different sources).

    Uses normalized title hashing to detect near-duplicate headlines.

    Args:
        title: The article title to check.
        db_path: Path to the SQLite database file.

    Returns:
        True if a similar title already exists in the database.
    """
    if not title or not title.strip():
        return False

    normalized = _normalize_title(title)
    title_hash = hash_url(normalized)

    with get_connection(db_path) as conn:
        # Check exact normalized title hash
        cursor = conn.execute(
            "SELECT 1 FROM sent_news WHERE url_hash = ? OR title = ? LIMIT 1;",
            (title_hash, title.strip()),
        )
        if cursor.fetchone():
            return True

        # Check if any existing title is very similar (shares 80%+ words)
        cursor = conn.execute(
            "SELECT title FROM sent_news WHERE sent_at > datetime('now', '-3 days');"
        )
        norm_words = set(normalized.split())
        if not norm_words:
            return False

        for row in cursor.fetchall():
            existing_normalized = _normalize_title(row[0] or "")
            existing_words = set(existing_normalized.split())
            if not existing_words:
                continue
            overlap = len(norm_words & existing_words)
            max_len = max(len(norm_words), len(existing_words))
            if max_len > 0 and overlap / max_len >= 0.7:
                logger.debug("Title duplicate detected: '%s' ≈ '%s'", title, row[0])
                return True

    return False


def mark_sent(
    url: str,
    title: str,
    category: str,
    db_path: str = DB_PATH,
) -> bool:
    """Record a news item as sent to prevent future duplicates.

    Args:
        url: The news article URL.
        title: The title or headline of the news item.
        category: The category identifier (e.g., 'trending_tech').
        db_path: Path to the SQLite database file.

    Returns:
        True if the record was newly inserted, False if it already existed.
    """
    url_digest = hash_url(url)
    with _db_lock:
        with get_connection(db_path) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO sent_news (url_hash, title, url, category)
                    VALUES (?, ?, ?, ?);
                    """,
                    (url_digest, title.strip(), url.strip(), category.strip()),
                )
                inserted = cursor.rowcount > 0

    if inserted:
        logger.debug("Marked news item as sent: [%s] %s", category, title)
    else:
        logger.debug("News item was already marked as sent: %s", url)

    return inserted


def cleanup_old(days: int = 30, db_path: str = DB_PATH) -> int:
    """Remove news records older than a specified number of days.

    Args:
        days: Age threshold in days. Defaults to 30.
        db_path: Path to the SQLite database file.

    Returns:
        Number of deleted rows.
    """
    if days < 0:
        raise ValueError("Days threshold must be non-negative.")

    with _db_lock:
        with get_connection(db_path) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM sent_news
                    WHERE sent_at < datetime('now', '-' || ? || ' days');
                    """,
                    (days,),
                )
                deleted_count = cursor.rowcount

    logger.info("Cleaned up %d sent_news record(s) older than %d days.", deleted_count, days)
    return deleted_count


class NewsDatabase:
    """Object-oriented wrapper around the news SQLite database."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        """Initialize the NewsDatabase instance.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self.init_db()

    def init_db(self) -> None:
        """Initialize the database schema."""
        init_db(self.db_path)

    def is_sent(self, url: str) -> bool:
        """Check if URL has already been sent."""
        return is_sent(url, self.db_path)

    def mark_sent(self, url: str, title: str, category: str) -> bool:
        """Mark URL as sent with title and category."""
        return mark_sent(url, title, category, self.db_path)

    def cleanup_old(self, days: int = 30) -> int:
        """Clean up records older than specified days."""
        return cleanup_old(days, self.db_path)
