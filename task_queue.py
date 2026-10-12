"""task_queue.py —— F5a 串行生成队列（批次②·步1）。

口径（计划书 §5.1 ② 行，用户 2026-09-21 定稿）：
- 队列真相 = task_store（data/tasks.json，9 字段契约）；本模块只做调度与执行。
- queued 取消 = 从队列移除（delete_task，任务永不进入生成）；
- running 取消 = **打持久化标记 `cancel_requested`** + ComfyUI 侧精准取消（`comfy_cancel_prompt`），
  由 job 在轮询中自检（`is_cancelled` 认标记）后收尾移除（`_finalize_cancelled`，内含「切换留一拍」）；
  ★ ② 步4 修 A9：此前只发 interrupt、不落标记，而 `is_cancelled` 又只认「记录已删除」→ 取消对 running 恒不生效；
  ★ ① 步4-①：ComfyUI 侧取消**全部收口**到 `comfy_cancel_prompt`（delete 优先 / 只打断自己的 / /history 校验），
    消除原先 4 处裸 `/interrupt` 的误杀风险；
- 被取消的任务不落库（job 内自检，命中即抛）；
- 拖动调序 = task_store.reorder；重启恢复 = running 孤儿 → fail（可重试，任务不丢）。
"""

from __future__ import annotations

import json
import threading
import time
import traceback

import node_pool
import task_store


class QueueSubmitError(ValueError):
    """入队参数不被接受（④：target_node 指向尚不存在的节点等）——端点层转 400。"""


class QueueCancelled(RuntimeError):
    """任务被用户取消（running 态 interrupt 命中或队列移除竞态）——job 捕获后静默收尾。"""


_JOBS: dict[str, callable] = {}          # kind -> fn(task_id, task_dict) -> result(dict)
_WAKE = threading.Event()
_LOCK = threading.RLock()
_STARTED = False
# ★ 派单 001 · T3（2026-10-11，架构-3）：调度器**心跳 + 异常兜底**状态
_LOOP_THREAD: "threading.Thread | None" = None
_LOOP_TICK: dict = {"t": 0.0, "err_streak": 0, "last_err": ""}
_LOOP_STALE_SEC = 15.0        # 心跳过期阈值（正常每 ≤1s 刷新；超此值 = 「线程活着但卡住」）
_LOOP_ERR_THROTTLE = 1.0      # 异常后限速基数（秒）：按连击次数线性放大，上限 30s（防异常风暴）


def register_job(kind: str, fn) -> None:
    """域侧注册执行体：fn(task_id, task) -> result；落库由 fn 自理（取消不落库由 fn 守卫）。"""
    _JOBS[kind] = fn


def wake() -> None:
    """唤醒调度循环（队列内容变化后调用，免等 2s 轮询间隔）。"""
    _WAKE.set()


# ---------- 任务提交（三域统一入口） ----------

def resolve_node_url(target_node: str = "") -> str:
    """★ ③ F5b 步1：生成节点 → ComfyUI 地址的**注册表解析口**（承 ④ 步4-④）。

    - `""` / `"local"` → 本机（settings.comfyui_url，动态读）；
    - 注册节点 id → 该节点 url（**热生效**：每次读 data/nodes.json）；
    - 未知 → 抛 `QueueSubmitError`（**fail-closed，绝不静默回落本机** —— 否则用户以为在 B 机
      生成、实际挤在 A 机，正是 ① 要防的误杀类问题）。
    ★ job 一律用 `node_url_for(task_id, target_node)`（优先取**派发时选定**的 meta.node_url）；
      本函数保留给「提交前校验」与「无派发上下文的兼容兜底」。
    """
    tn = (target_node or "").strip()
    if tn in ("", "local"):
        return node_pool.local_url()
    for n in node_pool.list_nodes():
        if n.get("id") == tn:
            return n.get("resolved_url") or ""
    raise QueueSubmitError("生成节点 '%s' 不在节点池中（队列页节点区可查在册节点）" % tn)


def node_url_for(task_id: str, target_node: str = "") -> str:
    """★ ③ F5b 步1：**job 取生成地址的唯一入口** —— 优先读派发时选定的 meta.node_url
    （排队中不绑定节点、执行时才选 —— 方案 §C-C10 的前提），无派发上下文时按 target_node 解析兜底。"""
    t = task_store.get_task(task_id)
    if t:
        m = _meta_of(t)
        if m.get("node_url"):
            return m["node_url"]
    return resolve_node_url(target_node)


def submit(kind: str, title: str, payload: dict, thumb: str = "") -> str:
    """入队一个生成任务，返回 task_id。payload 原样存 meta（json），供 job 取参与重试。

    ★ ④：target_node 在此**前置校验**（fail-fast，不建注定失败的任务）。
      ★ ③ F5b 步1：pinned（"local"/节点 id）查注册表在册性（不探测——探测留到派发时，
        避免提交被探活拖慢）；auto（""）恒合法（派发时在健康池里选，C10：排队中不绑定）。
    """
    _tn = (payload or {}).get("target_node", "")
    if not node_pool.has_node(_tn):
        raise QueueSubmitError("生成节点 '%s' 不在节点池中（队列页节点区可查在册节点）" % _tn)
    t = task_store.new_task(kind, title, meta=json.dumps({"payload": payload, "progress": "排队中…"},
                                                         ensure_ascii=False), thumb=thumb)
    task_store.save_task(t)
    _WAKE.set()
    return t["id"]


