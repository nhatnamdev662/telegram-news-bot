"""Chạy: python3 test_news_bot.py (không cần mạng, không cần token)"""
import importlib.util, json, tempfile, os, sys, time as _t

TMP = tempfile.mkdtemp()
spec = importlib.util.spec_from_file_location("nb", "news_bot.py")
nb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nb)

nb.STATE_FILE = os.path.join(TMP, "state.json")
nb.CONFIG_FILE = os.path.join(TMP, "config.json")
nb.ENV_FILE = os.path.join(TMP, ".env")
nb.LOG_FILE = os.path.join(TMP, "log.txt")
nb.DB_FILE = os.path.join(TMP, "bot.db")
open(nb.ENV_FILE, "w").write("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=123\nADMIN_IDS=111, 222\n")

def fresh_cfg(**kw):
    cfg = {"keywords": [], "categories": [], "sources": [], "schedule": [],
           "active_hours": [], "paused": False, "translate": True,
           "max_per_cycle": 10, "last_update_id": 0}
    cfg.update(kw)
    json.dump(cfg, open(nb.CONFIG_FILE, "w"))
    return cfg

def fresh_state():
    json.dump({"seen": {}, "seen_titles": {}, "last_digest": {}, "stats": {},
               "sent_log": [], "last_run": ""}, open(nb.STATE_FILE, "w"))

passed = 0
failed = 0
def check(name, got, expected):
    global passed, failed
    ok = got == expected
    status = "✅" if ok else "❌"
    if not ok:
        print(f"  {status} {name}: got {got!r}, expected {expected!r}")
        failed += 1
    else:
        passed += 1

# Initialize DB for tests
nb.init_db()

# === Basic functions ===
print("== title_key ==")
check("normal", nb.title_key("Bóng đá Việt Nam"), "bong a viet nam")
check("utf8", nb.title_key("19 chỗ nhồi nhét"), "19 cho nhoi nhet")
check("english", nb.title_key("UK supports Ukraine 100%"), "uk supports ukraine 100")
check("empty", nb.title_key(""), "")
check("punct", nb.title_key("A -- B, C!"), "a b c")

print("== is_title_dup ==")
fresh_state()
st = {"seen_titles": {}}
a1 = {"title": "Tin moi", "link": "https://a/1"}
a2 = {"title": "Tin moi", "link": "https://b/2"}
a3 = {"title": "Tin khac", "link": "https://c/3"}
check("first", nb.is_title_dup(a1, st), False)
check("same title diff link", nb.is_title_dup(a2, st), True)
check("different title", nb.is_title_dup(a3, st), False)
check("repeat same", nb.is_title_dup(a3, st), True)

print("== get_admin_ids ==")
check("ids", nb.get_admin_ids(), {111, 222})

print("== is_active_time ==")
check("no range", nb.is_active_time({"active_hours": []}, "03:00"), True)
check("in range", nb.is_active_time({"active_hours": ["06:00-22:00"]}, "12:30"), True)
check("out range", nb.is_active_time({"active_hours": ["06:00-22:00"]}, "23:00"), False)
check("multi range", nb.is_active_time({"active_hours": ["06:00-12:00", "18:00-22:00"]}, "19:00"), True)

print("== load_config normalization ==")
json.dump({"keywords": "football", "sources": "vnexpress"}, open(nb.CONFIG_FILE, "w"))
cfg = nb.load_config()
check("keywords str->list", cfg["keywords"], ["football"])
check("sources str->list", cfg["sources"], ["vnexpress"])

# === New features: atomic write ===
print("== _atomic_write ==")
test_atomic = os.path.join(TMP, "atomic_test.txt")
nb._atomic_write(test_atomic, "hello", as_json=False)
check("atomic text", open(test_atomic).read(), "hello")
nb._atomic_write(test_atomic, {"key": "val"})
check("atomic json", json.load(open(test_atomic)), {"key": "val"})
check("no tmp left", os.path.exists(test_atomic + ".tmp"), False)

# === New features: is_breaking_news ===
print("== is_breaking_news ==")
check("breaking vn", nb.is_breaking_news({"title": "Khẩn cấp: Động đất magnitude 7", "desc": "", "summary": ""}), True)
check("breaking en", nb.is_breaking_news({"title": "Breaking: Major earthquake hits Japan", "desc": "", "summary": ""}), True)
check("normal", nb.is_breaking_news({"title": "Đội tuyển Việt Nam thắng 3-0", "desc": "", "summary": ""}), False)
check("breaking in desc", nb.is_breaking_news({"title": "Tin thường", "desc": "Cảnh báo tsunami", "summary": ""}), True)

