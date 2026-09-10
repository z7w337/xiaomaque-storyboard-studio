# -*- coding: utf-8 -*-
"""阶段 6 · 版本索引 + SharedAssets 后台监听 + 自动登记（6-1 / 6-2）。

职责（规划书 4.2 / 4.3 与《工程管理规范》第 9 节）：
  1. 版本索引：SharedAssets\\.versions\\index.json 的读写。
     - 单写者（写死）：索引**只允许本模块写入**（Infinite Canvas 不读不写它；本模块内加锁串行）。
     - 原子写入（复用 data_io）+ 每次成功写入保留一份 .bak。
     - 启动校验完整性，损坏则从 .bak 恢复；.bak 也损坏则重置为空索引并提示。
  2. SharedAssets 后台监听（watchfiles）：监视全树新增/修改/删除的媒体文件。
     - 新增/修改 → register_file（幂等自动登记）；删除 → 标记"已删除"。
     - 监听与全量对账共用同一登记函数，天然幂等。
  3. 全量对账（启动基线 + 定期兜底）：扫描全树，注册缺失的媒体文件、标记已删除的，
     弥补文件系统事件遗漏。

幂等键（强制，08-20 变更区）：
  - 文件路径 + sha256 均相同 → 判定"已登记"并跳过。
  - 路径相同但 sha256 不同 → 按"自动版本"登记新版本，旧版本标记"历史"。
  - 禁止仅凭"同名"判断跳过（会错误阻断内容被覆盖后的版本更新）。

来源判定（6-3 前置，本步先按路径分区标来源）：
  output/uploads/library → 来自 Infinite Canvas；
  unassigned            → 未分配区；
  video                 → 场视频；
  audio                 → 音频（试听/参考）；
  input/import          → 外部导入；
  drama                 → 结构化资产参考图。
  （任务卡片时间窗自动关联 / 未分配区拖拽挂载 属 6-3 / 6-4，本步只登记来源标识。）
"""
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import data_io
import library

# 全局锁（单写者）：索引的读改写 + 监听/对账线程串行
_INDEX_LOCK = threading.RLock()

# 媒体扩展名（监听/对账只登记这些，其余跳过）
MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac",
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
}

# 排除前缀（相对路径）：隐藏目录/版本区/临时/非媒体
_EXCLUDE_PREFIXES = (".versions", ".git", "__pycache__", ".svn")
_TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".swp")
# 运行时临时目录（用户裁决 A：不算正式资产，不进版本索引）——试听/参考音频、临时上传图
_RUNTIME_TMP_DIRS = ("audio/tmp/", "drama/tmp/")

# 来源标识（6-1 本步：按路径分区判定）
SOURCE_IC = "ic"            # Infinite Canvas（output/uploads/library）
SOURCE_IMPORT = "import"    # 外部导入（input/import）
SOURCE_UNASSIGNED = "unassigned"
SOURCE_VIDEO = "video"
SOURCE_AUDIO = "audio"
SOURCE_DRAMA = "drama"      # 结构化资产参考图
SOURCE_REF = "reference"    # 参考引用类资产（v1.53：refs\，用户裁定）

# 状态
ST_IN_USE = "in_use"
ST_HISTORY = "history"
ST_DELETED = "deleted"

# 定期对账间隔（秒；兜底补文件系统事件遗漏）
RECONCILE_INTERVAL_SECONDS = 60

_INDEX_DIR_NAME = ".versions"
_INDEX_FILE_NAME = "index.json"

_OBSERVER_STARTED = False
_OBSERVER_STOP = threading.Event()
_OBSERVER_THREAD = None
_RECONCILE_THREAD = None


# ==================== 路径/索引文件 ====================

def _index_dir() -> Path:
    return Path(library.get_shared_assets_path()) / _INDEX_DIR_NAME


def _index_path() -> Path:
    return _index_dir() / _INDEX_FILE_NAME


def _bak_path() -> Path:
    return _index_dir() / (_INDEX_FILE_NAME + ".bak")


def init_index() -> None:
    """确保版本索引存在（幂等）；目录缺失则建。"""
    d = _index_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _index_path()
    if not p.exists():
        data_io.atomic_write_json(p, {"schema": 1, "entries": []})


def _blank() -> dict:
    return {"schema": 1, "updated_at": library.now_iso(), "entries": []}


