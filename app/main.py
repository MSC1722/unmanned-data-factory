"""
FastAPI 애플리케이션 엔트리포인트.

실행: uvicorn app.main:app --reload
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.camera_routes import router as camera_router
from app.api.routes import router
from app.core.config import settings
from app.core.logger import get_logger
from app.services import ai_cache
from app.services.camera_scheduler import scrape_loop

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("무인 데이터 공장 기동 (env=%s, mock=%s)", settings.app_env, settings.use_mock_data)

    # 스케줄러/파이프라인이 AI 캐시를 쓰기 전에 SQLite 테이블을 먼저 준비한다.
    await ai_cache.init_db()

    scheduler_task = None
    if settings.camera_scheduler_enabled:
        scheduler_task = asyncio.create_task(scrape_loop())
    else:
        logger.info("[scheduler] CAMERA_SCHEDULER_ENABLED=false → 카메라 수집 루프 비활성화")

    yield

    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
    logger.info("무인 데이터 공장 종료")


app = FastAPI(
    title="무인 데이터 공장",
    description="해외 마켓(이베이, 야후옥션 등) 매물 데이터를 수집하는 비동기 파이프라인",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(camera_router)

# 대시보드가 API와 다른 오리진(별도 정적 호스팅)에서 fetch할 수도 있어 열어둔다.
# 아직 인증/쿠키가 없는 MVP 단계라 전체 허용이며, 실서비스 전 도메인을 제한해야 한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 대시보드를 API와 같은 오리진에서 서빙 → /dashboard 접속 시 fetch("/api/v1/signals")가
# 별도 설정 없이 바로 동작한다.
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
if _DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")
