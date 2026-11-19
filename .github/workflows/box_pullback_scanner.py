# -*- coding: utf-8 -*-
"""
박스권 돌파 → 눌림 매수 스캐너 (box_pullback_scanner.py v1)
──────────────────────────────────────────────────────────
근거: box_breakout_study 811건 상방돌파 재분석 (2022.06~2026.10, 시총 상위 500)
  · 돌파 다음날 시가에 바로 사면  → 청산규칙 24가지 중 22가지가 손실 (기대값 ≈ 0 이하)
  · 돌파 후 박스 상단까지 눌릴 때 사면 → 24가지 전부 수익 (2024년 이전·2025년 이후 모두)
  · 아래 7개 조건 중 3개 이상 충족한 돌파만 매수 → 60일 안 +30% 폭등 확률 약 2배

매매 규칙
  ① 최근 10거래일 안에 박스 상방돌파가 나온 종목 중 '돌파 점수' 3점 이상
  ② 돌파 후 종가가 박스상단×0.97 아래로 한 번도 안 내려갔을 것 (내려가면 실패 → 제외)
  ③ 매수: 박스상단×1.02 지정가 (돌파 후 10거래일 안에 체결 안 되면 포기)
  ④ 손절 -10% / 익절 +20% / 최대 40거래일 보유 후 종가 정리
     (검증 기대값: 2024년 이전 +1.2%, 2025년 이후 +3.5% / 거래, 비용 0.25% 포함)

돌파 점수 (돌파일 기준, 각 1점 — 기준값은 2024년 이전 데이터 상위 1/3 경계로 고정)
  거래대금 5/20 ≥ 1.68   최근 5일 거래대금이 20일 평균보다 확 늘었다
  박스폭 ≥ 26.6%         넓은 박스 (좁은 박스는 폭등이 드묾)
  ATR 비율 ≥ 1.13        변동성이 박스 평균보다 커지기 시작
  돌파폭 ≥ 1.84%         종가가 상단을 확실히 넘음
  돌파일 거래량 ≥ 3.07배  직전 20일 평균 대비
  종가 위치 ≥ 0.92       고가 근처 마감
  저점 상승 ≤ -0.54%     최근 20일 저점이 이전보다 낮음 (흔들기 후 돌파)

실행 (C:\\quant_ai 가상환경)
  python box_pullback_scanner.py              # 시총 상위 500
  python box_pullback_scanner.py --top 1000   # 코스닥 더 포함
출력: box_study/pullback_watch_YYYYMMDD.xlsx
"""
import argparse
import os
from datetime import datetime

import numpy as np
import pandas as pd

import box_breakout_study as bs

TH = {  # 2024년 이전 데이터에서 고정한 기준값 (다시 맞추지 말 것 → 과최적화 방지)
    "amt5_20": 1.68, "box_width": 26.64, "atr_ratio": 1.13, "margin": 1.84,
    "vol_ratio": 3.07, "close_loc": 0.92, "higher_low": -0.54,
}
LABEL = {
    "amt5_20": "거래대금급증", "box_width": "넓은박스", "atr_ratio": "변동성확대", "margin": "확실한돌파",
    "vol_ratio": "거래량3배", "close_loc": "고가마감", "higher_low": "흔들기후돌파",
}
MIN_SCORE = 3
LOOKBACK = 10        # 돌파 후 눌림을 기다리는 최대 거래일
ENTRY_MULT = 1.02    # 매수 지정가 = 박스상단 × 1.02
FAIL_MULT = 0.97     # 종가가 박스상단 × 0.97 아래면 실패
SL, TP, HOLD = 0.10, 0.20, 40
MIN_AMT = 10e8       # 20일 평균 거래대금 10억 이상


