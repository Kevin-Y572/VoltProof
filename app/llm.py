"""OpenAI 兼容的 LLM 封装。模型/Key 全部走环境变量，不绑定供应商。  [W1]

环境变量：
  CIRCUITPILOT_API_KEY        必填
  CIRCUITPILOT_BASE_URL       选填，默认 DeepSeek https://api.deepseek.com
  CIRCUITPILOT_MODEL          选填，默认 deepseek-flash（推理型，思考 1-13 分钟/任务）
  CIRCUITPILOT_LLM_TIMEOUT    选填，单次调用超时秒数，默认 480（flash 长思考需要）
"""

from __future__ import annotations

import os

import openai
from openai import OpenAI

_BASE_URL = os.environ.get("CIRCUITPILOT_BASE_URL", "https://api.deepseek.com")
_MODEL = os.environ.get("CIRCUITPILOT_MODEL", "deepseek-flash")
_API_KEY = (os.environ.get("CIRCUITPILOT_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY", ""))
_TIMEOUT = float(os.environ.get("CIRCUITPILOT_LLM_TIMEOUT", "480"))

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError("缺少 CIRCUITPILOT_API_KEY 环境变量")
        _client = OpenAI(api_key=_API_KEY, base_url=_BASE_URL,
                         timeout=_TIMEOUT, max_retries=1)
    return _client


def chat(system: str, user: str, temperature: float = 0.2) -> str:
    """单轮调用。生成网表场景 temperature 要低，减少幻觉。

    慢模型（如 deepseek-flash 生成复杂网表）偶发挂起：超时/连接类错误
    自动重试一次，其他异常立即抛出。"""
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = _get_client().chat.completions.create(
                model=_MODEL,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            msg = resp.choices[0].message
            content = msg.content or ""
            if not content.strip():
                # 推理型模型（如 deepseek-flash）超长思考后 content 偶发为空。
                # 只有思考文本里确实产出了代码块才回落；纯散文思考不能当网表
                # （曾把中文说明片段喂进静态检查器产生荒诞报错）。
                rc = getattr(msg, "reasoning_content", "") or ""
                if "```" in rc:
                    content = rc
            return content
        except openai.OpenAIError as e:
            last = e
            transient = isinstance(e, (openai.APITimeoutError, openai.APIConnectionError))
            if attempt == 0 and transient:
                continue
            raise
    raise last  # pragma: no cover


def extract_code_block(text: str) -> str:
    """从回复里提取 ``` 代码块内容（LLM 常把网表包在 markdown 代码块里）。"""
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    # 取最长的代码段作为网表
    candidates = [p.split("\n", 1)[-1] for p in parts[1:-1] if p.strip()]
    return max(candidates, key=len).strip() if candidates else text.strip()


# TODO(W1+): 相同请求的结果缓存（演示前预跑省钱省时）——key 取 (model, system, user) 哈希
