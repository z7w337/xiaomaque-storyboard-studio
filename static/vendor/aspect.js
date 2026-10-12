/* 比例 → 宽高（1K 基准）：长边 1024，短边按比例取 64 的倍数（模型友好）。
 *
 * ★ 单一真源：提示词编辑器（assets.html）与生图工具（gentools.html）共用本文件，
 *   避免两处各写一份、日后漂移出「同一比例两处算出不同像素」。
 *
 * 用户 2026-10-01 定：清晰度选项去掉，分辨率固定 1K；宽高由比例决定
 * （工作流没有 3-4K 高分能力，要更高清去生图工具的 enhance / upscale）。
 */
(function () {
  var MAP = {
    '1:1': [1, 1], '16:9': [16, 9], '9:16': [9, 16], '4:3': [4, 3], '3:4': [3, 4], '21:9': [21, 9], '9:21': [9, 21]
  };
  var LONG = 1024;

  function snap(n) { return Math.max(64, Math.round(n / 64) * 64); }

  /* 与提示词编辑器弹窗里的比例列表保持一致（含「自动」；自动按 1:1 处理） */
  window.ASPECT_RATIOS = ['自动', '1:1', '16:9', '9:16', '4:3', '3:4', '21:9'];

  /* ratio: '16:9' 等；空/未选 = 1:1。返回 [width, height]。
   * long（可选）：长边基准像素 —— 省略/非法 = 1024（1K）；2048 = 2K（Qwen-Image-2.1 用）。
   * ★ 2026-10-01：加 long 参数，让 2K 也能按同一套比例算尺寸（不再各写一份换算）。 */
  window.dimsByRatio = function (ratio, long) {
    var L = parseInt(long, 10);
    if (!(L > 0)) L = LONG;
    var r = MAP[String(ratio || '').trim()];
    if (!r) return [L, L];
    return r[0] >= r[1] ? [L, snap(L * r[1] / r[0])] : [snap(L * r[0] / r[1]), L];
  };
})();
