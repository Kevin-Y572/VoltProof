/* VoltProof 电路画板核心逻辑（board.js）
   从 /draw.html 抽出：独立画板页与统一主页共用同一份。依赖页面提供相同
   结构的 DOM（palette / stage(canvasWrap+svg+status) / side(rounds+props) /
   resultPanel / aiBar 与头部控件，元素 id 与原 draw.html 一致）。
   宿主集成经 window.VPBoard（见文件末尾）。 */
(() => {
"use strict";
const tt = (k, v) => VPI18n.t(k, v);
const trs = s => VPI18n.tr(s);
const partName = type => tt("part." + type);
const partHint = type => tt("hint." + type);
let simBusy = false;
let lastResult = null;
// ---------------------------------------------------------------------------
// 状态：元件 / 导线 / 标签 全部以整数网格坐标存储（1 格 = 20px）
// ---------------------------------------------------------------------------
const U = 20, W = 50, H = 35;
let comps = [];   // {id,type,x,y,rot,name,value}
let wires = [];   // {id,pts:[[gx,gy],...]}
let labels = [];  // {id,x,y,text}
let nextId = 1;
let sel = null;          // {kind:'comp'|'wire'|'label', id}
let placing = null;      // 放置中的类型
let ghost = null;        // {gx,gy}
let wiring = null;       // {pts:[[gx,gy],...], preview:[[gx,gy],...]}
let drag = null;         // {id, ox, oy}
let mouse = {gx:0, gy:0};
let interpreting = false;

const $ = id => document.getElementById(id);
const svg = $("svg");

const DEFS = {
  R:    {cn:"电阻",       pre:"R", pins:[[-2,0],[2,0]],            val:"10k",  vh:"阻值，如 10k / 4.7k / 1Meg"},
  C:    {cn:"电容",       pre:"C", pins:[[-2,0],[2,0]],            val:"100n", vh:"容值，如 100n / 10u"},
  L:    {cn:"电感",       pre:"L", pins:[[-2,0],[2,0]],            val:"1m",   vh:"感值，如 1m / 10u"},
  D:    {cn:"二极管",     pre:"D", pins:[[-2,0],[2,0]],            val:"",     vh:"自动配教学模型，无需填值"},
  QN:   {cn:"NPN 三极管", pre:"Q", pins:[[-2,0],[0,-2],[0,2]],     val:"",     vh:"引脚：左=基极，上=集电极，下=发射极；自动配模型"},
  MN:   {cn:"NMOS",       pre:"M", pins:[[-2,0],[0,-2],[0,2]],     val:"",     vh:"引脚：左=栅极，上=漏极，下=源极；自动配模型"},
  OP:   {cn:"理想运放",   pre:"X", pins:[[-2,-1],[-2,1],[2,0]],    val:"",     vh:"上输入=反相端(−)，下输入=同相端(+)"},
  VDC:  {cn:"直流电压源", pre:"V", pins:[[-2,0],[2,0]],            val:"DC 12", vh:"如 DC 12（左侧引脚为 +）"},
  VSIN: {cn:"正弦电压源", pre:"V", pins:[[-2,0],[2,0]],            val:"SIN(0 1 1k)", vh:"SIN(偏置 幅度 频率)，如 SIN(0 1 1k)"},
  ISRC: {cn:"电流源",     pre:"I", pins:[[-2,0],[2,0]],            val:"DC 1m", vh:"如 DC 1m"},
  GND:  {cn:"接地",       pre:"",  pins:[[0,0]],                   val:"",     vh:"所在网络 = 节点 0"},
  LBL:  {cn:"网络标签",   pre:"",  pins:[],                         val:"net1", vh:"网络名：字母开头，如 in / out / vcc"},
};

// ---------------------------------------------------------------------------
// 项目持久化：画布是项目资产（服务端 project.json 权威存储，随工作区走）
// 编辑防抖 PUT；启动 GET 载入；旧版 localStorage 画布一次性迁移后清除
// ---------------------------------------------------------------------------
let saveTimer = null;
let projectLoaded = false, projectRevision = 0, projectVersion = "", projectDirty = false;
let projectRequirements=[];
let lastSavedPayload = "", saveQueue = Promise.resolve(), projectMessage = "";
let loadGeneration = 0;
let previewMeta = null, lastResultTask = "", activeAiRequest = null;
const stateBadge = document.createElement("span"); stateBadge.className = "proof-state";
$("title").after(stateBadge);
const requirementsBtn = document.createElement("button"); requirementsBtn.className="btn"; requirementsBtn.textContent=tt("btn.accept");
requirementsBtn.onclick = () => VPProof.editRequirements().catch(e => { projectMessage=e.message; updateProjectState(); });
$("simBtn").before(requirementsBtn);
const projectReport = document.createElement("a"); projectReport.className="btn"; projectReport.textContent=tt("btn.report"); projectReport.href="/api/project/report?lang=" + encodeURIComponent(VPI18n.apiLang()); projectReport.target="_blank";
requirementsBtn.before(projectReport);
const compareBtn=document.createElement('button');compareBtn.className='btn';compareBtn.textContent=tt("btn.compare");
compareBtn.onclick=()=>VPProof.compareVersions().catch(e=>{projectMessage=e.message;updateProjectState();});
projectReport.before(compareBtn);

function isDefaultTitle(name) {
  const raw = String(name || "").trim();
  return raw === "未命名电路" || raw === "Untitled circuit";
}
function applyTitle(name) {
  $("title").value = !name || isDefaultTitle(name) ? tt("untitled") : name;
}
function projectPayload() {
  return {name:$("title").value || tt("untitled"), canvas:{comps,wires,labels},
    analysis:{type:$("analysis").value,tstop:$("tstop").value,fstart:$("fstart").value,fstop:$("fstop").value},
    draft_netlist:comps.length ? buildNetlist() : ""};
}
function updateProjectState() {
  stateBadge.textContent = (projectMessage ? trs(projectMessage) : "") || `${projectVersion ? tt("state.version", {id: projectVersion.slice(0,8)}) : tt("state.draft")} · ${projectDirty ? tt("state.dirty") : tt("state.saved")}`;
  const note = document.getElementById("dirtyNote");
  if (note) note.hidden = !projectDirty;
}
function save() {
  if (!projectLoaded) return;
  if (JSON.stringify(projectPayload()) !== lastSavedPayload) { projectDirty=true; updateProjectState(); }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => flushProject().catch(e => { projectMessage=e.message; updateProjectState(); }), 600);
}
function flushProject() {
  clearTimeout(saveTimer);
  saveQueue = saveQueue.catch(()=>{}).then(async () => {
      if (!projectLoaded) throw new Error(tt("project.loading"));
      const generation=loadGeneration, payload = projectPayload(), serialized = JSON.stringify(payload);
      if (serialized === lastSavedPayload) return;
      const r = await fetch("/api/project", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({...payload, expected_revision:projectRevision}),
      });
      const d=await r.json(); if (!r.ok) throw new Error(trs(d.error) || tt("project.saveFail"));
      if(generation!==loadGeneration) return;
      projectRevision=d.revision; projectDirty=!!d.dirty; lastSavedPayload=serialized; projectMessage=""; updateProjectState();
  });
  return saveQueue;
}

function applyProjectCanvas(p) {
  const cv = p && p.canvas;
  comps=[]; wires=[]; labels=[]; nextId=1; sel=null; wiring=null; placing=null;
  if (cv && Array.isArray(cv.comps) && cv.comps.length) {
    let id = 1;
    comps = cv.comps; wires = cv.wires || []; labels = cv.labels || [];
    for (const c of comps) c.id = c.id ?? id++;
    for (const w of wires) w.id = w.id ?? id++;
    for (const l of labels) l.id = l.id ?? id++;
    nextId = Math.max(1, ...comps.map(c => c.id), ...wires.map(w => w.id),
                      ...labels.map(l => l.id)) + 1;
  }
  if (p) applyTitle(p.name);
  const an = p && p.analysis;
  if (an && an.type) {
    $("analysis").value = an.type;
    if (an.tstop) $("tstop").value = an.tstop;
    if (an.fstart) $("fstart").value = an.fstart;
    if (an.fstop) $("fstop").value = an.fstop;
  }
  $("analysis").onchange();
}