def submit_retry(kind: str, title: str, meta_json: str, thumb: str = "") -> str:
    """重试 = 同参重新入队（fail 原记录保留审计，新任务排到队尾）。"""
    t = task_store.new_task(kind, title, meta=meta_json, thumb=thumb)
    task_store.save_task(t)
    _WAKE.set()
    return t["id"]


# ---------- 取消 / 查询 ----------

def is_cancelled(task_id: str) -> bool:
    """job 执行中自检：任务**已被移除**（调序移出等）**或已收到取消请求**，即视为取消。

    ★ ② 步4 修 A9（死锁）：原实现只认「记录已删除」，而删除只在 job 抛出 QueueCancelled 之后
      才发生 —— job 要靠「记录已删除」才抛，删除又靠 job 抛出 → 循环依赖，
      **running 任务点了取消毫无反应**（旧 _MOTION_TASKS 里的 cancel_requested 在重构时丢了）。
    """
    t = task_store.get_task(task_id)
    if t is None:
        return True
    return bool(_meta_of(t).get("cancel_requested"))


def cancel(task_id: str) -> dict:
    """取消：queued = 直接移除（永不进入生成）；running = interrupt 后由 job 自检收尾移除。"""
    t = task_store.get_task(task_id)
    if not t:
        return {"ok": True, "status": "gone"}
    state = t.get("state")
    if state == "queued":
        # ★ ③ 步4-③ 取消留痕：不再物理删除 —— 置 cancelled 保留记录（任务卡详情/历史要用）
        _mark_cancelled(task_id, "已取消（未执行）")
        _WAKE.set()
        return {"ok": True, "status": "cancelled"}
    if state == "running":
        meta = _meta_of(t)
        # ★ ② 步4 修 A9：先落「已请求取消」的持久化标记 —— job 唯一的自检依据（此前缺这一步 → 死锁）
        set_meta(task_id, {"cancel_requested": True, "progress": "取消中…"})
        # ★ ① 步4-①：ComfyUI 侧精准取消（delete 优先；只有"在跑的是自己"才 interrupt，并做 /history 校验）
        # ★ ④：取消打在**任务自己的节点**上（不是写死的本机）
        pid = meta.get("prompt_id")
        if pid:
            # ★ ③ F5b 步1：取消打在**任务实际执行的节点**（派发时选定的 meta.node_url），
            #   而非 payload.target_node —— auto 任务的 payload 是空，派发地≠提交假设
            _cbase = meta.get("node_url") or resolve_node_url(
                (_meta_of(t).get("payload") or {}).get("target_node", ""))
            res = comfy_cancel_prompt(pid, base=_cbase)
            set_meta(task_id, {"cancel_comfy": res})
            print("[task_queue] 取消 %s：prompt=%s → %s" % (task_id, pid, res))
        # 不在请求处 delete：由 job 自检收尾（_finalize_cancelled），保证写入顺序一致
        return {"ok": True, "status": "cancelling"}
    return {"ok": True, "status": state}


def position_of(task_id: str):
    """排队位次（1 起；仅 queued 有效，其余返回 None）——队列页与状态行共用。"""
    q = [t for t in task_store.list_tasks() if t.get("state") == "queued"]
    q.sort(key=lambda x: (x.get("order") or 0, x.get("created_at") or ""))
    for i, t in enumerate(q):
        if t["id"] == task_id:
            return i + 1
    return None


# ══════════════ ① 步4-①：ComfyUI 侧取消（interrupt 误杀加固，2026-09-21）══════════════
#
# 真机限制（架构-2 确认）：ComfyUI 的 `POST /queue {delete}` **只能删 pending**，
# running 的"只取消指定"真机做不到 → 取消 running 的**唯一手段**是 `/interrupt`，
# 而 `/interrupt` 打断的是**当前正在执行的那个** —— 若此刻在跑的不是自己，就是**误杀**。
# 故三步加固（全部收口到本模块这一个例程，此前有 4 处裸 interrupt：task_queue/edit/enhance/gen）：
#   ① **delete 优先**（精确、无副作用）→ 只有确认"在跑的是自己"才 interrupt；
#   ② interrupt 后用 `/history/{pid}` **校验**（区分"真被打断"与"其实早就完成了"）；
#   ③ **切换留一拍**：等本任务的 prompt 从 ComfyUI 队列消失（有上限）再放行下一个任务，
#      否则下个 prompt 可能被"迟到的打断标记"误杀（见 `_wait_comfy_settle`）。
# 误杀**只留痕 + 日志**（观察项），不自动重试 —— 重试策略归 A4（步4 尾项），避免重复设计。

