/* 所有入口共用的验证结论、验收表和条件编辑器。 */
window.VPProof = (() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const labels = {passed:"✓ 指标达标", simulated:"仿真完成 · 尚未设置验收目标", incomplete:"验证不完整", failed:"未通过验证", cancelled:"任务已取消", explained:"电路解读"};
  const metrics = {fc:"截止频率", gain:"增益", ripple:"纹波", mean:"输出电压", vpp:"峰峰值", freq:"主频", rms:"有效值", thd:"总谐波失真", component_value:"元件参数"};
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
  function requirementFromEditor(input) {
    if (input && input.preserve) {
      const spec = input.preserve;
      if (!spec || typeof spec !== "object" || !spec.metric) throw new Error("无法保存未识别的验收条件");
      return spec;
    }
    const metric = input && input.metric;
    if (!metric || !Object.prototype.hasOwnProperty.call(metrics, metric)) throw new Error("请为每条条件选择指标");
    const item = {
      metric, op: input.op || "near", target: Number(input.target), tol: Number(input.tol),
      out: String(input.out || "out").trim(), in: String(input.in || "in").trim(),
      explicit_nodes: metric !== "component_value", filter_type: input.filter_type || "lowpass",
    };
    if (metric === "component_value") {
      const component = String(input.component || "").trim();
      if (!/^[RCLVI]\d+$/i.test(component)) throw new Error("元件参数需要位号，如 R1");
      item.component = component;
      item.unit = input.unit || "";
      item.name = input.name || `${component} 参数`;
    }
    return item;
  }
  const num = x => x == null ? "未测得" : Number(x).toLocaleString(undefined, {maximumSignificantDigits:6});
  function label(d) { return labels[d.verdict] || (d.ok ? "历史仿真结果 · 目标验收状态未知" : "验证失败"); }
  function checks(d) {
    const rows = (d.checks || []).map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.target_label || c.target || "—")}</td><td>${num(c.measured)} ${esc(c.unit || "")}</td><td>${c.ok === true ? "✓ 达标" : c.ok === false ? "未达标" : "未验证"}</td></tr><tr><td colspan="4" class="hints">${esc(c.detail || "")} ${esc(c.window || "")}</td></tr>`).join("");
    return rows ? `<table class="proof-table"><thead><tr><th>指标</th><th>目标</th><th>实测</th><th>判定</th></tr></thead><tbody>${rows}</tbody></table>` : "";
  }
  function details(d) {
    return `${(d.warnings || []).map(w => `<div class="proof-warning">${esc(w)}</div>`).join("")}
      ${d.from_cache ? `<div class="hints">复用历史验证结果 · 原始运行 ${esc(d.provenance?.created || "")}</div>` : ""}
      ${d.version_id ? `<div class="hints">电路版本 ${esc(d.version_id.slice(0, 8))}${d.applied === false ? " · 候选，尚未应用" : ""}</div>` : ""}
      ${d.changes ? `<details><summary>查看元件与网表修改</summary><pre>${esc(d.changes)}</pre></details>` : ""}
      ${d.task_id && d.validation?.measurement === "passed" ? `<button class="btn" data-wave-preview="${esc(d.task_id)}">交互查看波形</button>` : ""}
      ${d.candidate_id && !d.applied ? `<button class="btn" data-apply-version="${esc(d.candidate_id)}" data-base-revision="${d.base_revision}">应用此候选方案</button>` : ""}`;
  }
  function comparison(d) {
    if(!d?.left_id) return '';
    return `<div class="hints">${esc(d.note)}</div><table class="proof-table"><tr><th>指标</th><th>${esc(d.left_id.slice(0,8))}</th><th>${esc(d.right_id.slice(0,8))}</th><th>变化</th></tr>${(d.measurements || []).map(m=>`<tr><td>${esc(m.name)} ${esc(m.unit)}</td><td>${num(m.left)}</td><td>${num(m.right)}</td><td>${num(m.delta)}</td></tr>`).join('')}</table>`;
  }
  async function compareVersions() {
    await window.VPBoard?.flushProject();
    const d=await(await fetch('/api/project/versions')).json();
    const versions=d.versions || [];
    const dlg=document.createElement('dialog');dlg.className='proof-dialog';
    dlg.innerHTML=`<h3>电路版本对比</h3><p class="hints">选择两个版本，查看已保存指标和元件变化。候选版本也可查看；只有采用后才改变当前电路。</p><select data-left>${versions.map(v=>`<option value="${v.id}">${esc(v.id.slice(0,8)+' '+v.source+' '+v.created)}</option>`).join('')}</select><select data-right>${versions.map(v=>`<option value="${v.id}">${esc(v.id.slice(0,8)+' '+v.source+' '+v.created)}</option>`).join('')}</select><button class="btn" data-compare>对比</button><button class="btn" data-close>关闭</button><div data-result></div>`;
    if(versions.length>1)dlg.querySelector('[data-left]').selectedIndex=1;
    dlg.querySelector('[data-close]').onclick=()=>dlg.close();dlg.addEventListener('close',()=>dlg.remove());
    dlg.querySelector('[data-compare]').onclick=async()=>{
      const target=dlg.querySelector('[data-result]');
      try {const params=new URLSearchParams({left_id:dlg.querySelector('[data-left]').value,right_id:dlg.querySelector('[data-right]').value});
        const r=await fetch('/api/project/compare?'+params);const c=await r.json();if(!r.ok)throw new Error(c.error);
        target.innerHTML=comparison(c)+`<details open><summary>网表修改</summary><pre>${esc(c.changes)}</pre></details>`;
      }catch(e){target.textContent=e.message;}
    };
    document.body.append(dlg);dlg.showModal();
  }
  async function applyVersion(id, base) {
    await window.VPBoard?.flushProject();
    const r = await fetch("/api/project/apply", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({version_id:id, expected_revision:Number(base)})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "应用失败");
    await window.VPBoard?.reloadProject();
    await window.VPBoard?.reloadRounds();
  }
  document.addEventListener("click", async e => {
    const wave=e.target.closest("[data-wave-preview]");
    if(wave) {
      wave.disabled=true;
      try {
        const r=await fetch(`/api/files/${encodeURIComponent(wave.dataset.wavePreview)}/wave.json`);
        if(!r.ok) throw new Error("该次历史结果没有交互波形数据，请重新仿真");
        showWave(wave,await r.json());
      } catch(err) { const p=document.createElement("div");p.className="proof-warning";p.textContent=err.message;wave.after(p); }
      return;
    }
    const btn = e.target.closest("[data-apply-version]");
    if (!btn) return;
    btn.disabled = true;
    try { await applyVersion(btn.dataset.applyVersion, btn.dataset.baseRevision); btn.textContent = "已应用"; }
    catch (err) { btn.disabled = false; const p = document.createElement("div"); p.className="proof-warning"; p.textContent=err.message; btn.after(p); }
  });
  function showWave(button,data) {
    const box=document.createElement("section");box.className="proof-wave";
    box.innerHTML=`<div class="hints">${esc(data.note)} · 横轴 ${data.analysis==='ac'?'频率 Hz（对数）':data.analysis==='tran'?'时间 s':'样本'}</div><div data-signals></div><div data-cursor class="hints">移动鼠标读取波形；拖动下方范围查看局部。</div><canvas height="240"></canvas><label>起点 <input data-start type="range" min="0" max="98" value="0"></label><label>终点 <input data-end type="range" min="1" max="100" value="100"></label>${Object.keys(data.phase || {}).length ? '<label>纵轴 <select data-axis><option value="signals">幅值</option><option value="phase">节点相位 °</option></select></label>':''}`;
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
      const width=Math.max(280,box.clientWidth),height=240;
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
    dlg.innerHTML = `<h3>电路验收条件</h3><p class="hints">数值使用基本单位 Hz、V、倍、%。保存后需重新仿真；自然语言中的新目标仍可更新这些条件。</p><div class="requirement-rows"></div><button class="btn" data-add>＋ 添加条件</button><p class="proof-warning" data-message></p><div class="rowbtns"><button class="btn primary" data-save>保存条件</button><button class="btn" data-close>取消</button></div>`;
    function add(s={metric:"fc",target:1000,tol:.05,op:"near",out:"out",in:"in"}) {
      const row = document.createElement("div"); row.className="requirement-row";
      if (s.metric && !Object.prototype.hasOwnProperty.call(metrics, s.metric)) {
        row.dataset.preserve = JSON.stringify(s);
        row.innerHTML = `<span class="hints">保留原条件：${esc(s.name || s.metric)} = ${esc(s.target)} ${esc(s.unit || "")}</span><button class="btn" data-remove>删除</button>`;
      } else {
        row.dataset.name = s.name || "";
        row.innerHTML = `<select data-metric>${Object.entries(metrics).map(([k,v])=>`<option value="${k}" ${s.metric===k?"selected":""}>${v}</option>`).join("")}</select><select data-op>${[["near","约等于"],["le","≤"],["lt","<"],["ge","≥"],["gt",">"]].map(([k,v])=>`<option value="${k}" ${s.op===k?"selected":""}>${v}</option>`).join("")}</select><span data-component-wrap><label>位号 <input data-component value="${esc(s.component || "")}"></label><label>单位 <input data-unit value="${esc(s.unit || "")}"></label></span><label>目标 <input data-target type="number" step="any" value="${esc(s.target)}"></label><label>容差 % <input data-tol type="number" min="0" max="100" value="${(s.tol ?? .05)*100}"></label><label>输出节点 <input data-out value="${esc(s.out || "out")}"></label><label>输入节点 <input data-in value="${esc(s.in || "in")}"></label><select data-filter><option value="lowpass">低通</option><option value="highpass" ${s.filter_type==="highpass"?"selected":""}>高通</option></select><button class="btn" data-remove>删除</button>`;
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
        const d = await r.json(); if (!r.ok) throw new Error(d.error || "保存失败");
        await window.VPBoard?.reloadProject(); dlg.close();
      } catch(e) { dlg.querySelector("[data-message]").textContent=e.message; }
    };
    document.body.append(dlg); dlg.showModal();
  }
  return {esc, label, checks, details, comparison, compareVersions, applyVersion, editRequirements, requirementFromEditor, resultClass, clearsDirty};
})();
