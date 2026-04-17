#!/bin/bash
# Finance Bot — systemd service orqali boshqarish

# Oldin barcha stray processlarni o'ldirish (systemd orqali emas ishlaganlar)
SYSTEMD_PID=$(systemctl --user show finance-bot.service --property=MainPID --value 2>/dev/null)
for pid in $(pgrep -f "python.*bot.py" 2>/dev/null); do
    if [ "$pid" != "$SYSTEMD_PID" ] && [ -n "$pid" ]; then
        kill -9 "$pid" 2>/dev/null && echo "🗑  Stray process o'chirildi: $pid"
    fi
done

echo "🔄 Finance Bot qayta ishga tushirilmoqda..."
systemctl --user restart finance-bot.service
sleep 2
systemctl --user status finance-bot.service --no-pager -l
echo ""
echo "📄 Log: tail -f /home/orzu/Finance/finance_bot.log"
