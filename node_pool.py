# -*- coding: utf-8 -*-
"""node_pool.py —— 生成节点池（批次③ F5b · 步1 实例路由）

★ 职责（本步范围）：
  - 节点注册表（data/nodes.json）：增删 / 热生效（每次操作读盘，无需重启 —— 方案 §C-C5）；
    `local` 节点固定存在且不可删，url="" 表示**动态取 settings.comfyui_url**（向后兼容既有配置）。
  - 探活 + 负载：GET /queue 一次拿到（online + running+pending 数）；健康结果缓存 10s（probe 有成本，
    负载永不缓存 —— C3 双信号要实时）。
  - 选节点 `pick_node(target_node)`（调度核心，由 task_queue 在**派发时**调用）：
      · pinned（"local" 或注册节点 id）→ 校验在册 + 可达，返回该节点；
      · auto（""）→ 健康节点里选「双信号取 max 后最小」者（方案 §C-C1/C3）：
            load(node) = max(本地预留（RUNNING 中已占该节点的任务数）, 远端 /queue 实测)
        平局取注册顺序第一个（稳定可预期）；
      · 全部不健康 → **回退首个 enabled 节点**（方案 §C-C4 口径「全挂回退首个或报错」——
        选前者，让 job 上抛真实连接错误，比静默吞掉可诊断）。
  ★ 纪律（承 ④ 步4-④）：任何 job 不得直接调 get_comfyui_url() 解析生成地址；
    派发时选定的节点落 meta.node / meta.node_url，job 经 task_queue.node_url_for() 取用。
"""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NODES_FILE = ROOT / "data" / "nodes.json"

_LOCK = threading.Lock()
_HEALTH_TTL = 10.0            # 探活缓存（秒）—— 负载不缓存
MAX_SLOTS = 8                 # ★ ③ 步2b：单节点并发**执行槽**上限（防呆）。
                              #   slots 语义 = 「该节点同一时刻最多允许几个 RUNNING」；**默认 1**
                              #   = 与旧的全局串行完全等价；slot 数只在**该节点内**并发，跨节点天然并行。
_HEALTH: dict = {}            # url -> (ts, {"online": bool, "load": int})


# ★ 用户 2026-10-01（界面调整 Q2）：本机 ComfyUI 地址**改由「本地平台」的 local 节点承载**
#   —— 设置页原有「其他设置 → ComfyUI 地址」已删除，local 节点地址可在本地平台里直接改。
#   旧配置里 local.url 为空时自动迁移到本默认值（见 _load）。
# ★ ⑦ 验收 3 步整改 A-P0-1（2026-10-02）：默认值**不得硬编码开发机 tailnet 地址**
#   —— 原值 `http://100.94.21.26:8188` 是开发机 Tailscale IP，真实用户环境必然不存在
#   ⇒ local 节点恒离线。改为本机回环地址（发布形态 = 单机部署，ComfyUI 与工坊同机）。
DEFAULT_LOCAL_URL = "http://127.0.0.1:8188"


def _default() -> dict:
    return {"nodes": [{"id": "local", "name": "本机", "url": DEFAULT_LOCAL_URL, "enabled": True, "slots": 1}]}


def _norm_slots(raw) -> int:
    """★ ③ 步2b：槽数校验（**fail-closed**，与 2a 合规同口径）——非法抛 ValueError → 端点 400。

    ★ 严格口径：**只收「整数」或「纯数字字符串」**。
      `2.5` / `True` / `"2.5"` 一律拒 —— 否则 `int(2.5)=2` 会**静默截断**成合法值
      （单测抓到的真缺陷：用户填 2.5 却拿到 2 槽，无任何提示）。
    """
    if isinstance(raw, bool):                     # bool 是 int 子类，必须先挡
        raise ValueError("槽数必须是 1–%d 的整数（当前 %r）" % (MAX_SLOTS, raw))
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        n = int(raw.strip())
    else:
        raise ValueError("槽数必须是 1–%d 的整数（当前 %r）" % (MAX_SLOTS, raw))
    if not (1 <= n <= MAX_SLOTS):
        raise ValueError("槽数必须是 1–%d 的整数（当前 %d）" % (MAX_SLOTS, n))
    return n


