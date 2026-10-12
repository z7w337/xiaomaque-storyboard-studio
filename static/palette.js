// ============================================================
// 剧本分镜工坊 · 共享画风库（palette）
// 批2 生产化：统一 index/assets/scene 三处画风数据 + 预开发 SVG 示例图
// 预开发·参考用（style-ref.html）→ 生产化到正式工坊。
// 画风库无 CRUD（预置，规格表定稿即预置）；真实示例图随阶段 6 补齐，此处用内联 SVG 渐变+纹理示意。
// 共享组件：改本文件必须 bump 引用页面的 ?v=（index/assets/scene）。
// ============================================================
(function (global) {
  'use strict';

  // 预置画风（对齐预开发 style-ref.html 8 风格；id 用关键字=art_style 存储值，不破坏现有）
  var STYLES = [
    { id: '写实电影感', name: '写实电影感', type: 'real', desc: '胶片颗粒 · 光影写实 · 电影级质感', color: '#78716c', color2: '#292524', pat: 'film' },
    { id: '赛博朋克', name: '赛博朋克', type: 'real', desc: '霓虹夜景 · 高饱和对比 · 未来都市', color: '#7c3aed', color2: '#0f172a', pat: 'film' },
    { id: '日系动漫', name: '日系动漫', type: '2d', desc: '赛璐璐上色 · 日系线条 · 明快', color: '#60a5fa', color2: '#1e40af', pat: 'anime' },
    { id: '水墨国风', name: '水墨国风', type: '2d', desc: '水墨晕染 · 留白意境 · 写意', color: '#57534e', color2: '#111827', pat: 'ink' },
    { id: '古风', name: '古风', type: '2d', desc: '工笔重彩 · 古装意象 · 典雅', color: '#d97706', color2: '#7c2d12', pat: 'anime' },
    { id: '手绘插画', name: '手绘插画', type: '2d', desc: '铅笔质感 · 手绘笔触 · 温暖', color: '#f59e0b', color2: '#b45309', pat: 'ink' },
    { id: '低多边形', name: '低多边形', type: '3d', desc: '几何切面 · 明快色块 · 立体构成', color: '#10b981', color2: '#065f46', pat: 'film' },
    { id: '像素风', name: '像素风', type: '3d', desc: '复古像素 · 8-bit 质感 · 游戏记忆', color: '#ef4444', color2: '#7f1d1d', pat: 'pixel' }
  ];
  var TYPE_LABEL = { real: '真人', '2d': '2D', '3d': '3D' };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  // v1.8 画风卡"图"化：SVG 渐变 + 场景纹理 + 画风名水印（正式=阶段 6 真实图）
  function styleImg(s, w, h) {
    w = w || 210; h = h || 100;
    var c1 = s.color, c2 = s.color2 || '#111827';
    var pat = s.pat === 'film' ? '<rect y="' + (h - 3) + '" width="' + w + '" height="3" fill="rgba(0,0,0,.25)"/><rect x="' + (w * 0.12) + '" y="' + (h * 0.3) + '" width="' + (w * 0.5) + '" height="' + (h * 0.5) + '" fill="rgba(255,255,255,.14)"/>' :
      s.pat === 'anime' ? '<circle cx="' + (w * 0.75) + '" cy="' + (h * 0.35) + '" r="' + (h * 0.22) + '" fill="rgba(255,255,255,.3)"/><rect x="' + (w * 0.08) + '" y="' + (h * 0.7) + '" width="' + (w * 0.6) + '" height="3" fill="rgba(255,255,255,.2)"/>' :
      s.pat === 'ink' ? '<circle cx="' + (w * 0.3) + '" cy="' + (h * 0.4) + '" r="' + (h * 0.4) + '" fill="rgba(255,255,255,.12)"/><path d="M0 ' + (h * 0.85) + ' C ' + (w * 0.3) + ' ' + (h * 0.7) + ', ' + (w * 0.6) + ' ' + (h * 0.9) + ', ' + w + ' ' + (h * 0.75) + '" stroke="rgba(255,255,255,.25)" fill="none" stroke-width="2"/>' :
      s.pat === 'pixel' ? '<rect x="' + (w * 0.2) + '" y="' + (h * 0.3) + '" width="8" height="8" fill="rgba(255,255,255,.4)"/><rect x="' + (w * 0.3) + '" y="' + (h * 0.3) + '" width="8" height="8" fill="rgba(255,255,255,.2)"/><rect x="' + (w * 0.25) + '" y="' + (h * 0.45) + '" width="8" height="8" fill="rgba(255,255,255,.3)"/>' :
      '<circle cx="' + (w * 0.6) + '" cy="' + (h * 0.4) + '" r="' + (h * 0.16) + '" fill="rgba(255,255,255,.3)"/>';
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">' +
      '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="' + c1 + '"/><stop offset="1" stop-color="' + c2 + '"/></linearGradient></defs>' +
      '<rect width="' + w + '" height="' + h + '" fill="url(#g)"/>' + pat +
      '<text x="' + (w / 2) + '" y="' + (h / 2 + 6) + '" font-size="16" fill="rgba(255,255,255,.95)" text-anchor="middle" font-family="sans-serif" font-weight="700">' + esc(s.name || s.id || '') + '</text></svg>';
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  global.PALETTE = { STYLES: STYLES, TYPE_LABEL: TYPE_LABEL, styleImg: styleImg, esc: esc };
})(typeof window !== 'undefined' ? window : this);
