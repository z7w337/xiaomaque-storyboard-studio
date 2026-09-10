# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 共享资产索引域（assets_index 转发）单元测试（B-20260909-01）。

覆盖（对齐测试方案 §3.1 + 对账红线）：register_file（new/version/skip/missing/ignored/越界）、
mark_deleted、list_entries（状态/来源过滤）、get_summary、reconcile 全量对账、
entry_ref_of、行为一致性（经 dao 与直连根模块一致）。

注：register_file 需真实媒体文件（算 sha256/size/mtime）→ 测试在隔离共享目录建真文件。
"""
import json
from pathlib import Path

import pytest

# 统一经 dao 门面访问
from dao import assets_index
from dao import library


def _make_media(shared_dir: Path, rel: str, content: bytes = b"hello media") -> str:
    """在隔离共享目录建一个真实媒体文件，返回规范化相对路径。"""
    p = shared_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return rel.replace("\\", "/").lstrip("/")


def _real_shared_dir(iso_data_dir):
    """返回当前测试的隔离共享目录（与 conftest 里 mock 的 get_shared_assets_path 一致）。"""
    _, shared_dir = iso_data_dir
    return shared_dir


# ==================== register_file ====================

def test_register_new(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    rel = _make_media(shared, "output/uploads/new_shot.png", b"AAA")
    r = assets_index.register_file(rel)
    assert r["action"] == "new"
    assert r["entry"]["path"] == rel
    assert r["entry"]["status"] == "in_use"
    assert r["entry"]["source"]  # 有来源


def test_register_skip_same_content(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    rel = _make_media(shared, "output/up/x.png", b"same")
    assets_index.register_file(rel)
    r2 = assets_index.register_file(rel)  # 同路径同 sha → skip
    assert r2["action"] == "skip"


def test_register_version_on_change(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    rel = "output/up/v.png"
    _make_media(shared, rel, b"v1")
    assets_index.register_file(rel)
    _make_media(shared, rel, b"v2-changed")  # 内容变化
    r = assets_index.register_file(rel)
    assert r["action"] == "version"
    # 旧记录变 history
    entries = assets_index.load_index()["entries"]
    in_use = [e for e in entries if e["status"] == "in_use"]
    hist = [e for e in entries if e["status"] == "history"]
    assert len(in_use) == 1 and len(hist) == 1


def test_register_missing_file(iso_data_dir):
    r = assets_index.register_file("output/up/nothere.png")
    assert r["action"] == "missing"


def test_register_non_media_ignored(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    (shared / "output").mkdir(parents=True, exist_ok=True)
    (shared / "output" / "note.txt").write_text("x")
    r = assets_index.register_file("output/note.txt")
    assert r["action"] == "ignored"


def test_register_path_escape_ignored(iso_data_dir):
    # 目录穿越 → 越界拒绝
    r = assets_index.register_file("../../etc/passwd")
    assert r["action"] == "ignored"


def test_register_empty_path_ignored(iso_data_dir):
    assert assets_index.register_file("")["action"] == "ignored"


# ==================== mark_deleted / list_entries / summary ====================

def test_mark_deleted(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    rel = _make_media(shared, "output/up/del.png")
    assets_index.register_file(rel)
    r = assets_index.mark_deleted(rel)
    assert r == {"changed": True, "rel": rel}
    # 再删幂等（无 in_use 可标 → changed=False）
    assert assets_index.mark_deleted(rel)["changed"] is False


def test_list_entries_filter_by_status_and_source(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    a = _make_media(shared, "output/up/a.png")
    b = _make_media(shared, "unassigned/b.png")
    assets_index.register_file(a, source="ic")
    assets_index.register_file(b, source="unassigned")
    assets_index.mark_deleted(a)

    all_e = assets_index.list_entries()
    in_use = assets_index.list_entries(status="in_use")
    del_e = assets_index.list_entries(status="deleted")
    src_un = assets_index.list_entries(source="unassigned")
    assert len(all_e) == 2
    assert len(in_use) == 1 and in_use[0]["path"] == b
    assert len(del_e) == 1 and del_e[0]["path"] == a
    assert len(src_un) == 1 and src_un[0]["path"] == b


def test_get_summary_and_unclassified(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    _make_media(shared, "output/up/z.png")
    _make_media(shared, "unassigned/u1.png")
    _make_media(shared, "unassigned/u2.png")
    assets_index.register_file("output/up/z.png", source="ic")
    assets_index.register_file("unassigned/u1.png", source="unassigned")
    assets_index.register_file("unassigned/u2.png", source="unassigned")

    sm = assets_index.get_summary()
    assert sm.get("in_use") == 3 or sm.get("total") == 3
    # 收尾A④：register_file 感知裸图即自动资产化（指向原图）→ 图进入素材卡、脱离未分类
    # 未分类视图消灭（感知即资产化后无游离未分类实体）
    unc = assets_index.unclassified_entries()
    assert len(unc) == 0, f"register 感知后应自动资产化, 未分类应归零, 实得 {len(unc)}"
    # 三图均已建成 references 素材卡（方案A：images[0]=原图）
    refs = library.list_references()
    paths = {library.ref_primary_path(r) for r in refs}
    for expect in ("output/up/z.png", "unassigned/u1.png", "unassigned/u2.png"):
        assert expect in paths, f"裸图 {expect} 应已被自动资产化建卡"
    # 若再对同图 register → 幂等（不重复建卡）
    assets_index.register_file("output/up/z.png", source="ic")
    assert len(library.list_references()) == 3, "重复感知不重复建卡（幂等）"


# ==================== reconcile 全量对账（红线：对账保留） ====================

def test_reconcile_detects_new_file(iso_data_dir):
    shared = _real_shared_dir(iso_data_dir)
    # 直接放一个文件不入索引
    _make_media(shared, "output/up/orphan.png", b"orphan")
    # 既有索引里放一个"已删除"记录
    del_rel = _make_media(shared, "output/up/gone.png")
    assets_index.register_file(del_rel)
    # 模拟文件真被删
    (shared / del_rel).unlink()

    r = assets_index.reconcile()
    # orphan.png 应被登记 in_use；gone.png 应被标记 deleted
    entries = assets_index.load_index()["entries"]
    paths = {e["path"]: e["status"] for e in entries}
    assert "output/up/orphan.png" in paths and paths["output/up/orphan.png"] == "in_use"
    assert paths[del_rel] == "deleted"


# ==================== 索引文件损坏恢复（崩溃恢复） ====================

def test_load_corrupt_index_restore_or_blank(iso_data_dir, monkeypatch):
    shared = _real_shared_dir(iso_data_dir)
    vdir = shared / ".versions"
    vdir.mkdir(parents=True, exist_ok=True)
    # 主索引坏 + 无 bak → 重置空（带 _index_warning）
    (vdir / "index.json").write_text("{bad", encoding="utf-8")
    data = assets_index.load_index()
    assert data.get("_index_warning")  # 重置并提示
    assert data["entries"] == []


def test_init_index_idempotent(iso_data_dir):
    assets_index.init_index()  # 重复调幂等
    assets_index.init_index()
    assert (iso_data_dir[1] / ".versions" / "index.json").exists()


# ==================== 行为一致性 ====================

def test_assets_index_forward_is_same_object():
    import assets_index as root_ai
    assert assets_index is root_ai
    for fn in ("register_file", "mark_deleted", "list_entries", "unclassified_entries",
               "get_summary", "reconcile", "entry_ref_of", "init_index"):
        assert getattr(assets_index, fn) is getattr(root_ai, fn)
