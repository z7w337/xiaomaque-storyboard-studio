# -*- coding: utf-8 -*-
"""剧本分镜工坊 — FastAPI 后端主程序（阶段 1：基础服务）。

启动入口：storyboard-studio\\run.bat（或 python main.py）
监听：127.0.0.1:3100（默认仅本机访问；局域网模式开关后续阶段实现）
"""
import json
import logging
import queue
import re
import sys
import threading
import zipfile
import io
from pathlib import Path

# 代码路径弱关联（《工程管理规范》第 7 节第 4 条）：
# 自身路径一律运行时推导，项目目录可随意移动/改名而不影响运行。
BASE_DIR = Path(__file__).resolve().parent
# 嵌入式 Python（python310._pth）不会自动把脚本目录加入 sys.path，
# 显式加入以支持 import storage / data_io 等本地模块。
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import asset_validator
import handoff
import library_audit

# 批2 DAO 抽象层（B-20260909-01，用户裁定 A 薄门面）：数据访问统一经 dao 门面，
# 不再直接 import storage/library/assets_index/data_io/draft_store/batch_queue 根模块。
# dao 将数据域子模块 + 原子写层原样 re-export 为同名属性，
# 故本文件后续所有 storage./library./assets_index./data_io./draft_store./batch_queue. 调用点零改动（同一函数对象，行为 100% 不变）。
from dao import storage, library, assets_index, draft_store, batch_queue, data_io

# 模块级日志器（批4 P0-a：apply_draft scope 回填失败由"静默 continue"改为记录错误日志，杜绝不可观测的静默失败）
_log = logging.getLogger("storyboard.main")

# 运行时目录（data 等不进 git，见 .gitignore；启动时自动确保存在）
RUNTIME_DIRS = [
    BASE_DIR / "data",
    BASE_DIR / "data" / "projects",
    BASE_DIR / "handoff" / "outbox",
]

for _d in RUNTIME_DIRS:
    _d.mkdir(parents=True, exist_ok=True)

# 存储层初始化（幂等：确保 data\projects.json 索引存在）
storage.init_storage()
library.init_library()

# 阶段 6 · 版本索引 + SharedAssets 后台监听（6-1/6-2；单写者/原子/.bak/启动校验恢复）
assets_index.start()


def _read_version() -> str:
    """读取 VERSION 文件（0a-6 创建）；文件缺失时回退 'dev'。"""
    try:
        v = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
        return v or "dev"
    except OSError:
        return "dev"


app = FastAPI(title="剧本分镜工坊", version=_read_version())

# 静态资源（前端页面；阶段 1 起 index.html 为工作台首页）
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.ico")


@app.get("/", response_class=HTMLResponse)
def index():
    """工作台首页（项目列表 + 进度看板，阶段 1）。"""
    # HTML 页面不缓存（开发期前端频繁变更，防浏览器缓存旧版；静态 JS/CSS 用版本参数 ?v= 强制刷新）
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/script.html", response_class=HTMLResponse)
def script_page():
    """剧本摘要页（v1.35 全项目唯一页面）。"""
    return FileResponse(BASE_DIR / "static" / "script.html", headers={"Cache-Control": "no-cache"})


@app.get("/scene.html", response_class=HTMLResponse)
def scene_page():
    """场页（v1.34 取代 storyboard.html：场=最小存储粒度；阶段 4 最小骨架，阶段 5 完整实现）。"""
    return FileResponse(BASE_DIR / "static" / "scene.html", headers={"Cache-Control": "no-cache"})


@app.get("/assets.html", response_class=HTMLResponse)
def assets_page():
    """角色库/场景库/道具库页（阶段 3）。"""
    return FileResponse(BASE_DIR / "static" / "assets.html", headers={"Cache-Control": "no-cache"})


@app.get("/settings.html", response_class=HTMLResponse)
def settings_page():
    """设置页（阶段 4 骨架，功能逐步填充）。"""
    return FileResponse(BASE_DIR / "static" / "settings.html", headers={"Cache-Control": "no-cache"})


@app.get("/gsplat.html", response_class=HTMLResponse)
def gsplat_page():
    """高斯泼溅 3D 页（IC工作流工坊化·阶段3 UI先行占位：图槽/生成/3D预览功能条[Scale 实时/重置/保存占位/复制占位]）。
    真实 SHARP→ply→3D 预览在阶段 4-5 接通。"""
    return FileResponse(BASE_DIR / "static" / "gsplat.html", headers={"Cache-Control": "no-cache"})


@app.get("/gentools.html", response_class=HTMLResponse)
def gentools_page():
    """生图工具独立导航页（IC工作流工坊化·阶段3 UI先行占位：5 模式 Z-Image/2511/Klein/Enhance/upscale 假参数面板，
    替代原 nav"画布"项；真实出图阶段 4 /api/comfy/generate 接入）。"""
    return FileResponse(BASE_DIR / "static" / "gentools.html", headers={"Cache-Control": "no-cache"})


@app.get("/style-ref.html", response_class=HTMLResponse)
def style_ref_page():
    """画风库页（批2 生产化：画风是项目属性；读 PALETTE 预置 8 风格 + PUT /api/projects/{pid} art_style）。
    实施9 修复：实施8 遗漏注册该页路由 → 404（用户反馈"画风库看不到"根因）。"""
    return FileResponse(BASE_DIR / "static" / "style-ref.html", headers={"Cache-Control": "no-cache"})


@app.get("/inspect.html", response_class=HTMLResponse)
def inspect_page():
    """目检清单页（机制二 v1.8：服务就绪→可点击链接→目检清单页[逐项确认按钮+每项意见文本框+统一提交]→结果回传留痕）。
    纯前端工具页：目检项经 URL 参数 (?title=..&items=项1;;项2..) 传入，提交结果存 localStorage+可复制汇总+toast。"""
    return FileResponse(BASE_DIR / "static" / "inspect.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"status": "ok", "module": "storyboard-studio", "version": _read_version()}


# ==================== 项目 CRUD API（阶段 1） ====================

class ProjectCreate(BaseModel):
    name: str = ""            # 可空：空=系统推导（前端传名），后端兜底"未命名项目-日期"
    art_style: str = ""
    aspect_ratio: str = "16:9"
    script: str = ""          # v1.33：新建项目必须导入剧本（粘贴即建），不允许空白项目


class ProjectUpdate(BaseModel):
    name: str | None = None
    art_style: str | None = None
    aspect_ratio: str | None = None
    script: str | None = None
    cover: str | None = None  # 封面资产 id（"恢复自动选择"=空串清除）


@app.get("/api/projects")
def api_list_projects():
    """项目列表（索引条目 + 进度统计 + 封面，供工作台首页仪表盘）。"""
    items = []
    for p in storage.list_projects():
        detail = storage.load_project(p["id"])
        progress = storage.count_scenes(detail) if detail else storage.count_scenes({})
        cover = _project_cover(detail) if detail else None
        items.append({**p, "progress": progress, "cover": cover,
                      "episodes_count": len(detail.get("episodes", [])) if detail else 0})
    return {"projects": items}


def _unique_project_name(name: str, exclude_id: str = "") -> str:
    """项目名唯一化（v1.41 用户要求：项目名不允许重复，同名自动加编号：原名(2)、(3)…）。"""
    existing = {p.get("name") for p in storage.list_projects() if p.get("id") != exclude_id}
    if name not in existing:
        return name
    i = 2
    while f"{name}({i})" in existing:
        i += 1
    return f"{name}({i})"


@app.post("/api/projects", status_code=201)
def api_create_project(payload: ProjectCreate):
    name = payload.name.strip()
    if not name:
        # v1.33 系统推导兜底（前端通常已推导；粘贴=首行标题、txt/docx=文件名、专家文件=文件名前缀或摘要首句）
        name = "未命名项目-" + storage.now_iso()[:10].replace("-", "")
    name = _unique_project_name(name)   # v1.41：同名自动加编号
    proj = storage.new_project(name, payload.art_style.strip(), payload.aspect_ratio.strip())
    script = (payload.script or "").strip()
    if script:
        proj["script"] = script
    storage.save_project(proj)
    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": proj["id"],
        "name": proj["name"],
        "created_at": proj["created_at"],
        "updated_at": proj["updated_at"],
    })
    storage.save_projects_index(idx)
    return proj


@app.get("/api/projects/{pid}")
def api_get_project(pid: str):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    return proj


@app.put("/api/projects/{pid}")
def api_update_project(pid: str, payload: ProjectUpdate):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
            proj[k] = v
    if data.get("cover") == "":      # "恢复自动选择"：清除 cover，下次列表时自动重算
        proj.pop("cover", None)
    if not proj.get("name"):
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    idx = storage.load_projects_index()
    for item in idx["projects"]:
        if item["id"] == pid:
            item["name"] = proj["name"]
            item["updated_at"] = proj["updated_at"]
    storage.save_projects_index(idx)
    return proj


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    # 删前先收集该项目下全部集/场 id —— 删除后需回扫资产 scope 摘除引用（防悬空，v1.16 U10 根因修复）
    proj = storage.load_project(pid)
    eids = [e.get("id") for e in (proj.get("episodes") or []) if e.get("id")] if proj else []
    sids = [s.get("id") for e in (proj.get("episodes") or []) for s in (e.get("scenes") or []) if s.get("id")] if proj else []
    # P2：删除前快照项目下全部场截帧图，删后释放未分类图引用
    all_ths = []
    for e in (proj.get("episodes") or []):
        if isinstance(e, dict):
            all_ths.extend(_episode_thumb_paths(e))
    if not storage.delete_project(pid):
        raise HTTPException(status_code=404, detail="项目不存在")
    idx = storage.load_projects_index()
    idx["projects"] = [p for p in idx["projects"] if p["id"] != pid]
    storage.save_projects_index(idx)
    pruned_p = library.prune_project_scopes(pid)
    pruned_e = library.prune_episode_scopes(tuple(eids)) if eids else {"pruned": 0}
    pruned_s = library.prune_scene_scopes(tuple(sids)) if sids else {"pruned": 0}
    # P2：删项目 → 其全部场截帧图失引用（仍被其它项目/资产引用则保留），刷新引用计数与 source_scene
    rel_refs = _release_refs_for_paths(all_ths)
    # B 批 N3：删项目 → 释放该项目全部 generation 引用（集=最小删除单位，项目=全集）
    gen_rel = assets_index.release_generation_refs_by_scope({"pid": pid})
    return {"deleted": pid,
            "scope_pruned": {"projects": pruned_p["pruned"], "episodes": pruned_e["pruned"], "scenes": pruned_s["pruned"]},
            "ref_released": rel_refs, "generation_released": gen_rel}


# ==================== 项目扩展 API（v1.33/v1.34：封面/详情/副本/下载剧本） ====================

_KIND_ORDER = {"characters": 0, "scenes": 1, "props": 2}


def _project_cover(proj: dict) -> dict | None:
    """封面解析（v1.34：Project.cover=封面资产 id，显示其当前有效图，换版本自动跟随）。
    自动选择：出场资产推导→过滤有图→出场场次数降序→角色>场景>道具→最近更新；选中写回 cover。"""
    if not proj:
        return None
    pid = proj.get("id")
    stats = api_ref_stats()

    def cover_info(asset, kind):
        imgs = asset.get("images") or []
        if not imgs:
            return None
        sc = stats.get(kind, {}).get(asset.get("id"), {}).get("scenes") or []
        scene_count = len([n for n in sc if n.get("pid") == pid])
        if scene_count == 0:
            return None
        return {"asset_id": asset.get("id"), "kind": kind,
                "name": library.display_name(asset), "path": imgs[-1].get("path"),
                "scene_count": scene_count}

    # 已有 cover（资产 id）且有效 → 直接返回
    cid = proj.get("cover")
    if cid:
        for kind in library.KINDS:
            asset = library.get_item(kind, cid)
            if asset:
                info = cover_info(asset, kind)
                if info:
                    return info
    # 自动选择并写回
    candidates = []
    for kind in library.KINDS:
        for asset in library.list_items(kind):
            info = cover_info(asset, kind)
            if info:
                candidates.append((info["scene_count"], -_KIND_ORDER[kind], asset.get("updated_at", ""), info))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    best = candidates[0][3]
    if best["asset_id"] != proj.get("cover"):
        proj["cover"] = best["asset_id"]
        storage.save_project(proj)
    return best


@app.get("/api/projects/{pid}/script/download")
def api_download_script(pid: str):
    """剧本 .txt 下载（v1.33：剧本页顶部工具栏"下载剧本"，不留查看入口）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    text = proj.get("script") or ""
    fname = re.sub(r'[\\/:*?"<>|]', "_", proj.get("name") or "剧本") + ".txt"
    from urllib.parse import quote
    from fastapi.responses import Response
    # 中文文件名：RFC 5987 编码（Starlette header 按 latin-1 编码，直接放中文会 UnicodeEncodeError）
    return Response(content=text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.post("/api/projects/{pid}/duplicate")
def api_duplicate_project(pid: str):
    """项目创建副本（v1.33 统一操作菜单：含结构/摘要/封面；imported_asset_ids 清空，不清草案）。"""
    import copy
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    new = copy.deepcopy(proj)
    new["id"] = storage.new_id("p_")
    new["name"] = proj.get("name", "") + "（副本）"
    new["created_at"] = storage.now_iso()
    new["updated_at"] = storage.now_iso()
    new["script"] = proj.get("script", "")
    new["summary"] = proj.get("summary", "")
    new["cover"] = proj.get("cover")
    new["imported_asset_ids"] = {"characters": [], "scenes": [], "props": []}
    for ep in new.get("episodes", []):
        ep["id"] = storage.new_id("ep_")
        for sc in ep.get("scenes", []):
            sc["id"] = storage.new_id("sc_")
    storage.save_project(new)
    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": new["id"], "name": new["name"],
        "created_at": new["created_at"], "updated_at": new["updated_at"],
    })
    storage.save_projects_index(idx)
    return new


@app.get("/api/projects/{pid}/detail")
def api_project_detail(pid: str):
    """查看详情轻量数据（v1.33：摘要 + 集数/资产数/进度统计，不新开重复页）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    stats = api_ref_stats()
    asset_count = 0
    for kind in library.KINDS:
        for aid, rec in stats.get(kind, {}).items():
            if any(n.get("pid") == pid for n in rec.get("scenes", [])):
                asset_count += 1
    return {
        "summary": proj.get("summary", ""),
        "episodes_count": len(proj.get("episodes", [])),
        "asset_count": asset_count,
        "progress": storage.count_scenes(proj),
        "cover": _project_cover(proj),
        "created_at": proj.get("created_at"),
        "updated_at": proj.get("updated_at"),
    }


@app.get("/api/projects/{pid}/cover-options")
def api_cover_options(pid: str):
    """修改封面弹窗的出场有图资产列表（v1.34 按出场场次数排序：降序→角色>场景>道具→最近更新）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    stats = api_ref_stats()
    options = []
    for kind in library.KINDS:
        for asset in library.list_items(kind):
            imgs = asset.get("images") or []
            if not imgs:
                continue
            sc = stats.get(kind, {}).get(asset.get("id"), {}).get("scenes") or []
            scene_count = len([n for n in sc if n.get("pid") == pid])
            if scene_count == 0:
                continue
            options.append({
                "asset_id": asset.get("id"), "kind": kind,
                "name": library.display_name(asset),
                "path": imgs[-1].get("path"), "scene_count": scene_count,
                "updated_at": asset.get("updated_at", ""),
            })
    options.sort(key=lambda o: (o["scene_count"], -_KIND_ORDER[o["kind"]], o["updated_at"]), reverse=True)
    return {"options": options}


@app.post("/api/projects/{pid}/episodes/{eid}/duplicate")
def api_duplicate_episode(pid: str, eid: str):
    """剧集创建副本（v1.33 统一操作菜单：含场文本/场图片/场视频，标题加"(副本)"）。"""
    import copy
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    if not proj or not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    new = copy.deepcopy(ep)
    new["id"] = storage.new_id("ep_")
    new["title"] = (ep.get("title") or "第 1 集") + "（副本）"
    for sc in new.get("scenes", []):
        sc["id"] = storage.new_id("sc_")
    proj["episodes"].append(new)
    _touch(proj)
    return new


# ==================== 剧本导入（阶段 2） ====================

def _extract_docx_text(raw: bytes) -> str:
    """从 docx（zip+xml）提取纯文本，仅用标准库（zipfile/re），无第三方依赖。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return ""
    lines = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        line = "".join(texts)
        line = re.sub(r"<[^>]+>", "", line)
        lines.append(line)
    return "\n".join(lines)


def _extract_pdf_text(raw: bytes) -> str:
    """从 PDF 提取文本（v1.35：标准库最小实现——zlib 解压内容流 + 正则抓取 Tj/TJ 文本）。
    对文本型 PDF 有效；扫描件/加密 PDF 返回空串（由调用方提示）。"""
    import zlib
    try:
        streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S)
    except Exception:
        return ""
    chunks = []
    for s in streams:
        try:
            data = zlib.decompress(s)
        except Exception:
            data = s  # 未压缩流直接尝试
        try:
            text = data.decode("latin-1", errors="replace")
        except Exception:
            continue
        # 提取 (…)Tj 与 […]TJ 中的文本（含转义）
        for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj|\[((?:[^\[\]\\]|\\.)*)\]\s*TJ", text):
            seg = m.group(1) or m.group(2) or ""
            parts = re.findall(r"\(((?:[^()\\]|\\.)*)\)", seg)
            if parts:
                seg = "".join(parts)
            seg = re.sub(r"\\([()\\])", r"\1", seg)
            seg = re.sub(r"\\([nrt])", lambda mm: {"n": "\n", "r": "\r", "t": "\t"}[mm.group(1)], seg)
            chunks.append(seg)
    return "\n".join(chunks)


def _looks_like_expert_json(content: str) -> bool:
    """判断文本是否为《视频专家导入格式约定》的专家 JSON（v1.35 按内容判断路径）。
    特征：可解析为 JSON 且顶层含 script_text/script_summary/episodes/assets 中 ≥2 个键。"""
    if not content or not content.strip().startswith(("{", "[")):
        return False
    try:
        data = _parse_import_json(content)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    keys = {"script_text", "script_summary", "episodes", "assets"}
    hit = sum(1 for k in keys if k in data)
    return hit >= 2


