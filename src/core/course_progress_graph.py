import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Set

from src.core.course_progress import (
    _extract_sku_id_from_logs,
    _get_csrf_token,
    _thread_safe_get,
    _thread_safe_post,
)
from src.core.course_selection import CourseSelection, select_course
from src.network.http_client import SEPARATOR, SESSION_USER_AGENT
from src.utils.logging_utils import log_error, log_info, log_success, log_warning


SUPPORTED_LEAF_TYPES = {1, 2, 3, 5}
PAGE_TYPE_BY_LEAF_TYPE = {
    1: "graph",
    2: "graph",
    3: "notion",
    5: "document",
}


def _build_course_headers(classroom_id: str, university_id: int) -> Dict[str, str]:
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


def _append_courseware_item(
    items: List[Dict[str, Any]],
    seen_ids: Set[str],
    chapter_name: str,
    leaf_id: Any,
    leaf_type: Any,
    title: Any,
    allowed_types: Set[int],
) -> None:
    if leaf_id is None:
        return

    try:
        normalized_type = int(leaf_type)
    except (TypeError, ValueError):
        return

    if normalized_type not in allowed_types:
        return

    normalized_id = str(leaf_id)
    if normalized_id in seen_ids:
        return

    seen_ids.add(normalized_id)
    items.append(
        {
            "id": leaf_id,
            "type": normalized_type,
            "title": str(title or "无标题"),
            "chapter": chapter_name,
        }
    )


