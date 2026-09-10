# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 资产库域（library 转发）单元测试（B-20260909-01）。

覆盖（对齐测试方案 §3.1）：三库(characters/scenes/props) CRUD 全量、
统一资产模型 new_asset / display_name、references（list/get/find/new/save/delete/primary_path）、
引用计数（inc/dec_reference_for_asset）、scopes 剪枝（project/episode/scene 级）、
settings、边界（空/不存在/非法 id）、行为一致性（经 dao 与直连根模块一致）。
"""
import json
import pytest

# 统一经 dao 门面访问
from dao import library


# ==================== 三库通用 CRUD ====================

@pytest.mark.parametrize("kind", ["characters", "scenes", "props"])
def test_list_items_empty_on_fresh(iso_data_dir, kind):
    assert library.list_items(kind) == []


@pytest.mark.parametrize("kind", ["characters", "scenes", "props"])
def test_new_asset_and_save_get_roundtrip(iso_data_dir, kind, tmp_path):
    data_dir, _ = iso_data_dir
    a = library.new_asset(kind, prefix=f"角色{kind}", variant_name="变体1")
    assert a["id"].startswith(library.KINDS[kind]["id_prefix"])
    assert a["prefix"] == f"角色{kind}" and a["variant_name"] == "变体1"
    assert a["scopes"] == {"projects": [], "episodes": [], "scenes": []}
    assert a["images"] == [] and a["audios"] == []

    # 未保存不可见
    assert library.get_item(kind, a["id"]) is None
    library.save_item(kind, a)
    got = library.get_item(kind, a["id"])
    assert got is not None and got["id"] == a["id"] and got["prefix"] == a["prefix"]
    # 文件级验证
    f = data_dir / library.KINDS[kind]["file"]
    assert json.loads(f.read_text(encoding="utf-8"))["items"][0]["id"] == a["id"]


@pytest.mark.parametrize("kind", ["characters", "scenes", "props"])
def test_save_update_and_delete(iso_data_dir, kind):
    a = library.new_asset(kind, "X", "基础形象")
    library.save_item(kind, a)
    a["prefix"] = "改名"
    library.save_item(kind, a)  # 覆盖更新
    assert len(library.list_items(kind)) == 1
    assert library.get_item(kind, a["id"])["prefix"] == "改名"

    assert library.delete_item(kind, a["id"]) is True
    assert library.list_items(kind) == []
    assert library.delete_item(kind, a["id"]) is False  # 二次删幂等


def test_save_item_append_if_new_id(iso_data_dir):
    a = library.new_asset("characters", "A")
    b = library.new_asset("characters", "B")
    library.save_item("characters", a)
    library.save_item("characters", b)
    assert len(library.list_items("characters")) == 2


# ---- 边界 ----

def test_get_item_nonexistent(iso_data_dir):
    assert library.get_item("characters", "ch_nope") is None


def test_list_corrupt_kind_file_returns_empty(iso_data_dir):
    data_dir, _ = iso_data_dir
    (data_dir / "characters.json").write_text("{bad", encoding="utf-8")
    assert library.list_items("characters") == []


def test_display_name_and_default_variant(iso_data_dir):
    a = library.new_asset("characters", "程度")
    assert a["variant_name"] == "基础形象"  # 默认变体
    assert library.display_name(a) == "程度-基础形象"


# ==================== references（参考类） ====================

def test_new_reference_from_path(iso_data_dir):
    ref = library.new_reference(path="refs/d193e8a1_红色请柬.png")
    assert ref["id"].startswith("re_")
    assert ref["prefix"] == "红色请柬"  # 由文件名派生（去 id_ 前缀）
    assert ref["variant_name"] == library.REF_VARIANT_DEFAULT
    assert len(ref["images"]) == 1
    assert ref["images"][0]["path"] == "refs/d193e8a1_红色请柬.png"


def test_reference_crud(iso_data_dir):
    ref = library.new_reference(prefix="测试素材", path="refs/a.png")
    rid = ref["id"]
    # new_reference 内部已 save → 立即可见
    got = library.get_reference(rid)
    assert got is not None and got["prefix"] == "测试素材"
    assert library.list_references()[0]["id"] == rid
    assert library.find_reference_by_path("refs/a.png")["id"] == rid
    assert library.find_reference_by_path("refs/absent.png") is None

    assert library.delete_reference(rid) is True
    assert library.get_reference(rid) is None
    assert library.delete_reference(rid) is False  # 幂等


def test_ref_primary_path_fallback(iso_data_dir):
    # images[0] 优先
    ref = library.new_reference(prefix="P", path="refs/x.png")
    assert library.ref_primary_path(ref) == "refs/x.png"
    # 兼容旧结构（无 images，只有 path 字段）
    old = {"id": "re_old", "path": "refs/old.png", "ref_count": 0, "ref_sources": []}
    assert library.ref_primary_path(old) == "refs/old.png"


def test_ref_count_inc_dec(iso_data_dir):
    # new_reference with source_asset_id → ref_count 起始 1
    ref = library.new_reference(prefix="M", path="refs/m.png", source_asset_id="ch_asset1")
    assert ref["ref_count"] == 1
    assert library.get_reference(ref["id"])["ref_sources"] == ["ch_asset1"]

    # 同一资产再引用 → 幂等不重复计
    library.inc_reference_source(ref["id"], "ch_asset1")
    assert library.get_reference(ref["id"])["ref_count"] == 1

    # 另一资产引用 → +1
    library.inc_reference_source(ref["id"], "ch_asset2")
    assert library.get_reference(ref["id"])["ref_count"] == 2

    # 资产删除 → 计数-1
    library.dec_reference_for_asset("ch_asset2")
    assert library.get_reference(ref["id"])["ref_count"] == 1


# ==================== scopes 剪枝 ====================

def test_prune_scopes(iso_data_dir):
    a = library.new_asset("characters", "程度", scopes={
        "projects": ["p1", "p2"], "episodes": ["e1"], "scenes": ["s1", "s2"]})
    library.save_item("characters", a)

    # 删 project p1 → 摘除
    r = library.prune_project_scopes("p1")
    assert r["pruned"] == 1
    got = library.get_item("characters", a["id"])
    assert got["scopes"]["projects"] == ["p2"]
    assert got["scopes"]["episodes"] == ["e1"]  # 集/场不受影响

    # 删 scene s1,s2 → 摘除，全空时 scopes 空列表保留
    library.prune_scene_scopes("s1")
    library.prune_scene_scopes("s2")
    got = library.get_item("characters", a["id"])
    assert got["scopes"]["scenes"] == []


def test_prune_noop_when_no_match(iso_data_dir):
    a = library.new_asset("scenes", "视角X", scopes={"projects": ["p1"], "episodes": [], "scenes": []})
    library.save_item("scenes", a)
    r = library.prune_project_scopes("p_不存在")  # 无命中
    assert r["pruned"] == 0


# ---- settings ----

def test_settings_default_and_save(iso_data_dir):
    cfg = library.load_settings()
    # 默认值在（隔离 data，无既有配置 → 落默认）
    assert "shared_assets_path" in cfg and "comfyui_url" in cfg
    assert cfg["shared_assets_path"]  # mock 的 tmp 路径

    # 保存自定义字段，防抖不破坏
    cfg2 = library.save_settings({"analyze_threshold": 12345})
    assert library.load_settings()["analyze_threshold"] == 12345


# ---- 行为一致性 ----

def test_library_forward_is_same_object():
    import library as root_library
    assert library is root_library
    for fn in ("list_items", "save_item", "get_item", "delete_item",
               "new_asset", "list_references", "save_reference", "get_reference",
               "new_reference", "delete_reference", "inc_reference_source"):
        assert getattr(library, fn) is getattr(root_library, fn)
