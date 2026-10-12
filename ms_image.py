# -*- coding: utf-8 -*-
"""④ F5c · modelscope 生图云端通道（独立模块 —— main.py 不增长）。

形态依据（全部实测，Source of Truth）：
  · `keel/3-tech-pitfalls/modelscope-API推理-实测规格-20260924.md`（架构-2，18 侧真跑）
  · `keel/5-archives/④-F5c-modelscope生图分流-20260924/f5c-交付记录-20260924.md`（模型对照表 + 判装没装正解）
  · IC 已验收实现 `D:/infinite-canvas/main.py` generate_modelscope_provider_image（L9844）
  · 24 侧探针 f5c_probe_channel5.py：Z-Image-Turbo 47s 真出图（1.75MB PNG 落地）

调用形态（异步两段式，无同步变体）：
  POST {base}v1/images/generations   header X-ModelScope-Async-Mode: true
      body {model, prompt, width, height, size, image_url:[dataURL...]}（编辑类必须带 image_url）
  GET  {base}v1/tasks/{task_id}      header X-ModelScope-Task-Type: image_generation
      → PENDING/RUNNING → SUCCEED(output_images[0]=OSS URL) / FAILED(errors.message)
  ★ OSS 链接必须下载回本地，不能只存 URL。

模型对照（实测可用；★ 别用 GET /v1/models 判断装没装 —— 那清单不含生图模型）：
  文生图 Tongyi-MAI/Z-Image-Turbo(~15s) / Qwen/Qwen-Image-2512(~30s)
  图生图 Qwen/Qwen-Image-Edit(~80s) / Qwen/Qwen-Image-Edit-2509(~105s，工坊 A2 同款)
  ★ Qwen/Qwen-Image-2.1：09-24 曾 4/4 挂（后端 FC `unknown FC status: Retrying`）；
    **10-01 复测 3/3 成功、~17s（1 额/张）** ⇒ 已恢复，可作候选（是否启用待用户定）
    证据：keel/5-archives/⑥-Qwen-Image-2.1工具栏-20261001/Qwen-Image-2.1-云端复测-20261001.md
  lora：v1 不传（IC 生图函数同样不传；胶片风走提示词）

设置（settings.ms_image，Key 仅本机保存）：
  api_key / endpoint(默认国内站) / model_t2i / model_i2i / timeout(默认 240s)
"""
import base64
import io
import json
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).parent
DEFAULT_ENDPOINT = "https://api-inference.modelscope.cn/"
DEFAULT_MODEL_T2I = "Tongyi-MAI/Z-Image-Turbo"
DEFAULT_MODEL_I2I = "Qwen/Qwen-Image-Edit-2509"
DEFAULT_TIMEOUT = 300          # 规格 §四.2：≥180s；24 侧实测 Edit-2509 一次 217s ⇒ 取 300 留裕量
POLL_INTERVAL = 5              # 规格：5s 间隔（实测 16~23 轮）
SUBMIT_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 120
REF_MAX_EDGE = 1536            # IC 同款：参考图最长边压到 1536 再转 data URL
FAIL_STATES = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}


class MsCancelled(Exception):
    """用户取消（task_queue 捕获后归入 QueueCancelled 收尾）。"""


