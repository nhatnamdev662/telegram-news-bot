#!/data/data/com.termux/files/usr/bin/bash
# setup.sh — cài đặt cho Termux (không hỏi token, không chạy bot)
# Sau khi chạy xong: nhatnam config -> nhập token, nhatnam 30 -> chạy bot
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
ENV="$DIR/.env"

echo "🚀 BẮT ĐẦU CÀI ĐẶT..."
echo "1/3) Cập nhật Termux..."
pkg update -y && pkg upgrade -y
echo "2/3) Cài Python..."
pkg install -y python
echo "3/3) Cài thư viện..."
pip install feedparser requests deep_translator 2>/dev/null || pip install --break-system-packages feedparser requests deep_translator

# File cấu hình trống — điền bằng: nhatnam config
[ -f "$ENV" ] || cat > "$ENV" <<'ENVEOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF

# Ghi đè bản nhatnam cũ trong $PREFIX/bin nếu có
if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ]; then
    cp "$DIR/nhatnam" "$PREFIX/bin/nhatnam"
    chmod +x "$PREFIX/bin/nhatnam"
    echo "✅ Đã cài lệnh nhatnam toàn cục."
fi
chmod +x "$DIR/nhatnam" 2>/dev/null || true

echo ""
echo "✅ CÀI ĐẶT XONG!"
echo ""
echo "👉 Bước kế tiếp — chỉ 2 bước:"
echo "   1. Gõ: nhatnam config   (nhập bot token + chat id)"
echo "   2. Gõ: nhatnam 30       (chạy bot, dò tin mỗi 30 phút)"
echo ""
echo "💡 Chat id để trống cũng được — mở Telegram nhắn /start cho bot, bot tự tìm."
