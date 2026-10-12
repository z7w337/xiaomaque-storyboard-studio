# -*- coding: utf-8 -*-
"""回收站（用户 2026-10-01 裁定 A：删除不入土，先进回收站，可恢复）。

落点：`SharedAssets/.trash/`
    files/<tid>__<原文件名>   实体（**move 进来，不是 copy**）
    index.json                元数据（原相对路径 / 大小 / 删除时间 / 原因）

为什么这样做（三条硬约束，改动前先读）：
  1. **零额外磁盘**：用 move（同盘 rename，瞬时且不占新空间）。本机 C 盘仅剩约 4G，
     "登记时留一份保护性副本"（双份磁盘）方案已被用户否掉。
  2. **可恢复**：move 回原相对路径；原路径已被占用时**不覆盖**，改名加后缀。
  3. ★★ **必须让版本索引"看不见"**：`.trash` 已列入 `assets_index._EXCLUDE_PREFIXES`。
     否则 `reconcile()`（每 60s 全树 rglob）会把回收站里的文件当新资产**重新登记**、
     并重新出现在「未分类图」列表里 —— 删除等于白删。改动本模块时别动那条排除。
     （监听线程 watchfiles 默认忽略隐藏目录，天然不看 `.trash`。）

自动清理：超过 `KEEP_DAYS` 天的条目在服务启动时 + 每 6 小时彻底删除一次。
"""
import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import data_io
import library

TRASH_DIR_NAME = ".trash"
KEEP_DAYS = 30                       # 保留天数（超期自动彻底删除）
CLEAN_INTERVAL_SECONDS = 6 * 3600    # 自动清理间隔

_LOCK = threading.RLock()
_CLEAN_THREAD = None
_CLEAN_STOP = threading.Event()


# ==================== 路径 ====================

def _root() -> Path:
    return Path(library.get_shared_assets_path()) / TRASH_DIR_NAME


def _files_dir() -> Path:
    return _root() / "files"


def _index_path() -> Path:
    return _root() / "index.json"


def _blank() -> dict:
    return {"schema": 1, "updated_at": library.now_iso(), "items": []}


def _load() -> dict:
    p = _index_path()
    if not p.exists():
        return _blank()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return _blank()


def _save(d: dict) -> None:
    d["updated_at"] = library.now_iso()
    fs = _files_dir()
    fs.mkdir(parents=True, exist_ok=True)
    data_io.atomic_write_json(_index_path(), d)


# ==================== 写入：移入回收站 ====================

