"""
Gemini Vision API를 이용한 카메라/렌즈 외관 상태 분석 서비스.

google-genai 공식 SDK(google.genai) 구조로 작성했다. 이미지 URL 배열을
다운로드해 inline bytes로 Gemini에 전달하고, Structured Output(response_schema)
으로 condition_grade / condition_score / 결함 플래그를 강제로 받는다.

유료 결제가 연결된 계정 기준으로 이미지 URL 배열을 안정적으로 처리하도록
프로덕션 대비 가드를 넣었다:
- 매물 하나당 다운로드/분석하는 이미지 수를 MAX_IMAGES_PER_ITEM으로 캡핑한다
  (개수만큼 토큰 비용이 늘어나므로 유료 계정에서는 특히 중요).
- 이미지 다운로드는 전역 세마포어(IMAGE_DOWNLOAD_CONCURRENCY)로 동시 개수를
  제한한다 — 이미지가 많은 매물이 소스 서버/커넥션 풀을 독점하지 않게 한다.
- Content-Type을 확인해서 실제 이미지가 아니면 건너뛰고, 실제 MIME 타입을
  그대로 Gemini에 전달한다 (이전 코드는 전부 image/jpeg로 고정해서 PNG/WEBP
  원본이면 틀릴 수 있었다).
- 이미지 하나가 MAX_IMAGE_BYTES를 넘으면 건너뛴다 (페이로드/비용 억제).
- Gemini 호출 자체도 세마포어(GEMINI_CALL_CONCURRENCY)로 동시 요청 수를 제한해
  분당 요청 한도(RPM)를 자초하지 않게 한다.

GEMINI_API_KEY가 비어 있거나 호출이 실패하면(네트워크 오류, 이미지 접근 불가,
API 장애, 레이트리밋 등) _fallback_condition_analysis()로 안전하게 대체한다 —
무인 파이프라인이 AI API 장애 하나 때문에 통째로 멈추면 안 되기 때문이다.

모델 선택/드리프트에 대해: settings.gemini_model은 "-latest" 같은 별칭이
아니라 리터럴 버전 문자열로 고정해둔다 — 별칭은 구글이 내부적으로 가리키는
스냅샷을 예고 없이 바꿀 수 있어(모델 드리프트), 프롬프트 해석이나 JSON 출력
포맷이 서비스 운영 중 조용히 변할 위험이 있다. 그렇다고 리터럴 고정이 완전한
안전판은 아니라서, 실제 응답의 model_version을 매 호출마다 관찰해 이전
호출과 달라지면 경고 로그를 남긴다(_check_model_drift). 참고로 이 프로젝트를
개발한 시점 기준 gemini-1.5-*, gemini-pro, gemini-2.0/2.5-* 계열은 이 계정
키로 전부 404였고(SDK 최신 버전, Vertex/리전 설정 아님을 확인함 — 과금/SDK/
리전 문제가 아니라 해당 세대 스냅샷 자체가 은퇴한 것), gemini-3.5-flash가
현재 실제로 서빙되는 걸 확인해서 그 값으로 고정했다.
"""
import asyncio
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_VISION_ANALYSIS_PROMPT = """You are an expert appraiser of used cameras and lenses. Examine the attached
product photos and specifically look for: lens fungus, water damage, scratches, and dents.

Based on your analysis, return:
- condition_grade: one of "Mint", "Excellent", "Very Good", "Fair"
- condition_score: an integer from 0 to 100 (closer to 100 means closer to new condition)
- has_lens_fungus / has_water_damage / has_scratches / has_dents: boolean for each defect
- notes: a short English-language summary of anything notable (empty string if nothing found)

Respond only in the specified JSON schema. All text output, including `notes`, must be in English."""


class GeminiVisionResult(BaseModel):
    """Gemini Structured Output 스키마."""

    condition_grade: Literal["Mint", "Excellent", "Very Good", "Fair"]
    condition_score: int = Field(ge=0, le=100)
    has_lens_fungus: bool
    has_water_damage: bool
    has_scratches: bool
    has_dents: bool
    notes: str = ""


# has_* 필드명 → 영어 표준 결함 라벨. camera_pipeline이 detected_defects를 만들 때 재사용한다.
DEFECT_FIELD_LABELS = {
    "has_lens_fungus": "Lens Fungus",
    "has_water_damage": "Water Damage",
    "has_scratches": "Scratches",
    "has_dents": "Body Dents",
}

# 매물 하나당 실제로 다운로드/분석할 이미지 최대 개수. 이미지가 많을수록
# Gemini 토큰 비용이 늘어나므로 유료 계정에서는 특히 상한을 둬야 한다.
MAX_IMAGES_PER_ITEM = 6

# 이미지 한 장의 최대 허용 용량 (바이트). 넘으면 다운로드 결과를 버린다.
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB

# 동시에 진행할 이미지 다운로드 수 제한 (소스 서버/커넥션 풀 보호).
_download_semaphore = asyncio.Semaphore(4)

