#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QDII/跨境 ETF 场内溢价率监控脚本（纯 Python 标准库，零依赖）

功能：
1. 监控持仓 ETF 与自选 Top 名单的场内溢价率
2. 溢价率低于阈值（折价/低溢价，潜在买入机会）或高于阈值（高溢价，潜在卖出机会）时，
   通过 PushPlus 推送微信提醒
3. 同标的重复提醒有冷却时间，避免刷屏
4. 每个交易日收盘后推送一次当日全量快照

数据源（免费）：
- 腾讯行情 qt.gtimg.cn（单接口同时返回场内价格与基金净值）：
    [3]  最新价
    [78] 基金最新单位净值（QDII 为最近公布净值，与天天基金 dwjz 同源；
         同花顺溢价率即用该口径：溢价率 = (现价 - 净值) / 净值）
    [30] 行情时间戳 YYYYMMDDHHMMSS（用于交易日校验）

重要历史教训（勿回退）：
- 东财 push2his trends2 的 preSettlement 字段对 ETF 返回的是【昨日场内收盘价】，
  不是基金净值。用它对 QDII-ETF 算"溢价率"实际得到的是"当日涨跌幅"，完全失真。
- 腾讯 [78] 才是与同花顺/天天基金口径一致的最新单位净值，已交叉验证：
    513520 日经ETF  计算 +4.19% ≈ 同花顺 4.2%
    513310 中韩半导体 计算 +11.75% ≈ 同花顺 11.93%

用法：
  python3 monitor.py            # 正常监控（需环境变量 PUSHPLUS_TOKEN）
  python3 monitor.py --dry-run  # 本地试跑：只打印结果，不推送、不写状态

部署方式见 README.md（GitHub Actions 免费定时运行）。
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# ============================================================
# 配置区（想改监控标的 / 阈值，只改这里）
# ============================================================

# group: held=你的持仓 | watch_nq=Top纳指100 | watch_sp=Top标普500
# low: 溢价率低于此值提醒（折价/低溢价 = 潜在买入）
# high: 溢价率高于此值提醒（高溢价 = 潜在卖出）
# 阈值基于近一年（254 交易日）溢价率历史分布统计，按分群设定：
#   纳指系：买入线 2% (≈P15) / 卖出线 8% (≈P80)
#   标普系：买入线 1.5% (≈P20) / 卖出线 6% (≈P80)
#   日经：  买入线 1.5% (≈P20) / 卖出线 6% (≈P80)（分布与标普系一致）
#   中韩半导体：买入线 5% (≈P30) / 卖出线 20% (≈P88)
# 详见 results/etf_premium_history.json
ETFS = [
    # ---- 你的持仓（独立阈值） ----
    {"code": "513870", "market": "sh", "name": "纳指ETF富国",        "group": "held",     "low": 2.0,  "high": 8.0},
    {"code": "513650", "market": "sh", "name": "标普500ETF南方",      "group": "held",     "low": 1.5,  "high": 6.0},
    {"code": "513310", "market": "sh", "name": "中韩半导体ETF华泰柏瑞", "group": "held",    "low": 5.0,  "high": 20.0},
    {"code": "513520", "market": "sh", "name": "日经ETF华夏",          "group": "held",     "low": 1.5,  "high": 6.0},

    # ---- Top 5 纳斯达克100（按规模/流动性/成立时间/费率综合） ----
    {"code": "159941", "market": "sz", "name": "广发纳指100ETF",      "group": "watch_nq", "low": 2.0,  "high": 8.0},
    {"code": "513100", "market": "sh", "name": "国泰纳斯达克100",     "group": "watch_nq", "low": 2.0,  "high": 8.0},
    {"code": "513300", "market": "sh", "name": "华夏纳斯达克100",     "group": "watch_nq", "low": 2.0,  "high": 8.0},
    {"code": "159501", "market": "sz", "name": "嘉实纳斯达克100",     "group": "watch_nq", "low": 2.0,  "high": 8.0},
    {"code": "159632", "market": "sz", "name": "华安纳斯达克100",     "group": "watch_nq", "low": 2.0,  "high": 8.0},

    # ---- Top 标普500（场内纯跟踪标普500 只有 4 只，全部纳入） ----
    {"code": "513500", "market": "sh", "name": "博时标普500ETF",      "group": "watch_sp", "low": 1.5,  "high": 6.0},
    {"code": "159655", "market": "sz", "name": "华夏标普500ETF",      "group": "watch_sp", "low": 1.5,  "high": 6.0},
    {"code": "159612", "market": "sz", "name": "国泰标普500ETF",      "group": "watch_sp", "low": 1.5,  "high": 6.0},
]

