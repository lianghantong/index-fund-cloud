# -*- coding: utf-8 -*-
"""
A股股票筛选器 v2
================
数据源：AKShare stock_financial_abstract（新浪财经）
粗筛：连续五年 ROE>20%、净利润现金含量>80%、毛利率>40%
细筛：五年平均净利润现金含量>=100%、资产负债率<60%、分红比率>=25%
买卖：深证A股PE<20 且 个股TTM PE<15 且 动态股息率>十年国债 → 买入；
      个股PE>50 或 动态股息率<十年国债/3 → 卖出；其余观望。
"""
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

# ── 配置 ──────────────────────────────────────────────
YEARS = 5
COARSE_ROE = 20.0
COARSE_CASH = 80.0
COARSE_GROSS = 40.0
FINE_CASH_AVG = 100.0
FINE_DEBT = 60.0
FINE_DIV_PAYOUT = 25.0
BUY_MARKET_PE = 20.0
BUY_PE = 15.0
SELL_PE = 50.0
MAX_WORKERS = 5
PER_STOCK_TIMEOUT = 30
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "stock_data.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        f = float(v)
        if pd.isna(f) or f == float('inf') or f == float('-inf'):
            return None
        return f
    except (ValueError, TypeError):
        return None


# ── 1. 股票列表 ───────────────────────────────────────
def get_stock_list():
    log("获取A股股票列表...")
    df = ak.stock_info_a_code_name()
    stocks = []
    for _, row in df.iterrows():
        code = str(row["code"]).zfill(6)
        name = str(row["name"]).strip()
        if code.startswith(("8", "4", "9")):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        if not code.startswith(("60", "68", "00", "30")):
            continue
        stocks.append({"code": code, "name": name})
    log(f"共 {len(stocks)} 只A股")
    return stocks


# ── 2. 实时价格 ───────────────────────────────────────
def get_prices():
    log("获取A股实时行情...")
    try:
        df = ak.stock_zh_a_spot()
        price_map = {}
        for _, row in df.iterrows():
            raw = str(row["代码"])
            code = raw[2:] if len(raw) > 6 else raw
            price = safe_float(row["最新价"])
            if price and price > 0:
                price_map[code] = price
        log(f"获取到 {len(price_map)} 只股票价格")
        return price_map
    except Exception as e:
        log(f"获取行情失败: {e}")
        return {}


# ── 3. 从 abstract 提取年度财务数据 ───────────────────
def extract_annual_from_abstract(df):
    """
    从 stock_financial_abstract 结果中提取最近5年年报数据。
    返回 dict: {year: {roe, gross, debt, ocf, net_profit, eps}}
    """
    # 年报列：8位数字且以1231结尾
    annual_cols = []
    for c in df.columns:
        cs = str(c)
        if len(cs) == 8 and cs.endswith("1231") and cs[:4].isdigit():
            annual_cols.append(cs)
    # 列按从新到旧排列，取前 YEARS+1 个（多取一年用于TTM）
    annual_cols = sorted(annual_cols, reverse=True)[:YEARS + 1]

    if len(annual_cols) < YEARS:
        return None, None

    def get_val(indicator_name, col):
        rows = df[df['指标'] == indicator_name]
        if rows.empty:
            return None
        return safe_float(rows.iloc[0].get(col))

    result = {}
    for col in annual_cols:
        year = int(col[:4])
        roe = get_val('净资产收益率(ROE)', col)
        gross = get_val('毛利率', col)
        debt = get_val('资产负债率', col)
        ocf = get_val('经营现金流量净额', col)
        net_profit = get_val('归母净利润', col)
        eps = get_val('基本每股收益', col)

        # 净利润现金含量 = 经营现金流 / 归母净利润 * 100
        cash_content = None
        if ocf is not None and net_profit is not None and net_profit != 0:
            cash_content = round(ocf / net_profit * 100, 2)

        result[year] = {
            'roe': roe,
            'gross': gross,
            'debt': debt,
            'ocf': ocf,
            'net_profit': net_profit,
            'eps': eps,
            'cash_content': cash_content,
        }

    # 同时提取季报EPS用于TTM计算
    quarterly_eps = {}
    for c in df.columns:
        cs = str(c)
        if len(cs) == 8 and cs.isdigit():
            month = cs[4:6]
            if month in ('03', '06', '09', '12'):
                v = get_val('基本每股收益', cs)
                if v is not None:
                    quarterly_eps[cs] = v

    return result, quarterly_eps


