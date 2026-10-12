# -*- coding: utf-8 -*-
"""发布前收口整改 B-20260910-07 ① · 清理收尾批A④真机验证遗留的测试图与素材卡。

背景：收尾批A④（register_file 自动资产化）真机验证时，向 SharedAssets/uploads 复制了测试图
      batchA_test_auto.png，observer 感知后自动建卡 re_0ce4b5b18411（images[0] 指向该原图）。
      发布前须清理该测试痕迹（规划-9 B-20260910-07 ①：备份→删→核销）。

红线遵守：
  - **备份**：references.json 整文件副本 + 该卡完整 JSON + 物理文件移入备份目录（可回滚，非直接 unlink）
  - **幂等**：卡/文件任一已不存在则跳过（可重复执行）
  - 删除语义对齐现有 delete_reference 端点（删记录 + assets_index.mark_deleted）
  - 只删这一张卡/一个文件，不误伤其它（比对主图路径，不符则跳过）
  - H3 / 三库 / 对账不动

用法：python tools/cleanup_b07_test_artifact.py
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library
import assets_index


# 目标（规划-9 B-20260910-07 ① 指令指定）
TARGET_REF_ID = "re_0ce4b5b18411"
TARGET_REL = "uploads/batchA_test_auto.png"


def backup_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bd = ROOT / "data" / f"backup_{ts}_b07_test_artifact_cleanup"
    bd.mkdir(parents=True, exist_ok=True)
    return bd


def run() -> dict:
    ref_path = ROOT / "data" / "references.json"
    bd = backup_dir()

    # 1) 备份 references.json 整文件
    shutil.copy2(ref_path, bd / "references.json.bak")

    items = json.loads(ref_path.read_text(encoding="utf-8")).get("items", [])
    shared_root = Path(library.get_shared_assets_path()).resolve()
    before = len(items)

    deleted_records = []
    deleted_physical = []
    notes = []

    # 2) 删卡片记录（幂等 + 主图路径比对防误删）
    card = next((it for it in items if it.get("id") == TARGET_REF_ID), None)
    if card is None:
        notes.append(f"卡片 {TARGET_REF_ID} 已不存在（幂等跳过）")
    else:
        rel = library.ref_primary_path(card) or ""
        if rel != TARGET_REL:
            notes.append(f"卡片主图={rel!r} 非目标 {TARGET_REL!r}，跳过不误删")
        else:
            (bd / f"record_{TARGET_REF_ID}.json").write_text(
                json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
            if library.delete_reference(TARGET_REF_ID):
                deleted_records.append(TARGET_REF_ID)
            else:
                notes.append(f"delete_reference({TARGET_REF_ID}) 返回 False")

    # 3) 删物理文件（移动到备份目录，可回滚）
    full = (shared_root / TARGET_REL).resolve()
    if str(full).startswith(str(shared_root)) and full.is_file():
        try:
            shutil.move(str(full), str(bd / full.name))
            deleted_physical.append(TARGET_REL)
        except OSError as e:
            notes.append(f"物理文件移动失败：{e}")
    else:
        notes.append(f"物理文件 {TARGET_REL} 已不存在（幂等跳过）")

    # 4) 索引核销（物理文件已删 → 在用记录标 deleted）
    if TARGET_REL in deleted_physical:
        try:
            assets_index.mark_deleted(TARGET_REL)
        except Exception as e:
            notes.append(f"mark_deleted 失败：{e}")

    after = len(library.list_references())
    return {
        "backup_dir": str(bd),
        "references_before": before,
        "references_after": after,
        "deleted_records": deleted_records,
        "deleted_physical": deleted_physical,
        "notes": notes,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