@app.post("/api/projects/{pid}/import-docx")
async def api_import_docx(pid: str, file: UploadFile = File(...)):
    """上传 docx 文件，提取文本并保存为项目剧本。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    raw = await file.read()
    text = _extract_docx_text(raw).strip()
    if not text:
        raise HTTPException(status_code=400, detail="未从 docx 提取到文本（文件可能损坏或为空）")
    proj["script"] = text
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    return {"script": text, "length": len(text)}


# ==================== 集/场/镜 CRUD API（阶段 2） ====================

def _touch(proj: dict) -> None:
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)


class EpisodeCreate(BaseModel):
    title: str = ""
    summary: str | None = None   # 本集剧情摘要（v1.31：可编辑 + AI 润色）


class SceneCreate(BaseModel):
    title: str = ""
    text: str | None = None          # 场文本（v1.34：镜1/镜2 纯文本段，含尖括号标记）
    images: list | None = None       # 参考图/生成图[]（可选）
    videos: list | None = None       # 场级视频[]（多版本/历史/切换启用）
    status: str | None = None        # 场状态：待生成/已生成/已确认


# --- 排序（集/场）注意：必须在 {eid}/{sid} 路由之前声明，避免 /order 被当作 id 匹配 ---

class OrderPayload(BaseModel):
    ids: list[str]


@app.put("/api/projects/{pid}/episodes/order")
def api_order_episodes(pid: str, payload: OrderPayload):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    storage.reorder_items(proj["episodes"], payload.ids)
    _touch(proj)
    return {"ok": True}


@app.put("/api/projects/{pid}/episodes/{eid}/scenes/order")
def api_order_scenes(pid: str, eid: str, payload: OrderPayload):
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    if not proj or not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    storage.reorder_items(ep["scenes"], payload.ids)
    _touch(proj)
    return {"ok": True}


# --- 集 Episode ---

@app.post("/api/projects/{pid}/episodes", status_code=201)
def api_create_episode(pid: str, payload: EpisodeCreate):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    ep = storage.new_episode(proj, payload.title.strip())
    proj["episodes"].append(ep)
    _touch(proj)
    return ep


@app.put("/api/projects/{pid}/episodes/{eid}")
def api_update_episode(pid: str, eid: str, payload: EpisodeCreate):
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    if not proj or not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    if payload.title.strip():
        ep["title"] = payload.title.strip()
    if payload.summary is not None:
        ep["summary"] = payload.summary.strip()
    _touch(proj)
    return ep


@app.delete("/api/projects/{pid}/episodes/{eid}")
def api_delete_episode(pid: str, eid: str):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    # 删前收集该集下全部场 id —— 删后需回扫资产 scope 摘除该集及场的引用（防悬空，v1.16 U10 根因修复）
    ep = next((e for e in (proj.get("episodes") or []) if e.get("id") == eid), None)
    sids = [s.get("id") for s in (ep.get("scenes") or []) if s.get("id")] if ep else []
    old_ths = _episode_thumb_paths(ep)   # P2：删除前快照该集全部场截帧图，删后释放未分类图引用
    before = len(proj["episodes"])
    proj["episodes"] = [e for e in proj["episodes"] if e.get("id") != eid]
    if len(proj["episodes"]) == before:
        raise HTTPException(status_code=404, detail="集不存在")
    _touch(proj)
    pruned_e = library.prune_episode_scopes(eid)
    pruned_s = library.prune_scene_scopes(tuple(sids)) if sids else {"pruned": 0}
    # P2：删集 → 其全部场截帧图失引用（仍被其它资产/场引用则保留），刷新引用计数与 source_scene
    rel_refs = _release_refs_for_paths(old_ths)
    # B 批 N3：删集 → 按集聚合释放该集全部场作为"生成动作发生地"的 generation 引用（集=最小删除单位）
    gen_rel = assets_index.release_generation_refs_by_scope({"pid": pid, "eid": eid})
    return {"deleted": eid, "scope_pruned": {"episodes": pruned_e["pruned"], "scenes": pruned_s["pruned"]},
            "ref_released": rel_refs, "generation_released": gen_rel}


# --- 场 Scene ---

@app.post("/api/projects/{pid}/episodes/{eid}/scenes", status_code=201)
def api_create_scene(pid: str, eid: str, payload: SceneCreate):
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    if not proj or not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    sc = storage.new_scene(ep, payload.title.strip())
    ep["scenes"].append(sc)
    _touch(proj)
    return sc


def _scene_thumb_paths(scene: dict) -> list:
    """收集某场 videos[].thumbnail 相对 SharedAssets path（第6批步骤②/⑤：场视频截帧引用刷新）。"""
    if not scene:
        return []
    out = []
    for v in scene.get("videos", []) or []:
        t = v.get("thumbnail") if isinstance(v, dict) else None
        if t:
            out.append(str(t))
    return out


def _episode_thumb_paths(ep: dict) -> list:
    """收集某集下所有场 videos[].thumbnail（P2：删集时释放该集全部场截帧引用）。"""
    out = []
    for sc in (ep.get("scenes") or []):
        if isinstance(sc, dict):
            out.extend(_scene_thumb_paths(sc))
    return out


def _release_refs_for_paths(paths: list) -> dict:
    """对一组图 path 做引用计数局部对账（删集/删项目后其截帧图失引用，仍被其它引用的保留）。
    全量 collect 为权威，只刷指定 path。"""
    try:
        return assets_index.refresh_specific_refs(
            list(dict.fromkeys(p for p in (paths or []) if p)),
            storage.load_projects_index, storage.load_project)
    except Exception:
        return {"fixed": 0, "unchanged": 0, "changed_paths": [], "log": []}


def _sync_scene_refs(pid: str, eid: str, sid: str, extra_paths: list = ()) -> dict:
    """第6批步骤②/⑤：场 videos[].thumbnail 变更/删除后，对该场涉及的截帧图刷新引用计数与 source_scene。
    局部兜底（以当前全量 scan 为准）。"""
    try:
        proj = storage.load_project(pid)
        sc = storage.find_scene(proj, eid, sid) if proj else None
        cur = _scene_thumb_paths(sc)
        return assets_index.refresh_specific_refs(
            list(dict.fromkeys(list(cur) + list(extra_paths))),
            storage.load_projects_index, storage.load_project)
    except Exception:
        return {"fixed": 0, "unchanged": 0, "changed_paths": [], "log": []}


@app.put("/api/projects/{pid}/episodes/{eid}/scenes/{sid}")
def api_update_scene(pid: str, eid: str, sid: str, payload: SceneCreate):
    proj = storage.load_project(pid)
    sc = storage.find_scene(proj, eid, sid) if proj else None
    if not proj or not sc:
        raise HTTPException(status_code=404, detail="场不存在")
    old_ths = _scene_thumb_paths(sc)   # 第6批：保存前快照旧 thumbnail，diff 刷新截帧引用
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
                if k == "text":
                    v = _fix_mojibake(v)   # 阶段5复检：防场文本被双重编码写坏
            sc[k] = v
    if not sc.get("title"):
        sc["title"] = "场景"
    _touch(proj)
    # 第6批步骤②/⑤：截帧 thumbnail 变更 → 刷新受影响截帧图的引用计数与 source_scene 归属
    _sync_scene_refs(pid, eid, sid, old_ths)
    return sc


@app.delete("/api/projects/{pid}/episodes/{eid}/scenes/{sid}")
def api_delete_scene(pid: str, eid: str, sid: str):
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    sc = storage.find_scene(proj, eid, sid) if (proj and ep) else None
    if not proj or not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    old_ths = _scene_thumb_paths(sc)   # 第6批：删除前快照该场截帧图，删后释放引用
    before = len(ep["scenes"])
    ep["scenes"] = [s for s in ep["scenes"] if s.get("id") != sid]
    if len(ep["scenes"]) == before:
        raise HTTPException(status_code=404, detail="场不存在")
    _touch(proj)
    # 删后回扫资产 scope 摘除该场引用（防悬空，v1.16 U10 根因修复）
    pruned = library.prune_scene_scopes(sid)
    # 第6批步骤②/⑤：删场 → 其截帧图失引用（仍被其它资产/场引用则不清零），刷新引用计数与 source_scene
    _sync_scene_refs(pid, eid, sid, old_ths)
    # B 批 N3：删场 → 释放以该场为"生成动作发生地"的 generation 引用（该场生成的视频@ 的参考图保护解除）
    gen_rel = assets_index.release_generation_refs_by_scope({"pid": pid, "eid": eid, "sid": sid})
    return {"deleted": sid, "scope_pruned": pruned["pruned"], "generation_released": gen_rel}


# ==================== 资产/统计 API（固定前缀，必须在 /api/{kind} 通配之前声明） ====================

# LLM 预置模板（v1.14：3 个可选模板，不内置 Key；key_url=获取 Key 页面，参考 IC 实现）
# 模型名须为 Modelscope OpenAI 兼容端点可识别的完整 model id（含组织前缀 Qwen/，2026-08-22 实测：
# "Qwen3-235B-A22B" 报 Invalid model id 400；"Qwen/Qwen3-235B-A22B" 返回 200 可用）
LLM_TEMPLATES = {
    "qwen": {"name": "Modelscope Qwen", "base_url": "https://api-inference.modelscope.cn/v1",
             "model": "Qwen/Qwen3-235B-A22B", "api_key": "", "temperature": 0.7, "price_per_1k": 0.0,
             "key_url": "https://www.modelscope.cn/my/access/token"},
    # v1.51：小模型默认档（降延迟；235B 保留可选高质量档；30B-A3B 实测可用）
    "qwen_small": {"name": "Modelscope Qwen（小·快）", "base_url": "https://api-inference.modelscope.cn/v1",
                   "model": "Qwen/Qwen3-30B-A3B", "api_key": "", "temperature": 0.7, "price_per_1k": 0.0,
                   "key_url": "https://www.modelscope.cn/my/access/token"},
    "openai": {"name": "通用 OpenAI 兼容", "base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini", "api_key": "", "temperature": 0.7, "price_per_1k": 0.15,
               "key_url": "https://platform.openai.com/api-keys"},
    "blank": {"name": "自定义模型", "base_url": "", "model": "", "api_key": "",
              "temperature": 0.7, "price_per_1k": 0.0, "key_url": ""},
}


class SettingsPayload(BaseModel):
    llm_models: list | None = None
    ic_path: str | None = None
    listen: str | None = None
    protect_copy: bool | None = None
    analyze_threshold: int | None = None   # v1.37：自适应拆解阈值（字，默认 8000）
    comfyui_url: str | None = None   # 收尾A②：ComfyUI 直连地址（设置页可配置；不再写死内网 IP）


@app.get("/api/settings")
def api_get_settings():
    return library.load_settings()


@app.put("/api/settings")
def api_save_settings(payload: SettingsPayload):
    data = payload.model_dump(exclude_unset=True)
    return library.save_settings(data)


class TemplatePayload(BaseModel):
    template: str = "blank"


@app.post("/api/settings/apply-template")
def api_apply_template(payload: TemplatePayload):
    """填充 LLM 预置模板（不覆盖已有条目，追加一个新模型行）。"""
    tpl = LLM_TEMPLATES.get(payload.template)
    if not tpl:
        raise HTTPException(status_code=400, detail="模板不存在")
    cfg = library.load_settings()
    models = list(cfg.get("llm_models") or [])
    models.append({
        "id": library.new_id("m_"),
        **{k: v for k, v in tpl.items()},
        "is_default": len(models) == 0,
    })
    return library.save_settings({"llm_models": models})

@app.get("/api/tts/voices")
def api_tts_voices():
    return {"voices": [{"id": v, "label": l} for v, l in library.TTS_VOICES]}


class TtsPlayPayload(BaseModel):
    voice: str = "zh-CN-XiaoxiaoNeural"
    text: str = ""


@app.post("/api/tts/play")
async def api_tts_play(payload: TtsPlayPayload):
    """独立试听：不依赖资产，用给定文本+音色生成试听 mp3 并返回路径（前端直接播放）。"""
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="无可朗读文本（请填写音色文本）")
    voice = payload.voice or "zh-CN-XiaoxiaoNeural"
    try:
        import edge_tts
    except ImportError:
        raise HTTPException(status_code=500, detail="edge-tts 未安装")
    audio_dir = Path(library.get_shared_assets_path()) / "audio" / "tmp"
    audio_dir.mkdir(parents=True, exist_ok=True)
    fname = f"trial_{library.new_id()}.mp3"
    out = audio_dir / fname
    try:
        comm = edge_tts.Communicate(text, voice)
        await comm.save(str(out))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听生成失败（需联网）：{e}")
    return {"path": f"audio/tmp/{fname}"}


@app.post("/api/tmp-image-upload")
async def api_tmp_image_upload(file: UploadFile = File(...)):
    """图片上传（提示词编辑器"本地上传参考图" + 资产元数据上传图）：存 SharedAssets/uploads/（被监听→可进未分类），返回相对路径。

    v1.6 改：由 drama/tmp 改为 uploads/（用户裁定：上传图应进被监听的上传目录，可进未分类/保留归类）。
    文件名保留原名 + 短 id 前缀防重名（便于未分类识别），扩展名沿用校验过的文件后缀。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    d = Path(library.get_shared_assets_path()) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    orig = Path(file.filename or "img").name or "img"
    safe = "".join(ch for ch in orig if ch not in '\\/:*?"<>|').strip() or "img"
    stem = Path(safe).stem or "img"
    fname = f"{library.new_id()[:8]}_{stem}{ext}"
    (d / fname).write_bytes(raw)
    return {"path": f"uploads/{fname}"}


# ==================== 参考引用类资产（计划书 v1.53，kind=reference） ====================
_REF_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


@app.post("/api/ref-upload")
async def api_ref_upload(file: UploadFile = File(...)):
    """参考图上传（提示词编辑器"＋→⬆ 本地上传参考图"）：存 SharedAssets/refs/，登记为参考类资产（kind=reference）。

    不进"未分类"（unclassified_entries 已排除 refs/）；由资产库"参考类"标签维护；可被引用/复用。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    if ext not in _REF_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    d = Path(library.get_shared_assets_path()) / "refs"
    d.mkdir(parents=True, exist_ok=True)
    orig = Path(file.filename or "img").name or "img"
    safe = "".join(ch for ch in orig if ch not in '\\/:*?"<>|').strip() or "img"
    stem = Path(safe).stem or "img"
    fname = f"{library.new_id()[:8]}_{stem}{ext}"
    (d / fname).write_bytes(raw)
    rel = f"refs/{fname}"
    # 3b修复(2026-09-09): new_reference 首参=prefix、次参=path。此前单参(rel)会把路径错塞 prefix、
    #   path/images 留空 → 建空卡(无主图)。改为传 path, prefix 由文件名派生 → 建带图素材卡(落地即资产正确化)。
    ref = library.new_reference("", rel)
    try:
        assets_index.register_file(rel)   # 幂等登记入版本索引（供复用/清理查询）
    except Exception:
        pass
    return {"path": rel, "ref_id": ref["id"]}


@app.get("/api/references")
def api_list_references():
    items = library.list_references()
    # P1（2026-09-04）：为每条 reference 合并 assets_index 对主图物理文件的引用（场/资产引用），
    #   供前端卡判定"可删"用——reference.ref_count 只算资产生成引用，落地自截帧图的主图可能还被场引用。
    enriched = []
    for r in items:
        rel = (library.ref_primary_path(r) or r.get("path") or "").lstrip("/")
        merged = dict(r)
        try:
            idx_ref = assets_index.entry_ref_of(rel) if rel else {}
            merged["_idx_ref_count"] = int(idx_ref.get("ref_count") or 0)
            merged["_idx_ref_sources"] = idx_ref.get("ref_sources") or []
        except Exception:
            merged["_idx_ref_count"] = 0
            merged["_idx_ref_sources"] = []
        enriched.append(merged)
    return {"items": enriched}


@app.get("/api/references/{ref_id}")
def api_get_reference(ref_id: str):
    """参考类资产单条 GET（openMeta 打开编辑用）：不进 _valid_kind，独立维护避免与三库 KINDS 混写。"""
    ref = library.get_reference(ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="参考类资产不存在")
    return ref


@app.delete("/api/references/{ref_id}")
def api_delete_reference(ref_id: str):
    ref = library.get_reference(ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="参考类资产不存在")
    # P1（第三方审计·2026-09-04）：区分两层引用语义——
    #   ①reference.ref_count（资产生成引用，inc_reference_source）：>0 则 reference 记录本身仍被用 → 拦删除记录；
    #   ②assets_index 对主图物理文件的引用（场 videos[].thumbnail / 主三库资产 images[]）：
    #     仅表示"物理图还被场/资产用"，与 reference 记录是两个独立事实。
    #     → 物理图是否连删须查 assets_index：若物理图仍被场/资产引用，只删 reference 记录、保留物理图
    #       （避免删落地自截帧图的 reference 时误删仍在用的物理图、破坏场缩略图）。
    ref_cnt = ref.get("ref_count") or 0
    rel = (library.ref_primary_path(ref) or ref.get("path") or "").lstrip("/")
    idx_ref = {}
    if rel:
        try:
            idx_ref = assets_index.entry_ref_of(rel)
        except Exception:
            idx_ref = {}
    idx_cnt = int(idx_ref.get("ref_count") or 0)
    # ①记录引用拦截：仅 reference.ref_count（资产生成引用）>0 时不可删记录
    if ref_cnt > 0:
        srcs = list(ref.get("ref_sources") or [])
        detail = f"该参考素材仍被 {ref_cnt} 个资产生成引用，不能删除"
        if srcs:
            detail += "（引用来源：" + "、".join(str(x) for x in srcs) + "）"
        raise HTTPException(status_code=409, detail=detail)
    # ②物理图是否连删：assets_index.ref_count=0（不再被场/资产/其它引用）才删物理文件
    physical_deleted = False
    if rel and idx_cnt == 0:
        root = Path(library.get_shared_assets_path()).resolve()
        full = (root / rel).resolve()
        if str(full).startswith(str(root)) and full.is_file():
            try:
                full.unlink()
                physical_deleted = True
            except OSError:
                pass
        # 物理图删除后：assets_index 中该 entry 标记 deleted（若仍在 in_use）
        try:
            assets_index.mark_deleted(rel)
        except Exception:
            pass
    # 始终删除 reference 记录（记录本身不再被资产生成引用即可删；物理图被场/资产引用时仅脱壳，图保留）
    library.delete_reference(ref_id)
    return {"ok": True, "record_deleted": True,
            "physical_deleted": physical_deleted,
            "physical_kept_for_refs": (idx_cnt > 0)}


class RefUsePayload(BaseModel):
    paths: list[str] = []
    asset_id: str = ""


@app.post("/api/references/use")
def api_references_use(payload: RefUsePayload):
    """资产生成用到参考类资产 → 引用计数+1、引用源[]记所引用资产ID（幂等不重复计）。"""
    for p in (payload.paths or []):
        if not p:
            continue
        ref = library.find_reference_by_path(p)
        if ref:
            library.inc_reference_source(ref["id"], payload.asset_id)
    return {"ok": True}


@app.get("/api/references/by-asset/{asset_id}")
def api_references_by_asset(asset_id: str):
    """自动复用：返回该资产用过的参考类资产（引用源[] 含该资产ID），按更新倒序。"""
    items = [re for re in library.list_references()
             if asset_id and asset_id in (re.get("ref_sources") or [])]
    items.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    return {"items": items}


@app.post("/api/tmp-audio-upload")
async def api_tmp_audio_upload(file: UploadFile = File(...)):
    """临时参考音频上传（不依赖资产）：存 SharedAssets\audio\tmp\，保存资产时引用。
    §十四 B-a 项③：接入 asset_validator 单文件合规校验（时长 2~15s/采样率≥16kHz/大小/格式）。
    合规 → {path,warn}；不合规 → 保留 tmp 文件供前端"试听+×删除"处置（B4 用户走查 09-06）：
      返回 {ok:false, path, error}（HTTP 200，不 400 拒——让用户能听到/删掉/重传这条音频；
      文件保留在 audio/tmp 但不作为卡音源入库，防超长音频进 H3 OOM 的安全底线仍在）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    ext = Path(file.filename or "ref.mp3").suffix.lower() or ".mp3"
    d = Path(library.get_shared_assets_path()) / "audio" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    fname = f"ref_{library.new_id()}{ext}"
    fp = d / fname
    fp.write_bytes(raw)
    v = asset_validator.validate_audio_file_upload(fp)
    rel = f"audio/tmp/{fname}"
    if not v.get("ok"):
        # 不合规：保留文件，让前端能试听辨别/删除重传（用户自行处置，勿自动删）
        return {"ok": False, "path": rel, "error": v.get("error") or "音频不合规", "warn": v.get("warn") or ""}
    return {"path": rel, "warn": v.get("warn") or ""}


@app.delete("/api/tmp-audio")
def api_tmp_audio_delete(path: str = Query("", description="相对 shared 根的 audio/tmp/xxx 路径")):
    """删除一段临时参考音频（audio/tmp 下，前端处置不合规/未采用音频时调用）。只允许删 tmp 目录内文件，防路径穿越。"""
    rel = (path or "").replace("\\", "/")
    if not rel.startswith("audio/tmp/"):
        raise HTTPException(status_code=400, detail="仅允许删除 audio/tmp 下的临时音频")
    root = Path(library.get_shared_assets_path())
    fp = root / rel
    # 防穿越：解析后必须仍在 audio/tmp 内
    try:
        resolved = fp.resolve()
        tmp_root = (root / "audio" / "tmp").resolve()
    except Exception:
        resolved = fp; tmp_root = (root / "audio" / "tmp")
    if not str(resolved).startswith(str(tmp_root)):
        raise HTTPException(status_code=400, detail="非法路径")
    if fp.exists():
        fp.unlink()
        return {"ok": True, "deleted": rel}
    return {"ok": True, "deleted": rel, "note": "文件已不存在"}


