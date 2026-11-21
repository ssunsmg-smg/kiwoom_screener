# -*- coding: utf-8 -*-
"""
내일 거래량 급증 후보 (surge_candidates.py)
──────────────────────────────────────────────────────────
근거 (시총 상위 500, 일봉 2022.12~2026.11, 2025년 이후는 처음 보는 데이터로 검증)
  '다음날 거래량 ≥ 20일 평균 3배 + 장중 고가 ≥ 시가 +3%' 발생률   기준 2.6%
  아래 점수 상위 5종목                                            → 23% (약 9배)
  ※ 방향은 못 맞춘다: 후보들은 시가→고가 평균 +4.7%, 시가→저가 -4.9%, 시가→종가 -1.0%.
    '많이 움직일 종목'을 고르는 것이고, 어느 쪽으로 움직일지는 5분봉 진입이 결정한다.

점수 = 아래 6개 지표의 '그날 전체 종목 중 백분위 순위' 평균 (0~1)
  vr1      오늘 거래량 / 직전 20일 평균      (가장 강한 신호 — 거래량은 이어지는 성질)
  amt5_20  최근 5일 거래대금 / 20일 평균
  ret5     5일 수익률
  atrc     최근 5일 변동폭 / 20일 변동폭      (변동성 확대)
  ma20d    20일선 대비 위치
  cloc     오늘 종가가 고가 근처인지 (0~1)

실행:  python surge_candidates.py [--top 500] [--n 10] [--no-telegram]
출력:  surge/candidates_YYYYMMDD.json  (YYYYMMDD = 기준 일봉 날짜, 다음 거래일에 매매)
"""
import argparse
import json
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "surge")
SCORE_FEATS = ["vr1", "amt5_20", "ret5", "atrc", "ma20d", "cloc"]
MIN_AMT20 = 3e9          # 20일 평균 거래대금 30억 이상 (5분봉에서 체결 가능한 유동성)


def features(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 점수용 지표 (그날까지의 데이터만 사용)"""
    o, h, l, c, v = (df[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    amt = c * v
    v20 = v.rolling(20).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f = pd.DataFrame(index=df.index)
    f["vr1"] = v / v20.shift(1)
    f["amt5_20"] = amt.rolling(5).mean() / amt.rolling(20).mean()
    f["ret5"] = c / c.shift(5) - 1
    f["atrc"] = tr.rolling(5).mean() / tr.rolling(20).mean()
    f["ma20d"] = c / c.rolling(20).mean() - 1
    f["cloc"] = (c - l) / (h - l).replace(0, np.nan)
    f["amt20"] = amt.rolling(20).mean()
    f["close"] = c
    f["ret1"] = c / c.shift() - 1
    return f.replace([np.inf, -np.inf], np.nan)


def score_panel(prices: dict) -> pd.DataFrame:
    """{code: OHLCV} → (date, code) 패널 + 날짜별 백분위 점수"""
    parts = []
    for code, df in prices.items():
        df = df.dropna()
        if len(df) < 30:
            continue
        f = features(df)
        f["code"] = code
        parts.append(f.reset_index().rename(columns={f.index.name or "index": "date"}))
    P = pd.concat(parts, ignore_index=True)
    P = P.rename(columns={P.columns[0]: "date"}) if "date" not in P.columns else P
    P = P.dropna(subset=SCORE_FEATS)
    for k in SCORE_FEATS:
        P["r_" + k] = P.groupby("date")[k].rank(pct=True)
    P["score"] = P[["r_" + k for k in SCORE_FEATS]].mean(axis=1)
    return P


def pick(P: pd.DataFrame, date, n=10) -> pd.DataFrame:
    D = P[(P["date"] == date) & (P["amt20"] >= MIN_AMT20)]
    return D.sort_values("score", ascending=False).head(n)


def main():
    import box_breakout_study as bs
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    uni = bs.load_universe(a.top)
    names = {r.Code: r.Name for r in uni.itertuples()}
    prices = bs.download_prices(uni, 1, refresh=True)
    P = score_panel(prices)
    last = P["date"].max()
    top = pick(P, last, a.n)
    rows = [{"rank": i + 1, "code": r.code, "name": names.get(r.code, r.code), "score": round(r.score, 3),
             "vol_ratio": round(r.vr1, 2), "ret1": round(r.ret1, 4), "ret5": round(r.ret5, 4),
             "close": round(r.close), "amt20_eok": round(r.amt20 / 1e8)} for i, r in enumerate(top.itertuples())]
    os.makedirs(OUT_DIR, exist_ok=True)
    ds = pd.Timestamp(last).strftime("%Y%m%d")
    path = os.path.join(OUT_DIR, f"candidates_{ds}.json")
    json.dump({"base_date": ds, "made_with": "surge_candidates v1", "candidates": rows},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    msg = [f"🔥 내일 거래량 급증 후보 ({pd.Timestamp(last):%m-%d} 일봉 기준, 상위 {len(rows)})"]
    for r in rows:
        msg.append(f"{r['rank']:>2}. {r['name']}({r['code']}) 거래량 {r['vol_ratio']:.1f}배 · 오늘 {r['ret1'] * 100:+.1f}% · 점수 {r['score']:.2f}")
    msg.append("\n※ 많이 움직일 종목 후보일 뿐 방향 예측 아님 — 시가 추격 매수는 백테스트상 손실(평균 시가→종가 -1%)")
    text = "\n".join(msg)
    print(text)
    if not a.no_telegram:
        try:
            from box_paper_trader import telegram
            telegram(text)
        except Exception as e:  # noqa
            print(f"  텔레그램 실패: {e}")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
