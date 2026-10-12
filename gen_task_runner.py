# -*- coding: utf-8 -*-
"""生成任务运行器（步A 共享件）：异步任务簿 + 取消语义，A1/A2/A3 三域共用。

协议（前端）：
  spawn → {task_id}；GET 任务状态 {status: queued|running|succeeded|failed|cancelled, progress, result, prompt_id}；
  request_cancel(task_id) → 标记取消 + 由域侧对 ComfyUI 发中断（/interrupt 或 /queue delete）。
取消语义（手册口径延伸）：被取消的任务**不落库**；poll 循环每轮检查 is_cancelled，命中即抛 CancelledError。
"""
import threading
import time
import uuid
import traceback

_TASKS: dict = {}
_LOCK = threading.Lock()


class CancelledError(RuntimeError):
    """用户主动取消（非故障）——worker 捕获后任务置 cancelled，不落库。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def spawn_task(fn, *args) -> str:
    """fn(task_id, task_dict, *args) —— 域侧 worker；返回 task_id。"""
    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        _TASKS[tid] = {"status": "queued", "progress": "排队中…", "created_at": _now(),
                       "cancel": False, "result": None, "error": None, "prompt_id": None}
    threading.Thread(target=_wrap, args=(tid, fn, args), daemon=True).start()
    return tid


def _wrap(tid: str, fn, args) -> None:
    t = _TASKS[tid]
    try:
        result = fn(tid, t, *args)
        if t["cancel"]:
            t["status"] = "cancelled"; t["progress"] = "已取消"; return
        t["status"] = "succeeded"
        t["progress"] = "完成"
        t["result"] = result
    except CancelledError as e:
        t["status"] = "cancelled"
        t["progress"] = "已取消"
        t["error"] = str(e)
    except Exception as e:
        t["status"] = "failed"
        t["progress"] = "失败"
        tail = "".join(traceback.format_exception_only(type(e), e)).strip()[-300:]
        t["error"] = "%s | at %s" % (e, tail)


def get_task(tid: str) -> dict | None:
    t = _TASKS.get(tid)
    if not t:
        return None
    out = {k: v for k, v in t.items() if k != "cancel"}
    out["task_id"] = tid
    return out


def is_cancelled(tid: str) -> bool:
    t = _TASKS.get(tid)
    return bool(t and t["cancel"])


def request_cancel(tid: str) -> bool:
    t = _TASKS.get(tid)
    if not t:
        return False
    t["cancel"] = True
    return True


def sleep_checked(seconds: float, tid: str, step: float = 4.0) -> None:
    """poll 循环用的可取消 sleep（每步检查取消标记）。"""
    waited = 0.0
    while waited < seconds:
        if is_cancelled(tid):
            raise CancelledError("用户已取消")
        time.sleep(step)
        waited += step
