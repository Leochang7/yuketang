import json
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from src.network.http_client import SEPARATOR, SESSION_REFERER, SESSION_USER_AGENT, session
from src.utils.logging_utils import log_error, log_info, log_success, log_warning
from src.utils.config_utils import get_default_comment
from src.llm import generate_comment_by_llm
from src.auth.cookies_manager import get_cookie_value
from src.core.course_selection import (
    CourseSelection,
    select_course as _shared_select_course,
    select_courses as _shared_select_courses,
)
from src.utils.http_debug import log_http_failure, log_http_payload, log_http_success, now_ms

# 全局锁，用于保护 requests.Session 的访问（Session 不是线程安全的）
_session_lock = threading.Lock()


def _thread_safe_get(url, **kwargs):
    """线程安全的 session.get 包装函数"""
    started = now_ms()
    try:
        with _session_lock:
            response = session.get(url, **kwargs)
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
    """线程安全的 session.post 包装函数"""
    started = now_ms()
    try:
        with _session_lock:
            response = session.post(url, **kwargs)
        log_http_success("POST", url, response.status_code, now_ms() - started, params=kwargs.get("params"))
        try:
            log_http_payload(url, response.json())
        except Exception:
            pass
        return response
    except Exception as exc:
        log_http_failure("POST", url, exc, now_ms() - started, params=kwargs.get("params"))
        raise


def random_sleep_interval():
    """随机心跳睡眠，避免被判异常。"""
    base = random.uniform(0.3, 0.8)
    if random.random() < 0.1:
        base += random.uniform(0.5, 1.5)
    time.sleep(base)


def _preload_video_cache(classroom_id: str, video_id: str, headers: dict) -> bool:
    """
    预加载视频缓存，解决"加载时长失败"问题。
    通过访问视频页面来触发服务器缓存视频信息。
    """
    try:
        # 先访问leaf_info接口
        url = (
            'https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/'
            f"{classroom_id}/{video_id}/"
        )
        _thread_safe_get(url=url, headers=headers, timeout=15)
        time.sleep(0.5)

        # 再尝试访问视频播放页面
        play_url = f"https://www.yuketang.cn/v2/web/player/{classroom_id}/{video_id}"
        play_headers = headers.copy()
        play_headers.update({
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'upgrade-insecure-requests': '1',
        })
        try:
            _thread_safe_get(play_url, headers=play_headers, timeout=15)
        except Exception:
            pass

        log_info(f"视频 {video_id} 预加载完成")
        return True
    except Exception as exc:
        log_warning(f"视频 {video_id} 预加载失败：{exc}")
        return False


def _get_video_duration_with_retry(
    classroom_id: str,
    video_id: str,
    c_course_id: str,
    u: str,
    headers: dict,
    max_retries: int = 3
) -> Tuple[int, Dict]:
    """
    重试获取视频时长，解决"加载时长失败"问题。
    """
    d = 0
    video_data = {}

    for retry in range(max_retries):
        try:
            # 先获取leaf_info
            url = (
                'https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/'
                f"{classroom_id}/{video_id}/"
            )
            response = _thread_safe_get(url=url, headers=headers, timeout=10)
            leaf_info = response.json()

            if leaf_info.get('data', {}).get('content_info', {}).get('media'):
                d = leaf_info['data']['content_info']['media'].get('duration', 0)

            # 再获取进度信息
            url = (
                "https://www.yuketang.cn/video-log/get_video_watch_progress/"
                f"?cid={c_course_id}&user_id={u}&classroom_id={classroom_id}"
                f"&video_type=video&vtype=rate&video_id={video_id}&snapshot=1"
            )
            response_new = _thread_safe_get(url=url, headers=headers, timeout=10)
            progress_response = response_new.json()
            video_data = progress_response.get('data', {}).get(video_id, {})
            if not video_data and progress_response.get(video_id):
                video_data = progress_response[video_id]

            if d == 0 and video_data:
                try:
                    d = int(video_data.get('video_length', 0))
                except Exception:
                    pass

            if d > 0:
                return d, video_data

            if retry < max_retries - 1:
                wait_time = (retry + 1) * 2
                log_warning(f"视频 {video_id} 时长获取失败，{wait_time}秒后重试 ({retry + 1}/{max_retries})")
                time.sleep(wait_time)

        except Exception as exc:
            if retry < max_retries - 1:
                wait_time = (retry + 1) * 2
                log_warning(f"获取视频信息异常：{exc}，{wait_time}秒后重试 ({retry + 1}/{max_retries})")
                time.sleep(wait_time)

    return d, video_data


