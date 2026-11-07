// 画面の動き（WAKAREME AI 共通）。動きを減らす設定の端末では何もしない
(function () {
  const calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  // 承認：はんこをポンと押してから送る（data-stamp="押す文字"）
  window.stamp = function (text) {
    if (calm) return;
    const s = document.createElement('div');
    s.className = 'stamp-pop'; s.textContent = text || '合ってる';
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 1200);
  };
  document.addEventListener('click', e => {
    const b = e.target.closest('button[data-stamp]');
    if (!b || calm || b.dataset.stamped || !b.form) return;
    if (!b.form.checkValidity()) return;
    e.preventDefault();
    b.dataset.stamped = '1';
    window.stamp(b.dataset.stamp);
    setTimeout(() => b.form.requestSubmit(b), 550);
  });

  // 教育モードの答え合わせ：合っていた＝✓が描かれる／違っていた＝小さく揺れる
  window.feedback = function (ok) {
    if (calm) return;
    const f = document.createElement('div');
    f.className = 'feedback ' + (ok ? 'ok' : 'ng');
    f.innerHTML = ok
      ? '<svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M15 27 l8 8 l15 -17"/></svg>'
      : '<span>もう一度見てみよう</span>';
    document.body.appendChild(f);
    setTimeout(() => f.remove(), 1100);
  };

  // ピン留め：押したピンが、戻ってきた画面で刺さる
  document.addEventListener('submit', e => {
    const f = e.target.closest('.pin-form');
    if (f) try { sessionStorage.setItem('pin-anim', f.getAttribute('action')); } catch (x) {}
  });
  document.addEventListener('DOMContentLoaded', () => {
    let a = null;
    try { a = sessionStorage.getItem('pin-anim'); sessionStorage.removeItem('pin-anim'); } catch (x) {}
    if (!a || calm) return;
    const f = document.querySelector('.pin-form[action="' + a + '"]');
    if (f) f.querySelector('.pin-btn').classList.add('drop');
  });

  // 保存などの小さなボタン：押すとポンと弾む（data-pop）
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-pop]');
    if (!b || calm) return;
    b.classList.remove('pop'); void b.offsetWidth; b.classList.add('pop');
  });
})();