async function loadProject() {
  loadGeneration++;
  projectLoaded=false; clearTimeout(saveTimer);
  let p = null;
  try {
    p = await (await fetch("/api/project")).json();
  } catch (e) { projectMessage=tt("project.loadFail"); updateProjectState(); return; }
  applyProjectCanvas(p);
  const hasServerCanvas = !!(p && p.canvas && p.canvas.comps && p.canvas.comps.length);
  let migrated=false;
  if (!hasServerCanvas) {
    // 一次性迁移：旧版 localStorage 画布上传进项目后清除
    try {
      const old = JSON.parse(localStorage.getItem("vp_draw_v1") || "null");
      if (old && Array.isArray(old.comps) && old.comps.length) {
        applyProjectCanvas(old);
        if (old.title) $("title").value = old.title;
        migrated=true;
      }
      localStorage.removeItem("vp_draw_v1");
    } catch (e) { /* 迁移失败不阻塞 */ }
  }
  applyTitle(p && p.name);
  projectRevision=p?.revision ?? 0; projectVersion=p?.version_id || ""; projectDirty=!!p?.dirty;
  projectRequirements=p?.requirements || [];
  projectMessage=""; lastSavedPayload=migrated ? "" : JSON.stringify(projectPayload()); projectLoaded=true;
  const generation = loadGeneration;
  const restoreId = VPProof.projectLoadPanel({
    dirty: projectDirty, evidenceId: p?.evidence_id || "",
    hasCanvas: !!(p && p.canvas && p.canvas.comps && p.canvas.comps.length),
  }) === "restore" ? p.evidence_id : "";
  if (restoreId) {
    verifiedHTML = ""; verifiedStamp = ""; verifiedOK = false; resultMode = "idle"; blockedKey = "";
    const panel = $("resultPanel");
    panel.innerHTML = ""; panel.style.display = "none";
  }
  render(); updateProjectState();
  updateAiPlaceholder();
  if (restoreId) restoreEvidence(restoreId, generation);
}

async function restoreEvidence(taskId, generation) {
  try {
    const r = await fetch("/api/files/" + encodeURIComponent(taskId) + "/evidence.json");
    if (!r.ok || generation !== loadGeneration) return;
    const ev = await r.json();
    if (generation !== loadGeneration || projectDirty) return;
    ev.task_id = ev.task_id || taskId;
    if (!ev.waveform_b64) {
      const img = await fetch("/api/files/" + encodeURIComponent(taskId) + "/wave.png");
      if (img.ok && generation === loadGeneration) {
        const bytes = new Uint8Array(await img.arrayBuffer());
        let binary = "";
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        ev.waveform_b64 = btoa(binary);
      }
    }
    if (generation !== loadGeneration || projectDirty) return;
    renderResult(ev, circuitStamp());
    resultArmed = true;
  } catch (e) { /* 证据缺失时保持空白，不把已保存电路标成过期 */ }
}

// ---------------------------------------------------------------------------
// 几何：旋转 / 引脚绝对坐标
// ---------------------------------------------------------------------------
function rotPt(p, rot) {  // SVG rotate(90°*rot)（顺时针，y 向下）：(x,y)->(-y,x)
  let [x, y] = p;
  for (let i = 0; i < (rot & 3); i++) { const t = x; x = -y; y = t; }
  return [x, y];
}
function compPins(c) {
  return DEFS[c.type].pins.map(p => {
    const [dx, dy] = rotPt(p, c.rot);
    return [c.x + dx, c.y + dy];
  });
}
const inb = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ---------------------------------------------------------------------------
// 元件符号（局部坐标绘制，单位=格；外层 g 负责 translate+rotate）
// ---------------------------------------------------------------------------
// 符号内部坐标为格单位（1 格 = U px），由外层 g 的 scale(U) 放大；
// 描边/字号因此也用单位制（0.08 单位 ≈ 1.6px）
const ST = `fill="none" stroke="#c8d2e8" stroke-linecap="round" stroke-linejoin="round"`;
function symbolSVG(type) {
  const tx = (t, x, y, size, fill, anchor) =>
    `<text x="${x}" y="${y}" font-size="${size}" fill="${fill || "#9aa1b2"}" text-anchor="${anchor || "middle"}" font-family="Consolas,monospace" stroke="none">${t}</text>`;
  switch (type) {
    case "R": return `<polyline points="-2,0 -1.4,0 -1.1,-0.42 -0.55,0.42 0,-0.42 0.55,0.42 1.1,-0.42 1.4,0 2,0" ${ST}/>`;
    case "C": return `<line x1="-2" y1="0" x2="-0.18" y2="0" ${ST}/><line x1="-0.18" y1="-0.62" x2="-0.18" y2="0.62" ${ST}/><line x1="0.18" y1="-0.62" x2="0.18" y2="0.62" ${ST}/><line x1="0.18" y1="0" x2="2" y2="0" ${ST}/>`;
    case "L": return `<line x1="-2" y1="0" x2="-1.6" y2="0" ${ST}/><path d="M -1.6 0 a .4 .4 0 1 1 .8 0 a .4 .4 0 1 1 .8 0 a .4 .4 0 1 1 .8 0 a .4 .4 0 1 1 .8 0" ${ST}/><line x1="1.6" y1="0" x2="2" y2="0" ${ST}/>`;
    case "D": return `<line x1="-2" y1="0" x2="-0.7" y2="0" ${ST}/><polygon points="-0.7,-0.55 -0.7,0.55 0.2,0" fill="#c8d2e8" stroke="none"/><line x1="0.2" y1="-0.55" x2="0.2" y2="0.55" ${ST}/><line x1="0.2" y1="0" x2="2" y2="0" ${ST}/>`;
    case "QN": return `<line x1="-2" y1="0" x2="-0.6" y2="0" ${ST}/><line x1="-0.6" y1="-0.75" x2="-0.6" y2="0.75" ${ST}/><line x1="0" y1="-2" x2="0" y2="-0.45" ${ST}/><line x1="-0.6" y1="-0.3" x2="0" y2="-0.45" ${ST}/><line x1="0" y1="0.45" x2="0" y2="2" ${ST}/><line x1="-0.6" y1="0.3" x2="0" y2="0.45" ${ST}/><polygon points="-0.05,0.1 -0.1,0.52 0.28,0.32" fill="#c8d2e8" stroke="none"/>`;
    case "MN": return `<line x1="-2" y1="0" x2="-0.85" y2="0" ${ST}/><line x1="-0.85" y1="-0.75" x2="-0.85" y2="0.75" ${ST}/><line x1="-0.45" y1="-0.75" x2="-0.45" y2="0.75" ${ST}/><line x1="0" y1="-2" x2="0" y2="-0.5" ${ST}/><line x1="-0.45" y1="-0.5" x2="0" y2="-0.5" ${ST}/><line x1="0" y1="0.5" x2="0" y2="2" ${ST}/><line x1="-0.45" y1="0.5" x2="0" y2="0.5" ${ST}/><polygon points="-0.32,0.34 -0.02,0.5 -0.32,0.66" fill="#c8d2e8" stroke="none"/>`;
    case "OP": return `<polygon points="-1,1.3 -1,-1.3 1.7,0" ${ST}/><line x1="-2" y1="-1" x2="-1" y2="-1" ${ST}/><line x1="-2" y1="1" x2="-1" y2="1" ${ST}/><line x1="1.7" y1="0" x2="2" y2="0" ${ST}/>${tx("−", -0.72, -0.82, 0.5)}${tx("+", -0.72, 1.28, 0.5)}`;
    case "VDC": case "VSIN": {
      let inner = tx("+", -0.32, -0.08, 0.45, "#c8d2e8") + tx("−", 0.3, -0.08, 0.45, "#c8d2e8");
      if (type === "VSIN") inner = `<path d="M -0.45 0.12 q .18 -.5 .36 0 q .18 .5 .36 -.12" ${ST}/>`;
      return `<line x1="-2" y1="0" x2="-0.72" y2="0" ${ST}/><circle cx="0" cy="0" r="0.72" ${ST}/>${inner}<line x1="0.72" y1="0" x2="2" y2="0" ${ST}/>`;
    }
    case "ISRC": return `<line x1="-2" y1="0" x2="-0.72" y2="0" ${ST}/><circle cx="0" cy="0" r="0.72" ${ST}/><line x1="-0.35" y1="0" x2="0.35" y2="0" stroke="#c8d2e8" stroke-width="0.07"/><polygon points="0.3,-0.22 0.3,0.22 0.6,0" fill="#c8d2e8" stroke="none"/><line x1="0.72" y1="0" x2="2" y2="0" ${ST}/>`;
    case "GND": return `<line x1="0" y1="0" x2="0" y2="0.5" ${ST}/><line x1="-0.55" y1="0.5" x2="0.55" y2="0.5" ${ST}/><line x1="-0.35" y1="0.82" x2="0.35" y2="0.82" ${ST}/><line x1="-0.15" y1="1.14" x2="0.15" y2="1.14" ${ST}/>`;
    case "LBL": return `<circle cx="0" cy="0" r="0.14" fill="#e6a23c" stroke="none"/>`;
  }
  return "";
}

