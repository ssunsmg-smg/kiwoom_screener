# -*- coding: utf-8 -*-
"""
외국인·기관 수급 수집기 (flow_collector.py) — 키움 REST ka10045 (종목별 기관매매추이)
──────────────────────────────────────────────────────────
수급 기반 매도 규칙(예: 외국인+기관 2일 연속 순매도 시 매도)을 검증하려면 과거 수급이 필요하다.
  · 매일:   시총 상위 N + 감시/보유 종목의 최근 10일 수급을 받아 합친다
  · 과거:   --days 180 → 최근 180일치를 한 번에
저장: data/flow/CODE.csv  (date, foreign_net, inst_net — 순매수 수량, 같은 날짜는 최신값으로 덮어씀)
키움 조회만 한다 (주문 없음).
"""
import argparse
import glob
import json
import os
import time
from datetime import timedelta

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data", "flow")


def _int(v):
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def fetch(ki, code, days):
    from kiwoom_trader import now_kst
    end = now_kst()
    rows = ki._fetch_investor_trend(code, (end - timedelta(days=days)).strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    time.sleep(0.25)
    out = [{"date": str(r.get("dt", "")), "foreign_net": _int(r.get("for_daly_nettrde_qty")),
            "inst_net": _int(r.get("orgn_daly_nettrde_qty"))} for r in rows if r.get("dt")]
    return pd.DataFrame(out)


def merge_save(code, df):
    if df.empty:
        return 0
    path = os.path.join(OUT, f"{code}.csv")
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"date": str})
        df = pd.concat([old, df]).drop_duplicates("date", keep="last")
    df = df.sort_values("date")
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def watch_codes():
    codes = set()
    for pat in ("surge/candidates_*.json", "unified/watch_*.json"):
        fs = sorted(glob.glob(os.path.join(BASE, pat)))
        if fs:
            J = json.load(open(fs[-1], encoding="utf-8"))
            codes |= {c["code"] for c in J.get("candidates", J.get("watch", []))}
    for f in ("trade_state_kiwoom/kiwoom_positions.json", "trades/kiwoom_positions.json"):
        p = os.path.join(BASE, f)
        if os.path.exists(p):
            codes |= set(json.load(open(p, encoding="utf-8")).get("positions", {}).keys())
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="조회 기간(달력일). 처음 한 번은 180 권장")
    ap.add_argument("--top", type=int, default=500)
    a = ap.parse_args()
    import box_breakout_study as bs
    from kiwoom_intraday import KiwoomIntraday
    ki = KiwoomIntraday()
    codes = list(dict.fromkeys(list(watch_codes()) + list(bs.load_universe(a.top)["Code"])))
    ok = empty = 0
    for c in codes:
        try:
            n = merge_save(c, fetch(ki, c, a.days))
        except Exception as e:  # noqa
            print(f"  ⚠ {c}: {e}")
            n = 0
        ok += n > 0
        empty += n == 0
    msg = f"📊 수급 수집: {ok}종목 저장 · {empty}종목 0건 (요청 {len(codes)}, 기간 {a.days}일)"
    print(msg)
    if ok == 0 and codes:
        try:
            from box_paper_trader import telegram
            telegram(msg + "\n⚠ 수급이 하나도 안 옴 — 키움 모의서버가 ka10045 를 지원하지 않을 수 있음")
        except Exception:  # noqa
            pass


if __name__ == "__main__":
    main()