# ── 4. 计算年度分红比率 ───────────────────────────────
def calc_dividend_payout(code, annual_data):
    """
    计算每年分红比率 = 当年每股分红 / 当年EPS * 100。
    返回 {year: payout_ratio}
    """
    try:
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
        if df is None or df.empty:
            return {}
        df = df[df["进度"] == "实施"].copy()
        if df.empty:
            return {}
        df["除权除息日"] = pd.to_datetime(df["除权除息日"], errors="coerce")
        df = df.dropna(subset=["除权除息日"])

        payout = {}
        for year in annual_data:
            year_divs = df[df["除权除息日"].dt.year == year]
            if year_divs.empty:
                payout[year] = 0.0
                continue
            total_per_10 = year_divs["派息"].sum()
            div_per_share = safe_float(total_per_10) / 10.0 if safe_float(total_per_10) else 0
            eps = annual_data[year].get('eps')
            if eps and eps > 0:
                payout[year] = round(div_per_share / eps * 100, 2)
            else:
                payout[year] = None
        return payout
    except Exception:
        return {}


# ── 5. 单只股票筛选 ───────────────────────────────────
def fetch_and_screen(stock):
    code = stock["code"]
    name = stock["name"]
    try:
        df = ak.stock_financial_abstract(symbol=code)
        if df is None or df.empty:
            return None

        annual_data, quarterly_eps = extract_annual_from_abstract(df)
        if not annual_data or len(annual_data) < YEARS:
            return None

        # 取最近5年（排除可能多取的第6年）
        years = sorted(annual_data.keys(), reverse=True)[:YEARS]

        roe_vals = [annual_data[y]['roe'] for y in years]
        gross_vals = [annual_data[y]['gross'] for y in years]
        cash_vals = [annual_data[y]['cash_content'] for y in years]
        debt_vals = [annual_data[y]['debt'] for y in years]

        # 粗筛
        if any(v is None or v <= COARSE_ROE for v in roe_vals):
            return None
        if any(v is None or v <= COARSE_GROSS for v in gross_vals):
            return None
        if any(v is None or v <= COARSE_CASH for v in cash_vals):
            return None

        # 细筛：现金含量五年平均
        valid_cash = [v for v in cash_vals if v is not None]
        if not valid_cash or sum(valid_cash) / len(valid_cash) < FINE_CASH_AVG:
            return None
        # 资产负债率
        if any(v is None or v >= FINE_DEBT for v in debt_vals):
            return None

        # 分红比率（需要额外接口）
        div_payout = calc_dividend_payout(code, {y: annual_data[y] for y in years})
        valid_div = [div_payout.get(y) for y in years if div_payout.get(y) is not None]
        if not valid_div or min(valid_div) < FINE_DIV_PAYOUT:
            return None

        return {
            "code": code,
            "name": name,
            "exchange": "SH" if code.startswith(("60", "68")) else "SZ",
            "years": years,
            "roe5y": [round(v, 2) if v else None for v in roe_vals],
            "grossMargin5y": [round(v, 2) if v else None for v in gross_vals],
            "cashContent5y": [round(v, 2) if v else None for v in cash_vals],
            "debtRatio5y": [round(v, 2) if v else None for v in debt_vals],
            "dividendPayout5y": [round(div_payout.get(y), 2) if div_payout.get(y) is not None else None for y in years],
            "_quarterly_eps": quarterly_eps,
        }
    except Exception:
        return None


# ── 6. TTM PE 计算 ────────────────────────────────────
def calc_ttm_pe(quarterly_eps, price):
    """从累计EPS序列计算TTM PE。"""
    if not quarterly_eps or not price or price <= 0:
        return None
    # 按日期从新到旧排序
    dates = sorted(quarterly_eps.keys(), reverse=True)
    if not dates:
        return None
    latest = dates[0]
    latest_eps = quarterly_eps[latest]
    latest_month = latest[4:6]
    latest_year = int(latest[:4])

    if latest_month == "12":
        ttm_eps = latest_eps
    else:
        # 找上年同期和上年年报
        prev_same = f"{latest_year - 1}{latest_month}"
        prev_annual = f"{latest_year - 1}1231"
        if prev_same in quarterly_eps and prev_annual in quarterly_eps:
            ttm_eps = quarterly_eps[prev_annual] - quarterly_eps[prev_same] + latest_eps
        else:
            ttm_eps = latest_eps

    if ttm_eps is None or ttm_eps <= 0:
        return None
    return round(price / ttm_eps, 2)


