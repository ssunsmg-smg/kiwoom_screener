"""
==============================================================
  kiwoom_intraday.py — 키움 REST API 분봉시세 / 투자자(외인·기관) 데이터
  kiwoom_trader.py 의 KiwoomRestTrader 를 그대로 상속해서
  인증(get_access_token)·주문(place_order) 코드는 손대지 않고 재사용.

  ⚠ kis_intraday.py와 가장 큰 차이점:
    KIS는 1분봉만 줘서 우리가 직접 pandas로 5/10/15/30/60분봉을 만들어야
    했지만(kis_intraday.py의 get_minute_chart 참고), 키움 분봉조회(ka10080)는
    tic_scope 파라미터로 1/3/5/10/15/30/45/60분을 서버에 바로 요청할 수 있다.
    → 리샘플링 로직 자체가 필요 없음. 그만큼 코드가 더 짧고 단순하다.

  ⚠ 사용 전 확인:
    - kiwoom_trader.py 와 같은 폴더에 둘 것 (import 때문에)
    - kiwoom_config.json 의 app_key/app_secret 은 실거래용이므로,
      이 모듈은 "조회"만 하지만 같은 토큰을 쓰는 만큼 신중하게 다룰 것
==============================================================
"""

import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from kiwoom_trader import KiwoomRestTrader   # 기존 인증·주문 클래스 재사용

try:
    from kiwoom_trader import now_kst as _kst_now
except ImportError:                          # 구버전 kiwoom_trader.py 와 함께 써도 동작
    from zoneinfo import ZoneInfo
    def _kst_now():
        return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


