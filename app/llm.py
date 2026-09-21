"""OpenAI 兼容的 LLM 封装。模型/Key 全部走环境变量，不绑定供应商。

实现依据 DeepSeek 官方文档（https://api-docs.deepseek.com/zh-cn/）：
- 模型：`deepseek-flash`（默认）/ `deepseek-v4-pro`；上下文 1M、最大输出 384K；
  并发限制 flash 2500 / pro 500（账号级，超限 429）。
- 思考模式默认开启且 effort 默认 high，关闭需显式下发
  `extra_body={"thinking": {"type": "disabled"}}`。思考模式下 temperature /
  top_p 被服务端静默忽略（top_p 仅思考模式 0.95-1.0 有效）——因此思考开启时
  本模块不下发 temperature，避免"假象可调"。
- 思维链经 `reasoning_content` 字段返回（与 content 同级）；不带 tools 的调用
  无需回传。空 content 时回落 reasoning_content 的做法保留（项目实测）。
- JSON Output：`response_format={"type": "json_object"}`，官方要求 prompt 出现
  "json" 字样并给格式示例；已知有概率返回空 content、max_tokens 过小会截断
  （finish_reason=length）——chat_json 已做对应处理。
- 错误码：400 格式 / 401 认证失败 / 402 余额不足 / 422 参数 / 429 并发超限 /
  500、503 服务端故障。SDK 自带指数退避重试（含 429/5xx/连接类，默认 2 次），
  本模块再把剩余错误翻译成带官方解释的 LLMError。
- 上下文硬盘缓存默认开启且尽力而为：稳定前缀（system 消息固定在最前）可命中
  `prompt_cache_hit_tokens`，命中价约为未命中的 1/50——用量记入 last_usage。
- 长等待期间服务器用空行 / keep-alive 注释保活，10 分钟未开始推理会断连
  （SDK 已处理保活）；默认超时 480s 在此窗口内。

环境变量：
  CIRCUITPILOT_API_KEY            必填（也接受 DEEPSEEK_API_KEY / OPENAI_API_KEY）
  CIRCUITPILOT_BASE_URL           选填，默认 https://api.deepseek.com
  CIRCUITPILOT_MODEL              选填，默认 deepseek-flash
  CIRCUITPILOT_LLM_TIMEOUT        选填，单次调用超时秒数，默认 480（长思考需要）
  CIRCUITPILOT_LLM_RETRIES        选填，SDK 对超时/断连/429/5xx 的自动重试次数，默认 2
  CIRCUITPILOT_THINKING           选填，on/off，默认 on（调用处可用 thinking= 参数覆盖）
  CIRCUITPILOT_REASONING_EFFORT   选填，none/low/high/max，默认不传（服务端 high）
"""

from __future__ import annotations

import json
import os
import re

import openai
from openai import OpenAI

_BASE_URL = os.environ.get("CIRCUITPILOT_BASE_URL", "https://api.deepseek.com")
_MODEL = os.environ.get("CIRCUITPILOT_MODEL", "deepseek-flash")
_API_KEY = (os.environ.get("CIRCUITPILOT_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY", ""))
_TIMEOUT = float(os.environ.get("CIRCUITPILOT_LLM_TIMEOUT", "480"))
_RETRIES = int(os.environ.get("CIRCUITPILOT_LLM_RETRIES", "2"))

_client: OpenAI | None = None

# 最近一次调用的用量与结束原因（含上下文硬盘缓存命中，供成本/提速观察）
last_usage: dict = {}
last_finish_reason: str = ""

# 官方限速文档：user_id 需匹配 [a-zA-Z0-9-_]{1,512}，且不要含用户隐私
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

# 官方错误码表（quick_start/error_codes）的中文解释
_ERROR_HINTS = {
    400: "请求体格式错误（400），请对照返回的错误信息修正请求",
    401: "认证失败（401）：API Key 无效，请检查 CIRCUITPILOT_API_KEY",
    402: "余额不足（402）：请前往 platform.deepseek.com 充值",
    422: "参数错误（422）：请求参数不合法，请对照返回的错误信息修正",
    429: "并发超限（429）：账号并发已达上限（flash 2500 / pro 500），请稍后重试",
    500: "服务器故障（500）：请稍后重试，持续出现请联系官方",
    503: "服务器繁忙（503）：请稍后重试",
}

