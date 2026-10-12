# -*- coding: utf-8 -*-
"""导入演示项目（验收基线数据，计划书 9.2）。

用法：python samples\\import_demo.py（在模块根目录或任意位置运行均可，
自身路径用 __file__ 推导，弱关联）
幂等：若同名演示项目已存在则跳过，不重复导入、不覆盖。
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

DEMO_NAME = "雨夜寻踪（演示项目）"


def main():
    samples_dir = BASE_DIR / "samples"
    script = (samples_dir / "演示剧本.txt").read_text(encoding="utf-8").strip()
    data = json.loads((samples_dir / "演示项目.json").read_text(encoding="utf-8"))

    # 幂等：已存在则跳过
    for p in storage.list_projects():
        if p["name"] == DEMO_NAME:
            print(f"演示项目已存在（id={p['id']}），跳过导入")
            return

    proj = storage.new_project(DEMO_NAME, data["art_style"], data["aspect_ratio"])
    proj["script"] = script

    for ep_def in data["episodes"]:
        ep = storage.new_episode(proj, ep_def["title"])
        for sc_def in ep_def["scenes"]:
            sc = storage.new_scene(ep, sc_def["title"])
            for i, sh_def in enumerate(sc_def["shots"], 1):
                sh = storage.new_shot(sc, i)
                sh.update(sh_def)
                sc["shots"].append(sh)
            ep["scenes"].append(sc)
        proj["episodes"].append(ep)

    storage.save_project(proj)
    idx = storage.load_projects_index()
    idx["projects"].append({
        "id": proj["id"],
        "name": proj["name"],
        "created_at": proj["created_at"],
        "updated_at": proj["updated_at"],
    })
    storage.save_projects_index(idx)

    stat = storage.count_shots(proj)
    print(f"演示项目已导入：id={proj['id']}")
    print(f"名称：{proj['name']} / 画风：{proj['art_style']} / 画幅：{proj['aspect_ratio']}")
    print(f"镜头统计：总 {stat['total']}（待生成 {stat['pending']} / 已生成 {stat['generated']} / 已确认 {stat['confirmed']}）")


if __name__ == "__main__":
    main()
