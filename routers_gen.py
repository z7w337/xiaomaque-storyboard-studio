# -*- coding: utf-8 -*-
"""routers_gen.py —— 生成域并入统一任务队列（批次② F5a · 步2「队列本体」· 第一批）

★ 步2 的真实内容（2026-09-21 核实结论，非原排期字面）：
  原排期写的「gen_task_runner 升级：持久化→task_store + 并发闸 + 重试」**其实已经完成** ——
  `task_store`（持久化）/ `task_queue`（串行调度 = 并发闸=1）/ `/api/tasks/{id}/retry`（重试）
  早已落地，`routers_edit` / `routers_enhance` / `motion` 三域也早已入队。
  **剩下的"最后一公里" = 其余生成路径收编**：

    · `/api/voice-sample/generate`  音色样本（原**同步** ~10s）        ← ✅ 步2 第一批收编
    · `/api/gsplat/generate`        SHARP 单图重建（原**同步** 1–3 分钟）← ✅ 步2 第一批收编
    · `/api/comfy/generate`         统一编辑器生图（原同步直出 PNG）      ← ✅ 步2遗留收编（契约={task_id}，前端 assets.html 同改）
    · `/api/video/generate`         场页生视频（原自带 VIDEO_TASKS）      ← ✅ 步2遗留收编 → **routers_video.py**（自有机制退役）
    · `/api/ic/generate`            已 **DEPRECATED** → ✅ 步2遗留**已删**（前端零调用，IC 只读代理保留）

  ★ ②步2遗留（2026-09-22）后：**全站 8 条生成路径全部入统一队列**（ic 除外，已删）——③ F5b 的
    多节点调度从此覆盖全部生成域。

口径与 A2/A3 完全一致：端点 → `task_queue.submit(...)` 立即返回 `{task_id}`；
进度 / 取消 / 重试走 `/api/tasks`；**工作流组装与落库都在 job 内完成，且取消不落库**（job 内自检）。

★ 契约变化（前端须同步改，见交付说明）：
  两个端点由「同步返回产物」改为「返回 {task_id}」——
  voice 原返回 `{path,size,asset_id,name,audio}`，gsplat 原返回 `{ply,url,prompt_id,ply_abs}`
  → 现在都改从 `/api/tasks` 的 `meta.result` 取（字段名保持不变）。
"""
from __future__ import annotations

import json
import time as _time
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import comfy_client
import task_queue
import ply_proxy
import ms_image
from dao import library

router = APIRouter(tags=["gen"])


class GsplatGeneratePayload(BaseModel):
    target_node: str = ""                 # ★ ④ 步4-④：生成节点（F5b 预留；空=本机）
    ref_image: str = ""                 # 参考图（相对 SharedAssets / 绝对 / http）
    focal_length_mm: float = 30.0       # 相机焦距（方案 GS 规格默认 30.0）


class VoiceSamplePayload(BaseModel):
    target_node: str = ""                 # ★ ④ 步4-④：生成节点（F5b 预留；空=本机）
    asset_id: str = ""          # 资产 id（音色仅人物，ch_ 前缀）
    kind: str = "characters"    # 资产类别（默认人物）
    instruct: str = ""          # 音色描述文本（必填）
    text: str = ""              # 朗读文本（留空=按资产名自动生成测试句）
    language: str = "Chinese"


_VOICE_POLL_MAX = 100           # 100 × 3s = 300s 上限（与原同步实现同口径）
_VOICE_POLL_STEP = 3


# ① 步4-①：原先此处有一份私有 `_comfy_interrupt`（只裸发 /interrupt，有误杀风险）——
# 已**删除并收口**到 `task_queue.comfy_cancel_prompt()`（delete 优先 / 只打断自己的 / /history 校验）。


# ══════════════════ 音色样本（Qwen3-TTS VoiceDesign）══════════════════

