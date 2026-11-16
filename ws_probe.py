
import asyncio
import json
import time
import websockets

URL = "wss://lbkperpws.lbank.com/ws"
SYMBOL = "Exchange_BTCUSDT"

TOPICS = ["11", "12", "13"]


async def main():
    print("Connecting to LBank Futures...")
    try:
        async with websockets.connect(
            URL,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:

            print("CONNECTED")

            # Subscribe to all three suspected KLine channels
            for index, topic in enumerate(TOPICS, start=1):
                message = {
                    "SendTopicAction": {
                        "Action": "1",
                        "LocalNo": index,
                        "TopicID": topic,
                        "FilterValue": SYMBOL,
                        "ResumeNo": -1,
                    }
                }

                await ws.send(json.dumps(message))
                print(f"SUBSCRIBED: Topic {topic}")
                await asyncio.sleep(0.5)

            print("\nWaiting for Futures KLine messages...\n")

            deadline = time.monotonic() + 60

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()

                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=remaining
                    )

                    if raw == "ping":
                        await ws.send("pong")
                        print("PING/PONG")
                        continue

                    print("\n----- RAW MESSAGE -----")
                    print(raw[:5000])

                    try:
                        data = json.loads(raw)

                        if isinstance(data, dict):
                            action = data.get("action", "")

                            if action == "PushKLine":
                                print("\n*** KLINE MESSAGE DETECTED ***")
                                print(json.dumps(
                                    data,
                                    indent=2,
                                    ensure_ascii=False
                                ))

                            elif "RecvTopicAction" in data:
                                print("SUBSCRIPTION ACK:", data)

                    except json.JSONDecodeError:
                        print("Non-JSON message received")

                except asyncio.TimeoutError:
                    print("No more messages received.")
                    break

            print("\nProbe finished.")

    except Exception as e:
        print("CONNECTION ERROR:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
