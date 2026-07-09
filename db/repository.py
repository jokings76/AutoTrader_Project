"""Repository 패턴 - 테이블별 CRUD 캡슐화"""
import logging
from datetime import datetime, date
from typing import Any, Optional

from psycopg2.extras import execute_values

from db.connection import get_cursor

logger = logging.getLogger(__name__)


class BaseRepository:
    """공통 CRUD 베이스. 각 테이블 클래스는 table_name만 오버라이드."""
    table_name: str = ""

    @classmethod
    def insert(cls, data: dict) -> int:
        """단일 row 삽입, 생성된 id 반환"""
        if not data:
            raise ValueError("insert 데이터가 비어있음")

        cols = list(data.keys())
        vals = list(data.values())
        col_str = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = (
            f"INSERT INTO {cls.table_name} ({col_str}) "
            f"VALUES ({placeholders}) RETURNING id"
        )

        with get_cursor() as cur:
            cur.execute(sql, vals)
            row = cur.fetchone()
            return row["id"] if row else None

    @classmethod
    def insert_many(cls, data_list: list[dict]) -> int:
        """벌크 삽입, 삽입된 row 개수 반환"""
        if not data_list:
            return 0

        cols = list(data_list[0].keys())
        col_str = ", ".join(cols)
        values = [tuple(d[c] for c in cols) for d in data_list]
        sql = f"INSERT INTO {cls.table_name} ({col_str}) VALUES %s"

        with get_cursor(dict_cursor=False) as cur:
            execute_values(cur, sql, values)
            return cur.rowcount

    @classmethod
    def update(cls, row_id: int, data: dict) -> bool:
        if not data:
            return False
        set_clause = ", ".join(f"{k} = %s" for k in data.keys())
        vals = list(data.values()) + [row_id]
        sql = f"UPDATE {cls.table_name} SET {set_clause} WHERE id = %s"
        with get_cursor() as cur:
            cur.execute(sql, vals)
            return cur.rowcount > 0

    @classmethod
    def find_by_id(cls, row_id: int) -> Optional[dict]:
        sql = f"SELECT * FROM {cls.table_name} WHERE id = %s"
        with get_cursor() as cur:
            cur.execute(sql, (row_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @classmethod
    def count(cls, where: str | None = None, params: tuple | None = None) -> int:
        sql = f"SELECT COUNT(*) AS cnt FROM {cls.table_name}"
        if where:
            sql += f" WHERE {where}"
        with get_cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()["cnt"]


# ─────────────────────────────────────────────────────────────
class TradeRepository(BaseRepository):
    """매매 기록 (trades, 24컬럼: id, stock_code, stock_name, buy_time, buy_price,
    buy_quantity, buy_amount, sell_time, sell_price, sell_quantity, sell_amount,
    fee, tax, profit_amount, profit_rate, strategy_phase, sub_strategy,
    entry_reason, exit_reason, status, ...)"""
    table_name = "trades"

    @classmethod
    def insert_buy(
        cls,
        stock_code: str,
        stock_name: str,
        buy_price: float,
        buy_quantity: int,
        strategy_phase: int,
        sub_strategy: str | None = None,   # '1A' | '1B' | '2' | None
        entry_reason: str | None = None,
        **extra: Any,
    ) -> int:
        """매수 시점 기록"""
        data = {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "buy_time": datetime.now(),
            "buy_price": buy_price,
            "buy_quantity": buy_quantity,
            "buy_amount": buy_price * buy_quantity,
            "strategy_phase": strategy_phase,
            "sub_strategy": sub_strategy,
            "entry_reason": entry_reason,
            "status": "holding",
        }
        data.update(extra)
        return cls.insert(data)

    @classmethod
    def update_sell(
        cls,
        trade_id: int,
        sell_price: float,
        sell_quantity: int,
        exit_reason: str | None = None,
        fee: float = 0,
        tax: float = 0,
        **extra: Any,
    ) -> bool:
        """매도 체결 시 기존 row 업데이트 + 손익 계산"""
        trade = cls.find_by_id(trade_id)
        if not trade:
            raise ValueError(f"trade_id={trade_id} not found")

        sell_amount = sell_price * sell_quantity
        buy_amount = float(trade["buy_amount"])
        profit_amount = sell_amount - buy_amount - fee - tax
        profit_rate = (profit_amount / buy_amount * 100) if buy_amount else 0

        data = {
            "sell_time": datetime.now(),
            "sell_price": sell_price,
            "sell_quantity": sell_quantity,
            "sell_amount": sell_amount,
            "fee": fee,
            "tax": tax,
            "profit_amount": profit_amount,
            "profit_rate": profit_rate,
            "exit_reason": exit_reason,
            "status": "closed",
        }
        data.update(extra)
        return cls.update(trade_id, data)

    @classmethod
    def find_holdings(cls) -> list[dict]:
        """보유 중(status='holding') 종목 전체"""
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE status = 'holding' ORDER BY buy_time"
        )
        with get_cursor() as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def count_holdings(cls) -> int:
        return cls.count("status = 'holding'")

    @classmethod
    def find_by_date(cls, target_date: date) -> list[dict]:
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE DATE(buy_time) = %s ORDER BY buy_time"
        )
        with get_cursor() as cur:
            cur.execute(sql, (target_date,))
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def find_holding_by_code(cls, stock_code: str) -> Optional[dict]:
        """특정 종목의 보유 row (중복 매수 방지용)"""
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE stock_code = %s AND status = 'holding' "
            f"ORDER BY buy_time DESC LIMIT 1"
        )
        with get_cursor() as cur:
            cur.execute(sql, (stock_code,))
            row = cur.fetchone()
            return dict(row) if row else None

    @classmethod
    def find_closed_by_substrategy(
        cls, sub_strategy: str, limit: int = 200,
    ) -> list[dict]:
        """sub_strategy별 청산 완료 거래 (켈리 계산용)"""
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE sub_strategy = %s AND status = 'closed' "
            f"ORDER BY sell_time DESC NULLS LAST, id DESC "
            f"LIMIT %s"
        )
        with get_cursor() as cur:
            cur.execute(sql, (sub_strategy, limit))
            return [dict(r) for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────
class WatchListRepository(BaseRepository):
    """09:00~09:20 워치리스트 + 이후 후보 (16컬럼 가정)"""
    table_name = "watch_list_log"

    @classmethod
    def add(
        cls,
        stock_code: str,
        stock_name: str,
        phase: int,
        **extra: Any,
    ) -> int:
        data = {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "phase": phase,
            "added_time": datetime.now(),
            "is_bought": False,
        }
        data.update(extra)
        return cls.insert(data)

    @classmethod
    def mark_bought(cls, watch_id: int) -> bool:
        return cls.update(watch_id, {"is_bought": True})

    @classmethod
    def find_by_date(cls, target_date: date) -> list[dict]:
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE DATE(added_time) = %s ORDER BY added_time"
        )
        with get_cursor() as cur:
            cur.execute(sql, (target_date,))
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def exists_today(cls, stock_code: str) -> bool:
        """오늘 이미 워치리스트에 들어갔는지 (중복 추가 방지)"""
        sql = (
            f"SELECT 1 FROM {cls.table_name} "
            f"WHERE stock_code = %s AND DATE(added_time) = CURRENT_DATE LIMIT 1"
        )
        with get_cursor() as cur:
            cur.execute(sql, (stock_code,))
            return cur.fetchone() is not None


