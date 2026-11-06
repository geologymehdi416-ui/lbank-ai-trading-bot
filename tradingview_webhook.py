import os
import json
import hmac
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/webhook", methods=["POST"])
def webhook():
    expected = os.environ.get("WEBHOOK_SECRET", "")
    payload = request.get_json(silent=True)

    if not expected:
        return jsonify({"error": "Secret not configured"}), 503

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    supplied = str(payload.pop("secret", ""))

    if not hmac.compare_digest(supplied, expected):
        return jsonify({"error": "Unauthorized"}), 401

    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source": "TradingView",
        "payload": payload
    }

    print(json.dumps(event), flush=True)

    return jsonify({"status": "received"}), 200