"""Telegram News Channel package."""

from src.database import (
    NewsDatabase,
    cleanup_old,
    init_db,
    is_sent,
    mark_sent,
)
from src.sender import (
    TelegramSender,
    close_html_tags,
    send_message,
    send_news_post,
    send_photo,
    send_video,
    strip_html_tags,
    truncate_caption,
)

__all__ = [
    "NewsDatabase",
    "init_db",
    "is_sent",
    "mark_sent",
    "cleanup_old",
    "TelegramSender",
    "send_photo",
    "send_video",
    "send_message",
    "send_news_post",
    "truncate_caption",
    "close_html_tags",
    "strip_html_tags",
]
