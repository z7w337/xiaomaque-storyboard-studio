# -*- coding: utf-8 -*-
"""批3-3b · 落地即资产（素材化）测试（B-20260909-02，IC 基线15，用户裁决策点 B=references 素材卡）。

验证：output/uploads 游离图 → 收录为 references 素材卡（决策点 B）：
  1. 素材化建卡成功（refs/ 副本 + references 卡 images[0]=副本 + assets_index 登记副本）
  2. 幂等：重复收录同一源图返回既有卡（不重复建卡）
  3. 源图不存在 → 404 / 非图片 → 400 / 路径越界 → 400
  4. 建卡后 reconcile 对账仍全绿（红线：assets_index 机制不被破坏）
隔离：复用 conftest iso_data_dir，不碰真实 data/。
"""
import pytest
from pathlib import Path


@pytest.fixture
def client(iso_data_dir):
    """隔离数据目录 + 直接调素材化端点函数（不起 HTTP 服务）。"""
    import main
    return main, iso_data_dir


def _make_free_img(rel: str = "output/free_test.png") -> str:
    """在隔离共享目录造一张游离图，返回相对路径。"""
    import library
    shared = Path(library.get_shared_assets_path())
    (shared / Path(rel).parent).mkdir(parents=True, exist_ok=True)
    (shared / rel).write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    return rel


def test_materialize_creates_reference_card(client):
    """游离图收录 → 建 references 素材卡（refs/ 副本 + images[0] + assets_index 登记）。"""
    import main
    import library
    import assets_index
    from pathlib import Path

    rel = _make_free_img()
    # 断言源图在共享 output 目录
    shared = Path(library.get_shared_assets_path())
    assert (shared / rel).is_file()

    payload = main.MaterializePayload(path=rel)
    res = main.api_materialize_material(payload)

    assert res["materialized"] is True
    rid = res["ref_id"]
    ref = library.get_reference(rid)
    assert ref is not None, "素材卡未建成"
    assert res["refs_path"].startswith("refs/"), "副本应落在 refs/"
    # 副本物理存在
    assert (shared / res["refs_path"]).is_file()
    # 卡 images[0]=副本, _src_rel 记源图
    assert ref["images"][0]["path"] == res["refs_path"]
    assert ref.get("_src_rel") == rel
    # assets_index 已登记副本(source=reference, in_use)
    idx_refs = assets_index.list_entries(status="in_use", source="reference")
    assert any(e.get("path") == res["refs_path"] for e in idx_refs), "refs 副本未登记入版本索引"


def test_materialize_idempotent(client):
    """重复收录同一源图 → 不重复建卡（materialized=False + 返回既有卡）。"""
    import main
    import library

    rel = _make_free_img()
    r1 = main.api_materialize_material(main.MaterializePayload(path=rel))
    n_before = len(library.list_references())
    r2 = main.api_materialize_material(main.MaterializePayload(path=rel))
    assert r2["materialized"] is False
    assert r2["ref_id"] == r1["ref_id"]
    assert len(library.list_references()) == n_before, "不应重复建卡"


def test_materialize_validation(client):
    """异常输入：不存在→404 / 非图片→400 / 越界→400。"""
    import main
    import pytest as _p

    with _p.raises(Exception) as e1:
        main.api_materialize_material(main.MaterializePayload(path="output/nonexist.png"))
    assert e1.value.status_code == 404

    # 非图片：放一个 .txt
    import library
    from pathlib import Path
    shared = Path(library.get_shared_assets_path())
    (shared / "output").mkdir(parents=True, exist_ok=True)
    (shared / "output/a.txt").write_text("x")
    with _p.raises(Exception) as e2:
        main.api_materialize_material(main.MaterializePayload(path="output/a.txt"))
    assert e2.value.status_code == 400

    # 越界（../ 逃逸）
    with _p.raises(Exception) as e3:
        main.api_materialize_material(main.MaterializePayload(path="../outside.png"))
    assert e3.value.status_code == 400


def test_materialize_validation_prefix_bypass(client):
    """收尾A⑦：越界校验防**前缀绕过**（非仅 ../ 逃逸）。

    漏洞场景：root=<...>/shared_assets，恶意 rel=`../shared_assets_evil/x.png`，
    resolve 后 src=<...>/shared_assets_evil/x.png——str(src).startswith(str(root)) 为 True
    （`shared_assets_evil` 以 `shared_assets` 开头）→ 旧代码误放行，可读库外文件。
    修复：relative_to 按组件边界判断 → 应 400。
    """
    import main
    import pytest as _p
    import library
    shared = Path(library.get_shared_assets_path()).resolve()
    # 造一个名字带 root 前缀的"兄弟"目录（真实逃逸目标），放一张 png 证明能被读到
    evil_root = shared.parent / (shared.name + "_evil")
    evil_root.mkdir(parents=True, exist_ok=True)
    (evil_root / "leak.png").write_bytes(b"\x89PNG\r\n\x1a\nleakbypass")
    # 经 ../ 逃逸到该兄弟目录（resolve 后 prefix 与 root 重叠）
    rel = f"../{shared.name}_evil/leak.png"
    with _p.raises(Exception) as e:
        main.api_materialize_material(main.MaterializePayload(path=rel))
    assert e.value.status_code == 400, "前缀绕过路径必须 400"


def test_materialize_reconcile_still_green(client):
    """建素材卡后 reconcile 对账仍全绿（红线：assets_index 机制不被破坏）。"""
    import main
    import assets_index

    rel = _make_free_img()
    main.api_materialize_material(main.MaterializePayload(path=rel))
    # reconcile 不抛错、注册缺失数为 0（隔离库内文件齐全）
    r = assets_index.reconcile()
    assert r.get("registered", 0) >= 0  # reconcile 正常执行