def _voice_job(task_id: str, task: dict):
    """队列执行体：组 TTS 工作流 → 提交 → 可取消轮询 → 落库 audios[]（role=voice_sample）。"""
    p = VoiceSamplePayload(**task_queue._meta_of(task).get("payload", {}))
    asset_id = (p.asset_id or "").strip()
    kind = (p.kind or "characters").strip()
    instruct = (p.instruct or "").strip()
    item = library.get_item(kind, asset_id)
    if not item:
        raise RuntimeError(f"资产不存在：{kind}/{asset_id}")
    name = f"{item.get('prefix', '')}-{item.get('variant_name', '')}".strip("-")
    text = (p.text or "").strip() or f"大家好，我是{item.get('prefix') or '这个角色'}。很高兴认识你。"
    language = (p.language or "Chinese").strip() or "Chinese"

    base = task_queue.node_url_for(task_id, p.target_node)   # ★ ③F5b步1：job 唯一入口
    workflow = {"prompt": {
        "1": {"class_type": "TDQwen3TTSModelLoader", "inputs": {
            "model_path": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "precision": "bf16", "device": "cuda",
            "attn_implementation": "sdpa", "auto_download": False, "download_source": "ModelScope"}},
        "2": {"class_type": "TDQwen3TTSVoiceDesign", "inputs": {
            "model": ["1", 0], "text": text, "instruct": instruct, "language": language}},
        "3": {"class_type": "SaveAudio", "inputs": {"audio": ["2", 0], "filename_prefix": f"vs_{asset_id}"}},
    }}

    task_queue.set_meta(task_id, {"progress": "提交语音设计工作流…"})
    try:
        r = requests.post(base + "/prompt", data=json.dumps(workflow).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"ComfyUI 不可达（{base}）：{e}")
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI /prompt 返回 {r.status_code}：{r.text[:300]}")
    res = r.json()
    if res.get("node_errors"):
        raise RuntimeError(f"工作流节点错误：{json.dumps(res['node_errors'], ensure_ascii=False)[:500]}")
    prompt_id = res.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 未返回 prompt_id：{str(res)[:300]}")
    task_queue.set_meta(task_id, {"prompt_id": prompt_id, "progress": "生成音频（语音设计）…"})

    hist = None
    for _ in range(_VOICE_POLL_MAX):
        if task_queue.is_cancelled(task_id):        # 取消：精准打断自己的 prompt + 不落库
            # ① 步4-①：收口到 task_queue.comfy_cancel_prompt（delete 优先 / 只打断自己的 / /history 校验）
            task_queue.comfy_cancel_prompt(prompt_id)
            raise task_queue.QueueCancelled("用户取消")
        try:
            hr = requests.get(f"{base}/history/{prompt_id}", timeout=10)
            if hr.status_code == 200:
                h = hr.json()
                if prompt_id in h:
                    hist = h[prompt_id]
                    break
        except (requests.RequestException, ValueError):
            pass
        _time.sleep(_VOICE_POLL_STEP)

    if hist is None:
        raise RuntimeError(f"音色样本生成超时（prompt_id={prompt_id}），请稍后在 ComfyUI 查看")
    status = (hist.get("status") or {}).get("status_str")
    if status != "success":
        msgs = (hist.get("status") or {}).get("messages") or []
        detail = "；".join(str(m[1].get("exception_message", ""))[:200]
                          for m in msgs if m and m[0] == "execution_error")
        raise RuntimeError(f"生成失败（{status}）：{detail or '未知错误'}")

    out = ((hist.get("outputs") or {}).get("3") or {}).get("audio") or []
    if not out:
        raise RuntimeError("工作流未产出音频（SaveAudio 节点 3 无 audio）")
    info = out[0]

    task_queue.set_meta(task_id, {"progress": "取回音频并入库…"})
    try:
        vr = requests.get(base + "/view", timeout=60, params={
            "filename": info.get("filename", ""), "subfolder": info.get("subfolder", ""),
            "type": info.get("type", "output")})
    except requests.RequestException as e:
        raise RuntimeError(f"拉取音频失败：{e}")
    if vr.status_code != 200:
        raise RuntimeError(f"ComfyUI /view 返回 {vr.status_code}")

    if task_queue.is_cancelled(task_id):            # 落库前最后一道守卫
        raise task_queue.QueueCancelled("用户取消")

    shared = Path(library.get_shared_assets_path())
    (shared / "audio" / "voice_samples").mkdir(parents=True, exist_ok=True)
    ts = library.now_iso().replace(":", "").replace("-", "")
    rel = f"audio/voice_samples/{asset_id}_{ts}.flac"
    (shared / rel).write_bytes(vr.content)

    entry = {"path": rel, "type": "reference", "role": "voice_sample", "voice": "",
             "added_at": library.now_iso()}
    # ★ 重新读一次资产再写：任务排队期间资产可能被别的操作改过（避免用旧快照整体覆盖）
    fresh = library.get_item(kind, asset_id) or item
    fresh.setdefault("audios", []).append(entry)
    fresh["updated_at"] = library.now_iso()
    library.save_item(kind, fresh)
    return {"path": rel, "size": len(vr.content), "asset_id": asset_id, "name": name, "audio": entry}


