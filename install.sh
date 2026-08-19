#!/usr/bin/env bash
# install.sh — cài Telegram News Bot từ GitHub, chuẩn cho Termux mới tải
# Cài xong KHÔNG hỏi token, KHÔNG chạy bot. Bạn tự làm 2 bước:
#   nhatnam config   -> nhập bot token + chat id
#   nhatnam 30       -> chạy bot (dò tin mỗi 30 phút)
# Dùng:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/nhatnamdev662/telegram-news-bot/main/install.sh)"
set -e
REPO_RAW="https://raw.githubusercontent.com/nhatnamdev662/telegram-news-bot/main"

echo "📁 Chuẩn bị thư mục..."
if [ -z "${INSTALL_DIR:-}" ]; then
    # Cấp quyền storage nếu là Termux
    if command -v termux-setup-storage >/dev/null 2>&1 && [ ! -d "$HOME/storage/downloads" ]; then
        echo "👉 Hãy bấm 'Cho phép' khi Termux xin quyền truy cập bộ nhớ..."
        termux-setup-storage >/dev/null 2>&1 || true
        sleep 5
    fi
    if [ -d "$HOME/storage/downloads" ]; then
        TARGET="$HOME/storage/downloads/botnews"
    else
        TARGET="$HOME/botnews"
    fi
else
    TARGET="$INSTALL_DIR"
fi
mkdir -p "$TARGET"
cd "$TARGET"
echo "   Thư mục: $TARGET"

echo "📥 Tải mã nguồn từ GitHub..."
for f in news_bot.py nhatnam setup.sh .env.example config.example.json; do
    curl -fsSL "$REPO_RAW/$f" -o "$TARGET/$f"
done
chmod +x nhatnam setup.sh 2>/dev/null || true

echo "🐍 Cài Python + thư viện (auto-yes)..."
if ! command -v python >/dev/null 2>&1; then
    if command -v pkg >/dev/null 2>&1; then
        pkg update -y >/dev/null 2>&1 || true
        pkg install -y python
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y >/dev/null 2>&1 || true
        sudo apt-get install -y python3 python3-pip >/dev/null 2>&1 || true
    fi
fi
pip install -q feedparser requests deep_translator 2>/dev/null || pip install -q --break-system-packages feedparser requests deep_translator || echo "⚠️  Không cài được thư viện — kiểm tra lại pip"

echo "⚙️  Tạo file cấu hình .env (trống — điền sau bằng nhatnam config)..."
[ -f "$TARGET/.env" ] || cp "$TARGET/.env.example" "$TARGET/.env"

if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ]; then
    cp "$TARGET/nhatnam" "$PREFIX/bin/nhatnam"
    chmod +x "$PREFIX/bin/nhatnam"
    echo "✅ Đã cài lệnh nhatnam (dùng được mọi nơi)."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ CÀI ĐẶT XONG — Bot ở: $TARGET"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📌 Bước tiếp theo — chỉ 2 bước:"
echo "   1. nhatnam config    -> nhập bot token (từ @BotFather) + chat id"
echo "   2. nhatnam 30        -> chạy bot, dò tin mỗi 30 phút (Ctrl+C dừng)"
echo ""
echo "💡 Lệnh khác: nhatnam test (gửi thử 1 lượt), nhatnam log (xem log)"
echo "   Chat id để trống cũng được — bot tự tìm khi bạn nhắn /start cho bot."
