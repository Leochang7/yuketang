import json
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

import requests

from src.network.http_client import SEPARATOR, SESSION_REFERER, SESSION_USER_AGENT, session
from src.utils.logging_utils import log_error, log_info, log_success, log_warning
from src.auth.cookies_manager import get_cookie_value
from src.core.course_selection import CourseSelection
from src.utils.http_debug import log_http_failure, log_http_payload, log_http_success, now_ms

_thread_local = threading.local()


def _clone_session() -> requests.Session:
    cloned = requests.Session()
    cloned.trust_env = False
    cloned.cookies.update(session.cookies)
    return cloned


def _get_worker_session() -> requests.Session:
    worker_session = getattr(_thread_local, "session", None)
    if worker_session is None:
        worker_session = _clone_session()
        _thread_local.session = worker_session
    return worker_session


def _thread_safe_get(url, **kwargs):
    """为当前线程提供独立 session，避免全局锁串行化网络请求。"""
    started = now_ms()
    try:
        response = _get_worker_session().get(url, **kwargs)
        log_http_success("GET", url, response.status_code, now_ms() - started, params=kwargs.get("params"))
        try:
            log_http_payload(url, response.json())
        except Exception:
            pass
        return response
    except Exception as exc:
        log_http_failure("GET", url, exc, now_ms() - started, params=kwargs.get("params"))
        raise


def _thread_safe_post(url, **kwargs):
    """为当前线程提供独立 session，避免全局锁串行化网络请求。"""
    started = now_ms()
    try:
        response = _get_worker_session().post(url, **kwargs)
        log_http_success("POST", url, response.status_code, now_ms() - started, params=kwargs.get("params"))
        try:
            log_http_payload(url, response.json())
        except Exception:
            pass
        return response
    except Exception as exc:
        log_http_failure("POST", url, exc, now_ms() - started, params=kwargs.get("params"))
        raise


def _get_csrf_token():
    """从当前 session.cookies 中尝试提取 csrf token"""
    candidates = ['csrftoken', 'csrf_token', 'csrfmiddlewaretoken']
    for name in candidates:
        value = get_cookie_value(name)
        if value:
            return value
    return None


def _get_video_duration_with_retry(
    classroom_id: str,
    video_id: str,
    c_course_id: str,
    user_id: str,
    headers: Dict[str, str],
    max_retries: int = 3,
) -> tuple[int, Dict[str, Any]]:
    duration = 0
    video_data: Dict[str, Any] = {}

    for retry in range(max_retries):
        try:
            leaf_info_url = (
                'https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/'
                f"{classroom_id}/{video_id}/"
            )
            leaf_info = _thread_safe_get(leaf_info_url, headers=headers, timeout=10).json()
            media = leaf_info.get('data', {}).get('content_info', {}).get('media') or {}
            duration = int(media.get('duration', 0) or 0)

            progress_url = (
                "https://www.yuketang.cn/video-log/get_video_watch_progress/"
                f"?cid={c_course_id}&user_id={user_id}&classroom_id={classroom_id}"
                f"&video_type=video&vtype=rate&video_id={video_id}&snapshot=1"
            )
            progress_response = _thread_safe_get(progress_url, headers=headers, timeout=10).json()
            video_data = progress_response.get('data', {}).get(video_id, {}) or progress_response.get(video_id, {})

            if duration <= 0:
                duration = int(video_data.get('video_length', 0) or 0)

            if duration > 0:
                return duration, video_data

            if retry < max_retries - 1:
                wait_time = (retry + 1) * 2
                log_warning(f"视频 {video_id} 时长获取失败，{wait_time}秒后重试 ({retry + 1}/{max_retries})")
                time.sleep(wait_time)
        except Exception as exc:
            if retry < max_retries - 1:
                wait_time = (retry + 1) * 2
                log_warning(f"获取视频信息异常：{exc}，{wait_time}秒后重试 ({retry + 1}/{max_retries})")
                time.sleep(wait_time)

    return duration, video_data


