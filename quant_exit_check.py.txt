"""
==============================================================
  quant_exit_check.py — 장중 보유종목 청산(익절/손절/트레일링) 체크
  ★ 신규 모듈 (v1.0) — 기존 파이프라인에 "매도 로직이 아예 없음"이
    확인되어 신설. quant_signal_check.py(매수 전용)의 형제 스크립트.

  배경 (2026-08 분석에서 확인된 사실):
    - quant_signal_check.py 는 매수만 함 (SELL 경로 자체가 없음)
    - kiwoom_config.json 의 stop_loss_pct / take_profit_min·max_pct 는
      전달만 되고 아무도 읽지 않는 죽은 설정이었음
    - 스크리너 신호 JSON의 "손절선 이탈 시 즉시 매도" 문구는 표시용
    → 매수된 종목은 자동 청산 장치가 전혀 없는 상태로 방치되고 있었음

  이 스크립트가 하는 일 (매 실행마다):
    1) 브로커 잔고 조회 → 보유종목 목록 + 매입평균단가 확보
       (상태파일이 아니라 "계좌가 주는 매입단가"를 진입가의 원천으로 사용
        — GitHub Actions 상태파일 유실/재생성과 무관하게 정확)
    2) 종목별 수익률 계산 후 순서대로 판정:
       ① 손절:      수익률 ≤ -stop_loss_pct        → 전량 매도
       ② 상한익절:  수익률 ≥ take_profit_max_pct    → 전량 매도
       ③ 1차익절:   수익률 ≥ take_profit_min_pct 최초 도달
                     → tp1_sell_ratio(기본 30%) 부분 매도, 이후 재실행 안 함
       ④ 트레일링:  1차익절 이후 최고가 대비 trail_giveback_pct(기본 5%)
                     하락 시 잔량 전량 매도
    3) 부분익절 완료 여부 / 최고가를 exit 상태파일에 영속화
       (trade_state_kiwoom/ 에 커밋되어 다음 실행에 복원)

  설정 (kiwoom_config.json — 기존 키 재사용 + 신규 키는 없으면 기본값):
    stop_loss_pct        : 손절 트리거 % (기존 7.0)
    take_profit_min_pct  : 1차(부분)익절 트리거 % (기존 2.0)
        ※ 주의: 기존 값 2.0은 거래비용(세금+수수료 ~0.2%) 감안 시 실효
          이익이 얇음. 5.0 이상으로 올리는 것을 권장 (별도 검토 필요)
    take_profit_max_pct  : 전량 익절 상한 % (기존 25.0)
    tp1_sell_ratio       : 1차익절 매도 비율 (기본 0.30)
    trail_giveback_pct   : 트레일링 반납 허용 % (기본 5.0)

  사용법:
    python quant_exit_check.py --broker kiwoom --output-dir .
    python quant_exit_check.py --broker kiwoom --dry-run   # 주문 없이 판정만 출력

  실행 순서 권장: 워크플로우에서 "매수 스크립트보다 먼저" 실행
    (청산으로 확보된 현금이 같은 실행의 매수 예산에 반영되도록)

  ⚠ 이 스크립트는 계좌의 "모든" 보유종목을 관리 대상으로 본다.
    봇 전용 계좌(모의투자) 전제. 수동 보유 종목이 섞인 실계좌라면
    --exclude 로 제외하거나 전용 계좌 분리를 권장.
==============================================================
"""

import argparse
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo   # Python 3.9+ 표준 — GitHub Actions(UTC) 러너에서도 KST 보장

# 기존 스크리너/시그널체크와 같은 폴더에 있어야 함
from quant_screener_v41f import TRADE_DIR
from quant_signal_check import KRX_HOLIDAYS   # 휴장일 목록 단일 관리 (중복 정의 방지)

KST = ZoneInfo("Asia/Seoul")

EXIT_STATE_TMPL = os.path.join(TRADE_DIR, "{broker}_exit_state.json")

