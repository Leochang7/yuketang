import asyncio
import threading
from typing import Any, Dict, List, Optional, Tuple

from src.core.course_progress import _get_csrf_token, _thread_safe_get
from src.core.course_selection import CourseSelection, select_courses
from src.core.course_progress_graph import (
    SUPPORTED_LEAF_TYPES,
    collect_courseware_items,
    process_courseware_item,
)
from src.core.course_progress_multithread import _watch_single_video, scan_videos_for_completion
from src.network.async_http_client import async_get_json, close_async_session
from src.network.http_client import SEPARATOR
from src.utils.logging_utils import log_error, log_info, log_success, log_warning


Job = Dict[str, Any]


def _collect_videos(chapter_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    videos: List[Dict[str, Any]] = []
    seen_ids = set()

    chapters = chapter_data.get("data", {}).get("course_chapter", [])
    for chapter in chapters:
        chapter_name = chapter.get("name", "未知章节")
        for section in chapter.get("section_leaf_list", []):
            candidates = [section]
            leaf_list = section.get("leaf_list", [])
            if isinstance(leaf_list, list):
                candidates.extend(leaf_list)

            for leaf in candidates:
                if leaf.get("leaf_type") != 0 or not leaf.get("id"):
                    continue

                video_id = str(leaf["id"])
                if video_id in seen_ids:
                    continue

                seen_ids.add(video_id)
                videos.append(
                    {
                        "id": leaf["id"],
                        "title": leaf.get("name", leaf.get("title", "无标题")),
                        "chapter": chapter_name,
                    }
                )

    return videos


def _build_headers(classroom_id: str, university_id: int) -> Dict[str, str]:
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
    return headers


async def _fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    data = await async_get_json(url, headers=headers, params=params, timeout=timeout)
    return data if isinstance(data, dict) else {}


async def _resolve_item_sku_id(
    classroom_id: str,
    item_id: Any,
    headers: Dict[str, str],
) -> Optional[int]:
    leaf_info_url = f"https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{item_id}/"
    try:
        leaf_info = await _fetch_json(leaf_info_url, headers=headers, timeout=10)
        data = leaf_info.get("data", {})
        sku_id = data.get("sku_id") or data.get("sku", {}).get("id")
        if sku_id:
            return int(sku_id)
    except Exception as exc:
        log_warning(f"提取 sku_id 失败(item_id={item_id})：{exc}")
    return None


async def _resolve_course_context(
    classroom_id: str,
    headers: Dict[str, str],
    videos: List[Dict[str, Any]],
    graphs: List[Dict[str, Any]],
) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[str]]:
    all_items = videos or graphs
    if not all_items:
        return None, None, None, None

    sample_id = all_items[0]["id"]
    leaf_info_url = f"https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{sample_id}/"
    leaf_info = await _fetch_json(leaf_info_url, headers=headers, timeout=10)
    data = leaf_info.get("data", {})
    sku = data.get("sku", {})

    video_sku_id = data.get("sku_id") or sku.get("id")
    if videos:
        video_sku_id = video_sku_id or await _resolve_item_sku_id(classroom_id, videos[0]["id"], headers)
    graph_sku_id = await _resolve_item_sku_id(classroom_id, graphs[0]["id"], headers) if graphs else None
    if graph_sku_id and not video_sku_id:
        video_sku_id = graph_sku_id

    c_course_id = sku.get("course_id") or data.get("course_id")
    s_id = sku.get("id") or data.get("sku_id")

    if c_course_id and s_id:
        return (
            int(video_sku_id) if video_sku_id else None,
            int(graph_sku_id) if graph_sku_id else None,
            str(c_course_id),
            str(s_id),
        )

    logs_url = f"https://www.yuketang.cn/v2/api/web/logs/learn/{classroom_id}?actype=-1&page=0&offset=20&sort=-1"
    logs_data = await _fetch_json(logs_url, timeout=10)
    activities = logs_data.get("data", {}).get("activities", [])
    target_activity = None
    for activity in activities:
        content = activity.get("content") or {}
        if not video_sku_id and "sku_id" in content:
            try:
                video_sku_id = int(content["sku_id"])
            except (TypeError, ValueError):
                pass
        if not target_activity and activity.get("courseware_id"):
            target_activity = activity

    if target_activity:
        detail_url = (
            "https://www.yuketang.cn/c27/online_courseware/xty/kls/pub_news/"
            f"{target_activity['courseware_id']}/"
        )
        detail_data = await _fetch_json(
            detail_url,
            headers={"xtbz": "ykt", "classroom-id": str(classroom_id)},
            timeout=10,
        )
        detail_payload = detail_data.get("data", {})
        c_course_id = c_course_id or detail_payload.get("course_id")
        s_id = s_id or detail_payload.get("s_id")

    if not c_course_id and video_sku_id:
        c_course_id = classroom_id
        log_warning(f"未解析到 course_id，回退使用 classroom_id={classroom_id}")

    return (
        int(video_sku_id) if video_sku_id else None,
        int(graph_sku_id) if graph_sku_id else None,
        str(c_course_id) if c_course_id is not None else None,
        str(s_id) if s_id is not None else None,
    )


