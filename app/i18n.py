"""工作台界面语言。

默认简体中文。只有显式选择才切换，不根据浏览器的语言列表自动改成英文：

- 查询参数 ``lang=en`` / ``lang=zh-CN``
- 请求头 ``X-VP-Lang``（页面脚本会带上）
- ``Accept-Language`` 恰好是 ``en`` 或 ``zh-CN``（页面会覆盖浏览器自带的列表）

含逗号或 q 值的浏览器语言列表一律忽略。
"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path

_LANG = ContextVar("vp_lang", default="zh-CN")
_PATH = Path(__file__).resolve().parent / "static" / "messages.json"
_CATALOG: dict[str, dict[str, str]] = json.loads(_PATH.read_text(encoding="utf-8"))

_EXACT: dict[str, str] = {}
_TEMPLATES: list[tuple[str, re.Pattern[str], list[str]]] = []


def _compile(text: str) -> tuple[re.Pattern[str], list[str]]:
    """把 ``{name}`` 占位变成捕获组。先转义，再把占位还原，避免把正文里的符号当成正则。"""
    names: list[str] = []
    pattern = re.escape(text)
    pattern = re.sub(r"\\\{(\w+)\\\}", lambda m: names.append(m.group(1)) or r"(.+?)", pattern)
    return re.compile("^" + pattern + "$"), names


def _index() -> None:
    _EXACT.clear()
    _TEMPLATES.clear()
    ranked = []
    for key, entry in _CATALOG.items():
        for text in (entry.get("zh-CN") or "", entry.get("en") or ""):
            if not text:
                continue
            if "{" in text:
                pattern, names = _compile(text)
                ranked.append((len(text), key, pattern, names))
            else:
                _EXACT.setdefault(text, key)
    ranked.sort(key=lambda item: item[0], reverse=True)
    _TEMPLATES.extend((key, pattern, names) for _n, key, pattern, names in ranked)


_index()


def current() -> str:
    return _LANG.get()


def use(lang: str):
    """切换当前请求的语言，返回可交给 ``reset`` 的 token。"""
    return _LANG.set(_normalize(lang))


def reset(token) -> None:
    _LANG.reset(token)


def _normalize(lang: str | None) -> str:
    value = (lang or "").strip().lower().replace("_", "-")
    if value in ("en", "en-us", "en-gb"):
        return "en"
    if value in ("zh", "zh-cn", "zh-hans"):
        return "zh-CN"
    return "zh-CN"


def negotiate(accept_language: str | None = None, query_lang: str | None = None,
              explicit: str | None = None) -> str:
    """解析本次请求的界面语言。浏览器的多语言列表不是用户选择。"""
    for raw in (query_lang, explicit):
        if raw and raw.strip():
            return _normalize(raw)
    header = (accept_language or "").strip()
    if not header or "," in header or ";" in header:
        return "zh-CN"
    if header.lower() in ("en", "zh-cn", "zh"):
        return _normalize(header)
    return "zh-CN"


def from_request(request) -> str:
    return negotiate(
        request.headers.get("accept-language"),
        request.query_params.get("lang"),
        request.headers.get("x-vp-lang"),
    )


def t(key: str, **kwargs) -> str:
    """按当前请求语言取文案。未知键原样返回，避免界面出现空白。"""
    return _format(key, current(), kwargs)


def t_zh(key: str, **kwargs) -> str:
    """固定中文。修网表时喂给模型的报错保持中文，避免界面语言改变修复行为。"""
    return _format(key, "zh-CN", kwargs)


def _format(key: str, lang: str, kwargs: dict) -> str:
    entry = _CATALOG.get(key) or {}
    text = entry.get(lang) or entry.get("zh-CN") or key
    return text.format(**kwargs) if kwargs else text


def translate(text: str, depth: int = 0) -> str:
    """把已知的中文或英文整句换成当前语言。认不出的原文保持不动。"""
    if text is None or depth > 4:
        return "" if text is None else str(text)
    raw = str(text)
    if not raw:
        return raw
    key = _EXACT.get(raw)
    if key:
        return t(key)
    for key, pattern, names in _TEMPLATES:
        match = pattern.fullmatch(raw)
        if not match:
            continue
        values = {name: translate(group, depth + 1) for name, group in zip(names, match.groups())}
        return _format(key, current(), values)
    return raw
