# -*- coding: utf-8 -*-
"""
박스권 돌파 징후 이벤트 스터디 (box_breakout_study.py v1)
──────────────────────────────────────────────────────────
목적
  "보합(박스권) 구간에서 위로 돌파할 때 어떤 징후가 있었는가"를
  과거 데이터로 숫자로 확인한다. → 3단계(포착 신호) 설계의 근거 데이터.

절차
  ① 종목 유니버스: 코스피+코스닥 시총 상위 N (우선주·스팩 제외)
  ② 매일 박스권 판정: quant_screener_v41g 의 _detect_box_range 와 같은 규칙
       - 120 → 90 → 60일 순으로 검사, 최근 3일은 박스 계산에서 제외
       - 구간 가격폭 ≤ 35%, 전반부/후반부 평균가 차이 ≤ 12%
       - 종가가 박스 상단권·하단권을 3회 이상 왕복 (--swings 로 변경)
  ③ 이벤트: 박스가 유효한 상태에서 종가가 처음으로 상단 위(상방돌파) 또는
       하단 아래(하방이탈)로 나간 날. 같은 종목은 이벤트 후 20일 쿨다운.
  ④ 결과 라벨(상방돌파): 다음날 시가 진입 → 20거래일 안에
       +10% 먼저 도달 = 성공 / -7% 먼저 도달 또는 기간만료 = 실패
       (같은 날 둘 다 닿으면 보수적으로 손절 처리)
       5일 안에 종가가 박스 상단 아래로 돌아오면 '가짜돌파'
  ⑤ 징후 계산: 돌파 '전날까지' 데이터로 계산한 사전징후 +
       돌파 당일 징후 + 시장(코스피) 환경
  ⑥ 엑셀 리포트: 징후별 효과, 구간별 성공률, 규칙 조합(IS/OOS 분리), 연도별

실행 (C:\\quant_ai 가상환경 기준)
  python box_breakout_study.py                 # 기본: 시총 상위 500, 5년
  python box_breakout_study.py --top 1000 --years 7
  python box_breakout_study.py --tp 15 --sl 7 --horizon 30
  python box_breakout_study.py --refresh       # 가격 캐시 무시하고 다시 받기
  python box_breakout_study.py --compare       # 왕복 기준 0/2/3/4회 비교 시트 추가

출력
  box_study/box_breakout_study_YYYYMMDD_HHMMSS.xlsx

주의
  - 현재 상장 종목만 대상(상장폐지 종목 빠짐) → 결과가 약간 낙관적일 수 있음
  - 외국인·기관 수급은 yfinance에 없어 이번 버전에서는 제외
    (KIS/키움 API로 수급을 붙이는 건 다음 단계)
"""
import argparse
import itertools
import os
import pickle
import sys
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, ".cache", "box_study")
OUT_DIR = os.path.join(BASE, "box_study")

# ── 박스 판정 파라미터 (v41g _detect_box_range 와 동일) ──
WINDOWS = (120, 90, 60)
RECENT_EXCL = 3
MAX_WIDTH = 35.0
MAX_DRIFT = 12.0
MIN_SWINGS = 3      # 종가가 상단권(65%↑)·하단권(35%↓)을 오간 최소 횟수
# 2026-11-18: v41g 의 inside_ratio(종가 90%가 밴드 안) 조건은 항상 100%라
#             효과가 없어 '왕복 횟수' 조건으로 교체 (스크리너도 동일하게 수정).
#             --swings 0 이면 수정 전 규칙과 같음.

COOLDOWN = 20

