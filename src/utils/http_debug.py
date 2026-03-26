import time
from typing import Any, Optional
from urllib.parse import urlencode

from src.utils.config_utils import get_config_value
from src.utils.logging_utils import log_info, log_warning


def is_http_debug_enabled() -> bool:
    return bool(get_config_value("HTTP_DEBUG", False))


def is_http_debug_detail_enabled() -> bool:
    return bool(get_config_value("HTTP_DEBUG_DETAIL", False))


def _truncate(value: str, max_length: int = 240) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


def _render_url(url: str, params: Optional[Any] = None) -> str:
    if not params:
        return url
    try:
        query = urlencode(params, doseq=True)
    except Exception:
        query = str(params)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def log_http_success(method: str, url: str, status_code: int, elapsed_ms: float, params: Optional[Any] = None) -> None:
    if not is_http_debug_enabled():
        return
    full_url = _truncate(_render_url(url, params=params))
    log_info(f"[HTTP] {method.upper()} {status_code} {elapsed_ms:.0f}ms {full_url}")


def log_http_failure(method: str, url: str, exc: Exception, elapsed_ms: float, params: Optional[Any] = None) -> None:
    if not is_http_debug_enabled():
        return
    full_url = _truncate(_render_url(url, params=params))
    log_warning(f"[HTTP] {method.upper()} ERROR {elapsed_ms:.0f}ms {full_url} :: {exc}")


def _summarize_leaf_info(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or {}
    content_info = data.get("content_info") or {}
    media = content_info.get("media") or {}
    summary = {
        "success": payload.get("success"),
        "msg": payload.get("msg"),
        "leaf_id": data.get("id"),
        "classroom_id": data.get("classroom_id"),
        "course_id": data.get("course_id"),
        "user_id": data.get("user_id"),
        "leaf_type": data.get("leaf_type"),
        "sku_id": data.get("sku_id"),
        "duration": media.get("duration"),
        "ccid": media.get("ccid"),
    }
    return _truncate(str(summary), max_length=400)


def _summarize_progress(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    inner = payload.get("data")
    if isinstance(inner, dict) and inner:
        first_value = next(iter(inner.values()))
    else:
        first_value = payload if payload else {}
    if not isinstance(first_value, dict):
        return None
    summary = {
        "watch_length": first_value.get("watch_length"),
        "video_length": first_value.get("video_length"),
        "completed": first_value.get("completed"),
        "rate": first_value.get("rate"),
        "last_point": first_value.get("last_point"),
    }
    return _truncate(str(summary), max_length=320)


def _summarize_heartbeat(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        return _truncate(str(payload), max_length=240)
    return None


def _summarize_video_detail(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    heartbeat = payload.get("data", {}).get("heartbeat", {})
    result = heartbeat.get("result") or []
    if not isinstance(result, list):
        result = []
    summary = {
        "success": payload.get("success"),
        "completed": heartbeat.get("completed"),
        "rate": heartbeat.get("rate"),
        "video_length": heartbeat.get("video_length"),
        "range_count": len(result),
        "last_point": heartbeat.get("last_point"),
        "cumulative_watch_length": heartbeat.get("cumulative_watch_length"),
    }
    return _truncate(str(summary), max_length=320)


def log_http_payload(url: str, payload: Any) -> None:
    if not is_http_debug_detail_enabled():
        return

    summary: Optional[str] = None
    if "leaf_info/" in url:
        summary = _summarize_leaf_info(payload)
    elif "get_video_watch_progress" in url:
        summary = _summarize_progress(payload)
    elif "video-log/heartbeat" in url:
        summary = _summarize_heartbeat(payload)
    elif "video-log/detail" in url:
        summary = _summarize_video_detail(payload)

    if summary:
        log_info(f"[HTTP-DATA] {summary}")


def now_ms() -> float:
    return time.perf_counter() * 1000.0
