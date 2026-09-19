"""OpenAI 兼容的 LLM 封装。模型/Key 全部走环境变量，不绑定供应商。  [W1]

环境变量：
  CIRCUITPILOT_API_KEY   必填
  CIRCUITPILOT_BASE_URL  选填，默认智谱 https://open.bigmodel.cn/api/paas/v4/
  CIRCUITPILOT_MODEL     选填，默认 glm-4-flash
"""

from __future__ import annotations

import os

from openai import OpenAI

_BASE_URL = os.environ.get("CIRCUITPILOT_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
_MODEL = os.environ.get("CIRCUITPILOT_MODEL", "glm-4-flash")
_API_KEY = os.environ.get("CIRCUITPILOT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError("缺少 CIRCUITPILOT_API_KEY 环境变量")
        _client = OpenAI(api_key=_API_KEY, base_url=_BASE_URL)
    return _client


def chat(system: str, user: str, temperature: float = 0.2) -> str:
    """单轮调用。生成网表场景 temperature 要低，减少幻觉。"""
    resp = _get_client().chat.completions.create(
        model=_MODEL,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def extract_code_block(text: str) -> str:
    """从回复里提取 ``` 代码块内容（LLM 常把网表包在 markdown 代码块里）。"""
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    # 取最长的代码段作为网表
    candidates = [p.split("\n", 1)[-1] for p in parts[1:-1] if p.strip()]
    return max(candidates, key=len).strip() if candidates else text.strip()


# TODO(W1+): 相同请求的结果缓存（演示前预跑省钱省时）——key 取 (model, system, user) 哈希