# 동시에 진행할 Gemini generate_content 호출 수 제한 (분당 요청 한도 보호).
_gemini_call_semaphore = asyncio.Semaphore(3)

# 모델 드리프트 감지용: 마지막으로 확인한 실제 서빙 모델 버전.
# gemini_model을 리터럴 문자열로 고정해도, 구글이 그 이름 뒤의 스냅샷을 조용히
# 바꾸는 경우가 있을 수 있어 실제 응답의 model_version을 계속 관찰해둔다.
_last_seen_model_version: Optional[str] = None


def _check_model_drift(actual_version: Optional[str]) -> None:
    """직전 호출과 다른 model_version이 오면 경고 로그를 남긴다 (드리프트 조기 감지)."""
    global _last_seen_model_version
    if not actual_version:
        return
    if _last_seen_model_version is not None and actual_version != _last_seen_model_version:
        logger.warning(
            "[gemini] 모델 드리프트 감지: 이전 응답은 '%s'였는데 이번 응답은 '%s'. "
            "설정된 gemini_model='%s'가 실제로 다른 스냅샷으로 서빙되고 있을 수 있다.",
            _last_seen_model_version,
            actual_version,
            settings.gemini_model,
        )
    _last_seen_model_version = actual_version


async def _download_image(client: httpx.AsyncClient, url: str) -> Optional[tuple]:
    """이미지를 (bytes, mime_type)으로 다운로드한다.

    실패, 이미지가 아닌 Content-Type, 용량 초과 중 하나라도 해당하면 None을
    반환하고 건너뛴다 — 매물 하나의 이미지 한 장이 문제라고 전체 분석을
    막으면 안 되기 때문이다.
    """
    async with _download_semaphore:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("[gemini] 이미지 다운로드 실패: %s (%s)", url, exc)
            return None

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        logger.warning("[gemini] 이미지가 아닌 Content-Type이라 건너뜀: %s (%s)", url, content_type or "unknown")
        return None

    if len(response.content) > MAX_IMAGE_BYTES:
        logger.warning(
            "[gemini] 이미지 용량 초과(%.1fMB > %dMB)로 건너뜀: %s",
            len(response.content) / (1024 * 1024),
            MAX_IMAGE_BYTES // (1024 * 1024),
            url,
        )
        return None

    return response.content, content_type


async def analyze_condition_with_gemini(image_urls: list) -> GeminiVisionResult:
    """이미지 URL 배열을 Gemini Vision에 넘겨 외관 상태를 분석한다.

    image_urls가 MAX_IMAGES_PER_ITEM보다 많으면 앞에서부터 그만큼만 쓴다
    (스크레이퍼가 대표 이미지를 앞쪽에 두는 경향을 활용).
    """
    if not settings.gemini_api_key:
        logger.info("[gemini] GEMINI_API_KEY 미설정 → fallback 분석 사용")
        return _fallback_condition_analysis()

    urls_to_try = [str(url) for url in image_urls][:MAX_IMAGES_PER_ITEM]
    if not urls_to_try:
        logger.info("[gemini] 이미지 URL이 없어 fallback 분석 사용")
        return _fallback_condition_analysis()

    try:
        from google import genai
        from google.genai import types

        async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=True) as client:
            downloads = await asyncio.gather(*(_download_image(client, url) for url in urls_to_try))

        image_parts = [
            types.Part.from_bytes(data=data, mime_type=mime_type)
            for data, mime_type in (d for d in downloads if d is not None)
        ]
        if not image_parts:
            raise ValueError("분석 가능한 이미지를 하나도 가져오지 못함 (다운로드 실패/형식 부적합/용량 초과)")

        client_ai = genai.Client(api_key=settings.gemini_api_key)
        async with _gemini_call_semaphore:
            response = await client_ai.aio.models.generate_content(
                model=settings.gemini_model,
                contents=[_VISION_ANALYSIS_PROMPT, *image_parts],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GeminiVisionResult,
                ),
            )

        if not response.text:
            raise ValueError("Gemini 응답이 비어있음")

        _check_model_drift(getattr(response, "model_version", None))
        return GeminiVisionResult.model_validate_json(response.text)

    except Exception:
        logger.exception("[gemini] 비전 분석 실패 → fallback 분석 사용")
        return _fallback_condition_analysis()


def _fallback_condition_analysis() -> GeminiVisionResult:
    """API 미설정/실패 시 사용하는 중립적 기본값.

    실제 결함 판정이 아니라 파이프라인이 죽지 않도록 하는 안전장치이므로,
    낙관도 비관도 하지 않는 중간 등급(Very Good)과 결함 없음으로 채운다.
    """
    return GeminiVisionResult(
        condition_grade="Very Good",
        condition_score=70,
        has_lens_fungus=False,
        has_water_damage=False,
        has_scratches=False,
        has_dents=False,
        notes="AI vision analysis unavailable, returning a neutral default (check GEMINI_API_KEY or image accessibility)",
    )