@app.get("/api/audio/list")
def api_audio_list():
    """共享资产库音频清单（相对路径，供参考音频绑定；复用版本索引阶段 6）。"""
    root = Path(library.get_shared_assets_path())
    files = []
    for ext in (".mp3", ".wav", ".ogg", ".m4a", ".flac"):
        for p in root.rglob(f"*{ext}"):
            if p.is_file():
                files.append(str(p.relative_to(root)).replace("\\", "/"))
    return {"audios": sorted(files)}


@app.get("/api/asset-file/{path:path}")
def api_asset_file(path: str):
    """读取共享资产库中的文件（缩略图/预览用）。外部路径来自配置注入（弱关联）。"""
    root = Path(library.get_shared_assets_path()).resolve()
    full = (root / path).resolve()
    # 收尾B-07⑤(2026-09-11): 越界校验改 relative_to(按组件边界, 防 str.startswith 前缀绕过如 SharedAssets_evil)，与 A⑦ materialize 同款
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(full)


@app.get("/api/assets/list")
def api_assets_list():
    """共享资产库图片清单（相对路径，供挂图选择；版本登记阶段 6）。"""
    root = Path(library.get_shared_assets_path())
    images = []
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        for p in root.rglob(f"*{ext}"):
            if p.is_file():
                images.append(str(p.relative_to(root)).replace("\\", "/"))
    return {"images": sorted(images)}


# ---- 阶段 6 · 版本索引查询（6-1/6-2；只读，供 6-8 资产库总览页）----

@app.get("/api/assets/index/summary")
def api_assets_index_summary():
    """版本索引概览（总数/各状态/各来源/保护副本开关/索引恢复警告）。"""
    return assets_index.get_summary()


@app.get("/api/assets/index/entries")
def api_assets_index_entries(status: str | None = None, source: str | None = None,
                             limit: int = 200):
    """版本索引条目列表（可按状态/来源过滤，最近登记在前）。"""
    return {"entries": assets_index.list_entries(status=status, source=source, limit=limit)}


@app.get("/api/assets/index/unassigned")
def api_assets_index_unassigned():
    """未分配区条目（物理位于 SharedAssets\\unassigned\\ 的在用媒体）。"""
    entries = assets_index.list_entries(status="in_use", source="unassigned", limit=500)
    return {"entries": entries}


@app.get("/api/assets/index/unclassified")
def api_assets_index_unclassified():
    """未分类/未分配图：索引中 in_use 但未被任何资产（角色/场景/道具）images 引用的媒体（用户裁定 A：未归属的图都显示）。
    第6批：每条附 ref_count/ref_sources/source_scene，供前端展示引用次数与截帧归属。"""
    return {"entries": assets_index.unclassified_entries(limit=500)}


@app.delete("/api/assets/unclassified")
def api_delete_unclassified(path: str = ""):
    """删除未分类图片（第6批 item1/3 核心：引用计数拦截）。

    读索引计数器（不需扫描）：ref_count>0 → 409 拒删并返回引用来源 ref_sources；
    ref_count=0 → 删物理文件 + mark_deleted 更新索引（标记"已删除"）。
    """
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    root = Path(library.get_shared_assets_path()).resolve()
    abs_path = (root / rel).resolve()
    if not str(abs_path).startswith(str(root)):
        raise HTTPException(status_code=400, detail="路径越界")
    # 读引用计数器拦截（ref_count>0 的截帧图/被引用图不可删）
    info = assets_index.entry_ref_of(rel)
    if info["ref_count"] > 0:
        raise HTTPException(status_code=409, detail={
            "msg": f"该未分类图被 {info['ref_count']} 处引用，不可删除。请先解除引用。",
            "referenced": True,
            "count": info["ref_count"],
            "ref_sources": info["ref_sources"],
            "source_scene": info["source_scene"],
        })
    # =0 → 删物理文件 + 索引标记已删除
    if abs_path.is_file():
        try:
            abs_path.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"物理文件删除失败：{e}")
    assets_index.mark_deleted(rel)
    return {"deleted": rel}


@app.post("/api/assets/index/recount")
def api_assets_index_recount():
    """第6批 item4（体检自动跑）：全量对账引用计数（collect+refresh，以扫描为准，不一致自动修正）。
    返回 {scanned, fixed, unchanged, changed_paths}，供前端加载资产页自动调用（不弹窗，修正入日志）。"""
    res = assets_index.apply_all_ref_counts(storage.load_projects_index, storage.load_project)
    return {
        "scanned": res["scanned"],
        "fixed": res["refresh"]["fixed"],
        "unchanged": res["refresh"]["unchanged"],
        "changed_paths": res["refresh"]["changed_paths"],
    }


@app.post("/api/assets/index/reconcile")
def api_assets_index_reconcile():
    """手动全量对账（基线/兜底：注册缺失、标记已删除）。"""
    return assets_index.reconcile()


class GenRefsPayload(BaseModel):
    paths: list[str] = []   # 本次图生成 @ 引用的物理图相对路径（uploads/ 等版本索引已登记图）
    pid: str = ""           # 调用方"当前项目"上下文（兜底；每张图优先取自身 source_scene.pid 归属项目）


@app.post("/api/assets/generation/refs")
def api_assets_generation_refs(payload: GenRefsPayload):
    """B 批 N5：资产库图生图 @ 未分类/截帧图 的"未归类路径"补 generation {pid} 项目级保护。

    背景：资产生成绑卡(id 非空)已由第6批步骤⑤(ref_auto 把草稿未分类/截帧参考图并入资产 images[])
    受资产引用保护；但**未绑卡**（生成产物进未分类区，id 空）时，@ 引用的未分类/截帧图无任何记录 →
    删其归属场后 ref_count 归零可被物理删 → 保护失效（P4/N5 缺口）。
    本项目级保护：对引用的截帧图记一条 generation 引用（ref_sources 加 {type:generation, pid}，
    ref_count+1，删除拦截自动生效），删对应项目时按 {pid} 聚合释放（复用 api_delete_project 链路）。

    粒度自决（数据层）：每张图的保护项目**优先取自身 source_scene.pid**（截帧图归属项目，语义最稳，
    源场删后仍保护至该项目删除，符合用户裁定）；无 source_scene 者用 payload.pid 兜底；
    两者皆空（非截帧且无项目上下文）→ 跳过（无项目可保护，仍按既有引用逻辑处理）。
    幂等：add_generation_refs 同 rel + 同 {pid} 只记一条。"""
    buckets: dict = {}
    unresolved: list = []
    for p in (payload.paths or []):
        if not p:
            continue
        rel = p.strip().replace("\\", "/").lstrip("/")
        if not rel:
            continue
        ss = assets_index.entry_ref_of(rel).get("source_scene")
        pid = (ss or {}).get("pid") if isinstance(ss, dict) and ss.get("pid") else (payload.pid or "").strip()
        if not pid:
            unresolved.append(rel)
            continue
        buckets.setdefault(pid, []).append(rel)
    total = {"added": 0, "skipped": 0, "missing": 0, "changed_paths": []}
    for pid, rels in buckets.items():
        r = assets_index.add_generation_refs(rels, {"pid": pid})
        for k in ("added", "skipped", "missing"):
            total[k] += r[k]
        total["changed_paths"].extend(r["changed_paths"])
    total["changed_paths"] = list(dict.fromkeys(total["changed_paths"]))
    total["unresolved"] = unresolved
    return total


def _scene_tree_nodes() -> list:
    """展开当前全部在案项目的场节点（含 pid/eid/scid/名称），供出场统计与删卡拦截共用同一数据源。"""
    nodes = []
    for p in storage.list_projects():
        proj = storage.load_project(p["id"])
        if not proj:
            continue
        for ep in proj.get("episodes", []):
            for sc in ep.get("scenes", []):
                nodes.append({
                    "pid": proj["id"], "eid": ep.get("id"), "scid": sc.get("id"),
                    "project": proj.get("name", ""), "episode": ep.get("title", ""),
                    "scene": sc.get("title", ""),
                })
    return nodes


def _asset_hit_nodes(scopes: dict, nodes: list) -> list:
    """单资产 scope 命中哪些在案场节点（与出场统计同一推导语义）。scopes 可缺失容错。"""
    scopes = scopes or {}
    s_projects = set(scopes.get("projects", []) or [])
    s_episodes = set(scopes.get("episodes", []) or [])
    s_scenes = set(scopes.get("scenes", []) or [])
    return [n for n in nodes
            if n["pid"] in s_projects or n["eid"] in s_episodes or n["scid"] in s_scenes]


@app.get("/api/ref-stats")
def api_ref_stats():
    """出场资产统计（计划书 4.11，v1.34 三层推导语义）：
    项目归属→展开该项目全部集/场；集归属→展开该集全部场次；场次归属→精确场；
    多粒度并存→全部展开求并集、按场去重。"""
    stats = {kind: {} for kind in library.KINDS}
    nodes = _scene_tree_nodes()
    for kind in library.KINDS:
        for item in library.list_items(kind):
            hit = _asset_hit_nodes(item.get("scopes", {}) or {}, nodes)
            if hit:
                stats[kind][item["id"]] = {
                    "count": len(hit),
                    "scenes": [{"pid": n["pid"], "project": n["project"], "episode": n["episode"],
                                "scene": n["scene"]} for n in hit],
                }
    return stats


# ==================== 全局库 CRUD API（阶段 3：统一资产模型 v1.20） ====================

class AssetPayload(BaseModel):
    prefix: str = ""
    variant_name: str = ""
    scopes: dict | None = None
    prompt: str = ""
    images: list | None = None
    voice_text: str = ""
    audios: list | None = None
    voice_source: str = ""  # 音色来源单选：text / audio / edge-tts（v1.26/4.12）
    voice: str = ""         # edge-tts 预置音色 id（voice_source=edge-tts 时）


def _valid_kind(kind: str) -> str:
    if kind not in library.KINDS:
        raise HTTPException(status_code=404, detail="库类型不存在")
    return kind


# ==================== 参考类资产 · 模型统一升级（2026-09-04，用户裁定 A） ====================
# 参考类与三库资产统一结构（可复用 openMeta 元数据编辑器），不进 library.KINDS（避免污染三库遍历/审计/封面/引用对账）。
# 端点放本处（AssetPayload 已定义于上），供前端参考类标签编辑保存 + 未分类裸图/尾帧截帧"落地为资产"。
@app.post("/api/references")
def api_create_reference(payload: AssetPayload):
    """参考类资产生成（模型统一）：收三库资产结构(AssetPayload)，建统一 reference 资产。

    用于：①未分类裸图/尾帧截帧"落地为资产"（images[0]=该图，prefix 缺省取文件名）
    ②资产库参考类标签"＋新建素材"。不进 library.KINDS，独立维护 references.json。
    """
    images = payload.images or []
    im0 = images[0] if images else None
    p0 = im0.get("path") if isinstance(im0, dict) else (str(im0) if im0 else "")
    ref = library.new_reference(
        prefix=(payload.prefix or "").strip(),
        path=p0 or "",
        variant_name=(payload.variant_name or "").strip(),
        scopes=payload.scopes,
        prompt=(payload.prompt or "").strip(),
        images=images,
        voice_text=(payload.voice_text or "").strip(),
        audios=payload.audios or [],
        voice_source=payload.voice_source or "text",
        voice=payload.voice or "",
    )
    # 落地后：uploads/ 等来源图由 unclassified_entries 的 referenced 扫描（assets_index 已纳入 reference）
    #   自动脱离"未分类游离区"——图落地即资产、资产库无游离图。refs/ 来源本就不属未分类。
    return ref


@app.put("/api/references/{ref_id}")
def api_update_reference(ref_id: str, payload: AssetPayload):
    """参考类资产编辑保存（openMeta 保存落库）：更新统一结构字段。"""
    ref = library.get_reference(ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="参考类资产不存在")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
            ref[k] = v
    ims = ref.get("images") or []
    if ims and isinstance(ims[0], dict) and ims[0].get("path"):
        ref["path"] = ims[0]["path"]   # 主图 path 与 images[0] 保持同步，供派生/删除定位
    ref.setdefault("variant_name", library.REF_VARIANT_DEFAULT)
    ref["updated_at"] = library.now_iso()
    library.save_reference(ref)
    return ref


class MaterializePayload(BaseModel):
    """3b(IC 基线15 落地即资产)：把一张 output/uploads 游离图收录为素材卡。"""
    path: str = ""


@app.post("/api/materials/materialize")
def api_materialize_material(payload: MaterializePayload):
    """3b(2026-09-09, IC 基线15 落地即资产, 用户裁决策点 B=references 素材卡)：
    把一张 output/uploads 游离图收录为"素材卡"（references 域，与三库平行）。

    做法（最小安全、不改 assets_index 既有逻辑）：
      源图物理**复制**到 refs/（遵循 references 素材卡主图在 refs/ 的约定），
      `library.new_reference(refs_rel)` 建素材卡（images[0]=refs 副本）→ assets_index 登记副本。
      源 output/uploads 原图与 assets_index 原条目不触碰（历史保留）。
    幂等：素材卡记私有字段 `_src_rel`=源图路径，重复收录同一源图返回既有卡（不重复建卡）。
    防误建：仅对"显式收录"触发（reconcile/observer 全库扫描不会无差别建卡）。
    """
    import shutil as _sh
    src_rel = (payload.path or "").strip().replace("\\", "/").lstrip("/")
    if not src_rel:
        raise HTTPException(status_code=400, detail="缺少源图路径")
    root = Path(library.get_shared_assets_path()).resolve()
    src = (root / src_rel).resolve()
    # 收尾A⑦(2026-09-10): 越界校验改 relative_to(按组件边界, 防 str.startswith 前缀绕过如 /shared_evil)
    try:
        src.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="路径越界")
    if not src.is_file():
        raise HTTPException(status_code=404, detail=f"源图不存在：{src_rel}")
    ext = Path(src).suffix.lower()
    if ext not in _REF_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    # 幂等：已有指向该源图的素材卡（_src_rel 记源图）→ 返回既有卡
    for r in library.list_references():
        if r.get("_src_rel") == src_rel:
            return {"materialized": False, "ref_id": r["id"], "path": src_rel, "note": "该游离图已是素材卡"}
    # 复制到 refs/ 并建素材卡
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(src).stem or "material"
    safe_stem = "".join(ch for ch in stem if ch not in '\\/:*?"<>|').strip() or "material"
    fname = f"{library.new_id()[:8]}_{safe_stem}{ext}"
    refs_rel = f"refs/{fname}"
    _sh.copy2(src, refs_dir / fname)
    # new_reference(prefix, path, ...) 首参=prefix、次参=path；传 path 才有 images[0] 主图（refs/副本）
    ref = library.new_reference("", refs_rel)   # prefix 由 path 文件名派生；建素材卡 images[0]=refs/副本
    ref["_src_rel"] = src_rel               # 私有字段记源图（幂等去重用）
    library.save_reference(ref)
    try:
        assets_index.register_file(refs_rel)   # 登记副本入版本索引(source=reference)
    except Exception:
        pass
    return {"materialized": True, "ref_id": ref["id"], "path": src_rel, "refs_path": refs_rel}


def _enforce_asset_validation(kind: str, asset: dict) -> dict:
    """资产保存前 fail-fast 校验（6-13）。errors 非空 → 400 字段级阻断；仅 warnings → 返回 warnings 由调用方经 header 附带。"""
    if kind not in ("characters", "scenes", "props"):
        return {}
    res = asset_validator.validate_asset_on_save(asset, kind)
    errs = res.get("errors") or {}
    if errs:
        raise HTTPException(status_code=400, detail={
            "msg": "资产未保存：存在阻断项（见 errors，字段级）",
            "errors": errs,
        })
    return res.get("warnings") or {}


def _get_asset(kind: str, item_id: str) -> dict:
    item = library.get_item(kind, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


# ==================== 通用 AI 能力（4-6：AI 优化提示词 + 归属智能推荐·LLM） ====================
# 固定前缀，声明在 /api/{kind} 通配符之前

class OptimizePromptPayload(BaseModel):
    text: str = ""
    context: str = ""


def _require_model(model_id: str | None = None) -> dict:
    """统一 LLM 模型检查（v1.55 P0-5b）：区分"未配置"vs"Key 未填写"——帮助定位 key 丢失/未保存问题。"""
    model = ai.get_model(model_id)
    if model is None:
        models = list((library.load_settings().get("llm_models") or []))
        if not models:
            raise HTTPException(status_code=400, detail="尚未配置 LLM 模型：请到 设置页 添加模型并填写 API Key（保存后生效）")
        raise HTTPException(status_code=400, detail="模型选择失败：请到 设置页 检查模型列表（若仅一个模型会自动选中）")
    if not (model.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="模型「" + (model.get("name") or model.get("model") or "未命名") + "」未填写 API Key：请到 设置页 填写后保存")
    return model


@app.post("/api/optimize-prompt/estimate")
def api_optimize_prompt_estimate(payload: OptimizePromptPayload):
    """AI 优化提示词成本预估（4-6：按输入文本长度估算）。"""
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="无可优化的文本")
    model = _require_model()
    est = ai.estimate_cost(len(text), model)
    return {**est, "model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}


@app.post("/api/optimize-prompt")
def api_optimize_prompt(payload: OptimizePromptPayload):
    """AI 优化提示词（4-6：LLM 润色回填，成本确认后调用）。"""
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="无可优化的文本")
    model = _require_model()
    try:
        optimized = ai.optimize_prompt(model, text)
    except Exception as e:
        raise HTTPException(status_code=500, detail="AI 优化失败：" + str(e))
    return {"optimized": optimized}


class RecommendScopesPayload(BaseModel):
    model_id: str | None = None


def _scope_context() -> str:
    """归属推荐可选项上下文：项目/集/场列表（名称+id）。"""
    lines = []
    for p in storage.list_projects():
        detail = storage.load_project(p["id"])
        lines.append(f"项目: {p['name']} (id={p['id']})")
        for ep in (detail or {}).get("episodes", []):
            lines.append(f"  集: {ep.get('title')} (id={ep.get('id')})")
            for sc in ep.get("scenes", []):
                lines.append(f"    场: {sc.get('title')} (id={sc.get('id')})")
    return "\n".join(lines) or "（暂无项目，请先创建项目并拆解/导入结构）"