COMFY_TIMEOUT = 10                # 单次 ComfyUI 请求超时（秒）
SETTLE_TIMEOUT = 12.0             # "留一拍"上限：等本任务 prompt 离开队列
SETTLE_MIN = 1.0                  # 最少静默时长（保底一拍）
AUTO_RETRY_LIMIT = 1              # ★ ⑤ A4（用户 2026-09-21 21:45 批准）：失败自动重试上限（仍失败转手动）
GEN_SERIAL = threading.Lock()    # ★ 保留：**全局兜底锁**（`gen_serial()` 拿不到节点 key 时回退用它）

# ★★ ③ 步2b附 裁定①：视频类（video/motion）串行保险从**全局锁**改为**按节点锁**。
#    依据：**双机单部署 = 显存物理隔离** ⇒ 跨节点 video/motion 并行**不引入 GPU 冲突**；
#    而全局锁会把步2b 的收益直接抵消（video/motion 恰恰是主力任务类型）。
_NODE_LOCKS: dict = {}
_NODE_LOCKS_GUARD = threading.Lock()


def gen_serial(node_key: str = ""):
    """★ 步2b附 裁定①：取「视频类串行保险」的锁（**按节点**）。

    - **key = 实际派发节点** —— 调用方传 `node_url_for(task_id, target_node)` 的结果
      （与 `meta.node_url` **同源**）。★ **别传 `target_node` 字符串**：pinned/auto 下两者不同
      （auto 的 `payload.target_node` 是空串，拿它当 key 会把所有 auto 视频任务挤到同一把锁上）。
    - **同节点内** video/motion 互斥（防同一台机器的 GPU / HostBuffer 冲突 —— Y2 结论保持不变）；
      **跨节点**可并行（显存物理隔离）。
    - key 为空 → 回退 `GEN_SERIAL`（fail-safe：拿不到节点时**宁可串行**，不冒险并行）。
    """
    k = (node_key or "").strip().rstrip("/")
    if not k:
        return GEN_SERIAL
    with _NODE_LOCKS_GUARD:
        lk = _NODE_LOCKS.get(k)
        if lk is None:
            lk = threading.Lock()
            _NODE_LOCKS[k] = lk
        return lk    # ★ ②步2遗留：视频类 job（video/motion）串行保险 —— 统一队列本就
                                 #   RUNNING≤1，此锁防未来并发闸放宽后视频类重叠（Y2 结论随迁），
                                 #   防 HostBuffer 1450 并发冲突
MISFIRE_LOG: list = []            # 误杀观测（最近 20 条；`GET /api/tasks` 之外仅供排查）