CourseSelection = Tuple[str, int, Dict[str, Any]]


def _fetch_course_list() -> List[Dict[str, Any]]:
    url = 'https://www.yuketang.cn/v2/api/web/courses/list?identity=2'
    response = _thread_safe_get(url=url)

    course_response = response.json()
    course_list = course_response.get('data', {}).get('list', [])

    if not course_list:
        log_warning("未检测到课程数据，请检查是否登录成功。")
        raise SystemExit(-1)

    return course_list


def _parse_course_indices(user_input: str, max_value: int, allow_multiple: bool) -> Optional[List[int]]:
    normalized_input = user_input.strip().lower()
    if allow_multiple and normalized_input in ("all", "*"):
        return list(range(max_value + 1))

    tokens = [normalized_input]
    if allow_multiple:
        normalized_input = normalized_input.replace("，", ",")
        tokens = [token.strip() for token in normalized_input.split(",") if token.strip()]

    if not tokens:
        return None

    indices: List[int] = []
    for token in tokens:
        range_tokens = [token]
        if allow_multiple and "-" in token:
            try:
                start_text, end_text = token.split("-", 1)
                start_value = int(start_text)
                end_value = int(end_text)
            except ValueError:
                return None

            step = 1 if start_value <= end_value else -1
            range_tokens = [str(value) for value in range(start_value, end_value + step, step)]

        for value_text in range_tokens:
            try:
                value = int(value_text)
            except ValueError:
                return None
            if value < 0 or value > max_value:
                return None
            indices.append(value)

    deduplicated: List[int] = []
    seen = set()
    for index in indices:
        if index in seen:
            continue
        seen.add(index)
        deduplicated.append(index)

    return deduplicated or None


def _build_course_selections(course_list: List[Dict[str, Any]], indices: List[int]) -> List[CourseSelection]:
    selections: List[CourseSelection] = []
    for index in indices:
        course_info = course_list[index]
        classroom_id = str(course_info['classroom_id'])
        university_id = int(course_info.get('course', {}).get('university_id', 0))
        if not university_id:
            log_warning("未获取到 university_id，后续部分接口可能会失败。")
        selections.append((classroom_id, university_id, course_info))
    return selections


def _select_courses(allow_multiple: bool = False) -> List[CourseSelection]:
    course_list = _fetch_course_list()

    if len(course_list) == 1:
        return _build_course_selections(course_list, [0])

    for i, course in enumerate(course_list):
        log_info(f"序号：{i} ----- {course['name']}")
    log_info(SEPARATOR)

    prompt = "请输入需要操作的课程编号：\n"
    if allow_multiple:
        prompt = "请输入需要操作的课程编号（多个用逗号分隔；all 表示全部）：\n"

    max_value = len(course_list) - 1
    while True:
        user_input = input(prompt)
        indices = _parse_course_indices(user_input, max_value, allow_multiple=allow_multiple)
        if indices:
            return _build_course_selections(course_list, indices)

        if allow_multiple:
            log_warning(f"输入错误，请输入 0 到 {max_value} 之间的编号，可使用 1,3,5 或 all。")
        else:
            log_warning(f"输入错误，请输入一个介于 0 和 {max_value} 之间的课程编号。")


def _select_course() -> Tuple[str, int, Dict]:
    """
    复用课程选择逻辑，返回 (classroom_id, university_id, course_info)。
    """
    url = 'https://www.yuketang.cn/v2/api/web/courses/list?identity=2'
    response = _thread_safe_get(url=url)

    course_response = response.json()
    course_list = course_response.get('data', {}).get('list', [])

    if not course_list:
        log_warning("未检测到课程数据，请检查是否登录成功。")
        raise SystemExit(-1)

    if len(course_list) > 1:
        for i, course in enumerate(course_list):
            log_info(f"序号：{i} ----- {course['name']}")
        log_info(SEPARATOR)

        min_value = 0
        max_value = len(course_list) - 1

        while True:
            user_input = input("请输入需要操作的课程编号：\n")
            try:
                num = int(user_input)
                if min_value <= num <= max_value:
                    course_info = course_list[num]
                    classroom_id = str(course_info['classroom_id'])
                    university_id = int(course_info.get('course', {}).get('university_id', 0))
                    if not university_id:
                        log_warning("未获取到 university_id，后续部分接口可能会失败。")
                    return classroom_id, university_id, course_info
                log_warning(f"输入错误，请输入一个介于 {min_value} 和 {max_value} 之间的课程编号。")
            except ValueError:
                log_warning("输入错误，请确保您输入的是一个整数。")
    else:
        course_info = course_list[0]
        classroom_id = str(course_info['classroom_id'])
        university_id = int(course_info.get('course', {}).get('university_id', 0))
        if not university_id:
            log_warning("未获取到 university_id，后续部分接口可能会失败。")
        return classroom_id, university_id, course_info


