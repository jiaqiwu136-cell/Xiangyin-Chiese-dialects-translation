"""
乡音方言翻译平台 - LLM 服务层
==============================
统一使用 **OpenAI Chat Completions 兼容协议**（stream），因此所有配置为
`openai-compatible` 的服务商（通义千问、文心一言、智谱AI、DeepSeek、
原生 OpenAI 等）均能接入。

本模块分三层：
  1) _low_level  : 最基础的流式 HTTP 请求（requests），按 token 输出 SSE
  2) _chat_stream : 封装带 system/user messages 的流式聊天，产出纯文本 token
  3) 四个专用任务：infer_origin / translate_dialect_to_mandarin
                  / translate_mandarin_to_dialect / generate_culture
     每个任务通过精心设计的 system prompt，要求模型输出 **严格 JSON**；
     这些任务返回 generator，Flask 层会把它包装成 `text/event-stream` 响应。

设计原则：
  - **零 Mock 数据**：若未配置任何 provider，函数立即抛出异常，绝不伪造数据。
  - **只按 PRD 输出结构**：每个专用任务的输出 JSON Schema 严格对齐 PRD 描述。
"""

import json
import re
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import requests

from config import CONFIG, LLMProviderConfig


# ============================================================
# 自定义异常
# ============================================================

class LLMConfigError(Exception):
    """未配置或缺少 LLM 服务时抛出。"""
    pass


class LLMAPIError(Exception):
    """调用 LLM API 时网络 / 服务端错误。"""
    pass


def _ensure_provider(model_id: Optional[str]) -> Tuple[str, LLMProviderConfig]:
    """校验并解析 model_id；未配置任何服务商时抛 LLMConfigError。"""
    if not CONFIG.providers:
        raise LLMConfigError(
            "当前未配置任何大模型服务。请在 .env 中至少配置一个 "
            "LLM provider（如 DASHSCOPE_API_KEY、DASHSCOPE_BASE_URL 等），"
            "并复制 .env.example 为 .env 后填入真实配置。"
        )
    return CONFIG.resolve_model_id(model_id or "")


# ============================================================
# 底层流式请求 (SSE -> token generator)
# ============================================================

def _sse_stream(payload: Dict[str, Any],
                provider: LLMProviderConfig,
                model: str,
                timeout: Tuple[int, int] = (15, 300)
                ) -> Generator[str, None, None]:
    """
    向 /chat/completions 发起 stream=true 的 POST，
    产出 assistant message 内容的 delta（按 SSE data: {...} 切分）。
    遵循标准 OpenAI 字段：choices[0].delta.content。
    """
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = dict(payload)
    body.setdefault("model", model)
    body.setdefault("stream", True)

    try:
        with requests.post(
            url, headers=headers, json=body, stream=True, timeout=timeout
        ) as resp:
            if resp.status_code != 200:
                # 非 200：尽量读取错误信息
                try:
                    err = resp.text[:2000]
                except Exception:
                    err = str(resp.status_code)
                raise LLMAPIError(
                    f"LLM API 错误 (HTTP {resp.status_code}): {err}"
                )

            # 逐行解析 SSE
            buf = b""
            for chunk in resp.iter_content(chunk_size=1024):
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except Exception:
                        # 部分服务商可能非标准 SSE，跳过坏块
                        continue
                    # 标准字段
                    try:
                        delta = obj["choices"][0]["delta"].get("content")
                    except Exception:
                        delta = None
                    if delta:
                        yield delta
    except LLMAPIError:
        raise
    except requests.RequestException as e:
        raise LLMAPIError(f"LLM 请求失败: {e}") from e


