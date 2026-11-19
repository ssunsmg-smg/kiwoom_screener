# -*- coding: utf-8 -*-
"""
박스 돌파 눌림매수 — 가상(페이퍼) 매매 장부 (box_paper_trader.py v1)
──────────────────────────────────────────────────────────
매일 장 마감 후 1회 실행. KIS 주문을 내지 않는다.
  · 기존 퀀트 시스템은 계좌의 모든 보유종목을 자기 포지션으로 동기화
    (sync_positions_from_broker) 하므로, 같은 계좌에서 실제 주문을 내면
    장중 워크플로우의 -7% 손절 / 트레일링 익절이 박스 종목에 걸린다.
  · 그래서 실제 일봉(시가·고가·저가·종가)으로 체결/손절/익절을 판정하는
    가상 장부로 먼저 '앞으로의 성과'를 검증한다. (백테스트와 같은 규칙)

처리 순서 (오늘 일봉까지 반영)
  ① 보유 포지션: 갭 손절 → 장중 손절 → 익절(+20%) → 40거래일 만기
  ② 대기 주문: 종가가 실패기준가(박스상단×0.97) 아래 → 취소
              저가 ≤ 지정가(박스상단×1.02) → 체결 (시가가 더 낮으면 시가)
              체결 당일 손절가 터치도 반영 (보수적)
              돌파 후 10거래일 지나면 만료
  ③ 새 후보: box_pullback_scanner 로 점수 3점↑ 눌림대기 종목을 지정가 주문으로 등록
  ④ box_state/ledger.json 저장 + box_state/report.md 갱신 + 텔레그램 요약

실행
  python box_paper_trader.py              # 기본 시총 상위 500
  python box_paper_trader.py --top 1000
  python box_paper_trader.py --no-telegram
"""
import argparse
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

import box_breakout_study as bs
import box_pullback_scanner as sc

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "box_state")
LEDGER = os.path.join(STATE_DIR, "ledger.json")
REPORT = os.path.join(STATE_DIR, "report.md")

PER_TRADE = 1_000_000      # 종목당 가상 매수금액
MAX_SLOTS = 10             # 보유 + 대기 주문 합계 상한
COST = 0.0025              # 왕복 비용 (수수료+세금+슬리피지)


# ───────────────────────────────────────── 장부
def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    return {"created": datetime.now().strftime("%Y-%m-%d"), "orders": [], "positions": [], "closed": [],
            "rules": {"entry": "박스상단x1.02 지정가", "sl": sc.SL, "tp": sc.TP, "hold": sc.HOLD,
                      "min_score": sc.MIN_SCORE, "per_trade": PER_TRADE, "cost": COST}}


def save_ledger(L):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(L, f, ensure_ascii=False, indent=1, default=str)


def _bars_after(df, date_str):
    return df[df.index > pd.Timestamp(date_str)]


def _close_pos(L, p, date, px, reason, log):
    ret = px / p["entry"] - 1 - COST
    L["closed"].append({**p, "exit_date": str(pd.Timestamp(date).date()), "exit": round(px, 1),
                        "reason": reason, "ret": round(ret, 4), "pnl": round(PER_TRADE * ret)})
    log.append(f"{'🟢' if ret > 0 else '🔴'} 청산 {p['name']}({p['code']}) {reason} {ret:+.1%}")


# ───────────────────────────────────────── ① 포지션
def step_position(p, df, L, log):
    """p 를 오늘까지 진행. 청산되면 True."""
    for d, r in _bars_after(df, p["last"]).iterrows():
        p["days"] += 1
        p["last"] = str(d.date())
        o, h, l, c = r["Open"], r["High"], r["Low"], r["Close"]
        if o <= p["stop"]:
            _close_pos(L, p, d, o, "갭손절", log); return True
        if l <= p["stop"]:
            _close_pos(L, p, d, p["stop"], "손절", log); return True
        if h >= p["target"]:
            _close_pos(L, p, d, max(p["target"], o), "익절", log); return True
        if p["days"] >= sc.HOLD:
            _close_pos(L, p, d, c, "만기", log); return True
        p["now"] = round(c, 1)
    return False


