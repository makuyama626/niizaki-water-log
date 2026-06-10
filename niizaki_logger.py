#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新崎川 水位 ＋ 周辺雨量ロガー（神奈川県 雨量水位情報）

取得対象:
  - 新崎橋[Niizaki Bridge] / 新崎川 ………… 水位(m)・10分間隔
  - 南郷山[Mt. Nango] (湯河原町南郷山・幕山公園直上) … 雨量(mm)・10分間隔
  - 白銀山[Mt. Shirogane] (湯河原源頭側・約990m) ……… 雨量(mm)・10分間隔
  - 浅間山[Mt. Sengen] (幕山隣接・※欠測のことあり) …… 雨量(mm)・10分間隔

出力（data/ 配下）:
  - niizaki_waterlevel.csv … 水位ログ
  - rainfall.csv ………………… 雨量ログ（縦持ち・観測所ごと）
  - combined_10min.csv ……… 同時刻で結合した比較用ワイド表（水位＋各点10分雨量）

使い方:
  python niizaki_logger.py          # 取得して各CSVに追記
  python niizaki_logger.py --test   # 解析ロジックの自己テスト（ネット接続不要）

基準水位（新崎橋）: 水防団待機0.80m / 氾濫注意1.15m / 避難判断1.20m / 氾濫危険1.65m
"""

import csv
import os
import re
import sys
from datetime import datetime, timedelta, timezone, date

JST = timezone(timedelta(hours=9), name="JST")
BASE = "https://www.pref.kanagawa.jp/sys/suibou/web_general/suibou_joho/html"

# ── 観測所定義 ───────────────────────────────────────────────
# col = 比較用ワイド表(combined)での列名
STATIONS = [
    {"key": "新崎橋", "kind": "water", "col": "level_m",
     "url": f"{BASE}/stage/10/p10202_18_3585_4_808.html"},
    {"key": "南郷山", "kind": "rain", "col": "南郷山_10分mm",
     "url": f"{BASE}/rain/10/p10102_18_3585_1_815.html"},
    {"key": "白銀山", "kind": "rain", "col": "白銀山_10分mm",
     "url": f"{BASE}/rain/10/p10102_18_3585_1_812.html"},
    {"key": "浅間山", "kind": "rain", "col": "浅間山_10分mm",
     "url": f"{BASE}/rain/10/p10102_18_3585_1_817.html"},
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WATER_CSV = os.path.join(DATA_DIR, "niizaki_waterlevel.csv")
RAIN_CSV = os.path.join(DATA_DIR, "rainfall.csv")
COMBINED_CSV = os.path.join(DATA_DIR, "combined_10min.csv")
EVENT_CSV = os.path.join(DATA_DIR, "event_log.csv")

# イベント検出パラメータ
EVENT_START_THRESHOLD = 0.15   # この水位を超えたらイベント開始(m)
NORMAL_THRESHOLD = 0.10        # この水位以下が3コマ連続で「回復」と判定(m)
NORMAL_CONSECUTIVE = 3         # 回復判定に必要な連続コマ数

EVENT_FIELDS = [
    "event_id",          # YYYYMMDD_HHMM (イベント開始時刻)
    "event_start",       # イベント開始時刻(ISO)
    "peak_level_m",      # ピーク水位(m)
    "peak_time",         # ピーク時刻(ISO)
    "recovery_time",     # 回復時刻(ISO) ※未回復は空欄
    "hours_to_recover",  # ピーク→回復の時間数 ※未回復は空欄
    "total_rain_mm",     # イベント期間の南郷山総雨量(mm)
    "antecedent_24h_mm", # イベント開始前24h南郷山雨量(mm)
    "antecedent_72h_mm", # イベント開始前72h南郷山雨量(mm)
    "month",             # 月（季節把握用）
    "status",            # "closed" or "in_progress"
]

WATER_FIELDS = ["timestamp", "level_m", "trend", "status", "fetched_at"]
RAIN_FIELDS = ["timestamp", "station", "rain_10min_mm", "rain_cum_mm", "status", "fetched_at"]

TREND_CHARS = {"\u2192", "\u2191", "\u2193"}      # → ↑ ↓
MISSING_TOKENS = {"*", "**", "--", "", "\u2015", "\u2014"}


# ── 値の解析 ───────────────────────────────────────────────
def parse_water_value(raw):
    """'0.07→' -> (0.07,'→','ok') / '**' -> (None,'','missing')"""
    s = (raw or "").strip()
    trend = ""
    if s and s[-1] in TREND_CHARS:
        trend, s = s[-1], s[:-1].strip()
    if s in MISSING_TOKENS or not re.search(r"\d", s):
        return None, trend, "missing"
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return (float(m.group(0)), trend, "ok") if m else (None, trend, "missing")


def parse_rain_value(raw):
    """'0'->(0,'ok') / '1'->(1,'ok') / '**'->(None,'missing')。整数mm。"""
    s = (raw or "").strip()
    if s in MISSING_TOKENS or not re.search(r"\d", s):
        return None, "missing"
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None, "missing"
    v = float(m.group(0))
    return (int(v) if v == int(v) else v), "ok"


def infer_year(month, day, now_jst):
    try:
        cand = date(now_jst.year, month, day)
    except ValueError:
        return now_jst.year - 1
    diff = (cand - now_jst.date()).days
    if diff > 2:
        return now_jst.year - 1
    if diff < -300:
        return now_jst.year + 1
    return now_jst.year


def assemble_timestamps(time_cells, now_jst):
    """時刻セル列（古い順、先頭のみ日付つき）-> ISO文字列のリスト。
    時刻が前行より戻ったら翌日へ繰り上げ（年末年始もtimedeltaで吸収）。"""
    out = []
    cur = None
    for t in time_cells:
        t = (t or "").strip()
        dm = re.match(r"(?:(\d{1,2})/(\d{1,2})\s+)?(\d{1,2}):(\d{2})", t)
        if not dm:
            out.append(None)
            continue
        mon, day, hh, mm = dm.group(1), dm.group(2), int(dm.group(3)), int(dm.group(4))
        if mon and day:
            y = infer_year(int(mon), int(day), now_jst)
            cur = datetime(y, int(mon), int(day), hh, mm, tzinfo=JST)
        elif cur is None:
            cur = datetime(now_jst.year, now_jst.month, now_jst.day, hh, mm, tzinfo=JST)
        else:
            base = cur + timedelta(days=1) if (hh * 60 + mm) < (cur.hour * 60 + cur.minute) else cur
            cur = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        out.append(cur.isoformat())
    return out


# ── HTML から表を抽出 ─────────────────────────────────────
def extract_rows(html):
    """観測時刻を含む表を見つけ、時刻行のセル配列を返す（古い順）。"""
    rows = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        target = None
        for tbl in soup.find_all("table"):
            if "観測時刻" in tbl.get_text():
                target = tbl
                break
        if target is not None:
            for tr in target.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if cells and re.match(r"(?:\d{1,2}/\d{1,2}\s+)?\d{1,2}:\d{2}$", cells[0].strip()):
                    rows.append(cells)
    except Exception as e:
        print(f"[warn] table parse skipped: {e}", file=sys.stderr)
    if not rows:
        # フォールバック: タグ除去テキストから「[日付] 時刻 値...」を拾う
        text = re.sub(r"<[^>]+>", " ", html)
        for m in re.finditer(
            r"(?:(\d{1,2}/\d{1,2})\s+)?(\d{1,2}:\d{2})\s+"
            r"(-?\d+(?:\.\d+)?[\u2192\u2191\u2193]?|\*{1,2}|--)"
            r"(?:\s+(-?\d+(?:\.\d+)?|\*{1,2}|--))?", text):
            tcell = (f"{m.group(1)} " if m.group(1) else "") + m.group(2)
            row = [tcell, m.group(3)]
            if m.group(4) is not None:
                row.append(m.group(4))
            rows.append(row)
    return rows


def parse_station(html, kind, now_jst):
    rows = extract_rows(html)
    times = assemble_timestamps([r[0] for r in rows], now_jst)
    recs = []
    for ts, r in zip(times, rows):
        if ts is None:
            continue
        if kind == "water":
            level, trend, status = parse_water_value(r[1] if len(r) > 1 else "")
            recs.append({"timestamp": ts, "level_m": "" if level is None else f"{level:.2f}",
                         "trend": trend, "status": status})
        else:  # rain: [time, 10min, cum]
            v10, st10 = parse_rain_value(r[1] if len(r) > 1 else "")
            vcum, _ = parse_rain_value(r[2] if len(r) > 2 else "")
            recs.append({"timestamp": ts,
                         "rain_10min_mm": "" if v10 is None else f"{v10}",
                         "rain_cum_mm": "" if vcum is None else f"{vcum}",
                         "status": st10})
    return recs


# ── CSV 入出力 ─────────────────────────────────────────────
def load_csv(path, key_fields):
    rows = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows[tuple(r[k] for k in key_fields)] = r
    return rows


def save_csv(path, fields, rows, sort_key):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows.values(), key=sort_key):
            w.writerow({k: r.get(k, "") for k in fields})


def merge_water(existing, recs, fetched_at):
    added = 0
    for rec in recs:
        k = (rec["timestamp"],)
        rec = dict(rec, fetched_at=fetched_at)
        cur = existing.get(k)
        if cur is None:
            existing[k] = rec; added += 1
        elif cur.get("status") == "missing" and rec.get("status") == "ok":
            existing[k] = rec
    return added


def merge_rain(existing, station, recs, fetched_at):
    added = 0
    for rec in recs:
        k = (rec["timestamp"], station)
        rec = dict(rec, station=station, fetched_at=fetched_at)
        cur = existing.get(k)
        if cur is None:
            existing[k] = rec; added += 1
        elif cur.get("status") == "missing" and rec.get("status") == "ok":
            existing[k] = rec
    return added


def build_combined(water_rows, rain_rows):
    """水位＋各観測所の10分雨量を同時刻で結合したワイド表を作る。"""
    rain_cols = [s["col"] for s in STATIONS if s["kind"] == "rain"]
    key_by_station = {s["key"]: s["col"] for s in STATIONS if s["kind"] == "rain"}
    table = {}  # ts -> {col: val}
    for (ts,), r in water_rows.items():
        table.setdefault(ts, {})["level_m"] = r.get("level_m", "")
    for (ts, station), r in rain_rows.items():
        col = key_by_station.get(station)
        if col:
            table.setdefault(ts, {})[col] = r.get("rain_10min_mm", "")
    fields = ["timestamp", "level_m"] + rain_cols
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(COMBINED_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for ts in sorted(table):
            row = {"timestamp": ts}
            row.update(table[ts])
            w.writerow({k: row.get(k, "") for k in fields})
    return fields


def update_event_log():
    """combined_10min.csv を読み込み、イベントを検出して event_log.csv を更新する。"""
    if not os.path.exists(COMBINED_CSV):
        return

    # combined を時系列順に読む
    rows = []
    with open(COMBINED_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    rows.sort(key=lambda r: r["timestamp"])

    def get_level(r):
        try:
            return float(r["level_m"]) if r["level_m"] else None
        except ValueError:
            return None

    def get_rain(r):
        try:
            return float(r["南郷山_10分mm"]) if r["南郷山_10分mm"] else 0.0
        except ValueError:
            return 0.0

    def rain_sum_before(ts_iso, hours):
        cutoff = datetime.fromisoformat(ts_iso) - timedelta(hours=hours)
        total = 0.0
        for r in rows:
            t = datetime.fromisoformat(r["timestamp"])
            if cutoff <= t < datetime.fromisoformat(ts_iso):
                total += get_rain(r)
        return round(total, 1)

    # 既存イベントログを読む
    existing = {}
    if os.path.exists(EVENT_CSV):
        with open(EVENT_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                existing[r["event_id"]] = r

    # イベント検出ループ
    in_event = False
    event = {}
    normal_streak = 0

    for i, r in enumerate(rows):
        level = get_level(r)
        if level is None:
            continue

        if not in_event:
            if level > EVENT_START_THRESHOLD:
                in_event = True
                normal_streak = 0
                event = {
                    "event_start": r["timestamp"],
                    "peak_level_m": level,
                    "peak_time": r["timestamp"],
                    "event_rain": get_rain(r),
                    "status": "in_progress",
                }
        else:
            # ピーク更新
            if level > event["peak_level_m"]:
                event["peak_level_m"] = level
                event["peak_time"] = r["timestamp"]
            event["event_rain"] = event.get("event_rain", 0) + get_rain(r)

            # 回復判定
            if level <= NORMAL_THRESHOLD:
                normal_streak += 1
            else:
                normal_streak = 0

            if normal_streak >= NORMAL_CONSECUTIVE:
                # 回復確定 → イベント確定
                peak_dt = datetime.fromisoformat(event["peak_time"])
                rec_dt = datetime.fromisoformat(r["timestamp"])
                hours_rec = round((rec_dt - peak_dt).total_seconds() / 3600, 1)
                event_id = datetime.fromisoformat(event["event_start"]).strftime("%Y%m%d_%H%M")
                rec = {
                    "event_id": event_id,
                    "event_start": event["event_start"],
                    "peak_level_m": f"{event['peak_level_m']:.2f}",
                    "peak_time": event["peak_time"],
                    "recovery_time": r["timestamp"],
                    "hours_to_recover": str(hours_rec),
                    "total_rain_mm": str(event.get("event_rain", "")),
                    "antecedent_24h_mm": str(rain_sum_before(event["event_start"], 24)),
                    "antecedent_72h_mm": str(rain_sum_before(event["event_start"], 72)),
                    "month": str(datetime.fromisoformat(event["event_start"]).month),
                    "status": "closed",
                }
                existing[event_id] = rec
                in_event = False
                event = {}
                normal_streak = 0

    # イベント未回復（データ末尾まで水位が高いまま）
    if in_event and event.get("event_start"):
        event_id = datetime.fromisoformat(event["event_start"]).strftime("%Y%m%d_%H%M")
        # すでにclosedなら上書きしない
        if existing.get(event_id, {}).get("status") != "closed":
            existing[event_id] = {
                "event_id": event_id,
                "event_start": event["event_start"],
                "peak_level_m": f"{event['peak_level_m']:.2f}",
                "peak_time": event["peak_time"],
                "recovery_time": "",
                "hours_to_recover": "",
                "total_rain_mm": str(event.get("event_rain", "")),
                "antecedent_24h_mm": str(rain_sum_before(event["event_start"], 24)),
                "antecedent_72h_mm": str(rain_sum_before(event["event_start"], 72)),
                "month": str(datetime.fromisoformat(event["event_start"]).month),
                "status": "in_progress",
            }

    if not existing:
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(EVENT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        w.writeheader()
        for rec in sorted(existing.values(), key=lambda r: r["event_id"]):
            w.writerow({k: rec.get(k, "") for k in EVENT_FIELDS})

    closed = sum(1 for r in existing.values() if r["status"] == "closed")
    inprog = sum(1 for r in existing.values() if r["status"] == "in_progress")
    print(f"event_log: {closed}件確定 / {inprog}件進行中")


def run():
    import requests
    now = datetime.now(JST)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KanagawaWaterRainLogger/1.0)"}
    water = load_csv(WATER_CSV, ["timestamp"])
    rain = load_csv(RAIN_CSV, ["timestamp", "station"])
    summary = []
    for st in STATIONS:
        try:
            resp = requests.get(st["url"], headers=headers, timeout=30)
            resp.encoding = resp.apparent_encoding or "utf-8"
            recs = parse_station(resp.text, st["kind"], now)
            if st["kind"] == "water":
                added = merge_water(water, recs, now.isoformat())
            else:
                added = merge_rain(rain, st["key"], recs, now.isoformat())
            summary.append(f"{st['key']}: {len(recs)}行取得/新規{added}")
            if not recs:
                print(f"[warn] {st['key']}: 0行 — ページ構造変化の可能性", file=sys.stderr)
        except Exception as e:
            summary.append(f"{st['key']}: 取得失敗({e})")
            print(f"[error] {st['key']}: {e}", file=sys.stderr)
    save_csv(WATER_CSV, WATER_FIELDS, water, lambda r: r["timestamp"])
    save_csv(RAIN_CSV, RAIN_FIELDS, rain, lambda r: (r["timestamp"], r["station"]))
    build_combined(water, rain)
    update_event_log()
    print(f"[{now.isoformat()}] " + " | ".join(summary))
    print(f"累計 水位{len(water)}行 / 雨量{len(rain)}行")


# ── 自己テスト ─────────────────────────────────────────────
def _test():
    now = datetime(2026, 6, 9, 21, 35, tzinfo=JST)

    assert parse_water_value("0.07\u2192") == (0.07, "\u2192", "ok")
    assert parse_water_value("-0.32\u2192") == (-0.32, "\u2192", "ok")
    assert parse_water_value("**") == (None, "", "missing")
    assert parse_rain_value("0") == (0, "ok")
    assert parse_rain_value("1") == (1, "ok")
    assert parse_rain_value("**") == (None, "missing")
    assert parse_rain_value("--") == (None, "missing")

    ts = assemble_timestamps(["06/09 23:40", "23:50", "00:00"], now)
    assert ts == ["2026-06-09T23:40:00+09:00", "2026-06-09T23:50:00+09:00",
                  "2026-06-10T00:00:00+09:00"], ts

    # 水位ページ（2列）
    water_html = """<html>観測時刻 2026/06/09 21:20
    <table><tr><th>観測時刻</th><th>水位(m)</th></tr>
    <tr><td>06/09 17:30</td><td>0.07\u2192</td></tr>
    <tr><td>17:40</td><td>0.08\u2191</td></tr></table></html>"""
    wr = parse_station(water_html, "water", now)
    assert wr[0] == {"timestamp": "2026-06-09T17:30:00+09:00", "level_m": "0.07",
                     "trend": "\u2192", "status": "ok"}, wr[0]

    # 雨量ページ（3列＋サブヘッダ「10分/累計」行を含む）
    rain_html = """<html>観測時刻 2026/06/09 21:30
    <table>
    <tr><th>観測時刻</th><th>雨量(mm)</th><th></th></tr>
    <tr><td></td><td>10分</td><td>累計</td></tr>
    <tr><td>06/09 18:20</td><td>0</td><td>0</td></tr>
    <tr><td>18:30</td><td>1</td><td>1</td></tr>
    <tr><td>18:40</td><td>0</td><td>1</td></tr></table></html>"""
    rr = parse_station(rain_html, "rain", now)
    assert len(rr) == 3, rr
    assert rr[1] == {"timestamp": "2026-06-09T18:30:00+09:00", "rain_10min_mm": "1",
                     "rain_cum_mm": "1", "status": "ok"}, rr[1]

    # マージ＆結合
    water = {}; rain = {}
    merge_water(water, wr, now.isoformat())
    merge_rain(rain, "南郷山", rr, now.isoformat())
    assert merge_water(water, wr, now.isoformat()) == 0  # 重複なし
    fields = build_combined(water, rain)
    assert "level_m" in fields and "南郷山_10分mm" in fields
    with open(COMBINED_CSV, encoding="utf-8-sig") as f:
        body = f.read()
    assert "南郷山_10分mm" in body
    os.remove(COMBINED_CSV)

    # 欠測→確定の上書き
    rain2 = {("2026-06-09T18:30:00+09:00", "浅間山"):
             {"timestamp": "2026-06-09T18:30:00+09:00", "station": "浅間山",
              "rain_10min_mm": "", "rain_cum_mm": "", "status": "missing", "fetched_at": ""}}
    merge_rain(rain2, "浅間山", [{"timestamp": "2026-06-09T18:30:00+09:00",
              "rain_10min_mm": "2", "rain_cum_mm": "2", "status": "ok"}], now.isoformat())
    assert rain2[("2026-06-09T18:30:00+09:00", "浅間山")]["rain_10min_mm"] == "2"

    print("OK: all self-tests passed")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
    else:
        run()
