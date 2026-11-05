import json
import websocket
import threading

URL = "wss://lbkperpws.lbank.com/ws"

def on_open(ws):
    print("CONNECTED")

    message = {
        "SendTopicAction": {
            "Action": "1",
            "LocalNo": 1,
            "TopicID": "7",
            "FilterValue": "Exchange_BTCUSDT",
            "ResumeNo": -1
        }
    }

    print("SENDING:", json.dumps(message))
    ws.send(json.dumps(message))

    threading.Timer(15, ws.close).start()

def on_message(ws, message):
    print("SERVER MESSAGE:", repr(message))

def on_error(ws, error):
    print("ERROR:", repr(error))

def on_close(ws, code, reason):
    print("CLOSED:", code, reason)

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)

ws.run_forever()