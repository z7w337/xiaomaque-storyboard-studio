# -*- coding: utf-8 -*-
"""批3-3a + F5 · 门面收敛一致性测试（B-20260909-02，用户裁定低成本切换预留；审计 20260909 F5）。

3a 验证目标：各上层消费方从"直接 import library 底层"收敛为"经 dao 门面"后，
经 dao 获取的仍是底层**同一函数/模块对象**（行为 100% 不变）；散落直连已被消除。

F5 追加验证目标：draft_store/batch_queue 两数据域收编进 dao 门面（__all__ 覆盖 6 数据域），
main 改经 dao 访问；handoff 经 dao 收编（assets_index→handoff 已解耦为函数内延迟）。
层归属澄清：数据域模块(batch_queue/draft_queue/storage/library/assets_index)被 dao re-export,
允许其内部直连底层工具(data_io)或平级库(library.now_iso), 不适用"上层收敛"检查。
"""
import pytest


def test_upper_modules_import_via_dao():
    """上层业务模块 import 的 library 应为 dao.library（=根 library 同一对象）。

    ai/library_audit 是上层消费方, 经 dao 取 library。
    """
    import library as root_lib
    import dao
    assert dao.library is root_lib

    import ai, library_audit
    assert ai.library is root_lib
    assert library_audit.library is root_lib


def test_handoff_import_via_dao_F5():
    """F5: handoff 已改经 dao 取 library/storage（assets_index→handoff 已解耦为函数内延迟）。"""
    import dao
    import handoff
    assert handoff.library is dao.library
    assert handoff.storage is dao.storage


def test_dao_reexports_all_data_domains_F5():
    """F5: dao 门面收编全部数据域(draft_store/batch_queue 已纳入), __all__ 覆盖 6 数据域+data_io。

    数据域模块(batch_queue/draft_queue)作为 dao 成员被 re-export, main 经 dao 访问即同一对象。
    """
    import dao
    import batch_queue as bq_root, draft_store as ds_root
    import storage as st_root, library as lib_root, assets_index as ai_root
    assert dao.batch_queue is bq_root
    assert dao.draft_store is ds_root
    assert dao.storage is st_root
    assert dao.library is lib_root
    assert dao.assets_index is ai_root
    assert set(dao.__all__) == {"storage", "library", "assets_index",
                                "draft_store", "batch_queue", "data_io"}


def test_comfy_client_functions_from_dao():
    """comfy_client 经 dao.library 取的函数 = 根 library 同一函数（调用点同名绑定零改）。"""
    import library as root_lib
    import comfy_client
    for fn in ("get_comfyui_url", "get_shared_assets_path", "list_items",
               "new_id", "ensure_settings"):
        assert getattr(comfy_client, fn) is getattr(root_lib, fn), f"{fn} 未来自 dao.library"


def test_asset_validator_delayed_import_via_dao():
    """asset_validator 函数内延迟 from dao import library 取同名函数。"""
    import library as root_lib
    import asset_validator
    # 触发函数内延迟 import 的路径：audit_all_library(空输入只读) 经 dao 正常执行
    r = asset_validator.audit_all_library({})  # 空 items_by_kind → 触发内部 from dao import library
    assert "total_cards" in r  # 正常返回, 未抛 import/循环错误
    assert asset_validator._resolve_path is not None  # 模块正常


def test_assets_index_handoff_is_delayed_F5():
    """F5: assets_index 不再模块级 import handoff(改为函数内延迟), 无 dao→assets_index→handoff 加载依赖。"""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "assets_index.py").read_text(encoding="utf-8")
    # 模块级 import handoff 已移除
    top = src.split("def _match_open_task")[0]
    assert "import handoff" not in top, "assets_index 模块级仍 import handoff"
    assert "from handoff import" in src, "_match_open_task 内未延迟 import handoff"


def test_no_scattered_direct_import_in_upper_business():
    """上层业务模块不再直接 import library(数据域直连已被 dao 收敛)。

    检查对象=上层业务模块(被 main 直接依赖、非数据域)。例外允许直连:
      - data_io: 底层原子写工具, 各层共享, 非业务数据访问
      - batch_queue/draft_store/storage/library/assets_index: 数据域, 被 dao re-export,
        内部可直连平级库/底层工具(data_io/now_iso), 不属"业务散落"(见文件头层归属澄清)
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    upper_business = ("ai", "comfy_client", "library_audit", "asset_validator", "handoff")
    for mod in upper_business:
        src = (root / f"{mod}.py").read_text(encoding="utf-8")
        lines = [l for l in src.splitlines()
                 if l.startswith(("import library", "from library import"))]
        assert not lines, f"{mod}.py 仍直接 import library: {lines}"