def _slots_of(node: dict) -> int:
    """★ ③ 步2b：读节点并发槽（**读时规范化** —— 老注册表无该字段 → 1；非法/越界 → 1）。

    默认 1 = 「每节点同一时刻最多 1 个 RUNNING」= 与旧全局串行完全等价。
    不写盘（与"C5 热生效、每次读盘"一致）：注册表老数据无需迁移即向后兼容。
    与 `_norm_slots` 同口径（含拒绝 2.5 这类会被静默截断的值）。
    """
    try:
        return _norm_slots(node.get("slots", 1))
    except ValueError:
        return 1


# ★★ ⑦ 验收 3 步整改 A-P1-1（2026-10-02）：节点**能力标签**（计划书 §四「能力匹配（硬约束）」落地）。
#   语义：`capability` = 该节点**能跑哪些 workflow_type**；**空/缺省 = 全能力（不过滤）**。
#   ★ 为什么必须有：双机单部署下 **两机模型不重叠** —— 18 只装 Qwen-Image-2.1（qwen21/qwen21_edit），
#     24 是 H3 旧版（z_image/2511/klein/enhance/upscale）。没有能力过滤时 auto 会把 qwen21 派给 24、
#     把 z_image 派给 18 ⇒ **必然失败**。
#   ★ 不硬校验工作流名（node_pool 不 import routers_gen，避免模块耦合）：写错名字的后果是
#     「任务报『池内无具备该能力的节点』」—— 报错里带上各节点实际能力，可诊断，不是静默。
#   当前生成工作流全集（供配置参考，真源见 routers_gen._GEN_WORKFLOWS）：
#     z_image / 2511 / klein / enhance / upscale / qwen21 / qwen21_edit
#   ⚠ 视频/音频类任务的 payload **没有 workflow_type 键** ⇒ 传空 ⇒ 不参与能力过滤（行为不变）。
def _norm_capability(raw) -> list:
    """规范化能力标签（fail-closed：类型非法 → ValueError，端点转 400 且零写入）。

    接受：`None` / `""` → `[]`（= 全能力）；`list`/`tuple` → 逐项 strip；逗号分隔字符串 → 拆分。
    非法（其它类型 / 含非字符串项）→ ValueError。项内**去重保序**；容忍多余逗号与空项。
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        items = raw.replace("，", ",").split(",")          # 容忍中文逗号
    else:
        raise ValueError("能力标签必须是字符串列表或逗号分隔字符串（当前 %r）" % (raw,))
    out: list = []
    for it in items:
        if not isinstance(it, str):
            raise ValueError("能力标签每一项都必须是字符串（当前 %r）" % (it,))
        s = it.strip()
        if not s:
            continue                                       # 容忍 "a,,b" / 尾随逗号
        if s not in out:
            out.append(s)
    return out


def _capability_of(node: dict) -> list:
    """读节点能力（**读时规范化**；脏数据 → 视作全能力 `[]`，不让一条坏字段把节点整体废掉）。"""
    try:
        return _norm_capability(node.get("capability"))
    except ValueError:
        return []


def _capable(node: dict, workflow_type: str) -> bool:
    """★ A-P1-1：能力匹配判据 —— `workflow_type` 为空**不约束**（兼容无该键的任务类）；空能力 = 全能力。"""
    wf = (workflow_type or "").strip()
    if not wf:
        return True
    cap = _capability_of(node)
    return (not cap) or (wf in cap)


def _load() -> dict:
    """读注册表（无文件/损坏 → 默认仅本机；每次读盘 = 热生效）。

    ★ 用户 10-01：local 节点若**没有地址**（老配置里 url="" ⇒ 当时动态取 settings.comfyui_url），
    就地补成 `DEFAULT_LOCAL_URL` **并落盘一次** —— 此后本机地址由注册表承载，不再依赖 settings。
    """
    d = None
    try:
        d = json.load(io.open(NODES_FILE, encoding="utf-8"))
        nodes = d.get("nodes")
        if not isinstance(nodes, list) or not any(n.get("id") == "local" for n in nodes):
            return _default()
    except Exception:
        return _default()
    try:
        for n in d.get("nodes") or []:
            if n.get("id") == "local" and not (n.get("url") or "").strip():
                n["url"] = DEFAULT_LOCAL_URL
                _save(d)                      # 一次性迁移（之后 url 有值，不再写盘）
                break
    except Exception:
        pass
    return d


def _save(d: dict) -> None:
    NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = NODES_FILE.with_suffix(".json.tmp")
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(json.dumps(d, ensure_ascii=False, indent=2))
    tmp.replace(NODES_FILE)                      # 原子替换（与 task_store 同口径）


def local_url() -> str:
    """本机 ComfyUI 地址 —— **唯一入口**（★ 用户 10-01：改为读「本地平台」local 节点的地址）。

    ⚠ 这里**绝不反向调用** `library.get_comfyui_url()`（那会形成循环：library → node_pool → library）。
      依赖方向固定为：**库层读节点池**（library.get_comfyui_url 调本函数），单向。
    """
    try:
        for n in (_load().get("nodes") or []):
            if n.get("id") == "local":
                u = (n.get("url") or "").strip()
                return u.rstrip("/") if u else DEFAULT_LOCAL_URL
    except Exception:
        pass
    return DEFAULT_LOCAL_URL


def _resolved_url(node: dict) -> str:
    url = (node.get("url") or "").strip()
    return url or local_url() if node.get("id") == "local" else url


def has_node(node_id: str) -> bool:
    tn = (node_id or "").strip()
    if tn in ("", "local"):
        return True
    return any(n.get("id") == tn for n in _load().get("nodes", []))


def list_nodes(with_status: bool = False) -> list:
    """节点清单（注册顺序）；with_status=True 时附 resolved_url/online/load。"""
    out = []
    for n in _load().get("nodes", []):
        item = {"id": n.get("id"), "name": n.get("name") or n.get("id"),
                "url": n.get("url") or "", "enabled": bool(n.get("enabled", True)),
                "slots": _slots_of(n),
                # ★ A-P1-1：能力标签（空列表 = 全能力）——供前端展示与调度过滤共用同一读法。
                "capability": _capability_of(n),
                # ★★ 步2b附修：`resolved_url` **恒返回**（纯计算、零开销）。
                #   原来只在 with_status=True 时才带 —— 而 `task_queue.resolve_node_url()` 用的是
                #   `list_nodes()`（无状态）→ 对**注册节点**恒拿到 None → 拼出 `"None/system_stats"`
                #   → 请求抛异常 → `/api/video/generate` 的 pinned 提交被误判「节点不可达」（400）。
                #   这是 **步1 起就潜伏**的缺陷：只有 video 的「提交期 pinned 可达性校验」会走到这条路
                #   （job 执行期走 `node_url_for`→meta.node_url，因此一直没暴露）。
                "resolved_url": _resolved_url(n)}
        if with_status:
            rurl = item["resolved_url"]
            h = probe(rurl) if item["enabled"] else {"online": False, "load": 0}
            item["online"] = bool(h.get("online"))
            item["load"] = int(h.get("load") or 0)
            # ★ ③ 步2b：本节点当前「在制」数（RUNNING 中 meta.node 命中）→ UI 显示「在制 x / 槽 n」
            item["running"] = _local_reserved(n.get("id"))
        out.append(item)
    return out


def parse_host_port(raw: str):
    """★ ③ 步2a 合规校验：地址 → (scheme, host, port)。

    规则（任务单 §一②，fail-closed）：
      · **必须 host + port 成对**（本机 18:8188 与 24:8188 同端口不同含义 → 只填端口不算合格）；
      · port 为整数且 1 ≤ port ≤ 65535；
      · host 非空；
      · 接受 `host:port` 与 `http(s)://host:port`（前者自动补 http:// 便于用户输入）。
    不合法 → ValueError（带具体原因，调用方转 400）。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("节点地址不能为空（需同时填写主机与端口，如 127.0.0.1:8189）")
    scheme = "http://"
    rest = raw
    if raw.lower().startswith(("http://", "https://")):
        scheme, rest = raw.split("://", 1)
        scheme += "://"
        rest = rest.strip()
    rest = rest.rstrip("/")
    host, _, port_s = rest.rpartition(":")
    if not host:
        raise ValueError("节点地址必须同时包含主机与端口（如 127.0.0.1:8189）—— 只填端口不算合格")
    port_s = port_s.strip()
    if not port_s or not port_s.isdigit():
        raise ValueError("节点地址必须同时包含主机与端口（如 127.0.0.1:8189）；端口必须是数字")
    port = int(port_s)
    if not (1 <= port <= 65535):
        raise ValueError("端口必须是 1–65535 的整数（当前 %s）" % port_s)
    if "/" in host:
        raise ValueError("主机部分不合法：%s" % host)
    return scheme, host, port


