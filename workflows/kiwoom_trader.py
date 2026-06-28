"""
==============================================================
  kiwoom_trader.py — 키움 REST API 자동매매 클라이언트
  KISAutoTrader(quant_screener_v41f.py)와 거의 동일한 구조로 작성.
  → execute_signals() 등 기존 매매로직 코드를 최소 수정으로 재사용 가능.

  ⚠ 키움 REST API는 KIS와 다른 점들:
    - 계좌번호를 요청마다 안 보냄 (토큰 = 계좌 1:1로 이미 묶여있음)
    - 매수/매도 주문 URL이 동일 (/api/dostk/ordr), api-id 헤더로만 구분
      (kt10000=매수, kt10001=매도)
    - 토큰 만료시각이 "발급 시점부터 N시간"이 아니라 expires_dt(절대시각, YYYYMMDDHHMMSS)로 옴

  설정파일: kiwoom_config.json (kis_config.json과 같은 폴더에 둘 것)
  {
    "app_key":    "발급받은 앱키",
    "app_secret": "발급받은 시크릿키",
    "is_real":    false,          ← true=실전(api.kiwoom.com), false=모의(mockapi.kiwoom.com)
    "max_daily_trades": 10,
    "stop_loss_pct": 7.0,
    "take_profit_min_pct": 2.0,
    "take_profit_max_pct": 25.0,
    "base_invest_amount": 10000000,
    "reinvest_mode": true,
    "buy_top_n": 20
  }
==============================================================
"""

import os
import json
import time
import threading
from datetime import datetime
from typing import Optional

import requests

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADE_DIR = os.path.join(BASE_DIR, "trades")


