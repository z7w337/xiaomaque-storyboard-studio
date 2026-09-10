# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 pytest 夹具（B-20260909-01）。

隔离策略（红线：测试不污染真实 data/）：
  - monkeypatch storage.DATA_DIR / library.DATA_DIR 到每测试的 tmp_path 下，
    使三库/projects/refs 读写全部落在临时目录；
  - monkeypatch library.get_shared_assets_path() 返回 tmp 共享目录，
    使 assets_index 的 .versions 索引与 refs/ 落在临时目录；
  - 因底层 storage/library/assets_index 是模块级 DATA_DIR 全局，运行时按模块属性取，
    monkeypatch.setattr(模块, 'DATA_DIR', tmp) 即生效（函数读的是模块全局名）。
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

# 保证能 import 工坊根模块（dao/storage/library/... 与工坊根同级）
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def iso_data_dir(tmp_path, monkeypatch):
    """把数据层重定向到 tmp_path 下的隔离 data + 共享目录，返回 (data_dir, shared_dir)。

    每次测试独立 tmp_path，互不污染；测试后 tmp_path 自动清理。
    """
    import storage
    import library
    import assets_index

    data_dir = tmp_path / "data"
    shared_dir = tmp_path / "shared_assets"

    # 重定向模块级 DATA_DIR 常量（storage/library 在运行时按模块全局读取）
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "PROJECTS_DIR", data_dir / "projects")
    monkeypatch.setattr(storage, "INDEX_FILE", data_dir / "projects.json")
    monkeypatch.setattr(library, "DATA_DIR", data_dir)

    # assets_index 路径经 library.get_shared_assets_path() 动态取 → mock 到 tmp 共享目录
    monkeypatch.setattr(library, "get_shared_assets_path", lambda: str(shared_dir))

    # 初始化各存储层（幂等）
    storage.init_storage()
    library.init_library()
    # assets_index.init_index() 会建 .versions；observer/reconcile 线程不开（测试确定性）
    assets_index.init_index()

    return data_dir, shared_dir
