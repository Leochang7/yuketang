import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from requests.cookies import create_cookie

from src.network.http_client import session
from src.utils.logging_utils import log_error, log_info
from src.utils.http_debug import log_http_failure, log_http_success, now_ms


COOKIE_FILE = Path("cookies.json")


@dataclass(frozen=True)
class LoginStatus:
    is_valid: bool
    reason: str
    course_count: Optional[int] = None


def _serialize_cookies() -> Dict[str, Any]:
    cookies: List[Dict[str, Any]] = []
    for cookie in session.cookies:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
        )
    return {"format": "cookiejar", "cookies": cookies}


def save_cookies() -> None:
    try:
        COOKIE_FILE.write_text(
            json.dumps(_serialize_cookies(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log_info("已保存登录 cookies。")
    except Exception as exc:
        log_error(f"保存 cookies 失败：{exc}")


def _load_legacy_cookie_map(cookie_map: Dict[str, Any]) -> None:
    for name, value in cookie_map.items():
        if value is None:
            continue
        session.cookies.set(name, str(value), domain=".yuketang.cn", path="/")


def _iter_serialized_cookies(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    cookies = payload.get("cookies", [])
    if isinstance(cookies, list):
        for cookie in cookies:
            if isinstance(cookie, dict):
                yield cookie


def load_cookies() -> None:
    if not COOKIE_FILE.exists():
        return

    try:
        payload = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log_error(f"加载 cookies 失败：{exc}")
        return

    try:
        session.cookies.clear()
        if isinstance(payload, dict) and payload.get("format") == "cookiejar":
            for cookie_data in _iter_serialized_cookies(payload):
                name = cookie_data.get("name")
                value = cookie_data.get("value")
                if not name or value is None:
                    continue
                cookie = create_cookie(
                    name=str(name),
                    value=str(value),
                    domain=str(cookie_data.get("domain") or ".yuketang.cn"),
                    path=str(cookie_data.get("path") or "/"),
                    secure=bool(cookie_data.get("secure", False)),
                    expires=cookie_data.get("expires"),
                )
                session.cookies.set_cookie(cookie)
        elif isinstance(payload, dict):
            _load_legacy_cookie_map(payload)
        else:
            raise ValueError("cookies.json 格式不受支持")

        log_info("已从本地加载 cookies。")
    except Exception as exc:
        log_error(f"应用 cookies 失败：{exc}")


def get_cookie_value(name: str, preferred_domains: Optional[Iterable[str]] = None) -> Optional[str]:
    domain_order = list(preferred_domains or ("www.yuketang.cn", ".yuketang.cn", ""))
    matched: List[Any] = []

    for cookie in session.cookies:
        if cookie.name != name:
            continue
        matched.append(cookie)

    if not matched:
        return None

    for domain in domain_order:
        for cookie in matched:
            cookie_domain = (cookie.domain or "").lstrip(".")
            normalized_domain = domain.lstrip(".")
            if not normalized_domain or cookie_domain == normalized_domain:
                return str(cookie.value)

    return str(matched[0].value)


def _extract_auth_message(data: Dict[str, Any]) -> Optional[str]:
    for key in ("detail", "message", "msg", "errmsg"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_login_status(timeout: int = 10) -> LoginStatus:
    url = "https://www.yuketang.cn/v2/api/web/courses/list?identity=2"
    started = now_ms()
    try:
        resp = session.get(url, timeout=timeout)
        log_http_success("GET", url, resp.status_code, now_ms() - started)
    except Exception as exc:
        log_http_failure("GET", url, exc, now_ms() - started)
        return LoginStatus(False, f"检测 cookies 有效性时网络异常：{exc}")

    if resp.status_code != 200:
        return LoginStatus(False, f"检测 cookies 有效性失败，状态码：{resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        return LoginStatus(False, "检测 cookies 有效性失败，响应非 JSON。")

    course_list = data.get("data", {}).get("list")
    if isinstance(course_list, list):
        if course_list:
            return LoginStatus(True, "检测到已有有效登录状态，将复用本地 cookies。", len(course_list))
        return LoginStatus(True, "检测到有效登录状态，但当前课程列表为空。", 0)

    auth_message = _extract_auth_message(data)
    if auth_message:
        lowered = auth_message.lower()
        auth_markers = ("login", "auth", "token", "credential", "登录", "认证", "未登录", "失效")
        if any(marker in lowered for marker in auth_markers):
            return LoginStatus(False, f"当前 cookies 已失效或未登录：{auth_message}")

    if isinstance(data.get("data"), dict):
        return LoginStatus(True, "课程列表接口返回有效 JSON，暂按已登录处理。")

    return LoginStatus(False, "当前 cookies 已失效或未登录，需要重新扫码登录。")


def are_cookies_valid() -> bool:
    return get_login_status().is_valid
