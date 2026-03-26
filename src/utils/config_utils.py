import os
from pathlib import Path
from typing import Any, Dict, Optional

from src.utils.logging_utils import log_warning


_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def _strip_inline_comment(value: str) -> str:
    in_single_quote = False
    in_double_quote = False
    result = []

    for char in value:
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif char == "#" and not in_single_quote and not in_double_quote:
            break
        result.append(char)

    return "".join(result).strip()


def _coerce_scalar(value: str) -> Any:
    normalized = value.strip()
    if not normalized:
        return ""

    if (normalized.startswith('"') and normalized.endswith('"')) or (
        normalized.startswith("'") and normalized.endswith("'")
    ):
        return normalized[1:-1]

    lowered = normalized.lower()
    if lowered in {"none", "null"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    try:
        if "." in normalized:
            return float(normalized)
        return int(normalized)
    except ValueError:
        return normalized


def _parse_simple_yaml(path: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                log_warning(f"忽略无法解析的配置行 {line_number}: {raw_line.rstrip()}")
                continue

            key, raw_value = line.split(":", 1)
            key = key.strip()
            if not key:
                log_warning(f"忽略缺少 key 的配置行 {line_number}: {raw_line.rstrip()}")
                continue

            value = _strip_inline_comment(raw_value)
            result[key] = _coerce_scalar(value)

    return result


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    try:
        project_root = Path(__file__).resolve().parents[2]
        cfg_path = project_root / "config.yml"
        if not cfg_path.exists():
            _CONFIG_CACHE = {}
            return _CONFIG_CACHE

        _CONFIG_CACHE = _parse_simple_yaml(cfg_path)
        return _CONFIG_CACHE
    except Exception as exc:
        log_warning(f"加载 config.yml 失败，将使用默认配置。原因：{exc}")
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE


def get_config_value(key: str, default: Optional[Any] = None) -> Any:
    env_value = os.getenv(key)
    if env_value is not None:
        return _coerce_scalar(env_value)

    cfg = load_config()
    return cfg.get(key, default)


def get_default_comment() -> str:
    value = get_config_value("default_comment", "None")
    if value is None:
        return "None"
    return str(value)


def get_dashscope_api_key() -> Optional[str]:
    key = str(get_config_value("DASHSCOPE_API_KEY", "") or "").strip()
    return key or None


def get_llm_model_name() -> str:
    return str(
        get_config_value(
            "LLM_MODEL",
            "qwen3-30b-a3b-thinking-2507",
        )
    )


def get_llm_base_url() -> str:
    return str(
        get_config_value(
            "LLM_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    )


def get_http_debug_enabled() -> bool:
    return bool(get_config_value("HTTP_DEBUG", False))


def get_http_debug_detail_enabled() -> bool:
    return bool(get_config_value("HTTP_DEBUG_DETAIL", False))
