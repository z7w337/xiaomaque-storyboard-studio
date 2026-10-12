# -*- coding: utf-8 -*-
"""任务域存取层（⓪-3·步3 · DAO 隔离层的 tasks 域）。

依据：《数据结构规格手册 v1.0》§1.1（任务对象 9 字段 + `GET /api/tasks` 契约）、§4.2（落库位置）。
用户 2026-09-19 01:25 拍板：**任务落库 = 独立 `data/tasks.json`**（v2 全新引入，无兼容包袱）。

★ 本文件只负责"**存取**"：读/写/删/计数。
  **持久化语义（重启恢复、重试、取消、派发）归批次② F5a** —— 本步只出存取口与空库，
  不接入任何生成流程（无调用方），保证"零行为变化"。

对齐既有分层：物理写入走 `data_io.atomic_write_json`（原子 + 滚动 .bak），
锁的做法对齐 `storage._STORAGE_LOCK`（单把全局 RLock，写路径串行）。
"""
import json
import threading
import time
from pathlib import Path

from data_io import atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TASKS_FILE = DATA_DIR / "tasks.json"

_TASKS_LOCK = threading.RLock()

# 任务对象 9 字段（手册 §1.1）
TASK_FIELDS = ("id", "kind", "title", "state", "pct", "meta", "thumb", "order", "created_at")
STATES = ("queued", "running", "done", "fail", "cancelled")
#                 ↑ ★ ③ 步4-③ 取消留痕：取消不再物理删除，而是置 cancelled 保留记录
#                   ★ A-P3-6（2026-10-02）：counts() **已补 cancelled 桶** —— 原为四态，
#                     与 list_tasks()（含 cancelled）口径不一致（sum(counts) ≠ len(tasks)）；
#                     前端当前不使用 counts（已核实），故补桶对 UI 零影响、只修 API 口径。