task_queue.register_job("voice", _voice_job)


# ══════════════════ SHARP 单图重建（3DGS）══════════════════

def _gsplat_job(task_id: str, task: dict):
    """队列执行体：调 comfy_client.generate_sharp（同步 1–3 分钟）→ 返回 ply 信息。

    ★ ③ F5b 步2c：ply 留在**实际执行节点**的 ComfyUI output —— 结果 url 由
    `ply_proxy.gsplat_ply_url(ply_name, node_id)` 构造，**带节点标识**（`?node=<id>`；
    local/空 = 不带参数旧行为），代理端按它回源 —— 多节点下不再固定去本机 404。"""
    p = GsplatGeneratePayload(**task_queue._meta_of(task).get("payload", {}))
    if not (p.ref_image or "").strip():
        raise RuntimeError("ref_image 必填")
    if task_queue.is_cancelled(task_id):
        raise task_queue.QueueCancelled("用户取消")
    task_queue.set_meta(task_id, {"progress": "SHARP 重建中（首次含模型加载 1–3 分钟）…"})
    res = comfy_client.generate_sharp(p.ref_image, p.focal_length_mm,
                                      base=task_queue.node_url_for(task_id, p.target_node))   # ★ ③F5b步1
    ply_name = res.get("ply_name") or ""
    # ★ ③ 步2c：meta.node 在 _run_task 派发时已写入（claim 快照里没有）→ 重新读 store 取派发节点
    _t = task_queue.task_store.get_task(task_id) or task
    node_id = str(task_queue._meta_of(_t).get("node") or "")
    return {"ply": ply_name, "url": ply_proxy.gsplat_ply_url(ply_name, node_id),
            "prompt_id": str(res.get("prompt_id") or ""), "ply_abs": res.get("ply_abs") or ""}


task_queue.register_job("gsplat", _gsplat_job)


# ══════════════════ 端点（入队：立即返回 task_id）══════════════════

@router.post("/api/voice-sample/generate")
def api_voice_sample_generate(payload: VoiceSamplePayload):
    """音色样本生成（**已入队**）：立即返回 `{task_id}`；结果在 `/api/tasks` 的 `meta.result`。
    返回契约与原同步版**逐字一致**（path/size/asset_id/name/audio），只是改从任务结果取。"""
    asset_id = (payload.asset_id or "").strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id 不能为空")
    kind = (payload.kind or "characters").strip()
    if kind not in library.KINDS:
        raise HTTPException(status_code=400, detail=f"未知资产类别：{kind}")
    if not (payload.instruct or "").strip():
        raise HTTPException(status_code=400, detail="音色描述（instruct）不能为空")
    if not library.get_item(kind, asset_id):
        raise HTTPException(status_code=400, detail=f"资产不存在：{kind}/{asset_id}")
    task_id = task_queue.submit("voice", "音色样本", payload.model_dump())
    return {"task_id": task_id}


@router.post("/api/gsplat/generate")
def api_gsplat_generate(payload: GsplatGeneratePayload):
    """SHARP 单图重建（**已入队**）：立即返回 `{task_id}`；结果在 `/api/tasks` 的 `meta.result`。"""
    if not (payload.ref_image or "").strip():
        raise HTTPException(status_code=400, detail="ref_image 必填")
    task_id = task_queue.submit("gsplat", "3D 重建（SHARP）", payload.model_dump())
    return {"task_id": task_id}


