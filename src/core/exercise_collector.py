from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

from src.core.course_selection import CourseSelection, select_courses
from src.core.exercise_solver import (
    _extract_exercise_leaf_ids,
    _get_course_chapter,
    _get_exercise_list,
    _get_leaf_info,
    _parse_problem,
    _session_get,
)
from src.utils.font_decode_utils import load_or_build_font_map
from src.utils.logging_utils import log_info, log_success, log_warning


OUTPUT_DIR = Path("questions")


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]+', "_", name).strip()
    sanitized = sanitized.rstrip(". ")
    return sanitized or "unnamed_course"


def _format_question_block(question: Dict[str, object]) -> List[str]:
    lines = [
        f"### 第{question['index']}题（{question['type_text']}）",
        f"题目：{question['body']}",
    ]

    options = question.get("options") or []
    if options:
        lines.append("选项：")
        for option in options:
            lines.append(f"{option['key']}. {option['value']}")

    lines.append("")
    return lines


def _write_course_questions(
    course_name: str,
    classroom_id: str,
    exercises: List[Dict[str, object]],
) -> Tuple[Path, int]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{_sanitize_filename(course_name)}.txt"

    total_questions = sum(len(exercise["questions"]) for exercise in exercises)
    lines: List[str] = [
        f"# {course_name}",
        f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"classroom_id：{classroom_id}",
        f"测试题数量：{len(exercises)}",
        f"题目数量：{total_questions}",
        "",
        "说明：本文件仅收集题目，不包含答案，不会自动作答。",
        "",
    ]

    for exercise_index, exercise in enumerate(exercises, start=1):
        lines.extend(
            [
                f"## 测试 {exercise_index}：{exercise['chapter_name']} - {exercise['exercise_name']}",
                f"leaf_id：{exercise['leaf_id']}",
                f"题目数：{len(exercise['questions'])}",
                "",
            ]
        )
        for question in exercise["questions"]:
            lines.extend(_format_question_block(question))

    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return output_path, total_questions


def _collect_course_questions(selected_course: CourseSelection) -> Tuple[Optional[Path], int, int]:
    classroom_id, university_id, course_info = selected_course
    course_name = course_info.get("name", "")
    log_info(f"开始收集课程题目：{course_name}")

    chapter_data = _get_course_chapter(classroom_id, university_id)
    if not chapter_data:
        log_warning(f"获取课程章节失败，跳过：{course_name}")
        return None, 0, 0

    exercise_leaves = _extract_exercise_leaf_ids(chapter_data)
    if not exercise_leaves:
        log_warning(f"当前课程未找到测试题：{course_name}")
        return None, 0, 0

    exercises: List[Dict[str, object]] = []
    total_questions = 0
    font_cache: Dict[str, Dict[str, str]] = {}

    for exercise in exercise_leaves:
        leaf_id = exercise["id"]
        exercise_name = exercise.get("name", "未命名测试")
        chapter_name = exercise.get("chapter_name", "未知章节")

        leaf_info = _get_leaf_info(classroom_id, leaf_id, university_id)
        if not leaf_info or not leaf_info.get("success"):
            log_warning(f"获取 leaf_info 失败，跳过：{chapter_name} - {exercise_name}")
            continue

        content_info = leaf_info.get("data", {}).get("content_info", {})
        leaf_type_id = content_info.get("leaf_type_id")
        if not leaf_type_id:
            log_warning(f"未找到 leaf_type_id，跳过：{chapter_name} - {exercise_name}")
            continue

        exercise_list = _get_exercise_list(leaf_type_id, classroom_id, university_id)
        if not exercise_list or not exercise_list.get("success"):
            log_warning(f"获取题目列表失败，跳过：{chapter_name} - {exercise_name}")
            continue

        problems = exercise_list.get("data", {}).get("problems", [])
        if not problems:
            continue

        font_url = exercise_list.get("data", {}).get("font", "")
        font_map: Dict[str, str] = {}
        if font_url:
            if font_url not in font_cache:
                try:
                    font_cache[font_url] = load_or_build_font_map(font_url)
                except Exception as exc:
                    log_warning(f"字体映射解析失败，将继续收集原始文本：{exc}")
                    font_cache[font_url] = {}
            font_map = font_cache[font_url]

        questions: List[Dict[str, object]] = []
        for problem_index, problem in enumerate(problems, start=1):
            parsed_problem = _parse_problem(problem, font_map)
            question_body = (parsed_problem.get("body") or "").strip() or "[空题干]"
            type_text = parsed_problem.get("type_text") or parsed_problem.get("type") or "未知题型"
            options = parsed_problem.get("options") or []
            questions.append(
                {
                    "index": parsed_problem.get("index") or problem_index,
                    "type_text": type_text,
                    "body": question_body,
                    "options": options,
                }
            )

        if questions:
            exercises.append(
                {
                    "leaf_id": leaf_id,
                    "chapter_name": chapter_name,
                    "exercise_name": exercise_name,
                    "questions": questions,
                }
            )
            total_questions += len(questions)

    if not exercises:
        log_warning(f"课程没有可导出的题目：{course_name}")
        return None, 0, 0

    output_path, written_questions = _write_course_questions(course_name, classroom_id, exercises)
    log_success(f"题目收集完成：{course_name} -> {output_path}")
    return output_path, len(exercises), written_questions


def run_collect_questions_session(selected_courses: Optional[List[CourseSelection]] = None) -> None:
    if selected_courses is None:
        selected_courses = select_courses(fetcher=_session_get, allow_multiple=True)

    total_courses = len(selected_courses)
    success_courses = 0
    total_exercises = 0
    total_questions = 0

    for course_index, selection in enumerate(selected_courses, start=1):
        if total_courses > 1:
            course_name = selection[2].get("name", "")
            log_info(f"开始处理第 {course_index}/{total_courses} 门课程：{course_name}")

        output_path, exercise_count, question_count = _collect_course_questions(selection)
        if output_path:
            success_courses += 1
            total_exercises += exercise_count
            total_questions += question_count

    log_success(
        f"题目收集结束：成功导出 {success_courses}/{total_courses} 门课程，"
        f"共 {total_exercises} 个测试、{total_questions} 道题，输出目录：{OUTPUT_DIR}"
    )