class KiwoomIntraday(KiwoomRestTrader):
    """
    KiwoomRestTrader 상속 → get_access_token(), _headers(), base_url 등
    인증 로직을 그대로 쓰면서, 분봉/투자자 조회 메서드만 추가.
    """

    _SUPPORTED_INTERVALS = (1, 3, 5, 10, 15, 30, 45, 60)

    # ── 주식분봉차트조회요청 (ka10080) ──
    # KIS와 달리 tic_scope로 원하는 분봉 주기를 서버에 직접 요청 가능
    # (리샘플링 불필요 — kis_intraday.py의 get_minute_chart와 비교해보면 코드가 훨씬 짧다)
    def get_minute_chart(self, stock_code: str, interval: int = 1,
                          base_dt: str = "", **kwargs) -> pd.DataFrame:
        """
        N분봉 OHLCV DataFrame 반환 (N = 1/3/5/10/15/30/45/60).
        컬럼: Open, High, Low, Close, Volume  (index: 시간 오름차순, pandas Timestamp)

        base_dt: 기준일자(YYYYMMDD), 비워두면 당일 기준.
        **kwargs: KIS판(kis_intraday.KISIntraday)과 동일한 호출 코드를 그대로
                  쓸 수 있게, lookback_calls 같은 KIS 전용 인자를 받아도 무시한다.
        """
        if interval not in self._SUPPORTED_INTERVALS:
            raise ValueError(f"interval은 {self._SUPPORTED_INTERVALS} 중 하나여야 합니다 (입력값: {interval})")

        try:
            resp = requests.post(
                f"{self.base_url}/api/dostk/chart",
                headers=self._headers("ka10080"),
                json={
                    "stk_cd": stock_code,
                    "tic_scope": str(interval),
                    "upd_stkpc_tp": "1",      # 1=수정주가 반영
                    "base_dt": base_dt,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("return_code") != 0:
                print(f"  ⚠ [분봉조회] {stock_code} 실패: code={data.get('return_code')} "
                      f"{data.get('return_msg', '')} (HTTP {resp.status_code}, "
                      f"{'실전' if self.is_real else '모의'}서버, 한국시각 {_kst_now():%H:%M})")
                return pd.DataFrame()

            rows = data.get("stk_min_pole_chart_qry", [])
            if not rows:
                # 0건 원인 추적용 — 다음 실행 로그에서 바로 원인이 보이게
                t = _kst_now()
                why = "장 시작 전" if t.hour < 9 else ("장 마감 후" if (t.hour, t.minute) > (15, 30) else "장중")
                print(f"  ⚠ [분봉조회] {stock_code} 0건 — 한국시각 {t:%H:%M}({why}), "
                      f"{'실전' if self.is_real else '모의'}서버, 응답키 {list(data.keys())[:6]}")
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df = df.rename(columns={
                "cntr_tm": "time", "open_pric": "Open", "high_pric": "High",
                "low_pric": "Low", "cur_prc": "Close", "trde_qty": "Volume",
            })
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col in df.columns:
                    # 키움 숫자 필드는 "+000000071200"처럼 0-padding+부호 포함으로 오는데,
                    # pandas to_numeric이 이 형식을 못 읽으므로 먼저 정수로 변환 후 절댓값.
                    df[col] = df[col].apply(lambda v: abs(int(v)) if v not in (None, "") else 0)

            df["time"] = pd.to_datetime(df["time"], format="%Y%m%d%H%M%S", errors="coerce")
            df = df.dropna(subset=["time", "Close"]).drop_duplicates(subset=["time"])
            df = df.sort_values("time").set_index("time")
            return df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception as e:
            print(f"  ⚠ [분봉조회] {stock_code} 오류: {e}")
            return pd.DataFrame()

    def get_minute_chart_multi(self, stock_code: str,
                                intervals=(1, 5, 10, 15, 30, 60),
                                base_dt: str = "", **kwargs) -> dict:
        """
        여러 분봉을 동시에 평가하고 싶을 때 호출. KIS판(kis_intraday.py)과 달리
        서버가 각 주기를 직접 줄 수 있어서, 분봉마다 API를 한 번씩 호출한다
        (1분봉만 받아서 리샘플링하는 KIS판보다 호출 수는 늘지만, 데이터는 더 정확함 —
         예: 서버가 만드는 5분봉이 우리가 1분봉으로 직접 리샘플링한 것보다 기준이 다를 수 있음).
        """
        result = {}
        for iv in intervals:
            if iv not in self._SUPPORTED_INTERVALS:
                result[iv] = pd.DataFrame()
                continue
            result[iv] = self.get_minute_chart(stock_code, interval=iv, base_dt=base_dt)
            time.sleep(0.3)   # rate limit 여유
        return result

    # ── 종목별기관매매추이요청 (ka10045) ──
    # KIS의 inquire-investor와 달리, 기관+외인 순매수가 한 번의 호출에 같이 옴
    # (시작일자~종료일자 구간을 직접 지정 가능 — 5일 누적도 날짜 범위로 바로 요청)
    def _fetch_investor_trend(self, stock_code: str, strt_dt: str, end_dt: str) -> list:
        resp = requests.post(
            f"{self.base_url}/api/dostk/mrkcond",
            headers=self._headers("ka10045"),
            json={
                "stk_cd": stock_code,
                "strt_dt": strt_dt,
                "end_dt": end_dt,
                "orgn_prsm_unp_tp": "1",   # 1=매수단가 기준
                "for_prsm_unp_tp": "1",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("return_code") != 0:
            print(f"  ⚠ [투자자조회] {stock_code} 실패: {data.get('return_msg', '')}")
            return []
        return data.get("stk_orgn_trde_trnsn", [])

    def get_investor_flow(self, stock_code: str) -> dict:
        """
        최근 영업일 기준 외국인/기관 순매수(수량) 반환 — kis_intraday.py와 동일한 리턴 형태.
        리턴: {"date": "YYYYMMDD", "foreign_net": int, "institution_net": int, "individual_net": 0}
        (키움 ka10045는 개인 순매수는 안 줌 — 항상 0으로 채움)
        """
        today = _kst_now().strftime("%Y%m%d")
        try:
            rows = self._fetch_investor_trend(stock_code, today, today)
            if not rows:
                return {"date": None, "foreign_net": 0, "institution_net": 0, "individual_net": 0}
            latest = rows[0]
            return {
                "date": latest.get("dt"),
                "foreign_net": int(latest.get("for_daly_nettrde_qty", 0) or 0),
                "institution_net": int(latest.get("orgn_daly_nettrde_qty", 0) or 0),
                "individual_net": 0,
            }
        except Exception as e:
            print(f"  ⚠ [투자자조회] {stock_code} 오류: {e}")
            return {"date": None, "foreign_net": 0, "institution_net": 0, "individual_net": 0}

    def get_investor_flow_accumulated(self, stock_code: str, days: int = 5) -> dict:
        """
        최근 N영업일 외국인/기관 순매수 합산. ka10045가 날짜범위를 직접 받으므로,
        여유있게 days*2일 전부터 조회해서(주말/휴장일 버퍼) 앞에서 N개 행만 합산.
        """
        end_dt = _kst_now()
        strt_dt = end_dt - timedelta(days=days * 2 + 3)
        try:
            rows = self._fetch_investor_trend(
                stock_code, strt_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"))
            rows = rows[:days]
            foreign_sum = sum(int(r.get("for_daly_nettrde_qty", 0) or 0) for r in rows)
            inst_sum = sum(int(r.get("orgn_daly_nettrde_qty", 0) or 0) for r in rows)
            return {"days": len(rows), "foreign_net_sum": foreign_sum, "institution_net_sum": inst_sum}
        except Exception as e:
            print(f"  ⚠ [투자자누적조회] {stock_code} 오류: {e}")
            return {"days": 0, "foreign_net_sum": 0, "institution_net_sum": 0}