def load_index() -> dict:
    """加载版本索引；损坏则从 .bak 恢复，.bak 也坏则重置为空索引并提示。"""
    p = _index_path()
    if not p.exists():
        init_index()
        return _blank()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if _validate(data):
            return data
        raise ValueError("索引结构无效")
    except (OSError, ValueError, json.JSONDecodeError):
        return _restore_from_bak()


def _restore_from_bak() -> dict:
    """从 .bak 恢复；.bak 也无效则重置为空索引并提示。"""
    b = _bak_path()
    try:
        with open(b, "r", encoding="utf-8") as f:
            data = json.load(f)
        if _validate(data):
            # 恢复成功，回写主文件
            data_io.atomic_write_json(_index_path(), data)
            data.setdefault("_restored_from_bak", True)
            return data
        raise ValueError("备份索引结构无效")
    except (OSError, ValueError, json.JSONDecodeError):
        fill = _blank()
        fill["_index_warning"] = "版本索引损坏且备份不可用，已重置为空索引"
        data_io.atomic_write_json(_index_path(), fill)
        return fill


def _validate(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("entries"), list):
        return False
    for e in data["entries"]:
        if not isinstance(e, dict):
            return False
        for k in ("id", "path", "sha256", "status"):
            if k not in e:
                return False
    return True


def save_index(data: dict) -> None:
    """原子写入 + 保留 .bak。仅本模块调用（单写者）。"""
    data["updated_at"] = library.now_iso()
    data_io.atomic_write_json(_index_path(), data)
    # 成功写入后保留 .bak（先复制当前有效文件）
    try:
        import shutil
        shutil.copy2(_index_path(), _bak_path())
    except OSError:
        pass


# ==================== 工具 ====================

def is_media_rel(rel: str) -> bool:
    """是否登记的媒体相对路径（排除隐藏目录/临时文件/非媒体）。"""
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not rel:
        return False
    parts = rel.split("/")
    if any(parts[0].startswith(p.rstrip("/")) for p in _EXCLUDE_PREFIXES if p and p in parts):
        return False
    if rel.startswith(_EXCLUDE_PREFIXES):
        return False
    if rel.startswith(_RUNTIME_TMP_DIRS):
        return False
    if rel.endswith(_TEMP_SUFFIXES):
        return False
    suffix = Path(rel).suffix.lower()
    return suffix in MEDIA_EXTS


def _norm_rel(rel: str) -> str:
    return rel.strip().replace("\\", "/").lstrip("/")