# =======================================================================
# ②步2遗留 · 统一编辑器生图收编：POST /api/comfy/generate（kind=image）
# 原同步直出 PNG bytes（阻塞 1-2 分钟）→ 异步任务：提交返 {task_id}，进度/取消/重试走 /api/tasks。
# 工作流表 / 域参数翻译 自 main.py 随迁（_slots_consistency 单一源自检经 main 顶部 import 回读）；
# _archive_gen_image 落盘助手随迁（SharedAssets/output/ + register_file 幂等登记，语义不变）。


# =======================================================================

# ═══════════════════════════════════════════════════════════════════════
# 阶段4（B-20260908-03 · IC 工作流工坊化）生图直连：POST /api/comfy/generate
# 依据《阶段4后端实施摸底清单-B-20260908-03-20260909.md》一/三 节点映射 + 方案 v1.5 四 后端规格。
# 直连 ComfyUI（绕 IC），复用 comfy_client 提交/轮询/回传；params 按节点 ID 覆盖（复刻 IC main.py:18233）。
# 说明：IC /api/ic/generate（阶段6 A 通道）待前端切走后标记废弃（见下方 deprecated 注释）。
# ═══════════════════════════════════════════════════════════════════════

# workflow_type → 工作流文件 + 便捷节点注入模板（文件在 workflows/custom/，模型名按远程 ComfyUI 现状勿改）
#   prompt_node/text_key  : 便捷 prompt 写入的文本节点；None=该类型无文本槽（如 upscale）
#   size_node             : 便捷 width/height 的尺寸节点（EmptyLatentImage）；None=尺寸固定不注入
#   seed_nodes            : 便捷随机种子注入的采样/噪声节点（KSampler.seed / RandomNoise.noise_seed）
#   seed_key              : 采样节点的种子字段名（默认 'seed'；Flux2 用 'noise_seed'）
#   refs                  : 参考图槽位 [{"img": LoadImage节点ID, "opt":是否可选}, ...]，按 ref_images 顺序填
_GEN_WORKFLOWS = {
    "z_image": {
        "file": "custom/Z-Image.json",
        "prompt_node": "23", "text_key": "text", "size_node": "144",
        "seed_nodes": ["22"], "seed_key": "seed", "refs": [],
    },
    "2511": {
        "file": "custom/2511.json",
        "prompt_node": "11", "text_key": "prompt", "size_node": None,
        "seed_nodes": ["14"], "seed_key": "seed", "refs": [{"img": "31"}],
    },
    "klein": {
        "file": "custom/Flux2-Klein.json",
        "prompt_node": "168", "text_key": "text", "size_node": None,
        "seed_nodes": ["158"], "seed_key": "noise_seed",
        "refs": [{"img": "278"}, {"img": "270", "opt": True}, {"img": "292", "opt": True}],
    },
    "enhance": {
        "file": "custom/Z-Image-Enhance.json",
        "prompt_node": "23", "text_key": "text", "size_node": None,
        "seed_nodes": ["146", "181"], "seed_key": "seed", "refs": [{"img": "15"}],
    },
    "upscale": {
        "file": "custom/upscale.json",
        "prompt_node": None, "text_key": None, "size_node": None,
        "seed_nodes": ["172"], "seed_key": "seed", "refs": [{"img": "15"}],
    },
    # ★ 用户 2026-10-01：Qwen-Image-2.1（18 的 ComfyUI 上的新版模型）。
    #   文生图走标准 CLIPTextEncode；图生图/编辑走 0.37 的 TextEncodeQwenImage21（多图槽）。
    "qwen21": {
        "file": "custom/qwen21.json",
        "prompt_node": "6", "text_key": "text", "size_node": "5",
        "seed_nodes": ["3"], "seed_key": "seed", "refs": [],
    },
    "qwen21_edit": {
        "file": "custom/qwen21_edit.json",
        "prompt_node": "30", "text_key": "prompt",     # ★ 该节点的提示词键叫 prompt（不是 text）
        "size_node": "5",
        "seed_nodes": ["3"], "seed_key": "seed",
        # ★ 10 图槽：首槽必填、其余可选。未填图的槽不连进 images ⇒ ComfyUI 不执行孤立节点，天然安全。
        "refs": [{"img": "20"}] + [{"img": str(20 + i), "opt": True} for i in range(1, 10)],
        # ★ 新增机制①：把所有"已填图"的槽汇聚成数组，写进 agg_node.agg_key
        #   （实测：API 格式里 autogrow 图槽必须传数组，不能按 image_1/image_2 拆分）
        "agg_node": "30", "agg_key": "images",
        # ★ 新增机制②：清晰度 —— TextEncodeQwenImage21.resolution（参考图缩放基准，1K=1024 / 2K=2048）
        "res_node": "30", "res_key": "resolution",
        # ★ 机制②d：resolution **同时**决定输出尺寸（否则产物恒为 latent 的 1024²，"选 2K 出 1K"）
        "res_also_size": True,
    },
}
_GEN_WF_DEFAULT = "z_image"
_GEN_WF_NAMES = list(_GEN_WORKFLOWS.keys())