# ── 판정 기본값 (kiwoom_config.json 에 키가 없을 때만 사용) ──
DEFAULT_TP1_SELL_RATIO    = 0.30   # 1차익절 때 파는 비율 (신호 문구 "30% 익절"과 일치)
DEFAULT_TRAIL_GIVEBACK    = 5.0    # 1차익절 후 최고가 대비 이만큼(%) 반납하면 잔량 청산
SELL_WINDOW               = ((9, 5), (15, 15))   # KST 매도주문 허용 시간대 (동시호가 전 여유)

# 잔고 응답에서 필드명을 방어적으로 찾기 위한 후보들
# (KIS 표준: pdno/hldg_qty/pchs_avg_pric — 키움 래퍼가 다른 이름을 쓸 가능성 대비)
F_CODE    = ("pdno", "stk_cd", "code", "종목코드")
F_NAME    = ("prdt_name", "stk_nm", "name", "종목명")
F_QTY     = ("hldg_qty", "rmnd_qty", "qty", "보유수량")
F_SELLQTY = ("ord_psbl_qty", "sell_psbl_qty", "매도가능수량")
F_AVGPRC  = ("pchs_avg_pric", "pchs_avg_prc", "pur_pric", "avg_prc", "avg_price", "매입단가", "평균단가")


