"""
로깅 설정 모듈.

AI API 연동 전 단계이므로, 지금은 "무엇을 얼마나 수집했는지"를
콘솔 + 파일(logs/pipeline.log)에 남기는 것이 유일한 관측 수단이다.
추후 AI 분석/알림 단계가 붙어도 이 로거 설정은 그대로 재사용한다.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Windows 콘솔의 기본 코드페이지(cp949 등)는 en-dash(–)나 이모지처럼 흔한
# 유니코드 문자를 인코딩하지 못해 로깅 자체가 "Logging error"로 깨진다.
# 해외 매물 제목(이베이 등)에는 이런 문자가 흔히 섞여 있으므로, 콘솔 출력을
# UTF-8로 강제하고 그래도 안 되는 문자는 예외를 던지는 대신 이스케이프한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass


def get_logger(name: str) -> logging.Logger:
    """모듈별로 이름이 다른 로거를 반환한다 (예: get_logger(__name__))."""
    logger = logging.getLogger(name)

    if logger.handlers:
        # 이미 설정된 로거면 핸들러를 중복으로 붙이지 않는다.
        return logger

    logger.setLevel(settings.log_level)
    formatter = logging.Formatter(_LOG_FORMAT)

    # 콘솔 출력
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 파일 출력 (5MB씩 3개 롤링 백업)
    file_handler = RotatingFileHandler(
        LOG_DIR / "pipeline.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
