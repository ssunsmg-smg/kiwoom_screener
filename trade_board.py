# -*- coding: utf-8 -*-
"""
매매 현황판 (trade_board.py) — 여러 전략의 매수·매도 / 보유 / 손익을 한 화면에
──────────────────────────────────────────────────────────
· 각 전략은 '공통 형식'으로 데이터를 내놓기만 하면 된다 (어댑터 함수 하나씩).
    거래(trade): time, strategy, mode, code, name, side('매수'|'매도'), qty, price, amount, fee, reason, pnl, ret
    보유(position): strategy, mode, code, name, qty, avg, now, buy_time, stop, target, days
    실행(run): strategy, run_at, data_date, ok, detail
· 결과: box_state/trade_board.html — 파일 하나, 인터넷 없이 열림, 전략·종목·기간 필터
· 새 전략 추가: SOURCES 에 (이름, 로더함수) 한 줄 추가

단독 실행: python trade_board.py
"""
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "box_state", "trade_board.html")
COST_BUY, COST_SELL = 0.00015, 0.00015 + 0.0020   # 화면 표시용 수수료·세금 추정 (매도에 거래세 포함)


# ═════════════════════════════════════════ 어댑터
def load_box_paper(path=os.path.join(BASE, "box_state")):
    """박스 눌림매수 가상장부 → 공통 형식 (일봉 판정이라 시각은 '장중' 표기)"""
    lp = os.path.join(path, "ledger.json")
    if not os.path.exists(lp):
        return [], [], []
    L = json.load(open(lp, encoding="utf-8"))
    S, M = "박스 눌림매수", "가상"
    trades, poss = [], []
    for c in L.get("closed", []) + L.get("positions", []):
        qty = c.get("qty") or max(1, int(1_000_000 // c["entry"]))
        amt = qty * c["entry"]
        trades.append({"time": f"{c['entry_date']} 장중", "strategy": S, "mode": M, "code": c["code"],
                       "name": c["name"], "side": "매수", "qty": qty, "price": c["entry"], "amount": round(amt),
                       "fee": round(amt * COST_BUY), "reason": f"눌림 지정가 (점수 {c.get('score', '-')})",
                       "pnl": None, "ret": None})
    for c in L.get("closed", []):
        qty = c.get("qty") or max(1, int(1_000_000 // c["entry"]))
        amt = qty * c["exit"]
        pnl = qty * (c["exit"] - c["entry"]) - qty * c["entry"] * COST_BUY - amt * COST_SELL
        trades.append({"time": f"{c['exit_date']} {'시가' if c['reason'] == '갭손절' else ('종가' if c['reason'] == '만기' else '장중')}",
                       "strategy": S, "mode": M, "code": c["code"], "name": c["name"], "side": "매도",
                       "qty": qty, "price": c["exit"], "amount": round(amt), "fee": round(amt * COST_SELL),
                       "reason": c["reason"], "pnl": round(pnl), "ret": round(c["ret"], 4)})
    for p in L.get("positions", []):
        poss.append({"strategy": S, "mode": M, "code": p["code"], "name": p["name"],
                     "qty": p.get("qty") or max(1, int(1_000_000 // p["entry"])), "avg": p["entry"], "now": p["now"],
                     "buy_time": p["entry_date"], "stop": p["stop"], "target": p["target"], "days": p["days"]})
    for o in L.get("orders", []):
        poss.append({"strategy": S, "mode": M, "code": o["code"], "name": o["name"], "qty": 0,
                     "avg": o["limit"], "now": None, "buy_time": None, "stop": None, "target": None,
                     "days": None, "pending": True, "note": f"지정가 {o['limit']:,.0f} 대기 · 실패기준 {o['fail']:,.0f}"})
    runs = []
    rp = os.path.join(path, "runs.json")
    if os.path.exists(rp):
        for r in json.load(open(rp, encoding="utf-8"))[-30:]:
            runs.append({"strategy": S, "run_at": r["run_at"], "data_date": r["data_date"],
                         "ok": bool(r.get("ok", True)) and r.get("n_prices", 0) >= 400,
                         "detail": f"가격 {r.get('n_prices', 0)}종목 · 돌파 {r.get('n_breakouts', 0)} · "
                                   f"신규 {r.get('n_new', 0)} · 체결 {r.get('n_fills', 0)} · 청산 {r.get('n_exits', 0)}"})
    return trades, poss, runs


def _pick(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _ymd(x):
    x = str(x or "")
    return f"{x[:4]}-{x[4:6]}-{x[6:8]}" if len(x) == 8 and x.isdigit() else x


def load_kiwoom(path=os.path.join(BASE, "trade_state_kiwoom")):
    """키움 퀀트 v41f 자동매매 → 공통 형식
      · kiwoom_positions.json  {"positions": {코드: {...}}, "updated_at": "..."}  → 보유
      · *pending*.json         {"date": "YYYYMMDD", "candidates": {코드: {status, tries, first_seen,
                                 last_check, last_detail}}}                       → 매수 후보·게이트 상태
      · 체결 로그(*trade*/*fill*/*order*.json 리스트)가 있으면 매수·매도 내역으로 옮김
    """
    import glob
    if not os.path.isdir(path):
        return [], [], []
    S, M = "퀀트 v41f", "키움"
    trades, poss, runs = [], [], []
    updated = None
    pp = os.path.join(path, "kiwoom_positions.json")
    if not os.path.exists(pp):
        pp = os.path.join(path, "positions.json")
    if os.path.exists(pp):
        J = json.load(open(pp, encoding="utf-8"))
        updated = J.get("updated_at") if isinstance(J, dict) else None
        P = J.get("positions", J) if isinstance(J, dict) else {}
        for code, p in (P.items() if isinstance(P, dict) else []):
            if not isinstance(p, dict):
                continue
            qty = int(float(_pick(p, "qty", "quantity", "hldg_qty", "holding_qty", default=0)))
            avg = float(_pick(p, "avg_price", "avg", "buy_price", "pchs_avg_pric", "price", default=0))
            poss.append({"strategy": S, "mode": M, "code": code, "name": _pick(p, "name", "stock_name", default=code),
                         "qty": qty, "avg": avg, "now": _pick(p, "now", "cur_price", "current_price", "prpr"),
                         "buy_time": _pick(p, "buy_time", "buy_date", "entry_date"),
                         "stop": _pick(p, "stop", "stop_price"), "target": _pick(p, "target", "target_price"),
                         "days": _pick(p, "days", "hold_days")})
    # 매수 후보 · 게이트 상태 (가장 최근 pending 파일)
    pend = sorted(glob.glob(os.path.join(path, "*pending*.json")))
    errors = []
    if pend:
        J = json.load(open(pend[-1], encoding="utf-8"))
        day = _ymd(J.get("date", ""))
        held = {p["code"] for p in poss}
        last_check = None
        for code, c in (J.get("candidates") or {}).items():
            det = c.get("last_detail") or {}
            err = det.get("error") if isinstance(det, dict) else None
            if err:
                errors.append(f"{code} {err}")
            last_check = max(last_check or "", c.get("last_check") or "")
            if code in held:
                continue
            msg = err or (", ".join(f"{k} {v}" for k, v in det.items()) if isinstance(det, dict) else str(det))
            poss.append({"strategy": S, "mode": M, "code": code, "name": c.get("name", code), "qty": 0, "avg": None,
                         "now": None, "buy_time": None, "stop": None, "target": None, "days": None, "pending": True,
                         "note": f"매수 후보 · 게이트 {c.get('status', '-')} · {c.get('tries', 0)}회 확인 · "
                                 f"최근 {c.get('last_check', '-')} · {msg or '대기'}"})
        runs.append({"strategy": S, "run_at": f"{day} {last_check or ''}".strip(), "data_date": day,
                     "ok": not errors,
                     "detail": (" / ".join(errors) if errors else
                                f"후보 {len(J.get('candidates') or {})}종목 확인 · 보유 {len([p for p in poss if not p.get('pending')])}종목")})
    elif updated:
        runs.append({"strategy": S, "run_at": updated[:16], "data_date": updated[:10], "ok": True,
                     "detail": f"보유 {len(poss)}종목 동기화"})
    # 체결 로그 (있을 때만)
    for f in sorted(glob.glob(os.path.join(path, "*trade*.json")) + glob.glob(os.path.join(path, "*fill*.json"))
                    + glob.glob(os.path.join(path, "*order*.json"))):
        try:
            J = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa
            continue
        rows = J if isinstance(J, list) else (J.get("trades") or J.get("fills") or J.get("orders") or [])
        for t in rows if isinstance(rows, list) else []:
            side = str(_pick(t, "side", "type", "action", default=""))
            side = "매수" if side.lower() in ("buy", "b", "매수", "2") else ("매도" if side.lower() in ("sell", "s", "매도", "1") else side)
            qty = int(float(_pick(t, "qty", "quantity", "filled_qty", default=0)))
            px = float(_pick(t, "price", "filled_price", "avg_price", default=0))
            trades.append({"time": str(_pick(t, "time", "datetime", "filled_at", "date", default="")), "strategy": S,
                           "mode": f"{M} {t.get('mode', '')}".strip(), "code": str(_pick(t, "code", "stock_code", default="")),
                           "name": _pick(t, "name", "stock_name", default=""), "side": side, "qty": qty, "price": px,
                           "amount": round(qty * px), "fee": _pick(t, "fee", "commission", default=None),
                           "reason": _pick(t, "reason", "note", default=""), "pnl": _pick(t, "pnl", "profit"),
                           "ret": _pick(t, "ret", "return")})
    return trades, poss, runs


SOURCES = [("퀀트 v41f (키움)", load_kiwoom), ("박스 눌림매수", load_box_paper)]
# 전략 색은 이 순서로 고정 (파랑 → 주황 → 초록 → 노랑). 새 전략은 끝에 추가 — 기존 전략 색이 바뀌지 않게
STRATEGY_ORDER = ["퀀트 v41f", "박스 눌림매수"]


# ═════════════════════════════════════════ 화면
def build(out=OUT):
    T, P, R = [], [], []
    for _, fn in SOURCES:
        try:
            t, p, r = fn()
            T += t; P += p; R += r
        except Exception as e:  # noqa
            R.append({"strategy": _, "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "data_date": "-",
                      "ok": False, "detail": f"데이터 읽기 실패: {e}"})
    data = json.dumps({"trades": T, "positions": P, "runs": R, "order": STRATEGY_ORDER,
                       "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("/*DATA*/null", data))
    return out


PAGE = r"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>매매 현황판</title>
<style>
:root{--bg:#f4f4f2;--sf:#fcfcfb;--t1:#0b0b0b;--t2:#52514e;--t3:#8a8984;--ln:#e4e3df;--gr:#ecebe7;
--up:#e34948;--dn:#2a78d6;--good:#0ca30c;--bad:#d03b3b;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100}
@media (prefers-color-scheme:dark){:root{--bg:#111110;--sf:#1a1a19;--t1:#f0efec;--t2:#c3c2b7;--t3:#8f8e86;--ln:#2c2c2a;
--gr:#262624;--up:#e66767;--dn:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t1);
font:14px/1.5 -apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:18px 14px 40px}
h1{font-size:20px;margin:0}.sub{color:var(--t2);font-size:12px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;position:sticky;top:0;background:var(--bg);padding:6px 0;z-index:5}
select,input{font:inherit;background:var(--sf);color:var(--t1);border:1px solid var(--ln);border-radius:8px;padding:6px 10px}
.seg{display:inline-flex;border:1px solid var(--ln);border-radius:8px;overflow:hidden}
.seg button{font:inherit;border:0;background:var(--sf);color:var(--t2);padding:6px 12px;cursor:pointer}
.seg button.on{background:var(--t1);color:var(--sf)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:12px}
.tile,.card{background:var(--sf);border:1px solid var(--ln);border-radius:12px;padding:12px 14px}
.k{color:var(--t2);font-size:12px}.v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.s{color:var(--t3);font-size:12px}
.card{margin-bottom:12px}.card h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:baseline}
.card h2 small{color:var(--t3);font-weight:400;font-size:12px}
.strats{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px;margin-bottom:12px}
.st{background:var(--sf);border:1px solid var(--ln);border-radius:12px;padding:12px 14px;border-top:4px solid var(--c)}
.st h3{margin:0;font-size:14px;display:flex;justify-content:space-between}.st h3 em{font-style:normal;color:var(--t3);font-size:12px;font-weight:400}
.st dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:8px 0 0;font-size:13px}
.st dt{color:var(--t2)}.st dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12px}.pill i{width:8px;height:8px;border-radius:50%}
.ok i{background:var(--good)}.ng i{background:var(--bad)}.na i{background:var(--t3)}
.tw{overflow-x:auto;max-height:520px}table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{position:sticky;top:0;background:var(--sf);text-align:left;color:var(--t2);font-weight:500;border-bottom:1px solid var(--ln);padding:6px 8px;white-space:nowrap;cursor:pointer}
td{border-bottom:1px solid var(--gr);padding:6px 8px;white-space:nowrap}td.n,th.n{text-align:right}
tr.click{cursor:pointer}tr.click:hover td{background:var(--gr)}
.side{display:inline-block;min-width:34px;text-align:center;border-radius:4px;padding:0 6px;font-size:12px;color:#fff}
.side.b{background:var(--up)}.side.s{background:var(--dn)}
.sg{display:inline-flex;align-items:center;gap:5px}.sg i{width:8px;height:8px;border-radius:2px;background:var(--t3)}
.sg.p i{background:var(--up)}.sg.m i{background:var(--dn)}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;background:var(--c)}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--t2);font-size:12px;margin-bottom:4px}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:14px;height:3px;border-radius:2px;background:var(--c)}
svg{width:100%;height:auto;display:block}.ax{fill:var(--t3);font-size:11px}.lab{font-size:12px;fill:var(--t1)}
.empty{color:var(--t3);text-align:center;padding:16px}
#tip{position:fixed;pointer-events:none;background:var(--t1);color:var(--sf);padding:6px 10px;border-radius:8px;font-size:12px;opacity:0;z-index:9;max-width:320px}
.foot{color:var(--t3);font-size:12px}
</style></head><body><div class="wrap">
<div class="top"><div><h1>매매 현황판</h1><div class="sub" id="gen"></div></div><div id="health"></div></div>
<div class="filters">
 <select id="fs"><option value="">전체 전략</option></select>
 <input id="fc" placeholder="종목명·코드 검색" size="14">
 <div class="seg" id="fp"><button data-d="1">오늘</button><button data-d="7">1주</button><button data-d="30" class="on">1개월</button><button data-d="0">전체</button></div>
</div>
<div class="tiles" id="tiles"></div>
<div class="strats" id="strats"></div>
<div class="card"><h2>누적 실현손익 <small>전략별 · 매도일 기준</small></h2><div class="legend" id="lg"></div><div id="eq"></div></div>
<div class="card"><h2>보유 현황 <small id="ph"></small></h2><div class="tw" id="pos"></div></div>
<div class="card"><h2>매수·매도 내역 <small id="th"></small></h2><div class="tw" id="trd"></div></div>
<div class="card"><h2>종목별 집계 <small>필터 기간 내</small></h2><div class="tw" id="bystock"></div></div>
<div class="card"><h2>실행 기록 <small>최근 30회 · 전략별</small></h2><div class="tw" id="runs"></div></div>
<div class="foot">수익=빨강 · 손실=파랑 (부호 항상 표시) · 가상 전략의 체결은 일봉 기준 판정이라 정확한 시각 대신 '장중/시가/종가'로 표기 · 수수료·세금은 추정치</div>
</div><div id="tip"></div>
<script>
const D=/*DATA*/null;
const $=s=>document.querySelector(s), W=n=>(n==null?'–':Math.round(n).toLocaleString('ko-KR')), Wsg=n=>(n==null?'–':(n>0?'+':'')+Math.round(n).toLocaleString('ko-KR'));
const P=r=>r==null?'–':(r>0?'+':'')+(r*100).toFixed(1)+'%';
const SG=r=>r==null?'–':`<span class="sg ${r>0?'p':r<0?'m':''}"><i></i>${P(r)}</span>`;
const seen=new Set([...D.trades,...D.positions,...D.runs].map(x=>x.strategy));const strategies=[...(D.order||[]).filter(s=>seen.has(s)),...[...seen].filter(s=>!(D.order||[]).includes(s))];
const ALL=[...(D.order||[]),...strategies.filter(s=>!(D.order||[]).includes(s))];const COL=Object.fromEntries(ALL.map((s,i)=>[s,`var(--s${i%4+1})`]));   // 전략 색 = 등장 순서 고정
strategies.forEach(s=>$('#fs').insertAdjacentHTML('beforeend',`<option>${s}</option>`));
$('#gen').textContent=`생성 ${D.generated} · 전략 ${strategies.length}개`;
let days=30, sortT={k:'time',d:-1};
const dOf=t=>(t||'').slice(0,10);
const today=[...D.trades.map(t=>dOf(t.time)),...D.runs.map(r=>r.data_date)].filter(Boolean).sort().pop()||'';
function inRange(t){if(!days)return true;const a=new Date(today),b=new Date(dOf(t));return (a-b)/864e5<days}
function F(x){const s=$('#fs').value,c=$('#fc').value.trim();return(!s||x.strategy===s)&&(!c||x.name.includes(c)||x.code.includes(c))}
function table(head,rows,empty){if(!rows.length)return`<div class="empty">${empty}</div>`;
 return`<table><thead><tr>${head.map(h=>`<th class="${h[1]||''}" data-k="${h[2]||''}">${h[0]}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`}
function render(){
 const T=D.trades.filter(F).filter(t=>inRange(t.time)), Pp=D.positions.filter(F), held=Pp.filter(p=>!p.pending);
 const buys=T.filter(t=>t.side==='매수'), sells=T.filter(t=>t.side==='매도');
 const val=held.reduce((a,p)=>a+p.qty*(p.now??p.avg),0), cost=held.reduce((a,p)=>a+p.qty*p.avg,0);
 const real=sells.reduce((a,t)=>a+(t.pnl||0),0), wins=sells.filter(t=>t.pnl>0).length;
 const tile=(k,v,s)=>`<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
 $('#tiles').innerHTML=tile('보유 평가금액',W(val)+'원',`매입 ${W(cost)} · 평가손익 ${Wsg(val-cost)}`)
  +tile('실현손익',Wsg(real)+'원',`매도 ${sells.length}건 · 승률 ${sells.length?Math.round(wins/sells.length*100)+'%':'–'}`)
  +tile('매수',`${buys.length}건`,`${W(buys.reduce((a,t)=>a+t.amount,0))}원`)
  +tile('매도',`${sells.length}건`,`${W(sells.reduce((a,t)=>a+t.amount,0))}원`)
  +tile('보유 · 대기',`${held.length} · ${Pp.length-held.length}`,'종목');
 // 전략 카드
 $('#strats').innerHTML=strategies.filter(s=>!$('#fs').value||s===$('#fs').value).map(s=>{
  const st=D.trades.filter(t=>t.strategy===s&&t.side==='매도'), sp=D.positions.filter(p=>p.strategy===s&&!p.pending);
  const r=D.runs.filter(x=>x.strategy===s).slice(-1)[0], mode=[...new Set(D.positions.filter(p=>p.strategy===s).map(p=>p.mode).concat(D.trades.filter(t=>t.strategy===s).map(t=>t.mode)))][0]||'';
  const pnl=st.reduce((a,t)=>a+(t.pnl||0),0), w=st.filter(t=>t.pnl>0).length, avg=st.length?st.reduce((a,t)=>a+(t.ret||0),0)/st.length:null;
  const ev=sp.reduce((a,p)=>a+p.qty*((p.now??p.avg)-p.avg),0);
  const h=r?(r.ok?`<span class="pill ok"><i></i>정상</span>`:`<span class="pill ng"><i></i>점검</span>`):`<span class="pill na"><i></i>기록없음</span>`;
  return`<div class="st" style="--c:${COL[s]}"><h3>${s} <em>${mode}</em></h3><dl>
  <dt>실행상태</dt><dd>${h}${r?' '+r.run_at.slice(5):''}</dd><dt>누적 실현손익</dt><dd>${Wsg(pnl)}원</dd>
  <dt>매도 건수 · 승률</dt><dd>${st.length}건 · ${st.length?Math.round(w/st.length*100)+'%':'–'}</dd>
  <dt>거래당 평균</dt><dd>${P(avg)}</dd><dt>보유 · 평가손익</dt><dd>${sp.length}종목 · ${Wsg(ev)}원</dd></dl></div>`}).join('');
 // 누적손익 차트 (전략별 라인)
 eq(D.trades.filter(F).filter(t=>t.side==='매도'&&t.pnl!=null));
 // 보유
 $('#ph').textContent=`${held.length}종목 보유 · ${Pp.length-held.length}건 대기`;
 $('#pos').innerHTML=table([['전략'],['종목'],['수량','n'],['평균단가','n'],['현재가','n'],['매입금액','n'],['평가금액','n'],['평가손익','n'],['수익률','n'],['매수일'],['손절/목표','n'],['보유일','n']],
  Pp.sort((a,b)=>(a.pending?1:0)-(b.pending?1:0)).map(p=>p.pending?
   `<tr><td><span class="dot" style="--c:${COL[p.strategy]}"></span>${p.strategy}</td><td>${p.name} <span class="s">${p.code}</span></td><td colspan="10" class="s">⏳ ${p.note}</td></tr>`:
   `<tr><td><span class="dot" style="--c:${COL[p.strategy]}"></span>${p.strategy}</td><td>${p.name} <span class="s">${p.code}</span></td><td class="n">${W(p.qty)}</td><td class="n">${W(p.avg)}</td><td class="n">${W(p.now)}</td>
   <td class="n">${W(p.qty*p.avg)}</td><td class="n">${W(p.qty*(p.now??p.avg))}</td><td class="n">${Wsg(p.qty*((p.now??p.avg)-p.avg))}</td><td class="n">${SG(p.now?p.now/p.avg-1:null)}</td>
   <td>${p.buy_time||'–'}</td><td class="n">${p.stop?W(p.stop)+' / '+W(p.target):'–'}</td><td class="n">${p.days??'–'}</td></tr>`),'보유 종목 없음');
 // 거래 내역
 const keyv={time:t=>t.time,amount:t=>t.amount,pnl:t=>t.pnl??-1e18,name:t=>t.name};
 const TT=[...T].sort((a,b)=>{const f=keyv[sortT.k]||keyv.time,x=f(a),y=f(b);return(x>y?1:x<y?-1:0)*sortT.d});
 $('#th').textContent=`${T.length}건 · 매수 ${W(buys.reduce((a,t)=>a+t.amount,0))}원 / 매도 ${W(sells.reduce((a,t)=>a+t.amount,0))}원 · 머리글 눌러 정렬`;
 $('#trd').innerHTML=table([['시간','','time'],['전략'],['종목','','name'],['구분'],['수량','n'],['단가','n'],['금액','n','amount'],['비용(추정)','n'],['사유'],['손익','n','pnl'],['수익률','n']],
  TT.map(t=>`<tr class="click" data-code="${t.code}"><td>${t.time}</td><td><span class="dot" style="--c:${COL[t.strategy]}"></span>${t.strategy} <span class="s">${t.mode}</span></td>
  <td>${t.name} <span class="s">${t.code}</span></td><td><span class="side ${t.side==='매수'?'b':'s'}">${t.side}</span></td><td class="n">${W(t.qty)}</td><td class="n">${W(t.price)}</td>
  <td class="n">${W(t.amount)}</td><td class="n">${W(t.fee)}</td><td>${t.reason||''}</td><td class="n">${t.pnl==null?'':Wsg(t.pnl)}</td><td class="n">${t.ret==null?'':SG(t.ret)}</td></tr>`),'해당 기간 거래 없음');
 document.querySelectorAll('#trd th[data-k]').forEach(th=>{if(th.dataset.k)th.onclick=()=>{sortT={k:th.dataset.k,d:sortT.k===th.dataset.k?-sortT.d:-1};render()}});
 document.querySelectorAll('#trd tr.click').forEach(tr=>tr.onclick=()=>{$('#fc').value=tr.dataset.code;render()});
 // 종목별
 const g={};T.forEach(t=>{const k=t.strategy+'|'+t.code;g[k]=g[k]||{s:t.strategy,n:t.name,c:t.code,b:0,ba:0,se:0,sa:0,p:0};
  if(t.side==='매수'){g[k].b+=t.qty;g[k].ba+=t.amount}else{g[k].se+=t.qty;g[k].sa+=t.amount;g[k].p+=t.pnl||0}});
 $('#bystock').innerHTML=table([['전략'],['종목'],['매수수량','n'],['매수금액','n'],['매도수량','n'],['매도금액','n'],['실현손익','n']],
  Object.values(g).sort((a,b)=>b.p-a.p).map(x=>`<tr><td><span class="dot" style="--c:${COL[x.s]}"></span>${x.s}</td><td>${x.n} <span class="s">${x.c}</span></td>
  <td class="n">${W(x.b)}</td><td class="n">${W(x.ba)}</td><td class="n">${W(x.se)}</td><td class="n">${W(x.sa)}</td><td class="n">${Wsg(x.p)}</td></tr>`),'해당 기간 거래 없음');
 // 실행 기록
 const RR=D.runs.filter(r=>!$('#fs').value||r.strategy===$('#fs').value).sort((a,b)=>a.run_at<b.run_at?1:-1);
 $('#runs').innerHTML=table([['실행 시각'],['전략'],['일봉 기준일'],['상태'],['내용']],RR.map(r=>`<tr><td>${r.run_at}</td><td><span class="dot" style="--c:${COL[r.strategy]}"></span>${r.strategy}</td>
  <td>${r.data_date}</td><td>${r.ok?'<span class="pill ok"><i></i>정상</span>':'<span class="pill ng"><i></i>점검</span>'}</td><td class="s">${r.detail}</td></tr>`),'실행 기록 없음');
 const last=D.runs.slice().sort((a,b)=>a.run_at<b.run_at?1:-1);const bad=strategies.filter(s=>{const r=last.find(x=>x.strategy===s);return r&&!r.ok});
 $('#health').innerHTML=bad.length?`<span class="pill ng"><i></i>점검 필요: ${bad.join(', ')}</span>`:(last.length?`<span class="pill ok"><i></i>모든 전략 정상 실행</span>`:'');
 tips();
}
function eq(S){
 const el=$('#eq'),lg=$('#lg');if(!S.length){el.innerHTML='<div class="empty">매도(청산)가 생기면 전략별 누적 손익이 그려집니다</div>';lg.innerHTML='';return}
 const by={};S.slice().sort((a,b)=>a.time<b.time?-1:1).forEach(t=>{(by[t.strategy]=by[t.strategy]||[]).push(t)});
 const dates=[...new Set(S.map(t=>dOf(t.time)))].sort();const series=Object.entries(by).map(([s,ts])=>{let c=0;return{s,pts:ts.map(t=>(c+=t.pnl,{d:dOf(t.time),v:c,t}))}});
 const all=series.flatMap(x=>x.pts.map(p=>p.v)).concat([0]);let lo=Math.min(...all),hi=Math.max(...all);const pd=(hi-lo)*.12||50000;lo-=pd;hi+=pd;
 const Wd=760,H=250,L=60,R=90,Tp=12,B=26,X=d=>L+(Wd-L-R)*(dates.length>1?dates.indexOf(d)/(dates.length-1):.5),Y=v=>Tp+(H-Tp-B)*(1-(v-lo)/(hi-lo));
 let g='';for(let k=0;k<5;k++){const v=lo+(hi-lo)*k/4;g+=`<line x1="${L}" x2="${Wd-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--gr)"/><text x="${L-6}" y="${Y(v)+4}" class="ax" text-anchor="end">${Math.round(v/1e4).toLocaleString()}만</text>`}
 g+=`<line x1="${L}" x2="${Wd-R}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--t3)"/><text x="${L}" y="${H-6}" class="ax">${dates[0]}</text><text x="${Wd-R}" y="${H-6}" class="ax" text-anchor="end">${dates[dates.length-1]}</text>`;
 series.forEach(({s,pts})=>{const c=COL[s];g+=`<path d="${pts.map((p,i)=>(i?'L':'M')+X(p.d)+','+Y(p.v)).join(' ')}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`;
  pts.forEach(p=>{g+=`<circle cx="${X(p.d)}" cy="${Y(p.v)}" r="4" fill="${c}" stroke="var(--sf)" stroke-width="2"/><circle cx="${X(p.d)}" cy="${Y(p.v)}" r="12" fill="transparent" data-tip="${p.d} · ${s} · ${p.t.name} ${p.t.reason} ${P(p.t.ret)} → 누적 ${Wsg(p.v)}원"/>`});
  const e=pts[pts.length-1];g+=`<text x="${X(e.d)+8}" y="${Y(e.v)+4}" class="lab">${s.split(' ')[0]} ${Wsg(e.v/1e4)}만</text>`});
 el.innerHTML=`<svg viewBox="0 0 ${Wd} ${H}" role="img" aria-label="전략별 누적 실현손익">${g}</svg>`;
 lg.innerHTML=series.length<2?'':series.map(x=>`<span style="--c:${COL[x.s]}"><i></i>${x.s}</span>`).join('');
}
function tips(){const tp=$('#tip');document.querySelectorAll('[data-tip]').forEach(el=>{const sh=e=>{const q=e.touches?e.touches[0]:e;tp.textContent=el.dataset.tip;tp.style.opacity=1;tp.style.left=Math.min(q.clientX+12,innerWidth-330)+'px';tp.style.top=(q.clientY+14)+'px'};
 el.onmousemove=sh;el.ontouchstart=sh;el.onmouseleave=()=>tp.style.opacity=0})}
document.addEventListener('touchend',()=>setTimeout(()=>$('#tip').style.opacity=0,1500));
$('#fs').onchange=render;$('#fc').oninput=render;
document.querySelectorAll('#fp button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#fp button').forEach(x=>x.classList.remove('on'));b.classList.add('on');days=+b.dataset.d;render()});
render();
</script></body></html>"""


if __name__ == "__main__":
    print(build())
