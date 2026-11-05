import json
import threading
import websocket

URL = "wss://lbkperpws.lbank.com/ws"

print("TESTING TOPIC 11")
ws = websocket.WebSocketApp(URL, on_open=lambda w: (print("CONNECTED 11"), w.send(json.dumps({"SendTopicAction":{"Action":"1","LocalNo":1,"TopicID":"11","FilterValue":"Exchange_BTCUSDT","ResumeNo":-1}})), threading.Timer(8,w.close).start()), on_message=lambda w,m: print("TOPIC 11 MESSAGE:",repr(m)), on_error=lambda w,e: print("TOPIC 11 ERROR:",repr(e)), on_close=lambda w,c,r: print("TOPIC 11 CLOSED:",c,r))
ws.run_forever()

print("TESTING TOPIC 12")
ws = websocket.WebSocketApp(URL, on_open=lambda w: (print("CONNECTED 12"), w.send(json.dumps({"SendTopicAction":{"Action":"1","LocalNo":1,"TopicID":"12","FilterValue":"Exchange_BTCUSDT","ResumeNo":-1}})), threading.Timer(8,w.close).start()), on_message=lambda w,m: print("TOPIC 12 MESSAGE:",repr(m)), on_error=lambda w,e: print("TOPIC 12 ERROR:",repr(e)), on_close=lambda w,c,r: print("TOPIC 12 CLOSED:",c,r))
ws.run_forever()

print("TESTING TOPIC 13")
ws = websocket.WebSocketApp(URL, on_open=lambda w: (print("CONNECTED 13"), w.send(json.dumps({"SendTopicAction":{"Action":"1","LocalNo":1,"TopicID":"13","FilterValue":"Exchange_BTCUSDT","ResumeNo":-1}})), threading.Timer(8,w.close).start()), on_message=lambda w,m: print("TOPIC 13 MESSAGE:",repr(m)), on_error=lambda w,e: print("TOPIC 13 ERROR:",repr(e)), on_close=lambda w,c,r: print("TOPIC 13 CLOSED:",c,r))
ws.run_forever()

print("ALL TESTS FINISHED")