def _watch_single_video(
    video_id: str,
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    chapter_index: int,
    video_index: int,
    thread_lock: threading.Lock
) -> bool:
    """
    单视频刷取函数，用于多线程调用。
    """
    video_id_str = str(video_id)
    headers = {
        'xtbz': 'ykt',
        'classroom-id': str(classroom_id)
    }

    with thread_lock:
        log_info(f"[线程 {threading.current_thread().name}] 开始处理第{chapter_index + 1}章 第{video_index + 1}个视频 {video_id_str}")

    # 预加载视频缓存
    _preload_video_cache(classroom_id, video_id_str, headers)

    # 获取视频信息（带重试）
    url = (
        'https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/'
        f"{classroom_id}/{video_id_str}/"
    )
    try:
        response = _thread_safe_get(url=url, headers=headers, timeout=10)
        leaf_info = response.json()
        ccid = leaf_info['data']['content_info']['media']['ccid']
        v = str(leaf_info['data']['id'])
        u = str(leaf_info['data']['user_id'])
    except Exception as exc:
        with thread_lock:
            log_error(f"[线程 {threading.current_thread().name}] 获取视频 {video_id_str} 基础信息失败：{exc}")
        return False

    # 获取视频时长（带重试）
    d, video_data = _get_video_duration_with_retry(
        classroom_id, video_id_str, c_course_id, u, headers
    )

    completed_flag = video_data.get('completed', 0)
    watched_seconds = video_data.get('watch_length', 0)

    if not d or d <= 0:
        with thread_lock:
            log_warning(f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 未获取到有效时长，尝试从进度接口获取...")
        # 再试一次从进度接口获取
        url = (
            "https://www.yuketang.cn/video-log/get_video_watch_progress/"
            f"?cid={c_course_id}&user_id={u}&classroom_id={classroom_id}"
            f"&video_type=video&vtype=rate&video_id={video_id_str}&snapshot=1"
        )
        try:
            response_new = _thread_safe_get(url=url, headers=headers, timeout=10)
            progress_response = response_new.json()
            video_data = progress_response.get('data', {}).get(video_id_str, {}) or progress_response.get(video_id_str, {})
            d = int(video_data.get('video_length', 0))
            watched_seconds = video_data.get('watch_length', 0)
            completed_flag = video_data.get('completed', 0)
        except Exception:
            pass

        if not d or d <= 0:
            with thread_lock:
                log_warning(f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 仍未获取到有效时长，跳过。")
            return False

    def calculate_coverage(watch_len, video_len):
        if not video_len or video_len <= 0:
            return 0.0
        return min(100.0, (watch_len / video_len) * 100.0)

    COVERAGE_THRESHOLD = 100.0
    initial_coverage = calculate_coverage(watched_seconds, d)

    def is_video_completed(watch_len, video_len, server_completed):
        coverage = calculate_coverage(watch_len, video_len)
        if coverage >= COVERAGE_THRESHOLD:
            return True
        return False

    if is_video_completed(watched_seconds, d, completed_flag):
        with thread_lock:
            log_info(
                f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 覆盖率已达标（{initial_coverage:.1f}% >= {COVERAGE_THRESHOLD}%），跳过。"
            )
        return True

    if completed_flag == 1:
        with thread_lock:
            log_warning(
                f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 服务器标记为完成，但覆盖率仅 {initial_coverage:.1f}%，继续刷课。"
            )

    timestamp_ms = int(time.time() * 1000)
    current_cp = watched_seconds if watched_seconds else random.uniform(
        5, min(60, max(10, d * 0.1)))
    simulated_rate = random.uniform(0.9, 1.25)
    ts_pointer = timestamp_ms
    stuck_reset_notice_shown = False
    last_heartbeat_time = time.time()
    is_restarting = False
    last_watched_before_restart = watched_seconds

    while not is_video_completed(watched_seconds, d, completed_flag):
        increment = random.uniform(max(2, d * 0.01), max(5, d * 0.05))
        current_cp = min(d, current_cp + increment)
        time_elapsed = (increment / simulated_rate) * 1000
        ts_pointer += int(time_elapsed + random.randint(100, 500))
        progress_percent = int(min(100, (current_cp / d) * 100))
        coverage = calculate_coverage(watched_seconds, d)

        current_time = time.time()
        elapsed_since_last = current_time - last_heartbeat_time
        min_interval = 0.5
        max_interval = 1.5
        if elapsed_since_last < min_interval:
            time.sleep(min_interval - elapsed_since_last)
        elif elapsed_since_last < max_interval:
            random_sleep_interval()
        last_heartbeat_time = time.time()

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
            'method': 'GET',
            'path': '/v2/api/web/courses/list?identity=2',
            'referer': SESSION_REFERER,
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
        }

        max_retries = 3
        for retry in range(max_retries):
            try:
                response = _thread_safe_post(
                    url=heartbeat_url,
                    data=json.dumps(payload),
                    headers=headers1,
                    timeout=10
                )
                if response.status_code == 200:
                    break
                if retry < max_retries - 1:
                    time.sleep(0.5)
            except Exception as exc:
                if retry < max_retries - 1:
                    time.sleep(0.5)
                else:
                    with thread_lock:
                        log_error(f"[线程 {threading.current_thread().name}] 心跳发送失败：{exc}")

        url = (
            "https://www.yuketang.cn/video-log/get_video_watch_progress/"
            f"?cid={c_course_id}&user_id={u}&classroom_id={classroom_id}"
            f"&video_type=video&vtype=rate&video_id={video_id_str}&snapshot=1"
        )
        try:
            response_new = _thread_safe_get(url=url, headers=headers, timeout=10)
        except Exception as exc:
            continue
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

        current_coverage = calculate_coverage(watched_seconds, d)
        is_completed = is_video_completed(watched_seconds, d, completed_flag)

        if is_completed:
            with thread_lock:
                log_success(
                    f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 完成！覆盖率: {current_coverage:.1f}%"
                )
            break

        if current_cp >= d and current_coverage < COVERAGE_THRESHOLD:
            if not stuck_reset_notice_shown:
                with thread_lock:
                    log_warning(
                        f"[线程 {threading.current_thread().name}] 视频 {video_id_str} 进度100%但覆盖率 {current_coverage:.1f}%，重新播放。"
                    )
                stuck_reset_notice_shown = True
            current_cp = 0
            last_watched_before_restart = watched_seconds
            ts_pointer = int(time.time() * 1000)
            is_restarting = True
            random_sleep_interval()
            continue

    return True


def run_course_session(selected_course: Optional[CourseSelection] = None):
    """选择课程并持续刷课（视频）- 支持多线程。"""
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

    activities = course_logs['data'].get('activities', [])
    target_activity = None

    if len(activities) > 1 and activities[1].get('courseware_id'):
        target_activity = activities[1]
    else:
        for activity in activities:
            courseware_id = activity.get('courseware_id')
            if courseware_id:
                target_activity = activity
                break

    if not target_activity:
        log_warning("选中课程暂无可刷视频，自动跳过。")
        return

    url = (
        'https://www.yuketang.cn/c27/online_courseware/xty/kls/pub_news/'
        f"{target_activity['courseware_id']}/"
    )
    headers = {
        'xtbz': 'ykt',
        'classroom-id': str(classroom_id)
    }
    response = _thread_safe_get(url, headers=headers)

    courseware_detail = response.json()
    c_course_id = str(courseware_detail['data']['course_id'])
    s_id = str(courseware_detail['data']['s_id'])

    def extract_video_leafs(chapter):
        section_list = chapter.get('section_list', [])
        videos = []
        if section_list:
            for section in section_list:
                leafs = section.get('leaf_list', [])
                if not leafs:
                    continue
                for leaf in leafs:
                    if leaf.get('leaf_type') == 0 and leaf.get('id'):
                        videos.append(leaf)
        else:
            for leaf in chapter.get('leaf_list', []):
                if leaf.get('leaf_type') == 0 and leaf.get('id'):
                    videos.append(leaf)
        return videos

    # 备用：通过章节接口一次性获取每章视频 leaf
    fallback_chapter_videos = _get_course_chapter_videos(
        classroom_id=classroom_id,
        university_id=university_id,
    )

    # 收集所有视频
    all_videos = []
    for i, chapter in enumerate(courseware_detail['data']['content_info']):
        primary_videos = extract_video_leafs(chapter)
        extra_videos = []
        if fallback_chapter_videos and i < len(fallback_chapter_videos):
            extra_videos = fallback_chapter_videos[i] or []

        if extra_videos:
            seen_ids = {str(v["id"]) for v in primary_videos if v.get("id")}
            merged = list(primary_videos)
            for v in extra_videos:
                vid = v.get("id")
                if vid is None:
                    continue
                vid_str = str(vid)
                if vid_str not in seen_ids:
                    merged.append({"id": vid})
                    seen_ids.add(vid_str)
            video_leafs = merged
        else:
            video_leafs = primary_videos

        for j, leaf in enumerate(video_leafs):
            all_videos.append({
                'video_id': str(leaf['id']),
                'chapter_index': i,
                'video_index': j
            })

        log_info(
            f"第{i + 1}章----共找到{len(video_leafs)}个视频。"
        )

    if not all_videos:
        log_warning("未找到可刷视频。")
        return

    log_info(SEPARATOR)
    log_info(f"总共找到 {len(all_videos)} 个视频")

    # 询问是否使用多线程
    use_multithread = input("是否使用多线程刷视频？(y/n，默认y): ").strip().lower()
    if use_multithread in ('', 'y', 'yes'):
        max_workers = input(f"请输入并发线程数 (1-5，默认2): ").strip()
        try:
            max_workers = int(max_workers)
            if max_workers < 1:
                max_workers = 1
            elif max_workers > 5:
                max_workers = 5
        except ValueError:
            max_workers = 2

        log_info(f"使用 {max_workers} 个线程并发刷视频")
        thread_lock = threading.Lock()
        completed = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="VideoWorker") as executor:
            futures = []
            for video_info in all_videos:
                future = executor.submit(
                    _watch_single_video,
                    video_info['video_id'],
                    classroom_id,
                    c_course_id,
                    s_id,
                    university_id,
                    video_info['chapter_index'],
                    video_info['video_index'],
                    thread_lock
                )
                futures.append(future)

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        completed += 1
                    else:
                        failed += 1
                except Exception as exc:
                    log_error(f"线程执行异常：{exc}")
                    failed += 1

        log_success(f"多线程刷课完成！成功: {completed}, 失败: {failed}")
    else:
        log_info("使用单线程模式刷视频")
        # 单线程模式
        for video_info in all_videos:
            thread_lock = threading.Lock()  # 单线程也需要一个锁保持接口一致
            _watch_single_video(
                video_info['video_id'],
                classroom_id,
                c_course_id,
                s_id,
                university_id,
                video_info['chapter_index'],
                video_info['video_index'],
                thread_lock
            )

    log_success("该课程已完成刷课！")