# 阶段5-2：gentools 领域参数 → 节点覆盖翻译（供生图工具页 5 模式真调）。
#   gentools p.params 收集的是"领域参数"(angle_h/angle_v/distance/strength/target_res/…)，
#   与节点级覆盖(数字节点ID)不同源；此处把领域键按 workflow_type 翻译成 {节点ID:{字段:值}}，
#   与便捷层/节点级 params merge。数字节点ID 由调用方直接透传(不走此函数)。
#   映射基于 workflows/custom 各 json 的实测节点：2511→11.prompt(角度指令)/enhance→204.value/upscale→172.resolution。
def _translate_domain_params(wf_type: str, domain: dict, prompt_hint: str = "") -> dict:
    out: dict = {}
    if wf_type == "2511":
        parts = []
        for label, key, suffix in (("水平旋转", "angle_h", "°"), ("垂直旋转", "angle_v", "°")):
            if key in domain:
                try: v = float(domain[key])
                except Exception: v = None
                if v not in (None, 0):
                    parts.append(f"{label}{v:g}{suffix}")
        if "distance" in domain:
            try: d = float(domain["distance"])
            except Exception: d = None
            if d is not None and abs(d - 4.0) > 0.01:   # 默认距离4 视为未调，避免噪音
                parts.append(f"摄像机距离{d:g}")
        # 仅当未提供显式 prompt(文本框空)时用角度拼 11.prompt（IC 2511 语义：11 即角度指令文本节点）
        if parts and not prompt_hint:
            out.setdefault("11", {})["prompt"] = "，".join(parts) + "。"
    elif wf_type == "enhance":
        if "strength" in domain:
            try: out.setdefault("204", {})["value"] = float(domain["strength"])
            except Exception: pass
    elif wf_type == "upscale":
        if "target_res" in domain:
            try:
                r = int(float(domain["target_res"]))
                if r > 0: out.setdefault("172", {})["resolution"] = r
            except Exception: pass
    # klein: detail_lora/lora_strength 在 Flux2-Klein json 无对应 LoRA 节点 → 忽略（工作流无此能力，不误写）
    return out

class ComfyGeneratePayload(BaseModel):
    workflow_type: str = _GEN_WF_DEFAULT      # z_image/2511/klein/enhance/upscale/qwen21/qwen21_edit
    prompt: str = ""                          # 便捷 prompt（有文本槽类型时写入文本节点）
    ref_images: list = []                     # 便捷参考图（相对 SharedAssets 路径或本地绝对路径，按 refs 槽位顺序填）
    width: int = 0                            # 便捷尺寸（z_image 需传；0=不覆盖）
    height: int = 0
    resolution: int = 0                       # ★ 10-01：清晰度（TextEncodeQwenImage21.resolution，1024=1K / 2048=2K；0=不覆盖）
    seed: int | None = None                   # 便捷种子（None=随机）
    params: dict = {}                         # 节点级覆盖 params={节点ID:{字段:值}}，merge 覆盖便捷层（值 None 则 pop）
    client_id: str = "storyboard-studio"
    # ★ ④ 步4-④ / ②步2遗留：
    target_node: str = ""                     # 生成节点（F5b 预留；空=本机，经 resolve_node_url 唯一解析口）
    use_cloud: bool = False                   # ★ ④ F5c：手动勾选走云端（channel 由提交口统一判定落 payload）
    cloud_model: str = ""                     # ★ ④ F5c：本次使用的云端模型 id（空=按 kind 取设置默认）
    source_kind: str = ""                     # 触发资产类别（characters/scenes/props）——占位匹配/任务卡详情
    source_id: str = ""                       # 触发资产 id（未绑卡生成=空）