# ───────────────────────────────────────── ② 대기 주문
def step_order(o_, df, L, log):
    """'open' 유지면 None, 체결이면 새 포지션 dict, 취소/만료면 False"""
    bars = df[df.index > pd.Timestamp(o_["breakout"])]
    for k, (d, r) in enumerate(bars.iterrows(), start=1):
        if d <= pd.Timestamp(o_["last"]):
            continue
        o_["last"] = str(d.date())
        if k > sc.LOOKBACK:
            log.append(f"⌛ 만료 {o_['name']}({o_['code']}) — 10일 안에 눌림 없음"); return False
        if r["Close"] < o_["fail"] and not (r["Low"] <= o_["limit"]):
            log.append(f"✖ 취소 {o_['name']}({o_['code']}) — 박스 복귀"); return False
        if r["Close"] < o_["fail"]:           # 같은 날 눌림 + 박스 복귀 → 백테스트와 같이 미체결 처리
            log.append(f"✖ 취소 {o_['name']}({o_['code']}) — 눌림 후 박스 복귀"); return False
        if r["Low"] <= o_["limit"]:
            e = float(min(o_["limit"], r["Open"]))
            p = {"code": o_["code"], "name": o_["name"], "score": o_["score"], "breakout": o_["breakout"],
                 "entry_date": str(d.date()), "entry": round(e, 1), "qty": int(PER_TRADE // e),
                 "stop": round(e * (1 - sc.SL), 1), "target": round(e * (1 + sc.TP), 1),
                 "days": 0, "last": str((d - pd.Timedelta(days=1)).date()), "now": round(r["Close"], 1)}
            log.append(f"✅ 체결 {p['name']}({p['code']}) {e:,.0f}원 · 손절 {p['stop']:,.0f} · 목표 {p['target']:,.0f}")
            return p
    return None


# ───────────────────────────────────────── 리포트
def stats(L):
    c = pd.DataFrame(L["closed"])
    if c.empty:
        return {"closed": 0}
    return {"closed": len(c), "win": float((c.ret > 0).mean()), "avg": float(c.ret.mean()),
            "pnl": int(c.pnl.sum()), "best": float(c.ret.max()), "worst": float(c.ret.min())}


def write_report(L, today):
    s = stats(L)
    lines = [f"# 박스 눌림매수 가상장부 — {today}", "",
             f"시작일 {L['created']} · 종목당 {PER_TRADE:,}원 · 손절 -{sc.SL:.0%} / 익절 +{sc.TP:.0%} / {sc.HOLD}일", "",
             "## 누적 성과"]
    if s["closed"]:
        lines += [f"- 청산 {s['closed']}건 · 승률 {s['win']:.0%} · 평균 {s['avg']:+.2%} · 누적손익 {s['pnl']:+,}원",
                  f"- 백테스트 기대값 +1.2~3.5%/거래, 승률 약 47% — 30건 이상 쌓인 뒤 비교할 것"]
    else:
        lines.append("- 아직 청산 없음")
    lines += ["", "## 보유", "| 종목 | 매수일 | 매수가 | 현재가 | 수익률 | 손절 | 목표 | 보유일 |", "|---|---|---|---|---|---|---|---|"]
    for p in L["positions"]:
        lines.append(f"| {p['name']}({p['code']}) | {p['entry_date']} | {p['entry']:,.0f} | {p['now']:,.0f} | "
                     f"{p['now'] / p['entry'] - 1:+.1%} | {p['stop']:,.0f} | {p['target']:,.0f} | {p['days']} |")
    lines += ["", "## 대기 주문 (지정가)", "| 종목 | 돌파일 | 점수 | 지정가 | 실패기준(종가) |", "|---|---|---|---|---|"]
    for o in L["orders"]:
        lines.append(f"| {o['name']}({o['code']}) | {o['breakout']} | {o['score']} | {o['limit']:,.0f} | {o['fail']:,.0f} |")
    lines += ["", "## 최근 청산 10건", "| 종목 | 매수일 | 청산일 | 사유 | 수익률 |", "|---|---|---|---|---|"]
    for c in L["closed"][-10:][::-1]:
        lines.append(f"| {c['name']}({c['code']}) | {c['entry_date']} | {c['exit_date']} | {c['reason']} | {c['ret']:+.1%} |")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def telegram(text):
    path = os.path.join(BASE, "telegram_config.json")
    if not os.path.exists(path):
        print("  (텔레그램 설정 없음 — 알림 생략)")
        return
    import requests
    cfg = json.load(open(path, encoding="utf-8"))
    try:
        requests.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                      data={"chat_id": cfg["chat_id"], "text": text}, timeout=10)
    except Exception as e:  # noqa
        print(f"  텔레그램 실패: {e}")


