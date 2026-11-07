// 動画の指操作（すべての動画プレイヤー共通）
//  右：素早く2回タップ＝10秒送る／0.5秒以上長押し＝押している間だけ2倍速
//  左：素早く2回タップ＝10秒戻す／0.5秒以上長押し＝押している間だけ2倍速で巻き戻し
//  1回タップ＝再生・停止（opt.onSingle で差し替えできる）
(function () {
  const HOLD = 500, DOUBLE = 300;

  function flashText(flash, text) {
    if (!flash) return;
    flash.textContent = text; flash.hidden = false;
    clearTimeout(flash.t); flash.t = setTimeout(() => { flash.hidden = true; }, 600);
  }

  // el：指を受ける要素、v：動画。opt.side（'left'/'right'。無ければ指の位置で決める）、
  // opt.min/opt.max（動ける範囲を返す関数）、opt.onSingle、opt.flash、opt.onAny（触ったら呼ぶ）
  window.attachVideoGestures = function (el, v, opt = {}) {
    const min = opt.min || (() => 0), max = opt.max || (() => v.duration || 1e9);
    let holdTimer = null, holding = null, rewind = null, savedRate = 1, wasPaused = true;
    let lastTap = 0, lastSide = null, single = null, moved = false;

    const sideOf = ev => {
      if (opt.side) return opt.side;
      const r = el.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width;
      return x < 0.4 ? 'left' : x > 0.6 ? 'right' : 'mid';
    };
    function stopHold() {
      clearTimeout(holdTimer); holdTimer = null;
      if (holding === 'right') v.playbackRate = savedRate;
      if (holding === 'left') { clearInterval(rewind); if (!wasPaused) v.play().catch(() => {}); }
      if (holding && opt.flash) opt.flash.hidden = true;
      holding = null;
    }
    el.addEventListener('contextmenu', ev => ev.preventDefault());
    // iPhone の「2回タップで拡大」を止める
    el.addEventListener('touchend', ev => { if (ev.cancelable) ev.preventDefault(); }, { passive: false });
    el.addEventListener('pointerdown', ev => {
      if (opt.onAny) opt.onAny();
      const side = sideOf(ev); moved = false;
      if (side === 'mid') return;
      holdTimer = setTimeout(() => {
        holding = side; savedRate = v.playbackRate; wasPaused = v.paused;
        if (side === 'right') { v.playbackRate = 2; v.play().catch(() => {}); }
        else { v.pause(); rewind = setInterval(() => { v.currentTime = Math.max(min(), v.currentTime - 0.1); }, 50); }
        if (opt.flash) { opt.flash.textContent = side === 'right' ? '2倍速 ⏩' : '⏪ 2倍速'; opt.flash.hidden = false; clearTimeout(opt.flash.t); }
      }, HOLD);
    });
    el.addEventListener('pointercancel', () => { moved = true; stopHold(); });
    el.addEventListener('pointerleave', () => { if (holding) stopHold(); });
    el.addEventListener('pointerup', ev => {
      if (holding) { stopHold(); return; }
      clearTimeout(holdTimer);
      if (moved) return;
      const side = sideOf(ev), now = Date.now();
      if (side !== 'mid' && now - lastTap < DOUBLE && side === lastSide) {
        clearTimeout(single); lastTap = 0;
        if (side === 'right') { v.currentTime = Math.min(max() - 0.5, v.currentTime + 10); flashText(opt.flash, '10秒 ⏩'); }
        else { v.currentTime = Math.max(min(), v.currentTime - 10); flashText(opt.flash, '⏪ 10秒'); }
        return;
      }
      lastTap = now; lastSide = side;
      clearTimeout(single);
      single = setTimeout(() => {
        if (opt.onSingle) opt.onSingle(); else (v.paused ? v.play().catch(() => {}) : v.pause());
      }, side === 'mid' ? 0 : DOUBLE);
    });
  };

  // 操作バーつきのプレイヤー：左右に見えない受け口を重ねる（真ん中と下の操作バーはそのまま使える）
  function wrapPlayer(v) {
    if (v.dataset.gesture) return;
    v.dataset.gesture = '1';
    const box = document.createElement('div');
    box.className = 'vwrap' + (v.classList.contains('pin-video') ? ' pin-video-wrap' : '');
    v.parentNode.insertBefore(box, v); box.appendChild(v);
    const flash = document.createElement('div');
    flash.className = 'vflash'; flash.hidden = true; box.appendChild(flash);
    ['left', 'right'].forEach(side => {
      const z = document.createElement('div');
      z.className = 'vzone ' + side; box.appendChild(z);
      // data-start / data-end があれば、その場面の中だけで送り・戻しする
      const s = parseFloat(v.dataset.start), e = parseFloat(v.dataset.end);
      const range = e > s ? { min: () => s, max: () => e } : {};
      window.attachVideoGestures(z, v, { side, flash, ...range });
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('video.player').forEach(wrapPlayer);
  });
})();
