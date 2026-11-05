import websocket

URL = "wss://lbkperpws.lbank.com/ws"

try:
    ws = websocket.create_connection(URL, timeout=15)

    print("FUTURES WEBSOCKET CONNECTED")
    print("Connection successful.")

    ws.close()

except Exception as error:
    print("WEBSOCKET CONNECTION FAILED:")
    print(error)