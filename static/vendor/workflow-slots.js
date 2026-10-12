/* 工作流槽位声明（v2 升级 ⓪-2 步2 建立 · ⓪-3 步2 改为**读后端单一源**）

   ★ 2026-09-19 用户拍板：**声明来源统一 = main.py 权威 + 后端出端点，前端读它渲染**
     —— 不是"两边各写一份靠约定对齐"。理由：`main.py _GEN_WORKFLOWS` 是把素材注入节点的地方，
     **离"真实提交"最近**。
   ★ 因此本文件**不再持有静态声明副本**（单源）：`get()/all()` 的数据来自
     `GET /api/workflows`（见 main.py `_WORKFLOW_SLOTS`），页面在用之前**先 `await WorkflowSlots.load()`**。
   ⚠️ 行为约定（⓪-2 验收基线，不许变）：槽位渲染 / 必填与数量校验 / 提交参数组装
     的输入结构逐字段与原来一致（key/type/label/min/max/required/node/hint）。
   ⚠️ 声明不可得时**不静默降级**：槽位为空 + toast 报错（宁可响亮失败，也不拿假声明渲染）。

   槽字段：key / type(image|video|audio) / label(自然语言，红线) / min / max / required / node(注入用) / hint
*/
(function () {
  var SLOTS = null;          /* load() 成功后填充 */
  var MISSING = [];          /* 未补齐信息（后端下发，供需求报告/自检引用） */
  var WARN = [];             /* 一致性告警（声明 vs _GEN_WORKFLOWS） */
  var _pending = null;

  function load() {
    if (_pending) return _pending;
    _pending = fetch('/api/workflows').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (j) {
      SLOTS = j.slots || {};
      MISSING = j.missing || [];
      WARN = j.warnings || [];
      if (WARN.length && window.console) {
        console.warn('[workflow-slots] 声明一致性告警（单一源纪律）：', WARN);
      }
      return SLOTS;
    }).catch(function (e) {
      SLOTS = {};
      _pending = null;       /* 允许重试 */
      if (window.toast) window.toast('槽位声明加载失败：' + (e.message || e), 'error');
      throw e;
    });
    return _pending;
  }

  window.WorkflowSlots = {
    load: load,
    /* 同步读（页面在 load 之后调用；未 load 时返回空，绝不返回假声明） */
    get: function (name) { return SLOTS ? (SLOTS[name] || []).slice() : []; },
    all: function () { return SLOTS || {}; },
    get MISSING() { return MISSING; },
    get warnings() { return WARN; },
    loaded: function () { return SLOTS !== null; }
  };
})();
