# -*- coding: utf-8 -*-
"""
통합 전략 v1 (unified_strategy.py) — 키움 단일 계좌용
──────────────────────────────────────────────────────────
  python unified_strategy.py watch     # 장 마감 후: 내일 감시 목록 만들기
  python unified_strategy.py run       # 장중 15분마다: 매도 점검 → 매수 점검 (키움 주문)
  python unified_strategy.py run --dry-run   # 주문 없이 판단만 출력

[종목 선별 — watch]
  ① 박스권 돌파 + 돌파 점수 3점↑ + 아직 박스 상단까지 안 눌린 종목 (box_pullback_scanner)
       → 백테스트: 2024년 이전 +1.2~1.6% / 2025년 이후 +1.6~3.5% (거래당, 비용 포함)
  ② 우선순위 = 돌파 점수(0~7) + 거래량 급증 점수×3(0~3) + DART 호재 공시 +1
     DART 악재 공시(유상증자·CB·감자 등)가 최근 3일 안에 있으면 제외
  ※ v1 은 검증된 '박스 눌림' 진입만 주문한다. 5분봉 눌림목 진입은 5분봉 검증 후 v2 에서 추가.

[매수 — run]  09:00~14:50
  현재가 ≤ 지정가(박스상단×1.02) 이고 ≥ 박스상단×0.97 (무너지지 않음) → 시장가 매수
  종목당 PER_TRADE 원, 보유 최대 MAX_POS 종목, 하루 신규 최대 MAX_NEW_PER_DAY

[매도 — run]  (백테스트 274건 비교 결과 가장 일관된 방법)
  · 손절: 매수가 -10%
  · +7% 도달: 절반 매도 → 남은 절반의 손절선을 본전(+0.3%)으로
  · 나머지: 매수 후 최고가 대비 -10% 하락 시 매도
  · 40거래일 지나면 전량 정리
  기존 v41f 보유분이 남아 있으면 같은 규칙으로 관리한다 (매수가 = 평균단가).

상태 파일: trades/unified_state.json  (워크플로가 trade_state_kiwoom/ 에 보존)
"""
import argparse
import glob
import json
import os
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
WATCH_DIR = os.path.join(BASE, "unified")
STATE = os.path.join(BASE, "trades", "unified_state.json")

PER_TRADE = 1_000_000
MAX_POS = 10
MAX_NEW_PER_DAY = 5
STOP_PCT, TP1_PCT, TRAIL_PCT, MAX_HOLD = 0.10, 0.07, 0.10, 40
BUY_START, BUY_END = "09:00", "14:50"


