#!/usr/bin/env python3
"""News bot: quét tin VN + toàn cầu từ nhiều nguồn, gửi về Telegram.
Điều khiển bằng lệnh Telegram (gửi trực tiếp cho bot):
  /help          - danh sách lệnh
  /them kw1, kw2 - thêm từ khóa quan tâm (chỉ gửi tin có từ khóa)
  /xoa  kw1      - bỏ từ khóa
  /chude cat1    - chỉ nhận chủ đề (thể thao, kinh doanh, công nghệ, giải trí, thế giới, thời sự)
  /chude rong    - nhận mọi chủ đề
  /lich 07:00, 18:00 - gửi bản tin tổng hợp theo giờ
  /lich rong     - tắt bản tin theo lịch
  /nguon vnexpress - chỉ nhận từ các nguồn chọn (rong = tất cả)
  /gio 06:00-22:00      - chỉ gửi tin trong khung giờ (rong = cả ngày)
  /tamngung / tieptuc    - tạm dừng / tiếp tục gửi tin
  /test                 - quét + gửi ngay 1 vòng
  /tinmoi [chủ đề]      - xem tin mới nhất theo chủ đề
  /tinday               - tổng hợp tin trong ngày
  /thongke              - thống kê bài đã gửi hôm nay
  /xem           - xem cấu hình hiện tại
  /trangthai     - xem trạng thái bot
Cấu hình: .env (token/chat_id) + config.json (tự sinh, do lệnh Telegram điều khiển).
Database: bot.db (SQLite — state, config, logs multi-user).
"""
import feedparser, requests, re, html, json, hashlib, os, sys, time, threading, unicodedata, logging, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from deep_translator import GoogleTranslator
    HAS_TRANSLATE = True
except ImportError:
    HAS_TRANSLATE = False
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote as url_quote

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'news_state.json')
CONFIG_FILE = os.path.join(BASE, 'config.json')
ENV_FILE = os.path.join(BASE, '.env')
LOG_FILE = os.path.join(BASE, 'news_bot.log')
PID_FILE = os.path.join(BASE, 'news_bot.pid')
DB_FILE = os.path.join(BASE, 'bot.db')
CFG_LOCK = threading.RLock()
CYCLE_LOCK = threading.Lock()
LOG_MAX_BYTES = 1024 * 1024
STATE_MAX_SEEN = 3000
VN_TZ = timezone(timedelta(hours=7))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
DIGEST_MAX = 10
TITLE_DUP_SECONDS = 24 * 3600
RETRY_MAX = 3
RETRY_BASE_DELAY = 2
BREAKING_KEYWORDS = [
    'khẩn cấp', 'phát khẩn', 'breaking', 'urgent', 'alert',
    'cảnh báo', 'thảm họa', 'động đất', 'tsunami', 'bão lớn',
    'nổ', 'tai nạn nghiêm trọng', 'thiên tai',
]
_trans_cache = {}
_bot_id_cache = None
logger = logging.getLogger('newsbot')


def safe_button_url(link, title):
    """Build inline keyboard with validated URLs. Returns None if link is invalid."""
    if not link or not re.match(r'https?://', link):
        return None
    share_url = "https://t.me/share/url?url=" + url_quote(link, safe='') + "&text=" + url_quote(title or '', safe='')
    # style: "primary" (xanh dương), "success" (xanh lá), "danger" (đỏ)
    # Yêu cầu Bot API 9.4+ (đầu 2026). Client Telegram cũ hơn sẽ tự bỏ qua field này
    # và hiển thị nút với màu mặc định — không gây lỗi.
    return {"inline_keyboard": [[
        {"text": "📰 Đọc thêm", "url": link, "style": "primary"},
        {"text": "🔗 Chia sẻ", "url": share_url, "style": "success"}
    ]]}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging():
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + '.1')
    except OSError:
        pass
    fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

def log(msg):
    logger.info(msg)

# ---------------------------------------------------------------------------
# SQLite database layer
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()

def _get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            config TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            title TEXT,
            title_vi TEXT,
            link TEXT,
            source TEXT,
            summary TEXT,
            summary_vi TEXT,
            pub TEXT,
            image TEXT,
            cat TEXT,
            sent_at REAL,
            UNIQUE(chat_id, link)
        );
        CREATE TABLE IF NOT EXISTS sent_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            title TEXT,
            link TEXT,
            source TEXT,
            summary TEXT,
            sent_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_articles_chat ON articles(chat_id);
        CREATE INDEX IF NOT EXISTS idx_sent_log_chat ON sent_log(chat_id);
    """)
    conn.close()

def db_get_user_config(chat_id):
    conn = _get_db()
    row = conn.execute("SELECT config FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['config'])
        except Exception:
            pass
    return None

def db_save_user_config(chat_id, cfg):
    conn = _get_db()
    conn.execute("INSERT OR REPLACE INTO users (chat_id, config) VALUES (?, ?)",
                 (chat_id, json.dumps(cfg, ensure_ascii=False)))
    conn.commit()
    conn.close()

def db_add_user(chat_id):
    """Đăng ký một chat_id nhận tin tự động (gọi khi user bấm /start)."""
    conn = _get_db()
    conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def db_list_users():
    """Trả về danh sách chat_id của tất cả người đã /start bot."""
    conn = _get_db()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r['chat_id'] for r in rows]

def db_remove_user(chat_id):
    """Bỏ đăng ký — dùng khi user chặn bot (gửi tin thất bại)."""
    conn = _get_db()
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def db_save_article(chat_id, art):
    conn = _get_db()
    try:
        conn.execute("""INSERT OR IGNORE INTO articles
            (chat_id, title, title_vi, link, source, summary, summary_vi, pub, image, cat, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, art.get('title'), art.get('title_vi'), art.get('link'),
             art.get('source'), art.get('summary'), art.get('summary_vi'),
             art.get('pub'), art.get('image'), art.get('cat'), art.get('sent_at', time.time())))
        conn.commit()
    except Exception:
        pass
    conn.close()

def db_save_sent_log(chat_id, art):
    conn = _get_db()
    try:
        conn.execute("INSERT INTO sent_log (chat_id, title, link, source, summary, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (chat_id, art.get('title'), art.get('link'), art.get('source'),
                      art.get('summary'), art.get('sent_at', time.time())))
        conn.commit()
    except Exception:
        pass
    conn.close()

def db_get_recent_articles(chat_id, hours=24, limit=20):
    conn = _get_db()
    cutoff = time.time() - hours * 3600
    rows = conn.execute(
        "SELECT * FROM articles WHERE chat_id=? AND sent_at>? ORDER BY sent_at DESC LIMIT ?",
        (chat_id, cutoff, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_today_stats(chat_id):
    conn = _get_db()
    today = datetime.now(VN_TZ).strftime('%Y-%m-%d')
    start = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0).timestamp()
    rows = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM articles WHERE chat_id=? AND sent_at>=? GROUP BY source",
        (chat_id, start)).fetchall()
    conn.close()
    return {r['source']: r['cnt'] for r in rows}

