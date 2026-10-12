# -*- coding: utf-8 -*-
"""④ F5c · 云端出图状态端点（独立模块 —— main.py 不增长）。

GET /api/cloud/status：设置页/编辑器用它判断"云端可用性、分流方式、当日用量"。
★ 不返回任何密钥（api_key 永不出此端点）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

import ms_image

router = APIRouter(prefix="/api/cloud", tags=["cloud"])


@router.get("/status")
def api_cloud_status():
    cfg = ms_image.config()
    try:
        usage = json.load(open(Path(ms_image.ROOT) / "data" / "ms_usage.json", encoding="utf-8"))
    except Exception:
        usage = {}
    return {
        "ready": bool(cfg["api_key"]),
        "route_mode": cfg["route_mode"],          # off=只用本地 | manual=手工勾选走云端
        "concurrency": cfg["concurrency"],
        "models": {"t2i": cfg["model_t2i"], "i2i": cfg["model_i2i"]},   # 供编辑器「平台→模型」列出（不含密钥）
        "usage": usage,                           # {date, t2i, i2i, total:{t2i,i2i}}
    }