def now_kst():
    try:
        from kiwoom_trader import now_kst as f
        return f()
    except ImportError:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def jload(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa
        return d


def jsave(p, o):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


def tg(text):
    try:
        from box_paper_trader import telegram
        telegram(text)
    except Exception as e:  # noqa
        print(f"  (텔레그램 생략: {e})")


# ═════════════════════════════════ watch (장 마감 후)
def build_watch(prices, meta, surge_scores=None, dart_flags=None, top_n=15):
    import box_pullback_scanner as sc
    buy, allb = sc.scan(prices, meta)
    surge_scores = surge_scores or {}
    dart_flags = dart_flags or {}
    rows = []
    for r in (buy.to_dict("records") if len(buy) else []):
        code = r["종목코드"]
        f = dart_flags.get(code, {})
        if f.get("bad"):
            print(f"  제외(악재 공시) {r['종목명']}: {f['bad'][0]}")
            continue
        s_surge = float(surge_scores.get(code, 0.0))
        pri = int(r["점수"]) + 3 * s_surge + (1 if f.get("good") else 0)
        bdate = pd.Timestamp(r["돌파일"])
        rows.append({
            "code": code, "name": r["종목명"], "priority": round(pri, 2), "box_score": int(r["점수"]),
            "surge_score": round(s_surge, 2), "dart_good": f.get("good", [])[:2],
            "breakout": str(bdate.date()), "expire": str((bdate + pd.offsets.BDay(sc.LOOKBACK)).date()),
            "box_top": int(r["박스상단"]), "limit": int(r["매수지정가"]), "floor": int(r["실패기준가(종가)"]),
            "reason": r["충족조건"],
        })
    rows.sort(key=lambda x: -x["priority"])
    return rows[:top_n], allb


def cmd_watch(a):
    import box_breakout_study as bs
    from surge_candidates import score_panel
    uni = bs.load_universe(a.top)
    meta = {r.Code: (r.Name, r.Market) for r in uni.itertuples()}
    prices = bs.download_prices(uni, 1, refresh=a.refresh)
    P = score_panel(prices)
    last = P["date"].max()
    surge_scores = P[P["date"] == last].set_index("code")["score"].to_dict()
    dart_flags = jload(os.path.join(BASE, "news", "dart_flags.json"), {}).get("flags", {})
    rows, allb = build_watch(prices, meta, surge_scores, dart_flags)
    ds = pd.Timestamp(last).strftime("%Y%m%d")
    jsave(os.path.join(WATCH_DIR, f"watch_{ds}.json"), {"base_date": ds, "made_at": str(now_kst()), "watch": rows,
                                                        "rules": {"stop": STOP_PCT, "tp1": TP1_PCT, "trail": TRAIL_PCT,
                                                                  "max_hold": MAX_HOLD, "per_trade": PER_TRADE}})
    lines = [f"🎯 통합전략 내일 감시 목록 ({pd.Timestamp(last):%m-%d} 기준, {len(rows)}종목 · 최근 박스돌파 {len(allb)})"]
    for x in rows:
        tag = " ·공시호재" if x["dart_good"] else ""
        lines.append(f" {x['name']}({x['code']}) 지정가 {x['limit']:,} · 점수 {x['box_score']}+급증{x['surge_score']:.2f}{tag} · ~{x['expire'][5:]}")
    if not rows:
        lines.append(" 조건 맞는 종목 없음 — 내일은 매도 점검만")
    print("\n".join(lines))
    if not a.no_telegram:
        tg("\n".join(lines))


# ═════════════════════════════════ run (장중)
def bdays_between(d0, d1):
    return max(len(pd.bdate_range(pd.Timestamp(d0), pd.Timestamp(d1))) - 1, 0)


def latest_watch(today):
    fs = sorted(f for f in glob.glob(os.path.join(WATCH_DIR, "watch_*.json")) if os.path.basename(f)[6:14] < today)
    return jload(fs[-1], {"watch": []}) if fs else {"watch": []}


def cmd_run(a, trader=None):
    if trader is None:
        from kiwoom_trader import KiwoomRestTrader
        trader = KiwoomRestTrader()
    t = trader
    now = now_kst()
    today = now.strftime("%Y%m%d")
    hhmm = now.strftime("%H:%M")
    st = jload(STATE, {"positions": {}, "bought": {}, "log": []})
    st.setdefault("positions", {}); st.setdefault("bought", {}); st.setdefault("log", [])
    t.sync_positions_from_broker()
    held = {c: p for c, p in t.positions.items() if int(p.get("qty", 0)) > 0}
    acts = []

    # 상태 정리: 브로커에 없는 종목은 상태에서 제거(외부 매도 등)
    for c in list(st["positions"]):
        if c not in held:
            st["positions"].pop(c)

    # ── 매도 점검 ──
    for code, p in held.items():
        m = st["positions"].get(code)
        if m is None:   # v41f 보유분 등 — 같은 규칙으로 관리 시작
            e = float(p.get("avg_price") or 0)
            m = {"entry": e, "entry_date": p.get("buy_date", now.strftime("%Y-%m-%d")), "half": False,
                 "peak": e, "stop": round(e * (1 - STOP_PCT)), "origin": "기존보유"}
            st["positions"][code] = m
        px = t.get_current_price(code)
        if px <= 0:
            continue
        qty = int(p["qty"])
        e = float(m["entry"])
        m["peak"] = max(float(m["peak"]), px)
        held_days = bdays_between(m["entry_date"], now.date())
        why, sell_qty = None, 0
        if px <= m["stop"]:
            why, sell_qty = ("본전 손절" if m["half"] else "손절 -10%"), qty
        elif not m["half"] and px >= e * (1 + TP1_PCT):
            why, sell_qty = "1차 익절 +7% (절반)", max(qty // 2, 1)
        elif m["half"] and px <= m["peak"] * (1 - TRAIL_PCT):
            why, sell_qty = "트레일링 (고점 -10%)", qty
        elif held_days >= MAX_HOLD and hhmm >= "15:00":
            why, sell_qty = f"보유 {held_days}일 만기", qty
        if why:
            acts.append(f"SELL {code} {sell_qty}주 @{px:,} — {why}")
            if not a.dry_run:
                r = t.place_order(code, "SELL", sell_qty, 0, reason=why)
                if not r.get("success"):
                    acts.append(f"  ⚠ 매도 실패: {r.get('msg')}")
                    continue
            if why.startswith("1차 익절") and sell_qty < qty:
                m["half"] = True
                m["stop"] = max(m["stop"], round(e * 1.003))
            else:
                st["positions"].pop(code, None)

    # ── 매수 점검 ──
    if BUY_START <= hhmm <= BUY_END and not a.no_buy:
        W = latest_watch(today)
        new_today = sum(1 for d in st["bought"].values() if d == today)
        n_pos = len([c for c in held if c in st["positions"]])
        for w in W.get("watch", []):
            if n_pos >= MAX_POS or new_today >= MAX_NEW_PER_DAY:
                break
            code = w["code"]
            if code in held or code in st["bought"] or now.strftime("%Y-%m-%d") > w["expire"]:
                continue
            px = t.get_current_price(code)
            if px <= 0:
                continue
            floor_intraday = w["box_top"] * 0.97
            if px <= w["limit"] and px >= floor_intraday:
                qty = int(PER_TRADE // px)
                if qty < 1:
                    continue
                reason = f"박스 눌림 매수 (지정가 {w['limit']:,}, 점수 {w['box_score']})"
                acts.append(f"BUY {code} {w['name']} {qty}주 @{px:,} — {reason}")
                if not a.dry_run:
                    r = t.place_order(code, "BUY", qty, 0, reason=reason)
                    if not r.get("success"):
                        acts.append(f"  ⚠ 매수 실패: {r.get('msg')}")
                        continue
                    px = r.get("price") or px
                st["positions"][code] = {"entry": px, "entry_date": now.strftime("%Y-%m-%d"), "half": False,
                                         "peak": px, "stop": round(px * (1 - STOP_PCT)), "origin": "통합v1",
                                         "name": w["name"], "box_top": w["box_top"]}
                st["bought"][code] = today
                new_today += 1
                n_pos += 1

    # 오래된 매수 기록 정리 (30일 지난 것)
    cut = (now - pd.Timedelta(days=30)).strftime("%Y%m%d")
    st["bought"] = {c: d for c, d in st["bought"].items() if d >= cut}
    st["log"] = (st["log"] + [f"{now:%m-%d %H:%M} {x}" for x in acts])[-300:]
    st["updated"] = str(now)
    if not a.dry_run:
        jsave(STATE, st)
    print(f"[통합v1 {now:%H:%M}] 보유 {len(held)} · 조치 {len(acts)}")
    for x in acts:
        print("  " + x)
    return acts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "run"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-buy", action="store_true", help="매도 점검만")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--refresh", action="store_true", default=True)
    a = ap.parse_args()
    cmd_watch(a) if a.cmd == "watch" else cmd_run(a)


if __name__ == "__main__":
    main()
