# -*- coding: utf-8 -*-
"""routers_nodes.py —— 生成节点池 API（批次③ F5b · 步1 实例路由）

★ 步A 纪律：新域独立模块；main.py 只 include。
端点（热生效，注册表即 data/nodes.json，增删无需重启 —— 方案 §C-C5）：
  GET  /api/nodes                节点清单（含 resolved_url / online / load，探活带 10s 缓存）
  POST /api/nodes                新增 {name, url}（url 须 http(s)://；local 不可重名覆盖）
  POST /api/nodes/{id}/remove    删除（local 不可删）
  POST /api/nodes/{id}/probe     立即探活（绕缓存）
  POST /api/nodes/{id}/enabled   启停 {enabled: bool}（停用=摘出调度池但不删配置）
  POST /api/nodes/{id}/slots     设并发执行槽 {slots: int}（★ ③ 步2b；1–8，热生效）
  POST /api/nodes/{id}/url       改地址 {url: str}（★ 用户 10-01 Q2：本机地址改在这里 —— 设置页原「ComfyUI 地址」已删）
  POST /api/nodes/{id}/capability 设能力标签 {capability: [workflow_type,...]}（★ A-P1-1；空 = 全能力）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import node_pool

router = APIRouter(tags=["nodes"])


class NodeAddPayload(BaseModel):
    name: str = ""
    url: str = ""
    slots: int = 1                 # ★ ③ 步2b：并发执行槽（默认 1 = 与旧全局串行等价）
    capability: list = []          # ★ A-P1-1：能力标签（能跑哪些 workflow_type；空 = 全能力）


class NodeEnabledPayload(BaseModel):
    enabled: bool = True


class NodeSlotsPayload(BaseModel):
    slots: int = 1                 # ★ ③ 步2b：并发执行槽（1–8）


class NodeUrlPayload(BaseModel):
    url: str = ""                  # ★ 用户 10-01 Q2：改节点地址（含本机 local —— 设置页原「ComfyUI 地址」已删）


class NodeCapabilityPayload(BaseModel):
    capability: list = []          # ★ A-P1-1：能力标签（空 = 全能力）


@router.get("/api/nodes")
def api_nodes_list():
    """节点池清单（含在线状态与负载；探活带 10s 缓存）。"""
    return {"nodes": node_pool.list_nodes(with_status=True)}


@router.post("/api/nodes")
def api_nodes_add(payload: NodeAddPayload):
    """新增节点（热生效）。返回新节点（含探活结果，便于前端即时反馈可达性）。"""
    try:
        node = node_pool.add_node(payload.name, payload.url, payload.slots, payload.capability)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    node = dict(node)
    node["resolved_url"] = node["url"]
    h = node_pool.probe(node["url"], use_cache=False)
    node["online"] = bool(h.get("online"))
    node["load"] = int(h.get("load") or 0)
    return node


@router.post("/api/nodes/{node_id}/remove")
def api_nodes_remove(node_id: str):
    try:
        ok = node_pool.remove_node(node_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="节点不存在")
    return {"ok": True}


@router.post("/api/nodes/{node_id}/probe")
def api_nodes_probe(node_id: str):
    """立即探活（绕缓存）。"""
    hit = [n for n in node_pool.list_nodes() if n.get("id") == node_id]
    if not hit:
        raise HTTPException(status_code=404, detail="节点不存在")
    rurl = hit[0].get("resolved_url") or hit[0].get("url") or ""
    h = node_pool.probe(rurl or node_pool.local_url(), use_cache=False)
    return {"id": node_id, **h}


@router.post("/api/nodes/{node_id}/enabled")
def api_nodes_enabled(node_id: str, payload: NodeEnabledPayload):
    if not node_pool.set_enabled(node_id, payload.enabled):
        raise HTTPException(status_code=400, detail="节点不存在或不可停用（local 恒启用）")
    return {"ok": True}


@router.post("/api/nodes/{node_id}/url")
def api_nodes_set_url(node_id: str, payload: NodeUrlPayload):
    """改节点地址（★ 用户 10-01 Q2：本机地址也在这里改 —— 设置页的「ComfyUI 地址」已删除）。
    校验口径与新增一致（host+port 成对 / 端口范围 / 不与他人重复），违规 400 且零写入。"""
    try:
        return node_pool.set_node_url(node_id, payload.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/nodes/{node_id}/slots")
def api_nodes_slots(node_id: str, payload: NodeSlotsPayload):
    """★ ③ 步2b：设置该节点并发执行槽（热生效；local 也可改 —— 槽数不改变节点身份）。

    语义：slots = 「该节点同一时刻最多几个 RUNNING」；1（默认）= 与旧全局串行等价。
    非法槽数 → 400 且注册表零改动（与 2a 合规同口径）。
    """
    try:
        ok = node_pool.set_slots(node_id, payload.slots)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="节点不存在")
    return {"ok": True}


@router.post("/api/nodes/{node_id}/capability")
def api_nodes_capability(node_id: str, payload: NodeCapabilityPayload):
    """★ A-P1-1：设置该节点**能力标签**（能跑哪些 workflow_type；空列表 = 全能力，热生效）。

    用途：双机单部署下两机模型不重叠（18 只装 Qwen-Image-2.1、24 是 H3 旧版）——
    声明能力后，调度器只把任务派给**具备该工作流能力**的节点，否则明确报错（不派错节点）。
    非法值（非字符串列表）→ 400 且注册表零改动。生成工作流全集见 `routers_gen._GEN_WORKFLOWS`。
    """
    try:
        return node_pool.set_capability(node_id, payload.capability)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