async def _run_video_job(
    worker_id: int,
    video: Dict[str, Any],
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    thread_lock: threading.Lock,
    fast_mode: bool,
) -> bool:
    return await asyncio.to_thread(
        _watch_single_video,
        str(video["id"]),
        classroom_id,
        c_course_id,
        s_id,
        university_id,
        course_name,
        thread_lock,
        fast_mode,
        worker_id,
    )


async def _run_courseware_job(
    worker_id: int,
    graph: Dict[str, Any],
    courseware_sku: int,
    classroom_id: str,
    request_headers: Dict[str, str],
    thread_lock: threading.Lock,
) -> bool:
    with thread_lock:
        log_info(
            f"[线程{worker_id}] 开始处理课件 "
            f"{graph.get('name', graph.get('title', graph.get('id')))} "
            f"(id={graph.get('id')}, type={graph.get('type')})"
        )
    return await asyncio.to_thread(
        process_courseware_item,
        graph,
        thread_lock,
        courseware_sku,
        classroom_id,
        request_headers,
    )


async def _worker_loop(
    worker_id: int,
    queue: "asyncio.Queue[Optional[Job]]",
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    thread_lock: threading.Lock,
    fast_mode: bool,
    courseware_sku: int,
    request_headers: Dict[str, str],
) -> Tuple[int, int, List[Job]]:
    completed = 0
    failed = 0
    failed_jobs: List[Job] = []

    while True:
        job = await queue.get()
        if job is None:
            queue.task_done()
            return completed, failed, failed_jobs

        try:
            if job["kind"] == "video":
                result = await _run_video_job(
                    worker_id,
                    job["payload"],
                    classroom_id,
                    c_course_id,
                    s_id,
                    university_id,
                    course_name,
                    thread_lock,
                    fast_mode,
                )
            else:
                result = await _run_courseware_job(
                    worker_id,
                    job["payload"],
                    courseware_sku,
                    classroom_id,
                    request_headers,
                    thread_lock,
                )

            if result:
                completed += 1
            else:
                failed += 1
                failed_jobs.append(job)
        except Exception as exc:
            log_error(f"[线程{worker_id}] 异步任务执行异常：{exc}")
            failed += 1
            failed_jobs.append(job)
        finally:
            queue.task_done()


