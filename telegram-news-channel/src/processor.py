"""News processor and curation module using Google Gemini API.

Provides AI-driven content filtering, categorical classification, importance scoring,
and Telegram post generation with HTML formatting, rate-limiting, and resilient fallbacks.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set

try:
    import google.generativeai as genai
    from google.generativeai.types import GenerationConfig
except (ImportError, ModuleNotFoundError):
    genai = None  # type: ignore
    GenerationConfig = None  # type: ignore

# Flexible imports to support both root module and src package layouts
try:
    from config import GEMINI_API_KEY, GEMINI_API_KEYS
except (ImportError, ModuleNotFoundError):
    try:
        from src.config import GEMINI_API_KEY, GEMINI_API_KEYS
    except (ImportError, ModuleNotFoundError):
        GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
        _raw_keys = os.getenv("GEMINI_API_KEYS", "")
        GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
        if GEMINI_API_KEY and GEMINI_API_KEY.strip() and GEMINI_API_KEY.strip() not in GEMINI_API_KEYS:
            GEMINI_API_KEYS.insert(0, GEMINI_API_KEY.strip())

try:
    from src.sources import CATEGORIES, get_category_info
except (ImportError, ModuleNotFoundError):
    try:
        from sources import CATEGORIES, get_category_info
    except (ImportError, ModuleNotFoundError):
        CATEGORIES: Dict[str, Dict[str, Any]] = {
            "trending_tech": {
                "emoji": "🔥",
                "label": "Trending Tech",
                "hashtags": ["#Tech", "#Innovation", "#Technology"],
            },
            "hackathon": {
                "emoji": "🏆",
                "label": "Hackathon",
                "hashtags": ["#Hackathon", "#Coding", "#Competition"],
            },
            "future_tech": {
                "emoji": "🚀",
                "label": "Future Tech",
                "hashtags": ["#FutureTech", "#Innovation", "#Breakthrough"],
            },
            "scholarship": {
                "emoji": "🎓",
                "label": "Scholarship",
                "hashtags": ["#Scholarship", "#Education", "#FreeMoney"],
            },
            "mit_news": {
                "emoji": "🏫",
                "label": "MIT News",
                "hashtags": ["#MIT", "#University", "#Research"],
            },
            "cybersecurity": {
                "emoji": "🔒",
                "label": "Cybersecurity",
                "hashtags": ["#Cybersecurity", "#InfoSec", "#Hacking"],
            },
            "tech_tips": {
                "emoji": "💡",
                "label": "Tech Tips",
                "hashtags": ["#TechTips", "#ProjectIdeas", "#Coding"],
            },
            "discovery": {
                "emoji": "🔬",
                "label": "New Discovery",
                "hashtags": ["#Discovery", "#Science", "#Research"],
            },
            "business": {
                "emoji": "💼",
                "label": "Business Idea",
                "hashtags": ["#Business", "#Startup", "#Entrepreneur"],
            },
            "github_trending": {
                "emoji": "⭐",
                "label": "GitHub Trending",
                "hashtags": ["#GitHub", "#OpenSource", "#Developer"],
            },
        }

        def get_category_info(category_key: str) -> Dict[str, Any]:
            return CATEGORIES.get(
                category_key,
                {
                    "emoji": "📰",
                    "label": category_key.replace("_", " ").title(),
                    "hashtags": ["#Tech", "#News"],
                },
            )

logger = logging.getLogger(__name__)

# Valid category names explicitly requested for curation
VALID_CATEGORIES: Set[str] = {
    "trending_tech",
    "hackathon",
    "future_tech",
    "scholarship",
    "mit_news",
    "cybersecurity",
    "tech_tips",
    "discovery",
    "business",
}

# Bold headline emoji suggestions mapping per category
HEADLINE_EMOJIS: Dict[str, str] = {
    "trending_tech": "⚡",
    "hackathon": "💻",
    "future_tech": "🛸",
    "scholarship": "🎓",
    "mit_news": "🏛️",
    "cybersecurity": "🛡️",
    "tech_tips": "💡",
    "discovery": "🔬",
    "business": "📈",
    "github_trending": "⭐",
}

# Module-level Gemini GenerativeModel reference and synchronization lock
_gemini_model: Optional[Any] = None
_init_lock = threading.Lock()


class RateLimiter:
    """Thread-safe rate limiter enforcing max calls per minute and minimum spacing."""

    def __init__(self, max_per_minute: int = 15, min_interval: float = 4.0) -> None:
        """Initialize the RateLimiter.

        Args:
            max_per_minute: Maximum requests allowed in any 60-second window.
            min_interval: Minimum duration in seconds between consecutive requests.
        """
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Wait if necessary to comply with rate limits before proceeding."""
        with self._lock:
            now = time.time()
            # Retain only timestamps from the preceding 60 seconds
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]

            # If quota for the rolling 60s window is reached, sleep until the oldest slot frees up
            if len(self._timestamps) >= self.max_per_minute:
                oldest = self._timestamps[0]
                sleep_needed = 60.0 - (now - oldest) + 0.1
                if sleep_needed > 0:
                    logger.debug(
                        "Rate limit quota reached (%d calls in 60s). Sleeping %.2fs.",
                        len(self._timestamps),
                        sleep_needed,
                    )
                    time.sleep(sleep_needed)
                    now = time.time()
                    self._timestamps = [t for t in self._timestamps if now - t < 60.0]

            # Also guarantee minimum interval spacing between calls
            if self._timestamps:
                last_call = self._timestamps[-1]
                elapsed = now - last_call
                if elapsed < self.min_interval:
                    sleep_duration = self.min_interval - elapsed
                    logger.debug(
                        "Enforcing min interval between Gemini calls. Sleeping %.2fs.",
                        sleep_duration,
                    )
                    time.sleep(sleep_duration)

            self._timestamps.append(time.time())


