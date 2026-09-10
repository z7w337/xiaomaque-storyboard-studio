/* 全局左侧导航（v1.30~v1.41）：
   v1.39/v1.41 定稿——导航仅宽/窄二态切换（取消消失态/悬停展开/自动收起/锁概念）；
   默认态=宽；切换按钮=方向箭头（宽态 ← 点击收窄 / 窄态 → 点击展开，悬浮提示）；
   窄态=图标栏（仅图标，每个图标悬浮提示）；样式参考 Infinite Canvas 导航锁（36×36 圆角按钮）；
   状态持久化 localStorage ss_nav_mode（wide/narrow）；两态导航项 + 工作台项 + 历史创作内嵌 + 全局 toast */
(function () {
  const CSS = `
  #sidebar { background:#f5f6f8; border-right:1px solid #e5e7eb; width:240px; flex:none; transition:width .18s; display:flex; flex-direction:column; overflow:hidden; position:sticky; top:0; height:100vh; }
  #sidebar.narrow { width:56px; }
  #sidebar.narrow .sb-section { padding:10px 4px; }
  #sidebar.narrow .nav-item { justify-content:center; padding:9px 0; }
  #sidebar.narrow .nav-item .txt, #sidebar.narrow .sb-section .sec-t, #sidebar.narrow .sb-top .logo, #sidebar.narrow .sb-top .proj-tag, #sidebar.narrow .sb-history { display:none; }
  .sb-top { display:flex; align-items:center; gap:8px; height:56px; padding:0 10px; background:#1f2937; box-sizing:border-box; }
  .sb-top .logo { font-size:15px; font-weight:600; white-space:nowrap; overflow:hidden; flex:1; cursor:pointer; color:#fff; }
  .sb-top .proj-tag { font-size:11px; color:#93c5fd; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:90px; }
  /* 导航切换按钮（v1.41：方向箭头，参考 IC 导航锁样式——36×36 圆角、2px 边框、hover 光晕） */
  .nav-toggle { width:36px; height:36px; flex:none; border:2px solid #cbd5e1; border-radius:12px; background:#fff; color:#334155; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; font-size:16px; line-height:1; transition:border-radius .3s, background-color .18s, box-shadow .2s, transform .3s; }
  .nav-toggle:hover { background:#f1f5f9; border-radius:50%; box-shadow:0 0 0 4px rgba(148,163,184,.16), 0 10px 24px rgba(15,23,42,.08); }
  .nav-toggle:active { transform:scale(.94); }
  .sb-section { padding:10px 8px; }
  .sb-section .sec-t { font-size:11px; color:#9ca3af; padding:0 8px 6px; white-space:nowrap; }
  .nav-item { display:flex; align-items:center; gap:8px; padding:9px 10px; border-radius:8px; font-size:13px; color:#374151; cursor:pointer; white-space:nowrap; text-decoration:none; }
  .nav-item:hover { background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.05); }
  .nav-item.active { background:#dbeafe; color:#2563eb; font-weight:600; }
  .nav-item .ic { flex:none; width:20px; text-align:center; }
  .nav-item.placeholder { color:#9ca3af; }
  .sb-history { padding:6px 8px; }
  .sb-history .h-item { padding:6px 10px; font-size:12px; color:#6b7280; border-radius:6px; cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sb-history .h-item:hover { background:#fff; }
  .sb-history .h-item .t { color:#9ca3af; font-size:10px; }
  #toast { position:fixed; top:14px; right:16px; z-index:300; display:none; padding:10px 16px; border-radius:8px; font-size:13px; box-shadow:0 4px 14px rgba(0,0,0,.18); max-width:340px; }
  /* 全局自定义模态对话框（v1.42：弃用原生 confirm/alert/prompt——顶部白底黑字对话框，统一页面内模态） */
  #g-modal-mask { position:fixed; inset:0; background:rgba(17,24,39,.4); z-index:700; display:none; align-items:center; justify-content:center; }
  #g-modal-mask .g-modal { background:#fff; border-radius:12px; width:420px; max-width:92vw; padding:18px 20px; box-shadow:0 10px 30px rgba(0,0,0,.2); }
  #g-modal-mask .g-modal h3 { font-size:15px; color:#1f2937; margin:0 0 10px; }
  #g-modal-mask .g-modal .g-msg { font-size:13px; color:#4b5563; line-height:1.8; white-space:pre-wrap; word-break:break-word; }
  #g-modal-mask .g-modal input[type=text] { width:100%; box-sizing:border-box; margin-top:10px; padding:8px 10px; border:1px solid #d1d5db; border-radius:8px; font-size:13px; }
  #g-modal-mask .g-modal .g-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
  #g-modal-mask .g-modal .g-btn { padding:8px 16px; border:none; border-radius:8px; cursor:pointer; font-size:13px; }
  #g-modal-mask .g-modal .g-btn.g-cancel { background:#f3f4f6; color:#374151; border:1px solid #e5e7eb; }
  #g-modal-mask .g-modal .g-btn.g-ok { background:#2563eb; color:#fff; }
  #g-modal-mask .g-modal .g-btn.g-ok:hover { background:#1d4ed8; }
  /* v1.43：全站按钮 disabled 视觉置灰（统一，不依赖各页自带样式；成本确认类弹窗进度/汇总样式） */
  button:disabled { opacity:.55; cursor:not-allowed; }
  .btn-primary:disabled, .g-btn.g-ok:disabled { background:#9ca3af !important; color:#fff !important; opacity:.85; }
  #g-modal-mask .g-modal .cost-row { font-size:13px; color:#4b5563; line-height:1.9; }
  #g-modal-mask .g-modal .g-progress { display:none; margin-top:10px; font-size:13px; border-radius:8px; padding:10px 12px; line-height:1.7; word-break:break-word; height:240px; overflow-y:auto; }  /* v1.51：流式内容固定高度+内部滚动，弹窗高度恒定防抖动 */
  #g-modal-mask .g-modal .g-progress.running { display:block; color:#7c3aed; background:#f5f3ff; border:1px solid #ddd6fe; }
  #g-modal-mask .g-modal .g-progress.success { display:block; color:#16a34a; background:#dcfce7; border:1px solid #bbf7d0; }
  #g-modal-mask .g-modal .g-progress.error { display:block; color:#dc2626; background:#fee2e2; border:1px solid #fecaca; }
  #g-modal-mask .g-modal .g-progress.plain { display:block; color:#374151; background:#fff; border:1px solid #e5e7eb; }
  /* v1.45 资产信息页布局（多版本平铺/分区卡片/集多选/chips 固定高度；nav.js 注入并带 !important 防 HTML 缓存时样式不更新） */
  .meta-grid { grid-template-columns: 1fr 1.45fr !important; gap: 18px !important; }
  .meta-left { position: relative; display: flex; flex-direction: column; }   /* v1.45 复检：上传素材绝对定位左栏左下角 */
  .mp-versions { flex: 1; min-height: 120px; display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; max-height: 400px; overflow-y: auto; padding: 2px 2px 48px; }
  .upload-drop { position: absolute; bottom: 0; left: 0; right: 0; background: #fff; z-index: 2; }
  .mp-ver-card { position: relative; aspect-ratio: 3 / 4; border-radius: 10px; overflow: hidden; border: 2px solid #e5e7eb; cursor: pointer; background: #f9fafb; }
  .mp-ver-card.cur { border-color: #2563eb; }
  .mp-ver-card img { width: 100%; height: 100%; object-fit: cover; }
  .mp-ver-card .ph-card { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; width: 100%; height: 100%; color: #9ca3af; font-size: 12px; border: 2px dashed #d1d5db; border-radius: 8px; background: #fafafa; }
  .mp-ver-card .ph-ic { font-size: 24px; }
  .mp-ver-card .ph-sub { font-size: 10px; color: #b0b7c3; }
  .mp-ver-badge { position: absolute; top: 6px; left: 6px; background: #2563eb; color: #fff; font-size: 10px; border-radius: 6px; padding: 1px 6px; }
  .mp-ver-del { position: absolute; top: 4px; right: 4px; background: rgba(17,24,39,.7); color: #fff; border: none; border-radius: 50%; width: 20px; height: 20px; font-size: 12px; line-height: 20px; cursor: pointer; display: none; }
  .mp-ver-card:hover .mp-ver-del { display: flex; align-items: center; justify-content: center; }
  .mp-ver-zoom { position: absolute; bottom: 4px; right: 4px; background: rgba(17,24,39,.7); color: #fff; border: none; border-radius: 6px; width: 20px; height: 20px; font-size: 12px; line-height: 20px; cursor: pointer; display: none; align-items: center; justify-content: center; text-decoration: none; }
  .mp-ver-card:hover .mp-ver-zoom { display: flex; }
  .mp-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px 12px; margin-bottom: 12px; max-height: 300px; overflow-y: auto; }
  .mp-card-t { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }
  .ep-multi { max-height: 160px; overflow-y: auto; border: 1px solid #f3f4f6; border-radius: 8px; padding: 6px 8px; margin-top: 6px; background: #fafafa; font-size: 13px; }
  .ep-multi label { display: flex; align-items: center; gap: 6px; padding: 3px 0; cursor: pointer; }
  .scope-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; max-height: 66px !important; overflow-y: auto; }
  /* v1.50：归属明细 chips 紧凑横排（每已归属项目组一行、集标签横向滚动） */
  .scope-groups { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
  .scope-group { display: flex; align-items: center; gap: 6px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 4px 8px; max-width: 100%; }
  .scope-group-head { flex: none; font-size: 12px; font-weight: 600; color: #374151; white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; }
  .scope-group-del { border: none; background: none; cursor: pointer; color: #9ca3af; font-size: 13px; padding: 0 2px; }
  .scope-group-del:hover { color: #dc2626; }
  .scope-group-eps { display: inline-flex; gap: 6px; overflow-x: auto; white-space: nowrap; padding-bottom: 2px; }
  `;

  function activeKey() {
    const p = location.pathname;
    if (p === '/' || p.endsWith('/index.html')) return 'home';
    if (p.includes('script')) return 'script';
    if (p.includes('scene')) return 'scene';
    if (p.includes('assets')) return 'assets';
    if (p.includes('gsplat')) return 'gsplat';
    if (p.includes('gentools')) return 'gentools';
    if (p.includes('settings')) return 'settings';
    return '';
  }

  function currentPid() {
    // URL project 参数优先；否则用 localStorage 记住的当前项目（v1.41 补：切出再回来不丢项目上下文）
    return new URLSearchParams(location.search).get('project') || localStorage.getItem('ss_current_project') || '';
  }

  function injectCSS() {
    const el = document.createElement('style');
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  function navHTML() {
    const a = activeKey();
    const pid = currentPid();
    const item = (key, ic, txt, href, placeholder, extra) =>
      `<a class="nav-item ${key === a ? 'active' : ''} ${placeholder ? 'placeholder' : ''}" href="${href}" title="${txt}${placeholder ? '（阶段 ' + placeholder + ' 提供）' : ''}" ${extra || ''}><span class="ic">${ic}</span><span class="txt">${txt}</span></a>`;
    const go = (key, ic, txt, href, ph) => item(key, ic, txt, href || 'javascript:;', ph);
    const workHref = pid ? '/?project=' + pid : '/';
    const sceneClick = pid ? `onclick="event.preventDefault();window.__gotoScene('${pid}')"` : '';
    return `
    <div class="sb-top">
      <span class="logo" onclick="location.href='/'">🎬 剧本分镜工坊</span>
      ${pid ? `<span class="proj-tag" id="proj-tag" title="当前项目"></span>` : ''}
      <button class="nav-toggle" id="nav-toggle" onclick="window.__navToggle()" title="切换为窄栏"><span id="nav-ic">←</span></button>
    </div>
    <div class="sb-section">
      <div class="sec-t">项目内</div>
      ${go('home', '🏠', '工作台', workHref)}
      ${go('script', '📜', '剧本大纲', '/script.html' + (pid ? '?project=' + pid + '&view=outline' : ''))}
      ${item('scene', '🎞️', '场页', pid ? '#' : 'javascript:;', '', sceneClick)}
      ${go('assets', '🗂️', '资产库', '/assets.html')}
      ${go('gsplat', '🧊', '高斯泼溅', '/gsplat.html')}
      ${go('gentools', '🎨', '生图工具', '/gentools.html')}
      ${go('settings', '⚙️', '设置', '/settings.html')}
    </div>
    <div class="sb-section">
      <div class="sec-t">项目外</div>
      ${go('home', '🏠', '工作台', '/')}
      ${go('', '🕘', '历史创作', 'javascript:;')}
      ${go('assets', '🗂️', '资产库', '/assets.html')}
      ${go('gsplat', '🧊', '高斯泼溅', '/gsplat.html')}
      ${go('gentools', '🎨', '生图工具', '/gentools.html')}
      ${go('settings', '⚙️', '设置', '/settings.html')}
    </div>
    <div class="sb-section">
      <div class="sec-t">历史创作</div>
      <div class="sb-history" id="history-list"><div class="h-item">加载中…</div></div>
    </div>`;
  }

  // 场页导航：定位到该项目第一个场（无场时提示）
  window.__gotoScene = async function (pid) {
    try {
      if (!pid) { window.toast && window.toast('请先进入项目（工作台/剧本大纲）再点场页', 'error'); return; }
      const resp = await fetch('/api/projects/' + pid);
      if (!resp.ok) { window.toast && window.toast('项目加载失败：' + resp.status, 'error'); return; }
      const p = await resp.json();
      const ep = (p.episodes || [])[0];
      const sc = ep && (ep.scenes || [])[0];
      if (ep && sc) location.href = '/scene.html?project=' + pid + '&episode=' + ep.id;
      else { window.toast && window.toast('该项目暂无场次（可先拆解/导入生成）', 'error'); }
    } catch (e) { window.toast && window.toast('加载失败：' + e.message, 'error'); }
  };

  function init() {
    injectCSS();
    const sb = document.getElementById('sidebar');
    if (!sb) return;
    sb.innerHTML = navHTML();
    // v1.39/v1.41：宽/窄二态，默认宽，持久化 ss_nav_mode
    let narrow = localStorage.getItem('ss_nav_mode') === 'narrow';
    applyMode();

    function applyMode() {
      sb.classList.toggle('narrow', narrow);
      const ic = document.getElementById('nav-ic');
      const btn = document.getElementById('nav-toggle');
      if (narrow) { ic.textContent = '>'; btn.title = '切换为宽栏'; }
      else { ic.textContent = '<'; btn.title = '切换为窄栏'; }
    }

    window.__navToggle = function () {
      narrow = !narrow;
      localStorage.setItem('ss_nav_mode', narrow ? 'narrow' : 'wide');
      applyMode();
    };

    // 顶部显示当前项目名（v1.35 ④）
    const pid = currentPid();
    if (pid) {
      fetch('/api/projects/' + pid).then(r => r.json()).then(p => {
        const tag = document.getElementById('proj-tag');
        if (tag && p.name) tag.textContent = p.name;
      }).catch(() => {});
    }
    syncHistory();
  }

  async function syncHistory() {
    const el = document.getElementById('history-list');
    try {
      const resp = await fetch('/api/projects');
      const data = await resp.json();
      const list = (data.projects || []).sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '')).slice(0, 8);
      el.innerHTML = list.map(p =>
        `<div class="h-item" onclick="location.href='/script.html?project=${p.id}&view=outline'">${esc(p.name)}<div class="t">${(p.updated_at || '').slice(0, 10)}</div></div>`).join('')
        || '<div class="h-item">暂无项目</div>';
    } catch (e) { el.innerHTML = '<div class="h-item">加载失败</div>'; }
  }

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // 全局 toast（v1.30：右上角 fixed）
  window.toast = function (text, type) {
    let el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
    el.textContent = text;
    el.style.background = type === 'success' ? '#dcfce7' : type === 'error' ? '#fee2e2' : '#111827';
    el.style.color = type === 'success' ? '#16a34a' : type === 'error' ? '#dc2626' : '#fff';
    el.style.display = 'block';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.display = 'none'; }, 3200);
  };

  // 全局自定义模态对话框（v1.42：弃用原生 confirm/alert/prompt，统一页面内模态样式）
  function gModal() {
    let m = document.getElementById('g-modal-mask');
    if (!m) {
      m = document.createElement('div');
      m.id = 'g-modal-mask';
      m.addEventListener('click', function (e) { if (e.target === m) m.style.display = 'none'; });
      document.body.appendChild(m);
    }
    return m;
  }
  window.confirmDialog = function (title, message, onOk, okText) {
    const m = gModal();
    m.innerHTML = `<div class="g-modal"><h3>${esc(title || '确认')}</h3><div class="g-msg">${message || ''}</div>
      <div class="g-actions"><button class="g-btn g-cancel" type="button">取消</button><button class="g-btn g-ok" type="button">${esc(okText || '确定')}</button></div></div>`;
    m.style.display = 'flex';
    m.querySelector('.g-cancel').onclick = function () { m.style.display = 'none'; };
    m.querySelector('.g-ok').onclick = function () { m.style.display = 'none'; if (onOk) onOk(); };
  };
  window.promptDialog = function (title, defaultValue, onOk, placeholder) {
    const m = gModal();
    m.innerHTML = `<div class="g-modal"><h3>${esc(title || '输入')}</h3><div class="g-msg"><input type="text" id="g-input" value="${esc(defaultValue == null ? '' : defaultValue)}" placeholder="${esc(placeholder || '')}"></div>
      <div class="g-actions"><button class="g-btn g-cancel" type="button">取消</button><button class="g-btn g-ok" type="button">确定</button></div></div>`;
    m.style.display = 'flex';
    const inp = m.querySelector('#g-input');
    inp.focus();
    inp.select();
    m.querySelector('.g-cancel').onclick = function () { m.style.display = 'none'; };
    m.querySelector('.g-ok').onclick = function () { m.style.display = 'none'; if (onOk) onOk(inp.value); };
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') { m.style.display = 'none'; if (onOk) onOk(inp.value); } });
  };

  // v1.51：通用 LLM 流式请求（SSE：meta → 逐 delta → result/error）；供成本确认弹窗同窗逐步显示生成内容
  window.streamLLM = async function (task, payload, onDelta) {
    const resp = await fetch('/api/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(Object.assign({ task: task }, payload || {}))
    });
    if (!resp.ok) { let d = '请求失败'; try { d = (await resp.json()).detail || d; } catch (e) {} throw new Error(d); }
    if (!resp.body) { const j = await resp.json(); return j; }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let result = null;
    let err = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = 'message';
        let data = '';
        for (const l of block.split('\n')) {
          if (l.startsWith('event:')) event = l.slice(6).trim();
          else if (l.startsWith('data:')) data += l.slice(5).trim();
        }
        if (!data) continue;
        let obj;
        try { obj = JSON.parse(data); } catch (e) { continue; }
        if (event === 'delta') { if (onDelta) { try { onDelta(obj.text || ''); } catch (e) {} } }
        else if (event === 'result') { result = obj; }
        else if (event === 'error') { err = new Error((obj && obj.error) || '生成失败'); }
      }
    }
    if (err) throw err;
    return result;
  };
  window.costRunDialog = function (opts) {
    const m = gModal();
    const cost = opts.est || {};
    const unit = opts.unit || '¥';
    const costTxt = (unit === '积分') ? String(Math.round(cost.cost || 0)) : (cost.cost || 0).toFixed(4);
    const costRows = [
      ['输入字数', (cost.chars || 0).toLocaleString() + ' 字'],
      ['使用模型', cost.model_name || cost.model || '未命名模型'],
      ['预估 token', '约 ' + (cost.tokens || 0).toLocaleString()],
      ['预估费用', '<b style="color:#dc2626">' + esc(unit) + costTxt + '</b>']
    ].map(function (r) { return '<div class="cost-row">' + r[0] + '：<b>' + r[1] + '</b></div>'; }).join('');
    m.innerHTML = `<div class="g-modal" style="width:480px">
      <h3>${esc(opts.title || '成本确认')}</h3>
      <div class="g-msg">${costRows}<div style="color:#9ca3af;font-size:11px;padding-top:6px">* 本地估算，仅供参考，实际消耗以服务商官网收费为准</div></div>
      <div class="g-progress" id="g-progress"></div>
      <div class="g-actions"><button class="g-btn g-cancel" type="button">取消</button><button class="g-btn g-ok" type="button">${esc(opts.confirmText || '确定')}</button></div></div>`;
    m.style.display = 'flex';
    const progress = m.querySelector('#g-progress');
    const okBtn = m.querySelector('.g-ok');
    const cancelBtn = m.querySelector('.g-cancel');
    const setProgress = function (html, type) {
      progress.className = 'g-progress ' + (type || 'running');
      progress.innerHTML = html || '';
    };
    cancelBtn.onclick = function () { m.style.display = 'none'; if (opts.onCancel) { try { opts.onCancel(); } catch (e) {} } };
    okBtn.onclick = function () {
      okBtn.disabled = true;
      okBtn.textContent = '进行中…';
      const finish = function (summaryHtml, type) {
        okBtn.disabled = false;
        cancelBtn.disabled = false;
        okBtn.textContent = '完成';
        okBtn.onclick = function () { m.style.display = 'none'; if (opts.onDone) opts.onDone(); };
        if (summaryHtml) setProgress(summaryHtml, type || 'success');
      };
      const fail = function (e) {
        okBtn.disabled = false; cancelBtn.disabled = false;
        okBtn.textContent = opts.confirmText || '确定';
        setProgress('❌ ' + ((e && e.message) || String(e)), 'error');
      };
      try {
        Promise.resolve(opts.run(setProgress)).then(function (r) { finish(r && r.summary, (r && r.type) || 'success'); })
          .catch(fail);
      } catch (e) { fail(e); }
    };
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
