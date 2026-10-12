# -*- coding: utf-8 -*-
"""comfy_client.py — 工坊直连远程 ComfyUI（6-10 业务层，2026-08-31 落地）。

替代经 IC 的双通道（R6 停更 / R18 视频回传缺陷 处置：主通道绕开 IC，IC 仅备用）。
职责：加载工作流 → 解析 prompt 标记派生参考图/音色 → 上传资源(ASCII 名) →
注入 363 节点 → 提交 /prompt(显式捕获 node_errors) → 轮询 /history → 拉取 /view 视频。

红线（规划 6-10 批复，2026-08-31）：
- node_errors 必捕获（不静默，直接抛错）
- history + view 兜底（不依赖 IC，避免 R18 复现）
- mp4 完整性（ftyp+moov+mdat）由 main.py _is_mp4_useful 校验（此处仅拉回字节）
- 产物须用户目检（机制二）

设计约束（裁定）：
- 参考图/音频"后端派生"（前端无感）：解析 payload.prompt 的 <role>/<scene>/<prop>/<reference>
  标记 → 查库得 images[-1] + 人物"当前有效音色"音频（批3-3：audios 首条，无论 role）；前端若显式填 ref_images/ref_audios 则优先用。
- 扩槽 A1（14:19 信标）：N 说话人卡 = N ref_image 槽 + N ref_audios 槽，一一对应；
  expand_workflow 节点复制法克隆 LoadImage/LoadAudio 节点到 N（id 顺延），不做选片、不"仅注入 1 个全局"。
- B1：某卡无有效音色音频 → 该卡报错跳过（不静默、不幻觉填音）。
- 参考图/音频必须 ASCII 名上传（中文名触发 ComfyUI Custom validation failed）。
- 时长走 350(PrimitiveFloat 秒)→351(×24 对齐 17) 原生链路注入 363.length（帧数），勿直写 363.length=秒。
"""
import os
import re
import time
import json
import hashlib
import requests

# 批3 3a：数据访问统一经 dao 门面（留低成本切换点）。
# 从 dao.library 取函数并赋同名局部，保持调用点 `get_comfyui_url()` 等零改动。
from dao import library

get_comfyui_url = library.get_comfyui_url
get_shared_assets_path = library.get_shared_assets_path
list_items = library.list_items
new_id = library.new_id
ensure_settings = library.ensure_settings
read_settings = library.read_settings  # 收尾A③：读路径用纯读版（零写副作用）

import logging
_log = logging.getLogger("storyboard.comfy")   # v1.59 作用域回退告警用

# ── 工作流关键节点（minimax_h3_t8.json，扁平 dict：key=节点 id）──
COND_NODE = "363"            # MiniMaxH3AudioConditioningT8（参考图/音频条件节点）
REF_IMAGE_NODES = ["362", "364", "365"]   # LoadImage ×3（ref_image_0/1/2）
REF_AUDIO_NODE = "361"       # LoadAudio ×1（ref_audio_0）
VIDEO_OUT_NODE = "360"       # VHS_VideoCombine（视频产物输出）
PROMPT_NODE = "339"          # PrimitiveStringMultiline（363.prompt 上游）
SIZE_NODE = "340"            # ResolutionSelector（363.width/height 上游）

# 363 节点注入口径（与 v1.54 实测 + 裁定一致）
AUDIO_MODE_REF = "reference_only"   # 注入音色样本时用
AUDIO_MODE_NATIVE = "native"       # 无音色样本时用（不引用音频）

# prompt 标记：<role>程度</role> / <scene>X</scene> / <prop>X</prop> / <reference>path</reference>
_MARK_RE = re.compile(r"<((?:role|scene|prop))\b[^>]*>([^<]+)</\1>", re.IGNORECASE)
_REF_RE = re.compile(r"<reference>([^<]+)</reference>", re.IGNORECASE)
_KIND_OF = {"role": "characters", "scene": "scenes", "prop": "props"}
# 仅接受常见图片/音频后缀
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_AUD_EXT = (".wav", ".mp3", ".flac", ".m4a", ".ogg")


class ComfyCancelled(RuntimeError):
    """轮询期间被用户取消（由 `cancel_check` 回调触发）。

    ★ ② 步4-②：与"失败"区分开 —— 调用方应转为**取消**语义（不落库），而不是记成失败原因。
    """


def _wf_path(name: str) -> str:
    """工作流本地路径：优先工坊自有 workflows/ 目录（运行时零第三方依赖），找不到回退 IC 目录。
    name 约定含 'custom/' 前缀（与 IC /api/workflows/{name} 一致），如 'custom/minimax_h3_t8.json'。
    工坊自有目录 = 本文件所在目录下的 workflows/（第5批阶段1已复制 minimax_h3_t8.json 至此）。
    """
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows", name)
    if os.path.exists(local):
        return local
    # 回退 IC 目录（兼容旧配置/历史调用，IC 删除前作为兜底）——收尾A③：纯读不写
    cfg = read_settings()
    base = cfg.get("ic_path") or ""  # 收口整改 2026-09-11：去本机绝对路径兜底；IC 已下线，本地 workflows/ 命中时不走此分支
    return os.path.join(base, "workflows", name)


def load_workflow(name: str) -> dict:
    """加载工作流 JSON（扁平 dict：key=节点 id，value={class_type, inputs}）。"""
    with open(_wf_path(name), "r", encoding="utf-8") as f:
        return json.load(f)


def _abs(p: str, shared: str) -> str:
    """相对 SharedAssets 路径 → 绝对路径；已是绝对/http 则原样返回。"""
    if p.startswith(("http://", "https://", "/")):
        return p
    return os.path.join(shared, p.lstrip("/"))


def _asset_key(it: dict) -> str:
    """前端 findAsset 匹配口径：prefix + '-' + variant_name（去尾随连字符）。"""
    return ((it.get("prefix", "") or "") + "-" + (it.get("variant_name", "") or "")).rstrip("-")


def resolve_references(prompt: str, prefer_ref_images=None, prefer_ref_audios=None,
                       pid: str = "", eid: str = ""):
    """解析参考图/音频（扩槽 A1 版）。

    返回 (images, audios, skipped)：
      images: [绝对路径, ...]（按出场顺序；不截断，扩槽由 expand_workflow 负责）
      audios: [{"path":绝对路径, "name":角色名, "subject_index":int}, ...]（N 卡=N 音，一一对应）
      skipped: [{"name":卡名, "reason":跳过原因}, ...]（B1：无有效音色音频的卡报错跳过；
               scene/prop 无主图亦跳过上报——v1.59 杜绝"场景缺图静默"）

    优先级：payload 显式传的 ref_images/ref_audios（P0-6 字段层）→ 否则后端派生（裁定：前端无感）。
    后端派生（同源单次 pass，14:19 扩槽 A1 信标）：解析 prompt 的 <role>/<scene>/<prop>/<reference>
    标记 → 按前端 findAsset 口径（prefix + '-' + variant_name）查库，每张 @卡在**同一次 pass** 里
    同时取 images[-1]（当前主图）+ audios[0]（当前有效音色，批3-3 无论 role）→ 顺序生成 ref_image_i / ref_audio_j，
    保证 N 个 @卡 = N 图 = N 音一一对应。B1：某卡无有效音色音频 → 该卡报错跳过（图音都不注入，不静默）。

    v1.59（7-3 根因A，2026-09-06）：传入 pid/eid 时，匹配**按当前项目/集作用域优先**——
      取 scopes.projects 含 pid（进一步 episodes 含 eid）的同名卡，防"跨项目同名角色/场景"串卡
      （曾命中他项目残留孤儿无资卡 → 参考不注入 → H3 乱语/场景错/风格错）。
      本项目无同名卡 → 回退全局匹配并告警（防串卡兜底，不静默）。
    """
    shared = get_shared_assets_path()
    images, audios, skipped, subjects, rel_images = [], [], [], [], []

    if prefer_ref_images or prefer_ref_audios:
        if prefer_ref_images:
            # 显式传入的 ref_images 本身即相对 SharedAssets 的相对路径（ASCII 名）
            images = [_abs(p, shared) for p in prefer_ref_images]
            rel_images = list(prefer_ref_images)
        if prefer_ref_audios:
            audios = [{
                "path": _abs(a.get("path", ""), shared),
                "name": a.get("name") or a.get("subject") or "",
                "subject_index": int(a.get("subject_index", 0) or 0),
            } for a in prefer_ref_audios]
        # B 批 N2：rel_images 与 images 同步——只回传"实际注入"的参考图相对路径
        return images, audios, skipped, subjects, rel_images

    seen = set()
    for kind, name in _MARK_RE.findall(prompt or ""):
        nm = name.strip()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        items = list_items(_KIND_OF[kind])
        # v1.59 匹配口径 = 前端 findAsset：prefix + '-' + variant_name（去尾连字符）
        def _match_key(it: dict) -> bool:
            return _asset_key(it) == nm or (it.get("prefix") or "").strip() == nm
        hit = None
        if pid:
            # 优先命中当前项目/集的卡（防跨项目同名卡串卡，7-3 根因A）
            in_proj = [it for it in items if pid in ((it.get("scopes") or {}).get("projects") or [])
                       and _match_key(it)]
            if eid:
                in_ep = [it for it in in_proj if eid in ((it.get("scopes") or {}).get("episodes") or [])]
                if in_ep:
                    hit = in_ep[0]
            if hit is None and in_proj:
                hit = in_proj[0]
            if hit is None:
                # 本项目无同名卡 → 回退全局匹配并告警（防串卡兜底，不静默）
                hit = next((it for it in items if _match_key(it)), None)
                if hit:
                    _log.warning(
                        "[resolve_references] 项目 pid=%s 无与「%s」同名的%s卡，回退全局命中 id=%s "
                        "(scopes.projects=%s) —— 若为非本项目卡请核对", pid, nm, kind,
                        hit.get("id"), (hit.get("scopes") or {}).get("projects"))
        else:
            hit = next((it for it in items if _match_key(it)), None)
        if not hit:
            skipped.append({"name": nm, "reason": "资产卡不存在"})
            continue
        imgs = hit.get("images") or []
        p = imgs[-1].get("path") if imgs else None      # images[-1]（与 6-13 取图口径一致）
        # v1.59（7-3 根因B）：scene/prop 需主图作 H3 参考——无主图则明确跳过上报（不再静默放任 H3 脑补场景）
        if kind != "role" and not p:
            skipped.append({"name": nm, "reason": f"卡「{nm}」无主图（{kind} 需主图作参考，H3 无图会脑补场景），该卡跳过"})
            continue
        voice = None
        if kind == "role":                              # 仅人物有音色
            # 批3-3（用户裁定）：克隆取源 = "当前有效音色" = audios 列表首条（无论 role 标签）。
            # 资产卡 audios 语义：saveMeta 按 added_at 降序存、历史弹窗手动选中亦置 [0]，
            # 故 audios[0] = 时间最新或用户手动选中的"当前有效"，即为 H3 音色参考。
            # 原实现仅取 role=voice_sample，用户指正"reference 不参与克隆是错误概念"——现改一视同仁。
            _cur = next((a for a in (hit.get("audios") or []) if a.get("path")), None)
            if _cur:
                voice = _cur["path"]
            # B1（语义保留）：无任何有效音色音频 → 该卡报错跳过（图音都不注入，不静默、不幻觉填音）
            if not voice:
                skipped.append({"name": nm, "reason": f"卡「{nm}」无有效音色音频（B1），该卡跳过"})
                continue
        img_idx = len(images)
        if p:
            images.append(_abs(p, shared))
            rel_images.append(p.lstrip("/"))     # 相对路径（assets_index 口径）与 images 同步
        aud_idx = None
        if voice:
            aud_idx = len(audios)
            audios.append({"path": _abs(voice, shared), "name": nm, "subject_index": 0})
        # v1.14 主体有序记录（供 Ref2VA prompt 构造：<Subject N>/<Audio N> 映射 + 视觉描述保身份）
        if p or voice:
            # v1.17（09-02 H3六段式）：desc 用英文 gen_prompt 优先（H3 六段式要求英文 Picture 描述，
            #   否则中文描述导致 H3 无法理解 Picture 身份 → 乱语/串扰，见 H3代码修改实施指南 §1.3/六）。
            #   资产卡 images[-1].gen_prompt 是生成该图的英文 prompt，与图像最一致；无 gen_prompt 退回中文 prompt。
            _gen = (imgs[-1].get("gen_prompt") or "").strip() if imgs else ""
            _desc = _gen or (hit.get("prompt") or "").strip()
            subjects.append({"name": nm, "kind": kind,
                             "short": (hit.get("prefix") or "").strip() or nm,
                             "img": img_idx if p else None, "aud": aud_idx,
                             "desc": _desc})

    # <reference>path</reference> 直接路径（参考类资产/用户上传参考图）
    for rp in _REF_RE.findall(prompt or ""):
        rp = rp.strip()
        if rp:
            images.append(_abs(rp, shared))
            rel_images.append(rp.lstrip("/"))     # 相对路径与 images 同步

    # v1.17 返回前排序：人物(role)在前、场景(scene)在后、道具(prop)最后（H3六段式/测试H人物在前配置）。
    #   同步重建 images/audios 与 subjects 的 img/aud 索引，保证 ref_image_0=人物1, ref_image_1=人物2,
    #   ref_image_2=场景 与 subjects 顺序、expand_workflow 注入严格一致（防串扰）。
    def _kind_rank(k):
        return {"role": 0, "scene": 1, "prop": 2}.get(k, 3)
    if len(subjects) > 1:
        _order = sorted(range(len(subjects)), key=lambda i: (_kind_rank(subjects[i]["kind"]), subjects[i]["name"]))
        _subj_ordered = [subjects[i] for i in _order]
        _name_img = {subjects[i]["name"]: images[subjects[i]["img"]] for i in range(len(subjects)) if subjects[i].get("img") is not None}
        _name_rel = {subjects[i]["name"]: rel_images[subjects[i]["img"]] for i in range(len(subjects)) if subjects[i].get("img") is not None}
        _name_aud = {subjects[i]["name"]: audios[subjects[i]["aud"]] for i in range(len(subjects)) if subjects[i].get("aud") is not None}
        _imgs2, _auds2, _rels2, _img_cnt, _aud_cnt = [], [], [], 0, 0
        for s in _subj_ordered:
            if s.get("img") is not None:
                _imgs2.append(_name_img[s["name"]]); s["img"] = _img_cnt; _img_cnt += 1
            if s.get("aud") is not None:
                _auds2.append(_name_aud[s["name"]]); s["aud"] = _aud_cnt; _aud_cnt += 1
        _rels2 = [_name_rel[s["name"]] for s in _subj_ordered if s.get("img") is not None]
        images, audios, subjects, rel_images = _imgs2, _auds2, _subj_ordered, _rels2

    return images, audios, skipped, subjects, rel_images   # 不截断：扩槽由 expand_workflow 动态处理（A1）