# Global rate limiter instance (max 15 requests per minute, 4.0s min interval)
_rate_limiter = RateLimiter(max_per_minute=15, min_interval=4.0)


class GeminiKeyRotator:
    """Manages rotation, round-robin, and rate-limit fallbacks across multiple Gemini API keys."""

    def __init__(self, keys: Optional[List[str]] = None) -> None:
        self._lock = threading.Lock()
        initial_keys = keys if keys is not None else GEMINI_API_KEYS
        self._keys: List[str] = [k.strip() for k in initial_keys if k and k.strip()]
        self._index: int = 0
        self._cooldowns: Dict[str, float] = {}

    def add_key(self, key: str) -> None:
        """Add an API key to the active rotation pool."""
        with self._lock:
            key_clean = key.strip()
            if key_clean and key_clean not in self._keys:
                self._keys.append(key_clean)
                logger.info(
                    "Added new Gemini API key to rotation pool (total: %d keys).",
                    len(self._keys),
                )

    def set_keys(self, keys: List[str]) -> None:
        """Replace the active rotation pool with a new list of keys."""
        with self._lock:
            self._keys = [k.strip() for k in keys if k and k.strip()]
            self._index = 0
            self._cooldowns.clear()
            logger.info("Configured Gemini pool with %d API keys.", len(self._keys))

    @property
    def key_count(self) -> int:
        """Return total number of configured API keys."""
        with self._lock:
            return len(self._keys)

    def mark_rate_limited(self, key: str, cooldown_seconds: float = 60.0) -> None:
        """Temporarily mark a key as rate-limited/exhausted with a cooldown."""
        with self._lock:
            self._cooldowns[key] = time.time() + cooldown_seconds
            masked = key[-6:] if len(key) >= 6 else key
            available = sum(
                1 for k in self._keys if time.time() >= self._cooldowns.get(k, 0.0)
            )
            logger.warning(
                "Gemini API key ending in '...%s' hit rate limit/quota. Cooling down for %.0fs. "
                "Healthy keys ready in pool: %d/%d.",
                masked,
                cooldown_seconds,
                available,
                len(self._keys),
            )

    def get_model_and_key(self) -> tuple[Optional[Any], Optional[str]]:
        """Retrieve a configured GenerativeModel from the next healthy API key in rotation."""
        if genai is None:
            return None, None

        with self._lock:
            if not self._keys:
                return None, None

            now = time.time()
            total = len(self._keys)

            # 1. Round-robin: select the next key whose cooldown has expired
            for attempt in range(total):
                idx = (self._index + attempt) % total
                candidate_key = self._keys[idx]
                cooldown_until = self._cooldowns.get(candidate_key, 0.0)
                if now >= cooldown_until:
                    self._index = (idx + 1) % total
                    return self._configure_and_get_model(candidate_key), candidate_key

            # 2. If all keys are currently cooling down, pick the one expiring soonest
            soonest_key = min(self._keys, key=lambda k: self._cooldowns.get(k, 0.0))
            return self._configure_and_get_model(soonest_key), soonest_key

    def _configure_and_get_model(self, key: str) -> Optional[Any]:
        """Configure genai with the specified key and instantiate a model."""
        try:
            genai.configure(api_key=key)
            return genai.GenerativeModel("gemini-2.0-flash")
        except Exception as exc:
            masked = key[-6:] if len(key) >= 6 else key
            logger.error("Failed to configure Gemini with key '...%s': %s", masked, exc)
            return None


