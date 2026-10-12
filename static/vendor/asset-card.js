// 
// 预开发·参考用（v1.3 · 2026-08-28）
// 统一资产卡片组件（AssetCard）—— 全局卡片统一、共享组件化
// 
// 设计决策（用户裁定 v1.3）：参考类/未分类/普通资产卡视觉 90% 相同，
// 差异是"行为配置"而非"结构" → 统一为单一 AssetCard + options 驱动。
// 本组件取代 components/reference-card.* 与 components/unassigned-card.*
// （两者已并入本组件，仅存档参考，不再单独使用）。
//
// options 结构：
// {
//   kind: 'character' | 'scene' | 'prop' | 'reference' | 'unassigned',  // 卡种
//   data: { id, path, thumbnailUrl, name, sub, ref_count, ref_sources }, // 数据（name/sub 可覆盖默认派生）
//   icons: {  // 四角图标配置（缺省按 kind 给默认）
//     tl: { icon: '⋮⋮', title: '…', action: 'drag' },
//     tr: { icon: '✎' | '🕘' | '⬆' | '🏷️', title: '…', action: 'jump'|'hist'|'upload'|'bind' },
//     br: { icon: '🗑' | '⋯', title: '…', action: 'delete'|'meta' }
//   },
//   onClick: function(card),      // 卡面点击回调（普通资产=浮出提示词编辑器；参考/未分类=null）
//   onDragStart: fn, onDragEnd: fn, // 拖拽回调（未分类=归类挂载；普通=排序）
//   editable: true,               // 可编辑（⋯进元数据）；参考/未分类=false
//   zero: false                   // 参考类计数=0 → 标红"⚠️ 无用可删除"
// }
//
// 数据对接（预开发标注，v4-flash 落地核实）：
// - 普通资产：GET /{kind} → items[]（images[0].path 为缩略图）
// - 参考类：参考类资产独立列表端点（占位待确认，如 /api/assets/list?kind=reference）；删除 DELETE /api/references/{id}
// - 未分类：GET /api/assets/index/unclassified → unclassified_entries[]；归类 PUT /{kind}/{id}
// - 图片：/api/asset-file/{path}；全局模态 nav.js confirmDialog；toast
//

