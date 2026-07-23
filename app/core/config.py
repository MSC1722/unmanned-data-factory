"""
전역 설정 모듈.

.env 파일 / 환경 변수를 읽어 pydantic-settings로 검증한다.
다른 모듈은 이 파일의 `settings` 싱글턴 인스턴스만 import해서 쓴다.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 실행 환경
    app_env: str = "development"
    log_level: str = "INFO"

    # 스크레이핑 대상
    target_base_url: str = "https://example.com/search"
    request_timeout: int = 10
    # 봇 탐지를 자초하지 않도록 일반 브라우저 UA를 기본값으로 쓴다.
    # ("DataFactoryBot" 같은 자기신고형 UA는 이베이 등에서 즉시 403으로 막힌다.)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # 파이프라인 (수집 결과를 적재하는 폴더 = 다음 프로세스가 읽어가는 지점)
    outbox_dir: str = "outbox"

    # 목(mock) 데이터 스위치. 실제 타깃이 정해지기 전까지는 true로 두고 개발한다.
    use_mock_data: bool = True

    # eBay Browse API (공식 개발자 API) 인증 정보.
    # RSS 피드(_rss=1)가 403으로 차단되는 것을 확인해 공식 API로 전환했다 —
    # https://developer.ebay.com 에서 애플리케이션을 등록하면 발급된다.
    # 비어 있으면 ebay_browse_api.fetch_ebay_browse_items가 매 사이클 경고
    # 로그만 남기고 빈 리스트를 반환한다(스케줄러 전체를 죽이지 않기 위함).
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    # "sandbox" 또는 "production". App ID/Cert ID에 SBX- 접두사가 붙어있으면
    # sandbox 키이므로 반드시 "sandbox"로 맞춰야 인증이 통과한다(엔드포인트가 다름).
    # sandbox는 실제 매물이 아니라 eBay가 제공하는 가짜 테스트 데이터만 반환한다.
    ebay_env: str = "production"

    # AI API 키. 비어 있으면 각 서비스는 규칙 기반 fallback으로 동작한다.
    gemini_api_key: str = ""
    claude_api_key: str = ""
    # 진단 결과: SDK는 이미 최신(1.47.0)이고 Vertex/리전 설정도 아니다
    # (client.vertexai=False, 기본 전역 엔드포인트). gemini-1.5-*, gemini-pro,
    # gemini-2.0/2.5-* 전부 이 키로는 404("not found" 또는 "no longer
    # available")를 반환한다 — 과금/SDK/리전 문제가 아니라 해당 세대 스냅샷이
    # 전부 은퇴한 것. "-latest" 별칭으로 실제 서빙 모델을 확인해보니
    # gemini-3.5-flash였고, 리터럴 문자열로도 직접 호출 성공을 검증했다.
    # 별칭 대신 리터럴로 고정해 "모델 드리프트"(별칭이 가리키는 대상이 예고
    # 없이 바뀌는 위험)를 피한다. 이 문자열이 다시 404가 나면
    # "gemini-flash-latest"로 잠깐 돌려서 현재 서빙 모델명을 재확인할 것.
    gemini_model: str = "gemini-3.5-flash"
    claude_model: str = "claude-sonnet-5"

    # 환율 (글로벌 USD 기준 연산). 실시간 환율 API 연동 전까지는 .env 값
    # (또는 아래 기본값)을 그대로 쓴다. USD는 기준 통화이므로 환율이 없다(1.0 고정).
    fx_rate_jpy_usd: float = 1 / 150  # 150엔 = $1 기준 고정 환율

    # 글로벌 표준 배송비 (모든 매물에 공통 적용되는 고정 상수, USD)
    global_shipping_fee_usd: float = 40.0

    # 카메라 도메인 백그라운드 스크레이핑 스케줄러 (app/services/camera_scheduler.py)
    camera_scheduler_enabled: bool = True
    camera_scrape_interval_seconds: int = 300  # 5분

    # AI 응답 캐시(app/services/ai_cache.py)가 쓰는 SQLite 파일 경로.
    # 컨테이너 재시작에도 살아남으려면 퍼시스턴트 볼륨(예: Railway Volume) 위의
    # 경로를 가리켜야 한다 — 일반 컨테이너 로컬 디스크는 재배포 시 초기화된다.
    ai_cache_db_path: str = "cache.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 프로세스 생애주기 동안 한 번만 생성해서 재사용한다."""
    return Settings()


settings = get_settings()