def _bootstrap_video_duration(
    video_id: str,
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    user_id: str,
    video_leaf_id: str,
    ccid: str,
    headers: Dict[str, str],
    thread_lock: threading.Lock,
    thread_id: int,
    bootstrap_points: Tuple[int, ...] = (8, 18, 30),
    sleep_interval: float = 0.8,
) -> tuple[int, Dict[str, Any]]:
    """在服务端尚未返回 duration 时，先发几次小步进心跳触发真实 video_length。"""
    heartbeat_url = 'https://www.yuketang.cn/video-log/heartbeat/'
    heartbeat_headers = {
        'User-Agent': SESSION_USER_AGENT,
        'Content-Type': 'application/json',
        'authority': 'changjiang.yuketang.cn',
        'referer': SESSION_REFERER,
    }
    progress_url = (
        "https://www.yuketang.cn/video-log/get_video_watch_progress/"
        f"?cid={c_course_id}&user_id={user_id}&classroom_id={classroom_id}"
        f"&video_type=video&vtype=rate&video_id={video_id}&snapshot=1"
    )

    guessed_duration = 600
    base_ts = int(time.time() * 1000)
    for attempt, cp in enumerate(bootstrap_points, start=1):
        payload = {
            "heart_data": [{
                "i": random.randint(3, 8),
                "et": "heartbeat",
                "p": "web",
                "n": "ali-cdn.xuetangx.com",
                "lob": "ykt",
                "cp": cp,
                "fp": 95,
                "tp": 100,
                "sp": 5,
                "ts": str(base_ts + attempt * 4000),
                "u": int(user_id),
                "uip": "",
                "c": int(c_course_id),
                "v": int(video_leaf_id),
                "skuid": int(s_id),
                "classroomid": classroom_id,
                "cc": ccid,
                "d": guessed_duration,
                "pg": video_id + "_x33v",
                "sq": random.randint(8, 15),
                "t": "video",
                "cards_id": 0,
                "slide": 0,
                "v_url": ""
            }]
        }

        try:
            _thread_safe_post(
                url=heartbeat_url,
                data=json.dumps(payload),
                headers=heartbeat_headers,
                timeout=10,
            )
        except Exception as exc:
            with thread_lock:
                log_warning(f"[线程{thread_id}] 视频 {video_id} 启动心跳失败（第{attempt}次）：{exc}")
            continue

        time.sleep(sleep_interval)

        try:
            response = _thread_safe_get(progress_url, headers=headers, timeout=10)
            progress_response = response.json()
            video_data = progress_response.get('data', {}).get(video_id, {}) or progress_response.get(video_id, {})
            video_length = int(video_data.get('video_length', 0) or 0)
            if video_length > 0:
                with thread_lock:
                    log_info(f"[线程{thread_id}] 视频 {video_id} 启动心跳成功，已获取真实时长 {video_length} 秒。")
                return video_length, video_data
        except Exception as exc:
            with thread_lock:
                log_warning(f"[线程{thread_id}] 视频 {video_id} 启动后查询进度失败（第{attempt}次）：{exc}")

    return 0, {}


def _calculate_watch_coverage(watch_len: float, video_len: int) -> float:
    if not video_len or video_len <= 0:
        return 0.0
    return min(100.0, (watch_len / video_len) * 100.0)


