"""
키움 REST API 호출 모듈
─────────────────────────────────────
역할: 주문, 잔고/예수금 조회, 현재가 조회, 분봉 차트 조회
"""
import logging
import json
import time
import requests
from datetime import datetime

from utils.logger import logger

from api.candle_cache import CandleCache

class KiwoomREST:
    """키움 REST API 클라이언트"""

    MOCK_HOST = "https://mockapi.kiwoom.com"
    REAL_HOST = "https://api.kiwoom.com"

    # 호출 빈도 제어 (1초 5회 제한 → 0.6초 안전마진)
    MIN_INTERVAL = 0.6

    def __init__(self, token: str, is_mock: bool = True):
        self.token = token
        self.host = self.MOCK_HOST if is_mock else self.REAL_HOST
        self._last_request_ts = 0.0
        self._candle_cache = CandleCache(
            self._raw_get_minute_candles, ttl_sec=8.0, logger=logger)

    # ─────────────────────────────────────
    # 공통 호출
    # ─────────────────────────────────────
    def _throttle(self):
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed)
        self._last_request_ts = time.time()

    def _request(self, path: str, api_id: str, body: dict,
                 cont_yn: str = "N", next_key: str = "") -> dict:
        """공통 POST 요청. 429는 자동 재시도."""
        self._throttle()
        url = f"{self.host}{path}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "api-id": api_id,
        }

        try:
            res = requests.post(url, headers=headers,
                                data=json.dumps(body), timeout=10)
            res.raise_for_status()
            data = res.json()

            if data.get("return_code") != 0:
                logger.warning(
                    f"⚠️ [{api_id}] 응답 실패: "
                    f"code={data.get('return_code')} "
                    f"msg={data.get('return_msg')}"
                )
            return data

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 429:
                logger.warning(f"⚠️ [{api_id}] 429 호출빈도 초과, 2초 대기 후 재시도")
                time.sleep(2)
                try:
                    res = requests.post(url, headers=headers,
                                        data=json.dumps(body), timeout=10)
                    res.raise_for_status()
                    return res.json()
                except Exception as e2:
                    logger.error(f"❌ [{api_id}] 재시도 실패: {e2}")
                    return {"return_code": -1, "return_msg": "rate_limit_retry_failed"}
            logger.error(f"❌ [{api_id}] HTTP 에러 {status}: {e.response.text[:200]}")
            return {"return_code": -1, "return_msg": str(e)}
        except Exception as e:
            logger.error(f"❌ [{api_id}] 요청 예외: {e}")
            return {"return_code": -1, "return_msg": str(e)}

    # ─────────────────────────────────────
    # 주문
    # ─────────────────────────────────────
    def buy_market_order(self, stock_code: str, qty: int,
                         price: int = 0, trde_tp: str = "3") -> dict:
        """매수 주문 (kt10000)"""
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": stock_code,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price > 0 else "",
            "trde_tp": trde_tp,
            "cond_uv": "",
        }
        result = self._request("/api/dostk/ordr", "kt10000", body)
        if result.get("return_code") == 0:
            logger.info(
                f"✅ 매수주문 성공 | {stock_code} | {qty}주 | "
                f"단가={price if price > 0 else '시장가'} | ord_no={result.get('ord_no')}"
            )
        return result

    def sell_market_order(self, stock_code: str, qty: int,
                          price: int = 0, trde_tp: str = "3") -> dict:
        """매도 주문 (kt10001)"""
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": stock_code,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price > 0 else "",
            "trde_tp": trde_tp,
            "cond_uv": "",
        }
        result = self._request("/api/dostk/ordr", "kt10001", body)
        if result.get("return_code") == 0:
            logger.info(
                f"✅ 매도주문 성공 | {stock_code} | {qty}주 | "
                f"단가={price if price > 0 else '시장가'} | ord_no={result.get('ord_no')}"
            )
        return result

    def cancel_order(self, orig_ord_no: str, stock_code: str,
                     cancel_qty: int = 0) -> dict:
        """주문 취소 (kt10003)"""
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": orig_ord_no,
            "stk_cd": stock_code,
            "mdfy_qty": "",
            "cncl_qty": str(cancel_qty),
        }
        result = self._request("/api/dostk/ordr", "kt10003", body)
        if result.get("return_code") == 0:
            logger.info(f"✅ 주문 취소 | orig_ord_no={orig_ord_no} | {stock_code}")
        return result

    # ─────────────────────────────────────
    # 시세 조회
    # ─────────────────────────────────────
    def get_current_price(self, stock_code: str) -> int:
        """주식기본정보 (ka10001) → 현재가"""
        result = self._request("/api/dostk/stkinfo", "ka10001",
                               {"stk_cd": stock_code})
        if result.get("return_code") != 0:
            return 0
        return self._safe_price(result.get("cur_prc", "0"))

    def get_stock_info(self, stock_code: str) -> dict:
        """주식기본정보 전체 (ka10001)"""
        return self._request("/api/dostk/stkinfo", "ka10001",
                             {"stk_cd": stock_code})

    def get_index_change_rate(self, sector_code: str = "001") -> float:
        """업종 현재가 (ka20001) → 전일 대비 등락률(%).
        sector_code: 001=코스피, 101=코스닥. 실패 시 0.0(정상 간주)."""
        result = self._request("/api/dostk/sect", "ka20001",
                               {"mrkt_tp": "0", "inds_cd": sector_code})
        
        if result.get("return_code") != 0:
            return 0.0
        raw = result.get("flu_rt") or result.get("prdy_ctrt") or "0"
        try:
            return float(str(raw).replace("+", "").strip())
        except (ValueError, TypeError):
            return 0.0

    # ─────────────────────────────────────
    # 분봉 차트 조회 (ka10080)
    # ─────────────────────────────────────
    def get_minute_candles(self, stock_code: str, interval: int = 1,
                           count: int = 60, base_date: str = None) -> list:
        """분봉 조회 (캐시 경유). 8초 TTL + 429/실패 시 직전 캐시 반환."""
        return self._candle_cache.get(
            stock_code, interval=interval, count=count, base_date=base_date) or []
    
    def _raw_get_minute_candles(self, stock_code: str, interval: int = 1,
                                count: int = 60, base_date: str = None) -> list:
        """분봉 조회 (ka10080) - 페이징 자동 대응 버전.
        반환: [{time_str(14자리), open, high, low, close, volume}, ...]"""
        if not base_date:
            base_date = datetime.now().strftime("%Y%m%d")

        body = {
            "stk_cd": stock_code,
            "tic_scope": str(interval),
            "upd_stkpc_tp": "1",
            "base_dt": base_date,
        }

        all_candles = []
        cont_yn = "N"
        next_key = ""

        # ★ 원하는 개수(count)가 채워질 때까지 루프 (최대 안전장치 10회)
        max_loops = 10 
        loop_count = 0
        
        while len(all_candles) < count and loop_count < max_loops:
            loop_count += 1
            result = self._request("/api/dostk/chart", "ka10080", body,
                                   cont_yn=cont_yn, next_key=next_key)
            
            if result.get("return_code") != 0:
                break  # 에러 시 중단

            rows = result.get("stk_min_pole_chart_qry", []) or []
            if not rows:
                break  # 더 이상 데이터 없음

            for row in rows:
                time_str = row.get("cntr_tm", "")
                if len(time_str) != 14:
                    continue
                all_candles.append({
                    "time_str": time_str,
                    "open":   self._safe_price(row.get("open_pric")),
                    "high":   self._safe_price(row.get("high_pric")),
                    "low":    self._safe_price(row.get("low_pric")),
                    "close":  self._safe_price(row.get("cur_prc")),
                    "volume": self._safe_int(row.get("trde_qty")),
                })

            # ★ 페이징 처리 핵심: 응답에 다음 데이터가 있으면 설정
            if result.get("cont_yn") == "Y" and len(all_candles) < count:
                cont_yn = "Y"
                next_key = result.get("next_key", "")
            else:
                break  # 마지막 페이지이거나 개수를 채움

        # 요청한 개수만큼만 슬라이싱하여 반환 (과도한 데이터 방지)
        final_candles = all_candles[:count]

        if final_candles:
            logger.info(
                "[%s] 📊 분봉 %d개 수집완료 (루프%d회): [0]=%s ~ [-1]=%s",
                stock_code, len(final_candles), loop_count,
                final_candles[0]["time_str"], final_candles[-1]["time_str"]
            )
        return final_candles
        
        
    # ─────────────────────────────────────
    # 계좌 조회
    # ─────────────────────────────────────
    def get_deposit(self) -> dict:
        """예수금상세현황 (kt00001)"""
        return self._request("/api/dostk/acnt", "kt00001",
                             {"qry_tp": "3"})

    def get_orderable_amount(self) -> int:
        """주문가능금액"""
        result = self.get_deposit()
        if result.get("return_code") != 0:
            return 0
        return self._safe_int(result.get("ord_alow_amt", "0"))

    def get_balance(self) -> dict:
        """계좌평가잔고내역 (kt00018)"""
        return self._request("/api/dostk/acnt", "kt00018",
                             {"qry_tp": "1", "dmst_stex_tp": "KRX"})

    def get_holdings(self) -> dict:
        """보유 종목 → {종목코드: {정보}}"""
        result = self.get_balance()
        if result.get("return_code") != 0:
            return {}

        holdings = {}
        for item in result.get("acnt_evlt_remn_indv_tot", []):
            code = (item.get("stk_cd") or "").strip()
            if code.startswith("A"):
                code = code[1:]
            if not code:
                continue
            holdings[code] = {
                "name": item.get("stk_nm", ""),
                "qty": self._safe_int(item.get("rmnd_qty", "0")),
                "avg_price": self._safe_price(item.get("pur_pric", "0")),
                "cur_price": self._safe_price(item.get("cur_prc", "0")),
                "eval_amt": self._safe_int(item.get("evlt_amt", "0")),
                "pnl": self._safe_int(item.get("evltv_prft", "0")),
                "pnl_rate": self._safe_float(item.get("prft_rt", "0")),
            }
        return holdings

    def get_unfilled_orders(self) -> dict:
        """미체결 (ka10075)"""
        body = {
            "all_stk_tp": "0",
            "trde_tp": "0",
            "stex_tp": "0",
            "stk_cd": "",
        }
        return self._request("/api/dostk/acnt", "ka10075", body)

    # ─────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────
    @staticmethod
    def _safe_int(v) -> int:
        """
        문자열 → 정수. 음수 보존.
        손익(pnl), 거래량 등에 사용.
        """
        if v is None:
            return 0
        s = str(v).replace(",", "").replace("+", "").strip()
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _safe_float(v) -> float:
        """
        문자열 → 실수. 음수 보존.
        손익률(pnl_rate) 등에 사용.
        """
        if v is None:
            return 0.0
        s = str(v).replace(",", "").replace("+", "").strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _safe_price(v) -> int:
        """
        가격 전용 변환.
        키움 API의 가격 응답에는 등락 부호('+'/'-')가 붙어있음:
        - '+78800' = 전일대비 상승, 가격 78,800원
        - '-78800' = 전일대비 하락, 가격 78,800원
        부호는 등락 표시일 뿐 진짜 음수가 아니므로 절댓값으로 처리.
        """
        if v is None:
            return 0
        s = str(v).replace(",", "").replace("+", "").replace("-", "").strip()
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return 0