# ───────────────────────────────────────── 메인
def run(prices, meta, L, today=None):
    log = []
    # ① 보유 포지션
    keep = []
    for p in L["positions"]:
        df = prices.get(p["code"])
        if df is None:
            log.append(f"⚠ {p['name']}({p['code']}) 가격 없음 — 오늘은 건너뜀"); keep.append(p); continue
        if not step_position(p, df.dropna(), L, log):
            keep.append(p)
    L["positions"] = keep
    # ② 대기 주문
    still = []
    for o_ in L["orders"]:
        df = prices.get(o_["code"])
        if df is None:
            still.append(o_); continue
        res = step_order(o_, df.dropna(), L, log)
        if res is None:
            still.append(o_)
        elif res is not False:
            if not step_position(res, df.dropna(), L, log):   # 체결 당일 손절 등
                L["positions"].append(res)
    L["orders"] = still
    # ③ 새 후보
    buy, allb = sc.scan(prices, meta)
    held = {p["code"] for p in L["positions"]} | {o["code"] for o in L["orders"]}
    slots = MAX_SLOTS - len(held)
    for r in (buy.to_dict("records") if len(buy) else []):
        if slots <= 0:
            break
        if r["종목코드"] in held:
            continue
        last = str(prices[r["종목코드"]].dropna().index[-1].date())
        L["orders"].append({"code": r["종목코드"], "name": r["종목명"], "score": int(r["점수"]),
                            "breakout": str(r["돌파일"]), "limit": float(r["매수지정가"]),
                            "fail": float(r["실패기준가(종가)"]), "reason": r["충족조건"],
                            "created": last, "last": last})
        log.append(f"📝 신규 지정가 {r['종목명']}({r['종목코드']}) {r['매수지정가']:,}원 · 점수 {r['점수']} ({r['충족조건']})")
        slots -= 1
    return log, allb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    L = load_ledger()
    uni = bs.load_universe(a.top)
    meta = {r.Code: (r.Name, r.Market) for r in uni.itertuples()}
    prices = bs.download_prices(uni, 1, refresh=True)
    # 유니버스에서 빠진 보유/대기 종목도 가격 확보
    need = {x["code"] for x in L["positions"] + L["orders"]} - set(prices)
    if need:
        import FinanceDataReader as fdr
        start = (pd.Timestamp.today() - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
        for c in need:
            try:
                d = bs._clean_ohlcv(fdr.DataReader(c, start))
                if d is not None:
                    prices[c] = d
            except Exception:
                pass
    last = max(df.index[-1] for df in prices.values())
    today = str(last.date())
    log, allb = run(prices, meta, L, today)
    L["updated"] = today
    save_ledger(L)
    write_report(L, today)
    s = stats(L)
    head = f"📦 박스 눌림매수 가상장부 {today}\n보유 {len(L['positions'])} · 대기 {len(L['orders'])} · 최근돌파 {len(allb)}"
    if s["closed"]:
        head += f"\n누적 {s['closed']}건 승률 {s['win']:.0%} 평균 {s['avg']:+.2%} ({s['pnl']:+,}원)"
    msg = head + ("\n\n" + "\n".join(log) if log else "\n\n오늘 변동 없음")
    print(msg)
    if not a.no_telegram:
        telegram(msg)


if __name__ == "__main__":
    main()
