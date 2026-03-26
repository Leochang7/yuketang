"""异步HTTP客户端模块，使用 aiohttp 替代 requests"""
import asyncio
import json
from typing import Dict, Optional, Any, Tuple
from aiohttp import ClientSession, ClientTimeout, CookieJar

from src.network.http_client import (
    SEPARATOR,
    SESSION_REFERER,
    SESSION_USER_AGENT,
)
from src.utils.logging_utils import log_error, log_info, log_success, log_warning
from src.utils.http_debug import log_http_failure, log_http_payload, log_http_success, now_ms


# 全局异步 session
_async_session: Optional[ClientSession] = None
_session_lock = asyncio.Lock()


async def get_async_session() -> ClientSession:
    """获取或创建全局异步 session"""
    global _async_session
    async with _session_lock:
        if _async_session is None or _async_session.closed:
            # 从现有 cookies 创建
            from src.network.http_client import session as sync_session

            cookie_jar = CookieJar()
            for cookie in sync_session.cookies:
                cookie_jar.update_cookies({cookie.name: cookie.value})

            timeout = ClientTimeout(total=30)
            _async_session = ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers={
                    'User-Agent': SESSION_USER_AGENT,
                    'Referer': SESSION_REFERER,
                }
            )
        return _async_session


async def close_async_session():
    """关闭异步 session"""
    global _async_session
    async with _session_lock:
        if _async_session and not _async_session.closed:
            await _async_session.close()
            _async_session = None


async def async_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    **kwargs
) -> Any:
    """异步 GET 请求"""
    session = await get_async_session()
    client_timeout = ClientTimeout(total=timeout)
    started = now_ms()
    try:
        async with session.get(url, headers=headers, params=params, timeout=client_timeout, **kwargs) as resp:
            content = await resp.read()
            log_http_success("GET", url, resp.status, now_ms() - started, params=params)
            if resp.headers.get('content-type', '').startswith('application/json'):
                payload = json.loads(content.decode(resp.charset or 'utf-8'))
                log_http_payload(url, payload)
                return payload
            return content
    except Exception as exc:
        log_http_failure("GET", url, exc, now_ms() - started, params=params)
        raise


async def async_post(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[str] = None,
    json: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    **kwargs
) -> Any:
    """异步 POST 请求"""
    session = await get_async_session()
    client_timeout = ClientTimeout(total=timeout)
    started = now_ms()
    try:
        async with session.post(url, headers=headers, data=data, json=json, timeout=client_timeout, **kwargs) as resp:
            content = await resp.read()
            log_http_success("POST", url, resp.status, now_ms() - started)
            if resp.headers.get('content-type', '').startswith('application/json'):
                payload = json.loads(content.decode(resp.charset or 'utf-8'))
                log_http_payload(url, payload)
                return payload
            return content
    except Exception as exc:
        log_http_failure("POST", url, exc, now_ms() - started)
        raise


async def async_get_text(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    **kwargs
) -> str:
    """异步 GET 请求，返回文本"""
    session = await get_async_session()
    client_timeout = ClientTimeout(total=timeout)
    started = now_ms()
    try:
        async with session.get(url, headers=headers, params=params, timeout=client_timeout, **kwargs) as resp:
            text = await resp.text()
            log_http_success("GET", url, resp.status, now_ms() - started, params=params)
            return text
    except Exception as exc:
        log_http_failure("GET", url, exc, now_ms() - started, params=params)
        raise


async def async_get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    **kwargs
) -> Dict[str, Any]:
    """异步 GET 请求，返回 JSON"""
    session = await get_async_session()
    client_timeout = ClientTimeout(total=timeout)
    started = now_ms()
    try:
        async with session.get(url, headers=headers, params=params, timeout=client_timeout, **kwargs) as resp:
            data = await resp.json()
            log_http_success("GET", url, resp.status, now_ms() - started, params=params)
            log_http_payload(url, data)
            return data
    except Exception as exc:
        log_http_failure("GET", url, exc, now_ms() - started, params=params)
        raise


async def async_post_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    **kwargs
) -> Dict[str, Any]:
    """异步 POST 请求，发送和返回 JSON"""
    session = await get_async_session()
    client_timeout = ClientTimeout(total=timeout)
    started = now_ms()
    try:
        async with session.post(url, headers=headers, json=data, timeout=client_timeout, **kwargs) as resp:
            result = await resp.json()
            log_http_success("POST", url, resp.status, now_ms() - started)
            log_http_payload(url, result)
            return result
    except Exception as exc:
        log_http_failure("POST", url, exc, now_ms() - started)
        raise
