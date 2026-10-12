# -*- coding: utf-8 -*-
"""AI 能力封装（阶段 4：LLM 调用 / JSON 解析 / 成本估算）。

- 模型来自 data\settings.json 的 llm_models（默认模型 = is_default，弱关联外部配置）
- 协议：OpenAI 兼容 Chat Completions（可配置 Modelscope，复用用户 Key）
- Y1 已实测：Modelscope Qwen3 拆解可用、返回结构化 JSON（注意剥离 ```json 代码块包裹）
"""
import json
import re

import requests

# 批3 3a：数据访问统一经 dao 门面（避免散落直连底层，留低成本切换点）
from dao import library

ANALYZE_SYSTEM_PROMPT = """你是专业短剧编剧与分镜拆解助手。请把给定的剧本拆解为结构化 JSON，用于"剧本分镜工坊"系统入库。
输出必须严格符合以下结构（仅输出 JSON，不要任何额外文字、不要 markdown 代码块包裹）：

{
  "characters": [{"name": "角色名", "variant_name": "基础形象", "prompt": "该形象的生成提示词（外貌/服装/气质，30-60字）", "voice_text": "音色文本描述（如：低沉沙哑的中年男声）"}],
  "scenes": [{"name": "场景名", "variant_name": "白天", "prompt": "该视角的环境生成提示词（光线/色调/陈设，20-40字）"}],
  "props": [{"name": "道具名", "variant_name": "小卖部", "prompt": "该场景下道具的外观生成提示词（材质/形状/特征，15-30字）"}],
  "structure": [
    {
      "episode_title": "第X集 标题",
      "summary": "本集剧情摘要（4-8 句话，概括本集主要情节、冲突与进展）",
      "scenes": [
        {
          "scene_title": "场景名",
          "shots": [
            {"desc": "画面描述", "dialogue": "台词（无则空串）", "narration": "旁白（无则空串）",
             "characters": ["出场角色名"], "scene": "场景名", "props": ["道具名"],
             "shot_size": "景别", "camera": "运镜", "duration": 5, "status": "待生成"}
          ]
        }
      ]
    }
  ]
}

角色/场景/道具引用：structure 中的 characters/scene/props 必须与上方的 characters/scenes/props 的 name 完全一致。
不要输出镜号（系统落地时按顺序将镜头合并为场文本："镜1：…镜2：…"纯文本段，行内时长/运镜/景别转尖括号标记保留在文本中）。"""


def _fix_mojibake(s: str | None) -> str | None:
    """修复疑似 UTF-8 被误读为 CP1252/Latin-1 的中文乱码（如模型名 '（小·快）' 变 'ï¼ˆå°Â·å¿«ï¼‰'）。

    仅当字符串可整体按某编码（cp1252 优先，其次 latin-1）编码成字节、又能按 UTF-8 解码、
    且解码结果含 CJK 字符时才修复；否则原样返回（避免误伤正常英文/数字）。
    """
    if not s or not isinstance(s, str):
        return s
    for enc in ("cp1252", "latin-1"):
        try:
            raw = s.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            fixed = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
            return fixed
    return s


def get_model(model_id: str | None = None) -> dict | None:
    cfg = library.load_settings()
    models = cfg.get("llm_models") or []
    if not models:
        return None
    m = None
    if model_id:
        for x in models:
            if x.get("id") == model_id:
                m = x
                break
    else:
        for x in models:
            if x.get("is_default"):
                m = x
                break
        if m is None:
            m = models[0]
    if m is None:
        return None
    m = dict(m)
    for k in ("name", "base_url", "model"):
        if isinstance(m.get(k), str):
            m[k] = _fix_mojibake(m[k])
    return m


def estimate_cost(text_chars: int, model: dict) -> dict:
    """按剧本字数粗略估算 token 与费用（中文约 1 字 ≈ 1~1.2 token）。"""
    tokens = int(text_chars * 1.2)
    cost = round(tokens / 1000 * float(model.get("price_per_1k") or 0), 4)
    return {"tokens": tokens, "cost": cost, "chars": text_chars}


def _gen_cost_entry(cfg: dict, model: str, key: str) -> dict:
    """从 settings.generation_cost 取某模型单价的某字段（unit_price），带 default 兜底。"""
    gc = (cfg or {}).get("generation_cost") or {}
    models = gc.get("models") or {}
    entry = models.get(model) or models.get("default") or {}
    unit_price = float(entry.get(key) or 0)
    cost_confirm_enabled = bool(gc.get("cost_confirm_enabled", True))
    return {"model": model, "unit_price": unit_price, "cost_confirm_enabled": cost_confirm_enabled}


