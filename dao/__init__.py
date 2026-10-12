# -*- coding: utf-8 -*-
"""批2 DAO 抽象层 · 数据访问门面（B-20260909-01，用户裁定 A：薄门面）。

本包是工坊全部数据访问的**统一入口**，供上层（main.py 端点等）经此访问，
**不再直接 import storage / library / assets_index**。

设计原则（批2 红线：只加包装、不改行为/数据结构、零逻辑改动）：
  - 门面仅做「模块级 re-export」——把底层三域子模块原样暴露为同名属性，
    main 等上层 `from dao import storage, library, assets_index` 后，
    内部所有 `storage.xxx() / library.xxx() / assets_index.xxx()` 调用点零改动，
    且调用的仍是**底层同一函数对象**（行为 100% 不变，天然成立）。
  - 不在此包内新增缓存 / 锁 / 包装逻辑 / 数据校验，避免改变行为。
  - assets_index 对账机制（批3 已验收）原样经此暴露，不触碰。

三域划分（对应测试方案 §3.1 覆盖面）：
  storage      项目文件域：projects.json 索引 + data/projects/{id}.json 详情 + 集/场模型
  library      资产库域：三库(characters/scenes/props) + references + settings
  assets_index 共享资产索引域：SharedAssets 版本索引 + 引用计数 + 对账

★ 2026-09-19 ⓪-3·步3 并入第 7 域（用户 01:25 拍板"收存取型四样"；用户 02:23 裁定方案 A）：
  task_store   任务域：data/tasks.json（任务对象 9 字段；**持久化/派发语义归批次② F5a**）
  → 「方案 A = 现状即 DAO」：本门面就是隔离层，本步只**补任务域 + 定边界 + 加防回潮自检**，
    不新建门面、不搬 285 处调用点（做减法；用户裁定的原话见 `keel\4-user-needs\用户原话-20260919.md`）。

边界纪律（⓪-3·步3 定，自检脚本 `keel\script\dao_boundary_check.py` 强制）：
  ① 上层（main.py 等）**不得绕过 dao 直接 import 数据域根模块**（须 `from dao import ...`）；
  ② 上层**不得对数据域文件裸读写**（`json.dump/json.load`、`open(...json)`）；
  ③ `assets_index` **留在 dao 内但仍是对账器角色**（DAO 提供数据、它做对账；见手册 §4.5）。

上游模块若需原子写底层，仍可 `from dao import data_io`（或直接用其转发的 data_io）。
"""
import sys as _sys

# 门面层向上可访问的路径（dao 与工坊根同级，根模块 import 无需改 sys.path；
# 此处兼容 dao 被以包形式 import 的场景，保持三域可 `from dao import ...`）。
import data_io as _data_io
import storage as _storage
import library as _library
import assets_index as _assets_index
# F5(审计20260909): 门面收编另两个数据域——draft_store(data/drafts/)/batch_queue(data/batch/)
# 使 dao 成为工坊全部数据访问的完整统一入口(此前 __all__ 仅四模块覆盖不全)。
# 二者均为纯底层数据域(不反向依赖 dao), 可安全 re-export。
import draft_store as _draft_store
import batch_queue as _batch_queue
import task_store as _task_store      # ⓪-3·步3：任务域（data/tasks.json）

# 统一出口：数据域子模块 + 原子写底层，全部按原名暴露。
# 用法：`from dao import storage, library, assets_index, draft_store, batch_queue, data_io`
# 调用方保持 `storage.load_project(...)` / `library.save_item(...)` /
#      `assets_index.register_file(...)` / `draft_store.save_draft(...)` /
#      `batch_queue.create_batch(...)` 原签名不变。
storage = _storage
library = _library
assets_index = _assets_index
draft_store = _draft_store
batch_queue = _batch_queue
task_store = _task_store
data_io = _data_io

__all__ = ["storage", "library", "assets_index", "draft_store", "batch_queue", "task_store", "data_io"]