def _get_csrf_token() -> Optional[str]:
    """
    从当前 session.cookies 中尝试提取 csrf token。
    不同学校可能字段名略有差异，这里做一个尽量兼容的尝试。
    """
    with _session_lock:
        candidates = ['csrftoken', 'csrf_token', 'csrfmiddlewaretoken']
        for name in candidates:
            value = get_cookie_value(name)
            if value:
                return value
    return None


def _get_course_chapter_videos(classroom_id: str, university_id: int) -> List[List[Dict]]:
    """
    通过章节接口补充获取每一章下的视频 leaf。
    """
    url = "https://www.yuketang.cn/mooc-api/v1/lms/learn/course/chapter"
    params = {
        "cid": classroom_id,
        "term": "latest",
        "uv_id": university_id,
        "classroom_id": classroom_id,
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
        "xtbz": "ykt",
        "x-client": "web",
    }
    csrf = _get_csrf_token()
    if csrf:
        headers["x-csrftoken"] = csrf

    try:
        resp = _thread_safe_get(url, params=params, headers=headers, timeout=10)
        data = resp.json()
    except Exception as exc:
        log_warning(f"调用章节接口获取视频列表失败：{exc}")
        return []

    chapters = data.get("data", {}).get("course_chapter", [])
    result: List[List[Dict]] = []

    for chapter in chapters:
        chapter_videos: List[Dict] = []
        for sec in chapter.get("section_leaf_list", []):
            leaf_list = sec.get("leaf_list")
            if isinstance(leaf_list, list):
                for leaf in leaf_list:
                    if leaf.get("leaf_type") == 0 and leaf.get("id"):
                        chapter_videos.append({"id": leaf["id"]})
            else:
                if sec.get("leaf_type") == 0 and sec.get("id"):
                    chapter_videos.append({"id": sec["id"]})
        result.append(chapter_videos)

    return result