def estimate_video_cost(cfg: dict, seconds: float, model: str = "h3_video") -> dict:
    """生视频成本预估（秒数 × 每秒单价；H3 本地 ComfyUI 现 0 元/秒）。"""
    e = _gen_cost_entry(cfg, model, "video_per_second")
    seconds = float(seconds or 0)
    cost = round(seconds * e["unit_price"], 4)
    return {"seconds": seconds, **e, "cost": cost}


def estimate_image_cost(cfg: dict, count: int = 1, model: str = "comfyui_image") -> dict:
    """生图成本预估（张数 × 单张单价）。count 默认 1（单张生成入口）；批量 N 张留升版用 count=N。"""
    e = _gen_cost_entry(cfg, model, "image_per_unit")
    count = int(count or 1)
    cost = round(count * e["unit_price"], 4)
    return {"count": count, **e, "cost": cost}


def call_chat(model: dict, messages: list, temperature: float | None = None,
              stream: bool = False, on_delta=None) -> str:
    """OpenAI 兼容 Chat Completions 调用。

    - stream=False：同步返回完整 content（原逻辑）
    - stream=True：流式读取（v1.51），逐 chunk 调 on_delta(增量文本)（可选），返回完整 content
    """
    url = (model.get("base_url") or "").rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + (model.get("api_key") or ""), "Content-Type": "application/json"}
    payload = {
        "model": model.get("model"),
        "messages": messages,
        "temperature": temperature if temperature is not None else float(model.get("temperature") or 0.7),
    }
    # v1.51：Qwen3 系列默认思考模式（content 为空、输出在 reasoning_content）→ 显式关闭，
    # 否则流式/非流式均拿不到正文（实测 enable_thinking=false 顶层参数有效）
    if "qwen3" in (model.get("model") or "").lower():
        payload["enable_thinking"] = False
    # v1.51：显式 max_tokens（拆解/推荐等长 JSON 输出防默认 token 上限截断；模型行可配覆盖，默认 8192）
    _mt = model.get("max_tokens")
    payload["max_tokens"] = int(_mt) if _mt else 8192
    if stream:
        payload["stream"] = True
    resp = requests.post(url, json=payload, headers=headers, timeout=300, stream=stream)
    if resp.status_code >= 400:
        # ★ 2026-10-01（用户报"AI 优化提示词 400 看不出原因"）：把服务端原文透出来。
        #   原先 raise_for_status() 只给 "400 Client Error: Bad Request for url: ..."，
        #   真因（如 "Model id ... has no provider supported"）藏在对端响应体里，用户无从下手。
        _body = ""
        try:
            _body = (resp.text or "").strip()[:300]
        except Exception:
            pass
        raise RuntimeError("模型调用失败（HTTP %s）：%s" % (resp.status_code, _body or "服务端未返回内容"))
    if not stream:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    # 流式：逐 chunk 解析 SSE data 行，累积 content
    # 复检修复：requests 探测的响应编码可能为 ISO-8859-1（服务端未给 UTF-8 charset），
    # iter_lines(decode_unicode=True) 会按 Latin-1 解码致中文 mojibake；强制 UTF-8 解码。
    resp.encoding = "utf-8"
    parts = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0].get("delta") or {}).get("content")
        if delta:
            parts.append(delta)
            if on_delta:
                try:
                    on_delta(delta)
                except Exception:
                    pass
    return "".join(parts)


def _strip_ctrl_in_strings(text: str) -> str:
    """移除 JSON 字符串值内未转义的裸控制字符（\x00-\x1f，含 \t\n\r）。
    json.loads 只接受转义形式的控制字符；字符串值外的 \t\n\r 是合法结构空白保留。"""
    out = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ord(ch) < 0x20:
            continue  # 字符串值内裸控制字符丢弃（LLM 输出常见裸换行/制表符）
        out.append(ch)
    return "".join(out)


def _try_repair_json(text: str) -> dict:
    """尽力修复被截断的 JSON（LLM 输出截断场景）：补未闭合字符串引号 + 按括号栈补闭合。"""
    # 1) 补未闭合字符串的引号
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
    if in_str:
        text += '"'
    # 2) 按括号栈补闭合（仅结构括号，忽略字符串内）
    stack = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    closes = "".join("}" if c == "{" else "]" for c in reversed(stack))
    return json.loads(text + closes)


