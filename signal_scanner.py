import json
import os
import requests
from pathlib import Path

BYBIT_URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAMES = ["15", "60", "240"]


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd_histogram(closes):
    if len(closes) < 35:
        return None

    macd_values = []

    for i in range(25, len(closes) + 1):
        subset = closes[:i]
        fast = ema(subset, 12)
        slow = ema(subset, 26)
        macd_values.append(fast - slow)

    if len(macd_values) < 9:
        return None

    signal = ema(macd_values, 9)
    return macd_values[-1] - signal


def get_candles(symbol, interval):
    response = requests.get(
        BYBIT_URL,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": 100
        },
        timeout=20
    )
    response.raise_for_status()

    data = response.json()

    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))

    rows = data["result"]["list"]
    rows.reverse()

    return [
        {
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]


def load_lbank_prices():
    path = Path("data/prices.jsonl")
    prices = {}

    if not path.exists():
        return prices

    with path.open(encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
                symbol = record.get("symbol")
                price = record.get("lastPrice")

                if symbol and price:
                    prices[symbol] = float(price)

            except (ValueError, TypeError, json.JSONDecodeError):
                continue

    return prices


def analyze(symbol, interval, lbank_prices):
    candles = get_candles(symbol, interval)

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    current = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi = calculate_rsi(closes)
    macd = calculate_macd_histogram(closes)

    average_volume = sum(volumes[-21:-1]) / 20
    volume_ratio = (
        volumes[-1] / average_volume
        if average_volume > 0 else 0
    )

    support = min(closes[-20:])
    resistance = max(closes[-20:])

    if ema20 is None or ema50 is None or rsi is None or macd is None:
        return f"{symbol} | داده کافی نیست"

    if current > ema20 > ema50 and macd > 0:
        trend = "صعودی"
    elif current < ema20 < ema50 and macd < 0:
        trend = "نزولی"
    else:
        trend = "خنثی"

    if trend == "صعودی" and 45 <= rsi <= 68:
        signal = "بررسی موقعیت لانگ"
    elif trend == "نزولی" and 32 <= rsi <= 55:
        signal = "بررسی موقعیت شورت"
    else:
        signal = "ورود نکن"

    lbank_price = lbank_prices.get(symbol)

    result = [
        f"📊 {symbol} | تایم‌فریم {interval} دقیقه",
        f"روند: {trend}",
        f"وضعیت: {signal}",
        f"قیمت تحلیلی Bybit: {current:.6g}",
        f"قیمت LBank: {lbank_price if lbank_price else 'داده موجود نیست'}",
        f"EMA20: {ema20:.6g}",
        f"EMA50: {ema50:.6g}",
        f"RSI: {rsi:.2f}",
        f"MACD Histogram: {macd:.6g}",
        f"نسبت حجم: {volume_ratio:.2f}",
        f"حمایت: {support:.6g}",
        f"مقاومت: {resistance:.6g}",
    ]

    if lbank_price:
        difference = abs(current - lbank_price) / lbank_price * 100
        result.append(f"اختلاف قیمت دو صرافی: {difference:.2f}%")

    result.append("⚠️ تحلیل آزمایشی؛ قبل از معامله قیمت LBank را بررسی کن.")
    result.append("━━━━━━━━━━━━━━━━")

    return "\n".join(result)


def main():
    lbank_prices = load_lbank_prices()
    report = [
        "🤖 گزارش اسکنر فیوچرز LBank",
        "منبع کندل‌ها: Bybit",
        "━━━━━━━━━━━━━━━━"
    ]

    for symbol in SYMBOLS:
        for interval in TIMEFRAMES:
            try:
                report.append(
                    analyze(symbol, interval, lbank_prices)
                )
            except Exception as error:
                report.append(
                    f"❌ {symbol} / {interval}: {str(error)}"
                )

    report.append("⚠️ این ربات سفارش ثبت نمی‌کند.")
    report.append("از اهرم بالا استفاده نکن.")

    text = "\n".join(report)

    Path("report.txt").write_text(
        text,
        encoding="utf-8"
    )

    print(text)


if __name__ == "__main__":
    main()