def config() -> dict:
    """读设置（settings.ms_image），补默认值；文件缺失/字段缺失都安全。"""
    try:
        raw = json.load(open(ROOT / "data" / "settings.json", encoding="utf-8"))
    except Exception:
        raw = {}
    ms = raw.get("ms_image") or {}
    route_mode = ms.get("route_mode")
    # ★ 用户裁定（2026-09-24）：云端只走手工指定，不做自动调度 —— 旧"auto"值一并作废，归一到 manual
    if route_mode not in ("off", "manual"):
        route_mode = "manual"
    return {
        "api_key": (ms.get("api_key") or "").strip(),
        "endpoint": (ms.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/") + "/",
        "model_t2i": (ms.get("model_t2i") or DEFAULT_MODEL_T2I).strip(),
        "model_i2i": (ms.get("model_i2i") or DEFAULT_MODEL_I2I).strip(),
        "timeout": int(ms.get("timeout") or DEFAULT_TIMEOUT),
        "route_mode": route_mode,             # off=只用本地 / manual=生成时手工勾选走云端
        "concurrency": max(1, int(ms.get("concurrency") or 2)),
    }


# ── 分流决策（步3）──────────────────────────────────────────────────────────
# 提交时定通道（frozen at submit）：手动开关优先；自动规则=本地生图积压≥阈值。
# 返回 ""=本地 / "modelscope"=云端。不可分流（enhance/upscale）恒 ""。

_CLOUDABLE_IMAGE_WF = {"z_image", "2511", "klein", "qwen21", "qwen21_edit"}      # enhance/upscale 云端无对应（工程裁定 v1）
# ★ 2026-10-01：qwen21 / qwen21_edit 加入 —— 实测云端 `Qwen/Qwen-Image-2.1` 文生图与多图编辑**都可用**
#   （编辑：2 张参考图 → 正确融合，27~34s，1 额度；探针 keel/script/verify_qwen21_cloud_edit.py）。
#   注意：前端 GT_CLOUD_MODES 与这里要保持一致（前端决定"能不能选"，这里决定"选了会不会被拦"）。


def cloudable(kind: str, workflow_type: str = "") -> bool:
    if kind == "edit":
        return True
    if kind == "image":
        return workflow_type in _CLOUDABLE_IMAGE_WF
    return False


def route_channel(use_cloud: bool, kind: str, workflow_type: str = "") -> str:
    """提交口调用：返回通道标记（"" =本地 / "modelscope"=云端）。

    ★ 用户裁定（2026-09-24）：**只有用户手工指定（use_cloud）才走云端，不做任何自动调度**。
    设置 route_mode=off（只用本地）时即使勾选也落本地；要求云端但未就绪 → ValueError（端点转 400）。
    """
    if not cloudable(kind, workflow_type):
        return ""
    if not use_cloud:
        return ""
    if config().get("route_mode") == "off":
        return ""
    if not is_ready():
        raise ValueError("要走云端出图，请先在设置页「云端平台」填写访问令牌")
    return "modelscope"


# ── 额度记账（成功才计；按日累计）─────────────────────────────────────────
# 意见（已入开工方案）：记账做、硬上限不做 —— 池子量级未知，硬限会误伤。

def record_usage(kind: str) -> None:
    """kind: t2i / i2i。失败不记（额度按成功产出扣）。"""
    try:
        p = ROOT / "data" / "ms_usage.json"
        try:
            u = json.load(open(p, encoding="utf-8"))
        except Exception:
            u = {}
        today = time.strftime("%Y-%m-%d")
        if u.get("date") != today:
            u = {"date": today, "t2i": 0, "i2i": 0, "total": u.get("total") or {"t2i": 0, "i2i": 0}}
        u[kind] = int(u.get(kind) or 0) + 1
        tot = u.setdefault("total", {})
        tot[kind] = int(tot.get(kind) or 0) + 1
        tmp = str(p) + ".tmp"
        json.dump(u, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        Path(tmp).replace(p)
    except Exception:
        pass                                                  # 记账失败不影响生成


def is_ready() -> bool:
    return bool(config()["api_key"])


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.trust_env = False                     # ★ 规格 §四.3：绕本机代理（否则 502 假信号）
    s.headers.update({"Authorization": "Bearer " + token,
                      "Content-Type": "application/json"})
    return s


def _ref_to_data_url(src: str, max_edge: int = REF_MAX_EDGE) -> str:
    """本地文件/相对路径 → 压缩后的 data URL（IC 同款思路：贴近其已验收链路）。"""
    p = Path(src)
    if not p.is_file():
        raise RuntimeError("参考图文件不存在: %s" % src)
    im = Image.open(p)
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        has_alpha = im.getextrema()[3][0] < 255
        if not has_alpha:
            im = im.convert("RGB")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    scale = max_edge / float(max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    if im.mode == "RGBA":
        im.save(buf, "PNG")
        mime = "image/png"
    else:
        im.save(buf, "JPEG", quality=90)
        mime = "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(buf.getvalue()).decode())


def generate(prompt: str, model: str = "", ref_images: list | None = None,
             width: int = 0, height: int = 0,
             cancel_check=None, progress_cb=None, timeout: int = 0) -> bytes:
    """一次云端生图，返回图片字节（PNG/JPEG 以服务端产物为准）。

    ref_images: 本地路径 / data URL / http(s) URL 混合列表（编辑类必传；文生图忽略）。
    cancel_check: 每次轮询间调用，返回 True 即中止（MsCancelled）。
    progress_cb: str 回调（轮询进度文案）。
    """
    cfg = config()
    if not cfg["api_key"]:
        raise RuntimeError("云端生图未配置：请先在设置页填写云端平台的访问令牌")
    model = (model or "").strip() or cfg["model_t2i"]
    budget = int(timeout or cfg["timeout"] or DEFAULT_TIMEOUT)

    payload = {"model": model, "prompt": (prompt or "").strip()}
    if width and height:
        payload["width"] = int(width)
        payload["height"] = int(height)
        payload["size"] = "%dx%d" % (int(width), int(height))
    refs = []
    for src in (ref_images or []):
        if not src:
            continue
        s = str(src)
        if s.startswith("data:image/") or s.startswith(("http://", "https://")):
            refs.append(s)                      # 已是远端/内联形态，原样传
        else:
            refs.append(_ref_to_data_url(s))    # 本地文件 → 压缩 data URL
    if refs:
        payload["image_url"] = refs             # 编辑类必须；服务端 400 校验（实测）

    s = _session(cfg["api_key"])
    base = cfg["endpoint"]
    t0 = time.time()
    r = s.post(base + "v1/images/generations",
               headers={"X-ModelScope-Async-Mode": "true"},
               json=payload, timeout=SUBMIT_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError("云端生图提交失败（HTTP %s）：%s" % (r.status_code, r.text[:300]))
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError("云端生图提交返回非 JSON：%s" % r.text[:200])
    tid = j.get("task_id")
    if not tid:
        # 理论上的同步直出形态（实测未见，兜底照 IC 处理）
        imgs = (j.get("output_images") or [])
        if imgs:
            return _download(s, imgs[0])
        raise RuntimeError("云端生图未返回任务标识：%s" % json.dumps(j, ensure_ascii=False)[:300])

    poll_headers = {"X-ModelScope-Task-Type": "image_generation"}
    while time.time() - t0 < budget:
        if cancel_check is not None and cancel_check():
            raise MsCancelled("用户取消")
        if progress_cb is not None:
            progress_cb("云端生成中…（%ds）" % int(time.time() - t0))
        time.sleep(POLL_INTERVAL)
        try:
            pj = s.get(base + "v1/tasks/" + tid, headers=poll_headers, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            continue                            # 单次轮询抖动不致命，下一轮再看
        st = str(pj.get("task_status") or "").upper()
        if st in ("SUCCEED", "SUCCESS"):
            imgs = pj.get("output_images") or []
            if not imgs:
                raise RuntimeError("云端生成成功但未返回图片地址")
            if cancel_check is not None and cancel_check():
                raise MsCancelled("用户取消")
            return _download(s, imgs[0])
        if st in FAIL_STATES:
            err = (pj.get("error_info") or pj.get("message") or pj.get("detail")
                   or ((pj.get("errors") or {}).get("message")) or st)
            raise RuntimeError("云端生成失败：%s" % err)
    raise RuntimeError("云端生成超时（>%ds），任务号 %s" % (budget, tid))


def _download(s: requests.Session, url: str) -> bytes:
    b = s.get(url, timeout=DOWNLOAD_TIMEOUT).content
    if not b or len(b) < 100:
        raise RuntimeError("云端产物下载异常（%d bytes）" % len(b))
    return b