COOLDOWN_MINUTES = 30            # 同一标的两次提醒的最短间隔（分钟）
NAV_RETRY_MINUTES = 30           # 净值拉取失败后的重试冷却（分钟）
SUMMARY_WINDOW = (1500, 1515)    # 每个交易日收盘后推送全量快照的时间窗口（北京时间 HHMM）
NAV_REFRESH_WINDOW = (1000, 1030)  # 每天强制刷新净值的时间窗（捕获上午新公布净值）
STATE_FILE = "state.json"        # 提醒状态文件（自动维护，勿手动改）

# ============================================================
# 以下为代码逻辑，一般无需修改
# ============================================================

CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TENCENT_HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
PUSHPLUS_URL = "https://www.pushplus.plus/send"

GROUP_LABELS = {
    "held": "🔵 我的持仓",
    "watch_nq": "🟣 Top纳指100",
    "watch_sp": "🟢 Top标普500",
}


def now_cn():
    return datetime.now(CN_TZ)


def in_trading_time(t):
    """北京时间 A 股交易时段（9:30-11:30, 13:00-15:00）"""
    if t.weekday() >= 5:
        return False
    hm = t.hour * 100 + t.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def http_get(url, headers, timeout=10, binary=False):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="ignore")


def http_post_json(url, payload, timeout=10):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def fetch_tencent_quotes(codes):
    """腾讯行情单接口批量获取：场内价格 + 基金最新单位净值。

    返回 {code: {name, price, nav, date}}，其中：
    - price: 字段[3] 最新成交价
    - nav:   字段[78] 基金最新单位净值（QDII 为最近公布净值，盘中基本固定，
             与同花顺/天天基金 dwjz 同口径，溢价率=(price-nav)/nav）
    - date:  字段[30] 行情日期 YYYY-MM-DD（用于非交易日校验）
    """
    symbol_list = ",".join(c["market"] + c["code"] for c in codes)
    url = "https://qt.gtimg.cn/q=" + symbol_list
    raw = http_get(url, TENCENT_HEADERS, binary=True).decode("gbk", errors="ignore")
    result = {}
    for line in raw.strip().splitlines():
        m = line.split("=", 1)
        if len(m) != 2 or '"' not in m[1]:
            continue
        fields = m[1].strip().strip('";').split("~")
        if len(fields) < 79:
            continue
        code, name = fields[2], fields[1]
        ts = fields[30]
        try:
            price = float(fields[3])
            nav = float(fields[78])
        except (ValueError, IndexError):
            continue
        date = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
        result[code] = {"name": name, "price": price, "nav": nav, "date": date}
    return result


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def should_refresh_nav(st, now, today):
    """净值每日缓存：当天已刷新过就直接复用；10:00-10:30 强制刷新一次（防盘中净值更新漏网）；
    拉取失败后进入 30 分钟重试冷却。"""
    if st.get("nav_date") != today:
        if st.get("nav_retry_at") and now.timestamp() < st["nav_retry_at"]:
            return False
        return True
    hm = now.hour * 100 + now.minute
    if NAV_REFRESH_WINDOW[0] <= hm <= NAV_REFRESH_WINDOW[1]:
        return True
    return False


def zone_of(premium, cfg):
    if premium < cfg["low"]:
        return "low"
    if premium > cfg["high"]:
        return "high"
    return "normal"


def fmt_premium(p):
    return f"{p:+.2f}%" if p is not None else "N/A"


def zone_desc(zone, cfg):
    if zone == "low":
        return f"⚠️ 低于 {cfg['low']:.0f}% 买入线"
    if zone == "high":
        return f"🔺 高于 {cfg['high']:.0f}% 卖出线"
    return "正常区间"


def pushplus_send(token, title, content):
    payload = {"token": token, "title": title, "content": content, "template": "markdown"}
    try:
        resp = http_post_json(PUSHPLUS_URL, payload)
        ok = isinstance(resp, dict) and resp.get("code") == 200
        print(f"[push] {'成功' if ok else '失败: ' + str(resp)[:200]}")
        return ok
    except Exception as e:
        print(f"[push] 异常: {e}")
        return False


