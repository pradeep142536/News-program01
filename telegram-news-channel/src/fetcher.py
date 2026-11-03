"""News fetcher module for Telegram News Channel automation.

Fetches tech news, articles, and trending repositories from RSS feeds,
Hacker News Firebase API, and GitHub Trending with robust error handling,
standardized data structures, User-Agent headers, and timeout management.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

import feedparser
import requests
from bs4 import BeautifulSoup

# Ensure project root is in sys.path for direct script execution and relative imports
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    import config
except ImportError:
    config = None

try:
    from src.sources import (
        DEVPOST_API,
        GITHUB_TRENDING_URL,
        HACKER_NEWS_API,
        RSS_FEEDS,
        SCHOLARSHIP_FEEDS,
        HACKATHON_FEEDS,
    )
except ImportError:
    try:
        from sources import (
            DEVPOST_API,
            GITHUB_TRENDING_URL,
            HACKER_NEWS_API,
            RSS_FEEDS,
            SCHOLARSHIP_FEEDS,
            HACKATHON_FEEDS,
        )
    except ImportError:
        DEVPOST_API = "https://devpost.com/api/hackathons"
        GITHUB_TRENDING_URL = "https://github.com/trending"
        HACKER_NEWS_API = "https://hacker-news.firebaseio.com/v0"
        RSS_FEEDS = {}
        SCHOLARSHIP_FEEDS = {}
        HACKATHON_FEEDS = {}

logger = logging.getLogger(__name__)

# Request settings
DEFAULT_TIMEOUT: int = getattr(config, "REQUEST_TIMEOUT", 15) if config else 15
DEFAULT_USER_AGENT: str = getattr(
    config,
    "USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
) if config else (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class NewsItem(TypedDict):
    """Unified news item data structure across all sources."""

    title: str
    url: str
    summary: str
    published: str
    source: str
    raw_content: str


def _get_headers() -> Dict[str, str]:
    """Build standard HTTP headers with User-Agent.

    Returns:
        Dictionary of HTTP request headers.
    """
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.8,*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def _clean_html(html_text: str) -> str:
    """Strip HTML tags and normalize whitespace.

    Args:
        html_text: String containing raw HTML or plain text.

    Returns:
        Clean plain text string.
    """
    if not html_text:
        return ""
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        return " ".join(text.split())
    except Exception as exc:
        logger.debug("HTML cleaning error: %s", exc)
        return " ".join(html_text.split())


def _extract_rss_published(entry: Any) -> str:
    """Extract published timestamp from a feedparser entry as an ISO string.

    Args:
        entry: A feedparser entry object or dictionary.

    Returns:
        ISO formatted UTC timestamp or empty string fallback.
    """
    published_parsed = getattr(entry, "published_parsed", None) or entry.get("published_parsed")
    if published_parsed:
        try:
            pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            return pub_dt.isoformat()
        except Exception:
            pass

    raw_pub = str(
        getattr(entry, "published", None)
        or entry.get("published")
        or getattr(entry, "updated", None)
        or entry.get("updated")
        or ""
    ).strip()

    if raw_pub:
        return raw_pub

    return datetime.now(timezone.utc).isoformat()


def fetch_rss_feeds(
    feeds: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch articles from all RSS feeds defined in sources.py.

    Uses feedparser library combined with requests for strict timeout and
    custom User-Agent control. Handles network and parsing errors per feed
    gracefully without crashing.

    Args:
        feeds: Optional mapping of source names to feed URLs. If None, uses
            all RSS feeds defined in sources.py (including scholarship feeds).
        timeout: Network timeout in seconds per request. Defaults to 15.

    Returns:
        List of dictionaries with keys: title, url, summary, published, source, raw_content.
    """
    if feeds is None:
        target_feeds: Dict[str, str] = {**RSS_FEEDS, **SCHOLARSHIP_FEEDS, **HACKATHON_FEEDS}
    else:
        target_feeds = feeds

    all_items: List[Dict[str, Any]] = []
    headers = _get_headers()

    logger.info("Beginning RSS fetch for %d configured feeds...", len(target_feeds))

    for source_name, feed_url in target_feeds.items():
        feed_items: List[Dict[str, Any]] = []
        try:
            logger.debug("Fetching RSS feed for '%s' from %s", source_name, feed_url)
            response = requests.get(feed_url, headers=headers, timeout=timeout)
            response.raise_for_status()

            parsed_feed = feedparser.parse(response.content)

            if parsed_feed.bozo and not parsed_feed.entries:
                logger.warning(
                    "Feed '%s' could not be parsed: %s",
                    source_name,
                    getattr(parsed_feed, "bozo_exception", "Unknown XML error"),
                )
                continue

            for entry in parsed_feed.entries:
                title = str(getattr(entry, "title", None) or entry.get("title") or "").strip()
                url = str(getattr(entry, "link", None) or entry.get("link") or "").strip()

                if not title or not url:
                    continue

                # Extract summary / content
                raw_summary = ""
                content_list = getattr(entry, "content", None) or entry.get("content")
                if content_list and isinstance(content_list, list) and len(content_list) > 0:
                    content_first = content_list[0]
                    if isinstance(content_first, dict):
                        raw_summary = content_first.get("value", "")
                    elif hasattr(content_first, "value"):
                        raw_summary = str(content_first.value)

                if not raw_summary:
                    raw_summary = str(
                        getattr(entry, "summary", None)
                        or entry.get("summary")
                        or getattr(entry, "description", None)
                        or entry.get("description")
                        or ""
                    )

                clean_summary = _clean_html(raw_summary) or title
                published = _extract_rss_published(entry)

                item: NewsItem = {
                    "title": title,
                    "url": url,
                    "summary": clean_summary,
                    "published": published,
                    "source": source_name,
                    "raw_content": raw_summary or clean_summary,
                }
                feed_items.append(item)

            logger.info("Fetched %d articles from RSS feed '%s'.", len(feed_items), source_name)
            all_items.extend(feed_items)

        except requests.Timeout:
            logger.warning("Timeout (%ds) fetching RSS feed '%s' (%s).", timeout, source_name, feed_url)
        except requests.RequestException as req_err:
            logger.warning("HTTP error fetching RSS feed '%s' (%s): %s", source_name, feed_url, req_err)
        except Exception as exc:
            logger.warning("Unexpected error processing RSS feed '%s': %s", source_name, exc)

    logger.info("Total RSS articles fetched across all feeds: %d.", len(all_items))
    return all_items


