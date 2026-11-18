/* 所有入口共用的验证结论、验收表和条件编辑器。 */
window.VPProof = (() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const t = (k, v) => {
    if (typeof VPI18n !== "undefined") return VPI18n.t(k, v);
    const zh = {
      "req.bad": "无法保存未识别的验收条件",
      "req.needMetric": "请为每条条件选择指标",
      "req.needRef": "元件参数需要位号，如 R1",
      "req.name": "{ref} 参数",
    }[k] || k;
    return String(zh).replace(/\{(\w+)\}/g, (_, name) => v && v[name] != null ? String(v[name]) : "");
  };
  const tr = s => (typeof VPI18n !== "undefined" ? VPI18n.tr(s) : String(s ?? ""));
  const METRIC_KEYS = {fc:"metric.fc.short", freq:"metric.freq", gain:"metric.gain", ripple:"metric.ripple", mean:"metric.mean.full", vpp:"metric.vpp", rms:"metric.rms", thd:"metric.thd", component_value:"metric.component_value"};
  const metrics = METRIC_KEYS;
  function resultClass(input) {
    const errors = (input && input.errors) || [];
    const watching = !!(input && (input.panelActive || input.armed || input.hasVerified));
    if (!watching) return "idle";
    if (errors.length) return "blocked";
    if (input.hasVerified && input.stamp === input.verifiedStamp) return "fresh";
    return "stale";
  }
  function clearsDirty(input) {
    return !!(input && input.applied && input.sameCircuit);
  }
  function gridFromPointer(clientX, clientY, rect, opts) {
    const viewW = (opts && opts.viewW) || 1000;
    const viewH = (opts && opts.viewH) || 700;
    const unit = (opts && opts.unit) || 20;
    const maxX = opts && opts.maxX != null ? opts.maxX : viewW / unit;
    const maxY = opts && opts.maxY != null ? opts.maxY : viewH / unit;
    const width = rect && rect.width;
    const height = rect && rect.height;
    const scale = Math.min(width / viewW, height / viewH);
    const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
    if (!(scale > 0) || !isFinite(scale)) return { gx: 0, gy: 0 };
    const originX = rect.left + (width - viewW * scale) / 2;
    const originY = rect.top + (height - viewH * scale) / 2;
    const x = (clientX - originX) / scale;
    const y = (clientY - originY) / scale;
    return { gx: clamp(Math.round(x / unit), 0, maxX), gy: clamp(Math.round(y / unit), 0, maxY) };
  }
  function projectLoadPanel(input) {
    if (input && !input.dirty && input.evidenceId && input.hasCanvas) return "restore";
    return "keep";
  }
  function requirementFromEditor(input) {
    if (input && input.preserve) {
      const spec = input.preserve;
      if (!spec || typeof spec !== "object" || !spec.metric) throw new Error(t("req.bad"));
      return spec;
    }
    const metric = input && input.metric;
    if (!metric || !Object.prototype.hasOwnProperty.call(metrics, metric)) throw new Error(t("req.needMetric"));
    const item = {
      metric, op: input.op || "near", target: Number(input.target), tol: Number(input.tol),
      out: String(input.out || "out").trim(), in: String(input.in || "in").trim(),
      explicit_nodes: metric !== "component_value", filter_type: input.filter_type || "lowpass",
    };
    if (metric === "component_value") {
      const component = String(input.component || "").trim();
      if (!/^[RCLVI]\d+$/i.test(component)) throw new Error(t("req.needRef"));
      item.component = component;
      item.unit = input.unit || "";
      item.name = input.name || t("req.name", {ref: component});
    }
    return item;
  }
  const num = x => x == null ? t("check.unmeasured") : Number(x).toLocaleString(VPI18n.lang() === "en" ? "en" : "zh-CN", {maximumSignificantDigits:6});
  function metricLabel(key) { return METRIC_KEYS[key] ? t(METRIC_KEYS[key]) : key; }
  function showUnit(c) { return c && c.metric === "gain" ? t("unit.gain") : ((c && c.unit) || ""); }
  function showTarget(c) {
    let label = String((c && (c.target_label || c.target)) || "");
    if (c && c.metric === "gain") label = VPI18n.lang() === "en" ? label.replace(/倍/g, "×") : label.replace(/×/g, "倍");
    return label;
  }
  function checkName(c) {
    if (c.metric === "component_value" && c.component) return t("req.name", {ref: c.component});
    if (c.metric && METRIC_KEYS[c.metric]) return metricLabel(c.metric);
    return tr(c.name || "");
  }
  function checkWindow(c) {
    if (c.metric === "component_value" || c.window_key === "component") return t("window.component");
    if (c.window_key === "tran" || /暂态|discarded/i.test(c.window || "")) {
      const pct = c.skip != null ? Math.round(Number(c.skip) * 100) : (((c.window || "").match(/(\d+)\s*%/) || [])[1] || 20);
      return t("window.tran", {pct});
    }
    if (!c.window || c.window_key === "full") return t("window.full");
    return tr(c.window);
  }
  function checkDetail(c) {
    if (c.measured != null && Number.isFinite(Number(c.measured))) {
      const kind = c.metric === "component_value" ? t("detail.kind.netlist") : t("detail.kind.measured");
      return t("detail.line", {kind, value: num(c.measured), unit: showUnit(c), target: showTarget(c)});
    }
    return tr(c.detail || "");
  }
  function label(d) {
    const key = "verdict." + (d && d.verdict);
    const text = t(key);
    if (text !== key) return text;
    return d && d.ok ? t("verdict.legacyOk") : t("verdict.legacyBad");
  }
  function checks(d) {
    const rows = (d.checks || []).map(c => `<tr><td>${esc(checkName(c))}</td><td>${esc(showTarget(c) || "—")}</td><td>${c.measured == null ? esc(t("check.unmeasured")) : esc(num(c.measured) + " " + showUnit(c))}</td><td>${c.ok === true ? esc(t("check.pass")) : c.ok === false ? esc(t("check.fail")) : esc(t("check.unknown"))}</td></tr><tr><td colspan="4" class="hints">${esc(checkDetail(c))} ${esc(checkWindow(c))}</td></tr>`).join("");
    return rows ? `<table class="proof-table"><thead><tr><th>${esc(t("check.col.metric"))}</th><th>${esc(t("check.col.target"))}</th><th>${esc(t("check.col.measured"))}</th><th>${esc(t("check.col.judge"))}</th></tr></thead><tbody>${rows}</tbody></table>` : "";
  }
  function details(d) {
    return `${(d.warnings || []).map(w => `<div class="proof-warning">${esc(w)}</div>`).join("")}
      ${d.from_cache ? `<div class="hints">${esc(t("proof.cached", {when: d.provenance?.created || ""}))}</div>` : ""}
      ${d.version_id ? `<div class="hints">${esc(t("proof.version", {id: d.version_id.slice(0, 8)}))}${d.applied === false ? esc(t("proof.candidate")) : ""}</div>` : ""}
      ${d.changes ? `<details><summary>${esc(t("proof.changes"))}</summary><pre>${esc(d.changes)}</pre></details>` : ""}
      ${d.task_id && d.validation?.measurement === "passed" ? `<button class="btn" data-wave-preview="${esc(d.task_id)}">${esc(t("proof.wave"))}</button>` : ""}
      ${d.candidate_id && !d.applied ? `<button class="btn" data-apply-version="${esc(d.candidate_id)}" data-base-revision="${d.base_revision}">${esc(t("proof.apply"))}</button>` : ""}`;
  }
  function comparison(d) {
    if(!d?.left_id) return '';
    return `<div class="hints">${esc(tr(d.note))}</div><table class="proof-table"><tr><th>${esc(t("check.col.metric"))}</th><th>${esc(d.left_id.slice(0,8))}</th><th>${esc(d.right_id.slice(0,8))}</th><th>${esc(t("cmp.col.change"))}</th></tr>${(d.measurements || []).map(m=>`<tr><td>${esc(tr(m.name))} ${esc(m.metric === "gain" ? t("unit.gain") : (m.unit || ""))}</td><td>${num(m.left)}</td><td>${num(m.right)}</td><td>${num(m.delta)}</td></tr>`).join('')}</table>`;
  }
  async function compareVersions() {
    await window.VPBoard?.flushProject();
    const d=await(await fetch('/api/project/versions')).json();
    const versions=d.versions || [];
    const dlg=document.createElement('dialog');dlg.className='proof-dialog';
    dlg.innerHTML=`<h3 data-i18n="cmp.title">${esc(t("cmp.title"))}</h3><p class="hints" data-i18n="cmp.help">${esc(t("cmp.help"))}</p><select data-left>${versions.map(v=>`<option value="${v.id}">${esc(v.id.slice(0,8)+' '+v.source+' '+v.created)}</option>`).join('')}</select><select data-right>${versions.map(v=>`<option value="${v.id}">${esc(v.id.slice(0,8)+' '+v.source+' '+v.created)}</option>`).join('')}</select><button class="btn" data-compare data-i18n="cmp.run">${esc(t("cmp.run"))}</button><button class="btn" data-close data-i18n="cmp.close">${esc(t("cmp.close"))}</button><div data-result></div>`;
    if(versions.length>1)dlg.querySelector('[data-left]').selectedIndex=1;
    dlg.querySelector('[data-close]').onclick=()=>dlg.close();dlg.addEventListener('close',()=>dlg.remove());
    dlg.querySelector('[data-compare]').onclick=async()=>{
      const target=dlg.querySelector('[data-result]');
      try {const params=new URLSearchParams({left_id:dlg.querySelector('[data-left]').value,right_id:dlg.querySelector('[data-right]').value});
        const r=await fetch('/api/project/compare?'+params);const c=await r.json();if(!r.ok)throw new Error(c.error);
        target.innerHTML=comparison(c)+`<details open><summary>${esc(t("cmp.netlist"))}</summary><pre>${esc(c.changes)}</pre></details>`;
      }catch(e){target.textContent=e.message;}
    };
    document.body.append(dlg);dlg.showModal();
  }
  async function applyVersion(id, base) {
    await window.VPBoard?.flushProject();
    const r = await fetch("/api/project/apply", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({version_id:id, expected_revision:Number(base)})});
    const d = await r.json();
    if (!r.ok) throw new Error(tr(d.error) || t("proof.applyFail"));
    await window.VPBoard?.reloadProject();
    await window.VPBoard?.reloadRounds();
    if (window.refreshWorkspaceInfo) window.refreshWorkspaceInfo();
  }
  document.addEventListener("click", async e => {
    const wave=e.target.closest("[data-wave-preview]");
    if(wave) {
      wave.disabled=true;
      try {
        const r=await fetch(`/api/files/${encodeURIComponent(wave.dataset.wavePreview)}/wave.json`);
        if(!r.ok) throw new Error(t("proof.noWave"));
        showWave(wave,await r.json());
      } catch(err) { const p=document.createElement("div");p.className="proof-warning";p.textContent=err.message;wave.after(p); }
      return;
    }
    const btn = e.target.closest("[data-apply-version]");
    if (!btn) return;
    btn.disabled = true;
    try { await applyVersion(btn.dataset.applyVersion, btn.dataset.baseRevision); btn.textContent = t("proof.applied"); }
    catch (err) { btn.disabled = false; const p = document.createElement("div"); p.className="proof-warning"; p.textContent=err.message; btn.after(p); }
  });
  function showWave(button,data) {
    const box=document.createElement("section");box.className="proof-wave";
    const axis = data.analysis==='ac' ? t('wave.ac') : data.analysis==='tran' ? t('wave.tran') : t('wave.sample');
    box.innerHTML=`<div class="hints" data-wave-axis>${esc(tr(data.note))} · ${esc(t('wave.axis', {axis}))}</div><div data-signals></div><div data-plot><canvas></canvas></div><div data-cursor class="wave-hint" data-i18n="wave.hint">${esc(t('wave.hint'))}</div><label><span data-i18n="wave.start">${esc(t('wave.start'))}</span> <input data-start type="range" min="0" max="98" value="0"></label><label><span data-i18n="wave.end">${esc(t('wave.end'))}</span> <input data-end type="range" min="1" max="100" value="100"></label>${Object.keys(data.phase || {}).length ? `<label><span data-i18n="wave.y">${esc(t('wave.y'))}</span> <select data-axis><option value="signals">${esc(t('wave.mag'))}</option><option value="phase">${esc(t('wave.phase'))}</option></select></label>`:''}`;
    box._wave = data;
    button.after(box);button.hidden=true;
    const names=Object.keys(data.signals),palette=['#4f8cff','#e6a23c','#3fb96f','#e05555','#c391ef','#70d6d8'];
    box.querySelector('[data-signals]').innerHTML=names.map((n,i)=>`<label style="color:${palette[i%palette.length]};margin-right:8px"><input type="checkbox" data-signal="${i}" checked> ${esc(n)}</label>`).join('');
    const canvas=box.querySelector('canvas'),ctx=canvas.getContext('2d'),start=box.querySelector('[data-start]'),end=box.querySelector('[data-end]');
    let view={};
    function draw() {
      let lo=Number(start.value)/100,hi=Number(end.value)/100;
      if(hi<=lo) { hi=Math.min(1,lo+.01);end.value=String(hi*100); }
      const first=Math.floor(lo*(data.x.length-1)),last=Math.max(first+1,Math.ceil(hi*(data.x.length-1)));
      const active=[...box.querySelectorAll('[data-signal]:checked')].map(e=>Number(e.dataset.signal));
      const source=data[box.querySelector('[data-axis]')?.value || 'signals'];
      const plot = box.querySelector("[data-plot]");
      const width=Math.max(280,(plot || box).clientWidth),height=220;
      canvas.width=width*devicePixelRatio;canvas.height=height*devicePixelRatio;ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
      ctx.clearRect(0,0,width,height);
      let min=Infinity,max=-Infinity;
      for(const i of active) for(const y of (source[names[i]] || []).slice(first,last+1)) { min=Math.min(min,y);max=Math.max(max,y); }
      if(!Number.isFinite(min)) {min=0;max=1;} if(max===min) {max+=.5;min-=.5;}
      const transform=x=>data.analysis==='ac' && x>0 ? Math.log10(x) : x;
      const xmin=transform(data.x[first]),xmax=transform(data.x[Math.min(last,data.x.length-1)]);
      const px=x=>45+(transform(x)-xmin)/(xmax-xmin || 1)*(width-60);
      const py=y=>15+(max-y)/(max-min)*(height-40);
      ctx.strokeStyle='#394251';ctx.fillStyle='#9aa1b2';ctx.font='10px sans-serif';
      for(let j=0;j<=4;j++) {const y=15+j*(height-40)/4;ctx.beginPath();ctx.moveTo(45,y);ctx.lineTo(width-15,y);ctx.stroke();ctx.fillText((max-j*(max-min)/4).toPrecision(3),0,y+4);}
      ctx.fillText(num(data.x[first]),45,height-5);ctx.fillText(num(data.x[Math.min(last,data.x.length-1)]),width-80,height-5);
      for(const i of active) {ctx.strokeStyle=palette[i%palette.length];ctx.beginPath();const ys=source[names[i]] || [];for(let j=first;j<=last && j<ys.length;j++){if(j===first)ctx.moveTo(px(data.x[j]),py(ys[j]));else ctx.lineTo(px(data.x[j]),py(ys[j]));}ctx.stroke();}
      view={first,last,width,active,source,xmin,xmax};
    }
    box.addEventListener('input',draw);
    canvas.addEventListener('pointermove',e=>{
      const x=(e.clientX-canvas.getBoundingClientRect().left-45)/(view.width-60);
      const wanted=view.xmin+Math.max(0,Math.min(1,x))*(view.xmax-view.xmin);
      const axis=data.analysis==='ac'?10**wanted:wanted;
      let index=view.first;for(let j=view.first;j<=view.last && j<data.x.length;j++) if(Math.abs(data.x[j]-axis)<Math.abs(data.x[index]-axis))index=j;
      box.querySelector('[data-cursor]').textContent=`${data.analysis==='ac'?'f':'t'}=${num(data.x[index])} · `+view.active.map(i=>`${names[i]}=${num(view.source[names[i]]?.[index])}`).join(' · ');
    });
    draw();
  }
  async function editRequirements() {
    await window.VPBoard?.flushProject();
    const p = await (await fetch("/api/project")).json();
    const dlg = document.createElement("dialog"); dlg.className="proof-dialog";
    dlg.innerHTML = `<h3 data-i18n="req.title">${esc(t("req.title"))}</h3><p class="hints" data-i18n="req.help">${esc(t("req.help"))}</p><div class="requirement-rows"></div><button class="btn" data-add data-i18n="req.add">${esc(t("req.add"))}</button><p class="proof-warning" data-message></p><div class="rowbtns"><button class="btn primary" data-save data-i18n="req.save">${esc(t("req.save"))}</button><button class="btn" data-close data-i18n="cancel">${esc(t("cancel"))}</button></div>`;
    const ops = [["near","op.near"],["le","op.le"],["lt","op.lt"],["ge","op.ge"],["gt","op.gt"]];
    function add(s={metric:"fc",target:1000,tol:.05,op:"near",out:"out",in:"in"}) {
      const row = document.createElement("div"); row.className="requirement-row";
      if (s.metric && !Object.prototype.hasOwnProperty.call(metrics, s.metric)) {
        row.dataset.preserve = JSON.stringify(s);
        row.innerHTML = `<span class="hints">${esc(t("req.keep", {name: s.name || s.metric, target: s.target, unit: s.unit || ""}))}</span><button class="btn" data-remove>${esc(t("req.remove"))}</button>`;
      } else {
        row.dataset.name = s.name || "";
        row.innerHTML = `<select data-metric>${Object.keys(metrics).map(k=>`<option value="${k}" ${s.metric===k?"selected":""}>${esc(metricLabel(k))}</option>`).join("")}</select><select data-op>${ops.map(([k,key])=>`<option value="${k}" ${s.op===k?"selected":""}>${esc(t(key))}</option>`).join("")}</select><span data-component-wrap><label><span data-i18n="req.ref">${esc(t("req.ref"))}</span> <input data-component value="${esc(s.component || "")}"></label><label><span data-i18n="req.unit">${esc(t("req.unit"))}</span> <input data-unit value="${esc(s.unit || "")}"></label></span><label><span data-i18n="req.target">${esc(t("req.target"))}</span> <input data-target type="number" step="any" value="${esc(s.target)}"></label><label><span data-i18n="req.tol">${esc(t("req.tol"))}</span> <input data-tol type="number" min="0" max="100" value="${(s.tol ?? .05)*100}"></label><label><span data-i18n="req.out">${esc(t("req.out"))}</span> <input data-out value="${esc(s.out || "out")}"></label><label><span data-i18n="req.in">${esc(t("req.in"))}</span> <input data-in value="${esc(s.in || "in")}"></label><select data-filter><option value="lowpass">${esc(t("req.lowpass"))}</option><option value="highpass" ${s.filter_type==="highpass"?"selected":""}>${esc(t("req.highpass"))}</option></select><button class="btn" data-remove>${esc(t("req.remove"))}</button>`;
        const metricSel = row.querySelector("[data-metric]");
        const sync = () => { row.querySelector("[data-component-wrap]").hidden = metricSel.value !== "component_value"; };
        metricSel.addEventListener("change", sync);
        sync();
      }
      row.querySelector("[data-remove]").onclick=()=>row.remove();
      dlg.querySelector(".requirement-rows").append(row);
    }
    (p.requirements || []).forEach(add);
    dlg.querySelector("[data-add]").onclick=()=>add();
    dlg.querySelector("[data-close]").onclick=()=>dlg.close();
    dlg.addEventListener("close", ()=>dlg.remove());
    dlg.querySelector("[data-save]").onclick=async()=>{
      try {
        const requirements = [...dlg.querySelectorAll(".requirement-row")].map(row => row.dataset.preserve
          ? requirementFromEditor({preserve: JSON.parse(row.dataset.preserve)})
          : requirementFromEditor({
            metric:row.querySelector("[data-metric]").value, op:row.querySelector("[data-op]").value,
            target:row.querySelector("[data-target]").value, tol:Number(row.querySelector("[data-tol]").value)/100,
            out:row.querySelector("[data-out]").value, in:row.querySelector("[data-in]").value,
            filter_type:row.querySelector("[data-filter]").value,
            component:row.querySelector("[data-component]").value, unit:row.querySelector("[data-unit]").value,
            name:row.dataset.name,
          }));
        const r = await fetch("/api/project/requirements", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({requirements, expected_revision:p.revision})});
        const d = await r.json(); if (!r.ok) throw new Error(tr(d.error) || t("llm.saveFail"));
        await window.VPBoard?.reloadProject(); dlg.close();
      } catch(e) { dlg.querySelector("[data-message]").textContent=e.message; }
    };
    document.body.append(dlg); dlg.showModal();
  }
  document.addEventListener("vp-lang", () => {
    document.querySelectorAll("dialog.proof-dialog, .proof-wave").forEach(node => VPI18n.apply(node));
    document.querySelectorAll(".proof-wave").forEach(box => {
      if (!box._wave) return;
      const data = box._wave;
      const axis = data.analysis==='ac' ? t('wave.ac') : data.analysis==='tran' ? t('wave.tran') : t('wave.sample');
      const line = box.querySelector("[data-wave-axis]");
      if (line) line.textContent = `${tr(data.note)} · ${t('wave.axis', {axis})}`;
      const axisSel = box.querySelector("[data-axis]");
      if (axisSel) {
        const mag = axisSel.querySelector('option[value="signals"]');
        const phase = axisSel.querySelector('option[value="phase"]');
        if (mag) mag.textContent = t("wave.mag");
        if (phase) phase.textContent = t("wave.phase");
      }
    });
  });
  return {esc, label, checks, details, comparison, compareVersions, applyVersion, editRequirements, requirementFromEditor, resultClass, clearsDirty, gridFromPointer, projectLoadPanel};
})();