def add_node(name: str, url: str, slots: int = 1, capability=None) -> dict:
    """新增节点（热生效）。★ ③ 步2a：四条合规校验 **fail-closed**（违规抛 ValueError → 端点 400，
    且**不改注册表**，零脏写入）。★ ③ 步2b：`slots` 并发执行槽（默认 1，非法 → 400 同样零写入）。
    ★ A-P1-1：`capability` 能力标签（可空 = 全能力；非法 → 400 零写入）。
    返回新节点（url 统一规范化为 `http://host:port`）。"""
    slots = _norm_slots(slots)                    # ★ 步2b：先校验槽数（fail-closed）
    cap = _norm_capability(capability)            # ★ A-P1-1：能力标签（fail-closed）
    # ① 名称非空（去首尾空格后 ≥1）
    nm = (name or "").strip()
    if not nm:
        raise ValueError("节点名称不能为空")
    # ②/④ 地址：host+port 成对 + 端口范围（parse_host_port 内含）
    scheme, host, port = parse_host_port(url)
    norm = "%s%s:%d" % (scheme, host, port)
    with _LOCK:
        d = _load()
        # ③ 重复：同名 或 同 host:port（含与本机节点 resolved 地址）→ 拒绝并指明冲突对象
        for n in d.get("nodes", []):
            if (n.get("name") or "").strip() == nm:
                raise ValueError("名称「%s」与已有节点（%s）重复" % (nm, n.get("name")))
            if n.get("id") == "local":
                try:                                  # 本机节点 url 为空 = 动态取 settings → 用 resolved 比
                    _, lhost, lport = parse_host_port(_resolved_url(n))
                except ValueError:
                    continue
                if (lhost, lport) == (host, port):
                    raise ValueError("地址 %s 与本机节点（local）重复 —— 同一台机器已在池中" % norm)
                continue
            try:
                _, ohost, oport = parse_host_port(n.get("url"))
            except ValueError:
                continue
            if (ohost, oport) == (host, port):
                raise ValueError("地址 %s 与已有节点「%s」重复" % (norm, n.get("name")))
        nid = "nd_%s" % ("%x" % (int(time.time() * 1000) & 0xFFFFFFFF))
        while any(n.get("id") == nid for n in d["nodes"]):     # 极小概率同毫秒
            nid = "nd_%s" % ("%x" % (int(time.time() * 1001) & 0xFFFFFFFF))
        node = {"id": nid, "name": nm, "url": norm, "enabled": True, "slots": slots, "capability": cap}
        d["nodes"].append(node)
        _save(d)
        return node