def db_get_day_articles(chat_id, limit=30):
    conn = _get_db()
    start = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0).timestamp()
    rows = conn.execute(
        "SELECT * FROM articles WHERE chat_id=? AND sent_at>=? ORDER BY sent_at DESC LIMIT ?",
        (chat_id, start, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def is_english(text):
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    ascii_letters = sum(1 for c in letters if ord(c) < 128)
    return ascii_letters / len(letters) > 0.7

def translate_vi(text):
    if not HAS_TRANSLATE or not text or len(text.strip()) < 3:
        return text
    if text in _trans_cache:
        return _trans_cache[text]
    try:
        result = GoogleTranslator(source='auto', target='vi').translate(text[:5000])
        if result and result.strip():
            _trans_cache[text] = result
            time.sleep(0.2)
            return result
    except Exception:
        pass
    return text

# ---------------------------------------------------------------------------
# Sources & categories
# ---------------------------------------------------------------------------
SOURCES = [
    {"name": "VnExpress", "cat": "vn", "url": "https://vnexpress.net/rss/thoi-su.rss"},
    {"name": "VnExpress Thế giới", "cat": "world", "url": "https://vnexpress.net/rss/the-gioi.rss"},
]

CATEGORY_KEYWORDS = {
    "thể thao": ["bóng đá", "bóng chuyền", "tennis", "cầu lông", "thể thao", "đội tuyển", "v-league",
                 "world cup", "golf", "đua xe", "cầu thủ", "huấn luyện viên", "quần vợt", "football",
                 "soccer", "olympic", "athlete", "champion", "basketball", "ngoại hạng", "vô địch",
                 "trận đấu", "bàn thắng", "chung kết", "đội bóng", "giải đấu", "bóng rổ"],
    "kinh doanh": ["chứng khoán", "cổ phiếu", "doanh nghiệp", "ngân hàng", "lãi suất", "tỷ giá",
                   "bất động sản", "thị trường", "xuất khẩu", "nhập khẩu", "gdp", "lạm phát",
                   "doanh thu", "lợi nhuận", "đầu tư", "kinh tế", "cổ đông", "trái phiếu", "vàng",
                   "business", "stock", "market", "economy", "inflation", "trade", "investment",
                   "bank", "company", "finance"],
    "công nghệ": ["trí tuệ nhân tạo", "smartphone", "iphone", "chip", "phần mềm", "ứng dụng",
                  "công nghệ", "robot", "điện thoại", "máy tính", "internet", "dữ liệu",
                  "mạng xã hội", "tiktok", "google", "apple", "microsoft", "technology", "tech",
                  "software", "artificial intelligence", "crypto", "bitcoin", "chatgpt"],
    "giải trí": ["phim", "ca sĩ", "diễn viên", "concert", "âm nhạc", "showbiz", "mv", "hòa nhạc",
                 "giải trí", "idol", "ngôi sao", "điện ảnh", "nghệ sĩ", "rapper", "actor", "movie",
                 "entertainment", "celebrity", "singer", "album", "song"],
    "thế giới": ["israel", "ukraine", "syria", "iran", "hàn quốc", "nhật bản", "liên hợp quốc",
                 "nato", "trump", "biden", "nga", "mỹ", "trung quốc", "biến đổi khí hậu",
                 "ukraine", "ceasefire", "war", "conflict", "election", "summit", "diplomacy"],
    "thời sự": ["quốc hội", "chính phủ", "thủ tướng", "bộ trưởng", "chính sách",
                "pháp luật", "công an", "tòa án", "thời sự", "văn bản", "nghị định", "sáp nhập",
                "tuyển dụng", "việc làm", "đại học", "thi cử", "kỳ thi", "giáo dục",
                "chính trị", "bầu cử", "nghị sĩ", "nghị quyết"],
}

CATEGORY_EMOJI = {
    "thể thao": "⚽",
    "kinh doanh": "📈",
    "công nghệ": "💻",
    "giải trí": "🎬",
    "thế giới": "🌍",
    "thời sự": "📰",
}

def get_emoji(art):
    cats = classify(art)
    if cats:
        return CATEGORY_EMOJI.get(cats[0], "📌")
    return "📌"

# ---------------------------------------------------------------------------
# State / Config (atomic writes)
# ---------------------------------------------------------------------------
def _atomic_write(filepath, data, as_json=True):
    tmp = filepath + '.tmp'
    try:
        if as_json:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
        else:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, filepath)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def get_token():
    env = load_env()
    return env.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN') or ''

def get_chat_id():
    env = load_env()
    return env.get('TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID') or ''

def get_admin_ids():
    env = load_env()
    raw = env.get('ADMIN_IDS') or os.environ.get('ADMIN_IDS') or ''
    ids = set()
    for x in raw.split(','):
        x = x.strip()
        if x.lstrip('-').isdigit():
            ids.add(int(x))
    return ids

def get_bot_id(token):
    """Lấy bot ID từ getMe (cache 1 lần). Dùng để filter không tự gửi cho chính mình."""
    global _bot_id_cache
    if _bot_id_cache:
        return _bot_id_cache
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if data.get('ok'):
            _bot_id_cache = data['result']['id']
            return _bot_id_cache
    except Exception:
        pass
    return None

def save_chat_id(cid):
    env = load_env()
    env['TELEGRAM_CHAT_ID'] = str(cid)
    lines = [f"# Cấu hình bot Telegram\n",
             f"TELEGRAM_BOT_TOKEN={env.get('TELEGRAM_BOT_TOKEN', '')}\n",
             f"TELEGRAM_CHAT_ID={cid}\n"]
    admin = env.get('ADMIN_IDS', '')
    if admin:
        lines.append(f"ADMIN_IDS={admin}\n")
    _atomic_write(ENV_FILE, ''.join(lines), as_json=False)

def load_config():
    default = {"keywords": [], "categories": [], "sources": [], "schedule": [],
               "active_hours": [], "paused": False, "translate": True,
               "max_per_cycle": 10, "last_update_id": 0}
    if os.path.exists(CONFIG_FILE):
        try:
            data = json.load(open(CONFIG_FILE))
            for k, v in default.items():
                data.setdefault(k, v)
            for k in ('keywords', 'categories', 'sources', 'schedule'):
                if isinstance(data.get(k), str):
                    data[k] = [x.strip() for x in data[k].split(',') if x.strip()]
            return data
        except Exception:
            pass
    return default

def save_config(cfg):
    _atomic_write(CONFIG_FILE, cfg)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"seen": {}, "seen_titles": {}, "last_digest": {}, "stats": {},
            "sent_log": [], "last_run": ""}

def save_state(st):
    _atomic_write(STATE_FILE, st)

def title_key(title):
    t = unicodedata.normalize('NFD', (title or '').lower())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', t).strip()

def is_title_dup(art, st):
    k = title_key(art.get('title', ''))
    if not k:
        return False
    titles = st.setdefault('seen_titles', {})
    if titles.get(k) and time.time() - titles[k] < TITLE_DUP_SECONDS:
        return True
    titles[k] = time.time()
    return False

def key_of(art):
    return hashlib.sha1((art['title'].lower() + '|' + art['link']).encode()).hexdigest()

def esc(s, quote=False):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return html.escape(s, quote=quote)

def parse_dt(s):
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None

def fmt_time(pub):
    dt = parse_dt(pub)
    if dt:
        return dt.astimezone(VN_TZ).strftime('%H:%M')
    m = re.search(r'(\d{2}:\d{2})', pub or '')
    return m.group(1) if m else ''

def fmt_date(pub):
    dt = parse_dt(pub)
    return dt.astimezone(VN_TZ).strftime('%d/%m/%Y') if dt else datetime.now(VN_TZ).strftime('%d/%m/%Y')

# ---------------------------------------------------------------------------
# Article processing
# ---------------------------------------------------------------------------
def clean_paras(text):
    """Trích xuất đoạn văn từ HTML, lọc bỏ junk/code/quảng cáo."""
    paras = []
    junk_patterns = [
        r'^(ảnh|Ảnh|nội dung|Nội dung|>>>|xem thêm|theo |nguồn:|đường dẫn)',
        r'^(BƯỚC \d|bước \d)',
        r'(Nhấp vào|Click vào|Bấm vào|Chọn vào)',
        r'(Thêm .*trên Google|theo dõi|subscribe|đăng ký)',
        r'(\d+\.\s*(Thêm|Bấm|Chọn|Nhấp))',
        r'(-->)',
        r'^(Để đọc|Để xem|Để nhận)',
        r'^(Tải ứng dụng|Download|Cài đặt)',
        r'^(Chia sẻ|Share|Gửi cho)',
        r'^(Bình luận|Comment|Ý kiến)',
        r'(function\s*\(|var\s+\w+\s*=|const\s+\w+\s*=)',
        r'(\{.*:.*\})',
        r'(window\.|document\.|element\.)',
        r'(https?://[^\s]+\.(js|css|png|jpg|gif))',
        r'(arfAsync|avivid|gtag|analytics)',
        r'(@font-face|font-family|font-weight)',
        # Navigation / menu junk
        r'(navigation menu|menu điều hướng|Show navigation)',
        r'(Sign up|Log in|Đăng nhập|Đăng ký|play Live)',
        r'(Show more|Hiển thị thêm|Xem thêm)',
        r'^(Live|BREAKING|LIVE|Phát trực tiếp)',
        r'(navigat|menu-|sidebar|footer|header)',
        r'^(Africa|Asia|Europe|Middle East|Châu|Asia Pacific)',
        r'^(Explained|Video|Podcasts|Du lịch|Khoa học)',
        r'(reCAPTCHA|EXPLORE MORE|blinking-dot)',
    ]
    for p in re.findall(r'<p[^>]*>(.*?)</p>', text, re.S):
        p = html.unescape(re.sub(r'<[^>]+>', '', p)).strip()
        p = re.sub(r'\s+', ' ', p).strip()
        if len(p) < 50:
            continue
        # Check junk patterns
        skip = False
        for pat in junk_patterns:
            if re.search(pat, p, re.IGNORECASE):
                skip = True
                break
        if skip:
            continue
        paras.append(p)
    return paras