// ---------------------------------------------------------------------------
// 连通性：并查集 over 网格点（导线逐点栅格化；引脚=点；共点即连通）
// ---------------------------------------------------------------------------
function wirePoints(w) {  // 折线覆盖的所有网格点
  const pts = [];
  for (let i = 0; i + 1 < w.pts.length; i++) {
    const [x1, y1] = w.pts[i], [x2, y2] = w.pts[i + 1];
    const dx = Math.sign(x2 - x1), dy = Math.sign(y2 - y1);
    let x = x1, y = y1;
    pts.push([x, y]);
    while (x !== x2 || y !== y2) { x += dx; y += dy; pts.push([x, y]); }
  }
  return pts;
}
const key = (x, y) => x + "," + y;

function connectivity() {
  const parent = {};
  const find = k => { while (parent[k] !== k) { parent[k] = parent[parent[k]]; k = parent[k]; } return k; };
  const uni = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[a] = b; };
  const add = k => { if (!(k in parent)) parent[k] = k; };

  // 导线：整条折线一个导体
  const wireCover = new Map();  // point -> 该点上经过的导线 id 集合
  for (const w of wires) {
    const ks = [];
    for (const [x, y] of wirePoints(w)) { const k = key(x, y); add(k); ks.push(k); }
    const root = ks[0];
    for (const k of ks) uni(root, k);
    for (const k of ks) {
      if (!wireCover.has(k)) wireCover.set(k, new Set());
      wireCover.get(k).add(w.id);
    }
  }
  // 引脚：先注册（即使悬空也要有网络）
  const pinAt = [];
  for (const c of comps) {
    const pins = compPins(c);
    pins.forEach((p, i) => { const k = key(p[0], p[1]); add(k); pinAt.push({ comp: c, idx: i, k, x: p[0], y: p[1] }); });
  }
  // 引脚互相共点 → 直接连通（引脚与导线共点也由并查集自然合并）
  return { parent, find, pinAt, wireCover };
}

// 网络命名：GND→0；标签命名；其余 n1..nK。返回 {nets: Map(rootKey->name), pinNet: Map(pinKey->name), info}
function netNames() {
  const { parent, find, pinAt, wireCover } = connectivity();
  const roots = new Map();  // root -> {pins:[], labels:[], gnd:false}
  for (const k of Object.keys(parent)) {
    const r = find(k);
    if (!roots.has(r)) roots.set(r, { pins: [], labels: [], gnd: false });
  }
  for (const p of pinAt) {
    const r = find(p.k);
    const info = roots.get(r);
    info.pins.push(p);
    if (p.comp.type === "GND") info.gnd = true;
  }
  const labelErrors = [];
  for (const l of labels) {
    const k = key(l.x, l.y);
    if (!(k in parent)) { labelErrors.push(tt("err.label.off", {name: l.text})); continue; }
    const r = find(k);
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(l.text)) { labelErrors.push(tt("err.label.bad", {name: l.text})); continue; }
    roots.get(r).labels.push(l.text);
  }
  const used = new Set(["0"]);
  const nets = new Map();  // root -> name
  let n = 0;
  for (const [r, info] of roots) {
    if (info.gnd) { nets.set(r, "0"); continue; }
    if (info.labels.length) {
      const name = info.labels[0];
      if (info.labels.length > 1) labelErrors.push(tt("err.label.multi", {names: info.labels.join(" / ")}));
      if (used.has(name.toLowerCase())) labelErrors.push(tt("err.label.dup", {name}));
      used.add(name.toLowerCase());
      nets.set(r, name);
      continue;
    }
    if (!info.pins.length && !wireCover.size) continue;  // 空网络不命名
    let name;
    do { n++; name = "n" + n; } while (used.has(name));
    used.add(name);
    nets.set(r, name);
  }
  const pinNet = new Map();
  for (const p of pinAt) pinNet.set(p, nets.get(find(p.k)) || null);
  return { nets, pinNet, pinAt, find, labelErrors };
}

// ---------------------------------------------------------------------------
// 预检：连线错误在画布上直接标红（后端还会再拦一道）
// ---------------------------------------------------------------------------
function precheck() {
  const errors = [], warnings = [];
  const { nets, pinAt, find, labelErrors } = netNames();
  for (const e of labelErrors) errors.push(e);

  const rootPins = new Map();
  for (const p of pinAt) {
    const r = find(p.k);
    if (!rootPins.has(r)) rootPins.set(r, []);
    rootPins.get(r).push(p);
  }
  const floating = [];
  for (const [r, ps] of rootPins) {
    if (ps.length === 1 && ps[0].comp.type !== "GND" && ps[0].comp.type !== "LBL")
      floating.push(ps[0]);
  }
  for (const p of floating)
    errors.push(tt("err.float", {name: p.comp.name || partName(p.comp.type), pin: p.idx + 1}));
  if (comps.length && !comps.some(c => c.type === "GND"))
    errors.push(tt("err.gnd"));
  if (comps.length && !comps.some(c => c.type.startsWith("V") || c.type === "ISRC"))
    errors.push(tt("err.src"));
  for (const c of comps) {
    const def = DEFS[c.type];
    if (def.pre && !new RegExp("^" + def.pre, "i").test(c.name || ""))
      errors.push(tt("err.prefix", {kind: partName(c.type), pre: def.pre, name: c.name || tt("err.prefix.empty")}));
    if ((c.type === "R" || c.type === "C" || c.type === "L" || c.type === "VDC" || c.type === "VSIN" || c.type === "ISRC") && !String(c.value || "").trim())
      errors.push(tt("err.value", {name: c.name || partName(c.type)}));
    if (/\d+M(?![A-Za-z])/.test(String(c.value || "")))
      errors.push(tt("err.meg", {name: c.name, value: c.value}));
  }
  // 有导线但网上一个引脚都没有 → 孤立导线（移动元件后常见）
  for (const w of wires) {
    const r = find(key(w.pts[0][0], w.pts[0][1]));
    if ((rootPins.get(r) || []).length === 0) warnings.push(tt("err.orphan"));
  }
  return { errors, warnings, floating };
}

