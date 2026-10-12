/* 全局左侧导航（v1.30~v1.41）：
   v1.39/v1.41 定稿——导航仅宽/窄二态切换（取消消失态/悬停展开/自动收起/锁概念）；
   默认态=宽；切换按钮=方向箭头（宽态 ← 点击收窄 / 窄态 → 点击展开，悬浮提示）；
   窄态=图标栏（仅图标，每个图标悬浮提示）；样式参考 Infinite Canvas 导航锁（36×36 圆角按钮）；
   状态持久化 localStorage ss_nav_mode（wide/narrow）；两态导航项 + 工作台项 + 历史创作内嵌 + 全局 toast */
(function () {
  const CSS = `
  #sidebar { background:var(--ss-bg); border-right:1px solid var(--ss-border); width:240px; flex:none; transition:width .18s; display:flex; flex-direction:column; overflow:hidden; position:sticky; top:0; height:100vh; }
  #sidebar.narrow { width:56px; }
  #sidebar.narrow .sb-section { padding:10px 4px; }
  #sidebar.narrow .nav-item { justify-content:center; padding:9px 0; }
  #sidebar.narrow .nav-item .txt, #sidebar.narrow .sb-section .sec-t, #sidebar.narrow .sb-top .logo, #sidebar.narrow .sb-history { display:none; }
  .sb-top { display:flex; align-items:center; gap:8px; height:56px; padding:0 10px; background:var(--ss-ink-bar); box-sizing:border-box; }
  .sb-top .logo { font-size:15px; font-weight:600; white-space:nowrap; overflow:hidden; flex:1; cursor:pointer; color:#fff; }
  /* v2 升级（⓪-2 步6 回炉，用户 14:12 定）：项目名**不再放顶部黑条**，
     改为黑条下一行单独显示（无当前项目则宽态不显示）；窄态=📁 图标 + hovertip（项目名 / 无当前项目） */
  .sb-proj { display:flex; align-items:center; gap:6px; padding:8px 12px 2px; font-size:12px; color:var(--ss-text-2); white-space:nowrap; overflow:hidden; cursor:pointer; }
  .sb-proj .ic { flex:none; }
  .sb-proj .nm { overflow:hidden; text-overflow:ellipsis; }
  .sb-proj.none { display:none; }
  #sidebar.narrow .sb-proj { display:flex; justify-content:center; padding:8px 0 2px; }
  #sidebar.narrow .sb-proj .nm { display:none; }
  #sidebar.narrow .sb-proj.none { display:flex; }
  /* 导航切换按钮（v1.41：方向箭头，参考 IC 导航锁样式——36×36 圆角、2px 边框、hover 光晕） */
  .nav-toggle { width:36px; height:36px; flex:none; border:2px solid var(--ss-border-strong); border-radius:12px; background:var(--ss-card); color:#334155; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; font-size:16px; line-height:1; transition:border-radius .3s, background-color .18s, box-shadow .2s, transform .3s; }
  .nav-toggle:hover { background:#f1f5f9; border-radius:50%; box-shadow:0 0 0 4px rgba(148,163,184,.16), 0 10px 24px rgba(15,23,42,.08); }
  .nav-toggle:active { transform:scale(.94); }
  .sb-section { padding:10px 8px; }
  .sb-section .sec-t { font-size:11px; color:var(--ss-text-4); padding:0 8px 6px; white-space:nowrap; }
  .nav-item { display:flex; align-items:center; gap:8px; padding:9px 10px; border-radius:8px; font-size:13px; color:var(--ss-text-2); cursor:pointer; white-space:nowrap; text-decoration:none; }
  .nav-item:hover { background:var(--ss-hover-bg); box-shadow:0 1px 2px rgba(0,0,0,.05); }
  .nav-item.active { background:var(--ss-green-100); color:var(--ss-green-600); font-weight:600; }
  .nav-item .ic { flex:none; width:20px; text-align:center; }
  .nav-item.placeholder { color:var(--ss-text-4); }
  .sb-history { padding:6px 8px; }
  .sb-history .h-item { padding:6px 10px; font-size:12px; color:var(--ss-text-3); border-radius:6px; cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sb-history .h-item:hover { background:var(--ss-hover-bg); }
  .sb-history .h-item .t { color:var(--ss-text-4); font-size:10px; }

  /* ⓪-VI-二期 步2：主题切换按钮
     注：本段颜色为字面量（JS 模板串内，步1 的 var() 化不覆盖此类上下文）
         → 属 vi-tokens-dark-patch.css 注意 1 所列「JS 内硬编码色」，归步3 统一收敛。 */
  .theme-toggle { display:flex; align-items:center; justify-content:center; gap:6px; width:100%;
    margin-bottom:6px; padding:5px 10px; border:1px solid #cbd5e1; border-radius:999px;
    background:var(--ss-card); color:#6b7280; font-size:11px; cursor:pointer; }
  .theme-toggle:hover { border-color:#94a3b8; color:#374151; }
  #sidebar.narrow .theme-toggle { font-size:10px; padding:4px 2px; }
  #sidebar.narrow .theme-toggle .tt-txt { display:none; }
  [data-theme="dark"] .theme-toggle { background:#222826; color:#B4C4B9; border-color:#46504b; }
  [data-theme="dark"] .theme-toggle:hover { border-color:#7FAE9B; color:#E9EFE9; }

  /* v2 升级（⓪-2 步1）：队列项状态角标 + 底部版本胶囊 */
  .nav-item .ql-badge { flex:none; margin-left:auto; min-width:18px; height:18px; padding:0 5px; border-radius:9px; font-size:11px; line-height:18px; text-align:center; color:#fff; background:var(--ss-text-4); }
  .nav-item .ql-badge.q-idle { background:var(--ss-text-4); }
  .nav-item .ql-badge.q-run { background:var(--ss-running); }
  .nav-item .ql-badge.q-queue { background:var(--ss-green-600); }
  .nav-item .ql-badge.q-fail { background:var(--ss-error); }
  /* ② F5a：任务入队时「队列 + 数字」整行提示（用户 09-21 反馈「太不明显」→ 强化版）
     老版只闪 opacity（肉眼几乎看不出）；改为 **整行绿底脉冲 + 左侧色条 + 角标放大 1.5 倍**，
     总时长 ≈ 2s（.65s × 3）。overflow:visible 防止放大后的角标被父级裁掉。 */
  .nav-item.q-flash { animation:qflash .65s ease-in-out 3; overflow:visible; position:relative; }
  .nav-item.q-flash .ql-badge { animation:qpop .65s ease-in-out 3; transform-origin:center; z-index:3; }
  @keyframes qflash {
    0%   { background:transparent; box-shadow:inset 0 0 0 0 var(--ss-green-600); }
    18%  { background:rgba(58,107,87,.28); box-shadow:inset 4px 0 0 0 var(--ss-green-600); }
    45%  { background:rgba(58,107,87,.10); box-shadow:inset 4px 0 0 0 var(--ss-green-600); }
    72%  { background:rgba(58,107,87,.24); box-shadow:inset 4px 0 0 0 var(--ss-green-600); }
    100% { background:transparent; box-shadow:inset 0 0 0 0 var(--ss-green-600); }
  }
  @keyframes qpop {
    0%,100% { transform:scale(1); }
    18%     { transform:scale(1.5); box-shadow:0 0 0 3px rgba(58,107,87,.35); }
    45%     { transform:scale(1.12); box-shadow:0 0 0 0 rgba(58,107,87,0); }
    72%     { transform:scale(1.45); box-shadow:0 0 0 3px rgba(58,107,87,.35); }
  }
  [data-theme="dark"] .nav-item.q-flash { animation-name:qflash-d; }
  @keyframes qflash-d {
    0%   { background:transparent; box-shadow:inset 0 0 0 0 #7FAE9B; }
    18%  { background:rgba(127,174,155,.30); box-shadow:inset 4px 0 0 0 #7FAE9B; }
    45%  { background:rgba(127,174,155,.12); box-shadow:inset 4px 0 0 0 #7FAE9B; }
    72%  { background:rgba(127,174,155,.26); box-shadow:inset 4px 0 0 0 #7FAE9B; }
    100% { background:transparent; box-shadow:inset 0 0 0 0 #7FAE9B; }
  }
  .sb-foot { margin-top:auto; padding:10px 12px 14px; }
  .ver-pill { display:block; width:100%; text-align:center; padding:5px 10px; border:1px solid var(--ss-border-strong); border-radius:999px; background:var(--ss-card); color:var(--ss-text-3); font-size:11px; cursor:pointer; }
  .ver-pill:hover { border-color:#94a3b8; color:var(--ss-text-2); }
  #sidebar.narrow .sb-foot { padding:8px 6px 12px; }
  #sidebar.narrow .ver-pill { font-size:10px; padding:4px 2px; }
  .sb-top .logo-mark{display:none}
  #sidebar.narrow .sb-top .logo-mark{display:block!important;margin:0 auto}
  #toast { position:fixed; top:14px; right:16px; z-index:300; display:none; padding:10px 16px; border-radius:8px; font-size:13px; box-shadow:0 4px 14px rgba(0,0,0,.18); max-width:340px; }
  /* 全局自定义模态对话框（v1.42：弃用原生 confirm/alert/prompt——顶部白底黑字对话框，统一页面内模态） */
  #g-modal-mask { position:fixed; inset:0; background:rgba(17,24,39,.4); z-index:700; display:none; align-items:center; justify-content:center; }
  #g-modal-mask .g-modal { background:var(--ss-card); border-radius:12px; width:420px; max-width:92vw; padding:18px 20px; box-shadow:0 10px 30px rgba(0,0,0,.2); }
  #g-modal-mask .g-modal h3 { font-size:15px; color:var(--ss-text-1); margin:0 0 10px; }
  #g-modal-mask .g-modal .g-msg { font-size:13px; color:#4b5563; line-height:1.8; white-space:pre-wrap; word-break:break-word; }
  #g-modal-mask .g-modal input[type=text] { width:100%; box-sizing:border-box; margin-top:10px; padding:8px 10px; border:1px solid var(--ss-border-strong); border-radius:8px; font-size:13px; }
  #g-modal-mask .g-modal .g-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
  #g-modal-mask .g-modal .g-btn { padding:8px 16px; border:none; border-radius:8px; cursor:pointer; font-size:13px; }
  #g-modal-mask .g-modal .g-btn.g-cancel { background:var(--ss-bg); color:var(--ss-text-2); border:1px solid var(--ss-border); }
  #g-modal-mask .g-modal .g-btn.g-ok { background:var(--ss-green-600); color:#fff; }
  #g-modal-mask .g-modal .g-btn.g-ok:hover { background:var(--ss-green-700); }
  /* v1.43：全站按钮 disabled 视觉置灰（统一，不依赖各页自带样式；成本确认类弹窗进度/汇总样式） */
  button:disabled { opacity:.55; cursor:not-allowed; }
  .btn-primary:disabled, .g-btn.g-ok:disabled { background:var(--ss-text-4) !important; color:#fff !important; opacity:.85; }
  #g-modal-mask .g-modal .cost-row { font-size:13px; color:#4b5563; line-height:1.9; }
  #g-modal-mask .g-modal .g-progress { display:none; margin-top:10px; font-size:13px; border-radius:8px; padding:10px 12px; line-height:1.7; word-break:break-word; height:240px; overflow-y:auto; }  /* v1.51：流式内容固定高度+内部滚动，弹窗高度恒定防抖动 */
  #g-modal-mask .g-modal .g-progress.running { display:block; color:var(--ss-running); background:var(--ss-running-bg); border:1px solid var(--ss-running-bg); }
  #g-modal-mask .g-modal .g-progress.success { display:block; color:var(--ss-success); background:var(--ss-success-bg); border:1px solid var(--ss-success-bg); }
  #g-modal-mask .g-modal .g-progress.error { display:block; color:var(--ss-error); background:var(--ss-error-bg); border:1px solid var(--ss-error-bg); }
  #g-modal-mask .g-modal .g-progress.plain { display:block; color:var(--ss-text-2); background:var(--ss-card); border:1px solid var(--ss-border); }
  /* v1.45 资产信息页布局（多版本平铺/分区卡片/集多选/chips 固定高度；nav.js 注入并带 !important 防 HTML 缓存时样式不更新） */
  .meta-grid { grid-template-columns: 1fr 1.45fr !important; gap: 18px !important; }
  .meta-left { position: relative; display: flex; flex-direction: column; }   /* v1.45 复检：上传素材绝对定位左栏左下角 */
  .mp-versions { flex: 1; min-height: 120px; display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; max-height: 400px; overflow-y: auto; padding: 2px 2px 48px; }
  .upload-drop { position: absolute; bottom: 0; left: 0; right: 0; background: var(--ss-card); z-index: 2; }
  .mp-ver-card { position: relative; aspect-ratio: 3 / 4; border-radius: 10px; overflow: hidden; border: 2px solid var(--ss-border); cursor: pointer; background: var(--ss-bg); }
  .mp-ver-card.cur { border-color: var(--ss-green-600); }
  .mp-ver-card img { width: 100%; height: 100%; object-fit: cover; }
  .mp-ver-card .ph-card { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; width: 100%; height: 100%; color: var(--ss-text-4); font-size: 12px; border: 2px dashed var(--ss-border-strong); border-radius: 8px; background: var(--ss-bg); }
  .mp-ver-card .ph-ic { font-size: 24px; }
  .mp-ver-card .ph-sub { font-size: 10px; color: #b0b7c3; }
  .mp-ver-badge { position: absolute; top: 6px; left: 6px; background: var(--ss-green-600); color: #fff; font-size: 10px; border-radius: 6px; padding: 1px 6px; }
  .mp-ver-del { position: absolute; top: 4px; right: 4px; background: rgba(17,24,39,.7); color: #fff; border: none; border-radius: 50%; width: 20px; height: 20px; font-size: 12px; line-height: 20px; cursor: pointer; display: none; }
  .mp-ver-card:hover .mp-ver-del { display: flex; align-items: center; justify-content: center; }
  .mp-ver-zoom { position: absolute; bottom: 4px; right: 4px; background: rgba(17,24,39,.7); color: #fff; border: none; border-radius: 6px; width: 20px; height: 20px; font-size: 12px; line-height: 20px; cursor: pointer; display: none; align-items: center; justify-content: center; text-decoration: none; }
  .mp-ver-card:hover .mp-ver-zoom { display: flex; }
  .mp-card { background: var(--ss-card); border: 1px solid var(--ss-border); border-radius: 10px; padding: 10px 12px; margin-bottom: 12px; max-height: 300px; overflow-y: auto; }
  .mp-card-t { font-size: 13px; font-weight: 600; color: var(--ss-text-2); margin-bottom: 8px; }
  .ep-multi { max-height: 160px; overflow-y: auto; border: 1px solid var(--ss-bg); border-radius: 8px; padding: 6px 8px; margin-top: 6px; background: var(--ss-bg); font-size: 13px; }
  .ep-multi label { display: flex; align-items: center; gap: 6px; padding: 3px 0; cursor: pointer; }
  .scope-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; max-height: 66px !important; overflow-y: auto; }
  /* v1.50：归属明细 chips 紧凑横排（每已归属项目组一行、集标签横向滚动） */
  .scope-groups { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
  .scope-group { display: flex; align-items: center; gap: 6px; background: var(--ss-bg); border: 1px solid var(--ss-border); border-radius: 8px; padding: 4px 8px; max-width: 100%; }
  .scope-group-head { flex: none; font-size: 12px; font-weight: 600; color: var(--ss-text-2); white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; }
  .scope-group-del { border: none; background: none; cursor: pointer; color: var(--ss-text-4); font-size: 13px; padding: 0 2px; }
  .scope-group-del:hover { color: var(--ss-error); }
  .scope-group-eps { display: inline-flex; gap: 6px; overflow-x: auto; white-space: nowrap; padding-bottom: 2px; }
  `;


  // ── ⓪-VI-二期 步2：主题切换（localStorage ss_theme: light|dark|system）──
  var SS_THEME_KEY = 'ss_theme';
  function ssSystemDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function ssApplyTheme(mode) {
    var dark = mode === 'dark' || (mode === 'system' && ssSystemDark());
    document.documentElement.dataset.theme = dark ? 'dark' : '';
    var b = document.getElementById('theme-toggle');
    if (b) {
      var ic = b.querySelector('.tt-ic'), tx = b.querySelector('.tt-txt');
      if (ic) ic.textContent = dark ? '☀' : '◐';
      if (tx) tx.textContent = dark ? '浅色' : '深色';
      // 文案 = 「点一下会去哪」，两态下必然准确
      b.title = (dark ? '切换为浅色' : '切换为深色') + '　（Shift+点击：跟随系统）';
    }
  }
  function ssInitTheme() {
    var b = document.getElementById('theme-toggle');
    if (!b) return;
    var mode = localStorage.getItem(SS_THEME_KEY) || 'system';
    ssApplyTheme(mode);
    /* ── 两态开关：浅 ⇄ 深（2026-09-20 用户裁定）──
       为什么改成两态：原来是「跟随系统→浅色→深色」三档轮转，但**三档只有两种外观**
       （跟随系统在系统为浅色时看起来就是浅色）→ 任何完整轮转**必然有一步画面不变**，
       实测「默认态起步第 1 击无变化」（按钮写着『深色』点了却不变）→ 与「一键切换」相悖。
       现改为：点击在 浅/深 之间直切 → **每次点击画面必变**。
       首次点击前仍读系统偏好定初值；「跟随系统」保留为 Shift+点击（清掉存储值即回默认）。 */
    b.onclick = function (e) {
      if (e && e.shiftKey) {
        try { localStorage.removeItem(SS_THEME_KEY); } catch (err) { }
        ssApplyTheme('system');
        return;
      }
      var curDark = document.documentElement.dataset.theme === 'dark';
      var next = curDark ? 'light' : 'dark';
      try { localStorage.setItem(SS_THEME_KEY, next); } catch (err) { }
      ssApplyTheme(next);
    };
    // 跟随系统时，系统主题变化要实时响应（仅在「跟随系统」档位生效）
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
        if ((localStorage.getItem(SS_THEME_KEY) || 'system') === 'system') ssApplyTheme('system');
      });
    }
  }

  function activeKey() {
    const p = location.pathname;
    if (p === '/' || p.endsWith('/index.html')) return 'home';
    if (p.includes('script')) return 'script';
    if (p.includes('scene')) return 'scene';
    if (p.includes('assets')) return 'assets';
    if (p.includes('gsplat')) return 'gsplat';
    if (p.includes('gentools')) return 'gentools';
    if (p.includes('motion')) return 'motion';
    if (p.includes('tasks')) return 'tasks';
    if (p.includes('trash')) return 'trash';
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
    /* v2 升级（⓪-2 步1 回炉）：场页在"无当前项目"时也要有反馈——
       原写法（无 pid 不绑 onclick）会让菜单项变成死链，点了毫无反应（用户目检 #6 提出） */
    const sceneClick = `onclick="event.preventDefault();window.__gotoScene('${pid || ''}')"`;
    /* v2 升级（⓪-2 步1）：队列项（状态角标 4 态 + 常驻标识）+ 动作迁移项
       回炉（步5 目检，用户 13:50）：队列方向要有**明显标识** —— 角标常驻显示（空闲=灰 0），
       有任务才变色带数字（紫=生成中 / 蓝=排队 / 红=有失败）。 */
    const qItem = () => `<a class="nav-item ${a === 'tasks' ? 'active' : ''}" href="/tasks.html" title="任务队列（排队与进度）"><span class="ic">📋</span><span class="txt">队列</span><span class="ql-badge q-idle">0</span></a>`;
    return `
    <div class="sb-top">
      <span class="logo" onclick="location.href='/'"><img src="/static/brand/lockup-darkbar.svg" alt="小麻雀脚本与分镜工坊" style="height:34px;display:block"/></span><img class="logo-mark" src="/static/brand/favicon.svg" alt="" style="height:28px;display:none"/>
      <button class="nav-toggle" id="nav-toggle" onclick="window.__navToggle()" title="切换为窄栏"><span id="nav-ic">←</span></button>
    </div>
    <!-- v2 升级（⓪-2 步6 回炉）：项目名单独一行（黑条之下）；无当前项目宽态不显示；窄态=图标+hovertip -->
    <div class="sb-proj ${pid ? '' : 'none'}" id="sb-proj" title="${pid ? '当前项目' : '无当前项目'}" onclick="location.href='/'">
      <span class="ic">📁</span><span class="nm" id="sb-proj-nm"></span>
    </div>
    <div class="sb-section">
      ${go('home', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11 L12 4 L21 11"/><path d="M5 10 V20 H19 V10"/><path d="M10 20 V14 H14 V20"/></svg>', '工作台', workHref)}
      ${go('script', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 3 H18 a1 1 0 0 1 1 1 V21 H8 a2 2 0 0 1 -2 -2 V5"/><path d="M9 8 H15 M9 12 H15"/></svg>', '剧本大纲', '/script.html' + (pid ? '?project=' + pid + '&view=outline' : ''))}
      ${item('scene', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5 V19 M17 5 V19 M10 9 H14 M10 15 H14"/></svg>', '场页', pid ? '#' : 'javascript:;', '', sceneClick)}
      ${go('assets', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7 a2 2 0 0 1 2 -2 H9 L11 7 H19 a2 2 0 0 1 2 2 V18 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 Z"/></svg>', '资产库', '/assets.html')}
      ${go('motion', '🎬', '动作迁移', '/motion.html')}
      ${go('gsplat', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3 L21 8 V16 L12 21 L3 16 V8 Z"/><path d="M3 8 L12 13 L21 8 M12 13 V21"/></svg>', '高斯泼溅', '/gsplat.html')}
      ${go('gentools', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3 a9 9 0 1 0 9 9 c0-2-1.5-3-3-3 h-2 a2 2 0 0 1 -2 -2 c0-1.5-1-3-3-3"/><circle cx="7.5" cy="10.5" r="1"/><circle cx="12" cy="7.5" r="1"/><circle cx="16.5" cy="10.5" r="1"/></svg>', '生图工具', '/gentools.html')}
      ${qItem()}
      ${go('trash', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7 H20"/><path d="M9 7 V5 a1 1 0 0 1 1 -1 h4 a1 1 0 0 1 1 1 v2"/><path d="M6 7 l1 13 a1 1 0 0 0 1 1 h8 a1 1 0 0 0 1 -1 l1 -13"/><path d="M10 11 V17 M14 11 V17"/></svg>', '回收站', '/trash.html')}
      ${go('settings', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2 V5 M12 19 V22 M2 12 H5 M19 12 H22 M4.9 4.9 L7 7 M17 17 L19.1 19.1 M19.1 4.9 L17 7 M7 17 L4.9 19.1"/></svg>', '设置', '/settings.html')}
    </div>
    <div class="sb-section">
      <div class="sec-t">历史创作</div>
      <div class="sb-history" id="history-list"><div class="h-item">加载中…</div></div>
    </div>
    <div class="sb-foot">
      <button class="theme-toggle" id="theme-toggle" title="切换主题">
        <span class="tt-ic">◐</span><span class="tt-txt">主题</span>
      </button>
      <button class="ver-pill" id="ver-pill" title="当前版本">v—</button>
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

    // ② F5a：导航渲染完成后启动队列轮询（4s；全站唯一源，页面通过 __onQueuePoll 订阅）
    window.__startQueuePoll(4000);

    // 当前项目名（v2 ⓪-2 步6 回炉：黑条下一行 sb-proj 显示；窄态=图标 + hovertip）
    const pid = currentPid();
    const projBox = document.getElementById('sb-proj');
    if (pid && projBox) {
      fetch('/api/projects/' + pid).then(r => r.json()).then(p => {
        const nm = document.getElementById('sb-proj-nm');
        if (nm && p.name) nm.textContent = p.name;
        if (p.name) projBox.title = '当前项目：' + p.name;
      }).catch(() => {});
    } else if (projBox) {
      projBox.title = '无当前项目';
    }
    syncHistory();
    initVersionPill();
    ssInitTheme();
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

  // ── v2 升级（⓪-2 步1）：导航底部版本胶囊（读本地 VERSION；在线检测后续批接入）──
  function initVersionPill() {
    var el = document.getElementById('ver-pill'); if (!el) return;
    fetch('/api/version').then(function (r) { return r.json(); }).then(function (j) {
      var v = (j && j.version) || 'dev';
      el.textContent = 'v' + v;
      el.title = '当前版本：v' + v + ' · 点击检测更新';
    }).catch(function () {
      el.textContent = 'v—';
      el.title = '当前版本获取失败 · 点击检测更新';
    });
  }
  window.__checkUpdate = function () {
    var el = document.getElementById('ver-pill');
    var v = el ? el.textContent.replace(/^v/, '') : '';
    window.toast('当前版本 v' + v + '，已是最新版本（在线检测后续接入）', 'info');
  };

  // ── v2 升级（⓪-2 步1）：队列角标 4 态接口（空闲=灰 0 常驻 / 排队蓝 / 生成中紫 / 有失败红）──
  //    ② F5a（2026-09-21）：**已接真数据** —— 计数口径 = 活跃任务数（运行中 + 排队中），
  //    入队即 +1、开跑不变、完成/取消即 -1；无活跃任务时若有失败则红色显示失败数，否则灰 0。
  window.__setQueueBadge = function (state, n) {
    document.querySelectorAll('.ql-badge').forEach(function (b) {
      b.className = 'ql-badge';
      var cnt = parseInt(n || 0, 10);
      if (state === 'running') { b.classList.add('q-run'); b.textContent = cnt || '●'; }
      else if (state === 'queued') { b.classList.add('q-queue'); b.textContent = cnt || '●'; }
      else if (state === 'failed') { b.classList.add('q-fail'); b.textContent = cnt || '!'; }
      else { b.classList.add('q-idle'); b.textContent = '0'; }
    });
  };

  /* ② F5a：整行闪动（入队时调用；2s 后自动移除 class） */
  var _qFlashTimer = null;
  window.__flashQueue = function () {
    document.querySelectorAll('.nav-item').forEach(function (el) {
      if (!el.querySelector('.ql-badge')) return;
      el.classList.remove('q-flash');
      void el.offsetWidth;              /* 强制重排：连点也能重新触发动画 */
      el.classList.add('q-flash');
    });
    if (_qFlashTimer) clearTimeout(_qFlashTimer);
    _qFlashTimer = setTimeout(function () {
      document.querySelectorAll('.nav-item.q-flash').forEach(function (el) { el.classList.remove('q-flash'); });
    }, 2000);
  };

  /* ② F5a：任务数据广播订阅（页面注册回调，nav 轮询到新数据就通知它们） */
  window.__onQueuePoll = function (fn) {
    (window.__qSubs = window.__qSubs || []).push(fn);
    if (window.__qLast) { try { fn(window.__qLast); } catch (e) {} }   /* 立刻补发上一帧 */
  };

  /* ② F5a：拉 /api/tasks → 更新角标 → 广播给页面（全站唯一轮询源，页面不再各自轮询） */
  var _qPolling = false;
  window.__refreshQueue = function () {
    if (_qPolling) return Promise.resolve(window.__qLast || null);
    _qPolling = true;
    return fetch('/api/tasks', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var c = (j && j.counts) || {};
        var running = c.running || 0, queued = c.queued || 0, failed = c.failed || 0;
        var active = running + queued;
        var st = active ? (running ? 'running' : 'queued') : (failed ? 'failed' : '');
        window.__setQueueBadge(st, active || failed);
        window.__qLast = j;
        (window.__qSubs || []).forEach(function (fn) { try { fn(j); } catch (e) {} });
        return j;
      })
      .catch(function () { return null; })
      .then(function (r) { _qPolling = false; return r; });
  };

  window.__startQueuePoll = function (ms) {
    if (window.__qTimer) return;
    window.__refreshQueue();
    window.__qTimer = setInterval(function () { window.__refreshQueue(); }, ms || 4000);
  };

  // 全局 toast（v1.30：右上角 fixed）
  window.toast = function (text, type) {
    let el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
    el.textContent = text;
    el.style.background = type === 'success' ? 'var(--ss-success-bg)' : type === 'error' ? 'var(--ss-error-bg)' : '#111827';
    el.style.color = type === 'success' ? '#16a34a' : type === 'error' ? 'var(--ss-error)' : '#fff';
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
  // G2-1 裁定备案(09-11 规划-9, B-20260911-02)：confirmDialog.message 是 **HTML 通道**
  // （调用方有意传 '<div style=...>' 等 HTML 片段渲染富文本，故 message **不做统一 esc()**；promptDialog 各参数已 esc）。
  // 约定：**调用方须对用户可控内容自行 esc()**（示例：scene.html 删除引用已对 data-ref/delLabel esc）。
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
      ['预估费用', '<b style="color:var(--ss-error)">' + esc(unit) + costTxt + '</b>']
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
