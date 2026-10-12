# -*- coding: utf-8 -*-
"""数据文件原子写入工具（《工程管理规范》第 9 节 / 计划书 2.4 强制要求）。

所有 JSON/文本数据文件的写入统一走本封装：
先写同目录临时文件 -> fsync 落盘 -> os.replace 原子替换。
任何时刻断电/崩溃，文件要么是旧的完整版本、要么是新的完整版本，
不会出现半截损坏。

可选滚动备份（审计 G3-1）：bak_every>0 时每 N 次成功写入保留一份 `<file>.bak`。
"""
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


# 滚动备份（审计 G3-1：三库/references/projects 数据域补"滚动 .bak"）
# 计数按目标文件维度，进程内累计；备份失败静默，绝不影响主写入。
_BAK_LOCK = threading.Lock()
_BAK_WRITE_COUNT: dict[str, int] = {}


def _roll_bak(path: Path, every: int) -> None:
    """把刚写成功的有效文件复制为 `<path>.bak`（滚动备份）。

    :param every: <=1 = 每次写入都滚动；>1 = 每 every 次写入滚动一次。
    语义对齐 assets_index 索引域既有做法（写成功后复制当前有效文件），
    用于从"事后损坏/截断"中恢复（滚动刷新 → 恢复丢失≈0）。
    """
    try:
        key = str(path)
        with _BAK_LOCK:
            n = _BAK_WRITE_COUNT.get(key, 0) + 1
            _BAK_WRITE_COUNT[key] = n
        if every > 1 and (n % every) != 0:
            return
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
    except Exception:
        pass  # 备份属增强能力，任何失败都不回传


def atomic_write_json(path, data, indent=2, ensure_ascii=False, bak_every=0):
    """原子写入 JSON 文件。

    :param path: 目标文件路径（str 或 Path）
    :param data: 可 JSON 序列化的数据
    :param indent: JSON 缩进
    :param ensure_ascii: 是否转义非 ASCII（默认 False，保留中文可读）
    :param bak_every: 滚动备份开关——>0 时每 bak_every 次成功写入把有效文件复制为
        `<path>.bak`（1=每次写入都滚动）。默认 0=关闭，与旧版行为完全一致。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        # Windows Defender 实时扫描持锁 → os.replace 可能抛 [WinError 5] 拒绝访问（P0-6 治疗：
        # 专家文件导入时 34 个资产密集写 characters.json 实测触发）。指数退避重试 6 次（累计约 3s）。
        last_err = None
        for attempt in range(6):
            try:
                os.replace(tmp_path, path)
                if bak_every:
                    _roll_bak(path, bak_every)  # 内部吞异常，不影响主写入
                return
            except OSError as e:
                last_err = e
                time.sleep(0.15 * (attempt + 1))
        raise last_err
    except BaseException:
        _cleanup_tmp(tmp_path)
        raise


def atomic_write_text(path, text, encoding="utf-8"):
    """原子写入文本文件（同 atomic_write_json 的原子性保证）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        _cleanup_tmp(tmp_path)
        raise


def _cleanup_tmp(tmp_path):
    """清理写入失败时残留的临时文件。"""
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
