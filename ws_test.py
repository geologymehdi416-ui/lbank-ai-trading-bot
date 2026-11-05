import websocket
import json
import time

URL = "wss://lbkperpws.lbank.com/ws"

def on_open(ws):
    print("CONNECTED")

    message = {
        "action": "subscribe",
        "subscribe": "1",
        "channel": "market_btcusdt_kline_15min"
    }

    ws.send(json.dumps(message))
    print("SUBSCRIBE SENT")

def on_message(ws, message):
    print("MARKET DATA:", message)

def on_error(ws, error):
    print("ERROR:", error)

def on_close(ws, code, msg):
    print("CONNECTION CLOSED")

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)

ws.run_forever()