# -*- coding: utf-8 -*-
"""
5분봉 눌림목 백테스트 (pullback5_backtest.py)
──────────────────────────────────────────────────────────
대상: data/minute5/YYYYMMDD/CODE.csv  (후보로 뽑힌 다음 날의 5분봉)
      + data/intraday/bars/ (예전 KIS 수집분, 같은 형식)

규칙 (변형별로 비교)
  ① 시초 상승: 장 시작 후 imp_min 분 안에 고가가 시가 대비 +imp_pct 이상 → 상승 고점(H) 기록
  ② 눌림:      고점 이후 저가가
                 mode='vwap' : VWAP 까지 내려옴 (VWAP×1.002 이하) & 종가는 VWAP 위 유지
                 mode='fib'  : 상승폭(시가→H)의 38.2%~61.8% 되돌림 구간
  ③ 진입:      눌림 뒤 '직전 봉 고가를 넘는 봉'에서 매수 (직전 고가 +0.1%), 마감시각 entry_until 전까지
  ④ 손절:      눌림 저점 -0.2% (최대 -max_loss)
     익절:      진입가 + rr × (진입가-손절가)   또는 tp_pct 고정
     시간청산:  15:15 종가
  같은 봉에서 손절·익절이 동시에 닿으면 손절로 처리 (보수적). 비용 왕복 0.25%.

실행: python pullback5_backtest.py            → 변형별 성과표 + data/minute5/pullback5_result.csv
"""
import glob
import itertools
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COST = 0.0025


def load_days():
    files = glob.glob(os.path.join(BASE, "data", "minute5", "*", "*.csv")) + \
            glob.glob(os.path.join(BASE, "data", "intraday", "bars", "*", "*.csv"))
    out = {}
    for f in files:
        day, code = os.path.basename(os.path.dirname(f)), os.path.basename(f)[:-4]
        try:
            d = pd.read_csv(f, parse_dates=["time"]).set_index("time").sort_index()
        except Exception:  # noqa
            continue
        d = d.between_time("09:00", "15:30")
        if len(d) >= 40:                       # 하루 78봉 중 절반 이상 있어야 사용
            out[(day, code)] = d
    return out


def simulate(d: pd.DataFrame, imp_pct=0.02, imp_min=30, mode="vwap", entry_until="11:30",
             rr=1.5, tp_pct=None, max_loss=0.025):
    o, h, l, c, v = (d[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    t = d.index
    tp_ = (h + l + c) / 3
    vwap = np.cumsum(tp_ * v) / np.maximum(np.cumsum(v), 1)
    op = o[0]
    t0 = t[0]
    # ① 시초 상승
    H, hi_i = -1, -1
    for i in range(len(c)):
        if (t[i] - t0).total_seconds() / 60 >= imp_min:
            break
        if h[i] >= op * (1 + imp_pct) and h[i] > H:
            H, hi_i = h[i], i
    if hi_i < 0:
        return None                                     # 시초 상승 없음 → 매매 안 함
    # 고점 갱신 추적하며 눌림 찾기
    pb_low, pb_i = None, None
    until = pd.Timestamp(f"{t[0]:%Y-%m-%d} {entry_until}")
    for i in range(hi_i + 1, len(c)):
        if t[i] > until:
            return None
        if pb_i is None:
            if h[i] > H:                                # 아직 상승 중 → 고점 갱신
                H, hi_i = h[i], i
                continue
            if mode == "vwap":
                touched = l[i] <= vwap[i] * 1.002 and c[i] >= vwap[i] * 0.995
            else:
                rng = H - op
                touched = H - 0.618 * rng <= l[i] <= H - 0.382 * rng
            if touched:
                pb_low, pb_i = l[i], i
            elif c[i] < min(vwap[i], op) * 0.99:
                return None                             # 눌림이 아니라 무너짐 → 포기
            continue
        # ② 눌림 중: 저점 갱신
        if l[i] < pb_low:
            if mode == "vwap" and c[i] < vwap[i] * 0.99:
                return None
            pb_low = l[i]
        # ③ 진입: 직전 봉 고가 돌파
        trig = h[i - 1] * 1.001
        if h[i] >= trig:
            e = max(o[i], trig)
            stop = max(pb_low * 0.998, e * (1 - max_loss))
            if stop >= e:
                return None
            tgt = e * (1 + tp_pct) if tp_pct else e + rr * (e - stop)
            # 진입 봉 자체에서 손절 닿으면 손절
            if l[i] <= stop:
                return dict(entry_t=t[i], e=e, x=stop, why="stop", ret=stop / e - 1 - COST)
            for j in range(i + 1, len(c)):
                if t[j].strftime("%H:%M") >= "15:15":
                    return dict(entry_t=t[i], e=e, x=c[j], why="time", ret=c[j] / e - 1 - COST)
                if l[j] <= stop:
                    x = min(stop, o[j])
                    return dict(entry_t=t[i], e=e, x=x, why="stop", ret=x / e - 1 - COST)
                if h[j] >= tgt:
                    x = max(tgt, o[j])
                    return dict(entry_t=t[i], e=e, x=x, why="tp", ret=x / e - 1 - COST)
            return dict(entry_t=t[i], e=e, x=c[-1], why="eod", ret=c[-1] / e - 1 - COST)
    return None


def baseline(d):
    """비교용: 시가 매수 → 종가 매도"""
    return d["Close"].iloc[-1] / d["Open"].iloc[0] - 1 - COST


GRID = dict(imp_pct=[0.015, 0.02, 0.03], mode=["vwap", "fib"], entry_until=["10:30", "11:30"],
            rr=[1.0, 1.5, 2.0])


def run(days=None):
    days = days or load_days()
    print(f"분석 대상 {len(days)} 종목·일")
    rows = []
    for vals in itertools.product(*GRID.values()):
        kw = dict(zip(GRID, vals))
        rs = [r["ret"] for d in days.values() if (r := simulate(d, **kw))]
        if not rs:
            continue
        r = np.array(rs)
        rows.append(dict(**kw, trades=len(r), trade_rate=len(r) / len(days), win=(r > 0).mean(),
                         avg=r.mean(), med=np.median(r),
                         t=r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else np.nan))
    T = pd.DataFrame(rows).sort_values("avg", ascending=False)
    b = np.array([baseline(d) for d in days.values()])
    print(f"비교) 시가 매수→종가 매도: 평균 {b.mean() * 100:+.2f}% · 승률 {(b > 0).mean():.0%}")
    if len(T):
        show = T.copy()
        for k in ("trade_rate", "win", "avg", "med"):
            show[k] = (show[k] * 100).round(2)
        print(show.head(15).to_string(index=False))
        os.makedirs(os.path.join(BASE, "data", "minute5"), exist_ok=True)
        T.to_csv(os.path.join(BASE, "data", "minute5", "pullback5_result.csv"), index=False)
    return T


if __name__ == "__main__":
    run()
