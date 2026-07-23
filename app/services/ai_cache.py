"""
AI 응답 캐시 매니저. (SQLite 파일 기반 — cache.db)

이전에는 인메모리 dict였는데, 클라우드 배포 환경에서 컨테이너가 재시작되면
메모리가 통째로 사라져 이미 분석한 매물도 다음 재시작 후 다시 Gemini/Claude를
호출하게 되는 문제가 있었다(동일 매물 재분석 = 중복 비용 청구). SQLite 파일
(cache.db)에 저장해서 프로세스가 재시작돼도 캐시가 살아남게 한다.

sqlite3는 파이썬 표준 라이브러리라 별도 의존성이 없다. 다만 sqlite3 API는
동기(blocking)라서 이벤트 루프를 막지 않도록 모든 DB 접근을 asyncio.to_thread로
스레드에 위임한다.

동시성 주의: SQLite 조회만으로는 두 요청이 거의 동시에 들어왔을 때 둘 다
캐시 미스로 판단해 AI를 두 번 부를 수 있다(경쟁 상태). 그래서 이전 인메모리
버전과 동일하게 키별 asyncio.Lock + double-checked locking을 유지한다 — 이건
"같은 프로세스 안에서의" 동시 요청 중복 호출을 막는 것이고, SQLite 자체는
"프로세스가 재시작돼도 이미 계산한 결과가 남아있는지"를 보장한다. 두 계층은
서로 다른 문제를 풀기 때문에 둘 다 필요하다.

배포 시 주의: cache.db는 컨테이너의 로컬 디스크에 쓰인다. 컨테이너가 재시작만
되는 경우(같은 디스크 유지)는 캐시가 남지만, 완전히 새로 배포되며 디스크까지
새로 생성되는 환경(대부분의 컨테이너 플랫폼 기본 동작)에서는 여전히 초기화된다.
진짜 영구 보존이 필요하면 AI_CACHE_DB_PATH를 퍼시스턴트 볼륨(예: Railway
Volume) 위의 경로로 설정해야 한다.
"""
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Type, TypeVar

from pydantic import BaseModel

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_DB_PATH = Path(settings.ai_cache_db_path)

# 키별 asyncio.Lock (프로세스 내 동시 요청 중복 호출 방지용). _locks 딕셔너리
# 자체에 대한 동시 접근은 asyncio는 단일 스레드라 별도 보호가 필요 없다.
_locks: dict = {}


def _get_connection() -> sqlite3.Connection:
    """새 SQLite 커넥션을 연다. sqlite3 커넥션은 기본적으로 만든 스레드에서만
    안전하게 쓸 수 있는데, asyncio.to_thread는 호출마다 스레드 풀의 다른 스레드를
    쓸 수 있어 커넥션을 재사용하지 않고 호출마다 새로 연다 (SQLite는 파일 기반이라
    커넥션 오픈 비용이 매우 낮다)."""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기/쓰기 안정성 향상
    return conn


def _init_db_sync() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key     TEXT PRIMARY KEY,
                result_json   TEXT NOT NULL,
                discovered_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    """cache.db와 테이블이 없으면 만든다. 앱 기동 시(main.py lifespan) 한 번 호출."""
    await asyncio.to_thread(_init_db_sync)
    logger.info("[ai_cache] SQLite 캐시 초기화 완료: %s", _DB_PATH.resolve())


def _read_sync(key: str) -> Optional[str]:
    conn = _get_connection()
    try:
        row = conn.execute("SELECT result_json FROM ai_cache WHERE cache_key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _write_sync(key: str, result_json: str, discovered_at: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache (cache_key, result_json, discovered_at) VALUES (?, ?, ?)",
            (key, result_json, discovered_at),
        )
        conn.commit()
    finally:
        conn.close()


def _get_lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def get_or_compute(key: str, compute: Callable[[], Awaitable[T]], result_type: Type[T]) -> T:
    """key로 SQLite를 조회해서 있으면 즉시 반환하고, 없으면 compute()를 딱 한 번만
    실행해 SQLite에 적재한 뒤 반환한다. 동시에 들어온 요청들도 compute()를
    중복 실행하지 않는다.

    result_type은 캐시된 JSON을 다시 원래 Pydantic 모델로 복원하기 위해 필요하다
    (예: GeminiVisionResult, ClaudeScamResult).
    """
    cached = await asyncio.to_thread(_read_sync, key)
    if cached is not None:
        logger.info("[ai_cache] HIT: %s", key)
        return result_type.model_validate_json(cached)

    async with _get_lock(key):
        # 락을 얻는 동안 다른 요청이 이미 계산 + DB 적재를 끝냈을 수 있다.
        cached = await asyncio.to_thread(_read_sync, key)
        if cached is not None:
            logger.info("[ai_cache] HIT (대기 후): %s", key)
            return result_type.model_validate_json(cached)

        logger.info("[ai_cache] MISS: %s → 실제 AI 호출 실행", key)
        result = await compute()

        discovered_at = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_write_sync, key, result.model_dump_json(), discovered_at)
        return result


def _clear_sync() -> None:
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM ai_cache")
        conn.commit()
    finally:
        conn.close()


async def clear() -> None:
    """테스트/개발 편의용 전체 초기화."""
    await asyncio.to_thread(_clear_sync)
    _locks.clear()


def _cache_size_sync() -> int:
    conn = _get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0]
    finally:
        conn.close()


async def cache_size() -> int:
    return await asyncio.to_thread(_cache_size_sync)
