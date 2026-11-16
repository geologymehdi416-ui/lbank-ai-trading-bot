# فایل‌های جایگزین ربات LBank Futures

## فایل‌هایی که باید جایگزین شوند
1. `collector.py`
2. `sniper_entry.py`
3. `.github/workflows/scan.yml`

فایل `requirements.txt` فعلی را نگه دارید؛ نیازی به تغییر آن نیست.

## نصب
- در مخزن GitHub، فایل‌ها را با همین مسیرها جایگزین کنید.
- مطمئن شوید GitHub Actions فعال است.
- در Settings → Secrets and variables → Actions این دو Secret وجود داشته باشند:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
- از Actions، workflow با نام `LBank Futures Smart Scanner` را یک‌بار دستی اجرا کنید.

## رفتار این نسخه
- فقط endpointهای عمومی Futures برای instrument و marketData را می‌خواند.
- قرارداد را با metadata رسمی instrument تطبیق می‌دهد؛ پسوند USDT به‌تنهایی کافی نیست.
- snapshot قبلی را جایگزین می‌کند تا فایل داده بی‌نهایت بزرگ نشود.
- حداکثر ۵ بازار تازه را بر اساس turnover ۲۴ساعته گزارش می‌کند.
- هیچ سیگنال لانگ/شورت تولید نمی‌کند، چون OHLCV معتبر Futures هنوز تأیید نشده است.
- هیچ سفارش واقعی ثبت نمی‌کند و permission مخزن را فقط خواندنی می‌خواهد.

## نکته مهم
این نسخه عمداً یک گزارش مانیتورینگ امن است، نه «نقطه‌زن» نهایی. برای تحلیل EMA/RSI/MACD و ورود 1m باید ابتدا منبع کندل Futures یا جریان معاملات Futures با schema رسمی و تست‌شده تأیید شود. استفاده از Spot به‌جای Futures ممنوع باقی می‌ماند.
