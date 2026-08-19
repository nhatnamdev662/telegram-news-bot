# Telegram News Bot

Lightweight news bot that scrapes RSS feeds from 7 Vietnamese & international sources, summarizes articles, and sends them to Telegram with cover images. No AI required — pure Python.

## Features

- **2 sources**: VnExpress, VnExpress Thế giới
- **Cover images** with fallback to text when images fail
- **Smart summaries** — editorial sapo + opening paragraphs, no AI needed
- **Topic classification** — thể thao, kinh doanh, công nghệ, giải trí, thế giới, thời sự
- **Anti-duplicate** — same article from multiple sources detected via normalized title matching (24h window)
- **No missed articles** — backlog rolls over to next cycle instead of being dropped
- **Round-robin sources** — each cycle pulls from different sources evenly
- **Scheduled digests** — summary of articles sent in the last 24h at configurable hours
- **Admin whitelist** — restrict bot control to specific user IDs
- **Auto-restart** — boot script restarts the bot if it crashes
- **Resilient** — RSS timeout, failed sends retry next cycle, exponential backoff on errors
- **Breaking news alerts** — bypasses filters and sends immediately for urgent news (khẩn cấp, breaking, etc.)
- **Beautiful notifications** — emoji topic tags, source badges, clean separators (Option B design)
- **Parallel RSS fetch** — all 2 sources fetched concurrently via ThreadPoolExecutor
- **SQLite database** — persistent article history, per-user config, daily stats
- **Multi-user ready** — per-chat config stored in database
- **Webhook mode** — alternative to polling for production deployment
- **Atomic state writes** — crash-safe config/state persistence via tmp + rename

## Quick Start

### Prerequisites

Install [Termux](https://f-droid.org/en/packages/com.termux/) from F-Droid. For auto-start on boot, also install [Termux:Boot](https://f-droid.org/en/packages/com.termux.boot/).

### Install

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/nhatnamdev662/telegram-news-bot/main/install.sh)"
```

> Get a bot token from [@BotFather](https://t.me/BotFather): `/newbot`

### Configure & Run

```bash
nhatnam config   # enter bot token + chat ID (leave chat ID empty to auto-detect)
nhatnam 30       # run bot, check every 30 min
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `nhatnam config` | Set bot token + chat ID |
| `nhatnam <min>` | Run bot with interval (auto-starts on boot) |
| `nhatnam test` | Send one test cycle |
| `nhatnam log` | View last 50 log lines |
| `nhatnam setup` | Reinstall Python + dependencies |

## Telegram Commands

Send these directly to your bot:

| Command | Description |
|---------|-------------|
| `/test` | Run a scan + send cycle immediately |
| `/tinmoi [chủ đề]` | View latest news (optionally filter by topic) |
| `/tinday` | Daily summary with stats and topic breakdown |
| `/them kw1, kw2` | Filter by keywords |
| `/xoa kw` | Remove keyword |
| `/chude cat1, cat2` | Filter by topic (thể thao, kinh doanh, ...) |
| `/chude rong` | Accept all topics |
| `/nguon vnexpress` | Filter by source (`/nguon rong` = all) |
| `/gio 06:00-22:00` | Active hours (`/gio rong` = all day) |
| `/lich 07:00, 18:00` | Scheduled digest |
| `/lich rong` | Disable digest |
| `/tamngung` | Pause sending |
| `/tieptuc` | Resume sending |
| `/thongke` | Today's stats by source |
| `/xem` | View current config |
| `/trangthai` | Bot status |
| `/help` | List all commands |

## Configuration

**`.env`** (set via `nhatnam config`, never commit):

```
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<id>        # leave empty to auto-detect on /start
ADMIN_IDS=<id1>, <id2>       # restrict control (empty = anyone)
```

**`config.json`** (auto-generated, managed via Telegram commands):

```json
{
  "keywords": [],
  "categories": [],
  "sources": [],
  "schedule": [],
  "active_hours": [],
  "paused": false,
  "translate": true,
  "max_per_cycle": 10
}
```

## Project Structure

```
botnews/
├── news_bot.py        # core: RSS fetch → filter → summarize → send (1200+ lines)
├── nhatnam            # CLI wrapper
├── install.sh         # one-line installer
├── setup.sh           # manual setup
├── .env               # secrets (auto-generated)
├── config.json        # runtime config (auto-generated)
├── news_state.json    # dedup state + stats (auto-generated)
├── bot.db             # SQLite database (articles, per-user config, logs)
├── news_bot.log       # logs (auto-rotated at 1MB)
└── test_news_bot.py   # unit tests (84 tests, no network needed)
```

## Notification Design

Each news notification uses Option B format:

```
⚽ Đội tuyển Việt Nam thắng 3-0 trước Thái Lan

📰 VnExpress · 🕒 14:30
━━━━━━━━━━━━━━━━━━━━

Trận đấu lượt về vòng loại World Cup 2026 đã kết thúc
với chiến thắng thuyết phục của đội tuyển Việt Nam.

🔗 Đọc thêm →
```

Topic emojis: ⚽ thể thao · 📈 kinh doanh · 💻 công nghệ · 🎬 giải trí · 🌍 thế giới · 📰 thời sự

## Requirements

- Python 3.8+
- `feedparser`, `requests` (installed automatically)
- Telegram bot token from [@BotFather](https://t.me/BotFather)

## Running 24/7

- **Termux:Boot** — `nhatnam 30` installs a boot script that auto-restarts on crash
- **VPS** — run `nhatnam 30` in a `tmux`/`screen` session or use `systemd`
- **Webhook** — `python3 news_bot.py webhook 8443 https://your-domain.com/webhook`

## License

[MIT](LICENSE)
