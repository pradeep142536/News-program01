"""Entry point for the Telegram News Channel automation application.

Coordinates RSS feed fetching, content filtering, Gemini AI post generation,
media acquisition, and Telegram channel publishing on an automated schedule,
while exposing Flask health and status monitoring endpoints.
"""

from __future__ import annotations

import atexit
import inspect
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify

# Ensure application directory is in sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

import config
from src import database

# Configure application logging
_log_level_str = getattr(config, "LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_str.upper(), logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("telegram_news_channel")

# Import pipeline modules with graceful fallback for parallel module development
try:
    from src import fetcher
except ImportError:
    fetcher = None  # type: ignore[assignment]

try:
    from src import processor
except ImportError:
    processor = None  # type: ignore[assignment]

try:
    from src import media
except ImportError:
    media = None  # type: ignore[assignment]

try:
    from src import sender
except ImportError:
    sender = None  # type: ignore[assignment]


# Application state
START_TIME: float = time.time()
_last_check_time: Optional[str] = None
_app_initialized: bool = False

# Scheduler instance
scheduler: BackgroundScheduler = BackgroundScheduler(daemon=True)

# Flask application
app: Flask = Flask(__name__)


def _extract_field(article: Any, field_name: str, default: str = "") -> str:
    """Extract a string attribute or dictionary key from an article object.

    Args:
        article: Article dictionary or object.
        field_name: The attribute or dictionary key to retrieve.
        default: Fallback value if field is absent or None.

    Returns:
        The extracted string value stripped of surrounding whitespace.
    """
    if isinstance(article, dict):
        val = article.get(field_name, default)
    else:
        val = getattr(article, field_name, default)
    return str(val).strip() if val is not None else default


def _ensure_pipeline_modules() -> None:
    """Dynamically load pipeline modules if they became available after import."""
    global fetcher, processor, media, sender
    if fetcher is None:
        try:
            from src import fetcher as f_mod
            fetcher = f_mod
        except ImportError:
            pass

    if processor is None:
        try:
            from src import processor as p_mod
            processor = p_mod
        except ImportError:
            pass

    if media is None:
        try:
            from src import media as m_mod
            media = m_mod
        except ImportError:
            pass

    if sender is None:
        try:
            from src import sender as s_mod
            sender = s_mod
        except ImportError:
            pass


def _dispatch_send_news_post(article: Dict[str, Any]) -> bool:
    """Send a processed news article to the Telegram channel.

    The article dict must contain 'post_text' and 'media' keys.

    Args:
        article: The processed news article with post_text and media.

    Returns:
        True if publication succeeded, False otherwise.
    """
    if sender is None:
        logger.error("Sender module is not available.")
        return False

    send_func = getattr(sender, "send_news_post", None)
    if not callable(send_func):
        logger.error("sender.send_news_post is not callable.")
        return False

    try:
        bot_token = config.TELEGRAM_BOT_TOKEN
        channel_id = config.TELEGRAM_CHANNEL_ID
        result = send_func(bot_token, channel_id, article)
        return bool(result) if result is not None else True
    except Exception as exc:
        logger.error("Error executing sender.send_news_post: %s", exc, exc_info=True)
        return False


def get_total_sent_count() -> int:
    """Query total sent news records from the database.

    Returns:
        Total count of sent records, or 0 if query fails.
    """
    try:
        with database.get_connection(config.DB_PATH) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM sent_news;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug("Failed to count sent articles from database: %s", exc)
        return 0


def get_sources_count() -> int:
    """Calculate the total count of configured news sources.

    Returns:
        Total number of active RSS feeds and scrapers.
    """
    try:
        from src import sources
        rss_count = len(getattr(sources, "RSS_FEEDS", {}))
        scholarship_count = len(getattr(sources, "SCHOLARSHIP_FEEDS", {}))
        # Additional scrapers/APIs (Hacker News, GitHub Trending, Product Hunt)
        extra_count = 3
        return rss_count + scholarship_count + extra_count
    except Exception as exc:
        logger.debug("Failed to calculate sources count: %s", exc)
        return 0


def validate_env_vars(raise_on_error: bool = False) -> bool:
    """Validate that required environment variables are configured.

    Checks for TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, and GEMINI_API_KEY.

    Args:
        raise_on_error: If True, raises ValueError when any variable is missing.

    Returns:
        True if all required variables are present, False otherwise.

    Raises:
        ValueError: If raise_on_error is True and any variable is missing.
    """
    missing: list[str] = []
    if not config.TELEGRAM_BOT_TOKEN or not str(config.TELEGRAM_BOT_TOKEN).strip():
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_CHANNEL_ID or not str(config.TELEGRAM_CHANNEL_ID).strip():
        missing.append("TELEGRAM_CHANNEL_ID")

    gemini_keys = getattr(config, "GEMINI_API_KEYS", [])
    has_gemini = bool(gemini_keys) or bool(config.GEMINI_API_KEY and str(config.GEMINI_API_KEY).strip())
    if not has_gemini:
        missing.append("GEMINI_API_KEYS (or GEMINI_API_KEY)")

    if missing:
        msg = f"Missing required environment variables: {', '.join(missing)}"
        logger.error(msg)
        if raise_on_error:
            raise ValueError(msg)
        return False
    logger.info(
        "All required environment variables validated successfully (Gemini key pool size: %d).",
        len(gemini_keys) if gemini_keys else 1,
    )
    return True


def init_database() -> None:
    """Initialize the SQLite database schema and indices."""
    try:
        database.init_db(config.DB_PATH)
        logger.info("Database initialized successfully at '%s'.", config.DB_PATH)
    except Exception as exc:
        logger.error("Failed to initialize database: %s", exc, exc_info=True)


def init_gemini() -> None:
    """Configure Google Gemini Generative AI SDK with key pool credentials."""
    gemini_keys = getattr(config, "GEMINI_API_KEYS", [])
    if not gemini_keys and config.GEMINI_API_KEY:
        gemini_keys = [config.GEMINI_API_KEY]

    if not gemini_keys:
        logger.warning("No Gemini API keys configured; Gemini features will be inactive.")
        return

    try:
        if processor is not None and hasattr(processor, "init_gemini"):
            for k in gemini_keys:
                processor.init_gemini(k)
            logger.info("Google Gemini key pool initialized with %d key(s).", len(gemini_keys))
        else:
            import google.generativeai as genai
            genai.configure(api_key=gemini_keys[0])
            logger.info("Google Gemini initialized with primary key.")
    except ImportError:
        logger.warning("google-generativeai package not installed; skipping genai initialization.")
    except Exception as exc:
        logger.error("Failed to configure Google Gemini: %s", exc, exc_info=True)


def check_and_post_news() -> int:
    """Fetch, filter, process, and broadcast news posts to the Telegram channel.

    Workflow:
        a. Call fetcher.fetch_all_news() to get raw news
        b. Check each against database (skip already sent)
        c. Call processor.process_articles() to filter/categorize/generate posts via Gemini
        d. For each processed article, get media via media.get_best_media()
        e. Call sender.send_news_post() for each
        f. Mark sent in database
        g. Log summary: "Posted X new articles"

    Returns:
        int: Number of new articles successfully posted.
    """
    global _last_check_time
    _last_check_time = datetime.now(timezone.utc).isoformat()
    logger.info("Starting news check cycle at %s...", _last_check_time)

    # Ensure dynamic modules are resolved if loaded after startup
    _ensure_pipeline_modules()

    if not all([fetcher, processor, media, sender]):
        missing = [
            mod_name
            for mod_name, mod in [
                ("fetcher", fetcher),
                ("processor", processor),
                ("media", media),
                ("sender", sender),
            ]
            if mod is None
        ]
        logger.warning(
            "News pipeline incomplete; missing module(s): %s. Skipping cycle.",
            ", ".join(missing),
        )
        logger.info("Posted 0 new articles")
        return 0

    posted_count = 0

    try:
        # a. Call fetcher.fetch_all_news() to get raw news
        logger.info("Fetching raw news from all sources...")
        raw_news = fetcher.fetch_all_news()  # type: ignore[union-attr]
        if not raw_news:
            logger.info("No raw articles retrieved.")
            logger.info("Posted 0 new articles")
            return 0
        logger.info("Retrieved %d raw news items.", len(raw_news))

        # b. Check each against database (skip already sent)
        unsent_news: List[Any] = []
        for item in raw_news:
            url = _extract_field(item, "url") or _extract_field(item, "link")
            title = _extract_field(item, "title")
            if not url:
                logger.debug("Skipping item without valid URL: %s", item)
                continue
            if database.is_sent(url):
                logger.debug("Skipping already sent article (URL match): %s", url)
                continue
            if title and hasattr(database, "is_title_duplicate") and database.is_title_duplicate(title):
                logger.debug("Skipping duplicate article (title match): %s", title)
                continue
            unsent_news.append(item)

        if not unsent_news:
            logger.info("All %d retrieved articles have already been sent.", len(raw_news))
            logger.info("Posted 0 new articles")
            return 0

        logger.info("Found %d new unsent articles to process.", len(unsent_news))

        # c. Call processor.process_articles() to filter/categorize/generate posts via Gemini
        logger.info("Processing unsent articles via Gemini...")
        processed_articles = processor.process_articles(unsent_news)  # type: ignore[union-attr]
        if not processed_articles:
            logger.info("No articles remained after processing/filtering.")
            logger.info("Posted 0 new articles")
            return 0

        logger.info("Processor prepared %d articles for publication.", len(processed_articles))

        # d, e, f. Acquire media, send post, and mark sent
        for article in processed_articles:
            try:
                # d. For each processed article, get media via media.get_best_media()
                article_url = _extract_field(article, "url") or _extract_field(article, "link")
                article_title = _extract_field(article, "title", "News")
                pexels_key = getattr(config, "PEXELS_API_KEY", "") or ""

                media_info = {"type": "none"}
                try:
                    media_info = media.get_best_media(  # type: ignore[union-attr]
                        article_url=article_url,
                        title=article_title,
                        pexels_api_key=pexels_key,
                    )
                except Exception as media_err:
                    logger.warning("Failed to obtain media for article: %s", media_err)

                # Attach media info to the article dict for sender
                article["media"] = media_info

                # e. Call sender.send_news_post() for each
                success = _dispatch_send_news_post(article)

                if success:
                    # f. Mark sent in database
                    url = _extract_field(article, "url") or _extract_field(article, "link")
                    title = _extract_field(article, "title", "Untitled News")
                    category = _extract_field(article, "category", "trending_tech")
                    if url:
                        database.mark_sent(url=url, title=title, category=category)
                    posted_count += 1
                else:
                    logger.warning(
                        "Sender failed to publish article: %s",
                        _extract_field(article, "title"),
                    )
            except Exception as item_err:
                logger.error("Error publishing article: %s", item_err, exc_info=True)

    except Exception as exc:
        logger.error("Unexpected error in check_and_post_news: %s", exc, exc_info=True)

    # g. Log summary: "Posted X new articles"
    logger.info("Posted %d new articles", posted_count)
    return posted_count


def start_scheduler() -> None:
    """Configure and start APScheduler BackgroundScheduler."""
    if not scheduler.running:
        interval_minutes = max(1, int(getattr(config, "CHECK_INTERVAL", 5) or 5))
        scheduler.add_job(
            check_and_post_news,
            trigger="interval",
            minutes=interval_minutes,
            id="check_and_post_news_job",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(),
        )
        scheduler.start()
        logger.info(
            "APScheduler started; running news check every %d minute(s).",
            interval_minutes,
        )


def shutdown_handler(signum: Any = None, frame: Any = None) -> None:
    """Handle graceful shutdown on process termination or exit signals."""
    logger.info("Graceful shutdown invoked (signal: %s)...", signum)
    if scheduler.running:
        try:
            scheduler.shutdown(wait=False)
            logger.info("APScheduler stopped successfully.")
        except Exception as exc:
            logger.warning("Error shutting down scheduler: %s", exc)


atexit.register(shutdown_handler)

try:
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
except (ValueError, AttributeError):
    pass


def init_app() -> None:
    """Execute startup sequence: validate env, init DB, init Gemini, start scheduler."""
    global _app_initialized
    if _app_initialized:
        return
    _app_initialized = True

    logger.info("Executing application startup sequence...")
    validate_env_vars(raise_on_error=False)
    init_database()
    init_gemini()
    start_scheduler()
    logger.info("Application startup sequence completed successfully.")


# Route definitions

@app.route("/", methods=["GET"])
def index() -> Response:
    """Root route returning application running status and metadata."""
    return jsonify({
        "status": "running",
        "app": "Telegram News Channel",
        "version": "1.0.0",
    })


@app.route("/health", methods=["GET"])
def health() -> Response:
    """Health check route returning application health and uptime in seconds."""
    uptime_seconds = int(time.time() - START_TIME)
    return jsonify({
        "status": "healthy",
        "uptime": uptime_seconds,
    })


@app.route("/stats", methods=["GET"])
def stats() -> Response:
    """Stats route returning publication count, check timestamp, and source count."""
    return jsonify({
        "total_sent": get_total_sent_count(),
        "last_check_time": _last_check_time,
        "sources_count": get_sources_count(),
    })


@app.route("/check", methods=["GET", "POST"])
def trigger_check() -> Response:
    """Manually trigger a news fetch-and-post cycle on demand."""
    logger.info("Manual news check cycle triggered via /check endpoint.")
    count = check_and_post_news()
    return jsonify({
        "status": "success",
        "posted_articles": count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# Run startup sequence on import so Gunicorn workers are properly initialized
init_app()


if __name__ == "__main__":
    if "--test" in sys.argv or "--run-once" in sys.argv:
        logger.info("Executing one-time news check cycle (--test mode)...")
        posted = check_and_post_news()
        logger.info("Test execution complete. Total articles posted: %d.", posted)
        sys.exit(0)

    port = int(os.getenv("PORT", str(getattr(config, "PORT", 8080) or 8080)))
    logger.info("Starting Flask application server on 0.0.0.0:%d...", port)
    app.run(host="0.0.0.0", port=port, use_reloader=False)
