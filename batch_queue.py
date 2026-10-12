# -*- coding: utf-8 -*-
"""阶段 6 · 批3（6-9 批量资产提取）批量任务队列（batch_queue.py）。

目的（计划书 4.6 / B1-B7）：
  三档入口（按场/按集/全剧本）聚合出 missing 资产（库内无者，见 handoff.aggregate_scene_assets
  → missing）后，为其批量生成图片/资产。本模块承载批量队列的存储与状态机：
    - 一个 batch 一批 missing 资产（scope 三档 + model），每个资产一条 item；
    - item 状态机：pending → in_progress → done / failed；
    - 前端逐条"取下一待生成 → 出图 → 回挂 → mark done"（半自动；用户裁定缓做"自动拉起画布"，
      改由前端列表逐条确认/触发）；
    - 出图产物经 image-upload/image-bind 落库回挂（复用 main.py 现有资产图端点），落库后 mark done
      并自动回填该 missing 资产的 library id 与归属（B4 闭环）。

存储：data\\batch\\{batch_id}.json（单文件含 batch 元数据 + items 列表，避免散文件）。
状态机常量独立于此，避免与 handoff 去程卡（open/done/expired，时间窗自动关联语义）混用。
"""
import json
import uuid
from pathlib import Path

import data_io
# F5(审计20260909): batch_queue 是数据域(管理 data/batch/), 收编为 dao 门面成员(dao re-export 它)。
# 故此处直连底层 library(仅用 now_iso 取时间), 不反向依赖 dao(避免 dao→batch_queue→dao 循环)。
import library

BASE_DIR = Path(__file__).resolve().parent
BATCH_DIR = BASE_DIR / "data" / "batch"

# item 状态机
IT_PENDING = "pending"      # 待生成（未取走）
IT_IN_PROGRESS = "in_progress"  # 已取走、生成中
IT_DONE = "done"            # 成功（已建资产/回挂图）
IT_FAILED = "failed"        # 失败（可重试）
IT_SKIPPED = "skipped"      # 跳过（用户/调用方显式跳过，等价库内已存在）

# batch 状态
ST_OPEN = "open"            # 队列活动（仍有 pending/in_progress 项）
ST_DONE = "done"            # 全部 done/skipped
ST_CANCELLED = "cancelled"  # 用户取消


def now_iso() -> str:
    return library.now_iso()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _batch_path(batch_id: str) -> Path:
    return BATCH_DIR / f"{batch_id}.json"


def _ensure_dir() -> None:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)


def _load(batch_id: str) -> dict | None:
    p = _batch_path(batch_id)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(batch: dict) -> None:
    _ensure_dir()
    data_io.atomic_write_json(_batch_path(batch["id"]), batch)


# ---- B1：创建批量队列 ----

def create_batch(pid: str, missing: list, *, eid: str = "", sid: str = "",
                 scope_label: str = "", model_id: str = "",
                 gen_prompt: str = "", ref_paths: list | None = None,
                 note: str = "") -> dict:
    """由 missing 资产列表创建一批队列。

    missing: handoff.aggregate_scene_assets 返回的 missing[] 每项须含：
        {name: "名称-变体", kind: characters/scenes/props, tags: [...], scenes: [ep:sc,...]}
    kind_map 兼容 "characters/scenes/props" 与单数 "character/scene/prop"。
    """
    _ensure_dir()
    kind_map = {"characters": "characters", "scenes": "scenes", "props": "props",
                "character": "characters", "scene": "scenes", "prop": "props"}
    items = []
    seen = set()
    for m in missing:
        if not m:
            continue
        kind = kind_map.get(str(m.get("kind") or "").lower())
        name = str(m.get("name") or "").strip()
        if not kind or not name or name in seen:
            continue
        seen.add(name)
        # 名称-变体 → prefix / variant（统一按前缀拆分：最后一段"-"前的名称 + 变体）
        prefix, _, variant = name.rpartition("-")
        prefix = prefix or name
        variant = variant or ("基础形象" if kind == "characters" else "")
        items.append({
            "index": len(items),
            "kind": kind,                    # characters/scenes/props
            "name": name,                    # "名称-变体名称"（完整资产粒度）
            "prefix": prefix,                # 名称（分组前缀）
            "variant_name": variant,         # 变体名
            "tags": m.get("tags") or [],     # role/scene/prop
            "scenes": m.get("scenes") or [], # 来源场列表 ["ep:sc", ...]
            "gen_prompt": gen_prompt,        # 该资产组装提示词（可空，落库时补）
            "ref_paths": list(ref_paths or []),  # 参考图路径（生图输入），逐 item 可覆盖
            "status": IT_PENDING,
            "library_id": "",                # 出图落库后回填的库资产 id
            "image_path": "",                # 回填后的图路径（相对 SharedAssets）
            "error": "",
            "retries": 0,
            "updated_at": now_iso(),
        })
    batch = {
        "id": new_id("bq_"),
        "kind": "batch_extract",
        "pid": pid, "eid": eid, "sid": sid,   # scope 三档（均空=全剧本）
        "scope_label": scope_label,           # 人类可读范围（如 "全剧本" / "第1集" / "场-xxxx"）
        "model_id": model_id,
        "note": note,
        "status": ST_OPEN,
        "items": items,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "summary": {"total": len(items), "done": 0, "failed": 0, "skipped": 0, "in_progress": 0},
    }
    _save(batch)
    return batch


