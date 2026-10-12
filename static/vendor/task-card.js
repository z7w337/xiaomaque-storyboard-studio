/* 统一任务卡组件（TaskCard）—— v2 升级⓪-2：队列页与后续各处共用
   做法对齐 static/vendor/asset-card.js（自注入样式 + 全局构造函数），单源、可复用。

   用法：
     TaskCard.inject();                                  // 页面引一次即可（幂等）
     var el = TaskCard.render(task, { onOp: function(op, task){} });
     // task: { kind:'video'|'image'|'audio', title, state:'queued'|'running'|'done'|'fail',
     //         meta, pct, order }
     // 拖拽重排由页面负责（本组件只加 draggable 与拖拽提示样式）

   状态：左上角**图标**（不写文字），颜色+符号区分四态；
        状态文字放在卡片底部小字里（不丢字，只是不再占据左上角）。
*/
(function () {
  var INJECTED = false;

  var CSS = `
  /* ===== 任务卡（图像区满铺 + 下部文字带）=====
     ★ 2026-09-20 版式调整（用户「方案B」+ 六条指示）：
        · 图像区定 1:1 满铺 —— 与素材 1:1 同比 → object-fit:cover **零裁切**
        · 文字移到卡内下部「文字带」：不再压在图上 → **不需要遮罩**
        · 进度条移进文字带 → 不再与文字抢位置
        · 去掉 ⋮⋮ 手柄（整卡可拖；「运行中不可拖」的旧规则保留，见 render()）
     没给 task.thumb 时，原来的「色块 + 图标 + 类型字」海报同样铺满此区
     （用户 2026-09-18 反馈："大图标换成图（纯色图+文字区分也可以），撑满整个缩略区"） */
  .tkc { position: relative; border: 1px solid var(--ss-border); border-radius: 10px; background: var(--ss-card); overflow: hidden; display: flex; flex-direction: column; cursor: grab; transition: box-shadow .18s, border-color .18s, opacity .18s, transform .18s; }
  .tkc:hover { box-shadow: 0 3px 12px rgba(15,23,42,.09); border-color: var(--ss-border-strong); }
  .tkc:active { cursor: grabbing; }
  .tkc.dragging { opacity: .4; }
  .tkc.drop-hint { border-color: var(--ss-green-600); border-style: dashed; }

  /* 图像区：1:1 满铺 */
  .tkc-thumb { position: relative; width: 100%; aspect-ratio: 1 / 1; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; }
  .tkc-video .tkc-thumb { background: #4f46e5; }
  .tkc-image .tkc-thumb { background: #047857; }
  .tkc-audio .tkc-thumb { background: #be185d; }
  .tkc-thumb .ic { font-size: 34px; line-height: 1; filter: drop-shadow(0 1px 2px rgba(0,0,0,.18)); }
  .tkc-thumb .ph { font-size: 13px; color: #fff; opacity: .95; letter-spacing: 3px; }
  /* 有真图/真视频时满铺（视频由 hover 播放，见 render() 末尾的绑定） */
  .tkc-thumb.has-img { padding: 0; }
  .tkc-thumb.has-img img, .tkc-thumb.has-img video { width: 100%; height: 100%; object-fit: cover; display: block; }
  /* 状态插画：浅/深两套图同时存在，由 [data-theme] 决定显示哪一张（免 JS 判主题）
     ⚠ 选择器必须带上 .has-img：上面那条 .tkc-thumb.has-img img 的 display:block 特异性 (0,2,1)
       会压过 .tkc-thumb .ill-d 的 display:none (0,2,0) → 浅色下两张都显示、后一张盖住前一张
     ⚠ 本段在 JS 模板串里，注释中**不可出现反引号**（会提前结束模板串） */
  .tkc-thumb.has-img img.ill { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .tkc-thumb.has-img img.ill-d { display: none; }
  [data-theme="dark"] .tkc-thumb.has-img img.ill-l { display: none; }
  [data-theme="dark"] .tkc-thumb.has-img img.ill-d { display: block; }
  /* hover 时动态插入的动图 */
  .tkc-thumb .tkc-media { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }

  /* 类型角标：图像区一旦被真图/插画盖满，原来靠「类型色底」传达的类型信号就丢了
     （色块海报自带类型字，故只在 .has-img 时补角标，不重复）
     · 位置取图像区**左下** —— 右下留给素材自身的水印，四角留给 hover 才出现的操作按钮
       ★ 用户 2026-09-20 裁决「水印保留」→ 左下就是**必需**的：角标若在右下会与
         「豆包AI生成」压在同一块区域互相压字。现为 左下角标 + 右下水印 互不干扰。
     · 用单色字形而非 emoji：emoji 是彩色的、无法随主题换色，深色下会糊成一块
     · 双主题各给一套底/字色，保证两侧都看得清 */
  .tkc-thumb.has-img::after { position: absolute; left: 6px; bottom: 6px; min-width: 20px; height: 20px; padding: 0 5px; box-sizing: border-box; border-radius: 6px; background: rgba(255,255,255,.86); border: 1px solid rgba(15,23,42,.12); color: #374151; font-size: 11px; font-weight: 600; line-height: 18px; text-align: center; z-index: 3; pointer-events: none; }
  [data-theme="dark"] .tkc-thumb.has-img::after { background: rgba(17,24,39,.72); border-color: rgba(255,255,255,.18); color: #E5E7EB; }
  .tkc-video .tkc-thumb.has-img::after { content: '▶'; }
  .tkc-image .tkc-thumb.has-img::after { content: '▣'; }
  .tkc-audio .tkc-thumb.has-img::after { content: '♪'; }

  /* 文字带：卡内下部（不叠加在图上，因此不需要遮罩） */
  .tkc-body { padding: 8px 10px 10px; display: flex; flex-direction: column; }
  .tkc-name { font-size: 12px; color: var(--ss-text-1); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .tkc-meta { font-size: 11px; color: var(--ss-text-4); margin-top: 3px; line-height: 1.45; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tkc-prog { height: 5px; border-radius: 3px; background: var(--ss-bg); overflow: hidden; margin-top: 6px; }
  .tkc-prog > i { display: block; height: 100%; background: var(--ss-green-600); border-radius: 3px; }

  /* 左上角：状态图标（取代状态文字）；加白描边，压在彩色缩略区上也看得清 */
  .tkc-st { position: absolute; left: 7px; top: 7px; width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; line-height: 1; color: #fff; box-shadow: 0 0 0 2px var(--ss-card), 0 1px 3px rgba(15,23,42,.25); z-index: 2; }
  .tkc-st.s-queued { background: var(--ss-green-600); }
  .tkc-st.s-running { background: var(--ss-running); }
  .tkc-st.s-running .sp { display: inline-block; animation: tkc-spin 1.4s linear infinite; }
  .tkc-st.s-paused { background: var(--ss-warning); }   /* 暂停（⏸ 之后的状态）—— 用户 2026-09-20 确认此态存在 */
  .tkc-st.s-done { background: var(--ss-success); }
  .tkc-st.s-fail { background: var(--ss-error); }
  .tkc-st.s-cancelled { background: var(--ss-text-4); }   /* 已取消：中性灰（③ 留痕）*/
  @keyframes tkc-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }

  /* 另三角：图标化操作（hover 显示） */
  .tkc-op { position: absolute; width: 22px; height: 22px; border-radius: 6px; border: none; background: rgba(17,24,39,.62); color: #fff; font-size: 12px; line-height: 1; padding: 0; display: none; align-items: center; justify-content: center; cursor: pointer; }
  .tkc:hover .tkc-op { display: flex; }
  .tkc-op:hover { background: rgba(17,24,39,.88); }
  .tkc-op.del { top: 7px; right: 7px; }
  .tkc-op.retry { bottom: 7px; left: 7px; }
  .tkc-op.cancel { bottom: 7px; right: 7px; }
  `;

  var KIND_ICON = { video: '🎬', image: '🖼', audio: '🎙' };
  var KIND_TEXT = { video: '视 频', image: '图 片', audio: '音 色' };
  /* 状态插画（无真缩略图时按 task.state 自动选图；浅/深两套由 CSS 按主题切）
     ★ 只认队列里会出现的 4 个状态：running/queued/paused/fail
       （「已完成」不在队列里 —— 任务完成即离队；未知状态走色块海报兜底） */
  var ILL_BASE = '/static/brand/task-states/';
  var ILL_KEY = { running: 'run', queued: 'queue', paused: 'pause', fail: 'fail' };
  var ST = {
    queued:  { icon: '⏱', name: '排队中' },
    running: { icon: '<span class="sp">◐</span>', name: '生成中' },
    paused:  { icon: '⏸', name: '暂停' },
    done:    { icon: '✓', name: '已完成' },
    fail:    { icon: '!', name: '失败' },
    /* ★ ③ 步4-③ 取消留痕：取消的任务**保留在列表里**（不再物理删除）→ 卡片需要这个状态。
       不进 ILL_KEY → 缩略区走"色块海报"兜底（不复用 fail 插画，避免看成失败）。*/
    cancelled: { icon: '⊘', name: '已取消' }
  };

  function inject() {
    if (INJECTED || document.getElementById('tkc-style')) { INJECTED = true; return; }
    var s = document.createElement('style');
    s.id = 'tkc-style';
    s.textContent = CSS;
    document.head.appendChild(s);
    INJECTED = true;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* 渲染一张任务卡，返回 DOM 元素 */
  function render(task, opts) {
    inject();
    opts = opts || {};
    var st = ST[task.state] || ST.queued;
    var el = document.createElement('div');
    el.className = 'tkc tkc-' + (task.kind || 'video');
    /* 用户 2026-09-18 14:09：**运行中的卡片不能拖动**（已在执行，顺序不可改）；其余可拖。
       ⚠️ draggable 同时设属性+property（兼容性双保险） */
    var canDrag = (task.state !== 'running');
    el.setAttribute('draggable', canDrag ? 'true' : 'false');
    el.draggable = canDrag;
    if (!canDrag) el.style.cursor = 'default';
    if (task.order != null) el.setAttribute('data-i', String(task.order));

    var ops = '';
    /* ② F5a（用户 09-21 口径）：队列里**没有「暂停」** —— 只有「取消 = 移出队列」
       （取消 queued = 永不进入生成；取消 running = 打断并丢弃，均不落库）。
       删除 ✕ 给失败卡与已取消卡（清理历史），「重试」= 同参重新入队到队尾。 */
    if (task.state === 'fail' || task.state === 'cancelled')
      ops += '<button class="tkc-op del" title="删除记录" data-op="删除">✕</button>';
    if (task.state === 'fail' || task.state === 'done') ops += '<button class="tkc-op retry" title="重试" data-op="重试">↻</button>';
    if (task.state === 'running' || task.state === 'queued') ops += '<button class="tkc-op cancel" title="取消（移出队列）" data-op="取消">⊘</button>';

    /* 图像区内容优先级：真视频 > 真图 > 状态插画（浅/深自动切）> 色块海报 */
    var isVid = task.thumb && /\.(mp4|webm)(\?|$)/i.test(task.thumb);
    var ill = ILL_KEY[task.state];
    var thumb;
    if (task.thumb) {
      thumb = isVid
        ? '<div class="tkc-thumb has-img"><video class="tkc-media" muted loop playsinline preload="metadata" src="' + esc(task.thumb) + '"></video></div>'
        : '<div class="tkc-thumb has-img"><img src="' + esc(task.thumb) + '" alt=""></div>';
    } else if (ill) {
      thumb = '<div class="tkc-thumb has-img" data-ill="' + ill + '">' +
                '<img class="ill ill-l" src="' + ILL_BASE + 'light_' + ill + '.webp" alt="" draggable="false">' +
                '<img class="ill ill-d" src="' + ILL_BASE + 'dark_' + ill + '.webp" alt="" draggable="false">' +
              '</div>';
    } else {
      thumb = '<div class="tkc-thumb"><span class="ic">' + (KIND_ICON[task.kind] || '📄') + '</span>' +
              '<span class="ph">' + (KIND_TEXT[task.kind] || '任务') + '</span></div>';
    }

    /* 文字带第二行 = 状态名 + 附加信息。附加信息**已以状态名开头**时不再重复拼
       （后端 progress 形如「生成中（Lightning 4 步…）」，直接拼会变成「生成中 · 生成中（…）」）。 */
    var metaTxt = String(task.meta == null ? '' : task.meta);
    var metaLine = (metaTxt.indexOf(st.name) === 0)
      ? metaTxt
      : (st.name + (metaTxt ? ' · ' + metaTxt : ''));

    el.innerHTML =
      '<span class="tkc-st s-' + (task.state || 'queued') + '" title="' + st.name + '">' + st.icon + '</span>' + ops +
      thumb +
      '<div class="tkc-body">' +
        '<div class="tkc-name">' + esc(task.title) + '</div>' +
        '<div class="tkc-meta">' + esc(metaLine) + '</div>' +
        ((task.state === 'running') ? '<div class="tkc-prog"><i style="width:' + (task.pct || 0) + '%"></i></div>' : '') +
      '</div>';

    /* 方案B（用户 2026-09-20 定）：静态图 → Hover 播动图。
       动图按**当前主题**取 浅/深 两套；动态创建 + 离场销毁
       → 同一时刻最多 1 个在播、且只加载被 hover 的那 1 个。
       注：触屏设备没有 hover → 动图不会播（已知限制，未擅自加 autoplay）。 */
    var v = el.querySelector('video.tkc-media');
    if (v) {
      /* ⚠ 属性 + property 双保险：muted 属性在部分浏览器/动态插入场景不生效，
         只写属性会导致「静音失败 → 出声」。用户 2026-09-20 明确要求卡片不出声 */
      v.muted = true;
      el.addEventListener('mouseenter', function () {
        var pr = v.play(); if (pr && pr.catch) pr.catch(function () { });
      });
      el.addEventListener('mouseleave', function () { v.pause(); });
    } else {
      var illBox = el.querySelector('.tkc-thumb[data-ill]');
      if (illBox) {
        var key = illBox.getAttribute('data-ill');
        el.addEventListener('mouseenter', function () {
          if (el._hv) return;
          var dark = document.documentElement.getAttribute('data-theme') === 'dark';
          var nv = document.createElement('video');
          nv.className = 'tkc-media';
          nv.muted = true; nv.loop = true; nv.playsInline = true;
          nv.setAttribute('muted', ''); nv.setAttribute('playsinline', '');
          nv.src = ILL_BASE + (dark ? 'dark_' : 'light_') + key + '.mp4';
          illBox.appendChild(nv);
          var pr = nv.play(); if (pr && pr.catch) pr.catch(function () { });
          el._hv = nv;
        });
        el.addEventListener('mouseleave', function () {
          var nv = el._hv;
          if (nv) { try { nv.pause(); } catch (e) { } nv.remove(); el._hv = null; }
        });
      }
    }

    el.querySelectorAll('.tkc-op').forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        if (opts.onOp) opts.onOp(b.getAttribute('data-op'), task, el);
      });
    });
    return el;
  }

  window.TaskCard = { inject: inject, render: render, STATES: ST, KINDS: KIND_ICON };
})();