def set_capability(node_id: str, capability) -> dict:
    """★ A-P1-1：设置某节点的**能力标签**（热生效）。空 = 全能力。
    非法 → ValueError（端点 400）且**不写注册表**；节点不存在 → ValueError("节点不存在")。
    """
    cap = _norm_capability(capability)
    nid = (node_id or "").strip()
    with _LOCK:
        d = _load()
        target = None
        for n in d.get("nodes", []):
            if n.get("id") == nid:
                target = n
                break
        if target is None:
            raise ValueError("节点不存在")
        target["capability"] = cap
        _save(d)
        return dict(target)


def set_node_url(node_id: str, url: str) -> dict:
    """改某个节点的地址（用户 10-01：本机地址不再由设置页配置，改在「本地平台」里改）。

    校验口径与 add_node 完全一致（host+port 成对 / 端口范围 / 不与他人重复），
    **fail-closed**：违规抛 ValueError（端点转 400）且**不写注册表**。
    """
    scheme, host, port = parse_host_port(url)
    norm = "%s%s:%d" % (scheme, host, port)
    nid = (node_id or "").strip()
    with _LOCK:
        d = _load()
        target = None
        for n in d.get("nodes", []):
            if n.get("id") == nid:
                target = n
                continue
            other = (_resolved_url(n) or "").strip()
            if not other:
                continue
            try:
                _, ohost, oport = parse_host_port(other)
            except ValueError:
                continue
            if "%s:%d" % (ohost, oport) == "%s:%d" % (host, port):
                raise ValueError("地址 %s 已被节点「%s」占用 —— 同一台机器不要在池中重复"
                                 % (norm, n.get("name") or n.get("id")))
        if target is None:
            raise ValueError("节点不存在")
        target["url"] = norm
        _save(d)
        return dict(target)


