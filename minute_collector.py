# -*- coding: utf-8 -*-
"""
5분봉 수집기 (minute_collector.py) — 키움 REST ka10080
──────────────────────────────────────────────────────────
5분봉 눌림목 전략을 검증하려면 '후보로 뽑힌 날의 다음 날 5분봉'이 필요하다.
  · 매일 모드:   어제 저녁 후보(surge/candidates_*.json) 종목의 오늘 5분봉을 저장
  · 과거 채우기: --backfill N  → 지난 N 거래일 각각에 대해 그날 후보(일봉으로 재계산)를 뽑고,
                 다음 거래일 5분봉을 키움에서 받아 저장 (ka10080 base_dt 사용)
저장: data/minute5/YYYYMMDD/CODE.csv  (time,Open,High,Low,Close,Volume — 기존 data/intraday/bars 와 같은 형식)

키움 조회만 한다 (주문 없음). kiwoom_config.json 은 워크플로가 시크릿으로 만든다.
※ 모의서버가 분봉을 안 주면 0건이 찍힌다 → 로그와 텔레그램으로 바로 알 수 있게 함.
"""
import argparse
import glob
import json
import os
import time

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data", "minute5")


def save_day(ki, code: str, day: str) -> int:
    """code 의 day(YYYYMMDD) 5분봉 저장. 이미 있으면 건너뜀. 반환: 저장한 봉 수"""
    path = os.path.join(OUT, day, f"{code}.csv")
    if os.path.exists(path):
        return -1
    df = ki.get_minute_chart(code, interval=5, base_dt=day)
    time.sleep(0.3)                       # 키움 초당 조회 제한 여유
    if df is None or df.empty:
        return 0
    d = df[df.index.strftime("%Y%m%d") == day]
    if d.empty:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d.reset_index().rename(columns={"index": "time"}).to_csv(path, index=False)
    return len(d)


def trading_days_after(daily_index, d):
    nxt = daily_index[daily_index > d]
    return nxt[0] if len(nxt) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="지난 N 거래일 후보의 다음날 5분봉 채우기")
    ap.add_argument("--n", type=int, default=10, help="날짜별 후보 수")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()

    from kiwoom_intraday import KiwoomIntraday
    ki = KiwoomIntraday()
    jobs = []                                            # (code, day)

    # ① 매일 모드: 가장 최근 후보 파일 중 '기준일 < 오늘'인 것 → 오늘 5분봉
    try:
        from kiwoom_trader import now_kst
    except ImportError:                                  # 구버전 kiwoom_trader.py
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_kst = lambda: datetime.now(ZoneInfo("Asia/Seoul"))  # noqa
    today = now_kst().strftime("%Y%m%d")
    files = sorted(f for f in glob.glob(os.path.join(BASE, "surge", "candidates_*.json"))
                   if os.path.basename(f)[11:19] < today)
    if files:
        J = json.load(open(files[-1], encoding="utf-8"))
        jobs += [(c["code"], today) for c in J["candidates"]]

    # ② 과거 채우기: 일봉으로 그날 후보를 다시 뽑아 다음날 5분봉 요청
    if a.backfill:
        import box_breakout_study as bs
        from surge_candidates import score_panel, pick
        uni = bs.load_universe(500)
        prices = bs.download_prices(uni, 1, refresh=False)
        P = score_panel(prices)
        dates = sorted(P["date"].unique())
        idx = pd.DatetimeIndex(dates)
        for d in dates[-(a.backfill + 1):-1]:
            nd = trading_days_after(idx, d)
            if nd is None:
                continue
            for code in pick(P, d, a.n)["code"]:
                jobs.append((code, pd.Timestamp(nd).strftime("%Y%m%d")))

    jobs = list(dict.fromkeys(jobs))
    got = skipped = empty = 0
    for code, day in jobs:
        n = save_day(ki, code, day)
        if n == -1:
            skipped += 1
        elif n == 0:
            empty += 1
        else:
            got += 1
    msg = f"🗂 5분봉 수집: 저장 {got} · 이미있음 {skipped} · 0건 {empty} (요청 {len(jobs)})"
    print(msg)
    if empty and not got and not a.no_telegram:
        try:
            from box_paper_trader import telegram
            telegram(msg + "\n⚠ 분봉이 하나도 안 옴 — 키움 모의서버가 분봉조회(ka10080)를 지원하지 않을 수 있음. 로그 확인 필요")
        except Exception:  # noqa
            pass


if __name__ == "__main__":
    main()
