# -*- coding: utf-8 -*-
"""一次性抓取：登录 wglh -> 抓取 5 只指数 -> 写出 data.json 到指定目录。

用法：python scripts/fetch_data.py <输出目录>
环境变量：WGLH_USERNAME / WGLH_PASSWORD
"""
import os
import sys
import json
import time

# 让脚本可以 import 仓库根目录下的 wglh_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wglh_client as w  # noqa: E402


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    username = os.environ.get("WGLH_USERNAME")
    password = os.environ.get("WGLH_PASSWORD")
    if not username or not password:
        raise SystemExit("缺少 WGLH_USERNAME / WGLH_PASSWORD 环境变量")

    session = w._new_session(username, password)
    data = w._scrape_once(session)

    now = time.time()
    payload = {
        "timestamp": now,
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "data": data,
        "fromCache": False,
    }

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("wrote %s, items=%d" % (out_path, len(data)))
    for d in data:
        print("  %-9s %s  price=%-10s pe=%-6s %s" %
              (d["name"], d["date"], d["price"], d["pe"], d["signal"]))


if __name__ == "__main__":
    main()
