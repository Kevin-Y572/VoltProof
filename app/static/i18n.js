/* VoltProof 界面语言。默认 zh-CN，只认 localStorage 里的用户选择。
   文案表与服务端共用 messages.json。 */
(function () {
  "use strict";
  const STORAGE = "vp_lang";
  let catalog = {};
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "messages.json?v=0.4.0-i18n", false);
    xhr.send(null);
    if (xhr.status === 200 || xhr.status === 0) catalog = JSON.parse(xhr.responseText);
  } catch (e) {
    catalog = {};
  }

  let lang = "zh-CN";
  try {
    const saved = localStorage.getItem(STORAGE);
    if (saved === "en" || saved === "zh-CN") lang = saved;
  } catch (e) { /* 隐私模式保持默认中文 */ }

  const exact = new Map();
  const templates = [];

  function compile(text) {
    const names = [];
    const pattern = String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\{(\w+)\\\}/g, function (_, name) {
      names.push(name);
      return "(.+?)";
    });
    return { re: new RegExp("^" + pattern + "$"), names: names };
  }

  Object.keys(catalog).forEach(function (key) {
    const entry = catalog[key] || {};
    ["zh-CN", "en"].forEach(function (code) {
      const text = entry[code];
      if (!text) return;
      if (text.indexOf("{") >= 0) {
        const compiled = compile(text);
        templates.push({ key: key, len: text.length, re: compiled.re, names: compiled.names });
      } else if (!exact.has(text)) {
        exact.set(text, key);
      }
    });
  });
  templates.sort(function (a, b) { return b.len - a.len; });

  function format(text, vars) {
    if (!vars) return text;
    return String(text).replace(/\{(\w+)\}/g, function (_, name) {
      return vars[name] == null ? "" : String(vars[name]);
    });
  }

  function t(key, vars) {
    const entry = catalog[key];
    if (!entry) return key;
    return format(entry[lang] || entry["zh-CN"] || key, vars);
  }

  function tr(text, depth) {
    if (text == null) return "";
    const raw = String(text);
    if (!raw || (depth || 0) > 4) return raw;
    const key = exact.get(raw);
    if (key) return t(key);
    for (let i = 0; i < templates.length; i++) {
      const tpl = templates[i];
      const match = tpl.re.exec(raw);
      if (!match) continue;
      const vars = {};
      tpl.names.forEach(function (name, idx) { vars[name] = tr(match[idx + 1], (depth || 0) + 1); });
      return t(tpl.key, vars);
    }
    return raw;
  }

  function apiLang() { return lang === "en" ? "en" : "zh-CN"; }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach(function (el) { el.textContent = t(el.getAttribute("data-i18n")); });
    root.querySelectorAll("[data-i18n-html]").forEach(function (el) { el.innerHTML = t(el.getAttribute("data-i18n-html")); });
    root.querySelectorAll("[data-i18n-title]").forEach(function (el) { el.title = t(el.getAttribute("data-i18n-title")); });
    root.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) { el.placeholder = t(el.getAttribute("data-i18n-placeholder")); });
    root.querySelectorAll("[data-i18n-aria]").forEach(function (el) { el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria"))); });
    if (document.documentElement) document.documentElement.lang = apiLang();
    const titleKey = document.body && document.body.getAttribute("data-title-key");
    if (titleKey) document.title = t(titleKey);
    document.querySelectorAll("[data-set-lang]").forEach(function (btn) {
      const on = btn.getAttribute("data-set-lang") === lang;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setLang(next) {
    if (next !== "en" && next !== "zh-CN") return;
    lang = next;
    try { localStorage.setItem(STORAGE, lang); } catch (e) {}
    apply(document);
    document.dispatchEvent(new CustomEvent("vp-lang", { detail: lang }));
  }

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      const next = init ? Object.assign({}, init) : {};
      const headers = new Headers(next.headers || (typeof Request !== "undefined" && input instanceof Request ? input.headers : undefined));
      headers.set("Accept-Language", apiLang());
      headers.set("X-VP-Lang", apiLang());
      next.headers = headers;
      return origFetch.call(this, input, next);
    };
  }

  document.addEventListener("click", function (e) {
    const btn = e.target.closest && e.target.closest("[data-set-lang]");
    if (!btn) return;
    e.preventDefault();
    setLang(btn.getAttribute("data-set-lang"));
  });

  function boot() { apply(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.VPI18n = { lang: function () { return lang; }, apiLang: apiLang, t: t, tr: tr, setLang: setLang, apply: apply };
})();