def compute_sha256(abs_path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _source_from_path(rel: str) -> str:
    rel = _norm_rel(rel)
    top = rel.split("/")[0]
    if top in ("output", "uploads", "library"):
        return SOURCE_IC
    if top in ("input",):
        return SOURCE_IMPORT
    if top == "unassigned":
        return SOURCE_UNASSIGNED
    if top == "video":
        return SOURCE_VIDEO
    if top == "audio":
        return SOURCE_AUDIO
    if top == "drama":
        return SOURCE_DRAMA
    if top == "refs":
        return SOURCE_REF
    return SOURCE_IMPORT


# ==================== 核心登记（幂等） ====================

# 收尾A④自动资产化：需建卡的目标图片后缀（素材卡=图片；音视频不资产化）
_AUTO_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _auto_materialize_if_needed(rel: str) -> None:
    """收尾A④：感知裸图 → 自动资产化建 references 素材卡（方案A：主图指向原图，不复制到 refs/）。

    触发面：observer 事件 + reconcile 全量扫描感知到的 output/uploads/截帧等游离裸图。
    - 仅图片；refs/ 前缀（素材卡主图存放处）跳过，避免对已资产化副本重复建卡
    - 幂等：library.find_materialized_by_src_rel(rel) 命中（materialize _src_rel 或本方案 images[0]=源图）→ 跳过
    - fail-open：建卡异常 → 吞掉不阻断（observer/reconcile 循环不受影响）
    - 锁序：本函数在 register_file 的 _INDEX_LOCK **之外**调用（new_reference/save_reference 取 _LIBRARY_LOCK），
      满足"先 library 后 assets_index"，绝不持 _INDEX_LOCK 时取 _LIBRARY_LOCK。
    """
    if not rel or Path(rel).suffix.lower() not in _AUTO_IMG_EXTS:
        return
    r = _norm_rel(rel)
    if r.startswith("refs/"):
        return  # refs/ 是素材卡主图存放处，跳过
    try:
        if library.find_materialized_by_src_rel(r):
            return  # 已资产化（materialize 副本卡或本方案卡）
        # 建素材卡：主图指向原图（方案A，用户裁定）。new_reference 内部 save_reference（幂等建卡）
        library.new_reference("", r)
    except Exception:
        pass  # fail-open：不阻断 observer/reconcile 循环


def register_file(rel: str, source: str | None = None, task_id: str = "",
                  note: str = "") -> dict:
    """幂等登记一个媒体文件（监听/对账共用）。

    收尾A④：登记前若为"未被资产化的游离裸图"则**自动资产化建卡**（方案A指向原图）；
    登记本身仍只写版本索引。返回：{"action": "skip"|"new"|"version"|"ignored"|"missing", ...}
    """
    rel = _norm_rel(rel)
    if not rel:
        return {"action": "ignored", "entry": None, "msg": "空路径"}
    if not is_media_rel(rel):
        return {"action": "ignored", "entry": None, "msg": f"非媒体/被排除：{rel}"}

    root = Path(library.get_shared_assets_path()).resolve()
    abs_path = (root / rel).resolve()
    # 防目录穿越：必须落在共享库内
    if not str(abs_path).startswith(str(root)):
        return {"action": "ignored", "entry": None, "msg": f"路径越界：{rel}"}
    if not abs_path.is_file():
        return {"action": "missing", "entry": None, "msg": f"文件不存在：{rel}"}

    # 收尾A④：自动资产化（锁外、登记前；内部 _LIBRARY_LOCK 建卡，先于下方 _INDEX_LOCK，锁序安全）
    _auto_materialize_if_needed(rel)

    sha = compute_sha256(abs_path)
    src = source or _source_from_path(rel)

    with _INDEX_LOCK:
        data = load_index()
        entries = data["entries"]

        # 幂等：路径 + sha256 均同 → 已登记，跳过
        for e in entries:
            if e.get("path") == rel and e.get("sha256") == sha:
                e.setdefault("last_seen", library.now_iso())
                return {"action": "skip", "entry": e, "msg": "已登记（路径+sha256 相同）"}

        # 同路径的既有记录：把当前"在用"标记为"历史"（内容变化 → 自动版本）
        same_path = [e for e in entries if e.get("path") == rel]
        for e in same_path:
            if e.get("status") == ST_IN_USE:
                e["status"] = ST_HISTORY

        entry = {
            "id": library.new_id("v_"),
            "path": rel,
            "sha256": sha,
            "size": abs_path.stat().st_size,
            "mtime": datetime.fromtimestamp(abs_path.stat().st_mtime).isoformat(timespec="seconds"),
            "first_seen": library.now_iso(),
            "last_seen": library.now_iso(),
            "source": src,
            "task_id": task_id or _match_open_task(rel),
            "status": ST_IN_USE,
            "note": note,
            # 第6批：未分类图片引用计数（本模块新增；旧条目由 refresh/apply_all_ref_counts 补齐默认值）
            # ref_sources[] 元素: {"type":"asset","kind":...,"id":...,"field":"images"}
            #                    | {"type":"scene_video","pid":...,"eid":...,"sid":...,"field":"thumbnail"}
            "ref_count": 0,
            "ref_sources": [],
            "source_scene": None,   # 截帧图额外记录归属场 {"pid","eid","sid"}；非截帧图为 None
        }
        entries.append(entry)
        data["entries"] = entries
        # 保护性副本开关（默认关）
        if _protect_copy_enabled():
            _store_protect_copy(abs_path, entry["id"])
        save_index(data)
        action = "version" if same_path else "new"
        return {"action": action, "entry": entry, "msg": f"登记成功（{action}）：{rel}"}


def _match_open_task(rel: str) -> str:
    """时间窗关联（6-3/6-5）：新图登记时，若 outbox 存在未完成任务卡（时间窗内），自动关联最近一张的 task_id。
    宽匹配：优先 resource_path 含文件名/资产名；无则取最近一张 open 卡（时间窗内）。"""
    # F5(审计20260909): 函数内延迟 import handoff——解耦 assets_index→handoff 模块加载依赖,
    # 使 handoff 可改经 dao(门面收编)而不与 dao→assets_index 构成循环 import。
    from handoff import find_active_cards as _find_active_cards
    try:
        cards = _find_active_cards(window_seconds=0)
        if not cards:
            return ""
        name = Path(rel).name  # 文件名（如 xxx.png）
        # 1) 按 resource_path 文件名匹配
        for c in cards:
            rp = c.get("resource_path") or ""
            if rp and (Path(rp).name == name or rp == rel):
                return c.get("id", "")
        # 2) 宽匹配：最近一张 open 卡（时间窗内）
        return cards[0].get("id", "")
    except Exception:
        return ""


def _protect_copy_enabled() -> bool:
    try:
        cfg = library.load_settings()
        return bool(cfg.get("protect_copy"))
    except Exception:
        return False


def _store_protect_copy(abs_path: Path, vid: str) -> None:
    store_dir = _index_dir() / "store"
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(abs_path, store_dir / f"{vid}_{abs_path.name}")
    except OSError:
        pass


def mark_deleted(rel: str) -> dict:
    """标记某路径的所有"在用"记录为"已删除"（文件被物理删除）。"""
    rel = _norm_rel(rel)
    with _INDEX_LOCK:
        data = load_index()
        entries = data["entries"]
        changed = False
        for e in entries:
            if e.get("path") == rel and e.get("status") == ST_IN_USE:
                e["status"] = ST_DELETED
                e.setdefault("deleted_at", library.now_iso())
                changed = True
        if changed:
            save_index(data)
        return {"changed": changed, "rel": rel}


# ==================== 全量对账（基线 + 兜底） ====================

def reconcile() -> dict:
    """全量扫描共享库：注册缺失媒体、标记已删除。返回统计。"""
    root = Path(library.get_shared_assets_path())
    if not root.is_dir():
        return {"new": 0, "version": 0, "skipped": 0, "marked_deleted": 0, "total_files": 0}
    new_c = version_c = skipped_c = deleted_c = 0
    seen = set()
    # 磁盘上实际存在的媒体文件
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = _norm_rel(str(p.relative_to(root)))
        if not is_media_rel(rel):
            continue
        seen.add(rel)
        r = register_file(rel)
        if r["action"] in ("new", "version"):
            new_c += r["action"] == "new"
            version_c += r["action"] == "version"
        elif r["action"] == "skip":
            skipped_c += 1
    # 索引中"在用"但磁盘已不存在 → 标记已删除
    with _INDEX_LOCK:
        data = load_index()
        for e in data["entries"]:
            if e.get("status") == ST_IN_USE and e.get("path") not in seen:
                e["status"] = ST_DELETED
                e.setdefault("deleted_at", library.now_iso())
                deleted_c += 1
        if deleted_c:
            save_index(data)
    return {
        "new": new_c, "version": version_c, "skipped": skipped_c,
        "marked_deleted": deleted_c, "total_files": len(seen),
    }


# ==================== 查询（供 6-8 总览页用，本步先做只读） ====================

def get_summary() -> dict:
    with _INDEX_LOCK:
        data = load_index()
        entries = data["entries"]
    by_status = {ST_IN_USE: 0, ST_HISTORY: 0, ST_DELETED: 0}
    by_source: dict = {}
    for e in entries:
        st = e.get("status")
        by_status[st] = by_status.get(st, 0) + 1
        src = e.get("source") or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total": len(entries),
        "by_status": by_status,
        "by_source": by_source,
        "updated_at": data.get("updated_at"),
        "index_warning": data.get("_index_warning", ""),
        "protect_copy": _protect_copy_enabled(),
    }


