"""PostgreSQL 연결 관리 (커넥션 풀 + 컨텍스트 매니저)"""
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
    """프로젝트 루트의 config.ini 경로"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    return os.path.join(root, "config.ini")


def _load_db_config() -> dict:
    cfg = configparser.ConfigParser()
    path = _config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"config.ini를 찾을 수 없습니다: {path}")
    cfg.read(path, encoding="utf-8")

    if "DATABASE" not in cfg:
        raise ValueError("config.ini에 [DATABASE] 섹션이 없습니다")

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
    """커넥션 풀 초기화. 이미 있으면 재사용."""
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
        "DB pool 초기화 완료: %s:%s/%s",
        cfg["host"], cfg["port"], cfg["dbname"],
    )
    return _pool


def close_pool() -> None:
    """프로그램 종료 시 호출"""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("DB pool 종료")


@contextmanager
def get_connection():
    """
    풀에서 커넥션을 가져옴.
    with문이 정상 종료되면 commit, 예외 발생 시 rollback.
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
    커서 컨텍스트 매니저.
    dict_cursor=True (기본): 결과를 dict-like(RealDictRow)로 반환
    dict_cursor=False: tuple로 반환 (execute_values 사용 시 필요)
    """
    with get_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
        finally:
            cursor.close()


def test_connection() -> tuple[bool, str]:
    """DB 연결 헬스체크"""
    try:
        with get_cursor() as cur:
            cur.execute("SELECT version() AS v;")
            row = cur.fetchone()
            return True, row["v"]
    except Exception as e:
        return False, str(e)