def move_to_trash(abs_path, rel: str, reason: str = "") -> dict:
    """把库内一个物理文件移入回收站（同盘 move）。文件不存在则静默返回 not moved。

    ★ 本模块是**删除物理媒体的唯一出口**：以后任何要删库内文件的地方都调它，
      不要再直接 unlink（那会绕过回收站、用户将无法恢复）。
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    try:
        src = Path(abs_path)
    except TypeError:
        return {"moved": False, "msg": "路径无效"}
    if not src.is_file():
        return {"moved": False, "msg": "文件不存在"}
    with _LOCK:
        try:
            fs = _files_dir()
            fs.mkdir(parents=True, exist_ok=True)
            tid = library.new_id("t_")
            name = src.name
            dest = fs / f"{tid}__{name}"
            _move(src, dest)
        except OSError as e:
            return {"moved": False, "msg": f"移入回收站失败：{e}"}
        item = {
            "id": tid,
            "name": name,
            "rel": rel,                 # 原相对路径（恢复回这里）
            "size": dest.stat().st_size if dest.is_file() else 0,
            "deleted_at": library.now_iso(),
            "reason": reason or "删除",
            "file": f"files/{dest.name}",                       # 回收站内相对路径（恢复/彻底删用它定位）
            "preview": f"{TRASH_DIR_NAME}/files/{dest.name}",   # 相对库根（页面缩略图走 /api/asset-file/）
        }
        d = _load()
        d["items"].append(item)
        _save(d)
        return {"moved": True, "item": item}


def _move(src: Path, dest: Path) -> None:
    """同盘优先 os.replace（原子瞬时）；跨盘回退 shutil.move。"""
    try:
        os.replace(str(src), str(dest))
    except OSError:
        shutil.move(str(src), str(dest))


# ==================== 读取 ====================

def list_items(limit: int = 500) -> dict:
    with _LOCK:
        d = _load()
    items = sorted(d.get("items") or [], key=lambda x: x.get("deleted_at") or "", reverse=True)
    return {"items": items[:limit], "total": len(d.get("items") or []),
            "keep_days": KEEP_DAYS, "stats": _stats(d)}


def _stats(d: dict) -> dict:
    items = d.get("items") or []
    return {
        "count": len(items),
        "bytes": sum(int(x.get("size") or 0) for x in items),
        "oldest": min((x.get("deleted_at") or "" for x in items), default=""),
        "keep_days": KEEP_DAYS,
    }


def stats() -> dict:
    with _LOCK:
        return _stats(_load())


# ==================== 恢复 ====================

def _free_target(root: Path, rel: str) -> Path:
    """恢复目标：原相对路径；已被占用则加后缀（**绝不覆盖**现有文件）。"""
    target = (root / rel).resolve()
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for i in range(1, 1000):
        cand = parent / f"{stem}__恢复{i}{suffix}"
        if not cand.exists():
            return cand
    return parent / f"{stem}__恢复{library.new_id()[:6]}{suffix}"


def restore(tid: str) -> dict:
    """把回收站条目移回原位置，并**重新登记**版本索引（恢复它在库里的"在用"身份）。"""
    with _LOCK:
        d = _load()
        item = next((x for x in (d.get("items") or []) if x.get("id") == tid), None)
        if not item:
            return {"ok": False, "msg": "回收站里没有这一条（可能已被清理）"}
        src = _root() / (item.get("file") or "")
        if not src.is_file():
            # 实体没了 → 顺手清掉这条僵尸记录
            d["items"] = [x for x in (d.get("items") or []) if x.get("id") != tid]
            _save(d)
            return {"ok": False, "msg": "文件已不在回收站（可能已被彻底清理）"}
        root = Path(library.get_shared_assets_path()).resolve()
        target = _free_target(root, item.get("rel") or item.get("name") or "restored")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _move(src, target)
        except OSError as e:
            return {"ok": False, "msg": f"恢复失败：{e}"}
        d["items"] = [x for x in (d.get("items") or []) if x.get("id") != tid]
        _save(d)
        rel_new = str(target.relative_to(root)).replace("\\", "/")
    # 重新登记（锁外做，避免与索引锁交叉；失败不影响文件已恢复的事实）
    # ★ 先 revive 再 register：恢复的是**同一份文件**（sha 不变），register_file 会因幂等键命中而 skip，
    #   不复活的话该图会永远停在 deleted 状态 → 不出现在「未分类图」里（用户以为"恢复了却找不到"）。
    # ★★ A-P2-4（2026-10-02）：原来 `except Exception: pass` **静默吞** → **假成功**
    #   （接口回 ok:true，而索引没更新 ⇒ 资产库里看不到恢复的图，用户无从判断）。
    #   现改为：记留痕 + 返回 `index_updated: false` + 人类可读 msg（文件确实已恢复，只是索引待修）。
    _index_updated = True
    try:
        import assets_index
        if not assets_index.revive(rel_new).get("changed"):
            assets_index.register_file(rel_new)
    except Exception as e:                              # noqa: BLE001
        _index_updated = False
        print("[trash] ★ 恢复后索引更新失败（文件已恢复，索引待修）：rel=%s err=%r" % (rel_new, e))
    out = {"ok": True, "rel": rel_new, "renamed": rel_new != (item.get("rel") or ""),
           "index_updated": _index_updated}
    if not _index_updated:
        out["msg"] = "文件已恢复到原位置，但资产索引更新失败 —— 它可能暂时不出现在资产库（刷新或跑一次资产体检可修复）"
    return out


# ==================== 彻底删除 ====================

def purge(tid: str) -> dict:
    with _LOCK:
        d = _load()
        item = next((x for x in (d.get("items") or []) if x.get("id") == tid), None)
        if not item:
            return {"ok": False, "msg": "回收站里没有这一条"}
        src = _root() / (item.get("file") or "")
        try:
            if src.is_file():
                src.unlink()
        except OSError as e:
            return {"ok": False, "msg": f"删除失败：{e}"}
        d["items"] = [x for x in (d.get("items") or []) if x.get("id") != tid]
        _save(d)
        return {"ok": True, "deleted": item.get("name")}


def purge_all() -> dict:
    with _LOCK:
        d = _load()
        items = d.get("items") or []
        n = 0
        failed = []
        for it in items:
            src = _root() / (it.get("file") or "")
            try:
                if src.is_file():
                    src.unlink()
                n += 1
            except OSError as e:
                failed.append(f"{it.get('name')}: {e}")
        d["items"] = []
        _save(d)
        return {"ok": True, "deleted": n, "failed": failed}


def auto_cleanup(keep_days: int = KEEP_DAYS) -> dict:
    """彻底删除超过保留期的条目（启动时 + 定期）。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with _LOCK:
        d = _load()
        keep, drop = [], []
        for it in (d.get("items") or []):
            (drop if (it.get("deleted_at") or "") and (it.get("deleted_at") or "") < cutoff else keep).append(it)
        if not drop:
            return {"ok": True, "deleted": 0, "kept": len(keep)}
        n = 0
        for it in drop:
            src = _root() / (it.get("file") or "")
            try:
                if src.is_file():
                    src.unlink()
                n += 1
            except OSError:
                keep.append(it)          # 删不掉的留着，下次再试
        d["items"] = keep
        _save(d)
        return {"ok": True, "deleted": n, "kept": len(keep)}


# ==================== 启动 ====================

def _clean_loop() -> None:
    while not _CLEAN_STOP.is_set():
        for _ in range(CLEAN_INTERVAL_SECONDS):
            if _CLEAN_STOP.wait(1):
                return
        try:
            auto_cleanup()
        except Exception:
            pass


def start() -> dict:
    """服务启动钩子：清一次超期条目，并起定时清理线程。幂等。"""
    global _CLEAN_THREAD
    try:
        r = auto_cleanup()
    except Exception as e:
        r = {"ok": False, "msg": str(e)}
    if not (_CLEAN_THREAD and _CLEAN_THREAD.is_alive()):
        _CLEAN_STOP.clear()
        _CLEAN_THREAD = threading.Thread(target=_clean_loop, name="trash-clean", daemon=True)
        _CLEAN_THREAD.start()
    return {"cleanup": r, "keep_days": KEEP_DAYS}
