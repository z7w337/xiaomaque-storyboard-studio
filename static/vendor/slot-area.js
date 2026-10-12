/* 统一槽位区组件（SlotArea）—— v2 升级⓪-2 步2
   做法对齐 vendor/asset-card.js、vendor/task-card.js：自注入样式 + 全局构造函数，单源可复用。

   为什么要有它：
     同一个 A1（动作迁移）有"导航独立页"和"场页"两个入口，参数区不能各写一份。
     槽位**按工作流声明渲染**（见 vendor/workflow-slots.js）：
       声明 1 个槽 → 页面长成单槽页；声明 3 个槽 → 页面长成"人物图/场景图/动作视频"。
     数量浮动靠"可选槽留空"表达，**不增删槽**（顺序稳定，便于对齐"按序注入节点"）。

   用法：
     SlotArea.inject();                                     // 幂等
     var api = SlotArea.create(el, {
       slots: WorkflowSlots.get('ref2va'),
       onAdd: function (slotKey, slot, api) { ...页面处理上传/选库，再调 api.set... },
       onChange: function (values) {}
     });
     api.setValues({ char: [{name, thumb}], scene: [...] });  // 自动填充（场页用）
     api.getValues();   // → { char:[...], scene:[...], motion:[...] }
     api.validate();    // → { ok, errors: [] }
     api.flat();        // → { images:[], videos:[], audios:[] }（按声明顺序，供提交）

   ⚠️ 本步（步2）仍**不接真实上传与生成**：onAdd 由页面给占位反馈；
      视频上传通道、节点注入都属后续（见待补清单）。

   ⚠️ 写注释时的坑（2026-09-18 自检抓到）：**这种块注释里不要再嵌一层块注释** ——
      内层的"注释结束符"会提前把外层注释结束掉，后面半截变成代码，整文件语法错误、页面静默失灵。
      本文件头就踩过一次（连带这次说明又踩一次），已改为不出现该符号的写法。
*/
(function () {
  var INJECTED = false;

  var CSS = `
  .sa-wrap { display: flex; flex-direction: column; gap: 10px; }
  .sa-slot { border: 1px solid var(--ss-border); border-radius: 10px; background: var(--ss-card); padding: 10px 12px; }
  .sa-slot.empty { border-style: dashed; background: var(--ss-bg); }
  .sa-head { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--ss-text-2); flex-wrap: wrap; }
  .sa-label { font-weight: 600; }
  .sa-req { color: var(--ss-error); font-weight: 400; font-size: 11px; }
  .sa-opt { color: var(--ss-text-4); font-weight: 400; font-size: 11px; }
  .sa-spacer { flex: 1; }
  .sa-btn { padding: 3px 10px; font-size: 12px; border-radius: 6px; border: 1px solid var(--ss-border); background: var(--ss-bg); color: var(--ss-text-2); cursor: pointer; }
  .sa-btn:hover { background: var(--ss-border); }
  .sa-btn.add { background: var(--ss-green-600); border-color: var(--ss-green-600); color: #fff; }
  .sa-btn.add:hover { background: var(--ss-green-700); }
  .sa-items { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .sa-item { position: relative; width: 64px; }
  .sa-item .thumb { width: 64px; height: 64px; border-radius: 8px; overflow: hidden; background: var(--ss-bg); border: 1px solid var(--ss-border); display: flex; align-items: center; justify-content: center; font-size: 22px; color: var(--ss-text-4); }
  .sa-item .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .sa-item .nm { font-size: 10px; color: var(--ss-text-3); margin-top: 3px; text-align: center; word-break: break-all; line-height: 1.35; max-height: 26px; overflow: hidden; }
  .sa-item .x { position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%; border: none; background: rgba(17,24,39,.7); color: #fff; font-size: 11px; line-height: 1; cursor: pointer; display: none; align-items: center; justify-content: center; padding: 0; }
  .sa-item:hover .x { display: flex; }
  .sa-hint { font-size: 11px; color: var(--ss-text-4); line-height: 1.6; margin-top: 6px; }
  .sa-err { font-size: 11px; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 7px 10px; line-height: 1.7; display: none; }
  .sa-err.show { display: block; }
  
/* ===== 步4 深色覆盖 ===== */
[data-theme="dark"] .sa-err { background: var(--ss-error-bg); color: var(--ss-error); border-color: #5c2020; }
`;

  var TYPE_THUMB = { video: '🎬', image: '🖼', audio: '🎙' };

  function inject() {
    if (INJECTED || document.getElementById('sa-style')) { INJECTED = true; return; }
    var s = document.createElement('style');
    s.id = 'sa-style';
    s.textContent = CSS;
    document.head.appendChild(s);
    INJECTED = true;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function create(container, cfg) {
    inject();
    cfg = cfg || {};
    var slots = (cfg.slots || []).slice();
    var values = {};        // key -> [{name, thumb, path}]
    slots.forEach(function (s) { values[s.key] = []; });

    var wrap = document.createElement('div');
    wrap.className = 'sa-wrap';
    container.appendChild(wrap);

    var api = {
      el: wrap,
      set: function (key, items) { values[key] = items || []; render(); emit(); },
      setValues: function (obj) {
        Object.keys(obj || {}).forEach(function (k) { if (values[k] !== undefined) values[k] = obj[k] || []; });
        render(); emit();
      },
      getValues: function () {
        var out = {}; slots.forEach(function (s) { out[s.key] = (values[s.key] || []).slice(); }); return out;
      },
      /* 供提交用：按声明顺序压平 + 按类型分组（对齐后端 ref_images / ref_videos） */
      flat: function () {
        var images = [], videos = [], audios = [];
        slots.forEach(function (s) {
          (values[s.key] || []).forEach(function (it) {
            var rec = { slot: s.key, type: s.type, node: s.node, name: it.name, path: it.path || '' };
            if (s.type === 'video') videos.push(rec);
            else if (s.type === 'audio') audios.push(rec);
            else images.push(rec);
          });
        });
        return { images: images, videos: videos, audios: audios };
      },
      validate: function () {
        var errors = [];
        slots.forEach(function (s) {
          var n = (values[s.key] || []).length;
          if (n < (s.min || 0)) errors.push(s.label + ' 至少需要 ' + s.min + ' 个（当前 ' + n + '）');
          if (n > (s.max || 99)) errors.push(s.label + ' 最多 ' + s.max + ' 个（当前 ' + n + '）');
        });
        return { ok: errors.length === 0, errors: errors };
      },
      render: render
    };

    function emit() { if (cfg.onChange) cfg.onChange(api.getValues()); }

    function render() {
      wrap.innerHTML = '';
      slots.forEach(function (s) {
        var items = values[s.key] || [];
        var box = document.createElement('div');
        box.className = 'sa-slot' + (items.length ? '' : ' empty');
        var need = s.required ? '<span class="sa-req">必填</span>' : '';
        var lim = (s.min === s.max) ? (s.min + ' 个') : ((s.min || 0) + '-' + (s.max || '不限') + ' 个');
        var btns = '';
        if (items.length < (s.max || 99)) btns += '<button class="sa-btn add" data-act="add">＋ 添加</button>';
        if (items.length) btns += '<button class="sa-btn" data-act="clear" style="margin-left:6px">清空</button>';
        var html = '<div class="sa-head"><span class="sa-label">' + esc(s.label) + '</span>' + need +
          '<span class="sa-opt">' + lim + '</span><span class="sa-spacer"></span>' + btns + '</div>';
        if (items.length) {
          html += '<div class="sa-items">' + items.map(function (it, i) {
            var th = it.thumb ? '<img src="' + esc(it.thumb) + '" alt="">' : (TYPE_THUMB[s.type] || '📄');
            return '<div class="sa-item" data-i="' + i + '"><div class="thumb">' + th + '</div>' +
              '<div class="nm">' + esc(it.name || '未命名') + '</div>' +
              '<button class="x" data-act="del" title="移除">✕</button></div>';
          }).join('') + '</div>';
        } else if (s.hint) {
          html += '<div class="sa-hint">' + esc(s.hint) + '</div>';
        }
        if (cfg.autoHint && items.length) html += '<div class="sa-hint">' + esc(cfg.autoHint) + '</div>';
        box.innerHTML = html;
        box.querySelectorAll('[data-act]').forEach(function (b) {
          b.addEventListener('click', function (e) {
            e.stopPropagation();
            var act = b.getAttribute('data-act');
            if (act === 'add') { if (cfg.onAdd) cfg.onAdd(s.key, s, api); return; }
            if (act === 'clear') { values[s.key] = []; render(); emit(); return; }
            if (act === 'del') {
              var i = Number(b.parentNode.getAttribute('data-i'));
              values[s.key].splice(i, 1); render(); emit(); return;
            }
          });
        });
        wrap.appendChild(box);
      });
      /* 校验红条（只显示一次，挂在最后） */
      var v = api.validate();
      var err = document.createElement('div');
      err.className = 'sa-err' + (v.ok ? '' : ' show');
      err.textContent = v.ok ? '' : ('还差素材：' + v.errors.join('；'));
      wrap.appendChild(err);
    }

    render();
    return api;
  }

  window.SlotArea = { create: create, inject: inject, TYPES: TYPE_THUMB };
})();
