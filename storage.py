# -*- coding: utf-8 -*-
"""项目数据存储层（阶段 1）。

所有 JSON 读写统一走 data_io 原子写入（《工程管理规范》第 9 节 / 计划书 2.4）。
代码路径弱关联（规范第 7 节第 4 条）：自身路径用 __file__ 推导。

数据布局：
  data\projects.json          项目索引（id/名称/创建时间/更新时间）
  data\projects\{id}.json     项目详情（剧本、集/场/镜结构）
"""
import threading
import uuid
from datetime import datetime
from pathlib import Path

from data_io import atomic_write_json

# F1 加锁止血(B-20260910-01, 审计F1并发写丢数据): 单把全局 RLock, 对齐 assets_index._INDEX_LOCK,
# 使项目文件写路径串行, 杜绝并发写覆盖丢数据。锁在调用层, data_io 不双锁。
_STORAGE_LOCK = threading.RLock()

# ---- 路径（弱关联：运行时推导，目录可移动/改名） ----
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
INDEX_FILE = PROJECTS_DIR.parent / "projects.json"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def init_storage() -> None:
    """初始化存储：确保目录与索引文件存在（幂等，可重复调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        save_projects_index({"projects": []})


# ---- 项目索引 ----

def load_projects_index() -> dict:
    if not INDEX_FILE.exists():
        return {"projects": []}
    import json
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # 索引损坏时回退为空索引（数据文件由原子写入保证，此处兜底）
        return {"projects": []}


def save_projects_index(index: dict) -> None:
    # F1 加锁止血: 项目索引写串行, 防并发覆盖
    with _STORAGE_LOCK:
        atomic_write_json(INDEX_FILE, index, bak_every=1)  # ③G3-1 滚动 .bak


def list_projects() -> list:
    """返回项目索引列表（不含详情）。"""
    return load_projects_index().get("projects", [])


# ---- 项目详情 ----

def project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


def load_project(project_id: str) -> dict | None:
    path = project_path(project_id)
    if not path.exists():
        return None
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_project(project: dict) -> None:
    # F1 加锁止血: 项目详情写串行, 防并发覆盖
    with _STORAGE_LOCK:
        atomic_write_json(project_path(project["id"]), project, bak_every=1)  # ③G3-1 滚动 .bak


def delete_project(project_id: str) -> bool:
    # F1 加锁止血
    with _STORAGE_LOCK:
        path = project_path(project_id)
        if not path.exists():
            return False
        path.unlink()
        return True


# ---- 数据模型（计划书第 3 节，v1.34：场=最小存储粒度，无镜头实体） ----


def new_project(name: str, art_style: str = "", aspect_ratio: str = "16:9") -> dict:
    """新建空项目（含完整数据模型结构）。"""
    now = now_iso()
    return {
        "id": new_id("p_"),
        "name": name,
        "art_style": art_style,
        "aspect_ratio": aspect_ratio,
        "created_at": now,
        "updated_at": now,
        "script": "",
        "summary": "",      # 剧本全局摘要（v1.31：只读区，仅"重新生成"）
        "episodes": [],  # 集 Episode[]
    }


def new_episode(project: dict, title: str = "") -> dict:
    """在项目下新建一集。"""
    return {
        "id": new_id("ep_"),
        "title": title or f"第 {len(project['episodes']) + 1} 集",
        "summary": "",  # 本集剧情摘要（v1.31：可编辑 + AI 润色）
        "scenes": [],  # 场次 Scene[]
    }


def new_scene(episode: dict, title: str = "") -> dict:
    """新建场（v1.34：场=最小存储粒度；镜=场文本内纯文本段，无独立镜头实体）。"""
    return {
        "id": new_id("sc_"),
        "title": title or f"场景 {len(episode['scenes']) + 1}",
        "text": "",            # 场文本单一文字编辑区（"镜1：…镜2：…"纯文本段，含尖括号标记，计划书 4.10）
        "text_history": [],    # ③ 场文本历史版本[]（每次保存快照{text,saved_at}，供历史版本管理）
        "images": [],          # 参考图/生成图[]（可选，不强求）
        "videos": [],          # 场级视频[]（多版本/历史/切换启用，计划书 4.7）
        "status": "待生成",    # 场状态：待生成 / 已生成 / 已确认
    }


# ---- 结构查找辅助（阶段 2 集/场/镜 CRUD 用） ----

def find_episode(project: dict, episode_id: str) -> dict | None:
    for ep in project.get("episodes", []):
        if ep.get("id") == episode_id:
            return ep
    return None


def find_scene(project: dict, episode_id: str, scene_id: str) -> dict | None:
    ep = find_episode(project, episode_id)
    if not ep:
        return None
    for sc in ep.get("scenes", []):
        if sc.get("id") == scene_id:
            return sc
    return None


def reorder_items(items: list, ids: list) -> None:
    """按 ids 顺序重排 items（原地）；ids 未覆盖的项保持原相对顺序追加在尾部。"""
    by_id = {x["id"]: x for x in items}
    ordered = []
    seen = set()
    for i in ids:
        if i in by_id and i not in seen:
            ordered.append(by_id[i])
            seen.add(i)
    for x in items:
        if x["id"] not in seen:
            ordered.append(x)
    items[:] = ordered


# ---- 进度统计（进度看板数据，v1.34 按场统计） ----

def count_scenes(project: dict) -> dict:
    """统计项目场数与状态分布（总场次/待生成/已生成/已确认）。"""
    total = pending = generated = confirmed = 0
    for ep in project.get("episodes", []):
        for sc in ep.get("scenes", []):
            total += 1
            status = sc.get("status", "待生成")
            if status == "已生成":
                generated += 1
            elif status == "已确认":
                confirmed += 1
            else:
                pending += 1
    return {
        "total": total,
        "pending": pending,
        "generated": generated,
        "confirmed": confirmed,
    }