def list_batches(limit: int = 100) -> list:
    """按 created_at 倒序列出全部批次（仅轻量元数据，含计数）。"""
    _ensure_dir()
    out = []
    for p in BATCH_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                b = json.load(f)
        except (OSError, ValueError):
            continue
        out.append(b)
    out.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return out[:limit]


def get_batch(batch_id: str) -> dict | None:
    return _load(batch_id)


def delete_batch(batch_id: str) -> bool:
    """删除批次（幂等）。仅允许已终结（done/cancelled）批次物理删除。"""
    b = _load(batch_id)
    if not b:
        return False
    if b.get("status") not in (ST_DONE, ST_CANCELLED):
        raise ValueError("批次未终结，不可删除（先取消或待全部完成）")
    try:
        _batch_path(batch_id).unlink()
        return True
    except OSError:
        return False


def _refresh_summary(batch: dict) -> None:
    s = {"total": len(batch.get("items") or []), "done": 0, "failed": 0,
         "skipped": 0, "in_progress": 0, "pending": 0}
    for it in (batch.get("items") or []):
        st = it.get("status")
        if st == IT_DONE:
            s["done"] += 1
        elif st == IT_FAILED:
            s["failed"] += 1
        elif st == IT_SKIPPED:
            s["skipped"] += 1
        elif st == IT_IN_PROGRESS:
            s["in_progress"] += 1
        elif st == IT_PENDING:
            s["pending"] += 1
    batch["summary"] = s
    # 全 done/skipped → batch done
    if s["total"] and (s["pending"] + s["in_progress"]) == 0 and batch.get("status") == ST_OPEN:
        batch["status"] = ST_DONE
        batch["finished_at"] = now_iso()
    batch["updated_at"] = now_iso()


def _persist(batch: dict) -> dict:
    _refresh_summary(batch)
    _save(batch)
    return batch


# ---- B3：取下一待生成 / 标记进行中 ----

def next_item(batch_id: str) -> dict | None:
    """取该批次第一条 pending 项并置为 in_progress（原子化：读改写落盘）。无则返回 None。"""
    b = _load(batch_id)
    if not b or b.get("status") != ST_OPEN:
        return None
    for it in (b.get("items") or []):
        if it.get("status") == IT_PENDING:
            it["status"] = IT_IN_PROGRESS
            it["updated_at"] = now_iso()
            _persist(b)
            return {**it, "batch_id": batch_id}
    return None


def cancel_batch(batch_id: str) -> dict | None:
    """取消批次：所有 pending/in_progress 项置 failed(cancelled)，batch → cancelled。"""
    b = _load(batch_id)
    if not b:
        return None
    if b.get("status") != ST_OPEN:
        return b
    for it in (b.get("items") or []):
        if it.get("status") in (IT_PENDING, IT_IN_PROGRESS):
            it["status"] = IT_FAILED
            it["error"] = "cancelled"
    b["status"] = ST_CANCELLED
    return _persist(b)


# ---- B4 闭环：登记/失败/跳过 回写 ----

def mark_item_done(batch_id: str, index: int, *, library_id: str = "",
                   image_path: str = "", gen_prompt: str = "", ref_path: str = "") -> dict | None:
    """出图落库成功后回调：回填库资产 id + 图路径，置 done。返回更新后的 item。"""
    b = _load(batch_id)
    if not b:
        return None
    it = next((i for i in (b.get("items") or []) if i.get("index") == index), None)
    if not it:
        return None
    it["status"] = IT_DONE
    if library_id:
        it["library_id"] = library_id
    if image_path:
        it["image_path"] = image_path
    if gen_prompt:
        it["gen_prompt"] = gen_prompt
    if ref_path:
        it.setdefault("ref_paths", []).append(ref_path) if ref_path not in it.get("ref_paths", []) else None
    it["error"] = ""
    it["updated_at"] = now_iso()
    _persist(b)
    return it


def mark_item_failed(batch_id: str, index: int, error: str = "",
                     max_retries: int = 3) -> dict | None:
    """生成失败回调：记录 error；retries 超限才置 failed（否则回 pending 待重取）。"""
    b = _load(batch_id)
    if not b:
        return None
    it = next((i for i in (b.get("items") or []) if i.get("index") == index), None)
    if not it:
        return None
    it["retries"] = int(it.get("retries") or 0) + 1
    it["error"] = error
    it["updated_at"] = now_iso()
    if it["retries"] >= max_retries:
        it["status"] = IT_FAILED
    else:
        it["status"] = IT_PENDING  # 允许重取重试
    _persist(b)
    return it


def mark_item_skipped(batch_id: str, index: int, reason: str = "") -> dict | None:
    """显式跳过（如出图前发现库内已存在同 名称-变体）。"""
    b = _load(batch_id)
    if not b:
        return None
    it = next((i for i in (b.get("items") or []) if i.get("index") == index), None)
    if not it:
        return None
    it["status"] = IT_SKIPPED
    it["error"] = reason
    it["updated_at"] = now_iso()
    _persist(b)
    return it