def fetch_hacker_news(
    limit: int = 10,
    min_score: int = 100,
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch top stories from Hacker News API filtering for score > 100.

    Calls the Hacker News Firebase topstories endpoint, retrieves story metadata,
    filters by score, and formats items consistently with RSS feeds.

    Args:
        limit: Maximum number of qualifying stories to return. Defaults to 10.
        min_score: Minimum upvote score threshold (exclusive). Defaults to 100.
        timeout: Network timeout in seconds per request. Defaults to 15.

    Returns:
        List of dictionaries with keys: title, url, summary, published, source, raw_content.
    """
    if limit <= 0:
        return []

    items: List[Dict[str, Any]] = []
    top_stories_url = f"{HACKER_NEWS_API}/topstories.json"
    headers = _get_headers()

    logger.info("Fetching Hacker News top stories list...")

    try:
        response = requests.get(top_stories_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        story_ids: List[int] = response.json()
        if not isinstance(story_ids, list):
            logger.warning("Unexpected Hacker News topstories response type: %s", type(story_ids))
            return []
    except requests.Timeout:
        logger.warning("Timeout (%ds) connecting to Hacker News topstories endpoint.", timeout)
        return []
    except requests.RequestException as req_err:
        logger.warning("HTTP error connecting to Hacker News API: %s", req_err)
        return []
    except Exception as exc:
        logger.warning("Unexpected error fetching Hacker News top stories: %s", exc)
        return []

    # Check top candidate stories up to max_scan to gather limit items with score > min_score
    max_scan = max(limit * 4, 30)
    candidate_ids = story_ids[:max_scan]
    logger.debug("Scanning up to %d Hacker News story IDs for items with score > %d...", len(candidate_ids), min_score)

    with requests.Session() as session:
        session.headers.update(headers)

        for story_id in candidate_ids:
            if len(items) >= limit:
                break

            story_url = f"{HACKER_NEWS_API}/item/{story_id}.json"
            try:
                item_resp = session.get(story_url, timeout=timeout)
                if item_resp.status_code != 200:
                    continue

                story_data = item_resp.json()
                if not story_data or not isinstance(story_data, dict):
                    continue

                # Ensure it is a valid active story
                if story_data.get("type") != "story":
                    continue
                if story_data.get("deleted") or story_data.get("dead"):
                    continue

                score = story_data.get("score", 0)
                if score <= min_score:
                    continue

                title = str(story_data.get("title", "")).strip()
                if not title:
                    continue

                actual_id = story_data.get("id", story_id)
                hn_item_url = f"https://news.ycombinator.com/item?id={actual_id}"
                url = str(story_data.get("url") or hn_item_url).strip()

                time_val = story_data.get("time")
                if time_val and isinstance(time_val, (int, float)):
                    try:
                        published = datetime.fromtimestamp(time_val, tz=timezone.utc).isoformat()
                    except Exception:
                        published = datetime.now(timezone.utc).isoformat()
                else:
                    published = datetime.now(timezone.utc).isoformat()

                by_user = story_data.get("by", "unknown")
                comments_count = story_data.get("descendants", 0)
                raw_text = str(story_data.get("text", "") or "")
                clean_text = _clean_html(raw_text) if raw_text else ""

                summary = clean_text or f"Hacker News story by {by_user} with {score} points and {comments_count} comments."
                raw_content = raw_text or summary

                item: NewsItem = {
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published": published,
                    "source": "Hacker News",
                    "raw_content": raw_content,
                }
                items.append(item)
                logger.debug("Accepted HN story #%d: '%s' (score=%d)", actual_id, title, score)

            except requests.Timeout:
                logger.debug("Timeout fetching HN item %s, skipping.", story_id)
                continue
            except requests.RequestException as req_err:
                logger.debug("Request error fetching HN item %s: %s", story_id, req_err)
                continue
            except Exception as item_err:
                logger.debug("Error processing HN item %s: %s", story_id, item_err)
                continue

    logger.info("Fetched %d qualifying Hacker News stories (score > %d).", len(items), min_score)
    return items


def fetch_github_trending(
    limit: Optional[int] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Scrape GitHub trending page for current trending repositories.

    Extracts repo name, description, stars, language, and URL using
    requests and BeautifulSoup, formatted as standardized news items.

    Args:
        limit: Optional maximum number of repositories to return.
        timeout: Network timeout in seconds per request. Defaults to 15.

    Returns:
        List of dictionaries with keys: title, url, summary, published, source, raw_content.
    """
    items: List[Dict[str, Any]] = []
    headers = _get_headers()

    logger.info("Scraping GitHub trending repositories from %s...", GITHUB_TRENDING_URL)

    try:
        response = requests.get(GITHUB_TRENDING_URL, headers=headers, timeout=timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.find_all("article", class_="Box-row")
        logger.debug("Found %d repository rows on GitHub trending page.", len(articles))

        for art in articles:
            if limit is not None and len(items) >= limit:
                break

            try:
                h2 = art.find("h2")
                if not h2:
                    continue

                a_tag = h2.find("a")
                if not a_tag or not a_tag.get("href"):
                    continue

                repo_path = a_tag["href"].strip("/")
                if not repo_path:
                    continue

                url = f"https://github.com/{repo_path}"

                # Description
                p_tag = art.find("p")
                description = p_tag.get_text(strip=True) if p_tag else ""

                # Programming Language
                lang_span = art.find("span", itemprop="programmingLanguage")
                language = lang_span.get_text(strip=True) if lang_span else "Unknown"

                # Stars
                stars_a = art.find("a", href=lambda h: bool(h and h.endswith("/stargazers")))
                stars = stars_a.get_text(strip=True).replace(",", "") if stars_a else "0"

                # Stars gained today/period (if available)
                stars_today_span = art.find("span", class_=lambda c: bool(c and "float-sm-right" in c))
                stars_today = stars_today_span.get_text(strip=True) if stars_today_span else ""

                title = f"{repo_path} ({language})" if language != "Unknown" else repo_path

                summary_parts: List[str] = []
                if description:
                    summary_parts.append(description)

                stats_line = f"Language: {language} | Stars: {stars}"
                if stars_today:
                    stats_line += f" ({stars_today})"
                summary_parts.append(stats_line)

                summary = "\n\n".join(summary_parts)
                published = datetime.now(timezone.utc).isoformat()

                raw_content = (
                    f"Repository: {repo_path}\n"
                    f"Description: {description}\n"
                    f"Language: {language}\n"
                    f"Stars: {stars}\n"
                    f"URL: {url}"
                )

                item: NewsItem = {
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published": published,
                    "source": "GitHub Trending",
                    "raw_content": raw_content,
                }
                items.append(item)

            except Exception as row_err:
                logger.warning("Error parsing a GitHub trending repository row: %s", row_err)
                continue

        logger.info("Fetched %d repositories from GitHub Trending.", len(items))

    except requests.Timeout:
        logger.warning("Timeout (%ds) scraping GitHub Trending page.", timeout)
    except requests.RequestException as req_err:
        logger.warning("HTTP error scraping GitHub Trending page: %s", req_err)
    except Exception as exc:
        logger.warning("Unexpected error scraping GitHub Trending: %s", exc)

    return items


def fetch_devpost_hackathons(timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """Fetch live hackathons with major cash prizes from Devpost API.

    Extracts title, prize pool (e.g. $100,000), location, and deadline.

    Returns:
        List of formatted hackathon dictionaries with high importance.
    """
    items: List[Dict[str, Any]] = []
    try:
        headers = _get_headers()
        logger.info("Fetching live prize hackathons from Devpost API...")
        response = requests.get(DEVPOST_API, headers=headers, timeout=timeout)
        if response.status_code == 200:
            data = response.json()
            hackathons = data.get("hackathons", [])
            for h in hackathons:
                title = str(h.get("title", "")).strip()
                url = str(h.get("url", "")).strip()
                raw_prize = str(h.get("prize_amount", ""))
                prize = _clean_html(raw_prize).strip()
                location_data = h.get("displayed_location") or {}
                location = (
                    location_data.get("location", "Online")
                    if isinstance(location_data, dict)
                    else "Online"
                )
                time_left = str(h.get("time_left_to_submission", "")).strip()
                themes = [
                    t.get("name")
                    for t in h.get("themes", [])
                    if isinstance(t, dict) and t.get("name")
                ]
                theme_str = ", ".join(themes) if themes else "Technology & Coding"

                if not title or not url:
                    continue

                summary = (
                    f"🏆 Prize Pool: {prize or 'Cash Prizes'}\n"
                    f"📍 Location: {location}\n"
                    f"⏳ Status: {time_left or 'Open for registration'}\n"
                    f"🎯 Focus Areas: {theme_str}\n"
                    f"Compete online, build innovative projects, and win from the prize pool!"
                )

                items.append({
                    "title": f"🏆 Hackathon: {title} ({prize or 'Cash Prizes'})",
                    "url": url,
                    "summary": summary,
                    "published": datetime.now(timezone.utc).isoformat(),
                    "source": "Devpost",
                    "raw_content": summary,
                    "category": "hackathon",
                    "importance": 10 if any(sym in prize for sym in ("$", "€", "£")) else 9,
                })
            logger.info("Retrieved %d prize hackathons from Devpost.", len(items))
    except Exception as exc:
        logger.warning("Error fetching Devpost hackathons: %s", exc)

    return items


def fetch_all_news() -> List[Dict[str, Any]]:
    """Master function that aggregates news items from all sources.

    Fetches articles from RSS feeds, Devpost prize hackathons,
    top stories from Hacker News, and trending repositories from GitHub.
    Handles errors per source to ensure partial failures do not prevent
    returning items from surviving sources.

    Returns:
        Unified list of news item dictionaries from all configured sources.
    """
    logger.info("Executing master fetch across all news sources...")
    all_news: List[Dict[str, Any]] = []

    # 1. Devpost Live Hackathons (Highest Priority)
    devpost_items: List[Dict[str, Any]] = []
    try:
        devpost_items = fetch_devpost_hackathons()
    except Exception as exc:
        logger.error("Unexpected error in fetch_devpost_hackathons: %s", exc)

    # 2. RSS Feeds (Scholarships, MIT, Future Tech, Google News)
    rss_items: List[Dict[str, Any]] = []
    try:
        rss_items = fetch_rss_feeds()
    except Exception as exc:
        logger.error("Unexpected error in fetch_rss_feeds: %s", exc)

    # 3. Hacker News
    hn_items: List[Dict[str, Any]] = []
    try:
        hn_items = fetch_hacker_news(limit=10)
    except Exception as exc:
        logger.error("Unexpected error in fetch_hacker_news: %s", exc)

    # 4. GitHub Trending
    gh_items: List[Dict[str, Any]] = []
    try:
        gh_items = fetch_github_trending()
    except Exception as exc:
        logger.error("Unexpected error in fetch_github_trending: %s", exc)

    # Put high-value Hackathons and Scholarships first
    all_news.extend(devpost_items)
    all_news.extend(rss_items)
    all_news.extend(hn_items)
    all_news.extend(gh_items)

    logger.info(
        "News aggregation finished: %d total items (Devpost: %d, RSS: %d, Hacker News: %d, GitHub: %d).",
        len(all_news),
        len(devpost_items),
        len(rss_items),
        len(hn_items),
        len(gh_items),
    )
    return all_news


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = fetch_all_news()
    print(f"Total aggregated news items: {len(results)}")
    if results:
        print("Sample item:", results[0])