def build_snapshot_table(rows):
    lines = ["| 代码 | 名称 | 分组 | 价格 | 净值 | 溢价率 | 状态 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["premium"] is None:
            continue
        lines.append(
            f"| {r['code']} | {r['name']} | {GROUP_LABELS.get(r['group'], r['group'])} | "
            f"{r['price']:.3f} | {r['nav']:.4f} | {fmt_premium(r['premium'])} | {zone_desc(r['zone'], r)} |"
        )
    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not token and not dry_run:
        print("[error] 缺少环境变量 PUSHPLUS_TOKEN")
        sys.exit(1)

    now = now_cn()
    today = now.strftime("%Y-%m-%d")
    hm_now = now.hour * 100 + now.minute
    state = load_state()

    # ---- 1. 取场内价格 + 净值（腾讯单接口） ----
    quotes = fetch_tencent_quotes(ETFS)
    if not quotes:
        print("[info] 无有效行情（可能接口异常），跳过本次")
        sys.exit(0)

    # 校验行情日期：非交易日腾讯返回上一交易日数据
    sample = next(iter(quotes.values()))
    if sample["date"] != today:
        print(f"[info] 行情日期 {sample['date']} 非今日 {today}，跳过（节假日/周末）")
        sys.exit(0)

    if not in_trading_time(now) and not (SUMMARY_WINDOW[0] <= hm_now <= SUMMARY_WINDOW[1]):
        print("[info] 非交易时段，跳过")
        sys.exit(0)

    # ---- 2. 净值每日缓存（[78] 盘中基本固定；失败用缓存兜底 + 重试冷却） ----
    navs = {}
    for cfg in ETFS:
        code = cfg["code"]
        st = state.setdefault(code, {})
        q = quotes.get(code)
        if q and q["nav"] and should_refresh_nav(st, now, today):
            st["nav"] = q["nav"]
            st["nav_date"] = today
            st.pop("nav_retry_at", None)
            navs[code] = q["nav"]
            print(f"[nav] {code} {cfg['name']}: {q['nav']}")
        else:
            # 未到刷新时点（用当天缓存）或本次请求缺值（用旧缓存 + 冷却）
            fallback = st.get("nav") or (q["nav"] if q else None)
            if q and q["nav"]:
                navs[code] = fallback
                print(f"[nav] {code} {cfg['name']}: {fallback} (缓存)")
            else:
                st["nav_retry_at"] = now.timestamp() + NAV_RETRY_MINUTES * 60
                navs[code] = fallback
                print(f"[warn] {code} 腾讯无净值，使用缓存 {fallback}（30 分钟后重试）")

    # ---- 3. 计算溢价率并汇总 ----
    rows = []
    for cfg in ETFS:
        code = cfg["code"]
        q = quotes.get(code)
        nav = navs.get(code)
        if not q or not nav:
            rows.append({**cfg, "price": None, "nav": nav, "premium": None, "zone": None})
            continue
        premium = (q["price"] - nav) / nav * 100
        rows.append({**cfg, "price": q["price"], "nav": nav, "premium": premium, "zone": zone_of(premium, cfg)})

    # ---- 4. 判定触发提醒（状态机 + 冷却） ----
    alerts = []
    first_run_all = all(cfg["code"] not in state or "zone" not in state.get(cfg["code"], {}) for cfg in ETFS)
    is_summary_time = SUMMARY_WINDOW[0] <= hm_now <= SUMMARY_WINDOW[1]
    summary_sent_today = state.get("_summary_date") == today

    for row in rows:
        code = row["code"]
        if row["premium"] is None:
            continue
        st = state.setdefault(code, {})
        cur_zone = row["zone"]
        if "zone" not in st:
            # 首次纳入监控：静默记录基线，不触发提醒
            st["zone"] = cur_zone
            st["last_alert"] = None
            continue
        if cur_zone != st.get("zone"):
            # 状态发生切换才提醒（含从预警区回到正常区）
            last = st.get("last_alert")
            cooldown_ok = (last is None) or (now.timestamp() - last >= COOLDOWN_MINUTES * 60)
            if cooldown_ok:
                alerts.append(row)
                st["last_alert"] = now.timestamp()
            st["zone"] = cur_zone

    # ---- 5. 组装消息 ----
    messages = []
    snapshot = build_snapshot_table(rows)

    if first_run_all:
        messages.append(
            "🛰️ 监控已启动，以下为当前溢价率快照\n（首次运行仅建立基线，之后进入预警区才会提醒）\n\n" + snapshot
        )
    elif alerts:
        lines = ["| 代码 | 名称 | 分组 | 溢价率 | 状态 |", "|---|---|---|---|---|"]
        for r in alerts:
            lines.append(
                f"| {r['code']} | {r['name']} | {GROUP_LABELS.get(r['group'], r['group'])} | "
                f"{fmt_premium(r['premium'])} | {zone_desc(r['zone'], r)} |"
            )
        messages.append("## 🚨 溢价率提醒\n\n" + "\n".join(lines))

    if is_summary_time and not summary_sent_today and not first_run_all:
        messages.append("## 📊 收盘溢价快照\n\n" + snapshot)
        state["_summary_date"] = today

    # ---- 6. 推送 + 保存状态 ----
    if dry_run:
        print("\n" + "=" * 60)
        print(f"[dry-run] {now.strftime('%Y-%m-%d %H:%M')} 模拟推送内容:")
        if messages:
            print("\n".join(messages))
        else:
            print("（无提醒、无快照，所有标的处于正常区间）")
        print("=" * 60)
        return  # dry-run 不保存状态，方便反复调试

    if messages:
        title = f"ETF溢价提醒 {now.strftime('%m-%d %H:%M')}"
        ok = pushplus_send(token, title, "\n\n".join(messages))
        if not ok:
            print("[error] 推送失败，本次状态不保存（下次运行重试）")
            sys.exit(1)

    save_state(state)
    print("[done] 状态已保存")


if __name__ == "__main__":
    main()
