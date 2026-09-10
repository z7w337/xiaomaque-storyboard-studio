# -*- coding: utf-8 -*-
"""拆解草稿存储层（P0-5 收口，数据隔离层核心）。

数据布局：data\\drafts\\{pid}.json  拆解进度草稿
收口 main.py 7 处直接 open JSON 散点（审计：docs\\storyboard-studio\\main.py数据访问点审计.md A 节）。
数据格式不变、业务零改动、前端无感。批3（批量提取）将直接复用本模块。
"""
import json
from pathlib import Path

from data_io import atomic_write_json

# ---- 路径（弱关联：运行时推导，目录可移动/改名） ----
BASE_DIR = Path(__file__).resolve().parent
DRAFTS_DIR = BASE_DIR / "data" / "drafts"


def _draft_path(pid: str) -> Path:
    return DRAFTS_DIR / f"{pid}.json"


def draft_exists(pid: str) -> bool:
    """草稿文件是否存在。"""
    return _draft_path(pid).exists()


def load_draft(pid: str):
    """读取草稿；不存在或损坏返回 None（调用方按业务区分 404 / 空态兜底）。"""
    p = _draft_path(pid)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_draft(pid: str, draft: dict) -> None:
    """原子写草稿（幂等建目录；数据格式不变）。"""
    p = _draft_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, draft)


def delete_draft(pid: str) -> bool:
    """删除草稿；不存在返回 False。"""
    p = _draft_path(pid)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