def list_entries(status: str | None = None, source: str | None = None,
                 limit: int = 200) -> list:
    with _INDEX_LOCK:
        data = load_index()
        entries = list(data["entries"])
    out = []
    for e in entries:
        if status and e.get("status") != status:
            continue
        if source and e.get("source") != source:
            continue
        out.append(e)
    out.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
    return out[:limit]


def unclassified_entries(limit: int = 500) -> list:
    """未分类/未分配图：版本索引中 in_use 的**图片**，但**未被任何资产（角色/场景/道具）images 引用**。
    即"已登记但尚未挂到任何资产"的图（用户裁定 A：未归属的图都显示，无论物理分区）。仅图片，不含视频/音频。
    """
    img_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    # 收集所有资产引用的图片路径（相对 SharedAssets）
    referenced = set()
    for kind in library.KINDS:
        for it in library.list_items(kind):
            for img in it.get("images", []) or []:
                p = img.get("path")
                if p:
                    referenced.add(_norm_rel(p))
    # 2026-09-04 模型统一：参考类资产也纳入"已引用"扫描——uploads/ 等裸图落地为参考类资产后，
    #   不再作为"待归类的游离未分类图"（图落地即资产）。refs/ 前缀图本就在 unclassified 排除，无影响。
    try:
        for r in library.list_references():
            rp = library.ref_primary_path(r)
            if rp:
                referenced.add(_norm_rel(rp))
    except Exception:
        pass
    # 索引中 in_use 且未被引用的图片 = 未分类
    with _INDEX_LOCK:
        data = load_index()
        entries = data["entries"]
    out = []
    for e in entries:
        if e.get("status") != ST_IN_USE:
            continue
        p = e.get("path", "")
        if not p or Path(p).suffix.lower() not in img_exts:
            continue  # 仅图片（排除视频/音频）
        if not is_media_rel(p):
            continue
        if p.startswith("refs/"):
            continue  # v1.53：参考类资产不属"待归类"，单独维护
        if p in referenced:
            continue
        # 第6批：附上引用信息（供前端展示"未分类·引用N次/🎬截帧·归属场"）。旧条目无字段 → 默认全零，不落盘。
        ri = _ref_entry(e)
        out.append({
            "id": e.get("id"),
            "path": p,
            "sha256": e.get("sha256"),
            "size": e.get("size"),
            "source": e.get("source"),
            "first_seen": e.get("first_seen"),
            "mtime": e.get("mtime"),
            "status": e.get("status"),
            "ref_count": ri["ref_count"],
            "ref_sources": ri["ref_sources"],
            "source_scene": ri["source_scene"],
        })
    out.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
    return out[:limit]