def remove_node(node_id: str) -> bool:
    """删除节点（local 不可删；热生效）。返回是否删除了东西。"""
    node_id = (node_id or "").strip()
    if node_id in ("", "local"):
        raise ValueError("本机节点不可删除")
    with _LOCK:
        d = _load()
        before = len(d["nodes"])
        d["nodes"] = [n for n in d["nodes"] if n.get("id") != node_id]
        if len(d["nodes"]) == before:
            return False
        _save(d)
        return True


def set_enabled(node_id: str, enabled: bool) -> bool:
    with _LOCK:
        d = _load()
        hit = [n for n in d["nodes"] if n.get("id") == node_id]
        if not hit or node_id == "local":
            return False
        hit[0]["enabled"] = bool(enabled)
        _save(d)
        return True


def set_slots(node_id: str, slots: int) -> bool:
    """★ ③ 步2b：设置节点并发执行槽（**热生效**，与 C5 一致 —— 每次读盘，无需重启）。

    与 `set_enabled` 的差别：**local 节点也允许改槽**（槽数只影响调度并发度，不改变节点身份/可删性）；
    非法槽数 → ValueError（端点 400，注册表零改动）。
    """
    n = _norm_slots(slots)
    with _LOCK:
        d = _load()
        hit = [x for x in d["nodes"] if x.get("id") == node_id]
        if not hit:
            return False
        hit[0]["slots"] = n
        _save(d)
        return True


def probe(url: str, timeout: float = 4.0, use_cache: bool = True) -> dict:
    """探活 + 负载：GET /queue 一次拿两样（online + running+pending）。健康缓存 _HEALTH_TTL。"""
    url = (url or "").rstrip("/")
    now = time.time()
    if use_cache:
        hit = _HEALTH.get(url)
        if hit and now - hit[0] < _HEALTH_TTL:
            return hit[1]
    import requests
    result = {"online": False, "load": 0}
    try:
        r = requests.get(url + "/queue", timeout=timeout)
        if r.status_code == 200:
            q = r.json()
            result = {"online": True,
                      "load": len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])}
    except Exception:
        result = {"online": False, "load": 0}
    _HEALTH[url] = (now, result)
    return result


def _reserved_map() -> dict:
    """★ 步2b：一次性统计「各节点在制任务数」（本地预留信号）—— 调度每轮要问多节点，避免逐节点重复扫表。"""
    import task_store
    out = {}
    for t in task_store.list_tasks():
        if t.get("state") != "running":
            continue
        m = t.get("meta")
        m = m if isinstance(m, dict) else (json.loads(m or "{}") if isinstance(m, str) else {})
        nid = m.get("node")
        if nid:
            out[nid] = out.get(nid, 0) + 1
    return out


def _local_reserved(node_id: str) -> int:
    """本地预留信号：RUNNING 中已占该节点的任务数（单点查询，供 list_nodes 显示「在制」用）。"""
    return _reserved_map().get(node_id, 0)


