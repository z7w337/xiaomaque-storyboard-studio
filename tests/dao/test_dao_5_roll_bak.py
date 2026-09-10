# -*- coding: utf-8 -*-
"""收尾批 B-07③ · G3-1 数据域滚动 .bak 回归（B-20260910-07）。

对齐审计 G3-1 建议：为三库（characters/scenes/props）、references、projects 写路径
补"滚动 .bak"——落在 data_io.atomic_write_json 的可选参数 bak_every（默认 0=关）。

断言：
  1. 默认（不传 bak_every）→ 不产生 .bak（行为与旧版完全一致，防误伤）
  2. bak_every=1 → 每次写都滚动 .bak，内容 == 刚写入的有效内容（恢复丢失≈0）
  3. bak_every=N>1 → 每 N 次写滚动一次（周期语义）
  4. 备份失败不影响主写入（fail-safe）
  5. 各数据域真实写接口（save_item/delete_item/new_reference/save_project/save_projects_index）
     均产生对应 .bak
"""
import json

import pytest


# ==================== data_io 层：参数语义 ====================

def test_default_writes_no_bak(tmp_path):
    """默认不启用 → 不产生 .bak（"行为不变"红线）。"""
    import data_io
    p = tmp_path / "d.json"
    data_io.atomic_write_json(p, {"n": 1})
    assert p.exists()
    assert not (tmp_path / "d.json.bak").exists()


def test_bak_every_1_creates_and_refreshes(tmp_path):
    """bak_every=1 → 每次都滚动 .bak，内容 == 最新有效内容（恢复丢失≈0）。"""
    import data_io
    p = tmp_path / "d.json"
    bak = tmp_path / "d.json.bak"
    data_io.atomic_write_json(p, {"n": 1}, bak_every=1)
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"n": 1}
    data_io.atomic_write_json(p, {"n": 2}, bak_every=1)
    assert json.loads(bak.read_text(encoding="utf-8")) == {"n": 2}
    assert json.loads(p.read_text(encoding="utf-8")) == {"n": 2}


def test_bak_every_n_is_periodic(tmp_path, monkeypatch):
    """bak_every=N>1 → 每 N 次写滚动一次（周期语义）。"""
    import data_io
    monkeypatch.setattr(data_io, "_BAK_WRITE_COUNT", {})
    p = tmp_path / "d.json"
    bak = tmp_path / "d.json.bak"
    for i in (1, 2):
        data_io.atomic_write_json(p, {"n": i}, bak_every=3)
        assert not bak.exists(), f"第 {i} 次不应备份"
    data_io.atomic_write_json(p, {"n": 3}, bak_every=3)
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"n": 3}


def test_bak_failure_never_blocks_write(tmp_path, monkeypatch):
    """备份失败（磁盘/权限等）静默，主写入不受影响（fail-safe）。"""
    import data_io

    class _Boom:
        @staticmethod
        def copy2(*a, **k):
            raise OSError("boom")

    monkeypatch.setattr(data_io, "shutil", _Boom)
    p = tmp_path / "d.json"
    data_io.atomic_write_json(p, {"ok": True}, bak_every=1)
    assert json.loads(p.read_text(encoding="utf-8")) == {"ok": True}


# ==================== 数据域真实调用点（隔离 tmp，不碰线上）====================

def test_three_libraries_write_rolls_bak(iso_data_dir):
    """三库（characters/scenes/props）save_item/delete_item → 对应 <file>.bak。"""
    import library
    data_dir, _ = iso_data_dir
    for kind in library.KINDS:
        library.save_item(kind, {"id": f"{kind}_bak_test", "name": "T", "images": []})
        assert (data_dir / (library.KINDS[kind]["file"] + ".bak")).exists(), kind
    assert library.delete_item("characters", "characters_bak_test") is True
    assert (data_dir / "characters.json.bak").exists()


def test_references_write_rolls_bak(iso_data_dir):
    """references 写路径 → references.json.bak。"""
    import library
    data_dir, _ = iso_data_dir
    ref = library.new_reference(prefix="t", path="uploads/t.png")
    assert (data_dir / "references.json.bak").exists()
    assert library.delete_reference(ref["id"]) is True
    assert (data_dir / "references.json.bak").exists()


def test_projects_write_rolls_bak(iso_data_dir):
    """projects 索引 + 详情写路径 → projects.json.bak / projects/<id>.json.bak。"""
    import storage
    data_dir, _ = iso_data_dir
    storage.save_projects_index({"projects": []})
    assert (data_dir / "projects.json.bak").exists()
    storage.save_project({"id": "p_bak_test", "name": "T"})
    assert (data_dir / "projects" / "p_bak_test.json.bak").exists()