def _build_image_params(pl: dict):
    """payload dict → (wf_type, wf_file, bp 节点覆盖)。纯组装无 IO；
    400 级问题抛 ValueError（端点转 400，job 转任务失败）。组装逻辑与原 main.py 同步端点逐行等价
    （便捷层四步：槽位/prompt/尺寸/种子 + params merge + 领域键翻译）。"""
    wf_type = pl.get("workflow_type") or _GEN_WF_DEFAULT
    spec = _GEN_WORKFLOWS.get(wf_type)
    if not spec:
        raise ValueError(f"未知 workflow_type={wf_type!r}，可选：{', '.join(_GEN_WF_NAMES)}")
    refs = list(pl.get("ref_images") or [])
    bp: dict = {}
    # ① 参考图槽位分配（z_image 无槽 → 忽略多余 refs；必填槽缺失即拒）
    for i, slot in enumerate(spec["refs"]):
        if i < len(refs) and refs[i]:
            bp.setdefault(slot["img"], {})["image"] = refs[i]
        elif not slot.get("opt"):
            raise ValueError(f"workflow_type={wf_type} 需参考图 #{i+1}（ref_images 不足）")
    # ② prompt → 文本节点
    if pl.get("prompt") and spec.get("prompt_node"):
        bp.setdefault(spec["prompt_node"], {})[spec["text_key"]] = pl["prompt"]
    # ②b ★ 新增机制（Qwen-Image-2.1）：把"已填图"的槽汇聚成**数组**写进 agg_node.agg_key。
    #     为什么是数组：0.37 的 autogrow 图槽在 API 格式里必须整体传 list，
    #     按 image_1/image_2 拆开会被拒（实测报错 unexpected keyword argument）。
    if spec.get("agg_node") and spec.get("agg_key"):
        _arr = [[slot["img"], 0] for i, slot in enumerate(spec["refs"])
                if i < len(refs) and refs[i]]
        bp.setdefault(spec["agg_node"], {})[spec["agg_key"]] = _arr
    # ②c ★ 新增机制：清晰度（resolution，单值 INT）—— 目前用于 TextEncodeQwenImage21
    if spec.get("res_node") and pl.get("resolution"):
        bp.setdefault(spec["res_node"], {})[spec.get("res_key") or "resolution"] = int(pl["resolution"])
    # ②d ★ 2026-10-01 实测修复：清晰度**必须同时决定输出尺寸**。
    #     坑：`resolution` 只是 TextEncodeQwenImage21 的「参考图缩放基准」，**不改变 latent 尺寸** ——
    #     官方工作流的 EmptyLatentImage 也是写死 1024²。所以只透传 resolution 时，
    #     界面选"2K"、产物仍是 1024×1024（实测量化核对才发现，光看"成功"会误判）。
    #     ⇒ 标了 res_also_size 的工作流，把 resolution 同时灌进 size_node 的 width/height（方形）。
    if spec.get("res_also_size") and pl.get("resolution") and spec.get("size_node"):
        _r = max(256, int(pl["resolution"]))
        bp.setdefault(spec["size_node"], {})["width"] = _r
        bp.setdefault(spec["size_node"], {})["height"] = _r
    # ③ 尺寸（z_image）
    if spec.get("size_node") and (pl.get("width") or pl.get("height")):
        if pl.get("width"):
            bp.setdefault(spec["size_node"], {})["width"] = int(pl["width"])
        if pl.get("height"):
            bp.setdefault(spec["size_node"], {})["height"] = int(pl["height"])
    # ④ 种子（缺省随机；int32 正区间 —— 超大 seed 会被 prompt_outputs_failed_validation 拒绝）
    _seed = pl.get("seed")
    if spec["seed_nodes"]:
        for nid in spec["seed_nodes"]:
            if _seed is None:
                import random as _rnd
                _seed = _rnd.randint(0, 2**31 - 1)
            bp.setdefault(nid, {})[spec["seed_key"]] = _seed
    # merge 显式 params + 领域键翻译（gentools 生图工具页）
    _node_p: dict = {}
    _dom_p: dict = {}
    for _pk, _pv in (pl.get("params") or {}).items():
        if str(_pk).isdigit():
            _node_p[str(_pk)] = _pv
        else:
            _dom_p[str(_pk)] = _pv
    _dom_nodes = _translate_domain_params(wf_type, _dom_p, pl.get("prompt") or "")
    for _nid, _nv in _dom_nodes.items():
        bp.setdefault(str(_nid), {}).update(_nv)
    bp.update({str(k): v for k, v in _node_p.items()})
    return wf_type, spec["file"], bp


