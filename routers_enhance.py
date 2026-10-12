# -*- coding: utf-8 -*-
"""①·步3 A3 细节增强（★ 步A 第二个独立路由模块 —— main.py 停止增长）。

v3（批次② F5a，用户 09-21 口径）：并入统一任务队列 task_queue ——
  POST /generate → 入队（data/tasks.json 9 字段）返回 {task_id}；
  GET /task/{task_id} → 统一任务态（state→旧形状映射，前端零改动）；
  POST /cancel/{task_id} → queued=移除 / running=interrupt（job 自检收尾，不落库）。
产品口径不变：无提示词，仅强度控件（默认 0.5 / 上限 0.7）；落库 images[] unshift 不新增字段。
工作流 = workflows/custom/Z-Image-Enhance.json（gentools 阶段 5-2 已真调同款，23 节点）：
  15 LoadImage 原图 ｜ 204 FloatConstant 强度（→202→146/181 KSampler denoise）｜ 146/181 seed ｜ 174 SaveImage。
"""
import json as _json
import random as _rnd
import time as _time

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path

from dao import library
import comfy_client
import task_queue
import task_store

router = APIRouter(prefix="/api/enhance", tags=["enhance"])

_ENHANCE_WF_FILE = "custom/Z-Image-Enhance.json"
_ENHANCE_OUT_NODE = "174"
_ENHANCE_POLL_TIMEOUT = 900


class EnhanceGeneratePayload(BaseModel):
    target_node: str = ""                 # ★ ④ 步4-④：生成节点（F5b 预留；空=本机）
    source_kind: str
    source_id: str
    strength: float = 0.5     # 默认 0.5，上限 0.7（计划书 §5.1 A3，用户已拍板）


