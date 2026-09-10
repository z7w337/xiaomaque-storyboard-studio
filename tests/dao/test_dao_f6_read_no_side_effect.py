# -*- coding: utf-8 -*-
"""收尾A③(F6) · 读路径零写副作用测试（B-20261010-04）。

验证：ensure_settings/list_references 的"读带写"副作用已挪至启动期 self_heal 一次，
运行期读函数（read_settings/get_shared_assets_path/get_comfyui_url/list_references）
**绝不写盘** —— 反复读后 settings.json / references.json 的 mtime 不变。
"""
import json
import os
from pathlib import Path

from dao import library


def _mtime(p: Path):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def test_read_settings_no_side_effect_on_mtime(iso_data_dir):
    """反复读配置 → settings.json mtime 不变（运行期读零写）。"""
    data_dir = iso_data_dir[0]
    p = data_dir / "settings.json"
    # 启动自愈先落一次默认（确保存在且完整）
    library.ensure_settings()
    assert p.exists()
    m0 = _mtime(p)
    # 反复读（读函数各调数次，模拟高频运行期读）
    for _ in range(5):
        library.read_settings()
        library.get_shared_assets_path()
        library.get_comfyui_url()
    assert _mtime(p) == m0, "读 settings 后 mtime 不应变（读路径零写副作用）"


def test_read_references_no_side_effect_on_mtime(iso_data_dir):
    """反复读 references → references.json mtime 不变（读不再触发迁移写）。"""
    data_dir = iso_data_dir[0]
    p = data_dir / "references.json"
    # 先建一个"旧扁平"条目触发启动迁移，确认迁移只在启动期发生
    p.write_text(json.dumps({"items": [{"id": "re_old1", "path": "refs/x.png", "ref_count": 0}]}), encoding="utf-8")
    library.startup_self_heal()  # 启动自愈：此处应完成一次迁移写盘
    m1 = _mtime(p)
    # 读后 references.json 结构已统一（有 images/prefix）
    items = library.list_references()
    assert items and items[0].get("images"), "启动自愈应已把旧条目迁移为统一结构"
    # 反复读 → mtime 不变
    for _ in range(5):
        library.list_references()
    assert _mtime(p) == m1, "读 references 后 mtime 不应变（迁移写已挪启动期）"


def test_startup_self_heal_idempotent(iso_data_dir):
    """启动自愈幂等：多次调不反复写（settings/references 已齐则零写）。"""
    data_dir = iso_data_dir[0]
    library.ensure_settings()
    sp = data_dir / "settings.json"
    rp = data_dir / "references.json"
    if not rp.exists():
        rp.write_text(json.dumps({"items": []}), encoding="utf-8")
    m_s = _mtime(sp)
    m_r = _mtime(rp)
    library.startup_self_heal()
    library.startup_self_heal()
    assert _mtime(sp) == m_s, "settings 已齐时自愈不应重复写"
    assert _mtime(rp) == m_r, "references 已迁移时自愈不应重复写"
