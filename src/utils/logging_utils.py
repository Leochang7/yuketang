import sys
import time


_MOJIBAKE_HINTS = (
    "寮€",
    "璇?",
    "鏄惁",
    "鍙栨秷",
    "澶辫触",
    "鎴愬姛",
    "褰撳墠",
    "璇剧▼",
    "瑙嗛",
    "绾跨▼",
    "璇ヤ换",
    "鎵弿",
    "閲嶈瘯",
    "纭",
)


def _configure_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_stdio()


def _maybe_fix_mojibake(message: str) -> str:
    if not isinstance(message, str):
        return str(message)

    if not any(hint in message for hint in _MOJIBAKE_HINTS):
        return message

    for source_encoding in ("gb18030", "gbk"):
        try:
            repaired = message.encode(source_encoding).decode("utf-8")
        except Exception:
            continue
        if repaired and repaired != message:
            return repaired

    return message


def log(message, level="INFO"):
    """统一输出格式。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    normalized = _maybe_fix_mojibake(str(message))
    print(f"[{timestamp}] [{level.upper()}] {normalized}")


def log_info(message):
    log(message, "INFO")


def log_warning(message):
    log(message, "WARN")


def log_error(message):
    log(message, "ERROR")


def log_success(message):
    log(message, "SUCCESS")
