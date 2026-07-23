"""
처리된 카메라 매물(ProcessedCameraItem)을 보관하는 인메모리 캐시.

아직 DB가 없으므로 재시작하면 초기화된다. `/api/v1/signals`는 이 캐시를
조회해서 필터링/정렬만 담당하고, 실제 값 채우기는 파이프라인 실행(현재는
`/api/v1/test-feed`, 추후에는 실제 스크레이핑 잡)이 담당한다.
실제 DB(Postgres 등)로 교체할 때는 이 파일의 함수 시그니처만 유지하고
내부 구현만 바꾸면 다른 모듈은 건드릴 필요가 없다.

`item_id` 기준 dict로 관리한다(과거엔 단순 list라 같은 매물이 스케줄러
사이클마다 중복으로 계속 쌓였고, discovered_at도 매번 새로 찍혀서 free-signals의
30분 지연 로직이 사실상 영구히 통과하지 못했다 — 실매물은 팔릴 때까지 여러
사이클에 걸쳐 반복 발견되므로 이 버그가 실사용에서도 그대로 터졌을 것).
"""
from app.schemas.camera import ProcessedCameraItem

_MAX_CACHE_SIZE = 500
_processed_items: dict[str, ProcessedCameraItem] = {}


def cache_processed_item(item: ProcessedCameraItem) -> None:
    """item_id 기준으로 upsert한다. 이미 있던 매물이면 discovered_at(최초 발견
    시각)은 그대로 유지하고 나머지 필드만 최신 값으로 갱신한다 — free-signals의
    30분 지연 판정이 "최초 발견 시점"을 기준으로 정상 동작하려면 필수적이다."""
    item_id = item.raw.item_id
    existing = _processed_items.get(item_id)
    if existing is not None:
        item = item.model_copy(update={"discovered_at": existing.discovered_at})
    _processed_items[item_id] = item

    if len(_processed_items) > _MAX_CACHE_SIZE:
        oldest_item_id = next(iter(_processed_items))
        del _processed_items[oldest_item_id]


def get_all_processed_items() -> list:
    return list(_processed_items.values())


def is_cache_empty() -> bool:
    return len(_processed_items) == 0