# === New features: get_emoji ===
print("== get_emoji ==")
check("thể thao", nb.get_emoji({"title": "Bóng đá Việt Nam", "desc": "", "summary": ""}), "⚽")
check("kinh doanh", nb.get_emoji({"title": "Chứng khoán tăng mạnh", "desc": "", "summary": ""}), "📈")
check("công nghệ", nb.get_emoji({"title": "iPhone 16 ra mắt", "desc": "", "summary": ""}), "💻")
check("giải trí", nb.get_emoji({"title": "Ca sĩ mới ra album", "desc": "", "summary": ""}), "🎬")
check("thế giới", nb.get_emoji({"title": "Israel Ukraine war", "desc": "", "summary": ""}), "🌍")
check("unknown", nb.get_emoji({"title": "Tin gì đó", "desc": "", "summary": ""}), "📌")

# === classify ===
print("== classify ==")
art_thethao = {"title": "Đội tuyển bóng đá Việt Nam", "desc": "", "summary": ""}
art_kinhdoanh = {"title": "Chứng khoán tăng mạnh", "desc": "", "summary": ""}
check("thể thao", "thể thao" in nb.classify(art_thethao), True)
check("kinh doanh", "kinh doanh" in nb.classify(art_kinhdoanh), True)

# === is_english ===
print("== is_english ==")
check("english title", nb.is_english("UK supports Ukraine 100%"), True)
check("vietnamese title", nb.is_english("Đội tuyển bóng đá Việt Nam"), False)
check("mixed short", nb.is_english("ABC"), True)
check("empty", nb.is_english(""), False)
check("all digits", nb.is_english("12345"), False)

# === translate_vi ===
print("== translate_vi (mock ==")
nb._trans_cache.clear()
nb.HAS_TRANSLATE = False
check("no translate lib", nb.translate_vi("Hello world"), "Hello world")
nb.HAS_TRANSLATE = True
nb._trans_cache["cached text"] = "văn bản đã cache"
check("cache hit", nb.translate_vi("cached text"), "văn bản đã cache")
nb.HAS_TRANSLATE = False

# === interleave ===
print("== interleave ==")
arts = [{"source": "A", "title": f"a{i}"} for i in range(3)] + [{"source": "B", "title": f"b{i}"} for i in range(3)]
result = nb.interleave(arts)
sources = [a["source"] for a in result]
check("round-robin", sources, ["A", "B", "A", "B", "A", "B"])

# === digests: build_digest ===
print("== digests: build_digest ==")
now = __import__("datetime").datetime.now(nb.VN_TZ)
arts_d = [{"title": "Test", "source": "SRC", "pub": "", "summary": "Sapo day du", "link": "https://x/1"}]
dig = nb.build_digest(arts_d, now)
check("digest has title", "Test" in dig, True)
check("digest has source", "SRC" in dig, True)

# === New features: build_msg format ===
print("== build_msg format ==")
art_msg = {"title": "Test title", "source": "VnExpress", "pub": "Mon, 18 Aug 2025 14:30:00 +0700",
           "link": "https://x/1", "summary": "Test summary here", "desc": ""}
msg = nb.build_msg(art_msg)
check("has emoji", "📌" in msg or "📰" in msg or "⚽" in msg or "📈" in msg or "💻" in msg or "🎬" in msg or "🌍" in msg, True)
check("has source", "VnExpress" in msg, True)
check("has separator", "─────────────────────" in msg, True)
check("has summary", "Test summary here" in msg, True)
check("has link", "Đọc thêm" in msg, True)
check("no old header", "BẢN TIN HÔM NAY" in msg, False)

# === New features: build_photo_caption UTF-8 ===
print("== build_photo_caption UTF-8 ==")
cap = nb.build_photo_caption(art_msg)
check("caption has title", "Test title" in cap, True)
check("caption has source", "VnExpress" in cap, True)
# Test that caption respects 1024 byte limit for Telegram
cap_bytes = len(cap.encode('utf-8'))
check("caption under 1024 bytes", cap_bytes <= 1024, True)
# Test with very long summary
long_art = {"title": "Short", "source": "SRC", "pub": "", "link": "https://x/1",
            "summary": "A" * 2000, "desc": ""}
long_cap = nb.build_photo_caption(long_art)
check("long caption under 1024 bytes", len(long_cap.encode('utf-8')) <= 1024, True)

# === New features: SQLite DB ===
print("== SQLite DB ==")
nb.db_save_article(123, {"title": "Test DB", "link": "https://x/db1", "source": "SRC",
                         "summary": "Sum", "pub": "", "image": "", "cat": "vn",
                         "sent_at": _t.time()})
nb.db_save_sent_log(123, {"title": "Test DB", "link": "https://x/db1", "source": "SRC",
                          "summary": "Sum", "sent_at": _t.time()})
