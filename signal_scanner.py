
import json
import os
from datetime import datetime, timezone

SIGNALS_FILE = "data/signals.json"
OUTPUT_FILE = "data/final_signals.json"
HISTORY_FILE = "data/signal_history.json"

MIN_RR = 1.5
MAX_SIGNALS = 5


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def validate_signal(signal):
    required = [
        "symbol",
        "direction",
        "entry",
        "stop_loss",
        "take_profit",
        "rr",
    ]

    if not all(key in signal for key in required):
        return False

    try:
        entry = float(signal["entry"])
        sl = float(signal["stop_loss"])
        tp = float(signal["take_profit"])
        rr = float(signal["rr"])
    except (TypeError, ValueError):
        return False

    if min(entry, sl, tp) <= 0:
        return False

    if rr < MIN_RR:
        return False

    direction = signal["direction"].upper()

    if direction == "LONG":
        if not (sl < entry < tp):
            return False

        actual_rr = (tp - entry) / (entry - sl)

    elif direction == "SHORT":
        if not (tp < entry < sl):
            return False

        actual_rr = (entry - tp) / (sl - entry)

    else:
        return False

    # Recalculate RR instead of trusting the supplied value.
    if actual_rr < MIN_RR:
        return False

    signal["rr"] = round(actual_rr, 2)
    return True


def quality_score(signal):
    score = 0

    rr = float(signal["rr"])
    rsi = float(signal.get("rsi_15m", 50))
    volume = float(signal.get("volume_ratio", 0))

    if rr >= 2:
        score += 2
    elif rr >= 1.5:
        score += 1

    if signal["direction"] == "LONG" and 52 <= rsi <= 68:
        score += 2

    elif signal["direction"] == "SHORT" and 32 <= rsi <= 48:
        score += 2

    if volume >= 1.5:
        score += 2
    elif volume >= 1.2:
        score += 1

    if signal.get("trend_4h") == signal.get("trend_1h"):
        score += 1

    expected_structure = (
        "BULLISH_BREAKOUT"
        if signal["direction"] == "LONG"
        else "BEARISH_BREAKDOWN"
    )

    if signal.get("structure_5m") == expected_structure:
        score += 1

    return score


def signal_key(signal):
    return (
        f"{signal['symbol']}:"
        f"{signal['direction']}"
    )


def main():
    signals = load_json(SIGNALS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    if not isinstance(signals, list):
        signals = []

    accepted = []

    for signal in signals:
        if not validate_signal(signal):
            continue

        score = quality_score(signal)

        # Minimum quality score.
        if score < 5:
            continue

        signal["quality_score"] = score
        signal["created_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        key = signal_key(signal)

        # Prevent repeating the same direction for the same symbol.
        previous = history.get(key)

        if previous:
            previous_entry = float(previous.get("entry", 0))
            current_entry = float(signal["entry"])

            if previous_entry > 0:
                difference = abs(
                    current_entry - previous_entry
                ) / previous_entry

                if difference < 0.002:
                    continue

        accepted.append(signal)

    accepted.sort(
        key=lambda x: (
            x["quality_score"],
            x["rr"]
        ),
        reverse=True
    )

    accepted = accepted[:MAX_SIGNALS]

    for signal in accepted:
        history[signal_key(signal)] = {
            "entry": signal["entry"],
            "created_at": signal["created_at"],
        }

    save_json(OUTPUT_FILE, accepted)
    save_json(HISTORY_FILE, history)

    print("=" * 40)

    if not accepted:
        print("NO VALID SIGNAL")
        print("DO NOT ENTER")
    else:
        for signal in accepted:
            print(
                signal["symbol"],
                signal["direction"],
                "Entry:", signal["entry"],
                "SL:", signal["stop_loss"],
                "TP:", signal["take_profit"],
                "RR:", signal["rr"],
                "Score:", signal["quality_score"],
            )

    print("=" * 40)


if __name__ == "__main__":
    main()