def init_tasks() -> None:
    """初始化任务库（幂等）：确保目录与空文件存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_FILE.exists():
        atomic_write_json(TASKS_FILE, {"tasks": []}, bak_every=1)


def _load() -> dict:
    if not TASKS_FILE.exists():
        return {"tasks": []}
    import json
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return {"tasks": []}
        return data
    except (OSError, ValueError):
        return {"tasks": []}


def _save(data: dict) -> None:
    with _TASKS_LOCK:
        atomic_write_json(TASKS_FILE, data, bak_every=1)


def list_tasks() -> list:
    """全部任务（按 order 升序；order 缺省视为尾部）。"""
    tasks = _load().get("tasks", [])
    return sorted(tasks, key=lambda t: (t.get("order") if isinstance(t.get("order"), int) else 10 ** 9))


def get_task(task_id: str) -> dict | None:
    for t in _load().get("tasks", []):
        if t.get("id") == task_id:
            return t
    return None


def save_task(task: dict) -> None:
    """按 id 覆盖写入（不存在则追加）。"""
    if not task.get("id"):
        raise ValueError("task 缺 id")
    with _TASKS_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        for i, t in enumerate(tasks):
            if t.get("id") == task["id"]:
                tasks[i] = task
                break
        else:
            tasks.append(task)
        data["tasks"] = tasks
        _save(data)


def patch_meta(task_id: str, patch: dict) -> dict | None:
    """★ ③ 步2b：**原子**更新 meta（同一把锁内读-改-写）。返回更新后的任务，任务不存在返回 None。

    为什么必须原子 —— `task_queue.set_meta` 原来是 `get_task` → 改 → `save_task` **三步、跨两次加锁**。
    并发下（多任务同时执行 + 取消线程）会互相覆盖：job 线程读到的是"取消之前"的快照，
    写回时就把 `cancel_requested` **抹掉** → 用户点了取消、任务却照跑完并变成 `done`。
    旧的全局串行下这个窗口极小、几乎撞不上；一旦放开并发（步2b）立刻可复现 —— 由 P4 用例抓到。
    """
    with _TASKS_LOCK:
        t = get_task(task_id)
        if not t:
            return None
        m = t.get("meta")
        m = m if isinstance(m, dict) else (json.loads(m or "{}") if isinstance(m, str) else {})
        m.update(patch)
        t["meta"] = json.dumps(m, ensure_ascii=False)
        save_task(t)
        return t


def finalize(task_id: str, state: str, meta_patch: dict, pct: int | None = None,
             cancel_wins: bool = False):
    """★ ③ 步2b附 裁定③：**原子终态写** —— 终态「判定 + 写入」在**同一把锁内**完成。

    为什么需要它：`_run_task` 原来是「get_task → 改 state/meta → save_task」三步，
    若用户**恰在这一瞬**点了取消，`cancel_requested` 会被这一次写覆盖掉 →
    任务静默变成 `done` 且**取消不留痕**（这正是「取消必须留痕」口径的唯一例外）。

    `cancel_wins=True`（完成/失败路径都用）：在同一把锁内**先判** meta 里是否已有
    `cancel_requested` —— 有则**取消优先**，把 state 落成 `cancelled` 并补
    `cancelled_at`（留痕），而**不是**传入的 `state`。

    返回 `(最终 state, 是否被取消改写)`；任务不存在返回 `(None, False)`。
    """
    with _TASKS_LOCK:
        t = get_task(task_id)
        if not t:
            return None, False
        m = t.get("meta")
        m = m if isinstance(m, dict) else (json.loads(m or "{}") if isinstance(m, str) else {})
        cancelled = bool(m.get("cancel_requested")) and cancel_wins
        m.update(meta_patch or {})
        if cancelled:
            m["cancel_requested"] = True
            m["progress"] = "已取消"
            m.setdefault("cancelled_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            t["state"] = "cancelled"
        else:
            t["state"] = state
            if pct is not None:
                t["pct"] = pct
        t["meta"] = json.dumps(m, ensure_ascii=False)
        save_task(t)
        return t["state"], cancelled


def delete_task(task_id: str) -> bool:
    with _TASKS_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        left = [t for t in tasks if t.get("id") != task_id]
        if len(left) == len(tasks):
            return False
        data["tasks"] = left
        _save(data)
        return True


def claim_queued(task_id: str) -> dict | None:
    """③ 步4-③：**原子**地把 `queued → running`（同一把锁内读-查-写）。

    为什么必须下沉到存储层：调度线程的「取快照 → 复查 → 置 running」与取消线程的「置 cancelled」
    是两个线程各自的读改写；分开做就会出现**取消被覆盖回 running**（= 取消了却还在跑）。
    返回改好的任务；若任务不存在、或状态已不是 `queued`（已被取消/移除）→ 返回 `None`，调度器应跳过。
    """
    with _TASKS_LOCK:
        data = _load()
        for t in data.get("tasks", []):
            if t.get("id") == task_id:
                if t.get("state") != "queued":
                    return None
                t["state"] = "running"
                _save(data)
                return t
        return None


def mark_cancelled(task_id: str, meta_patch: dict | None = None) -> dict | None:
    """③ 步4-③：**原子**地把任务置 `cancelled`（留痕，不删记录）；`meta_patch` 合并进 meta。

    与 `claim_queued` 共用同一把锁 → 两者不会交错，"已取消"不会被调度覆盖。
    """
    import json
    with _TASKS_LOCK:
        data = _load()
        for t in data.get("tasks", []):
            if t.get("id") == task_id:
                m = t.get("meta") or "{}"
                try:
                    m = json.loads(m) if isinstance(m, str) else dict(m or {})
                except (ValueError, TypeError):
                    m = {}
                if meta_patch:
                    m.update(meta_patch)
                t["meta"] = json.dumps(m, ensure_ascii=False)
                t["state"] = "cancelled"
                _save(data)
                return t
        return None


def reorder(ids: list) -> None:
    """按给定 id 顺序重排 order 字段（拖动排序用；未覆盖的项排在其后）。"""
    with _TASKS_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        rank = {tid: i for i, tid in enumerate(ids)}
        for t in tasks:
            t["order"] = rank.get(t.get("id"), len(ids) + tasks.index(t))
        _save(data)


def counts() -> dict:
    """五态计数（对齐 `GET /api/tasks` 的 counts 契约）。

    ★ A-P3-6（2026-10-02）：补 `cancelled` 桶 —— 原实现只统计四态，而 `items`（list_tasks）**含**
      cancelled（取消留痕是合法终态）⇒ `sum(counts) != len(items)`，账对不上。
      前端当前不使用 counts（已核实），故此改动对 UI 零影响、只修 API 口径。
    """
    c = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for t in _load().get("tasks", []):
        s = t.get("state")
        if s == "fail":
            c["failed"] += 1
        elif s in ("queued", "running", "done", "cancelled"):
            c[s] += 1
    return c


def new_task(kind: str, title: str, meta: str = "", thumb: str = "") -> dict:
    """构造一个任务对象（9 字段齐）。id 用 storage 同款前缀风格。"""
    import uuid
    from datetime import datetime
    return {
        "id": "tk_" + uuid.uuid4().hex[:12],
        "kind": kind,                     # video | image | audio
        "title": title,
        "state": "queued",
        "pct": 0,
        "meta": meta,
        "thumb": thumb,
        "order": len(_load().get("tasks", [])),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