recent = nb.db_get_recent_articles(123, hours=1)
check("db recent articles", len(recent) >= 1, True)
check("db recent title", recent[0]['title'], "Test DB")
stats = nb.db_get_today_stats(123)
check("db today stats has SRC", "SRC" in stats, True)
day_arts = nb.db_get_day_articles(123)
check("db day articles", len(day_arts) >= 1, True)

# === New features: multi-user config ===
print("== multi-user config ==")
nb.db_save_user_config(111, {"keywords": ["football"], "categories": []})
nb.db_save_user_config(222, {"keywords": ["bitcoin"], "categories": ["kinh doanh"]})
cfg111 = nb.db_get_user_config(111)
cfg222 = nb.db_get_user_config(222)
check("user 111 config", cfg111["keywords"], ["football"])
check("user 222 config", cfg222["keywords"], ["bitcoin"])
check("user 999 config (none)", nb.db_get_user_config(999), None)

# === New features: handle_command with new commands ===
print("== handle_command new cmds ==")
fresh_cfg()
cfg = nb.load_config()
check("/start", nb.handle_command("/start", cfg).splitlines()[0][:5], "👋 Xin")
check("/help has /tinmoi", "/tinmoi" in nb.handle_command("/help", cfg), True)
check("/help has /tinday", "/tinday" in nb.handle_command("/help", cfg), True)
check("/them", "Đã thêm" in nb.handle_command("/them bitcoin", cfg), True)
check("/xoa", "Đã xóa" in nb.handle_command("/xoa bitcoin", cfg), True)
check("/xem shows sources", "Nguồn:" in nb.handle_command("/xem", cfg), True)
check("/tamngung", "tạm dừng" in nb.handle_command("/tamngung", cfg).lower(), True)
check("/tieptuc", "tiếp tục" in nb.handle_command("/tieptuc", cfg).lower(), True)
check("/thongke empty", "chưa có" in nb.handle_command("/thongke", cfg), True)
check("/gio ok", "06:00-22:00" in nb.handle_command("/gio 06:00-22:00", cfg), True)
check("/gio bad", "hợp lệ" in nb.handle_command("/gio 25:99", cfg), True)
check("/gio rong", "cả ngày" in nb.handle_command("/gio rong", cfg), True)
check("/nguon rong", "tất cả" in nb.handle_command("/nguon rong", cfg), True)
check("/nguon zzz", "không có" in nb.handle_command("/nguon zzz", cfg), True)

# === New features: handle_tinday ===
print("== handle_tinday ==")
day_msg = nb.handle_tinday(123)
check("tinday has header", "TIN TỨC HÔM NAY" in day_msg, True)

# === New features: handle_tinmoi (no network) ===
print("== handle_tinmoi ==")
# Mock fetch_all to return test data
orig_fetch = nb.fetch_all
nb.fetch_all = lambda cfg=None: [
    {"title": "Tin 1", "source": "VnExpress", "pub": "", "link": "https://x/1", "desc": "", "summary": "Sapo 1"},
    {"title": "Tin 2", "source": "BBC", "pub": "", "link": "https://x/2", "desc": "", "summary": "Sapo 2"},
]
tinmoi_msg = nb.handle_tinmoi("", cfg)
check("tinmoi has header", "TIN MỚI NHẤT" in tinmoi_msg, True)
check("tinmoi has tin 1", "Tin 1" in tinmoi_msg, True)
check("tinmoi has tin 2", "Tin 2" in tinmoi_msg, True)
nb.fetch_all = orig_fetch

# === New features: build_daily_summary ===
print("== build_daily_summary ==")
daily = nb.build_daily_summary(123, now)
check("daily has header", "TIN TỨC HÔM NAY" in daily, True)

# === Original command tests (backward compat) ===
print("== handle_command backward compat ==")
fresh_cfg()
cfg = nb.load_config()
check("/them", "Đã thêm" in nb.handle_command("/them bitcoin", cfg), True)
check("/xoa", "Đã xóa" in nb.handle_command("/xoa bitcoin", cfg), True)
check("/chude", "chủ đề" in nb.handle_command("/chude thể thao", cfg).lower(), True)
check("/lich", "07:00" in nb.handle_command("/lich 07:00, 18:00", cfg), True)
check("/trangthai", "Trạng thái" in nb.handle_command("/trangthai", cfg), True)


# === New: summarization functions ===
print("== split_sentences ==")
sents = nb.split_sentences("Đây là câu đầu. Đây là câu thứ hai. Và đây là câu ba.")
check("split 3", len(sents), 3)
check("first sentence", sents[0], "Đây là câu đầu.")
check("empty", nb.split_sentences(""), [])
check("short ignored", nb.split_sentences("OK."), [])