async def _run_job_queue(
    jobs: List[Job],
    max_concurrent: int,
    classroom_id: str,
    c_course_id: str,
    s_id: str,
    university_id: int,
    course_name: str,
    thread_lock: threading.Lock,
    fast_mode: bool,
    courseware_sku: int,
    request_headers: Dict[str, str],
    phase_name: str,
) -> Tuple[int, int, List[Job]]:
    if not jobs:
        return 0, 0, []

    queue: "asyncio.Queue[Optional[Job]]" = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    worker_count = min(max_concurrent, len(jobs))
    for _ in range(worker_count):
        queue.put_nowait(None)

    log_info(f"{phase_name}阶段开始，共 {len(jobs)} 个任务，使用 {worker_count} 个并发槽位")

    workers = [
        asyncio.create_task(
            _worker_loop(
                worker_id=index,
                queue=queue,
                classroom_id=classroom_id,
                c_course_id=c_course_id,
                s_id=s_id,
                university_id=university_id,
                course_name=course_name,
                thread_lock=thread_lock,
                fast_mode=fast_mode,
                courseware_sku=courseware_sku,
                request_headers=request_headers,
            )
        )
        for index in range(1, worker_count + 1)
    ]
    results = await asyncio.gather(*workers)
    completed = sum(item[0] for item in results)
    failed = sum(item[1] for item in results)
    failed_jobs: List[Job] = []
    for _, _, worker_failed_jobs in results:
        failed_jobs.extend(worker_failed_jobs)
    log_info(f"{phase_name}阶段完成，成功: {completed}, 失败: {failed}")
    return completed, failed, failed_jobs


async def _run_single_async_course(
    classroom_id: str,
    university_id: int,
    course_info: Dict[str, Any],
    max_concurrent: int,
    fast_mode: bool,
) -> Tuple[int, int]:
    log_info(f"当前选择课程：{course_info.get('name')}")

    chapter_url = "https://www.yuketang.cn/mooc-api/v1/lms/learn/course/chapter"
    chapter_params = {
        "cid": classroom_id,
        "term": "latest",
        "uv_id": university_id,
        "classroom_id": classroom_id,
    }
    headers = _build_headers(classroom_id, university_id)

    chapter_data = await _fetch_json(chapter_url, headers=headers, params=chapter_params, timeout=10)
    videos = _collect_videos(chapter_data)
    graphs = collect_courseware_items(chapter_data, allowed_types=SUPPORTED_LEAF_TYPES)

    log_info(SEPARATOR)
    if graphs:
        courseware_type_counts: Dict[int, int] = {}
        for graph in graphs:
            graph_type = int(graph.get("type", 1))
            courseware_type_counts[graph_type] = courseware_type_counts.get(graph_type, 0) + 1
        type_summary = ", ".join(
            f"type={graph_type}:{count}"
            for graph_type, count in sorted(courseware_type_counts.items())
        )
        log_info(f"总共找到 {len(videos)} 个视频，{len(graphs)} 个课件内容（{type_summary}）")
    else:
        log_info(f"总共找到 {len(videos)} 个视频，0 个课件内容")

    if not videos and not graphs:
        log_warning("未找到任何视频或课件。")
        return 0, 0

    video_sku_id, graph_sku_id, c_course_id, s_id = await _resolve_course_context(
        classroom_id,
        headers,
        videos,
        graphs,
    )
    if not video_sku_id or not c_course_id or not s_id:
        log_error("未获取到异步处理所需的课程上下文，无法继续。")
        return 0, len(videos) + len(graphs)

    log_info(f"使用 course_id: {c_course_id}, s_id: {s_id}, sku_id: {video_sku_id}")

    thread_lock = threading.Lock()
    request_headers = {
        "xtbz": "ykt",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
    }
    courseware_sku = graph_sku_id or video_sku_id

    completed = 0
    failed = 0
    if graphs:
        graph_jobs = [{"kind": "courseware", "payload": graph} for graph in graphs]
        graph_completed, graph_failed, _ = await _run_job_queue(
            jobs=graph_jobs,
            max_concurrent=max_concurrent,
            classroom_id=classroom_id,
            c_course_id=c_course_id,
            s_id=s_id,
            university_id=university_id,
            course_name=course_info.get("name"),
            thread_lock=thread_lock,
            fast_mode=fast_mode,
            courseware_sku=courseware_sku,
            request_headers=request_headers,
            phase_name="课件",
        )
        completed += graph_completed
        failed += graph_failed

    if videos:
        pending_videos, scanned_completed, unknown_count = await asyncio.to_thread(
            scan_videos_for_completion,
            videos,
            classroom_id,
            c_course_id,
            s_id,
            university_id,
            course_info.get("name", ""),
            max_concurrent,
        )
        completed += scanned_completed
        if unknown_count:
            log_warning(
                f"课程《{course_info.get('name')}》有 {unknown_count} 个视频在扫描阶段未判定稳定，"
                "将直接进入补刷阶段。"
            )

        pending_video_jobs = [{"kind": "video", "payload": video} for video in pending_videos]
        retry_round = 0
        stagnant_rounds = 0
        previous_failed_ids: Optional[Tuple[str, ...]] = None
        max_retry_rounds = max(3, len(pending_video_jobs) * 2)

        if not pending_video_jobs:
            log_success(f"课程《{course_info.get('name')}》所有视频覆盖率均已达标。")

        while pending_video_jobs:
            retry_round += 1
            phase_name = "视频" if retry_round == 1 else f"视频失败重试第 {retry_round - 1} 轮"
            video_completed, video_failed, failed_video_jobs = await _run_job_queue(
                jobs=pending_video_jobs,
                max_concurrent=max_concurrent,
                classroom_id=classroom_id,
                c_course_id=c_course_id,
                s_id=s_id,
                university_id=university_id,
                course_name=course_info.get("name"),
                thread_lock=thread_lock,
                fast_mode=fast_mode,
                courseware_sku=courseware_sku,
                request_headers=request_headers,
                phase_name=phase_name,
            )
            completed += video_completed
            if not failed_video_jobs:
                break

            current_failed_ids = tuple(
                sorted(str(job.get("payload", {}).get("id")) for job in failed_video_jobs)
            )
            if current_failed_ids == previous_failed_ids:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            previous_failed_ids = current_failed_ids

            log_warning(
                f"课程《{course_info.get('name')}》仍有 {len(failed_video_jobs)} 个失败视频，"
                "将在本轮结束后自动继续重试。"
            )

            if retry_round >= max_retry_rounds or stagnant_rounds >= 2:
                failed += len(failed_video_jobs)
                log_warning(
                    f"课程《{course_info.get('name')}》失败视频重试已达到保护阈值，"
                    f"停止继续重试。剩余失败视频数：{len(failed_video_jobs)}"
                )
                break

            pending_video_jobs = failed_video_jobs

        if retry_round > 1:
            log_info(f"课程《{course_info.get('name')}》视频阶段共执行 {retry_round} 轮。")

    log_success(f"课程《{course_info.get('name')}》异步处理完成！成功: {completed}, 失败: {failed}")
    return completed, failed


