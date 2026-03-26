from typing import Any, Dict, List, Optional, Tuple

from src.network.http_client import SEPARATOR
from src.utils.logging_utils import log_info, log_warning


CourseSelection = Tuple[str, int, Dict[str, Any]]


def _fetch_course_list(fetcher) -> List[Dict[str, Any]]:
    url = "https://www.yuketang.cn/v2/api/web/courses/list?identity=2"
    response = fetcher(url=url)
    course_response = response.json()
    course_list = course_response.get("data", {}).get("list", [])

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
        classroom_id = str(course_info["classroom_id"])
        university_id = int(course_info.get("course", {}).get("university_id", 0))
        if not university_id:
            log_warning("未获取到 university_id，后续部分接口可能会失败。")
        selections.append((classroom_id, university_id, course_info))
    return selections


def select_courses(fetcher, allow_multiple: bool = False) -> List[CourseSelection]:
    course_list = _fetch_course_list(fetcher)

    if len(course_list) == 1:
        return _build_course_selections(course_list, [0])

    for index, course in enumerate(course_list):
        log_info(f"序号：{index} ----- {course['name']}")
    log_info(SEPARATOR)

    prompt = "请输入需要操作的课程编号：\n"
    if allow_multiple:
        prompt = "请输入需要操作的课程编号（多个用逗号分隔，如 1,3,5；all 表示全部）：\n"

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


def select_course(fetcher) -> CourseSelection:
    return select_courses(fetcher, allow_multiple=False)[0]
