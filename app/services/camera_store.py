"""
처리된 카메라 매물(ProcessedCameraItem)을 보관하는 인메모리 캐시.

아직 DB가 없으므로 재시작하면 초기화된다. `/api/v1/signals`는 이 캐시를
조회해서 필터링/정렬만 담당하고, 실제 값 채우기는 파이프라인 실행(현재는
`/api/v1/test-feed`, 추후에는 실제 스크레이핑 잡)이 담당한다.
실제 DB(Postgres 등)로 교체할 때는 이 파일의 함수 시그니처만 유지하고
내부 구현만 바꾸면 다른 모듈은 건드릴 필요가 없다.
"""
from app.schemas.camera import ProcessedCameraItem

_MAX_CACHE_SIZE = 500
_processed_items: list = []


def cache_processed_item(item: ProcessedCameraItem) -> None:
    """새로 처리된 매물을 캐시에 추가한다 (오래된 항목부터 자동 정리)."""
    _processed_items.append(item)
    del _processed_items[: max(0, len(_processed_items) - _MAX_CACHE_SIZE)]


def get_all_processed_items() -> list:
    return list(_processed_items)


def is_cache_empty() -> bool:
    return len(_processed_items) == 0