async def run_async_session(
    max_concurrent: int = 5,
    selected_courses: Optional[List[CourseSelection]] = None,
    fast_mode: bool = False,
) -> None:
    if selected_courses is None:
        selected_courses = await asyncio.to_thread(select_courses, _thread_safe_get, True)

    total_completed = 0
    total_failed = 0
    try:
        for index, (classroom_id, university_id, course_info) in enumerate(selected_courses, start=1):
            if len(selected_courses) > 1:
                log_info(SEPARATOR)
                log_info(
                    f"\u5f00\u59cb\u5904\u7406\u7b2c {index}/{len(selected_courses)} "
                    f"\u95e8\u8bfe\u7a0b\uff1a{course_info.get('name')}"
                )

            completed, failed = await _run_single_async_course(
                classroom_id=classroom_id,
                university_id=university_id,
                course_info=course_info,
                max_concurrent=max_concurrent,
                fast_mode=fast_mode,
            )
            total_completed += completed
            total_failed += failed

        if len(selected_courses) > 1:
            log_success(
                f"\u591a\u95e8\u8bfe\u7a0b\u5f02\u6b65\u5904\u7406\u5b8c\u6210\uff01"
                f"\u6210\u529f: {total_completed}, \u5931\u8d25: {total_failed}"
            )
    finally:
        await close_async_session()