def _comfy_get(url: str):
    """GET 一个 ComfyUI JSON 端点；不可达/非 200 → None（与"合法空值"区分）。"""
    try:
        import requests
        r = requests.get(url, timeout=COMFY_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _queue_pids(raw) -> list:
    """从 /queue 的某一段取 prompt_id 列表 —— 兼容真机（item=list，pid 在下标 1）与 stub（item=dict）。"""
    out = []
    for it in (raw or []):
        if isinstance(it, dict):
            pid = it.get("prompt_id")
        elif isinstance(it, (list, tuple)) and len(it) > 1:
            pid = it[1]
        else:
            pid = None
        if pid:
            out.append(pid)
    return out


def comfy_queue_snapshot(base: str):
    """ComfyUI 队列快照 → {"running":[...], "pending":[...]}；**不可达返回 None**。"""
    j = _comfy_get(base + "/queue")
    if j is None:
        return None
    return {"running": _queue_pids(j.get("queue_running")),
            "pending": _queue_pids(j.get("queue_pending"))}


def comfy_cancel_prompt(prompt_id: str, base: str | None = None) -> dict:
    """在 ComfyUI 侧精准取消**自己**的 prompt；返回可观测的处置结果（供 cancel / 各 job 复用）。

    verdict：deleted_pending（只在排队→已删）/ interrupted（真打断）/ stopped（未打断但已不在队列）/
             already_done（打断前其实已完成）/ not_mine（在跑的是别人 → **不动**，防误杀）/ unreachable
    """
    res = {"reachable": True, "deleted": False, "interrupted": False,
           "misfire_avoided": False, "verdict": ""}
    if not prompt_id:
        res["verdict"] = "no_prompt"
        return res
    try:
        import requests
        if base is None:
            from library import get_comfyui_url
            base = get_comfyui_url()
    except Exception:
        res["reachable"] = False
        res["verdict"] = "unreachable"
        return res

    # 先看清「它在哪一段」——快照必须在 delete **之前**取，否则 pending 项已被删、判不出「精确删除」这一档
    snap0 = comfy_queue_snapshot(base)

    # ① delete 优先：只影响 pending，精确且无副作用（真机对 running 无效，故不依赖它）
    try:
        requests.post(base + "/queue", json={"delete": [prompt_id]}, timeout=COMFY_TIMEOUT)
        res["deleted"] = True
    except Exception:
        res["reachable"] = False
        res["verdict"] = "unreachable"
        return res

    if snap0 is not None:
        if prompt_id in snap0["pending"] and prompt_id not in snap0["running"]:
            res["verdict"] = "deleted_pending"          # 只在排队 → 已精确删除，**不打断任何人**
            return res
        if prompt_id in snap0["running"]:
            _do_interrupt(requests, base, res)
            _verify_cancelled(base, prompt_id, res)
            return res
        if snap0["running"]:
            # 自己不在队列、在跑的是别人 → **不打断**（宁可本任务自然收尾，也不误杀）
            res["misfire_avoided"] = True
            res["verdict"] = "not_mine"
            _note_misfire(prompt_id, snap0["running"])
            return res
    # 队列读不到 / 队列空：用 /history 判是否已完成，未完成才打断（空队列时 interrupt 是 no-op）
    h = _comfy_get(f"{base}/history/{prompt_id}")
    if h and h.get(prompt_id):
        res["verdict"] = "already_done"
        return res
    _do_interrupt(requests, base, res)
    _verify_cancelled(base, prompt_id, res)
    return res


def _do_interrupt(requests, base: str, res: dict) -> None:
    try:
        requests.post(base + "/interrupt", timeout=COMFY_TIMEOUT)
        res["interrupted"] = True
    except Exception:
        pass                                             # 不可达时靠 job 自检兜底


def _verify_cancelled(base: str, prompt_id: str, res: dict, tries: int = 4) -> None:
    """② interrupt 后校验：/history 出现记录 = **打断前其实已完成**（可能已误杀别人）；否则确认已停。"""
    for _ in range(tries):
        h = _comfy_get(f"{base}/history/{prompt_id}")
        if h and h.get(prompt_id):
            res["verdict"] = "already_done"
            if res.get("interrupted"):                   # 已完成却打了一枪 → 那一枪可能打在别人身上
                snap = comfy_queue_snapshot(base) or {}
                if snap.get("running"):
                    _note_misfire(prompt_id, snap["running"])
            return
        snap = comfy_queue_snapshot(base)
        if snap is not None and prompt_id not in snap["running"] and prompt_id not in snap["pending"]:
            res["verdict"] = "interrupted" if res["interrupted"] else "stopped"
            return
        time.sleep(0.5)
    res["verdict"] = res["verdict"] or ("interrupted" if res["interrupted"] else "unknown")


def _note_misfire(prompt_id: str, others: list) -> None:
    """误杀观测（观察项：只留痕 + 日志，**不自动重试**）。"""
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "our_prompt": prompt_id, "others": list(others)}
    MISFIRE_LOG.append(rec)
    del MISFIRE_LOG[:-20]
    print("[task_queue] ★ 误杀规避/疑似：在跑的是别人的任务 %s，跳过或已晚于 interrupt（本任务 %s 仅本地取消）"
          % (others, prompt_id))


def _wait_comfy_settle(prompt_id: str = "", base_url: str = "") -> None:
    """③ 切换留一拍：等**本任务的 prompt** 从 ComfyUI 队列消失（上限 SETTLE_TIMEOUT），再补 SETTLE_MIN。

    只等自己的 prompt（不等别人跑完）→ 取消自己时不会被别人的长任务拖住。ComfyUI 不可达则跳过。

    ★ ⑦ A-P2-2（2026-10-02）：`base_url` = **该任务实际派发的节点**（`meta.node_url`）。
      原实现硬用 `library.get_comfyui_url()`（= 本机地址）⇒ **多节点下等错节点**：
      任务跑在 18，却去 24 的队列里找 prompt_id（永远找不到 → 这一拍被静默跳过；
      反之若 24 队列里恰好有同名 pending，还会误判）。取不到时回退旧行为（向后兼容）。
    """
    base = (base_url or "").strip()
    if not base:
        try:
            from library import get_comfyui_url
            base = get_comfyui_url()
        except Exception:
            return
    if not base:
        return
    t0 = time.time()
    while time.time() - t0 < SETTLE_TIMEOUT:
        snap = comfy_queue_snapshot(base)
        if snap is None:
            break                                        # 读不到 → 不空等
        if prompt_id:
            if prompt_id not in snap["running"] and prompt_id not in snap["pending"]:
                break
        elif not snap["running"]:
            break
        time.sleep(0.5)
    time.sleep(SETTLE_MIN)


def _meta_of(t: dict) -> dict:
    try:
        return json.loads(t.get("meta") or "{}")
    except (ValueError, TypeError):
        return {}


def set_meta(task_id: str, patch: dict) -> None:
    """job 执行中回写 meta（progress 文案 / prompt_id / result 摘要）。

    ★ ③ 步2b：改为调 `task_store.patch_meta`（**同一把锁内读-改-写**）。
      原实现是「get_task → update → save_task」三步、跨两次加锁 → 并发下会**丢失刚写入的键**：
      job 线程读到"取消之前"的 meta 快照，写回时把 `cancel_requested` 抹掉
      → 用户点了取消、任务照跑完变 done（步2b P4 用例实测抓到）。
    """
    task_store.patch_meta(task_id, patch)