_FALLBACK_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
]

def _fetch_with_retry(url, retries=2):
    """Fetch URL with UA rotation and retry on connection errors."""
    uas = [UA] + _FALLBACK_UAS
    for attempt in range(retries + 1):
        ua = uas[attempt % len(uas)]
        try:
            r = requests.get(url, headers={
                'User-Agent': ua,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9,vi;q=0.8',
            }, timeout=8)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 503):
                time.sleep(0.5 * (attempt + 1))
                continue
            return r
        except (requests.ConnectionError, requests.Timeout):
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
                continue
    return None

def fetch_article_paras(url):
    """Fetch bài viết từ URL, thử nhiều selectors khác nhau."""
    try:
        r = _fetch_with_retry(url)
        if not r or r.status_code != 200:
            return []
        text = r.text
        
        # Thử nhiều selectors theo thứ tự ưu tiên
        selectors = [
            r'<article[^>]*>(.*?)</article>',
            r'<div[^>]*class="[^"]*detail-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*content-detail[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*singular-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*body-content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*wysiwyg[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*ssrcss[^"]*"[^>]*data-testid="[^"]*body[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article__content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*post-content[^"]*"[^>]*>(.*?)</div>',
        ]
        
        for sel in selectors:
            m = re.search(sel, text, re.S)
            if m:
                paras = clean_paras(m.group(1))
                if paras and len(paras) >= 2:
                    return paras
        
        # Fallback 1: extract tất cả <p> từ trang, lọc junk
        paras = clean_paras(text)
        if paras and len(paras) >= 2:
            return paras
        
        # Fallback 2: extract <p> có class chứa "content" hoặc "body"
        p_class_paras = []
        for m in re.finditer(r'<p[^>]*class="[^"]*(?:content|body|text|article)[^"]*"[^>]*>(.*?)</p>', text, re.S):
            p = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
            p = re.sub(r'\s+', ' ', p).strip()
            if len(p) >= 50:
                p_class_paras.append(p)
        if p_class_paras:
            return p_class_paras
        
        return paras if paras else []
    except Exception:
        return []


_stop_str = ("của và là trong cho với của có được các bị từ "
    "the a an in of to for and or is are was were be been being have has had do does did "
    "will would shall should may might can could am that this these those it its he she they "
    "we you my your our their his her not no but if at by on up out so than too very just "
    "also more most some any all each every both few how what when where who whom which "
    "và là của có trong cho với từ bị được các năm người theo như nào nếu "
    "còn vẫn đang rất nên cần phải")
STOP_WORDS = set(_stop_str.lower().split())

def split_sentences(text):
    """Tách câu cho cả tiếng Việt và tiếng Anh."""
    if not text:
        return []
    # Tách theo . ! ? followed by whitespace or end
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = []
    for p in parts:
        p = p.strip()
        if len(p) > 10:
            sentences.append(p)
    return sentences

def _title_words(title):
    """Trích từ khóa từ title, bỏ stop words."""
    words = set(re.findall(r'[a-zA-Zà-ỹÀ-Ỹ]{3,}', (title or '').lower()))
    return words - STOP_WORDS

def score_sentence(sentence, title_words, position, total):
    """Tính điểm cho 1 câu: vị trí, overlap title, info density, quote, số liệu."""
    # Position score: câu đầu quan trọng nhất
    if total <= 1:
        pos_score = 1.0
    else:
        pos_score = 1.0 - (position / (total - 1)) * 0.5  # 1.0 -> 0.5

    # Title overlap
    sent_words = set(re.findall(r'[a-zA-Zà-ỹÀ-Ỹ]{3,}', sentence.lower()))
    if title_words and sent_words:
        overlap = len(sent_words & title_words) / max(len(title_words), 1)
    else:
        overlap = 0.0

    # Penalty nếu >50% từ trùng title (câu lặp title)
    if title_words and sent_words and overlap > 0.5:
        overlap *= 0.5  # Giảm điểm câu lặp title

    # Info density — tách biệt số liệu và tên riêng
    info = 0.0
    # Số liệu cụ thể: %, tỷ đồng, triệu, người, cases, percent
    if re.search(r'\d+[.,]?\d*\s*(%|tỷ|triệu|nghìn|người|cases|billion|million|tr)', sentence.lower()):
        info += 0.35
    elif re.search(r'\d', sentence):
        info += 0.2
    # Tên riêng / chức danh
    if re.search(r'[A-Z][a-z]+|Thủ tướng|Bộ trưởng|Chủ tịch|Tổng thống|President|Minister|CEO|Giám đốc', sentence):
        info += 0.25
    # Độ dài câu — sweet spot 30-150 chars
    sents_len = len(sentence)
    if 30 <= sents_len <= 150:
        info += 0.2
    elif sents_len > 150:
        info += 0.1

    # Quote bonus: câu có ngoặc kép hoặc từ trích dẫn
    quote_bonus = 0.0
    if re.search(r'["“”]', sentence):
        quote_bonus = 0.2
    elif re.search(r'cho biết|nói|tuyên bố|nhấn mạnh|bảo rằng|said|stated|announced', sentence.lower()):
        quote_bonus = 0.15

    return 0.30 * pos_score + 0.25 * overlap + 0.30 * min(info, 1.0) + 0.15 * quote_bonus

def extract_quote(text):
    """Trích 1 câu quote quan trọng từ bài viết. Trả về (quote, speaker) hoặc (None, None)."""
    if not text:
        return None, None
    # Pattern VN: attribution BEFORE quote (nói: "..." / cho biết: "...")
    patterns_vi = [
        r'(\w+ \w+ (?:cho biết|nói|tuyên bố|nhấn mạnh|bảo rằng|cũng cho biết|đồng thời cho biết))[:\s]*"([^"]{20,300})"',
        r'(theo (\w+ \w+))[,\s]*"([^"]{20,300})"',
        r'(\w+ \w+ cho hay)[:\s]*"([^"]{20,300})"',
    ]
    # Pattern VN: quote BEFORE attribution ("..." nói)
    patterns_vi_rev = [
        r'"([^"]{20,300})"[,\s]*(\w+ (?:cho biết|nói|tuyên bố|nhấn mạnh|bảo rằng|cho hay))',
    ]
    # Pattern EN: attribution BEFORE quote (said: "...")
    patterns_en_pre = [
        r'(\w+ \w+ (?:said|says|told|stated|announced|added|noted|explained))[:\s]*"([^"]{20,300})"',
    ]
    # Pattern EN: quote + attribution ("..." said)
    patterns_en = [
        r'"([^"]{20,300})"[,\s]*(\w+ (?:said|says|told|stated|announced|added|noted|explained))',
        r'(according to (\w+ \w+))[,\s]*"([^"]{20,300})"',
    ]
    
    # Try VN patterns (attribution before quote)
    for pat in patterns_vi:
        m = re.search(pat, text)
        if m:
            speaker = m.group(1).strip()
            quote = m.group(2).strip()
            # Check for title in surrounding context
            context = text[max(0, m.start()-80):m.end()+120]
            title_match = re.search(r'(Thủ tướng|Bộ trưởng|Chủ tịch|Tổng thống|Giám đốc|PGS\.|TS\.|GS\.)\s+(\w+ \w+)', context)
            if title_match:
                speaker = title_match.group(0)
            return quote, speaker
    
    # Try VN reversed (quote before attribution)
    for pat in patterns_vi_rev:
        m = re.search(pat, text)
        if m:
            quote = m.group(1).strip()
            speaker = m.group(2).strip()
            context = text[max(0, m.start()-120):m.end()+60]
            title_match = re.search(r'(Thủ tướng|Bộ trưởng|Chủ tịch|Tổng thống|Giám đốc|PGS\.|TS\.|GS\.)\s+(\w+ \w+)', context)
            if title_match:
                speaker = title_match.group(0)
            return quote, speaker
    
    # Try EN patterns (attribution before quote)
    for pat in patterns_en_pre:
        m = re.search(pat, text)
        if m:
            speaker = m.group(1).strip()
            quote = m.group(2).strip()
            context = text[max(0, m.start()-80):m.end()+120]
            title_match = re.search(r'(President|Minister|CEO|Director|Professor|Dr\.)\s+(\w+ \w+)', context)
            if title_match:
                speaker = title_match.group(0)
            return quote, speaker
    
    # Try EN patterns (quote before attribution)
    for pat in patterns_en:
        m = re.search(pat, text)
        if m:
            quote = m.group(1).strip() if 'according' not in pat else m.group(3).strip()
            speaker = m.group(2).strip() if 'according' not in pat else m.group(1).strip()
            context = text[max(0, m.start()-120):m.end()+60]
            title_match = re.search(r'(President|Minister|CEO|Director|Professor|Dr\.)\s+(\w+ \w+)', context)
            if title_match:
                speaker = title_match.group(0)
            return quote, speaker
    
    return None, None


