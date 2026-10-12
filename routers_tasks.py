"""routers_tasks.py —— F5a 统一任务 API（批次②·步1）。

★ 挂载点红线：本模块必须在 main.py 的 GET/POST /api/{kind} 通配路由**之前** include，
否则 /api/tasks 会被通配吞掉（kind="tasks" 校验 400）——挂载处有注释固化。

- GET  /api/tasks                  列表 + counts + queued 的排队位次（1 起）+ scheduler_alive（★ T3 心跳）
- POST /api/tasks/{tid}/cancel     queued=移除（永不进入生成）；running=interrupt 后由 job 收尾移除
- POST /api/tasks/{tid}/retry      fail → 同参重新入队（队尾）
- POST /api/tasks/reorder          body {ids:[...]} 全量顺序（拖动调序；未出现的 queued 视为移出=中止）
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

import task_queue
import task_store

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_KIND_TITLE = {"edit": "指令编辑", "enhance": "细节增强", "motion": "动作迁移",
               "voice": "音色样本", "gsplat": "3D 重建（SHARP）",
               "image": "生图", "video": "场视频"}


def _pub(t: dict) -> dict:
    """task_store 记录 → 前端形状：meta 解析、queued 附排队位次。"""
    out = dict(t)
    try:
        out["meta"] = json.loads(t.get("meta") or "{}")
    except (ValueError, TypeError):
        out["meta"] = {}
    out["progress"] = out["meta"].get("progress", "")
    if t.get("state") == "queued":
        out["position"] = task_queue.position_of(t["id"])
    return out


@router.get("")
def list_all():
    ts = sorted(task_store.list_tasks(),
                key=lambda x: (x.get("order") or 0, x.get("created_at") or ""))
    return {"tasks": [_pub(t) for t in ts], "counts": task_store.counts(),
            # ★ 派单 001 · T3（2026-10-11）：调度器心跳 —— 让「静默停摆」（线程悄悄死掉、
            #   任务永远排队）变成**可观测**。字段为附加项，前端不读也不受影响。
            "scheduler_alive": task_queue.scheduler_status()}


@router.get("/{task_id}")
def get_one(task_id: str):
    t = task_store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _pub(t)


@router.post("/{task_id}/cancel")
def cancel_one(task_id: str):
    return task_queue.cancel(task_id)


@router.post("/{task_id}/remove")
def remove_one(task_id: str):
    """删除任务记录（清理失败/历史残留）。running 先打断 → 由 job 自检收尾，避免继续写盘。"""
    t = task_store.get_task(task_id)
    if not t:
        return {"ok": True, "status": "gone"}
    if t.get("state") == "running":
        task_queue.cancel(task_id)
    task_store.delete_task(task_id)
    task_queue.wake()
    return {"ok": True, "status": "removed"}


@router.post("/{task_id}/retry")
def retry_one(task_id: str):
    t = task_store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t.get("state") != "fail":
        raise HTTPException(status_code=400, detail="仅失败任务可重试")
    meta = t.get("meta") or "{}"
    new_id = task_queue.submit_retry(t["kind"], _KIND_TITLE.get(t["kind"], t["kind"]) + "（重试）",
                                     meta, thumb=t.get("thumb", ""))
    return {"ok": True, "task_id": new_id}


@router.post("/reorder")
async def reorder_all(request: Request):
    """全量顺序提交：ids 里出现的按给出顺序重排；列表里有而 ids 没有的 queued 任务 = 移出（中止）。"""
    body = await request.json()
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids 必须为数组")
    before = {t["id"]: t for t in task_store.list_tasks()}
    # 移出 = 中止：queued 且不在 ids 里 → 删除（running/done/fail 不受调序影响）
    removed = [tid for tid, t in before.items()
               if t.get("state") == "queued" and tid not in ids]
    for tid in removed:
        task_store.delete_task(tid)
    # 只对现存任务重排（保持相对顺序追加到尾部）
    existing = [tid for tid in ids if tid in before and tid not in removed]
    if existing:
        task_store.reorder(existing)
    return {"ok": True, "removed": removed, "reordered": len(existing)}