def _ascii_name(path: str) -> str:
    """生成 ASCII 上传文件名（避免中文名触发 ComfyUI Custom validation failed）。"""
    ext = os.path.splitext(path)[1].lower() or ".png"
    return f"ref_{new_id('')}{ext}"


def _upload(comfy_base: str, abs_path: str, field: str, ascii_name: str) -> str:
    """上传单文件到 ComfyUI。统一走 /upload/image（实测本远程 ComfyUI 无 /upload/audio→405），
    files key 一律 'image'（该端点接受任意文件存入 input 目录，音频/图片通用）。"""
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)
    with open(abs_path, "rb") as f:
        data = f.read()
    try:
        r = requests.post(f"{comfy_base}/upload/image",
                          files={"image": (ascii_name, data, "application/octet-stream")},
                          timeout=120)
    except requests.RequestException as e:
        raise RuntimeError(f"上传网络错误: {abs_path} ({e})")
    if r.status_code == 200:
        try:
            name = (r.json() or {}).get("name") or ascii_name
        except ValueError:
            name = ascii_name
        return name
    raise RuntimeError(f"上传失败: {abs_path} ({r.status_code}:{r.text[:120]})")


def upload_ref_assets(comfy_base: str, images, audios):
    """上传参考图 + 音频（全部）。返回 (img_names, audio_names)。
    任一音频上传失败 → 抛"音频…"错误由 generate_video 捕获降级 native（保持图音不混）。"""
    img_names, audio_names = [], []
    for p in images:
        try:
            img_names.append(_upload(comfy_base, p, "image", _ascii_name(p)))
        except Exception as e:
            # 单个图失败不阻断其余；记录由调用方进度回调
            raise RuntimeError(f"参考图上传失败 {p}: {e}")
    for a in audios:
        try:
            audio_names.append(_upload(comfy_base, a["path"], "audio", _ascii_name(a["path"])))
        except Exception as e:
            # 音频 best-effort：任一失败 → 降级 native（不注入任何音频，保持 N 图 / 0 音，不破坏对齐）
            raise RuntimeError(f"音频上传失败 {a.get('path')}: {e}")
    return img_names, audio_names


def _next_node_id(wf: dict) -> str:
    """工作流下一个可用数字节点 id（max+1 顺延，保证不与现有冲突）。"""
    mx = 0
    for k in wf.keys():
        if k.isdigit():
            mx = max(mx, int(k))
    return str(mx + 1)


def _ensure_load_nodes(wf: dict, class_type: str, existing_ids, needed: int, input_key: str):
    """A1 节点复制法：确保有 needed 个 LoadImage/LoadAudio 节点。

    existing_ids：工作流原生节点 id 列表（如 LoadImage 的 362/364/365、LoadAudio 的 361）。
    不足则按 id 顺延克隆新节点（class_type/input 结构同源）。返回足额节点 id 列表。
    """
    nodes = [nid for nid in existing_ids if wf.get(nid, {}).get("class_type") == class_type]
    while len(nodes) < needed:
        nid = _next_node_id(wf)
        wf[nid] = {"class_type": class_type, "inputs": {input_key: ""}}
        nodes.append(nid)
    return nodes[:needed]


def expand_workflow(wf: dict, img_names, audio_names):
    """A1 扩槽：按 N 图 N 音动态扩展 363 参考槽（节点复制法），并接线。

    - 图槽：ref_images.ref_image_0/1/2… → LoadImage 节点（原生 362/364/365，不足克隆顺延）
    - 音槽：ref_audios.ref_audio_0/1/2… → LoadAudio 节点（原生 361，不足克隆顺延）
    - audio_mode：一律 native（v1.14e 起，参考用户可工作的 T8 配置）；v1.15b 起 native 不设 drive_audio（防重放乱语）
    返回 (used_audio, unused_ref_keys)：是否注入音频 + 363 中残留的旧 ref 键（供清理）。
    """
    cond = wf.setdefault(COND_NODE, {})
    inp = cond.setdefault("inputs", {})

    # 图槽：按需扩展 + 接线
    img_nodes = _ensure_load_nodes(wf, "LoadImage", REF_IMAGE_NODES, len(img_names), "image")
    for i, nm in enumerate(img_names):
        wf[img_nodes[i]].setdefault("inputs", {})["image"] = nm
        inp[f"ref_images.ref_image_{i}"] = [img_nodes[i], 0]

    # 音槽：按需扩展 + 接线（A1：N 说话人 = N 个 ref_audios 槽，全量注入，不做选片）
    used_audio = False
    if audio_names:
        aud_nodes = _ensure_load_nodes(wf, "LoadAudio", [REF_AUDIO_NODE], len(audio_names), "audio")
        for j, an in enumerate(audio_names):
            wf[aud_nodes[j]].setdefault("inputs", {})["audio"] = an
            inp[f"ref_audios.ref_audio_{j}"] = [aud_nodes[j], 0]
        # v1.14e 切换 audio_mode=native（参考用户可工作的 T8 配置；ref_audios 仍按音色克隆）。
        # v1.15b 乱语根因修复：native 模式**不得设置 drive_audio**——模板默认 drive_audio=None，
        # h3 单测（TC1/2/3 无 drive_audio）无乱语；工坊此前显式注入 drive_audio=[外卖员音色样本]，
        # native 下 H3 仍重放 drive_audio 内容 → 男人（第1说话人）开头 2-3s 乱语（用户 09-01 两次复测实证）。
        # 音色克隆走 ref_audios（native 下也被编码进 real_ref_blocks 作音色参考，源码 conditioning.py L283-287），
        # 无需 drive_audio。若音色失效回退 reference_only 时才需 drive_audio（届时临时加回）。
        inp["audio_mode"] = AUDIO_MODE_NATIVE
        inp.pop("drive_audio", None)
        used_audio = True
    else:
        inp.pop("drive_audio", None)
        inp["audio_mode"] = AUDIO_MODE_NATIVE

    # v1.14d：add_source_as_reference=False（v1.15b 起无 drive_audio，此开关已无同文件重放场景；
    # 音色克隆仍走 ref_audios，若音色失效回退 reference_only 时再评估）
    inp["add_source_as_reference"] = False

    # 返回 363 中残留的旧 ref 键（长度超过当前 N 的槽位，供调用方清理）
    ref_keys = [k for k in inp if k.startswith("ref_images.ref_image_") or k.startswith("ref_audios.ref_audio_")]
    return used_audio, ref_keys


def _set_cond_params(wf: dict, payload, prompt_override: str | None = None,
                     duration_override: float | None = None) -> None:
    """设置 363 主参数：prompt(339) / 尺寸(340) / 时长(350→351 原生链路)。

    时长修复（0.2s 缺陷根因）：H3 363 的 length 单位=帧数，且默认经 351(ComfyMathExpression)
    由 350(PrimitiveFloat Duration, 秒) 换算（×24 对齐 17 的倍数）。此前直写 363.length=秒 被
    当作帧数注入，导致只生成数帧。现改为写 350.inputs.value=秒，保留原生换算链路。
    """
    # prompt：直接写到 339 value（覆盖默认 H3 模板；prompt_override 用于 Ref2VA 构造后的提示词）
    wf.setdefault(PROMPT_NODE, {}).setdefault("inputs", {})["value"] = (
        prompt_override if prompt_override is not None else (payload.prompt or ""))
    # 尺寸：写到 ResolutionSelector 340（aspect_ratio / megapixels）
    # v1.14 比例短格式→工作流全称归一（'16:9' 不在 340 COMBO 选项会 node_errors，防御任何调用方传短格式）
    _RATIO_FULL = {
        "1:1": "1:1 (Square)", "2:3": "2:3 (Portrait Photo)", "3:2": "3:2 (Photo)",
        "3:4": "3:4 (Portrait Standard)", "4:3": "4:3 (Standard)",
        "9:16": "9:16 (Portrait Widescreen)", "16:9": "16:9 (Widescreen)", "21:9": "21:9 (Ultrawide)",
    }
    sz = wf.setdefault(SIZE_NODE, {}).setdefault("inputs", {})
    if payload.aspect_ratio:
        sz["aspect_ratio"] = _RATIO_FULL.get(str(payload.aspect_ratio).strip(), payload.aspect_ratio)
    if payload.megapixels:
        sz["megapixels"] = payload.megapixels
    # 时长：写 350(PrimitiveFloat Duration, 单位=秒)，由 351 原生换算为帧数注入 363.length
    # v1.14 duration_override：按台词量自适应加长（多角色对话给足时长，避免"说完就结束"）
    dur = float(duration_override if duration_override is not None else (payload.duration or 3.0))
    # v1.16：业务层硬上限收紧 15→10s（乱语根因·H3 duration>台词时长即念 Picture 定义）。
    #   H3 policy 官方支持 2~15s，但我们业务层统一封顶 10s，杜绝"多余时间让 H3 脑补"。
    dur = max(2.0, min(10.0, dur))
    wf.setdefault("350", {}).setdefault("inputs", {})["value"] = dur