@app.post("/api/{kind}/{item_id}/recommend-scopes/estimate")
def api_recommend_scopes_estimate(kind: str, item_id: str, payload: RecommendScopesPayload):
    """归属智能推荐·LLM 成本预估（4-6：按资产名称/描述长度估算）。"""
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    desc = f"{asset.get('prefix') or ''}-{asset.get('variant_name') or ''}：{(asset.get('prompt') or '')[:200]}"
    model = _require_model(payload.model_id)
    est = ai.estimate_cost(len(desc) + 200, model)
    return {**est, "model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}


@app.post("/api/{kind}/{item_id}/recommend-scopes")
def api_recommend_scopes(kind: str, item_id: str, payload: RecommendScopesPayload):
    """归属智能推荐·LLM（4-6：按名称/描述/关联场推荐，返回候选；用户确认后生效）。"""
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    desc = f"{asset.get('prefix') or ''}-{asset.get('variant_name') or ''}：{(asset.get('prompt') or '')[:200]}"
    model = _require_model(payload.model_id)
    try:
        data = ai.recommend_scopes(model, desc, _scope_context())
    except Exception as e:
        raise HTTPException(status_code=500, detail="LLM 推荐失败：" + str(e))
    # 校验 level/id 合法性
    recs = []
    for r in (data.get("recommendations") or []):
        level = str(r.get("level") or "")
        if level not in ("project", "episode", "scene"):
            continue
        rid = str(r.get("id") or "")
        if rid:
            recs.append({"level": level, "id": rid, "reason": str(r.get("reason") or "").strip()})
    return {"recommendations": recs}


# 固定前缀 API 必须声明在 /api/{kind} 通配符之前（否则被吞）

# ---- IC 代理（阶段 5，固定前缀：必须在 /api/{kind} 通配之前） ----

IC_DEFAULT_URL = ""   # IC 已下线：默认不再指向任何地址，兼容旧部署时由设置页 settings.ic_url 提供


def _ic_url() -> str:
    cfg = library.load_settings()
    return (cfg.get("ic_url") or IC_DEFAULT_URL).rstrip("/")


def _fix_mojibake(text: str) -> str:
    """自动修复双重编码乱码（阶段5复检：sc.text 曾被 latin1→utf8 双重编码写坏成 mojibake）。
    检测 UTF-8 中文被误读的标志字节序列，用 latin1 编码+utf-8 解码还原；非乱码原文原样返回。
    幂等：还原后若仍含乱码特征则跳过（防误改正常文本）。"""
    if not text:
        return text
    try:
        if is_mojibake(text):
            fixed = text.encode("latin-1").decode("utf-8")
            # 还原后再检测一次，确仍乱码则还原失败，保留原文
            if not is_mojibake(fixed):
                return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text


def is_mojibake(text: str) -> bool:
    """检测文本是否为 UTF-8 中文被当 latin1 误读的 mojibake（双重编码乱码）。
    判定：①含 latin1 高位字符（U+0080-U+00FF，'ä¸­' 等是中文 UTF-8 字节被读成 latin1 的特征）
    ②尝试 latin1→utf-8 还原能得到显著更多的 CJK 字符（证明原本是真中文被误读）。
    幂等：还原后 CJK 不再增加则判非 mojibake（正常文本不受影响）。"""
    if not text or not re.search(r"[\u0080-\u00ff]", text):
        return False
    try:
        fixed = text.encode("latin-1").decode("utf-8")

        def _cjk(s):
            return len(re.findall(r"[\u4e00-\u9fff]", s))
        return _cjk(fixed) > _cjk(text)
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _ic_request(method: str, path: str, json_body: dict | None = None, timeout: int = 30) -> dict:
    """经 HTTP 调用 IC 公开接口（Y2 结论：主链路统一经 IC，不直连 ComfyUI）。"""
    import requests
    url = _ic_url() + path
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=json_body or {}, timeout=timeout)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"IC 不可达（{_ic_url()}）：{e}")
    if r.status_code != 200:
        detail = r.text[:200]
        try:
            detail = r.json().get("detail") or detail
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"IC 接口 {path} 返回 {r.status_code}：{detail}")
    return r.json()


@app.get("/api/ic/providers")
def api_ic_providers():
    """平台列表（供场页"平台→模型"两级联动：经 IC /api/providers 动态拉取，v1.28）。"""
    data = _ic_request("GET", "/api/providers")
    return {"providers": data.get("providers") or []}


@app.get("/api/ic/workflows")
def api_ic_workflows():
    """IC 自定义工作流列表（含 minimax_h3_t8 跑通版，Y2 注册）。"""
    data = _ic_request("GET", "/api/workflows")
    return {"workflows": data.get("workflows") or []}


@app.get("/api/ic/workflows/{name:path}")
def api_ic_workflow(name: str):
    """工作流详情（含 config 字段映射，供参数注入）。"""
    return _ic_request("GET", "/api/workflows/" + name)


@app.get("/api/ic/health")
def api_ic_health():
    """生成视频前置环境完备检查（用户复检：点生成视频先检环境，不成立提示/自动拉起）。
    返回：ic(IC服务是否可达)/comfy(ComfyUI后端是否可用)/uu(UU端口映射是否通)。"""
    import requests
    from urllib.parse import urlparse
    result = {"ic": {"ok": False, "msg": ""}, "comfy": {"ok": False, "msg": ""}, "uu": {"ok": False, "msg": ""}}
    # 1) IC 服务可达
    try:
        r = requests.get(_ic_url(), timeout=4)
        result["ic"]["ok"] = r.status_code == 200
        result["ic"]["msg"] = "IC 服务可达" if result["ic"]["ok"] else f"IC 返回 {r.status_code}"
    except requests.RequestException as e:
        result["ic"]["msg"] = "IC 服务未运行（可自动拉起）"
    # 2) ComfyUI 实例（经 IC）
    instances = []
    if result["ic"]["ok"]:
        try:
            data = _ic_request("GET", "/api/comfyui/instances", timeout=6)
            instances = data.get("instances") or []
        except HTTPException as e:
            result["comfy"]["msg"] = "IC 未返回 ComfyUI 实例"
            instances = []
    else:
        result["comfy"]["msg"] = "IC 未运行，无法检测 ComfyUI"
    if instances:
        result["comfy"]["ok"] = True
        result["comfy"]["msg"] = "ComfyUI 实例： " + ", ".join(instances)
        # 3) UU 端口映射：探测第一个 ComfyUI 后端地址是否真可达
        addr = instances[0]
        host, _, port = addr.rpartition(":")
        try:
            rr = requests.get(f"http://{addr}/system_stats", timeout=6)
            result["uu"]["ok"] = rr.status_code == 200
            result["uu"]["msg"] = "UU 端口映射可达（ComfyUI /system_stats）" if result["uu"]["ok"] else f"ComfyUI 可达但 /system_stats 返回 {rr.status_code}"
        except requests.RequestException as e:
            result["uu"]["msg"] = f"ComfyUI 不可达（UU 端口映射未建立/远程机未启动）：{e}"
    if not result["comfy"]["ok"]:
        result["uu"]["msg"] = result["comfy"]["msg"]
    return result