def collect_courseware_items(
    chapter_data: Dict[str, Any],
    allowed_types: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    target_types = set(allowed_types or SUPPORTED_LEAF_TYPES)
    items: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    chapters = chapter_data.get("data", {}).get("course_chapter", [])
    for chapter in chapters:
        chapter_name = chapter.get("name", "未知章节")
        for section in chapter.get("section_leaf_list", []):
            _append_courseware_item(
                items,
                seen_ids,
                chapter_name,
                section.get("id"),
                section.get("leaf_type"),
                section.get("name", section.get("title", "无标题")),
                target_types,
            )

            leaf_list = section.get("leaf_list", [])
            if not isinstance(leaf_list, list):
                continue

            for leaf in leaf_list:
                _append_courseware_item(
                    items,
                    seen_ids,
                    chapter_name,
                    leaf.get("id"),
                    leaf.get("leaf_type"),
                    leaf.get("name", leaf.get("title", "无标题")),
                    target_types,
                )

    return items


def _resolve_sku_id(
    classroom_id: str,
    headers: Dict[str, str],
    items: List[Dict[str, Any]],
) -> Optional[int]:
    if not items:
        return None

    sample_id = items[0]["id"]
    url = f"https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{sample_id}/"
    try:
        resp = _thread_safe_get(url, headers=headers, timeout=10)
        data = resp.json().get("data", {})
        sku_id = data.get("sku_id") or data.get("sku", {}).get("id")
        if sku_id:
            return int(sku_id)
    except Exception as exc:
        log_warning(f"从 leaf_info 提取 sku_id 失败：{exc}")

    return _extract_sku_id_from_logs(classroom_id)


def _select_target_types(items: List[Dict[str, Any]]) -> List[int]:
    available_types = {int(item["type"]) for item in items}

    print("\nleaf_type 说明:")
    print("  1 = 课件/graph")
    print("  2 = 课件变体")
    print("  3 = 笔记/notion")
    print("  5 = 文档/document")

    raw_value = input(
        "\n请输入要处理的 leaf_type（多个用逗号分隔，如 1,3,5；回车或 all 表示处理全部支持类型）："
    ).strip()

    if not raw_value or raw_value.lower() == "all":
        selected = [leaf_type for leaf_type in sorted(SUPPORTED_LEAF_TYPES) if leaf_type in available_types]
        log_info(f"将处理类型: {selected}")
        return selected

    try:
        selected = sorted({int(item.strip()) for item in raw_value.split(",") if item.strip()})
    except ValueError:
        log_warning("输入格式错误，使用默认类型 [1, 2, 3, 5]。")
        selected = [leaf_type for leaf_type in sorted(SUPPORTED_LEAF_TYPES) if leaf_type in available_types]

    selected = [leaf_type for leaf_type in selected if leaf_type in SUPPORTED_LEAF_TYPES]
    if not selected:
        selected = [leaf_type for leaf_type in sorted(SUPPORTED_LEAF_TYPES) if leaf_type in available_types]

    log_info(f"将处理类型: {selected}")
    return selected


def process_courseware_item(
    item: Dict[str, Any],
    thread_lock: threading.Lock,
    sku_id: int,
    classroom_id: str,
    headers: Dict[str, str],
) -> bool:
    item_id = item["id"]
    item_title = item.get("title", "无标题")
    item_type = int(item.get("type", 1))

    with thread_lock:
        log_info(f"处理: {item_title} (id={item_id}, type={item_type})")

    status_url = f"https://www.yuketang.cn/mooc-api/v1/lms/learn/user_article_finish_status/{item_id}/"
    try:
        status_resp = _thread_safe_get(status_url, headers=headers, timeout=10)
        status_data = status_resp.json()
        if status_data.get("data", {}).get("finish", 0) == 1:
            with thread_lock:
                log_info(f"  [跳过] 已完成: {item_title}")
            return True

        with thread_lock:
            log_info(f"  [未完成] 开始处理: {item_title}")

        leaf_info_url = f"https://www.yuketang.cn/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{item_id}/"
        _thread_safe_get(leaf_info_url, headers=headers, timeout=10)

        page_type = PAGE_TYPE_BY_LEAF_TYPE.get(item_type, "graph")
        page_url = f"https://www.yuketang.cn/v2/web/lms/{classroom_id}/{page_type}/{item_id}"
        page_headers = headers.copy()
        page_headers["accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        _thread_safe_get(page_url, headers=page_headers, timeout=10)

        finish_url = (
            "https://www.yuketang.cn/mooc-api/v1/lms/learn/"
            f"user_article_finish/{item_id}/?cid={classroom_id}&sid={sku_id}"
        )
        finish_resp = _thread_safe_get(finish_url, headers=headers, timeout=10)
        finish_data = finish_resp.json()

        with thread_lock:
            log_info(f"  user_article_finish 接口: {finish_data}")

        track_url = "https://www.yuketang.cn/video-log/log/track/"
        timestamp_ms = int(time.time() * 1000)
        track_payload = {
            "uip": "",
            "data": {
                "platform": 2,
                "terminal_type": "Web",
                "time": timestamp_ms,
                "language": "zh_CN",
                "original_id": "".join(random.choice("0123456789abcdef") for _ in range(40)),
                "distinct_id": "",
                "event": "page_view",
                "properties": {
                    "channel": "",
                    "classroom_id": int(classroom_id),
                    "user_agent": SESSION_USER_AGENT,
                    "page_name": "雨课堂",
                    "host": "www.yuketang.cn",
                    "url": page_url,
                    "referer": "",
                    "original_referrer": "",
                },
            },
            "ts_ms": timestamp_ms,
        }

        track_headers = headers.copy()
        track_headers["Content-Type"] = "application/json"
        try:
            _thread_safe_post(track_url, data=json.dumps(track_payload), headers=track_headers, timeout=10)
        except Exception:
            pass

        time.sleep(1)
        final_status_resp = _thread_safe_get(status_url, headers=headers, timeout=10)
        final_status = final_status_resp.json()
        if final_status.get("data", {}).get("finish", 0) == 1:
            with thread_lock:
                log_success(f"  ✓ 成功标记完成: {item_title}")
            return True

        with thread_lock:
            log_warning(f"  ? 未完成，当前状态: {final_status}")
        return False
    except Exception as exc:
        with thread_lock:
            log_error(f"  ✗ 处理失败: {exc}")
        return False


def run_graph_session(
    selected_course: Optional[CourseSelection] = None,
    target_types: Optional[Iterable[int]] = None,
    confirm_start: bool = True,
    use_multithread: Optional[bool] = None,
    max_workers: Optional[int] = None,
) -> None:
    if selected_course is None:
        classroom_id, university_id, course_info = select_course(_thread_safe_get)
    else:
        classroom_id, university_id, course_info = selected_course
    log_info(f"当前选择课程：{course_info.get('name')}")

    chapter_url = "https://www.yuketang.cn/mooc-api/v1/lms/learn/course/chapter"
    chapter_params = {
        "cid": classroom_id,
        "term": "latest",
        "uv_id": university_id,
        "classroom_id": classroom_id,
    }
    headers = _build_course_headers(classroom_id, university_id)

    resp = _thread_safe_get(chapter_url, params=chapter_params, headers=headers, timeout=10)
    chapter_data = resp.json()

    all_items = collect_courseware_items(chapter_data)
    log_info(SEPARATOR)
    log_info(f"扫描到 {len(all_items)} 个可处理内容项。")

    if not all_items:
        log_warning("未找到任何可处理的课件内容。")
        return

    normalized_target_types = set(target_types) if target_types is not None else set(_select_target_types(all_items))
    items_to_process = [item for item in all_items if int(item["type"]) in normalized_target_types]

    if not items_to_process:
        log_warning(f"没有找到类型为 {sorted(normalized_target_types)} 的内容。")
        return

    for idx, item in enumerate(items_to_process):
        log_info(f"  [{idx}] {item['chapter']} - type={item['type']} - {item['title']}")

    if confirm_start:
        confirm = input("\n确认访问这些内容？(y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            log_info("取消操作。")
            return

    sku_id = _resolve_sku_id(classroom_id, headers, items_to_process)
    if not sku_id:
        log_error("未获取到 sku_id，无法标记课件完成。")
        return

    log_info(f"使用 sku_id: {sku_id}")

    if use_multithread is None:
        use_multithread_input = input("是否使用多线程访问课件？(y/n，默认 y): ").strip().lower()
        use_multithread = use_multithread_input in ("", "y", "yes")

    if max_workers is None:
        max_workers = 1
        if use_multithread:
            raw_workers = input("请输入并发线程数 (1-5，默认 3): ").strip()
            try:
                max_workers = max(1, min(5, int(raw_workers)))
            except ValueError:
                max_workers = 3
    else:
        max_workers = max(1, min(5, int(max_workers)))

    log_info("开始访问课件...")
    request_headers = {
        "xtbz": "ykt",
        "classroom-id": str(classroom_id),
        "university-id": str(university_id),
        "uv-id": str(university_id),
    }

    if max_workers > 1:
        thread_lock = threading.Lock()
        completed = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="GraphWorker") as executor:
            futures = [
                executor.submit(
                    process_courseware_item,
                    item,
                    thread_lock,
                    sku_id,
                    classroom_id,
                    request_headers,
                )
                for item in items_to_process
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

        log_success(f"多线程访问完成！成功: {completed}, 失败: {failed}")
        return

    thread_lock = threading.Lock()
    success_count = 0
    for item in items_to_process:
        if process_courseware_item(item, thread_lock, sku_id, classroom_id, request_headers):
            success_count += 1
        time.sleep(random.uniform(0.8, 1.5))

    log_success(f"完成！成功标记 {success_count}/{len(items_to_process)} 个内容。")
