import json
import websocket

URL = "wss://lbkperpws.lbank.com/ws"

def on_open(ws):
    print("CONNECTED")

    message = {
        "SendTopicAction": {
            "Action": "1",
            "LocalNo": 1,
            "TopicID": "2",
            "FilterValue": "Exchange_BTCUSDT",
            "ResumeNo": -1
        }
    }

    ws.send(json.dumps(message))
    print("SUBSCRIBE SENT")

def on_message(ws, message):
    print("MARKET DATA:", message[:2000])

def on_error(ws, error):
    print("ERROR:", error)

def on_close(ws, code, reason):
    print("CLOSED:", code, reason)

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)

ws.run_forever(ping_interval=20, ping_timeout=10)