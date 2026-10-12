# -*- coding: utf-8 -*-
"""播种"UI 目检测试项目"（为 ⓪-2 UI 先行 目检/联调准备一份可点、可测的数据）。

为什么需要它：场页需要"项目 + 集 + 场"才能进入；空库时场页点不进去，UI 没法目检。

用法：python tools/seed_test_project.py
- 幂等：同名项目已存在则跳过（不覆盖、不重复）
- 素材：从 E:\\视频AI\\ 复制 2 张图作资产主图（**复制时改 ASCII 名** —— 参考图路径要求 ASCII）
- 不调 LLM、不调 ComfyUI，纯本地落库
"""
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import storage            # noqa: E402
import library            # noqa: E402
import assets_index       # noqa: E402

PROJ_NAME = "测试项目（UI 目检用）"
ART_STYLE = "写实电影"
ASPECT = "16:9"

# E:\视频AI 里任选的两张：一张当人物主图、一张当场景主图
SEED_IMAGES = [
    (r"E:\视频AI\演唱白\歌手\正面全身.png", "seed_char_main.png"),
    (r"E:\视频AI\TBD\至交\酒馆外街道白天.jpg", "seed_scene_main.jpg"),
]


def _copy_media() -> list:
    """把种子图复制进 SharedAssets/uploads/seed/（ASCII 名），并登记进共享索引。"""
    root = Path(library.get_shared_assets_path())
    dst_dir = root / "uploads" / "seed"
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for src, name in SEED_IMAGES:
        sp = Path(src)
        if not sp.exists():
            print("  [跳过] 源图不存在：%s" % src)
            continue
        dp = dst_dir / name
        if not dp.exists():
            shutil.copy2(sp, dp)
        rel = "uploads/seed/" + name
        try:
            assets_index.register_file(rel)
        except Exception as e:
            print("  [警告] register_file 失败 %s：%s" % (rel, e))
        out.append(rel)
        print("  [OK] %s  ←  %s" % (rel, src))
    return out


def main():
    storage.init_storage()
    library.init_library()

    for p in storage.list_projects():
        if p["name"] == PROJ_NAME:
            print("测试项目已存在（id=%s），跳过。若要用新数据请先删掉它。" % p["id"])
            return

    print("1) 复制种子图 + 登记共享索引")
    rels = _copy_media()
    char_img = rels[0] if len(rels) > 0 else ""
    scene_img = rels[1] if len(rels) > 1 else ""

    print("2) 建项目 / 集 / 场")
    proj = storage.new_project(PROJ_NAME, ART_STYLE, ASPECT)
    proj["script"] = (
        "【测试剧本】\n"
        "第 1 集：\n"
        "  场 1：夜里的小饭馆门口，人不多。他站在灯下等人，手里攥着一把钥匙。\n"
        "  场 2：屋里，桌上摊着一张旧照片，他坐下，看了很久。\n"
    )
    ep = storage.new_episode(proj, "第 1 集 · 夜里的钥匙")
    scene_defs = [
        ("场 1 · 小饭馆门口（夜）", "夜晚，小饭馆门口，暖黄灯光，人不多。他站在灯下等人。", 4.0),
        ("场 2 · 屋里（内景）", "屋内，桌上摊着一张旧照片，暖光，安静。他坐下看着照片。", 3.0),
    ]
    for title, prompt, dur in scene_defs:
        sc = storage.new_scene(ep, title)
        sc["prompt"] = prompt
        sc["duration"] = dur
        ep["scenes"].append(sc)
    proj["episodes"].append(ep)
    storage.save_project(proj)

    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": proj["id"], "name": proj["name"],
        "created_at": proj["created_at"], "updated_at": proj["updated_at"],
    })
    storage.save_projects_index(idx)

    print("3) 建资产（人物 / 场景，挂种子主图，归属本项目）")
    scopes = {"projects": [proj["id"]], "episodes": [ep["id"]], "scenes": []}
    now = library.now_iso()
    if char_img:
        a = library.new_asset(
            "characters", "测试角色", "基础形象", scopes=scopes,
            prompt="二十多岁的年轻人，深色外套，安静",
            images=[{"path": char_img, "version": 1, "gen_prompt": "UI 目检用种子图", "source": "seed", "added_at": now}],
        )
        library.save_item("characters", a)
        print("  [OK] 人物：%s（id=%s）主图=%s" % (library.display_name(a), a["id"], char_img))
    if scene_img:
        a = library.new_asset(
            "scenes", "测试场景", "夜里的小饭馆门口", scopes=scopes,
            prompt="夜晚街边小饭馆门口，暖黄灯光，人很少",
            images=[{"path": scene_img, "version": 1, "gen_prompt": "UI 目检用种子图", "source": "seed", "added_at": now}],
        )
        library.save_item("scenes", a)
        print("  [OK] 场景：%s（id=%s）主图=%s" % (library.display_name(a), a["id"], scene_img))

    print("")
    print("完成：")
    print("  项目 id = %s" % proj["id"])
    print("  名称   = %s" % proj["name"])
    print("  集/场  = 1 集 / %d 场" % len(ep["scenes"]))
    print("  打开   = http://127.0.0.1:3100/script.html?project=%s&view=outline" % proj["id"])
    print("  场页   = http://127.0.0.1:3100/scene.html?project=%s&episode=%s" % (proj["id"], ep["id"]))


if __name__ == "__main__":
    main()
