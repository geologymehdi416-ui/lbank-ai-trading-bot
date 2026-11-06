import json

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
prices = {s: [] for s in symbols}

try:
    with open("data/prices.jsonl") as f:
        for line in f:
            item = json.loads(line)
            s = item["symbol"]
            if s in prices:
                prices[s].append(float(item["lastPrice"]))

    for s in symbols:
        p = prices[s]
        print("\n", s)

        if len(p) < 50:
            print("WAIT: Not enough data")
            continue

        avg = sum(p[-20:]) / 20
        print("Price:", p[-1])
        print("Average:", round(avg, 4))

        if p[-1] > avg:
            print("BIAS: LONG")
        elif p[-1] < avg:
            print("BIAS: SHORT")
        else:
            print("NO TRADE")

except Exception as e:
    print("ERROR:", e)