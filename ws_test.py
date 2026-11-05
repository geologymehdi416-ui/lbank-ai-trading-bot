import json
import websocket
import threading
import time

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

    payload = json.dumps(message)
    print("SENDING:", payload)
    ws.send(payload)

    threading.Timer(20, ws.close).start()

def on_message(ws, message):
    print("RAW SERVER MESSAGE:", repr(message))

def on_data(ws, data, data_type, continue_flag):
    print("RAW DATA TYPE:", data_type)
    print("RAW DATA:", repr(data))

def on_error(ws, error):
    print("WEBSOCKET ERROR:", repr(error))

def on_close(ws, code, reason):
    print("CONNECTION CLOSED:", code, reason)

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_data=on_data,
    on_error=on_error,
    on_close=on_close
)

ws.run_forever(
    ping_interval=10,
    ping_timeout=5
)