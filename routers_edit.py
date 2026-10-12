# -*- coding: utf-8 -*-
"""①·步2 A2 Qwen-Edit 指令编辑（★ 步A 首个独立路由模块 —— main.py 停止增长）。

v3（批次② F5a，用户 09-21 口径）：并入统一任务队列 task_queue ——
  POST /generate → task_store.new_task（data/tasks.json 9 字段）+ 入队，立即返回 {task_id}；
  GET /task/{task_id} → 统一任务态（state→旧形状映射，前端零改动）；
  POST /cancel/{task_id} → queued=移除（永不进入生成）/ running=interrupt（job 自检收尾）。
  被取消的任务不落库（job 内自检）。

v1 红线不变：界面/文档不出现 "task_type"；落库（手册 §2.1/§2.2）：产物 images[] unshift。
工作流：09-19 实测真实提交（PNG 元数据提取；可研 §3.3.3 的 Diffsynth* 节点本机不存在，失效信息）。
"""
import time as _time

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path

from dao import library
import comfy_client
import task_queue
import task_store
import ms_image

router = APIRouter(prefix="/api/edit", tags=["edit"])

_EDIT_WF: dict = {
    "4": {"inputs": {"vae_name": "qwen_image_vae.safetensors"}, "class_type": "VAELoader"},
    "5": {"inputs": {"unet_name": "qwen_image_edit_2509_fp8_e4m3fn.safetensors",
                     "weight_dtype": "default"}, "class_type": "UNETLoader"},
    "6": {"inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                     "type": "qwen_image"}, "class_type": "CLIPLoader"},
    "7": {"inputs": {"image": "input.jpg"}, "class_type": "LoadImage"},
    "8": {"inputs": {
        "model": ["5", 0], "clip": ["6", 0], "vae": ["4", 0],
        "positive_prompt": "<运行期注入：自然语言编辑指令>",
        "negative_prompt": "cartoon, anime, low quality, blurry, distorted, extra limbs",
        "generation_mode": "图生图 image-to-image",
        "batch_size": 1, "width": 1024, "height": 1024,
        "seed": 123456789, "steps": 4, "cfg": 1.0,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        "image1": ["7", 0],
    }, "class_type": "QwenImageIntegratedKSampler"},
    "9": {"inputs": {"images": ["8", 0], "filename_prefix": "edit/asset"},
          "class_type": "SaveImage"},
}
_EDIT_OUT_NODE = "9"
_EDIT_POLL_TIMEOUT = 900


class EditGeneratePayload(BaseModel):
    target_node: str = ""                 # ★ ④ 步4-④：生成节点（F5b 预留；空=本机）
    use_cloud: bool = False               # ★ ④ F5c：手动勾选走云端（channel 由提交口统一判定）
    cloud_model: str = ""                 # ★ ④ F5c：本次使用的云端模型 id（空=设置默认图生图模型）
    source_kind: str
    source_id: str
    instruction: str
    seed: int | None = None


def _archive_edit_result(payload: "EditGeneratePayload", asset: dict, img_bytes: bytes,
                         gen_prompt: str) -> str:
    """云端/本地共用的落库：产物 PNG 落盘 + images[] unshift（复用版本管理语义）。"""
    root = Path(library.get_shared_assets_path())
    sub = Path("drama") / payload.source_kind / payload.source_id
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{library.new_id()}.png"
    (d / fname).write_bytes(img_bytes)
    rec_img = {"path": (sub / fname).as_posix(), "version": "",
               "gen_prompt": gen_prompt, "source": "edit",
               "added_at": library.now_iso()}
    asset.setdefault("images", []).insert(0, rec_img)
    asset["updated_at"] = library.now_iso()
    library.save_item(payload.source_kind, asset)
    return rec_img["path"]


def _cloud_edit_job(task_id: str, raw_pl: dict):
    """★ ④ F5c：指令编辑云端执行体（Qwen-Image-Edit-2509；主图作参考图 + 编辑指令）。"""
    payload = EditGeneratePayload(**{k: v for k, v in raw_pl.items()
                                     if k in EditGeneratePayload.model_fields})
    instruction = payload.instruction.strip()
    asset = library.get_item(payload.source_kind, payload.source_id)
    if not asset:
        raise RuntimeError(f"资产不存在: {payload.source_kind}/{payload.source_id}")
    images = list(asset.get("images") or [])
    if not images or not images[0].get("path"):
        raise RuntimeError("这张资产还没有主图")
    before_rel = images[0]["path"]
    root = Path(library.get_shared_assets_path())
    src_abs = str((root / before_rel).resolve())
    if not Path(src_abs).is_file():
        raise RuntimeError(f"当前主图文件缺失: {before_rel}")

    task_queue.set_meta(task_id, {"progress": "提交云端编辑…"})
    model = (raw_pl.get("cloud_model") or "").strip() or ms_image.config()["model_i2i"]
    if "edit" not in model.lower():     # ★ 指令编辑必须用图生图模型（否则会变成"只按指令文生图"的静默错配）
        raise RuntimeError("「%s」是文生图模型，不能做指令编辑 —— 请在平台里选 Edit 类模型" % model.split("/")[-1])
    try:
        img_bytes = ms_image.generate(
            instruction, model=model, ref_images=[src_abs],
            cancel_check=lambda: task_queue.is_cancelled(task_id),
            progress_cb=lambda m: task_queue.set_meta(task_id, {"progress": m, "pct": 60}))
    except ms_image.MsCancelled:
        raise task_queue.QueueCancelled("用户取消")
    if task_queue.is_cancelled(task_id):      # 落库前守卫（取消不落库）
        raise task_queue.QueueCancelled("用户取消")
    if img_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("云端产物校验失败（非 PNG），请重试")
    ms_image.record_usage("i2i")
    task_queue.set_meta(task_id, {"progress": "取回产物并入库…", "pct": 90})
    after = _archive_edit_result(payload, asset, img_bytes, instruction)
    return {"before": before_rel, "after": after, "asset_id": payload.source_id, "cloud": True}


def _edit_job(task_id: str, task: dict):
    """队列执行体：meta.payload 取参 → 上传 → 提交 → 可取消轮询 → 落库。"""
    raw_pl = task_queue._meta_of(task).get("payload", {})
    if raw_pl.get("channel") == "modelscope":   # ★ ④ F5c：云端通道
        return _cloud_edit_job(task_id, raw_pl)
    payload = EditGeneratePayload(**raw_pl)
    instruction = payload.instruction.strip()
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
    char_name = comfy_client._upload(comfy_base, src_abs, "image",
                                     comfy_client._ascii_name(src_abs))
    task_queue.set_meta(task_id, {"progress": "组装工作流并提交…"})
    seed = payload.seed if payload.seed is not None else int(_time.time() * 1000) % (2**31)
    wf = {nid: {"inputs": dict(n["inputs"]), "class_type": n["class_type"]}
          for nid, n in _EDIT_WF.items()}
    wf["7"]["inputs"]["image"] = char_name
    wf["8"]["inputs"]["positive_prompt"] = instruction
    wf["8"]["inputs"]["seed"] = seed
    wf["9"]["inputs"]["filename_prefix"] = f"edit/{payload.source_id}"
    prompt_id = comfy_client.submit_workflow(wf, comfy_base)
    task_queue.set_meta(task_id, {"prompt_id": prompt_id, "progress": "生成中（Lightning 4 步，约 30s）…"})

    waited, step = 0, 4
    while waited < _EDIT_POLL_TIMEOUT:
        if task_queue.is_cancelled(task_id):
            # ① 步4-①：精准取消**自己的** prompt（delete 优先；只有"在跑的是自己"才 interrupt）——
            #    不再裸发 /interrupt（那会打断当前正在跑的**任意**任务 = 误杀风险）
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
            for it in ((rec.get("outputs") or {}).get(_EDIT_OUT_NODE, {}) or {}).get("images", []):
                if it.get("filename"):
                    if task_queue.is_cancelled(task_id):        # 落库前守卫
                        raise task_queue.QueueCancelled("用户取消")
                    task_queue.set_meta(task_id, {"progress": "取回产物并入库…", "pct": 90})
                    img_bytes = comfy_client.fetch_output(
                        {"filename": it["filename"], "type": it.get("type") or "output",
                         "subfolder": it.get("subfolder") or ""}, comfy_base)
                    if not img_bytes or img_bytes[:8] != b"\x89PNG\r\n\x1a\n":
                        raise RuntimeError("编辑产物校验失败（非 PNG），请重试")
                    sub = Path("drama") / payload.source_kind / payload.source_id
                    d = root / sub
                    d.mkdir(parents=True, exist_ok=True)
                    fname = f"{library.new_id()}.png"
                    (d / fname).write_bytes(img_bytes)
                    rec_img = {"path": (sub / fname).as_posix(), "version": "",
                               "gen_prompt": instruction, "source": "edit",
                               "added_at": library.now_iso()}
                    asset.setdefault("images", []).insert(0, rec_img)
                    asset["updated_at"] = library.now_iso()
                    library.save_item(payload.source_kind, asset)
                    return {"before": before_rel, "after": rec_img["path"],
                            "asset_id": payload.source_id, "seed": seed}
        _time.sleep(step); waited += step
    raise RuntimeError(f"ComfyUI 任务轮询超时（>{_EDIT_POLL_TIMEOUT}s）")


task_queue.register_job("edit", _edit_job)


def _state_compat(state: str) -> str:
    """统一 state → 旧前端认的 status 形状（前端零改动）。"""
    return {"queued": "queued", "running": "running",
            "done": "succeeded", "fail": "failed"}.get(state, state)


def _task_view(task_id: str) -> dict:
    t = task_store.get_task(task_id)
    if not t:
        # 被取消的任务已从队列移除 → 前端按 cancelled 处理（轮询 404 的兼容面）
        return {"status": "cancelled", "progress": "已取消", "result": None, "error": None}
    meta = task_queue._meta_of(t)
    return {"status": _state_compat(t.get("state")),
            "progress": meta.get("progress", ""),
            "result": meta.get("result"),
            "error": meta.get("error")}


@router.post("/generate")
def api_edit_generate(payload: EditGeneratePayload):
    """发起指令编辑（入队）：立即返回 {task_id}；进度/结果走 GET /task/{task_id}；取消走 POST /cancel。"""
    if payload.source_kind not in library.KINDS:
        raise HTTPException(status_code=400, detail=f"未知资产类型: {payload.source_kind}")
    instruction = (payload.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="请填写编辑指令（要具体，如：把衣服改成红色，其他都保持不变）")
    asset = library.get_item(payload.source_kind, payload.source_id)
    if not asset or not (asset.get("images") or []):
        raise HTTPException(status_code=400, detail="触发资产不存在或还没有主图")
    pl = payload.model_dump()
    try:
        pl["channel"] = ms_image.route_channel(bool(pl.get("use_cloud")), "edit")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    task_id = task_queue.submit("edit", "指令编辑", pl)
    return {"task_id": task_id}


@router.get("/task/{task_id}")
def api_edit_task(task_id: str):
    return _task_view(task_id)


@router.post("/cancel/{task_id}")
def api_edit_cancel(task_id: str):
    return task_queue.cancel(task_id)