# ---------- 调度循环 ----------

def _recover_orphans() -> None:
    """重启恢复：上次运行遗留的 running 孤儿 → fail（任务不丢，可重试）。"""
    for t in task_store.list_tasks():
        if t.get("state") == "running":
            t["state"] = "fail"
            t["meta"] = json.dumps({**_meta_of(t), "progress": "服务重启中断",
                                    "error": "服务重启中断，可重试"}, ensure_ascii=False)
            task_store.save_task(t)


def _mark_cancelled(task_id: str, progress: str = "已取消") -> None:
    """③ 步4-③：取消**留痕**的唯一落点（queued 取消 与 running 收尾共用）。

    - `state` 置 `cancelled`（已在 `task_store.STATES` 登记），**记录保留**供"任务卡详情/历史"查看；
    - meta 同时落 `cancel_requested=True`（保持 `is_cancelled()` 语义）与 `cancelled_at`；
    - ★ 与"移除"的差别**只在于记录是否保留**：产物依旧**不落库**（各 job 的守卫不变）；
    - 一次写入（原子），避免"状态已变但原因未落"的可见中间态。
    """
    m = {"cancel_requested": True, "progress": progress,
         "cancelled_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    # ★ 原子留痕（与调度侧的 claim_queued 共用同一把锁）——防止"取消后被覆盖回 running"
    task_store.mark_cancelled(task_id, m)


def _settle_after_cancel(task_id: str) -> None:
    """★ 步2b附 裁定③：**原子终态写判定为「取消优先」**后的收尾。

    与 `_finalize_cancelled` 的分工：留痕（state=cancelled + cancel_requested + cancelled_at）
    已由 `task_store.finalize(cancel_wins=True)` 在**同一把锁内**完成，这里**只需"切换留一拍"**
    （等本任务 prompt 离开 ComfyUI，防"迟到的打断"误杀下一个任务 —— 承 ① 步4-①）。
    """
    t = task_store.get_task(task_id)
    _m = _meta_of(t) if t else {}
    pid = (_m.get("prompt_id") if t else "") or ""
    _wait_comfy_settle(pid, _m.get("node_url") or "")          # ★ A-P2-2：等**该任务实际节点**的队列


def _finalize_cancelled(task_id: str) -> None:
    """取消收尾（**唯一出口**）：③ 步4-③ 起改为**留痕**（state=cancelled，记录保留）。
    口径若再调整（例如加"清理已取消"）仍只改这一处。

    ★ ① 步4-①「切换留一拍」：收尾后不立刻回到调度 —— 先等本任务的 prompt 从 ComfyUI 队列消失
      （`_wait_comfy_settle`，有上限、只等自己的、不可达即跳过），
      否则下个任务可能与"迟到的打断"抢跑而被误杀。
    """
    t = task_store.get_task(task_id)
    _m = _meta_of(t) if t else {}
    pid = (_m.get("prompt_id") if t else "") or ""
    _mark_cancelled(task_id, "已取消")
    _wait_comfy_settle(pid, _m.get("node_url") or "")          # ★ A-P2-2：等**该任务实际节点**的队列


def _queued_all() -> list:
    """★ 步2b：按 `order` 升序的**全部**排队任务（多槽调度器每轮遍历用）。

    排序口径与原 `_next_queued` 完全一致（order 优先、created_at 兜底）—— 抽出来只为让
    "取队列"只有一个排序实现，避免两处漂移。
    """
    ts = [t for t in task_store.list_tasks() if t.get("state") == "queued"]
    ts.sort(key=lambda x: (x.get("order") or 0, x.get("created_at") or ""))
    return ts


def _next_queued() -> dict | None:
    ts = _queued_all()
    return ts[0] if ts else None


# ── ★ 步2b：节点执行槽（in-flight）计数 ──────────────────────────────────────
# 语义：`slots` =「该节点同一时刻最多几个 RUNNING」；默认 1 = 与旧的全局串行等价。
# 为什么还需要调度器自己的计数（而不只读任务表）：
#   `claim_queued` 把任务置 running 之后，`meta.node` 是**随后**由 `_run_task` 写入的 ——
#   这中间有个窗口期。若调度器只认 `meta.node`，会在窗口期内认为"该节点还空着"而重复派发
#   ⇒ **同节点并行**（正是 Y2 GPU 冲突保险要防的事）。故用一个**立即生效**的在途计数兜住。

_INFLIGHT: dict = {}                 # node_id -> 在途任务数（claim 成功即 +1，任务收尾 -1）
_INFLIGHT_LOCK = threading.Lock()


def _inflight_add(node_id: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT[node_id] = _INFLIGHT.get(node_id, 0) + 1


def _inflight_del(node_id: str) -> None:
    with _INFLIGHT_LOCK:
        n = _INFLIGHT.get(node_id, 0) - 1
        if n > 0:
            _INFLIGHT[node_id] = n
        else:
            _INFLIGHT.pop(node_id, None)


def _inflight_snapshot() -> dict:
    with _INFLIGHT_LOCK:
        return dict(_INFLIGHT)


def _run_task(t: dict, node_id: str, node_url: str) -> None:
    """★ 步2b：**单任务执行体**（在独立线程里跑）—— 原 `_loop` 里「claim 之后」的全部逻辑。

    进入本函数时：任务已是 running、节点已由调度器选定（`pick_node` 已确认有空槽）、
    在途计数已 +1。本函数负责：写 meta（含 node/node_url/started_at）→ 跑 job → 收尾
    （完成 / 取消留痕 / 失败 + A4 自动重试）；**无论如何退出，finally 都释放槽并唤醒调度器**。
    """
    tid = t["id"]
    _t0 = time.time()
    try:
        kind = t.get("kind")
        fn = _JOBS.get(kind)
        if not fn:                                   # 调度器已前置检查；此处仅兜底
            t["state"] = "fail"
            task_store.save_task(t)
            set_meta(tid, {"progress": "无 %s 执行体" % kind, "error": "no job for %s" % kind})
            return
        # ★ ② 步4-② 超时兜底：记 started_at，失败时配 elapsed —— 超时类故障一眼可判
        set_meta(tid, {"progress": "开始执行…", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "node": node_id, "node_url": node_url})
        try:
            result = fn(tid, t) or {}
            # ★ ③ 步2b附 裁定③：终态「**判定 + 写入**」在同一把锁内原子完成 ——
            #   这样"恰在完成那一瞬点取消"也**绝不丢取消痕迹**（原先是三步读写，会被覆盖成 done）。
            _fin, _was_cancelled = task_store.finalize(
                tid, "done",
                {"progress": "完成", "result": result, "elapsed": round(time.time() - _t0, 1)},
                pct=100, cancel_wins=True)
            if _was_cancelled:
                _settle_after_cancel(tid)            # 取消优先：产物丢弃 + 切换留一拍
            return
        except QueueCancelled:
            _finalize_cancelled(tid)                 # 取消 = 收尾（走唯一出口）
        except Exception as e:
            if is_cancelled(tid):
                _finalize_cancelled(tid)             # interrupt 打断引发的报错 → 归类为取消，而非失败
                return
            tail = "".join(traceback.format_exception_only(type(e), e)).strip()[-300:]
            t = task_store.get_task(tid) or t
            _m = _meta_of(t)
            _cnt = int(_m.get("auto_retry_count") or 0)
            # ★ ③ 步2b附 裁定③：**先**原子落终态（取消优先）——
            #   取消命中则**不重试**（用户已取消的任务不该被自动重试复活）。
            _fin, _was_cancelled = task_store.finalize(
                tid, "fail",
                {"progress": "失败", "elapsed": round(time.time() - _t0, 1),
                 "error": "%s | at %s" % (e, tail)},
                cancel_wins=True)
            if _was_cancelled:
                _settle_after_cancel(tid)
                return
            # ★ ⑤ A4：失败**自动重试 1 次**（带上限；仍失败转手动）。新任务 = 同参重新入队，
            #    只继承 payload（不继承失败现场）；原记录 `auto_retry_of` 指向新任务。
            #    注：此处"回写审计字段"是**终态落定之后**的补充写，不影响「取消留痕」口径。
            if _cnt < AUTO_RETRY_LIMIT:
                _m2 = {"payload": _m.get("payload") or {},
                       "progress": "排队中…（自动重试 %d/%d）" % (_cnt + 1, AUTO_RETRY_LIMIT),
                       "auto_retry_count": _cnt + 1, "auto_retry_src": tid}
                t2 = task_store.new_task(t["kind"], t["title"],
                                         meta=json.dumps(_m2, ensure_ascii=False),
                                         thumb=t.get("thumb") or "")
                task_store.save_task(t2)
                task_store.patch_meta(tid, {"auto_retried": True, "auto_retry_count": _cnt + 1,
                                            "auto_retry_of": t2["id"]})
                print("[task_queue] A4 自动重试：%s 失败 → 已自动重试为 %s（第 %d/%d 次）"
                      % (tid, t2["id"], _cnt + 1, AUTO_RETRY_LIMIT))
    except Exception as _fatal:
        # ★★ 步2b 加固：**执行体里任何未预期异常都不得让任务卡死在 running**。
        #    教训：原 `_loop` 是"一整块 try"包住全部逻辑；拆成 `_run_task` 后，
        #    `set_meta(...)` 落在了「外层 try」与「内层 try」之间 —— 它一抛异常就无人接住，
        #    线程静默崩溃、任务永久停在 running（实测：`patch_meta` 少 import json 时 6 个任务全卡死）。
        #    ⇒ 任何"重构执行体"都必须给最外层留兜底。
        try:
            _tt = task_store.get_task(tid)
            if _tt and _tt.get("state") == "running":
                task_store.patch_meta(tid, {"progress": "失败",
                                            "elapsed": round(time.time() - _t0, 1),
                                            "error": "调度执行体异常：%s" % _fatal})
                _tt["state"] = "fail"
                task_store.save_task(_tt)
        except Exception:
            pass
        print("[task_queue] 执行体异常 %s: %r" % (tid, _fatal))
    finally:
        _release_slot(node_id)                   # ★ 释放槽（云任务减云计数 / 本地减节点在途）
        wake()                                   # ★ 叫醒调度器立刻补位（否则要等下一轮轮询）


def _loop() -> None:
    """★ 步2b 起：**多槽调度器** —— 为每个「有空槽的节点」各取其 1 个任务，并发执行。

    与旧行为的等价性（这是本步最要紧的一条，逐条对照）：
      · **单节点 + slots=1**（默认部署）：每轮最多派 1 个，且在它跑完前该节点始终"满槽"
        ⇒ 与旧的**全局串行完全等价**（旧行为是本设计的一个特例）；
      · **双节点各 slots=1**：两台机器各自跑 1 个 ⇒ 真并行（旧行为的严格超集）；
      · **同一节点内**并发数 ≤ 其 `slots` —— slots=1 即"同节点严格串行"（Y2 GPU 冲突保险）；
      · **保序**：按 `order` 依次尝试；**队首派不出（其可选节点全满）→ 本轮即停**
        —— 不跳过队首去派后面的任务，保证 FIFO 语义不被并发打乱。
    路由失败（pinned 不在册/不可达）仍在**派发时**判定并让任务 fail（承步1；A4 会重新路由）。
    ★ ④ F5c：云端任务（channel=modelscope）走**独立车道**（`_dispatch_cloud`）——
      不占节点槽、不参与负载平衡；与本地车道**互不阻塞**（云端轻量 API，排队反增延迟），
      车道内仍严格 FIFO + 并发上限。
    ★ 派单 001 · T3（2026-10-11，架构-3）：**循环体整体兜底** —— 任何异常
      （含 `KeyboardInterrupt` / `SystemExit` 这类 `Exception` **捕不到**的）都
      **只记日志 + 限速，绝不逃逸**。否则调度线程静默死掉 ⇒ 之后所有任务永远停在
      queued（界面无任何提示的"静默停摆"）。心跳写 `_LOOP_TICK`（供 `scheduler_status()`）。
    """
    while True:
        _LOOP_TICK["t"] = time.time()            # ★ T3：心跳（每轮刷新）
        try:
            dispatched = _dispatch_cloud()           # ★ F5c：先派云车道（独立 FIFO）
            dispatched = _dispatch_local() or dispatched
            if not dispatched:
                _WAKE.wait(timeout=1.0)
                _WAKE.clear()
            _LOOP_TICK["err_streak"] = 0             # 本轮正常 → 连击计数复位
        except BaseException as e:                   # ★ T3：含 KeyboardInterrupt / SystemExit
            _LOOP_TICK["err_streak"] += 1
            _LOOP_TICK["last_err"] = "%s: %s" % (type(e).__name__, e)
            print("[task_queue] ★ 调度主循环异常（连击第 %d 次；**线程存活**，限速后继续）：\n%s"
                  % (_LOOP_TICK["err_streak"], traceback.format_exc()))
            # ★ T3：限速 —— 防异常风暴刷爆日志/CPU（连击越多等越久，上限 30s）
            time.sleep(min(_LOOP_ERR_THROTTLE * _LOOP_TICK["err_streak"], 30.0))


def scheduler_status() -> dict:
    """★ 派单 001 · T3：调度器心跳 —— 让"静默停摆"变成**可见**（供 `/api/tasks` 的 `scheduler_alive`）。

    - `alive`：线程对象存在且 `is_alive()`（线程真死了这里就是 False）；
    - `last_tick` / `tick_age_sec`：最近一次循环体开始的时间与距今秒数（空闲时每 1s 刷新）；
    - `stale`：线程活着但心跳超过 `_LOOP_STALE_SEC` 未刷新（卡在某个不返回的调用里）；
    - `err_streak` / `last_err`：连续异常次数与最近一次异常摘要（正常恒 0 / ""）。
    """
    with _LOCK:
        th = _LOOP_THREAD
        started = _STARTED
    alive = bool(th is not None and th.is_alive())
    tick = float(_LOOP_TICK.get("t") or 0.0)
    age = (time.time() - tick) if tick else None
    return {
        "started": bool(started),
        "alive": alive,
        "last_tick": tick or None,
        "tick_age_sec": (round(age, 1) if age is not None else None),
        "stale": bool(alive and age is not None and age > _LOOP_STALE_SEC),
        "err_streak": int(_LOOP_TICK.get("err_streak") or 0),
        "last_err": _LOOP_TICK.get("last_err") or "",
    }


# ── ★ ④ F5c：云车道（独立并发上限，不占节点槽）────────────────────────────
_CLOUD_ID = "云端"                             # meta.node 展示名 + 车道标识
_CLOUD_INFLIGHT = 0
_CLOUD_LOCK = threading.Lock()


def _cloud_inflight() -> int:
    with _CLOUD_LOCK:
        return _CLOUD_INFLIGHT


def _cloud_add() -> None:
    global _CLOUD_INFLIGHT
    with _CLOUD_LOCK:
        _CLOUD_INFLIGHT += 1


def _cloud_limit() -> int:
    try:
        import ms_image
        return int(ms_image.config().get("concurrency") or 2)
    except Exception:
        return 2


def _release_slot(node_id: str) -> None:
    """任务收尾释放车道槽：云任务减云计数，本地任务减节点在途计数。"""
    global _CLOUD_INFLIGHT
    if node_id == _CLOUD_ID:
        with _CLOUD_LOCK:
            _CLOUD_INFLIGHT = max(0, _CLOUD_INFLIGHT - 1)
    else:
        _inflight_del(node_id)


def _dispatch_cloud() -> bool:
    """云车道派发：channel=modelscope 的排队任务，独立 FIFO + 并发上限（settings.ms_image.concurrency）。"""
    dispatched = False
    for t in _queued_all():
        if (_meta_of(t).get("payload") or {}).get("channel") != "modelscope":
            continue
        tid = t["id"]
        kind = t.get("kind")
        if not _JOBS.get(kind):                  # 无执行体 → 标 fail（与本地车道同语义，不 claim）
            t["state"] = "fail"
            task_store.save_task(t)
            set_meta(tid, {"progress": "无 %s 执行体" % kind, "error": "no job for %s" % kind})
            continue
        if _cloud_inflight() >= _cloud_limit():
            break                                # 云车道满 → 本轮云道到此为止（车道内 FIFO）
        claimed = task_store.claim_queued(tid)
        if not claimed:
            continue                             # 已被取消/移除 → 看下一个
        _cloud_add()                             # 先占槽再起线程
        threading.Thread(target=_run_task, args=(claimed, _CLOUD_ID, ""),
                         name="task-cloud-%s" % tid[-6:], daemon=True).start()
        dispatched = True
    return dispatched


def _dispatch_local() -> bool:
    """本地车道派发（原 `_loop` 循环体原样迁移；仅跳过云端任务）。"""
    dispatched = False
    for t in _queued_all():
        if (_meta_of(t).get("payload") or {}).get("channel") == "modelscope":
            continue                             # 云任务归云车道
        tid = t["id"]
        kind = t.get("kind")
        if not _JOBS.get(kind):                  # 无执行体 → 标 fail（原语义：**不 claim**）
            t["state"] = "fail"
            task_store.save_task(t)
            set_meta(tid, {"progress": "无 %s 执行体" % kind, "error": "no job for %s" % kind})
            continue                             # 不算"派发"，让本轮继续看别的任务
        tn = (_meta_of(t).get("payload") or {}).get("target_node", "")
        _wf = (_meta_of(t).get("payload") or {}).get("workflow_type", "")
        try:
            # ★★ A-P1-1：把 workflow_type 交给调度器做**能力匹配**（视频/音频类 payload 无该键 → "" 不约束）
            sel = node_pool.pick_node(tn, _inflight_snapshot(), _wf)
        except ValueError as e:                  # pinned 不在册/不可达 → 任务 fail 带真实原因（承 ④/步1）
            claimed = task_store.claim_queued(tid)
            if claimed:
                claimed["state"] = "fail"
                task_store.save_task(claimed)
                set_meta(tid, {"progress": "失败", "elapsed": 0.0, "error": "节点路由失败：%s" % e})
            continue
        if sel is None:
            # ★ 池内可达但全部满槽（或 pinned 节点忙）→ **等待，绝不超槽**；队首保留 → 停本轮
            break
        node_id, node_url = sel
        # ★ 原子认领（步4-③）：同一把锁内读-查-写，否则"取消"与"派发"会互相覆盖
        claimed = task_store.claim_queued(tid)
        if not claimed:
            continue                             # 已被取消/移除 → 看下一个（不阻塞队列）
        _inflight_add(node_id)                   # ★ 先占槽再起线程（窗口期由它兜住）
        threading.Thread(target=_run_task, args=(claimed, node_id, node_url),
                         name="task-%s" % tid[-6:], daemon=True).start()
        dispatched = True
    return dispatched


def start() -> None:
    global _STARTED, _LOOP_THREAD
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    # ★ 派单 001 · T3：孤儿恢复失败**不得阻断调度器启动**（原实现无兜底 ⇒ 恢复抛异常则
    #   线程根本起不来，队列整个停摆，且启动日志里只有一段 traceback）。
    try:
        _recover_orphans()
    except BaseException:
        print("[task_queue] ★ 启动恢复孤儿失败（**不阻断调度器启动**）：\n%s" % traceback.format_exc())
    _LOOP_THREAD = threading.Thread(target=_loop, name="task-queue", daemon=True)
    _LOOP_THREAD.start()
    _LOOP_TICK["t"] = time.time()            # ★ T3：心跳基准（线程启动即计）