(function (global) {
  'use strict';

  // 各卡种默认四角图标
  var DEFAULT_ICONS = {
    character: {
      tl: { icon: '⋮⋮', title: '拖动排序（按住 ⋮⋮ 才可拖动）', action: 'drag' },
      tr: { icon: '🕘', title: '历史版本', action: 'hist' },
      br: { icon: '⋯', title: '资产信息', action: 'meta' }
    },
    scene: {
      tl: { icon: '⋮⋮', title: '拖动排序（按住 ⋮⋮ 才可拖动）', action: 'drag' },
      tr: { icon: '🕘', title: '历史版本', action: 'hist' },
      br: { icon: '⋯', title: '资产信息', action: 'meta' }
    },
    prop: {
      tl: { icon: '⋮⋮', title: '拖动排序（按住 ⋮⋮ 才可拖动）', action: 'drag' },
      tr: { icon: '🕘', title: '历史版本', action: 'hist' },
      br: { icon: '⋯', title: '资产信息', action: 'meta' }
    },
    reference: {
      tl: { icon: '⋮⋮', title: '拖拽（按住 ⋮⋮ 才可拖动）', action: 'drag' },
      tr: { icon: '🔗', title: '查看被引用资产', action: 'view' },
      br: { icon: '🗑', title: '删除参考类资产', action: 'delete' }
      // v1.8 用户裁定：跨标签拖拽取消，改点击式——参考类右上 🔗=查看被引用资产；计数+1 走生成流程（/api/references/use）
    },
    unassigned: {
      tl: { icon: '⋮⋮', title: '拖拽（按住 ⋮⋮ 才可拖动）', action: 'drag' },
      tr: { icon: '🏷️', title: '归类/绑定到资产', action: 'bind' },
      br: { icon: '🗑', title: '删除该未分类资产', action: 'delete' }
      // v1.8 用户裁定：跨标签拖拽取消，改点击式——未分类右上 🏷️=归类选择器
    }
  };

  function AssetCard(container, options) {
    this.container = container;
    this.options = options || {};
    this.kind = this.options.kind || 'character';
    this.data = this.options.data || {};
    this.el = null;
    this.render();
    this.bindEvents();
  }

  AssetCard.prototype._fileName = function (p) {
    return String(p || '').split('/').pop() || '未命名';
  };

  // 派生显示字段（name/sub，options.data 可覆盖）
  AssetCard.prototype._name = function () {
    if (this.data.name != null) return this.data.name;
    return this._fileName(this.data.path);
  };
  AssetCard.prototype._sub = function () {
    if (this.data.sub != null) return this.data.sub;
    switch (this.kind) {
      case 'character': return '形象';
      case 'scene': return '视角';
      case 'prop': return '所在场景';
      case 'reference': {
        var cnt = Number(this.data.ref_count) || 0;
        return cnt === 0 ? '⚠️ 无用可删除' : '被 ' + cnt + ' 个资产引用';
      }
      case 'unassigned': return '未分类 · 点 🏷️ 归类';
      default: return '';
    }
  };

  AssetCard.prototype.render = function () {
    var d = this.data;
    var icons = Object.assign({}, DEFAULT_ICONS[this.kind] || {}, this.options.icons || {});
    var zero = !!(this.options.zero || (this.kind === 'reference' && (Number(d.ref_count) || 0) === 0));
    var img = d.thumbnailUrl || ('/api/asset-file/' + encodeURIComponent(d.path || ''));
    var name = this._name();
    var sub = this._sub();

    var iconHTML = '';
    // 左上 tl（拖拽把手）
    if (icons.tl) {
      iconHTML += '<button class="ac-icon ac-tl" data-action="' + escAttr(icons.tl.action || 'drag') + '" title="' + escAttr(icons.tl.title || '拖拽') + '">' + esc(icons.tl.icon) + '</button>';
    }
    // 右上 tr
    if (icons.tr) {
      iconHTML += '<button class="ac-icon ac-tr" data-action="' + escAttr(icons.tr.action || '') + '" title="' + escAttr(icons.tr.title || '') + '">' + esc(icons.tr.icon) + '</button>';
    }
    // 左下 bl（B 批：四角统一——左下=查看引用归属）
    if (icons.bl) {
      iconHTML += '<button class="ac-icon ac-bl" data-action="' + escAttr(icons.bl.action || '') + '" title="' + escAttr(icons.bl.title || '') + '">' + esc(icons.bl.icon) + '</button>';
    }
    // 右下 br
    if (icons.br) {
      iconHTML += '<button class="ac-icon ac-br" data-action="' + escAttr(icons.br.action || '') + '" title="' + escAttr(icons.br.title || '') + '">' + esc(icons.br.icon) + '</button>';
    }

    this.container.innerHTML =
      '<div class="asset-card' + (zero ? ' zero' : '') + '" data-kind="' + escAttr(this.kind) + '" data-id="' + escAttr(d.id || '') + '" data-path="' + escAttr(d.path || '') + '">' +
        '<div class="ac-thumb">' +
          '<img src="' + esc(img) + '" alt="' + escAttr(name) + '" class="ac-img" loading="lazy">' +
          iconHTML +   // 四角图标锚定到图片区（tl 左上/tr 右上/bl 左下/br 右下），避免落到下方文字区
        '</div>' +
        '<div class="ac-body">' +
          '<div class="ac-name" title="' + escAttr(d.path || name) + '">' + esc(name) + '</div>' +
          (sub ? '<div class="ac-sub' + (zero ? ' ac-count-label' : '') + '">' + esc(sub) + '</div>' : '') +
          (this.kind === 'reference' || this.kind === 'unassigned'
            ? '<div class="ac-count"><span class="ac-count-label">引用计数：</span><span class="ac-count">' + (Number(d.ref_count) || 0) + '</span></div>'
            : '') +
        '</div>' +
      '</div>';

    this.el = this.container.firstElementChild;
    this._icons = {
      tl: this.el.querySelector('.ac-tl'),
      tr: this.el.querySelector('.ac-tr'),
      bl: this.el.querySelector('.ac-bl'),
      br: this.el.querySelector('.ac-br')
    };
  };

  AssetCard.prototype.bindEvents = function () {
    var self = this;

    // 卡面点击（onClick 回调；四角图标按钮 stopPropagation 不触发）
    if (this.el) {
      this.el.addEventListener('click', function () {
        if (typeof self.options.onClick === 'function') self.options.onClick(self);
      });
    }

    // 四角图标：统一事件分发（按 data-action）
    Object.keys(this._icons).forEach(function (pos) {
      var btn = self._icons[pos];
      if (!btn) return;
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        self._handleAction(btn.getAttribute('data-action'), btn);
      });
    });

    // 左上 ⋮⋮ 把手：仅把手临时开启 draggable（卡面默认不可拖）
    // v1.4：用 _dragArmed 标志判定（draggable 设在卡片上时 dragstart 的 e.target 是卡片本身，
    //       closest('.ac-tl') 永远取不到把手 → 拖拽被取消；改由把手 mousedown 置位标志）
    if (this._icons.tl && this.el) {
      this._dragArmed = false;
      this._icons.tl.addEventListener('mousedown', function (e) {
        e.stopPropagation();
        self._dragArmed = true;
        self.el.setAttribute('draggable', 'true');
      });
      this.el.addEventListener('dragstart', function (e) {
        if (!self._dragArmed) { e.preventDefault(); return; }
        if (e.dataTransfer) {
          // v1.6：dataTransfer 传 JSON（拖拽会话内保留，跨标签视图切换/源卡 DOM 隐藏不丢失）
          e.dataTransfer.setData('text/plain', JSON.stringify({
            kind: self.kind,
            id: self.data.id || '',
            path: self.data.path || '',
            name: self._name()
          }));
          // 兼容宿主页 drop（场页 onSceneDrop 认 text/asset-name / text/asset-ref）
          e.dataTransfer.setData('text/asset-name', self._name() || self.data.name || '');
          if (self.data.path) e.dataTransfer.setData('text/asset-ref', self.data.path);
          e.dataTransfer.effectAllowed = 'move';
          // 拖拽缩略图作为 dragImage（小偏移，避免整卡遮挡光标）
          var dragImg = self.el.querySelector('.ac-img');
          if (dragImg) e.dataTransfer.setDragImage(dragImg, -10, -10);
          self.el.classList.add('dragging');
          // v1.7：进入拖拽态（宿主页可借此把视图切换改为 opacity 堆叠，避免 display:none 取消 DnD 会话）
          if (global.document && global.document.body) global.document.body.classList.add('ss-dragging');
          if (typeof self.options.onDragStart === 'function') self.options.onDragStart(self);
        }
      });
      this.el.addEventListener('dragend', function () {
        self._dragArmed = false;
        self.el.removeAttribute('draggable');
        self.el.classList.remove('dragging');
        if (global.document && global.document.body) global.document.body.classList.remove('ss-dragging');
        if (typeof self.options.onDragEnd === 'function') self.options.onDragEnd(self);
      });
      this.el.addEventListener('mouseup', function () {
        self._dragArmed = false;
        self.el.removeAttribute('draggable');
      });
    }
  };

  // 四角动作分发（宿主页可覆盖：this.options.onAction）
  AssetCard.prototype._handleAction = function (action, btn) {
    var self = this;
    if (typeof this.options.onAction === 'function') {
      var handled = this.options.onAction(this, action, btn);
      if (handled) return;
    }
    switch (action) {
      case 'jump':
        // ✎ 跳资产库参考类标签（对接点：正式 assets.html?tab=reference）
        if (typeof global.toast === 'function') global.toast('✎ 跳转资产库·参考类标签（预开发示例：assets.html?tab=reference）', 'info');
        break;
      case 'delete': {
        // 🗑 删除（参考类：确认模态，计数>0 拦截"被引用"；未分类：确认后删除）
        var isRef = this.kind === 'reference';
        var cnt = Number(this.data.ref_count) || 0;
        var msg = isRef
          ? '<div style="line-height:1.7">确认删除参考类资产？<br>' +
            '<span style="color:' + (cnt > 0 ? 'var(--ss-error)' : 'var(--ss-text-4)') + ';font-size:12px">' +
            (cnt > 0 ? '⚠️ 该资产被 ' + cnt + ' 个资产引用，后端将拦截并提示"被引用"，不可删除。' : '引用计数为 0（无用可删除），此操作不可撤销。') +
            '</span></div>'
          : '<div style="line-height:1.7">确认删除该未分类资产？<br><span style="color:var(--ss-text-4);font-size:12px">此操作不可撤销。</span></div>';
        if (typeof global.confirmDialog === 'function') {
          global.confirmDialog(isRef ? '删除参考类资产' : '删除未分类资产', msg, function () {
            if (isRef && cnt > 0) { if (typeof global.toast === 'function') global.toast('该参考类资产正被 ' + cnt + ' 个资产引用，无法删除', 'error'); return; }
            if (typeof self.options.onDelete === 'function') self.options.onDelete(self);
            else {
              if (typeof global.toast === 'function') global.toast(isRef ? '🗑 已删除参考类资产（预开发示例）' : '🗑 已删除未分类资产（预开发示例）', 'success');
              if (self.el) self.el.remove();
            }
          }, '删除');
        }
        break;
      }
      case 'meta':
      case 'hist':
      case 'upload':
      case 'bind':
      case 'view':
      case 'zoom':
        // 宿主页处理（元数据/历史/上传/归类/查看被引用/放大预览）
        if (typeof self.options['on' + action[0].toUpperCase() + action.slice(1)] === 'function') {
          self.options['on' + action[0].toUpperCase() + action.slice(1)](self);
        }
        break;
    }
  };

  // 快速定位：命中高亮（未分类用）
  AssetCard.prototype.highlight = function (keyword) {
    if (!this.el) return;
    var nameEl = this.el.querySelector('.ac-name');
    var raw = this._name();
    var k = String(keyword || '').trim();
    if (!k) { nameEl.innerHTML = esc(raw); this.el.style.opacity = ''; return; }
    var idx = raw.toLowerCase().indexOf(k.toLowerCase());
    if (idx >= 0) {
      nameEl.innerHTML = esc(raw.slice(0, idx)) + '<mark>' + esc(raw.slice(idx, idx + k.length)) + '</mark>' + esc(raw.slice(idx + k.length));
      this.el.style.opacity = '';
    } else {
      nameEl.innerHTML = esc(raw);
      this.el.style.opacity = '0.35';
    }
  };

  // 更新数据重渲染
  AssetCard.prototype.updateData = function (newData, newOptions) {
    this.data = Object.assign({}, this.data, newData);
    if (newOptions) this.options = Object.assign({}, this.options, newOptions);
    this.render();
    this.bindEvents();
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function escAttr(s) { return esc(s).replace(/"/g, '&quot;'); }

  // 挂全局（共享组件化：window.AssetCard）
  global.AssetCard = AssetCard;

  // CommonJS 导出（node 语法自检友好）
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AssetCard;
  }
})(typeof window !== 'undefined' ? window : this);