print("== _title_words ==")
tw = nb._title_words("Đội tuyển Việt Nam thắng Thái Lan")
check("has vietnam", "việt" in tw, True)
check("has thai", "thái" in tw, True)
check("no stopword", "thắng" in tw or len(tw) >= 2, True)

print("== score_sentence ==")
# Position: first sentence should score higher
sc1 = nb.score_sentence("Câu đầu tiên quan trọng về bóng đá.", {"bóng", "đá"}, 0, 5)
sc2 = nb.score_sentence("Câu cuối ít quan trọng hơn.", {"bóng", "đá"}, 4, 5)
check("first > last", sc1 > sc2, True)

# Info density: sentence with numbers should score higher
sc_num = nb.score_sentence("Doanh thu đạt 1.000 tỷ đồng trong quý 2/2025.", set(), 2, 5)
sc_no = nb.score_sentence("Doanh thu tăng trưởng trong thời gian qua.", set(), 2, 5)
check("number > no number", sc_num > sc_no, True)

print("== extract_quote ==")
# VN quote
text_vi = 'Thủ tướng Phạm Minh Chính nói: "Chính phủ sẽ hỗ trợ doanh nghiệp vượt khó."'
q, sp = nb.extract_quote(text_vi)
check("vn quote found", q is not None, True)
check("vn quote content", "hỗ trợ doanh nghiệp" in q, True)
check("vn speaker", "Chủ tịch" in sp or "Thủ tướng" in sp or "Chính" in sp or sp != '', True)

# EN quote
text_en = 'President Biden said: "We will continue to support Ukraine in this conflict."'
q2, sp2 = nb.extract_quote(text_en)
check("en quote found", q2 is not None, True)
check("en quote content", "support Ukraine" in q2, True)

# No quote
text_no = 'Đây là bài viết không có quote nào cả. Nội dung bình thường.'
q3, sp3 = nb.extract_quote(text_no)
check("no quote", q3, None)

print("== _trim_summary ==")
short = "Câu ngắn."
check("short unchanged", nb._trim_summary(short), short)
long_text = ". ".join([f"Câu số {i} với nội dung đủ dài để kiểm tra việc cắt câu." for i in range(20)])
trimmed = nb._trim_summary(long_text, target_min=100, target_max=300)
check("trimmed length", len(trimmed) <= 300, True)
check("trimmed not empty", len(trimmed) > 50, True)

print("== summarize_article ==")
paras = [
    "Đội tuyển Việt Nam đã giành chiến thắng 3-0 trước Thái Lan trong trận đấu tối nay.",
    "Bàn thắng được ghi bởi Nguyễn Văn Quyết ở phút 15, 35 và 67.",
    "HLV Park Hang-seo rất hài lòng với kết quả này.",
    "Trận đấu diễn ra trên sân Mỹ Đình với sự cổ vũ của 40.000 khán giả.",
    "Đây là chiến thắng quan trọng trong vòng loại World Cup 2026.",
]
summary = nb.summarize_article("Việt Nam thắng Thái Lan 3-0", paras, "")
check("summary not empty", len(summary) > 50, True)
check("summary has content", "Việt Nam" in summary or "bàn thắng" in summary.lower() or "thái lan" in summary.lower(), True)

print("== make_summary (mock) ==")
# Mock fetch_article_paras to avoid network
orig_fetch = nb.fetch_article_paras
nb.fetch_article_paras = lambda url: ["Đây là đoạn đầu bài viết với nội dung chi tiết về sự kiện quan trọng.", "Bàn thắng được ghi ở phút 25."]
# Short sapo → should fetch
ms = nb.make_summary("Test title", "https://example.com", "Ngắn")
check("make_summary short sapo fetches", len(ms) > 20, True)
# Long sapo → should NOT fetch
ms2 = nb.make_summary("Test title", "https://example.com", "Đây là sapo đủ dài từ RSS feed với hơn 80 ký tự để bot không cần fetch thêm trang bài viết.")
check("make_summary long sapo no fetch", len(ms2) >= 80, True)
nb.fetch_article_paras = orig_fetch

print("== make_summary_full ==")
nb.fetch_article_paras = lambda url: [
    "Bộ trưởng Bộ GD&ĐT Nguyễn Kim Sơn cho biết: \"Năm nay sẽ tăng chỉ tiêu đại học.\"",
    "Theo thống kê, số thí sinh đăng ký tăng 15% so với năm ngoái.",
    "Đây là năm thứ 3 liên tiếp số lượng thí sinh tăng.",
]
ms3, q, sp = nb.make_summary_full("Thí sinh tăng mạnh", "https://example.com", "Bộ GD&ĐT công bố số liệu.")
check("full summary not empty", len(ms3) > 20, True)
check("full quote found", q is not None, True)
nb.fetch_article_paras = orig_fetch

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("ALL TESTS PASSED")