# 官方 effort 映射表：minimal→low，medium/xhigh→high（API 参考页）
_EFFORT_ALIAS = {"none": "none", "minimal": "low", "low": "low",
                 "medium": "high", "high": "high", "xhigh": "high", "max": "max"}


class LLMError(RuntimeError):
    """LLM 调用失败（附官方文档的解释，原始异常挂在 __cause__）。"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError("缺少 CIRCUITPILOT_API_KEY 环境变量")
        # SDK 的 max_retries 覆盖超时/断连/429/5xx，指数退避并尊重 Retry-After，
        # 官方错误码页对 429/500/503 的建议（稍后重试）由它承担
        _client = OpenAI(api_key=_API_KEY, base_url=_BASE_URL,
                         timeout=_TIMEOUT, max_retries=_RETRIES)
    return _client


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "on", "yes", "y", "enable", "enabled"):
        return True
    if v in ("0", "false", "off", "no", "n", "disable", "disabled"):
        return False
    return default


def _resolve_thinking(thinking: bool | None) -> bool:
    if thinking is None:
        return _env_flag("CIRCUITPILOT_THINKING", True)  # 官方默认开启
    return thinking


def _record_usage(u) -> None:
    global last_usage
    if u is None:
        return
    last_usage = {
        "prompt_tokens": getattr(u, "prompt_tokens", 0),
        "completion_tokens": getattr(u, "completion_tokens", 0),
        "total_tokens": getattr(u, "total_tokens", 0),
        "prompt_cache_hit_tokens": getattr(u, "prompt_cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": getattr(u, "prompt_cache_miss_tokens", 0),
        "reasoning_tokens": getattr(getattr(u, "completion_tokens_details", None),
                                    "reasoning_tokens", 0),
    }


def _build_kwargs(system: str, user: str, temperature: float,
                  thinking: bool | None, reasoning_effort: str | None,
                  max_tokens: int | None, user_id: str | None,
                  response_format) -> dict:
    thinking_on = _resolve_thinking(thinking)
    kwargs: dict = {
        "model": _MODEL,
        # system 固定在最前：官方 KV 缓存按前缀完整匹配，稳定前缀才命中
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if not thinking_on:
        # 思考模式下 temperature 被服务端静默忽略（官方文档），只在关思考时下发
        kwargs["temperature"] = temperature
    if reasoning_effort:
        effort = _EFFORT_ALIAS.get(str(reasoning_effort).lower())
        if effort is None:
            raise ValueError(f"reasoning_effort 只支持 none/low/high/max"
                             f"（及别名 minimal/medium/xhigh），收到: {reasoning_effort!r}")
        if thinking_on:
            kwargs["reasoning_effort"] = effort
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)
    if response_format is not None:
        kwargs["response_format"] = response_format
    extra = {"thinking": {"type": "enabled" if thinking_on else "disabled"}}
    if user_id:
        if not _USER_ID_RE.match(user_id):
            raise ValueError("user_id 只能含 [A-Za-z0-9_-] 且长度 1-512（官方限制）")
        extra["user_id"] = user_id
    kwargs["extra_body"] = extra
    return kwargs


def _translate_api_error(e: Exception) -> Exception:
    """按官方错误码表把 SDK 异常翻译成带解释的 LLMError。"""
    if isinstance(e, openai.APITimeoutError):
        return LLMError(f"请求超时（>{_TIMEOUT:.0f}s，SDK 已自动重试 {_RETRIES} 次）；"
                        f"长思考任务可调大 CIRCUITPILOT_LLM_TIMEOUT")
    if isinstance(e, openai.APIConnectionError):
        return LLMError(f"无法连接 {_BASE_URL}（SDK 已自动重试 {_RETRIES} 次）：{e}")
    if isinstance(e, openai.APIStatusError):
        code = getattr(e, "status_code", None) or 0
        hint = _ERROR_HINTS.get(code, f"接口返回 HTTP {code}")
        return LLMError(f"{hint}；原始信息：{getattr(e, 'message', e)}")
    return e


def chat(system: str, user: str, temperature: float = 0.2, *,
         thinking: bool | None = None, reasoning_effort: str | None = None,
         max_tokens: int | None = None, user_id: str | None = None,
         response_format=None) -> str:
    """单轮调用（非流式）。生成网表场景默认思考开启（官方默认），减少幻觉。

    thinking: None=跟随 CIRCUITPILOT_THINKING（默认开）。temperature 仅在
    思考关闭时才会真正生效（官方文档：思考模式下被静默忽略）。
    传输/限速类错误由 SDK 自动重试，其余翻译成带官方解释的 LLMError。"""
    global last_finish_reason
    kwargs = _build_kwargs(system, user, temperature, thinking, reasoning_effort,
                           max_tokens, user_id, response_format)
    try:
        resp = _get_client().chat.completions.create(**kwargs)
    except openai.OpenAIError as e:
        raise _translate_api_error(e) from e
    _record_usage(getattr(resp, "usage", None))
    choice = resp.choices[0]
    last_finish_reason = getattr(choice, "finish_reason", "") or ""
    msg = choice.message
    content = msg.content or ""
    if not content.strip():
        # 推理型模型超长思考后 content 偶发为空。
        # 只有思考文本里确实产出了代码块才回落；纯散文思考不能当网表
        # （曾把中文说明片段喂进静态检查器产生荒诞报错）。
        rc = getattr(msg, "reasoning_content", "") or ""
        if "```" in rc:
            content = rc
    return content


def chat_json(system: str, user: str, *, max_tokens: int | None = None,
              user_id: str | None = None, thinking: bool | None = None) -> dict:
    """JSON Output 模式（官方指南 guides/json_mode）。

    官方要求与已知问题，均已处理：
    - prompt 必须出现 "json" 字样（缺失时这里自动补一句提示）；
    - 有概率返回空 content（官方已知问题）→ 自动重试一次；
    - max_tokens 过小会截断（finish_reason=length）→ 报截断错而不是解析失败；
    - 解析失败时尝试截取最外层 {...} 再解一次（容忍模型偶尔的包裹文字）。
    格式示例仍应由调用方在 prompt 里给出（官方建议）。"""
    if "json" not in (system + user).lower():
        user = user + "\n请严格以 JSON 格式输出（json），不要输出 JSON 以外的文字。"
    text = ""
    for _ in range(2):
        text = chat(system, user, max_tokens=max_tokens, user_id=user_id,
                    thinking=thinking, response_format={"type": "json_object"})
        if text.strip():
            break
    if not text.strip():
        raise LLMError("JSON 模式连续返回空 content（官方已知问题），请调整 prompt 重试")
    if last_finish_reason == "length":
        raise LLMError("JSON 输出被 max_tokens 截断（finish_reason=length），"
                       "请调大 max_tokens 或精简输出要求")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise LLMError(f"JSON 解析失败，前 200 字符：{text[:200]!r}")


def chat_stream(system: str, user: str, temperature: float = 0.2, *,
                thinking: bool | None = None, reasoning_effort: str | None = None,
                max_tokens: int | None = None, user_id: str | None = None,
                yield_reasoning: bool = False):
    """流式调用：逐段 yield (kind, text)，kind ∈ {"reasoning", "content"}。

    官方文档（thinking_mode / API 参考）：
    - 流式 delta 中 reasoning_content 与 content 分开到达，须分别累计；
    - usage 需 stream_options={"include_usage": True} 才随最后一个 chunk 返回
      （记入 last_usage）；
    - 等待期间服务器的空行 / keep-alive 注释由 SDK 处理，无需关心。"""
    global last_finish_reason
    kwargs = _build_kwargs(system, user, temperature, thinking, reasoning_effort,
                           max_tokens, user_id, None)
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}
    try:
        stream = _get_client().chat.completions.create(**kwargs)
        for chunk in stream:
            _record_usage(getattr(chunk, "usage", None))
            for ch in getattr(chunk, "choices", None) or []:
                fr = getattr(ch, "finish_reason", None)
                if fr:
                    last_finish_reason = fr
                delta = getattr(ch, "delta", None)
                if delta is None:
                    continue
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    if yield_reasoning:
                        yield "reasoning", rc
                elif getattr(delta, "content", None):
                    yield "content", delta.content
    except openai.OpenAIError as e:
        raise _translate_api_error(e) from e


def extract_code_block(text: str) -> str:
    """从回复里提取 ``` 代码块内容（LLM 常把网表包在 markdown 代码块里）。"""
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    # 取最长的代码段作为网表
    candidates = [p.split("\n", 1)[-1] for p in parts[1:-1] if p.strip()]
    return max(candidates, key=len).strip() if candidates else text.strip()


# TODO(W1+): 相同请求的结果缓存（演示前预跑省钱省时）——key 取 (model, system, user) 哈希
