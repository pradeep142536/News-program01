# 📡 Telegram News Channel Automation

An automated Python program that continuously monitors 20+ tech news sources, uses **Google Gemini AI** to filter, categorize, and summarize relevant news, and posts it to a **public Telegram channel** with photos/videos — all running on a web server.

## ✨ Features

- 🔄 **Real-time monitoring** — Checks for new news every 5 minutes
- 🤖 **AI-powered curation** — Google Gemini filters irrelevant content and writes engaging summaries
- 📸 **Smart media** — Sends video if available, otherwise photo (auto-extracted from articles)
- 🚫 **No duplicates** — SQLite database tracks sent articles
- 🌐 **20+ sources** — TechCrunch, Hacker News, MIT News, The Verge, Wired, and more
- 🚀 **Railway-ready** — One-click deployment to Railway (free tier)

## 📋 Content Categories

| Emoji | Category | What it covers |
|-------|----------|----------------|
| 🔥 | Trending Tech | Major tech announcements, product launches |
| 🏆 | Hackathons | Coding competitions, company-hosted events |
| 🚀 | Future Tech | AI breakthroughs, quantum computing, space tech |
| 🎓 | Scholarships | Funding opportunities, grants, free money |
| 🏫 | MIT News | MIT research, admissions, university updates |
| 🔒 | Cybersecurity | Hacks, vulnerabilities, security advisories |
| 💡 | Tech Tips | Project ideas, coding tips, tutorials |
| 🔬 | Discoveries | Scientific breakthroughs, inventions |
| 💼 | Business Ideas | Startup ideas, business opportunities |
| ⭐ | GitHub Trending | Hot open-source projects |

## 📂 Project Structure

```
telegram-news-channel/
├── main.py                 # Entry point — Flask server + scheduler
├── config.py               # Environment configuration
├── requirements.txt        # Python dependencies
├── Procfile                # Railway/Heroku deployment
├── railway.json            # Railway config
├── .env.example            # Environment variables template
├── .gitignore              # Git ignore rules
├── README.md               # This file
└── src/
    ├── __init__.py         # Package init
    ├── database.py         # SQLite duplicate prevention
    ├── sources.py          # News source definitions & categories
    ├── fetcher.py          # Fetches news from RSS, APIs, web scraping
    ├── processor.py        # Gemini AI processing & post generation
    ├── media.py            # Photo/video extraction from articles
    └── sender.py           # Telegram channel posting
```

## 🛠️ Setup Guide

### Step 1: Get Your API Keys

#### Telegram Bot Token (required)
1. Open Telegram → Search **@BotFather**
2. Send `/newbot` → Give it a name (e.g., "News Poster")
3. Copy the **bot token** you receive
4. Create a **public channel** (e.g., `@mytechnews`)
5. **Add the bot as Admin** to your channel (Settings → Administrators → Add Admin)

#### Google Gemini API Key (required)
1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Create an API key
3. Copy it

#### Pexels API Key (optional — for fallback images)
1. Go to [Pexels API](https://www.pexels.com/api/new/)
2. Sign up and get a free API key

### Step 2: Configure Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHANNEL_ID=@your_channel_username
GEMINI_API_KEY=your_gemini_api_key
PEXELS_API_KEY=your_pexels_key_optional
CHECK_INTERVAL=5
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Run Locally (Optional)

```bash
python main.py
```

The server will start on `http://localhost:8080` and begin posting news automatically.

## 🚀 Deploy to Railway (Free)

### Method 1: Deploy from GitHub

1. Push this project to a **GitHub repository**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/yourusername/telegram-news-channel.git
   git push -u origin main
   ```

2. Go to [Railway.app](https://railway.app/) → Sign up with GitHub

3. Click **"New Project"** → **"Deploy from GitHub Repo"**

4. Select your repository

5. Add **Environment Variables** in Railway dashboard:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHANNEL_ID` = @your_channel_username
   - `GEMINI_API_KEY` = your Gemini key
   - `PEXELS_API_KEY` = your Pexels key (optional)

6. Railway will auto-detect the `Procfile` and deploy! ✅

### Method 2: Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

## 📡 API Endpoints

Once running, the server exposes these endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /` | App status — `{"status": "running", "app": "Telegram News Channel"}` |
| `GET /health` | Health check with uptime — `{"status": "healthy", "uptime": 3600}` |
| `GET /stats` | Stats — total posts sent, last check time, source count |

## 📝 Post Format Example

```
🔒 Cybersecurity

🔴 Major Data Breach Hits CloudFlare — 10M Users Affected

• CloudFlare disclosed a massive breach affecting 10M accounts
• Hackers exploited a zero-day vulnerability in their auth system
• Users are advised to reset passwords immediately
• The company has patched the vulnerability

🔗 Read more: https://example.com/article

#Cybersecurity #DataBreach #Tech
```

- If **video** is available → posted as native video with caption
- If **no video** → posted as photo with caption

## 📰 News Sources

### Tech News
- TechCrunch, The Verge, Wired, Ars Technica, Engadget, VentureBeat

### MIT & University
- MIT News, MIT Technology Review

### Cybersecurity
- BleepingComputer, The Hacker News, Krebs on Security

### Science & Discovery
- Science Daily, New Scientist

### Aggregators
- Google News (Tech), Google News (Science), Hacker News

### Startups & Business
- TechStartups, Startup Daily, ProductHunt

### Developer
- GitHub Trending

### Scholarships
- Scholarship Positions, DAAD

## ⚙️ Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | ✅ | — | Channel username (e.g., `@mychannel`) |
| `GEMINI_API_KEY` | ✅ | — | Google Gemini API key |
| `PEXELS_API_KEY` | ❌ | — | Pexels API key for fallback images |
| `CHECK_INTERVAL` | ❌ | `5` | Minutes between news checks |
| `PORT` | ❌ | `8080` | Server port (Railway sets automatically) |
| `LOG_LEVEL` | ❌ | `INFO` | Logging level |

## 🔧 How It Works

```
┌─────────────────────────────────────────────────────┐
│                  Every 5 Minutes                     │
├─────────────────────────────────────────────────────┤
│                                                      │
│  1. FETCH    → RSS feeds, Hacker News, GitHub, etc. │
│              → ~100+ articles collected              │
│                        ↓                             │
│  2. DEDUP    → Check SQLite DB                      │
│              → Skip already-sent articles            │
│                        ↓                             │
│  3. AI FILTER → Send to Google Gemini               │
│              → Filter out crime, politics, gossip    │
│              → Keep only tech, scholarships, etc.    │
│              → Categorize & rate importance           │
│                        ↓                             │
│  4. GENERATE → Gemini writes engaging summary       │
│              → Adds emoji, hashtags, bullet points   │
│                        ↓                             │
│  5. MEDIA    → Extract video/photo from article     │
│              → Video priority > Photo                │
│              → Fallback to Pexels if nothing found   │
│                        ↓                             │
│  6. POST     → Send to Telegram channel             │
│              → Video + caption OR Photo + caption    │
│              → 3-sec delay between posts             │
│                        ↓                             │
│  7. RECORD   → Mark as sent in SQLite DB            │
│              → Auto-cleanup entries older than 30d   │
│                                                      │
└─────────────────────────────────────────────────────┘
```

## 📄 License

This project is open source. Feel free to use, modify, and distribute.

---

Made with ❤️ for the tech community