# ─────────────────────────────────────────────────────────────
class DailySummaryRepository(BaseRepository):
    """일일 요약 (19컬럼 가정)"""
    table_name = "daily_summary"

    @classmethod
    def upsert(cls, trade_date: date, data: dict) -> int:
        """trade_date 기준 있으면 update, 없으면 insert"""
        sql_check = f"SELECT id FROM {cls.table_name} WHERE trade_date = %s"
        with get_cursor() as cur:
            cur.execute(sql_check, (trade_date,))
            row = cur.fetchone()

        if row:
            cls.update(row["id"], data)
            return row["id"]
        else:
            payload = {"trade_date": trade_date, **data}
            return cls.insert(payload)

    @classmethod
    def find_by_date(cls, target_date: date) -> Optional[dict]:
        sql = f"SELECT * FROM {cls.table_name} WHERE trade_date = %s"
        with get_cursor() as cur:
            cur.execute(sql, (target_date,))
            row = cur.fetchone()
            return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
class SystemEventRepository(BaseRepository):
    """시스템 이벤트 로그 (5컬럼: id, event_type, event_message, severity, created_at)"""
    table_name = "system_events"

    @classmethod
    def log(
        cls,
        event_type: str,
        event_message: str,
        severity: str = "INFO",
    ) -> int:
        data = {
            "event_type": event_type,
            "event_message": event_message,
            "severity": severity,
            "created_at": datetime.now(),
        }
        return cls.insert(data)

    @classmethod
    def find_recent(cls, limit: int = 100) -> list[dict]:
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"ORDER BY created_at DESC LIMIT %s"
        )
        with get_cursor() as cur:
            cur.execute(sql, (limit,))
            return [dict(r) for r in cur.fetchall()]