def pick_node(target_node: str = "", busy: dict | None = None, workflow_type: str = ""):
    """★ 调度核心：派发时选节点。返回 `(node_id, url)`；**返回 `None` = 池内可达但全部满载 → 调用方等待**。

    - pinned（"local"/注册 id）：
        · 不在册 / 不可达 → raise ValueError（任务 fail 带真实原因；A4 自动重试会重新路由）；
        · ★ **能力不符 → raise ValueError**（A-P1-1：不派给不支持的节点）；
        · ★ **在册可达但该节点槽已满 → 返回 None**（"忙"不是错误 → 排队等待，绝不超槽）。
    - auto（""）：
        · ★ **先按能力过滤**（A-P1-1）→ 在「online **且有空槽**」的候选里选 `load = max(本地预留, 远端 /queue)` 最小者（C1/C3）；
          平局取注册顺序第一个（稳定可预期）；
        · ★ **池内无任何节点具备该能力 → raise ValueError**（明确报错，而非派给不支持的节点）；
        · ★ 有 online 但**全部满槽** → 返回 None（等待）；
        · 全部不可达 → 回退**首个 enabled 且能力匹配**的候选（C4「全挂回退首个」，错误由 job 真实上抛）。
    - `busy`：调度器的**在途计数** `{node_id: n}`（★ 步2b）—— 与本地预留**取 max** 作占用信号
      （claim 后到 meta 落盘之间有个窗口期，靠它兜住，否则调度器会在窗口内重复派给同一节点 → 同节点并行）。
      缺省（None）时只用本地预留 → 与步2b-① 之前的行为完全一致（向后兼容）。
    - `workflow_type`：★ A-P1-1 新增（能力匹配的唯一输入）。**空 = 不约束**
      （老调用点/视频音频类任务不带该键 → 行为完全不变）。
    """
    tn = (target_node or "").strip()
    wf = (workflow_type or "").strip()
    nodes = [n for n in _load().get("nodes", []) if n.get("enabled", True)]
    if not nodes:                                   # 注册表被清空 → 视同仅本机
        nodes = _default()["nodes"]
    reserved = _reserved_map()

    def _used(n) -> int:
        u = reserved.get(n.get("id"), 0)
        if busy:
            try:
                u = max(u, int(busy.get(n.get("id")) or 0))
            except (TypeError, ValueError):
                pass
        return u

    if tn:                                          # ── pinned ──
        hit = [n for n in nodes if n.get("id") == tn] or \
              ([n for n in nodes if n.get("id") == "local"] if tn == "local" else [])
        if not hit:
            raise ValueError("生成节点 '%s' 不在节点池中（可在队列页节点区查看在册节点）" % tn)
        n = hit[0]
        # ★★ A-P1-1：pinned 也**先按能力过滤** —— 指定了不支持的节点 → **明确报错**，
        #   不派过去让 ComfyUI 报"缺节点类"（那是更难诊断的失败）。
        if not _capable(n, wf):
            raise ValueError("节点「%s」不具备「%s」能力（该节点能力标签：%s）—— 请改选支持它的节点"
                             % (n.get("name") or n.get("id"), wf,
                                "、".join(_capability_of(n)) or "（未声明=全能力）"))
        url = _resolved_url(n)
        if not probe(url).get("online"):
            # ★ 2026-10-01 实测修复：探活抖动防护。
            #   4s 超时 + 10s 结果缓存在**经 tailnet 的远端节点**上会误判 —— 冷连接/对方 GPU 忙时
            #   一次 GET /queue 就可能超过 4s，于是"节点明明在线"却被判离线，任务**直接 fail**
            #   （实测：同一节点连续两次"不可达"失败，几分钟后同一 payload 成功）。
            #   故离线判定前**免缓存重探一次、超时加倍**；仍不通才判不可达。
            if not probe(url, timeout=8.0, use_cache=False).get("online"):
                raise ValueError("生成节点 '%s'（%s）不可达" % (n.get("name"), url))
        if _used(n) >= _slots_of(n):
            return None                             # ★ 步2b：忙 → 等待（不是错误）
        return n.get("id"), url

    # ── auto：C1 负载最小优先 + C3 双信号取 max（+ 步2b 空槽过滤）──
    # ★★ A-P1-1：**先按能力过滤**，再走负载选择 —— 否则 qwen21 会被派给只有 H3 的 24（必然失败）。
    cand = [n for n in nodes if _capable(n, wf)]
    if wf and not cand:
        raise ValueError(
            "节点池内没有具备「%s」能力的节点（各节点能力：%s）—— 请检查节点能力配置或新增节点"
            % (wf, "；".join("%s=[%s]" % (n.get("name") or n.get("id"),
                                        "、".join(_capability_of(n)) or "全能力") for n in nodes)))
    scored = []
    for n in cand:
        url = _resolved_url(n)
        h = probe(url)
        if not h.get("online"):
            scored.append((n, url, False, None))
            continue
        if _used(n) >= _slots_of(n):                # ★ 步2b：满槽 → 本轮不入选
            scored.append((n, url, True, None))
            continue
        scored.append((n, url, True, max(_used(n), int(h.get("load") or 0))))
    avail = [(n, url, load) for n, url, on, load in scored if load is not None]
    if avail:
        avail.sort(key=lambda x: (x[2], ))          # 稳定排序：平局保持注册顺序
        return avail[0][0].get("id"), avail[0][1]
    if any(on for _, _, on, _ in scored):
        return None                                 # ★ 步2b：都在线但全满 → 等待（不超槽）
    n = cand[0]                                     # 全挂 → C4 回退首个 enabled（★ A-P1-1：在**能力匹配**的候选里回退）
    return n.get("id"), _resolved_url(n)


def any_reachable() -> bool:
    """池内是否存在任一可达节点（供提交前 preflight 用，auto 语义下不再绑死本机）。"""
    for n in _load().get("nodes", []):
        if n.get("enabled", True) and probe(_resolved_url(n)).get("online"):
            return True
    return False