// ---------------------------------------------------------------------------
// 网表合成
// ---------------------------------------------------------------------------
function buildNetlist() {
  const conn = connectivity();
  const { nets } = netNames();
  const netOf = c => compPins(c).map(p => nets.get(conn.find(key(p[0], p[1]))) || "?");

  const cards = [];
  let hasQ = false, hasM = false, hasD = false, hasOP = false;
  for (const c of comps) {
    const n = netOf(c);
    switch (c.type) {
      case "R": case "C": case "L":
        cards.push(`${c.name} ${n[0]} ${n[1]} ${String(c.value).trim()}`); break;
      case "D":
        cards.push(`${c.name} ${n[0]} ${n[1]} DMOD`); hasD = true; break;
      case "QN":
        cards.push(`${c.name} ${n[1]} ${n[0]} ${n[2]} QNPN`); hasQ = true; break;  // C B E
      case "MN":
        cards.push(`${c.name} ${n[1]} ${n[0]} ${n[2]} ${n[2]} MNMOS`); hasM = true; break;  // D G S B
      case "OP":
        cards.push(`${c.name} ${n[0]} ${n[1]} ${n[2]} OPAMP`); hasOP = true; break;  // in− in+ out
      case "VDC": case "VSIN": {
        let v = String(c.value).trim();
        const an = $("analysis").value;
        if (an === "ac" && !/\bAC\b/i.test(v)) v += " AC 1";
        cards.push(`${c.name} ${n[0]} ${n[1]} ${v}`); break;
      }
      case "ISRC":
        cards.push(`${c.name} ${n[0]} ${n[1]} ${String(c.value).trim()}`); break;
    }
  }
  const models = [];
  if (hasQ) models.push(".model QNPN NPN (bf=150)");
  if (hasM) models.push(".model MNMOS NMOS (kp=50u vto=1)");
  if (hasD) models.push(".model DMOD D (is=1e-14)");
  if (hasOP) models.push(".subckt OPAMP nn np vout\nE1 vout 0 np nn 200k\n.ends");

  const an = $("analysis").value;
  let cmd;
  if (an === "op") cmd = "op";
  else if (an === "tran") {
    const ts = ($("tstop").value || "10m").trim().replace(/^\./, "");
    cmd = `tran ${tstepOf(ts)} ${ts}`;
  } else {
    const f1 = ($("fstart").value || "10").trim(), f2 = ($("fstop").value || "100k").trim();
    cmd = `ac dec 20 ${f1} ${f2}`;
  }
  // write 全部非地网络（上限 40 个）
  const seen = new Set(), wnodes = [];
  for (const name of nets.values()) {
    if (name === "0" || seen.has(name)) continue;
    seen.add(name);
    if (wnodes.length < 40) wnodes.push(`v(${name})`);
  }
  const title = ($("title").value || tt("untitled")).replace(/\n/g, " ");
  return [`* ${title} -- VoltProof 画板生成`, ...cards, ...models,
          ".control", "set filetype=ascii", cmd,
          `write out.raw ${wnodes.join(" ")}`, ".endc", ".end"].join("\n") + "\n";
}
// tstep = tstop/1000，沿用原单位字面量（5m → 0.005m，ngspice 直接解析）
function tstepOf(ts) {
  const m = /^(\d*\.?\d+)([tgmkunpf]?)$/i.exec(ts.trim());
  if (!m) return "1u";
  const unit = m[2] || "";
  return String(parseFloat(m[1]) / 1000) + unit;
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
const px = g => g * U;

function render(withProps = true) {
  const pre = precheck();
  const floatKeys = new Set(pre.floating.map(p => key(p.x, p.y)));
  const conn = connectivity();
  let s = "";
  // 网格
  let dots = "";
  for (let x = 0; x <= W; x++) for (let y = 0; y <= H; y++) dots += `${px(x)},${px(y)} `;
  s += `<polyline points="${dots.trim()}" fill="none" stroke="#1a1e29" stroke-width="1"/>`;
  // 边框
  s += `<rect x="0" y="0" width="${px(W)}" height="${px(H)}" fill="none" stroke="#222739"/>`;

  // 导线
  for (const w of wires) {
    const on = sel && sel.kind === "wire" && sel.id === w.id;
    const pts = w.pts.map(p => px(p[0]) + "," + px(p[1])).join(" ");
    s += `<polyline points="${pts}" fill="none" stroke="${on ? "var(--acc)" : "#8fa3c8"}" stroke-width="2" data-wire="${w.id}" style="cursor:pointer"/>`;
  }
  // 结点圆点（语义与并查集一致）：≥2 条导线共享，或导线+引脚相接
  const wiresAt = new Map();  // point -> 覆盖它的导线数
  for (const w of wires)
    for (const [x, y] of new Set(wirePoints(w).map(p => key(p[0], p[1])))) {
      const k = key(x, y);
      wiresAt.set(k, (wiresAt.get(k) || 0) + 1);
    }
  const pinsAt = new Map();
  for (const p of conn.pinAt) {
    const k = key(p.x, p.y);
    pinsAt.set(k, (pinsAt.get(k) || 0) + 1);
  }
  const dotAt = new Set();
  for (const [k, n] of wiresAt) if (n >= 2 || pinsAt.get(k)) dotAt.add(k);
  for (const k of pinsAt.keys()) if (wiresAt.get(k)) dotAt.add(k);
  for (const k of dotAt) {
    const [x, y] = k.split(",").map(Number);
    s += `<circle cx="${px(x)}" cy="${px(y)}" r="3" fill="#8fa3c8" pointer-events="none"/>`;
  }

  // 网络标签
  for (const l of labels) {
    const on = sel && sel.kind === "label" && sel.id === l.id;
    s += `<g data-label="${l.id}" style="cursor:pointer">
      <rect x="${px(l.x) - 8}" y="${px(l.y) - 26}" width="${Math.max(30, l.text.length * 8 + 10)}" height="18" rx="4" fill="${on ? "#2a3d5f" : "#1d2b45"}" stroke="${on ? "var(--acc)" : "#33415e"}"/>
      <text x="${px(l.x) - 3}" y="${px(l.y) - 13}" font-size="11.5" fill="#e6a23c" font-family="Consolas,monospace">${esc(l.text)}</text>
      <line x1="${px(l.x)}" y1="${px(l.y) - 8}" x2="${px(l.x)}" y2="${px(l.y)}" stroke="#e6a23c" stroke-width="1"/>
      </g>`;
  }

  // 元件（选中态：剥掉符号里写死的描边色，让外层 g 的颜色生效；
  // scale(U)：符号内部坐标是格单位，viewBox 是像素）
  for (const c of comps) {
    const on = sel && sel.kind === "comp" && sel.id === c.id;
    s += `<g transform="translate(${px(c.x)},${px(c.y)}) rotate(${(c.rot & 3) * 90}) scale(${U})" data-comp="${c.id}" style="cursor:move">
      <g stroke="${on ? "var(--acc)" : "#c8d2e8"}" stroke-width="0.08">${symbolSVG(c.type).replaceAll('stroke="#c8d2e8"', "")}</g></g>`;
    // 名称/值（不随旋转，保持水平）
    if (c.type !== "GND" && c.type !== "LBL") {
      if (c.name) s += `<text x="${px(c.x)}" y="${px(c.y) - 0.55 * U}" font-size="11" fill="#7f95c0" text-anchor="middle" font-family="Consolas,monospace" pointer-events="none">${esc(c.name)}</text>`;
      if (c.value && c.type !== "OP" && c.type !== "QN" && c.type !== "MN" && c.type !== "D")
        s += `<text x="${px(c.x)}" y="${px(c.y + 1.45)}" font-size="10.5" fill="#8892a8" text-anchor="middle" font-family="Consolas,monospace" pointer-events="none">${esc(String(c.value).slice(0, 14))}</text>`;
    }
    // 引脚热点
    compPins(c).forEach((p, i) => {
      const fk = floatKeys.has(key(p[0], p[1]));
      s += `<circle cx="${px(p[0])}" cy="${px(p[1])}" r="4.5" fill="${fk ? "var(--err)" : "var(--acc)"}" opacity="${fk ? 0.95 : 0.75}" data-pin="${c.id}:${i}" style="cursor:crosshair"/>`;
    });
  }

  // 放置幽灵 / 连线预览
  if (placing && ghost) {
    s += `<g transform="translate(${px(ghost.gx)},${px(ghost.gy)}) rotate(${((placingRot || 0) & 3) * 90}) scale(${U})" opacity="0.45" stroke-width="0.08">${symbolSVG(placing)}</g>`;
  }
  if (wiring && mouse.gx >= 0) {
    const last = wiring.pts[wiring.pts.length - 1];
    const path = [last, [mouse.gx, last[1]], [mouse.gx, mouse.gy]]
      .map(p => px(p[0]) + "," + px(p[1])).join(" ");
    s += `<polyline points="${path}" fill="none" stroke="var(--acc)" stroke-width="2" stroke-dasharray="5,4" pointer-events="none"/>`;
    s += `<circle cx="${px(last[0])}" cy="${px(last[1])}" r="4" fill="var(--acc)" pointer-events="none"/>`;
  }
  // 预览态：当前画布压暗，AI 电路高亮叠加，编辑锁定
  if (preview) {
    s = `<g opacity="0.28">${s}</g>` + previewSVG();
    $("previewBar").style.display = "flex";
  } else {
    $("previewBar").style.display = "none";
  }
  svg.innerHTML = s;

  // 状态栏
  let st = "";
  if (placing) st += `<span class="k">${esc(tt("status.place", {name: partName(placing)}))}</span><br>`;
  if (wiring) st += `<span class="k">${esc(tt("status.wire"))}</span><br>`;
  for (const e of pre.errors.slice(0, 8)) st += `<span class="e">✗ ${esc(e)}</span><br>`;
  for (const w2 of pre.warnings.slice(0, 4)) st += `<span class="w">⚠ ${esc(w2)}</span><br>`;
  if (!st) st = `<span class="k">${esc(tt("status.ok"))}</span>　<span>${esc(tt("status.okHint"))}</span>`;
  if (!comps.length && !placing && !wiring && !preview)
    st = `<span class="k">${esc(tt("status.empty"))}</span>`;
  if (preview)
    st = `<span class="k">${esc(tt("status.preview"))}</span>`;
  $("status").innerHTML = st + `<span id="dirtyNote" class="w"><br>${esc(tt("status.dirty"))}</span>`;
  updateProjectState();
  if (withProps) renderProps(pre);
  syncResultPanel(pre);
  save();
}

let placingRot = 0;

// 预览层渲染（accent 高亮、不可交互）
function previewSVG() {
  const P = "#4f8cff";
  let s = "";
  for (const w of preview.wires) {
    s += `<polyline points="${w.pts.map(p => px(p[0]) + "," + px(p[1])).join(" ")}" fill="none" stroke="${P}" stroke-width="2.6" pointer-events="none"/>`;
  }
  for (const k of preview.labels) {
    s += `<rect x="${px(k.x) - 8}" y="${px(k.y) - 26}" width="${Math.max(30, (k.text || "").length * 8 + 10)}" height="18" rx="4" fill="#1d2b45" stroke="${P}" pointer-events="none"/>
      <text x="${px(k.x) - 3}" y="${px(k.y) - 13}" font-size="11.5" fill="#e6a23c" font-family="Consolas,monospace" pointer-events="none">${esc(k.text)}</text>`;
  }
  for (const c of preview.comps) {
    s += `<g transform="translate(${px(c.x)},${px(c.y)}) rotate(${(c.rot & 3) * 90}) scale(${U})" pointer-events="none">
      <g stroke="${P}" stroke-width="0.08">${symbolSVG(c.type).replaceAll('stroke="#c8d2e8"', "")}</g></g>`;
    if (c.name && c.type !== "GND")
      s += `<text x="${px(c.x)}" y="${px(c.y) - 0.55 * U}" font-size="11" fill="${P}" text-anchor="middle" font-family="Consolas,monospace" pointer-events="none">${esc(c.name)}</text>`;
    if (c.value && ["R", "C", "L", "VDC", "VSIN", "ISRC"].includes(c.type))
      s += `<text x="${px(c.x)}" y="${px(c.y + 1.45)}" font-size="10.5" fill="#7fa8e0" text-anchor="middle" font-family="Consolas,monospace" pointer-events="none">${esc(String(c.value).slice(0, 14))}</text>`;
    for (const p of compPins(c))
      s += `<circle cx="${px(p[0])}" cy="${px(p[1])}" r="4" fill="${P}" opacity="0.9" pointer-events="none"/>`;
  }
  return s;
}

// ---------------------------------------------------------------------------
// 属性面板
// ---------------------------------------------------------------------------
function renderProps(pre) {
  const body = $("propBody");
  if (!sel) {
    body.innerHTML = tt("props.empty");
    return;
  }
  if (sel.kind === "comp") {
    const c = comps.find(x => x.id === sel.id);
    if (!c) { sel = null; return; }
    const def = DEFS[c.type];
    const showVal = ["R", "C", "L", "VDC", "VSIN", "ISRC", "LBL"].includes(c.type);
    body.innerHTML = `
      <b>${esc(partName(c.type))}</b>
      ${def.pre ? `<label>${esc(tt("prop.name", {pre: def.pre}))}</label>
      <input type="text" id="pName" value="${esc(c.name)}">` : ""}
      ${showVal ? `<label>${esc(tt("prop.value"))}</label><input type="text" id="pVal" value="${esc(String(c.value))}">` : ""}
      <div id="propHint">${esc(partHint(c.type))}</div>
      <div class="rowbtns">
        <button class="btn" id="pRot">${esc(tt("prop.rotate"))}</button>
        <button class="btn" id="pDel" style="color:var(--err)">${esc(tt("prop.delete"))}</button>
      </div>`;
    const nm = $("pName"), vl = $("pVal");
    // render(false)：只重画画布与状态栏，不重建属性面板——否则输入焦点丢失
    if (nm) nm.oninput = () => { c.name = nm.value.trim(); render(false); };
    if (vl) vl.oninput = () => { c.value = vl.value; render(false); };
    $("pRot").onclick = () => { c.rot = (c.rot + 1) & 3; render(); };
    $("pDel").onclick = () => { comps = comps.filter(x => x.id !== c.id); sel = null; render(); };
  } else if (sel.kind === "wire") {
    const w = wires.find(x => x.id === sel.id);
    if (!w) { sel = null; return; }
    body.innerHTML = `<b>${esc(tt("prop.wire"))}</b><div class="rowbtns"><button class="btn" id="pDel" style="color:var(--err)">${esc(tt("prop.wireDel"))}</button></div>`;
    $("pDel").onclick = () => { wires = wires.filter(x => x.id !== w.id); sel = null; render(); };
  } else if (sel.kind === "label") {
    const l = labels.find(x => x.id === sel.id);
    if (!l) { sel = null; return; }
    body.innerHTML = `<b>${esc(tt("prop.label"))}</b><label>${esc(tt("prop.net"))}</label>
      <input type="text" id="pLbl" value="${esc(l.text)}">
      <div id="propHint">${esc(tt("prop.netHint"))}</div>
      <div class="rowbtns"><button class="btn" id="pDel" style="color:var(--err)">${esc(tt("prop.delete"))}</button></div>`;
    $("pLbl").oninput = () => { l.text = $("pLbl").value.trim(); render(false); };
    $("pDel").onclick = () => { labels = labels.filter(x => x.id !== l.id); sel = null; render(); };
  }
}

// ---------------------------------------------------------------------------
// 交互
// ---------------------------------------------------------------------------
function evtGrid(e) {
  const r = svg.getBoundingClientRect();
  return VPProof.gridFromPointer(e.clientX, e.clientY, r, { viewW: 1000, viewH: 700, unit: U, maxX: W, maxY: H });
}
function autoName(pre) {
  let n = 1;
  while (comps.some(c => (c.name || "").toLowerCase() === (pre + n).toLowerCase())) n++;
  return pre + n;
}
function placeComp(type, gx, gy) {
  const def = DEFS[type];
  const c = { id: nextId++, type, x: gx, y: gy, rot: placingRot & 3,
              name: def.pre ? autoName(def.pre) : "", value: def.val };
  if (type === "LBL") c.value = "net" + (labels.length + 1);
  if (type === "LBL") labels.push({ id: c.id, x: gx, y: gy, text: c.value });
  else comps.push(c);
  placing = null; ghost = null;
  sel = { kind: type === "LBL" ? "label" : "comp", id: c.id };
  render();
}

svg.addEventListener("mousedown", e => {
  e.preventDefault();
  if (preview) return;  // 预览态锁定编辑（应用/丢弃见预览条）
  const { gx, gy } = evtGrid(e);
  const t = e.target;
  const pinRef = t.dataset && t.dataset.pin;
  if (wiring) {
    if (pinRef) { finishWire([gx, gy]); return; }
    // 点在已有导线上？
    const onWire = wires.some(w => wirePoints(w).some(p => p[0] === gx && p[1] === gy));
    if (onWire) { finishWire([gx, gy]); return; }
    const last = wiring.pts[wiring.pts.length - 1];
    if (gx === last[0] && gy === last[1]) { wiring = null; render(); return; }
    wiring.pts.push([gx, gy]);  // 加拐点继续
    render(); return;
  }
  if (placing) { placeComp(placing, gx, gy); return; }
  if (pinRef) {  // 从引脚开始连线
    const [cid] = pinRef.split(":");
    const c = comps.find(x => x.id == cid);
    const p = c && compPins(c)[+pinRef.split(":")[1]];
    if (p) { wiring = { pts: [[p[0], p[1]]] }; sel = { kind: "comp", id: c.id }; render(); }
    return;
  }
  if (t.dataset.wire) { sel = { kind: "wire", id: +t.dataset.wire }; render(); return; }
  if (t.dataset.label) { sel = { kind: "label", id: +t.dataset.label }; render(); return; }
  const g = t.closest && t.closest("g[data-comp]");
  if (g) {
    const c = comps.find(x => x.id == g.dataset.comp);
    if (c) { sel = { kind: "comp", id: c.id }; drag = { id: c.id, ox: gx - c.x, oy: gy - c.y }; render(); return; }
  }
  // 标签的引脚区域 / 空白
  const lbl = labels.find(l => l.x === gx && l.y === gy);
  if (lbl) { sel = { kind: "label", id: lbl.id }; render(); return; }
  sel = null; render();
});
svg.addEventListener("mousemove", e => {
  const { gx, gy } = evtGrid(e);
  mouse = { gx, gy };
  if (placing) { ghost = { gx, gy }; render(); return; }
  if (drag) {
    const c = comps.find(x => x.id === drag.id);
    if (c) { c.x = inb(gx - drag.ox, 0, W); c.y = inb(gy - drag.oy, 0, H); render(); }
  }
  if (wiring) render();
});
window.addEventListener("mouseup", () => { drag = null; });
svg.addEventListener("mouseleave", () => { ghost = null; if (!wiring) render(); });
svg.addEventListener("contextmenu", e => { e.preventDefault(); wiring = null; placing = null; ghost = null; render(); });

function finishWire(end) {
  wiring.pts.push(end);
  if (wiring.pts.length >= 2) wires.push({ id: nextId++, pts: wiring.pts });
  wiring = null;
  render();
}

window.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (e.key === "Escape") {
    if (preview) { discardPreview(); return; }
    wiring = null; placing = null; ghost = null; sel = null; render();
  }
  if (preview) return;  // 预览态只允许 Esc（丢弃）
  if (e.key === "r" || e.key === "R") {
    if (placing) { placingRot = (placingRot + 1) & 3; render(); return; }
    if (sel && sel.kind === "comp") { const c = comps.find(x => x.id === sel.id); if (c) { c.rot = (c.rot + 1) & 3; render(); } }
  }
  if (e.key === "Delete" || e.key === "Backspace") {
    if (!sel) return;
    if (sel.kind === "comp") comps = comps.filter(x => x.id !== sel.id);
    if (sel.kind === "wire") wires = wires.filter(x => x.id !== sel.id);
    if (sel.kind === "label") labels = labels.filter(x => x.id !== sel.id);
    sel = null; render();
  }
});