def parse_json_strict(text: str) -> dict:
    """剥离 ```json 代码块包裹后解析 JSON；失败抛 ValueError。
    v1.51 增强容错：①移除 JSON 字符串值内裸控制字符（\x00-\x1f，LLM 输出常见）
    ②按最外层花括号对截取（应对尾部多余解释文字）③尾部回退重试④截断修复器（补引号/补闭合括号）。"""
    text = (text or "").strip()
    text = _strip_ctrl_in_strings(text)
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        text = text[s:e + 1]
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 兜底①：按最外层花括号对截取（跳过尾部多余内容）
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[:i + 1])
                except ValueError:
                    break
    # 兜底②：从尾部回退到上一个 "}" 重试（应对 LLM 输出被截断）
    end = len(text)
    while end > 0:
        try:
            return json.loads(text[:end])
        except ValueError:
            prev = text.rfind("}", 0, end - 1)
            if prev < 0:
                break
            end = prev
    # 兜底③：截断修复器（补未闭合引号/括号）
    try:
        return _try_repair_json(text)
    except ValueError:
        raise ValueError("LLM 输出解析失败（可能被截断或格式异常），请重试")


def split_episodes(script: str) -> list[dict]:
    """按'第X集'切分剧本为多集；无集标记则整本一集。"""
    script = (script or "").strip()
    parts = re.split(r"(?=第[一二三四五六七八九十百\d]+集)", script)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return []
    # 首段若无集标记，并入第一集
    episodes = []
    for p in parts:
        m = re.match(r"^(第[一二三四五六七八九十百\d]+集[^\n]*)", p)
        title = m.group(1) if m else (episodes[-1]["title"] if episodes else "第一集")
        if episodes and title == episodes[-1]["title"] and not m:
            episodes[-1]["content"] += "\n" + p
        else:
            episodes.append({"title": title, "content": p})
    return episodes


def get_fallback_model(model: dict) -> dict | None:
    """解析失败降级兜底模型：非默认且已配 Key 的高质量档（如 Qwen3-235B）。"""
    models = (library.load_settings().get("llm_models") or [])
    for m in models:
        if m.get("id") != model.get("id") and m.get("api_key") and "235B" in (m.get("model") or ""):
            return m
    return None


def _call_parse_retry(model: dict, messages: list, on_delta=None,
                      retries: int = 1, temperature: float | None = None) -> dict:
    """LLM 调用 + JSON 解析，多层兜底（v1.51）：
    ①流式尝试（retries 次，parse 失败重试）→ ②非流式兜底（Modelscope 流式可能累积多余内容致 parse 失败）
    → ③降级备用模型（235B 高质量档）。全部失败抛最后一次 ValueError。"""
    last = None
    for _ in range(retries + 1):
        try:
            raw = call_chat(model, messages, temperature=temperature,
                            stream=on_delta is not None, on_delta=on_delta)
            return parse_json_strict(raw)
        except ValueError as e:
            last = e
    # ② 非流式兜底
    try:
        raw2 = call_chat(model, messages, temperature=temperature, stream=False)
        return parse_json_strict(raw2)
    except ValueError as e:
        last = e
    # ③ 降级备用模型
    fb = get_fallback_model(model)
    if fb:
        try:
            raw3 = call_chat(fb, messages, temperature=temperature,
                             stream=on_delta is not None, on_delta=on_delta)
            return parse_json_strict(raw3)
        except ValueError as e:
            last = e
    raise last


def analyze_episode(model: dict, episode: dict, on_delta=None) -> dict:
    """拆解单集，返回结构化数据。"""
    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
        {"role": "user", "content": f"【{episode['title']}】\n{episode['content']}"},
    ]
    return _call_parse_retry(model, messages, on_delta=on_delta)


def analyze_script(model: dict, script: str, on_delta=None) -> dict:
    """整本一次拆解（v1.37 自适应：短剧本 <8000 字整本一次，成本最低）。
    整本内容一次发送，LLM 输出 structure 含全部集。"""
    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
        {"role": "user", "content": script},
    ]
    return _call_parse_retry(model, messages, on_delta=on_delta)