@router.post("/api/comfy/generate")
def api_comfy_generate(payload: ComfyGeneratePayload):
    """②步2遗留：统一编辑器生图（原同步直出 PNG）→ **异步任务**。
    契约变化：返回 {task_id}（原 {images,url,prompt_id,workflow_type}）——
    产物经 /api/tasks/{id} 的 meta.result 取（path/url/fname/prompt_id/workflow_type 字段保持）。
    组装期校验（未知 workflow_type / 参考图不足）在此 fail-fast → 400，不建脏任务。"""
    pl = payload.model_dump()
    # ★ ④ F5c：提交时定通道（手动开关优先；自动=本地积压≥阈值）。enhance/upscale 云端无对应 → 恒本地。
    try:
        pl["channel"] = ms_image.route_channel(bool(pl.get("use_cloud")), "image", payload.workflow_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        _build_image_params(pl)               # 组装期校验（job 内会再组装一次，纯函数无副作用）
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    label = {"z_image": "文生图", "2511": "参考图生图", "klein": "Flux 生图",
             "enhance": "图像增强", "upscale": "放大",
             "qwen21": "Qwen2.1 文生图", "qwen21_edit": "Qwen2.1 图像编辑"}.get(payload.workflow_type, payload.workflow_type)
    tid = task_queue.submit("image", label, pl)
    return {"task_id": tid}


def _comfy_image_job(task_id: str, task: dict):
    """生图队列执行体：组装 → 提交 → 可取消轮询 → 落盘 SharedAssets/output/。
    取消不落库（归档前自检）；prompt_id 尽早落 meta（统一 cancel 依赖）。"""
    pl = task_queue._meta_of(task).get("payload", {})
    if task_queue.is_cancelled(task_id):
        raise task_queue.QueueCancelled("用户取消")
    if pl.get("channel") == "modelscope":     # ★ ④ F5c：云端通道（独立于 ComfyUI 节点池）
        return _cloud_image_job(task_id, pl)
    comfy_base = task_queue.node_url_for(task_id, pl.get("target_node", ""))   # ★ ③F5b步1
    wf_type, wf_file, bp = _build_image_params(pl)
    task_queue.set_meta(task_id, {"progress": "提交 ComfyUI 生图任务…"})
    try:
        img_bytes, meta = comfy_client.generate_image(
            wf_file, params=bp,
            base=comfy_base,
            cancel_check=lambda: task_queue.is_cancelled(task_id),
            meta_cb=lambda m: task_queue.set_meta(task_id, m),
            progress_cb=lambda m: task_queue.set_meta(task_id, {"progress": m}))
    except comfy_client.ComfyCancelled:
        raise task_queue.QueueCancelled("用户取消")
    if task_queue.is_cancelled(task_id):      # 完成与取消竞态：宁可丢弃不落库
        raise task_queue.QueueCancelled("用户取消")
    task_queue.set_meta(task_id, {"progress": "落盘产物…"})
    rel = _archive_gen_image(img_bytes)
    return {"path": rel, "url": "/api/asset-file/" + rel,
            "fname": rel.split("/")[-1], "prompt_id": str(meta.get("prompt_id") or ""),
            "workflow_type": wf_type}


def _abs_ref(p: str) -> str:
    """参考图路径 → 绝对路径（相对 = SharedAssets 下；绝对/原样）。"""
    from pathlib import Path as _P
    s = str(p or "").strip()
    if not s:
        return s
    if _P(s).is_absolute():
        return s
    return str((_P(library.get_shared_assets_path()) / s).resolve())


def _cloud_image_job(task_id: str, pl: dict):
    """★ ④ F5c：云端生图执行体（channel=modelscope）。产物契约与本地完全一致。

    ★ 云端模型：优先用本次指定（`cloud_model`）；未指定时按有无参考图取设置默认。
    ★ 2026-10-01 实测修正：判"能不能吃参考图"**不能只看模型名含不含 edit** ——
      `Qwen/Qwen-Image-2.1` 名字不含 edit，但实测**能做多图编辑**
      （2 张参考图 + 指令 → 正确融合；27~34s / 1 额度；探针见 keel/script/verify_qwen21_cloud_edit.py）。
      故改为三态能力判断：both（文生图+编辑二合一）/ edit_only（仅编辑）/ t2i（仅文生图）。
      规则不变：**宁可报错，也不静默出"文不对题"的图**。
    """
    wf_type = pl.get("workflow_type") or ""
    cfg = ms_image.config()
    refs: list = []
    # ★ 2026-10-01：改为"只要带参考图就收"（原为仅 wf_type=="2511"）。
    #   否则 qwen21_edit 走云端时 ref_images 会被**静默丢弃**、悄悄降级成文生图 —— 违背"宁可报错不静默降级"。
    if pl.get("ref_images"):
        refs = [_abs_ref(p) for p in (pl.get("ref_images") or []) if p]
    model = (pl.get("cloud_model") or "").strip()
    if not model:
        model = cfg["model_i2i"] if refs else cfg["model_t2i"]
    _short = model.split("/")[-1]
    _low = model.lower()
    # 二合一模型（实测）：文生图与参考图编辑都能接
    _BOTH_CAPABLE = ("qwen-image-2.1", "qwen-image-2_1")
    if any(k in _low for k in _BOTH_CAPABLE):
        _cap = "both"
    elif "edit" in _low:
        _cap = "edit_only"
    else:
        _cap = "t2i"
    if refs and _cap == "t2i":
        raise RuntimeError("「%s」是文生图模型，不能做参考图生图 —— 请在平台里选支持参考图的模型" % _short)
    if (not refs) and _cap == "edit_only":
        raise RuntimeError("「%s」是图生图模型，需要参考图 —— 请改用文生图模型，或在编辑器里挂上参考图" % _short)

    def _cancelled():
        return task_queue.is_cancelled(task_id)

    try:
        img = ms_image.generate(pl.get("prompt") or "", model=model, ref_images=refs,
                                width=int(pl.get("width") or 0), height=int(pl.get("height") or 0),
                                cancel_check=_cancelled,
                                progress_cb=lambda m: task_queue.set_meta(task_id, {"progress": m, "pct": 60}))
    except ms_image.MsCancelled:
        raise task_queue.QueueCancelled("用户取消")
    if img[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("云端产物校验失败（非 PNG），请重试")
    ms_image.record_usage("i2i" if refs else "t2i")
    task_queue.set_meta(task_id, {"progress": "落盘产物…", "pct": 90})
    rel = _archive_gen_image(img)
    return {"path": rel, "url": "/api/asset-file/" + rel,
            "fname": rel.split("/")[-1], "prompt_id": "",
            "workflow_type": wf_type, "cloud": True}


def _archive_gen_image(img_bytes: bytes, prefix: str = "gen") -> str:
    """生图产物落盘 SharedAssets/output/{prefix}_{ts}_{id}.png，返回相对路径 'output/<fname>'。
    经 /api/asset-file 可达（root=SharedAssets）；register_file 幂等登记供版本索引/清理（best-effort）。
    注：assets 前端按 fname 绑资产 images，命名含时间戳+随机防撞。"""
    import time as _t
    d = Path(library.get_shared_assets_path()) / "output"
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{prefix}_{_t.strftime('%Y%m%d_%H%M%S')}_{library.new_id('')}.png"
    (d / fname).write_bytes(img_bytes)
    rel = f"output/{fname}"
    try:
        assets_index.register_file(rel)
    except Exception:
        pass
    return rel

task_queue.register_job("image", _comfy_image_job)
