# -*- coding: utf-8 -*-
"""收尾A④ · register_file 自动资产化专项测试（B-20261010-04，用户裁定方案A：素材卡指向原图）。

覆盖验收五类：
  1. 感知即资产化：register 裸图 → references 素材卡出现、未分类不增（归零）
  2. 幂等：同文件重复感知 → 不重复建卡
  3. 全量（甲案）：reconcile 扫描存量裸图 → 全部资产化
  4. 锁序：并发"资产化 + 删卡/引用增减"交叉 → 无死锁（超时保护）、无丢数据
  5. fail-open：构造建卡失败（如不可读图）→ 不阻断 observer/reconcile 循环
"""
import threading
import time
from pathlib import Path

import pytest

from dao import assets_index, library


def _real_shared_dir(iso_data_dir):
    return Path(library.get_shared_assets_path())


def _make_media(shared: Path, rel: str, body: bytes = b"\x89PNG\r\n\x1a\nfakedata") -> str:
    p = shared / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return rel.replace("\\", "/").lstrip("/")


def _card_paths():
    return {library.ref_primary_path(r) for r in library.list_references()}


def test_auto_materialize_on_register(iso_data_dir):
    """1.感知即资产化：register 裸图 → 素材卡出现、未分类归零。"""
    shared = _real_shared_dir(iso_data_dir)
    _make_media(shared, "output/up/free1.png")
    assets_index.register_file("output/up/free1.png")
    paths = _card_paths()
    assert "output/up/free1.png" in paths, "register 感知裸图应自动建 references 卡(指向原图)"
    # 未分类归零（感知即资产化后无游离未分类）
    assert assets_index.unclassified_entries() == []


def test_auto_materialize_idempotent(iso_data_dir):
    """2.幂等：同文件重复 register → 不重复建卡。"""
    shared = _real_shared_dir(iso_data_dir)
    _make_media(shared, "output/up/idem.png")
    for _ in range(5):
        assets_index.register_file("output/up/idem.png")
    assert len(library.list_references()) == 1, "重复感知只建一张卡"


def test_reconcile_materializes_all_legacy(iso_data_dir):
    """3.全量(甲案)：reconcile 扫描存量裸图(output/uploads 均有) → 全部资产化。"""
    shared = _real_shared_dir(iso_data_dir)
    rels = []
    for base in ("output/gen/a.png", "output/gen/b.png", "uploads/c.png", "output/sub/d.png"):
        _make_media(shared, base)
        rels.append(base)
    # 不预先 register，直接 reconcile → 存量一次性全转正
    assets_index.reconcile()
    paths = _card_paths()
    for r in rels:
        assert r in paths, f"reconcile 后存量裸图 {r} 应全部资产化"
    assert assets_index.unclassified_entries() == [], "存量未分类应归零(全部转正)"


def test_auto_materialize_skips_refs_and_ref_upload(iso_data_dir):
    """refs/ 副本与已资产化源图不再重复建卡；refs/ 上传链不破坏。"""
    shared = _real_shared_dir(iso_data_dir)
    # refs/ 下的文件(素材卡主图) register → 不自动资产化(已由建卡流程处理)
    _make_media(shared, "refs/existing_ref.png")
    assets_index.register_file("refs/existing_ref.png")
    assert "refs/existing_ref.png" not in _card_paths(), "refs/ 主图不重复资产化"
    # 模拟 materialize：源图已资产化(_src_rel 记源图)→ 再 register 源图不重复建卡
    import shutil
    src = "output/srcmat.png"
    _make_media(shared, src)
    refs_dir = shared / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shared / src, refs_dir / "mat_copy.png")
    ref = library.new_reference("", "refs/mat_copy.png")
    ref["_src_rel"] = src
    library.save_reference(ref)
    assets_index.register_file(src)  # 感知源图
    n = sum(1 for r in library.list_references() if r.get("_src_rel") == src)
    assert n == 1, "materialize 已资产化的源图，register 感知不重复建卡"


def test_lock_order_no_deadlock(iso_data_dir):
    """4.锁序：并发"资产化 + 删卡/引用增减"交叉 → 无死锁、无丢数据。"""
    shared = _real_shared_dir(iso_data_dir)
    n = 6
    rels = []
    for i in range(n):
        r = _make_media(shared, f"output/lock/{i}.png")
        rels.append(r)
    stop = time.time() + 8  # 超时保护(8s)：无死锁则秒级完成
    errors = []

    def register_worker(rel):
        try:
            assets_index.register_file(rel)
        except Exception as e:
            errors.append(("reg", rel, repr(e)))

    def delete_worker(rel):
        # 并发删卡/引用增减(dec_reference_for_asset 走 _LIBRARY_LOCK)
        try:
            library.dec_reference_for_asset("some_asset")
        except Exception as e:
            errors.append(("dec", rel, repr(e)))

    threads = []
    for rel in rels:
        threads.append(threading.Thread(target=register_worker, args=(rel,)))
        threads.append(threading.Thread(target=delete_worker, args=(rel,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    assert time.time() < stop, "并发锁序超时——疑似死锁"
    assert not errors, f"并发异常: {errors[:3]}"
    # 无丢数据：每张裸图都应资产化
    paths = _card_paths()
    assert all(r in paths for r in rels), "并发资产化不应丢卡"


def test_auto_materialize_fail_open(iso_data_dir):
    """5.fail-open：register 内部建卡若异常 → 不阻断（登记仍完成，无抛错）。"""
    shared = _real_shared_dir(iso_data_dir)
    # 构造一个 register 能登记、但自动资产化 new_reference 会遇到的边界(模拟异常被吞):
    # 用一个不可建卡的场景——如文件名触发 new_id/建卡无碍; 改用一个会 raise 的: 临时 monkeypatch
    _make_media(shared, "output/failopen/x.png")
    orig_new = library.new_reference

    def boom(*a, **k):
        raise RuntimeError("模拟建卡失败(fail-open测试)")
    library.new_reference = boom
    try:
        # register 应吞掉建卡异常仍登记索引(不抛错)
        r = assets_index.register_file("output/failopen/x.png")
        assert r["action"] in ("new", "version", "skip"), f"register 应正常返回, 实得 {r}"
    finally:
        library.new_reference = orig_new
    # 该文件仍登记在索引(登记未被建卡异常阻断)
    entries = {e["path"]: e["status"] for e in assets_index.load_index()["entries"]}
    assert entries.get("output/failopen/x.png") == "in_use", "fail-open:登记不被建卡失败阻断"