def submit_workflow(wf: dict, comfy_base: str) -> str:
    """POST /prompt。显式捕获 node_errors（不静默）。返回 prompt_id。"""
    client_id = f"storyboard_{new_id('')}"
    try:
        r = requests.post(f"{comfy_base}/prompt",
                          json={"prompt": wf, "client_id": client_id}, timeout=120)
    except requests.RequestException as e:
        raise RuntimeError(f"提交工作流网络错误: {e}")
    body = r.json() if r.content else {}
    if r.status_code != 200:
        raise RuntimeError(f"提交工作流失败({r.status_code}): {body.get('error') or r.text[:200]}")
    # ⚠️ 红线：node_errors 必捕获
    node_errors = body.get("node_errors")
    if node_errors:
        raise RuntimeError("工作流节点校验失败: " + json.dumps(node_errors, ensure_ascii=False)[:500])
    pid = body.get("prompt_id")
    if not pid:
        raise RuntimeError(f"未返回 prompt_id: {body}")
    return pid


def _find_video(outputs: dict):
    """从 history outputs 找视频产物（优先 VHS_VideoCombine 360，兜底任意 videos/gifs）。"""
    order = [VIDEO_OUT_NODE] + [k for k in outputs.keys() if k != VIDEO_OUT_NODE]
    for nid in order:
        node_out = outputs.get(nid) or {}
        for key in ("videos", "gifs"):
            for it in (node_out.get(key) or []):
                fn = it.get("filename")
                if fn and str(fn).lower().endswith((".mp4", ".webm", ".mov", ".m4v")):
                    return {"filename": fn, "type": it.get("type") or "output",
                            "subfolder": it.get("subfolder") or ""}
    return None


def poll_history(prompt_id: str, comfy_base: str, timeout: int = 600, cancel_check=None):
    """轮询 /history/{prompt_id}。识别 status_str=='error' 抛错；成功返回视频 item。

    ★ ② 步4-② 新增可选 `cancel_check`：以 1s 为粒度调用，命中即抛 `ComfyCancelled`。
      此前是**无取消感知的阻塞轮询**（step=5s）：被 `/interrupt` 后若 `/history` 恒空（stub 语义），
      会一直静默等到 timeout —— motion 域传入 1200s，**用户点了取消要等 20 分钟才收尾**。
      不传 `cancel_check` 时行为与原先完全一致（5s 一跳）。
    """
    waited = 0
    step = 5
    tick = 1 if cancel_check is not None else step      # 有取消检查 → 细粒度 1s；否则保持原节奏
    while waited < timeout:
        if cancel_check is not None and cancel_check():
            raise ComfyCancelled("用户取消")
        try:
            h = requests.get(f"{comfy_base}/history/{prompt_id}", timeout=30).json()
        except (requests.RequestException, ValueError):
            pass                                        # 网络抖动：落到底部 sleep 后重试（与原先一致）
        else:
            rec = (h or {}).get(prompt_id)
            if rec:
                status = (rec.get("status") or {}).get("status_str") or ""
                if status == "error":
                    msgs = (rec.get("status") or {}).get("messages") or []
                    raise RuntimeError(f"ComfyUI 任务失败: {msgs}")
                item = _find_video(rec.get("outputs") or {})
                if item:
                    return item
        for _ in range(tick):
            time.sleep(1)
            waited += 1
            if cancel_check is not None and cancel_check():
                raise ComfyCancelled("用户取消")
    raise RuntimeError(f"ComfyUI 任务轮询超时（>{timeout}s）")


def fetch_output(item: dict, comfy_base: str) -> bytes:
    """GET /view 精确拉取视频字节（不模糊兜底）。"""
    params = {"filename": item["filename"], "type": item["type"], "subfolder": item["subfolder"]}
    try:
        r = requests.get(f"{comfy_base}/view", params=params, timeout=300)
    except requests.RequestException as e:
        raise RuntimeError(f"拉取视频网络错误: {e}")
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"拉取视频失败({r.status_code})：{item}")
    return r.content


def generate_video(payload, progress_cb=None, base: str | None = None,
                   cancel_check=None, meta_cb=None):
    """编排：加载工作流 → 派生参考（A1 同源 N 图 N 音）→ 上传 → 扩槽 → 提交 → 轮询 → 拉取。
    返回 (video_bytes, meta)。meta={prompt_id, ref_images, rel_ref_images, ref_audios, used_audio, skipped, post_check}。
    rel_ref_images = 实际注入参考图的相对路径列表（B 批 N2 记账用，与 ref_images 计数同源）。

    v1.20（B-20260907-01）新增流水线（规格 §2/§4/§5/§6）：
      组装层出口唯一 → 结构校验（fail-closed）→ 审计落盘（含 wf_digest/model_name/refs_file_hash）
      → 提交/轮询/拉取（失败错误上下文入审计）→ 生成后自检（台词起始，fail-open）→ 审计补全。

    ★ ②步2遗留 注入口：base（生成节点地址，④ 唯一解析口注入）/ cancel_check（轮询取消感知）
      / meta_cb（提交成功回传 {"prompt_id"}，job 落 meta 供统一 cancel 精准打断）。
      三者缺省时行为与原先完全一致。
    """
    comfy_base = base or get_comfyui_url()

    def prog(m):
        if progress_cb:
            progress_cb(m)

    prog("加载工作流并解析参考资源…")
    wf = load_workflow(payload.workflow)
    wf_digest = _sha256_file(_wf_path(payload.workflow))   # v1.20 审计字段（规格 §5）
    images, audios, skipped, subjects, rel_images = resolve_references(
        payload.prompt, payload.ref_images, payload.ref_audios,
        getattr(payload, "pid", "") or "", getattr(payload, "eid", "") or "")

    # B1：无有效音色音频的卡已跳过（resolve_references 记录）。全部被跳过 / 无可用参考 → 明确报错
    # v1.59（7-3 根因C）：报错带具体原因（角色缺音色 / 场景道具缺主图），中止生成，不降级、不静默。
    if not images and not audios:
        reasons = [f"{s.get('name')}: {s.get('reason')}" for s in skipped]
        detail = "；".join(reasons) if reasons else "场文本未 @ 引用任何有参考的资产"
        raise RuntimeError(f"无可用参考资源，已中止生成：{detail}。"
                           "请先为角色绑定音色、为场景/道具生成主图后再试")

    # v1.20 组装层（出口唯一，映射#6 骨架保留）：六段式构造 + desc 清洗（映射#7）+ 负面约束注入
    h3_prompt, build_info = build_h3_prompt(payload.prompt, subjects)
    # v1.14 时长自适应：<d> 台词字数 → 至少 4 字/秒 + 2.5s 缓冲（多角色对话给足时长）
    # v1.16（09-02 乱语根因·时长收紧，依据 H3根因报告4.1 + 用户"删场景即不乱语"实验）：
    #   H3 的 TTS 在 duration > 台词所需时长时会**把 Picture 定义中的描述当台词念出**（乱语）。
    #   实测：删场景（无 Picture 定义可念）→ 不乱语；加回场景 + duration 11.25s → 乱语。
    #   修复：硬上限 15→10s，且按报告建议"字数/5+2"（35 字≈9s）收紧，杜绝"多余时间"让 H3 脑补。
    # v1.17→v1.20（映射#3）：台词格式 <d>[Chinese]（H3六段式），时长复用 compute_duration（≤10s）。
    _dlg = compute_duration(h3_prompt)
    dur_override = min(10.0, max(float(payload.duration or 3.0), _dlg)) if _dlg else None

    # v1.20 结构校验层（模块7，fail-closed，规格 §2/§4）：不通过 → 拒绝提交，审计留痕
    struct = validate_structure(images, audios, subjects,
                                build_info.get("n_speakers", 0), h3_prompt)
    if not struct["pass"]:
        audit_submit({"workflow": payload.workflow, "wf_digest": wf_digest,
                      "prompt_hash": hashlib.sha256(h3_prompt.encode("utf-8")).hexdigest()[:16],
                      "prompt": h3_prompt, "structure_check": struct,
                      "error": "结构校验不通过：" + "；".join(struct["errors"])})
        raise RuntimeError("结构校验不通过：" + "；".join(struct["errors"]))

    # v1.20 审计层（模块5，规格 §5）：提交参数全量落盘（含 wf_digest/model_name/refs_file_hash）
    _model_parts = []
    for _nid, _node in wf.items():
        if isinstance(_node, dict):
            for _k, _v in (_node.get("inputs") or {}).items():
                if _k in ("ckpt_name", "lora_name", "model_name", "unet_name") and isinstance(_v, str) and _v:
                    _model_parts.append(f"{_k}={_v}")
    _refs_h = hashlib.sha256()
    for _p in list(images) + [a.get("path", "") for a in audios]:
        _refs_h.update(str(_p).encode("utf-8", "ignore"))
        _refs_h.update(_sha256_file(_p).encode("ascii", "ignore"))
    audit_rec = {
        "workflow": payload.workflow,
        "wf_digest": wf_digest,
        "model_name": ";".join(sorted(set(_model_parts))) or "minimax_h3_t8(workflow-inline)",
        "prompt_hash": hashlib.sha256(h3_prompt.encode("utf-8")).hexdigest()[:16],
        "prompt": h3_prompt,
        "seed": getattr(payload, "seed", None),
        "duration": dur_override if dur_override is not None else float(payload.duration or 3.0),
        "ref_images": list(rel_images or []),
        "ref_audios": [a.get("path") for a in audios],
        "refs_file_hash": _refs_h.hexdigest()[:32],
        "n_speakers": build_info.get("n_speakers", 0),
        "structure_check": struct,
        "skipped": skipped,
        "transient_clean": None,
        "post_check": None,
        "error": None,
    }

    try:
        # 上传参考图（必须）；音频 best-effort（失败降级 native）
        prog(f"上传参考资源（图{len(images)}/音{len(audios)}）…")
        audio_ok = True
        try:
            img_names, audio_names = upload_ref_assets(comfy_base, images, audios)
        except Exception as e:
            # 音频上传失败 → 降级无音色
            if audios and "音频" in str(e):
                img_names, audio_names, audio_ok = _upload_images_only(comfy_base, images), [], False
                prog(f"音频上传失败，降级无音色：{e}")
            else:
                raise

        used_audio, _ref_keys = expand_workflow(wf, img_names, audio_names)
        # v1.15c（09-02 乱语真根因修复·空槽清理）：363 原生 3 个 ref_images 槽位（ref_image_0/1/2），
        # 若实际只注入 N 张图，超出 N 的槽位（如 ref_image_2）仍指向空 LoadImage（image=""）→
        # H3 收到"3 槽位 N 图"结构不一致，把 prompt 里游离场景描述当台词念出（乱语）。
        # expand_workflow 返回 ref_keys（全部 ref 键），这里按实际注入数删除残留空槽。
        _cond_inp = wf.setdefault(COND_NODE, {}).setdefault("inputs", {})
        for _k in list(_cond_inp.keys()):
            if _k.startswith("ref_images.ref_image_"):
                _idx = int(_k.split("_")[-1])
                if _idx >= len(img_names):
                    _cond_inp.pop(_k, None)
            elif _k.startswith("ref_audios.ref_audio_"):
                _idx = int(_k.split("_")[-1])
                if _idx >= len(audio_names):
                    _cond_inp.pop(_k, None)
        _set_cond_params(wf, payload, h3_prompt, dur_override)

        prog(f"提交 ComfyUI 任务（参考图{len(img_names)}张/音频{len(audio_names)}个）…")
        prompt_id = submit_workflow(wf, comfy_base)
        if meta_cb:
            meta_cb({"prompt_id": prompt_id})  # ★ 步2遗留：尽早暴露 prompt_id（统一取消依赖它）

        prog("轮询生成进度（通常 30-90s）…")
        item = poll_history(prompt_id, comfy_base, cancel_check=cancel_check)

        prog("拉取视频产物…")
        data = fetch_output(item, comfy_base)
        if not data:
            raise RuntimeError("视频产物为空")
    except Exception as e:
        # v1.20（规格 §4）：任务 error/超时 fail-closed，错误上下文保留入审计后原样上抛
        audit_rec["error"] = str(e)[:500]
        audit_submit(audit_rec)
        raise

    # v1.21（B-20260907-02，用户裁定替换 v1.20a）归档链路条件化瞬态清理：
    #   消除 H3 引擎级起始瞬态（"切"音），条件化靶向、干净样本零改动、不伤台词。
    prog("开头瞬态条件化清理（clean_head_transient）…")
    data, tc_note = clean_head_transient(data)
    audit_rec["transient_clean"] = tc_note
    if tc_note.get("action") == "fallback" and tc_note.get("note"):
        prog(f"瞬态清理降级：{tc_note['note']}")

    # v1.20 自检层（模块6，fail-open，规格 §4/§6）：台词起始能量检测（>2s 告警 = 7-4 切音模式）
    prog("生成后自检（台词起始能量检测）…")
    import tempfile as _tf
    _fd, _tmp_mp4 = _tf.mkstemp(suffix=".mp4")
    os.close(_fd)
    try:
        with open(_tmp_mp4, "wb") as _f:
            _f.write(data)
        post_check = post_check_head(_tmp_mp4)
    finally:
        try:
            os.remove(_tmp_mp4)
        except OSError:
            pass
    audit_rec["post_check"] = post_check
    if post_check.get("warning"):
        prog(f"自检提示：{post_check['warning']}")
    _rules_now = _load_guard_rules()
    if (_rules_now.get("block_on_speech_delay") and post_check.get("speech_start_s") is not None
            and post_check["speech_start_s"] > float(_rules_now.get("speech_start_threshold_s") or 2.0)):
        audit_rec["error"] = "自检阻断: " + str(post_check.get("warning"))
        audit_submit(audit_rec)
        raise RuntimeError("自检阻断（block_on_speech_delay=true）：" + str(post_check.get("warning")))
    audit_submit(audit_rec)

    # B 批 N2 修复：meta 回传"实际注入参考图的相对路径"(rel_images)，供 main.py _archive_video 记账。
    #   仅回传相对路径不破解耦(comfy_client 不 import assets_index)；优先显式 ref_images，回退派生相对路径。
    meta = {"prompt_id": prompt_id, "ref_images": len(img_names),
            "rel_ref_images": rel_images or list(payload.ref_images or []),
            "ref_audios": len(audio_names), "used_audio": used_audio,
            "skipped": skipped, "post_check": post_check}
    return data, meta