def _chat_stream(system_prompt: str,
                 user_prompt: str,
                 *,
                 model_id: Optional[str] = None,
                 temperature: float = 0.7,
                 top_p: float = 0.9,
                 max_tokens: int = 4000,
                 extra_body: Optional[Dict[str, Any]] = None,
                 ) -> Tuple[str, Generator[str, None, None]]:
    """
    返回 (resolved_model_id, token_generator)。
    追加一段 instruction：禁止 markdown，只输出 JSON——但这放在各个专用任务的
    system prompt 里更稳妥，所以此函数只做通用封包。
    """
    model, provider = _ensure_provider(model_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    if extra_body:
        payload.update(extra_body)
    resolved = f"{provider.name}/{model}"
    return resolved, _sse_stream(payload, provider, model)


# ============================================================
# 任务一：归属地推理 LLM
# ============================================================

_INFER_ORIGIN_SYSTEM = """你是「中文方言归属地推理专家」。
职责：仅根据用户提供的一段方言文本，判断它最可能属于哪个地区的方言，并给出简短的推理依据。
严格约束：
  1. 你只能输出一个合法 JSON 对象，**绝不能** 输出任何 markdown、代码块、说明文字、前后缀。
  2. 必须按以下 Schema 输出（字段齐全即可，顺序不做要求）：
     {
       "origin":        "方言归属地名称，例如 四川话(成都片) / 东北官话 / 粤语广州话 / 闽南语泉州腔",
       "province":      "大致所属的省份或大区，例如 四川省 / 广东省 / 东北地区",
       "confidence":    0 到 1 之间的小数，两位精度，
       "candidates":    ["候选1", "候选2", "候选3"]  数量 1~3 个，
       "reasoning":     "一句话推理依据，30 字以内"
     }
  3. 如果文本是纯普通话或无法判断，origin 填「普通话/无法判断」，confidence 填 0.1~0.3，
     reasoning 说明判断失败的原因。
"""


def infer_origin_stream(dialect_text: str, *, model_id: Optional[str] = None
                       ) -> Tuple[str, Generator[str, None, None]]:
    """返回 (resolved_model_id, JSON_string_stream)。stream 即完整 JSON 的流式字符。"""
    user = f"请推理下面这段方言文本的归属地：\n\n{dialect_text}"
    return _chat_stream(
        _INFER_ORIGIN_SYSTEM,
        user,
        model_id=model_id,
        temperature=CONFIG.origin_temperature,
        max_tokens=800,
    )


# ============================================================
# 任务二：方言 -> 英语 三版本翻译（逐词对齐）
# ============================================================

_TRANSLATE_D2E_SYSTEM = """你是「方言 -> 英语」转译专家，熟稔中国各地方言与英语的地道表达。
职责：把用户提供的【方言原文】+【归属地信息】精确翻译成英语，按三种不同"温度/风格"输出三个版本，并给出方言原文每个字符切片与英文译文词范围的逐段对齐（alignment）。
严格约束：
  1. 你只能输出一个合法 JSON 对象，**绝不能** 输出任何 markdown、代码块、说明文字、前后缀。
  2. 必须按以下 Schema 输出：
     {
       "versions": [
         {
           "temperature": 0.3,
           "label":       "Faithful Literal 忠实直译（保留方言语态、语气词）",
           "translation": "English translation string",
           "alignment": [
             { "src_start":0, "src_end":2, "tgt_start":0, "tgt_end":3,
               "note":"short note on the dialect word, <=16 chars, empty if none" },
             ...按原文顺序，尽量覆盖原文所有汉字的切片；tgt 端（英语）可允许空隙/重叠，但需可对应。索引按 Unicode 字符位置（左闭右开）。
           ]
         },
         {
           "temperature": 0.7,
           "label":       "Natural 自然通顺（兼顾原味与英语习惯）",
           "translation": "...",
           "alignment": [ ... ]
         },
         {
           "temperature": 1.2,
           "label":       "Idiomatic 地道意译（按英语母语者习惯重写）",
           "translation": "...",
           "alignment": [ ... ]
         }
       ]
     }
  3. 三版本的 translation 不可完全相同，必须体现温度/风格差异。
  4. alignment 中 tgt_start/tgt_end 是英语译文字符索引（按 Unicode，左闭右开）。
  5. 若原文过短（≤5字），alignment 可只覆盖整句。
"""


def translate_d2e_stream(dialect_text: str,
                         origin_region: str,
                         *,
                         model_id: Optional[str] = None,
                         temperature_override: Optional[Tuple[float, float, float]] = None,
                         ) -> Tuple[str, Generator[str, None, None]]:
    t_low, t_mid, t_high = temperature_override or CONFIG.translate_temperatures
    user = (
        "【方言原文 Dialect Text】\n"
        f"{dialect_text}\n\n"
        "【归属地 Origin Region】\n"
        f"{origin_region}\n\n"
        f"请输出三种英语翻译版本（temperature {t_low} / {t_mid} / {t_high}），"
        "并严格遵守 system 里的 JSON Schema。"
    )
    return _chat_stream(
        _TRANSLATE_D2E_SYSTEM,
        user,
        model_id=model_id,
        temperature=t_mid,
        max_tokens=3500,
    )


# ============================================================
# 任务三：英语 -> 方言 带注音翻译
# ============================================================

_TRANSLATE_E2D_SYSTEM = """你是「英语 → 方言」转译专家。
职责：把用户提供的英语句子，翻译成目标方言的地道中文汉字写法，并为 **每个汉字** 生成分词注音（方言拼音 / 罗马字）。
严格约束：
  1. 你只能输出一个合法 JSON 对象，**绝不能** 输出任何 markdown、代码块、说明文字、前后缀。
  2. 必须按以下 Schema 输出：
     {
       "translation":   "方言译文的汉字字符串（使用本方言真实用字，不要用拼音代替字；不要写成普通话）",
       "pronunciation": [
         { "char": "明",  "pinyin": "mín"  },
         { "char": "天",  "pinyin": "tiān" },
         ... 与 translation 中每个 Unicode 字符 **一一对应**，长度必须完全相等。
         注意：
           - 标点符号、阿拉伯数字、外文字母，char 原样保留，pinyin 填空字符串 ""。
           - pinyin 必须使用 **带声调符号或调号数字** 的真实方言拼音 / 罗马字注音：
             粤语用粤拼 Jyutping，闽南语用 POJ/TL，吴语用吴拼或学界罗马字，其他方言使用学界常用方案；
             若无法确定具体方言罗马字，可写普通话拼音并尽可能贴合方音语气。
       ],
       "notes": "30~80 字简短说明，解释：(a) 方言用字的特殊之处 / 古字 / 俗字；(b) 关键音变 / 连读 / 语气。不需要可留空字符串。"
     }
  3. pronunciation 数组长度必须严格等于 len(translation)（按 Unicode 字符计数）。
  4. 不要输出普通话译文，必须是地道的目标方言。
"""


def translate_e2d_stream(english_text: str,
                         target_dialect: str,
                         *,
                         model_id: Optional[str] = None,
                         ) -> Tuple[str, Generator[str, None, None]]:
    user = (
        "【English Text 英语原文】\n"
        f"{english_text}\n\n"
        "【Target Dialect 目标方言】\n"
        f"{target_dialect}\n\n"
        "请输出地道的方言译文，并为每个汉字生成注音。严格遵守 system 里的 JSON Schema。"
    )
    return _chat_stream(
        _TRANSLATE_E2D_SYSTEM,
        user,
        model_id=model_id,
        temperature=CONFIG.zhuyin_temperature,
        max_tokens=3000,
    )


# ============================================================
# 任务四：风土科普 LLM
# ============================================================

_GENERATE_CULTURE_SYSTEM = """你是「中国方言与地域文化」科普作家。
职责：针对某个方言归属地，生成一段优雅、可读性强的结构化科普内容。
严格约束：
  1. 你只能输出一个合法 JSON 对象，**绝不能** 输出任何 markdown、代码块、说明文字、前后缀。
  2. 必须按以下 Schema 输出：
     {
       "title":   "标题，15 字以内，例如 四川话 · 巴蜀大地的麻辣官话",
       "summary": "100~150 字的总览简介。",
       "sections": [
         { "heading": "历史渊源", "body": "2~3 句段落。" },
         { "heading": "典型用语",
           "body":   "介绍若干代表性词汇，2~4句。",
           "examples": [
             { "src": "巴适得板", "tgt": "特别舒服、很好" },
             { "src": "雄起",      "tgt": "加油、振作" }
           ]
         },
         { "heading": "文化趣闻", "body": "2~3 句，最好有趣味故事。" }
       ],
       "tags":   ["不超过 5 个短标签"],
       "related_dialects": ["和它关系较近的 2~4 个方言名，可空数组"]
     }
  3. 字数不求多，要求精炼优雅，适合网页卡片式展示。
  4. 内容必须基于该方言归属地的真实文化特征，**禁止胡编乱造**；不确定时实事求是，
     不要编造虚假历史或词条。
"""


def generate_culture_stream(origin_region: str,
                            *,
                            model_id: Optional[str] = None,
                            ) -> Tuple[str, Generator[str, None, None]]:
    user = (
        "请为以下方言归属地生成科普卡片：\n\n"
        f"{origin_region}"
    )
    return _chat_stream(
        _GENERATE_CULTURE_SYSTEM,
        user,
        model_id=model_id,
        temperature=CONFIG.culture_temperature,
        max_tokens=2500,
    )


# ============================================================
# 辅助：把 stream 里累积的 JSON 对象（完整的）解析出来
# 用于在非流式场景（如科普缓存写入前）得到完整对象。
# ============================================================

async def _noop():
    return None


def collect_stream_sync(gen: Generator[str, None, None]) -> str:
    """把流式 token generator 消费完毕，拼成一个完整字符串。同步版本。"""
    buf: List[str] = []
    for tok in gen:
        buf.append(tok)
    return "".join(buf)


def parse_loose_json(raw: str) -> Any:
    """
    宽松 JSON 解析：
      1. 去掉首尾可能的 ```json / ``` 包裹
      2. 去掉前后空白、以及多余的 trailing 文本
    """
    if not raw:
        raise ValueError("模型返回为空")
    s = raw.strip()
    # 1) 去掉 ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", s)
    if m:
        s = m.group(1).strip()
    # 2) 若只是部分 JSON，尝试提取第一个 { ... } 配对
    first_brace = s.find("{")
    if first_brace == -1:
        first_brace = s.find("[")
    if first_brace == -1:
        raise ValueError(f"模型输出非 JSON: {s[:200]}")
    depth = 0
    in_str = False
    esc = False
    end = -1
    opener = s[first_brace]
    closer = "}" if opener == "{" else "]"
    for i in range(first_brace, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        # 没闭合，尝试直接从 first_brace 到最后 parse 看是否侥幸
        sub = s[first_brace:]
    else:
        sub = s[first_brace:end + 1]
    return json.loads(sub)