// ---------------------------------------------------------------------------
// 工具栏
// ---------------------------------------------------------------------------
function buildPalette() {
  const order = ["R", "C", "L", "D", "QN", "MN", "OP", "VDC", "VSIN", "ISRC", "GND", "LBL"];
  $("palette").innerHTML = order.map(type => `
    <div class="pal" data-type="${type}" title="${esc(partHint(type))}">
      <svg width="44" height="44" viewBox="-2.8 -2.8 5.6 5.6"><g stroke-width="0.1">${symbolSVG(type)}</g></svg>
      <span>${esc(partName(type))}</span>
    </div>`).join("") +
    `<div class="hints" style="margin-top:10px;font-size:11px;line-height:1.6">${tt("pal.hint")}</div>`;
  for (const el of document.querySelectorAll(".pal")) {
    el.onclick = () => {
      placing = el.dataset.type; placingRot = 0; wiring = null; sel = null;
      for (const p of document.querySelectorAll(".pal")) p.classList.toggle("on", p === el);
      render();
    };
  }
}
$("analysis").onchange = () => {
  $("tstop").style.display = $("analysis").value === "tran" ? "" : "none";
  $("acBox").style.display = $("analysis").value === "ac" ? "flex" : "none";
  save();
};
["title", "tstop", "fstart", "fstop"].forEach(id => { $(id).oninput = save; });

