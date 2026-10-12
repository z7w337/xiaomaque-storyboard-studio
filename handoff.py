# -*- coding: utf-8 -*-
"""阶段 6 · 6-5：提示词组装 + 去程任务卡（handoff/outbox）。

沿计划书 4.4 —— 去程不再推送文件，改为**任务卡片 + 提示词自动组装**：
  点"去画布编辑" → 生成任务卡写入 handoff\\outbox\\（含目标类型/名称/资产路径/时间戳/组装提示词），
  卡片时间窗内的新产出会被自动关联（6-3 时间窗，见 assets_index）。

提示词组装规则（4.4 v1.34 场粒度）：
  全局画风 + 场景变体提示词（含场景道具）+ 出场角色变体提示词（逐个，含手持道具）
  + 本场分镜文本（镜1/镜2 纯文本段）+ 场文本内行内标记（时长/运镜/景别）。

数据（画风库 style-templates、运镜库 camera-moves 尚未建，属 6-12/6-5 后续）：
  全局画风当前取项目 art_style；行内标记从场文本 <camera-move>/<shot-size>/<duration-ms> 提取。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

import data_io
# F5(审计20260909): 门面收编——经 dao 统一数据访问入口(同 3a 收敛模式, 经 dao 取同一底层函数对象, 调用点零改动)。
# 依赖: assets_index 已改为函数内延迟 import handoff, 故此处可安全经 dao(无 dao→assets_index→handoff→dao 循环)。
from dao import library, storage

BASE_DIR = Path(__file__).resolve().parent
OUTBOX_DIR = BASE_DIR / "handoff" / "outbox"

# 任务卡状态
ST_OPEN = "open"        # 未完成任务卡（时间窗内可关联新产出）
ST_DONE = "done"        # 已完成（被关联/用户确认）
ST_EXPIRED = "expired"  # 超出时间窗未完成


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_outbox() -> None:
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ==================== 提示词组装（4.4） ====================

def _inline_marks(text: str) -> list:
    """从场文本提取行内标记（时长/运镜/景别），非破坏性（保留在文本，此处复制提取作组装参考）。"""
    import re
    marks = []
    for m in re.finditer(r"<(camera-move|shot-size|duration-ms)>([^<]+)</\1>", text or ""):
        marks.append((m.group(1), m.group(2).strip()))
    return marks


def _scene_asset_ids(pid: str, eid: str, sid: str) -> list:
    """某场的出场资产 id：归属推导（scopes 三层：项目→全集/集→全场/场→精确）∪ 场文本 @ 显式引用。
    返回 [（kind, asset_id, prefix, variant_name), ...]。
    """
    proj = storage.load_project(pid)
    if not proj:
        return []
    ep = storage.find_episode(proj, eid) if eid else None
    sc = storage.find_scene(proj, eid, sid) if (eid and sid) else None
    # 收集场关联的归属引用：scopes.projects/episodes/scenes 命中 pid/eid/sid 的资产
    # 归属推导：项目归属→展开全部集/场；集归属→展开该集全部场；场归属→精确场
    result = []
    seen = set()
    for kind in library.KINDS:
        for it in library.list_items(kind):
            scopes = it.get("scopes") or {}
            # 场级命中
            if sid and sid in (scopes.get("scenes") or []):
                _push(result, seen, kind, it)
            # 集级命中（含该集所有场）
            elif eid and eid in (scopes.get("episodes") or []):
                _push(result, seen, kind, it)
            # 项目级命中（含所有集场）
            elif pid and pid in (scopes.get("projects") or []):
                _push(result, seen, kind, it)
    # 场文本 @ 显式引用（<role ...>名称</role> 等）—— 补充归属未覆盖的
    from html import unescape
    import re
    text = (sc.get("text") or "") if sc else ""
    for m in re.finditer(r"<([a-z-]+)[^>]*data-material-pippit-asset-id=\"([^\"]+)\"", text):
        pass  # @ 引用资产 id 可由此提取；此处保留框架（后续 @ 联想已插 id）
    return result


def _push(result, seen, kind, it):
    key = (kind, it.get("id"))
    if key in seen:
        return
    seen.add(key)
    result.append({"kind": kind, "id": it.get("id"),
                   "prefix": it.get("prefix", ""), "variant_name": it.get("variant_name", ""),
                   "prompt": it.get("prompt", "")})


# ==================== 批量资产提取（4.6 / 批3-2 B5 聚合器） ====================
# 目的：按「当前场 / 按集 / 全剧本」聚合出场资产引用（"名称-变体名称"完整资产粒度），
#   跨场按 名称-变体 去重（同一剧本同名同变体只算一次），与库内已有资产比对分出
#   missing(库内无→待提取新建) / existing(库内已有→直接引用不重建)。纯只读无副作用。

def _iter_scope_scenes(pid: str, eid: str = "", sid: str = "") -> list:
    """按范围枚举 (project, episode, scene) 元组（优先级 sid > eid > 全剧本）：
       sid 精确 → 仅该场（跨集定位所属集）；仅 eid → 该集全部场；两者空 → 全剧本全部场。"""
    proj = storage.load_project(pid)
    if not proj:
        return []
    eps = proj.get("episodes") or []
    if sid:
        # sid 优先：精确锁定单场（可能在任意集），返回 [(pid,所属集,sid)]
        for ep in eps:
            for s in (ep.get("scenes") or []):
                if s.get("id") == sid:
                    return [(pid, ep.get("id"), s)]
        return []
    if eid:
        ep = next((e for e in eps if e.get("id") == eid), None)
        if not ep:
            return []
        return [(pid, eid, s) for s in (ep.get("scenes") or [])]
    # 全剧本
    return [(pid, ep.get("id"), s) for ep in eps for s in (ep.get("scenes") or [])]


def _ref_regex():
    import re
    # 场文本出场资产标签：<role>名-变体</role> / <scene>…</scene> / <prop>…</prop>
    # 兼容带属性(如 data-*-asset-id) 与无属性两种形态，取标签内文 name
    return re.compile(r"<(role|scene|prop)[^>]*>([^<]+)</\1>")


def _asset_by_name(name: str):
    """在 characters/scenes/props 三库按 display_name("前缀-变体") 精确匹配；命中返回 {kind,id,prefix,variant_name,prompt}。"""
    for kind in library.KINDS:
        for it in library.list_items(kind):
            if library.display_name(it) == name:
                return {"kind": kind, "id": it.get("id"),
                        "prefix": it.get("prefix", ""), "variant_name": it.get("variant_name", ""),
                        "prompt": it.get("prompt", "")}
    return None


def aggregate_scene_assets(pid: str, eid: str = "", sid: str = "") -> dict:
    """B5 聚合器：按场/集/全剧本聚合出场资产引用并去重。
    返回 {pid,eid,sid, scene_count, refs[], missing[], existing[]}：
      refs   = 去重后的全部出场引用（含场景/角色/道具标签内文 name）
      missing= refs 中库内尚无的（prefix-variant 与库不匹配）→ 待提取新建
      existing=refs 中库内已有的 → 直接引用，不重建
      （另带 extra 说明；name 唯一，同一 name 跨场只算一次）"""
    scenes = _iter_scope_scenes(pid, eid, sid)
    seen = {}          # name -> {name, kinds:set, scenes:[(eid,sid)]}
    order = []
    for _pid, _eid, sc in scenes:
        txt = (sc.get("text") or "")
        if not txt:
            continue
        for m in _ref_regex().finditer(txt):
            tag, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if name not in seen:
                seen[name] = {"name": name, "kinds": set(), "scenes": []}
                order.append(name)
            seen[name]["kinds"].add(tag)
            loc = f"{_eid}:{sc.get('id')}"
            if loc not in seen[name]["scenes"]:
                seen[name]["scenes"].append(loc)
    refs, missing, existing = [], [], []
    for name in order:
        info = seen[name]
        ref = {"name": name, "kind": "characters" if "role" in info["kinds"] else ("scenes" if "scene" in info["kinds"] else "props"),
               "tags": sorted(info["kinds"]), "scenes": info["scenes"]}
        refs.append(ref)
        lib = _asset_by_name(name)
        if lib:
            ref["existing_id"] = lib["id"]
            existing.append({**ref, "library": lib})
        else:
            missing.append(ref)
    return {"pid": pid, "eid": eid, "sid": sid, "scene_count": len(scenes),
            "refs": refs, "missing": missing, "existing": existing}


def ensure_missing_assets(pid: str, missing: list, *, gen_prompt: str = "",
                          prompt_by_kind: dict | None = None) -> dict:
    """把 missing 资产批量建成库资产占位卡（B1 建卡口径：只建卡不生成，单张出图走已有入口）。

    missing: aggregate_scene_assets 的 missing[]（ref：{name, kind, tags, scenes:[ep:sc,...]}）。
    每资产：库内无同名同变体才建 → library.new_asset（含 prefix/variant + prompt），
    归属 scopes 自动带上来源 项目/集/场（从 ref.scenes "ep:sc" 反查），来源场即自动出场。
    返回 {created:[{kind,name,id}], skipped:[{name,reason}]}。零 LLM、纯结构建卡。
    """
    proj = storage.load_project(pid)
    if not proj:
        return {"created": [], "skipped": [{"name": f"<项目{pid}不存在>", "reason": "project_missing"}]}
    kind_map = {"characters": "characters", "scenes": "scenes", "props": "props",
                "character": "characters", "scene": "scenes", "prop": "props"}
    created, skipped = [], []
    # 项目内 ep/sc id → 供归属
    for ref in (missing or []):
        if not ref:
            continue
        kind = kind_map.get(str(ref.get("kind") or "").lower())
        name = str(ref.get("name") or "").strip()
        if not kind or not name:
            skipped.append({"name": name or "<空名>", "reason": "invalid"})
            continue
        prefix, _, variant = name.rpartition("-")
        prefix = prefix or name
        variant = variant or ("基础形象" if kind == "characters" else "")
        # 已存在 → skip
        dup = [it for it in library.list_items(kind)
               if (it.get("prefix") == prefix and (it.get("variant_name") or "") == variant)]
        if dup:
            skipped.append({"name": name, "reason": "already_in_lib", "existing_id": dup[0].get("id")})
            continue
        # 提示词：按 kind 的补充模板 + 全局 gen_prompt（可空；空则由用户后续在卡上填/单图生成时组装）
        prompt = (prompt_by_kind or {}).get(kind, "") or gen_prompt
        asset = library.new_asset(kind, prefix, variant, prompt=prompt)
        # 归属：解析来源场 scenes "ep:sc" → 项目/集/场 三层；无 scenes 则只挂项目级
        asset["scopes"] = {"projects": [pid], "episodes": [], "scenes": []}
        ep_ids, sc_ids = [], []
        for loc in (ref.get("scenes") or []):
            eid_s, _, sid = str(loc).partition(":")
            # 校验该 ep/sc 确属本项目（防脏引用）
            ep = next((e for e in (proj.get("episodes") or []) if e.get("id") == eid_s), None)
            if not ep:
                continue
            if eid_s not in ep_ids:
                ep_ids.append(eid_s)
            if sid and sid in {s.get("id") for s in (ep.get("scenes") or [])} and sid not in sc_ids:
                sc_ids.append(sid)
        if ep_ids:
            asset["scopes"]["episodes"] = ep_ids
        if sc_ids:
            asset["scopes"]["scenes"] = sc_ids
        library.save_item(kind, asset)
        created.append({"kind": kind, "name": name, "id": asset["id"]})
    return {"created": created, "skipped": skipped}


def _match_style_template(style: str):
    """v1.55：按 art_style 精确匹配 static/style-templates.json 模板（id/name 同值）；无则 None。"""
    p = Path(__file__).resolve().parent / "static" / "style-templates.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    for it in (data.get("items") or []):
        if it.get("id") == style or it.get("name") == style:
            return it
    return None


def assemble_prompt(pid: str, eid: str = "", sid: str = "", extra: str = "") -> dict:
    """按 4.4 组装提示词（场粒度）。返回 {prompt, parts, marks, assets}（parts 便于前端展示/复制）。"""
    proj = storage.load_project(pid) if pid else None
    ep = storage.find_episode(proj, eid) if (proj and eid) else None
    sc = storage.find_scene(proj, eid, sid) if (proj and eid and sid) else None
    style = (proj.get("art_style") or "").strip() if proj else ""

    # 出场资产（场景/角色/道具 变体提示词）
    assets = _scene_asset_ids(pid, eid, sid)
    scene_prompts = [a["prompt"] for a in assets if a["kind"] == "scenes" and a["prompt"]]
    role_prompts = [a["prompt"] for a in assets if a["kind"] == "characters" and a["prompt"]]
    prop_prompts = [a["prompt"] for a in assets if a["kind"] == "props" and a["prompt"]]
    text = (sc.get("text") or "") if sc else ""
    marks = _inline_marks(text)

    parts = []
    if style:
        parts.append(f"【全局画风】{style}")
        # v1.55：按 art_style 匹配 style-templates.json 模板，注入 positive/negative 段（在现有拼接上扩展，不推翻）
        tpl = _match_style_template(style)
        if tpl:
            pos = (tpl.get("positive_template") or "").strip()
            neg = (tpl.get("negative_template") or "").strip()
            if pos:
                parts.append(f"【画风正向】{pos}")
            if neg:
                parts.append(f"【画风负向】{neg}")
    if scene_prompts:
        parts.append(f"【场景】{('；'.join(scene_prompts))}")
    if role_prompts:
        parts.append(f"【出场角色】{('；'.join(role_prompts))}")
    if prop_prompts:
        parts.append(f"【道具】{('；'.join(prop_prompts))}")
    if text:
        parts.append(f"【本场分镜】{text}")
    if marks:
        parts.append("【运镜/景别/时长】" + "；".join(f"{k}={v}" for k, v in marks))
    if extra:
        parts.append(extra)

    prompt = "\n".join(parts)
    return {"prompt": prompt, "parts": parts, "marks": marks,
            "assets": [{"kind": a["kind"], "name": f"{a['prefix']}-{a['variant_name']}"} for a in assets]}


# ==================== 任务卡（outbox） ====================

def create_task_card(target_type: str, name: str, resource_path: str = "",
                     prompt: str = "", meta: dict | None = None) -> dict:
    """生成去程任务卡写入 handoff\\outbox\\{id}.json。返回卡片 dict。"""
    ensure_outbox()
    card = {
        "id": new_id("tc_"),
        "target_type": target_type,     # 角色/场景/道具/场图片/场视频
        "name": name,                    # 名称（名称-变体名称 / 场标题）
        "resource_path": resource_path,  # 资产路径（相对 SharedAssets）或场 id
        "prompt": prompt,                # 组装好的完整提示词
        "meta": meta or {},              # 附加（kind/id/pid/eid/sid 等）
        "created_at": now_iso(),
        "expires_at": now_iso(),         # 时间窗起点（6-3 时间窗关联用，可设窗宽）
        "status": ST_OPEN,
    }
    data_io.atomic_write_json(OUTBOX_DIR / f"{card['id']}.json", card)
    return card


def list_task_cards(limit: int = 200) -> list:
    ensure_outbox()
    cards = []
    for p in OUTBOX_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cards.append(json.load(f))
        except (OSError, ValueError):
            continue
    cards.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return cards[:limit]


def find_active_cards(window_seconds: int = 0) -> list:
    """时间窗内未完成任务卡（供自动登记时间窗关联 / 6-3）。"""
    cards = list_task_cards(limit=500)
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(seconds=window_seconds) if window_seconds else None
    out = []
    for c in cards:
        if c.get("status") != ST_OPEN:
            continue
        if cutoff:
            try:
                if datetime.fromisoformat(c["created_at"]) < cutoff:
                    continue
            except Exception:
                pass
        out.append(c)
    return out


def mark_card_done(card_id: str) -> bool:
    cards = list_task_cards(limit=500)
    for c in cards:
        if c.get("id") == card_id and c.get("status") == ST_OPEN:
            c["status"] = ST_DONE
            c["done_at"] = now_iso()
            data_io.atomic_write_json(OUTBOX_DIR / f"{c['id']}.json", c)
            return True
    return False