# ── 7. 动态股息率 ─────────────────────────────────────
def calc_dividend_yield(code, price):
    if not price or price <= 0:
        return None
    try:
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
        if df is None or df.empty:
            return 0.0
        df = df[df["进度"] == "实施"].copy()
        if df.empty:
            return 0.0
        df["除权除息日"] = pd.to_datetime(df["除权除息日"], errors="coerce")
        cutoff = datetime.now() - timedelta(days=365)
        recent = df[df["除权除息日"] >= cutoff]
        if recent.empty:
            return 0.0
        total_per_10 = recent["派息"].sum()
        div_per_share = safe_float(total_per_10) / 10.0 if safe_float(total_per_10) else 0
        return round(div_per_share / price * 100, 3)
    except Exception:
        return None


# ── 8. 十年国债 ───────────────────────────────────────
def get_treasury_10y():
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        df = ak.bond_china_yield(start_date=start, end_date=end)
        gov = df[df["曲线名称"] == "中债国债收益率曲线"]
        if gov.empty:
            return None
        latest = gov.sort_values("日期").iloc[-1]
        return safe_float(latest.get("10年"))
    except Exception as e:
        log(f"国债收益率获取失败: {e}")
        return None


# ── 9. 深证A股PE ──────────────────────────────────────
def get_shenzhen_pe():
    # 优先乐咕市场PE
    try:
        df = ak.stock_market_pe_lg(symbol="深证A股")
        if df is not None and not df.empty:
            latest = df.sort_values("日期").iloc[-1]
            pe = safe_float(latest.get("市盈率"))
            if pe and 5 < pe < 80:
                log(f"深证A股PE(乐咕): {pe}")
                return pe
    except Exception:
        pass
    # 备选：深证100滚动PE
    try:
        df = ak.stock_index_pe_lg(symbol="深证100")
        if df is not None and not df.empty:
            latest = df.sort_values("日期").iloc[-1]
            pe = safe_float(latest.get("滚动市盈率"))
            if pe:
                log(f"深证A股PE(深证100代理): {pe}")
                return pe
    except Exception:
        pass
    log("深证A股PE获取失败，使用默认值25")
    return 25.0


# ── 10. 买卖判断 ──────────────────────────────────────
def judge_signal(info, market_pe, treasury_yield):
    pe = info.get("peTTM")
    dy = info.get("dividendYield")

    if pe is None:
        return "hold", "PE数据缺失，暂观望"

    if pe > SELL_PE:
        return "sell", f"PE {pe} > {SELL_PE}，高估卖出"
    if dy is not None and treasury_yield and dy < treasury_yield / 3:
        return "sell", f"股息率 {dy}% < 国债1/3({treasury_yield/3:.2f}%)"

    if market_pe < BUY_MARKET_PE and pe < BUY_PE:
        if dy is not None and treasury_yield and dy > treasury_yield:
            return "buy", f"深证PE {market_pe:.1f}<20, 个股PE {pe}<15, 股息率 {dy}%>国债{treasury_yield:.2f}%"
        elif dy is None:
            return "hold", "股息率数据缺失，暂观望"

    return "hold", f"PE {pe}，估值合理区间"