EXTRACT_ASSETS_SYSTEM_PROMPT = """你是资深短剧拆解专家。根据给定的"集剧情摘要"，提取该集中出现的全部角色、场景、道具。
只输出 JSON（不要任何解释文字、不要 markdown 代码块包裹），结构：
{
  "assets": [
    {"kind": "character", "name": "角色名", "variant_name": "基础形象", "prompt": "该形象生成提示词（外貌/服装/气质，30-60字）", "voice_text": "音色文本描述"},
    {"kind": "scene", "name": "场景名", "variant_name": "白天", "prompt": "环境生成提示词（光线/色调/陈设，20-40字）"},
    {"kind": "prop", "name": "道具名", "variant_name": "所在场景", "prompt": "外观生成提示词（材质/形状/特征，15-30字）"}
  ]
}
提示词尽量详细具体，但绝不实际生成任何图片或音频。"""


def extract_assets(model: dict, summary_text: str, on_delta=None) -> dict:
    """LLM 从集剧情摘要提取该集资产（v1.38：非聚合已有归属；供修改摘要后重新提取）。"""
    messages = [
        {"role": "system", "content": EXTRACT_ASSETS_SYSTEM_PROMPT},
        {"role": "user", "content": f"集剧情摘要：\n{summary_text}"},
    ]
    return _call_parse_retry(model, messages, on_delta=on_delta)


OPTIMIZE_PROMPT_SYSTEM = """你是资深影视美术与提示词优化师（4-6 通用 AI 能力）。把给定的生成提示词优化得更具体、更利于 AI 生图/生视频，保持原意与风格不变。
只输出优化后的提示词文本，不要任何解释、不要引号包裹、不要 markdown。
重要：文本中可能含内联标记或占位符（形如 <role ...>、<duration-ms>、<camera-move>、<shot-size>、@@REFn@@），这些是结构占位，必须【原样保留、一个都不能少、不得改写/删除/移动】，只优化其余纯文字部分。"""


def optimize_prompt(model: dict, text: str, on_delta=None) -> str:
    """LLM 优化提示词（4-6：AI 优化提示词，润色回填）。"""
    messages = [
        {"role": "system", "content": OPTIMIZE_PROMPT_SYSTEM},
        {"role": "user", "content": text},
    ]
    raw = call_chat(model, messages, stream=on_delta is not None, on_delta=on_delta)
    return (raw or "").strip()


RECOMMEND_SCOPES_SYSTEM = """你是短剧项目组织专家（4-6 归属智能推荐·LLM 推荐）。根据资产的名称与描述，从给定的项目/集/场列表中，推荐该资产应归属的位置（角色/场景/道具属于哪个项目、哪一集、哪个场次）。
只输出 JSON（不要任何解释、不要 markdown 代码块包裹）：
{"recommendations": [{"level": "project", "id": "...", "reason": "推荐理由(10-20字)"}]}
规则：level 只能取 project / episode / scene；id 必须是给定列表中出现的 id；最多推荐 3 条；无法判断时输出空数组。"""


def recommend_scopes(model: dict, asset_desc: str, context: str, on_delta=None) -> dict:
    """LLM 推荐资产归属（4-6：按名称/描述/关联场推荐，结果需用户确认后生效）。"""
    messages = [
        {"role": "system", "content": RECOMMEND_SCOPES_SYSTEM},
        {"role": "user", "content": f"资产：{asset_desc}\n\n可选项目/集/场：\n{context}"},
    ]
    return _call_parse_retry(model, messages, on_delta=on_delta)


SUMMARY_SYSTEM_PROMPT = """你是专业短剧编辑。请把给定的各集剧情摘要汇总为整部剧的全局摘要。
输出要求：3-5 句话，概括整部剧的主题、主线剧情、核心冲突与结局走向。
仅输出摘要正文，不要任何解释、不要列表、不要 markdown 标记。"""


def summarize_script(model: dict, episode_summaries: list, on_delta=None) -> str | None:
    """以各集摘要为输入，LLM 汇总剧本全局摘要；失败返回 None（调用方拼接兜底）。"""
    if not episode_summaries:
        return None
    body = "\n".join(f"【{s.get('title', '')}】\n{s.get('summary', '')}" for s in episode_summaries if s.get("summary"))
    if not body.strip():
        return None
    try:
        raw = call_chat(model, [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"各集剧情摘要：\n{body}"},
        ], temperature=0.3, stream=on_delta is not None, on_delta=on_delta)
        text = (raw or "").strip()
        return text[:500] if text else None
    except Exception:
        return None


def local_summary_fallback(episode_summaries: list) -> str:
    """本地拼接兜底（v1.31：LLM 未返回时退化为各集摘要拼接，字段永不为空）。"""
    parts = [s.get("summary", "").strip() for s in episode_summaries if s.get("summary", "").strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "；".join(parts)
