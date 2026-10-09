# -*- coding: utf-8 -*-
"""Flask 后端：提供估值数据接口并托管前端页面。"""
import time
import threading
from flask import Flask, jsonify, send_from_directory

import wglh_client

app = Flask(__name__, static_folder="static", static_url_path="")


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/getIndexData")
def get_index_data():
    try:
        payload = wglh_client.get_data(force=False)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh")
def refresh():
    try:
        payload = wglh_client.get_data(force=True)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/healthz")
def healthz():
    return "ok", 200


def _background_loop():
    """后台每 12 小时尝试刷新（Render 休眠期间不执行，由 cron 唤醒）。"""
    while True:
        try:
            time.sleep(60 * 60 * 12)
            wglh_client.get_data(force=True)
        except Exception:
            pass


t = threading.Thread(target=_background_loop, daemon=True)
t.start()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