# ==================== 第6批：未分类图片引用计数 + 截帧归属（体检自动跑核心兜底） ====================
# 定位：为版本索引每个图片 entry 维护 ref_count/ref_sources/source_scene 三字段。
#   - ref_count   ：被引用次数（被 资产 images[] 或 场视频 videos[].thumbnail 引用）
#   - ref_sources ：引用来源明细（删除未分类图时展示给用户，防止误删被引用的截帧图）
#   - source_scene：截帧图归属场 {"pid","eid","sid"}（跨场人物位置一致性参考）；非截帧图为 None
# 策略（用户裁定）："实时维护计数器 + 体检自动兜底"。本模块提供**以扫描为准**的全量对账
#   apply_all_ref_counts()（权威），写接口侧对受影响图调用 refresh_ref_counts() 做增量/兜底；
#   体检自动跑（加载资产页）调用全量对账，遗漏更新点也能自动修正。

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _ref_entry(e: dict) -> dict:
    """规范化读取某 entry 的引用字段（兼容旧无字段条目：补默认值，不落盘）。"""
    return {
        "ref_count": int(e.get("ref_count") or 0),
        "ref_sources": e.get("ref_sources") if isinstance(e.get("ref_sources"), list) else [],
        "source_scene": e.get("source_scene"),
    }


