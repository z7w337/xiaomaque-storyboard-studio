/* ===== 7-1 环境守护：动作前自动检测 + 异常自动兜底 =====
 * 阶段7-1「异常兜底」B 全自动方案（第三方/用户修正，2026-09-06）：
 *   3 触发点（生图前/生成视频前/关键环境操作如清显存）→ 调 /api/ic/health 三重检查 →
 *   4 类异常自动兜底。**红线：面向用户的提示文案一律自然语言**（配音服务/视频生成服务/工坊），
 *   技术词（netstat/PID/端口号/IC/API/health/URL/ComfyUI/Infinite Canvas）只进 console 日志，不上屏。
 *
 * 暴露：
 *   window.envGuard.ensure(kind) -> Promise<{ok:true} | {ok:false, msg:<自然语言>}>
 *     kind: 'voice'|'image' = 需"配音服务"(自动拉起)   'video'|'tts' = 需"视频生成服务"(只提示引导)
 *   window.envGuard.toast(msg, type)  依赖 window.toast，无则 alert
 */
(function () {
  var HK = '/api/ic/health', BOOT = '/api/ic/boot';
  function _get(p) {
    return fetch(p).then(function (r) { return r.json(); });
  }
  function _post(p) {
    return fetch(p, { method: 'POST' }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }
  function _toast(msg, type) { if (window.toast) window.toast(msg, type || 'info'); else try { alert(msg); } catch (e) {} }
  function _log() { /* 技术细节只进日志，不面向用户 */ if (window.console) console.log.apply(console, ['[env-guard]'].concat([].slice.call(arguments))); }

  /* 读健康（失败抛 {error:true}） */
  function _health() {
    return _get(HK).then(function (h) {
      return { ic: h.ic, comfy: h.comfy, uu: h.uu };
    }).catch(function (e) { _log('health 请求失败', e); return { error: true, raw: String(e && e.message || e) }; });
  }
  /* IC(配音) 是否真的不可达——若"有东西占了却答非200"更可能是端口被占 */
  function _isIcDown(h) { return !!(h && h.ic && h.ic.ok === false); }
  function _looksPortHeld(h) {
    var m = String((h && h.ic && h.ic.msg) || '');
    return /返回|refused|拒绝/.test(m);   // 有回应但非 200 → 疑似端口被其他程序占用
  }
  /* 视频生成服务(远程 ComfyUI)是否不可达：health 的 uu/comfy 层 */
  function _videoDown(h) {
    if (h && h.error) return false;
    var c = h && h.comfy, u = h && h.uu;
    // IC 在但 ComfyUI/UU 任一不可达 → 视频服务连不上
    if (h.ic && h.ic.ok && ((c && c.ok === false) || (u && u.ok === false))) return true;
    // IC 也不在时这里由上层先处理 IC；返回 false 避免重复
    return false;
  }
  /* 自动拉起配音服务(IC) */
  function _boot() {
    return _post(BOOT).then(function (b) { _log('boot 返回', b); return b || {}; })
      .catch(function (e) { _log('boot 请求失败', e); return { ok: false, msg: String(e && e.message || e) }; });
  }

  /* kind:
   *   'voice'|'image' — 需配音服务(IC)；未运行则自动拉起，拉起失败/端口被占给人类引导
   *   'video'|'tts'   — 需视频生成服务(远程)；IC 也在则一并处理，最终视频服务不可达只提示引导
   * 返回 Promise<{ok:true} | {ok:false,msg}>；resolve 不 throw（调用方按 ok 决定继续/中止并提示）
   */
  function ensure(kind) {
    var needVoice = (kind === 'voice' || kind === 'image');
    return _health().then(function (h) {
      // ① 工坊服务异常：连健康检查都拿不到
      if (h && h.error) return { ok: false, msg: '工坊服务异常，请重启工坊后重试' };
      var icDown = _isIcDown(h);
      if (needVoice && icDown) {
        // 配音服务未运行 → 自动拉起
        _toast('正在自动启动配音服务…', 'info');
        return _boot().then(function (b) {
          if (b && b.ok) { _toast('已自动启动配音服务，请重试', 'success'); return { ok: true, booted: true }; }
          // boot 失败：可能是端口被占，或路径未配置/服务未真正起来
          if (_looksPortHeld(h) || /占用|超时|失败/.test(String(b && b.msg || ''))) {
            return { ok: false, msg: '启动端口被其他程序占用，请关闭占用程序后重试' };
          }
          return { ok: false, msg: '自动启动配音服务失败，请到设置里确认配音服务安装路径后重试' };
        });
      }
      // 配音服务没在跑但本次不需要它（如纯看视频服务）：对视频路径仍先自动拉起配音以便探到视频服务
      if (icDown && !needVoice) {
        return _boot().then(function (b) {
          var bd = !!(b && b.ok);
          if (!bd && _looksPortHeld(h)) return { ok: false, msg: '启动端口被其他程序占用，请关闭占用程序后重试' };
          // 拉起后重查，看视频服务
          return _health().then(function (h2) {
            if (h2 && h2.error) return { ok: false, msg: '工坊服务异常，请重启工坊后重试' };
            if (_videoDown(h2)) return { ok: false, msg: '视频生成服务连不上，请检查远程电脑是否开机、软件是否已启动' };
            if (bd) _toast('已自动启动配音服务', 'success');
            return { ok: true };
          });
        });
      }
      // 服务在跑：按需查视频服务
      if (!needVoice && _videoDown(h)) return { ok: false, msg: '视频生成服务连不上，请检查远程电脑是否开机、软件是否已启动' };
      if (icDown) return { ok: false, msg: '自动启动配音服务失败，请到设置里确认配音服务安装路径后重试' };
      return { ok: true };
    });
  }

  window.envGuard = { ensure: ensure, toast: _toast };
})();
