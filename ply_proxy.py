"""gsplat ply 跨节点产物代理（★ ③ F5b 步2c · 任务单 20260923）。

病根：`GET /api/gsplat/ply/<file>` 的转发目标原写死 `get_comfyui_url()`（单节点时代口径）——
gsplat 任务跑在 **B 节点**时，ply 落在 **B 的 ComfyUI output**，代理却固定去 **A 回源 = 404**。

设计（任务单 §二 五要点全覆盖）：
  · **回源地址按 `node` 参数解析**（走 node_pool 注册表）：
      - 缺省 / `"local"` → `settings.comfyui_url`（= **旧行为完全不变**，含缓存路径 `data/gsplat_ply/`）；
      - 在册节点 id → 该节点 resolved_url（热生效）；
      - **未知 id → ValueError（端点 400，fail-closed —— 代理绝不做 SSRF 跳板，§二-5）**。
  · **缓存按节点分目录**：`data/gsplat_ply/<node_id>/`（§二-3）—— A/B 两节点可能有**同名 ply**
    （如 `output_00001_.ply`），平铺会互相覆盖；不带 node 参数仍落**旧路径**（判据 3：local-only 零回归）。
  · **节点不可达 → 502 带节点信息**（§二-4）—— 与"文件不存在"（404 带节点名）明确区分，不静默。
  · `basename` 防目录穿越 / 仅 `.ply` / CORS `*` / `Cache-Control: max-age=3600` —— 全部保留。
  · **`gsplat_ply_url`**：给 `_gsplat_job` 用的**纯函数** url 构造（可单测）：local/空 → 不带参数
    （旧行为）；注册节点 → `?node=<id>` —— 前端直接用结果 url 即可（§二-1）。

流式约定（承原实现）：read 65536 chunk **勿整读内存**（ply 60-70MB）；边转边写临时文件、
拉完原子 rename 落盘，半截文件不留。
"""
from __future__ import annotations

import os as _os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import library

BASE_DIR = Path(__file__).resolve().parent
CACHE_ROOT = BASE_DIR / "data" / "gsplat_ply"

_HEADERS = {"Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600"}


# ── 回源地址解析（§二-2 / §二-5）──────────────────────────────────────────────
def resolve_origin(node: str = "") -> str:
    """`node` 参数 → ComfyUI base url。

    - 缺省 / `"local"` → `settings.comfyui_url`（动态读，旧行为）；
    - 在册节点 id → 该节点 resolved_url（每次读注册表 = 热生效，与 C5 一致）；
    - 未知 → **ValueError**（fail-closed：不接受的 URL 参数一律拒绝，代理不做 SSRF 跳板）。
    """
    tn = (node or "").strip()
    if tn in ("", "local"):
        return library.get_comfyui_url()
    import node_pool
    for n in node_pool.list_nodes():
        if n.get("id") == tn:
            u = (n.get("resolved_url") or "").strip()
            if not u:
                raise ValueError("节点 '%s' 未配置地址（设置页生成节点区可查看/编辑）" % tn)
            return u
    raise ValueError("节点 '%s' 不在节点池中（设置页生成节点区可查在册节点）—— 拒绝代理到未登记地址" % tn)


# ── 缓存路径（§二-3：按节点分目录防同名互覆）──────────────────────────────────
def cache_path(node: str = "", fname: str = "") -> Path:
    """本地缓存路径。缺省/`local` → `data/gsplat_ply/<fname>`（**旧路径，零回归**）；
    注册节点 → `data/gsplat_ply/<node_id>/<fname>`（A/B 同名 ply 不互相覆盖）。"""
    tn = (node or "").strip()
    fn = _os.path.basename(fname or "")
    if tn in ("", "local"):
        return CACHE_ROOT / fn
    return CACHE_ROOT / tn / fn


# ── url 构造（§二-1：给 _gsplat_job 用；纯函数可单测）────────────────────────
def gsplat_ply_url(ply_name: str, node_id: str = "") -> str:
    """结果 url：local/空 → 不带参数（**旧行为**）；注册节点 → `?node=<id>`（前端直接用）。"""
    nid = (node_id or "").strip()
    url = "/api/gsplat/ply/" + (ply_name or "")
    if nid and nid != "local":
        url += "?node=" + nid
    return url