def collect_ref_counts(load_projects_index, load_project) -> dict:
    """全量扫描所有资产 images[] + 所有场 videos[].thumbnail，统计每张图的引用。
    返回 {rel: {"count":int,"sources":[ref...],"source_scene":obj|None}}。
    以扫描为准（权威兜底）。不写索引。
    """
    refs: dict = {}
    # 1) 资产 images[]（主三库字符/场景/道具）
    for kind in library.KINDS:
        for it in library.list_items(kind):
            aid = it.get("id")
            for img in it.get("images", []) or []:
                p = img.get("path") if isinstance(img, dict) else img
                if not p:
                    continue
                rel = _norm_rel(p)
                if not rel or Path(rel).suffix.lower() not in _IMG_EXTS:
                    continue
                d = refs.setdefault(rel, {"count": 0, "sources": [], "source_scene": None})
                d["count"] += 1
                d["sources"].append({
                    "type": "asset", "kind": kind, "id": aid, "field": "images",
                })
    # 2) 场 videos[].thumbnail（截帧图 → 归属 source_scene）
    idx = load_projects_index() or {}
    for p in idx.get("projects") or []:
        pid = p.get("id")
        if not pid:
            continue
        proj = load_project(pid) if callable(load_project) else None
        if not proj:
            continue
        for ep in proj.get("episodes") or []:
            eid = ep.get("id")
            for sc in ep.get("scenes") or []:
                sid = sc.get("id")
                for v in sc.get("videos") or []:
                    t = v.get("thumbnail") if isinstance(v, dict) else None
                    if not t:
                        continue
                    rel = _norm_rel(t)
                    if not rel or Path(rel).suffix.lower() not in _IMG_EXTS:
                        continue
                    d = refs.setdefault(rel, {"count": 0, "sources": [], "source_scene": None})
                    d["count"] += 1
                    d["sources"].append({
                        "type": "scene_video", "pid": pid, "eid": eid, "sid": sid,
                        "field": "thumbnail",
                    })
                    # 截帧图归属场（多场复用同一 thumbnail 时记录最近一个，足够标注用途）
                    d["source_scene"] = {"pid": pid, "eid": eid, "sid": sid}
    # 3) generation 引用账本（B 批 N1：读现有持久化账本，非扫描——generation 是历史生成事实，
    #    只靠扫资产 images[]/场 thumbnail 无法还原；把已持久化在 entry.ref_sources 的 generation 引用并入，
    #    保证全量对账(recount)不清零"仅被生成引用、未挂资产/未当 thumbnail"的图）
    with _INDEX_LOCK:
        data = load_index()
        for e in data["entries"]:
            if e.get("status") != ST_IN_USE:
                continue
            rel = e.get("path", "")
            srcs = e.get("ref_sources")
            if not rel or not isinstance(srcs, list):
                continue
            gen = [s for s in srcs if isinstance(s, dict) and s.get("type") == "generation"]
            if not gen:
                continue
            d = refs.setdefault(rel, {"count": 0, "sources": [], "source_scene": None})
            d["count"] += len(gen)
            d["sources"].extend(gen)
    return refs


def add_generation_refs(paths: list, ctx: dict) -> dict:
    """B 批 N2：视频/图生成 @ 参考图后，给该组图各 in_use entry 记一条 generation 引用（持久化账本）。
    ctx={pid,eid,sid} = 发生这次生成的场（生成动作发生地，非图归属场）。
    幂等：同 rel + 同 pid/eid/sid + type=generation 只记一次。返回 {added, skipped, missing, changed_paths[]}。
    注意：generation 引用经此显式写入 entry.ref_sources；collect_ref_counts 读回并入（非扫描还原）。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {"added": 0, "skipped": 0, "missing": 0, "changed_paths": []}
    ref = {"type": "generation", "pid": ctx.get("pid", ""), "eid": ctx.get("eid", ""),
           "sid": ctx.get("sid", ""), "field": "ref"}
    added = skipped = missing = 0
    changed = []
    with _INDEX_LOCK:
        data = load_index()
        for rel in set(_norm_rel(p) for p in paths):
            target = None
            for e in data["entries"]:
                if e.get("path") == rel and e.get("status") == ST_IN_USE:
                    target = e
                    break
            if target is None:
                missing += 1
                continue
            srcs = target.setdefault("ref_sources", [])
            dup = any(isinstance(s, dict) and s.get("type") == "generation"
                      and s.get("pid") == ref["pid"] and s.get("eid") == ref["eid"]
                      and s.get("sid") == ref["sid"] for s in srcs)
            if dup:
                skipped += 1
                continue
            srcs.append(ref)
            target["ref_count"] = int(target.get("ref_count") or 0) + 1
            added += 1
            changed.append(rel)
        if added:
            save_index(data)
    return {"added": added, "skipped": skipped, "missing": missing, "changed_paths": changed}


def release_generation_refs(paths: list, ctx: dict | None = None) -> dict:
    """B 批 N2/N3：删除/替换视频、删场/删集/删项目后，释放对应 generation 引用。
    ctx 空={} → 移除这些 path 上全部 generation 引用（删集/删项目按集聚合回收用）。
    ctx={pid}/ {pid,eid}/ {pid,eid,sid} → 仅移除匹配该上下文范围的 generation 引用（删视频只释放该场那次生成的）。
    返回 {released, untouched, changed_paths[]}。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {"released": 0, "untouched": 0, "changed_paths": []}
    scope = {k: (ctx or {}).get(k) for k in ("pid", "eid", "sid") if (ctx or {}).get(k)}
    released = untouched = 0
    changed = []
    with _INDEX_LOCK:
        data = load_index()
        for rel in set(_norm_rel(p) for p in paths):
            for e in data["entries"]:
                if e.get("path") != rel or e.get("status") != ST_IN_USE:
                    continue
                srcs = e.get("ref_sources")
                if not isinstance(srcs, list):
                    continue
                keep, drop = [], []
                for s in srcs:
                    if isinstance(s, dict) and s.get("type") == "generation" and _gen_matches(s, scope):
                        drop.append(s)
                    else:
                        keep.append(s)
                if not drop:
                    untouched += 1
                    continue
                e["ref_sources"] = keep
                e["ref_count"] = max(0, int(e.get("ref_count") or 0) - len(drop))
                released += len(drop)
                changed.append(rel)
        if released:
            save_index(data)
    return {"released": released, "untouched": untouched, "changed_paths": list(dict.fromkeys(changed))}