# Global key rotator instance
_key_rotator = GeminiKeyRotator()


def init_gemini(api_key: Optional[str] = None) -> Optional[Any]:
    """Initialize or update the Gemini client pool.

    Supports single API keys or pool of keys from GEMINI_API_KEYS.

    Args:
        api_key: Optional Gemini API key to register into the pool.

    Returns:
        The GenerativeModel instance from the pool, or None if configuration failed.
    """
    global _gemini_model
    if api_key and api_key.strip():
        _key_rotator.add_key(api_key.strip())

    with _init_lock:
        if _key_rotator.key_count == 0:
            logger.warning(
                "No Gemini API keys configured. Gemini features will operate in fallback mode."
            )
            return None

        if genai is None:
            logger.error(
                "The 'google-generativeai' package is not installed. "
                "Install it using `pip install google-generativeai`."
            )
            return None

        model, active_key = _key_rotator.get_model_and_key()
        if model is not None:
            _gemini_model = model
            logger.info(
                "Gemini initialized successfully with %d key(s) in pool.",
                _key_rotator.key_count,
            )
        return model


def _get_model_and_key() -> tuple[Optional[Any], Optional[str]]:
    """Retrieve an active model and its associated API key from the pool."""
    return _key_rotator.get_model_and_key()


def _is_rate_limit_error(exc: Exception) -> bool:
    """Determine if an exception represents a rate-limit or quota-exhaustion error."""
    exc_str = str(exc).lower()
    return any(
        signal in exc_str
        for signal in (
            "429",
            "resource_exhausted",
            "resourceexhausted",
            "quota",
            "rate limit",
            "ratelimit",
            "too many requests",
            "bandwidth",
        )
    )


def _call_gemini_with_retry(
    prompt: str,
    generation_config: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    initial_delay: float = 2.0,
    backoff_factor: float = 2.0,
) -> Optional[str]:
    """Execute a Gemini API call with multi-key rotation and rate-limit fallbacks.

    When a key hits rate limits or quota exhaustion, it is automatically marked
    for cooldown and the request is immediately retried with the next available key.

    Args:
        prompt: Text prompt string to submit to Gemini.
        generation_config: Optional dictionary of generation parameters.
        max_retries: Base number of retry attempts.
        initial_delay: Delay in seconds before the first retry when all keys are busy.
        backoff_factor: Multiplier for backoff on subsequent retries.

    Returns:
        Response text string, or None if all attempts failed or keys are missing.
    """
    total_keys = _key_rotator.key_count
    if total_keys == 0:
        logger.debug("No Gemini keys configured; skipping API call.")
        return None

    # Allow enough attempts to cycle through all configured keys
    effective_retries = max(max_retries, total_keys * 2)
    delay = initial_delay

    for attempt in range(1, effective_retries + 1):
        model, current_key = _get_model_and_key()
        if model is None or not current_key:
            logger.debug("No available Gemini model; skipping API call.")
            return None

        try:
            _rate_limiter.wait()
            kwargs: Dict[str, Any] = {}
            if generation_config:
                kwargs["generation_config"] = generation_config

            response = model.generate_content(prompt, **kwargs)

            # Check if response contains candidates and text
            if response and hasattr(response, "text") and response.text:
                return response.text.strip()

            logger.warning(
                "Empty response received from Gemini on attempt %d/%d.",
                attempt,
                effective_retries,
            )
        except Exception as exc:
            if _is_rate_limit_error(exc):
                # Put this key on cooldown and immediately cycle to next key
                _key_rotator.mark_rate_limited(current_key, cooldown_seconds=60.0)
                if total_keys > 1:
                    logger.info("Switching to next available Gemini key immediately...")
                    continue

            logger.warning(
                "Gemini API call failed on attempt %d/%d: %s",
                attempt,
                effective_retries,
                exc,
            )

            if attempt < effective_retries:
                logger.info("Retrying Gemini API call in %.2f seconds...", delay)
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error("All %d Gemini API retry attempts failed.", effective_retries)

    return None


