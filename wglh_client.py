# -*- coding: utf-8 -*-
"""wglh 数据抓取模块：登录 -> 抓取 5 个指数 -> 解析点位与 PE -> 缓存。"""
import os
import re
import ast
import json
import time
import threading
import requests

BASE = "https://wglh.com"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache.json")
REFRESH_TTL = 60 * 60 * 14  # 缓存有效期 14 小时
LOCK = threading.Lock()

# 5 个指数：(基金名称, 页面代码, 买入线, 卖出线, 市场标签, 样式类型)
INDEX_CONFIG = [
    ("沪深300ETF", "sh000300", 10, 30, "A股", "a"),
    ("红利ETF", "sh000015", 7, 30, "A股", "a"),
    ("恒生ETF", "hkhsi", 9, 20, "港股", "hk"),
    ("标普500ETF", "sp500", 10, 25, "美股", "us"),
    ("纳指ETF", "ndx", 15, 30, "美股", "us"),
]


def _new_session(username, password):
    """用账号密码登录，返回已认证的 requests.Session。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    lp = s.get(BASE + "/auth/login/", timeout=20)
    m = re.search(r'csrfmiddlewaretoken" value="([^"]+)"', lp.text)
    if not m:
        raise RuntimeError("未获取到登录页 CSRF token")
    csrf = m.group(1)
    r = s.post(
        BASE + "/auth/dologbypwd/",
        data={"username": username, "password": password, "csrfmiddlewaretoken": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/auth/login/"},
        timeout=20,
    )
    out = r.json()
    if not out.get("success"):
        raise RuntimeError("登录失败：%s" % out.get("msg"))
    return s


def _parse_datas(html):
    """从页面内联脚本解析 datas 字典（Python 字面量）。"""
    m = re.search(r"var datas\s*=\s*(\{.*?\})\s*;\s*var positions", html, re.S)
    if not m:
        m = re.search(r"var datas\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        raise RuntimeError("页面中未找到 datas 数据")
    return ast.literal_eval(m.group(1))


def _signal_of(pe, buy_pe, sell_pe):
    if pe < buy_pe:
        return "buy"
    if pe > sell_pe:
        return "sell"
    return "hold"


def _scrape_once(session):
    """登录后依次抓取全部指数，返回结果列表。"""
    results = []
    for name, code, buy, sell, mkt, mkt_cls in INDEX_CONFIG:
        url = "%s/chinaindicespe/%s/" % (BASE, code)
        r = session.get(url, timeout=25)
        if "var datas" not in r.text:
            raise RuntimeError("%s 页面无数据（可能会话过期）" % name)
        datas = _parse_datas(r.text)
        dates = sorted(datas.keys())
        last = dates[-1]
        e = datas[last]
        pe = float(e["pe"])
        price = float(e["price"])
        results.append({
            "id": code,
            "name": name,
            "date": last.replace(".", "-"),
            "price": price,
            "pe": pe,
            "buyPe": buy,
            "sellPe": sell,
            "mkt": mkt,
            "mktCls": mkt_cls,
            "url": url,
            "signal": _signal_of(pe, buy, sell),
        })
    return results


def _read_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(payload):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def get_data(force=False):
    """获取数据：缓存有效直接返回，否则登录抓取；抓取失败则返回旧缓存。"""
    with LOCK:
        cached = _read_cache()
        now = time.time()
        fresh = cached and (now - cached.get("timestamp", 0) < REFRESH_TTL)
        if fresh and not force:
            cached["fromCache"] = True
            return cached

        username = os.environ.get("WGLH_USERNAME")
        password = os.environ.get("WGLH_PASSWORD")
        if not username or not password:
            if cached:
                cached["fromCache"] = True
                cached["warning"] = "未配置账号环境变量，返回旧缓存"
                return cached
            raise RuntimeError("缺少 WGLH_USERNAME / WGLH_PASSWORD 环境变量")

        try:
            session = _new_session(username, password)
            results = _scrape_once(session)
            payload = {
                "timestamp": now,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "data": results,
                "fromCache": False,
            }
            _write_cache(payload)
            return payload
        except Exception as e:
            if cached:
                cached["fromCache"] = True
                cached["warning"] = "本次抓取失败，返回旧缓存：%s" % e
                return cached
            raise