def release_generation_refs_by_scope(scope: dict) -> dict:
    """B 批 N3：按实体删除释放 generation 引用——扫描全部 in_use entry，移除 ref_sources 中
    pid/eid/sid 匹配 scope 的 generation 引用（scope 只含要删的实体键，如删场传 {pid,eid,sid}、
    删集传 {pid,eid}、删项目传 {pid}）。删场只清该场那次生成、删集/删项目按集/项目聚合清——与
    "集最小删除粒度"一致。返回 {released, entries_affected, changed_paths[]}。
    注意：scope 为空/无有效键时返回 0（不误清）。"""
    scope = {k: (scope or {}).get(k) for k in ("pid", "eid", "sid") if (scope or {}).get(k)}
    if not scope:
        return {"released": 0, "entries_affected": 0, "changed_paths": []}
    released = affected = 0
    changed = []
    with _INDEX_LOCK:
        data = load_index()
        for e in data["entries"]:
            if e.get("status") != ST_IN_USE:
                continue
            srcs = e.get("ref_sources")
            if not isinstance(srcs, list):
                continue
            keep, drop = [], []
            for s in srcs:
                if isinstance(s, dict) and s.get("type") == "generation" and _gen_matches(s, scope):
                    drop.append(s)
                else:
                    keep.append(s)
            if not drop:
                continue
            e["ref_sources"] = keep
            e["ref_count"] = max(0, int(e.get("ref_count") or 0) - len(drop))
            released += len(drop)
            affected += 1
            rel = e.get("path", "")
            if rel:
                changed.append(rel)
        if released:
            save_index(data)
    return {"released": released, "entries_affected": affected,
            "changed_paths": list(dict.fromkeys(changed))}


def _gen_matches(s: dict, scope: dict) -> bool:
    """generation 引用 s 是否命中 scope 过滤（scope 里有的键须相等；scope 空=全命中）。"""
    if not scope:
        return True
    return all(s.get(k) == v for k, v in scope.items())


def refresh_ref_counts(count_map: dict, only_these: bool = False) -> dict:
    """把 collect 出的 count_map 写回版本索引 entries 的 ref 字段。
    以扫描为准，覆盖既有值；仅对有变化的 entry 落盘（幂等）。返回 {fixed, unchanged, changed_paths[]}。
    only_these=True：只刷 count_map 内的 key（写接口局部兜底用），不影响 count_map 外的其它 entry。
    only_these=False：全量对账（不在 count_map 的 in_use 条目引用清零）——体检自动跑权威兜底用。
    """
    if not count_map:
        return {"fixed": 0, "unchanged": 0, "changed_paths": [], "log": []}
    log = []
    fixed = unchanged = 0
    with _INDEX_LOCK:
        data = load_index()
        for e in data["entries"]:
            rel = e.get("path", "")
            if not rel:
                continue
            new = count_map.get(rel)
            if new is None:
                # 全量对账模式：不在引用扫描里 → 若曾记录过引用，须清零（该图不再被任何处引用）
                if only_these:
                    continue  # 局部模式：跳过不在本次扫描集合的条目，不动它们
                new = {"count": 0, "sources": [], "source_scene": None}
            old_count = int(e.get("ref_count") or 0)
            old_src = e.get("ref_sources") or []
            old_ss = e.get("source_scene")
            new_count = int(new.get("count") or 0)
            new_src = new.get("sources") or []
            new_ss = new.get("source_scene")
            # 旧条目缺失 ref 字段（历史登记未带）即使值相同也须补写字段，保证全量图片 entry 字段完整
            old_missing_field = "ref_count" not in e
            if (not old_missing_field and old_count == new_count and old_ss == new_ss
                    and [tuple(x.items()) for x in old_src] == [tuple(x.items()) for x in new_src]):
                unchanged += 1
                continue
            e["ref_count"] = new_count
            e["ref_sources"] = new_src
            e["source_scene"] = new_ss
            log.append({
                "path": rel,
                "before": {"ref_count": old_count, "ref_sources": old_src, "source_scene": old_ss},
                "after": {"ref_count": new_count, "ref_sources": new_src, "source_scene": new_ss},
            })
            fixed += 1
        if fixed:
            save_index(data)
    return {"fixed": fixed, "unchanged": unchanged,
            "changed_paths": [x["path"] for x in log], "log": log}


