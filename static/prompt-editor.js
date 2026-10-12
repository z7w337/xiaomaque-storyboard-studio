/*
 * 阶段6 · 统一提示词编辑器 共享组件 v3（最终定稿，用户"清楚，可改"）
 * 参考小麻雀图1：顶部资产引用区 / 中部文本区(@+chips) / 底部选择器区。
 * 形态：锚定卡片下方 popover（资产页）；场页中部嵌入（视频，后续）。组件本身可就地嵌入任意容器。
 *
 * window.createPromptEditor(root, cfg) -> { handle }
 * cfg = {
 *   genKind: 'image'|'video',
 *   initialText, initialSelectors: {style, model(workflow), ratio, size, prov, wf, duration, mp, seed},
 *   styles: [ {name, prompt, img?, type?} ],           // 风格库数据（占位图）
 *   data: {
 *     getAssets(kind) -> [{prefix,variant_name,prompt,id,images}],   // characters/scenes/props
 *     getComponents() -> [{name,kind:'dur'|'cam'|'shot'}],
 *     assetImg(kind,asset) -> url,
 *     providers -> [{id,name}], workflows -> [{name,title}], wfByProv -> {prov:[{name,title}]},
 *     workflowInputSlots(workflow) -> { images: N, audio: N }   // 从工作流 JSON 推导
 *   },
 *   callbacks: { onGenerate(payload), onSave(text), onTextChange(text), onDraft(draft) },
 *   draft (=getDraft), onDraftChange(draft)
 * }
 * payload = { prompt, text, references:[{kind:'image'|'audio', name?, path?}], params:{...} }
 */