# ═══════════════════════════════════════════════════════════════════════
# 阶段4（B-20260908-03 · IC 工作流工坊化）生图直连编排 · generate_image
# 依据《阶段4后端实施摸底清单-B-20260908-03-20260909.md》+ IC /api/generate 契约
#   （绕 IC，直连 ComfyUI；params 覆盖机制复刻 IC main.py:18233）
# ═══════════════════════════════════════════════════════════════════════

def _resolve_img_abs(p: str) -> str:
    """图像路径 → 绝对路径（相对 SharedAssets 转绝对；已是绝对/http 原样）。"""
    if p.startswith(("http://", "https://", "/")):
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(get_shared_assets_path(), p.lstrip("/"))


def _apply_params(wf: dict, params: dict | None) -> None:
    """IC params 覆盖机制重写（复刻 IC main.py:18233）：params={节点ID:{字段:值}}。
    命中节点则覆盖其 inputs；字段值=None 则删除该字段（pop）。仅覆盖已存在节点。
    阶段4 生图工作流节点已固化，不做"新增节点"分支（与 IC 差异：IC 支持新增，这里拒绝未知节点防误改）。"""
    if not params:
        return
    for node_id, node_inputs in params.items():
        node = wf.get(str(node_id))
        if not isinstance(node, dict):
            continue  # 未知节点忽略（防误改）
        inp = node.setdefault("inputs", {})
        for fname, fval in (node_inputs or {}).items():
            if fval is None:
                inp.pop(fname, None)
            else:
                inp[fname] = fval


def _upload_params_images(comfy_base: str, params: dict | None) -> None:
    """把 params 里所有指向**本地/相对文件**的图片字段自动上传到 ComfyUI input，
    回填为 ASCII 名（ComfyUI input 目录内已有名/http URL 则跳过，不重复上传）。
    识别字段：input 值含图片路径的字段名 key（image / images / filename / 参考图等）或值以图片后缀结尾。
    内联修改 params（原地替换为 ASCII 名），供 _apply_params 覆盖。"""
    if not params:
        return
    _seen = {}
    for node_id, node_inputs in params.items():
        if not isinstance(node_inputs, dict):
            continue
        for fname, fval in list(node_inputs.items()):
            if not isinstance(fval, str) or not fval:
                continue
            ext = os.path.splitext(fval)[1].lower()
            is_img_val = ext in _IMG_EXT
            is_img_field = any(tok in str(fname).lower() for tok in
                               ("image", "images", "filename", "img", "ref_"))
            if not (is_img_field or is_img_val):
                continue
            # http URL 或纯 ASCII 名(ComfyUI 已持有，非本地文件路径)跳过上传
            if fval.startswith(("http://", "https://", "/api/")) or _is_comfy_held_name(fval):
                continue
            ap = _resolve_img_abs(fval)
            if not os.path.exists(ap):
                continue  # 本地不存在则跳过（由端点层保证 ref 有效）
            if ap in _seen:
                node_inputs[fname] = _seen[ap]
            else:
                up = _upload(comfy_base, ap, "image", _ascii_name(ap))
                _seen[ap] = up
                node_inputs[fname] = up


def _is_comfy_held_name(name: str) -> bool:
    """粗略判断是否已是 ComfyUI input 目录内持有的 ASCII 文件名（无路径分隔 + 图片后缀）。"""
    if "/" in name or "\\" in name or os.path.sep in name:
        return False
    return os.path.splitext(name)[1].lower() in _IMG_EXT


def _find_image(outputs: dict):
    """从 history outputs 找图像产物：优先 SaveImage（type=output）的 images，
    兜底 PreviewImage（type=temp）的 images。返回 {filename, type, subfolder} 或 None。"""
    if not outputs:
        return None
    # 1) 优先 output 类型（SaveImage）
    for nid, node_out in outputs.items():
        for it in (node_out.get("images") or []):
            if it.get("filename") and (it.get("type") or "output") == "output":
                return {"filename": it["filename"], "type": "output",
                        "subfolder": it.get("subfolder") or ""}
    # 2) 兜底 temp（PreviewImage）
    for nid, node_out in outputs.items():
        for it in (node_out.get("images") or []):
            if it.get("filename"):
                return {"filename": it["filename"], "type": it.get("type") or "temp",
                        "subfolder": it.get("subfolder") or ""}
    return None


def poll_image_history(prompt_id: str, comfy_base: str, timeout: int = 600, cancel_check=None):
    """轮询 /history/{prompt_id} 直至图像产物就绪（区分 SaveImage/output 与 PreviewImage/temp）。
    status_str=='error' 抛错；超时抛错。返回图像 item。

    ★ ②步2遗留：可选 `cancel_check`（形态与 poll_history 一致，命中抛 ComfyCancelled）——
      统一编辑器生图入队后，job 靠它在轮询中感知取消（不传时行为与原先完全一致）。
    """
    waited = 0
    step = 5
    tick = 1 if cancel_check is not None else step
    while waited < timeout:
        if cancel_check is not None and cancel_check():
            raise ComfyCancelled("用户取消")
        try:
            h = requests.get(f"{comfy_base}/history/{prompt_id}", timeout=30).json()
        except (requests.RequestException, ValueError):
            time.sleep(step); waited += step; continue
        rec = (h or {}).get(prompt_id)
        if rec:
            status = (rec.get("status") or {}).get("status_str") or ""
            if status == "error":
                msgs = (rec.get("status") or {}).get("messages") or []
                raise RuntimeError(f"ComfyUI 生图任务失败: {msgs}")
            if status == "success":
                item = _find_image(rec.get("outputs") or {})
                if item:
                    return item
                raise RuntimeError("ComfyUI 生图完成但未找到图像输出（工作流缺 SaveImage/PreviewImage 或未接 VAEDecode）")
        time.sleep(tick); waited += tick
    raise RuntimeError(f"ComfyUI 生图轮询超时（>{timeout}s）")


def fetch_image(item: dict, comfy_base: str) -> bytes:
    """GET /view 拉取图像字节（区分 type=temp/output）。"""
    params = {"filename": item["filename"], "type": item["type"], "subfolder": item["subfolder"]}
    try:
        r = requests.get(f"{comfy_base}/view", params=params, timeout=300)
    except requests.RequestException as e:
        raise RuntimeError(f"拉取图像网络错误: {e}")
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"拉取图像失败({r.status_code})：{item}")
    return r.content


def generate_image(workflow: str, params: dict | None = None,
                   progress_cb=None, base: str | None = None,
                   cancel_check=None, meta_cb=None):
    """阶段4 生图编排（直连 ComfyUI，绕 IC）。
      workflow  : 工作流名，如 'custom/Z-Image.json'（工坊 workflows/ 优先，回退 IC 目录）
      params    : {节点ID:{字段:值}} 覆盖（复刻 IC main.py:18233；值 None 则 pop）。
                  image 字段可传本地/相对 SharedAssets 路径——自动经 /upload/image 上传并回填
                  ASCII 名（ComfyUI input 已持有/http 则跳过）。width/height/seed/prompt 等由
                  main.py 端点按 workflow_type→节点模板注入（见摸底清单一/三 节点映射）。
      base        : 生成节点地址（★ ④ 步4-④：缺省=本机；多节点时由 task_queue.resolve_node_url 注入）。
      cancel_check: 轮询中的取消自检（命中抛 ComfyCancelled；步2遗留：生图入队后接 is_cancelled）。
      meta_cb     : 提交成功后回调 {"prompt_id": ...}（★ 步2遗留：job 据此落 meta.prompt_id，
                    统一 cancel 才能精准 delete+interrupt）。
    返回 (image_bytes, meta{prompt_id, filename})。"""
    comfy_base = base or get_comfyui_url()

    def prog(m):
        if progress_cb:
            progress_cb(m)

    prog("加载生图工作流…")
    wf = load_workflow(workflow)

    _p = dict(params or {})
    # params 内嵌 image（本地/相对路径）→ 上传回填 ASCII 名；再按节点覆盖到工作流
    _upload_params_images(comfy_base, _p)
    _apply_params(wf, _p)

    prog("提交 ComfyUI 生图任务…")
    prompt_id = submit_workflow(wf, comfy_base)
    if meta_cb:
        meta_cb({"prompt_id": prompt_id})      # ★ 步2遗留：尽早暴露 prompt_id（统一取消依赖它）

    prog("轮询生图进度（首次调用含模型加载，约 1-2 分钟）…")
    item = poll_image_history(prompt_id, comfy_base, cancel_check=cancel_check)

    prog("拉取图像产物…")
    data = fetch_image(item, comfy_base)
    if not data:
        raise RuntimeError("生图产物为空")
    meta = {"prompt_id": prompt_id, "filename": item.get("filename"),
            "type": item.get("type")}
    return data, meta