def _pick(d: dict, keys, default=None):
    """dict에서 후보 키들 중 처음으로 존재하는 값을 반환 (방어적 필드 접근)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _now_kst() -> datetime:
    return datetime.now(KST)


def _is_trading_day(now: datetime = None) -> bool:
    """KST 기준 거래일 판단 (quant_signal_check와 달리 러너 로컬시간이 아닌 KST 명시).
    ⚠ 기존 quant_signal_check._is_trading_day 는 datetime.now()(=러너 UTC)를 써서
      GitHub Actions에서 컷오프가 영구히 동작하지 않는 버그가 있었음 — 여기선 수정."""
    now = now or _now_kst()
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y%m%d") in KRX_HOLIDAYS:
        return False
    return True


def _in_sell_window(now: datetime = None) -> bool:
    now = now or _now_kst()
    hm = (now.hour, now.minute)
    return SELL_WINDOW[0] <= hm <= SELL_WINDOW[1]


def _state_path(broker: str) -> str:
    return EXIT_STATE_TMPL.format(broker=broker)


def _load_state(broker: str) -> dict:
    path = _state_path(broker)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}}


def _save_state(state: dict, broker: str):
    os.makedirs(TRADE_DIR, exist_ok=True)
    with open(_state_path(broker), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _get_holdings(trader) -> list:
    """잔고에서 (code, name, qty, sell_qty, avg_price) 목록 추출. 필드명 방어적 처리."""
    bal = trader.get_balance()
    out = []
    for h in bal.get("holdings", []):
        code = str(_pick(h, F_CODE, "")).strip()
        qty = int(float(_pick(h, F_QTY, 0) or 0))
        if not code or qty <= 0:
            continue
        sell_qty = int(float(_pick(h, F_SELLQTY, qty) or qty))
        avg_raw = _pick(h, F_AVGPRC)
        avg_price = float(avg_raw) if avg_raw is not None else 0.0
        name = str(_pick(h, F_NAME, code))
        out.append({"code": code, "name": name, "qty": qty,
                    "sell_qty": min(qty, sell_qty) if sell_qty > 0 else qty,
                    "avg_price": avg_price})
    return out


def _resolve_entry_price(code: str, holding: dict, pos_state: dict) -> tuple:
    """진입가 결정. 우선순위:
       ① 계좌 매입평균단가 (가장 신뢰) ② exit 상태파일에 저장해둔 값
       ③ 없으면 현재가를 기준점으로 '지금부터' 관리 시작 (경고 출력)
       리턴: (entry_price, source_str)"""
    if holding["avg_price"] > 0:
        return holding["avg_price"], "broker_avg"
    saved = pos_state.get("entry_price", 0)
    if saved and saved > 0:
        return float(saved), "state_file"
    return 0.0, "unknown"


def run(args):
    now = _now_kst()
    broker = args.broker
    print(f"\n  [청산체크] {now.strftime('%Y-%m-%d %H:%M:%S %Z')} 실행 (broker={broker}"
          f"{', DRY-RUN' if args.dry_run else ''})")

    if not _is_trading_day(now):
        print("  🚫 거래일 아님 — 청산 체크 스킵")
        return []
    if not args.dry_run and not _in_sell_window(now):
        print(f"  ⏰ 매도주문 허용 시간대({SELL_WINDOW[0][0]:02d}:{SELL_WINDOW[0][1]:02d}"
              f"~{SELL_WINDOW[1][0]:02d}:{SELL_WINDOW[1][1]:02d} KST) 밖 — 스킵")
        return []

    if broker == "kiwoom":
        from kiwoom_intraday import KiwoomIntraday
        trader = KiwoomIntraday()
    else:
        from kis_intraday import KISIntraday
        trader = KISIntraday()

    if not trader._is_configured():
        print("  ⚠ 앱키 미설정 → 중단")
        return []
    trader.sync_positions_from_broker()

    cfg = trader.cfg
    stop_pct   = float(cfg.get("stop_loss_pct", 7.0))
    tp_min_pct = float(cfg.get("take_profit_min_pct", 2.0))
    tp_max_pct = float(cfg.get("take_profit_max_pct", 25.0))
    tp1_ratio  = float(cfg.get("tp1_sell_ratio", DEFAULT_TP1_SELL_RATIO))
    trail_pct  = float(cfg.get("trail_giveback_pct", DEFAULT_TRAIL_GIVEBACK))

    # 정합성 가드: 설정이 뒤집혀 있으면(익절상한 < 익절시작 등) 실행 거부
    if not (0 < tp_min_pct < tp_max_pct and stop_pct > 0 and 0 < tp1_ratio < 1):
        print(f"  ❌ 설정 이상: stop={stop_pct} tp_min={tp_min_pct} tp_max={tp_max_pct} "
              f"tp1_ratio={tp1_ratio} — 청산 중단 (설정을 확인하세요)")
        return []

    print(f"  [설정] 손절 -{stop_pct}% | 1차익절 +{tp_min_pct}%에 {tp1_ratio*100:.0f}% 매도 "
          f"| 상한 +{tp_max_pct}% 전량 | 트레일링 반납 {trail_pct}%")

    exclude = set((args.exclude or "").split(",")) - {""}
    state = _load_state(broker)
    positions_state = state.setdefault("positions", {})

    holdings = _get_holdings(trader)
    if not holdings:
        print("  ℹ 보유종목 없음")
        # 잔고가 비었으면 상태파일의 잔재도 정리 (전량 매도 완료된 종목들)
        if positions_state:
            positions_state.clear()
            _save_state(state, broker)
        return []

    held_codes = {h["code"] for h in holdings}
    # 이미 계좌에서 사라진 종목의 상태는 정리
    for gone in [c for c in list(positions_state.keys()) if c not in held_codes]:
        del positions_state[gone]

    results = []
    for h in holdings:
        code, name = h["code"], h["name"]
        if code in exclude:
            print(f"  ⏭ {name}({code}) --exclude 지정 → 관리 제외")
            continue

        pos = positions_state.setdefault(code, {
            "first_seen": now.strftime("%Y%m%d"),
            "tp1_done": False,
            "peak_price": 0.0,
        })

        entry, src = _resolve_entry_price(code, h, pos)
        cur = float(trader.get_current_price(code) or 0)
        time.sleep(0.3)   # rate limit 여유
        if cur <= 0:
            print(f"  ⚠ {name}({code}) 현재가 조회 실패 → 이번 실행 스킵")
            continue

        if entry <= 0:
            # 진입가를 어디서도 못 구함 — 지금 가격을 기준점으로 저장하고 다음 실행부터 관리
            pos["entry_price"] = cur
            pos["entry_price_note"] = f"진입가 불명 → {now.strftime('%m/%d %H:%M')} 현재가로 대체"
            print(f"  ⚠ {name}({code}) 매입단가를 잔고에서 찾지 못함 — 현재가({cur:,.0f})를 "
                  f"기준점으로 저장, 다음 실행부터 관리 (kiwoom_intraday.py의 잔고 필드명 확인 필요!)")
            continue

        pos["entry_price"] = entry
        pnl_pct = (cur / entry - 1.0) * 100.0
        if cur > pos.get("peak_price", 0):
            pos["peak_price"] = cur
        peak = pos["peak_price"]
        drawdown_from_peak = (1.0 - cur / peak) * 100.0 if peak > 0 else 0.0

        tag = f"{name}({code}) 진입가{entry:,.0f}[{src}] 현재가{cur:,.0f} ({pnl_pct:+.2f}%)"

        action, sell_qty, reason = None, 0, ""

        # ① 손절 — 전량
        if pnl_pct <= -stop_pct:
            action, sell_qty = "STOP", h["sell_qty"]
            reason = f"손절 {pnl_pct:+.2f}% ≤ -{stop_pct}%"
        # ② 익절 상한 — 전량
        elif pnl_pct >= tp_max_pct:
            action, sell_qty = "TP_MAX", h["sell_qty"]
            reason = f"상한익절 {pnl_pct:+.2f}% ≥ +{tp_max_pct}%"
        # ③ 1차 부분익절 — 최초 1회만
        elif (not pos["tp1_done"]) and pnl_pct >= tp_min_pct:
            part = int(h["qty"] * tp1_ratio)
            if part >= 1 and part < h["qty"]:
                action, sell_qty = "TP1", min(part, h["sell_qty"])
                reason = f"1차익절 {pnl_pct:+.2f}% ≥ +{tp_min_pct}% → {tp1_ratio*100:.0f}% 매도"
            elif h["qty"] == 1:
                # 1주뿐이면 부분매도 불가 — 트레일링만 걸고 보유
                pos["tp1_done"] = True
                print(f"  ℹ {tag} — 1주 보유라 부분익절 생략, 트레일링 전환")
        # ④ 트레일링 — 1차익절 이후, 최고가 대비 반납
        elif pos["tp1_done"] and peak > 0 and drawdown_from_peak >= trail_pct and pnl_pct > 0:
            action, sell_qty = "TRAIL", h["sell_qty"]
            reason = f"트레일링: 최고가{peak:,.0f} 대비 -{drawdown_from_peak:.2f}% (허용 {trail_pct}%)"

        if action is None:
            trail_info = (f" | 최고가{peak:,.0f}(-{drawdown_from_peak:.2f}%) 트레일링 감시중"
                          if pos["tp1_done"] else "")
            print(f"  ─ {tag} 홀드{trail_info}")
            continue

        print(f"  🔔 {tag} → {action} {sell_qty}주 매도 ({reason})")
        if args.dry_run:
            results.append({"code": code, "name": name, "action": action,
                            "qty": sell_qty, "dry_run": True})
            continue

        r = trader.place_order(code, "SELL", sell_qty, reason=f"[자동청산:{action}] {reason}")
        if r.get("success"):
            results.append({"code": code, "name": name, "action": action,
                            "qty": sell_qty, "price": r.get("price")})
            if action == "TP1":
                pos["tp1_done"] = True
                pos["tp1_time"] = now.strftime("%m/%d %H:%M")
            else:
                # 전량 매도 계열 — 상태 제거는 다음 실행의 잔고 대조에서 처리
                pos["closed"] = action
                pos["closed_time"] = now.strftime("%m/%d %H:%M")
            print(f"  ✅ {name}({code}) {action} 매도 {sell_qty}주 @{r.get('price', 0):,}원")
        else:
            print(f"  ⚠ {name}({code}) 매도주문 실패: {r.get('msg', '')} — 다음 실행에 재시도")

    _save_state(state, broker)

    print(f"\n  [청산체크] 완료 — 이번 실행 매도 {len(results)}건"
          f"{' (dry-run: 주문 미실행)' if args.dry_run else ''}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="장중 보유종목 청산(손절/부분익절/트레일링) 체크")
    parser.add_argument("--broker", choices=["kis", "kiwoom"], default="kiwoom")
    parser.add_argument("--output-dir", type=str, default=".",
                        help="(예약) 신호 파일 폴더 — 현재 버전은 잔고 기반이라 사용하지 않음")
    parser.add_argument("--exclude", type=str, default="",
                        help="관리 제외할 종목코드 콤마구분 (수동 보유분 보호용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="주문 없이 판정 결과만 출력 (최초 도입 시 1~2일 검증 권장)")
    args = parser.parse_args()
    run(args)
