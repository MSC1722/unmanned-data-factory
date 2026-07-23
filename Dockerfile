# 무인 데이터 공장 — FastAPI 글로벌 차익거래 백엔드
#
# 빌드:  docker build -t arbitrage-backend .
# 실행:  docker run -p 8000:8000 --env-file .env arbitrage-backend
#
# SQLite AI 캐시(cache.db)는 컨테이너 로컬 디스크에 쓰인다. 컨테이너가 재생성
# (재배포)되면 로컬 디스크도 함께 초기화되므로, 재배포를 넘어서는 영구
# 캐시가 필요하면 AI_CACHE_DB_PATH가 가리키는 경로를 퍼시스턴트 볼륨(예:
# Railway Volume, Fly.io Volume, Docker named volume)에 마운트해야 한다:
#
#   docker run -v arbitrage-cache:/app/data \
#              -e AI_CACHE_DB_PATH=/app/data/cache.db \
#              -p 8000:8000 --env-file .env arbitrage-backend

FROM python:3.12-slim AS runtime

WORKDIR /app

# 파이썬 출력 버퍼링/바이트코드 캐시 비활성화 (컨테이너 로그 확인 편의 + 이미지 용량 절약)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 의존성 레이어를 소스 코드보다 먼저 복사 — 코드만 바뀌는 재빌드에서 pip install 레이어가 캐시된다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드
COPY app/ ./app/
COPY dashboard/ ./dashboard/

# outbox/logs/SQLite 캐시가 쓰는 디렉터리를 미리 만들어둔다.
# (볼륨을 마운트하지 않으면 재배포 시 초기화된다 — 위 주석 참고)
RUN mkdir -p /app/outbox /app/logs

# 루트가 아닌 사용자로 실행 (컨테이너 보안 기본 수칙)
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
