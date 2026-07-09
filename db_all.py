

===== C:\Users\rober\OneDrive\문서\vscode\AutoTrader_Project\db\__init__.py =====

"""DB ?⑦궎吏 吏꾩엯??""
from db.connection import (
    init_pool,
    close_pool,
    get_connection,
    get_cursor,
    test_connection,
)
from db.repository import (
    TradeRepository,
    WatchListRepository,
    DailySummaryRepository,
    SystemEventRepository,
)

__all__ = [
    "init_pool",
    "close_pool",
    "get_connection",
    "get_cursor",
    "test_connection",
    "TradeRepository",
    "WatchListRepository",
    "DailySummaryRepository",
    "SystemEventRepository",
]


===== C:\Users\rober\OneDrive\문서\vscode\AutoTrader_Project\db\connection.py =====

"""PostgreSQL ?곌껐 愿由?(而ㅻ꽖??? + 而⑦뀓?ㅽ듃 留ㅻ땲?)"""
import os
import logging
import configparser
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None


def _config_path() -> str:
    """?꾨줈?앺듃 猷⑦듃??config.ini 寃쎈줈"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    return os.path.join(root, "config.ini")


def _load_db_config() -> dict:
    cfg = configparser.ConfigParser()
    path = _config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"config.ini瑜?李얠쓣 ???놁뒿?덈떎: {path}")
    cfg.read(path, encoding="utf-8")

    if "DATABASE" not in cfg:
        raise ValueError("config.ini??[DATABASE] ?뱀뀡???놁뒿?덈떎")

    section = cfg["DATABASE"]
    return {
        "host": section.get("host", "localhost"),
        "port": section.getint("port", 5432),
        "dbname": section.get("dbname", "autotrader_db"),
        "user": section.get("user"),
        "password": section.get("password"),
        "min_conn": section.getint("min_conn", 1),
        "max_conn": section.getint("max_conn", 5),
    }


def init_pool() -> ThreadedConnectionPool:
    """而ㅻ꽖??? 珥덇린?? ?대? ?덉쑝硫??ъ궗??"""
    global _pool
    if _pool is not None:
        return _pool

    cfg = _load_db_config()
    _pool = ThreadedConnectionPool(
        minconn=cfg.pop("min_conn"),
        maxconn=cfg.pop("max_conn"),
        **cfg,
    )
    logger.info(
        "DB pool 珥덇린???꾨즺: %s:%s/%s",
        cfg["host"], cfg["port"], cfg["dbname"],
    )
    return _pool


def close_pool() -> None:
    """?꾨줈洹몃옩 醫낅즺 ???몄텧"""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("DB pool 醫낅즺")


@contextmanager
def get_connection():
    """
    ??먯꽌 而ㅻ꽖?섏쓣 媛?몄샂.
    with臾몄씠 ?뺤긽 醫낅즺?섎㈃ commit, ?덉쇅 諛쒖깮 ??rollback.
    """
    if _pool is None:
        init_pool()

    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    """
    而ㅼ꽌 而⑦뀓?ㅽ듃 留ㅻ땲?.
    dict_cursor=True (湲곕낯): 寃곌낵瑜?dict-like(RealDictRow)濡?諛섑솚
    dict_cursor=False: tuple濡?諛섑솚 (execute_values ?ъ슜 ???꾩슂)
    """
    with get_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
        finally:
            cursor.close()


def test_connection() -> tuple[bool, str]:
    """DB ?곌껐 ?ъ뒪泥댄겕"""
    try:
        with get_cursor() as cur:
            cur.execute("SELECT version() AS v;")
            row = cur.fetchone()
            return True, row["v"]
    except Exception as e:
        return False, str(e)


===== C:\Users\rober\OneDrive\문서\vscode\AutoTrader_Project\db\repository.py =====

"""Repository ?⑦꽩 - ?뚯씠釉붾퀎 CRUD 罹≪뒓??""
import logging
from datetime import datetime, date
from typing import Any, Optional

from psycopg2.extras import execute_values

from db.connection import get_cursor

logger = logging.getLogger(__name__)


class BaseRepository:
    """怨듯넻 CRUD 踰좎씠?? 媛??뚯씠釉??대옒?ㅻ뒗 table_name留??ㅻ쾭?쇱씠??"""
    table_name: str = ""

    @classmethod
    def insert(cls, data: dict) -> int:
        """?⑥씪 row ?쎌엯, ?앹꽦??id 諛섑솚"""
        if not data:
            raise ValueError("insert ?곗씠?곌? 鍮꾩뼱?덉쓬")

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
        """踰뚰겕 ?쎌엯, ?쎌엯??row 媛쒖닔 諛섑솚"""
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


# ?????????????????????????????????????????????????????????????
class TradeRepository(BaseRepository):
    """留ㅻℓ 湲곕줉 (trades, 24而щ읆: id, stock_code, stock_name, buy_time, buy_price,
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
        """留ㅼ닔 ?쒖젏 湲곕줉"""
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
        """留ㅻ룄 泥닿껐 ??湲곗〈 row ?낅뜲?댄듃 + ?먯씡 怨꾩궛"""
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
        """蹂댁쑀 以?status='holding') 醫낅ぉ ?꾩껜"""
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
        """?뱀젙 醫낅ぉ??蹂댁쑀 row (以묐났 留ㅼ닔 諛⑹???"""
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
        """sub_strategy蹂?泥?궛 ?꾨즺 嫄곕옒 (耳덈━ 怨꾩궛??"""
        sql = (
            f"SELECT * FROM {cls.table_name} "
            f"WHERE sub_strategy = %s AND status = 'closed' "
            f"ORDER BY sell_time DESC NULLS LAST, id DESC "
            f"LIMIT %s"
        )
        with get_cursor() as cur:
            cur.execute(sql, (sub_strategy, limit))
            return [dict(r) for r in cur.fetchall()]


# ?????????????????????????????????????????????????????????????
class WatchListRepository(BaseRepository):
    """09:00~09:20 ?뚯튂由ъ뒪??+ ?댄썑 ?꾨낫 (16而щ읆 媛??"""
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
        """?ㅻ뒛 ?대? ?뚯튂由ъ뒪?몄뿉 ?ㅼ뼱媛붾뒗吏 (以묐났 異붽? 諛⑹?)"""
        sql = (
            f"SELECT 1 FROM {cls.table_name} "
            f"WHERE stock_code = %s AND DATE(added_time) = CURRENT_DATE LIMIT 1"
        )
        with get_cursor() as cur:
            cur.execute(sql, (stock_code,))
            return cur.fetchone() is not None


# ?????????????????????????????????????????????????????????????
class DailySummaryRepository(BaseRepository):
    """?쇱씪 ?붿빟 (19而щ읆 媛??"""
    table_name = "daily_summary"

    @classmethod
    def upsert(cls, trade_date: date, data: dict) -> int:
        """trade_date 湲곗? ?덉쑝硫?update, ?놁쑝硫?insert"""
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


# ?????????????????????????????????????????????????????????????
class SystemEventRepository(BaseRepository):
    """?쒖뒪???대깽??濡쒓렇 (5而щ읆: id, event_type, event_message, severity, created_at)"""
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