def _trim_summary(text, target_min=350, target_max=600):
    """Cắt tóm tắt tại sentence boundaries trong khoảng target 400-600 chars."""
    if not text:
        return text
    if len(text) <= target_max:
        return text
    sentences = split_sentences(text)
    if not sentences:
        # Fallback: cut at word boundary
        cut = text[:target_max]
        last_period = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '), target_min)
        return cut[:last_period + 1] if last_period > 50 else cut
    
    result = []
    current_len = 0
    for s in sentences:
        if current_len + len(s) + 1 > target_max:
            break
        result.append(s)
        current_len += len(s) + 1
    joined = ' '.join(result)
    
    # Mở rộng nếu quá ngắn (< target_min)
    if len(joined) < target_min and sentences:
        for idx in range(len(result), len(sentences)):
            candidate = joined + ' ' + sentences[idx]
            if len(candidate) <= target_max:
                joined = candidate
            else:
                break
    
    return joined

def _dedup_sentences(selected):
    """Loại câu có >60% từ trùng lặp với câu đã chọn."""
    if not selected:
        return selected
    result = []
    seen_words = set()
    for sc, idx, sent in selected:
        words = set(sent.lower().split())
        if not words:
            result.append((sc, idx, sent))
            continue
        overlap = len(words & seen_words) / max(len(words), 1)
        if overlap > 0.6:
            continue  # Bỏ câu trùng lặp
        result.append((sc, idx, sent))
        seen_words |= words
    return result

def summarize_article(title, paragraphs, sapo):
    """Extractive summarization: scoring câu, ghép top 7-8 câu, dedup."""
    # Ghép tất cả text
    all_text = []
    if sapo:
        all_text.append(sapo)
    all_text.extend(paragraphs)
    full_text = ' '.join(all_text)
    
    sentences = split_sentences(full_text)
    if not sentences:
        return _trim_summary(sapo) if sapo else ''
    
    title_words = _title_words(title)
    scored = []
    for i, s in enumerate(sentences):
        sc = score_sentence(s, title_words, i, len(sentences))
        scored.append((sc, i, s))
    
    # Chọn top 10 câu theo score, sau đó dedup
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = _dedup_sentences(scored[:10])
    
    # Lấy tối đa 8 câu, sắp xếp lại theo vị trí gốc
    top = sorted(selected[:8], key=lambda x: x[1])
    
    summary = ' '.join(s for _, _, s in top)
    return _trim_summary(summary)

def make_summary(title, link, desc):
    """Tạo tóm tắt: LUÔN fetch bài viết để có nội dung đầy đủ."""
    sapo = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', desc or ''))).strip()
    
    # LUÔN fetch trang bài viết
    paras = fetch_article_paras(link)
    
    # Nếu fetch thành công → dùng extractive scoring
    if paras:
        summary = summarize_article(title, paras, sapo)
        if len(summary) >= 60:
            return summary
    
    # Fallback: dùng sapo RSS nếu fetch fail hoặc summary quá ngắn
    if sapo and len(sapo) >= 40:
        return _trim_summary(sapo)
    
    return sapo or title

def make_summary_full(title, link, desc):
    """Tạo tóm tắt đầy đủ + trích quote. Trả về (summary, quote, speaker)."""
    sapo = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', desc or ''))).strip()
    
    quote, speaker = None, None
    
    # LUÔN fetch bài viết để có nội dung đầy đủ
    paras = fetch_article_paras(link)
    
    if paras:
        full_text = sapo + ' ' + ' '.join(paras)
        summary = summarize_article(title, paras, sapo)
    else:
        full_text = sapo
        summary = _trim_summary(sapo) if sapo else title
    
    # Extract quote từ full text
    if full_text and len(full_text) > 100:
        quote, speaker = extract_quote(full_text)
        # Tích hợp quote vào summary nếu có
        if quote and len(summary) > 60:
            # Chèn quote vào giữa summary
            sents = split_sentences(summary)
            if len(sents) >= 2:
                mid = len(sents) // 2
                quote_text = f'"{quote}"'
                if speaker:
                    quote_text += f' — {speaker}'
                sents.insert(mid, quote_text)
                summary = ' '.join(sents)
                summary = _trim_summary(summary)
            elif len(sents) == 1:
                summary = sents[0] + f' "{quote}"'
                if speaker:
                    summary += f' — {speaker}'
                summary = _trim_summary(summary)
    
    if len(summary) < 60:
        summary = _trim_summary(sapo) or summary or title
    
    return summary, quote, speaker

def classify(art):
    text = (art['title'] + ' ' + art.get('desc', '') + ' ' + art.get('summary', '')).lower()
    cats = []
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if len(kw) <= 3:
                if re.search(r'\b' + re.escape(kw) + r'\b', text):
                    cats.append(cat)
                    break
            else:
                if kw in text:
                    cats.append(cat)
                    break
    return cats

def is_breaking_news(art):
    text = (art['title'] + ' ' + art.get('desc', '') + ' ' + art.get('summary', '')).lower()
    return any(kw in text for kw in BREAKING_KEYWORDS)

def matches_filters(art, cfg):
    kw_list = [k.strip().lower() for k in cfg.get('keywords', []) if k.strip()]
    if kw_list:
        text = (art['title'] + ' ' + art.get('desc', '') + ' ' + art.get('summary', '')).lower()
        if not any(k in text for k in kw_list):
            return False
    cats = cfg.get('categories', [])
    if cats:
        art_cats = classify(art)
        if not set(art_cats) & set(cats):
            return False
    return True

def interleave(arts):
    by_src = {}
    for a in arts:
        by_src.setdefault(a['source'], []).append(a)
    names = list(by_src.keys())
    idx = {n: 0 for n in names}
    out = []
    while True:
        done = True
        for n in names:
            if idx[n] < len(by_src[n]):
                out.append(by_src[n][idx[n]])
                idx[n] += 1
                done = False
        if done:
            break
    return out

# ---------------------------------------------------------------------------
# Telegram message builders (Option B design)
# ---------------------------------------------------------------------------
def build_msg(art):
    emoji = get_emoji(art)
    title = esc(art.get("title_vi", art["title"]))
    source = esc(art["source"])
    time_str = fmt_time(art.get("pub", ""))
    summary = esc(art.get("summary_vi", art.get("summary", "")))
    link = esc(art["link"], quote=True)
    lines = []
    # Header: emoji + title
    lines.append(f"{emoji} <b>{title}</b>")
    lines.append("")
    # Source + time line
    lines.append(f"📰 <i>{source}</i>  ·  🕒 <i>{time_str}</i>")
    lines.append("─────────────────────")
    # Summary in blockquote
    if summary:
        lines.append("")
        lines.append(f"<blockquote>{summary}</blockquote>")
    # Quote block
    if art.get("quote"):
        q = esc(art["quote"])
        sp = esc(art.get("quote_speaker", ""))
        lines.append("")
        if sp:
            lines.append(f'<blockquote expandable><i>"{q}"</i>\n<i>— {sp}</i></blockquote>')
        else:
            lines.append(f'<blockquote expandable><i>"{q}"</i></blockquote>')
    # Footer link
    lines.append("")
    lines.append(f'🔗 <a href="{link}"><b>Đọc thêm tại {source}</b></a>')
    return "\n".join(lines)

def get_image(e):
    try:
        if e.get('media_content'):
            return e['media_content'][0].get('url', '')
        if e.get('media_thumbnail'):
            return e['media_thumbnail'][0].get('url', '')
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', e.get('summary', '') or '')
        if m:
            return html.unescape(m.group(1))
    except Exception:
        pass
    return ''

