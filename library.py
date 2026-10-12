# -*- coding: utf-8 -*-
"""全局库存储层（角色/场景/道具，统一资产模型 v1.20，计划书第 3 节）。

数据布局（data\ 下，全原子写入）：
  data\characters.json   人物库：资产 = 名称 + 变体[]（变体=形象名/提示词/图版本/音色文本/音频）
  data\scenes.json       场景库：资产 = 名称 + 变体[]（变体=视角名/提示词/图版本）
  data\props.json        道具库：资产 = 名称 + 变体[]（变体=所在场景名/提示词/图版本）
  data\settings.json     运行配置（含 SharedAssets 路径，外部路径走配置注入，规范第 7 节第 4 条）

统一资产模型（v1.20，v1.34 scopes 三层）：
  Asset = { id, prefix, variant_name, scopes{projects[],episodes[],scenes[]},
            prompt, images[]({path,version,gen_prompt,source,added_at}),
            voice_text(仅人物), audios[]({path,type(trial/reference),role(voice_sample/reference/trial),voice,added_at}),
            voice_source, voice, created_at, updated_at }
"""
import contextlib
import threading
import uuid
from datetime import datetime
from pathlib import Path

from data_io import atomic_write_json

# F1 加锁止血(B-20260910-01, 审计F1并发写丢数据): 单把全局 RLock, 对齐 assets_index._INDEX_LOCK,
# 使同库写路径(读改写整段)串行, 杜绝并发读改写交叉覆盖丢数据。锁在调用层, data_io 不双锁。
_LIBRARY_LOCK = threading.RLock()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

KINDS = {
    "characters": {"file": "characters.json", "label": "人物", "id_prefix": "ch_", "variant_dim": "形象"},
    "scenes": {"file": "scenes.json", "label": "场景", "id_prefix": "sc_", "variant_dim": "视角"},
    "props": {"file": "props.json", "label": "道具", "id_prefix": "pr_", "variant_dim": "所在场景"},
}

# 14 种 edge-tts 中文预置音色（v1.22/4.12，默认晓晓 zh-CN-XiaoxiaoNeural）
TTS_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓（女·温柔）"),
    ("zh-CN-XiaoyiNeural", "晓伊（女·活泼）"),
    ("zh-CN-YunjianNeural", "云健（男·浑厚）"),
    ("zh-CN-YunxiNeural", "云希（男·阳光）"),
    ("zh-CN-YunxiaNeural", "云夏（男·少年）"),
    ("zh-CN-YunyangNeural", "云扬（男·专业）"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北（女·东北）"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮（女·陕西）"),
    ("zh-TW-HsiaoChenNeural", "晓臻（女·台湾）"),
    ("zh-TW-HsiaoYuNeural", "晓雨（女·台湾）"),
    ("zh-TW-YunJheNeural", "云哲（男·台湾）"),
    ("zh-HK-HiuGaaiNeural", "曉佳（女·粤语）"),
    ("zh-HK-HiuMaanNeural", "曉曼（女·粤语）"),
    ("zh-HK-WanLungNeural", "雲龍（男·粤语）"),
]