# ── 代理主逻辑（原 main.py 端点实现整体下沉，行为承原 + 节点维度）─────────────
def serve(file: str, node: str = "") -> StreamingResponse:
    """流式代理 ply。`node` 语义见 `resolve_origin`；错误分三级：
    400（参数不合法：非 .ply / 不在册节点）｜ 502（在册节点不可达，带节点信息）｜
    404（节点可达但该文件不存在，带节点名 —— 与"节点不可达"明确区分）。"""
    fname = _os.path.basename(file or "")          # 防目录穿越（复刻 gsplat-preview server.py）
    if not fname.lower().endswith(".ply"):
        raise HTTPException(status_code=400, detail="仅支持 .ply")
    node_disp = (node or "").strip() or "本机"
    try:
        origin = resolve_origin(node)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    local = cache_path(node, fname)
    local.parent.mkdir(parents=True, exist_ok=True)

    # ① 命中本地缓存 → 直接流式发本机副本（不再回源远程，秒开）
    if local.exists() and local.stat().st_size > 0:
        def _local_chunks():
            with open(local, "rb") as f:
                while True:
                    c = f.read(65536)
                    if not c:
                        break
                    yield c
        return StreamingResponse(_local_chunks(), media_type="application/octet-stream",
                                 headers={**_HEADERS,
                                          "Content-Disposition": f'attachment; filename="{fname}"'})

    # ② 未命中 → 流式转发远程（按节点解析的 origin），边转边写临时文件，完成后原子 rename 落盘
    import requests as _req
    try:
        r = _req.get(f"{origin}/view",
                     params={"filename": fname, "type": "output", "subfolder": ""},
                     stream=True, timeout=30)
    except _req.RequestException as e:
        # ★ §二-4：明确报错带节点信息，别静默 404 让前端以为"文件不存在"
        raise HTTPException(status_code=502,
                            detail=f"生成节点「{node_disp}」（{origin}）不可达：{e}")
    if r.status_code != 200:
        code = r.status_code
        r.close()
        if code in (502, 503, 504):
            # ★ 步2c 修：经代理回源时，**目标端口没监听表现为上游网关 502**（直连则是
            #   ConnectionError → 同样 502）。统一归为"节点不可达"（带节点信息），**不冒充 404**
            #   让前端误判"文件不存在"（§二-4）。
            raise HTTPException(status_code=502,
                                detail=f"生成节点「{node_disp}」（{origin}）不可达（上游网关 {code}）")
        raise HTTPException(status_code=404,
                            detail=f"ply 不存在/拉取失败({code})@节点「{node_disp}」（{origin}）："
                                   f"{fname}（SHARP 尚未生成？或 ply 在另一台节点上）")
    tmp = local.with_suffix(local.suffix + ".tmp")

    def _chunks():
        tmpf = None
        try:
            tmpf = open(tmp, "wb")
            for c in r.iter_content(chunk_size=65536):
                if c:
                    tmpf.write(c)
                    yield c                # 客户端边收边等；拉完（生成器耗尽）才走到下面
            tmpf.flush()
        except Exception:
            try:
                if tmpf: tmpf.close()
            except Exception: pass
            try: _os.remove(tmp)
            except Exception: pass
            raise
        finally:
            r.close()
            try:
                if tmpf: tmpf.close()
            except Exception: pass
        # ★ 步2c 修：**先关句柄再 rename** —— 原实现（承旧 main.py）在 tmp 句柄开着时
        #   `os.replace`，Windows 上必抛 PermissionError 且被吞 → **"边转边落盘、下次命中"
        #   从未真正生效过**（实测 data/gsplat_ply/ 只留 .tmp 残留；客户端因已收完字节仍见 200）。
        try:
            _os.replace(tmp, local)        # 完整拉完且句柄已关才落盘，避免半截文件
        except Exception:
            try: _os.remove(tmp)
            except Exception: pass
    return StreamingResponse(_chunks(), media_type="application/octet-stream",
                             headers={**_HEADERS,
                                      "Content-Disposition": f'attachment; filename="{fname}"'})