def _extract_sku_id_from_logs(classroom_id: str) -> Optional[int]:
    """
    从学习日志接口中提取 sku_id。
    """
    url = (
        "https://www.yuketang.cn/v2/api/web/logs/learn/"
        f"{classroom_id}?actype=-1&page=0&offset=20&sort=-1"
    )
    response = _thread_safe_get(url)
    data = response.json()
    activities = data.get('data', {}).get('activities', [])
    for act in activities:
        content = act.get('content') or {}
        if 'sku_id' in content:
            return int(content['sku_id'])
    return None


def _get_score_detail(sku_id: int, classroom_id: str, university_id: int) -> dict:
    """
    调用单个 sku 的 score_detail 接口，返回 JSON。
    """
    url = f"https://www.yuketang.cn/c27/online_courseware/schedule/score_detail/single/{sku_id}/0/"
    headers = {
        "accept": "application/json, text/plain, */*",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
        "xt-agent": "web",
        "xtbz": "ykt",
    }
    response = _thread_safe_get(url, headers=headers)
    return response.json()


def _iter_discussion_leaf_ids(score_detail: dict):
    """
    从 score_detail 中筛选所有"未得分"的讨论题 leaf_id。
    """
    leaf_infos = score_detail.get('data', {}).get('leaf_level_infos', [])
    for item in leaf_infos:
        if (
            item.get('leaf_type') == 4
            and item.get('evaluation_id') == 10
            and item.get('id')
        ):
            user_score = item.get("user_score", 0)
            try:
                user_score_val = float(user_score)
            except (TypeError, ValueError):
                user_score_val = 0.0
            if user_score_val == 0.0:
                yield int(item['id'])