def fetch_og_image(url):
    """Fetch Open Graph image from article page as fallback."""
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=5)
        if r.status_code != 200:
            return ''
        # Try og:image
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if m:
            img = html.unescape(m.group(1))
            if img.startswith('https://'):
                return img
        # Try twitter:image
        m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if m:
            img = html.unescape(m.group(1))
            if img.startswith('https://'):
                return img
    except Exception:
        pass
    return ''

def build_photo_caption(art):
    emoji = get_emoji(art)
    title = esc(art.get("title_vi", art["title"]))
    source = esc(art["source"])
    time_str = fmt_time(art.get("pub", ""))
    summary = esc(art.get("summary_vi", art.get("summary", "")))
    link = esc(art["link"], quote=True)
    header = f"{emoji} <b>{title}</b>\n\n📰 <i>{source}</i>  ·  🕒 <i>{time_str}</i>\n─────────────────────"
    quote_block = ""
    if art.get("quote"):
        q = esc(art["quote"])
        sp = esc(art.get("quote_speaker", ""))
        if sp:
            quote_block = f'\n\n<i>💬 "{q}"</i>\n<i>— {sp}</i>'
        else:
            quote_block = f'\n\n<i>💬 "{q}"</i>'
    footer = f'\n\n🔗 <a href="{link}"><b>Đọc thêm tại {source}</b></a>'
    budget = 1024 - len(header.encode('utf-8')) - len(quote_block.encode('utf-8')) - len(footer.encode('utf-8'))
    if summary and len(summary.encode('utf-8')) > budget:
        cut = summary[:max(budget // 2, 80)]
        cut = cut[:max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), 60) + 1]
        summary = cut if len(cut) > 60 else summary[:budget // 2]
    lines = [header]
    if summary:
        lines.append("")
        lines.append(f"<blockquote>{summary}</blockquote>")
    if quote_block:
        lines.append(quote_block)
    lines.append(footer)
    return "".join(lines)

def send_telegram_photo(image, caption, token, chat_id, reply_markup=None):
    if not image.startswith('https://'):
        return False, 'ảnh không phải https'
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    for attempt in range(RETRY_MAX):
        try:
            payload = {"chat_id": chat_id, "photo": image,
                       "caption": caption, "parse_mode": "HTML"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            r = requests.post(url, json=payload, timeout=15)
            data = r.json()
            ok = data.get('ok') is True
            if ok:
                return True, json.dumps(data, ensure_ascii=False)[:200]
            err = data.get('description', '')
            if 'BUTTON_URL_INVALID' in err or 'PHOTO_INVALID' in err or 'Bad Request' in err:
                return False, err[:200]
            if attempt < RETRY_MAX - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Exception as ex:
            if attempt < RETRY_MAX - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            else:
                return False, str(ex)
    return False, 'max retries exceeded'

def build_digest(arts, now):
    lines = [f"📋 <b>BẢN TIN TỔNG HỢP</b> — {now.strftime('%d/%m/%Y')}",
             "═══════════════════════", ""]
    for i, a in enumerate(arts[:DIGEST_MAX], 1):
        emoji = get_emoji(a)
        title = esc(a.get('title_vi', a['title']))
        source = esc(a['source'])
        time_str = fmt_time(a.get('pub', ''))
        link = esc(a["link"], quote=True)
        lines.append(f'{i}. {emoji} <a href="{link}"><b>{title}</b></a>')
        lines.append(f"   📰 <i>{source}</i>  ·  🕒 <i>{time_str}</i>")
        lines.append("")
    lines.append("═══════════════════════")
    return "\n".join(lines)

def build_daily_summary(chat_id, now):
    articles = db_get_day_articles(chat_id, limit=30)
    if not articles:
        return "📋 <b>TIN TỨC HÔM NAY</b>\n═══════════════════════\n\nChưa có bài nào được gửi hôm nay."
    stats = {}
    for a in articles:
        src = a.get('source', 'Unknown')
        stats[src] = stats.get(src, 0) + 1
    lines = [f"📋 <b>TIN TỨC HÔM NAY</b> — {now.strftime('%d/%m/%Y')}",
             "═══════════════════════", ""]
    cat_count = {}
    for a in articles[:10]:
        emoji = "📌"
        cats = classify({"title": a.get('title', ''), "desc": "", "summary": a.get('summary', '')})
        if cats:
            emoji = CATEGORY_EMOJI.get(cats[0], "📌")
            cat_count[cats[0]] = cat_count.get(cats[0], 0) + 1
        title = esc(a.get('title_vi', a.get('title', '')))
        source = esc(a.get('source', ''))
        time_str = fmt_time(a.get('pub', ''))
        link = esc(a.get("link", ""), quote=True)
        lines.append(f'{emoji} <a href="{link}"><b>{title}</b></a>')
        lines.append(f"   📰 <i>{source}</i>  ·  🕒 <i>{time_str}</i>")
        lines.append("")
    lines.append("═══════════════════════")
    lines.append(f"📊 <b>Tổng: {len(articles)} bài</b>")
    for src, cnt in sorted(stats.items(), key=lambda x: -x[1]):
        lines.append(f"  • {src}: {cnt}")
    if cat_count:
        lines.append("")
        lines.append("🏷️ <b>Chủ đề:</b>")
        for cat, cnt in sorted(cat_count.items(), key=lambda x: -x[1]):
            emoji = CATEGORY_EMOJI.get(cat, "📌")
            lines.append(f"  {emoji} {cat}: {cnt}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Telegram API — with retry & exponential backoff
# ---------------------------------------------------------------------------
def send_telegram(text, token, chat_id, html=True, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for attempt in range(RETRY_MAX):
        try:
            r = requests.post(url, json=payload, timeout=15)
            data = r.json()
            ok = data.get('ok') is True
            if ok:
                return True, json.dumps(data, ensure_ascii=False)[:300]
            err = data.get('description', '')
            if 'BUTTON_URL_INVALID' in err:
                return False, err[:300]
            if 'Too Many Requests' in err or '502' in err or '503' in err:
                if attempt < RETRY_MAX - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
            return False, err[:300]
        except Exception as ex:
            if attempt < RETRY_MAX - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            else:
                return False, str(ex)
    return False, 'max retries exceeded'

def fetch_source(src, cfg=None):
    """Fetch a single RSS source. Used by parallel fetch."""
    arts = []
    try:
        r = requests.get(src['url'], headers={'User-Agent': UA}, timeout=20)
        d = feedparser.parse(r.content)
        if not d.entries:
            log(f"  ⚠ {src['name']}: 0 entries")
            return arts
        for e in d.entries[:30]:
            title = html.unescape((e.get('title') or '').strip())
            link = (e.get('link') or '').strip()
            if not title or not link:
                continue
            arts.append({
                "source": src['name'], "cat": src['cat'], "title": title,
                "link": link, "pub": e.get('published') or e.get('updated') or '',
                "desc": e.get('summary') or '', "summary": "", "image": get_image(e),
            })
        log(f"  ✓ {src['name']}: {len(d.entries)} entries")
    except Exception as ex:
        log(f"  ✗ {src['name']}: {ex}")
    return arts

def broadcast_article(a, token, targets, inline_kb):
    """Gửi 1 bài viết tới danh sách chat_id (mọi người đã /start).
    Trả về (any_ok, last_error) — any_ok=True nếu gửi thành công tới ít nhất 1 người.
    Tự động bỏ đăng ký user nếu họ đã chặn bot / xoá chat."""
    any_ok = False
    last_out = ''
    # Filter bot ID
    bot_id = get_bot_id(token)
    if bot_id:
        targets = [t for t in targets if t != bot_id]
    for cid in targets:
        ok, out = False, ''
        if a.get('image'):
            ok, out = send_telegram_photo(a['image'], build_photo_caption(a), token, cid, reply_markup=inline_kb)
            if not ok:
                ok, out = send_telegram(build_msg(a), token, cid)
        else:
            ok, out = send_telegram(build_msg(a), token, cid, reply_markup=inline_kb)
        if ok:
            any_ok = True
        else:
            last_out = out
            low = (out or '').lower()
            if 'bot was blocked' in low or 'chat not found' in low or 'user is deactivated' in low or 'kicked' in low:
                log(f"     ⚠ Bỏ đăng ký chat_id {cid} (không gửi được: {out[:80]})")
                db_remove_user(cid)
    return any_ok, last_out

def fetch_all(cfg=None):
    sel = [s['name'] for s in SOURCES]
    if cfg and cfg.get('sources'):
        sel = [n for n in sel if n in cfg['sources']]
    sources = [s for s in SOURCES if s['name'] in sel]
    arts = []
    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(fetch_source, src, cfg): src for src in sources}
        for future in as_completed(futures):
            try:
                arts.extend(future.result())
            except Exception as ex:
                log(f"  ✗ Fetch error: {ex}")
    return arts

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def process_commands(token, chat_id, cfg):
    with CFG_LOCK:
        last = int(cfg.get('last_update_id', 0))
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                             params={"offset": last + 1, "timeout": 0,
                                     "allowed_updates": ["message"]}, timeout=5)
            data = r.json()
        except Exception as ex:
            log(f"Lỗi getUpdates: {ex}")
            return False
        if not data.get('ok'):
            log("getUpdates thất bại: " + str(data.get('description', '')))
            return False
        admin_ids = get_admin_ids()
        max_id = last
        for u in data.get('result', []):
            max_id = max(max_id, int(u.get('update_id', 0)))
            msg = u.get('message') or {}
            text = (msg.get('text') or '').strip()
            if not text.startswith('/'):
                continue
            from_id = (msg.get('from') or {}).get('id')
            cid = (msg.get('chat') or {}).get('id') or chat_id
            cmd = text.split(None, 1)[0].lower().split('@')[0]
            # /start luôn cho phép — tự động đăng ký nhận tin, kể cả không phải admin
            if cmd == '/start':
                db_add_user(cid)
                reply = handle_command(text, cfg, cid)
                if isinstance(reply, tuple) and len(reply) == 2:
                    msg, bg_fn = reply
                    send_telegram(msg, token, cid, html=False)
                    threading.Thread(target=bg_fn, daemon=True).start()
                elif reply:
                    send_telegram(reply, token, cid, html=False)
                continue
            # Các lệnh khác chỉ admin mới được dùng
            if admin_ids and from_id not in admin_ids:
                send_telegram("⛔ Chỉ admin mới được dùng lệnh này.", token, cid, html=False)
                continue
            reply = handle_command(text, cfg, cid)
            if isinstance(reply, tuple) and len(reply) == 2:
                msg, bg_fn = reply
                send_telegram(msg, token, cid, html=False)
                threading.Thread(target=bg_fn, daemon=True).start()
            elif reply:
                send_telegram(reply, token, cid, html=False)
        if max_id > last:
            cfg['last_update_id'] = max_id
            save_config(cfg)
        return True

def handle_command(text, cfg, chat_id=None):
    parts = text.split(None, 1)
    cmd = parts[0].lower().split('@')[0]
    arg = (parts[1] if len(parts) > 1 else '').strip()
    if cmd == '/start':
        return ("👋 Xin chào! Mình là bot tin tức tự động 📰\n"
                "Gõ /help để xem danh sách lệnh.\n"
                "Thử: /test (gửi tin ngay), /tinmoi (tin mới nhất), /tinday (tổng hợp hôm nay).")
    if cmd == '/help':
        return ("Các lệnh bot:\n"
                "/them bóng đá, bitcoin — thêm từ khóa quan tâm\n"
                "/xoa bóng đá — bỏ từ khóa\n"
                "/chude thể thao, kinh doanh — chỉ nhận chủ đề\n"
                "/chude rong — nhận mọi chủ đề\n"
                "/lich 07:00, 18:00 — bản tin tổng hợp theo giờ\n"
                "/lich rong — tắt bản tin theo lịch\n"
                "/nguon vnexpress — chỉ nhận từ nguồn chọn (rong = tất cả)\n"
                "/gio 06:00-22:00 — chỉ gửi tin trong khung giờ (rong = cả ngày)\n"
                "/tamngung /tieptuc — tạm dừng / tiếp tục gửi tin\n"
                "/test — quét + gửi ngay 1 vòng\n"
                "/tinmoi [chủ đề] — xem tin mới nhất\n"
                "/tinday — tổng hợp tin trong ngày\n"
                "/thongke — thống kê bài đã gửi\n"
                "/xem — xem cấu hình\n"
                "/trangthai — trạng thái bot")
    if cmd == '/them':
        kws = [k.strip().lower() for k in arg.split(',') if k.strip()]
        if not kws:
            return "Cách dùng: /them từ_khóa1, từ_khóa2"
        cur = cfg.setdefault('keywords', [])
        added = []
        for k in kws:
            if k not in cur:
                cur.append(k)
                added.append(k)
        save_config(cfg)
        return f"✅ Đã thêm: {', '.join(added) if added else '(đều đã có)'}\nTừ khóa hiện tại: {', '.join(cur) if cur else '(trống)'}"
    if cmd == '/xoa':
        kws = [k.strip().lower() for k in arg.split(',') if k.strip()]
        cur = cfg.get('keywords', [])
        removed = [k for k in kws if k in cur]
        cfg['keywords'] = [k for k in cur if k not in kws]
        save_config(cfg)
        return f"Đã xóa: {', '.join(removed) if removed else '(không có từ khóa nào khớp)'}\nTừ khóa còn lại: {', '.join(cfg['keywords']) if cfg['keywords'] else '(trống)'}"
    if cmd == '/chude':
        if not arg or arg.lower() in ('rong', 'tat', 'all', 'none', '0'):
            cfg['categories'] = []
            save_config(cfg)
            return "✅ Đã tắt lọc chủ đề — nhận mọi chủ đề."
        cats = [c.strip().lower() for c in arg.split(',') if c.strip()]
        valid = list(CATEGORY_KEYWORDS.keys())
        bad = [c for c in cats if c not in valid]
        if bad:
            return f"Chủ đề không hợp lệ: {', '.join(bad)}\nChủ đề có sẵn: {', '.join(valid)}"
        cfg['categories'] = cats
        save_config(cfg)
        return f"✅ Chỉ nhận chủ đề: {', '.join(cats)}"
    if cmd == '/lich':
        if not arg or arg.lower() in ('rong', 'tat', 'off', 'none'):
            cfg['schedule'] = []
            save_config(cfg)
            return "✅ Đã tắt bản tin theo lịch."
        times = [t.strip() for t in arg.split(',') if t.strip()]
        ok_times, bad_times = [], []
        for t in times:
            if re.match(r'^([01]?\d|2[0-3]):[0-5]\d$', t):
                ok_times.append(t)
            else:
                bad_times.append(t)
        if bad_times:
            return f"Giờ không hợp lệ: {', '.join(bad_times)} (dùng HH:MM, VD 07:00)"
        cfg['schedule'] = ok_times
        save_config(cfg)
        return f"✅ Bản tin tổng hợp lúc: {', '.join(ok_times) if ok_times else '(trống)'}"
    if cmd == '/test':
        log("🔄 /test — chạy 1 vòng quét + gửi ngay (background)")
        return ("__BG__🔄 Đang quét tin và gửi ngay... Tin sẽ đến trong vài giây!",
                lambda: run_once())
    if cmd == '/tamngung':
        cfg['paused'] = True
        save_config(cfg)
        return "⏸ Đã tạm dừng gửi tin. Gõ /tieptuc để tiếp tục."
    if cmd == '/tieptuc':
        cfg['paused'] = False
        save_config(cfg)
        return "▶️ Đã tiếp tục gửi tin."
    if cmd == '/nguon':
        if not arg or arg.lower() in ('rong', 'tat', 'all', 'none'):
            cfg['sources'] = []
            save_config(cfg)
            return "✅ Đã bật tất cả nguồn tin."
        names = [n.strip() for n in arg.split(',') if n.strip()]
        ok_names, bad = [], []
        for n in names:
            nl = n.lower()
            hits = [s['name'] for s in SOURCES if nl.replace(' ', '') in s['name'].lower().replace(' ', '')]
            if hits:
                for h in hits:
                    if h not in ok_names:
                        ok_names.append(h)
            else:
                bad.append(n)
        if bad:
            return f"Nguồn không có: {', '.join(bad)}\nNguồn có sẵn: {', '.join(s['name'] for s in SOURCES)}"
        cfg['sources'] = ok_names
        save_config(cfg)
        return f"✅ Chỉ nhận từ: {', '.join(ok_names)}"
    if cmd == '/gio':
        if not arg or arg.lower() in ('rong', 'tat', 'off', 'none'):
            cfg['active_hours'] = []
            save_config(cfg)
            return "✅ Đã bỏ giới hạn giờ — gửi tin cả ngày."
        ranges = [r.strip() for r in arg.split(',') if r.strip()]
        ok_r, bad_r = [], []
        for r in ranges:
            if re.match(r'^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$', r):
                ok_r.append(r)
            else:
                bad_r.append(r)
        if bad_r:
            return f"Khung giờ không hợp lệ: {', '.join(bad_r)} (VD: 06:00-22:00)"
        cfg['active_hours'] = ok_r
        save_config(cfg)
        return f"✅ Chỉ gửi tin trong: {', '.join(ok_r)}"
    if cmd == '/thongke':
        st = load_state()
        stats = st.get('stats', {})
        today = datetime.now(VN_TZ).strftime('%Y-%m-%d')
        d = stats.get(today, {})
        if not d:
            return "📊 Hôm nay chưa có bài nào được gửi."
        lines = [f"📊 Đã gửi hôm nay ({today}):"]
        total = 0
        for s in SOURCES:
            n = d.get(s['name'], 0)
            if n:
                lines.append(f"  • {s['name']}: {n} bài")
            total += n
        lines.append(f"Tổng: {total} bài")
        return "\n".join(lines)
    if cmd == '/xem':
        kw = ', '.join(cfg.get('keywords', [])) or '(trống)'
        cat = ', '.join(cfg.get('categories', [])) or '(tất cả)'
        sch = ', '.join(cfg.get('schedule', [])) or '(tắt)'
        srcs = ', '.join(cfg.get('sources', [])) or '(tất cả)'
        hours = ', '.join(cfg.get('active_hours', [])) or '(cả ngày)'
        pause = '⏸ Đang tạm dừng' if cfg.get('paused') else 'Đang chạy'
        return (f"⚙️ Cấu hình hiện tại:\nTừ khóa: {kw}\nChủ đề: {cat}\n"
                f"Nguồn: {srcs}\nKhung giờ: {hours}\nBản tin theo lịch: {sch}\n"
                f"Trạng thái: {pause}\nTối đa bài/vòng: {cfg.get('max_per_cycle', 10)}")
    if cmd == '/trangthai':
        st = load_state()
        return (f"📊 Trạng thái:\nĐã đọc/gửi: {len(st.get('seen', {}))} bài\n"
                f"Lần chạy gần nhất: {st.get('last_run', 'chưa có')}\n"
                f"Nguồn: {len(SOURCES)}")
    if cmd == '/tinmoi':
        def _bg_tinmoi():
            result = handle_tinmoi(arg, cfg, chat_id)
            if result:
                send_telegram(result, get_token(), chat_id, html=False)
        threading.Thread(target=_bg_tinmoi, daemon=True).start()
        return "📰 Đang tải tin mới... Tin sẽ hiện trong vài giây!"
    if cmd == '/tinday':
        def _bg_tinday():
            result = handle_tinday(chat_id)
            if result:
                send_telegram(result, get_token(), chat_id, html=False)
        threading.Thread(target=_bg_tinday, daemon=True).start()
        return "📋 Đang tổng hợp tin hôm nay... Sẽ hiện trong vài giây!"
    return "Không hiểu lệnh. Gõ /help để xem danh sách."

def handle_tinmoi(arg, cfg, chat_id=None):
    arts = fetch_all(cfg)
    # Use RSS sapo directly — skip slow article fetch for quick preview
    for a in arts:
        if not a.get('summary'):
            sapo = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', a.get('desc', '')))).strip()
            a['summary'] = _trim_summary(sapo) if sapo else a['title']
    arts = interleave(arts)
    cat_filter = None
    if arg:
        cat_arg = arg.strip().lower()
        valid = list(CATEGORY_KEYWORDS.keys())
        for v in valid:
            if cat_arg in v or v in cat_arg:
                cat_filter = v
                break
    if cat_filter:
        arts = [a for a in arts if cat_filter in classify(a)]
    arts = arts[:10]
    if not arts:
        msg = "📭 Không tìm thấy tin mới"
        if cat_filter:
            msg += f" về chủ đề \"{cat_filter}\""
        return msg
    lines = [f"📰 <b>TIN MỚI NHẤT</b>" + (f" — {cat_filter}" if cat_filter else ""), "━━━━━━━━━━━━━━━━━━━━", ""]
    for i, a in enumerate(arts, 1):
        emoji = get_emoji(a)
        title = esc(a.get('title_vi', a['title']))
        source = esc(a['source'])
        time_str = fmt_time(a.get('pub', ''))
        link = esc(a['link'], quote=True)
        lines.append(f"{i}. {emoji} <b>{title}</b>")
        lines.append(f"   📰 {source} · 🕒 {time_str}")
        lines.append(f'   🔗 <a href="{link}">Đọc thêm</a>')
        lines.append("")
    return "\n".join(lines)

def handle_tinday(chat_id=None):
    now = datetime.now(VN_TZ)
    return build_daily_summary(chat_id, now)

def is_active_time(cfg, hm):
    ranges = cfg.get('active_hours', [])
    if not ranges:
        return True
    for r in ranges:
        if '-' in r:
            a, b = r.split('-', 1)
            if re.match(r'^([01]\d|2[0-3]):[0-5]\d$', a) and re.match(r'^([01]\d|2[0-3]):[0-5]\d$', b) and a <= hm <= b:
                return True
    return False

# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_once(max_send=None):
    if not CYCLE_LOCK.acquire(blocking=False):
        log("⏳ Đang có một vòng quét chạy rồi — bỏ qua.")
        return
    try:
        _run_once(max_send)
    finally:
        CYCLE_LOCK.release()

def _run_once(max_send=None):
    token = get_token()
    if not token:
        log("❌ Chưa có TELEGRAM_BOT_TOKEN. Điền vào file .env rồi chạy lại.")
        return
    chat_id = get_chat_id()
    if not chat_id:
        log("⏳ Chưa có chat_id, tự tìm qua getUpdates... (bạn cần nhắn /start cho bot trước)")
        chat_id = discover_chat_id(token)
        if not chat_id:
            log("❌ Không tìm thấy chat nào. Hãy mở bot trên Telegram và nhắn /start, rồi chạy lại.")
            return
        save_chat_id(chat_id)
        log(f"✅ Đã tìm thấy và lưu chat_id = {chat_id}")
    db_add_user(chat_id)  # đảm bảo chat_id mặc định trong .env luôn nằm trong danh sách nhận tin
    cfg = load_config()
    process_commands(token, chat_id, cfg)
    cfg = load_config()
    for k in ('keywords', 'categories', 'schedule', 'sources'):
        v = cfg.get(k)
        if isinstance(v, str):
            cfg[k] = [x.strip() for x in v.split(',') if x.strip()]
    if cfg.get('paused'):
        log("⏸ Bot đang tạm dừng — bỏ qua vòng gửi tin.")
        return
    now = datetime.now(VN_TZ)
    hm = now.strftime('%H:%M')
    if not is_active_time(cfg, hm):
        log(f"⏰ Ngoài khung giờ hoạt động ({hm}) — bỏ qua vòng gửi tin.")
        return
    st = load_state()
    st['last_run'] = now.strftime('%Y-%m-%d %H:%M:%S')
    st.setdefault('seen_titles', {})
    now_ts = time.time()
    st['seen_titles'] = {k: ts for k, ts in st['seen_titles'].items() if now_ts - ts < 2 * TITLE_DUP_SECONDS}
    if len(st['seen']) > STATE_MAX_SEEN:
        drop = len(st['seen']) - STATE_MAX_SEEN
        for k in list(st['seen'])[:drop]:
            del st['seen'][k]
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    st['stats'] = {k: v for k, v in st.get('stats', {}).items() if k >= week_ago}
    st['sent_log'] = [x for x in st.get('sent_log', []) if now_ts - x.get('t', 0) < 26 * 3600][-200:]
    log("Quét nguồn tin...")
    arts = fetch_all(cfg)
    new = [a for a in arts if key_of(a) not in st['seen']]
    new = interleave(new)
    log(f"Tổng {len(arts)} bài, mới {len(new)}")
    try:
        max_n = int(max_send if max_send is not None else cfg.get('max_per_cycle', 10))
    except (TypeError, ValueError):
        max_n = 10
    if max_n < 1:
        max_n = 10
    sent = 0
    # Phase 1: Remove duplicates
    to_process = []
    for a in new:
        if sent + len(to_process) >= max_n:
            break
        if is_title_dup(a, st):
            log(f"  ⏭ Trùng nội dung: {a['title'][:60]} | {a['source']}")
            st['seen'][key_of(a)] = 1
            continue
        to_process.append(a)
    # Phase 2: Parallel fetch summaries + translate (biggest bottleneck)
    def _summarize_one(art):
        art['summary'], art['quote'], art['quote_speaker'] = make_summary_full(art['title'], art['link'], art['desc'])
        if cfg.get('translate') and is_english(art['title']):
            art['title_vi'] = translate_vi(art['title'])
            if art.get('summary'):
                art['summary_vi'] = translate_vi(art['summary'])
        return art
    if to_process:
        with ThreadPoolExecutor(max_workers=5) as ex:
            to_process = list(ex.map(_summarize_one, to_process))
    # Phase 3: Filter + send (fast, no network)
    for a in to_process:
        breaking = is_breaking_news(a)
        if not breaking and not matches_filters(a, cfg):
            st['seen'][key_of(a)] = 1
            continue
        if not a.get('image') and a.get('link'):
            a['image'] = fetch_og_image(a['link'])
        inline_kb = safe_button_url(a.get("link", ""), a.get("title_vi", a.get("title", "")))
        targets = db_list_users() or [chat_id]
        bot_id = get_bot_id(token)
        if bot_id:
            targets = [t for t in targets if t != bot_id]
        ok, out = broadcast_article(a, token, targets, inline_kb)
        tag = "🚨 BREAKING" if breaking else "✅"
        log(f"  {tag} [{a['cat']}] {a['title'][:60]} | {a['source']} → {len(targets)} người nhận")
        if not ok:
            log("     " + out.replace('\n', ' ')[:200])
            log("  🔁 Gửi lỗi — bài sẽ được thử lại ở vòng sau.")
            break
        st['seen'][key_of(a)] = 1
        today = now.strftime('%Y-%m-%d')
        st.setdefault('stats', {}).setdefault(today, {})
        st['stats'][today][a['source']] = st['stats'][today].get(a['source'], 0) + 1
        st.setdefault('sent_log', []).append({
            't': time.time(), 'title': a['title'], 'link': a['link'],
            'source': a['source'], 'summary': a['summary'], 'pub': a.get('pub', ''),
            'title_vi': a.get('title_vi'), 'summary_vi': a.get('summary_vi'),
        })
        try:
            db_save_article(int(chat_id), {**a, 'sent_at': time.time()})
            db_save_sent_log(int(chat_id), {**a, 'sent_at': time.time()})
        except Exception:
            pass
        save_state(st)
        sent += 1
        time.sleep(0.3)
    if hm in cfg.get('schedule', []):
        today = now.strftime('%Y-%m-%d')
        if st.get('last_digest', {}).get(hm) != today:
            cutoff = time.time() - 24 * 3600
            recent = [a for a in st.get('sent_log', []) if a.get('t', 0) >= cutoff and matches_filters(a, cfg)]
            recent.sort(key=lambda a: a.get('t', 0), reverse=True)
            if recent:
                digest_text = build_digest(recent[:DIGEST_MAX], now)
                digest_targets = db_list_users() or [chat_id]
                bot_id = get_bot_id(token)
                if bot_id:
                    digest_targets = [t for t in digest_targets if t != bot_id]
                any_ok = False
                for cid in digest_targets:
                    ok, out = send_telegram(digest_text, token, cid)
                    if ok:
                        any_ok = True
                    else:
                        low = (out or '').lower()
                        if 'bot was blocked' in low or 'chat not found' in low or 'user is deactivated' in low or 'kicked' in low:
                            db_remove_user(cid)
                log(f"  📅 Bản tin tổng hợp {hm}: {'✅' if any_ok else '❌'} → {len(digest_targets)} người nhận")
            st.setdefault('last_digest', {})[hm] = today
            save_state(st)
    save_state(st)
    log("Xong cycle.")

# ---------------------------------------------------------------------------
# Chat ID discovery
# ---------------------------------------------------------------------------
def discover_chat_id(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
        data = r.json()
        if not data.get('ok'):
            log("getUpdates thất bại: " + str(data.get('description', '')))
            return None
        fallback = None
        for u in data.get('result', []):
            msg = u.get('message') or {}
            chat = msg.get('chat') or {}
            cid = chat.get('id')
            if not cid:
                continue
            if chat.get('type') == 'private' and (msg.get('text') or '').strip().startswith('/start'):
                return cid
            if fallback is None:
                fallback = cid
        return fallback
    except Exception as ex:
        log(f"Lỗi getUpdates: {ex}")
    return None

# ---------------------------------------------------------------------------
# Webhook mode
# ---------------------------------------------------------------------------
class WebhookHandler(BaseHTTPRequestHandler):
    bot_token = None
    bot_chat_id = None

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            update = json.loads(body)
            msg = update.get('message') or {}
            text = (msg.get('text') or '').strip()
            if text.startswith('/'):
                cfg = load_config()
                chat_id = (msg.get('chat') or {}).get('id') or self.bot_chat_id
                cmd = text.split(None, 1)[0].lower().split('@')[0]
                admin_ids = get_admin_ids()
                from_id = (msg.get('from') or {}).get('id')
                if cmd == '/start':
                    db_add_user(chat_id)
                elif admin_ids and from_id not in admin_ids:
                    send_telegram("⛔ Chỉ admin mới được dùng lệnh này.", self.bot_token, chat_id, html=False)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                reply = handle_command(text, cfg, chat_id)
                if reply:
                    send_telegram(reply, self.bot_token, chat_id, html=False)
        except Exception as ex:
            log(f"Webhook error: {ex}")
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format, *args):
        pass

def start_webhook(port=8443, webhook_url=None):
    token = get_token()
    if not token:
        log("❌ Need TELEGRAM_BOT_TOKEN for webhook mode")
        return
    WebhookHandler.bot_token = token
    WebhookHandler.bot_chat_id = get_chat_id()
    if webhook_url:
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/setWebhook",
                             json={"url": webhook_url}, timeout=15)
            log(f"Webhook set: {r.json()}")
        except Exception as ex:
            log(f"Webhook set error: {ex}")
    server = HTTPServer(('0.0.0.0', port), WebhookHandler)
    log(f"🌐 Webhook listening on port {port}")
    server.serve_forever()

# ---------------------------------------------------------------------------
# Boot / PID management
# ---------------------------------------------------------------------------
def already_running():
    if not os.path.exists(PID_FILE):
        return False
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False

def poll_commands_forever():
    fail = 0
    while True:
        try:
            token = get_token()
            if not token:
                time.sleep(30)
                continue
            chat_id = get_chat_id()
            if not chat_id:
                cid = discover_chat_id(token)
                if cid:
                    save_chat_id(cid)
                    log(f"✅ Đã tìm thấy và lưu chat_id = {cid}")
                    chat_id = cid
            ok = True
            if chat_id:
                ok = process_commands(token, chat_id, load_config())
            fail = 0 if ok else fail + 1
        except Exception as ex:
            fail += 1
            log(f"Lỗi nhận lệnh Telegram: {ex}")
        time.sleep(min(10, 2 * max(fail, 1)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    setup_logging()
    init_db()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        run_once()
    elif mode == 'webhook':
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
        url = sys.argv[3] if len(sys.argv) > 3 else None
        start_webhook(port, url)
    else:
        if already_running():
            log("⚠️ Bot đã có một bản đang chạy. Hãy tắt bản cũ trước: pkill -f news_bot.py")
            sys.exit(1)
        open(PID_FILE, 'w').write(str(os.getpid()))
        interval = int(os.environ.get('NEWS_INTERVAL', '1800'))
        log(f"Chạy nền, quét tin mỗi {interval}s — lệnh Telegram phản hồi liên tục")
        try:
            threading.Thread(target=poll_commands_forever, daemon=True).start()
            while True:
                try:
                    run_once()
                except Exception as ex:
                    log(f"Lỗi cycle: {ex}")
                time.sleep(interval)
        except KeyboardInterrupt:
            log("Đã tắt bot (Ctrl+C).")
            print("🛑 Đã tắt bot.")
        finally:
            try:
                os.remove(PID_FILE)
            except OSError:
                pass