# 人物/场景/道具的变体维度标签
VARIANT_DIM_LABELS = {
    "characters": "形象",
    "scenes": "视角",
    "props": "所在场景",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _path(kind: str) -> Path:
    return DATA_DIR / KINDS[kind]["file"]


# ---- 运行配置（外部路径注入） ----

DEFAULT_SHARED_ASSETS = str(BASE_DIR / "SharedAssets")  # 默认=程序目录下 SharedAssets（可移植、不含本机路径）；真机可在设置页改为任意目录
# 收尾A②(2026-09-10): 默认值改占位符,不写死内网IP; 真实地址由 data/settings.json 配置(get_comfyui_url 从 settings 读)
DEFAULT_COMFYUI_URL = "http://<comfyui-ip>:8188"  # 占位符; 真机在设置页配置实际 ComfyUI 地址


def _settings(read_only: bool) -> dict:
    """读取并合并默认配置。

    收尾A③(F6)：读路径去写副作用 —— `read_only=True` 时绝不写盘（运行期高频读走此）；
    仅 `read_only=False`（启动自愈 / save 前置）才在缺字段时补齐落盘。
    v1.55 治本修复（Windows Defender 写锁坑）：写仅当缺字段（默认值未落盘）时才发生；
    写入再包 try/except：补字段失败也不阻塞读取（由下次启动自愈再补）。
    """
    p = DATA_DIR / "settings.json"
    cfg = {}
    if p.exists():
        import json
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
    defaults = {
        "shared_assets_path": DEFAULT_SHARED_ASSETS,
        "comfyui_url": DEFAULT_COMFYUI_URL,  # v1.55：ComfyUI 直连（音色样本 + 6-10 视频）；收尾A②默认占位
        "ic_path": "",  # IC 已下线；历史工作流兜底路径留空（收口整改 2026-09-11，去本机绝对路径）
        "llm_models": [],       # 模型列表 [{id,name,base_url,model,api_key,temperature,price_per_1k,is_default}]
        "listen": "127.0.0.1",  # 监听地址
        "analyze_threshold": 8000,  # v1.37：自适应拆解阈值（字，短剧本整本一次/长剧本按集分批）
        # v1.60（批3 B2 成本门）：生图/生视频单价（元/张、元/秒）。H3=本地 ComfyUI 现无直接成本=0；
        # 未来有实际成本时改 settings.json 对应单价一个数字即可。cost_confirm_enabled=false 或单价=0 时不弹成本确认。
        "generation_cost": {
            "models": {
                "h3_video": {"video_per_second": 0.0, "image_per_unit": 0.0},
                "comfyui_image": {"video_per_second": 0.0, "image_per_unit": 0.0},
                "default": {"video_per_second": 0.0, "image_per_unit": 0.0},
            },
            "cost_confirm_enabled": True,
        },
        "created_at": now_iso(),
    }
    merged = {**defaults, **cfg}
    if not read_only and merged != cfg:
        try:
            atomic_write_json(p, merged)
        except OSError:
            pass  # 写失败（Defender 持锁等）不阻塞读取；启动自愈会再补
    return merged


def ensure_settings() -> dict:
    """读取配置；缺字段时**补齐落盘**（启动自愈 / 需持久化时用，读函数勿调）。"""
    return _settings(read_only=False)


def read_settings() -> dict:
    """纯读配置（绝不写盘）—— 收尾A③：运行期读函数统一走此，零写副作用。"""
    return _settings(read_only=True)


def load_settings() -> dict:
    return read_settings()  # 收尾A③：读函数零写副作用


def save_settings(cfg: dict) -> dict:
    # ★ R-1 入口清洗（派单 003，2026-10-12，依据第三方复核意见 §三 R-1）：
    #   前端把 `GET /api/settings` 的**脱敏结果**浅拷贝后原样回传
    #   （settings.html:316 `map(m => ({...m}))` → :358 `llm_models: models`；ms_image 走 Object.assign）
    #   ⇒ `mask_settings` 派生的 `api_key_set` 会经前端回流被 merge 落盘（**无害但污染数据**）。
    #   ★ 这是**入口清洗**，与 `mask_settings`（**出口脱敏**）**方向相反** —— 两者都在本模块，别混。
    prev_cfg = ensure_settings()
    new_models = cfg.get("llm_models")
    if new_models is not None:
        # 防抖：保存时若某已有模型的 api_key 传空，则沿用原密钥，避免误删已配置的 Key
        prev_keys = {m.get("id"): m.get("api_key") for m in (prev_cfg.get("llm_models") or [])}
        for m in new_models:
            m.pop("api_key_set", None)          # ★ R-1：丢掉前端回流的派生字段（非存储字段）
            mid = m.get("id")
            if not (m.get("api_key") or "").strip() and mid in prev_keys and (prev_keys[mid] or "").strip():
                m["api_key"] = prev_keys[mid]
        cfg = {**cfg, "llm_models": new_models}
    ms = cfg.get("ms_image")
    if isinstance(ms, dict):
        ms.pop("api_key_set", None)             # ★ R-1：同上（Object.assign 会把 gSettings 的派生字段带入）
        # 防抖（与上方 llm_models api_key 同口径）：令牌传空则沿用原值，避免误删已配置的云端密钥
        prev_ms = prev_cfg.get("ms_image") or {}
        if not (ms.get("api_key") or "").strip() and (prev_ms.get("api_key") or "").strip():
            ms["api_key"] = prev_ms["api_key"]
        cfg = {**cfg, "ms_image": ms}
    merged = {**prev_cfg, **cfg}
    merged["updated_at"] = now_iso()
    atomic_write_json(DATA_DIR / "settings.json", merged)
    return merged


def mask_settings(cfg: dict) -> dict:
    """★ 派单 001 · T2（2026-10-11，架构-3）：**HTTP 出口脱敏**（P0-1）。

    返回 `cfg` 的**浅层副本**，把密钥置空，并**补 `api_key_set` 布尔**（供前端判「是否已配置」）：
      · `llm_models[].api_key` → `""`，每项加 `api_key_set: bool`
      · `ms_image.api_key`     → `""`，并加 `ms_image.api_key_set: bool`

    ★★ **只用于过 HTTP 的出口**（`GET` / `PUT` `/api/settings`）。
    `load_settings()` / `read_settings()` **必须仍返回真值** —— 内部调用方（ai 拆解 / 生图路由等）
    要靠真令牌去请求服务商 API，若在**读取层**脱敏会全线 401。**本函数不改入参**（不做 in-place）。

    ★ 前端口径：`api_key` 为空**不代表没配**（脱敏后恒空）⇒ 判断一律看 `api_key_set`。
    """
    out = dict(cfg or {})
    models = []
    for m in (out.get("llm_models") or []):
        if not isinstance(m, dict):
            models.append(m)
            continue
        mm = dict(m)
        mm["api_key_set"] = bool((mm.get("api_key") or "").strip())
        mm["api_key"] = ""
        models.append(mm)
    if "llm_models" in out:
        out["llm_models"] = models
    ms = out.get("ms_image")
    if isinstance(ms, dict):
        ms2 = dict(ms)
        ms2["api_key_set"] = bool((ms2.get("api_key") or "").strip())
        ms2["api_key"] = ""
        out["ms_image"] = ms2
    return out


def get_shared_assets_path() -> str:
    cfg = read_settings()  # 收尾A③：读零写副作用
    return cfg.get("shared_assets_path") or DEFAULT_SHARED_ASSETS


def get_comfyui_url() -> str:
    """v1.55：ComfyUI 直连地址（音色样本 / 场视频 / 各处 job 共用）。

    ★ 用户 2026-10-01（界面调整 Q2）：**真源改为「本地平台」里的 local 节点地址** ——
      设置页原来的「其他设置 → ComfyUI 地址」已删除，本机地址改在本地平台里直接改。
      `settings.comfyui_url` 仅作**兜底**（兼容尚未迁移的老配置）。
      依赖方向固定单向：**库层读节点池**（node_pool 不反向调本函数，否则循环 import）。
    """
    try:
        import node_pool
        u = (node_pool.local_url() or "").strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    cfg = read_settings()  # 收尾A③：读零写副作用
    return (cfg.get("comfyui_url") or DEFAULT_COMFYUI_URL).rstrip("/")


# ---- 初始化 ----

def init_library() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        p = _path(kind)
        if not p.exists():
            atomic_write_json(p, {"items": []})
    startup_self_heal()


def startup_self_heal() -> None:
    """收尾A③(F6)：启动期自愈一次 —— 把"读路径带写盘"的副作用集中在此一次性补齐，
    使运行期读函数（read_settings/get_shared_assets_path/get_comfyui_url/list_references）零写副作用。
    ①settings 缺默认字段→补齐落盘（ensure_settings 写版）；②references 旧扁平条目→迁移统一结构（写一次）。
    """
    try:
        ensure_settings()   # 补齐默认字段落盘
    except Exception:
        pass
    try:
        _refs_ensure_migrated()   # 旧 reference 结构迁移落盘（若无旧数据则零开销纯读）
    except Exception:
        pass


# ---- 通用 CRUD ----

def list_items(kind: str) -> list:
    p = _path(kind)
    import json
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except (OSError, ValueError):
        return []


def get_item(kind: str, item_id: str) -> dict | None:
    for it in list_items(kind):
        if it.get("id") == item_id:
            return it
    return None


def save_item(kind: str, item: dict) -> None:
    # F1 加锁止血: 锁包"读改写"整段, 杜绝并发读改写交叉覆盖丢数据
    with _LIBRARY_LOCK:
        items = list_items(kind)
        replaced = False
        for i, it in enumerate(items):
            if it.get("id") == item["id"]:
                items[i] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
        atomic_write_json(_path(kind), {"items": items}, bak_every=1)  # ③G3-1 滚动 .bak


def delete_item(kind: str, item_id: str) -> bool:
    # F1 加锁止血
    with _LIBRARY_LOCK:
        items = list_items(kind)
        new_items = [it for it in items if it.get("id") != item_id]
        if len(new_items) == len(items):
            return False
        atomic_write_json(_path(kind), {"items": new_items}, bak_every=1)  # ③G3-1 滚动 .bak
        return True


def _prune_scopes_in_kind(kind: str, projects=(), episodes=(), scenes=()) -> int:
    """回扫某类资产库，摘除 scope 中引用给定 project/episode/scene 的项（命中才重写）。
    供删除 project/episode/scene 后清理残留 scope（防止悬空引用）。返回被清理的资产数。"""
    items = list_items(kind)
    target_p = set(projects)
    target_e = set(episodes)
    target_s = set(scenes)
    if not (target_p or target_e or target_s):
        return 0
    changed = 0
    for it in items:
        sc = it.get("scopes")
        if not isinstance(sc, dict):
            continue
        pr = [x for x in sc.get("projects", []) if x not in target_p]
        ep = [x for x in sc.get("episodes", []) if x not in target_e]
        sn = [x for x in sc.get("scenes", []) if x not in target_s]
        if len(pr) != len(sc.get("projects", [])) or len(ep) != len(sc.get("episodes", [])) or len(sn) != len(sc.get("scenes", [])):
            sc["projects"], sc["episodes"], sc["scenes"] = pr, ep, sn
            it["updated_at"] = now_iso()
            changed += 1
    if changed:
        atomic_write_json(_path(kind), {"items": items}, bak_every=1)  # ③G3-1 滚动 .bak
    return changed


def prune_project_scopes(pid: str) -> dict:
    """删除项目后清理：摘除所有资产卡 scope 里对该项目的引用（含其全部集/场由 delete 层负责，此处只清 project 级）。"""
    total = 0
    for k in KINDS:
        total += _prune_scopes_in_kind(k, projects=(pid,))
    return {"pruned": total}


def prune_episode_scopes(eid: str) -> dict:
    """删除集后清理：摘除所有资产卡 scope 里对该集的引用（含其下的场由 delete 层负责，此处只清 episode 级）。"""
    total = 0
    for k in KINDS:
        total += _prune_scopes_in_kind(k, episodes=(eid,))
    return {"pruned": total}


def prune_scene_scopes(sid: str) -> dict:
    """删除场后清理：摘除所有资产卡 scope 里对该场的引用。"""
    total = 0
    for k in KINDS:
        total += _prune_scopes_in_kind(k, scenes=(sid,))
    return {"pruned": total}


# ---- 统一资产模型（v1.25 定稿：平行独立资产） ----
# "名称-变体名称" = 一个完整独立资产（如 程度-50岁）；"名称"仅为分组前缀，非资产实体；
# 每个资产各自完整：归属 scopes / 提示词 / 图版本 / 音色文本 / 音频（互不共享，无并集）；
# 人物至少 1 变体，默认"基础形象"。

def new_asset(kind: str, prefix: str, variant_name: str = "", scopes: dict | None = None,
              prompt: str = "", images: list | None = None, voice_text: str = "",
              audios: list | None = None, voice_source: str = "text", voice: str = "") -> dict:
    now = now_iso()
    return {
        "id": new_id(KINDS[kind]["id_prefix"]),
        "prefix": prefix,                                   # 名称（分组前缀）
        "variant_name": variant_name or "基础形象",          # 变体名（人物默认基础形象）
        "scopes": scopes or {"projects": [], "episodes": [], "scenes": []},   # v1.34 归属三层
        "prompt": prompt,                                   # 提示词（当前生效，随图版本联动）
        "images": images or [],                             # 图版本历史 [{path, version, gen_prompt, source, added_at}]
        "voice_text": voice_text,                           # 音色文本（仅人物）
        "audios": audios or [],                             # 音频 [{path, type: trial/reference, role: voice_sample/reference/trial, voice, added_at}]（仅人物）
                                                            # v1.55/v1.58：role=voice_sample=音色样本干音（Qwen3-TTS 生成，H3 音色克隆输入）
                                                            # 兼容：旧条目无 role 视为 reference（不破坏现有数据）
        "voice_source": voice_source or "text",             # 音色来源单选：text / audio / edge-tts（v1.26/4.12）
        "voice": voice,                                     # edge-tts 预置音色 id（voice_source=edge-tts 时）
        "created_at": now,
        "updated_at": now,
    }


def display_name(asset: dict) -> str:
    return f"{asset.get('prefix', '')}-{asset.get('variant_name', '')}"


# ==================== 参考类资产（kind=reference，计划书 v1.53 → 2026-09-04 模型统一升级） ====================
# 【2026-09-04 用户裁定 A】"图落地即资产、资产库无游离图"，参考类与人物/场景/道具三库资产**统一为同一结构**，
#   可复用三库元数据编辑器(openMeta)直接编辑保存；未分类裸图/尾帧截帧可落地为参考类资产。
# 统一结构 = 三库资产字段 + 保留引用字段(供 refs/ 物理图 & 参考派生 & 引用计数)：
#   { id, prefix(名称分组), variant_name(素材名,默认基础素材), scopes{projects/episodes/scenes},
#     prompt, images[]({path,...}), voice_text, audios[], voice_source, voice,
#     path(主图相对路径,兼容派生/删除物理), ref_count, ref_sources[], created_at, updated_at }
# 兼容：旧条目 {id,path,ref_count,ref_sources,created_at,updated_at} 自动迁移为上述结构（prefix 取文件名、images=[{path}]）。
# 物理文件默认存 <shared>/refs/；不进 library.KINDS（避免污染三库遍历/审计/引用对账），由独立函数维护同一 references.json。
REF_ID_PREFIX = "re_"
REF_VARIANT_DEFAULT = "基础素材"


def _refs_path_from_cfg() -> Path:
    """参考图物理目录 = <shared_assets>/refs（用户裁定，2026-08-26）。"""
    return Path(get_shared_assets_path()) / "refs"


def _references_path() -> Path:
    return DATA_DIR / "references.json"


def _norm_ref_path(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("/")


def _migrate_old_ref(it: dict) -> dict:
    """旧扁平条目 {id,path,ref_count,...} → 三库统一结构（幂等：已是新结构则原样）。"""
    if not isinstance(it, dict):
        return it
    if "images" in it or "prefix" in it:
        # 已是新结构（含空 images 也视为新，保留）
        return it
    p = _norm_ref_path(it.get("path") or "")
    stem = Path(p).name or p
    # 名称取文件名（去 id_ 前缀）："refs/d193e8a1_红色请柬.png" → "红色请柬"
    name = stem
    if "_" in stem:
        name = stem.split("_", 1)[1] if stem.split("_", 1)[1] else stem
    name = Path(name).stem or name
    now = it.get("updated_at") or it.get("created_at") or now_iso()
    return {
        "id": it.get("id") or new_id(REF_ID_PREFIX),
        "prefix": name,
        "variant_name": REF_VARIANT_DEFAULT,
        "scopes": {"projects": [], "episodes": [], "scenes": []},
        "prompt": "",
        "images": [{"path": p, "version": "", "gen_prompt": "", "source": "reference", "added_at": now}] if p else [],
        "voice_text": "",
        "audios": [],
        "voice_source": "text",
        "voice": "",
        "path": p,                                  # 兼容：保留主图相对路径（派生/删除物理用）
        "ref_count": it.get("ref_count") or 0,
        "ref_sources": it.get("ref_sources") or [],
        "created_at": it.get("created_at") or now,
        "updated_at": now,
    }


def _load_reference_items() -> list:
    import json
    try:
        with open(_references_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _write_reference_items(items: list) -> None:
    atomic_write_json(_references_path(), {"items": items}, bak_every=1)  # ③G3-1 滚动 .bak


def _refs_ensure_migrated() -> None:
    """幂等迁移：旧扁平条目升级为统一结构（读时惰性迁移 + 落盘一次）。"""
    items = _load_reference_items()
    migrated = False
    for it in items:
        if isinstance(it, dict) and "images" not in it and "prefix" not in it:
            migrated = True
            break
    if migrated:
        new_items = [_migrate_old_ref(it) for it in items]
        _write_reference_items(new_items)


def list_references() -> list:
    """返回统一结构参考类资产列表。

    收尾A③(F6)：读路径零写副作用 —— 不再调 _refs_ensure_migrated()（迁移写盘已挪至启动自愈
    init_library 一次完成）；运行期此处纯读 references.json。
    """
    return _load_reference_items()


def get_reference(ref_id: str) -> dict | None:
    for it in list_references():
        if it.get("id") == ref_id:
            return it
    return None


def find_reference_by_path(path: str) -> dict | None:
    p = _norm_ref_path(path)
    for it in list_references():
        # 主图 path 字段或 images[0].path 命中即认（兼容新旧）
        if _norm_ref_path(it.get("path")) == p:
            return it
        im = (it.get("images") or [])
        if im and _norm_ref_path(im[0].get("path")) == p:
            return it
    return None


def find_materialized_by_src_rel(src_rel: str) -> dict | None:
    """收尾A④：判断某源图 rel 是否**已资产化**（已有素材卡）。

    兼容两种资产化落法：①materialize 复制到 refs/ 的卡（_src_rel==源 rel）；
    ②register_file 自动资产化指向原图的卡（主图 path/images[0]==源 rel）。
    命中 → 返回该卡（调用方应跳过重复建卡）。幂等统一入口。
    """
    s = _norm_ref_path(src_rel)
    if not s:
        return None
    for it in list_references():
        if _norm_ref_path(it.get("_src_rel")) == s:
            return it
        # 方案A：卡主图直接指向源图（path 或 images[0].path == s）
        if _norm_ref_path(it.get("path")) == s:
            return it
        im = (it.get("images") or [])
        if im and _norm_ref_path(im[0].get("path")) == s:
            return it
    return None


@contextlib.contextmanager
def lock():
    """G1-1(审计P3, 09-11)：对外暴露全局写锁，供调用方按「先 library 后 assets_index」组合临界区。

    铁律（锁序不可反）：持有本锁后**可以**再取 assets_index._INDEX_LOCK；
    反之（先持 _INDEX_LOCK 再取本锁）会造成与库内既有路径相反的锁序 → 禁止。
    本锁为 RLock（可重入），库内 save_reference/inc_* 等在同一线程内可安全重入。
    """
    with _LIBRARY_LOCK:
        yield


def build_reference(prefix: str = "", path: str = "", source_asset_id: str = "",
                    variant_name: str = "", scopes: dict | None = None, prompt: str = "",
                    images: list | None = None, voice_text: str = "", audios: list | None = None,
                    voice_source: str = "text", voice: str = "") -> dict:
    """只构造参考类资产 dict，**不落盘**（G1-2 批量建卡用）。
    落盘请走 new_reference()（单张）或 new_references_batch()（批量，一次写）。"""
    p = _norm_ref_path(path)
    stem = Path(p).name or p
    name = stem.split("_", 1)[1] if (stem and "_" in stem and stem.split("_", 1)[1]) else stem
    name = (Path(name).stem or name).strip() or "未命名素材"
    now = now_iso()
    cur_prefix = (prefix or "").strip() or name
    images = images if isinstance(images, list) and images else ([{"path": p, "version": "", "gen_prompt": "", "source": "reference", "added_at": now}] if p else [])
    return {
        "id": new_id(REF_ID_PREFIX),
        "prefix": cur_prefix,
        "variant_name": variant_name or REF_VARIANT_DEFAULT,
        "scopes": scopes or {"projects": [], "episodes": [], "scenes": []},
        "prompt": prompt or "",
        "images": images,
        "voice_text": voice_text or "",
        "audios": audios or [],
        "voice_source": voice_source or "text",
        "voice": voice or "",
        "path": p,
        "ref_count": 1 if source_asset_id else 0,
        "ref_sources": [source_asset_id] if source_asset_id else [],
        "created_at": now,
        "updated_at": now,
    }


def new_reference(prefix: str = "", path: str = "", source_asset_id: str = "",
                  variant_name: str = "", scopes: dict | None = None, prompt: str = "",
                  images: list | None = None, voice_text: str = "", audios: list | None = None,
                  voice_source: str = "text", voice: str = "") -> dict:
    """新建统一结构参考类资产。prefix 缺省时由 path 文件名派生（上传参考图走此，落地即资产）。"""
    ref = build_reference(prefix=prefix, path=path, source_asset_id=source_asset_id,
                          variant_name=variant_name, scopes=scopes, prompt=prompt,
                          images=images, voice_text=voice_text, audios=audios,
                          voice_source=voice_source, voice=voice)
    save_reference(ref)
    return ref


def new_references_batch(refs: list) -> list:
    """G1-2(审计P3, 09-11)：批量落盘参考类资产——**一次 load + 一次 write**。

    替代「逐张 save_reference」各做一次 references.json 全量读改写（N 张卡 = O(N²) 写放大）。
    入参为 build_reference() 构造好的 ref dict 列表；同 id 视为更新，否则追加。
    """
    if not refs:
        return []
    with _LIBRARY_LOCK:
        items = _load_reference_items()
        idx = {it.get("id"): i for i, it in enumerate(items)}
        out = []
        for raw_ref in refs:
            ref = _migrate_old_ref(raw_ref)
            ref.setdefault("variant_name", REF_VARIANT_DEFAULT)
            ref.setdefault("scopes", {"projects": [], "episodes": [], "scenes": []})
            pos = idx.get(ref.get("id"))
            if pos is None:
                idx[ref.get("id")] = len(items)
                items.append(ref)
            else:
                items[pos] = ref
            out.append(ref)
        _write_reference_items(items)
        return out


def materialized_src_set() -> set:
    """G1-2(审计P3, 09-11)：一次性返回「已被素材卡覆盖」的规范化源路径集合。

    与 find_materialized_by_src_rel() 的匹配口径完全一致（_src_rel / path / images[0].path），
    供批量幂等预筛用，避免逐项调用触发 N 次 references.json 全量读（O(N²) 读放大）。
    """
    out = set()
    try:
        for it in list_references():
            for k in ("_src_rel", "path"):
                v = it.get(k)
                if v:
                    out.add(_norm_ref_path(v))
            im = it.get("images") or []
            if im and isinstance(im[0], dict) and im[0].get("path"):
                out.add(_norm_ref_path(im[0]["path"]))
    except Exception:
        pass
    return out


def save_reference(ref: dict) -> None:
    # F1 加锁止血: 锁包"读改写"整段(references.json 并发写防丢数据)
    with _LIBRARY_LOCK:
        items = _load_reference_items()
        # 统一结构保障（幂等补齐字段，容忍外部传参缺省）
        ref = _migrate_old_ref(ref)
        ref.setdefault("variant_name", REF_VARIANT_DEFAULT)
        ref.setdefault("scopes", {"projects": [], "episodes": [], "scenes": []})
        replaced = False
        for i, it in enumerate(items):
            if it.get("id") == ref["id"]:
                items[i] = ref
                replaced = True
                break
        if not replaced:
            items.append(ref)
        _write_reference_items(items)


def delete_reference(ref_id: str) -> bool:
    # F1 加锁止血
    with _LIBRARY_LOCK:
        items = _load_reference_items()
        new_items = [it for it in items if it.get("id") != ref_id]
        if len(new_items) == len(items):
            return False
        _write_reference_items(new_items)
        return True


def ref_primary_path(ref: dict) -> str:
    """返回参考类资产的"主图路径"（images[0].path 优先，回退 path 字段）——供 AssetCard/删除物理/派生统一取图。"""
    if isinstance(ref, dict):
        im = ref.get("images") or []
        if im and isinstance(im[0], dict) and im[0].get("path"):
            return str(im[0]["path"])
        if ref.get("path"):
            return str(ref["path"])
    return ""


def inc_reference_source(ref_id: str, asset_id: str | None) -> None:
    """某资产用参考类资产生成 → 引用计数+1、引用源[] 记所引用资产ID（幂等：同资产不重复计）。

    收尾A⑥：读改写入锁（_LIBRARY_LOCK 串行），消除并发 inc 丢更新竞态；同 F1 模式。
    """
    if not ref_id:
        return
    with _LIBRARY_LOCK:
        ref = get_reference(ref_id)
        if not ref:
            return
        sources = ref.get("ref_sources") or []
        if asset_id and asset_id not in sources:
            sources.append(asset_id)
        ref["ref_sources"] = sources
        ref["ref_count"] = len(sources)
        ref["updated_at"] = now_iso()
        save_reference(ref)   # RLock 可重入；整段读改写已在外层锁内串行


def dec_reference_for_asset(asset_id: str | None) -> None:
    """资产删除 → 所有相关参考类资产 计数-1、引用源[] 移除该资产。

    收尾A⑥：读改写入锁（_LIBRARY_LOCK），消除并发 dec 竞态。
    """
    if not asset_id:
        return
    with _LIBRARY_LOCK:
        for ref in list_references():
            sources = ref.get("ref_sources") or []
            if asset_id in sources:
                sources.remove(asset_id)
                ref["ref_sources"] = sources
                ref["ref_count"] = len(sources)
                ref["updated_at"] = now_iso()
                save_reference(ref)
