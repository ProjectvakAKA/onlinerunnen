import hashlib
import hmac
import os
import subprocess
import threading
from typing import Optional

from flask import Flask, Response, request

app = Flask(__name__)

_run_lock = threading.Lock()
_run_process: Optional[subprocess.Popen] = None


def _is_valid_signature(raw_body: bytes, signature: str) -> bool:
    secret = os.getenv("DROPBOX_WEBHOOK_SECRET") or os.getenv("APP_SECRET_SOURCE_FULL")
    if not secret:
        return True
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _start_single_cycle() -> bool:
    global _run_process
    with _run_lock:
        if _run_process is not None and _run_process.poll() is None:
            # Already running, skip
            return False
        _run_process = subprocess.Popen(
            ["python", "contract_system.py", "--once"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return True


@app.get("/")
def health() -> Response:
    return Response("ok", status=200)


@app.get("/dropbox/webhook")
def verify_webhook() -> Response:
    """Dropbox webhook verification (GET challenge)"""
    challenge = request.args.get("challenge", "")
    return Response(challenge, status=200, mimetype="text/plain")


@app.post("/dropbox/webhook")
def on_dropbox_event() -> Response:
    """Dropbox webhook trigger (POST)"""
    signature = request.headers.get("X-Dropbox-Signature", "")
    raw_body = request.get_data(cache=False)

    if not _is_valid_signature(raw_body, signature):
        return Response("invalid signature", status=403)

    started = _start_single_cycle()
    if started:
        return Response("started", status=200)
    else:
        return Response("already running", status=200)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
