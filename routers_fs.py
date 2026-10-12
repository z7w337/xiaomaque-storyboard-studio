# -*- coding: utf-8 -*-
"""本机目录浏览 API —— 供设置页「资产库路径」的选择按钮（用户 2026-10-01）。

为什么不用系统原生目录对话框：服务可能由后台/非交互会话启动，系统弹窗不保证能出来
（且无法自动化验证）。改成"浏览器内目录选择器"，稳且可测。

安全说明：这是**用户给自己选目录**的功能，故**有意允许**浏览任意路径（不做白名单）；
仍有的两条基本防护：① 只列目录、不返回文件内容 ② 出错回空列表 + 原因，不抛栈。
★ 注册位置有约束：必须在 main.py 的 `GET /api/{kind}` 通配**之前** include，否则被吞。
"""
import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/fs/dirs")
def api_fs_dirs(path: str = ""):
    """列出某目录下的子目录；path 为空时列出可用盘符（Windows）。"""
    raw = (path or "").strip().strip('"')
    if not raw:
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            d = "%s:\\" % letter
            if os.path.isdir(d):
                drives.append({"name": letter + ":", "path": d})
        return {"path": "", "parent": "", "dirs": drives, "roots": True}

    p = Path(raw)
    try:
        if p.is_dir():
            p = p.resolve()
    except OSError:
        pass

    parent = str(p.parent) if str(p.parent) != str(p) else ""

    if not p.is_dir():
        return {"path": str(p), "parent": parent, "dirs": [], "error": "目录不存在或不可访问"}

    dirs = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except PermissionError:
        return {"path": str(p), "parent": parent, "dirs": [], "error": "没有权限读取该目录"}
    except OSError as e:
        return {"path": str(p), "parent": parent, "dirs": [], "error": "读取失败：%s" % e}

    dirs.sort(key=lambda d: d["name"].lower())
    return {"path": str(p), "parent": parent, "dirs": dirs}