$("clearBtn").onclick = () => {
  if (!confirm(tt("clear.confirm"))) return;
  comps = []; wires = []; labels = []; sel = null; wiring = null;
  render();  // 项目时间线（任务历史）不清——它是项目档案
  updateAiPlaceholder();
};
$("cirBtn").onclick = () => {
  const blob = new Blob([buildNetlist()], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (($("title").value || "circuit").replace(/[\\/:*?"<>|]/g, "_")) + ".cir";
  a.click(); URL.revokeObjectURL(a.href);
};
$("demoBtn").onclick = () => {
  if (comps.length && !confirm(tt("demo.confirm"))) return;
  loadDemo();
  render();
  updateAiPlaceholder();
};

function loadDemo() {
  nextId = 1;
  comps = [
    { id: nextId++, type: "VSIN", x: 6, y: 12, rot: 0, name: "V1", value: "SIN(0 1 1k)" },
    { id: nextId++, type: "R", x: 13, y: 12, rot: 0, name: "R1", value: "1.59k" },
    { id: nextId++, type: "C", x: 20, y: 14, rot: 1, name: "C1", value: "100n" },
    { id: nextId++, type: "GND", x: 8, y: 17, rot: 0, name: "", value: "" },
    { id: nextId++, type: "GND", x: 20, y: 17, rot: 0, name: "", value: "" },
  ];
  wires = [
    { id: nextId++, pts: [[4, 12], [4, 10], [11, 10], [11, 12]] },   // V+ → R（上沿绕行避开 V−）
    { id: nextId++, pts: [[15, 12], [20, 12]] },                     // R → C 顶端（out）
    { id: nextId++, pts: [[8, 12], [8, 17]] },                       // V− → GND
    { id: nextId++, pts: [[20, 16], [20, 17]] },                     // C 底端 → GND
  ];
  labels = [
    { id: nextId++, x: 4, y: 12, text: "in" },
    { id: nextId++, x: 17, y: 12, text: "out" },
  ];
  $("title").value = tt("demo.title");
  $("analysis").value = "tran"; $("tstop").value = "5m";
  $("analysis").onchange();
}

// ---------------------------------------------------------------------------
// 仿真 + 结果
// ---------------------------------------------------------------------------
let verifiedHTML = "", verifiedStamp = "", verifiedOK = false, resultMode = "idle", blockedKey = "", resultArmed = false;

function circuitStamp() {
  return JSON.stringify({comps, wires, labels,
    analysis:$("analysis").value, tstop:$("tstop").value, fstart:$("fstart").value, fstop:$("fstop").value});
}
function blockedHtml(errors) {
  return `<div class="evidence"><div class="verdict fail">${esc(tt("result.blocked"))}</div>
      <div class="errbox">${errors.map(esc).join("<br>")}</div></div>`;
}
function staleHtml() {
  return `<div class="evidence"><div class="verdict">${esc(tt("result.stale"))}</div>
      <div class="hints">${esc(tt("result.staleHint"))}</div></div>`;
}
function syncResultPanel(pre) {
  pre = pre || precheck();
  const panel = $("resultPanel");
  const panelActive = panel.style.display !== "none" && !!panel.innerHTML;
  const mode = VPProof.resultClass({
    errors: pre.errors, stamp: circuitStamp(), verifiedStamp,
    hasVerified: !!verifiedHTML, panelActive, armed: resultArmed,
  });
  const errKey = pre.errors.join("\n");
  if (mode === "fresh" && resultMode === "fresh") return;
  if (mode === "blocked" && resultMode === "blocked" && blockedKey === errKey) return;
  if (mode === "stale" && resultMode === "stale") return;
  if (mode === "idle") return;
  resultMode = mode;
  panel.style.display = "block";
  if (mode === "blocked") {
    blockedKey = errKey;
    panel.innerHTML = blockedHtml(pre.errors);
    $("aiBtn").disabled = true;
    return;
  }
  if (mode === "fresh") {
    blockedKey = "";
    panel.innerHTML = verifiedHTML;
    const ai2 = $("aiBtn2");
    if (ai2) ai2.onclick = runInterpret;
    $("aiBtn").disabled = !verifiedOK;
    return;
  }
  blockedKey = "";
  panel.innerHTML = staleHtml();
  $("aiBtn").disabled = true;
}

$("simBtn").onclick = async () => {
  const pre = precheck();
  if (pre.errors.length) {
    resultArmed = true;
    resultMode = "idle";
    syncResultPanel(pre);
    return;
  }
  const nl = buildNetlist();
  simBusy = true;
  $("simBtn").disabled = true; $("simBtn").textContent = tt("sim.running");
  try {
    await flushProject();
    const stampAtRequest = circuitStamp();
    const payloadAtRequest = JSON.stringify(projectPayload());
    const r = await fetch("/api/draw/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("title").value || tt("untitled"), netlist: nl,
                             use_current_version:!projectDirty && !!projectVersion,
                             expected_revision:projectRevision, canvas:{comps,wires,labels} }),
    });
    const d = await r.json();
    if (VPProof.clearsDirty({applied: !!d.applied, sameCircuit: circuitStamp() === stampAtRequest})) {
      projectRevision=d.project_revision; projectVersion=d.version_id; projectDirty=false;
      lastSavedPayload=payloadAtRequest; projectMessage="";
    }
    updateProjectState();
    renderResult(d, stampAtRequest);
    addRound("sim", $("title").value || tt("untitled"), !!d.ok);
    if (window.refreshWorkspaceInfo) window.refreshWorkspaceInfo();
  } catch (err) {
    verifiedHTML = "";
    resultMode = "idle";
    $("resultPanel").style.display = "block";
    $("resultPanel").innerHTML = `<div class="evidence"><div class="verdict fail">${esc(tt("result.requestFail", {msg: String(err)}))}</div></div>`;
  } finally {
    simBusy = false;
    $("simBtn").disabled = false; $("simBtn").textContent = tt("sim.run");
  }
};

function renderResult(d, stamp, quiet) {
  const panel = $("resultPanel");
  if (d.error && !d.stage) {
    verifiedHTML = ""; verifiedStamp = ""; resultMode = "idle";
    panel.style.display = "block";
    panel.innerHTML = `<div class="evidence"><div class="verdict fail">✗ ${esc(trs(d.error))}</div></div>`;
    return;
  }
  const ok = !!d.ok;
  lastResultTask=d.task_id || "";
  $("aiBtn").disabled = !ok;
  const chips = Object.entries(d.metrics || {}).slice(0, 14)
    .map(([k, v]) => `<span class="metric">${esc(k)} = <b>${typeof v === "number" ? (+v.toPrecision(4)) : esc(v)}</b></span>`).join("");
  const anKey = {op:"analysis.op", tran:"analysis.tran", ac:"analysis.ac"}[d.analysis];
  verifiedHTML = `<div class="evidence">
    <div class="verdict ${d.verdict === "passed" ? "ok" : ""}">${VPProof.label(d)}　<span style="color:var(--dim);font-weight:400;font-size:12px">${d.analysis ? esc(tt("result.analysis", {name: anKey ? tt(anKey) : d.analysis})) + " · " : ""}${esc(tt("result.elapsed", {s: d.elapsed ?? "–"}))}</span></div>
    ${VPProof.checks(d)}${VPProof.details({...d, candidate_id:undefined})}
    ${d.errors && d.errors.length ? `<div class="errbox">${d.errors.map(x => esc(trs(x))).join("<br>")}${d.hint ? `<br><span class="h">↳ ${esc(trs(d.hint))}</span>` : ""}</div>` : ""}
    ${chips ? `<div class="metrics">${chips}</div>` : ""}
    ${d.waveform_b64 ? `<img class="wave" src="data:image/png;base64,${d.waveform_b64}" alt="${esc(tt("chat.waveAlt"))}">` : ""}
    ${d.schematic_b64 ? `<img class="sch" src="data:image/png;base64,${d.schematic_b64}" alt="${esc(tt("chat.schAlt"))}">` : ""}
    <div id="interpBox"></div>
    ${ok ? `<div class="rowbtns">
        <button class="btn" id="aiBtn2">${esc(tt("ai.explain"))}</button>
        <a class="btn" href="/" target="_blank" style="text-decoration:none" title="${esc(tt("result.chatTip"))}">${esc(tt("result.chat"))}</a>
      </div>` : ""}
    <details><summary>${esc(tt("chat.netlist.repro"))}</summary><pre>${esc(d.netlist || "")}</pre></details>
    ${d.task_id ? `<div class="hints" style="margin-top:6px">${esc(tt("result.task", {id: d.task_id}))}</div>` : ""}
  </div>`;
  lastResult = {data: d, stamp: stamp || circuitStamp()};
  verifiedStamp = stamp || circuitStamp();
  verifiedOK = !!d.ok;
  resultMode = "idle";
  syncResultPanel();
  if (!quiet) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runInterpret() {
  if (interpreting) return;
  interpreting = true;
  const box = $("interpBox");
  box.innerHTML = `<div class="hints">${esc(tt("ai.explain.running"))}</div>`;
  try {
    const r = await fetch("/api/draw/interpret", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({task_id:lastResultTask || undefined}),
    });
    const d = await r.json();
    box.innerHTML = d.error
      ? `<div class="errbox">✗ ${esc(d.error)}</div>`
      : `<details open><summary style="color:var(--acc)">${esc(tt("ai.explain"))}</summary><div class="interp">${esc(d.text)}</div></details>`;
  } catch (err) {
    box.innerHTML = `<div class="errbox">✗ ${esc(String(err))}</div>`;
  } finally { interpreting = false; }
}
$("aiBtn").onclick = runInterpret;

// ---------------------------------------------------------------------------
// AI 画图：自然语言 → /api/draw/ai/stream（SSE）→ 预览 → 确认应用
// ---------------------------------------------------------------------------
function stageLabel(stage) {
  const key = "stage." + stage;
  const label = tt(key);
  return label === key ? stage : label;
}
let aiBusy = false;
let preview = null;    // 待应用的 AI 画布（预览态锁定编辑）
let undoStack = [];    // AI 应用前的画布快照（深 20）
let rounds = [];       // 会话轮次（{kind:'sim'|'ai', text, ok}）

function snapshotCanvas() {
  return JSON.parse(JSON.stringify({ comps, wires, labels, nextId, version:projectVersion, dirty:projectDirty,
    requirements:projectRequirements,
    analysis:{type:$("analysis").value,tstop:$("tstop").value,fstart:$("fstart").value,fstop:$("fstop").value} }));
}

function restoreSnapshot(s) {
  comps = s.comps; wires = s.wires; labels = s.labels;
  nextId = s.nextId;
  if (s.analysis) { $("analysis").value=s.analysis.type; $("tstop").value=s.analysis.tstop; $("fstart").value=s.analysis.fstart; $("fstop").value=s.analysis.fstop; $("analysis").onchange(); }
  sel = null; wiring = null; placing = null;
  render();
}

function updateAiPlaceholder() {
  $("aiInput").placeholder = comps.length ? tt("ai.placeholder.edit") : tt("ai.placeholder.empty");
}

function addAiStep(name, problems, state, stage) {
  const span = $("aiStageLine").querySelector(`[data-stage="${stage}"]`) || document.createElement("span");
  span.dataset.stage=stage;
  span.className = problems.length ? "st bad" : "st";
  span.textContent = (problems.length ? "✗" : state === "running" ? "◌" : "✓") + name;
  $("aiStageLine").appendChild(span);
  $("aiStageLine").scrollTop = 1e5;
}

async function sendAi() {
  const msg = $("aiInput").value.trim();
  if (!msg || aiBusy || preview) return;
  aiBusy = true;
  $("aiSend").disabled = true; $("simBtn").disabled = true;
  $("aiStageLine").innerHTML = "";
  try {
    await flushProject();
    activeAiRequest=crypto.randomUUID();
    cancelAiBtn.hidden=false;
    const r = await fetch("/api/draw/ai/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg,
                             request_id:activeAiRequest, expected_revision:projectRevision,
                             canvas_netlist: comps.length ? buildNetlist() : null }),
    });
    if (!r.ok || !r.body) { const d=await r.json(); throw new Error(trs(d.error) || tt("ai.requestFail")); }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const line = frame.split("\n").find(l => l.startsWith("data: "));
        if (!line) continue;
        handleAiEvent(JSON.parse(line.slice(6)), msg);
      }
    }
  } catch (e) {
    $("aiStageLine").innerHTML = `<span class="st bad">✗ ${esc(String(e))}</span>`;
  } finally {
    activeAiRequest=null;
    cancelAiBtn.hidden=true;
    aiBusy = false;
    $("aiSend").disabled = false; $("simBtn").disabled = false;
  }
}