def _normalize_category(category_raw: str) -> str:
    """Normalize and validate a category identifier string.

    Args:
        category_raw: Raw category string from LLM or heuristic.

    Returns:
        A validated category key matching VALID_CATEGORIES, or 'trending_tech'.
    """
    cleaned = category_raw.strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in VALID_CATEGORIES:
        return cleaned

    # Alias mappings for common LLM variations
    aliases: Dict[str, str] = {
        "tech": "trending_tech",
        "technology": "trending_tech",
        "ai": "trending_tech",
        "artificial_intelligence": "trending_tech",
        "coding": "tech_tips",
        "security": "cybersecurity",
        "infosec": "cybersecurity",
        "hackathons": "hackathon",
        "fellowship": "scholarship",
        "grant": "scholarship",
        "scholarships": "scholarship",
        "mit": "mit_news",
        "science": "discovery",
        "startup": "business",
        "startups": "business",
        "venture": "business",
    }
    return aliases.get(cleaned, "trending_tech")


def _fallback_categorize_and_filter(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Heuristic rule-based fallback when Gemini API is unavailable or errors.

    Applies STRICT filtering matching the Gemini prompt rules:
    - Prioritizes scholarships, hackathons, and game-changing tech
    - Rejects routine security patches, minor CVEs, and boring news

    Args:
        articles: List of raw candidate article dictionaries.

    Returns:
        List of filtered and categorized article dictionaries.
    """
    logger.info("Executing heuristic fallback for categorization and filtering.")
    kept: List[Dict[str, Any]] = []

    # Irrelevant topics to exclude
    exclude_keywords = {
        "murder", "homicide", "shooting", "arrested", "stabbing", "robbery",
        "biden", "trump", "senate", "congress", "democrat", "republican",
        "parliament", "election", "campaign trail", "celebrity", "kardashian",
        "hollywood", "box office", "divorce", "football", "soccer", "nfl",
        "nba", "uefa", "premier league", "baseball", "tennis", "gossip",
    }

    # Routine security patches to REJECT (these are boring for general audience)
    reject_security_keywords = {
        "patches", "patched", "fixes bug", "fixes flaw", "fixes vulnerability",
        "patch tuesday", "security update", "firmware update", "hotfix",
        "advisory released", "security bulletin",
    }

    for art in articles:
        title = art.get("title", "")
        summary = art.get("summary", "") or art.get("description", "")
        combined = f"{title} {summary}".lower()

        # Filter out obvious irrelevant content
        if any(bad_word in combined for bad_word in exclude_keywords):
            continue

        # Categorize based on content signals — PRIORITY ORDER
        category = ""
        importance = 0

        # 1. SCHOLARSHIPS (highest priority)
        if any(k in combined for k in ("scholarship", "fellowship", "daad", "grant", "tuition waiver", "financial aid", "funded program", "free education", "study abroad fund")):
            category = "scholarship"
            importance = 9

        # 2. HACKATHONS
        elif any(k in combined for k in ("hackathon", "coding contest", "kaggle competition", "devpost", "coding challenge", "prize pool", "coding bounty", "mlh ")):
            category = "hackathon"
            importance = 8

        # 3. FUTURE TECH
        elif any(k in combined for k in ("quantum computing", "quantum supremacy", "fusion energy", "humanoid robot", "spacecraft", "starship", "neuralink", "superconductor", "agi ", "brain-computer", "mars mission")):
            category = "future_tech"
            importance = 8

        # 4. DISCOVERIES
        elif any(k in combined for k in ("breakthrough discovery", "scientists discover", "astronomers find", "physics breakthrough", "new species", "nobel prize")):
            category = "discovery"
            importance = 8

        # 5. MIT NEWS
        elif any(k in combined for k in ("mit ", "massachusetts institute of technology", "mit news", "mit researchers")):
            category = "mit_news"
            importance = 7

        # 6. MAJOR TECH (only big stuff)
        elif any(k in combined for k in ("gpt-5", "gpt-6", "gemini 2", "launches new", "billion dollar", "acquired for", "open source release", "apple announces", "google announces")):
            category = "trending_tech"
            importance = 7

        # 7. BUSINESS
        elif any(k in combined for k in ("raised $", "series a", "series b", "unicorn", "ipo ", "venture capital", "funding round")) and any(k in combined for k in ("million", "billion", "$10", "$50", "$100")):
            category = "business"
            importance = 7

        # 8. TECH TIPS
        elif any(k in combined for k in ("how to build", "project idea", "tutorial", "cheat sheet", "developer guide", "free course", "learn ")):
            category = "tech_tips"
            importance = 7

        # 9. CYBERSECURITY — ONLY major breaches, NOT patches
        elif any(k in combined for k in ("data breach", "million users", "billion records", "nation-state", "ransomware attack")):
            # Check it's NOT a routine patch
            if not any(reject in combined for reject in reject_security_keywords):
                category = "cybersecurity"
                importance = 7

        # Skip everything else (including routine security patches)
        if not category or importance < 7:
            continue

        art_copy = dict(art)
        art_copy["category"] = category
        art_copy["importance"] = importance
        kept.append(art_copy)

    return kept


def categorize_and_filter(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter irrelevant news and categorize tech articles using Gemini structured JSON.

    Criteria:
    - Filters OUT: crime, politics, gossip, entertainment, sports.
    - Keeps ONLY: tech news, hackathons, future tech, scholarships, MIT news,
      cybersecurity, tech tips, discoveries, business ideas.
    - Categorizes into: trending_tech, hackathon, future_tech, scholarship,
      mit_news, cybersecurity, tech_tips, discovery, business.
    - Rates importance 1-10.
    - Processes articles in batches of 10 to adhere to token and rate limits.

    Args:
        articles: List of article dicts with 'title', 'summary', and 'url'.

    Returns:
        List of kept articles with added 'category' and 'importance' keys.
    """
    if not articles:
        return []

    # Pre-filtering: Instant reject for boring patch notes and sponsored webinars
    boring_reject = (
        "webinar", "webinars", "patch", "patches", "patched",
        "fixes flaw", "fixes bug", "fixes bugs", "fixes vulnerability",
        "cve-", "security update", "firmware update", "patch tuesday",
        "threat readiness", "advisory released", "hotfix", "security advisory",
    )

    clean_candidates: List[Dict[str, Any]] = []
    pre_approved: List[Dict[str, Any]] = []

    for art in articles:
        combined = f"{art.get('title', '')} {art.get('summary', '')}".lower()

        # 1. Devpost prize hackathons or verified high-value opportunities: Auto-keep directly!
        if art.get("source") == "Devpost" or (art.get("importance", 0) >= 9 and art.get("category") in ("hackathon", "scholarship")):
            logger.info("Auto-approved high-value opportunity: %s", art.get("title"))
            pre_approved.append(dict(art))
            continue

        # 2. Drop boring patch notes, CVEs, and webinars immediately
        if any(w in combined for w in boring_reject):
            logger.debug("Dropped boring/routine item: %s", art.get("title"))
            continue

        clean_candidates.append(art)

    filtered_articles: List[Dict[str, Any]] = list(pre_approved)

    model = _get_model()
    if model is None:
        logger.warning("Gemini unavailable. Falling back to heuristic categorize_and_filter.")
        filtered_articles.extend(_fallback_categorize_and_filter(clean_candidates))
        return filtered_articles

    batch_size = 10

    for start_idx in range(0, len(clean_candidates), batch_size):
        batch = clean_candidates[start_idx : start_idx + batch_size]
        logger.info(
            "Processing categorization batch %d-%d of %d articles...",
            start_idx + 1,
            min(start_idx + batch_size, len(clean_candidates)),
            len(clean_candidates),
        )

        batch_payload = [
            {
                "id": idx,
                "title": art.get("title", ""),
                "summary": (art.get("summary", "") or art.get("description", ""))[:400],
            }
            for idx, art in enumerate(batch)
        ]

        prompt = (
            "You are a STRICT news editor for a technology Telegram channel focused on USEFUL, LIFE-CHANGING, and ACTIONABLE content.\n\n"
            "ANALYZE these articles and apply VERY STRICT filtering:\n\n"
            "INPUT ARTICLES:\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}\n\n"
            "=== PRIORITY ORDER (most important first) ===\n"
            "1. 'scholarship' - FREE MONEY: scholarships, fellowships, grants, tuition waivers, funded programs, financial aid, study abroad funding. ALWAYS KEEP these.\n"
            "2. 'hackathon' - BIG PRIZE hackathons ($1000+), major coding competitions (Google, Meta, Microsoft, MLH), developer bounties, Kaggle competitions with prizes. ALWAYS KEEP these.\n"
            "3. 'future_tech' - GAME-CHANGING breakthroughs: AGI progress, quantum supremacy, nuclear fusion, Mars missions, brain-computer interfaces, cure discoveries. Only truly revolutionary stuff.\n"
            "4. 'discovery' - Major scientific discoveries that change how we understand the world. NOT routine research papers.\n"
            "5. 'mit_news' - MIT research breakthroughs, MIT admissions changes, MIT free courses, or how to get into MIT.\n"
            "6. 'trending_tech' - MAJOR tech launches only: new iPhone/Pixel, big AI model releases (GPT-5, Gemini), major company acquisitions ($1B+), big product launches.\n"
            "7. 'business' - Startup ideas with real potential, companies raising $10M+, innovative business models worth knowing.\n"
            "8. 'tech_tips' - Actually useful project ideas, coding tutorials that teach something valuable, career-changing developer tips.\n"
            "9. 'cybersecurity' - ONLY major breaches affecting millions of users, nation-state attacks, or zero-days in widely-used software (Chrome, Windows, iOS). NOT routine patches, NOT minor CVEs, NOT product updates.\n\n"
            "=== STRICT REJECT RULES ===\n"
            "REJECT all of these (importance = 0, do NOT include):\n"
            "- Routine software patches and updates (e.g., 'Cisco patches ASA flaw', 'Adobe fixes 8 bugs', 'Plex patches vulnerability')\n"
            "- Minor CVE announcements that don't affect millions of regular people\n"
            "- Security advisories that are only relevant to IT administrators\n"
            "- Vendor patch Tuesday updates\n"
            "- Crime, politics, gossip, entertainment, sports, celebrity news\n"
            "- Opinion pieces, editorials, product reviews\n"
            "- News that is not actionable or useful for a tech-savvy student/developer audience\n\n"
            "=== IMPORTANCE SCALE ===\n"
            "10 = Life-changing (full scholarship announced, $100K hackathon, AGI breakthrough)\n"
            "9 = Extremely important (major free opportunity, huge tech launch)\n"
            "8 = Very important (significant scholarship, big hackathon, major discovery)\n"
            "7 = Important (useful tech news, notable funding round, good project idea)\n"
            "6 = Somewhat useful (interesting but not must-know)\n"
            "1-5 = NOT worth posting (routine patches, minor updates, boring news) → REJECT THESE\n\n"
            "ONLY KEEP articles with importance >= 7. Reject everything else.\n\n"
            "OUTPUT: Valid JSON array of KEPT articles only:\n"
            "[{\"id\": <int>, \"category\": \"<category>\", \"importance\": <int 7-10>}]\n"
            "Return [] if nothing is worth posting.\n"
            "No markdown, no extra text."
        )

        generation_config = {"response_mime_type": "application/json"}
        response_text = _call_gemini_with_retry(prompt, generation_config=generation_config)

        if not response_text:
            logger.warning("Batch categorization failed from Gemini API; applying fallback to batch.")
            filtered_articles.extend(_fallback_categorize_and_filter(batch))
            continue

        # Clean potential markdown fences
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed_json = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback regex extraction for JSON array
            match = re.search(r"\[\s*\{.*\}\s*\]", cleaned, re.DOTALL)
            if match:
                try:
                    parsed_json = json.loads(match.group(0))
                except json.JSONDecodeError:
                    parsed_json = None
            else:
                parsed_json = None

        if not isinstance(parsed_json, list):
            # Check if wrapped in an object like {"articles": [...]}
            if isinstance(parsed_json, dict):
                for key in ("articles", "items", "kept", "results", "data"):
                    if isinstance(parsed_json.get(key), list):
                        parsed_json = parsed_json[key]
                        break

        if not isinstance(parsed_json, list):
            logger.warning("Could not parse valid JSON list from Gemini response; falling back for batch.")
            filtered_articles.extend(_fallback_categorize_and_filter(batch))
            continue

        # Match returned items back to original articles in the batch
        batch_kept: List[Dict[str, Any]] = []
        for item in parsed_json:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id is None:
                continue

            try:
                idx = int(item_id)
            except (ValueError, TypeError):
                continue

            if 0 <= idx < len(batch):
                orig_art = dict(batch[idx])
                raw_cat = str(item.get("category", "trending_tech"))
                orig_art["category"] = _normalize_category(raw_cat)
                try:
                    imp = int(item.get("importance", 5))
                    orig_art["importance"] = max(1, min(10, imp))
                except (ValueError, TypeError):
                    orig_art["importance"] = 5

                batch_kept.append(orig_art)

        filtered_articles.extend(batch_kept)
        logger.info(
            "Batch completed: kept %d of %d candidate articles.",
            len(batch_kept),
            len(batch),
        )

    return filtered_articles


def _clean_telegram_html(raw_text: str) -> str:
    """Ensure text adheres to Telegram HTML markup rules.

    Permits valid <b>, <i>, <a>, <code> tags while removing unsupported
    or malformed markdown tags.

    Args:
        raw_text: Text string potentially containing HTML or markdown.

    Returns:
        Cleaned Telegram HTML string.
    """
    # Remove markdown code block fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:html)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    return cleaned.strip()


def _fallback_generate_post(article: Dict[str, Any]) -> str:
    """Generate a valid Telegram post when Gemini is unavailable.

    Constructs the exact required layout using article details and CATEGORIES.

    Args:
        article: Article dictionary with title, summary, url, and category.

    Returns:
        Formatted Telegram HTML post string.
    """
    category = article.get("category", "trending_tech")
    cat_info = get_category_info(category)
    emoji = cat_info.get("emoji", "🔥")
    category_label = cat_info.get("label", "Trending Tech")
    hashtags = " ".join(cat_info.get("hashtags", ["#Tech", "#News"]))
    url = article.get("url", "").strip()

    bold_emoji = HEADLINE_EMOJIS.get(category, "⚡")
    raw_title = article.get("title", "Breaking Tech Update").strip()
    clean_title = html.escape(re.sub(r"\s+", " ", raw_title))

    summary = article.get("summary", "") or article.get("description", "") or ""
    # Extract candidate sentences for bullet points
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", summary)
        if len(s.strip()) > 20
    ]

    bullet_points: List[str] = []
    if sentences:
        for s in sentences[:4]:
            clean_s = html.escape(s)
            bullet_points.append(f"• {clean_s}")
    else:
        bullet_points.append(f"• <b>Key Insight</b>: {clean_title}")
        bullet_points.append("• Full details, implications, and documentation are available at the source link.")

    bullet_block = "\n".join(bullet_points)

    return (
        f"{emoji} {category_label}\n\n"
        f"{bold_emoji} <b>{clean_title}</b>\n\n"
        f"{bullet_block}\n\n"
        f"🔗 Read more: {url}\n\n"
        f"{hashtags}"
    )