def _get_topic_and_user(classroom_id: str, sku_id: int, leaf_id: int, university_id: int) -> Optional[Tuple[int, int]]:
    """
    根据 classroom_id + sku_id + leaf_id 获取 (topic_id, to_user)。
    """
    url = "https://www.yuketang.cn/v/discussion/v2/unit/discussion/"
    params = {
        "classroom_id": classroom_id,
        "sku_id": sku_id,
        "leaf_id": leaf_id,
        "topic_type": 4,
        "channel": "xt",
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
        "xt-agent": "web",
        "xtbz": "ykt",
    }
    response = _thread_safe_get(url, params=params, headers=headers)
    data = response.json().get("data") or {}
    user_id = data.get("user_id")
    topic_id = data.get("id")
    if not user_id or not topic_id:
        return None
    return int(topic_id), int(user_id)


def _post_comment(classroom_id: str, university_id: int, topic_id: int, to_user: int, text: str) -> bool:
    """
    向指定话题发送一条评论。
    """
    url = "https://www.yuketang.cn/v/discussion/v2/comment/"
    csrf_token = _get_csrf_token()
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
        "xt-agent": "web",
        "xtbz": "ykt",
    }
    if csrf_token:
        headers["x-csrftoken"] = csrf_token

    payload = {
        "to_user": to_user,
        "topic_id": topic_id,
        "content": {
            "text": text,
            "upload_images": [],
            "accessory_list": [],
        },
    }

    try:
        resp = _thread_safe_post(url, headers=headers, data=json.dumps(payload), timeout=10)
    except Exception as exc:
        log_error(f"发送评论失败（topic_id={topic_id}）：{exc}")
        return False

    try:
        data = resp.json()
    except Exception:
        data = None

    if resp.status_code == 200 and data and data.get("success"):
        log_success(f"评论成功：topic_id={topic_id}")
        return True

    log_warning(f"评论可能失败，状态码={resp.status_code}，响应={resp.text[:200]}")
    return False