function handleAiEvent(ev, msg) {
  if (ev.type === "stage") {
    addAiStep(stageLabel(ev.stage), ev.problems || [], ev.state, ev.stage);
  } else if (ev.type === "error") {
    $("aiStageLine").innerHTML += `<span class="st bad">✗ ${esc(trs(ev.error))}</span>`;
  } else if (ev.type === "done") {
    renderResult(ev);
    addRound("ai", msg, !!ev.ok);
    if (ev.ok && ev.canvas) {
      previewMeta=ev;
      enterPreview(ev.canvas);
    } else if (ev.ok) {
      $("aiStageLine").innerHTML +=
        `<span class="st bad">${esc(tt("ai.layoutFail"))}</span>`;
    }
    $("aiInput").value = "";
    updateAiPlaceholder();
  }
}

// ---- 预览层 ----
function enterPreview(cv) {
  let id = 1;
  for (const c of cv.comps) c.id = id++;
  for (const w of cv.wires) w.id = id++;
  for (const l of cv.labels) l.id = id++;
  preview = cv;
  sel = null; wiring = null; placing = null;
  render();
}

async function applyPreview() {
  if (!preview) return;
  $("applyBtn").disabled=true;
  try {
    await flushProject();
    const old=snapshotCanvas();
    const result={...previewMeta, applied:true};
    await VPProof.applyVersion(previewMeta.candidate_id, previewMeta.base_revision);
    undoStack.push(old); if (undoStack.length>20) undoStack.shift();
    preview=null; previewMeta=null; render(); renderResult(result); updateAiPlaceholder();
  } catch(e) { projectMessage=e.message; updateProjectState(); }
  finally { $("applyBtn").disabled=false; }
}