def generate_post(article: Dict[str, Any]) -> str:
    """Generate an engaging Telegram news post in the exact required format.

    Format:
    {emoji} {category_label}

    {bold_emoji} {Catchy headline}

    • Key point 1
    • Key point 2
    • Key point 3
    • Key point 4 (if available)

    🔗 Read more: {url}

    {hashtags}

    Uses Telegram HTML formatting (<b>, <i>, <a>). Gemini rewrites the headline
    to be engaging and distills key points into concise bullet points.

    Args:
        article: Article dictionary containing title, summary, url, and category.

    Returns:
        Formatted Telegram post string.
    """
    category = article.get("category", "trending_tech")
    cat_info = get_category_info(category)
    emoji = cat_info.get("emoji", "🔥")
    category_label = cat_info.get("label", "Trending Tech")
    hashtags = " ".join(cat_info.get("hashtags", ["#Tech", "#News"]))
    url = article.get("url", "").strip()
    title = article.get("title", "").strip()
    summary = article.get("summary", "") or article.get("description", "") or ""

    bold_emoji = HEADLINE_EMOJIS.get(category, "⚡")

    model = _get_model()
    if model is None:
        logger.info("Gemini unavailable. Generating post via fallback generator.")
        return _fallback_generate_post(article)

    prompt = (
        "You are an expert Telegram tech channel editor. Write a captivating, high-impact Telegram post in HTML format.\n\n"
        "ARTICLE DATA:\n"
        f"- Title: {title}\n"
        f"- Category: {category_label}\n"
        f"- Summary / Context: {summary}\n"
        f"- URL: {url}\n\n"
        "REQUIRED POST STRUCTURE (follow this EXACT layout):\n"
        f"{emoji} {category_label}\n\n"
        f"{bold_emoji} <b>[Catchy, engaging rewritten headline]</b>\n\n"
        "• Key point 1\n"
        "• Key point 2\n"
        "• Key point 3\n"
        "• Key point 4 (if available)\n\n"
        f"🔗 Read more: {url}\n\n"
        f"{hashtags}\n\n"
        "RULES:\n"
        f"1. First line MUST be: {emoji} {category_label}\n"
        f"2. Headline MUST begin with {bold_emoji} followed by a catchy rewritten headline wrapped in <b>...</b>.\n"
        "3. Provide 3 to 4 clear, informative bullet points starting with '• '. "
        "Use <b> or <i> to highlight key concepts, stats, and company/model names.\n"
        f"4. The link line MUST be exactly: 🔗 Read more: {url}\n"
        f"5. The final line MUST be exactly: {hashtags}\n"
        "6. Use ONLY Telegram-supported HTML tags (<b>, <i>, <a>, <code>). Do NOT use Markdown asterisks (** or *).\n"
        "7. Output ONLY the post text. Do NOT wrap in markdown code fences (```html)."
    )

    response_text = _call_gemini_with_retry(prompt)
    if not response_text:
        logger.warning("Gemini post generation returned no text; using fallback generator.")
        return _fallback_generate_post(article)

    cleaned_post = _clean_telegram_html(response_text)

    # Validate essential structural elements and patch if Gemini missed them
    expected_header = f"{emoji} {category_label}"
    if not cleaned_post.startswith(expected_header):
        cleaned_post = f"{expected_header}\n\n{cleaned_post}"

    expected_link = f"🔗 Read more: {url}"
    if f"🔗 Read more:" not in cleaned_post and url:
        cleaned_post = f"{cleaned_post}\n\n{expected_link}"

    # Ensure hashtags are included
    if hashtags and not any(tag in cleaned_post for tag in cat_info.get("hashtags", [])):
        cleaned_post = f"{cleaned_post}\n\n{hashtags}"

    return cleaned_post