@app.post("/api/ic/boot")
def api_ic_boot():
    """自动拉起 IC 服务（复检：用户点'自动拉起 IC'）——在 Infinite Canvas 目录启动 main.py（独立后台进程）。"""
    import requests
    import subprocess
    import sys
    import os
    # 先探测是否已运行
    try:
        r = requests.get(_ic_url(), timeout=3)
        if r.status_code == 200:
            return {"ok": True, "msg": "IC 已运行", "already": True}
    except requests.RequestException:
        pass
    # 从 settings.ic_path 拿 IC 目录；若无则以 _ic_url 为参考，尝试常见位置
    cfg = library.load_settings()
    ic_path = cfg.get("ic_path") or ""
    if not ic_path or not os.path.isdir(ic_path):
        return {"ok": False, "msg": "配置的 Infinite Canvas 路径无效，请先到设置页配置 ic_path"}
    # 在 IC 目录用其 python 启动（detached，独立进程）
    py = os.path.join(ic_path, "python", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    try:
        p = subprocess.Popen(
            [py, "main.py"], cwd=ic_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x08,  # DETACHED_PROCESS
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "msg": f"拉起 IC 失败：{e}"}
    # 等待就绪
    import time
    for _ in range(20):
        time.sleep(0.5)
        try:
            rr = requests.get(_ic_url(), timeout=2)
            if rr.status_code == 200:
                return {"ok": True, "msg": "IC 已自动拉起", "already": False}
        except requests.RequestException:
            continue
    return {"ok": False, "msg": "IC 启动命令已发出，但等待就绪超时（请检查 IC 目录/端口）", "pid": p.pid}


@app.post("/api/comfy/free")
def api_comfy_free():
    """§十四 B-c 清显存：代理远程 ComfyUI /free（卸载模型+释放显存）。
    供 H3 视频生成前/失败后一键调用，缓解显存 OOM（AudioVAE 超长波形编码→OOM→tiled 降级崩溃 根因之一）。"""
    import requests
    base = library.get_comfyui_url()
    try:
        r = requests.post(f"{base}/free",
                          json={"unload_models": True, "free_memory": True},
                          timeout=60)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI 不可达（{base}）：{e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ComfyUI /free 返回 {r.status_code}：{r.text[:200]}")
    return {"ok": True, "msg": "显存已释放（模型卸载 + 显存清理）", "status": r.status_code}


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
    workflow_type: str = _GEN_WF_DEFAULT      # z_image/2511/klein/enhance/upscale
    prompt: str = ""                          # 便捷 prompt（有文本槽类型时写入文本节点）
    ref_images: list = []                     # 便捷参考图（相对 SharedAssets 路径或本地绝对路径，按 refs 槽位顺序填）
    width: int = 0                            # 便捷尺寸（z_image 需传；0=不覆盖）
    height: int = 0
    seed: int | None = None                   # 便捷种子（None=随机）
    params: dict = {}                         # 节点级覆盖 params={节点ID:{字段:值}}，merge 覆盖便捷层（值 None 则 pop）
    client_id: str = "storyboard-studio"


def _gen_workflow_file(wf_type: str) -> str:
    """workflow_type → 工作流文件；未知类型报错（fail-closed，不静默回退默认）。"""
    spec = _GEN_WORKFLOWS.get(wf_type)
    if not spec:
        raise HTTPException(status_code=400,
                            detail=f"未知 workflow_type={wf_type!r}，可选：{', '.join(_GEN_WF_NAMES)}")
    return spec["file"]


@app.post("/api/comfy/generate")
def api_comfy_generate(payload: ComfyGeneratePayload):
    """阶段4 生图直连 ComfyUI（同步；首次调用含模型加载 1-2 分钟，UI 需 loading 态）。
    契约 {workflow_type, prompt, ref_images[], params{}}：
      便捷层（本端点按类型自动注入）→ 参考图按 refs 槽位填 LoadImage.image / prompt 写文本节点 /
        seed(缺省随机) 写采样节点 / width·height 写尺寸节点。
      params 层（节点级覆盖，复刻 IC）→ 便捷层已注入后，将显式 params merge 覆盖（值 None 则 pop）。
    返回 {images:[bytes?], url?, prompt_id, workflow_type}——本端点同步返回 PNG bytes（image/png 直出），
      异常抛 502 带原因。参考图上传复用 ComfyUI /upload/image（comfy_client 内部）。"""
    import comfy_client
    wf_type = payload.workflow_type or _GEN_WF_DEFAULT
    spec = _GEN_WORKFLOWS.get(wf_type)
    if not spec:
        raise HTTPException(status_code=400,
                            detail=f"未知 workflow_type={wf_type!r}，可选：{', '.join(_GEN_WF_NAMES)}")
    # 参考图相对 SharedAssets 路径 → 绝对路径（comfy_client._upload_params_images 亦能识别相对，但显式转绝对更稳）
    refs = list(payload.ref_images or [])
    # 便捷层：组 params（节点覆盖字典）
    bp: dict = {}
    # ① 参考图槽位分配（z_image 无槽 → 忽略多余 refs 并告警，其余按 refs 顺序填 LoadImage）
    for i, slot in enumerate(spec["refs"]):
        if i < len(refs) and refs[i]:
            bp.setdefault(slot["img"], {})["image"] = refs[i]
        elif not slot.get("opt"):
            raise HTTPException(status_code=400,
                                detail=f"workflow_type={wf_type} 需参考图 #{i+1}（ref_images 不足）")
    # ② prompt → 文本节点
    if payload.prompt and spec.get("prompt_node"):
        bp.setdefault(spec["prompt_node"], {})[spec["text_key"]] = payload.prompt
    # ③ 尺寸（z_image）
    if spec.get("size_node") and (payload.width or payload.height):
        if payload.width:
            bp.setdefault(spec["size_node"], {})["width"] = int(payload.width)
        if payload.height:
            bp.setdefault(spec["size_node"], {})["height"] = int(payload.height)
    # ④ 种子（缺省随机；写各采样/噪声节点）
    #   范围取 int32 正区间(0~2**31-1)：SeedVR2(upscale 节点172)等节点对超大 seed 会
    #   prompt_outputs_failed_validation 拒绝(实测 2**53 级随机 seed → 400)；KSampler/RandomNoise 亦兼容。
    _seed = payload.seed if payload.seed is not None else None
    if spec["seed_nodes"]:
        for nid in spec["seed_nodes"]:
            if _seed is None:
                import random as _rnd
                _seed = _rnd.randint(0, 2**31 - 1)
            bp.setdefault(nid, {})[spec["seed_key"]] = _seed
    # merge 显式 params：数字节点ID → 节点级覆盖（值 None 则 pop 语义由 _apply_params 处理；显式优先）
    #   领域键(angle_h/angle_v/distance/strength/target_res…) → 按 workflow_type 翻译成节点覆盖(gentools 生图工具页)
    _node_p: dict = {}
    _dom_p: dict = {}
    for _pk, _pv in (payload.params or {}).items():
        if str(_pk).isdigit():
            _node_p[str(_pk)] = _pv
        else:
            _dom_p[str(_pk)] = _pv
    _dom_nodes = _translate_domain_params(wf_type, _dom_p, payload.prompt or "")
    for _nid, _nv in _dom_nodes.items():
        bp.setdefault(str(_nid), {}).update(_nv)
    bp.update({str(k): v for k, v in _node_p.items()})
    # 调 comfy_client 生图编排（params 内嵌 image 本地/相对路径自动上传回填 ASCII 名）
    try:
        img_bytes, meta = comfy_client.generate_image(
            spec["file"], params=bp,
            progress_cb=lambda m: None)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生图失败：{e}")
    # 阶段5：产物落盘 SharedAssets/output/ 并返回相对路径（对齐 IC 契约 images[]，供 assets 绑卡/gentools 预览）
    rel = _archive_gen_image(img_bytes)
    return {"images": [rel], "url": "/api/asset-file/" + rel,
            "prompt_id": str(meta.get("prompt_id") or ""),
            "workflow_type": wf_type}


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


# ═══════════════════════════════════════════════════════════════════════
# 阶段4（B-20260908-03 · 方案 v1.5 四、GS 专用端点）高斯泼溅三端点
# 依据 gsplat-preview 组件（README v2 + server.py），工坊重写：
#   GET  /api/gsplat/ply/<file>   远程流式代理 ComfyUI output 的 ply（read 65536 chunk 勿整读内存；
#                                  basename 防穿越；CORS *；Cache-Control max-age=3600）
#   GET  /api/gsplat/viewer        返回工坊 3D 预览页 static/gsplat-viewer.html（?file=xxx.ply）
#   POST /api/gsplat/save         收 3D 预览截图 → 素材区 refs/（参考类 kind=reference，落地即资产，
#                                  复用 references 入库通道：存 refs/ + new_reference + register_file）
# 工坊无本地 ComfyUI output（ply 在远程 ComfyUI 的 output 目录，SHARP 输出 sharp_*.ply；地址由 get_comfyui_url() 从设置读）；
# ply 60-70MB 跨机加载 2-5 分钟需前端 loading 提示。
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/gsplat/ply/{file:path}")
def api_gsplat_ply(file: str):
    """远程代理 ComfyUI output 的 ply（工坊无本地 ComfyUI output，SHARP ply 在远程 get_comfyui_url()）。
    流式转发（read 65536 chunk 勿整读内存）；basename 防目录穿越；CORS * / Cache-Control max-age=3600。
    本地缓存（data/gsplat_ply/）：命中则直接发本机副本（秒开）；未命中转发远程时边转边落盘，下次同文件命中。"""
    import os as _os
    fname = _os.path.basename(file)          # 防目录穿越（复刻 gsplat-preview server.py）
    if not fname.lower().endswith(".ply"):
        raise HTTPException(status_code=400, detail="仅支持 .ply")
    cache_dir = BASE_DIR / "data" / "gsplat_ply"
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / fname

    # ① 命中本地缓存 → 直接流式发本机文件（不再回源远程，秒开）
    if local.exists() and local.stat().st_size > 0:
        def _local_chunks():
            with open(local, "rb") as f:
                while True:
                    c = f.read(65536)
                    if not c:
                        break
                    yield c
        return StreamingResponse(_local_chunks(), media_type="application/octet-stream",
                                 headers={"Access-Control-Allow-Origin": "*",
                                          "Cache-Control": "public, max-age=3600",
                                          "Content-Disposition": f'attachment; filename="{fname}"'})

    # ② 未命中 → 流式转发远程，边转边写临时文件，完成后原子 rename 落盘
    import requests as _req
    base = library.get_comfyui_url()
    try:
        r = _req.get(f"{base}/view",
                     params={"filename": fname, "type": "output", "subfolder": ""},
                     stream=True, timeout=30)
    except _req.RequestException as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI 不可达({base})：{e}")
    if r.status_code != 200:
        raise HTTPException(status_code=404,
                            detail=f"ply 不存在/拉取失败({r.status_code})：{fname}（SHARP 尚未生成？）")
    tmp = cache_dir / (fname + ".tmp")

    def _chunks():
        try:
            tmpf = open(tmp, "wb")
            try:
                for c in r.iter_content(chunk_size=65536):
                    if c:
                        tmpf.write(c)
                        yield c
                tmpf.flush()
                _os.replace(tmp, local)     # 完整拉完才落盘，避免半截文件
            except Exception:
                try: _os.remove(tmp)
                except Exception: pass
                raise
            finally:
                tmpf.close()
        finally:
            r.close()
    return StreamingResponse(_chunks(), media_type="application/octet-stream",
                             headers={"Access-Control-Allow-Origin": "*",
                                      "Cache-Control": "public, max-age=3600",
                                      "Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/gsplat/viewer")
def api_gsplat_viewer(file: str = ""):
    """返回工坊 3D 高斯泼溅预览页（static/gsplat-viewer.html；URL ?file=xxx.ply 指定 ply）。"""
    p = BASE_DIR / "static" / "gsplat-viewer.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="gsplat-viewer.html 未就绪")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.post("/api/gsplat/upload")
async def api_gsplat_upload(file: UploadFile = File(...)):
    """本地 .ply 上传 → 存到 ply 本地缓存目录 data/gsplat_ply/（命中 /api/gsplat/ply/ 秒发，
    不经远程 ComfyUI/不拉生成），供 3D 查看器直接查看本地 ply。分块落盘防大文件整读内存。"""
    import shutil as _sh
    orig = Path(file.filename or "upload.ply").name
    if not orig.lower().endswith(".ply"):
        raise HTTPException(status_code=400, detail="仅支持 .ply 文件")
    cache_dir = BASE_DIR / "data" / "gsplat_ply"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in orig if ch not in '\\/:*?"<>|').strip() or "upload.ply"
    fname = f"{library.new_id()[:8]}_{safe}"
    dest = cache_dir / fname
    try:
        with dest.open("wb") as f:
            _sh.copyfileobj(file.file, f, length=1 << 20)   # 1MB chunk 流式写
    except Exception as e:
        try: dest.unlink()
        except Exception: pass
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    return {"ok": True, "ply": fname, "url": "/api/gsplat/ply/" + fname,
            "size": dest.stat().st_size}

@app.post("/api/gsplat/save")
async def api_gsplat_save(file: UploadFile = File(...)):
    """3D 预览截图 → 素材区（参考类 kind=reference，落地即资产）。复用 ref-upload 入库通道：
    存 SharedAssets/refs/ + library.new_reference（prefix 由文件名派生）+ assets_index.register_file（幂等登记）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="截图文件为空")
    ext = Path(file.filename or "gs.png").suffix.lower() or ".png"
    if ext not in _REF_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="仅支持图片格式")
    d = Path(library.get_shared_assets_path()) / "refs"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in (Path(file.filename or "img").name or "img") if ch not in '\\/:*?"<>|').strip() or "img"
    stem = Path(safe).stem or "img"
    fname = f"{library.new_id()[:8]}_{stem}{ext}"
    (d / fname).write_bytes(raw)
    rel = f"refs/{fname}"
    # 3b修复: new_reference 传 path(prefix 由文件名派生) → 建带图素材卡(此前单参建空卡缺陷)
    ref = library.new_reference("", rel)
    try:
        assets_index.register_file(rel)   # 幂等登记入版本索引（供复用/清理查询）
    except Exception:
        pass
    return {"ok": True, "path": rel, "ref_id": ref["id"]}


# ── 阶段5-4（B-20260908-03）GS 生成：单图 → SHARP → .ply ──────────────────
class GsplatGeneratePayload(BaseModel):
    ref_image: str = ""                 # 参考图（相对 SharedAssets / 绝对 / http）
    focal_length_mm: float = 30.0       # 相机焦距（方案 GS 规格默认 30.0）


@app.post("/api/gsplat/generate")
def api_gsplat_generate(payload: GsplatGeneratePayload):
    """阶段5-4：单图 → SHARP 重建 → 返回 .ply 文件名（ply 留在远程 ComfyUI output，
    经 GET /api/gsplat/ply/<file> 流式代理拉取）。同步；SHARP 首个任务含模型加载约 1-3 分钟。
    返回 {ply, url, prompt_id}；异常抛 502。"""
    import comfy_client
    if not (payload.ref_image or "").strip():
        raise HTTPException(status_code=400, detail="ref_image 必填")
    try:
        res = comfy_client.generate_sharp(payload.ref_image, payload.focal_length_mm)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SHARP 生成失败：{e}")
    ply_name = res.get("ply_name") or ""
    return {"ply": ply_name,
            "url": "/api/gsplat/ply/" + ply_name,
            "prompt_id": str(res.get("prompt_id") or ""),
            "ply_abs": res.get("ply_abs") or ""}


# ---- 阶段 6 · 经 IC 生图（A 通道，封装 IC /api/generate，带 charset=utf-8 使中文 prompt 正常）----
class IcGeneratePayload(BaseModel):
    prompt: str = ""                        # 中文 prompt（经 charset utf-8 传，IC 正常解析）
    workflow_json: str = "Z-Image.json"     # 生图工作流：Z-Image.json(文生图)/2511.json(参考图)/Flux2-Klein.json(参考图迁移)
    width: int = 1024
    height: int = 1024
    type: str = "asset"
    client_id: str = "storyboard-studio"
    convert_to_jpg: bool = False
    params: dict = {}                       # 参考图传参(params 覆盖 LoadImage 等节点, 参考图用 ASCII 名) + 节点字段覆盖


@app.post("/api/ic/generate")
def api_ic_generate(payload: IcGeneratePayload):
    """⚠️ DEPRECATED（阶段4 B-20260908-03 标记，勿删路由与周边引用）：经 IC 生图 A 通道。
    阶段4 已新增直连 `POST /api/comfy/generate`（5 工作流，绕 IC）替代本通道。本路由保留至阶段5
    前端（assets 点卡生图/资产生成）切走后核对无引用再清。当前仍经 IC：封装 IC `/api/generate`，
    请求头显式带 charset=utf-8（IC 中文 prompt 编码根因=调用方 charset），用于资产生成（角色/场景/道具）、
    画风库样例图等。参考图传参走 params/ASCII 名（2511/Flux2-Klein 参考图生图）。
    注：IC 接口为同步（生成图片直至完成）；调用方需长超时。"""
    _log.warning("[deprecated] /api/ic/generate 被调用（stage4 直连 /api/comfy/generate 已上线）：type=%s workflow=%s",
                 payload.type, payload.workflow_json)
    import requests
    import json as _json
    url = _ic_url() + "/api/generate"
    body = {
        "prompt": payload.prompt,
        "workflow_json": payload.workflow_json,
        "width": payload.width,
        "height": payload.height,
        "type": payload.type,
        "client_id": payload.client_id,
        "convert_to_jpg": payload.convert_to_jpg,
        "params": payload.params or {},
    }
    try:
        # 显式 UTF-8 + charset：requests json= 默认 Content-Type 不带 charset (IC 按 latin-1 解析会损坏中文)
        r = requests.post(url, data=_json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, timeout=600)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"IC 不可达（{_ic_url()}）：{e}")
    if r.status_code != 200:
        detail = r.text[:300]
        try:
            detail = r.json().get("error") or r.json().get("detail") or detail
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"IC /api/generate 返回 {r.status_code}：{detail}")
    return r.json()


# ---- v1.55 音色样本生成工具（必须在 /api/{kind} 通配之前） ----

class VoiceSamplePayload(BaseModel):
    asset_id: str = ""          # 资产 id（音色仅人物，ch_ 前缀）
    kind: str = "characters"    # 资产类别（默认人物）
    instruct: str = ""          # 音色描述文本（必填，如"男声少年音色澄澈干净字正腔圆"）
    text: str = ""              # 朗读文本（留空=按资产名自动生成测试句）
    language: str = "Chinese"


@app.post("/api/voice-sample/generate")
def api_voice_sample_generate(payload: VoiceSamplePayload):
    """v1.55/v1.58：音色样本生成工具——文字描述音色 → Qwen3-TTS VoiceDesign → 24kHz 单声道 FLAC 干音。
    **直连远程 ComfyUI**（不经 IC；与 6-10 视频直连同源，地址取 settings.comfyui_url，默认尾网）。
    产物落盘 `<shared>/audio/voice_samples/{asset_id}_{ts}.flac`，并追加到资产 `audios[]`
    （`type=reference`, `role=voice_sample`）——role=voice_sample 即 H3 音色克隆输入。
    注：资产侧**一次性**工具（正式生视频不再跑）；同步调用（实测约 10 秒），调用方需长超时。"""
    import time
    import requests

    asset_id = (payload.asset_id or "").strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id 不能为空")
    kind = (payload.kind or "characters").strip()
    if kind not in library.KINDS:
        raise HTTPException(status_code=400, detail=f"未知资产类别：{kind}")
    instruct = (payload.instruct or "").strip()
    if not instruct:
        raise HTTPException(status_code=400, detail="音色描述（instruct）不能为空")

    item = _get_asset(kind, asset_id)
    name = f"{item.get('prefix', '')}-{item.get('variant_name', '')}".strip("-")
    text = (payload.text or "").strip() or f"大家好，我是{item.get('prefix') or '这个角色'}。很高兴认识你。"
    language = (payload.language or "Chinese").strip() or "Chinese"

    base = library.get_comfyui_url()
    workflow = {"prompt": {
        "1": {"class_type": "TDQwen3TTSModelLoader", "inputs": {
            "model_path": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "precision": "bf16", "device": "cuda",
            "attn_implementation": "sdpa", "auto_download": False, "download_source": "ModelScope"}},
        "2": {"class_type": "TDQwen3TTSVoiceDesign", "inputs": {
            "model": ["1", 0], "text": text, "instruct": instruct, "language": language}},
        "3": {"class_type": "SaveAudio", "inputs": {"audio": ["2", 0], "filename_prefix": f"vs_{asset_id}"}},
    }}
    try:
        r = requests.post(base + "/prompt", data=json.dumps(workflow).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, timeout=60)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI 不可达（{base}）：{e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ComfyUI /prompt 返回 {r.status_code}：{r.text[:300]}")
    res = r.json()
    if res.get("node_errors"):
        raise HTTPException(status_code=502, detail=f"工作流节点错误：{json.dumps(res['node_errors'], ensure_ascii=False)[:500]}")
    prompt_id = res.get("prompt_id")
    if not prompt_id:
        raise HTTPException(status_code=502, detail=f"ComfyUI 未返回 prompt_id：{str(res)[:300]}")

    # 轮询历史（实测约 10 秒；上限 300 秒）
    hist = None
    for _ in range(100):
        try:
            hr = requests.get(f"{base}/history/{prompt_id}", timeout=10)
            if hr.status_code == 200:
                h = hr.json()
                if prompt_id in h:
                    hist = h[prompt_id]
                    break
        except requests.RequestException:
            pass
        time.sleep(3)
    if hist is None:
        raise HTTPException(status_code=504, detail=f"音色样本生成超时（prompt_id={prompt_id}），请稍后在 ComfyUI 查看")
    status = (hist.get("status") or {}).get("status_str")
    if status != "success":
        msgs = (hist.get("status") or {}).get("messages") or []
        detail = "；".join(str(m[1].get("exception_message", ""))[:200] for m in msgs if m[0] == "execution_error")
        raise HTTPException(status_code=500, detail=f"生成失败（{status}）：{detail or '未知错误'}")

    # SaveAudio(节点3) 输出 → /view 拉取
    out = ((hist.get("outputs") or {}).get("3") or {}).get("audio") or []
    if not out:
        raise HTTPException(status_code=500, detail=f"工作流未产出音频（outputs={json.dumps(hist.get('outputs', {}), ensure_ascii=False)[:300]}）")
    info = out[0]
    try:
        vr = requests.get(base + "/view", timeout=60, params={
            "filename": info.get("filename", ""), "subfolder": info.get("subfolder", ""),
            "type": info.get("type", "output")})
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"拉取音频失败：{e}")
    if vr.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ComfyUI /view 返回 {vr.status_code}")

    # 落盘 <shared>/audio/voice_samples/{asset_id}_{ts}.flac（audios[] 存相对路径）
    shared = Path(library.get_shared_assets_path())
    (shared / "audio" / "voice_samples").mkdir(parents=True, exist_ok=True)
    ts = library.now_iso().replace(":", "").replace("-", "")
    rel = f"audio/voice_samples/{asset_id}_{ts}.flac"
    (shared / rel).write_bytes(vr.content)

    entry = {"path": rel, "type": "reference", "role": "voice_sample", "voice": "",
             "added_at": library.now_iso()}
    item.setdefault("audios", []).append(entry)
    item["updated_at"] = library.now_iso()
    library.save_item(kind, item)
    return {"path": rel, "size": len(vr.content), "asset_id": asset_id, "name": name, "audio": entry}


# ---- v1.55 画风库数据（style-templates.json 数据化，供 style-ref.html 读取；8 风格含 positive/negative_template） ----

@app.get("/api/style-templates")
def api_style_templates():
    """画风库模板（static/style-templates.json 预置数据入库；palette.js 为前端兜底）。"""
    p = BASE_DIR / "static" / "style-templates.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": "1.0", "items": []}


# ---- 6-5 提示词组装 + 去程任务卡（handoff/outbox，必须在 /api/{kind} 通配之前） ----

class AssemblePromptPayload(BaseModel):
    pid: str = ""
    eid: str = ""
    sid: str = ""
    extra: str = ""          # 追加提示词（可选）


@app.post("/api/assemble-prompt")
def api_assemble_prompt(payload: AssemblePromptPayload):
    """按 4.4 组装提示词（场粒度）：全局画风 + 场景变体 + 出场角色 + 道具 + 分镜文本 + 行内运镜/景别/时长。"""
    return handoff.assemble_prompt(payload.pid, payload.eid, payload.sid, payload.extra)


class TaskCardPayload(BaseModel):
    target_type: str = "场图片"   # 角色/场景/道具/场图片/场视频
    name: str = ""
    resource_path: str = ""        # 资产路径（相对 SharedAssets）或场 id
    prompt: str = ""
    meta: dict = {}


@app.post("/api/task-card")
def api_create_task_card(payload: TaskCardPayload):
    """生成去程任务卡写入 handoff\\outbox，返回卡片。"""
    return handoff.create_task_card(payload.target_type, payload.name,
                                    payload.resource_path, payload.prompt, payload.meta)


@app.get("/api/task-cards")
def api_list_task_cards(limit: int = 200):
    """列出去程任务卡（时间倒序；供资产库总览/时间窗关联展示）。"""
    return {"cards": handoff.list_task_cards(limit=limit)}


@app.post("/api/task-card/{card_id}/done")
def api_task_card_done(card_id: str):
    """标记任务卡为已完成（被关联/用户确认）。"""
    return {"ok": handoff.mark_card_done(card_id)}


# ---- 6-9 批量资产提取（批3 用户裁定口径：只建卡不自动生成，单张出图走已有入口；批量生成留升版） ----

class AssetExtractPayload(BaseModel):
    """三档入口 scope：mode=full(全剧本)/episode/scene；episode/scene 需对应 eid/sid。"""
    mode: str = "full"
    eid: str = ""
    sid: str = ""
    model_id: str = ""
    gen_prompt: str = ""           # 建卡时的统一生成提示词补充（可空）
    prompt_by_kind: dict = {}      # 每类资产的提示词模板覆盖，如 {"characters":"..."}（可空）


def _asset_extract_scope(pid: str, payload: AssetExtractPayload) -> tuple:
    """校验项目 + mode/eid/sid，返回 (proj, eid, sid, scope_label)。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    mode = (payload.mode or "full").lower()
    eid, sid = payload.eid or "", payload.sid or ""
    if mode == "scene" and not sid:
        raise HTTPException(status_code=400, detail="mode=scene 需提供 sid")
    if mode == "episode" and not eid:
        raise HTTPException(status_code=400, detail="mode=episode 需提供 eid")
    scope_label = {"full": "全剧本", "scene": "本场"}.get(mode, f"本集({eid})")
    return proj, eid, sid, scope_label


@app.post("/api/projects/{pid}/asset-extract/aggregate")
def api_asset_extract_aggregate(pid: str, payload: AssetExtractPayload):
    """按三档 scope 聚合出场资产引用 → 与库比对分出 missing/existing（只读，不建卡不生成）。
    供前端"提取相关资产"先预览 missing、做"将创建 N 张卡"确认后再建卡。"""
    _proj, eid, sid, scope_label = _asset_extract_scope(pid, payload)
    r = handoff.aggregate_scene_assets(pid, eid, sid)
    return {**r, "mode": (payload.mode or "full").lower(), "scope_label": scope_label,
            "missing": r["missing"], "existing": r["existing"]}


@app.post("/api/projects/{pid}/asset-extract/cards")
def api_asset_extract_cards(pid: str, payload: AssetExtractPayload):
    """批量建卡（用户确认后）：把该范围 missing 资产一键建成库资产占位卡（含提示词+自动归属+来源场）。
    只建卡不生成图；单张出图由用户后续在资产卡上走已有入口（成本门见 generation_cost）。返回 created/skipped。
    """
    _proj, eid, sid, scope_label = _asset_extract_scope(pid, payload)
    r = handoff.aggregate_scene_assets(pid, eid, sid)
    if not r["missing"]:
        raise HTTPException(status_code=400,
                            detail="该范围无可新建资产（missing 为空，引用的名称-变体均已入库）")
    # prompt_by_kind：从 payload 来（前端可传默认提示词模板），逐类注入建卡
    result = handoff.ensure_missing_assets(pid, r["missing"],
                                           gen_prompt=payload.gen_prompt,
                                           prompt_by_kind=payload.prompt_by_kind or None)
    return {**result, "mode": (payload.mode or "full").lower(), "scope_label": scope_label,
            "scene_count": r["scene_count"]}


# ---- B2 成本门（生成单价估算；单价在 settings.generation_cost，见第三方方案。本批只挂单张生成入口） ----

@app.get("/api/generate-cost/image")
def api_generate_cost_image(count: int = 1, model: str = "comfyui_image"):
    """单张资产生图成本预估（按钮旁显示用；单价 0 或未启用确认时不显示/不弹窗）。"""
    return ai.estimate_image_cost(library.load_settings(), count=count, model=model)


@app.get("/api/generate-cost/video")
def api_generate_cost_video(seconds: float = 3.0, model: str = "h3_video"):
    """单场视频生成成本预估（按钮旁显示 + 确认弹窗用；现 H3 本地 0 元/秒）。"""
    return ai.estimate_video_cost(library.load_settings(), seconds=seconds, model=model)


# ---- 批量生成队列（升版备用，本批不接入前端；批量自动生成图=升版与视频队列同批） ----
# 底座 = batch_queue.py（data/batch/{batch_id}.json，item 状态机 pending→in_progress→done/failed/skipped）。
# 本批批量提取到"建卡"即完成；逐张出图走资产卡已有入口。批量自动逐张生成（next→出图→mark_done）留升版。

@app.get("/api/batches")
def api_list_batches(limit: int = 100):
    """列批量生成队列（升版用，本批暂无人消费）。"""
    return {"batches": batch_queue.list_batches(limit=limit)}


@app.get("/api/batch/{batch_id}")
def api_get_batch(batch_id: str):
    b = batch_queue.get_batch(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批次不存在")
    return b


@app.post("/api/batch/{batch_id}/next")
def api_batch_next(batch_id: str):
    """B3（升版）：取下一待生成项并置 in_progress。"""
    it = batch_queue.next_item(batch_id)
    if not it:
        b = batch_queue.get_batch(batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="批次不存在")
        return {"item": None, "batch_status": b.get("status")}
    return {"item": it, "batch_status": "in_progress"}


class BatchItemDonePayload(BaseModel):
    library_id: str = ""
    image_path: str = ""
    gen_prompt: str = ""
    ref_path: str = ""


@app.post("/api/batch/{batch_id}/item/{index}/done")
def api_batch_item_done(batch_id: str, index: int, payload: BatchItemDonePayload):
    """B4（升版）：出图落库成功后回调，回填并置 done。"""
    it = batch_queue.mark_item_done(batch_id, index, library_id=payload.library_id,
                                    image_path=payload.image_path, gen_prompt=payload.gen_prompt,
                                    ref_path=payload.ref_path)
    if not it:
        raise HTTPException(status_code=404, detail="批次或项不存在")
    return {"item": it, "batch": batch_queue.get_batch(batch_id)}


@app.post("/api/batch/{batch_id}/item/{index}/failed")
def api_batch_item_failed(batch_id: str, index: int, error: str = ""):
    """B7（升版）：出图失败回调。"""
    it = batch_queue.mark_item_failed(batch_id, index, error=error)
    if not it:
        raise HTTPException(status_code=404, detail="批次或项不存在")
    return {"item": it, "batch": batch_queue.get_batch(batch_id)}


@app.post("/api/batch/{batch_id}/item/{index}/skip")
def api_batch_item_skip(batch_id: str, index: int, reason: str = ""):
    """（升版）显式跳过一项。"""
    it = batch_queue.mark_item_skipped(batch_id, index, reason=reason)
    if not it:
        raise HTTPException(status_code=404, detail="批次或项不存在")
    return {"item": it, "batch": batch_queue.get_batch(batch_id)}


@app.post("/api/batch/{batch_id}/cancel")
def api_batch_cancel(batch_id: str):
    """（升版）取消批次。"""
    b = batch_queue.cancel_batch(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批次不存在")
    return b


@app.delete("/api/batch/{batch_id}")
def api_batch_delete(batch_id: str):
    """（升版）删除已终结批次。"""
    try:
        ok = batch_queue.delete_batch(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": ok}


# ---- 场视频生成任务（阶段 5，固定前缀：必须在 /api/{kind} 通配之前） ----

VIDEO_TASKS: dict = {}          # task_id -> 任务状态（内存态，服务重启即失效，够用）
_VIDEO_SERIAL = threading.Lock()  # Y2 结论：串行出片（防 HostBuffer 1450 并发冲突）


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


def _preflight_video_generate(payload: VideoGeneratePayload) -> None:
    """§十四 B-a 项② 生成前双保险：参考音频合规(2~15s/≥16kHz/文件存在) + 参考图存在 + 远程 ComfyUI 可达。
    不满足 → raise HTTPException(提示带具体不达标项)。fail-fast，不排队空跑（防 AudioVAE OOM 用超长音频再崩溃）。"""
    import os
    import requests
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
    # 3) 远程 ComfyUI 可达（视频必走 ComfyUI，不可达生成必失败）
    base = library.get_comfyui_url()
    try:
        r = requests.get(f"{base}/system_stats", timeout=8)
        if r.status_code != 200:
            bad.append(f"远程 ComfyUI 不可用（/system_stats 返回 {r.status_code}）")
    except Exception:
        bad.append("远程 ComfyUI 不可达（UU 端口映射未建立/未启动），请先确认 ComfyUI 在线或点🧹清显存后重试")
    if bad:
        raise HTTPException(status_code=400,
                            detail="生成前校验未通过，请先修正：\n• " + "\n• ".join(bad))


@app.post("/api/video/generate")
def api_video_generate(payload: VideoGeneratePayload):
    """提交场视频生成任务（异步）：唯一通道 = 直连 ComfyUI（v1.59 移除静默降级 IC 链）→ 归档 + 回填 Scene.videos。"""
    proj = storage.load_project(payload.pid)
    sc = storage.find_scene(proj, payload.eid, payload.sid) if proj else None
    if not proj or not sc:
        raise HTTPException(status_code=404, detail="场不存在")
    # §十四 B-a 项② 生成前双保险（音频合规/图存在/ComfyUI 可达，fail-fast）
    _preflight_video_generate(payload)
    task_id = library.new_id("vt_")
    VIDEO_TASKS[task_id] = {"status": "queued", "progress": "排队中…", "result": None, "error": ""}
    threading.Thread(target=_video_worker, args=(task_id, payload), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/video/task/{task_id}")
def api_video_task(task_id: str):
    """查询视频生成任务状态（前端轮询）。"""
    t = VIDEO_TASKS.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在（服务可能已重启）")
    return t


@app.get("/api/import/expert-prompt")
def api_expert_prompt():
    """剧本页"获取提示词"按钮：返回标准提示词文本（v1.32，单一升级源）。"""
    return {"prompt": IMPORT_EXPERT_PROMPT}


# ==================== 资产库体检（批4-3 重定位：只读巡检 + 仅修正悬空/坏 proj 引用） ====================
# 注：本块必须注册在通用 /api/{kind} 通配路由之前，否则 /api/library/audit 会被 {kind}=library 捕获 404。
# 定位：体检只做检测孤儿(仅列示) + 修正悬空 eps/场 与 坏 project 引用；不删除、不合并、不自动清理。
# 删卡由用户在资产页自行完成（资产页已补"被出场引用 → 拒删"拦截）。

class LibraryAuditCleanupAction(BaseModel):
    # fix_dangling: list[{id, drop_episodes[], drop_scenes[]}]  —— B 剔悬空 eps/场
    # fix_bad_proj: list[{id, drop_projects[]}]                 —— C 剔坏 proj
    fix_dangling: list = []
    fix_bad_proj: list = []


class LibraryAuditCleanupPayload(BaseModel):
    actions: LibraryAuditCleanupAction | None = None


@app.get("/api/library/audit")
def api_library_audit():
    """触发只读巡检，返回三库资产 scope 健康度快照（孤儿/悬空 eps/坏 proj），供前端只读报告页展示。"""
    snapshot = library_audit.audit(storage.load_projects_index, storage.load_project)
    return snapshot


@app.post("/api/library/audit/cleanup")
def api_library_audit_cleanup(payload: LibraryAuditCleanupPayload):
    """执行悬空/坏 proj 引用修正（先整体备份三库，服务端逐项复核判据）。不删孤儿、不合并。返回 {backup_path, applied, errors}。"""
    actions = (payload.actions.model_dump() if payload.actions else {})
    has_work = any((actions.get(k) or []) for k in ("fix_dangling", "fix_bad_proj"))
    if not has_work:
        return {"backup_path": "", "applied": {"fix_dangling": 0, "fix_bad_proj": 0},
                "errors": [{"op": "none", "reason": "无待修正项，未执行任何写操作"}]}
    result = library_audit.execute_cleanup(storage.load_projects_index, storage.load_project, actions)
    return result


@app.get("/api/{kind}")
def api_list_library(kind: str):
    _valid_kind(kind)
    return {"items": library.list_items(kind)}


@app.get("/api/{kind}/{item_id}")
def api_get_library_item(kind: str, item_id: str):
    _valid_kind(kind)
    return _get_asset(kind, item_id)


@app.post("/api/{kind}", status_code=201)
def api_create_asset(kind: str, payload: AssetPayload, response: Response):
    _valid_kind(kind)
    prefix = payload.prefix.strip()
    if not prefix:
        raise HTTPException(status_code=400, detail="名称不能为空")
    variant_name = payload.variant_name.strip()
    if kind == "characters" and not variant_name:
        variant_name = "基础形象"  # 人物至少 1 变体，默认"基础形象"（v1.25）
    asset = library.new_asset(kind, prefix, variant_name, payload.scopes,
                              payload.prompt.strip(), payload.images or [],
                              payload.voice_text.strip(), payload.audios or [],
                              payload.voice_source or "text", payload.voice or "")
    # 6-13：保存前 fail-fast 校验（阻断 errors → 400 字段级；仅 warnings → 经 header 附带软提示）
    _warnings = _enforce_asset_validation(kind, asset)
    library.save_item(kind, asset)
    # 第6批步骤②：若新建即带图(罕见，通常后传/归类)，刷引用计数
    if asset.get("images"):
        _sync_asset_refs(kind, asset["id"])
    if _warnings:
        # 6-13 修复：header 只接受 latin-1；中文若 ensure_ascii=False 直接写入会 UnicodeEncodeError→500。
        # ensure_ascii=True 产出 \uXXXX 全 ASCII，前端 JSON.parse 自动还原中文。
        response.headers["X-Asset-Warnings"] = json.dumps(_warnings, ensure_ascii=True)
    return asset


def _image_paths_of(asset: dict | None) -> list:
    """收集资产卡 images[] 的相对 SharedAssets path（第6批引用计数刷新的受影响图集合）。"""
    if not asset:
        return []
    out = []
    for im in asset.get("images", []) or []:
        p = im.get("path") if isinstance(im, dict) else im
        if p:
            out.append(str(p))
    return out


def _sync_asset_refs(kind: str, item_id: str, extra_paths: list = ()) -> dict:
    """第6批步骤②：资产删/改/加图后，对该资产涉及的图(新旧 images)刷新引用计数。
    局部兜底（refresh_specific_refs only_these=True）：以当前全量 scan 为准，只刷受影响图。"""
    try:
        asset = library.get_item(kind, item_id)
        cur = _image_paths_of(asset)
        return assets_index.refresh_specific_refs(
            list(dict.fromkeys(list(cur) + list(extra_paths))),
            storage.load_projects_index, storage.load_project)
    except Exception:
        return {"fixed": 0, "unchanged": 0, "changed_paths": [], "log": []}


@app.put("/api/{kind}/{item_id}")
def api_update_asset(kind: str, item_id: str, payload: AssetPayload, response: Response):
    _valid_kind(kind)
    item = _get_asset(kind, item_id)
    old_paths = _image_paths_of(item)   # 第6批：保存前快照旧 images，便于 diff 刷新引用
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
            item[k] = v
    if not item.get("prefix"):
        raise HTTPException(status_code=400, detail="名称不能为空")
    if kind == "characters" and not item.get("variant_name"):
        item["variant_name"] = "基础形象"
    item["updated_at"] = library.now_iso()
    # 6-13：保存前 fail-fast 校验（阻断 errors → 400 字段级；仅 warnings → header）
    _warnings = _enforce_asset_validation(kind, item)
    library.save_item(kind, item)
    # 第6批步骤②：图变更 → 刷该资产新旧 images 的引用计数（归类/换图/删图的落点）
    _sync_asset_refs(kind, item_id, old_paths)
    if _warnings:
        # 6-13 修复：header 只接受 latin-1；中文若 ensure_ascii=False 直接写入会 UnicodeEncodeError→500。
        # ensure_ascii=True 产出 \uXXXX 全 ASCII，前端 JSON.parse 自动还原中文。
        response.headers["X-Asset-Warnings"] = json.dumps(_warnings, ensure_ascii=True)
    return item


@app.delete("/api/{kind}/{item_id}")
def api_delete_library_item(kind: str, item_id: str):
    _valid_kind(kind)
    asset = library.get_item(kind, item_id)
    if not asset:
        raise HTTPException(status_code=404, detail="条目不存在")
    # 批4-3：被出场引用 → 拒删（引用数=0 才可删）。同 ref-stats 语义：命中任意在案场即视为被引用。
    # 孤儿(projects=[])不命中任何场 → 可删，符合"删卡回归资产页"定位。
    hits = _asset_hit_nodes(asset.get("scopes", {}) or {}, _scene_tree_nodes())
    if hits:
        scenes = [{"project": h["project"], "episode": h["episode"], "scene": h["scene"]} for h in hits]
        raise HTTPException(status_code=409, detail={
            "msg": f"该资产被 {len(hits)} 个场次引用，不可删除。请先解除其在相关场次/项目中的归属。",
            "referenced": True,
            "count": len(hits),
            "scenes": scenes,
        })
    if not library.delete_item(kind, item_id):
        raise HTTPException(status_code=404, detail="条目不存在")
    # v1.53：参考类资产引用源维护——资产删除→相关参考类计数-1、引用源[]移除该资产
    try:
        library.dec_reference_for_asset(item_id)
    except Exception:
        pass
    # 第6批步骤②：资产删除 → 其旧 images 失引用，刷这些图的引用计数（仍被场 thumbnail 引用的截帧图不清零）
    _sync_asset_refs(kind, item_id, _image_paths_of(asset))
    return {"deleted": item_id}


@app.post("/api/{kind}/{item_id}/duplicate")
def api_duplicate_asset(kind: str, item_id: str):
    """创建资产副本（v1.26）：新 id，完整复制 图片版本/提示词/归属/音色音频，变体名加"(副本)"。"""
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    import copy
    new = copy.deepcopy(asset)
    new["id"] = library.new_id(library.KINDS[kind]["id_prefix"])
    new["variant_name"] = (asset.get("variant_name") or "基础形象") + "（副本）"
    new["created_at"] = library.now_iso()
    new["updated_at"] = library.now_iso()
    library.save_item(kind, new)
    # 第6批步骤②：副本复制 images → 这些图被多一个资产引用，刷新引用计数（原资产+副本各计）
    _sync_asset_refs(kind, new["id"])
    return new


# ---- 图版本（上传 / 绑定资产库 / 移除 / 切换联动由前端处理；资产即"名称-变体名称"，v1.25） ----

@app.post("/api/{kind}/{item_id}/image-upload")
async def api_asset_image_upload(kind: str, item_id: str, file: UploadFile = File(...)):
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    sub = Path("drama") / kind / asset["id"]
    d = Path(library.get_shared_assets_path()) / sub
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{library.new_id()}{ext}"
    (d / fname).write_bytes(raw)
    rec = {"path": (sub / fname).as_posix(), "version": "", "gen_prompt": "", "source": "upload", "added_at": library.now_iso()}
    asset.setdefault("images", []).append(rec)
    library.save_item(kind, asset)
    _sync_asset_refs(kind, item_id)   # 第6批步骤②：加图后刷引用
    return rec


class ImageBindPayload(BaseModel):
    path: str = ""
    gen_prompt: str = ""


@app.post("/api/{kind}/{item_id}/image-bind")
def api_asset_image_bind(kind: str, item_id: str, payload: ImageBindPayload):
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    if not payload.path.strip():
        raise HTTPException(status_code=400, detail="请选择资产库图片")
    rec = {"path": payload.path.strip(), "version": "", "gen_prompt": payload.gen_prompt.strip(),
           "source": "bind", "added_at": library.now_iso()}
    asset.setdefault("images", []).append(rec)
    library.save_item(kind, asset)
    _sync_asset_refs(kind, item_id, [payload.path.strip()])   # 第6批步骤②：绑已有图→刷新引用
    return rec


@app.delete("/api/{kind}/{item_id}/images/{idx}")
def api_asset_image_delete(kind: str, item_id: str, idx: int):
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    if idx < 0 or idx >= len(asset.get("images", [])):
        raise HTTPException(status_code=404, detail="图片不存在")
    removed = asset["images"].pop(idx)
    library.save_item(kind, asset)
    _sync_asset_refs(kind, item_id, _image_paths_of([{"path": (removed.get("path") if isinstance(removed, dict) else removed)}]) if removed else [])  # 第6批步骤②：删图→旧图失引用
    return {"deleted": idx}


# ---- 人物音色试听与参考音频（4.12，v1.22/1.23，仅人物资产） ----

class TtsPayload(BaseModel):
    voice: str = "zh-CN-XiaoxiaoNeural"
    text: str = ""


@app.post("/api/{kind}/{item_id}/tts")
async def api_asset_tts(kind: str, item_id: str, payload: TtsPayload):
    """edge-tts 生成试听音频（约 5 秒 mp3，存 SharedAssets\audio\）。
    试听为一次性试听（前端直接播放），不写入资产 audios 列表（参考音频才持久化）。"""
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    text = (payload.text or "").strip() or (asset.get("voice_text") or "").strip() or asset.get("prefix", "")
    if not text:
        raise HTTPException(status_code=400, detail="无可朗读文本（请填写音色文本或提供文本）")
    voice = payload.voice or "zh-CN-XiaoxiaoNeural"
    try:
        import edge_tts
    except ImportError:
        raise HTTPException(status_code=500, detail="edge-tts 未安装")
    audio_dir = Path(library.get_shared_assets_path()) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    fname = f"trial_{asset['id']}_{library.new_id()}.mp3"
    out = audio_dir / fname
    try:
        comm = edge_tts.Communicate(text, voice)
        await comm.save(str(out))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听生成失败（需联网）：{e}")
    return {"path": f"audio/{fname}", "type": "trial", "voice": voice}


@app.post("/api/{kind}/{item_id}/audio-upload")
async def api_asset_audio_upload(kind: str, item_id: str, file: UploadFile = File(...)):
    """参考音频上传（存 SharedAssets\audio\）。
    §十四 B-a 项③：接入 asset_validator 单文件合规校验，不合格 400 拒绝且不挂到资产（不静默放行超长音频）。"""
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    ext = Path(file.filename or "ref.mp3").suffix.lower() or ".mp3"
    audio_dir = Path(library.get_shared_assets_path()) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    fname = f"ref_{asset['id']}_{library.new_id()}{ext}"
    fp = audio_dir / fname
    fp.write_bytes(raw)
    v = asset_validator.validate_audio_file_upload(fp)
    if not v.get("ok"):
        fp.unlink(missing_ok=True)  # 不合规不入库，清除文件
        raise HTTPException(status_code=400, detail=v.get("error") or "音频不合规")
    rec = {"path": f"audio/{fname}", "type": "reference", "voice": "", "added_at": library.now_iso()}
    asset.setdefault("audios", []).append(rec)
    library.save_item(kind, asset)
    return rec


@app.delete("/api/{kind}/{item_id}/audios/{idx}")
def api_asset_audio_delete(kind: str, item_id: str, idx: int):
    _valid_kind(kind)
    asset = _get_asset(kind, item_id)
    if idx < 0 or idx >= len(asset.get("audios", [])):
        raise HTTPException(status_code=404, detail="音频不存在")
    asset["audios"].pop(idx)
    library.save_item(kind, asset)
    return {"deleted": idx}


# ==================== AI 拆解 API（阶段 4） ====================

def _compose_scene_text(shots: list) -> str:
    """将拆解/导入输出的镜头数组拼装为场文本（v1.34：镜=场文本内纯文本段，
    "镜1：…镜2：…"不自动编号不重排；时长/运镜/景别转行内尖括号标记保留在文本中）。"""
    lines = []
    for i, sh in enumerate(shots, 1):
        parts = []
        desc = (sh.get("desc") or "").strip()
        dialogue = (sh.get("dialogue") or "").strip()
        narration = (sh.get("narration") or "").strip()
        if desc:
            parts.append(desc)
        if dialogue:
            parts.append("台词：" + dialogue)
        if narration:
            parts.append("旁白：" + narration)
        line = "，".join(parts)
        tags = ""
        try:
            dur = int(sh.get("duration") or 0)
            if dur > 0:
                tags += f"<duration-ms>{dur * 1000}</duration-ms>"
        except (TypeError, ValueError):
            pass
        cam = (sh.get("camera_move") or sh.get("camera") or "").strip()
        if cam:
            tags += f"<camera-move>{cam}</camera-move>"
        size = (sh.get("shot_size") or "").strip()
        if size:
            tags += f"<shot-size>{size}</shot-size>"
        body = (line + tags).strip()
        lines.append(f"镜{i}：{body}" if body else f"镜{i}")
    return "\n".join(lines)

class AnalyzeCostPayload(BaseModel):
    model_id: str | None = None


@app.post("/api/projects/{pid}/analyze-cost")
def api_analyze_cost(pid: str, payload: AnalyzeCostPayload):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    model = _require_model(payload.model_id)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    est = ai.estimate_cost(len(proj.get("script") or ""), model)
    return {**est, "model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}


class AnalyzePayload(BaseModel):
    model_id: str | None = None


@app.post("/api/projects/{pid}/analyze")
def api_analyze(pid: str, payload: AnalyzePayload):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    script = (proj.get("script") or "").strip()
    if not script:
        raise HTTPException(status_code=400, detail="剧本为空，请先在剧本页填写剧本")
    model = _require_model(payload.model_id)
    # v1.37 自适应拆解：短剧本（< analyze_threshold 字，默认 8000）整本一次；长剧本按集分批
    # v1.40：draft 带 total（供前端进度显示）；逐集拆解即时写盘（前端轮询显示"正在拆解第 N/M 集"）
    threshold = int((library.load_settings().get("analyze_threshold") or 8000))
    draft = {"model_id": model.get("id"), "model_name": model.get("name"),
             "model": model.get("model"), "created_at": storage.now_iso(), "episodes": []}

    def _flush():
        draft_store.save_draft(pid, draft)

    if len(script) < threshold:
        draft["mode"] = "whole"
        draft["total"] = 1
        _flush()   # v1.41：先写初始态（mode/total），前端轮询从第一刻即可识别"整本一次"
        try:
            data = ai.analyze_script(model, script)
            draft["episodes"].append({"idx": 0, "title": "整本", "status": "ok", "data": data})
        except Exception as e:
            draft["episodes"].append({"idx": 0, "title": "整本", "status": "error", "error": str(e)})
        _flush()
    else:
        episodes = ai.split_episodes(script)
        if not episodes:
            raise HTTPException(status_code=400, detail="未能从剧本切分集数")
        draft["mode"] = "batch"
        draft["total"] = len(episodes)
        _flush()   # v1.41：先写初始态（mode/total），前端轮询可显示"正在拆解第 0/N 集"
        for i, ep in enumerate(episodes):
            try:
                data = ai.analyze_episode(model, ep)
                draft["episodes"].append({"idx": i, "title": ep["title"], "status": "ok", "data": data})
            except Exception as e:
                draft["episodes"].append({"idx": i, "title": ep["title"], "status": "error", "error": str(e)})
            _flush()   # 逐集写盘，前端轮询显示进度（v1.40 ①）
    return draft


@app.get("/api/projects/{pid}/drafts")
def api_get_drafts(pid: str):
    draft = draft_store.load_draft(pid)
    if draft is None:
        return {"episodes": [], "mode": "", "total": 0}   # v1.41：mode 空供前端显示中性"正在拆解…"兜底
    return draft


class RetryPayload(BaseModel):
    episode_idx: int = 0
    model_id: str | None = None


@app.post("/api/projects/{pid}/drafts/retry")
def api_retry(pid: str, payload: RetryPayload):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    if not draft_store.draft_exists(pid):
        raise HTTPException(status_code=404, detail="无拆解草案")
    draft = draft_store.load_draft(pid)
    if draft is None:
        raise HTTPException(status_code=404, detail="无拆解草案")
    if payload.episode_idx >= len(draft.get("episodes", [])):
        raise HTTPException(status_code=404, detail="集索引不存在")
    model = _require_model(payload.model_id)
    episodes = ai.split_episodes(proj.get("script") or "")
    ep_def = episodes[payload.episode_idx] if payload.episode_idx < len(episodes) else None
    if not ep_def:
        raise HTTPException(status_code=400, detail="剧本与草案集数不一致")
    ep = draft["episodes"][payload.episode_idx]
    try:
        data = ai.analyze_episode(model, ep_def)
        ep.update({"status": "ok", "data": data, "error": ""})
    except Exception as e:
        ep.update({"status": "error", "error": str(e)})
    draft_store.save_draft(pid, draft)
    return draft


@app.post("/api/projects/{pid}/drafts/apply")
def api_apply_draft(pid: str):
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    if not draft_store.draft_exists(pid):
        raise HTTPException(status_code=404, detail="无拆解草案")
    draft = draft_store.load_draft(pid)
    if draft is None:
        raise HTTPException(status_code=404, detail="无拆解草案")
    # v1.55 P0-6 防御：草案无成功拆解结果时拒绝 apply（防空草稿覆盖清空项目结构——P0-5 回归曾实测触发）
    ok_eps = [e for e in draft.get("episodes", []) if e.get("status") == "ok"]
    if not ok_eps:
        raise HTTPException(status_code=400, detail="草案无成功拆解结果（episodes 为空或全部失败），未做任何修改——请先重新拆解")

    # 1) 资产 → 全局库（先建，拿 name→id 映射供结构引用；v1.31：按"名称-变体名称"去重）
    # v1.59 幂等（治本 7-3 根因D，2026-09-06）：重复 apply 前按 pid 清理上次 apply 残留同名-变体孤儿卡，
    #   并复用当前生效卡（保留其已绑主图/音色），保证"同一项目同一(名称,变体) 全局只一张卡"——
    #   防 apply 不幂等重复建卡 → 两套同名卡 → resolve 全库 next() 命中残留无资卡 → H3 无参考乱语/场景错。
    #   判定口径：scope.projects 含 pid 且同 (prefix,variant) 的存量卡；
    #     episodes 命中"当前项目现存集 id"(live) 者 = 当前生效卡 → 复用不重建；
    #     其余(episodes 空或全指向已废弃集) = 上次 apply 残留孤儿 → 先删再落，杜绝累积。
    live_ep_ids = {e.get("id") for e in proj.get("episodes", []) if e.get("id")}
    created_ids = {kind: [] for kind in library.KINDS}
    char_id, scene_id, prop_id = {}, {}, {}
    for kind in library.KINDS:
        seen = {}   # (name, variant) -> {item, ep_ids:set}
        for ep in draft.get("episodes", []):
            if ep.get("status") != "ok":
                continue
            for it in (ep.get("data") or {}).get(kind) or []:
                key = (it.get("name") or "", it.get("variant_name") or "")
                if key not in seen:
                    seen[key] = {"item": it, "ep_ids": set()}
                seen[key]["ep_ids"].add(ep["idx"])
        for (name, vname), rec in seen.items():
            it = rec["item"]
            norm_v = vname or "基础形象"
            existing = [c for c in library.list_items(kind)
                        if (c.get("prefix") or "") == name
                        and ((c.get("variant_name") or "基础形象") == norm_v)
                        and pid in ((c.get("scopes") or {}).get("projects") or [])]
            live_cards = [c for c in existing
                          if any(e in live_ep_ids for e in ((c.get("scopes") or {}).get("episodes") or []))]
            orphan_cards = [c for c in existing if c not in live_cards]
            # 先清理上次 apply 残留孤儿卡（重复 apply 的病灶产物，根治不再累积）
            for oc in orphan_cards:
                library.delete_item(kind, oc.get("id"))
                _log.info("[apply_draft] pid=%s apply 幂等：清理上次残留孤儿卡 kind=%s id=%s %s-%s",
                          pid, kind, oc.get("id"), name, norm_v)
            # 复用当前生效卡（保留用户已绑主图/音色等成果），不重复建卡
            if live_cards:
                target = {"characters": char_id, "scenes": scene_id, "props": prop_id}[kind]
                target[name] = live_cards[0]["id"]
                continue
            # P0-b（批4，2026-09-03）：资产创建即写 scopes.projects=[pid]，episodes 留待步骤 3 回填——
            # 防 projects 归属因中间失败而丢失（诊断报告缺陷4：原到 L2095 才回填 projects，中途失败即产 projects=[] 孤儿）
            a = library.new_asset(kind, name, vname,
                                  prompt=(it.get("prompt") or "").strip(),
                                  voice_text=(it.get("voice_text") or "").strip(),
                                  scopes={"projects": [pid], "episodes": [], "scenes": []})
            library.save_item(kind, a)
            created_ids[kind].append(a["id"])
            target = {"characters": char_id, "scenes": scene_id, "props": prop_id}[kind]
            target[name] = a["id"]

    # 2) 拆解结构 → 项目（v1.34：集→场（title+text），镜头数组合并拼装为场文本，不再建 Shot 实体）
    new_episodes = []
    ep_idx_to_id = {}   # 草案集序号(idx) → 集 id
    scene_count = 0
    shot_count = 0      # 镜文本段计数（汇总展示用）
    for ep in draft.get("episodes", []):
        if ep.get("status") != "ok":
            continue
        d = ep.get("data") or {}
        for s in d.get("structure") or []:
            ep_obj = storage.new_episode(proj, s.get("episode_title") or "第 1 集")
            ep_obj["summary"] = (s.get("summary") or "").strip()
            for sc in s.get("scenes") or []:
                sc_obj = storage.new_scene(ep_obj, sc.get("scene_title") or "场景")
                sc_obj["text"] = _compose_scene_text(sc.get("shots") or [])
                shot_count += len(sc.get("shots") or [])
                scene_count += 1
                ep_obj["scenes"].append(sc_obj)
            new_episodes.append(ep_obj)
            ep_idx_to_id[ep["idx"]] = ep_obj["id"]
    proj["episodes"] = new_episodes

    # 3) 回填资产归属（v1.31：项目 + 出现集 id 列表）
    # P0-a（批4，2026-09-03）：回填时查找不到资产不再"静默 continue"，改记录错误日志 + 累计到 unlinked 返回告警，
    # 杜绝"资产创建失败/draft 仍记 ID 但 scope 回填失败"的不可观测静默失败（诊断报告缺陷2）。
    unlinked = []   # 回填归属失败、未挂上 scope 的资产 [(kind, name, vname, reason)]
    for kind in library.KINDS:
        seen = {}   # (name, variant) -> ep_ids
        for ep in draft.get("episodes", []):
            if ep.get("status") != "ok":
                continue
            for it in (ep.get("data") or {}).get(kind) or []:
                key = (it.get("name") or "", it.get("variant_name") or "")
                if key not in seen:
                    seen[key] = set()
                seen[key].add(ep["idx"])
        for (name, vname), ep_idxs in seen.items():
            aid = {"characters": char_id, "scenes": scene_id, "props": prop_id}[kind].get(name)
            item = library.get_item(kind, aid)
            if not item:
                reason = f"created_id_missing(key={name!r}/{vname!r}, mapped_id={aid!r})"
                _log.error("[apply_draft] pid=%s scope 回填失败：资产查找不到，可能已被外部删除或 ID 映射异常 — %s", pid, reason)
                unlinked.append({"kind": kind, "name": name, "variant_name": vname, "reason": reason})
                continue
            ep_ids = [ep_idx_to_id[i] for i in ep_idxs if i in ep_idx_to_id]
            item["scopes"] = {"projects": [pid], "episodes": ep_ids, "scenes": []}
            library.save_item(kind, item)

    # 4) 剧本摘要：本地拼接兜底（v1.31：LLM 未返回时退化为各集摘要拼接，字段永不为空）
    ep_summaries = [{"title": e.get("title"), "summary": e.get("summary")} for e in new_episodes]
    summary = ai.local_summary_fallback(ep_summaries)
    if summary:
        proj["summary"] = summary

    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)

    # 5) 草案记录本次创建资产（供"删除本次拆解全部资产"cleanup 使用）
    draft["applied_at"] = storage.now_iso()
    draft["created_asset_ids"] = created_ids
    draft_store.save_draft(pid, draft)

    applied = {
        "characters": len(created_ids["characters"]),
        "scenes": len(created_ids["scenes"]),
        "props": len(created_ids["props"]),
        "episodes": len(new_episodes),
        "scenes_count": scene_count,   # v1.34：场次数（最小粒度）
        "shots": shot_count,           # 镜文本段计数（兼容旧前端展示，4-13 前端改"场文本段"）
    }
    # P0-a：scope 回填失败告警（非空才附带，不破坏既有字段；前端可据 unlinked 提示存在未挂归属资产）
    if unlinked:
        applied["unlinked"] = unlinked
        _log.warning("[apply_draft] pid=%s scope 回填失败 %d 项：%s", pid, len(unlinked), [u["name"] for u in unlinked])
    return {"applied": applied}


@app.post("/api/projects/{pid}/drafts/cleanup")
def api_cleanup_draft(pid: str):
    """删除本次拆解/导入创建的全部资产（v1.30 ⑤ / v1.31 / v1.40 ② 范围修正）：
    范围=仅资产库中的资产（角色/场景/道具），**不得涉及项目结构（集/场/镜文本）**——
    剧本大纲/剧集/场结构保留；draft 记录保留（幂等，资产已删后再次清理自动跳过）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    removed = {"characters": 0, "scenes": 0, "props": 0}
    # 拆解路径：draft.created_asset_ids；导入路径：proj.imported_asset_ids
    id_sets = {kind: set() for kind in library.KINDS}
    draft = draft_store.load_draft(pid)
    if draft:
        created = draft.get("created_asset_ids") or {}
        for kind in library.KINDS:
            id_sets[kind].update(created.get(kind) or [])
    for kind in library.KINDS:
        id_sets[kind].update(proj.get("imported_asset_ids", {}).get(kind) or [])
    for kind in library.KINDS:
        for aid in id_sets[kind]:
            if library.delete_item(kind, aid):
                removed[kind] += 1
    # v1.40 ②：仅清空已删资产的记录，**保留项目 episodes/summary（集/场结构不受影响）**
    proj["imported_asset_ids"] = {"characters": [], "scenes": [], "props": []}
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    return {"removed": removed, "structure_kept": True}


class RegenerateSummaryPayload(BaseModel):
    model_id: str | None = None


@app.post("/api/projects/{pid}/summary/regenerate")
def api_regenerate_summary(pid: str, payload: RegenerateSummaryPayload):
    """重新生成剧本摘要（v1.31：以当前各集摘要为输入 LLM 汇总；失败回退本地拼接）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    ep_summaries = [{"title": e.get("title"), "summary": e.get("summary")} for e in proj.get("episodes", [])]
    if not any(s.get("summary") for s in ep_summaries):
        raise HTTPException(status_code=400, detail="尚无各集摘要，请先完成拆解或导入")
    model = _require_model(payload.model_id)
    llm_text = ai.summarize_script(model, ep_summaries)
    text = llm_text or ai.local_summary_fallback(ep_summaries)
    proj["summary"] = text
    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    return {"summary": text, "fallback": not bool(llm_text)}


@app.post("/api/projects/{pid}/summary/estimate")
def api_summary_estimate(pid: str, payload: RegenerateSummaryPayload):
    """重新生成剧本摘要的成本预估（各集摘要字数 → token/费用）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    model = _require_model(payload.model_id)
    chars = sum(len((e.get("summary") or "")) for e in proj.get("episodes", []))
    est = ai.estimate_cost(chars, model)
    return {**est, "model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}


# ==================== 提取本集资产（v1.38：LLM 从该集摘要文本提取，非聚合已有归属） ====================

class ExtractAssetsPayload(BaseModel):
    model_id: str | None = None


def _get_episode_or_404(pid: str, eid: str) -> dict:
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    ep = storage.find_episode(proj, eid) if proj else None
    if not ep:
        raise HTTPException(status_code=404, detail="集不存在")
    return ep


@app.post("/api/projects/{pid}/episodes/{eid}/extract-assets/estimate")
def api_extract_assets_estimate(pid: str, eid: str, payload: ExtractAssetsPayload):
    """提取本集资产成本预估（v1.38：以该集当前摘要文本为输入）。"""
    ep = _get_episode_or_404(pid, eid)
    summary = (ep.get("summary") or "").strip()
    if not summary:
        raise HTTPException(status_code=400, detail="该集暂无剧情摘要（请先填写/保存摘要后提取）")
    model = _require_model(payload.model_id)
    est = ai.estimate_cost(len(summary), model)
    return {**est, "model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}


@app.post("/api/projects/{pid}/episodes/{eid}/extract-assets")
def api_extract_assets(pid: str, eid: str, payload: ExtractAssetsPayload):
    """LLM 从该集当前摘要文本提取该集资产 → 按"名称-变体"去重 → 直接入库（scopes.projects+episodes 写该集归属）。
    v1.38：不是聚合该集已有归属资产；不因"暂无归属资产"返回空。"""
    ep = _get_episode_or_404(pid, eid)
    summary = (ep.get("summary") or "").strip()
    if not summary:
        raise HTTPException(status_code=400, detail="该集暂无剧情摘要（请先填写/保存摘要后提取）")
    model = _require_model(payload.model_id)
    try:
        data = ai.extract_assets(model, summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail="LLM 提取失败：" + str(e))
    created = {"characters": 0, "scenes": 0, "props": 0}
    skipped = 0
    kind_map = {"character": "characters", "scene": "scenes", "prop": "props"}
    for a in (data.get("assets") or []):
        kind = kind_map.get(str(a.get("kind") or "").lower())
        name = str(a.get("name") or "").strip()
        if not kind or not name:
            skipped += 1
            continue
        variant = str(a.get("variant_name") or "").strip() or ("基础形象" if kind == "characters" else "")
        existing = [it for it in library.list_items(kind)
                    if it.get("prefix") == name and it.get("variant_name") == variant]
        if existing:
            skipped += 1
            continue
        asset = library.new_asset(kind, name, variant,
                                  prompt=str(a.get("prompt") or "").strip(),
                                  voice_text=str(a.get("voice_text") or "").strip())
        asset["scopes"] = {"projects": [pid], "episodes": [eid], "scenes": []}
        library.save_item(kind, asset)
        created[kind] += 1
    return {"extracted": created, "skipped": skipped}


# ==================== 通用 AI 流式端点（v1.51：LLM 调用流式输出，成本弹窗同窗逐步显示生成内容） ====================

class AIStreamRequest(BaseModel):
    task: str = ""            # optimize_prompt / recommend_scopes / summarize / extract_assets / analyze
    text: str = ""            # optimize_prompt：待优化文本
    kind: str = ""            # recommend_scopes：资产 kind（characters/scenes/props）
    item_id: str = ""         # recommend_scopes：资产 id
    pid: str = ""             # summarize / extract_assets / analyze：项目 id
    eid: str = ""             # extract_assets：集 id
    model_id: str = ""


def _sse(event: str, data: dict):
    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/ai/stream")
def api_ai_stream(payload: AIStreamRequest):
    """通用 LLM 流式端点（SSE）：meta → 逐 delta（LLM 生成增量文本）→ result / error。
    前端 fetch + ReadableStream 逐步读取，成本确认弹窗同窗显示生成内容（v1.51）。"""
    def gen():
        q: "queue.Queue" = queue.Queue()

        def on_delta(t):
            q.put(("delta", t))

        def worker():
            try:
                model = ai.get_model(payload.model_id)
                if not model:
                    q.put(("error", "尚未配置 LLM 模型：请到 设置页 添加模型并填写 API Key（保存后生效）"))
                    return
                q.put(("meta", {"model_id": model.get("id"), "model_name": model.get("name"), "model": model.get("model")}))
                task = payload.task
                if task == "optimize_prompt":
                    text = (payload.text or "").strip()
                    if not text:
                        q.put(("error", "无可优化的文本")); return
                    optimized = ai.optimize_prompt(model, text, on_delta=on_delta)
                    q.put(("result", {"optimized": optimized}))
                elif task == "recommend_scopes":
                    asset = _get_asset(payload.kind, payload.item_id)
                    desc = f"{asset.get('prefix') or ''}-{asset.get('variant_name') or ''}：{(asset.get('prompt') or '')[:200]}"
                    data = ai.recommend_scopes(model, desc, _scope_context(), on_delta=on_delta)
                    recs = []
                    for r in (data.get("recommendations") or []):
                        level = str(r.get("level") or "")
                        if level not in ("project", "episode", "scene"):
                            continue
                        rid = str(r.get("id") or "")
                        if rid:
                            recs.append({"level": level, "id": rid, "reason": str(r.get("reason") or "").strip()})
                    q.put(("result", {"recommendations": recs}))
                elif task == "summarize":
                    proj = storage.load_project(payload.pid)
                    if not proj:
                        q.put(("error", "项目不存在")); return
                    ep_summaries = [{"title": e.get("title"), "summary": e.get("summary")} for e in proj.get("episodes", [])]
                    if not any(s.get("summary") for s in ep_summaries):
                        q.put(("error", "尚无各集摘要，请先完成拆解或导入")); return
                    llm_text = ai.summarize_script(model, ep_summaries, on_delta=on_delta)
                    text = llm_text or ai.local_summary_fallback(ep_summaries)
                    proj["summary"] = text
                    proj["updated_at"] = storage.now_iso()
                    storage.save_project(proj)
                    q.put(("result", {"summary": text, "fallback": not bool(llm_text)}))
                elif task == "extract_assets":
                    ep = _get_episode_or_404(payload.pid, payload.eid)
                    summary = (ep.get("summary") or "").strip()
                    if not summary:
                        q.put(("error", "该集暂无剧情摘要")); return
                    data = ai.extract_assets(model, summary, on_delta=on_delta)
                    created = {"characters": 0, "scenes": 0, "props": 0}
                    skipped = 0
                    kind_map = {"character": "characters", "scene": "scenes", "prop": "props"}
                    for a in (data.get("assets") or []):
                        kind = kind_map.get(str(a.get("kind") or "").lower())
                        name = str(a.get("name") or "").strip()
                        if not kind or not name:
                            skipped += 1; continue
                        variant = str(a.get("variant_name") or "").strip() or ("基础形象" if kind == "characters" else "")
                        existing = [it for it in library.list_items(kind)
                                    if it.get("prefix") == name and it.get("variant_name") == variant]
                        if existing:
                            skipped += 1; continue
                        asset = library.new_asset(kind, name, variant,
                                                  prompt=str(a.get("prompt") or "").strip(),
                                                  voice_text=str(a.get("voice_text") or "").strip())
                        asset["scopes"] = {"projects": [payload.pid], "episodes": [payload.eid], "scenes": []}
                        library.save_item(kind, asset)
                        created[kind] += 1
                    q.put(("result", {"extracted": created, "skipped": skipped}))
                elif task == "analyze":
                    # v1.51 流式拆解：复用自适应逻辑（whole/batch）+ drafts 写盘；LLM 增量流式输出
                    proj = storage.load_project(payload.pid)
                    if not proj:
                        q.put(("error", "项目不存在")); return
                    script = (proj.get("script") or "").strip()
                    if not script:
                        q.put(("error", "剧本为空，请先导入剧本")); return
                    threshold = int((library.load_settings().get("analyze_threshold") or 8000))
                    draft = {"model_id": model.get("id"), "model_name": model.get("name"),
                             "model": model.get("model"), "created_at": storage.now_iso(), "episodes": []}
                    def _flush():
                        draft_store.save_draft(payload.pid, draft)
                    if len(script) < threshold:
                        draft["mode"] = "whole"; draft["total"] = 1; _flush()
                        try:
                            data = ai.analyze_script(model, script, on_delta=on_delta)
                            draft["episodes"].append({"idx": 0, "title": "整本", "status": "ok", "data": data})
                        except Exception as e:
                            draft["episodes"].append({"idx": 0, "title": "整本", "status": "error", "error": str(e)})
                        _flush()
                    else:
                        episodes = ai.split_episodes(script)
                        if not episodes:
                            q.put(("error", "未能从剧本切分集数")); return
                        draft["mode"] = "batch"; draft["total"] = len(episodes); _flush()
                        for i, ep in enumerate(episodes):
                            try:
                                data = ai.analyze_episode(model, ep, on_delta=on_delta)
                                draft["episodes"].append({"idx": i, "title": ep["title"], "status": "ok", "data": data})
                            except Exception as e:
                                draft["episodes"].append({"idx": i, "title": ep["title"], "status": "error", "error": str(e)})
                            _flush()
                    q.put(("result", draft))
                else:
                    q.put(("error", "未知任务：" + str(task)))
            except Exception as e:
                q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        while True:
            ev, data = q.get()
            if ev == "delta":
                yield from _sse("delta", {"text": data})
            elif ev == "meta":
                yield from _sse("meta", data)
            elif ev == "result":
                yield from _sse("result", data)
                break
            else:
                yield from _sse("error", {"error": data})
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


# ==================== 外部视频专家导入（v1.31/1.32：免 AI 拆解） ====================

IMPORT_EXPERT_PROMPT = """【角色】你是资深的短剧编剧与剧本拆解专家。

【任务】基于我们对话中的这部剧本，完成以下工作，并把全部结果作为一个 JSON 对象输出。只输出 JSON，不要输出任何其他解释文字，不要生成图片/音频/视频：

0. 剧本原文：把剧本原文原样放入 script_text 字段（完整内容，不要删改）。
1. 剧本摘要：用 3-5 句话概括整部剧的主题、主线剧情、核心冲突与结局走向，放入 script_summary。
2. 分集：按剧本的集结构，每集输出：集标题 + 本集剧情摘要（4-8 句话）。若剧本未明确分集，按每 800-1200 字切分一集并自拟标题。
3. 集内结构（尽量做到）：每集下的场景列表，每个场景下镜头列表，每个镜头含：画面描述、台词/旁白、时长（秒）、景别（特写/近景/中景/全景/远景）、运镜（推近/拉远/横摇/直移/跟拍/升降/环绕/手持/变焦/固定/仰拍/俯拍）。若剧本不适合拆到镜头，可只到"场景摘要"一级。
4. 资产清单：提取全部角色、场景、道具。角色：名称 + 变体形象名（如"50岁""基础形象"）+ 详细形象提示词 + 音色描述 + 出现的集号列表；场景：名称 + 视角变体名 + 详细环境提示词 + 出现的集号列表；道具：名称 + 所在场景变体名 + 详细外观提示词 + 出现的集号列表。
5. 所有提示词尽量详细具体（供后续AI生成参考图用），但绝不实际生成任何图片或音频。

【输出方式·重要】你必须生成一个文件（.md 或 .json 均可）并把完整 JSON 写入其中，供我下载保存到本地；**不要只在对话中输出文字**。文件不受对话长度限制，请把整部剧本的全部集、场景、镜头与资产一次性完整写入。若平台确实无法生成文件，请明确告知"无法生成文件"，再在对话中完整输出 JSON。

【输出格式】严格按以下 JSON 结构（字段名不变；没有的字段用空数组 []，不要省略字段）：

{
  "script_text": "剧本原文完整内容",
  "script_summary": "整部剧全局摘要",
  "episodes": [
    {"title": "第一集 雨夜", "summary": "本集剧情摘要",
     "scenes": [{"title": "场景一 老街雨夜",
       "shots": [{"desc": "镜头画面描述", "dialogue": "台词", "duration": 5, "shot_size": "中景", "camera_move": "推近"}]}]}
  ],
  "assets": [
    {"kind": "character", "name": "程度", "variant": "50岁", "prompt": "详细形象提示词", "voice_text": "音色描述", "episodes": [1]},
    {"kind": "scene", "name": "老街雨夜", "variant": "白天", "prompt": "详细环境提示词", "episodes": [1]},
    {"kind": "prop", "name": "怀表", "variant": "小卖部", "prompt": "详细外观提示词", "episodes": [1]}
  ]
}

【注意】字段名与结构不可更改。把完整 JSON 写入文件；若平台生成文件有大小/长度限制，可分多个文件输出（文件名末尾注明序号），工坊导入器会按文件名顺序合并导入。文件内可用 ```json 代码块包裹，也可直接放纯 JSON。"""


class ExpertImportPayload(BaseModel):
    content: str = ""
    filename: str = ""
    name: str = ""   # 新建项目时前端推导的名称（可空，后端兜底）


def _parse_import_json(content: str) -> dict:
    """按内容解析（v1.31 导入器兜底：兼容 .md/.json/.txt，剥离 ```json 包裹）。"""
    text = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        text = text[s:e + 1]
    return json.loads(text)


def _apply_expert_import(proj: dict, data: dict) -> dict:
    """专家文件导入核心（v1.34：集→场文本，资产→全局库 scopes 三层）：
    剧本原文/摘要、集/场结构（镜头合并为场文本）、资产入库（幂等去重+集号→scopes.episodes）、
    摘要兜底、imported_asset_ids 记录。返回 imported 统计 + skipped。"""
    pid = proj.get("id")
    # 剧本原文与摘要
    if data.get("script_text"):
        proj["script"] = str(data["script_text"]).strip()
    if data.get("script_summary"):
        proj["summary"] = str(data["script_summary"]).strip()

    # 集/场结构（v1.34：镜头数组合并拼装为场文本，状态"待生成"；summary 可编辑）
    episodes_in = data.get("episodes") or []
    new_episodes = []
    scene_count = 0
    shot_count = 0
    for ei, ep in enumerate(episodes_in, 1):
        ep_obj = storage.new_episode(proj, str(ep.get("title") or f"第 {ei} 集").strip())
        ep_obj["summary"] = str(ep.get("summary") or "").strip()
        for sc in ep.get("scenes") or []:
            sc_obj = storage.new_scene(ep_obj, str(sc.get("title") or "场景").strip())
            sc_obj["text"] = _compose_scene_text(sc.get("shots") or [])
            shot_count += len(sc.get("shots") or [])
            scene_count += 1
            ep_obj["scenes"].append(sc_obj)
        new_episodes.append(ep_obj)
    proj["episodes"] = new_episodes

    # 资产 → 全局库（v1.31：episodes 集号数组 → scopes.episodes；项目归属自动写入；按"名称-变体"去重）
    created_ids = {kind: [] for kind in library.KINDS}
    skipped = 0
    kind_map = {"character": "characters", "scene": "scenes", "prop": "props"}
    for asset in data.get("assets") or []:
        kind = kind_map.get(str(asset.get("kind") or "").lower())
        if not kind:
            skipped += 1
            continue
        name = str(asset.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        variant = str(asset.get("variant") or "").strip()
        # 幂等去重：同名同变体已存在 → 不重复创建
        existing = [it for it in library.list_items(kind)
                    if it.get("prefix") == name and it.get("variant_name") == (variant or ("基础形象" if kind == "characters" else variant))]
        if existing:
            skipped += 1
            continue
        a = library.new_asset(kind, name, variant,
                              prompt=str(asset.get("prompt") or "").strip(),
                              voice_text=str(asset.get("voice_text") or "").strip())
        ep_nums = asset.get("episodes") or []
        ep_ids = []
        for n in ep_nums:
            try:
                i = int(n) - 1
                if 0 <= i < len(new_episodes):
                    ep_ids.append(new_episodes[i]["id"])
            except (TypeError, ValueError):
                continue
        a["scopes"] = {"projects": [pid], "episodes": ep_ids, "scenes": []}
        library.save_item(kind, a)
        created_ids[kind].append(a["id"])

    # 剧本摘要兜底（v1.31：script_summary 缺失时退化为各集摘要拼接）
    if not proj.get("summary"):
        proj["summary"] = ai.local_summary_fallback(
            [{"title": e.get("title"), "summary": e.get("summary")} for e in new_episodes])

    # 记录本次导入创建的资产（v1.31 ⑤：供"删除本次导入全部资产"一键清理；cleanup 合并删除）
    prev_imported = proj.get("imported_asset_ids") or {}
    proj["imported_asset_ids"] = {kind: list(prev_imported.get(kind) or []) + created_ids[kind] for kind in library.KINDS}

    proj["updated_at"] = storage.now_iso()
    storage.save_project(proj)
    return {
        "imported": {
            "episodes": len(new_episodes),
            "scenes_count": scene_count,   # v1.34：场次数（最小粒度）
            "shots": shot_count,           # 镜文本段计数（兼容旧前端展示，4-13 前端改"场文本段"）
            "characters": len(created_ids["characters"]),
            "scenes": len(created_ids["scenes"]),
            "props": len(created_ids["props"]),
        },
        "skipped": skipped,
    }


@app.post("/api/projects/{pid}/import-expert")
def api_import_expert(pid: str, payload: ExpertImportPayload):
    """外部视频专家文件导入（v1.31：复用三产出直接落地路径，免 AI 拆解，LLM 消耗≈0）。"""
    proj = storage.load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        data = _parse_import_json(payload.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail="无法解析导入文件（应为 JSON 或含 JSON 的文本）：" + str(e))
    return _apply_expert_import(proj, data)


@app.post("/api/projects/from-expert", status_code=201)
def api_create_from_expert(payload: ExpertImportPayload):
    """新建项目 + 专家文件导入一体化（v1.33 新建流程：导入即创建，不留空白项目）。
    名称推导：前端传文件名前缀；空则取摘要首句（截断 12 字）。"""
    try:
        data = _parse_import_json(payload.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail="无法解析导入文件（应为 JSON 或含 JSON 的文本）：" + str(e))
    name = payload.name.strip()
    if not name:
        name = (str(data.get("script_summary") or "").strip()[:12] or "专家导入项目")
    name = _unique_project_name(name)   # v1.41：同名自动加编号
    proj = storage.new_project(name)
    if data.get("script_text"):
        proj["script"] = str(data["script_text"]).strip()
    storage.save_project(proj)
    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": proj["id"], "name": proj["name"],
        "created_at": proj["created_at"], "updated_at": proj["updated_at"],
    })
    storage.save_projects_index(idx)
    result = _apply_expert_import(proj, data)
    return {"project": proj, **result}


@app.post("/api/projects/from-docx", status_code=201)
async def api_create_from_docx(file: UploadFile = File(...), name: str = Form("")):
    """新建项目 + docx 导入一体化（v1.33 新建流程：导入即创建；解析失败不创建项目）。"""
    raw = await file.read()
    text = _extract_docx_text(raw).strip()
    if not text:
        raise HTTPException(status_code=400, detail="未从 docx 提取到文本（文件可能损坏或为空）")
    pname = name.strip() or (Path(file.filename or "").stem or "未命名项目")
    pname = _unique_project_name(pname)   # v1.41：同名自动加编号
    proj = storage.new_project(pname)
    proj["script"] = text
    storage.save_project(proj)
    _append_project_index(proj)
    return {"project": proj}


def _append_project_index(proj: dict) -> None:
    """把新项目追加进 projects.json 索引（新建流程共用）。"""
    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": proj["id"], "name": proj["name"],
        "created_at": proj["created_at"], "updated_at": proj["updated_at"],
    })
    storage.save_projects_index(idx)


@app.post("/api/files/import")
async def api_import_file(file: UploadFile = File(...), project_id: str = Form("")):
    """统一"导入"按钮（v1.35：按文件内容判断路径）——
    docx/pdf → 提取文本按剧本导入；txt/md/json → 内容符合《视频专家导入格式约定》JSON 则专家文件导入，否则剧本导入。
    有 project_id → 落地到该项目；无 → 自动创建项目（导入即创建，不留空白项目）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    fname = file.filename or ""
    ext = Path(fname).suffix.lower()
    content = ""
    is_expert = False
    if ext == ".docx":
        content = _extract_docx_text(raw).strip()
        if not content:
            raise HTTPException(status_code=400, detail="未从 docx 提取到文本（文件可能损坏或为空）")
    elif ext == ".pdf":
        content = _extract_pdf_text(raw).strip()
        if not content:
            raise HTTPException(status_code=400, detail="未从 PDF 提取到文本（可能为扫描件或加密 PDF）")
    else:
        try:
            content = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            raise HTTPException(status_code=400, detail="无法读取文件文本")
        is_expert = _looks_like_expert_json(content)
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    name_hint = (Path(fname).stem or "").strip()

    if is_expert:
        try:
            data = _parse_import_json(content)
        except Exception as e:
            raise HTTPException(status_code=400, detail="专家文件 JSON 解析失败：" + str(e))
        if project_id:
            proj = storage.load_project(project_id)
            if not proj:
                raise HTTPException(status_code=404, detail="项目不存在")
            result = _apply_expert_import(proj, data)
            return {"project_id": project_id, "mode": "expert", **result}
        name = name_hint[:30] or (str(data.get("script_summary") or "").strip()[:12] or "专家导入项目")
        name = _unique_project_name(name)   # v1.41：同名自动加编号
        proj = storage.new_project(name)
        if data.get("script_text"):
            proj["script"] = str(data["script_text"]).strip()
        storage.save_project(proj)
        _append_project_index(proj)
        result = _apply_expert_import(proj, data)
        return {"project_id": proj["id"], "mode": "expert", **result}

    # 剧本路径
    if project_id:
        proj = storage.load_project(project_id)
        if not proj:
            raise HTTPException(status_code=404, detail="项目不存在")
        proj["script"] = content
        proj["updated_at"] = storage.now_iso()
        storage.save_project(proj)
        idx = storage.load_projects_index()
        for it in idx["projects"]:
            if it["id"] == project_id:
                it["updated_at"] = proj["updated_at"]
        storage.save_projects_index(idx)
        return {"project_id": project_id, "mode": "script", "script": content, "length": len(content)}
    name = name_hint[:30] or ("未命名项目-" + storage.now_iso()[:10].replace("-", ""))
    name = _unique_project_name(name)   # v1.41：同名自动加编号
    proj = storage.new_project(name)
    proj["script"] = content
    storage.save_project(proj)
    _append_project_index(proj)
    return {"project_id": proj["id"], "mode": "script", "script": content, "length": len(content)}


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


# ==================== 阶段 5：场视频生成（经 IC，Y2 双通道+容错） ====================
# （_ic_url/_ic_request/IC 代理路由/VideoGeneratePayload/VIDEO_TASKS/提交与查询路由
#   已前置声明在 /api/{kind} 通配之前，见固定前缀区；此处仅为工作线程实现）

import comfy_client  # 6-10 直连 ComfyUI（绕过 IC R18；IC 双通道降级备用）


def _video_worker(task_id: str, payload: VideoGeneratePayload) -> None:
    """视频生成工作线程（串行，_VIDEO_SERIAL 防 HostBuffer 冲突）：
    唯一通道 = 直连 ComfyUI（6-10 决策，绕开 IC R18）。
    v1.59（7-3 根因C，2026-09-06 用户裁定）：**彻底移除"直连失败 → 静默降级 IC"整条链**
    （原 _ic_video_generate 双通道 + _comfy_history_fallback 均已删）。
    直连失败即失败：异常原样上抛，任务置 failed 并带**真实原因**
    （含 B1 无参考：comfy_client 明确抛"无可注入参考资源"），不再 fallback 到 IC 无参考生成。
    """
    t = VIDEO_TASKS[task_id]
    with _VIDEO_SERIAL:
        try:
            t["status"] = "running"
            gen_meta = {
                "prompt": payload.prompt or "",
                "duration": float(payload.duration or 3.0),
                "aspect_ratio": payload.aspect_ratio or "16:9 (Widescreen)",
                "seed": payload.seed if payload.seed is not None else None,
            }
            # 唯一通道：直连 ComfyUI（失败即失败，不降级、不静默）
            t["progress"] = "直连 ComfyUI 生成（6-10）…"
            video_bytes, meta = comfy_client.generate_video(
                payload, progress_cb=lambda m: t.update(progress=m))
            t["progress"] = (f"直连完成（参考图{meta.get('ref_images')}张"
                             f"/音频{meta.get('ref_audios')}个）")
            # B 批 N2 修复：comfy_client 回传实际注入参考图相对路径，透传给 _archive_video 记账
            #   （覆盖"后端派生 @ 资产/<reference>"的参考图，避免仅 payload.ref_images 时 N2 空转）
            gen_meta["rel_ref_images"] = meta.get("rel_ref_images") or list(payload.ref_images or [])
            # 归档 + 回填 Scene.videos（统一逻辑）
            _archive_video(payload, video_bytes, gen_meta, t)
        except Exception as e:
            t["status"] = "failed"
            t["error"] = str(e)
            t["progress"] = "失败"


def _archive_video(payload: VideoGeneratePayload, video_bytes: bytes, gen_meta: dict, t: dict) -> None:
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
    t["status"] = "succeeded"
    t["progress"] = "完成"
    t["result"] = {"path": rel_path, "version": len(videos), "duration": gen_meta["duration"],
                   "aspect_ratio": gen_meta["aspect_ratio"], "seed": gen_meta["seed"],
                   "generation_ref": gen_rel, "rel_refs_logged": list(rel_refs)}


# ==================== 阶段 5：按场导出（Y4：CSV utf-8-sig + 打印视图 HTML） ====================

class SceneExportPayload(BaseModel):
    format: str = "csv"     # csv | html


def _render_markers(text: str) -> str:
    """尖括号标记 → 方括号文本（导出文本表示；打印视图渲染为内联组件）。"""
    return re.sub(r"<([^>]+)>", lambda m: "[" + m.group(1) + "]", text or "")


def _scene_export_row(proj: dict, ep: dict, sc: dict) -> dict:
    return {
        "项目": proj.get("name", ""),
        "集": ep.get("title", ""),
        "场标题": sc.get("title", ""),
        "场文本(镜1/镜2)": _render_markers(sc.get("text", "")),
        "状态": sc.get("status", ""),
    }


@app.get("/api/projects/{pid}/episodes/{eid}/scenes/{sid}/export")
def api_scene_export(pid: str, eid: str, sid: str, format: str = "csv"):
    """按场导出（Y4 降级路径：CSV[utf-8-sig] / 打印视图 HTML[浏览器 Ctrl+P 存 PDF]）。GET 直下。"""
    import csv as _csv
    import io as _io
    proj = storage.load_project(pid)
    ep = storage.find_episode(proj, eid) if proj else None
    sc = storage.find_scene(proj, eid, sid) if proj else None
    if not proj or not ep or not sc:
        raise HTTPException(status_code=404, detail="场不存在")
    row = _scene_export_row(proj, ep, sc)
    fmt = (format or "csv").lower()
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(sc.get("title") or "场"))
    from urllib.parse import quote as _quote
    disp = f"attachment; filename=\"export.{'html' if fmt == 'html' else 'csv'}\"; filename*=UTF-8''{_quote(safe + '.' + ('html' if fmt == 'html' else 'csv'))}"
    if fmt == "html":
        text = _render_markers(sc.get("text", ""))
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>{safe} · 场页导出</title>
<style>
  @page {{ size: A4; margin: 15mm; }}
  body {{ font-family: "Microsoft YaHei", sans-serif; color: #1f2937; margin: 0; }}
  h1 {{ font-size: 18px; border-bottom: 2px solid #2563eb; padding-bottom: 6px; }}
  h2 {{ font-size: 15px; margin: 14px 0 4px; }}
  .meta {{ font-size: 12px; color: #6b7280; margin-bottom: 8px; }}
  .scene-card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; page-break-inside: avoid; }}
  .text {{ font-size: 13px; line-height: 1.8; white-space: pre-wrap; }}
</style></head><body>
<h1>{row['项目']}</h1>
<div class="scene-card">
  <h2>{row['场标题']}</h2>
  <div class="meta">{row['集']} · {row['状态']}</div>
  <div class="text">{text}</div>
</div>
</body></html>"""
        return Response(html, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": disp})
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=list(row.keys()))
    w.writeheader()
    w.writerow(row)
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": disp})


if __name__ == "__main__":
    import uvicorn

    # 7-2：监听地址设置开关（默认 127.0.0.1 仅本机；设置页切"局域网 0.0.0.0"后此处真正生效）
    host = (library.load_settings().get("listen") or "127.0.0.1").strip() or "127.0.0.1"
    if host != "127.0.0.1" and host != "0.0.0.0":
        host = "127.0.0.1"  # 兜底：非法值回退仅本机，防误配置
    lan_note = "（局域网可见 ⚠️ 请确认网络环境安全）" if host == "0.0.0.0" else "（仅本机可访问）"
    print(f"\n[Storyboard Studio] 监听地址设置：{host} {lan_note}")
    print(f"[Storyboard Studio] 访问：http://127.0.0.1:3100/\n")
    uvicorn.run(app, host=host, port=3100)