def _upload_images_only(comfy_base: str, images):
    names = []
    for p in images:
        names.append(_upload(comfy_base, p, "image", _ascii_name(p)))
    return names


# ═══════════════════════════════════════════════════════════════════════
# 阶段5-4（B-20260908-03）GS 高斯泼溅：单图 → SHARP → .ply（真机验证 2026-09-09 通过）
# 工作流 custom/sharp_gs.json（源自 gsplat-preview/sharp_workflow_api.json，用户提供）：
#   LoadImage(1) → LoadSharpModel(2) → SharpPredict(3, focal_length_mm=30.0) → SaveText(4, format=txt)
#   SharpPredict 把 .ply 写入 ComfyUI output，其 ply_path(STRING) 输出 = ply 完整绝对路径；
#   SaveText 将 ply_path 落成 txt（history outputs[节点4].text 即该路径，basename=ply 文件名）。
# ═══════════════════════════════════════════════════════════════════════
def _poll_sharp_done(prompt_id: str, comfy_base: str, timeout: int = 600) -> dict:
    """专用轮询：等到 history status_str==success/error。成功返回 {outputs, ply_abs, ply_name}。
    SHARP 产物非视频/图像（无 _find_video 可等），按任务终态判断。"""
    waited, step = 0, 5
    import requests as _rq
    while waited < timeout:
        try:
            h = _rq.get(f"{comfy_base}/history/{prompt_id}", timeout=30).json()
        except Exception:
            time.sleep(step); waited += step; continue
        rec = (h or {}).get(prompt_id)
        if rec:
            st = ((rec.get("status") or {}).get("status_str") or "").lower()
            if st == "error":
                msgs = (rec.get("status") or {}).get("messages") or []
                raise RuntimeError("SHARP 任务失败: " + json.dumps(msgs, ensure_ascii=False)[:500])
            if st == "success":
                outputs = rec.get("outputs") or {}
                return _resolve_sharp_ply(outputs)
        time.sleep(step); waited += step
    raise RuntimeError(f"SHARP 任务轮询超时（>{timeout}s）")


def _resolve_sharp_ply(outputs: dict) -> dict:
    """从 history outputs 解析 SHARP 生成的 ply 文件名。
    SaveText 节点(本工作流为节点 4) 的 'text' 输出 = ply 完整绝对路径（实测确认）。
    兼容：任一节点 text/files 里含 .ply 路径的字符串兜底。"""
    ply_abs = None
    # ① 遍历所有节点输出的 text（STRING 数组）
    for nid, node_out in (outputs or {}).items():
        texts = (node_out or {}).get("text") or []
        for t in texts:
            s = str(t).strip()
            if s and s.lower().endswith(".ply"):
                ply_abs = s
                break
        if ply_abs:
            break
    if not ply_abs:
        # ② 兜底：files 里若含 .ply（本工作流 SaveText 写的是 .txt，一般不会命中）
        for nid, node_out in (outputs or {}).items():
            for f in ((node_out or {}).get("files") or []):
                fn = (f or {}).get("filename") or ""
                if str(fn).lower().endswith(".ply"):
                    ply_abs = "output/" + str(fn)
                    break
            if ply_abs:
                break
    if not ply_abs:
        raise RuntimeError("SHARP 任务完成但未找到 ply 输出（history 无 .ply 路径）")
    ply_name = os.path.basename(ply_abs)
    return {"outputs": outputs, "ply_abs": ply_abs, "ply_name": ply_name}


def generate_sharp(ref_image: str, focal_length_mm: float = 30.0,
                   progress_cb=None):
    """阶段5-4 GS 编排：单图 → SHARP → .ply（直连远程 ComfyUI，绕 IC）。
      ref_image      : 参考图路径（相对 SharedAssets / 绝对 / http），ASCII 化上传。
      focal_length_mm: 相机焦距（方案 GS 规格默认 30.0）。
    返回 {"ply_name": "sharp_xxx.ply", "ply_abs": "...", "prompt_id": "..."}。"""
    comfy_base = base or get_comfyui_url()   # ★ ④ 步4-④：base 由 task_queue.resolve_node_url 注入（缺省=本机）

    def prog(m):
        if progress_cb:
            progress_cb(m)

    prog("加载 SHARP 工作流…")
    wf = load_workflow("custom/sharp_gs.json")

    prog("上传参考图…")
    abs_img = _resolve_img_abs(ref_image)
    ascii_name = _upload(comfy_base, abs_img, "image", _ascii_name(abs_img))
    wf["1"]["inputs"]["image"] = ascii_name          # LoadImage
    wf["3"]["inputs"]["focal_length_mm"] = float(focal_length_mm or 30.0)  # SharpPredict
    wf["4"]["inputs"]["format"] = "txt"              # SaveText 需显式 format（API 提交校验）

    prog("提交 ComfyUI SHARP 任务…")
    prompt_id = submit_workflow(wf, comfy_base)

    prog("SHARP 重建中（含模型加载，首个任务约 1-3 分钟）…")
    res = _poll_sharp_done(prompt_id, comfy_base)
    res["prompt_id"] = prompt_id
    return res

# ═══════════════════════════════════════════════════════════════════════
# v1.20（2026-09-07，B-20260907-01）H3 Prompt 归口管理 + 提交审计 · 七模块防御架构
# 依据《H3归口管理与提交审计-v1.20重构规格-20260907.md》§0-§9 落地：
#   模块1 清洗层 sanitize_desc / 模块2 台词层 strip_quote_lead+wrap_dialogue /
#   模块3 组装层 build_h3_prompt / 模块4 时长层 compute_duration /
#   模块5 审计层 audit_submit / 模块6 自检层 post_check_head /
#   模块7 结构校验层 validate_structure（fail-closed）。
# v1.19 六处已闭环逻辑按规格 §1 映射表逐条迁移，禁止回退。
# 失败路径（规格 §4）：数据完整性校验=fail-closed；观测性能力失败=fail-open 但显式告警。
# ═══════════════════════════════════════════════════════════════════════
# v1.21（2026-09-07，B-20260907-02）clean_head_transient 条件化靶向清理
# v1.21b（2026-09-07，用户 v9 目检通过）检测升级为"开头噪声带[0,band_end]"（从 0 起
#   到持续100ms全<0.5%首窗），清理静音到 mute_gain=0.02(-34dB)——正常样本开头0-30ms
#   =0.0%真静音基线，1.4%听感阈值仍可闻(用户 v8/D 版拍板)；gap=ts-band_end≥0.02 防早台词。
# v1.21c（2026-09-07）两段式定界：路径1 原100ms静音定界(00102类)；路径2=ts前满足
#   gap≥0.02 的最后>1.5%窗。经用户试听 + 细窗全轨，00123 实为"开头伪音字'下'(0.02-0.24s)+
#   2.8s静音+3.5s真台词"，非早台词瞬态——v1.21c 的 ts/gap 模型基于错误定性，被 v1.21d 废弃。
# v1.21d（2026-09-07，用户拍板：保护从严→从宽）删 ts 依赖/gap 早台词保护/peak<10% 剧情
#   音效保护，只保留触发"开头带峰值≥2.5%"；band_end=0~1s 内首个"其后连续100ms全<0.5%"
#   窗起点；连续台词片无100ms静音分割→自动skip（用户原话：误伤500-1000ms听不出）。
#   00123 伪音"下"(带后接2.8s静音)→applied 静音[0,0.25s]；00013 出牌声(孤立撞击带)也
#   applied（用户接受，违和再单独豁免）。
# ═══════════════════════════════════════════════════════════════════════

_H3_CODE_VERSION = "v1.21d"

# ── 黑名单/约束配置（规格 §7：配置化 + mtime 热更新；缺失/损坏回退内置默认 + 显式告警）──
_GUARD_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data", "h3_guard_rules.json")
_GUARD_CACHE = {"mtime": None, "rules": None}

# 构图后置词黑名单（7-4 切音·层8 根因）：这些词暗示 H3"先立场景、人物台词后置"→
# 开头伪音孤立暴露。默认词表来自第三方同 seed 对照实验（exp_00102）实证清单。
_DEFAULT_COMPOSITION_BLACKLIST = [
    {"pattern": r",\s*wide establishing shot", "replacement": ""},
    {"pattern": r",\s*establishing shot", "replacement": ""},
    {"pattern": r",\s*no people(?: inside)?", "replacement": ""},
    {"pattern": r",\s*empty scene", "replacement": ""},
    {"pattern": r",\s*no scenery", "replacement": ""},
    {"pattern": r",\s*no other people(?: in (?:the )?scene)?", "replacement": ""},
    {"pattern": r"\bwide establishing\b", "replacement": ""},
    {"pattern": r"\bestablishing shot\b", "replacement": ""},
]

# 负面约束统一清单（每条来自一次真实故障：乱语/多字/旁白/串扰/音色复制）
_DEFAULT_NEGATIVE_RULES = [
    "The character speaks ONLY the <d> dialogue line, in Chinese, exactly as written.",
    "No other speakers, no replies, no repetitions, no additional utterances, no narration.",
    "Do not read, speak, or narrate any Chinese text outside the <d> tags.",
    "Each subject keeps its own distinct visual identity throughout: faces, ages, genders, "
    "and appearances must NOT blend or swap between subjects.",
    "Do NOT copy any original spoken words from the reference audio; generate brand-new dialogue text.",
]

_DEFAULT_GUARD_RULES = {
    "composition_blacklist": _DEFAULT_COMPOSITION_BLACKLIST,
    "negative_rules": _DEFAULT_NEGATIVE_RULES,
    "speech_start_threshold_s": 2.0,
    "block_on_speech_delay": False,
    "desc_max_len": 600,
    "ffmpeg_path": "ffmpeg",
    "changelog": [
        {"version": "v1.20", "date": "2026-09-07",
         "note": "初始内置默认（构图后置词 8 条 + 负面约束 5 条），依据 v1.20 规格 §7"},
    ],
}