# ── 징후 정의: 컬럼명 → (표시명, 구분, 설명) ──
FEATURES = {
    "f_box_window":  ("박스기간(일)", "사전", "박스로 인정된 구간 길이 (120/90/60)"),
    "f_box_width":   ("박스폭(%)", "사전", "박스 (상단-하단)/하단"),
    "f_pos_prev":    ("전날 박스내위치(%)", "사전", "돌파 전날 종가의 박스 내 위치 (0=하단, 100=상단)"),
    "f_vol_dryup":   ("거래량 말라붙음", "사전", "최근10일 평균거래량 ÷ 박스구간 평균거래량 (낮을수록 말라붙음)"),
    "f_bbw_rank":    ("볼린저폭 순위(%)", "사전", "볼린저밴드 폭의 최근 120일 내 백분위 (낮을수록 스퀴즈)"),
    "f_atr_ratio":   ("ATR 수축", "사전", "ATR14 ÷ 박스구간 평균 ATR14 (낮을수록 변동성 수축)"),
    "f_obv_div":     ("OBV 매집", "사전", "최근20일 OBV 변화 ÷ 20일 거래량 합 (+면 매집, -면 분산)"),
    "f_higher_low":  ("저점 상승(%)", "사전", "최근20일 최저가 ÷ 그 이전20일 최저가 - 1 (+면 저점 상승)"),
    "f_rsi_prev":    ("전날 RSI14", "사전", "돌파 전날 RSI14"),
    "f_ma20_slope":  ("20일선 기울기(%)", "사전", "전날 MA20 ÷ 5일 전 MA20 - 1"),
    "f_vol_ratio_t": ("돌파일 거래량배수", "당일", "돌파일 거래량 ÷ 직전 20일 평균거래량"),
    "f_amt5_20":     ("거래대금 5/20", "당일", "최근5일 평균거래대금 ÷ 20일 평균 (v41g 거래량급증 기준과 동일)"),
    "f_close_loc":   ("종가 위치", "당일", "(종가-저가)/(고가-저가), 1이면 고가 마감"),
    "f_gap":         ("시가 갭(%)", "당일", "돌파일 시가 ÷ 전날 종가 - 1"),
    "f_margin":      ("돌파폭(%)", "당일", "돌파일 종가 ÷ 박스상단 - 1 (하방은 ÷ 하단)"),
    "m_kospi_above": ("코스피 20일선 위", "시장", "전날 코스피 종가가 20일선 위면 1"),
    "m_kospi_ret20": ("코스피 20일 수익률(%)", "시장", "전날 기준 코스피 20일 수익률"),
}
PRE_FEATS = [k for k, v in FEATURES.items() if v[1] in ("사전", "시장")]
ALL_FEATS = list(FEATURES.keys())

# ── 단일 규칙 후보 (조합 탐색에 사용) ──
RULES = [
    ("돌파일 거래량 ≥ 2배",            lambda d: d.f_vol_ratio_t >= 2),
    ("돌파일 거래량 ≥ 3배",            lambda d: d.f_vol_ratio_t >= 3),
    ("거래대금 5/20 ≥ 1.5 (v41g)",     lambda d: d.f_amt5_20 >= 1.5),
    ("볼린저 스퀴즈 (폭 하위 20%)",    lambda d: d.f_bbw_rank <= 20),
    ("거래량 말라붙음 (≤ 0.8)",        lambda d: d.f_vol_dryup <= 0.8),
    ("ATR 수축 (≤ 0.8)",               lambda d: d.f_atr_ratio <= 0.8),
    ("OBV 매집 (> 0.1)",               lambda d: d.f_obv_div > 0.1),
    ("저점 상승 (> 0%)",               lambda d: d.f_higher_low > 0),
    ("전날 상단근접 (위치 ≥ 80%)",     lambda d: d.f_pos_prev >= 80),
    ("강한 마감 (종가위치 ≥ 0.7)",     lambda d: d.f_close_loc >= 0.7),
    ("돌파폭 ≥ 3%",                    lambda d: d.f_margin >= 3),
    ("갭상승 돌파 (시가 ≥ +2%)",       lambda d: d.f_gap >= 2),
    ("RSI 50~70",                      lambda d: d.f_rsi_prev.between(50, 70)),
    ("20일선 우상향",                  lambda d: d.f_ma20_slope > 0),
    ("120일 박스",                     lambda d: d.f_box_window == 120),
    ("좁은 박스 (폭 ≤ 20%)",           lambda d: d.f_box_width <= 20),
    ("코스피 20일선 위",               lambda d: d.m_kospi_above == 1),
]


# ═════════════════════════════════════════════════════════
# 데이터 수집
# ═════════════════════════════════════════════════════════
def load_universe(top_n):
    import FinanceDataReader as fdr
    frames = []
    for mk, suf in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
        df = fdr.StockListing(mk)
        df = df.rename(columns={"Symbol": "Code"})
        df["Market"] = mk
        df["Suffix"] = suf
        frames.append(df)
    u = pd.concat(frames, ignore_index=True)
    u["Code"] = u["Code"].astype(str).str.zfill(6)
    u = u[u["Code"].str.match(r"^\d{5}0$")]                       # 우선주 제외
    u = u[~u["Name"].astype(str).str.contains("스팩|SPAC|리츠", na=False)]
    if "Marcap" in u.columns:
        u = u.sort_values("Marcap", ascending=False)
    u = u.head(top_n)
    return u[["Code", "Name", "Market", "Suffix"]].reset_index(drop=True)


def _clean_ohlcv(df):
    need = ["Open", "High", "Low", "Close", "Volume"]
    if df is None or df.empty or not all(c in df.columns for c in need):
        return None
    df = df[need].dropna(subset=["Close"]).copy()
    df = df[(df["Close"] > 0) & (df["High"] >= df["Low"])]
    df = df[~((df["Volume"] <= 0) & (df["High"] == df["Low"]))]   # 거래정지일 제거
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df if len(df) >= 200 else None


