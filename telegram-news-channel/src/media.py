"""Media extraction and handling module for Telegram News Channel automation.

Extracts photos, videos, and thumbnails from news article web pages,
searches Pexels API for fallback stock imagery, and downloads media assets
with size limits adhering to Telegram specifications.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from config import PEXELS_API_KEY
except (ImportError, ModuleNotFoundError):
    PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")

logger = logging.getLogger(__name__)

# Network request defaults
DEFAULT_TIMEOUT: int = 15
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Maximum media sizes in megabytes (Telegram limits: photo=10MB, video=50MB)
MAX_VIDEO_SIZE_MB: int = 50
MAX_PHOTO_SIZE_MB: int = 10

# Regex patterns for video platform embeds
YOUTUBE_REGEX = re.compile(
    r"(?:youtube\.com/(?:embed/|v/|watch\?v=)|youtu\.be/)([\w\-]{11})",
    re.IGNORECASE,
)
VIMEO_REGEX = re.compile(
    r"(?:player\.vimeo\.com/video/|vimeo\.com/)(\d+)",
    re.IGNORECASE,
)


def _is_valid_http_url(url: Optional[str]) -> bool:
    """Validate whether the string is an HTTP or HTTPS URL.

    Args:
        url: URL string to check.

    Returns:
        True if the URL is a non-empty HTTP/HTTPS URL, False otherwise.
    """
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _clean_and_resolve_url(candidate: Optional[str], base_url: str) -> Optional[str]:
    """Strip, resolve relative paths against base URL, and validate an extracted URL.

    Args:
        candidate: Extracted raw URL or path string.
        base_url: Base URL of the article page.

    Returns:
        Absolute HTTP/HTTPS URL string, or None if invalid.
    """
    if not candidate or not isinstance(candidate, str):
        return None
    cleaned = candidate.strip()
    if not cleaned or cleaned.startswith("data:") or cleaned.startswith("javascript:"):
        return None

    # Handle protocol-relative URLs (e.g. //example.com/image.jpg)
    if cleaned.startswith("//"):
        parsed_base = urlparse(base_url)
        scheme = parsed_base.scheme if parsed_base.scheme else "https"
        cleaned = f"{scheme}:{cleaned}"
    else:
        cleaned = urljoin(base_url, cleaned)

    return cleaned if _is_valid_http_url(cleaned) else None


def extract_media_from_url(url: str) -> Dict[str, Optional[str]]:
    """Fetch an article webpage and extract media URLs (video, image, thumbnail).

    Inspects OpenGraph meta tags, Twitter card tags, HTML5 <video> elements,
    and embedded video players (YouTube, Vimeo) to discover media assets.

    Args:
        url: Web URL of the article to extract media from.

    Returns:
        Dict with keys:
            - 'video_url': Video URL or None.
            - 'image_url': Image URL or None.
            - 'thumbnail_url': Thumbnail URL or None.
        Returns an empty dict {} if fetching or parsing encounters an error.
    """
    if not _is_valid_http_url(url):
        logger.warning("Invalid URL supplied to extract_media_from_url: '%s'", url)
        return {}

    logger.info("Extracting media from article URL: '%s'", url)
    try:
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.error("Failed to fetch article URL '%s': %s", url, exc)
        return {}

    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        logger.error("Failed to parse HTML from '%s': %s", url, exc)
        return {}

    video_url: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    # --- 1. Video Extraction ---
    # Look for OpenGraph video meta tags
    og_video_properties = [
        "og:video",
        "og:video:url",
        "og:video:secure_url",
    ]
    for prop in og_video_properties:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            resolved = _clean_and_resolve_url(tag["content"], url)
            if resolved:
                video_url = resolved
                logger.debug("Found OpenGraph video tag [%s]: %s", prop, video_url)
                break

    # Look for Twitter player meta tags
    if not video_url:
        twitter_player_names = [
            "twitter:player:stream",
            "twitter:player",
        ]
        for name in twitter_player_names:
            tag = soup.find("meta", attrs={"name": name}) or soup.find(
                "meta", attrs={"property": name}
            )
            if tag and tag.get("content"):
                resolved = _clean_and_resolve_url(tag["content"], url)
                if resolved:
                    video_url = resolved
                    logger.debug("Found Twitter player tag [%s]: %s", name, video_url)
                    break

    # Look for HTML5 <video> tags
    video_tags = soup.find_all("video")
    for v_tag in video_tags:
        # Check poster attribute for thumbnail
        if not thumbnail_url and v_tag.get("poster"):
            thumbnail_url = _clean_and_resolve_url(v_tag["poster"], url)

        # Check video src attribute
        if not video_url and v_tag.get("src"):
            resolved = _clean_and_resolve_url(v_tag["src"], url)
            if resolved:
                video_url = resolved
                logger.debug("Found <video src>: %s", video_url)

        # Check nested <source> tags
        if not video_url:
            for source in v_tag.find_all("source"):
                if source.get("src"):
                    resolved = _clean_and_resolve_url(source["src"], url)
                    if resolved:
                        video_url = resolved
                        logger.debug("Found <video><source src>: %s", video_url)
                        break

    # Look for YouTube and Vimeo iframe embeds
    if not video_url:
        for iframe in soup.find_all(["iframe", "embed"]):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            yt_match = YOUTUBE_REGEX.search(src)
            if yt_match:
                yt_id = yt_match.group(1)
                video_url = f"https://www.youtube.com/watch?v={yt_id}"
                if not thumbnail_url:
                    thumbnail_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                logger.debug("Found YouTube embed: %s", video_url)
                break

            vimeo_match = VIMEO_REGEX.search(src)
            if vimeo_match:
                vimeo_id = vimeo_match.group(1)
                video_url = f"https://vimeo.com/{vimeo_id}"
                logger.debug("Found Vimeo embed: %s", video_url)
                break

    # If video is YouTube embed, ensure thumbnail is set
    if video_url and not thumbnail_url:
        yt_match = YOUTUBE_REGEX.search(video_url)
        if yt_match:
            yt_id = yt_match.group(1)
            thumbnail_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"

    # --- 2. Image Extraction ---
    # Look for OpenGraph image meta tags
    og_image_properties = [
        "og:image",
        "og:image:url",
        "og:image:secure_url",
    ]
    for prop in og_image_properties:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            resolved = _clean_and_resolve_url(tag["content"], url)
            if resolved:
                image_url = resolved
                logger.debug("Found OpenGraph image tag [%s]: %s", prop, image_url)
                break

    # Look for Twitter image meta tags
    if not image_url:
        twitter_image_names = [
            "twitter:image",
            "twitter:image:src",
        ]
        for name in twitter_image_names:
            tag = soup.find("meta", attrs={"name": name}) or soup.find(
                "meta", attrs={"property": name}
            )
            if tag and tag.get("content"):
                resolved = _clean_and_resolve_url(tag["content"], url)
                if resolved:
                    image_url = resolved
                    logger.debug("Found Twitter image tag [%s]: %s", name, image_url)
                    break

    # Look for link rel="image_src"
    if not image_url:
        link_tag = soup.find("link", attrs={"rel": lambda r: r and "image_src" in r})
        if link_tag and link_tag.get("href"):
            resolved = _clean_and_resolve_url(link_tag["href"], url)
            if resolved:
                image_url = resolved
                logger.debug("Found <link rel='image_src'>: %s", image_url)

    # Fallback: look for prominent <img> tags inside article/main container
    if not image_url:
        container = soup.find(["article", "main"]) or soup.body
        if container:
            for img in container.find_all("img"):
                img_src = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-original")
                )
                if not img_src:
                    continue
                # Skip tracking pixels, tiny icons, SVG sprites, and badges
                width = img.get("width")
                height = img.get("height")
                if width in ("1", "0") or height in ("1", "0"):
                    continue
                resolved = _clean_and_resolve_url(img_src, url)
                if resolved and not resolved.endswith(".svg"):
                    image_url = resolved
                    logger.debug("Found fallback article <img>: %s", image_url)
                    break

    # Default thumbnail to image_url if not already set
    if not thumbnail_url and image_url:
        thumbnail_url = image_url

    logger.info(
        "Extraction result for '%s': video=%s, image=%s, thumbnail=%s",
        url,
        bool(video_url),
        bool(image_url),
        bool(thumbnail_url),
    )

    return {
        "video_url": video_url,
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
    }


def search_pexels_image(query: str, api_key: str) -> Optional[str]:
    """Search Pexels API for a relevant stock photograph.

    Args:
        query: Search term or headline keywords.
        api_key: Pexels API authorization token.

    Returns:
        High-resolution image URL string if found, otherwise None.
    """
    if not api_key or not api_key.strip():
        logger.debug("No Pexels API key provided. Skipping Pexels search.")
        return None

    cleaned_query = query.strip()
    if not cleaned_query:
        logger.warning("Empty search query provided to search_pexels_image.")
        return None

    endpoint = "https://api.pexels.com/v1/search"
    headers = {
        "Authorization": api_key.strip(),
        "User-Agent": DEFAULT_USER_AGENT,
    }
    params = {
        "query": cleaned_query,
        "per_page": 1,
    }

    try:
        logger.info("Searching Pexels API for query: '%s'", cleaned_query)
        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )

        if response.status_code != 200:
            logger.warning(
                "Pexels API responded with status %d: %s",
                response.status_code,
                response.text[:200],
            )
            return None

        data = response.json()
        photos = data.get("photos", [])
        if not photos:
            logger.info("No Pexels photos found for query: '%s'", cleaned_query)
            return None

        photo = photos[0]
        src = photo.get("src", {})
        # Prioritize high quality sizes suitable for Telegram photo limits (<10MB)
        image_url = (
            src.get("large2x")
            or src.get("large")
            or src.get("original")
            or src.get("medium")
        )
        if image_url:
            logger.info("Found Pexels photo for '%s': %s", cleaned_query, image_url)
            return str(image_url)

        return None
    except Exception as exc:
        logger.error(
            "Exception occurred while searching Pexels for '%s': %s",
            cleaned_query,
            exc,
        )
        return None


def download_media(url: str, max_size_mb: int = 50) -> Optional[Tuple[bytes, str]]:
    """Download media from URL with size limits and streaming support.

    Validates Content-Length header first. Enforces Telegram limits:
    50MB for video and 10MB for photos when using default settings.

    Args:
        url: Web URL of the media file to download.
        max_size_mb: Maximum allowed media file size in megabytes (default: 50).

    Returns:
        Tuple of (media_bytes, content_type) if successful, None if skipped or on failure.
    """
    if not _is_valid_http_url(url):
        logger.error("Cannot download media from invalid URL: '%s'", url)
        return None

    try:
        logger.info("Initiating download for media URL: '%s'", url)
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            stream=True,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()

        # Determine Content-Type
        raw_content_type = response.headers.get("Content-Type", "")
        content_type = raw_content_type.split(";")[0].strip().lower()

        # Fallback MIME detection from URL filename if generic/empty
        if not content_type or content_type in (
            "application/octet-stream",
            "binary/octet-stream",
        ):
            guessed_type, _ = mimetypes.guess_type(url)
            if guessed_type:
                content_type = guessed_type.lower()
            else:
                content_type = "application/octet-stream"

        # Determine effective maximum size in MB
        # Telegram limits photos to 10MB; cap image downloads to 10MB if max_size_mb was default
        effective_max_mb = max_size_mb
        if content_type.startswith("image/") and max_size_mb > MAX_PHOTO_SIZE_MB:
            effective_max_mb = MAX_PHOTO_SIZE_MB

        max_bytes = effective_max_mb * 1024 * 1024

        # Check Content-Length header if provided by server
        content_length_header = response.headers.get("Content-Length")
        if content_length_header and content_length_header.isdigit():
            content_length = int(content_length_header)
            if content_length > max_bytes:
                logger.warning(
                    "Media at '%s' is %d bytes (%.2f MB), exceeding max limit of %d MB. Skipping.",
                    url,
                    content_length,
                    content_length / (1024 * 1024),
                    effective_max_mb,
                )
                return None

        # Stream download into bytearray to support large files without unbounded buffering
        downloaded = bytearray()
        chunk_size = 64 * 1024  # 64 KB per chunk
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                downloaded.extend(chunk)
                if len(downloaded) > max_bytes:
                    logger.warning(
                        "Media download for '%s' exceeded max limit of %d MB while streaming. Aborting.",
                        url,
                        effective_max_mb,
                    )
                    return None

        if not downloaded:
            logger.warning("Downloaded empty media payload from '%s'.", url)
            return None

        logger.info(
            "Downloaded %d bytes (type: %s) from '%s'.",
            len(downloaded),
            content_type,
            url,
        )
        return (bytes(downloaded), content_type)

    except Exception as exc:
        logger.error("Failed to download media from '%s': %s", url, exc)
        return None


def get_best_media(
    article_url: str,
    title: str,
    pexels_api_key: str = "",
) -> Dict[str, Any]:
    """Determine and retrieve the best media (video or photo) for a news article.

    Tries to extract video and image from the article URL first.
    - If video found -> returns {'type': 'video', 'url': video_url, ...}
    - If only photo found -> returns {'type': 'photo', 'url': image_url}
    - If nothing found and Pexels API key provided -> searches Pexels
    - If still nothing -> returns {'type': 'none'}

    Args:
        article_url: URL of the news article page.
        title: Title or headline of the news article.
        pexels_api_key: Optional Pexels API key. Falls back to config/env.

    Returns:
        Dict describing media type and URL:
        {'type': 'video', 'url': str, ...} | {'type': 'photo', 'url': str} | {'type': 'none'}
    """
    try:
        # Step 1: Extract media from article URL
        extracted = extract_media_from_url(article_url)
        video_url = extracted.get("video_url")
        image_url = extracted.get("image_url")
        thumbnail_url = extracted.get("thumbnail_url")

        # Step 2: If video found -> return video result
        if video_url:
            logger.info("Selected video for article '%s': %s", title, video_url)
            result: Dict[str, Any] = {"type": "video", "url": video_url}
            if thumbnail_url:
                result["thumbnail_url"] = thumbnail_url
            return result

        # Step 3: If only photo found -> return photo result
        if image_url:
            logger.info("Selected photo for article '%s': %s", title, image_url)
            return {"type": "photo", "url": image_url}

        # Step 4: If nothing found, try Pexels search
        effective_api_key = (
            pexels_api_key.strip() if pexels_api_key else PEXELS_API_KEY.strip()
        )
        if effective_api_key:
            logger.info(
                "No media on page for '%s'. Searching Pexels fallback...", title
            )
            pexels_image = search_pexels_image(title, effective_api_key)

            # If full title yields no results and is long, try first 5 words
            if not pexels_image and title:
                words = title.split()
                if len(words) > 5:
                    short_query = " ".join(words[:5])
                    logger.info("Retrying Pexels with shortened query: '%s'", short_query)
                    pexels_image = search_pexels_image(short_query, effective_api_key)

            if pexels_image:
                logger.info("Selected Pexels image for article '%s': %s", title, pexels_image)
                return {"type": "photo", "url": pexels_image}

        # Step 5: If still nothing found -> return type none
        logger.info("No media available for article '%s'.", title)
        return {"type": "none"}

    except Exception as exc:
        logger.error(
            "Unexpected error in get_best_media for '%s' (%s): %s",
            title,
            article_url,
            exc,
            exc_info=True,
        )
        return {"type": "none"}
