# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 项目文件域（storage 转发）单元测试（B-20260909-01）。

覆盖（对齐测试方案 §3.1）：项目索引/详情 CRUD、集/场模型、find/reorder/count、
边界（不存在项目/空索引/非法 id）、持久化往返、行为一致性（经 dao 与直连根模块一致）。
"""
import json
import pytest

# 统一经 dao 门面访问（被测对象 = dao.storage 转发的同一函数对象）
from dao import storage


# ---- 项目 CRUD ----

def test_list_projects_empty_on_fresh(iso_data_dir):
    assert storage.list_projects() == []


def test_new_and_save_load_project_roundtrip(iso_data_dir):
    data_dir, _ = iso_data_dir
    p = storage.new_project("测试项目Alpha")
    assert p["id"].startswith("p_")
    assert storage.load_project(p["id"]) is None  # 未保存前不可见

    storage.save_project(p)
    # 直接落盘验证（文件级）
    pf = data_dir / "projects" / f"{p['id']}.json"
    assert pf.exists()
    got = json.loads(pf.read_text(encoding="utf-8"))
    assert got["id"] == p["id"] and got["name"] == "测试项目Alpha"

    # 经 dao 读回
    loaded = storage.load_project(p["id"])
    assert loaded is not None and loaded["name"] == "测试项目Alpha"


def test_projects_index_maintained(iso_data_dir):
    # new_project + save 不自动写索引；save_projects_index 显式写
    p1 = storage.new_project("P1")
    p2 = storage.new_project("P2")
    storage.save_projects_index({"projects": [p1, p2]})
    idx = storage.load_projects_index()
    assert len(idx["projects"]) == 2
    assert storage.list_projects() == [p1, p2]


def test_delete_project(iso_data_dir):
    data_dir, _ = iso_data_dir
    p = storage.new_project("待删")
    storage.save_project(p)
    pf = data_dir / "projects" / f"{p['id']}.json"
    assert pf.exists()

    assert storage.delete_project(p["id"]) is True
    assert not pf.exists()
    # 二次删幂等（不存在返回 False）
    assert storage.delete_project(p["id"]) is False


# ---- 边界条件 ----

def test_load_nonexistent_project_returns_none(iso_data_dir):
    assert storage.load_project("p_不存在_id") is None


def test_load_corrupt_project_returns_none(iso_data_dir):
    data_dir, _ = iso_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "projects").mkdir(parents=True, exist_ok=True)
    # 写一个坏 JSON
    (data_dir / "projects" / "p_bad.json").write_text("{not json", encoding="utf-8")
    assert storage.load_project("p_bad") is None


def test_load_corrupt_index_returns_empty(iso_data_dir):
    data_dir, _ = iso_data_dir
    # 坏索引 → 兜底空索引
    (data_dir / "projects.json").write_text("{bad", encoding="utf-8")
    assert storage.load_projects_index() == {"projects": []}


# ---- 集/场模型 + 结构查找 ----

def test_episode_scene_model(iso_data_dir):
    p = storage.new_project("P")
    ep = storage.new_episode(p, "第1集")
    assert ep["id"].startswith("ep_") and ep["scenes"] == []
    p["episodes"].append(ep)

    sc = storage.new_scene(ep, "场景A")
    assert sc["id"].startswith("sc_")
    assert set(sc.keys()) >= {"id", "title", "text", "text_history", "images", "videos", "status"}
    ep["scenes"].append(sc)

    assert storage.find_episode(p, ep["id"]) is ep
    assert storage.find_episode(p, "ep_x") is None
    assert storage.find_scene(p, ep["id"], sc["id"]) is sc
    assert storage.find_scene(p, ep["id"], "sc_x") is None
    assert storage.find_scene(p, "ep_x", sc["id"]) is None


def test_reorder_items(iso_data_dir):
    items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    storage.reorder_items(items, ["c", "a"])
    assert [i["id"] for i in items] == ["c", "a", "b"]  # 未覆盖的 b 追加尾部
    storage.reorder_items(items, [])
    assert [i["id"] for i in items] == ["c", "a", "b"]  # 空 ids 不变


def test_count_scenes(iso_data_dir):
    p = storage.new_project("P")
    ep1 = storage.new_episode(p, "第1集")
    ep1["scenes"] = [
        {**storage.new_scene(ep1), "status": "待生成"},
        {**storage.new_scene(ep1), "status": "已生成"},
    ]
    ep2 = storage.new_episode(p, "第2集")
    ep2["scenes"] = [{**storage.new_scene(ep2), "status": "已确认"}]
    p["episodes"] = [ep1, ep2]

    c = storage.count_scenes(p)
    assert c == {"total": 3, "pending": 1, "generated": 1, "confirmed": 1}


# ---- 行为一致性（经 dao 与直连根模块，同一函数对象应逐项一致） ----

def test_dao_forward_is_same_object():
    import storage as root_storage
    assert storage is root_storage  # dao 门面转发 = 底层同一模块对象
    assert storage.load_project is root_storage.load_project
    assert storage.save_project is root_storage.save_project
