/* VoltProof PCB 页签（实验性）。
 * 面板流：工具能力 → 预检（角色/阻断/端口候选）→ 任务（轮询阶段）→
 * 版本（候选/采用/导出/预览）。预览为归档板文件的简化渲染：
 * 板框 + 焊盘（按位号着色系）+ 走线（F.Cu 正面 / B.Cu 背面）+ 过孔。
 * IIFE 封装，宿主接口 window.VPPcb = { setVisible }。 */
(function () {
  'use strict';
  const tt = (k, v) => VPI18n.t(k, v);
  const trs = s => VPI18n.tr(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const $ = (id) => document.getElementById(id);
  const svg = $('pcbSvg');
  const NS = 'http://www.w3.org/2000/svg';
  let pollTimer = null;
  let pre = null;           // 最近一次 preflight 结果
  let selectedPorts = new Set();
  let portsTouched = false;
  let lastVersionId = null; // 最近完成任务产出的候选（预览用）

  // —— 页签可见性（首次进入才拉数据）——
  let inited = false;
  function setVisible(v) {
    if (v && !inited) { inited = true; refreshCaps(); refreshVersions(); }
  }
  $('pcbBtn').addEventListener('click', () => {
    // 宽屏 header 按钮 = toggle（窄屏由 tabbar 三选一）
    const on = document.body.classList.toggle('tab-pcb');
    setVisible(on);
  });

  async function api(path, body, method) {
    const opt = { method: method || (body ? 'POST' : 'GET') };
    if (body) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(trs(d.error) || (r.status + ' ' + r.statusText));
    return d;
  }

  // —— 工具能力 ——
  async function refreshCaps() {
    const box = $('pcbCaps');
    try {
      const c = await api('/api/pcb/capabilities');
      const dot = (ok, name, hint) =>
        `<div class="pcbstage"><span class="${ok ? 'ok' : 'no'}">${ok ? '●' : '○'}</span>` +
        `<span>${esc(name)}${ok ? '' : ' —— ' + esc(hint)}</span></div>`;
      box.innerHTML = '<b style="font-size:13.5px">' + esc(tt('pcb.tools')) + '</b>' +
        dot(c.kicad.found, 'KiCad' + (c.kicad.version ? ' ' + c.kicad.version : ''), trs(c.kicad.hint)) +
        dot(c.freerouting.found, tt('pcb.free', {ver: c.freerouting.version || tt('pcb.freeMissing')}), trs(c.freerouting.hint)) +
        dot(c.java.found, tt('pcb.java'), trs(c.java.hint)) +
        '<div class="hint" style="margin-top:6px">' + esc(tt('pcb.catalog', {n: c.catalog_entries})) +
        esc(c.available ? tt('pcb.ready') : tt('pcb.notReady')) +
        '</div>';
    } catch (e) { box.innerHTML = '<span class="hint" style="color:var(--warn)">' + esc(tt('pcb.capFail', {msg: e.message})) + '</span>'; }
  }

  // —— 预检 ——
  let allNets = [];           // 预检返回的全部网络（端口手工添加用）

  async function doPreflight() {
    const out = $('pcbPreOut');
    out.textContent = tt('pcb.preRunning');
    $('pcbRunBtn').disabled = true;
    try {
      const body = { use_project: true };
      if (portsTouched) body.ports = [...selectedPorts];
      else body.auto_ports = true;
      pre = await api('/api/pcb/preflight', body);
      const roles = pre.elements.map(e =>
        `<span class="badge">${esc(e.ref)}·${esc(e.kind)}→${esc(e.role === 'device' ? tt('pcb.role.device') : e.role === 'stimulus' ? tt('pcb.role.stimulus') : tt('pcb.role.blocked'))}</span>`).join('');
      let html = '<div>' + roles + '</div>';
      if (pre.blockers.length) {
        html += '<div style="color:var(--warn);margin-top:6px">' + esc(tt('pcb.blockers')) + '</div>' +
          pre.blockers.map(b => `<div class="pcbstage"><span class="no">✗</span><span>${esc(b.ref)}：${esc(trs(b.reason))}</span></div>`).join('');
      }
      if (pre.current_board_stale)
        html += '<div style="color:#ffb27d;margin-top:6px">' + esc(trs(pre.current_board_reason)) + '</div>';
      if (!pre.tools_ready)
        html += '<div style="color:var(--warn);margin-top:6px">' + esc(pre.tool_hints.map(trs).join('；')) + '</div>';
      // 端口候选：激励源网络默认勾选；普通输出端口可从全部网络手工添加
      allNets = (pre.nets || []).filter(n => n !== 'GND');
      const newSel = new Set([...selectedPorts].filter(p => allNets.includes(p)));
      for (const p of pre.port_candidates) newSel.add(p);
      selectedPorts = newSel;
      renderPorts();
      if (pre.port_candidates.length)
        html += '<div class="hint" style="margin-top:6px">' + esc(tt('pcb.portsHint')) + '</div>';
      out.innerHTML = html;
      $('pcbRunBtn').disabled = !!pre.blockers.length;
    } catch (e) {
      out.innerHTML = '<span style="color:var(--warn)">' + e.message + '</span>';
    }
  }

  function renderPorts() {
    const box = $('pcbPorts');
    box.innerHTML = allNets.map(p =>
      `<span class="pport${selectedPorts.has(p) ? ' on' : ''}" data-p="${esc(p)}">${esc(p)}${selectedPorts.has(p) ? esc(tt('pcb.portTag')) : ''}</span>`).join('') || '';
    box.querySelectorAll('.pport').forEach(el => el.addEventListener('click', () => {
      portsTouched = true;
      const p = el.dataset.p;
      if (selectedPorts.has(p)) selectedPorts.delete(p);
      else selectedPorts.add(p);
      renderPorts();
    }));
  }

  function boardOptions() {
    const opts = { mount: $('pcbMount')?.value || 'tht' };
    const w = parseFloat($('pcbW')?.value), h = parseFloat($('pcbH')?.value);
    if (w > 0) opts.board_w = w;
    if (h > 0) opts.board_h = h;
    if ($('pcbHoles')?.checked) opts.mounting_holes = true;
    return opts;
  }

  // —— 任务（SSE 优先，失败回退轮询）——
  function watchJob(jid, onDone) {
    $('pcbCancelBtn').style.display = '';
    $('pcbCancelBtn').onclick = () => fetch('/api/pcb/jobs/' + jid + '/cancel', { method: 'POST' });
    const stages = [];
    const render = (ev) => {
      stages.push(ev);
      $('pcbJobOut').innerHTML = stages.map(s =>
        `<div class="pcbstage"><span class="${s.ok === true ? 'ok' : s.ok === false ? 'no' : ''}">` +
        `${s.ok === true ? '✓' : s.ok === false ? '✗' : '…'}</span><span>${esc(trs(s.stage || s.type || ''))}${s.detail ? '：' + esc(trs(s.detail)) : ''}</span></div>`
      ).join('');
    };
    const escJob = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const renderJobDone = (ev) => {
      const blockers = Array.isArray(ev.blockers) ? ev.blockers : [];
      const hints = Array.isArray(ev.tool_hints) ? ev.tool_hints : [];
      const failed = ev.ok !== true || blockers.length > 0;
      const seconds = ev.elapsed_s != null ? tt('pcb.seconds', {s: ev.elapsed_s}) : '';
      render({ stage: failed ? tt('pcb.jobFail') : tt('pcb.jobDone'), ok: !failed,
               detail: (failed ? tt('pcb.jobNotDone') : tt('pcb.jobFinished')) + seconds });
      const lines = blockers.map(b => typeof b === 'string' ? b : ((b.ref ? b.ref + '：' : '') + (b.reason || ''))).concat(hints);
      if (failed && lines.some(Boolean)) {
        $('pcbJobOut').insertAdjacentHTML('beforeend', lines.filter(Boolean).map(t =>
          `<div class="pcbstage"><span class="no">✗</span><span>${escJob(t)}</span></div>`).join(''));
      }
    };
    let es = null;
    try {
      es = new EventSource('/api/pcb/jobs/' + jid + '/events?lang=' + encodeURIComponent(VPI18n.apiLang()));
      let got = false;
      es.onmessage = (m) => {
        got = true;
        const ev = JSON.parse(m.data);
        if (ev.type === 'done') {
          es.close(); es = null;
          $('pcbCancelBtn').style.display = 'none';
          renderJobDone(ev);
          onDone(ev);
        } else if (ev.type === 'error') {
          es.close(); es = null;
          $('pcbCancelBtn').style.display = 'none';
          render({ stage: tt('pcb.jobFail'), ok: false, detail: trs(ev.error) });
          onDone(ev);
        } else {
          render(ev);
        }
      };
      es.onerror = () => { if (es) { es.close(); es = null; if (!got) pollFallback(); } };
    } catch (e) { es = null; }
    function pollFallback() {
      pollTimer = setInterval(async () => {
        try {
          const st = await api('/api/pcb/jobs/' + jid);
          while (stages.length < (st.stages || []).length)
            render((st.stages || [])[stages.length]);
          if (st.done) {
            clearInterval(pollTimer); pollTimer = null;
            $('pcbCancelBtn').style.display = 'none';
            renderJobDone(st);
            onDone(st);
          }
        } catch (e) { /* 网络抖动继续轮询 */ }
      }, 1200);
    }
    if (!es) pollFallback();
  }

  async function runJob() {
    const out = $('pcbJobOut');
    $('pcbJobBox').style.display = '';
    $('pcbRunBtn').disabled = true;
    out.innerHTML = '<span class="hint">' + esc(tt('pcb.submitted')) + '</span>';
    try {
      const j = await api('/api/pcb/jobs', { use_project: true, ports: [...selectedPorts],
                                             options: boardOptions(), timeout_s: 180 });
      watchJob(j.job_id, (st) => {
        $('pcbRunBtn').disabled = false;
        if (st.ok) {
          lastVersionId = st.version_id;
          renderPreview(lastVersionId);
        }
        refreshVersions();
      });
    } catch (e) {
      out.innerHTML = '<span style="color:var(--warn)">' + e.message + '</span>';
      $('pcbRunBtn').disabled = false;
      $('pcbCancelBtn').style.display = 'none';
    }
  }

  // —— 版本 ——
  async function refreshVersions() {
    const box = $('pcbVerList');
    try {
      const d = await api('/api/pcb/versions');
      if (!d.versions.length) { box.innerHTML = '<span class="hint">' + esc(tt('pcb.noVer')) + '</span>'; return; }
      box.innerHTML = d.versions.map(v => {
        const cur = d.current && d.current.version_id === v.pcb_version_id;
        return `<div class="vercard ${cur ? 'current' : ''}" data-vid="${v.pcb_version_id}">` +
          `<span class="st ${v.status === 'checked' ? 'checked' : ''}">${v.status}</span> ` +
          `<span class="hint">${v.created_at || ''} · ${v.tools ? v.tools.kicad + '/' + v.tools.freerouting : ''}</span>` +
          `<div class="row">` +
          (cur ? '<b style="color:var(--acc)">' + esc(tt('pcb.current')) + '</b>'
               : `<button class="btn p-adopt">${esc(tt('pcb.adopt'))}</button>`) +
          `<button class="btn p-view">${esc(tt('pcb.view'))}</button>` +
          `<button class="btn p-export" ${v.status === 'checked' && cur ? '' : 'disabled title="' + esc(tt('pcb.exportTip')) + '"'}>${esc(tt('pcb.export'))}</button>` +
          `</div></div>`;
      }).join('');
      box.querySelectorAll('.vercard').forEach(card => {
        const vid = card.dataset.vid;
        card.querySelector('.p-view').onclick = () => renderPreview(vid);
        card.querySelector('.p-export')?.addEventListener('click', async () => {
          try {
            const e = await api('/api/pcb/export', { version_id: vid });
            alert(tt('pcb.exportOk', {n: e.files.length, dir: e.download_dir}));
          } catch (err) { alert(tt('pcb.exportFail', {msg: err.message})); }
        });
        card.querySelector('.p-adopt')?.addEventListener('click', async () => {
          try {
            // F06：带当前指针 revision（迟到采用 → 409，页面提示重载）
            await api('/api/pcb/apply', { version_id: vid,
                                          expected_revision: (d.current && d.current.revision) || 0 });
            refreshVersions();
          } catch (err) { alert(tt('pcb.adoptFail', {msg: err.message})); }
        });
      });
    } catch (e) { box.innerHTML = '<span class="hint" style="color:var(--warn)">' + e.message + '</span>'; }
  }

  // —— 预览渲染 + 布局调整（拖动元件 → PUT placement → 新候选）——
  let view = null;              // {x0,y0,vs,pad,vid,refEls:{ref:[els]},centers:{ref:[x,y]}}
  let pendingMoves = {};        // ref -> {x,y}（板坐标，毫米）
  let dragRef = null, dragStart = null;

  async function renderPreview(vid) {
    if (!vid) return;
    const cap = $('pcbViewCap');
    cap.textContent = tt('pcb.loading');
    try {
      const d = await api('/api/pcb/preview/' + vid);
      drawBoard(d);
      cap.textContent = tt('pcb.caption', {
        id: vid.slice(0, 15), status: d.status, pads: d.pads.length, tracks: d.tracks.length,
        front: d.tracks.filter(t => t.layer === 'F.Cu').length,
        back: d.tracks.filter(t => t.layer === 'B.Cu').length,
        vias: d.vias.length, w: Math.round(d.bbox[2] - d.bbox[0]), h: Math.round(d.bbox[3] - d.bbox[1]),
      });
    } catch (e) { cap.textContent = tt('pcb.previewFail', {msg: e.message}); }
  }

  function drawBoard(d) {
    const [x0, y0, x1, y1] = d.bbox;
    const pad = 4;
    const vs = Math.min(1000 / (x1 - x0 + pad * 2), 700 / (y1 - y0 + pad * 2));
    const px = (x) => ((x - x0) + pad) * vs;
    const py = (y) => ((y - y0) + pad) * vs;
    const el = (tag, attrs) => {
      const e = document.createElementNS(NS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      return e;
    };
    svg.innerHTML = '';
    svg.appendChild(el('rect', { x: 0, y: 0, width: 1000, height: 700, fill: '#0d1a14' })); // PCB 绿底暗色
    const g = el('g', {});
    svg.appendChild(g);
    for (const [x, y] of d.outline)
      g.appendChild(el('circle', { cx: px(x), cy: py(y), r: 1.2, fill: '#f4d35e', opacity: .8 }));
    const trackEls = [];
    for (const t of d.tracks) {
      const back = t.layer === 'B.Cu';
      const ln = el('line', {
        x1: px(t.x1), y1: py(t.y1), x2: px(t.x2), y2: py(t.y2),
        stroke: back ? '#c9a227' : '#e8e2d0',  // 背金正银
        'stroke-width': Math.max(0.8, t.w * vs), opacity: back ? .55 : .95,
        'stroke-linecap': 'round',
      });
      trackEls.push(ln); g.appendChild(ln);
    }
    for (const v of d.vias)
      g.appendChild(el('circle', { cx: px(v.x), cy: py(v.y), r: Math.max(1, v.d * vs / 2),
                                   fill: '#9ad0ec', stroke: '#333', 'stroke-width': .4 }));
    // 焊盘（按 ref 分组登记，供拖动整件移动）+ 位号标签
    // F07 修正：拖动语义 = 移动【封装原点】，焊盘随原点刚性平移（各自保持
    // 相对位置），提交的坐标也是原点——曾把焊盘中心均值当原点提交、且把
    // 所有焊盘画到同一点。
    const refEls = {}, labelEls = {}, padsByRef = {};
    const origins = d.origins || {};
    for (const p of d.pads) {
      const r = el('rect', {
        x: px(p.x) - p.w * vs / 2, y: py(p.y) - p.h * vs / 2,
        width: p.w * vs, height: p.h * vs,
        fill: p.net === 'GND' ? '#7ddc9f' : '#f4a259',
        opacity: .95, rx: .5, style: 'cursor:move',
      });
      r.dataset.ref = p.ref;
      const t = el('title', {});
      t.textContent = tt('pcb.dragTip', {ref: p.ref, net: p.net});
      r.appendChild(t);
      (refEls[p.ref] = refEls[p.ref] || []).push(r);
      (padsByRef[p.ref] = padsByRef[p.ref] || []).push(p);
      g.appendChild(r);
      if (!labelEls[p.ref]) {
        const lab = el('text', { x: px(p.x), y: py(p.y) - p.h * vs,
                                 fill: '#cbd5e1', 'font-size': Math.max(7, 10 * vs / 8) });
        lab.textContent = p.ref;
        lab.dataset.dx = px(p.x);   // 标签锚定首个焊盘的原始屏幕位
        lab.dataset.dy = py(p.y) - p.h * vs;
        labelEls[p.ref] = lab; g.appendChild(lab);
      }
    }
    view = { x0, y0, vs, pad, vid: d.version_id, refEls, labelEls, padsByRef,
             origins, trackEls };
    pendingMoves = {};
    updateToolbar();
  }

  function svgToBoard(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const sp = pt.matrixTransform(svg.getScreenCTM().inverse());
    return [ (sp.x / view.vs) - view.pad + view.x0, (sp.y / view.vs) - view.pad + view.y0 ];
  }

  function applyOrigin(ref, newOrigin) {
    // 焊盘按【相对原点的原始位移】平移（封装内相对坐标不因拖动改变），
    // 标签随首焊盘走——绝不再把多个焊盘叠到同一点
    const origin0 = view.origins[ref] || { x: 0, y: 0 };
    const dx = newOrigin.x - origin0.x, dy = newOrigin.y - origin0.y;
    const sx = (bx) => (bx - view.x0 + view.pad) * view.vs;
    const sy = (by) => (by - view.y0 + view.pad) * view.vs;
    view.padsByRef[ref].forEach((p, i) => {
      const r = view.refEls[ref][i];
      r.setAttribute('x', sx(p.x + dx) - p.w * view.vs / 2);
      r.setAttribute('y', sy(p.y + dy) - p.h * view.vs / 2);
    });
    const lab = view.labelEls[ref];
    if (lab) {
      const p0 = view.padsByRef[ref][0];
      lab.setAttribute('x', sx(p0.x + dx));
      lab.setAttribute('y', sy(p0.y + dy) - p0.h * view.vs);
    }
  }

  svg.addEventListener('mousedown', (e) => {
    if (!view) return;
    const ref = e.target.dataset && e.target.dataset.ref;
    if (!ref || !view.origins[ref]) return;
    dragRef = ref;
    selectRef(ref);
    // 基准 = 原始原点 + 已有待提交位移（支持一次拖动未提交后再拖）
    const base = pendingMoves[ref] ||
      { x: view.origins[ref].x, y: view.origins[ref].y };
    dragStart = { mouse: svgToBoard(e), base };
    e.preventDefault();
  });
  svg.addEventListener('mousemove', (e) => {
    if (!dragRef || !view) return;
    const m = svgToBoard(e);
    const nx = dragStart.base.x + (m[0] - dragStart.mouse[0]);
    const ny = dragStart.base.y + (m[1] - dragStart.mouse[1]);
    applyOrigin(dragRef, { x: nx, y: ny });
    pendingMoves[dragRef] = { x: +nx.toFixed(2), y: +ny.toFixed(2) };
    updateToolbar();
  });
  svg.addEventListener('mouseup', (e) => {
    if (dragRef && dragStart) {
      const m = svgToBoard(e);
      const moved = Math.hypot(m[0] - dragStart.mouse[0], m[1] - dragStart.mouse[1]);
      if (moved < 0.3) highlightNetOfRef(dragRef);  // 无位移的 mouseup=点击 → 网络高亮
    }
    dragRef = null;
  });

  // —— 选中元件的旋转/锁定（调整条内联操作，进 pendingMoves）——
  let selectedRef = null;
  function selectRef(ref) {
    selectedRef = ref;
    for (const r of $('pcbSelOps')?.querySelectorAll('button') || []) r.disabled = !ref;
    $('pcbSelName').textContent = ref ? tt('pcb.selected', {ref}) : '';
  }
  $('pcbRotL')?.addEventListener('click', () => rotateSelected(-90));
  $('pcbRotR')?.addEventListener('click', () => rotateSelected(90));
  $('pcbLockBtn')?.addEventListener('click', () => {
    if (!selectedRef || !view) return;
    const base = pendingMoves[selectedRef] ||
      { x: view.origins[selectedRef].x, y: view.origins[selectedRef].y };
    base.lock = !base.lock;
    pendingMoves[selectedRef] = base;
    $('pcbLockBtn').textContent = base.lock ? tt('pcb.unlock') : tt('pcb.lock');
    updateToolbar();
  });
  function rotateSelected(deg) {
    if (!selectedRef || !view) return;
    const base = pendingMoves[selectedRef] ||
      { x: view.origins[selectedRef].x, y: view.origins[selectedRef].y };
    const cur = view.origins[selectedRef]?.rot || 0;
    base.rot = Math.round(((base.rot ?? cur) + deg + 360) % 360);
    pendingMoves[selectedRef] = base;
    updateToolbar();
  }

  // —— 网络高亮：点击焊盘高亮同网络焊盘（描边），再点空白清除 ——
  let highlightedNet = null;
  function highlightNetOfRef(ref) {
    selectRef(ref);
    if (!view) return;
    const pad = (view.padsByRef[ref] || [])[0];
    const net = pad && pad.net;
    highlightedNet = (highlightedNet === net || !net) ? null : net;
    for (const r of Object.values(view.refEls).flat())
      r.setAttribute('stroke', 'none');
    if (highlightedNet) {
      for (const [rref, pads] of Object.entries(view.padsByRef))
        pads.forEach((p, i) => {
          if (p.net === highlightedNet)
            view.refEls[rref][i].setAttribute('stroke', '#ff5f7e');
        });
      $('pcbViewCap').textContent += tt('pcb.highlight', {net: highlightedNet});
    }
  }
  svg.addEventListener('click', (e) => {
    if (!e.target.dataset || !e.target.dataset.ref) { highlightedNet = null; selectRef(null);
      for (const r of Object.values(view?.refEls || {}).flat()) r.setAttribute('stroke', 'none'); }
  });

  function updateToolbar() {
    const bar = $('pcbAdjustBar');
    const n = Object.keys(pendingMoves).length;
    bar.style.display = (n || selectedRef) ? 'flex' : 'none';
    $('pcbAdjustCount').textContent = tt('pcb.moved', {n});
  }

  async function rerouteWithMoves() {
    if (!view || !Object.keys(pendingMoves).length) return;
    const out = $('pcbJobOut');
    $('pcbJobBox').style.display = '';
    out.innerHTML = '<span class="hint">' + esc(tt('pcb.rerouting')) + '</span>';
    $('pcbRerouteBtn').disabled = true;
    try {
      const res = await api(`/api/pcb/versions/${view.vid}/placement`, { moves: pendingMoves }, 'PUT');
      const rows = (res.stages || []).map(s =>
        `<div class="pcbstage"><span class="${s.ok ? 'ok' : 'no'}">${s.ok ? '✓' : '✗'}</span>` +
        `<span>${esc(trs(s.stage))}${s.detail ? '：' + esc(trs(s.detail)) : ''}</span></div>`).join('') +
        (res.blockers || []).map(b => `<div class="pcbstage"><span class="no">✗</span><span>${esc(trs(b))}</span></div>`).join('');
      out.innerHTML = rows + `<div class="hint">${esc(tt('pcb.stateLine', {state: res.state, extra: res.elapsed_s != null ? '，' + res.elapsed_s + 's' : ''}))}</div>`;
      if (res.ok) {
        pendingMoves = {};
        updateToolbar();
        lastVersionId = res.version_id;
        renderPreview(res.version_id);
      }
      refreshVersions();
    } catch (e) {
      out.innerHTML += `<div style="color:var(--warn)">${e.message}</div>`;
    }
    $('pcbRerouteBtn').disabled = false;
  }

  // —— 绑定 ——
  $('pcbPreBtn').addEventListener('click', doPreflight);
  $('pcbRunBtn').addEventListener('click', runJob);
  $('pcbRunBtn').disabled = true; // 先预检
  $('pcbRerouteBtn').addEventListener('click', rerouteWithMoves);
  $('pcbAdjustResetBtn').addEventListener('click', () => {
    pendingMoves = {};
    if (view) renderPreview(view.vid);
  });

  document.addEventListener('vp-lang', () => {
    if (!inited) return;
    refreshCaps();
    refreshVersions();
    if (pre) doPreflight();
    updateToolbar();
    if (selectedRef) selectRef(selectedRef);
  });

  window.VPPcb = { setVisible };
})();
