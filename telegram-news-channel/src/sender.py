"""Telegram Sender Module for Telegram News Channel automation.

Dispatches formatted news posts (photos, videos, and text messages) to a
Telegram channel using direct HTTP calls to the Telegram Bot API via requests.
Includes intelligent caption truncation, rate-limit retry handling, and media fallbacks.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

try:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
except (ImportError, ModuleNotFoundError):
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

logger = logging.getLogger(__name__)

# Constants
TELEGRAM_API_BASE_URL: str = "https://api.telegram.org/bot{token}/{method}"
MAX_CAPTION_LENGTH: int = 1024
MAX_MESSAGE_LENGTH: int = 4096
DEFAULT_TIMEOUT: int = 35
MAX_RETRIES: int = 3
RATE_LIMIT_DELAY: float = 3.0  # Mandatory delay between posts (Telegram rate limit)


def close_html_tags(html_text: str) -> str:
    """Ensure any unclosed HTML tags in truncated text are properly closed.

    Args:
        html_text: The HTML string that may contain truncated or unclosed tags.

    Returns:
        Balanced HTML string with unclosed tags closed in reverse order.
    """
    # Remove any trailing incomplete tag slice like '<b' or '<a hr'
    last_open = html_text.rfind("<")
    last_close = html_text.rfind(">")
    if last_open > last_close:
        html_text = html_text[:last_open]

    # Find paired tags (ignoring void tags)
    tag_pattern = re.compile(r"<(/)?([a-zA-Z0-9_-]+)(?:\s+[^>]*)?>")
    void_tags = {"br", "hr", "img"}
    open_tags: list[str] = []

    for match in tag_pattern.finditer(html_text):
        is_closing = match.group(1) == "/"
        tag_name = match.group(2).lower()
        if tag_name in void_tags:
            continue
        if is_closing:
            if open_tags and open_tags[-1] == tag_name:
                open_tags.pop()
        else:
            open_tags.append(tag_name)

    # Append closing tags in reverse order
    for tag in reversed(open_tags):
        html_text += f"</{tag}>"

    return html_text


def strip_html_tags(text: str) -> str:
    """Remove all HTML markup tags from a text string.

    Args:
        text: HTML text string.

    Returns:
        Plain text with HTML tags stripped.
    """
    return re.sub(r"<[^<]+?>", "", text)


def is_bullet_line(line: str) -> bool:
    """Determine whether a line is a bullet point.

    Args:
        line: The text line to test.

    Returns:
        True if the line starts with a bullet marker or list numeral.
    """
    stripped = line.strip()
    return bool(re.match(r"^(?:[•\-\*\u25aa\u25ab\u2014\u2022]|\d+[\.\)])\s+", stripped))


def is_hashtag_line(line: str) -> bool:
    """Determine whether a line consists predominantly of hashtags.

    Args:
        line: The text line to test.

    Returns:
        True if all words in the line start with '#'.
    """
    stripped = line.strip()
    if not stripped:
        return False
    tokens = stripped.split()
    return all(t.startswith("#") for t in tokens) and len(tokens) > 0


def is_link_line(line: str) -> bool:
    """Determine whether a line represents a source link or read-more action.

    Args:
        line: The text line to test.

    Returns:
        True if the line contains a web URL, HTML anchor, or link icon.
    """
    stripped = line.strip()
    return bool(re.search(r"(?:https?://|<a\s+href|🔗|Source:|Read\s+more)", stripped, re.IGNORECASE))


def truncate_caption(caption: str, max_length: int = MAX_CAPTION_LENGTH) -> str:
    """Smartly truncate caption to fit within Telegram's character limit.

    Preserves essential elements in priority order:
    1. Headline and main context
    2. Hashtags (always retained at the end of the post)
    3. Primary source link
    4. At least 2 bullet points (or as many as space permits)

    If the text is still too long, bullet points are cut down or truncated.

    Args:
        caption: The formatted post caption (can include HTML tags).
        max_length: Maximum allowed characters (defaults to 1024 for Telegram).

    Returns:
        Properly formatted, truncated caption guaranteed to be <= max_length
        with balanced HTML tags.
    """
    if not caption or len(caption) <= max_length:
        return caption

    lines = [line.strip() for line in caption.strip().split("\n")]
    hashtag_lines: list[str] = []
    link_lines: list[str] = []
    content_lines: list[str] = []

    idx = len(lines) - 1
    # Traverse backwards from bottom to find trailing empty lines and hashtags
    while idx >= 0 and not lines[idx]:
        idx -= 1

    # Extract hashtag line(s) at bottom
    while idx >= 0 and (is_hashtag_line(lines[idx]) or not lines[idx]):
        if lines[idx]:
            hashtag_lines.insert(0, lines[idx])
        idx -= 1

    # Extract link line(s) above hashtags
    while idx >= 0 and (is_link_line(lines[idx]) or not lines[idx]):
        if lines[idx]:
            link_lines.insert(0, lines[idx])
        idx -= 1

    # Remaining lines constitute headline and body/bullets
    while idx >= 0:
        content_lines.insert(0, lines[idx])
        idx -= 1

    # Assemble footer string (link + hashtags)
    footer_parts: list[str] = []
    if link_lines:
        footer_parts.append("\n".join(link_lines))
    if hashtag_lines:
        footer_parts.append(" ".join(hashtag_lines))
    footer_str = "\n\n".join(footer_parts).strip()

    footer_reserve = len(footer_str) + (2 if footer_str else 0)
    body_budget = max_length - footer_reserve

    # If footer alone occupies nearly the entire limit, preserve hashtags
    if body_budget < 50:
        ht_str = " ".join(hashtag_lines)
        if len(ht_str) + 15 < max_length:
            avail = max_length - len(ht_str) - 6
            cut_text = caption[:avail].strip() + "...\n\n" + ht_str
            return close_html_tags(cut_text)
        return close_html_tags(caption[: max_length - 3] + "...")

    # Separate headline and bullets from content_lines
    non_empty_content = [l for l in content_lines if l]
    if not non_empty_content:
        headline = ""
        bullets: list[str] = []
    else:
        headline = non_empty_content[0]
        bullets = []
        for line in non_empty_content[1:]:
            if is_bullet_line(line):
                bullets.append(line)
            else:
                bullets.append(f"• {line}")

    body_str = ""
    if bullets:
        # Try keeping as many bullets as possible down to 2
        for k in range(len(bullets), 1, -1):
            candidate = f"{headline}\n\n" + "\n".join(bullets[:k])
            if len(candidate) <= body_budget:
                body_str = candidate
                break

        # If 2 bullets still exceed budget, try 1 bullet
        if not body_str:
            candidate = f"{headline}\n\n{bullets[0]}"
            if len(candidate) <= body_budget:
                body_str = candidate

        # If 1 bullet doesn't fit, truncate bullet text or keep headline only
        if not body_str:
            if len(headline) <= body_budget:
                avail_bullet = body_budget - len(headline) - 4
                if avail_bullet > 25 and bullets:
                    body_str = f"{headline}\n\n{bullets[0][:avail_bullet - 3]}..."
                else:
                    body_str = headline
            else:
                body_str = headline[: body_budget - 3] + "..."
    else:
        full_content = "\n\n".join(non_empty_content)
        if len(full_content) <= body_budget:
            body_str = full_content
        else:
            body_str = full_content[: body_budget - 3] + "..."

    # Combine body and footer
    if body_str and footer_str:
        result = f"{body_str}\n\n{footer_str}".strip()
    elif body_str:
        result = body_str.strip()
    else:
        result = footer_str.strip()

    # Balance any open HTML tags
    result = close_html_tags(result)

    # If closing tags caused slight overflow over max_length, trim iteratively
    while len(result) > max_length:
        overflow = len(result) - max_length
        if body_str and len(body_str) > overflow + 10:
            body_str = body_str[: -(overflow + 5)].rstrip() + "..."
            result = f"{body_str}\n\n{footer_str}".strip() if footer_str else body_str.strip()
            result = close_html_tags(result)
        else:
            result = result[: max_length - 4] + "..."
            result = close_html_tags(result)
            break

    return result


def _telegram_request(
    bot_token: str,
    method: str,
    payload: dict[str, Any],
    max_retries: int = MAX_RETRIES,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Execute a POST request to Telegram Bot API with retry and rate-limit handling.

    Args:
        bot_token: Telegram bot token.
        method: Telegram API method name (e.g. 'sendPhoto', 'sendVideo', 'sendMessage').
        payload: Parameters dictionary to serialize as JSON.
        max_retries: Maximum number of attempts for transient errors and rate limits.
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed Telegram JSON response dictionary on success, or None on failure.
    """
    if not bot_token:
        logger.error("Telegram bot token is not provided.")
        return None

    url = TELEGRAM_API_BASE_URL.format(token=bot_token, method=method)

    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(
                "Calling Telegram API %s (attempt %d/%d) with chat_id=%s",
                method,
                attempt,
                max_retries,
                payload.get("chat_id"),
            )
            response = requests.post(url, json=payload, timeout=timeout)

            # Handle Rate Limiting (HTTP 429)
            if response.status_code == 429:
                retry_after = 5
                try:
                    resp_data = response.json()
                    retry_after = int(resp_data.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    header_val = response.headers.get("Retry-After")
                    if header_val and header_val.isdigit():
                        retry_after = int(header_val)

                logger.warning(
                    "Telegram rate limit hit (HTTP 429) for %s. Retrying after %d seconds (attempt %d/%d)...",
                    method,
                    retry_after,
                    attempt,
                    max_retries,
                )
                if attempt < max_retries:
                    time.sleep(retry_after + 1)
                    continue
                logger.error("Telegram rate limit retry budget exceeded for %s", method)
                return None

            # Parse JSON response
            try:
                data = response.json()
            except ValueError:
                logger.error(
                    "Telegram returned non-JSON response (status %d): %s",
                    response.status_code,
                    response.text[:200],
                )
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return None

            if data.get("ok"):
                return data

            # Extract Telegram API error information
            error_code = data.get("error_code")
            description = data.get("description", "")
            logger.warning(
                "Telegram API error for %s: [%s] %s",
                method,
                error_code,
                description,
            )

            # Check for HTML entity parsing error and retry once with stripped plain text
            if "can't parse entities" in description.lower() and payload.get("parse_mode"):
                logger.warning(
                    "HTML parse error in %s. Retrying with stripped plain text...",
                    method,
                )
                clean_payload = dict(payload)
                clean_payload.pop("parse_mode", None)
                if "caption" in clean_payload:
                    clean_payload["caption"] = strip_html_tags(str(clean_payload["caption"]))
                if "text" in clean_payload:
                    clean_payload["text"] = strip_html_tags(str(clean_payload["text"]))

                try:
                    retry_resp = requests.post(url, json=clean_payload, timeout=timeout)
                    retry_data = retry_resp.json()
                    if retry_data.get("ok"):
                        logger.info("Successfully sent %s after stripping HTML entities.", method)
                        return retry_data
                except Exception as fb_exc:
                    logger.error("Fallback plain text attempt failed: %s", fb_exc)

            # For client errors (4xx other than 429), retrying identical payload will fail
            if response.status_code < 500 and response.status_code != 429:
                return None

            # For 5xx server errors, retry with exponential backoff
            if attempt < max_retries:
                backoff = attempt * 2
                logger.info("Server error %d on %s. Backing off for %d seconds...", response.status_code, method, backoff)
                time.sleep(backoff)

        except requests.exceptions.RequestException as exc:
            logger.error(
                "Network exception calling Telegram API %s (attempt %d/%d): %s",
                method,
                attempt,
                max_retries,
                exc,
            )
            if attempt < max_retries:
                time.sleep(attempt * 2)
            else:
                return None
        except Exception as exc:
            logger.exception("Unexpected error calling Telegram API %s: %s", method, exc)
            return None

    return None


def send_photo(
    bot_token: str,
    channel_id: str,
    photo_url: str,
    caption: str,
) -> bool:
    """Send a photo with a formatted caption to a Telegram channel.

    Uses Telegram sendPhoto API. Truncates captions longer than 1024 characters smartly
    preserving headline, bullet points, links, and hashtags at the end.

    Args:
        bot_token: Telegram bot token.
        channel_id: Target channel username (e.g. '@channel') or chat ID.
        photo_url: Web URL of the photo to send.
        caption: HTML-formatted caption for the photo.

    Returns:
        True if the photo was successfully sent, False otherwise.
    """
    if not bot_token:
        logger.error("send_photo: Missing bot_token.")
        return False
    if not channel_id:
        logger.error("send_photo: Missing channel_id.")
        return False
    if not photo_url:
        logger.warning("send_photo: Empty photo_url. Falling back to send_message.")
        return send_message(bot_token, channel_id, caption)

    truncated_caption = truncate_caption(caption, max_length=MAX_CAPTION_LENGTH)

    payload: dict[str, Any] = {
        "chat_id": channel_id,
        "photo": photo_url.strip(),
        "caption": truncated_caption,
        "parse_mode": "HTML",
    }

    logger.info("Sending photo to %s: %s", channel_id, photo_url)
    res = _telegram_request(bot_token, "sendPhoto", payload)
    success = bool(res and res.get("ok"))
    if success:
        logger.info("Successfully sent photo to %s", channel_id)
    else:
        logger.error("Failed to send photo to %s (URL: %s)", channel_id, photo_url)

    return success


def send_video(
    bot_token: str,
    channel_id: str,
    video_url: str,
    caption: str,
    thumbnail_url: str | None = None,
) -> bool:
    """Send a video with caption to a Telegram channel with fallback support.

    Uses Telegram sendVideo API. If the video fails (too large, invalid URL),
    falls back to sending a photo with the thumbnail and video link in the caption.

    Args:
        bot_token: Telegram bot token.
        channel_id: Target channel username or chat ID.
        video_url: Web URL of the video to send.
        caption: HTML-formatted caption. Truncated smartly to 1024 characters.
        thumbnail_url: Optional web URL of a thumbnail image for the video.

    Returns:
        True if the video or fallback was sent successfully, False otherwise.
    """
    if not bot_token:
        logger.error("send_video: Missing bot_token.")
        return False
    if not channel_id:
        logger.error("send_video: Missing channel_id.")
        return False

    truncated_caption = truncate_caption(caption, max_length=MAX_CAPTION_LENGTH)

    payload: dict[str, Any] = {
        "chat_id": channel_id,
        "video": video_url.strip() if video_url else "",
        "caption": truncated_caption,
        "parse_mode": "HTML",
        "supports_streaming": True,
    }
    if thumbnail_url:
        payload["thumbnail"] = thumbnail_url.strip()

    logger.info("Sending video to %s: %s", channel_id, video_url)
    res = _telegram_request(bot_token, "sendVideo", payload)
    if res and res.get("ok"):
        logger.info("Successfully sent video to %s", channel_id)
        return True

    # Fallback logic: If video fails, fall back to photo with video link in caption
    logger.warning(
        "Failed to send video to %s (%s). Falling back to photo with video link...",
        channel_id,
        video_url,
    )

    fallback_caption = caption
    if video_url and video_url not in fallback_caption:
        fallback_caption = f"{caption}\n\n🎬 <b>Watch Video:</b> {video_url}"

    if thumbnail_url:
        logger.info("Attempting fallback send_photo using thumbnail: %s", thumbnail_url)
        if send_photo(
            bot_token=bot_token,
            channel_id=channel_id,
            photo_url=thumbnail_url,
            caption=fallback_caption,
        ):
            return True

    logger.warning("Thumbnail fallback unavailable or failed. Falling back to send_message...")
    return send_message(bot_token, channel_id, fallback_caption)


def send_message(
    bot_token: str,
    channel_id: str,
    text: str,
) -> bool:
    """Send a text message to a Telegram channel.

    Uses Telegram sendMessage API. Used as fallback when no media is available
    or when media delivery fails.

    Args:
        bot_token: Telegram bot token.
        channel_id: Target channel username or chat ID.
        text: HTML-formatted message text (up to 4096 characters).

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if not bot_token:
        logger.error("send_message: Missing bot_token.")
        return False
    if not channel_id:
        logger.error("send_message: Missing channel_id.")
        return False
    if not text:
        logger.error("send_message: Empty text provided.")
        return False

    final_text = text
    if len(final_text) > MAX_MESSAGE_LENGTH:
        final_text = truncate_caption(text, max_length=MAX_MESSAGE_LENGTH)

    payload: dict[str, Any] = {
        "chat_id": channel_id,
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    logger.info("Sending text message to %s (%d chars)", channel_id, len(final_text))
    res = _telegram_request(bot_token, "sendMessage", payload)
    success = bool(res and res.get("ok"))
    if success:
        logger.info("Successfully sent text message to %s", channel_id)
    else:
        logger.error("Failed to send text message to %s", channel_id)

    return success


def send_news_post(
    bot_token: str,
    channel_id: str,
    article: dict[str, Any],
) -> bool:
    """Master function to dispatch a news post to the Telegram channel.

    Inspects the article media dictionary and routes to the appropriate sender:
    - media['type'] == 'video' -> send_video (falls back to photo with thumbnail if fails)
    - media['type'] == 'photo' -> send_photo (falls back to message if fails)
    - media['type'] == 'none'  -> send_message (text only)

    Enforces a 3-second delay after each post to respect Telegram rate limits.

    Args:
        bot_token: Telegram bot token.
        channel_id: Target channel identifier (e.g. '@channel').
        article: Article dictionary containing:
            - 'post_text': Formatted HTML post text.
            - 'media': Dictionary with 'type' ('photo', 'video', 'none'), 'url',
                       and optional 'thumbnail_url' / 'thumbnail'.

    Returns:
        True if the post was sent successfully, False otherwise.
    """
    if not article:
        logger.error("send_news_post: Empty article dictionary provided.")
        return False

    post_text: str = article.get("post_text") or article.get("text") or article.get("caption") or ""
    media: dict[str, Any] = article.get("media") or {}
    media_type: str = str(media.get("type", "none")).lower().strip()
    media_url: str = str(media.get("url") or "").strip()
    thumbnail_url: str | None = media.get("thumbnail_url") or media.get("thumbnail")
    if thumbnail_url:
        thumbnail_url = str(thumbnail_url).strip()

    logger.info(
        "Dispatching news post [media_type='%s'] to %s (text length: %d)",
        media_type,
        channel_id,
        len(post_text),
    )

    success = False

    try:
        if media_type == "video" and media_url:
            success = send_video(
                bot_token=bot_token,
                channel_id=channel_id,
                video_url=media_url,
                caption=post_text,
                thumbnail_url=thumbnail_url,
            )
            if not success:
                logger.warning("send_video returned False. Trying fallback photo with thumbnail...")
                if thumbnail_url:
                    success = send_photo(bot_token, channel_id, thumbnail_url, post_text)
                if not success:
                    logger.warning("Photo fallback failed. Trying fallback text message...")
                    success = send_message(bot_token, channel_id, post_text)

        elif media_type == "photo" and media_url:
            success = send_photo(
                bot_token=bot_token,
                channel_id=channel_id,
                photo_url=media_url,
                caption=post_text,
            )
            if not success:
                logger.warning("send_photo returned False. Retrying with text message...")
                success = send_message(bot_token, channel_id, post_text)

        else:
            # Media type is 'none' or media_url is not provided
            success = send_message(
                bot_token=bot_token,
                channel_id=channel_id,
                text=post_text,
            )

    finally:
        # Mandatory 3-second delay between posts to prevent Telegram rate limiting
        logger.debug("Applying rate limit delay of %.1f seconds...", RATE_LIMIT_DELAY)
        time.sleep(RATE_LIMIT_DELAY)

    if success:
        logger.info("Successfully dispatched news post to %s", channel_id)
    else:
        logger.error("Failed to dispatch news post to %s", channel_id)

    return success


class TelegramSender:
    """Client class for sending formatted news updates to Telegram channels."""

    def __init__(
        self,
        bot_token: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        """Initialize TelegramSender with bot token and channel ID.

        Falls back to environment/config settings if not explicitly provided.

        Args:
            bot_token: Telegram bot token.
            channel_id: Telegram channel username or ID.
        """
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN or ""
        self.channel_id = channel_id or TELEGRAM_CHANNEL_ID or ""

    def send_photo(self, photo_url: str, caption: str) -> bool:
        """Send a photo to the configured channel."""
        return send_photo(self.bot_token, self.channel_id, photo_url, caption)

    def send_video(self, video_url: str, caption: str, thumbnail_url: str | None = None) -> bool:
        """Send a video to the configured channel."""
        return send_video(self.bot_token, self.channel_id, video_url, caption, thumbnail_url)

    def send_message(self, text: str) -> bool:
        """Send a text message to the configured channel."""
        return send_message(self.bot_token, self.channel_id, text)

    def send_news_post(self, article: dict[str, Any]) -> bool:
        """Send a formatted news post to the configured channel."""
        return send_news_post(self.bot_token, self.channel_id, article)
