/* 引擎页：收编 / 剔掉 / 恢复。
   只改 project.state 一个字段，不动任何文件 —— 所以「剔掉」随时能恢复。 */
(function () {
  'use strict';

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-eng-set]');
    if (!btn) return;

    var to = btn.getAttribute('data-to');
    if (to === 'dropped' && !window.confirm(
      '剔掉这条？\n\n不会删你的文件夹，也不会删任何记录，只是不在项目列表里显示 —— 随时能在这里恢复。'
    )) return;

    var fd = new FormData();
    fd.append('path', btn.getAttribute('data-eng-set'));
    fd.append('to', to);

    btn.disabled = true;
    fetch('/api/engine/state', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          btn.disabled = false;
          return window.wbToast(j.error || '改不了', true);
        }
        window.wbToast(to === 'auto' ? '已收编' : (to === 'dropped' ? '已剔掉，可在「已剔除」里恢复' : '已退回候选'));
        setTimeout(function () { location.reload(); }, 380);
      })
      .catch(function (err) {
        btn.disabled = false;
        window.wbToast('改不了：' + err, true);
      });
  });
})();
