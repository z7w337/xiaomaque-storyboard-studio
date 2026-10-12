# -*- coding: utf-8 -*-
"""资产库体检（批4-3 重定位：只读巡检 + 仅修正悬空/坏项目引用）。

本次重定位依据（用户与第三方评估机构裁定，2026-09-03）：
  - 体检 = 「引用数刷新器」的定位已放弃（出场引用 ref-stats 本就实时现算，无需刷新）。
  - 体检**只做**：检测孤儿（projects=[]）+ 检测并修正悬空引用（eps/scenes 指向已删集/场）+ 坏 project 引用。
  - 体检**不做**删除、不做重复合并（D 类整个去掉）、不自动删任何资产；删不删由用户在资产页自行操作。
  - 删卡由资产页完成，且资产页将补"被出场引用 → 不可删"拦截（见 main.py api_delete_library_item）。

巡检（只读）+ 修正（写，仅悬空/坏 proj，先备份）。不提供删除与合并。

判定基于：每张资产卡读 scopes{projects[], episodes[], scenes[]}，
对照当前在案项目（data/projects.json 索引 + data/projects/{id}.json 详情）的有效 id 集合。

保留异常（只读检出/可修正）：
  A. 真孤儿        projects==[]（无任何项目归属）→ 只读列示，不删；删卡由资产页完成
  B. 悬空 eps/场     projects 非空且在案，但 episodes/scenes 含不存在集/场 id → 可修正（剔悬空，保留有效）
  C. 坏 project 引用 projects 含不在案项目 id → 可修正（剔坏 proj）

审计调用依赖注入 project_index / project 加载函数（避免 import main 造成循环依赖）：
  audit(load_project_index, load_project) 中 load_project(pid)->proj dict。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from data_io import atomic_write_json
# 批3 3a：业务数据访问经 dao 门面（data_io 底层原子写工具保留直连）
from dao import library

_log = logging.getLogger("storyboard.library_audit")

KINDS = ["characters", "scenes", "props"]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scope(asset: dict) -> dict:
    sc = asset.get("scopes")
    if not isinstance(sc, dict):
        return {"projects": [], "episodes": [], "scenes": []}
    return {
        "projects": [x for x in (sc.get("projects") or []) if isinstance(x, str)],
        "episodes": [x for x in (sc.get("episodes") or []) if isinstance(x, str)],
        "scenes": [x for x in (sc.get("scenes") or []) if isinstance(x, str)],
    }


def collect_valid_ids(load_project_index, load_project) -> dict:
    """遍历在案项目，收集有效的 project/episode/scene id 全集。返回 {projects, episodes, scenes} sets。"""
    valid = {"projects": set(), "episodes": set(), "scenes": set()}
    idx = load_project_index() or {}
    for p in idx.get("projects") or []:
        pid = p.get("id")
        if not pid:
            continue
        valid["projects"].add(pid)
        proj = load_project(pid)
        if not proj:
            continue
        for ep in proj.get("episodes") or []:
            eid = ep.get("id")
            if not eid:
                continue
            valid["episodes"].add(eid)
            for sc in ep.get("scenes") or []:
                sid = sc.get("id")
                if sid:
                    valid["scenes"].add(sid)
    return valid


def _asset_slim(kind: str, asset: dict) -> dict:
    """问题项精简：含缩略图路径(供只读报告图预览/点击跳转资产库)。images[0]=当前主图。"""
    imgs = asset.get("images") or []
    thumbs = []
    for im in imgs[:4]:
        p = im.get("path") if isinstance(im, dict) else im
        if p:
            thumbs.append(p)
    return {
        "kind": kind,
        "id": asset.get("id"),
        "name": asset.get("prefix"),
        "variant_name": asset.get("variant_name"),
        "thumb": thumbs[0] if thumbs else "",   # 主图（预览/跳转封面）
        "images": thumbs,
    }


def audit(load_project_index, load_project) -> dict:
    """只读巡检三库，检出孤儿/悬空/坏 proj 三类问题。返回健康度快照。

    不做删除、不做重复合并（D 类已移除）。孤儿仅列示（删卡由资产页完成）。
    """
    valid = collect_valid_ids(load_project_index, load_project)
    orphans = []            # A：仅列示，不删
    dangling = []           # B：可修正（剔悬空 eps/场）
    bad_proj = []           # C：可修正（剔坏 proj）

    for kind in KINDS:
        for asset in library.list_items(kind):
            sc = _scope(asset)
            pid_set = set(sc["projects"])
            valid_p = pid_set & valid["projects"]
            # --- A 真孤儿：无任何项目归属 → 列示（删卡由资产页） ---
            if not pid_set:
                orphans.append(_asset_slim(kind, asset))
                continue
            # --- C 坏 project 引用 ---
            bad_p = pid_set - valid["projects"]
            if bad_p:
                bad_proj.append({
                    **_asset_slim(kind, asset),
                    "bad_projects": sorted(bad_p),
                    "keep_projects": sorted(valid_p),
                })
            # --- B 悬空 eps / 场 引用（可修正） ---
            # eps 悬空判定：该 eps 只要不在任意在案项目的有效集/场集合中即视为悬空（scope 不记录 eps 属于哪个项目）。
            eps = sc["episodes"]
            scen = sc["scenes"]
            dangling_eps = [e for e in eps if e not in valid["episodes"]]
            dangling_scenes = [s for s in scen if s not in valid["scenes"]]
            if dangling_eps or dangling_scenes:
                dangling.append({
                    **_asset_slim(kind, asset),
                    "projects": sorted(pid_set),
                    "dangling_episodes": sorted(dangling_eps),
                    "keep_episodes": [e for e in eps if e in valid["episodes"]],
                    "dangling_scenes": sorted(dangling_scenes),
                    "keep_scenes": [s for s in scen if s in valid["scenes"]],
                })

    # 精确统计三库张数
    counts = {kind: len(library.list_items(kind)) for kind in KINDS}

    return {
        "generated_at": now_iso(),
        "scope": {
            "valid_projects": sorted(valid["projects"]),
            "valid_projects_count": len(valid["projects"]),
            "valid_episodes": len(valid["episodes"]),
            "valid_scenes": len(valid["scenes"]),
        },
        "library": counts,
        "issues": {
            "orphans": {"count": len(orphans), "items": orphans},
            "dangling_episodes": {"count": len(dangling), "items": dangling},
            "bad_project_refs": {"count": len(bad_proj), "items": bad_proj},
        },
    }


# ==================== 备份 ====================

def _backup_dir() -> Path:
    return library.DATA_DIR / ("backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_library_audit")


def backup_library() -> str:
    """修正执行前整体备份三库 JSON 到 data/backup_<ts>_library_audit/；返回相对 data 的目录名（路径）。"""
    import shutil
    dest = _backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        src = library.DATA_DIR / library.KINDS[kind]["file"]
        if src.exists():
            shutil.copy2(src, dest / src.name)
    return dest.name


# ==================== 修正执行（仅悬空 eps/场 + 坏 proj；不删孤儿、不合并） ====================

def _require_kind(kind: str) -> str:
    if kind not in library.KINDS:
        raise ValueError(f"库类型不存在: {kind}")
    return kind


def execute_cleanup(load_project_index, load_project, actions: dict) -> dict:
    """按前端提交的修正清单执行写操作（仅 fix_dangling / fix_bad_proj）。

    删除孤儿与重复合并已移除（体检不做删除，删卡由资产页完成）。
    先整体备份；每项服务端复核判据后再改。返回 {backup_path, applied, errors}。
    """
    errors = []
    applied = {
        "fix_dangling": 0,
        "fix_bad_proj": 0,
    }
    snapshot = audit(load_project_index, load_project)
    dangling_ids = {it["id"] for it in snapshot["issues"]["dangling_episodes"]["items"]}
    badproj_ids = {it["id"] for it in snapshot["issues"]["bad_project_refs"]["items"]}

    # 备份先行（有任一待执行项即备份）
    fix_dangling = actions.get("fix_dangling") or []
    fix_bad_proj = actions.get("fix_bad_proj") or []
    if fix_dangling or fix_bad_proj:
        backup_path = backup_library()
    else:
        backup_path = ""

    # --- B 修正悬空 eps/场（剔悬空，保留有效与 projects）---
    for fix in fix_dangling:
        kind, id_ = _split_kind_id(fix.get("id"))
        try:
            _require_kind(kind)
        except ValueError as e:
            errors.append({"op": "fix_dangling", "id": fix.get("id"), "reason": str(e)})
            continue
        asset = library.get_item(kind, id_)
        if not asset:
            errors.append({"op": "fix_dangling", "id": fix.get("id"), "reason": "资产不存在"})
            continue
        if id_ not in dangling_ids:
            errors.append({"op": "fix_dangling", "id": fix.get("id"),
                           "reason": "复核已无悬空引用，跳过"})
            continue
        sc = _scope(asset)
        new_eps = [e for e in sc["episodes"] if e not in set(fix.get("drop_episodes") or [])]
        new_scenes = [s for s in sc["scenes"] if s not in set(fix.get("drop_scenes") or [])]
        asset["scopes"]["episodes"] = new_eps
        asset["scopes"]["scenes"] = new_scenes
        asset["updated_at"] = now_iso()
        library.save_item(kind, asset)
        applied["fix_dangling"] += 1

    # --- C 修正坏 project 引用（剔坏 proj，保留有效）---
    for fix in fix_bad_proj:
        kind, id_ = _split_kind_id(fix.get("id"))
        try:
            _require_kind(kind)
        except ValueError as e:
            errors.append({"op": "fix_bad_proj", "id": fix.get("id"), "reason": str(e)})
            continue
        asset = library.get_item(kind, id_)
        if not asset:
            errors.append({"op": "fix_bad_proj", "id": fix.get("id"), "reason": "资产不存在"})
            continue
        if id_ not in badproj_ids:
            errors.append({"op": "fix_bad_proj", "id": fix.get("id"),
                           "reason": "复核已无坏 project 引用，跳过"})
            continue
        sc = _scope(asset)
        drop = set(fix.get("drop_projects") or [])
        new_proj = [p for p in sc["projects"] if p not in drop]
        asset["scopes"]["projects"] = new_proj
        asset["updated_at"] = now_iso()
        library.save_item(kind, asset)
        applied["fix_bad_proj"] += 1

    return {"backup_path": backup_path, "applied": applied, "errors": errors}


def _split_kind_id(compound: str) -> tuple:
    """由 id 反推 kind。id 前缀 ch_/sc_/pr_。若复合串（kind:id）也兼容。"""
    if not compound:
        return "", ""
    if ":" in compound:
        kind, id_ = compound.split(":", 1)
        return kind, id_
    for kind, meta in library.KINDS.items():
        if compound.startswith(meta["id_prefix"]):
            return kind, compound
    return "", compound
