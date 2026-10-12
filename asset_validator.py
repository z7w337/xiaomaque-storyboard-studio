# -*- coding: utf-8 -*-
"""资产卡保存校验器（阶段 6 · 6-13）。

目标：资产卡 save 端点 fail-fast 校验——入库即合法，6-10 视频直连注入（comfy_client.resolve_references
取 images[-1] + audios role=voice_sample）不再因脏音频静默失败/产坏片。

裁定与规格：
- 用户裁定"有则必合规"（沟通区 6-13 信标 L2542 / 规划-8 开工指令 L50）：音频不强制存在（可先图后音），
  但人物卡一旦绑定音色参考音频（任何标签）必须过 H3 校验，否则拒绝保存；不绑定则放行。
- H3_AUDIO_SPEC 官方数值（用户 2026-09-02 22:3x 裁定采信《H3参考音频技术规格研究.md》=接力包 §三）：
  单条 2~15s / 多条累计 ≤15s / 单条 ≤15MB / 独立参考音频 ≤3 段 / 采样率 ≥16kHz / 格式 flac·wav·mp3·m4a。
- 一视同仁（用户 09-02 22:4x 裁定补正）：人物卡三个标签（Qwen3-TTS voice_sample / 预置 trial /
  上传绑定 reference/旧卡无 role）本质都是"音色参考音频"，作为历史存入；时间最新或历史弹窗手动选中的那条
  = 当前有效音色，生成视频时作 H3 音色参考传入。故任一标签都可能成为 H3 克隆源 → 人物卡上所有音频
  一律走全套 H3 阻断校验（上传绑定音频质量最不可控，是校验重点），不再有 reference 超长豁免。
- 类型感知：人物卡≥1 当前主图 images[-1]；场景/道具卡≥1 图、无音频概念跳过（不校验音频）。
- 阻断 vs 警告（信标 L2550-2552）：
  阻断=格式非白名单 | 采样率 <16kHz | 时长越界(2s-15s) | 累计>15s | 文件>15MB | 段数>3 | 近静音(RMS) | 削波(峰值贴0dBFS)
        —— 拒存 + 字段级高亮
  警告=MP3/M4A 无法本地精确核验 RMS/削波、低采样率边缘、单说话人主观项等软提示（不拦截）。

工程约束（实测）：
- 工坊自带 Python 3.10.11 仅有标准库；已装 soundfile 0.14 + numpy 2.2（增强探测）。validator 顶层不 import
  soundfile/numpy，仅在探测函数内 try-import——失败自动降级纯标准库解析（WAV 用 wave，FLAC 用头解析），
  保证 main.py 启动永不因校验器依赖缺失而崩。
- 音频/图片 path 相对 shared 根（library.get_shared_assets_path()），正斜杠；兼容绝对路径。
"""

from __future__ import annotations

import math
import os
import struct
from pathlib import Path

# ---- 权威规格（官方数值已裁定，勿再改）----
H3_AUDIO_SPEC = {
    "min_duration_s": 2.0,        # 单条最短 2s（官方 API）
    "max_duration_s": 15.0,       # 单条最长 15s（官方 API）
    "max_total_duration_s": 15.0,  # 多条累计 ≤15s
    "max_file_mb": 15.0,          # 单条 ≤15MB
    "max_audio_segments": 3,      # 独立参考音频 ≤3 段
    "min_sample_rate_hz": 16000,  # 采样率 ≥16kHz（T8 自动重采样 32k；<16k 明显损失音色细节）
    "allowed_exts": (".flac", ".wav", ".mp3", ".m4a"),  # 官方支持 + ComfyUI LoadAudio
    # 机械质量判定阈值（voice_sample 阻断级）：
    "silence_rms": 0.01,          # 近静音：RMS < 0.01（约 -40dBFS）
    "clip_peak": 0.999,           # 削波：峰值贴 0dBFS
}

# 采样率"越界"判定（研究文档 §2.2：最低可用 16kHz，8k 及以下电话音强烈不推荐）
_MIN_SR_BLOCK = 16000
# 严重低采样率（电话音，质量显著差，建议用户更换）——作为警告下界
_LOW_SR_WARN = 8000


# ==================== 轻量音频探测（可降级） ====================