def _enhance_job(task_id: str, task: dict):
    """队列执行体：meta.payload 取参 → 上传 → 提交 → 可取消轮询 → 落库（取消不落库）。"""
    payload = EnhanceGeneratePayload(**task_queue._meta_of(task).get("payload", {}))
    asset = library.get_item(payload.source_kind, payload.source_id)
    if not asset:
        raise RuntimeError(f"资产不存在: {payload.source_kind}/{payload.source_id}")
    images = list(asset.get("images") or [])
    if not images or not images[0].get("path"):
        raise RuntimeError("这张资产还没有主图")
    before_rel = images[0]["path"]

    comfy_base = task_queue.node_url_for(task_id, payload.target_node)   # ★ ③F5b步1：job 唯一入口
    root = Path(library.get_shared_assets_path())
    src_abs = str((root / before_rel).resolve())
    if not Path(src_abs).is_file():
        raise RuntimeError(f"当前主图文件缺失: {before_rel}")

    task_queue.set_meta(task_id, {"progress": "上传原图到 ComfyUI…"})
    img_name = comfy_client._upload(comfy_base, src_abs, "image",
                                    comfy_client._ascii_name(src_abs))
    task_queue.set_meta(task_id, {"progress": "组装工作流并提交…"})
    wf = _json.load(open(Path(__file__).parent / "workflows" / _ENHANCE_WF_FILE, encoding="utf-8"))
    wf["15"]["inputs"]["image"] = img_name
    wf["204"]["inputs"]["value"] = float(payload.strength)
    for nid in ("146", "181"):
        wf[nid]["inputs"]["seed"] = _rnd.randint(0, 2**31 - 1)
    wf["174"]["inputs"]["filename_prefix"] = f"enhance/{payload.source_id}"
    prompt_id = comfy_client.submit_workflow(wf, comfy_base)
    task_queue.set_meta(task_id, {"prompt_id": prompt_id, "progress": "增强生成中…", "pct": 40})

    waited, step = 0, 4
    while waited < _ENHANCE_POLL_TIMEOUT:
        if task_queue.is_cancelled(task_id):
            # ① 步4-①：与 edit 同口径 —— 精准取消自己的 prompt，不裸发 /interrupt（防误杀）
            task_queue.comfy_cancel_prompt(prompt_id)
            raise task_queue.QueueCancelled("用户取消")
        try:
            h = requests.get(f"{comfy_base}/history/{prompt_id}", timeout=30).json()
        except (requests.RequestException, ValueError):
            _time.sleep(step); waited += step; continue
        rec = (h or {}).get(prompt_id)
        if rec:
            status = (rec.get("status") or {}).get("status_str") or ""
            if status == "error":
                if task_queue.is_cancelled(task_id):
                    raise task_queue.QueueCancelled("用户取消")
                msgs = (rec.get("status") or {}).get("messages") or []
                raise RuntimeError(f"ComfyUI 任务失败: {msgs}")
            for it in ((rec.get("outputs") or {}).get(_ENHANCE_OUT_NODE, {}) or {}).get("images", []):
                if it.get("filename"):
                    if task_queue.is_cancelled(task_id):        # 落库前守卫
                        raise task_queue.QueueCancelled("用户取消")
                    task_queue.set_meta(task_id, {"progress": "取回产物并入库…", "pct": 92})
                    img_bytes = comfy_client.fetch_output(
                        {"filename": it["filename"], "type": it.get("type") or "output",
                         "subfolder": it.get("subfolder") or ""}, comfy_base)
                    if not img_bytes or img_bytes[:8] != b"\x89PNG\r\n\x1a\n":
                        raise RuntimeError("增强产物校验失败（非 PNG），请重试")
                    sub = Path("drama") / payload.source_kind / payload.source_id
                    d = root / sub
                    d.mkdir(parents=True, exist_ok=True)
                    fname = f"{library.new_id()}.png"
                    (d / fname).write_bytes(img_bytes)
                    rec_img = {"path": (sub / fname).as_posix(), "version": "",
                               "gen_prompt": "细节增强（强度 %.2f）" % payload.strength,
                               "source": "enhance", "added_at": library.now_iso()}
                    asset.setdefault("images", []).insert(0, rec_img)
                    asset["updated_at"] = library.now_iso()
                    library.save_item(payload.source_kind, asset)
                    return {"before": before_rel, "after": rec_img["path"],
                            "asset_id": payload.source_id, "strength": payload.strength}
        _time.sleep(step); waited += step
    raise RuntimeError(f"ComfyUI 任务轮询超时（>{_ENHANCE_POLL_TIMEOUT}s）")


task_queue.register_job("enhance", _enhance_job)


def _state_compat(state: str) -> str:
    return {"queued": "queued", "running": "running",
            "done": "succeeded", "fail": "failed"}.get(state, state)


def _task_view(task_id: str) -> dict:
    t = task_store.get_task(task_id)
    if not t:
        return {"status": "cancelled", "progress": "已取消", "result": None, "error": None}
    meta = task_queue._meta_of(t)
    return {"status": _state_compat(t.get("state")),
            "progress": meta.get("progress", ""),
            "result": meta.get("result"),
            "error": meta.get("error")}


@router.post("/generate")
def api_enhance_generate(payload: EnhanceGeneratePayload):
    """发起细节增强（入队）：{task_id} 立即返回；GET /task/{task_id} 轮询；POST /cancel/{task_id} 取消。"""
    if payload.source_kind not in library.KINDS:
        raise HTTPException(status_code=400, detail=f"未知资产类型: {payload.source_kind}")
    if not 0.05 <= payload.strength <= 0.7:
        raise HTTPException(status_code=400, detail="强度需在 0.05–0.7（默认 0.5）")
    asset = library.get_item(payload.source_kind, payload.source_id)
    if not asset or not (asset.get("images") or []):
        raise HTTPException(status_code=400, detail="触发资产不存在或还没有主图")
    task_id = task_queue.submit("enhance", "细节增强", payload.model_dump())
    return {"task_id": task_id}


@router.get("/task/{task_id}")
def api_enhance_task(task_id: str):
    return _task_view(task_id)


@router.post("/cancel/{task_id}")
def api_enhance_cancel(task_id: str):
    return task_queue.cancel(task_id)