def _get_discussion_leaf_info(classroom_id: str, leaf_id: int, university_id: int) -> Optional[Dict]:
    """
    获取讨论题 leaf 的详细信息，包括题目内容（context）。
    """
    url = (
        "https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/"
        f"{classroom_id}/{leaf_id}/"
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
        "xt-agent": "web",
        "xtbz": "ykt",
    }
    try:
        resp = _thread_safe_get(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as exc:
        log_warning(f"获取讨论题 leaf_info 失败（leaf_id={leaf_id}）：{exc}")
        return None


def run_discussion_comment_session(selected_course: Optional[CourseSelection] = None):
    """
    自动刷当前课程的所有"讨论题"评论。
    """
    if selected_course is None:
        classroom_id, university_id, course_info = _select_course()
    else:
        classroom_id, university_id, course_info = selected_course
    log_info(f"当前选择课程：{course_info.get('name')}（classroom_id={classroom_id}）")

    sku_id = _extract_sku_id_from_logs(classroom_id)

    if not sku_id:
        log_warning("未从学习日志中找到 sku_id，无法继续自动评论。")
        return
    log_info(f"已获取 sku_id={sku_id}，开始获取讨论题列表。")

    score_detail = _get_score_detail(sku_id=sku_id, classroom_id=classroom_id, university_id=university_id)
    leaf_ids = list(_iter_discussion_leaf_ids(score_detail))

    if not leaf_ids:
        log_warning("在 score_detail 中未找到任何讨论题。")
        return

    log_info(f"检测到 {len(leaf_ids)} 个讨论题，将依次尝试发送评论。")

    default_comment = get_default_comment()

    for idx, leaf_id in enumerate(leaf_ids, start=1):
        log_info(SEPARATOR)
        log_info(f"正在处理第 {idx}/{len(leaf_ids)} 个讨论题，leaf_id={leaf_id}")

        topic_user = _get_topic_and_user(
            classroom_id=classroom_id,
            sku_id=sku_id,
            leaf_id=leaf_id,
            university_id=university_id,
        )
        if not topic_user:
            log_warning(f"获取讨论详情失败，跳过该讨论题（leaf_id={leaf_id}）。")
            continue

        topic_id, to_user = topic_user
        log_info(f"已获取 topic_id={topic_id}, to_user={to_user}，开始准备评论内容。")

        leaf_info = _get_discussion_leaf_info(classroom_id, leaf_id, university_id)
        question_html = ""
        if leaf_info and leaf_info.get("data"):
            question_html = (
                leaf_info["data"]
                .get("content_info", {})
                .get("context", "")
            )

        comment_text: Optional[str]
        use_llm = False
        if default_comment.strip().lower() == "none":
            comment_text = generate_comment_by_llm(
                question_html,
                course_info.get("name"),
            )
            if not comment_text:
                log_warning("LLM 生成评论失败，建议手动评论。")
                forum_url = (
                    f"https://www.yuketang.cn/v2/web/lms/{classroom_id}/forum/{leaf_id}?hide_return=1"
                )
                log_info(f"对应讨论区地址：{forum_url}")
                return
            use_llm = True
        else:
            comment_text = default_comment

        if not use_llm:
            delay = random.uniform(3, 8)
            log_info(f"使用固定评论模板，将随机等待 {delay:.1f} 秒后再发送评论。")
            time.sleep(delay)

        log_info("评论内容已生成，开始发送评论。")

        _post_comment(
            classroom_id=classroom_id,
            university_id=university_id,
            topic_id=topic_id,
            to_user=to_user,
            text=comment_text,
        )

    log_success("本课程所有讨论题评论流程已结束。")


def run_graph_session():
    from src.core.course_progress_graph import run_graph_session as _run_graph_session

    return _run_graph_session()


def _fetch_course_list() -> List[Dict[str, Any]]:
    return [selection[2] for selection in _shared_select_courses(_thread_safe_get, allow_multiple=True)]


def _select_courses(allow_multiple: bool = False) -> List[CourseSelection]:
    return _shared_select_courses(_thread_safe_get, allow_multiple=allow_multiple)


def _select_course() -> CourseSelection:
    return _shared_select_course(_thread_safe_get)