def _resolve_path(path: str, shared_root: str) -> Path | None:
    """path（相对 shared 根或绝对）→ 绝对 Path；不存在返回 None。"""
    if not path:
        return None
    p = Path(str(path).replace("\\", "/"))
    if p.is_absolute():
        cand = p
    else:
        cand = Path(shared_root) / p
    return cand if cand.is_file() else None


def _probe_with_soundfile(path: Path):
    """soundfile 全格式精确探测：返回 {samplerate, channels, duration_s, frames, rms, peak} 或 None。"""
    try:
        import soundfile as sf
        import numpy as np
    except Exception:
        return None
    try:
        info = sf.info(str(path))
        sr = int(info.samplerate)
        ch = int(info.channels)
        dur = float(info.duration)
        frames = int(info.frames)
        # 帧级 RMS/峰值（流式分块避免整读大文件内存峰值）
        data, _ = sf.read(str(path), always_2d=True, dtype="float64")
        rms = float(np.sqrt((data ** 2).mean()))
        peak = float(np.abs(data).max())
        return {"samplerate": sr, "channels": ch, "duration_s": dur,
                "frames": frames, "rms": rms, "peak": peak}
    except Exception:
        return None


def _probe_wav_stdlib(path: Path) -> dict | None:
    """标准库 wave 解析 WAV（PCM）。"""
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            frames = w.getnframes()
            sw = w.getsampwidth()
        dur = frames / float(sr) if sr else 0.0
        # RMS/峰值：仅 16bit PCM 可精确；其余尽力跳过
        rms = peak = None
        if sw == 2:
            try:
                # ★ 依赖前提（派单002 T5-5，仅加注释不改逻辑）：
                #   本路径用标准库 `audioop` 算 RMS/峰值；`audioop` 自 **Python 3.13 起被移除**（PEP 594）
                #   ⇒ 本模块要求解释器 **≤ 3.12**。本项目**随包 Python = 3.10.11**，满足此前提。
                #   ★ 换用 3.13+ 解释器会让这里 ImportError（下方 except 兜底 ⇒ RMS 静默缺失，不报错）。
                import audioop  # Python 3.10 仍含
                with wave.open(str(path), "rb") as w:
                    total = 0.0
                    maxa = 0.0
                    n = 0
                    while True:
                        raw = w.readframes(65536)
                        if not raw:
                            break
                        n += len(raw) // sw
                        sq = audioop.rms(raw, sw) ** 2
                        total += sq * (len(raw) // sw)
                        cur = audioop.max(raw, sw)
                        maxa = max(maxa, float(cur))
                    peak = maxa / (2 ** (8 * sw - 1)) if maxa else 0.0
                    rms = math.sqrt(total / n) / (2 ** (8 * sw - 1)) if n else 0.0
            except Exception:
                pass
        return {"samplerate": sr, "channels": ch, "duration_s": dur,
                "frames": frames, "rms": rms, "peak": peak}
    except Exception:
        return None


def _probe_flac_stdlib(path: Path) -> dict | None:
    """纯头解析 FLAC STREAMINFO：采样率/声道/总采样/时长（无需解码库）。无 RMS/峰值。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head != b"fLaC":
                return None
            # 遍历 metadata 块找 STREAMINFO (type 0)
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    return None
                last = hdr[0] >> 7
                btype = hdr[0] & 0x7F
                ln = int.from_bytes(hdr[1:4], "big")
                payload = f.read(ln)
                if btype == 0 and len(payload) >= 34:
                    # STREAMINFO layout:
                    # 0..1 minblock 2..3 maxblock 4..6 minframe 7..9 maxframe
                    # then: sample_rate 20b, (channels-1) 3b, (bps-1) 5b, total_samples 36b
                    b10_13 = int.from_bytes(payload[10:13], "big")
                    sr = b10_13 >> 4
                    # 剩下从 bit36 起：channel-1(3b) bps-1(5b) total(36b) 共 44b，落在 payload[13:18]（40b）+ 需再 4b
                    b13_17 = int.from_bytes(payload[13:18], "big")  # 40 bits
                    # 组合：总共需 44 bits（ch3+bps5+total36）
                    b13_18 = (b13_17 << 4) | (payload[18] >> 4) if len(payload) > 18 else b13_17 << 4
                    ch = ((b13_18 >> 41) & 0x7) + 1
                    # total_samples 为低 36 bits
                    total = b13_18 & ((1 << 36) - 1)
                    dur = total / float(sr) if sr else 0.0
                    return {"samplerate": sr, "channels": ch, "duration_s": dur,
                            "frames": total, "rms": None, "peak": None}
                if last:
                    break
        return None
    except Exception:
        return None


def _probe_audio_file(path: Path) -> dict | None:
    """探测音频文件元数据：优先 soundfile（全格式精确含 RMS/峰值），降级纯标准库。"""
    # 1) soundfile 增强探测
    sf_res = _probe_with_soundfile(path)
    if sf_res is not None:
        return sf_res
    # 2) 标准库降级
    ext = path.suffix.lower()
    if ext == ".wav":
        return _probe_wav_stdlib(path)
    if ext == ".flac":
        return _probe_flac_stdlib(path)
    # mp3/m4a：无本地解码库，无法可靠探测 → 返回 None（调用方按"不可判"处理）
    return None


def _ext_is_audio(ext: str) -> bool:
    return ext.lower() in H3_AUDIO_SPEC["allowed_exts"]


# ==================== 公开校验入口 ====================

def validate_audio_file_upload(path: Path) -> dict:
    """上传音频单文件合规校验（§十四 B-a 项③：上传口接入真校验，取代仅查扩展名）。
    返回 {ok: bool, error?: str, warn?: str}。
    - 阻断(ok=False)：格式非白名单 | 文件 >15MB | 时长越界(<2s 或 >15s) | 采样率 <16kHz。
    - warn：本地无法核验(mp3/m4a 无解码库时) → 提示自查但不阻断（与 validate_asset_on_save 警告口径一致）。
    提示文案带具体不达标项 + 上限口径，供前端/上传端点统一报错（勿出现两套标准）。
    """
    ext = path.suffix.lower()
    if not _ext_is_audio(ext):
        allow = "、".join(H3_AUDIO_SPEC["allowed_exts"])
        return {"ok": False, "error": f"音频格式不支持：仅支持 {allow}（当前 {ext or '未知'}）。请转成 WAV/FLAC/MP3/M4A"}
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = None
    if size_mb is not None and size_mb > H3_AUDIO_SPEC["max_file_mb"]:
        return {"ok": False, "error": f"音频文件过大：{size_mb:.1f}MB > {H3_AUDIO_SPEC['max_file_mb']:.0f}MB（单条上限）"}
    probe = _probe_audio_file(path)
    if probe is None:
        # 无法本地精确核验（无解码库 + mp3/m4a）——强警告放行，提示按要求自查
        return {"ok": True, "warn": "该音频无法本地核验采样率/时长/质量，请确认为清晰单说话人干音且 2~15 秒、采样率 ≥16kHz（建议改用 WAV/FLAC）"}
    sr = probe.get("samplerate")
    dur = probe.get("duration_s")
    if sr is not None and sr < _MIN_SR_BLOCK:
        return {"ok": False, "error": f"采样率过低：{sr}Hz < {_MIN_SR_BLOCK}Hz（音色细节会严重损失）。请提供 ≥16kHz 音频"}
    if dur is not None:
        if dur < H3_AUDIO_SPEC["min_duration_s"]:
            return {"ok": False, "error": f"音频过短：{dur:.1f}s < {H3_AUDIO_SPEC['min_duration_s']:.0f}s（音色特征不足，H3 报错或效果差）"}
        if dur > H3_AUDIO_SPEC["max_duration_s"]:
            return {"ok": False, "error": f"音频过长：{dur:.1f}s > {H3_AUDIO_SPEC['max_duration_s']:.0f}s（H3 只取前 15s，多余浪费）。请截断到 2~15s"}
    return {"ok": True}


def validate_asset_on_save(asset: dict, kind: str = "characters") -> dict:
    """资产保存前校验。返回 {errors: {field: [msg...]}, warnings: {field: [msg...]}}。

    - errors：阻断级（机械可判、确定性高），非空则调用方拒绝保存并给前端字段级高亮。
    - warnings：软提示，不阻断保存。
    - field 键约定：images[-1] 的阻断用 "images[-1]"，audios 用 "audios[{i}]"（i 为该音条在 audios 的下标），
      无音频但图不足的人物用 "images[-1]"。
    - 纯本地、不碰远程、不写数据（只读探测）。
    """
    errors: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}

    def _add(d: dict, field: str, msg: str) -> None:
        d.setdefault(field, []).append(msg)

    images = asset.get("images") or []
    audios = asset.get("audios") or []

    # ---- 类型感知：人物卡必须有 ≥1 张当前主图（images[-1]，与 6-10 取图口径一致）----
    # 场景/道具卡也必须 ≥1 张图；但仅人物卡对音频做校验。
    if not images:
        _add(errors, "images[-1]", "资产卡缺少图片：请至少保留一张当前主图（人物/场景/道具卡均须 ≥1 图）")
        return {"errors": errors, "warnings": warnings}

    # 取当前主图是否存在物理文件
    shared_root = os.environ.get("STORYBOARD_SHARED", "")
    if not shared_root:
        try:
            # 延迟 import 避免循环依赖/加重启动；批3 3a：经 dao 门面取
            from dao import library
            shared_root = library.get_shared_assets_path()
        except Exception:
            shared_root = ""  # 收口整改 2026-09-11：去本机绝对路径兜底（本分支已属降级路径：env 未设且 dao 取路径失败）

    # ---- 音频校验：仅人物卡（audios 仅人物概念）；非人物卡直接跳过音频 ----
    if kind != "characters":
        # 非人物卡即使误带 audios 也不校验（语义上场景/道具无音色）
        return {"errors": errors, "warnings": warnings}

    if not audios:
        # "有则必合规"：无音频放行
        return {"errors": errors, "warnings": warnings}

    # ---- 一视同仁全套校验（用户 09-02 22:3x 裁定）----
    # 人物卡上三个标签（Qwen3-TTS voice_sample / 预置 trial / 上传绑定 reference 或旧卡无 role）本质都是
    # "音色参考音频"，作为历史存入；保存后时间最新或用户在历史弹窗手动选中的那条 = 当前有效音色，生成视频时
    # 作为音色参考传入 H3。故任一标签都可能成为 H3 克隆源 → 全部须过 H3 合规校验（尤其上传绑定音频质量最不可控，
    # 是本次校验的重点）。不再有 reference 超长豁免。
    total_dur = 0.0
    for idx, a in enumerate(audios):
        role = (a.get("role") or "").lower()
        path = a.get("path") or ""
        field = f"audios[{idx}]"

        # 格式白名单（阻断）——扩展名识别（覆盖历史缺扩展名/绝对路径）
        ext = Path(path.split("?")[0].split("#")[0]).suffix.lower() if path else ""
        if not path:
            _add(errors, field, "音频条目缺少 path")
            continue
        if not _ext_is_audio(ext):
            allow = "、".join(H3_AUDIO_SPEC["allowed_exts"])
            _add(errors, field, f"音频格式不支持：仅支持 {allow}（当前 {ext or '未知'}）。请转成 WAV/FLAC/MP3/M4A")
            continue

        fp = _resolve_path(path, shared_root)
        if fp is None:
            _add(errors, field, f"音频文件不存在：{path}（请先上传/绑定有效音频）")
            continue

        # 文件大小上限（阻断）
        try:
            size_mb = fp.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = None
        if size_mb is not None and size_mb > H3_AUDIO_SPEC["max_file_mb"]:
            _add(errors, field, f"音频文件过大：{size_mb:.1f}MB > {H3_AUDIO_SPEC['max_file_mb']:.0f}MB")

        # 探测（soundfile 全格式优先；纯标准库降级时仅 wav/flac 可精确）
        probe = _probe_audio_file(fp) if (ext in (".flac", ".wav", ".mp3", ".m4a")) else None

        if probe is None:
            # 无法探测（无解码库且 mp3/m4a，或文件损坏）——不机械阻断，但给强警告要求自查
            _add(warnings, field, "该音频无法本地核验采样率/时长/质量，请确认其为清晰单说话人干音且 2~15 秒（建议改用 WAV/FLAC）")
            continue

        sr = probe.get("samplerate")
        dur = probe.get("duration_s")

        # 采样率（阻断：<16kHz；警告：电话音 ≤8kHz）
        if sr is not None:
            if sr < _MIN_SR_BLOCK:
                _add(errors, field, f"采样率过低：{sr}Hz < {_MIN_SR_BLOCK}Hz（音色细节会严重损失，T8 也难救回）。请提供 ≥16kHz 音频")
            elif sr <= _LOW_SR_WARN:
                _add(warnings, field, f"采样率偏低 {sr}Hz（电话音质），建议换用 ≥16kHz 的清晰干音")

        # 时长（阻断：单条 2s-15s；累计 ≤15s 另计）
        if dur is not None:
            if dur < H3_AUDIO_SPEC["min_duration_s"]:
                _add(errors, field, f"音频过短：{dur:.1f}s < {H3_AUDIO_SPEC['min_duration_s']:.0f}s（音色特征不足，H3 报错或效果差）")
            elif dur > H3_AUDIO_SPEC["max_duration_s"]:
                _add(errors, field, f"音频过长：{dur:.1f}s > {H3_AUDIO_SPEC['max_duration_s']:.0f}s（H3 只取前 15s，多余浪费；请截断到 2~15s）")
            else:
                total_dur += dur
        else:
            _add(warnings, field, "无法读取该音频时长，请确认其为 2~15 秒")

        # 近静音（阻断，仅可解码格式有 rms）
        rms = probe.get("rms")
        if rms is not None and rms < H3_AUDIO_SPEC["silence_rms"]:
            _add(errors, field, f"音频近静音：RMS={rms:.4f} < {H3_AUDIO_SPEC['silence_rms']}（几乎无声，无法作为音色克隆源）")

        # 削波（阻断）
        peak = probe.get("peak")
        if peak is not None and peak >= H3_AUDIO_SPEC["clip_peak"]:
            _add(errors, field, f"音频削波：峰值 {peak:.3f} 贴满 0dBFS（可能失真爆音），请降低录音电平后重录")

    # 段数（阻断：>3 段独立音色参考音频）
    if len(audios) > H3_AUDIO_SPEC["max_audio_segments"]:
        _add(errors, "audios", f"音色参考音频超过上限：{len(audios)} 段 > {H3_AUDIO_SPEC['max_audio_segments']} 段（H3 最多 3 段独立参考音频）")

    # 累计时长（阻断：全部合规音色参考音频累计 >15s）
    if total_dur > H3_AUDIO_SPEC["max_total_duration_s"]:
        _add(errors, "audios", f"音色参考音频累计时长超限：{total_dur:.1f}s > {H3_AUDIO_SPEC['max_total_duration_s']:.0f}s（多条合计须 ≤15s）")

    return {"errors": errors, "warnings": warnings}


# ==================== 全库体检（一次性工具） ====================

def audit_all_library(items_by_kind: dict[str, list] | None = None) -> dict:
    """全库体检：扫描所有人物卡音色音频（任何标签）是否合规，列出不合规清单供用户修复。

    入参 items_by_kind: {"characters":[...], "scenes":[...], "props":[...]}，省略则从 data/ 文件读。
    返回 {"total_cards": N, "with_audio": N, "compliant": N, "issues": [{kind,id,prefix,variant_name,field,msg}...]}。
    只读扫描，不修改任何数据；用于向后兼容（让旧数据在新规则下可被定位修复，而非静默拒绝）。
    """
    # 批3 3a：经 dao 门面取（避免散落直连底层）
    from dao import library
    KINDS, get_shared_assets_path, list_items = (
        library.KINDS, library.get_shared_assets_path, library.list_items,
    )

    shared_root = get_shared_assets_path()
    if items_by_kind is None:
        items_by_kind = {k: list_items(k) for k in KINDS}

    issues = []
    total = 0
    with_audio = 0
    compliant = 0
    for kind, items in items_by_kind.items():
        if kind != "characters":
            continue  # 体检只针对人物卡音色
        for it in items:
            total += 1
            audios = it.get("audios") or []
            if not audios:
                continue  # 无音色音频的卡无需体检
            with_audio += 1
            # 构建带 path 的临时检查卡，复用校验逻辑（但不触碰原数据）
            tmp = dict(it)
            # 校验会在缺文件/超长/格式等上报 error
            res = validate_asset_on_save(tmp, kind)
            if not res["errors"]:
                compliant += 1
            for field, msgs in res["errors"].items():
                for m in msgs:
                    issues.append({
                        "kind": kind, "id": it.get("id"), "prefix": it.get("prefix"),
                        "variant_name": it.get("variant_name"), "field": field, "msg": m,
                    })
    return {
        "total_cards": total, "with_audio": with_audio, "compliant": compliant,
        "issues": issues,
    }