(function () {
  var CSS = [
    '.pue{display:flex;flex-direction:column;gap:8px;width:100%;box-sizing:border-box}',
    '.pue-refs{display:flex;flex-wrap:wrap;gap:6px;min-height:0}',
    '.pue-refs .rlabel{font-size:11px;color:var(--ss-text-4);align-self:center;margin-right:2px}',
    '.pue-refs .rcard{display:inline-flex;align-items:center;gap:6px;background:var(--ss-card);color:#1f2937;border:1px solid var(--ss-border);border-radius:12px;padding:4px 10px;font-size:12px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.04)}',
    '.pue-refs .rcard:hover{box-shadow:0 4px 12px rgba(0,0,0,.1);border-color:var(--ss-border-strong)}',
    '.pue-refs .rcard img{width:24px;height:24px;border-radius:6px;object-fit:cover}',
    '.pue-refs .rcard .x{cursor:pointer;color:var(--ss-text-4);font-size:13px;line-height:1;margin-left:4px}',
    '.pue-refs .rcard .x:hover{color:#dc2626}',
    '.pue-ta{min-height:150px;border:1px solid var(--ss-border);border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.7;outline:none;white-space:pre-wrap;word-break:break-word;background:var(--ss-card)}',
    '.pue-ta .ph{color:var(--ss-text-4)}',
    '.pue-inline-c{display:inline-flex;align-items:center;background:var(--ss-green-50);color:var(--ss-green-700);border:1px solid var(--ss-green-200);border-radius:999px;padding:1px 8px;margin:1px 2px;font-size:12px;white-space:nowrap;cursor:pointer}',
    '.pue-inline-c.dur{background:#f0fdf4;color:#166534;border-color:#bbf7d0}',
    '.pue-inline-c.cam{background:var(--ss-card)7ed;color:#9a3412;border-color:#fed7aa}',
    '.pue-inline-c.shot{background:#f5f3ff;color:#5b21b6;border-color:#ddd6fe}',
    '.pue-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:2px 2px}',
    '.pue-pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--ss-text-2);background:var(--ss-card);border:1px solid var(--ss-border);border-radius:999px;padding:5px 12px;cursor:pointer}',
    '.pue-pill:hover{background:var(--ss-subtle-bg)}',
    '.pue-pill .arr{color:var(--ss-text-4);font-size:10px}',
    '.pue-icbtn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:50%;border:none;background:var(--ss-subtle-bg);color:var(--ss-text-2);cursor:pointer;font-size:15px}',
    '.pue-icbtn:hover{background:#e5e7eb}',
    '.pue-gen{display:inline-flex;align-items:center;justify-content:center;background:#111827;color:#fff;border:none;border-radius:50%;width:34px;height:34px;cursor:pointer;font-size:16px;margin-left:auto}',
    '.pue-gen.busy{background:#4b5563;animation:pue-pulse 1s ease-in-out infinite;cursor:progress}',
    '@keyframes pue-pulse{0%,100%{opacity:1}50%{opacity:.5}}',
    '.pue-inp{font-size:12px;padding:3px 6px;border:1px solid var(--ss-border-strong);border-radius:6px;width:52px}',
    '.pue-inp.wide{width:80px}',
    '.pue-ref{font-size:11px;color:var(--ss-text-3)}',
    '.pue-preview{display:none;background:#f9fafb;border:1px solid var(--ss-border);border-radius:8px;padding:8px;font-size:12px;max-height:180px;overflow:auto;white-space:pre-wrap}',
    '.pue-menu{position:absolute;z-index:510;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.14);min-width:150px;display:none}',
    '.pue-menu .mi{display:flex;align-items:center;gap:8px;padding:8px 12px;font-size:13px;cursor:pointer}',
    '.pue-menu .mi:hover{background:var(--ss-subtle-bg)}',
    '.pue-stylepop{position:fixed;z-index:512;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.18);width:360px;max-height:420px;display:none;flex-direction:column;overflow:hidden}',
    '.pue-stylepop .tabs{display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--ss-border-soft)}',
    '.pue-stylepop .tabs .t{font-size:12px;color:var(--ss-text-2);background:var(--ss-subtle-bg);border-radius:6px;padding:4px 10px;cursor:pointer}',
    '.pue-stylepop .tabs .t.active{background:#111827;color:#fff}',
    '.pue-stylepop .search{margin:8px;padding:6px 10px;border:1px solid var(--ss-border);border-radius:8px;font-size:12px}',
    '.pue-stylepop .grid{flex:1;overflow:auto;padding:0 8px 8px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px}',
    '.pue-stylepop .sc{display:flex;flex-direction:column;align-items:center;gap:4px;border:1px solid var(--ss-border);border-radius:8px;padding:6px;cursor:pointer;font-size:11px;text-align:center}',
    '.pue-stylepop .sc:hover{border-color:var(--ss-green-600)}',
    '.pue-stylepop .sc.sel{border-color:var(--ss-green-600);box-shadow:0 0 0 1px var(--ss-green-600)}',
    '.pue-stylepop .sc img{width:100%;height:64px;object-fit:cover;border-radius:6px;background:var(--ss-subtle-bg)}',
    '.pue-stylepop .sc .ph2{width:100%;height:64px;border-radius:6px;background:var(--ss-subtle-bg);display:flex;align-items:center;justify-content:center;color:var(--ss-text-4)}',
    '.pue-hoverprev{position:fixed;z-index:515;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.16);padding:8px;display:none;pointer-events:none}',
    '.pue-hoverprev img{max-width:160px;max-height:180px;border-radius:6px;display:block}',
    '.pue-hoverprev .nm{font-size:11px;color:var(--ss-text-3);margin-top:4px;text-align:center}',
    '.pue-atpop{position:fixed;z-index:511;width:520px;max-height:330px;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.16);display:none;flex-direction:column;overflow:hidden}',
    '.pue-atpop .tabs{display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--ss-border-soft)}',
    '.pue-atpop .tab{font-size:12px;color:var(--ss-text-2);background:var(--ss-subtle-bg);border-radius:6px;padding:4px 10px;cursor:pointer}',
    '.pue-atpop .tab.active{background:var(--ss-green-600);color:#fff}',
    '.pue-atpop .kinds{display:flex;gap:6px;flex-wrap:wrap;padding:6px 8px;border-bottom:1px solid #f1f2f4}',
    '.pue-atpop .kt{font-size:11px;color:var(--ss-text-3);background:var(--ss-subtle-bg);border-radius:999px;padding:2px 9px;cursor:pointer;border:1px solid transparent}',
    '.pue-atpop .kt.active{background:#DCE7E1;color:var(--ss-green-700);border-color:#A5C3B1}',
    '.pue-atpop .search{margin:8px;padding:6px 10px;border:1px solid var(--ss-border);border-radius:8px;font-size:12px}',
    '.pue-atpop .list{flex:1;overflow:auto;padding:0 8px 8px}',
    '.pue-atpop .grp{font-size:11px;color:var(--ss-text-4);padding:6px 2px}',
    '.pue-atpop .item{display:inline-flex;align-items:center;gap:5px;font-size:12px;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:999px;padding:4px 10px;margin:3px;cursor:pointer}',
    '.pue-atpop .item:hover{background:var(--ss-subtle-bg)}',
    '.pue-atpop .th-img{width:18px;height:18px;border-radius:4px;object-fit:cover}',
    '.pue-cascade{position:fixed;z-index:512;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.16);display:none;flex-direction:column;min-width:380px;max-width:520px;width:max-content;overflow:hidden}',
    '.pue-cascade .row{display:flex;min-height:200px;max-height:260px}',
    '.pue-cascade .col{overflow:auto;padding:4px;min-width:0}',
    '.pue-cascade .col[data-c="prov"]{flex:0 0 38%}',
    '.pue-cascade .col[data-c="wf"]{flex:1 1 auto;min-width:210px}',
    '.pue-cascade .col+.col{border-left:1px solid var(--ss-border-soft)}',
    '.pue-cascade .hd{font-size:11px;color:var(--ss-text-4);padding:6px 8px}',
    '.pue-cascade .it{display:flex;align-items:center;gap:6px;padding:6px 8px;font-size:13px;cursor:pointer;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}',
    '.pue-cascade .it:hover{background:var(--ss-subtle-bg)}',
    '.pue-cascade .it.sel{background:var(--ss-green-50);color:var(--ss-green-700)}',
    '.pue-cascade .it .d{font-size:11px;color:var(--ss-text-4)}',
    '.pue-cascade .it + .it{margin-top:2px}',
    '.pue-size{position:fixed;z-index:512;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.18);width:340px;max-height:420px;display:none;flex-direction:column;overflow:hidden}',
    '.pue-size .sec{padding:10px 12px 6px}',
    '.pue-size .sec-t{font-size:12px;color:var(--ss-text-3);margin-bottom:8px}',
    '.pue-size .row{display:flex;flex-wrap:wrap;gap:8px}',
    '.pue-size .opt{display:inline-flex;align-items:center;justify-content:center;gap:5px;border:1px solid var(--ss-border);border-radius:999px;padding:6px 12px;font-size:12px;cursor:pointer;min-width:52px}',
    '.pue-size .opt:hover{border-color:var(--ss-green-600)}',
    '.pue-size .opt.sel{border-color:var(--ss-green-600);box-shadow:0 0 0 1px var(--ss-green-600);color:var(--ss-green-600)}',
    '.pue-size .opt .shape{display:inline-block;width:14px;height:10px;border:1.5px solid currentColor;border-radius:2px}',
    '.pue-size .opt.sel .shape{background:var(--ss-green-600)}',
    '.pue-pop-root{position:fixed;z-index:135;background:transparent}',
    /* ===== @ 资产引用弹窗 v2（完全照 scene.html 新架构：资产/小工具 tab + 中部归属列表 + 底部资产库 hover 级联 + 跟随式预览）===== */
    '.pe2-atpop{position:fixed;z-index:511;width:430px;max-width:96vw;height:540px;max-height:88vh;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:10px;box-shadow:0 18px 50px rgba(15,23,42,.16),0 4px 14px rgba(15,23,42,.08);display:none;flex-direction:column;overflow:visible}',
    '.pe2-atpop.show{display:flex}',
    '.pe2-atpop .tabs{display:flex;gap:4px;padding:8px 12px 0;flex:0 0 auto}',
    '.pe2-atpop .tab{padding:7px 16px;font-size:13px;color:var(--ss-text-3);cursor:pointer;border:none;background:none;border-radius:8px 8px 0 0;position:relative}',
    '.pe2-atpop .tab:hover{background:var(--ss-subtle-bg)}',
    '.pe2-atpop .tab.active{color:var(--ss-text-1);font-weight:600}',
    '.pe2-atpop .tab.active::after{content:"";position:absolute;left:12px;right:12px;bottom:0;height:2px;background:#d97706;border-radius:2px}',
    '.pe2-atpop .own-wrap{flex:1;overflow-y:auto;margin-top:4px;padding:2px 10px 10px;min-height:0}',
    '.pe2-atpop .own-wrap::-webkit-scrollbar{width:6px}',
    '.pe2-atpop .own-wrap::-webkit-scrollbar-thumb{background:#d8dce2;border-radius:3px}',
    '.pe2-atpop .grp-title{font-size:12px;color:var(--ss-text-4);font-weight:600;padding:10px 6px 5px;letter-spacing:.5px}',
    '.pe2-atpop .arow{display:flex;align-items:center;gap:10px;padding:6px 7px;border-radius:8px;cursor:pointer}',
    '.pe2-atpop .arow:hover{background:var(--ss-subtle-bg)}',
    '.pe2-atpop .arow img{width:40px;height:40px;border-radius:8px;object-fit:cover;background:#eceff3;flex:0 0 40px;display:block}',
    '.pe2-atpop .arow .meta{min-width:0;flex:1}',
    '.pe2-atpop .arow .name{font-size:14px;color:var(--ss-text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pe2-atpop .arow .sub{font-size:12px;color:var(--ss-text-4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pe2-atpop .arow .atmark{font-size:12px;color:var(--ss-warning);opacity:0;flex:0 0 auto}',
    '.pe2-atpop .arow:hover .atmark{opacity:1}',
    '.pe2-atpop .arow .thumb-ph{width:40px;height:40px;border-radius:8px;flex:0 0 40px;font-size:15px}',
    '.pe2-atpop .tools-wrap{flex:1;overflow-y:auto;padding:12px;display:grid;grid-template-columns:1fr 1fr;gap:12px;align-content:start}',
    '.pe2-atpop .tool-card{border:1px solid var(--ss-border);border-radius:10px;padding:16px 14px;display:flex;flex-direction:column;align-items:center;gap:8px;background:var(--ss-card);cursor:pointer}',
    '.pe2-atpop .tool-card:hover{border-color:#f0c48f;background:#fdf3e3}',
    '.pe2-atpop .tool-card .tic{width:40px;height:40px;border-radius:10px;background:#eef1f5;display:grid;place-items:center;color:#5b6470}',
    '.pe2-atpop .tool-card .tt{font-size:13px;font-weight:600;color:var(--ss-text-1)}',
    '.pe2-atpop .tool-card .ts{font-size:11.5px;color:var(--ss-text-4);text-align:center}',
    '.pe2-atpop .lib-entry{border-top:1px solid #eef0f3;padding:9px 12px 10px;display:flex;align-items:center;gap:10px;background:#fafbfc;border-radius:0 0 10px 10px;cursor:pointer;flex:0 0 auto;position:relative;z-index:2}',
    '.pe2-atpop .lib-entry:hover{background:var(--ss-subtle-bg)}',
    '.pe2-atpop .lib-entry .lic{width:34px;height:34px;border-radius:8px;background:#eef1f5;display:grid;place-items:center;color:#5b6470;flex:0 0 34px}',
    '.pe2-atpop .lib-entry .lt{font-size:14px;font-weight:600;color:var(--ss-text-1)}',
    '.pe2-atpop .lib-entry .ls{font-size:12px;color:var(--ss-text-4)}',
    '.pe2-atpop .lib-entry .arrow{margin-left:auto;color:var(--ss-text-4);display:flex}',
    '.pe2-atpop .cascade{position:absolute;top:0;left:100%;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:8px;box-shadow:0 10px 26px rgba(0,0,0,.14);padding:5px;z-index:5;display:none}',
    '.pe2-atpop .cascade.show{display:block}',
    '.pe2-atpop .cascade.lvl2{width:176px}',
    '.pe2-atpop .cascade.lvl3{width:254px}',
    '.pe2-atpop .c-item{display:flex;align-items:center;gap:9px;padding:8px 9px;border-radius:7px;cursor:pointer;position:relative}',
    '.pe2-atpop .c-item:hover,.pe2-atpop .c-item.open{background:var(--ss-subtle-bg)}',
    '.pe2-atpop .c-item .cic{width:26px;height:26px;border-radius:6px;display:grid;place-items:center;background:#eef1f5;color:#5b6470;flex:0 0 26px}',
    '.pe2-atpop .c-item .ct{font-size:13px;color:var(--ss-text-1);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pe2-atpop .c-item .go{color:var(--ss-text-4);display:flex}',
    '.pe2-atpop .c-item img{width:36px;height:36px;border-radius:7px;object-fit:cover;background:#eceff3;flex:0 0 36px;display:block}',
    '.pe2-atpop .c-item .cmeta{min-width:0;flex:1}',
    '.pe2-atpop .c-item .cn{font-size:13px;color:var(--ss-text-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pe2-atpop .c-item .cs{font-size:11.5px;color:var(--ss-text-4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pe2-atpop .c-item .thumb-ph{width:36px;height:36px;border-radius:7px;flex:0 0 36px}',
    '.pe2-atpop .checker-ph{display:grid;place-items:center;color:#8a94a2;background:repeating-conic-gradient(#e9ebef 0% 25%,#fbfcfd 0% 50%) 0 0/14px 14px;border:1px dashed #cfd4db}',
    '.pe2-atpop .grp-empty{font-size:12px;color:#c3c8cf;padding:14px 6px}',
    '.pe2-preview{position:fixed;z-index:516;width:300px;background:var(--ss-card);border:1px solid var(--ss-border);border-radius:12px;box-shadow:0 10px 26px rgba(0,0,0,.18);padding:12px;display:none;pointer-events:none}',
    '.pe2-preview.show{display:block}',
    '.pe2-preview .pv-big{width:100%;aspect-ratio:1;border-radius:9px;object-fit:cover;background:#eceff3;display:block}',
    '.pe2-preview .pv-big-ph{width:100%;aspect-ratio:1;border-radius:9px;display:grid;place-items:center;color:#8a94a2;background:repeating-conic-gradient(#e9ebef 0% 25%,#fbfcfd 0% 50%) 0 0/16px 16px;border:1px dashed #cfd4db}',
    '.pe2-preview .pv-name{font-size:15px;font-weight:600;color:var(--ss-text-1);margin-top:9px;text-align:center}',
    '.pe2-preview .pv-type{font-size:12px;color:var(--ss-warning);background:#fdf3e3;padding:2px 10px;border-radius:20px;display:inline-block;margin:6px auto 0}',
    '.pe2-preview .pv-desc{font-size:12.5px;color:var(--ss-text-3);line-height:1.7;text-align:center;margin-top:5px;word-break:break-all;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}',
    '.pe2-preview .pv-type-wrap{text-align:center}',
    /* ===== v2.1 步4 深色覆盖（浅色字面量保留，仅深色改写）===== */
    '[data-theme="dark"] .pue-icbtn:hover{background:var(--ss-hover-bg)}',
    '[data-theme="dark"] .pue-icbtn{background:var(--ss-subtle-bg);color:var(--ss-text-2)}',
    '[data-theme="dark"] .pue-gen{background:var(--ss-ink-bar)}',
    '[data-theme="dark"] .pue-inline-c.dur{background:var(--ss-success-bg);color:var(--ss-success);border-color:#1d4a2c}',
    '[data-theme="dark"] .pue-inline-c.cam{background:var(--ss-warning-bg);color:var(--ss-warning);border-color:var(--ss-warning-border)}',
    '[data-theme="dark"] .pue-inline-c.shot{background:var(--ss-running-bg);color:var(--ss-running);border-color:#3a2f5c}',
    '[data-theme="dark"] .pe2-atpop .c-item .cic{background:var(--ss-subtle-bg);color:var(--ss-text-3)}',
    '[data-theme="dark"] .pe2-atpop .c-item img,.pe2-preview .pv-big{background:var(--ss-subtle-bg)}',
    '[data-theme="dark"] .checker-ph,.pe2-preview .pv-big-ph{background:repeating-conic-gradient(#2a302e 0% 25%,#222826 0% 50%) 0 0/14px 14px;border-color:var(--ss-border-strong)}',
    '[data-theme="dark"] .pe2-preview .pv-type{background:var(--ss-warning-bg);color:var(--ss-warning)}',
    '[data-theme="dark"] .pue-refs .rlabel{color:var(--ss-text-4)}',
  ].join('\n');
  var CSS_ID = 'pue3-css';
  function ensureCss() { if (document.getElementById(CSS_ID)) return; var st = document.createElement('style'); st.id = CSS_ID; st.textContent = CSS; document.head.appendChild(st); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function escAttr(s) { return String(s).replace(/'/g, '\\\''); }
  function toast(text, type) { if (window.toast) window.toast(text, type); else alert(text); }
  var COUNTER = 0;

  function createPromptEditor(cfg) {
    ensureCss();
    cfg = cfg || {};
    var root = cfg.root; if (!root) throw new Error('createPromptEditor: root 缺失');
    var uid = 'pe' + (++COUNTER);
    var genKind = cfg.genKind || 'image';
    var d = cfg.data || {};
    var cb = cfg.callbacks || {};
    var initialSel = cfg.initialSelectors || {};
    var styles = cfg.styles || [];
    var refPath = '';
    var draft = cfg.draft || null;
    var atGroup = 'asset', atKind = 'all', savedRange = null, atActive = false;
    // ★ size 固定 '1K'（用户 2026-10-01：清晰度去掉，分辨率恒定 1K；宽高由比例决定）
    var selState = { style: initialSel.style || '', workflow: initialSel.workflow || '', prov: initialSel.prov || '', ratio: initialSel.ratio || '', size: '1K', mp: initialSel.mp || '', duration: initialSel.duration || '5', seed: initialSel.seed || '', cloudModel: initialSel.cloudModel || '' };
    var refsList = [];   // 引用区（@资产名）展示
    var pop = null, stylepop = null, hover = null, menu = null, cascade = null, sizePop = null, changeMenu = null, fields = {}, prev2 = null;

    root.classList.add('pue');
    root.innerHTML = '' +
      '<div class="pue-refs" data-a="refs"></div>' +
      '<div class="pue-ta" contenteditable="true" spellcheck="false" placeholder="输入提示词（支持 @ 引用资产/组件）"></div>' +
      '<div class="pue-bar">' +
        '<button type="button" class="pue-icbtn" data-a="plus" title="添加（上传参考图 / 从资产库选择）">＋</button>' +
        '<span class="pue-pill" data-a="style" title="画风（点开风格库）"><span class="pv" data-pv="style">画风</span><span class="arr">▾</span></span>' +
        '<span class="pue-pill" data-a="workflow" title="模型/工作流"><span class="pv" data-pv="workflow">' + (genKind === 'video' ? '工作流' : '模型') + '</span><span class="arr">▾</span></span>' +
        '<span class="pue-pill" data-a="aspectsize" title="比例·分辨率"><span class="pv" data-pv="aspectsize">' + (initialSel.aspectsize || (genKind === 'video' ? '比例' : '16:9')) + '</span><span class="arr">▾</span></span>' +
        '<span style="flex:1"></span>' +
        '<button type="button" class="pue-icbtn" data-a="preview" title="组装预览（序列化/工作流入参）">👁</button>' +
        '<button type="button" class="pue-icbtn" data-a="ai" title="AI 优化提示词">✨</button>' +
        '<span class="pue-gen" data-a="gen" title="' + (genKind === 'video' ? '生成视频' : '生成图片') + '">↑</span>' +
      '</div>' +
      '<div class="pue-preview" data-a="previewbox"></div>' +
      (cfg.showSave === true ? '<button type="button" class="pue-pill" data-a="save" style="align-self:flex-end">保存</button>' : '');

    var ta = root.querySelector('.pue-ta');

    // ── 悬浮层（浮层元素，body 级）──
    function mkFloat(cls) { var el = document.createElement('div'); el.className = cls; document.body.appendChild(el); return el; }
    /* @ 资产引用弹窗 v2（完全照 scene.html 新架构） */
    pop = mkFloat('pue-atpop pe2-atpop');
    pop.innerHTML =
      '<div class="tabs" data-a="tabrow">' +
        '<button type="button" class="tab active" data-a2="asset">资产</button>' +
        '<button type="button" class="tab" data-a2="tools">小工具</button>' +
      '</div>' +
      '<div class="own-wrap" data-a2="assetView"></div>' +
      '<div class="tools-wrap" style="display:none" data-a2="toolView"></div>' +
      '<div class="lib-entry" data-a2="libEntry">' +
        '<span class="lic"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 5h18v14H3z"/><path d="M3 9h18M7 5v14"/></svg></span>' +
        '<span><div class="lt">资产库</div><div class="ls">人物 · 场景 · 道具 · 参考 · 未分类</div></span>' +
        '<span class="arrow"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 6l6 6-6 6"/></svg></span>' +
      '</div>' +
      '<div class="cascade lvl2" data-a2="L2"></div>' +
      '<div class="cascade lvl3" data-a2="L3"></div>';
    prev2 = mkFloat('pe2-preview');
    stylepop = mkFloat('pue-stylepop');
    stylepop.innerHTML = '<div class="tabs"><span class="t active" data-t="all">全部</span><span class="t" data-t="real">真人</span><span class="t" data-t="2d">2D</span><span class="t" data-t="3d">3D</span></div>' +
      '<input class="search" placeholder="搜索风格…" data-a="ssearch"><div class="grid" data-a="sgrid"></div>';
    hover = mkFloat('pue-hoverprev');
    menu = mkFloat('pue-menu');
    menu.innerHTML = '<div class="mi" data-a="upload">⬆ 本地上传</div><div class="mi" data-a="library">🗂 从资产库选择</div>';
    changeMenu = mkFloat('pue-menu');
    var changeOpts = { dur: ['2', '3', '5', '10', '15'], cam: ['推近', '拉远', '横摇', '直移', '跟拍', '升降', '环绕', '手持', '变焦', '固定', '仰拍', '俯拍'], shot: ['特写', '近景', '中景', '全景', '远景'] };
    var changeTarget = null;
    function openChangeMenu(chip, kind, e) {
      changeTarget = { chip: chip, kind: kind };
      changeMenu.innerHTML = changeOpts[kind].map(function (v) { return '<div class="mi" data-v="' + escAttr(v) + '">' + esc(v) + '</div>'; }).join('');
      changeMenu.style.display = 'block';
      var r = chip.getBoundingClientRect();
      changeMenu.style.left = Math.min(r.left, window.innerWidth - 160) + 'px';
      changeMenu.style.top = (r.bottom + 4) + 'px';
      changeMenu.querySelectorAll('[data-v]').forEach(function (mi) { mi.addEventListener('click', function (e2) { e2.stopPropagation(); applyChange(mi.getAttribute('data-v')); }); });
      changeMenu.addEventListener('mousedown', function (e2) { e2.stopPropagation(); });
    }
    function applyChange(v) {
      var t = changeTarget; if (!t) return;
      var marker;
      if (t.kind === 'dur') marker = '<duration-ms>' + (parseInt(v) * 1000) + '</duration-ms>';
      else if (t.kind === 'cam') marker = '<camera-move>' + v + '</camera-move>';
      else marker = '<shot-size>' + v + '</shot-size>';
      t.chip.setAttribute('data-marker', marker);
      t.chip.textContent = (t.kind === 'dur' ? '⏱ ' + parseInt(v) + 's' : t.kind === 'cam' ? '🎥 ' + v : '🖼 ' + v);
      changeMenu.style.display = 'none'; changeTarget = null;
      onTextChangeInternal();
    }
    function addChangeHandler(sp) {
      sp.addEventListener('click', function (e) {
        var kind = sp.getAttribute('data-kind');
        if ((kind === 'dur' || kind === 'cam' || kind === 'shot')) { e.stopPropagation(); openChangeMenu(sp, kind, e); }
      });
    }
    cascade = mkFloat('pue-cascade');
    cascade.innerHTML = '<div class="row"><div class="col" data-c="prov"></div><div class="col" data-c="wf"></div></div>';
    sizePop = mkFloat('pue-size');
    /* 用户 2026-10-01：**清晰度去掉，只保留比例** —— 本工作流没有 3-4K 高分能力，
       分辨率固定 1K（长边 1024，短边按比例）；要更高清去「生图工具」的 enhance/upscale。*/
    sizePop.innerHTML = '<div class="sec"><div class="sec-t">比例（分辨率固定 1K）</div><div class="row" data-s="ratio"></div></div>';
    var styleStrip = [];

    // ── 选择器/胶囊 ──
    function pv(k) { var e = root.querySelector('[data-pv="' + k + '"]'); return e ? e.textContent : ''; }
    function setPv(k, v) { var e = root.querySelector('[data-pv="' + k + '"]'); if (e) e.textContent = v; }
    function setupWorkflow() {
      var provs = d.providers || []; var wfs = d.workflows || [];
      if (genKind === 'image') {
        // ★ ④ F5c：平台选了云端 ⇒ 药丸显示本次选的云端模型（未选则显示平台名）
        if (selState.prov === 'modelscope') {
          setPv('workflow', (selState.cloudModel ? String(selState.cloudModel).split('/').pop() : 'Modelscope'));
          return;
        }
        /* ★ 用户 2026-10-01：药丸显示名**不带 .json**（如 Z-Image.json → Z-Image） */
        setPv('workflow', String(selState.workflow || '').replace(/\.json$/i, '').split('/').pop() || '模型');
      } else {
        setPv('workflow', selState.workflow || '工作流');
      }
    }

    // ── @ 引用 ──
    function getAtContext() { var s = window.getSelection(); if (!s || !s.rangeCount || !ta.contains(s.anchorNode)) return null; var r = s.getRangeAt(0); var n = r.startContainer; var before = (n.nodeType === 3) ? n.textContent.slice(0, r.startOffset) : ''; var idx = before.lastIndexOf('@'); if (idx < 0) return null; var after = before.slice(idx + 1); if (/\s/.test(after)) return null; return { at: after }; }
    /* ===== @ 资产引用弹窗 v2（照 scene.html 新架构）===== */
    var atTab = 'asset';               // 当前 tab: asset / tools
    var atHideTimer2 = null;
    function $pe2(q, scope) { return (scope || pop).querySelector(q); }
    function atP2(k) { return pop.querySelector('[data-a2="' + k + '"]'); }
    function checkAt() {
      var c = getAtContext();
      if (!c) { hideAtPop(); return; }
      var s = window.getSelection(); if (!s || !s.rangeCount) return;
      savedRange = s.getRangeAt(0).cloneRange();
      var r = s.getRangeAt(0).getBoundingClientRect();
      var W = 430, H = Math.min(540, window.innerHeight - 40);
      pop.style.width = W + 'px'; pop.style.height = H + 'px'; pop.style.maxHeight = H + 'px';
      var l = Math.max(8, Math.min(r.left, window.innerWidth - W - 16));
      var t = r.bottom + 6;
      if (t + H > window.innerHeight - 8) t = Math.max(8, r.top - H - 6);
      pop.style.left = l + 'px'; pop.style.top = t + 'px';
      pop.style.display = 'flex';
      pe2SwitchTab('asset');
    }
    function hideAtPop() { pop.style.display = 'none'; atActive = false; pe2HideAll(); }
    /* tab 切换 */
    function pe2SwitchTab(tab) {
      atTab = tab;
      pop.querySelectorAll('[data-a2="tabrow"] .tab').forEach(function (x) { x.classList.toggle('active', x.getAttribute('data-a2') === tab); });
      var isAsset = tab === 'asset';
      atP2('assetView').style.display = isAsset ? 'block' : 'none';
      atP2('toolView').style.display = isAsset ? 'none' : 'grid';
      pe2HideAll();
      if (isAsset) pe2RenderOwnAssets(); else pe2RenderTools();
    }
    /* 资产对象 → {kind, name, thumb, desc, sub} */
    function pe2FullName(a) { return ((a.prefix || '') + '-' + (a.variant_name || '')).replace(/-$/, ''); }
    function pe2Thumb(k, a) { return (d.assetImg ? d.assetImg(k, a) : '') || (a && a.images && a.images[0] ? ('/api/asset-file/' + encodeURIComponent(a.images[0].path)) : ''); }
    function pe2Desc(a) { return (a && a.prompt) ? String(a.prompt) : ''; }
    /* 资产库五类定义 */
    var PE2_LIB = [
      { title: '人物', key: 'characters', icon: 'person' },
      { title: '场景', key: 'scenes', icon: 'scene' },
      { title: '道具', key: 'props', icon: 'prop' },
      { title: '参考', key: 'reference', ref: true, icon: 'ref' },
      { title: '未分类', key: 'unclassified', ref: true, icon: 'ref' }
    ];
    var PE2_IC = {
      person: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="8" r="3.4"/><path d="M5 20c.8-3.6 3.4-5.4 7-5.4s6.2 1.8 7 5.4"/></svg>',
      scene: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 21h18M5 21V10l7-5 7 5v11"/><path d="M9 21v-6h6v6"/></svg>',
      prop: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 8h14v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8Z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>',
      ref: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M4 17l5-4 4 3 3-2 4 3"/></svg>'
    };
    function pe2GetItems(kd) {
      var items = (d.getAssets ? d.getAssets(kd.key) : null) || [];
      if (!items.length && kd.ref && d.refs && kd.key === 'reference') items = d.refs;
      return (items || []).map(function (a) {
        if (kd.ref) {
          var pth = a.path || ((a.images || [])[0] || {}).path || '';
          if (!pth) return null;
          return { kind: 'ref', key: kd.key, name: String(pth).split('/').pop(), path: pth, thumb: '/api/asset-file/' + encodeURIComponent(pth), desc: pe2Desc(a) };
        }
        return { kind: 'asset', key: kd.key, name: pe2FullName(a), asset: a, thumb: pe2Thumb(kd.key, a), desc: pe2Desc(a), sub: (a.variant_name || '') };
      }).filter(Boolean);
    }
    /* 中部归属列表：人物/场景/道具（prompt-editor 无集/项目上下文，展示全库三组；副标=变体） */
    function pe2RenderOwnAssets() {
      var wrap = atP2('assetView'); wrap.innerHTML = '';
      var MID = [PE2_LIB[0], PE2_LIB[1], PE2_LIB[2]];
      var any = false;
      MID.forEach(function (kd) {
        var items = pe2GetItems(kd); if (!items.length) return; any = true;
        var g = document.createElement('div'); g.className = 'grp-title'; g.textContent = kd.title; wrap.appendChild(g);
        items.forEach(function (it) { wrap.appendChild(pe2RowAsset(it, kd)); });
      });
      if (!any) { var e = document.createElement('div'); e.className = 'grp-empty'; e.textContent = '当前无资产，请展开下方「资产库」浏览'; wrap.appendChild(e); }
      pe2BindLibEntry();
    }
    function pe2RowAsset(it, kd) {
      var row = document.createElement('div'); row.className = 'arow';
      if (it.thumb) { var im = document.createElement('img'); im.src = it.thumb; im.alt = ''; row.appendChild(im); }
      else { var ph = document.createElement('div'); ph.className = 'thumb-ph checker-ph'; ph.innerHTML = PE2_IC[kd.icon]; row.appendChild(ph); }
      var meta = document.createElement('div'); meta.className = 'meta';
      meta.innerHTML = '<div class="name">' + esc(it.name) + '</div><div class="sub">' + esc(it.sub || kd.title) + '</div>';
      row.appendChild(meta);
      var atmark = document.createElement('span'); atmark.className = 'atmark'; atmark.textContent = '@'; row.appendChild(atmark);
      pe2BindPv(row, it);
      row.addEventListener('click', function () { pe2Pick(it); });
      return row;
    }
    /* 小工具 tab：PE_COMPS 具体值（运镜/景别/时长）卡片，点击 insertAt */
    function pe2RenderTools() {
      var wrap = atP2('toolView'); wrap.innerHTML = '';
      var comps = (d.getComponents ? d.getComponents() : []) || [];
      if (!comps.length) { comps = [{ name: '推近', kind: 'cam' }, { name: '特写', kind: 'shot' }, { name: '5', kind: 'dur' }]; }
      comps.forEach(function (c) {
        var card = document.createElement('div'); card.className = 'tool-card';
        card.innerHTML = '<span class="tic">' + PE2_IC[c.kind === 'cam' ? 'person' : (c.kind === 'shot' ? 'scene' : 'ref')] + '</span><div class="tt">' + esc(String(c.name)) + '</div>';
        card.addEventListener('click', function () { insertAt(String(c.name), c.kind || ''); });
        wrap.appendChild(card);
      });
    }
    /* 跟随式预览浮层 */
    function pe2PvShow(it, anchorRect) {
      var has = !!it.thumb;
      prev2.innerHTML =
        (has ? '<img class="pv-big" src="' + esc(it.thumb) + '">' : '<div class="pv-big-ph">' + PE2_IC.ref + '</div>') +
        '<div class="pv-name">' + esc(it.name) + '</div>' +
        '<div class="pv-type-wrap"><span class="pv-type">' + esc(it.type || '') + '</span></div>' +
        (it.desc ? '<div class="pv-desc">' + esc(it.desc) + '</div>' : '');
      prev2.classList.add('show');
      var r = anchorRect, pw = 300;
      var popR = pop.getBoundingClientRect();
      var l2el = atP2('L2'), l3el = atP2('L3');
      var inMid = !l3el.classList.contains('show');
      var cRight = popR.right;
      if (l2el.classList.contains('show')) { var r2 = l2el.getBoundingClientRect(); cRight = Math.max(cRight, r2.right); }
      if (l3el.classList.contains('show')) { var r3 = l3el.getBoundingClientRect(); cRight = Math.max(cRight, r3.right); }
      var left = r.right + 14;
      var hit = inMid && (l2el.classList.contains('show') || l3el.classList.contains('show')) && left < cRight + 12;
      if (hit || left + pw > window.innerWidth - 8) left = r.left - pw - 14;
      if (left < 8) left = 8;
      var top = r.top - 4;
      if (top + 390 > window.innerHeight - 8) top = Math.max(8, window.innerHeight - 398);
      prev2.style.left = left + 'px'; prev2.style.top = top + 'px';
    }
    function pe2PvHide() { prev2.classList.remove('show'); }
    function pe2BindPv(el, it) {
      el.addEventListener('mouseenter', function () { pe2PvShow(it, el.getBoundingClientRect()); });
      el.addEventListener('mouseleave', pe2PvHide);
    }
    /* 资产库级联（hover） */
    function pe2BindLibEntry() {
      var entry = atP2('libEntry');
      entry.onmouseenter = function () { pe2ClearHide(); pe2PvHide(); pe2BuildL2(); };
      entry.onmouseleave = function () { pe2ScheduleHide(); };
    }
    function pe2BuildL2() {
      var L2 = atP2('L2'); L2.innerHTML = '';
      PE2_LIB.forEach(function (kd) {
        var d2 = document.createElement('div'); d2.className = 'c-item';
        d2.innerHTML = '<span class="cic">' + PE2_IC[kd.icon] + '</span><span class="ct">' + kd.title + '</span><span class="go"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 6l6 6-6 6"/></svg></span>';
        d2.onmouseenter = function () { pe2BuildL3(kd, d2); };
        L2.appendChild(d2);
      });
      L2.classList.add('show'); pe2PlaceL2();
    }
    function pe2BuildL3(kd, anchor) {
      var L3 = atP2('L3'); L3.innerHTML = '';
      pe2GetItems(kd).forEach(function (it) {
        var d2 = document.createElement('div'); d2.className = 'c-item';
        d2.innerHTML = (it.thumb
          ? '<img src="' + esc(it.thumb) + '">'
          : '<div class="thumb-ph checker-ph">' + PE2_IC[kd.ref ? 'ref' : kd.icon] + '</div>') +
          '<div class="cmeta"><div class="cn">' + esc(it.name) + '</div><div class="cs">' + esc(it.sub || kd.title) + '</div></div>';
        var pvOpt = { thumb: it.thumb, name: it.name, type: kd.title, desc: it.desc };
        d2.onmouseenter = function () { pe2BuildL3Mark(anchor, d2); pe2PvShow(pvOpt, d2.getBoundingClientRect()); };
        d2.onmouseleave = pe2PvHide;
        d2.onclick = function () { pe2Pick(it); };
        L3.appendChild(d2);
      });
      if (!L3.children.length) { var e = document.createElement('div'); e.className = 'grp-empty'; e.textContent = '该分类暂无资产'; L3.appendChild(e); }
      L3.classList.add('show'); pe2PlaceL3(anchor);
    }
    function pe2BuildL3Mark(anchor, act) {
      atP2('L2').querySelectorAll('.c-item').forEach(function (i) { i.classList.toggle('open', i === anchor); });
      atP2('L3').querySelectorAll('.c-item').forEach(function (i) { i.classList.toggle('open', i === act); });
    }
    function pe2PlaceL2() {
      var L2 = atP2('L2'), popR = pop.getBoundingClientRect();
      var w2 = L2.offsetWidth;
      var rightSpace = window.innerWidth - 8 - popR.right, leftSpace = popR.left - 8;
      L2.style.left = ''; L2.style.right = ''; L2.style.top = '';
      if (rightSpace >= w2 || rightSpace >= leftSpace) L2.style.left = '100%'; else L2.style.right = '100%';
      var er = atP2('libEntry').getBoundingClientRect();
      L2.style.top = (er.top - popR.top - 6) + 'px';
    }
    function pe2PlaceL3(anchor) {
      var L2 = atP2('L2'), L3 = atP2('L3'), popR = pop.getBoundingClientRect();
      var l2R = L2.getBoundingClientRect();
      var w3 = L3.offsetWidth, h3 = L3.offsetHeight;
      L3.style.left = ''; L3.style.right = ''; L3.style.top = '';
      var rightSpace = window.innerWidth - 8 - l2R.right, leftSpace = l2R.left - 8;
      if (rightSpace >= w3 || rightSpace >= leftSpace) { L3.style.left = (l2R.right - popR.left - 2) + 'px'; }
      else { L3.style.left = Math.max(4, (l2R.left - w3 + 2 - popR.left)) + 'px'; }
      var top = (l2R.top - popR.top) + anchor.offsetTop - 6;
      if (top + h3 > window.innerHeight - 8) top = Math.max(4, window.innerHeight - h3 - 8);
      L3.style.top = Math.max(4, top) + 'px';
    }
    function pe2HideAll() { if (atP2('L2')) atP2('L2').classList.remove('show'); if (atP2('L3')) atP2('L3').classList.remove('show'); pe2PvHide(); }
    function pe2ClearHide() { if (atHideTimer2) { clearTimeout(atHideTimer2); atHideTimer2 = null; } }
    function pe2ScheduleHide() { pe2ClearHide(); atHideTimer2 = setTimeout(function () { pe2HideAll(); }, 150); }
    function pe2Pick(it) {
      pe2HideAll();
      if (it.kind === 'ref') insertAt(it.path, 'reference');
      else insertAt(it.name, it.key);
    }
    /* 中部列表（无图资产对象可能带 role/scene/prop key 用于 insertAt）——上面 pe2RowAsset 已按 pe2GetItems 取 kind 资产直接 insertAt(it.key) */

    function rangeAtClient(x, y) {
      var r = null;
      if (document.caretRangeFromPoint) { try { r = document.caretRangeFromPoint(x, y); } catch (e) {} }
      if (r && ta.contains(r.startContainer)) return r;
      return null;
    }
    function insertAt(name, kind, cx, cy) {
      var isRef = (kind === 'reference'); var isRole = (kind === 'characters' || kind === 'scenes' || kind === 'props'); var isNum = !isRole && !isRef && /^\d+$/.test(name);
      var isCam = kind === 'cam' || (!isRole && !isNum && !isRef && /^(推近|拉远|横摇|直移|跟拍|升降|环绕|手持|变焦|固定|仰拍|俯拍)$/.test(name)); var isShot = kind === 'shot' || (!isRole && !isNum && !isCam && !isRef);
      var marker;
      if (isRef) marker = '<reference>' + name + '</reference>';
      else if (isRole) marker = '<role data-' + (kind === 'characters' ? 'character' : kind === 'scenes' ? 'scene' : 'prop') + '-asset-id="">' + name + '</role>';
      else if (isNum) marker = '<duration-ms>' + (parseInt(name) * 1000) + '</duration-ms>';
      else if (isCam) marker = '<camera-move>' + name + '</camera-move>';
      else marker = '<shot-size>' + name + '</shot-size>';
      var sp = document.createElement('span'); sp.className = 'pue-inline-c ' + (isRef ? 'role' : isRole ? 'role' : isNum ? 'dur' : isCam ? 'cam' : 'shot'); sp.contentEditable = 'false';
      sp.setAttribute('data-marker', marker); sp.setAttribute('data-kind', isRef ? 'reference' : isRole ? 'role' : isNum ? 'dur' : isCam ? 'cam' : 'shot'); if (isRef) sp.setAttribute('data-refpath', name); if (isRole) sp.setAttribute('data-name', name);
      sp.appendChild(document.createTextNode((isRef ? '🖼 ' : isRole ? '👤 ' : isNum ? '⏱ ' : isCam ? '🎥 ' : '🖼 ') + (isRef ? refLabel(name) : (isNum ? (parseInt(name)) + 's' : name))));
      addChangeHandler(sp); if (isRole) { var aa = findAsset(name); addHoverPreview(sp, function () { return aa; }); } if (isRef) addHoverImage(sp, '/api/asset-file/' + encodeURIComponent(name), (name || '').split('/').pop());
      var ed = ta; var range = null;
      if (cx != null && cy != null) range = rangeAtClient(cx, cy);
      if (!range && savedRange) range = savedRange.cloneRange();
      if (!range) { var rsel = window.getSelection(); if (rsel && rsel.rangeCount && ta.contains(rsel.getRangeAt(0).startContainer)) range = rsel.getRangeAt(0).cloneRange(); }
      if (!range || !ed.contains(range.startContainer)) { range = document.createRange(); range.selectNodeContents(ed); range.collapse(false); }
      var sel = window.getSelection(); var node = range.startContainer; if (node.nodeType === 3) { var atIdx = node.textContent.lastIndexOf('@', range.startOffset); if (atIdx >= 0) { var after = node.textContent.slice(range.startOffset); var before = node.textContent.slice(0, atIdx); node.textContent = before + after; range.setStart(node, atIdx); range.collapse(true); } }
      savedRange = null; range.insertNode(sp); range.setStartAfter(sp); range.collapse(true); sel.removeAllRanges(); sel.addRange(range);
      hideAtPop(); onTextChangeInternal();
    }
    function setCaretAt(x, y) { var r = rangeAtClient(x, y); if (!r) return; var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }

    // ── 序列化 / 内联组件→自然语言 ──
    var NATUR = { dur: function (v) { return '时长' + (parseFloat(v) / 1000).toFixed(parseFloat(v) % 1000 ? 1 : 0) + '秒'; }, cam: function (v) { return '运镜：' + v; }, shot: function (v) { return '景别：' + v; }, role: function (v) { return v; } };
    function serializeRaw() { var c = ta.cloneNode(true); c.querySelectorAll('.pue-inline-c').forEach(function (sp) { sp.replaceWith(document.createTextNode(sp.getAttribute('data-marker') || '')); }); c.querySelectorAll('.ph').forEach(function (p) { p.remove(); }); return (c.textContent || '').replace(/\n{3,}/g, '\n\n').trim(); }
    function serialize() { var c = ta.cloneNode(true); c.querySelectorAll('.pue-inline-c').forEach(function (sp) { var kind = sp.getAttribute('data-kind'); if (kind === 'reference') { sp.remove(); return; } var v = (sp.textContent || '').replace(/^[^ ]*\s?/, ''); var mk = sp.getAttribute('data-marker') || ''; var raw = mk.replace(/^<[^>]*>/, '').replace(/<\/[^>]*>$/, ''); var t = raw; if (kind && NATUR[kind]) t = NATUR[kind](raw); sp.replaceWith(document.createTextNode(t)); }); c.querySelectorAll('.ph').forEach(function (p) { p.remove(); }); return (c.textContent || '').replace(/\n{3,}/g, '\n\n').trim(); }

    // ── 引用区（@资产卡片，与内联芯片同步）──
    function findAsset(nm) { var ks = ['characters', 'scenes', 'props']; for (var i = 0; i < ks.length; i++) { var ls = (d.getAssets ? d.getAssets(ks[i]) : []) || []; for (var j = 0; j < ls.length; j++) { var a = ls[j]; if (((a.prefix || '') + '-' + (a.variant_name || '')).replace(/-$/, '') === nm) return { kind: ks[i], asset: a }; } } return null; }
    function refLabel(p) { var f = (p || '').split('/').pop(); return f.length > 12 ? f.slice(0, 12) + '…' : f; }
    function refreshRefs() {
      var bar = root.querySelector('[data-a="refs"]'); if (!bar) return; var text = serializeRaw(); var names = [], seen = {};
      var re = /<((?:role|scene|prop))\b[^>]*>([^<]+)<\/\1>/g, m; while ((m = re.exec(text))) { var nm = (m[2] || '').replace(/-$/, '').trim(); if (nm && !seen[nm]) { seen[nm] = 1; names.push(nm); } }
      var refPats = [], seenRef = {}; var reRef = /<reference>([^<]+)<\/reference>/g, mm; while ((mm = reRef.exec(text))) { var rp = (mm[1] || '').trim(); if (rp && !seenRef[rp]) { seenRef[rp] = 1; refPats.push(rp); } }
      refsList = names; bar.innerHTML = '';
      // 参考图：仅显示文本中已引用的 reference 芯片（拖入文本后才出现），非自动显示
      refPats.slice(0, 12).forEach(function (rp) {
        var rc = document.createElement('span'); rc.className = 'rcard refimg';
        rc.innerHTML = '<img src="' + esc('/api/asset-file/' + encodeURIComponent(rp)) + '"><span>' + esc(refLabel(rp) || '参考图') + '</span><span class="x">✕</span>';
        rc.querySelector('.x').addEventListener('click', function (e) { e.stopPropagation(); removeRefPath(rp); });
        addHoverImage(rc, '/api/asset-file/' + encodeURIComponent(rp), (rp || '').split('/').pop());
        bar.appendChild(rc);
      });
      if (names.length || refPats.length) { var lb = document.createElement('span'); lb.className = 'rlabel'; lb.textContent = '本段引用'; bar.appendChild(lb); }
      names.slice(0, 20).forEach(function (nm) {
        var a = findAsset(nm); var img = (a && d.assetImg) ? d.assetImg(a.kind, a.asset) : '';
        var card = document.createElement('span'); card.className = 'rcard'; card.setAttribute('data-name', nm);
        card.innerHTML = (img ? '<img src="' + esc(img) + '">' : '') + esc(nm) + '<span class="x">✕</span>';
        card.querySelector('.x').addEventListener('click', function (e) { e.stopPropagation(); removeRef(nm); });
        addHoverPreview(card, function () { return a; });
        bar.appendChild(card);
      });
    }
    function removeRef(nm) { if (hover) hover.style.display = 'none'; var sk = nm.replace(/"/g, '\\"'); ta.querySelectorAll('.pue-inline-c[data-name="' + sk + '"]').forEach(function (sp) { sp.remove(); }); onTextChangeInternal(); }
    function removeRefPath(rp) { if (hover) hover.style.display = 'none'; var sk = rp.replace(/"/g, '\\"'); ta.querySelectorAll('.pue-inline-c[data-refpath="' + sk + '"]').forEach(function (sp) { sp.remove(); }); onTextChangeInternal(); }
    function addHoverPreview(el, getA) { el.addEventListener('mouseenter', function (e) { var a = getA && getA(); if (!a) return; var img = d.assetImg ? d.assetImg(a.kind, a.asset) : ''; var nm = ((a.asset.prefix || '') + '-' + (a.asset.variant_name || '')).replace(/-$/, ''); if (!img) return; hover.innerHTML = '<img src="' + esc(img) + '"><div class="nm">' + esc(nm) + '</div>'; hover.style.display = 'block'; moveHover(e); }); el.addEventListener('mousemove', moveHover); el.addEventListener('mouseleave', function () { hover.style.display = 'none'; }); }
    function addHoverImage(el, imageSrc, name) { el.addEventListener('mouseenter', function (e) { if (!imageSrc) return; hover.innerHTML = '<img src="' + esc(imageSrc) + '"><div class="nm">' + esc(name || '') + '</div>'; hover.style.display = 'block'; moveHover(e); }); el.addEventListener('mousemove', moveHover); el.addEventListener('mouseleave', function () { hover.style.display = 'none'; }); }
    function moveHover(e) { var r = hover.getBoundingClientRect(); var x = e.clientX + 16, y = e.clientY + 16; if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 16; if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 16; hover.style.left = x + 'px'; hover.style.top = y + 'px'; }

    // ── 组装 ──
    function assemble() {
      var parts = [];
      if (pv('style') && pv('style') !== '画风') parts.push('【画风】' + pv('style'));
      var text = serialize(); if (text) parts.push(text);
      if (selState.ratio || selState.size || selState.mp) parts.push('【尺寸】' + (selState.ratio || selState.size || selState.mp) + (selState.seed ? ' · seed=' + selState.seed : ''));
      return parts.join('\n');
    }
    function getReferences() {
      // 从文本区收集 @ 图片资产（供工作流输入槽）；@名字→资产→图像
      var text = serializeRaw(); var refs = []; var seen = {};
      var re = /<((?:role|scene|prop))\b[^>]*>([^<]+)<\/\1>/g, m;
      while ((m = re.exec(text))) { var nm = (m[2] || '').replace(/-$/, '').trim(); if (!nm || seen[nm]) continue; var a = findAsset(nm); if (a && a.asset && a.asset.images && a.asset.images[0]) { seen[nm] = 1; refs.push({ kind: 'image', name: nm, path: a.asset.images[0].path }); } }
      // v1.53 参考类资产：用户在文本中引用的 <reference> 芯片 → 参考图进工作流
      var reRef = /<reference>([^<]+)<\/reference>/g, mr; while ((mr = reRef.exec(text))) { var rp = (mr[1] || '').trim(); if (rp && !seen[rp]) { seen[rp] = 1; refs.push({ kind: 'image', name: '参考图', path: rp }); } }
      return refs;
    }
    function validateRefs(refs) {
      var slots = d.workflowInputSlots ? d.workflowInputSlots(selState.workflow) : { images: 0, audio: 0 };
      var imgs = refs.filter(function (r) { return r.kind === 'image'; });
      if (imgs.length > (slots.images == null ? 99 : slots.images)) {
        toast('⚠️ 当前工作流最多允许 ' + (slots.images == null ? '不限' : slots.images) + ' 张参考图，已忽略多余的图', 'error');
        return imgs.slice(0, slots.images == null ? imgs.length : slots.images);
      }
      return refs;
    }

    // ── 悬浮风格库 ──
    function openStylePop(anchor) {
      stylepop.style.display = 'flex';
      var r = anchor.getBoundingClientRect(); var w = 360, h = 420; var l = r.left; if (l + w > window.innerWidth - 8) l = Math.max(8, window.innerWidth - w - 8); var t = r.bottom + 6; if (t + h > window.innerHeight - 8) t = Math.max(8, r.top - h - 6); stylepop.style.left = l + 'px'; stylepop.style.top = t + 'px'; renderStyleGrid();
    }
    function renderStyleGrid() {
      var q = (stylepop.querySelector('[data-a="ssearch"]').value || '').trim().toLowerCase(); var grid = stylepop.querySelector('[data-a="sgrid"]'); grid.innerHTML = '';
      var type = stylepop.getAttribute('data-type') || 'all';
      var list = styles.filter(function (s) { return (type === 'all' || s.type === type) && (!q || (s.name || '').toLowerCase().indexOf(q) >= 0); });
      if (!list.length) { grid.innerHTML = '<div class="sc" style="width:100%">无匹配画风</div>'; }
      list.forEach(function (s) {
        var c = document.createElement('div'); c.className = 'sc' + (pv('style') === (s.name || s.id) ? ' sel' : '');
        // 画风项配 SVG 示例图（PALETTE.styleImg），无则占位色块
        var img = s.img || ((window.PALETTE && window.PALETTE.styleImg) ? window.PALETTE.styleImg(s, 120, 80) : '');
        c.innerHTML = (img ? '<img src="' + esc(img) + '">' : '<div class="ph2">🎨</div>') + '<div>' + esc(s.name || s.id) + '</div>';
        c.addEventListener('click', function () { setPv('style', s.name || s.id); selState.style = s.name || s.id; stylepop.style.display = 'none'; onDraft(); });
        grid.appendChild(c);
      });
    }

    // ── 模型-工作流 二级级联（平台 → 工作流）──
    function ensureCloudStatus(cb) {
      // 缓存 3 秒（够短，设置页改完令牌后立刻生效）
      try {
        var now = Date.now();
        if (!window.__msCloudReq || (now - window.__msCloudReq.at) > 3000) {
          window.__msCloudReq = { at: now, p: fetch('/api/cloud/status').then(function (r) { return r.json(); }) };
        }
        Promise.resolve(window.__msCloudReq.p).then(function (j) {
          // ★ 用户 2026-10-01：不再按 route_mode 收回云端选择（Modelscope 常显）；
          //    这里只取"是否配了令牌"与云端模型清单。
          window.__msCloud = { ready: !!j.ready, mode: j.route_mode || 'off', models: j.models || {} };
          if (cb) cb();
        }).catch(function () { if (cb) cb(); });
      } catch (e) { if (cb) cb(); }
    }
    function getProviders() {
      if (d.providers && d.providers.length) return d.providers;
      var list = [{ id: 'comfyui', name: '本地 ComfyUI' }];
      // ★ 用户 2026-10-01：**Modelscope 一直显示**（不再受"设置里是否允许"门控 —— 该开关已随批一下线）。
      //   视频编辑器仍例外（云端跑不了 H3 工作流）。未配令牌时选它会由后端 400 给出自然语言提示。
      if (genKind === 'image') list.push({ id: 'modelscope', name: 'Modelscope' });
      return list;
    }
    function cloudModelItems() {
      // ★ ④ F5c：云端"工作流"列 = 云端模型（设置里配的文生图/图生图模型），选中即本次用哪个模型
      var m = (window.__msCloud && window.__msCloud.models) || {};
      var t2i = m.t2i || 'Tongyi-MAI/Z-Image-Turbo';
      var i2i = m.i2i || 'Qwen/Qwen-Image-Edit-2509';
      function short(id) { return String(id).split('/').pop(); }
      return [{ name: 'ms:' + t2i, title: short(t2i) + '（文生图）' },
              { name: 'ms:' + i2i, title: short(i2i) + '（图生图·编辑）' }];
    }
    function getWfByProv() {
      if (d.wfByProv) return d.wfByProv;
      var defaultWf = (d.workflows && d.workflows.length) ? d.workflows : [{ name: 'Z-Image.json', title: 'Z-Image（文生图）' }, { name: 'Flux2-Klein.json', title: 'Flux2-Klein（参考迁移）' }, { name: '2511.json', title: '2511（参考图生图）' }];
      return { comfyui: defaultWf, modelscope: cloudModelItems() };
    }
    var selProv = null;
    function openCascade(anchor) {
      /* ★ 用户 2026-10-01：打开时**默认高亮当前平台** —— 右列直接列出该平台的项，
         与左侧药丸的显示一致（原来 selProv=null ⇒ 右列恒显示"无工作流"，用户以为坏了）。 */
      selProv = (selState.prov === 'modelscope') ? 'modelscope' : 'comfyui';
      cascade.style.display = 'flex'; var r = anchor.getBoundingClientRect(); var w = 300, h = 260; var l = r.left; if (l + w > window.innerWidth - 8) l = Math.max(8, window.innerWidth - w - 8); var t = r.bottom + 6; if (t + h > window.innerHeight - 8) t = Math.max(8, r.top - h - 6); cascade.style.left = l + 'px'; cascade.style.top = t + 'px';
      renderCascade();
      ensureCloudStatus(function () { if (cascade.style.display === 'flex') renderCascade(); });   // 状态未取到时补画一次
    }
    function renderCascade() {
      var provCol = cascade.querySelector('[data-c="prov"]'); provCol.innerHTML = '<div class="hd">平台</div>';
      getProviders().forEach(function (p) {
        var it = document.createElement('div'); it.className = 'it' + (selProv === p.id ? ' sel' : ''); it.textContent = p.name;
        it.addEventListener('click', function () { selProv = p.id; renderCascade(); });   // 选平台 → 右列列出该平台可选的工作流/模型
        provCol.appendChild(it);
      });
      var wfCol = cascade.querySelector('[data-c="wf"]'); wfCol.innerHTML = '<div class="hd">工作流（' + (selProv ? (getProviders().find(function (x) { return x.id === selProv; }) || {}).name : '选平台') + '）</div>';
      var wfs = selProv ? (getWfByProv()[selProv] || []) : [];
      if (!wfs.length) { wfCol.innerHTML += '<div class="it d">无工作流</div>'; }
      wfs.forEach(function (w) {
        var nm = w.name || w; var ttl = w.title || (String(nm).split('/').pop());
        var it = document.createElement('div'); it.className = 'it' + (selState.workflow === nm ? ' sel' : ''); it.textContent = ttl;
        it.addEventListener('click', function () {
          if (String(nm).indexOf('ms:') === 0) {              // ★ 云端模型（本次用哪个云端模型）
            selState.prov = 'modelscope'; selState.cloudModel = String(nm).slice(3);
            setPv('workflow', String(selState.cloudModel).split('/').pop());   // 药丸只显示模型短名（宽度友好）
            cascade.style.display = 'none'; onDraft(); return;
          }
          selState.workflow = nm; selState.prov = selProv; selState.cloudModel = '';
          setPv('workflow', ttl); cascade.style.display = 'none'; onDraft();
        });
        wfCol.appendChild(it);
      });
    }

    // ── 清晰度+比例 面板（参考图1）──
    var RATIOS = (d.ratios && d.ratios.length) ? d.ratios : [
      { v: '', label: '自动', shape: '' }, { v: '16:9', label: '16:9', shape: 'w' }, { v: '21:9', label: '21:9', shape: 'wide' }, { v: '9:16', label: '9:16', shape: 'tall' },
      { v: '4:3', label: '4:3', shape: 'w' }, { v: '3:4', label: '3:4', shape: 'tall' }, { v: '1:1', label: '1:1', shape: 'sq' }
    ];
    function openSizePop(anchor) {
      var rt = sizePop.querySelector('[data-s="ratio"]'); rt.innerHTML = '';
      RATIOS.forEach(function (r) {
        var it = document.createElement('span'); it.className = 'opt' + (selState.ratio === r.v ? ' sel' : '');
        var shape = r.shape ? '<span class="shape" style="' + (r.shape === 'wide' ? 'width:16px;height:8px' : r.shape === 'tall' ? 'width:8px;height:14px' : r.shape === 'sq' ? 'width:11px;height:11px' : '') + '"></span>' : '';
        it.innerHTML = shape + '<span>' + esc(r.label) + '</span>';
        it.addEventListener('click', function (e) { e.stopPropagation(); selState.ratio = r.v; setPv('aspectsize', (selState.ratio || '自动')); selState.size = '1K'; sizePop.querySelectorAll('[data-s="ratio"] .opt').forEach(function (x) { x.classList.remove('sel'); }); it.classList.add('sel'); onDraft(); }); rt.appendChild(it);
      });
      sizePop.style.display = 'flex'; var r = anchor.getBoundingClientRect(); var w = 340, h = 340; var l = r.left; if (l + w > window.innerWidth - 8) l = Math.max(8, window.innerWidth - w - 8); var t = r.bottom + 6; if (t + h > window.innerHeight - 8) t = Math.max(8, r.top - h - 6); sizePop.style.left = l + 'px'; sizePop.style.top = t + 'px';
    }

    // ── 交互 ──
    function onTextChangeInternal() { refreshRefs(); if (cb.onTextChange) cb.onTextChange(serialize()); onDraft(); if (typeof refreshPreview === 'function') refreshPreview(); }
    function onDraft() { var dr = { text: serializeRaw(), sel: selState, refPath: refPath, textNL: serialize() }; if (cb.onDraft) cb.onDraft(dr); }
    function uploadMenu(anchor) {
      menu.style.display = 'block'; var r = anchor.getBoundingClientRect(); menu.style.left = r.left + 'px'; menu.style.top = (r.bottom + 4) + 'px';
    }
    function doUpload() { var input = document.createElement('input'); input.type = 'file'; input.accept = 'image/*'; input.onchange = function () { var f = input.files[0]; if (!f) return; var fd = new FormData(); fd.append('file', f); fetch('/api/ref-upload', { method: 'POST', body: fd }).then(function (r) { if (!r.ok) return r.json().then(function (x) { throw new Error(x.detail || '失败'); }).catch(function (e) { throw e; }); return r.json(); }).then(function (rec) { toast('已上传参考图（登记为参考类资产）', 'success'); if (d.onRefsChanged) d.onRefsChanged(rec); 
      // v1.53 修复：上传成功后自动设置为当前参考图（提升用户体验）
      if (rec && rec.path) {
        refPath = rec.path;
        // 自动插入参考图芯片到编辑器
        insertAt(rec.path, 'reference');
      }
      refreshRefs();
      onDraft();
    }).catch(function (e) { toast(e.message, 'error'); }); }; input.click(); }
    function fromLibrary() { if (d.openLibraryPick) d.openLibraryPick(function (path) { refPath = path; toast('已从资产库选择', 'success'); insertAt(path, 'reference'); refreshRefs(); onDraft(); }); }
    function ai() { var raw = serializeRaw(); if (!raw.trim()) { toast('请先输入提示词', 'error'); return; } if (cb.onAIOptimize) { cb.onAIOptimize({ text: raw, setText: setText }); return; } if (!window.api || !window.costRunDialog || !window.streamLLM) { toast('AI 组件未就绪', 'error'); return; } var holders = [], markRe = /<((?:role|scene|prop))\b[^>]*>[^<]*<\/\1>|<duration-ms>[^<]*<\/duration-ms>|<camera-move>[^<]*<\/camera-move>|<shot-size>[^<]*<\/shot-size>/g; var phText = raw.replace(markRe, function (mm) { holders.push(mm); return '@@REF' + holders.length + '@@'; }); window.api('/optimize-prompt/estimate', { method: 'POST', headers: { 'Content-Type': 'application/json; charset=utf-8' }, body: JSON.stringify({ text: phText }) }).then(function (est) { window.costRunDialog({ title: 'AI 润色提示词', est: est, confirmText: '开始润色', run: function (progress) { var acc = ''; progress('⏳ 正在润色…'); return window.streamLLM('optimize_prompt', { text: phText }, function (dd) { acc += dd; progress('⏳ ' + esc(acc)); }).then(function (r) { var opt = r.optimized || phText; holders.forEach(function (mm, i) { opt = opt.split('@@REF' + (i + 1) + '@@').join(mm); }); setText(opt); return { summary: '✅ 已润色（内联组件已保留）' }; }); } }); }).catch(function (e) { toast(e.message, 'error'); }); }
    function gen() { var prompt = assemble(); if (!prompt.trim()) { toast('请先输入提示词', 'error'); return; } var refs = validateRefs(getReferences()); var payload = { prompt: prompt, text: serializeRaw(), refs: refs, params: { workflow: selState.workflow, prov: selState.prov, ratio: selState.ratio, size: selState.size, mp: selState.mp, duration: parseFloat(selState.duration) || 5, seed: selState.seed ? parseInt(selState.seed) : null, refPath: refPath, cloud: selState.prov === 'modelscope', cloud_model: selState.cloudModel || '' } }; var b = root.querySelector('[data-a="gen"]'); if (b) { b.classList.add('busy'); b.textContent = '⏳'; } var task = cb.onGenerate ? Promise.resolve().then(function () { return cb.onGenerate(payload); }) : Promise.resolve(); task.then(function () { if (b) { b.classList.remove('busy'); b.textContent = '↑'; var t = b.getAttribute('title'); b.title = t; } }).catch(function (e) { toast('生成失败：' + ((e && e.message) || e)); if (b) { b.classList.remove('busy'); b.textContent = '↑'; } }); }
    function save() { if (cb.onSave) cb.onSave(serialize()); }

    function setText(t) { var markerRe = /<((?:role|scene|prop))\b[^>]*>([^<]+)<\/\1>|<duration-ms>([^<]+)<\/duration-ms>|<camera-move>([^<]+)<\/camera-move>|<shot-size>([^<]+)<\/shot-size>|<reference>([^<]+)<\/reference>/g; ta.innerHTML = ''; if (!t) { onTextChangeInternal(); return; } var last = 0, m, frag = document.createDocumentFragment(); while ((m = markerRe.exec(t)) !== null) { if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index))); var mm = m[0]; var cls = '', label = '', kind = ''; if (/^<reference>/.test(mm)) { cls = 'role'; label = '🖼 '; kind = 'reference'; } else if (/^<role\b/.test(mm)) { cls = 'role'; label = '👤 '; kind = 'role'; } else if (/^<duration-ms>/.test(mm)) { cls = 'dur'; label = '⏱ '; kind = 'dur'; } else if (/^<camera-move>/.test(mm)) { cls = 'cam'; label = '🎥 '; kind = 'cam'; } else { cls = 'shot'; label = '🖼 '; kind = 'shot'; } var inner = mm.replace(/^<[^>]*>/, '').replace(/<\/[^>]*>$/, ''); var sp = document.createElement('span'); sp.className = 'pue-inline-c ' + cls; sp.contentEditable = 'false'; sp.setAttribute('data-marker', mm); sp.setAttribute('data-kind', kind); if (kind === 'reference') { sp.setAttribute('data-refpath', inner); sp.appendChild(document.createTextNode('🖼 ' + refLabel(inner))); } else if (kind === 'role') { sp.setAttribute('data-name', inner.replace(/-$/, '')); sp.appendChild(document.createTextNode(label + inner.replace(/-$/, ''))); } else { var disp = (kind === 'dur') ? (parseFloat(inner) / 1000).toFixed(parseFloat(inner) % 1000 ? 1 : 0) + 's' : inner; sp.appendChild(document.createTextNode(label + disp)); } var a = kind === 'role' ? findAsset(inner.replace(/-$/, '')) : null; addHoverPreview(sp, function () { return a; }); if (kind === 'reference') addHoverImage(sp, '/api/asset-file/' + encodeURIComponent(inner), (inner || '').split('/').pop()); addChangeHandler(sp); frag.appendChild(sp); last = m.index + mm.length; } if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); ta.appendChild(frag); onTextChangeInternal(); }

    // 组装预览（批1.5：👁 按钮 → 序列化三输出面板；实时刷新）
    var previewBox = root.querySelector('[data-a="previewbox"]');
    var previewOpen = false;
    function togglePreview() {
      previewOpen = !previewOpen;
      if (!previewOpen) { previewBox.style.display = 'none'; previewBox.innerHTML = ''; return; }
      refreshPreview();
      previewBox.style.display = 'block';
    }
    function refreshPreview() {
      if (!previewOpen) return;
      var raw = serializeRaw(), nl = serialize(), refs = getReferences();
      previewBox.innerHTML =
        '<b>① 原文（保留标记，存工作流）</b>\n' + esc(raw || '（空）') + '\n\n' +
        '<b>② 自然语言提示词（参考图不混入 ✓）</b>\n' + esc(nl || '（空）') + '\n\n' +
        '<b>③ 工作流入参 refs（参考图只进这里）</b>\n' + (refs.length ? refs.map(function (r) { return esc(r.path || r.name); }).join('\n') : '（无）');
    }

    // 事件
    ta.addEventListener('input', function (e) {
      if (e.data === '@') { atActive = true; checkAt(); }
      else if (atActive) { checkAt(); }
      else { hideAtPop(); }   // 正常打字（非@输入）→ 不弹
      onTextChangeInternal();
    });
    ta.addEventListener('keyup', function (e) { if (e.key === 'Escape') hideAtPop(); });
    ta.addEventListener('blur', function () { setTimeout(onTextChangeInternal, 120); });
    ta.addEventListener('click', function () { hideAtPop(); });
    ta.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideAtPop(); if (e.key === 'Enter' && pop.style.display === 'flex') { var f = pop.querySelector('.arow, .tool-card'); if (f) { e.preventDefault(); f.click(); } } });
    root.querySelector('[data-a="plus"]').addEventListener('click', function (e) { e.stopPropagation(); uploadMenu(e.currentTarget); });
    root.querySelector('[data-a="style"]').addEventListener('click', function (e) { e.stopPropagation(); openStylePop(e.currentTarget); });
    root.querySelector('[data-a="workflow"]').addEventListener('click', function (e) { e.stopPropagation(); openCascade(e.currentTarget); });
    root.querySelector('[data-a="aspectsize"]').addEventListener('click', function (e) { e.stopPropagation(); openSizePop(e.currentTarget); });
    root.querySelector('[data-a="gen"]').addEventListener('click', gen);
    /* ★ ④ F5c：云端不再单独设按钮 —— 走「平台」级联（见 getProviders），允许时才出现 Modelscope；
       这里只预热一次云端状态，让首次打开级联就能正确门控。 */
    ensureCloudStatus(function () { setupWorkflow(); });
    root.querySelector('[data-a="ai"]').addEventListener('click', function (e) { e.stopPropagation(); ai(); });
    root.querySelector('[data-a="preview"]').addEventListener('click', function (e) { e.stopPropagation(); togglePreview(); });
    var saveB = root.querySelector('[data-a="save"]'); if (saveB) saveB.addEventListener('click', save);
    // @ 弹窗 v2：资产/小工具 tab
    pop.querySelectorAll('[data-a2="tabrow"] .tab').forEach(function (t) { t.addEventListener('click', function () { pe2SwitchTab(t.getAttribute('data-a2')); }); });
    // 资产库级联防抖进出（移出资产库/级联短暂延迟收起，进入任一菜单取消）
    var L2el = atP2('L2'), L3el = atP2('L3'), entryEl = atP2('libEntry');
    [L2el, L3el].forEach(function (el) {
      el.addEventListener('mouseenter', pe2ClearHide);
      el.addEventListener('mouseleave', function () { pe2ScheduleHide(); });
    });
    pop.addEventListener('mouseleave', function (e) {
      var to = e.relatedTarget;
      if (to && (pop.contains(to) || L2el.contains(to) || L3el.contains(to))) return;
      pe2ScheduleHide();
    });
    // 级联/预览在弹窗外；hover 资产库入口在 pe2BindLibEntry 已绑
    menu.querySelectorAll('[data-a]').forEach(function (mi) { mi.addEventListener('click', function (e) { e.stopPropagation(); menu.style.display = 'none'; if (mi.getAttribute('data-a') === 'upload') doUpload(); else if (mi.getAttribute('data-a') === 'library') fromLibrary(); }); });
    stylepop.querySelectorAll('.tabs .t').forEach(function (t) { t.addEventListener('click', function () { stylepop.setAttribute('data-type', t.getAttribute('data-t')); stylepop.querySelectorAll('.tabs .t').forEach(function (x) { x.classList.toggle('active', x === t); }); renderStyleGrid(); }); });
    stylepop.querySelector('[data-a="ssearch"]').addEventListener('input', renderStyleGrid);
    document.addEventListener('mousedown', function (e) { if (pop.style.display === 'flex' && !pop.contains(e.target) && !ta.contains(e.target)) hideAtPop(); if (stylepop.style.display === 'flex' && !stylepop.contains(e.target) && !root.querySelector('[data-a="style"]').contains(e.target)) stylepop.style.display = 'none'; if (menu.style.display === 'block' && !menu.contains(e.target) && !root.querySelector('[data-a="plus"]').contains(e.target)) menu.style.display = 'none'; if (cascade.style.display === 'flex' && !cascade.contains(e.target) && !root.querySelector('[data-a="workflow"]').contains(e.target)) cascade.style.display = 'none'; if (sizePop.style.display === 'flex' && !sizePop.contains(e.target) && !root.querySelector('[data-a="aspectsize"]').contains(e.target)) sizePop.style.display = 'none'; });

    // 初始化
    setText(draft && draft.text != null ? draft.text : (cfg.initialText || ''));
    if (draft && draft.sel) { selState = Object.assign(selState, draft.sel); }
    if (draft && draft.refPath) refPath = draft.refPath;
    // 批1.5 #2 自动复用可视化：草稿 refPath 存在但文本无对应 <reference> 芯片 → 自动插入芯片 + toast（用户可见"自带出了哪张参考图"）
    if (refPath) {
      var hasRefChip = serializeRaw().indexOf('<reference>' + refPath + '</reference>') >= 0;
      if (!hasRefChip) { insertAt(refPath, 'reference'); toast('已自动复用上次参考图', 'info'); }
    }
    if (genKind === 'video') { } // 视频参数后续
    setupWorkflow();
    var prev = root.querySelector('[data-a="style"] span.pv'); if (selState.style) { setPv('style', selState.style); }
    onDraft();

    return { handle: { getText: serialize, getRaw: serializeRaw, getAssembled: assemble, getRefs: getReferences, getParams: function () { return { workflow: selState.workflow, ratio: selState.ratio, size: selState.size, mp: selState.mp, seed: selState.seed, refPath: refPath }; }, setText: setText, insertAsset: function (name, kind) { insertAt(name, kind || ''); }, instAtset: null, insertAssetAt: function (name, kind, x, y) { insertAt(name, kind || '', x, y); }, setCaretAt: function (x, y) { setCaretAt(x, y); }, gen: gen, save: save, setSelectors: function (s) { Object.assign(selState, s || {}); setupWorkflow(); var e = root.querySelector('[data-pv="style"]'); if (e && selState.style) e.textContent = selState.style; }, setRefPath: function (p) { refPath = p || ''; refreshRefs(); onDraft(); }, destroy: function () { if (pop && pop.parentNode) pop.parentNode.removeChild(pop); if (stylepop && stylepop.parentNode) stylepop.parentNode.removeChild(stylepop); if (menu && menu.parentNode) menu.parentNode.removeChild(menu); if (hover && hover.parentNode) hover.parentNode.removeChild(hover); if (cascade && cascade.parentNode) cascade.parentNode.removeChild(cascade); if (sizePop && sizePop.parentNode) sizePop.parentNode.removeChild(sizePop); if (changeMenu && changeMenu.parentNode) changeMenu.parentNode.removeChild(changeMenu); if (prev2 && prev2.parentNode) prev2.parentNode.removeChild(prev2); root.innerHTML = ''; }, _root: root } };
  }

  window.createPromptEditor = createPromptEditor;
})();