def refresh_specific_refs(paths: list, load_projects_index, load_project) -> dict:
    """写接口局部兜底：对指定 path 集合重新对账引用计数并写回。
    全量 collect（权威），但 only 刷 paths 内的条目。返回 refresh 结果。"""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {"fixed": 0, "unchanged": 0, "changed_paths": [], "log": []}
    full = collect_ref_counts(load_projects_index, load_project)
    subset = {}
    for p in paths:
        rel = _norm_rel(p)
        if not rel:
            continue
        subset[rel] = full.get(rel, {"count": 0, "sources": [], "source_scene": None})
    return refresh_ref_counts(subset, only_these=True)


def apply_all_ref_counts(load_projects_index, load_project) -> dict:
    """第6批核心兜底：全量对账引用计数并写回。体检自动跑/写接口兜底调用。返回 {scan, refresh}。"""
    cm = collect_ref_counts(load_projects_index, load_project)
    return {"scanned": len(cm), "refresh": refresh_ref_counts(cm)}


def entry_ref_of(rel: str) -> dict:
    """读某路径的引用信息（供删除拦截/前端展示读计数器，不需扫描）。entry 不在则返回全零。"""
    rel = _norm_rel(rel)
    with _INDEX_LOCK:
        data = load_index()
        for e in data["entries"]:
            if e.get("path") == rel:
                return _ref_entry(e)
    return {"ref_count": 0, "ref_sources": [], "source_scene": None}


# ==================== 监听服务（watchfiles） ====================

def start_observer() -> dict:
    global _OBSERVER_STARTED, _OBSERVER_THREAD
    if _OBSERVER_STARTED:
        return {"ok": True, "msg": "监听已在运行"}
    root = Path(library.get_shared_assets_path())
    if not root.is_dir():
        return {"ok": False, "msg": f"共享资产库不存在：{root}"}
    _OBSERVER_STOP.clear()
    _OBSERVER_THREAD = threading.Thread(target=_observer_loop, args=(root,), daemon=True)
    _OBSERVER_THREAD.start()
    _OBSERVER_STARTED = True


def _observer_loop(root: Path) -> None:
    try:
        from watchfiles import watch
    except ImportError:
        return
    try:
        # watchfiles 默认 watch_filter 排除隐藏目录(.git/.versions 等)，防自触发
        for changes in watch(str(root)):
            for change, path_str in changes:
                if _OBSERVER_STOP.is_set():
                    return
                try:
                    rel = _norm_rel(str(Path(path_str).relative_to(root)))
                except ValueError:
                    continue
                if not is_media_rel(rel):
                    continue
                from watchfiles import Change
                if change in (Change.deleted,):
                    mark_deleted(rel)
                else:  # added / modified → 登记（幂等）
                    register_file(rel)
    except Exception:
        pass


def stop_observer() -> None:
    global _OBSERVER_STARTED
    _OBSERVER_STOP.set()
    _OBSERVER_STARTED = False


# ==================== 定期对账线程 ====================

def start_reconcile_loop() -> None:
    global _RECONCILE_THREAD
    if _RECONCILE_THREAD and _RECONCILE_THREAD.is_alive():
        return
    _RECONCILE_THREAD = threading.Thread(target=_reconcile_loop, daemon=True)
    _RECONCILE_THREAD.start()


def _reconcile_loop() -> None:
    while not _OBSERVER_STOP.is_set():
        try:
            reconcile()
        except Exception:
            pass
        # 分段等待，便于响应停止
        for _ in range(RECONCILE_INTERVAL_SECONDS):
            if _OBSERVER_STOP.wait(1):
                return


# ==================== 启动入口 ====================

def start() -> dict:
    """启动版本索引 + 监听 + 定期对账；返回状态。"""
    init_index()
    obs = start_observer()
    start_reconcile_loop()
    return obs
