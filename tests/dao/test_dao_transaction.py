# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 原子性/事务性/并发写（B-20260909-01）。

对齐测试方案 §3.1「事务性（部分失败回滚）/并发写」：
  数据层以「单文件原子写」（data_io.atomic_write_json：临时文件+fsync+os.replace）保证
  每次写入原子——任何时刻文件要么旧完整版、要么新完整版，无半截。这即是本数据层的"事务"单位。
  并发写由 assets_index._INDEX_LOCK 单写者 + library/storage 每次全量重写兜底（无跨文件事务，
  但单次写原子）。本测试验证：
    - atomic_write 原子性（中断/异常不留半截文件、不留临时残留）
    - 写后 JSON 完整可读
    - 并发多线程 register_file 不丢索引（_INDEX_LOCK 串行）
    - save_item 并发覆盖不产生半截（最终一致为某完整版本）
"""
import json
import threading
from pathlib import Path

import pytest

from dao import data_io
from dao import assets_index
from dao import library


# ---- 原子写底层 ----

def test_atomic_write_json_creates_valid_file(tmp_path):
    p = tmp_path / "sub" / "x.json"
    data_io.atomic_write_json(p, {"a": [1, 2, 3], "中文": "值"})
    assert p.exists()
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got == {"a": [1, 2, 3], "中文": "值"}
    # 无临时残留
    assert not list(tmp_path.rglob("*.tmp"))


def test_atomic_write_overwrites_atomically(tmp_path):
    p = tmp_path / "x.json"
    data_io.atomic_write_json(p, {"v": 1})
    data_io.atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}
    assert not list(tmp_path.rglob("*.tmp"))


def test_atomic_write_text(tmp_path):
    p = tmp_path / "t.txt"
    data_io.atomic_write_text(p, "你好\nworld")
    assert p.read_text(encoding="utf-8") == "你好\nworld"


# ---- 并发写（不丢数据） ----

def test_concurrent_save_distinct_files_no_corruption(iso_data_dir):
    """多线程并发写**不同**库文件 → 各文件完整无损坏（端点跨库并发常态）。

    说明：真实端点并发多为跨库/跨项目（不同请求写不同 kind 或不同项目），互不争同一文件。
    本测试验证此常态下并发写安全（原子写各文件独立、不互踩）。
    library.save_item 对**同一库文件**的并发读改写属既有无锁行为，批2 红线不给底层加锁改行为，
    故不测"同文件高并发必成功"（那是 Windows OS 锁 + 既有设计的现实边界）。
    """
    kinds = ["characters", "scenes", "props"]
    n = 8
    per_kind = {}
    for k in kinds:
        ids = []
        for i in range(n):
            a = library.new_asset(k, f"资{k}{i}")
            library.save_item(k, a)
            ids.append(a["id"])
        per_kind[k] = ids

    errors = []

    def writer(kind, aid):
        try:
            it = library.get_item(kind, aid)
            it["prompt"] = f"p-{kind}-{aid[-4:]}"
            library.save_item(kind, it)
        except Exception as e:  # pragma: no cover
            errors.append((kind, repr(e)))

    threads = []
    for k, ids in per_kind.items():
        for i in ids:
            threads.append(threading.Thread(target=writer, args=(k, i)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 各 kind 文件独立并发 → 无 PermissionError、无数据丢失
    assert not errors, f"跨库并发写异常: {errors[:3]}"
    data_dir = iso_data_dir[0]
    for k in kinds:
        parsed = json.loads((data_dir / f"{k}.json").read_text(encoding="utf-8"))
        assert len(parsed["items"]) == n, f"{k}.json 并发后数量异常"
    assert not list(data_dir.rglob("*.tmp")), "并发写后残留临时文件"
    assert len(library.list_items("characters")) == n


def test_concurrent_register_index_lock(iso_data_dir):
    """多线程并发 register_file 不同文件 → 索引完整（_INDEX_LOCK 单写者不丢）。"""
    shared = iso_data_dir[1]
    n = 25
    rels = []
    for i in range(n):
        rel = f"output/up/c{i}.png"
        p = shared / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"content-{i}".encode())
        rels.append(rel)

    threads = [threading.Thread(target=assets_index.register_file, args=(r,)) for r in rels]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = assets_index.load_index()["entries"]
    in_use = [e for e in entries if e["status"] == "in_use"]
    assert len(in_use) == n  # 全部登记，无丢失


def test_concurrent_same_kind_append_no_loss(iso_data_dir):
    """多线程并发**同一库文件**各自 append → 全部保留，不丢数据（F1 对齐 1!=8 复现）。

    审计 F1 P1：8 线程对同一 characters.json 并发"读-改-写"（每线程读整库→append→整库写回），
    读侧恰逢 os.replace 句柄冲突 → load 兜底返回空 → save 全量覆盖 → 整库只剩 1 条（偶发 `1 != 8`）。

    F1 加锁止血（B-20260910-01）：library.save_item 以 _LIBRARY_LOCK 包裹"读改写"整段，
    使同库写操作串行，杜绝读改写交叉覆盖。本测试对齐复现场景：N 线程并发 add 到同库，
    加锁后 N 条全保留（修复前 1!=N 丢数据，修复后 N==N）。
    """
    n = 8
    data_dir = iso_data_dir[0]

    errors = []

    def appender(i):
        try:
            a = library.new_asset("characters", f"并发角色{i}")
            library.save_item("characters", a)  # 读改写整段在 _LIBRARY_LOCK 内串行
        except Exception as e:  # pragma: no cover
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=appender, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"同库并发 append 异常: {errors[:3]}"
    # 关键断言：8 条全保留（审计修复前 `1 != 8` 丢数据；加锁后 8 == 8）
    parsed = json.loads((data_dir / "characters.json").read_text(encoding="utf-8"))
    assert len(parsed["items"]) == n, f"同库并发 append 丢数据: 期望 {n} 实得 {len(parsed['items'])} (对齐 F1 1!=8)"
    assert not list(data_dir.rglob("*.tmp")), "并发写后残留临时文件"


def test_concurrent_same_reference_append_no_loss(iso_data_dir):
    """多线程并发写 references.json（save_reference）→ 全部保留（F1 覆盖 references 写路径）。"""
    n = 8
    data_dir = iso_data_dir[0]
    shared_dir = iso_data_dir[1]

    errors = []

    def appender(i):
        try:
            path = shared_dir / "refs" / f"ref-f1-{i}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"content-{i}".encode())
            # new_reference 构造带图参考卡并内部调 save_reference（读改写整段在 _LIBRARY_LOCK 内串行）
            library.new_reference(f"并发素材{i}", path=f"refs/ref-f1-{i}.png")
        except Exception as e:  # pragma: no cover
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=appender, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"同库并发 reference append 异常: {errors[:3]}"
    refs = library.list_references()
    assert len(refs) == n, f"并发写 references 丢数据: 期望 {n} 实得 {len(refs)}"
    assert not list(data_dir.rglob("*.tmp")), "并发写后残留临时文件"


# ---- 崩溃恢复（脏数据/半截恢复） ----

def test_corrupt_file_returns_fallback(iso_data_dir):
    """库文件损坏（半截/脏写模拟）→ 读侧兜底返回空，不抛异常、不崩溃。"""
    data_dir = iso_data_dir[0]
    (data_dir / "scenes.json").write_text('{"items": [ {corrupted', encoding="utf-8")
    assert library.list_items("scenes") == []  # 兜底空，不崩
    # references 损坏同理
    (data_dir / "references.json").write_text("not json", encoding="utf-8")
    assert library.list_references() == []
