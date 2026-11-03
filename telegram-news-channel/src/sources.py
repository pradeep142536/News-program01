"""News sources configuration and categorization metadata.

Defines RSS feed endpoints, API endpoints, external URLs, and channel category metadata
with corresponding emojis, human-readable labels, and hashtags.
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict


class CategoryInfo(TypedDict):
    """Metadata definition for a news category."""

    emoji: str
    label: str
    hashtags: List[str]


# RSS Feeds by tech topic and publication
RSS_FEEDS: Dict[str, str] = {
    # High-Value Hackathons & Competitions
    "Google News Hackathons": (
        "https://news.google.com/rss/search?q=hackathon+prize+OR+%22coding+contest%22+OR+%22developer+challenge%22&hl=en-US&gl=US&ceid=US:en"
    ),

    # High-Value Scholarships & Free Money
    "Google News Scholarships": (
        "https://news.google.com/rss/search?q=scholarship+students+OR+%22fully+funded+scholarship%22+OR+%22fellowship+program%22&hl=en-US&gl=US&ceid=US:en"
    ),

    # MIT News & Research Breakthroughs
    "MIT News": "https://news.mit.edu/rss/feed",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "Google News MIT": (
        "https://news.google.com/rss/search?q=%22MIT%22+research+OR+breakthrough+OR+admissions&hl=en-US&gl=US&ceid=US:en"
    ),

    # Major Tech & Big Inventions
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Wired": "https://www.wired.com/feed/rss",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",

    # Future Tech & Discoveries
    "Google News Future Tech": (
        "https://news.google.com/rss/search?q=%22artificial+intelligence%22+breakthrough+OR+%22quantum+computer%22+OR+robotics+breakthrough&hl=en-US&gl=US&ceid=US:en"
    ),
    "Science Daily": "https://www.sciencedaily.com/rss/all.xml",

    # Only Major Cyber Attacks (Filtered to avoid boring patches)
    "Google News Major Cyber Attacks": (
        "https://news.google.com/rss/search?q=%22data+breach%22+million+users+OR+%22critical+zero-day%22+iOS+OR+Windows+-patch+-webinar&hl=en-US&gl=US&ceid=US:en"
    ),

    # Startups & Business Opportunities
    "TechStartups": "https://techstartups.com/feed/",
    "Startup Daily": "https://www.startupdaily.net/feed/",
}

# External API and Trending URLs
HACKER_NEWS_API: str = "https://hacker-news.firebaseio.com/v0"
GITHUB_TRENDING_URL: str = "https://github.com/trending"
PRODUCTHUNT_URL: str = "https://www.producthunt.com"
DEVPOST_API: str = "https://devpost.com/api/hackathons"

# Scholarship & Opportunity Feeds (Verified working 200 OK)
SCHOLARSHIP_FEEDS: Dict[str, str] = {
    "Scholars4Dev": "https://www.scholars4dev.com/feed/",
    "ScholarshipOwl": "https://scholarshipowl.com/blog/feed/",
    "Opportunity Desk": "https://opportunitydesk.org/feed/",
}

# Hackathon & Competition Feeds
HACKATHON_FEEDS: Dict[str, str] = {}

# Categories mapping with UI emojis, labels, and relevant hashtags
CATEGORIES: Dict[str, CategoryInfo] = {
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


def get_category_info(category_key: str) -> CategoryInfo:
    """Retrieve metadata for a category, returning a default if unknown.

    Args:
        category_key: Category key string (e.g. 'trending_tech').

    Returns:
        CategoryInfo dict containing emoji, label, and hashtags.
    """
    return CATEGORIES.get(
        category_key,
        {
            "emoji": "📰",
            "label": category_key.replace("_", " ").title(),
            "hashtags": ["#Tech", "#News"],
        },
    )
