# -*- coding: utf-8 -*-
"""批3-3b · 删除存量 5 张空 references 卡（用户裁定 B，2026-09-09 23:0x）。

背景：refs/ 上传/GS 保存历史单参建卡缺陷，产生 5 张无主图空卡（prefix 塞 refs/ 路径、path/images 空）。
用户裁定 B=删除（ref_count=0 安全，连物理文件，素材库净减 5）。

红线遵守：
  - **备份**：references.json 整文件副本 + 5 张空卡完整 JSON + 5 个物理文件移入备份目录（可回滚，非直接 unlink）
  - **幂等**：空卡已不存在则跳过（可重复执行）
  - 删除语义对齐现有 delete_reference 端点（记录删 + ref_count=0 才删物理 + assets_index.mark_deleted）
  - 只删这 5 张确认的空卡，不误伤其它（含 4 张有主图的有效素材卡）
  - H3/三库/对账不动

用法：python 删除脚本 → 输出迁移前后核对
"""
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library
import assets_index
import data_io


# 待删空卡 id（用户裁定 B，2026-09-09 确认清单）
TARGET_IDS = {
    "re_3c072d37a7c7", "re_d826c618229c", "re_06afab17a507",
    "re_012cd10e5a42", "re_6abe97eec462",
}


def backup_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bd = ROOT / "data" / f"backup_{ts}_3b_emptycard_cleanup"
    bd.mkdir(parents=True, exist_ok=True)
    return bd


def run() -> dict:
    # 1) 备份 references.json 整文件
    ref_path = ROOT / "data" / "references.json"
    bd = backup_dir()
    shutil.copy2(ref_path, bd / "references.json.bak")

    items = json.loads(ref_path.read_text(encoding="utf-8")).get("items", [])
    shared_root = Path(library.get_shared_assets_path()).resolve()

    before = len(items)
    deleted_records = []
    deleted_physical = []
    kept_for_refs = []

    # 逐张处理（幂等：若某 id 已不存在则跳过）
    for it in list(items):
        if it.get("id") not in TARGET_IDS:
            continue
        # 该空卡确认为"无主图空卡"才删（防误删有图卡）
        im = it.get("images") or []
        has_img = bool(im and isinstance(im[0], dict) and im[0].get("path"))
        has_path = bool(it.get("path"))
        if has_img or has_path:
            continue  # 非空卡，跳过不误删

        rid = it["id"]
        # 物理路径：从 prefix 提取 refs/ 前缀
        pfx = str(it.get("prefix") or "")
        phys_rel = pfx if pfx.startswith("refs/") else ""
        # ref_count 必须为 0（安全删除前提）；>0 则保留记录仅记 kept
        if (it.get("ref_count") or 0) > 0:
            kept_for_refs.append(rid)
            continue

        # 备份物理文件（移动到备份目录，可回滚）
        phys_deleted = False
        if phys_rel:
            full = (shared_root / phys_rel).resolve()
            if str(full).startswith(str(shared_root)) and full.is_file():
                try:
                    shutil.move(str(full), str(bd / full.name))
                    phys_deleted = True
                except OSError:
                    phys_deleted = False

        # 备份卡 JSON 记录
        (bd / f"record_{rid}.json").write_text(
            json.dumps(it, ensure_ascii=False, indent=2), encoding="utf-8")

        # 删记录 + 索引标记
        library.delete_reference(rid)
        if phys_rel:
            try:
                assets_index.mark_deleted(phys_rel)
            except Exception:
                pass

        deleted_records.append(rid)
        if phys_deleted:
            deleted_physical.append(phys_rel)

    after = len(library.list_references())
    return {
        "backup_dir": str(bd),
        "references_before": before,
        "references_after": after,
        "deleted_records": deleted_records,
        "deleted_physical": deleted_physical,
        "kept_for_refs": kept_for_refs,
    }


if __name__ == "__main__":
    res = run()
    print(json.dumps(res, ensure_ascii=False, indent=2))
