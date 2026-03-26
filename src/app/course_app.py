import asyncio
from typing import Callable

from src.auth.cookies_manager import get_login_status, load_cookies
from src.auth.login_workflow import run_websocket_login
from src.core.course_progress import run_discussion_comment_session
from src.core.course_progress_async import run_async_session
from src.core.course_progress_graph import SUPPORTED_LEAF_TYPES, run_graph_session
from src.core.course_progress_multithread import run_video_session
from src.core.course_selection import CourseSelection, select_courses
from src.core.exercise_collector import run_collect_questions_session
from src.core.exercise_solver import run_exercise_solver_session
from src.network.http_client import SEPARATOR
from src.utils.logging_utils import log_error, log_info, log_warning


def _ensure_login() -> None:
    load_cookies()

    login_status = get_login_status()
    if login_status.is_valid:
        log_info(login_status.reason)
        return

    log_warning(login_status.reason)
    log_info("开始扫码登录流程...")
    asyncio.run(run_websocket_login())


def _fetch_courses(**kwargs):
    from src.core.course_progress import _thread_safe_get

    return _thread_safe_get(**kwargs)


def _run_for_selected_courses(
    title: str,
    runner: Callable[[CourseSelection], None],
    allow_multiple: bool = True,
) -> None:
    selected_courses = select_courses(fetcher=_fetch_courses, allow_multiple=allow_multiple)
    total = len(selected_courses)

    for index, selection in enumerate(selected_courses, start=1):
        if total > 1:
            log_info(SEPARATOR)
            log_info(f"开始处理第 {index}/{total} 个{title}：{selection[2].get('name')}")
        runner(selection)

    if total > 1:
        log_info(f"{title}已全部处理完成。")


def _run_multithread_session() -> None:
    max_workers_input = input("请输入并发线程数 (1-5，默认3): ").strip()
    try:
        max_workers = max(1, min(5, int(max_workers_input)))
    except ValueError:
        max_workers = 3

    fast_mode_input = input("是否使用快速模式？(y/n，默认 n，风险更高): ").strip().lower()
    fast_mode = fast_mode_input in ("y", "yes")
    if fast_mode:
        log_warning("【警告】快速模式速度更快，但被检测风险更高。")

    selected_courses = select_courses(fetcher=_fetch_courses, allow_multiple=True)
    confirm = input(f"确认开始处理 {len(selected_courses)} 门课程？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        log_info("取消操作。")
        return

    for index, selection in enumerate(selected_courses, start=1):
        course_name = selection[2].get("name")
        if len(selected_courses) > 1:
            log_info(SEPARATOR)
            log_info(f"开始处理第 {index}/{len(selected_courses)} 门课程：{course_name}")

        run_graph_session(
            selected_course=selection,
            target_types=SUPPORTED_LEAF_TYPES,
            confirm_start=False,
            use_multithread=max_workers > 1,
            max_workers=max_workers,
        )
        run_video_session(
            selected_course=selection,
            max_workers=max_workers,
            fast_mode=fast_mode,
        )


def _run_async_entry() -> None:
    max_concurrent_input = input("请输入并发数 (1-10，默认5): ").strip()
    try:
        max_concurrent = max(1, min(10, int(max_concurrent_input)))
    except ValueError:
        max_concurrent = 5

    selected_courses = select_courses(fetcher=_fetch_courses, allow_multiple=True)

    fast_mode_input = input("是否使用快速模式？(y/n，默认 n，风险更高): ").strip().lower()
    fast_mode = fast_mode_input in ("y", "yes")
    if fast_mode:
        log_warning("【警告】快速模式速度更快，但被检测风险更高。")

    confirm = input(f"确认开始异步处理 {len(selected_courses)} 门课程？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        log_info("取消操作。")
        return

    log_info(f"使用 {max_concurrent} 个并发")
    asyncio.run(
        run_async_session(
            max_concurrent=max_concurrent,
            selected_courses=selected_courses,
            fast_mode=fast_mode,
        )
    )


def main() -> None:
    _ensure_login()

    while True:
        print("请选择功能：")
        print("1. 自动刷讨论题评论")
        print("2. 自动刷测试题")
        print("3. 查看/完成课件")
        print("4. 多线程刷视频和课件")
        print("5. 异步刷视频和课件 (推荐)")
        print("6. 收集测试题（不作答）")
        print("0. 退出")
        choice = input("请输入功能编号：").strip()

        try:
            if choice == "1":
                _run_for_selected_courses(
                    "课程讨论题",
                    lambda selection: run_discussion_comment_session(selected_course=selection),
                )
            elif choice == "2":
                _run_for_selected_courses(
                    "课程测试题",
                    lambda selection: run_exercise_solver_session(selected_course=selection),
                )
            elif choice == "3":
                _run_for_selected_courses(
                    "课程课件",
                    lambda selection: run_graph_session(selected_course=selection),
                )
            elif choice == "4":
                _run_multithread_session()
            elif choice == "5":
                _run_async_entry()
            elif choice == "6":
                run_collect_questions_session()
            elif choice == "0":
                log_info("已退出程序，再见。")
                break
            else:
                log_info("输入有误，请重新选择。")
        except Exception as exc:
            import traceback

            log_error(f"处理过程中出现异常：{exc}")
            log_error(f"详细堆栈：\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