def _probe_video_status(
    video: Dict[str, Any],
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    thread_lock: threading.Lock,
    thread_id: int,
) -> Dict[str, Any]:
    video_id = str(video.get("video_id") or video.get("id"))
    headers = {
        "xtbz": "ykt",
        "classroom-id": str(classroom_id),
    }

    result: Dict[str, Any] = {
        "video": video,
        "status": "unknown",
        "coverage": None,
        "duration": 0,
        "reason": "",
    }

    try:
        leaf_info_url = (
            "https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/"
            f"{classroom_id}/{video_id}/"
        )
        leaf_info = _thread_safe_get(leaf_info_url, headers=headers, timeout=10).json()
        media = leaf_info.get("data", {}).get("content_info", {}).get("media", {}) or {}
        ccid = str(media.get("ccid") or "")
        video_leaf_id = str(leaf_info.get("data", {}).get("id") or video_id)
        user_id = str(leaf_info.get("data", {}).get("user_id") or "")

        try:
            _thread_safe_get(
                f"https://www.yuketang.cn/v2/web/player/{classroom_id}/{video_id}",
                headers=headers.copy(),
                timeout=10,
            )
        except Exception:
            pass

        duration, video_data = _get_video_duration_with_retry(
            classroom_id=classroom_id,
            video_id=video_id,
            c_course_id=c_course_id,
            user_id=user_id,
            headers=headers,
        )
        watched_seconds = float(video_data.get("watch_length", 0) or 0)
        completed_flag = int(video_data.get("completed", 0) or 0)

        if duration <= 0 and ccid and user_id:
            duration, video_data = _bootstrap_video_duration(
                video_id=video_id,
                classroom_id=classroom_id,
                c_course_id=c_course_id,
                s_id=s_id,
                user_id=user_id,
                video_leaf_id=video_leaf_id,
                ccid=ccid,
                headers=headers,
                thread_lock=thread_lock,
                thread_id=thread_id,
                bootstrap_points=(8, 18, 30),
                sleep_interval=0.6,
            )
            watched_seconds = float(video_data.get("watch_length", 0) or watched_seconds)
            completed_flag = int(video_data.get("completed", 0) or completed_flag)

        detail_coverage: Optional[float] = None
        uncovered_ranges: List[Tuple[float, float]] = []
        if duration > 0:
            detail_payload = _fetch_video_detail(
                classroom_id=classroom_id,
                user_id=user_id,
                video_id=video_id,
                university_id=university_id,
                headers=headers,
            )
            if detail_payload:
                detail_coverage, uncovered_ranges = _extract_coverage_status(detail_payload, duration)

        # 对状态未知的视频做更强一轮预热和回查，再下结论。
        if duration <= 0 or detail_coverage is None:
            if ccid and user_id:
                duration, video_data = _bootstrap_video_duration(
                    video_id=video_id,
                    classroom_id=classroom_id,
                    c_course_id=c_course_id,
                    s_id=s_id,
                    user_id=user_id,
                    video_leaf_id=video_leaf_id,
                    ccid=ccid,
                    headers=headers,
                    thread_lock=thread_lock,
                    thread_id=thread_id,
                    bootstrap_points=(12, 36, 90, 180, 300),
                    sleep_interval=0.5,
                )
                watched_seconds = float(video_data.get("watch_length", 0) or watched_seconds)
                completed_flag = int(video_data.get("completed", 0) or completed_flag)
                if duration > 0:
                    detail_payload = _fetch_video_detail(
                        classroom_id=classroom_id,
                        user_id=user_id,
                        video_id=video_id,
                        university_id=university_id,
                        headers=headers,
                    )
                    if detail_payload:
                        detail_coverage, uncovered_ranges = _extract_coverage_status(detail_payload, duration)

        result["duration"] = duration
        base_coverage = _calculate_watch_coverage(watched_seconds, duration)
        effective_coverage = detail_coverage if detail_coverage is not None else base_coverage
        result["coverage"] = effective_coverage if duration > 0 else None

        if duration > 0 and effective_coverage >= 100.0:
            result["status"] = "completed"
            return result

        if duration > 0:
            result["status"] = "pending"
            if uncovered_ranges:
                result["reason"] = f"{len(uncovered_ranges)} 个未覆盖区间"
            elif completed_flag == 1:
                result["reason"] = "服务器已标记完成但覆盖率不足"
            else:
                result["reason"] = "覆盖率不足"
            return result

        result["reason"] = "未探测到稳定的时长和覆盖率"
        return result
    except Exception as exc:
        result["reason"] = str(exc)
        return result