def _load_guard_rules() -> dict:
    """读 data/h3_guard_rules.json（mtime 缓存热更新，规格 §7）。
    缺失/损坏 → 回退内置默认 + 显式告警（fail-open，不静默）。"""
    try:
        mtime = os.path.getmtime(_GUARD_RULES_PATH)
    except OSError:
        if _GUARD_CACHE["rules"] is None:
            _log.warning("[h3_guard] 配置 %s 不存在，回退内置默认规则（fail-open）", _GUARD_RULES_PATH)
        return _DEFAULT_GUARD_RULES
    if _GUARD_CACHE["rules"] is not None and _GUARD_CACHE["mtime"] == mtime:
        return _GUARD_CACHE["rules"]
    try:
        with open(_GUARD_RULES_PATH, "r", encoding="utf-8") as f:
            rules = json.load(f)
        if not isinstance(rules.get("composition_blacklist"), list):
            raise ValueError("composition_blacklist 非列表")
        if not isinstance(rules.get("negative_rules"), list):
            raise ValueError("negative_rules 非列表")
        _GUARD_CACHE.update(mtime=mtime, rules=rules)
        _log.info("[h3_guard] 配置已加载/热更新：%s", _GUARD_RULES_PATH)
        return rules
    except Exception as e:
        _log.error("[h3_guard] 配置加载失败（%s），回退内置默认规则（fail-open）：%s",
                   _GUARD_RULES_PATH, e)
        return _DEFAULT_GUARD_RULES


def _compiled_blacklist():
    """配置黑名单 → [(编译正则, replacement)]。非法正则词条跳过并显式告警（fail-open）。"""
    out = []
    for item in (_load_guard_rules().get("composition_blacklist") or []):
        try:
            out.append((re.compile(item["pattern"], re.IGNORECASE), item.get("replacement")))
        except (KeyError, TypeError, re.error) as e:
            _log.error("[h3_guard] 黑名单词条非法已跳过：%s（%s）", item, e)
    return out