function discardPreview() {
  preview = null;
  previewMeta = null;
  clearResult();
  render();
}

function clearResult() {
  verifiedHTML=""; verifiedStamp=""; verifiedOK=false; resultMode="idle"; blockedKey=""; resultArmed=false;
  $("resultPanel").innerHTML="";
  $("resultPanel").style.display="none";
  lastResultTask="";
  $("aiBtn").disabled=true;
}

async function undoAi() {
  if (preview) { discardPreview(); return; }
  if (!undoStack.length) return;
  try {
    await flushProject(); const s=undoStack[undoStack.length-1];
    const r=await fetch("/api/project/restore", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({version_id:s.version || "",expected_revision:projectRevision,requirements:s.requirements})});
    const d=await r.json(); if(!r.ok) throw new Error(trs(d.error) || tt("ai.undoFail"));
    await loadProject();
    if(s.dirty || !s.version) { restoreSnapshot(s); await flushProject(); }
    undoStack.pop();
    clearResult();
    $("aiStageLine").textContent=tt("ai.undone");
  } catch(e) { projectMessage=e.message; updateProjectState(); }
  updateAiPlaceholder();
}

// ---- 会话轮次 ----
function addRound(kind, text, ok) {
  rounds.push({ kind, text, ok });
  if (rounds.length > 40) rounds.shift();
  renderRounds();
}

function roundLabel(text) {
  const raw = String(text || "").trim();
  if (!raw) return tt("rounds.untitled");
  return trs(raw);
}
function renderRounds() {
  const icon = { ai: "🤖", sim: "⚙", chat: "💬" };
  $("roundsList").innerHTML = rounds.length ? rounds.map(r => {
    const label = roundLabel(r.text);
    return `<div class="round ${r.ok ? "" : "fail"}" title="${esc(label)}">${icon[r.kind] || "•"} ${esc(label.slice(0, 40))} <span class="dim">${r.ok ? "✓" : "✗"}</span></div>`;
  }).join("") : `<span class="dim">${esc(tt("rounds.empty"))}</span>`;
}

async function restoreRounds() {
  // 项目时间线：全部任务档案（新在前），按 source 区分图标；本地 rounds 排在更后
  try {
    const d = await (await fetch("/api/ws/tasks")).json();
    const fromTasks = (d.tasks || []).slice(0, 30).map(task => {
      const src = task.source || "chat";
      const kind = src === "draw-sim" || src === "whatif" || src === "whatif-point" ? "sim" : (src === "draw-ai" ? "ai" : "chat");
      const text = (task.request || "").replace(/^画板仿真：/, "").replace(/^Canvas simulation: /, "");
      return { kind, text, ok: !!task.ok };
    });
    rounds = fromTasks;
  } catch (e) { /* 服务端不可达：留空 */ }
  renderRounds();
}

$("aiSend").onclick = sendAi;
const cancelAiBtn=document.createElement("button"); cancelAiBtn.className="btn"; cancelAiBtn.textContent=tt("ai.cancel");
cancelAiBtn.hidden=true;
cancelAiBtn.onclick=async()=>{ if(activeAiRequest) { await fetch("/api/runs/"+activeAiRequest+"/cancel",{method:"POST"}); $("aiStageLine").textContent=tt("ai.cancelNote"); } };
$("aiSend").after(cancelAiBtn);
$("aiInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAi(); }
});
$("applyBtn").onclick = applyPreview;
$("discardBtn").onclick = discardPreview;
$("undoBtn").onclick = undoAi;

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
buildPalette();
$("analysis").onchange();
render();          // 先出空画布（服务端电路回来后 loadProject 再渲染一次）
updateAiPlaceholder();
restoreRounds();
loadProject();

// ---------------------------------------------------------------------------
// 宿主集成（统一主页使用；独立 /draw.html 不调用）
// ---------------------------------------------------------------------------
document.addEventListener("vp-lang", () => {
  if (isDefaultTitle($("title").value)) {
    $("title").value = tt("untitled");
    // 只改默认标题的语言，不算电路被编辑，也不另存一版。
    if (projectLoaded) lastSavedPayload = JSON.stringify(projectPayload());
  }
  requirementsBtn.textContent = tt("btn.accept");
  compareBtn.textContent = tt("btn.compare");
  projectReport.textContent = tt("btn.report");
  projectReport.href = "/api/project/report?lang=" + encodeURIComponent(VPI18n.apiLang());
  cancelAiBtn.textContent = tt("ai.cancel");
  if (!simBusy) $("simBtn").textContent = tt("sim.run");
  else $("simBtn").textContent = tt("sim.running");
  buildPalette();
  updateAiPlaceholder();
  updateProjectState();
  render();
  renderRounds();
  if (lastResult) renderResult(lastResult.data, lastResult.stamp, true);
});

window.VPBoard = {
  reloadProject: loadProject,   // 重拉项目画布并渲染——chat 轮画布同步成功（canvas_synced）后调用
  reloadRounds: restoreRounds,  // 刷新项目时间线（新任务落档后调用）
  buildNetlist,                 // 当前画布 → 网表（宿主复用）
  hasComps: () => comps.length > 0,
  flushProject,
  revision: () => projectRevision,
};
})();
