# -*- coding: utf-8 -*-
"""回收站 API（用户 2026-10-01 裁定 A：删除不入土 → 回收站 → 导航栏可进、可恢复）。

★ 注册顺序有硬约束：本路由必须在 main.py 的 `GET/POST/DELETE /api/{kind}` 通配**之前**
  include，否则 `/api/trash` 会被当作 `kind="trash"` 吞掉（与 routers_tasks 同款红线）。
"""
from fastapi import APIRouter, HTTPException

import trash

router = APIRouter()


@router.get("/api/trash")
def api_trash_list():
    """回收站列表 + 统计（含保留天数，页面据此显示「超过 N 天自动清理」）。"""
    return trash.list_items()


@router.post("/api/trash/empty")
def api_trash_empty():
    """清空回收站（彻底删除全部条目，不可恢复）。"""
    return trash.purge_all()


@router.post("/api/trash/{tid}/restore")
def api_trash_restore(tid: str):
    """恢复到原位置（原路径被占用则改名，不覆盖）。"""
    r = trash.restore(tid)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("msg") or "恢复失败")
    return r


@router.delete("/api/trash/{tid}")
def api_trash_purge(tid: str):
    """彻底删除单条（不可恢复）。"""
    r = trash.purge(tid)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("msg") or "删除失败")
    return r