def score_breakout(df, t, top, bot, w):
    o, h, l, c, v = (df[k].values.astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    cs, vs = pd.Series(c), pd.Series(v)
    amt = cs * vs
    amt20, amt5 = amt.rolling(20).mean().values, amt.rolling(5).mean().values
    vol20_prev = vs.rolling(20).mean().shift(1).values
    atr = bs._atr(pd.Series(h), pd.Series(l), cs).values
    p = t - 1
    s, e = t - bs.RECENT_EXCL - w + 1, t - bs.RECENT_EXCL + 1
    box_atr = np.nanmean(atr[s:e])
    rng = h[t] - l[t]
    f = {
        "amt5_20": amt5[t] / amt20[t] if amt20[t] > 0 else np.nan,
        "box_width": (top - bot) / bot * 100,
        "atr_ratio": atr[p] / box_atr if box_atr > 0 else np.nan,
        "margin": (c[t] / top - 1) * 100,
        "vol_ratio": v[t] / vol20_prev[t] if vol20_prev[t] > 0 else np.nan,
        "close_loc": (c[t] - l[t]) / rng if rng > 0 else 0.5,
        "higher_low": (l[p - 19:p + 1].min() / l[p - 39:p - 19].min() - 1) * 100,
    }
    hit = {k: (f[k] <= TH[k]) if k == "higher_low" else (f[k] >= TH[k]) for k in TH}
    return f, hit, amt20[p]


def scan_one(code, name, market, df):
    c, l = df["Close"].values.astype(float), df["Low"].values.astype(float)
    n = len(c)
    if n < max(bs.WINDOWS) + bs.RECENT_EXCL + 45:
        return None
    box_w, box_top, box_bot, _ = bs.detect_boxes(c, df["High"].values.astype(float), l)
    for t in range(n - 1, max(n - 1 - LOOKBACK, 0), -1):        # 가장 최근 돌파부터
        if box_w[t] == 0:
            continue
        top, bot, w = box_top[t], box_bot[t], box_w[t]
        if not (c[t] > top and c[t - 1] <= top):
            continue
        f, hit, amt20 = score_breakout(df, t, top, bot, w)
        if not amt20 >= MIN_AMT:
            return None
        score = int(sum(hit.values()))
        after = slice(t + 1, n)
        limit = top * ENTRY_MULT
        if (c[after] < top * FAIL_MULT).any():
            status = "실패(박스복귀)"
        elif (l[after] <= limit).any():
            k = t + 1 + int(np.argmax(l[after] <= limit))
            status = f"체결({df.index[k]:%m-%d})"
        else:
            status = "눌림대기"
        days_left = LOOKBACK - (n - 1 - t)
        return {
            "종목코드": code, "종목명": name, "시장": market,
            "돌파일": df.index[t].date(), "경과일": n - 1 - t, "남은대기일": max(days_left, 0),
            "점수": score, "충족조건": ", ".join(LABEL[k] for k, v in hit.items() if v),
            "상태": status, "현재가": round(c[-1]), "박스상단": round(top), "박스하단": round(bot),
            "매수지정가": round(limit), "손절가": round(limit * (1 - SL)), "목표가": round(limit * (1 + TP)),
            "현재가-지정가(%)": round((c[-1] / limit - 1) * 100, 1),
            "실패기준가(종가)": round(top * FAIL_MULT),
            **{f"_{k}": round(v, 2) for k, v in f.items()},
        }
    return None


def scan(prices, meta):
    rows = []
    for code, df in prices.items():
        name, market = meta.get(code, (code, ""))
        try:
            r = scan_one(code, name, market, df.dropna())
            if r:
                rows.append(r)
        except Exception as e:  # noqa
            print(f"  {code} 오류: {e}")
    out = pd.DataFrame(rows)
    if out.empty:
        return out, out
    out = out.sort_values(["점수", "경과일"], ascending=[False, True])
    buy = out[(out["점수"] >= MIN_SCORE) & (out["상태"] == "눌림대기") & (out["남은대기일"] > 0)]
    return buy, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--refresh", action="store_true", help="오늘 가격 새로 받기")
    a = ap.parse_args()
    uni = bs.load_universe(a.top)
    meta = {r.Code: (r.Name, r.Market) for r in uni.itertuples()}
    prices = bs.download_prices(uni, 1, refresh=a.refresh)
    buy, allb = scan(prices, meta)
    os.makedirs(bs.OUT_DIR, exist_ok=True)
    path = os.path.join(bs.OUT_DIR, f"pullback_watch_{datetime.now():%Y%m%d}.xlsx")
    guide = pd.DataFrame({"규칙": [
        f"매수후보 시트: 최근 {LOOKBACK}거래일 안 박스 돌파 + 점수 {MIN_SCORE}점 이상 + 아직 박스상단까지 안 눌림",
        "매수지정가(박스상단×1.02)에 지정가 주문 → 남은대기일 안에 체결 안 되면 취소",
        "장중이라도 종가가 실패기준가 아래로 마감하면 후보에서 제외",
        f"체결 후: 손절 -{SL:.0%}, 익절 +{TP:.0%}, {HOLD}거래일 지나면 종가 정리",
        "한 번에 5~10종목 분산, 종목당 비중 동일 권장 (승률 약 47% — 연속 손절 정상)",
    ]})
    with pd.ExcelWriter(path) as w:
        guide.to_excel(w, sheet_name="0_사용법", index=False)
        buy.to_excel(w, sheet_name="1_매수후보", index=False)
        allb.to_excel(w, sheet_name="2_최근돌파전체", index=False)
    print(f"\n매수후보 {len(buy)}종목 / 최근 돌파 {len(allb)}종목 → {path}")
    if len(buy):
        print(buy[["종목코드", "종목명", "점수", "충족조건", "현재가", "매수지정가", "손절가", "목표가"]].to_string(index=False))


if __name__ == "__main__":
    main()
