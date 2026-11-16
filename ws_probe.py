
import asyncio
import json
import websockets

URL = "wss://lbkperpws.lbank.com/ws"

async def main():
    print("Connecting to LBank Futures...")
    try:
        async with websockets.connect(
            URL,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20
        ) as ws:
            print("CONNECTED")
            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    print(f"MESSAGE {i+1}:")
                    print(msg[:3000])
                except asyncio.TimeoutError:
                    print("No message received in 10 seconds")
                    break
    except Exception as e:
        print("CONNECTION ERROR:", repr(e))

asyncio.run(main())