def sanitize_desc(desc: str, max_len=None) -> str:
    """【模块1·清洗层】构图后置词黑名单替换 + 残留清理 + 句界长度裁剪。

    7-4 切音（层8）出口防线：任何 desc 进来，"wide establishing shot, no people" 等
    构图后置暗示被确定性移除，避免 H3 推后说话时机致开头伪音孤立暴露。
    """
    if not desc:
        return ""
    if max_len is None:
        max_len = int(_load_guard_rules().get("desc_max_len") or 600)
    out = desc.strip()
    for pat, repl in _compiled_blacklist():
        out = pat.sub(repl or "", out)
    # 清理残留：双逗号/尾逗号/双空格
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r",\s*$", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = out.strip(" ,")
    # 长度裁剪到句子边界
    if len(out) > max_len:
        cut = out[:max_len]
        idx = max(cut.rfind(". "), cut.rfind(", "), cut.rfind(" "))
        out = cut[: idx if idx > max_len // 2 else max_len].rstrip(" ,")
    return out


def wrap_dialogue(line: str) -> str:
    """【模块2·台词层】<d>[Chinese] 幂等包裹 + 引号残留清理。

    注意：保留句末句号——测试H 以来全部验证基线的 <d> 内均含"。"，勿漂移。
    """
    line = (line or "").strip()
    if not line:
        return ""
    if re.search(r"<d>.*?</d>", line, re.S):
        return line
    line = line.strip("\"'\u201c\u201d\u2018\u2019")
    return f"<d>[Chinese]{line}</d>"


def validate_structure(images, audios, subjects, n_speakers: int, prompt: str) -> dict:
    """【模块7·结构校验层】提交前硬规则校验，不依赖模型自觉（规格 §2，fail-closed）。

    R1 ref_images 数 == Picture 定义数（图数≠Picture数 → 槽位/定义结构不一致 = 乱语根源）
    R2 人物在前场景在后（Picture 顺序 kind_rank 非降）
    R3 N 音色槽 == 有音色主体数（A1 扩槽一一对应），有说话人则必须有音色槽
    R4 单人场（_speakers<2）不得含双人互动句 "second speaker replies"（v5 多字根因）

    subjects 为空（显式 ref_images/raw 直通模式）→ 跳过校验（pass + note）。
    返回 {"pass": bool, "errors": [...], "note": ...}。
    """
    if not subjects:
        return {"pass": True, "errors": [],
                "note": "raw-prompt mode（显式参考/直通，无 Subject 结构可校验，跳过）"}
    errors = []
    pic_subjects = [s for s in subjects if s.get("img") is not None]
    if len(images) != len(pic_subjects):
        errors.append(f"图数≠Picture数: ref_images={len(images)} vs Picture定义={len(pic_subjects)}")
    ranks = [{"role": 0, "scene": 1, "prop": 2}.get(s["kind"], 3) for s in pic_subjects]
    if ranks != sorted(ranks):
        errors.append("人物/场景顺序错误（须人物在前场景在后）: "
                      + str([f"{s['name']}/{s['kind']}" for s in pic_subjects]))
    aud_subjects = [s for s in subjects if s.get("aud") is not None]
    if len(audios) != len(aud_subjects):
        errors.append(f"音色槽数≠有音色主体数: ref_audios={len(audios)} vs subjects={len(aud_subjects)}")
    speaker_pics = {s["img"] + 1 for s in aud_subjects}
    if n_speakers > 0 and not speaker_pics:
        errors.append(f"有 {n_speakers} 个台词说话人但无任何音色槽")
    if n_speakers < 2 and "second speaker replies" in (prompt or ""):
        errors.append("单人场含双人互动句 second speaker replies（v5 多字根因，禁止）")
    return {"pass": not errors, "errors": errors, "note": ""}


def _sha256_file(path: str) -> str:
    """文件内容 sha256（大文件分块）。读不到返回空串（调用方自行降级）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def audit_submit(record: dict, out_dir: str = "") -> str:
    """【模块5·审计层】提交参数全量落盘 JSONL（data/logs/h3_submit_YYYYMMDD.jsonl，按天滚动）。

    审计是留痕不是闸门：落盘失败 fail-open，但 _log.error 显式告警（规格 §4/§5），禁止静默。
    """
    try:
        d = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"h3_submit_{time.strftime('%Y%m%d')}.jsonl")
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        record.setdefault("version", _H3_CODE_VERSION)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as e:
        _log.error("[h3_audit] 审计落盘失败（不阻断生成，fail-open）：%s", e)
        return ""


def _find_ffmpeg():
    """定位 ffmpeg：配置路径 → PATH。配置可为绝对路径或可执行名。"""
    import shutil
    rules = _load_guard_rules()
    return shutil.which(rules.get("ffmpeg_path") or "ffmpeg") or shutil.which("ffmpeg")


def _audio_rms_profile(mp4_path: str, ffmpeg: str, win_s: float = 0.01):
    """提取音轨 → 16kHz 单声道 → 逐窗 RMS 能量轮廓（全轨）。

    返回 (rms:list[float]%, win_s, sample_rate)。检测失败/ffmpeg 不可用抛异常（由调用方 fail-open）。
    """
    import array
    import subprocess
    import tempfile
    import wave
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run([ffmpeg, "-y", "-i", mp4_path, "-ar", "16000", "-ac", "1", tmp],
                       capture_output=True, check=True, timeout=60)
        w = wave.open(tmp, "rb")
        rate = w.getframerate()
        data = array.array("h", w.readframes(w.getnframes()))
        if w.getnchannels() > 1:
            data = data[0::w.getnchannels()]
        w.close()
        win = max(1, int(rate * win_s))
        rms = []
        for i in range(0, len(data) - win + 1, win):
            seg = data[i:i + win]
            rms.append((sum(x * x for x in seg) / len(seg)) ** 0.5 / 32768.0 * 100.0)
        return rms, win_s, rate
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def clean_head_transient(video_bytes: bytes) -> tuple:
    """归档链路清理 H3 引擎级"开头孤立能量带"（切音/伪音/出牌声）（v1.21d · B-20260907-02）。

    依据 MainAgent《瞬态清理-实施方案-20260907.md》§2 规格 + v1.21d 用户拍板
    （保护从严→从宽：删 ts 依赖/gap 早台词保护/剧情音效<10% 保护，只留峰值≥2.5% 触发）。

    算法（v1.21d 修正）：
      1. 全轨 10ms 窗 RMS 轮廓
      2. band_end = 0~1.5s 内首个"其后连续 ≥100 窗(1.0s) RMS 全 <1.0%"的窗起点（开头孤立带终点）
      3. 触发条件（仅一条）：开头带峰值 head_peak ≥ peak_thr(默认 2.5%)
      4. 清理：静音 [0, band_end+1窗]，mute_gain=0.02(-34dB)，a_end=band_end+win_s 无台词钳制
      5. 输出：-c:v copy + 音轨 AAC 192k
      6. skip 情形（结构自动保护，非人工分类）：找不到 1.0s 静音分割（带后无长静默，如
         00069 开场真台词句间仅 0.45s<1.0s / 00013 出牌声+底噪 1~1.6% 无 1.0s 静音）
         或开头带峰值 <2.5%（干净 00002/00018）→ 零改动（fail-open，不阻断出片）
    覆盖范围：短瞬态咔声(00102) / 开头伪音字(00123"下")。判据依据 MainAgent 裁决：
    伪音/瞬态孤立=带后长静默(≥1.0s)→清理；真台词连续=句间短停顿(<1.0s)→不误伤；
    **00013 出牌声因带后无 1.0s 静音(持续底噪)自动 skip、出牌声保留**（MainAgent 判定
    比压掉更安全，非剧情音效<10% 保护——该保护已删）。

    返回 (bytes, note)；note={action: applied|skipped|fallback, peak_pct, window_s,
    mute_gain, band_end_s}，随审计落盘。与 v1.20a apply_fade_in（无条件淡入，已废弃回滚）
    及 v1.21/v1.21c（ts/gap 保护模型，基于 00123 错误定性，已废弃）不同。
    """
    import subprocess
    import tempfile
    rules = _load_guard_rules()
    tc = rules.get("transient_clean") or {}
    enable = tc.get("enable", True)
    peak_thr = float(tc.get("peak_threshold_pct") or 2.5)
    fallback_s = float(tc.get("fallback_fade_s") or 0.08)
    if not enable or not video_bytes:
        return video_bytes, {"action": "skipped", "note": "disabled or empty"}
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return video_bytes, {"action": "skipped", "note": "unavailable: ffmpeg 不可用，跳过清理"}

    tin = tout = None
    try:
        fd, tin = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        fd, tout = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        with open(tin, "wb") as f:
            f.write(video_bytes)
        # 1) 全轨 RMS 轮廓
        rms, win_s, _rate = _audio_rms_profile(tin, ffmpeg, win_s=0.01)
        win_s = 0.01
        # 2) 检测"开头孤立带"（v1.21d 修正，MainAgent 裁决：伪音孤立=长静默、真台词连续=短停顿）：
        #    band_end = 0~1.5s 内，首个"其后连续 ≥100 窗(1.0s) RMS 全 <floor"的窗起点。
        #    【核心1】静音时长门槛 1.0s：开头带之后须有 ≥1.0s 真正安静，才判该带为"孤立事件"
        #    （伪音 00123/瞬态 00102 带后接长静默 → applied）。保护 00069 开场真台词（句间
        #    仅 0.45s 短停顿 <1.0s → 非孤立带 → skip，第一句 0.05~0.44s 台词零吞=硬门槛）。
        #    【核心2】静音阈值 floor=1.0%（非规格字面 0.5%）：实测带后"基本安静"区含 0.5~0.9%
        #    极弱底噪/残响，0.5% 会把它误判非静音打断"连续1.0s"判定导致伪音样本也 skip。
        #    <1.0% 视作安静（00013 底噪 1~1.6% 仍 >1.0% → 无1.0s静音 → skip，出牌声保留，
        #    MainAgent 判定比压掉更安全）。无 ts/gap/剧情音效保护（从宽保留）。
        scan_end = min(int(1.5 / win_s), len(rms))
        band_end = None
        scan_peak_max = 0.0   # 0~band_end 内的峰值；band_end 未定(band_end is None)时为 0~1.5s 扫描最大
        floor = 1.0      # 静音判定阈值（% RMS，<此值视作基本安静，含极弱底噪）
        quiet_win = 100  # 1.0s
        for i in range(scan_end):
            if rms[i] > scan_peak_max:
                scan_peak_max = rms[i]
            if i + quiet_win <= scan_end and all(r < floor for r in rms[i:i + quiet_win]):
                band_end = i * win_s
                break
        if band_end is None or scan_peak_max < peak_thr:
            # 找不到 1.0s 静音（00069 开场台词/00013 底噪连续/干净片）或开头带峰<2.5%（干净）
            if band_end is None:
                # 此时 scan_peak_max = 0~1.5s 扫描区间最大（可能含后续台词，非"开头带峰值"）
                note = (f"0~1.5s 无 1.0s 静音（带后无长静默，判非孤立事件，"
                        f"scan_peak_max(0~1.5s)={scan_peak_max:.2f}%）")
            else:
                note = (f"开头带峰值 {scan_peak_max:.2f}% < {peak_thr}%"
                        f"（干净，band_end={band_end:.3f}s）")
            return video_bytes, {"action": "skipped", "note": note,
                                 "peak_pct": round(scan_peak_max, 3),
                                 "window_s": [0.0, round(band_end, 3)] if band_end is not None else None}
        peak = scan_peak_max
        # 3) 清理条件（仅一条）：开头带峰值 ≥ peak_thr(=2.5%)，上面已判。
        # 4) 清理：静音 [0.0, band_end]，mute_gain（默认 0.02 ≈ -34dB）。末端 = band_end +
        #    1 窗，多压掉带的下降沿边界，不留可闻尾。不再做 ts 钳制（无台词保护）。
        mute_g = float(tc.get("mute_gain") or 0.02)
        a_start = 0.0
        a_end = band_end + win_s
        r = subprocess.run(
            [ffmpeg, "-y", "-i", tin, "-c:v", "copy",
             "-filter_complex",
             f"[0:a]atrim=0:{a_start},asetpts=PTS-STARTPTS[a0];"
             f"[0:a]atrim={a_start}:{a_end},volume={mute_g:.4f},asetpts=PTS-STARTPTS[a1];"
             f"[0:a]atrim={a_end},asetpts=PTS-STARTPTS[a2];"
             f"[a0][a1][a2]concat=n=3:v=0:a=1[a]",
             "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", tout],
            capture_output=True, timeout=120)
        out = b""
        if os.path.exists(tout):
            with open(tout, "rb") as f:
                out = f.read()
        if r.returncode != 0 or len(out) < 1024 or b"ftyp" not in out[:64]:
            raise RuntimeError(f"ffmpeg 清理返回 {r.returncode}，输出 {len(out)}B")
        return out, {"action": "applied", "peak_pct": round(peak, 3),
                     "window_s": [round(a_start, 3), round(a_end, 3)],
                     "mute_gain": mute_g, "band_end_s": round(band_end, 3)}
    except Exception as e:
        # 兜底（§2.4）：固定窗 afade st=0.02 d=fallback_s（仅当 tin 已建立；否则原样返回）
        if tin and os.path.exists(tin):
            try:
                r = subprocess.run(
                    [ffmpeg, "-y", "-i", tin, "-c:v", "copy", "-af",
                     f"afade=t=in:st=0.02:d={fallback_s}", "-c:a", "aac", "-b:a", "192k", tout],
                    capture_output=True, timeout=120)
                out = b""
                if os.path.exists(tout):
                    with open(tout, "rb") as f:
                        out = f.read()
                if r.returncode == 0 and len(out) > 1024 and b"ftyp" in out[:64]:
                    return out, {"action": "fallback", "note": f"afade in {fallback_s}s (原异常: {e})"}
            except Exception:
                pass
        _log.error("[h3_transient] 清理失败，原样返回（fail-open）：%s", e)
        return video_bytes, {"action": "fallback", "note": f"fail-open 原样返回: {e}"}
    finally:
        for p in (tin, tout):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def post_check_head(mp4_path: str, threshold_s=None) -> dict:
    """【模块6·自检层】生成后抽音轨做能量检测：台词起始时刻 + >2s 告警（7-4 切音模式）。

    能测：台词起始时间（RMS>6% 判起始）/伪音孤立暴露风险；
    不能测：乱语内容正确性、多字、串扰（规格 §6 边界，由人工目检兜底）。
    fail-open：ffmpeg 不可用/异常 → unavailable/告警，不阻断。
    """
    import array
    import subprocess
    import tempfile
    import wave

    rules = _load_guard_rules()
    if threshold_s is None:
        threshold_s = float(rules.get("speech_start_threshold_s") or 2.0)
    result = {"speech_start_s": None, "warning": None}
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        result["warning"] = "unavailable: ffmpeg 不可用，自检跳过"
        return result
    try:
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        subprocess.run([ffmpeg, "-y", "-i", mp4_path, "-ar", "16000", "-ac", "1", tmp],
                       capture_output=True, check=True, timeout=60)
        w = wave.open(tmp, "rb")
        rate = w.getframerate()
        data = array.array("h", w.readframes(w.getnframes()))
        if w.getnchannels() > 1:
            data = data[0::w.getnchannels()]
        w.close()
        os.remove(tmp)
        win = int(rate * 0.01) or 1
        start = None
        for i in range(0, len(data), win):
            seg = data[i:i + win]
            if not seg:
                break
            rms = (sum(s * s for s in seg) / len(seg)) ** 0.5 / 32768.0 * 100.0
            if start is None and rms > 6.0:
                start = i / rate
        result["speech_start_s"] = round(start, 3) if start is not None else None
        if start is not None and start > threshold_s:
            result["warning"] = (f"台词起始 {start:.2f}s > {threshold_s:.1f}s：H3 起始伪音孤立暴露"
                                 "风险高（7-4 切音模式），建议人工试听确认")
    except FileNotFoundError:
        result["warning"] = "unavailable: ffmpeg 不可用，自检跳过"
    except Exception as exc:  # 自检失败不阻断主流程
        result["warning"] = f"自检异常（不阻断）: {exc}"
    return result


# ── v1.14 Ref2VA prompt 构造：场页 raw prompt → H3 六段式（<Subject N>/<Audio N> 映射 + <d> 台词）──
# v1.14f P0 修复（依据 h3 研究报告 20260901 + 本地验证）：{0,20}→{0,50} 字窗口。
#   根因：长前缀台词（如"外卖员把雨衣帽子往后一推…带着歉意说："）前缀超 20 字时，正则回溯截断前缀、
#   丢失角色名 → _rewrite_descr 归因失败 → 台词不被 <d> 包裹 → H3 当场景描述"画"而非"念"，
#   开头 2-3s 乱语（TC0 复现）。扩到 50 字后角色名完整保留，双台词正确包裹归因。
_DIALOG_RE = re.compile(r"([^。！？\n]{0,50}?(?:说|答道|答|问道|问|回应|喊|叫道)[：:]\s*)([^。！？]*[。！？]?)")

# v1.19（7-3 v6"黄昏时分"旁白根因修复，2026-09-07）：台词引述引导剥离正则。
#   背景：detailed_description 里 `<d>` 前保留中文引述链（"黄昏时分，…轻声说：<d>…"）时，
#   H3 把紧邻台词的中文短句（如"黄昏时分，"）当开场旁白并入念出（走旁白弱合成通道 → "昏"字模糊）。
#   生产输入为中文场文本（无法英文化），故在代码出口剥离"动词+冒号"引述引导、改为英文结构化引导
#   "<Picture N> delivers the line:"，切断 H3 的"中文短句 → 说： → <d>"引述链。
#   仅剥离引述动词及紧邻修饰词，保留画面动作词（如"温和微笑"）。
_QUOTE_LEAD_RE = re.compile(
    r"(?:[，,、]\s*)?(?:"
    r"轻声|低声|温和|笑着|微笑|淡然|缓缓|平静|淡淡|微微|沉着|"
    r"不紧不慢|随意|随口|无奈|兴奋|高兴|激动|严肃|认真|小声|大声|"
    r"温柔|轻柔|轻松|没好气|不耐烦|冷冷|迟疑|犹豫"
    r")?\s*(?:说|答道|答|问道|问|回应|喊|叫道|念道|吟道|喝道|喊道)\s*[：:]\s*$"
)


def strip_quote_lead(prefix: str) -> str:
    """【模块2·台词层】v1.19 引述剥离（规格 §1 映射#1 直接迁）：剥离 prefix 末尾的
    中文引述引导（"，轻声说："），返回剩余叙述段。

    例："…在柜台后温和微笑，轻声说：" → "…在柜台后温和微笑"
    例："他说：" → "他"
    仅锚定行尾的 [逗号?][修饰词?]动词[：:]；不含引述动词的画面段不受影响。
    """
    p = _QUOTE_LEAD_RE.sub("", prefix)
    return re.sub(r"[，,]\s*$", "", p)


# v1.14b 运镜标记 → H3 官方自然语言描述（H3 模板即用自然语言描述运镜；XML 标记在 Ref2VA 结构内不再被识别）
_CAMERA_PHRASES = {
    "推近": "The camera slowly pushes in toward the subjects.",
    "拉远": "The camera pulls back to a wider view.",
    "横摇": "The camera pans horizontally across the scene.",
    "直移": "The camera dollies straight along the scene.",
    "跟拍": "The camera follows the subjects as they move.",
    "升降": "The camera cranes up or down for perspective.",
    "环绕": "The camera orbits around the subjects throughout the shot.",
    "手持": "Handheld camera style with subtle natural shake.",
    "变焦": "The camera zooms in for emphasis.",
    "固定": "The camera stays fixed throughout, no movement.",
    "仰拍": "Low-angle shot looking up at the subjects.",
    "俯拍": "High-angle shot looking down at the subjects.",
}
_CAMERA_RE = re.compile(r"<camera-move>\s*([^<]+?)\s*</camera-move>")
# v1.59（7-3 重走目检暴露）：<shot-size>/<duration-ms> 等控制标签不属 _MARK/_DIALOG/_CAMERA，
#   残留会随 detailed_description 进入 H3 → H3 把"中景/固定镜头"等中文当台词用默认女声念出。
_SHOT_SIZE_RE = re.compile(r"<shot-size>\s*[^<]*?\s*</shot-size>", re.IGNORECASE)


def _camera_phrase(v: str) -> str:
    v = v.strip()
    # 归一常见"镜头"后缀：标准场文本 camera-move 常为"固定镜头/推镜/拉镜"等，映射表用短动词"固定/推近"
    cand = v
    if cand not in _CAMERA_PHRASES:
        cand = re.sub(r"镜头?$", "", v)   # 固定镜头→固定 / 推镜→推
    return _CAMERA_PHRASES.get(cand) or _CAMERA_PHRASES.get(v, "")


def build_h3_prompt(raw: str, subjects: list):
    """【模块3·组装层·出口唯一】构造 H3 Ref2VA **六段式** prompt（v1.17 骨架，规格 §1 映射#6 直接迁）。

    v1.20（2026-09-07）：v1.19 六段式骨架原样保留，新增三道防线——
      ① Subject desc 必经清洗层 sanitize_desc（构图后置词黑名单，7-4 切音层8 防线）；
      ② detailed_description 尾部注入负面约束清单 NEGATIVE_RULES（配置化，规格 §3/§7）；
      ③ 返回 (prompt, build_info)，build_info.n_speakers 供结构校验层用。
    固定模板句（"Empty scene, no other people." 等）为测试H 验证基线，**不过**黑名单（仅清
    洗资产卡 gen_prompt 派生 desc，模板漂移风险为零）。

    v1.17（09-02）六段式→（重构为测试H六段式，纠正 v1.15 误判）：
      早期 v1.15 从六段式退到简洁式，依据旧根因报告"六段式导致乱语"。实施指南 §六 澄清：那是
      `is the scene` 区分格式（测试F）导致的，**不是** `<Subject N>/<Picture N>` 统一格式（测试H）。
      测试 H（test_subject_format，3图人物在前场景在后 + 3 Picture 英文定义 + `<d>[Chinese]` 台词
      + duration 10s + seed 123456789）完全成功：无乱语/无串扰/背景正确/音色正确。
      结构：subject_definitions + summary + retention_analysis + detailed_description
           + overall_soundscape + non_diegetic_music。

    若 raw 已是六段式（含 `subject_definitions:`），原样返回（n_speakers=0）。
    subjects 需已按"人物在前、场景在后"排序（resolve_references v1.17 已排序，结构校验层复核）。
    """
    if not subjects or not raw:
        return raw, {"n_speakers": 0}
    if "subject_definitions:" in raw or "<Picture " in raw or "<d>" in raw:
        return raw, {"n_speakers": 0}

    lines = []

    # ═══ 1. subject_definitions（英文描述，来自 assets gen_prompt；v1.20 必经清洗层）═══
    lines.append("subject_definitions:")
    for s in subjects:
        if s.get("img") is None:
            continue
        n = s["img"] + 1
        desc_en = sanitize_desc((s.get("desc") or "").strip())
        if not desc_en:
            if s["kind"] == "scene":
                desc_en = f"The scene setting of {s['name']}. Empty scene, no other people."
            elif s["kind"] == "role":
                desc_en = f"A character named {s['name']}."
            else:
                desc_en = f"A prop named {s['name']}."
        lines.append(f"- <Subject {n}> / <Picture {n}>: {desc_en}")
    # 音频定义（Voice-timbre reference 格式）
    for s in subjects:
        if s.get("aud") is not None and s.get("img") is not None:
            na, np_ = s["aud"] + 1, s["img"] + 1
            lines.append(
                f"- <Audio{na}>: Voice-timbre reference for <Subject {np_}> (S{np_}), "
                f"the voice of {s['name']}. ONLY inherit timbre, pitch and speaking rhythm. "
                f"DO NOT copy any original spoken words from this reference audio.")

    # ═══ 2. summary ═══
    lines.append("summary: [reference generation] [audio reference]")

    # ═══ 3. retention_analysis（英文）═══
    lines.append("retention_analysis:")
    for s in subjects:
        if s.get("img") is None:
            continue
        n = s["img"] + 1
        if s["kind"] == "role":
            lines.append(
                f"- <Picture {n}> ({s['name']}): fully_preserved. Locks the character's face, "
                f"hairstyle, body type, and outfit for {s['name']}.")
        elif s["kind"] == "scene":
            lines.append(
                f"- <Picture {n}> ({s['name']}): fully_preserved. Locks the scene environment, "
                f"lighting, atmosphere, and color temperature. Empty scene, no other people.")
        else:
            lines.append(f"- <Picture {n}> ({s['name']}): fully_preserved.")
    for s in subjects:
        if s.get("aud") is not None and s.get("img") is not None:
            na, np_ = s["aud"] + 1, s["img"] + 1
            lines.append(
                f"- <Audio{na}> ({s['name']} reference voice): reference. Locks {s['name']}'s vocal "
                f"timbre so the spoken line carries this voice quality. Do NOT reuse original "
                f"utterances from Audio{na}, generate brand-new dialogue text.")

    # ═══ 4. detailed_description（中文动作 + <Picture N> + <d>[Chinese] 台词 + 负面约束）═══
    lines.append("detailed_description:")
    shot_text, n_speakers = _build_shot_desc(raw, subjects)
    lines.append(shot_text)

    # ═══ 5. overall_soundscape（英文模板）═══
    lines.append("overall_soundscape:")
    lines.append(
        "Soft ambient atmosphere sounds throughout; gentle background noise supporting the scene mood, "
        "consistent with the scene setting, without overpowering the dialogue.")

    # ═══ 6. non_diegetic_music（英文模板）═══
    lines.append("non_diegetic_music:")
    lines.append(
        "A soft warm piano texture with gentle sustained notes, slow and unhurried, subtly supporting "
        "the mood without overpowering the diegetic sounds.")

    return "\n".join(lines), {"n_speakers": n_speakers}


def _build_shot_desc(raw: str, subjects: list):
    """生成 detailed_description 的 [Shot 1] 分镜描述（规格 §3 组装规范）。

    中文动作描述（raw 去标记后）+ 台词 `<d>[Chinese]</d>` 包裹 + <Picture N> 引用 + 运镜/光影英文句。
    台词保持中文原文（测试H即用 `<d>[Chinese] 中文台词</d>`），动作/运镜/光影用英文句增强 H3 理解。
    v1.20：台词包裹迁入台词层（strip_quote_lead + wrap_dialogue，映射#1/#4）；
    尾部注入负面约束清单 NEGATIVE_RULES（配置化，规格 §3"约束清单"段）；
    返回 (shot_text, n_speakers)，n_speakers 供结构校验层 R4 复核。
    """
    text = raw
    # 0a) 剥掉 duration-ms / shot-size 残留标签（不属于 _MARK/_DIALOG/_CAMERA 任一正则，会残留在输出干扰 H3）
    text = re.sub(r"<duration-ms>\s*\d+\s*</duration-ms>", "", text)
    text = _SHOT_SIZE_RE.sub("", text)
    # 0b) 抽运镜标记 → 英文短语（先于台词包裹）；不识别值 → 直接移除不残留中文
    cams = []
    def cam_repl(m):
        ph = _camera_phrase(m.group(1))
        if ph:
            cams.append(ph)
        return ""
    text = _CAMERA_RE.sub(cam_repl, text)

    # 1) 台词包裹：前缀匹配到说话人 → <Picture N> 归因 + <d>[Chinese] 台词
    _speakers = set()          # v1.18：记录实际念台词的去重说话人 Picture 编号（供双人句条件化判定）
    def repl(m):
        prefix, quote = m.group(1), m.group(2)
        # 1a) 精确命中：前缀含 short/name（如"小女孩仰头…说"→short"小女孩"）
        for s in subjects:
            if s.get("aud") is not None and s.get("img") is not None and (s["name"] in prefix or s["short"] in prefix):
                nm = s["name"] if s["name"] in prefix else s["short"]
                _speakers.add(s["img"] + 1)
                # v1.19→v1.20（映射#1/#4）：剥离引述引导 + wrap_dialogue 包裹 + 英文结构化引导，
                #   切断 H3"中文短句 → 说： → <d>"引述链（否则 H3 把引述前紧邻中文当开场旁白并入念出）
                _clean = strip_quote_lead(prefix).replace(nm, f"<Picture {s['img']+1}>").strip()
                return f"{_clean} <Picture {s['img']+1}> delivers the line: {wrap_dialogue(quote.strip())}"
        # 1b) 别名回退：说话人主词与卡 short 共享前 2 字（如"外卖员"vs"外卖大叔"→"外卖"）。
        #     v1.17（09-02）新增：用户场文本说话人常写成"外卖员"而卡 prefix 是"外卖大叔"，
        #     精确匹配失败→台词不包 <d>→H3 当画面描述"画"而非"念"→首句对话乱语/丢失（乱语根因之一）。
        for s in subjects:
            if s.get("aud") is None or s.get("img") is None:
                continue
            for cand in (s["name"], s["short"]):
                cand = cand.strip()
                if len(cand) >= 2 and cand[:2] in prefix:
                    _speakers.add(s["img"] + 1)
                    # v1.19→v1.20（映射#1/#4）：同 1a，剥离引述引导 + wrap_dialogue 包裹 + 英文引导
                    _clean = strip_quote_lead(prefix).strip()
                    return f"{_clean} <Picture {s['img']+1}> delivers the line: {wrap_dialogue(quote.strip())}"
        return m.group(0)
    text = _DIALOG_RE.sub(repl, text)

    # 2) 标记 → <Picture N>（长名优先）
    for s in sorted(subjects, key=lambda x: -len(x["name"])):
        if s.get("img") is None:
            continue
        tag = f"<{s['kind']}>{s['name']}</{s['kind']}>"
        text = text.replace(tag, f"<Picture {s['img']+1}>")

    # 2b) 清理残留实体包裹标签：dialog 归因的 prefix 可能含 <role>阿澈</role>，内部名已被换为 <Picture N>，
    #     留下 <role><Picture N></role> 畸形结构 → 剥离裸开/闭标签（仅留合法 <Picture N>/<d>…</d>）
    text = re.sub(r"</?(?:role|scene|prop)>", "", text, flags=re.IGNORECASE)

    # 3) 拼 [Shot 1] 框架（规格 §3 顺序：时长句→动作描述→运镜句→互动句→负面约束→色调光照）
    duration = compute_duration(text) or 8.0
    cam = ("The camera stays fixed throughout, no movement" if not cams else " ".join(cams))
    # v1.18→v1.20（映射#2 改造迁）：双人互动句按**实际念台词人数**条件化。
    #   以台词包裹阶段收集的 `_speakers`（去重说话人 Picture 编号）判定：单人/单说话人场景若误注入
    #   "then the second speaker replies" 会诱导 H3 念完主台词后补念一句回应（= v5 多字直接根因）。
    _n_speakers = len(_speakers)
    if _n_speakers >= 2:
        interaction = ("The characters remain in place facing each other for the entire shot; "
                       "the first speaker delivers the line, then the second speaker replies. ")
    else:
        interaction = "The character delivers the line; no other speakers, no replies. "
    # v1.20（规格 §3）：负面约束清单注入 detailed_description 尾部（色调光照固定尾句之前）
    rules_txt = " ".join(r for r in (_load_guard_rules().get("negative_rules") or []) if r) + " "
    shot = (f"[Shot 1] A static medium shot at eye level, total duration {duration:.1f} seconds. "
            f"{text.strip()} "
            f"{cam}. "
            f"{interaction}"
            "No speech, narration, or extra utterances other than the <d> dialogue lines. "
            f"{rules_txt}"
            "Each subject keeps its own distinct visual identity throughout: faces, ages, genders, "
            "and appearances must NOT blend or swap between subjects. "
            "Color grading is warm and natural. Light source is diffused, no hard shadows.")
    return shot, _n_speakers


def compute_duration(text: str) -> float:
    """【模块4·时长层】由 <d>[Chinese] 台词字数算时长（≤10s）（规格 §1 映射#3 直接迁）。

    v1.18（7-3 v5台词多字根因B收紧，2026-09-06）：公式由 `字数/5+2` 改为 `max(字数/4+1.2, 3.0)`。
      原公式 10 字→4.2s，余量 1.2-1.7s（正常语速~4字/s 仅需 2.5-3s），H3 念完台词后倾向补念
      短句/回应填充（v5"好，别留对"放大因素之一）。新公式 10 字≈3.7s，压缩 >1s 余量。
    """
    chars = 0
    for m in re.findall(r"<d>\s*\[Chinese\]\s*([^<]+?)\s*</d>", text):
        chars += len(re.sub(r"\s", "", m))
    if not chars:
        return 5.0
    return min(10.0, max(3.0, chars / 4.0 + 1.2))
