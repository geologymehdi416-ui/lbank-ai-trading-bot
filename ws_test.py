import json
import websocket
import threading

URL = "wss://lbkperpws.lbank.com/ws"

message_count = 0

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
    print("SUBSCRIBE SENT: BTCUSDT trades")

    # Close connection after 15 seconds
    threading.Timer(15, ws.close).start()

def on_message(ws, message):
    global message_count
    message_count += 1

    print("MARKET DATA:", message[:2000])

def on_error(ws, error):
    print("ERROR:", error)

def on_close(ws, code, reason):
    print("CLOSED:", code, reason)
    print("TOTAL MESSAGES:", message_count)

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)

ws.run_forever(ping_interval=20, ping_timeout=10)