# ── 主流程 ────────────────────────────────────────────
def main():
    log("=" * 50)
    log("A股股票筛选开始")
    log("=" * 50)
    log(f"akshare version: {getattr(ak, '__version__', 'unknown')}")

    try:
        stocks = get_stock_list()
    except Exception as e:
        log(f"获取股票列表失败: {e}")
        stocks = []

    try:
        price_map = get_prices()
    except Exception as e:
        log(f"获取行情失败: {e}")
        price_map = {}

    log(f"并发抓取财务数据 (workers={MAX_WORKERS})...")
    candidates = []
    failed = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_and_screen, s): s for s in stocks}
        done = 0
        for fut in as_completed(futures):
            done += 1
            if done % 500 == 0:
                elapsed = time.time() - t0
                log(f"  进度 {done}/{len(stocks)} ({elapsed:.0f}s), 通过: {len(candidates)}")
            try:
                result = fut.result(timeout=PER_STOCK_TIMEOUT + 10)
                if result:
                    candidates.append(result)
            except Exception:
                failed += 1

    elapsed = time.time() - t0
    log(f"财务筛选完成: {elapsed:.0f}s, 通过: {len(candidates)}, 失败/跳过: {failed}")

    market_pe = get_shenzhen_pe()
    treasury_yield = get_treasury_10y()
    log(f"市场: 深证A股PE={market_pe}, 十年国债={treasury_yield}%")

    if not candidates:
        output = {
            "updateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market": {"shenzhenPE": market_pe, "treasury10Y": treasury_yield},
            "candidates": [],
            "summary": {"totalScreened": len(stocks), "passed": 0, "buy": 0, "hold": 0, "sell": 0},
        }
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        log("无候选股，已输出空结果")
        return

    log(f"计算 {len(candidates)} 只候选股PE/股息率...")
    final = []
    for c in candidates:
        code = c["code"]
        price = price_map.get(code)
        pe = calc_ttm_pe(c.pop("_quarterly_eps", {}), price)
        dy = calc_dividend_yield(code, price)
        c["price"] = price
        c["peTTM"] = pe
        c["dividendYield"] = dy
        signal, reason = judge_signal(c, market_pe, treasury_yield)
        c["signal"] = signal
        c["signalReason"] = reason
        final.append(c)

    order = {"buy": 0, "sell": 1, "hold": 2}
    final.sort(key=lambda x: (order.get(x["signal"], 3), x.get("peTTM") or 999))

    buy_n = sum(1 for c in final if c["signal"] == "buy")
    sell_n = sum(1 for c in final if c["signal"] == "sell")
    hold_n = sum(1 for c in final if c["signal"] == "hold")

    output = {
        "updateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": {
            "shenzhenPE": round(market_pe, 2) if market_pe else None,
            "treasury10Y": round(treasury_yield, 4) if treasury_yield else None,
        },
        "criteria": {
            "coarse": f"连续{YEARS}年 ROE>{COARSE_ROE}% 且 净利润现金含量>{COARSE_CASH}% 且 毛利率>{COARSE_GROSS}%",
            "fine": f"{YEARS}年平均现金含量>={FINE_CASH_AVG}% 且 资产负债率<{FINE_DEBT}% 且 分红比率>={FINE_DIV_PAYOUT}%",
            "buy": f"深证A股PE<{BUY_MARKET_PE} 且 个股TTM PE<{BUY_PE} 且 动态股息率>十年国债",
            "sell": f"个股PE>{SELL_PE} 或 动态股息率<十年国债/3",
        },
        "candidates": final,
        "summary": {
            "totalScreened": len(stocks),
            "passedCoarseFine": len(final),
            "buy": buy_n,
            "hold": hold_n,
            "sell": sell_n,
        },
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"完成: 买入{buy_n} 观望{hold_n} 卖出{sell_n}")
    for c in final[:15]:
        log(f"  {c['code']} {c['name']}: PE={c['peTTM']}, 股息={c['dividendYield']}%, {c['signal']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"致命错误: {e}")
        traceback.print_exc()
        # 即使失败也输出空结果，保证网站不报错
        try:
            os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "updateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "market": {"shenzhenPE": None, "treasury10Y": None},
                    "criteria": {
                        "coarse": "连续5年 ROE>20% 且 净利润现金含量>80% 且 毛利率>40%",
                        "fine": "5年平均现金含量>=100% 且 资产负债率<60% 且 分红比率>=25%",
                        "buy": "深证A股PE<20 且 个股TTM PE<15 且 动态股息率>十年国债",
                        "sell": "个股PE>50 或 动态股息率<十年国债/3",
                    },
                    "candidates": [],
                    "summary": {"totalScreened": 0, "passedCoarseFine": 0, "buy": 0, "hold": 0, "sell": 0, "error": str(e)},
                }, f, ensure_ascii=False, indent=2)
            log("已输出空结果文件")
        except Exception as e2:
            log(f"输出空结果也失败: {e2}")
        sys.exit(0)