def download_prices(universe, years, refresh=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    cache = os.path.join(CACHE_DIR, f"prices_{years}y_{len(universe)}_{tag}.pkl")
    if os.path.exists(cache) and not refresh:
        print(f"  캐시 사용: {cache}")
        with open(cache, "rb") as f:
            return pickle.load(f)

    import yfinance as yf
    prices = {}
    tickers = [c + s for c, s in zip(universe.Code, universe.Suffix)]
    chunk = 40
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        print(f"  yfinance {i + 1}~{i + len(part)} / {len(tickers)}")
        try:
            raw = yf.download(part, period=f"{years}y", auto_adjust=True,
                              progress=False, group_by="ticker", threads=True)
        except Exception as e:
            print(f"    실패: {e}")
            continue
        for tk in part:
            try:
                df = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = _clean_ohlcv(df)
            if df is not None:
                prices[tk[:6]] = df
        time.sleep(0.5)

    # yfinance 누락분은 FinanceDataReader 로 보충
    missing = [c for c in universe.Code if c not in prices]
    if missing:
        import FinanceDataReader as fdr
        start = (pd.Timestamp.today() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        print(f"  FDR 보충 {len(missing)}종목")
        for c in missing:
            try:
                df = _clean_ohlcv(fdr.DataReader(c, start))
                if df is not None:
                    prices[c] = df
            except Exception:
                pass

    with open(cache, "wb") as f:
        pickle.dump(prices, f)
    return prices


def download_kospi(years):
    try:
        import yfinance as yf
        k = yf.download("^KS11", period=f"{years + 1}y", auto_adjust=True, progress=False)
        if isinstance(k.columns, pd.MultiIndex):
            k.columns = k.columns.get_level_values(0)
        s = k["Close"].dropna()
        if len(s) > 100:
            s.index = pd.to_datetime(s.index).tz_localize(None)
            return s
    except Exception:
        pass
    import FinanceDataReader as fdr
    start = (pd.Timestamp.today() - pd.DateOffset(years=years + 1)).strftime("%Y-%m-%d")
    s = fdr.DataReader("KS11", start)["Close"].dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


# ═════════════════════════════════════════════════════════
# 박스 판정 (v41g 규칙의 날짜별 벡터화 버전)
# ═════════════════════════════════════════════════════════
def swing_count(closes, top, bot, hi=0.65, lo=0.35):
    """종가가 박스 상단권(hi↑)과 하단권(lo↓)을 번갈아 오간 횟수 (스크리너와 동일)"""
    if top <= bot:
        return 0
    pos = (np.asarray(closes, dtype=float) - bot) / (top - bot)
    zone = np.where(pos >= hi, 1, np.where(pos <= lo, -1, 0))
    zone = zone[zone != 0]
    if len(zone) < 2:
        return 0
    return int((zone[1:] != zone[:-1]).sum())


def detect_boxes(close, high, low, min_swings=MIN_SWINGS):
    """각 날짜 t 에 대해 v41g _detect_box_range 가 '오늘=t' 일 때 내놓는 박스를 계산.
    반환: box_w(0=박스아님), top, bot, width_pct  (모두 길이 n 배열)"""
    n = len(close)
    cs, hs, ls = pd.Series(close), pd.Series(high), pd.Series(low)
    box_w = np.zeros(n, dtype=int)
    box_top = np.full(n, np.nan)
    box_bot = np.full(n, np.nan)
    box_width = np.full(n, np.nan)
    for w in WINDOWS:                       # 긴 구간부터 → 먼저 통과한 것 채택
        half = w // 2
        top = hs.rolling(w).max().shift(RECENT_EXCL).values
        bot = ls.rolling(w).min().shift(RECENT_EXCL).values
        back = cs.rolling(w - half).mean().shift(RECENT_EXCL).values
        front = cs.rolling(half).mean().shift(RECENT_EXCL + (w - half)).values
        with np.errstate(divide="ignore", invalid="ignore"):
            width = (top - bot) / bot * 100
            drift = np.abs(back - front) / front * 100
        ok = (box_w == 0) & (bot > 0) & (width <= MAX_WIDTH) & (drift <= MAX_DRIFT)
        ok &= ~np.isnan(width) & ~np.isnan(drift)
        if min_swings > 0:
            for t in np.where(ok)[0]:
                s = t - RECENT_EXCL - w + 1
                if swing_count(close[s:t - RECENT_EXCL + 1], top[t], bot[t]) < min_swings:
                    ok[t] = False
        box_w[ok] = w
        box_top[ok] = top[ok]
        box_bot[ok] = bot[ok]
        box_width[ok] = width[ok]
    return box_w, box_top, box_bot, box_width


# ═════════════════════════════════════════════════════════
# 지표
# ═════════════════════════════════════════════════════════
def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(high, low, close, n=14):
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ═════════════════════════════════════════════════════════
# 이벤트 추출
# ═════════════════════════════════════════════════════════
def extract_events(code, name, market, df, kospi, args):
    o = df["Open"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    c = df["Close"].values.astype(float)
    v = df["Volume"].values.astype(float)
    dates = df.index
    n = len(c)
    H = args.horizon
    if n < max(WINDOWS) + RECENT_EXCL + H + 30:
        return []

    box_w, box_top, box_bot, box_width = detect_boxes(c, h, l, args.swings)

    cs, vs = pd.Series(c), pd.Series(v)
    amt = cs * vs
    amt20 = amt.rolling(20).mean().values
    amt5 = amt.rolling(5).mean().values
    vol20_prev = vs.rolling(20).mean().shift(1).values        # 직전 20일 (당일 제외)
    vol10 = vs.rolling(10).mean().values
    ma20 = cs.rolling(20).mean()
    sd20 = cs.rolling(20).std()
    bbw = (4 * sd20 / ma20).values
    ma20 = ma20.values
    rsi = _rsi(cs).values
    atr = _atr(pd.Series(h), pd.Series(l), cs).values
    obv = (np.sign(np.diff(c, prepend=c[0])) * v).cumsum()

    k = kospi.reindex(dates).ffill()
    k_ma20 = k.rolling(20).mean().values
    k_ret20 = (k / k.shift(20) - 1).values * 100
    k = k.values

    events = []
    cool = -1
    min_amt = args.min_amt * 1e8
    for t in range(max(WINDOWS) + RECENT_EXCL + 1, n - H - 1):
        if t <= cool or box_w[t] == 0:
            continue
        top, bot, w = box_top[t], box_bot[t], box_w[t]
        if c[t] > top and c[t - 1] <= top:
            direction = 1
        elif c[t] < bot and c[t - 1] >= bot:
            direction = -1
        else:
            continue
        if not (amt20[t - 1] >= min_amt):
            continue
        cool = t + COOLDOWN

        p = t - 1                                    # 사전징후 기준일
        bs, be = t - RECENT_EXCL - w + 1, t - RECENT_EXCL + 1   # 박스 구간 [bs, be)
        box_vol = v[bs:be].mean()
        box_atr = np.nanmean(atr[bs:be])
        bbw_hist = bbw[max(0, p - 119):p + 1]
        bbw_hist = bbw_hist[~np.isnan(bbw_hist)]
        rng = h[t] - l[t]

        ev = {
            "종목코드": code, "종목명": name, "시장": market,
            "날짜": dates[t], "연도": dates[t].year,
            "방향": "상방돌파" if direction == 1 else "하방이탈",
            "dir": direction,
            "박스상단": round(top), "박스하단": round(bot), "종가": c[t],
            "f_box_window": w,
            "f_box_width": box_width[t],
            "f_pos_prev": (c[p] - bot) / (top - bot) * 100 if top > bot else np.nan,
            "f_vol_dryup": vol10[p] / box_vol if box_vol > 0 else np.nan,
            "f_bbw_rank": (bbw_hist <= bbw[p]).mean() * 100 if len(bbw_hist) >= 60 else np.nan,
            "f_atr_ratio": atr[p] / box_atr if box_atr > 0 else np.nan,
            "f_obv_div": (obv[p] - obv[p - 20]) / v[p - 19:p + 1].sum() if v[p - 19:p + 1].sum() > 0 else np.nan,
            "f_higher_low": (l[p - 19:p + 1].min() / l[p - 39:p - 19].min() - 1) * 100,
            "f_rsi_prev": rsi[p],
            "f_ma20_slope": (ma20[p] / ma20[p - 5] - 1) * 100,
            "f_vol_ratio_t": v[t] / vol20_prev[t] if vol20_prev[t] > 0 else np.nan,
            "f_amt5_20": amt5[t] / amt20[t] if amt20[t] > 0 else np.nan,
            "f_close_loc": (c[t] - l[t]) / rng if rng > 0 else 0.5,
            "f_gap": (o[t] / c[p] - 1) * 100,
            "f_margin": (c[t] / (top if direction == 1 else bot) - 1) * 100,
            "m_kospi_above": float(k[p] > k_ma20[p]) if not np.isnan(k_ma20[p]) else np.nan,
            "m_kospi_ret20": k_ret20[p],
        }

        # ── 결과: 다음날 시가 진입, TP/SL 레이스 ──
        entry = o[t + 1]
        res, days = "기간만료", H
        for j in range(t + 1, t + 1 + H):
            if direction == 1:
                if l[j] <= entry * (1 - args.sl / 100):
                    res, days = "손절", j - t
                    break
                if h[j] >= entry * (1 + args.tp / 100):
                    res, days = "목표달성", j - t
                    break
            else:   # 하방이탈: 추가 하락(-tp%)이 먼저면 '하락지속'
                if h[j] >= entry * (1 + args.sl / 100):
                    res, days = "반등", j - t
                    break
                if l[j] <= entry * (1 - args.tp / 100):
                    res, days = "하락지속", j - t
                    break
        fw = slice(t + 1, t + 1 + H)
        ev.update({
            "진입가(익일시가)": entry,
            "결과": res, "결과일수": days,
            "success": 1 if res == "목표달성" else 0,
            "fwd5(%)": (c[t + 5] / entry - 1) * 100,
            "fwd10(%)": (c[t + 10] / entry - 1) * 100,
            f"fwd{H}(%)": (c[t + H] / entry - 1) * 100,
            f"최대상승{H}(%)": (h[fw].max() / entry - 1) * 100,
            f"최대하락{H}(%)": (l[fw].min() / entry - 1) * 100,
            "fake": int((c[t + 1:t + 6] < top).any()) if direction == 1
                    else int((c[t + 1:t + 6] > bot).any()),
        })
        events.append(ev)
    return events


# ═════════════════════════════════════════════════════════
# 분석
# ═════════════════════════════════════════════════════════
def _buckets(x, q=5):
    x = x.dropna()
    if x.nunique() <= 6:
        return x.astype(str) if x.dtype == object else x, False
    return pd.qcut(x, q, duplicates="drop"), True


def _label(key, is_interval):
    if is_interval:
        return f"{key.left:,.2f} ~ {key.right:,.2f}"
    return f"{key:g}" if isinstance(key, (int, float, np.floating, np.integer)) else str(key)


def effect_table(up, H):
    """상방돌파 이벤트: 성공 vs 실패 그룹 간 징후 차이"""
    rows = []
    s, f = up[up.success == 1], up[up.success == 0]
    for col in ALL_FEATS:
        a, b = s[col].dropna(), f[col].dropna()
        if len(a) < 10 or len(b) < 10:
            continue
        pooled = np.sqrt(((len(a) - 1) * a.var() + (len(b) - 1) * b.var()) / (len(a) + len(b) - 2))
        d = (a.mean() - b.mean()) / pooled if pooled > 0 else 0
        # 상위구간 vs 하위구간 성공률
        bk, is_int = _buckets(up[col])
        g = up.loc[bk.index].groupby(bk, observed=True)["success"].agg(["mean", "size"])
        g = g[g["size"] >= 10]
        hi_lo = (g["mean"].iloc[-1] - g["mean"].iloc[0]) * 100 if len(g) >= 2 else np.nan
        ad = abs(d)
        grade = "강" if ad >= 0.5 else "중" if ad >= 0.2 else "약" if ad >= 0.1 else "거의없음"
        nm, kind, desc = FEATURES[col]
        rows.append({
            "징후": nm, "구분": kind,
            "성공 평균": a.mean(), "실패 평균": b.mean(),
            "성공 중앙값": a.median(), "실패 중앙값": b.median(),
            "효과크기(d)": d, "판정": grade,
            "방향": "높을수록 유리" if d > 0 else "낮을수록 유리",
            "최상위-최하위 구간 성공률차(%p)": hi_lo,
            "설명": desc,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.reindex(out["효과크기(d)"].abs().sort_values(ascending=False).index)
    return out


def bucket_table(up, H):
    base = up.success.mean()
    rows = []
    for col in ALL_FEATS:
        bk, is_int = _buckets(up[col])
        if len(bk) == 0:
            continue
        sub = up.loc[bk.index]
        for key, g in sub.groupby(bk, observed=True):
            if len(g) < 5:
                continue
            rows.append({
                "징후": FEATURES[col][0], "구간": _label(key, is_int), "건수": len(g),
                "성공률(%)": g.success.mean() * 100,
                "Lift(기준대비)": g.success.mean() / base if base > 0 else np.nan,
                "가짜돌파율(%)": g.fake.mean() * 100,
                f"평균 fwd{H}(%)": g[f"fwd{H}(%)"].mean(),
                f"중앙 fwd{H}(%)": g[f"fwd{H}(%)"].median(),
            })
    return pd.DataFrame(rows)


def direction_table(ev):
    """박스 이탈 전 사전징후 → 위로 나갈지/아래로 나갈지"""
    base = (ev.dir == 1).mean()
    rows = []
    for col in PRE_FEATS:
        bk, is_int = _buckets(ev[col])
        if len(bk) == 0:
            continue
        sub = ev.loc[bk.index]
        for key, g in sub.groupby(bk, observed=True):
            if len(g) < 5:
                continue
            up_ratio = (g.dir == 1).mean()
            rows.append({
                "사전징후": FEATURES[col][0], "구간": _label(key, is_int), "건수": len(g),
                "상방돌파 비율(%)": up_ratio * 100,
                "Lift(기준대비)": up_ratio / base if base > 0 else np.nan,
                "상방+성공 비율(%)": ((g.dir == 1) & (g.success == 1)).mean() * 100,
            })
    return pd.DataFrame(rows)


def rule_table(up, H, min_n):
    split = up["날짜"].quantile(0.7)
    is_mask = up["날짜"] <= split
    base = up.success.mean()
    masks = {}
    for nm, fn in RULES:
        try:
            masks[nm] = fn(up).fillna(False).values
        except Exception:
            pass
    rows = []
    names = list(masks)
    for r in (1, 2, 3):
        for combo in itertools.combinations(names, r):
            m = np.logical_and.reduce([masks[x] for x in combo])
            n = int(m.sum())
            if n < min_n:
                continue
            g = up[m]
            gi, go = up[m & is_mask.values], up[m & ~is_mask.values]
            rows.append({
                "조건": " + ".join(combo), "조건수": r, "건수": n,
                "성공률(%)": g.success.mean() * 100,
                "Lift": g.success.mean() / base if base > 0 else np.nan,
                "가짜돌파율(%)": g.fake.mean() * 100,
                f"평균 fwd{H}(%)": g[f"fwd{H}(%)"].mean(),
                "IS 건수": len(gi), "IS 성공률(%)": gi.success.mean() * 100 if len(gi) else np.nan,
                "OOS 건수": len(go), "OOS 성공률(%)": go.success.mean() * 100 if len(go) else np.nan,
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out, split
    out["보수적 성공률(%)"] = out[["IS 성공률(%)", "OOS 성공률(%)"]].min(axis=1)
    out = out[out["OOS 건수"] >= max(5, min_n // 4)]
    return out.sort_values("보수적 성공률(%)", ascending=False).head(200), split


def year_table(ev, H):
    rows = []
    for y, g in ev.groupby("연도"):
        u, d = g[g.dir == 1], g[g.dir == -1]
        rows.append({
            "연도": y, "상방돌파 건수": len(u), "하방이탈 건수": len(d),
            "상방 성공률(%)": u.success.mean() * 100 if len(u) else np.nan,
            "상방 가짜돌파율(%)": u.fake.mean() * 100 if len(u) else np.nan,
            f"상방 평균 fwd{H}(%)": u[f"fwd{H}(%)"].mean() if len(u) else np.nan,
            f"하방 평균 fwd{H}(%)": d[f"fwd{H}(%)"].mean() if len(d) else np.nan,
        })
    return pd.DataFrame(rows)


def summary_table(ev, args, n_stocks, split):
    H = args.horizon
    up, dn = ev[ev.dir == 1], ev[ev.dir == -1]
    rows = [
        ("분석 기간", f"{ev['날짜'].min():%Y-%m-%d} ~ {ev['날짜'].max():%Y-%m-%d}"),
        ("분석 종목 수", n_stocks),
        ("박스 이탈 이벤트", len(ev)),
        ("  상방돌파", len(up)),
        ("  하방이탈", len(dn)),
        ("상방돌파 비율(%)", round(len(up) / len(ev) * 100, 1) if len(ev) else None),
        ("", ""),
        ("[상방돌파 결과]  성공 기준", f"익일시가 진입 → {H}일 안에 +{args.tp}% 먼저 (-{args.sl}% 먼저 닿으면 실패)"),
        ("성공률(%)  ← 모든 비교의 기준선", round(up.success.mean() * 100, 1) if len(up) else None),
        ("손절 비율(%)", round((up["결과"] == "손절").mean() * 100, 1) if len(up) else None),
        ("기간만료 비율(%)", round((up["결과"] == "기간만료").mean() * 100, 1) if len(up) else None),
        ("가짜돌파율(%)  (5일 내 박스 복귀)", round(up.fake.mean() * 100, 1) if len(up) else None),
        (f"평균 fwd{H}(%)", round(up[f"fwd{H}(%)"].mean(), 2) if len(up) else None),
        (f"중앙 fwd{H}(%)", round(up[f"fwd{H}(%)"].median(), 2) if len(up) else None),
        ("", ""),
        ("[하방이탈 결과]", ""),
        (f"평균 fwd{H}(%)", round(dn[f"fwd{H}(%)"].mean(), 2) if len(dn) else None),
        (f"하락지속(-{args.tp}% 먼저) 비율(%)", round((dn["결과"] == "하락지속").mean() * 100, 1) if len(dn) else None),
        ("", ""),
        ("규칙조합 IS/OOS 분리일", f"{split:%Y-%m-%d}" if split is not None else "-"),
        ("파라미터", f"top={args.top}, years={args.years}, tp={args.tp}, sl={args.sl}, "
                     f"horizon={H}, min_amt={args.min_amt}억"),
    ]
    return pd.DataFrame(rows, columns=["항목", "값"])


GUIDE = [
    ("이 파일을 읽는 순서", ""),
    ("1_요약", "기준선 확인 — 박스 상방돌파를 아무 조건 없이 샀을 때의 성공률. 모든 비교는 이 숫자 대비."),
    ("2_징후효과", "성공한 돌파와 실패한 돌파에서 각 징후가 얼마나 달랐나. 효과크기(d) 0.2 이상이면 쓸만한 징후, 0.5 이상이면 강함."),
    ("3_구간별성공률", "징후 값을 5구간으로 나눠 구간별 성공률. Lift 1.2 = 기준선보다 20% 더 잘 됨."),
    ("4_사전징후_방향", "박스를 벗어나기 전날까지의 징후로 '위로 나갈지 아래로 나갈지'를 가늠. 돌파대기 종목 고를 때 사용."),
    ("5_규칙조합", "징후 1~3개 조합의 성공률. IS(앞 70% 기간)와 OOS(뒤 30%) 둘 다 높아야 믿을 수 있음 → '보수적 성공률' 순 정렬."),
    ("6_연도별", "해마다 결과가 비슷한지. 특정 연도만 좋으면 시장 국면 효과일 가능성."),
    ("8_왕복기준비교", "(--compare 시) 박스 인정 기준별 성과. 성공률·가짜돌파율이 가장 나은 기준을 스크리너 BOX_MIN_SWINGS 에 반영."),
    ("7_이벤트원본", "모든 이벤트 한 줄씩. 차트로 직접 확인할 때 사용."),
    ("", ""),
    ("주의", "현재 상장 종목만 대상이라 상장폐지 종목이 빠져 결과가 약간 낙관적. 수급(외국인·기관)은 미포함."),
    ("주의", "건수 30건 미만 조합은 우연일 가능성이 큼. 신호로 쓰기 전에 모의매매로 재확인."),
]


def write_excel(path, sheets):
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
        from openpyxl.styles import Alignment, Font, PatternFill
        hdr_fill = PatternFill("solid", fgColor="1F3864")
        for name, ws in xw.sheets.items():
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = hdr_fill
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.freeze_panes = "B2" if name != "0_읽는법" else None
            for col in ws.columns:
                vals = [str(c.value) if c.value is not None else "" for c in col[:300]]
                width = min(60, max(8, max(len(v) for v in vals) * 1.3))
                ws.column_dimensions[col[0].column_letter].width = width
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, float):
                        cell.number_format = "#,##0.00"


# ═════════════════════════════════════════════════════════
def run(args, prices=None, universe=None, kospi=None):
    t0 = time.time()
    if universe is None:
        print("[1/4] 종목 유니버스")
        universe = load_universe(args.top)
    if prices is None:
        print("[2/4] 가격 데이터")
        prices = download_prices(universe, args.years, args.refresh)
    if kospi is None:
        kospi = download_kospi(args.years)
    print(f"  가격 확보 {len(prices)} / {len(universe)} 종목")

    print(f"[3/4] 박스 이탈 이벤트 추출 (왕복 기준 {args.swings}회)")
    meta = universe.set_index("Code")
    ev = collect_events(prices, meta, kospi, args, verbose=True)
    if ev.empty:
        print("이벤트가 없습니다. --top 이나 --years 를 늘려 보세요.")
        return None
    ev = ev.sort_values("날짜").reset_index(drop=True)
    up = ev[ev.dir == 1].copy()
    H = args.horizon
    print(f"  이벤트 {len(ev)}건 (상방 {len(up)} / 하방 {len(ev) - len(up)})")

    print("[4/4] 분석·리포트")
    min_n = max(30, int(len(up) * 0.02))
    rules, split = rule_table(up, H, min_n) if len(up) >= 50 else (pd.DataFrame(), None)
    raw = ev.drop(columns=["dir"]).rename(columns={k: v[0] for k, v in FEATURES.items()})
    raw = raw.rename(columns={"success": "성공(1)", "fake": "가짜돌파(1)"})
    if args.compare:
        print("  왕복 기준 비교 (0/2/3/4회)...")
        sheets_cmp = compare_swings(prices, meta, kospi, args, ev)
    sheets = {
        "0_읽는법": pd.DataFrame(GUIDE, columns=["시트", "내용"]),
        "1_요약": summary_table(ev, args, len(prices), split),
        "2_징후효과": effect_table(up, H),
        "3_구간별성공률": bucket_table(up, H),
        "4_사전징후_방향": direction_table(ev),
        "5_규칙조합": rules,
        "6_연도별": year_table(ev, H),
        "7_이벤트원본": raw,
    }
    if args.compare:
        sheets["8_왕복기준비교"] = sheets_cmp
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"box_breakout_study_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    write_excel(out, sheets)

    # 콘솔 요약
    print("\n" + "=" * 60)
    print(f"상방돌파 {len(up)}건 | 기준 성공률 {up.success.mean() * 100:.1f}% | "
          f"가짜돌파 {up.fake.mean() * 100:.1f}% | 평균 fwd{H} {up[f'fwd{H}(%)'].mean():+.2f}%")
    eff = sheets["2_징후효과"]
    if not eff.empty:
        print("\n효과 큰 징후 TOP 5")
        for _, r in eff.head(5).iterrows():
            print(f"  {r['징후']:<18} d={r['효과크기(d)']:+.2f} ({r['판정']}, {r['방향']})")
    if not rules.empty:
        print("\n보수적 성공률 상위 조합 TOP 5")
        for _, r in rules.head(5).iterrows():
            print(f"  {r['보수적 성공률(%)']:.1f}% (n={r['건수']}) {r['조건']}")
    print(f"\n저장: {out}  ({time.time() - t0:.0f}초)")
    return out


def collect_events(prices, meta, kospi, args, verbose=False):
    events = []
    for i, (code, df) in enumerate(prices.items(), 1):
        nm = meta.loc[code, "Name"] if code in meta.index else code
        mk = meta.loc[code, "Market"] if code in meta.index else ""
        try:
            events += extract_events(code, nm, mk, df, kospi, args)
        except Exception as e:
            print(f"    {code} 오류: {e}")
        if verbose and i % 100 == 0:
            print(f"    {i}/{len(prices)} 종목, 이벤트 {len(events)}건")
    return pd.DataFrame(events)


def compare_swings(prices, meta, kospi, args, ev_main):
    """박스 인정 기준(왕복 횟수)별로 상방돌파 성과 비교 → 스크리너 BOX_MIN_SWINGS 결정용"""
    import copy
    H = args.horizon
    rows = []
    for s in (0, 2, 3, 4):
        if s == args.swings:
            ev = ev_main
        else:
            a = copy.copy(args)
            a.swings = s
            ev = collect_events(prices, meta, kospi, a)
        up = ev[ev.dir == 1] if len(ev) else ev
        dn = ev[ev.dir == -1] if len(ev) else ev
        rows.append({
            "왕복 기준(회)": s,
            "비고": "수정 전 규칙과 동일" if s == 0 else ("현재 기본값" if s == MIN_SWINGS else ""),
            "상방돌파 건수": len(up), "하방이탈 건수": len(dn),
            "상방 성공률(%)": up.success.mean() * 100 if len(up) else np.nan,
            "상방 가짜돌파율(%)": up.fake.mean() * 100 if len(up) else np.nan,
            f"상방 평균 fwd{H}(%)": up[f"fwd{H}(%)"].mean() if len(up) else np.nan,
            f"상방 중앙 fwd{H}(%)": up[f"fwd{H}(%)"].median() if len(up) else np.nan,
            f"하방 평균 fwd{H}(%)": dn[f"fwd{H}(%)"].mean() if len(dn) else np.nan,
        })
    return pd.DataFrame(rows)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="박스권 돌파 징후 이벤트 스터디")
    ap.add_argument("--top", type=int, default=500, help="시총 상위 N 종목 (기본 500)")
    ap.add_argument("--years", type=int, default=5, help="분석 기간(년) (기본 5)")
    ap.add_argument("--tp", type=float, default=10, help="성공 기준 상승률%% (기본 10)")
    ap.add_argument("--sl", type=float, default=7, help="손절 기준%% (기본 7, 스크리너와 동일)")
    ap.add_argument("--horizon", type=int, default=20, help="결과 관찰 기간(거래일) (기본 20)")
    ap.add_argument("--min-amt", type=float, default=10, help="최소 20일 평균 거래대금(억) (기본 10)")
    ap.add_argument("--swings", type=int, default=MIN_SWINGS,
                    help="박스 인정 최소 왕복 횟수 (기본 3, 0=수정 전 규칙)")
    ap.add_argument("--compare", action="store_true", help="왕복 기준 0/2/3/4회 비교 시트 추가")
    ap.add_argument("--refresh", action="store_true", help="가격 캐시 무시")
    return ap.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run(parse_args())