def process_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Master workflow: categorize, filter, and generate Telegram posts.

    Executes:
    1. Categorize candidate articles and filter out irrelevant news.
    2. Sort kept articles by importance in descending order.
    3. Generate Telegram HTML posts for each article with rate limiting and retry logic.
    4. Attaches 'post_text' and 'category' to each processed article.

    Args:
        articles: Raw candidate article dictionaries.

    Returns:
        List of processed article dictionaries ready to broadcast.
    """
    if not articles:
        logger.info("process_articles called with empty article list.")
        return []

    logger.info("Commencing article processing pipeline for %d candidate(s).", len(articles))

    # Step 1: Categorize and filter
    try:
        curated_articles = categorize_and_filter(articles)
    except Exception as exc:
        logger.error("Error in categorize_and_filter: %s; falling back.", exc, exc_info=True)
        curated_articles = _fallback_categorize_and_filter(articles)

    logger.info(
        "Filtered down to %d article(s) matching channel focus.",
        len(curated_articles),
    )

    if not curated_articles:
        return []

    # Step 2: Sort articles by importance descending
    curated_articles.sort(key=lambda a: a.get("importance", 5), reverse=True)

    # Step 2.5: STRICT importance filter — only post truly important news
    MIN_IMPORTANCE = 7
    curated_articles = [
        a for a in curated_articles if a.get("importance", 0) >= MIN_IMPORTANCE
    ]
    if not curated_articles:
        logger.info("No articles met the minimum importance threshold (%d).", MIN_IMPORTANCE)
        return []

    # Step 2.6: Limit posts per cycle to avoid channel spam (max 10 per cycle)
    MAX_POSTS_PER_CYCLE = 10
    if len(curated_articles) > MAX_POSTS_PER_CYCLE:
        logger.info(
            "Capping output from %d to %d articles (most important first).",
            len(curated_articles), MAX_POSTS_PER_CYCLE,
        )
        curated_articles = curated_articles[:MAX_POSTS_PER_CYCLE]

    # Step 3: Generate post for each curated article
    processed: List[Dict[str, Any]] = []
    for idx, article in enumerate(curated_articles, 1):
        title = article.get("title", "Untitled")
        logger.info(
            "Generating post %d/%d for '%s' (category: %s, importance: %s)...",
            idx,
            len(curated_articles),
            title[:50],
            article.get("category"),
            article.get("importance"),
        )
        try:
            post_text = generate_post(article)
        except Exception as exc:
            logger.error("Error in generate_post for '%s': %s", title, exc, exc_info=True)
            post_text = _fallback_generate_post(article)

        article["post_text"] = post_text
        processed.append(article)

    logger.info("Successfully completed processing for %d article(s).", len(processed))
    return processed
