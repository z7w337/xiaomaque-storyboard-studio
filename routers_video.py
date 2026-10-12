# -*- coding: utf-8 -*-
"""routers_video.py —— 场页生视频收编统一任务队列（批次② F5a · 步2遗留）

★ 本批要旨（任务单 ②-步2遗留-任务单-20260922.md）：
  - `/api/video/generate` 由**自带 VIDEO_TASKS + _video_worker 线程机制**收编进 `task_queue`
    （kind=video）——**退役第二套任务机制**：状态/取消/重试全部走 `/api/tasks`，前端不再自轮询。
  - `GET /api/video/task/{task_id}` 一并退役（前端改查 `GET /api/tasks/{task_id}`，契约：
    state=queued/running/done/fail/cancelled，进度在 meta.progress，产物在 meta.result）。
  - mp4 校验助手（_mp4_duration_seconds/_mp4_frame_count/_is_mp4_useful）自 main.py 随迁
    （motion job 经 main 顶部 import 继续使用）。
  - 视频类串行保险 = `task_queue.gen_serial(实际派发节点)`（★ 步2b附 裁定①：**按节点锁**，
    Y2 结论保留为「同节点内互斥」；跨节点可并行 —— 双机单部署显存物理隔离）。
  - 生成节点 = `task_queue.resolve_node_url(payload.target_node)`（④ 唯一解析口，F5b 前置）。
  - 取消链路与全站一致：统一 cancel 落 cancel_requested → job 经 generate_video 的
    cancel_check（1s 粒度）感知 → ComfyCancelled → QueueCancelled（不落库）；
    prompt_id 经 meta_cb 尽早落 meta → 统一 cancel 可精准 delete+interrupt。
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
import storage
import assets_index
import asset_validator
from dao import library

router = APIRouter(tags=["video"])


def _mp4_duration_seconds(data: bytes) -> float:
    """解析 mp4 的 mdhd 时长（秒）。解析失败返回 -1。"""
    import struct
    idx = data.find(b"mdhd")
    if idx < 0 or idx + 28 > len(data):
        return -1.0
    ver = data[idx + 8]
    ts = struct.unpack(">I", data[idx + 16:idx + 20])[0]
    if ver == 1:
        if idx + 32 > len(data):
            return -1.0
        dur = struct.unpack(">Q", data[idx + 20:idx + 28])[0]
    else:
        dur = struct.unpack(">I", data[idx + 20:idx + 24])[0]
    return (dur / ts) if ts else -1.0


def _mp4_frame_count(data: bytes) -> int:
    """解析 mp4 的 stsz 帧数。解析失败返回 0。"""
    import struct
    idx = data.find(b"stsz")
    if idx < 0 or idx + 16 > len(data):
        return 0
    return struct.unpack(">I", data[idx + 12:idx + 16])[0]


def _is_mp4_useful(data: bytes, expected_duration: float | None = None) -> bool:
    """校验视频产物是否可用（复检：缺 moov 的 mp4 浏览器无法解码/截帧）。
    要求含 ftyp 与 moov；mdat 尺寸不超文件总长（防截断）；
    0.2s 缺陷补强（2026-08-31）：时长/帧数校验——时长须 ≥ 请求时长合理下限且帧数 > 1，
    防止"首帧静帧/接近 0 时长"被当作成功产物放行。"""
    if not data:
        return False
    if data.find(b"moov") < 0 or data.find(b"ftyp") < 0:
        return False
    # 粗略：mdat box 声明尺寸不应大于文件总长（截断检测）
    import struct
    pos = 0
    n = len(data)
    while pos + 8 <= n:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        if size == 0:
            break
        if typ == b"mdat" and size > n:
            return False
        pos += size
        if size < 8:
            break
    # 时长/帧数校验（0.2s 缺陷补强）：时长须 ≥ max(1s, 请求时长×0.5) 且帧数 > 1
    dur = _mp4_duration_seconds(data)
    frames = _mp4_frame_count(data)
    if dur < 0 or frames <= 1:
        return False
    if expected_duration:
        floor = max(1.0, float(expected_duration) * 0.5)
        if dur < floor:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════
# 场视频生成（原阶段5，自 main.py 收编）：提交 → 统一队列（kind=video）→ 归档回填 Scene.videos
# ═══════════════════════════════════════════════════════════════════════

class VideoGeneratePayload(BaseModel):
    pid: str = ""
    eid: str = ""
    sid: str = ""
    workflow: str = "custom/minimax_h3_t8.json"   # Y2 注册的跑通版工作流
    prompt: str = ""                                # 组装好的视频提示词（4.4 规则）
    duration: float = 3.0                           # 时长（0.5~60s）
    aspect_ratio: str = "16:9 (Widescreen)"
    megapixels: str = "0.4"
    seed: int | None = None                         # None=IC 随机
    # v1.55 P0-6（契约②补排）：参考图/参考音频（6-10 直连注入工作流 363 节点；字段层本批、注入属业务层）
    ref_images: list[str] = []                      # 参考图相对路径（ASCII 名），按 @ 出场角色数动态剪枝（有几个传几个、不传空）
    ref_audios: list[dict] = []                     # 参考音频 [{path, subject_index}]；subject_index 与 @图片N/<Subject N>/(Sx)/ref_image_i/ref_audio_j 五处同源
    # ★ ④ 步4-④：生成节点（F5b 预留；空=本机，经 resolve_node_url 唯一解析口）
    target_node: str = ""


def _preflight_video_generate(payload: VideoGeneratePayload) -> None:
    """§十四 B-a 项② 生成前双保险：参考音频合规(2~15s/≥16kHz/文件存在) + 参考图存在 + 生成节点可达。
    不满足 → raise HTTPException(提示带具体不达标项)。fail-fast，不排队空跑（防 AudioVAE OOM 用超长音频再崩溃）。
    ★ ②步2遗留：可达性检查改走 resolve_node_url（④ 唯一解析口），不再直连 get_comfyui_url。"""
    import os
    shared_root = library.get_shared_assets_path()
    def _abs(p: str):
        p = str(p).replace("\\", "/")
        if p.startswith("http") or (len(p) > 1 and p[1] == ":"):
            return p
        return os.path.join(shared_root, p.lstrip("/")) if p else ""
    bad = []
    # 1) 参考音频合规（2~15s / ≥16kHz / 文件存在）
    for i, a in enumerate(payload.ref_audios or []):
        rel = (a.get("path") or "").strip()
        if not rel:
            bad.append(f"参考音频[{i}] 缺 path"); continue
        fp = Path(_abs(rel))
        if not fp.exists():
            bad.append(f"参考音频[{i}] 文件不存在：{rel}")
            continue
        v = asset_validator.validate_audio_file_upload(fp)
        if not v.get("ok"):
            bad.append(f"参考音频[{i}]：{v.get('error') or '不合规'}")
    # 2) 参考图存在
    for i, rel in enumerate(payload.ref_images or []):
        fp = Path(_abs(rel))
        if not fp.exists():
            bad.append(f"参考图[{i}] 文件不存在：{rel}")
    # 3) 生成节点可达（视频必走 ComfyUI，不可达生成必失败）
    #    ★ ③F5b步1：auto（""）= 池内任一可达即可（派发时才选节点，C10）；pinned → 必须该节点可达
    tn = (payload.target_node or "").strip()
    if tn:
        base = task_queue.resolve_node_url(tn)
        try:
            r = requests.get(f"{base}/system_stats", timeout=8)
            if r.status_code != 200:
                bad.append(f"远程 ComfyUI 不可用（/system_stats 返回 {r.status_code}）")
        except Exception:
            bad.append("生成节点 %s 不可达，请先确认该节点在线" % tn)
    else:
        import node_pool
        if not node_pool.any_reachable():
            bad.append("节点池内无任何可达节点（UU 端口映射未建立/未启动），请先确认 ComfyUI 在线或点🧹清显存后重试")
    if bad:
        raise HTTPException(status_code=400,
                            detail="生成前校验未通过，请先修正：\n• " + "\n• ".join(bad))


@router.post("/api/video/generate")
def api_video_generate(payload: VideoGeneratePayload):
    """提交场视频生成任务（★ ②步2遗留：改走统一队列，契约不变仍返 {task_id}）——
    查询/取消/重试统一走 /api/tasks（自带 VIDEO_TASKS 已退役）。归档 + 回填 Scene.videos 语义不变。"""
    proj = storage.load_project(payload.pid)
    sc = storage.find_scene(proj, payload.eid, payload.sid) if proj else None
    if not proj or not sc:
        raise HTTPException(status_code=404, detail="场不存在")
    # §十四 B-a 项② 生成前双保险（音频合规/图存在/节点可达，fail-fast）
    _preflight_video_generate(payload)
    tid = task_queue.submit("video", "场视频", payload.model_dump())
    return {"task_id": tid}


def _video_job(task_id: str, task: dict):
    """场视频队列执行体（原 _video_worker 收编）：串行保险（GEN_SERIAL，Y2）→ 直连 ComfyUI
    （失败即失败，不降级不静默 —— v1.59 用户裁定）→ 归档回填 Scene.videos。
    取消：cancel_check 1s 粒度感知（归档前再自检一道），不落库。"""
    p = VideoGeneratePayload(**task_queue._meta_of(task).get("payload", {}))
    # ★ 步2b附 裁定①：串行保险改**按节点锁**（与 motion 共用 `gen_serial`）——
    #   key = **实际派发节点**（与 meta.node_url 同源）；同节点内 video/motion 互斥，跨节点可并行。
    _node_key = task_queue.node_url_for(task_id, p.target_node)   # ★ ③F5b步1：job 唯一入口
    with task_queue.gen_serial(_node_key):
        if task_queue.is_cancelled(task_id):        # 排队期间就被取消
            raise task_queue.QueueCancelled("用户取消")
        gen_meta = {
            "prompt": p.prompt or "",
            "duration": float(p.duration or 3.0),
            "aspect_ratio": p.aspect_ratio or "16:9 (Widescreen)",
            "seed": p.seed if p.seed is not None else None,
        }
        comfy_base = _node_key
        try:
            video_bytes, meta = comfy_client.generate_video(
                p,
                progress_cb=lambda m: task_queue.set_meta(task_id, {"progress": m}),
                base=comfy_base,
                cancel_check=lambda: task_queue.is_cancelled(task_id),
                meta_cb=lambda m: task_queue.set_meta(task_id, m))
        except comfy_client.ComfyCancelled:
            raise task_queue.QueueCancelled("用户取消")
        gen_meta["rel_ref_images"] = meta.get("rel_ref_images") or list(p.ref_images or [])
        if task_queue.is_cancelled(task_id):        # 完成与取消竞态：宁可丢弃不落库
            raise task_queue.QueueCancelled("用户取消")
        task_queue.set_meta(task_id, {"progress": "归档（回填 Scene.videos）…"})
        return _archive_video(p, video_bytes, gen_meta)


def _archive_video(payload: VideoGeneratePayload, video_bytes: bytes, gen_meta: dict) -> dict:
    """校验 mp4 完整性 + 归档到 SharedAssets/video/{sid} + 回填 Scene.videos + 状态。"""
    import time as _t
    if not _is_mp4_useful(video_bytes, expected_duration=gen_meta.get("duration")):
        raise RuntimeError(
            f"视频产物校验失败（时长/帧数不足：请求{gen_meta.get('duration')}s，"
            "实际过短或仅首帧），请重试")
    proj = storage.load_project(payload.pid)
    sc = storage.find_scene(proj, payload.eid, payload.sid) if proj else None
    if not proj or not sc:
        raise RuntimeError("场不存在（可能已被删除）")
    video_dir = Path(library.get_shared_assets_path()) / "video" / payload.sid
    video_dir.mkdir(parents=True, exist_ok=True)
    fname = f"sc_{_t.strftime('%Y%m%d_%H%M%S')}_{library.new_id('')}.mp4"
    (video_dir / fname).write_bytes(video_bytes)
    rel_path = f"video/{payload.sid}/{fname}"
    videos = list(sc.get("videos") or [])
    for v in videos:
        v["enabled"] = False
    videos.append({
        "path": rel_path, "version": len(videos) + 1,
        "gen_prompt": gen_meta["prompt"], "duration": gen_meta["duration"],
        "aspect_ratio": gen_meta["aspect_ratio"], "seed": gen_meta["seed"],
        "created_at": storage.now_iso(), "enabled": True,
        "text": sc.get("text") or "",   # 生成该视频时的场文本快照（历史版本文本与视频 1对1）
    })
    sc["videos"] = videos
    sc["status"] = "已生成"
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    # B 批 N2：视频生成成功 → 给本次生成 @ 的参考图记 generation 引用（生成动作发生场 = payload 场）。
    # 参考图若为 assets_index 已登记的 in_use 图（尤其未分类/截帧图）→ 加保护，删归属实体后不失效；
    # 资产主图等已由 images[] 引用保护、此处幂等追加（同场只记一条）；未登记图 add 自动 skip(missing)。
    # 口径：comfy_client 零写入，generation +1 落 main.py 完成回调侧（第三方口径 2）。
    # N2 修复（§九）：优先用 comfy_client 回传的实际注入参考图相对路径（meta.rel_ref_images，覆盖后端派生
    #   @ 资产/<reference> 的参考图），回退 payload.ref_images（显式路径）。payload.ref_images 直连失败降级
    #   IC 时可能为空，但 meta.rel_ref_images 恒来自实际注入，保证派生参考图也能入账。
    rel_refs = (gen_meta or {}).get("rel_ref_images") or (payload.ref_images or [])
    gen_rel = assets_index.add_generation_refs(
        rel_refs, {"pid": payload.pid, "eid": payload.eid, "sid": payload.sid})
    # ★ ②步2遗留：不再写 t（VIDEO_TASKS 已退役）—— 返回 result dict，由统一队列终态一次写入
    return {"path": rel_path, "version": len(videos), "duration": gen_meta["duration"],
                   "aspect_ratio": gen_meta["aspect_ratio"], "seed": gen_meta["seed"],
                   "generation_ref": gen_rel, "rel_refs_logged": list(rel_refs)}

task_queue.register_job("video", _video_job)
