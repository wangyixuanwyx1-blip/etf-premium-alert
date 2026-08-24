#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 历史溢价率分析：拉取近一年每日收盘价 + 每日净值，按日对齐计算溢价率分布。
数据源：
- 收盘价：腾讯历史日K接口 web.ifzq.gtimg.cn（不复权原始价格）
- 净值：天天基金 pingzhongdata（Data_netWorthTrend）
对齐口径：A股 T 日收盘价 / 天天基金 T 日净值条目（= T 日盘中可见的最新净值，与同花顺口径一致）
注意：之前用东财 preSettlement 字段是错误的（那是昨收价不是净值），已弃用。
       东财日K接口限流严重，改用腾讯。
输出：results/etf_premium_history.json（原始分布数据）+ 控制台汇总
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

ETFS = [
    {"code": "513870", "market": "sh", "name": "纳指ETF富国",        "group": "held"},
    {"code": "513650", "market": "sh", "name": "标普500ETF南方",      "group": "held"},
    {"code": "513310", "market": "sh", "name": "中韩半导体ETF华泰柏瑞", "group": "held"},
    {"code": "513520", "market": "sh", "name": "日经ETF华夏",          "group": "held"},
    {"code": "159941", "market": "sz", "name": "广发纳指100ETF",      "group": "watch_nq"},
    {"code": "513100", "market": "sh", "name": "国泰纳斯达克100",     "group": "watch_nq"},
    {"code": "513300", "market": "sh", "name": "华夏纳斯达克100",     "group": "watch_nq"},
    {"code": "159501", "market": "sz", "name": "嘉实纳斯达克100",     "group": "watch_nq"},
    {"code": "159632", "market": "sz", "name": "华安纳斯达克100",     "group": "watch_nq"},
    {"code": "513500", "market": "sh", "name": "博时标普500ETF",      "group": "watch_sp"},
    {"code": "159655", "market": "sz", "name": "华夏标普500ETF",      "group": "watch_sp"},
    {"code": "159612", "market": "sz", "name": "国泰标普500ETF",      "group": "watch_sp"},
]


def http_get(url, referer, retries=3, timeout=15):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            if attempt == retries - 1:
                print(f"[warn] {url[:60]} 最终失败: {e}", file=sys.stderr)
                return None
            time.sleep(4 * (attempt + 1) + random.random())


def fetch_kline_tencent(code, market):
    """腾讯历史日K，不复权原始价格。返回 {日期: 收盘价}"""
    sym = market + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,2025-08-01,2026-08-19,300,"
    raw = http_get(url, "https://gu.qq.com/")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        # 不复权时 key 是 "day"，前复权是 "qfqday"
        klines = (data.get("data", {}).get(sym, {}).get("day")
                  or data.get("data", {}).get(sym, {}).get("qfqday")
                  or [])
        out = {}
        for k in klines:
            # [日期, 开盘, 收盘, 最高, 最低, 成交量]
            out[k[0]] = float(k[2])
        return out
    except Exception as e:
        print(f"  [warn] K线解析失败: {e}", file=sys.stderr)
        return {}


def fetch_nav_history(code):
    """天天基金历史净值。返回 {日期: 单位净值}"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    raw = http_get(url, "http://fund.eastmoney.com/")
    if not raw:
        return {}
    m = re.search(r"var Data_netWorthTrend = (\[.*?\]);", raw)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(1))
        return {time.strftime("%Y-%m-%d", time.localtime(it["x"] / 1000)): it["y"] for it in arr}
    except Exception:
        return {}


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = int(k), int(k) + 1
    if c >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    results = []
    for i, cfg in enumerate(ETFS):
        code = cfg["code"]
        print(f"[{i+1}/{len(ETFS)}] {code} {cfg['name']} 拉取中...")
        closes = fetch_kline_tencent(code, cfg["market"])
        time.sleep(1.5 + random.random())
        navs = fetch_nav_history(code)
        if not closes or not navs:
            print(f"  [fail] 数据缺失 closes={len(closes)} navs={len(navs)}")
            time.sleep(2)
            continue

        # 按日期对齐：T 日收盘价 / T 日净值
        # 注意：QDII-ETF 的净值 T 日盘中可见的是 T-1 日（或更早）公布的净值
        # 天天基金 Data_netWorthTrend 的日期标记是"净值公布日"
        # 我们按相同日期对齐（T 日收盘价 / T 日净值条目）
        aligned = []
        for d, c in sorted(closes.items()):
            if d in navs and navs[d] > 0:
                prem = (c - navs[d]) / navs[d] * 100
                aligned.append((d, c, navs[d], prem))
        if not aligned:
            # 尝试用前一个交易日的净值（QDII 净值可能滞后一天）
            nav_dates = sorted(navs.keys())
            for d, c in sorted(closes.items()):
                # 找 d 之前最近的净值日
                prev_nav = None
                for nd in reversed(nav_dates):
                    if nd <= d:
                        prev_nav = navs[nd]
                        break
                if prev_nav and prev_nav > 0:
                    prem = (c - prev_nav) / prev_nav * 100
                    aligned.append((d, c, prev_nav, prem))
        if not aligned:
            print(f"  [fail] 无对齐数据")
            continue

        prem = sorted(v[3] for v in aligned)
        stats = {
            "code": code, "name": cfg["name"], "group": cfg["group"],
            "n": len(prem),
            "min": round(prem[0], 2), "max": round(prem[-1], 2),
            "mean": round(sum(prem) / len(prem), 2),
            "p5": round(percentile(prem, 5), 2), "p10": round(percentile(prem, 10), 2),
            "p25": round(percentile(prem, 25), 2), "p50": round(percentile(prem, 50), 2),
            "p75": round(percentile(prem, 75), 2), "p90": round(percentile(prem, 90), 2),
            "p95": round(percentile(prem, 95), 2),
        }
        results.append(stats)
        print(f"  n={len(prem)} min={stats['min']:.2f}% P5={stats['p5']:.2f}% "
              f"P10={stats['p10']:.2f}% P25={stats['p25']:.2f}% P50={stats['p50']:.2f}% "
              f"P75={stats['p75']:.2f}% P90={stats['p90']:.2f}% P95={stats['p95']:.2f}% "
              f"max={stats['max']:.2f}%")
        time.sleep(1.5 + random.random())

    os.makedirs("results", exist_ok=True)
    with open("results/etf_premium_history.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n[done] 已保存 results/etf_premium_history.json")


if __name__ == "__main__":
    main()
