import json
import threading
import websocket

URL = "wss://lbkperpws.lbank.com/ws"
TOPICS = ["11", "12", "13"]

def test_topic(topic_id):
received = []

def on_open(ws):
    print("CONNECTED")
    message = {
        "SendTopicAction": {
            "Action": "1",
            "LocalNo": 1,
            "TopicID": topic_id,
            "FilterValue": "Exchange_BTCUSDT",
            "ResumeNo": -1
        }
    }
    print("SENDING TOPIC:", topic_id)
    ws.send(json.dumps(message))
    threading.Timer(8, ws.close).start()

def on_message(ws, message):
    received.append(message)
    print("SERVER MESSAGE:", repr(message))

def on_error(ws, error):
    print("ERROR:", repr(error))

def on_close(ws, code, reason):
    print("CLOSED:", code, reason)

print("TESTING TOPIC:", topic_id)
ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)
ws.run_forever()
print("MESSAGE COUNT:", len(received))

for topic_id in TOPICS:
test_topic(topic_id)