class KiwoomRestTrader:
    """
    키움 REST API 자동매매 클라이언트.
    KISAutoTrader와 메서드 이름·반환형을 가능한 한 똑같이 맞춰서,
    execute_signals() 같은 상위 로직을 거의 그대로 재사용할 수 있게 했다.
    """

    BASE_REAL = "https://api.kiwoom.com"
    BASE_MOCK = "https://mockapi.kiwoom.com"

    def __init__(self, config_path: str = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "kiwoom_config.json")
        self.cfg = {}
        self.token = None
        self.token_exp = None          # datetime, expires_dt를 파싱해서 저장
        self.is_real = False
        self.daily_trades = 0
        self.positions = {}            # {code: {"qty":N, "avg_price":P, "buy_date":D}}
        self._lock = threading.Lock()
        # 재투자 자금 풀(원) — KIS판(KISAutoTrader)과 동일한 개념
        self._reinvest_pool = None
        self._reinvest_path = os.path.join(TRADE_DIR, "kiwoom_reinvest_pool.json")
        # 보유 포지션 영속화 — 파일이 없으면 KIS판처럼 "보유 0종목"으로 잘못 판단할 수 있어
        # sync_positions_from_broker()로 항상 실제 잔고를 우선시함
        self._positions_path = os.path.join(TRADE_DIR, "kiwoom_positions.json")
        self._load_config()
        self._load_reinvest_pool()
        self._load_positions()

    # ── 설정 로드 ──
    def _load_config(self):
        if not os.path.exists(self.config_path):
            self._create_default_config()
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
            self.is_real = self.cfg.get("is_real", False)
            mode = "🔴 실전투자" if self.is_real else "🟡 모의투자"
            print(f"  [키움] 설정 로드 완료 ({mode})")
        except Exception as e:
            print(f"  ⚠ 키움 설정 로드 오류: {e}")

    def _create_default_config(self):
        default = {
            "app_key":             "YOUR_APP_KEY",
            "app_secret":          "YOUR_APP_SECRET",
            "is_real":             False,
            "max_daily_trades":    10,
            "stop_loss_pct":       7.0,
            "take_profit_min_pct": 2.0,
            "take_profit_max_pct": 25.0,
            "base_invest_amount":  10_000_000,
            "reinvest_mode":       True,
            "buy_top_n":           20,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print(f"  [키움] 기본 설정파일 생성: {self.config_path}")
        print(f"  → openapi.kiwoom.com 에서 앱키 발급 후 입력")

    # ── 재투자 자금 풀 영속 저장/로드 (KISAutoTrader와 동일한 패턴) ──
    def _load_reinvest_pool(self):
        try:
            if os.path.exists(self._reinvest_path):
                with open(self._reinvest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._reinvest_pool = data.get("pool_won")
                print(f"  [키움-재투자] 자금 풀 로드: {self._reinvest_pool:,.0f}원"
                      if self._reinvest_pool else "  [키움-재투자] 저장된 풀 없음 (최초 실행)")
        except Exception as e:
            print(f"  ⚠ 키움 재투자 풀 로드 오류: {e}")
            self._reinvest_pool = None

    def _save_reinvest_pool(self, pool_won: float, note: str = ""):
        self._reinvest_pool = pool_won
        try:
            os.makedirs(TRADE_DIR, exist_ok=True)
            with open(self._reinvest_path, "w", encoding="utf-8") as f:
                json.dump({"pool_won": pool_won,
                           "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "note": note}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  ⚠ 키움 재투자 풀 저장 오류: {e}")

    # ── 보유 포지션 영속 저장/로드 ──
    def _load_positions(self):
        try:
            if os.path.exists(self._positions_path):
                with open(self._positions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.positions = data.get("positions", {})
                if self.positions:
                    print(f"  [키움-포지션] 로컬 저장분 로드: {len(self.positions)}종목")
        except Exception as e:
            print(f"  ⚠ 키움 포지션 로드 오류: {e}")

    def _save_positions(self):
        try:
            os.makedirs(TRADE_DIR, exist_ok=True)
            with open(self._positions_path, "w", encoding="utf-8") as f:
                json.dump({"positions": self.positions,
                           "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  ⚠ 키움 포지션 저장 오류: {e}")

    def sync_positions_from_broker(self):
        """실행 시작 시 실제 키움 잔고로 self.positions를 덮어씀 (KIS판과 동일한 안전장치)."""
        if not self._is_configured():
            return
        bal = self.get_balance()
        holdings = bal.get("holdings")
        if holdings is None:
            print("  ⚠ 키움 잔고 조회 실패 → 로컬 kiwoom_positions.json 값을 그대로 사용")
            return

        broker_positions = {}
        for h in holdings:
            code = str(h.get("pdno", "")).strip()
            qty = int(float(h.get("hldg_qty", 0) or 0))
            if not code or qty <= 0:
                continue
            avg_price = float(h.get("pchs_avg_pric", 0) or 0)
            buy_date = self.positions.get(code, {}).get(
                "buy_date", datetime.today().strftime("%Y-%m-%d"))
            broker_positions[code] = {"qty": qty, "avg_price": round(avg_price), "buy_date": buy_date}

        if broker_positions != self.positions:
            print(f"  [키움-포지션] 브로커 실잔고로 동기화: {len(broker_positions)}종목")
        self.positions = broker_positions
        self._save_positions()

    @property
    def base_url(self):
        return self.BASE_REAL if self.is_real else self.BASE_MOCK

    def _is_configured(self) -> bool:
        key = self.cfg.get("app_key", "")
        return bool(key and key != "YOUR_APP_KEY")

    # ── OAuth2 접근토큰 발급 ──
    def get_access_token(self) -> Optional[str]:
        """접근토큰 발급 (만료 전이면 캐시된 토큰 재사용)"""
        if self.token and self.token_exp and datetime.now() < self.token_exp:
            return self.token
        if not self._is_configured():
            print("  ⚠ 키움 앱키 미설정 → kiwoom_config.json 확인")
            return None
        try:
            resp = requests.post(
                f"{self.base_url}/oauth2/token",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.cfg.get("app_key", ""),
                    "secretkey": self.cfg.get("app_secret", ""),
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("return_code") != 0:
                print(f"  ⚠ 키움 토큰 발급 실패: {data.get('return_msg', '')}")
                return None
            self.token = data.get("token")
            # expires_dt 형식: "YYYYMMDDHHMMSS"
            try:
                self.token_exp = datetime.strptime(data.get("expires_dt", ""), "%Y%m%d%H%M%S")
            except Exception:
                # 파싱 실패 시 안전하게 30분짜리로 캐시 (다음 호출에서 재발급되게)
                from datetime import timedelta
                self.token_exp = datetime.now() + timedelta(minutes=30)
            mode_str = "🔴실전" if self.is_real else "🟡모의"
            print(f"  ✅ [키움-{mode_str}] 토큰 발급 완료 (만료: {self.token_exp})")
            return self.token
        except Exception as e:
            print(f"  ⚠ 키움 토큰 발급 오류: {e}")
            return None

    def _headers(self, api_id: str) -> dict:
        token = self.get_access_token()
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": f"Bearer {token}" if token else "",
        }

    # ── 주문 실행 ──
    def place_order(self, stock_code: str, order_type: str,
                     qty: int, price: int = 0, reason: str = "") -> dict:
        """
        주문 실행
        order_type : "BUY" | "SELL"
        price      : 0 = 시장가, 양수 = 보통(지정가)
        """
        with self._lock:
            max_trades = self.cfg.get("max_daily_trades", 10)
            if self.daily_trades >= max_trades:
                msg = f"  ⚠ 일일 거래한도({max_trades}회) 초과 → 주문 차단: {stock_code}"
                print(msg)
                return {"success": False, "msg": msg}

            if not self._is_configured():
                print("  ⚠ 키움 앱키 미설정 → 주문 불가")
                return {"success": False, "msg": "앱키 미설정"}

            api_id = "kt10000" if order_type == "BUY" else "kt10001"
            trde_tp = "3" if price == 0 else "0"   # 3=시장가, 0=보통(지정가)

            try:
                resp = requests.post(
                    f"{self.base_url}/api/dostk/ordr",
                    headers=self._headers(api_id),
                    json={
                        "dmst_stex_tp": "KRX",
                        "stk_cd": stock_code,
                        "ord_qty": str(qty),
                        "ord_uv": str(price) if price else "",
                        "trde_tp": trde_tp,
                        "cond_uv": "",
                    },
                    timeout=10,
                )
                data = resp.json()

                if data.get("return_code") == 0:
                    self.daily_trades += 1
                    order_no = data.get("ord_no", "")
                    cur_price = price or self.get_current_price(stock_code)

                    self._update_position(stock_code, order_type, qty, cur_price)

                    mode_str = "🔴실전" if self.is_real else "🟡모의"
                    emoji = "📈" if order_type == "BUY" else "📉"
                    print(f"  {emoji} [키움-{mode_str}] {order_type} {stock_code} "
                          f"{qty:,}주 @{cur_price:,}원 → 주문번호: {order_no}")
                    return {"success": True, "order_no": order_no, "price": cur_price}
                else:
                    print(f"  ⚠ 키움 주문 실패: {data.get('return_msg', '')}")
                    return {"success": False, "msg": data.get("return_msg", "")}

            except Exception as e:
                print(f"  ⚠ 키움 주문 오류: {e}")
                return {"success": False, "msg": str(e)}

    def _update_position(self, code, order_type, qty, price):
        if order_type == "BUY":
            if code in self.positions:
                old = self.positions[code]
                total = old["qty"] + qty
                avg = (old["avg_price"] * old["qty"] + price * qty) / total
                self.positions[code] = {"qty": total, "avg_price": round(avg),
                                         "buy_date": old["buy_date"]}
            else:
                self.positions[code] = {"qty": qty, "avg_price": price,
                                         "buy_date": datetime.today().strftime("%Y-%m-%d")}
        elif order_type == "SELL" and code in self.positions:
            new_qty = self.positions[code]["qty"] - qty
            if new_qty <= 0:
                del self.positions[code]
            else:
                self.positions[code]["qty"] = new_qty
        self._save_positions()

    # ── 현재가 조회 (ka10001 주식기본정보요청) ──
    def get_current_price(self, stock_code: str) -> int:
        try:
            resp = requests.post(
                f"{self.base_url}/api/dostk/stkinfo",
                headers=self._headers("ka10001"),
                json={"stk_cd": stock_code},
                timeout=10,
            )
            data = resp.json()
            raw = data.get("cur_prc", "0")
            return abs(int(raw)) if raw else 0   # cur_prc가 부호 포함으로 올 수 있어 abs() 처리
        except Exception as e:
            print(f"  ⚠ 키움 현재가 조회 오류({stock_code}): {e}")
            return 0

    # ── 잔고/보유종목 조회 (kt00018 계좌평가잔고내역요청) ──
    def get_balance(self) -> dict:
        """
        주식 잔고 조회 (KIS의 get_balance()와 같은 형태로 변환해서 반환)
        리턴: {"총평가금액":int, "예수금총액":int(키움은 별도 TR이라 0으로 둠),
               "순자산":int, "holdings":[{"pdno","hldg_qty","pchs_avg_pric",...}]}
        """
        if not self._is_configured():
            return {"총평가금액": 0, "예수금총액": 0, "순자산": 10_000_000, "holdings": []}

        try:
            resp = requests.post(
                f"{self.base_url}/api/dostk/acnt",
                headers=self._headers("kt00018"),
                json={"qry_tp": "1", "dmst_stex_tp": "KRX"},
                timeout=10,
            )
            data = resp.json()
            if data.get("return_code") != 0:
                print(f"  ⚠ 키움 잔고 조회 실패: {data.get('return_msg', '')}")
                return {"총평가금액": 0, "예수금총액": 0, "순자산": 10_000_000, "holdings": []}

            tot_evlt = abs(int(data.get("tot_evlt_amt", "0") or 0))
            prsm_asset = abs(int(data.get("prsm_dpst_aset_amt", "0") or 0))

            # KIS의 output1(holdings) 포맷과 동일한 키 이름으로 변환
            # (kis_intraday.py의 _already_held() 등 기존 코드가 pdno/hldg_qty를 그대로 읽을 수 있게)
            raw_holdings = data.get("acnt_evlt_remn_indv_tot", [])
            holdings = []
            for h in raw_holdings:
                holdings.append({
                    "pdno": str(h.get("stk_cd", "")).lstrip("A"),  # 접두어 A(주식) 제거
                    "hldg_qty": h.get("rmnd_qty", "0"),
                    "pchs_avg_pric": h.get("pur_pric", "0"),
                    "prpr": h.get("cur_prc", "0"),
                    "evltv_prft": h.get("evltv_prft", "0"),
                    "prft_rt": h.get("prft_rt", "0"),
                })

            print(f"  [키움] 잔고: 총평가 {tot_evlt:,}원 / 추정예탁자산 {prsm_asset:,}원")
            return {"총평가금액": tot_evlt, "예수금총액": 0,
                    "순자산": prsm_asset or 10_000_000, "holdings": holdings}
        except Exception as e:
            print(f"  ⚠ 키움 잔고 조회 오류: {e}")
            return {"총평가금액": 0, "예수금총액": 0, "순자산": 10_000_000, "holdings": []}

    def status_summary(self) -> str:
        mode = "🔴실전" if self.is_real else "🟡모의"
        cfg_ok = "✅설정됨" if self._is_configured() else "❌미설정(kiwoom_config.json 확인)"
        return f"키움 API {mode} {cfg_ok} | 오늘 거래:{self.daily_trades}회"