def scan_videos_for_completion(
    videos: List[Dict[str, Any]],
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    max_workers: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    if not videos:
        return [], 0, 0

    worker_count = max(1, min(5, min(int(max_workers), len(videos))))
    thread_lock = threading.Lock()
    pending_videos: List[Dict[str, Any]] = []
    completed_count = 0
    unknown_count = 0

    log_info(f"课程《{course_name}》开始扫描视频覆盖率，共 {len(videos)} 个视频。")

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="VideoProbe") as executor:
        futures = [
            executor.submit(
                _probe_video_status,
                video,
                classroom_id,
                c_course_id,
                s_id,
                university_id,
                course_name,
                thread_lock,
                index,
            )
            for index, video in enumerate(videos, start=1)
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                log_warning(f"课程《{course_name}》扫描任务异常：{exc}")
                unknown_count += 1
                continue

            status = result.get("status")
            coverage = result.get("coverage")
            video = result["video"]
            video_id = str(video.get("video_id") or video.get("id"))

            if status == "completed":
                completed_count += 1
                log_info(f"视频 {video_id} 已完成，覆盖率 {coverage:.1f}%")
                continue

            pending_videos.append(video)
            if status == "unknown":
                unknown_count += 1
                log_warning(
                    f"视频 {video_id} 状态仍不稳定，纳入补刷队列。原因：{result.get('reason') or '未知'}"
                )
            else:
                log_info(
                    f"视频 {video_id} 当前覆盖率 {coverage:.1f}% ，纳入补刷队列。"
                )

    log_info(
        f"课程《{course_name}》扫描完成：已完成 {completed_count} 个，"
        f"待补刷 {len(pending_videos)} 个，其中状态未判稳 {unknown_count} 个。"
    )
    return pending_videos, completed_count, unknown_count


def _fetch_video_detail(
    classroom_id: str,
    user_id: str,
    video_id: str,
    university_id: int,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    detail_url_candidates = (
        "https://yjsbjtu.yuketang.cn/video-log/detail/",
        "https://www.yuketang.cn/video-log/detail/",
        "https://changjiang.yuketang.cn/video-log/detail/",
    )
    params = {
        "classroom_id": classroom_id,
        "user_id": user_id,
        "video_id": video_id,
        "term": "latest",
        "uv_id": university_id,
    }

    for detail_url in detail_url_candidates:
        try:
            payload = _thread_safe_get(detail_url, params=params, headers=headers, timeout=10).json()
        except Exception:
            continue
        if payload.get("success") and isinstance(payload.get("data"), dict):
            return payload
    return {}


def _merge_coverage_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not ranges:
        return []

    normalized = []
    for start, end in sorted(ranges, key=lambda item: item[0]):
        start = max(0.0, float(start))
        end = max(start, float(end))
        if not normalized:
            normalized.append((start, end))
            continue

        prev_start, prev_end = normalized[-1]
        if start <= prev_end + 1.0:
            normalized[-1] = (prev_start, max(prev_end, end))
        else:
            normalized.append((start, end))

    return normalized


def _extract_coverage_status(detail_payload: Dict[str, Any], video_length: int) -> Tuple[Optional[float], List[Tuple[float, float]]]:
    heartbeat = detail_payload.get("data", {}).get("heartbeat", {})
    detail_video_length = int(heartbeat.get("video_length", 0) or 0)
    total_length = int(video_length or detail_video_length or 0)
    if total_length <= 0:
        return None, []

    raw_ranges = heartbeat.get("result") or []
    ranges: List[Tuple[float, float]] = []
    for item in raw_ranges:
        if not isinstance(item, dict):
            continue
        start = item.get("s")
        end = item.get("e")
        if start is None or end is None:
            continue
        try:
            ranges.append((float(start), float(end)))
        except (TypeError, ValueError):
            continue

    merged = _merge_coverage_ranges(ranges)
    if not merged:
        return 0.0, [(0.0, float(total_length))]

    uncovered: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        start = min(max(0.0, start), float(total_length))
        end = min(max(start, end), float(total_length))
        if start > cursor:
            uncovered.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_length:
        uncovered.append((cursor, float(total_length)))

    uncovered = [(start, end) for start, end in uncovered if end - start >= 1.0]
    uncovered_total = sum(end - start for start, end in uncovered)
    coverage = max(0.0, min(100.0, (1.0 - uncovered_total / float(total_length)) * 100.0))
    return coverage, uncovered


def _watch_single_video(
    video_id: str,
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    thread_lock: threading.Lock,
    fast_mode: bool = False,
    thread_id: int = 0
) -> bool:
    """
    单视频刷取函数，用于多线程调用。
    """
    video_id_str = str(video_id)
    start_time = time.time()  # 记录开始时间
    headers = {
        'xtbz': 'ykt',
        'classroom-id': str(classroom_id)
    }

    with thread_lock:
        log_info(f"[线程{thread_id}] {course_name} 视频{video_id_str} 开始刷取...")

    try:
        # 预加载视频缓存
        url = (
            'https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/'
            f"{classroom_id}/{video_id_str}/"
        )
        _thread_safe_get(url=url, headers=headers, timeout=10)
        time.sleep(0.5)
        _thread_safe_get(
            f"https://www.yuketang.cn/v2/web/player/{classroom_id}/{video_id_str}",
            headers=headers.copy(),
            timeout=10
        )

        # 获取视频信息
        response = _thread_safe_get(url=url, headers=headers, timeout=10)
        leaf_info = response.json()
        ccid = leaf_info['data']['content_info']['media']['ccid']
        v = str(leaf_info['data']['id'])
        u = str(leaf_info['data']['user_id'])

        d, video_data = _get_video_duration_with_retry(
            classroom_id,
            video_id_str,
            c_course_id,
            u,
            headers,
        )

        completed_flag = video_data.get('completed', 0)
        watched_seconds = video_data.get('watch_length', 0)

        if not d or d <= 0:
            with thread_lock:
                log_warning(f"[线程{thread_id}] 视频 {video_id_str} 未获取到有效时长，尝试从进度接口再次获取...")

            progress_url = (
                "https://www.yuketang.cn/video-log/get_video_watch_progress/"
                f"?cid={c_course_id}&user_id={u}&classroom_id={classroom_id}"
                f"&video_type=video&vtype=rate&video_id={video_id_str}&snapshot=1"
            )
            try:
                response_new = _thread_safe_get(progress_url, headers=headers, timeout=10)
                progress_response = response_new.json()
                video_data = progress_response.get('data', {}).get(video_id_str, {}) or progress_response.get(video_id_str, {})
                d = int(video_data.get('video_length', 0))
                watched_seconds = video_data.get('watch_length', 0)
                completed_flag = video_data.get('completed', 0)
            except Exception:
                pass

            if not d or d <= 0:
                try:
                    time.sleep(1.0)
                    response_retry = _thread_safe_get(progress_url, headers=headers, timeout=10)
                    retry_payload = response_retry.json()
                    retry_video_data = retry_payload.get('data', {}).get(video_id_str, {}) or retry_payload.get(video_id_str, {})
                    retry_length = int(retry_video_data.get('video_length', 0))
                    if retry_length > 0:
                        d = retry_length
                        video_data = retry_video_data
                        watched_seconds = retry_video_data.get('watch_length', 0)
                        completed_flag = retry_video_data.get('completed', 0)
                except Exception:
                    pass

            if not d or d <= 0:
                bootstrap_duration, bootstrap_data = _bootstrap_video_duration(
                    video_id=video_id_str,
                    classroom_id=classroom_id,
                    c_course_id=c_course_id,
                    s_id=s_id,
                    user_id=u,
                    video_leaf_id=v,
                    ccid=ccid,
                    headers=headers,
                    thread_lock=thread_lock,
                    thread_id=thread_id,
                )
                if bootstrap_duration > 0:
                    d = bootstrap_duration
                    video_data = bootstrap_data
                    watched_seconds = bootstrap_data.get('watch_length', 0)
                    completed_flag = bootstrap_data.get('completed', 0)

            if not d or d <= 0:
                with thread_lock:
                    log_warning(f"[线程{thread_id}] 视频 {video_id_str} 仍未获取到有效时长，跳过。")
                return False

        def calculate_coverage(watch_len, video_len):
            if not video_len or video_len <= 0:
                return 0.0
            return min(100.0, (watch_len / video_len) * 100.0)

        detail_coverage: Optional[float] = None
        uncovered_ranges: List[Tuple[float, float]] = []

        def refresh_detail_coverage() -> Optional[float]:
            nonlocal detail_coverage, uncovered_ranges
            detail_payload = _fetch_video_detail(
                classroom_id=classroom_id,
                user_id=u,
                video_id=video_id_str,
                university_id=university_id,
                headers=headers,
            )
            if not detail_payload:
                return detail_coverage

            detail_coverage, uncovered_ranges = _extract_coverage_status(detail_payload, d)
            return detail_coverage

        def get_effective_coverage() -> float:
            base_coverage = calculate_coverage(watched_seconds, d)
            if detail_coverage is None:
                return base_coverage
            return detail_coverage

        def get_next_gap_target() -> Optional[Tuple[float, float, float]]:
            if not uncovered_ranges:
                return None
            gap_start, gap_end = max(uncovered_ranges, key=lambda item: item[1] - item[0])
            gap_length = gap_end - gap_start
            target_start = max(0.0, gap_start - 6.0)
            target_end = min(float(d), gap_end + 10.0)
            if target_end <= target_start:
                return None
            return target_start, target_end, gap_length

        COVERAGE_THRESHOLD = 100.0
        initial_coverage = calculate_coverage(watched_seconds, d)
        if initial_coverage >= 95.0 or completed_flag == 1:
            detail_value = refresh_detail_coverage()
            if detail_value is not None:
                initial_coverage = detail_value

        def is_video_completed(watch_len, video_len, server_completed):
            coverage = get_effective_coverage()
            if coverage >= COVERAGE_THRESHOLD:
                return True
            return False

        if is_video_completed(watched_seconds, d, completed_flag):
            with thread_lock:
                log_info(f"[线程{thread_id}] 视频 {video_id_str} 覆盖率已达标（{initial_coverage:.1f}%），跳过。")
            return True

        if completed_flag == 1:
            with thread_lock:
                log_warning(f"[线程{thread_id}] 视频 {video_id_str} 覆盖率仅 {initial_coverage:.1f}%，继续刷课。")

        timestamp_ms = int(time.time() * 1000)
        current_cp = watched_seconds if watched_seconds else random.uniform(
            5, min(60, max(10, d * 0.1)))

        # 根据模式选择参数
        if fast_mode:
            # 快速模式 - 风险更高但更快
            simulated_rate = random.uniform(1.5, 2.5)
            min_interval = 0.2
            max_interval = 0.5
            increment_min = d * 0.03
            increment_max = d * 0.1
            query_every_n = 5  # 每5次心跳才查询一次进度
        else:
            # 原始模式 - 安全但慢
            simulated_rate = random.uniform(0.9, 1.25)
            min_interval = 0.5
            max_interval = 1.5
            increment_min = max(2, d * 0.01)
            increment_max = max(5, d * 0.05)
            query_every_n = 1  # 每次都查询

        recovery_increment_min = max(1.0, d * 0.003)
        recovery_increment_max = max(3.0, d * 0.012)
        recovery_min_interval = 0.35
        recovery_max_interval = 0.9
        recovery_query_every_n = 1
        conservative_gap_threshold = max(30.0, min(90.0, d * 0.015))
        ts_pointer = timestamp_ms
        stuck_reset_notice_shown = False
        last_heartbeat_time = time.time()
        is_restarting = False
        last_watched_before_restart = watched_seconds
        heartbeat_count = 0
        coverage_recovery_mode = False
        conservative_gap_mode = False
        recovery_pass_count = 0
        recovery_target_end: Optional[float] = None

        while not is_video_completed(watched_seconds, d, completed_flag):
            active_increment_min = recovery_increment_min if conservative_gap_mode else increment_min
            active_increment_max = recovery_increment_max if conservative_gap_mode else increment_max
            active_min_interval = recovery_min_interval if conservative_gap_mode else min_interval
            active_max_interval = recovery_max_interval if conservative_gap_mode else max_interval
            active_query_every_n = recovery_query_every_n if conservative_gap_mode else query_every_n
            active_simulated_rate = min(simulated_rate, 1.0) if conservative_gap_mode else simulated_rate

            increment = random.uniform(active_increment_min, active_increment_max)
            current_cp = min(d, current_cp + increment)
            time_elapsed = (increment / active_simulated_rate) * 1000
            ts_pointer += int(
                time_elapsed + random.randint(80, 160)
                if conservative_gap_mode
                else (time_elapsed + random.randint(50, 200) if fast_mode else random.randint(100, 500))
            )
            progress_percent = int(min(100, (current_cp / d) * 100))
            coverage = get_effective_coverage()

            current_time = time.time()
            elapsed_since_last = current_time - last_heartbeat_time
            if elapsed_since_last < active_min_interval:
                time.sleep(active_min_interval - elapsed_since_last)
            elif elapsed_since_last < active_max_interval:
                time.sleep(
                    random.uniform(0.15, 0.3)
                    if conservative_gap_mode
                    else (random.uniform(0.1, 0.3) if fast_mode else random.uniform(0.3, 0.8))
                )
            last_heartbeat_time = time.time()

            heartbeat_count += 1

            heartbeat_url = 'https://www.yuketang.cn/video-log/heartbeat/'
            payload = {
                "heart_data": [{
                    "i": random.randint(3, 8),
                    "et": "heartbeat",
                    "p": "web",
                    "n": "ali-cdn.xuetangx.com",
                    "lob": "ykt",
                    "cp": round(current_cp, 2),
                    "fp": random.randint(80, 100),
                    "tp": 100,
                    "sp": random.randint(4, 6),
                    "ts": str(ts_pointer),
                    "u": int(u),
                    "uip": "",
                    "c": int(c_course_id),
                    "v": int(v),
                    "skuid": int(s_id),
                    "classroomid": classroom_id,
                    "cc": ccid,
                    "d": int(d),
                    "pg": video_id_str + "_x33v",
                    "sq": random.randint(8, 15),
                    "t": "video",
                    "cards_id": 0,
                    "slide": 0,
                    "v_url": ""
                }]
            }

            headers1 = {
                'User-Agent': SESSION_USER_AGENT,
                'Content-Type': 'application/json',
                'authority': 'changjiang.yuketang.cn',
                'referer': SESSION_REFERER,
            }

            for retry in range(3):
                try:
                    response = _thread_safe_post(
                        url=heartbeat_url,
                        data=json.dumps(payload),
                        headers=headers1,
                        timeout=10
                    )
                    if response.status_code == 200:
                        break
                    if retry < 2:
                        time.sleep(0.2 if fast_mode else 0.5)
                except Exception:
                    if retry < 2:
                        time.sleep(0.2 if fast_mode else 0.5)

            # 只在需要时查询进度
            if heartbeat_count % active_query_every_n == 0:
                progress_url = (
                    "https://www.yuketang.cn/video-log/get_video_watch_progress/"
                    f"?cid={c_course_id}&user_id={u}&classroom_id={classroom_id}"
                    f"&video_type=video&vtype=rate&video_id={video_id_str}&snapshot=1"
                )
                try:
                    response_new = _thread_safe_get(progress_url, headers=headers, timeout=10)
                except Exception:
                    pass
                else:
                    progress_response = response_new.json()
                    video_data = progress_response.get('data', {}).get(video_id_str, {}) or progress_response.get(video_id_str, {})
                    has_watched = video_data.get('watch_length', 0)
                    completed_flag = video_data.get('completed', 0)

                    if has_watched is not None:
                        if is_restarting:
                            if has_watched < last_watched_before_restart * 0.8 or has_watched > watched_seconds:
                                watched_seconds = has_watched
                                if has_watched < d * 0.2:
                                    is_restarting = False
                                    current_cp = max(current_cp, has_watched)
                                else:
                                    current_cp = max(current_cp, has_watched)
                        else:
                            if has_watched > current_cp:
                                current_cp = has_watched
                            watched_seconds = has_watched
                            if coverage_recovery_mode and has_watched >= last_watched_before_restart:
                                is_restarting = False
                    else:
                        if has_watched is not None and has_watched > current_cp:
                            current_cp = has_watched
                            watched_seconds = has_watched

                    if coverage_recovery_mode or completed_flag == 1 or watched_seconds >= d * 0.95:
                        refresh_detail_coverage()

            if coverage_recovery_mode and recovery_target_end is not None and current_cp >= recovery_target_end:
                next_gap_target = get_next_gap_target()
                if next_gap_target is not None:
                    current_cp, recovery_target_end, gap_length = next_gap_target
                    conservative_gap_mode = gap_length <= conservative_gap_threshold
                    last_watched_before_restart = watched_seconds
                    ts_pointer = int(time.time() * 1000)
                    is_restarting = True
                    with thread_lock:
                        log_info(
                            f"[线程{thread_id}] 视频 {video_id_str} 切换补刷目标区间 "
                            f"{current_cp:.0f}s - {recovery_target_end:.0f}s"
                        )
                    time.sleep(random.uniform(0.3, 0.6))
                    continue

            current_coverage = get_effective_coverage()
            is_completed = is_video_completed(watched_seconds, d, completed_flag)

            if is_completed:
                elapsed = time.time() - start_time
                with thread_lock:
                    log_success(f"[线程{thread_id}] 视频 {video_id_str} 完成！覆盖率: {current_coverage:.1f}%, 用时: {elapsed:.1f}秒")
                return True

            if current_cp >= d and current_coverage < COVERAGE_THRESHOLD:
                if not stuck_reset_notice_shown:
                    with thread_lock:
                        log_warning(f"[线程{thread_id}] 视频 {video_id_str} 进度100%但覆盖率 {current_coverage:.1f}%，重新播放。")
                    stuck_reset_notice_shown = True
                if not coverage_recovery_mode:
                    coverage_recovery_mode = True
                    with thread_lock:
                        log_info(
                            f"[线程{thread_id}] 视频 {video_id_str} 切换到保守补刷模式，"
                            f"当前覆盖率 {current_coverage:.1f}%"
                        )
                refresh_detail_coverage()
                next_gap_target = get_next_gap_target()
                if next_gap_target is not None:
                    current_cp, recovery_target_end, gap_length = next_gap_target
                    conservative_gap_mode = gap_length <= conservative_gap_threshold
                    with thread_lock:
                        log_info(
                            f"[线程{thread_id}] 视频 {video_id_str} 锁定未覆盖区间 "
                            f"{current_cp:.0f}s - {recovery_target_end:.0f}s"
                        )
                else:
                    current_cp = 0
                    conservative_gap_mode = True
                    recovery_target_end = None
                last_watched_before_restart = watched_seconds
                ts_pointer = int(time.time() * 1000)
                is_restarting = True
                recovery_pass_count += 1
                if recovery_pass_count >= 3:
                    with thread_lock:
                        log_warning(
                            f"[线程{thread_id}] 视频 {video_id_str} 已连续 {recovery_pass_count} 轮补刷，"
                            f"覆盖率仍为 {current_coverage:.1f}%"
                        )
                time.sleep(random.uniform(0.4, 0.9) if conservative_gap_mode else random.uniform(0.3, 0.8))
                continue

        return True

    except Exception as exc:
        elapsed = time.time() - start_time
        with thread_lock:
            log_error(f"[线程{thread_id}] 视频 {video_id_str} 处理异常：{exc}, 已用时: {elapsed:.1f}秒")
        return False


def _process_graph_item(
    item: Dict[str, Any],
    thread_lock: threading.Lock,
    sku_id: str,
    classroom_id: str,
    headers: Dict
) -> bool:
    from src.core.course_progress_graph import process_courseware_item

    return process_courseware_item(item, thread_lock, int(sku_id), classroom_id, headers)


def _select_course():
    """获取课程信息"""
    from src.core.course_progress import _select_course
    return _select_course()


def _extract_video_items(courseware_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    def extract_video_leafs(chapter: Dict[str, Any]) -> List[Dict[str, Any]]:
        section_list = chapter.get("section_list", [])
        videos: List[Dict[str, Any]] = []
        if section_list:
            for section in section_list:
                for leaf in section.get("leaf_list", []) or []:
                    if leaf.get("leaf_type") == 0 and leaf.get("id"):
                        videos.append(leaf)
        else:
            for leaf in chapter.get("leaf_list", []) or []:
                if leaf.get("leaf_type") == 0 and leaf.get("id"):
                    videos.append(leaf)
        return videos

    all_videos: List[Dict[str, Any]] = []
    for chapter_index, chapter in enumerate(courseware_detail.get("data", {}).get("content_info", [])):
        video_leafs = extract_video_leafs(chapter)
        for video_index, leaf in enumerate(video_leafs):
            all_videos.append(
                {
                    "video_id": str(leaf["id"]),
                    "chapter_index": chapter_index,
                    "video_index": video_index,
                }
            )
    return all_videos


def run_video_session(
    selected_course: Optional[CourseSelection] = None,
    max_workers: int = 3,
    fast_mode: bool = False,
) -> Tuple[int, int]:
    if selected_course is None:
        classroom_id, university_id, course_info = _select_course()
    else:
        classroom_id, university_id, course_info = selected_course

    url = (
        "https://www.yuketang.cn/v2/api/web/logs/learn/"
        f"{classroom_id}?actype=-1&page=0&offset=20&sort=-1"
    )
    response = _thread_safe_get(url)
    course_logs = response.json()

    activities = course_logs.get("data", {}).get("activities", [])
    target_activity = None
    if len(activities) > 1 and activities[1].get("courseware_id"):
        target_activity = activities[1]
    else:
        for activity in activities:
            if activity.get("courseware_id"):
                target_activity = activity
                break

    if not target_activity:
        log_warning(f"课程《{course_info.get('name')}》暂无可刷视频，自动跳过。")
        return 0, 0

    detail_url = (
        "https://www.yuketang.cn/c27/online_courseware/xty/kls/pub_news/"
        f"{target_activity['courseware_id']}/"
    )
    headers = {
        "xtbz": "ykt",
        "classroom-id": str(classroom_id),
    }
    courseware_detail = _thread_safe_get(detail_url, headers=headers).json()
    c_course_id = str(courseware_detail["data"]["course_id"])
    s_id = str(courseware_detail["data"]["s_id"])

    all_videos = _extract_video_items(courseware_detail)
    if not all_videos:
        log_warning(f"课程《{course_info.get('name')}》未找到可刷视频。")
        return 0, 0

    log_info(SEPARATOR)
    log_info(f"课程《{course_info.get('name')}》共找到 {len(all_videos)} 个视频。")

    max_workers = max(1, min(5, int(max_workers)))
    pending_videos, completed, unknown_count = scan_videos_for_completion(
        videos=all_videos,
        classroom_id=classroom_id,
        c_course_id=c_course_id,
        s_id=s_id,
        university_id=university_id,
        course_name=course_info.get("name", ""),
        max_workers=max_workers,
    )

    if not pending_videos:
        log_success(f"课程《{course_info.get('name')}》所有视频覆盖率均已达标。")
        return completed, 0

    if unknown_count:
        log_warning(f"有 {unknown_count} 个视频在扫描阶段未判定稳定，将直接进入补刷阶段。")

    thread_lock = threading.Lock()
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="VideoWorker") as executor:
        futures = [
            executor.submit(
                _watch_single_video,
                video_info["video_id"],
                classroom_id,
                c_course_id,
                s_id,
                university_id,
                course_info.get("name", ""),
                thread_lock,
                fast_mode,
                index,
            )
            for index, video_info in enumerate(pending_videos, start=1)
        ]

        for future in as_completed(futures):
            try:
                if future.result():
                    completed += 1
                else:
                    failed += 1
            except Exception as exc:
                log_error(f"线程执行异常：{exc}")
                failed += 1

    log_success(f"课程《{course_info.get('name')}》视频处理完成！成功: {completed}, 失败: {failed}")
    return completed, failed


def get_csrf_token():
    """获取 CSRF token"""
    return _get_csrf_token()


def get_thread_safe_get():
    """获取线程安全的 get 函数"""
    return _thread_safe_get


def get_thread_safe_post():
    """获取线程安全的 post 函数"""
    return _thread_safe_post
