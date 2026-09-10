# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 行为一致性 diff=0 + 门面转发同一性（B-20260909-01）。

对齐测试方案 §3.1「行为一致性：DAO 封装前后同一数据同操作 diff=0」：
  - 门面 = re-export 底层**同一函数对象**（dao.storage is storage 等），故对任何输入
    输出必然逐位一致（diff=0 恒成立）。此处以「同一性断言」形式固化为回归测试，
    防止未来有人把门面改成"包装+重实现"而引入行为漂移。
  - 真实数据只读对照：对线上 data/ 做只读操作，直连根模块 vs 经 dao 输出 diff=0
    （验证 main 切 dao 后真实数据访问路径一致；不做任何写操作，不污染线上）。
"""
import json
from pathlib import Path

import pytest


# ==================== 门面转发同一性（核心防漂移） ====================

def test_dao_exposes_same_module_objects():
    """dao.storage/library/assets_index/data_io = 根模块同一对象（行为 100% 不变的保证）。"""
    import storage, library, assets_index, data_io
    import dao
    assert dao.storage is storage
    assert dao.library is library
    assert dao.assets_index is assets_index
    assert dao.data_io is data_io


def test_dao_all_public_functions_are_same_callables():
    """dao 门面所有暴露子模块的 public 函数 = 底层同名函数对象（零包装漂移）。"""
    import storage, library, assets_index, data_io
    import dao

    for mod in (storage, library, assets_index, data_io):
        pub = [n for n in dir(mod) if not n.startswith("_")]
        for name in pub:
            obj = getattr(mod, name)
            if callable(obj):
                # dao.<modname>.<name> 必须存在且与底层同一对象
                got = getattr(getattr(dao, mod.__name__), name)
                assert got is obj, f"{mod.__name__}.{name} 被门面包装成不同对象!"


def test_dao_public_namespace_complete():
    """dao 暴露的全部数据域子模块命名空间完整（__all__ 覆盖数据层各域 + 原子写层）。

    F5(审计20260909) 门面收编后: 原四模块(storage/library/assets_index/data_io)
    + 新增 draft_store/batch_queue 两数据域。
    """
    import dao
    assert dao.__all__ == ["storage", "library", "assets_index",
                           "draft_store", "batch_queue", "data_io"]


# ==================== 真实数据只读行为对照（diff=0） ====================

@pytest.fixture
def real_root():
    """工坊真实 data 目录（只读对照用）。"""
    ROOT = Path(__file__).resolve().parents[2]
    return ROOT / "data"


def test_real_data_read_diff_zero(real_root):
    """对线上真实数据做只读操作：直连根 vs 经 dao 输出 diff=0。

    仅读不写（list/load/get/summary），验证 main 切 dao 后真实数据访问一致。
    跳过条件：线上 data 不存在或为空（如 CI 无真实数据）则不强制。
    """
    import storage, library, assets_index
    import dao

    # 注意：此测试不 monkeypatch DATA_DIR —— 直接读线上 data（只读操作）
    # 保证数据层真实路径与 main 一致。
    if not (real_root / "projects.json").exists():
        pytest.skip("无线上 data/projects.json，跳过真实数据对照")

    # 项目域
    assert storage.list_projects() == dao.storage.list_projects()
    idx = storage.load_projects_index()
    assert idx == dao.storage.load_projects_index()
    for proj in idx.get("projects", []):
        assert storage.load_project(proj["id"]) == dao.storage.load_project(proj["id"])

    # 资产域（三库 + references + settings）—— 只读
    for kind in library.KINDS:
        assert library.list_items(kind) == dao.library.list_items(kind)
    assert library.list_references() == dao.library.list_references()
    assert library.load_settings() == dao.library.load_settings()

    # 索引域 —— get_summary 只读
    assert assets_index.get_summary() == dao.assets_index.get_summary()
