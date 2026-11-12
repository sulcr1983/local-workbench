/* 设置页专用交互。 */
(function () {
  'use strict';

  var rootForm = document.getElementById('root-form');
  if (!rootForm) return;

  function show(el, text, good) {
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
    el.className = 'wb-test-result ' + (good ? 'ok' : 'err');
  }

  function post(url, fd) {
    return fetch(url, { method: 'POST', body: fd }).then(function (r) { return r.json(); });
  }

  /* ---------------- 界面开关 ---------------- */
  document.querySelectorAll('[data-ui-toggle]').forEach(function (box) {
    box.addEventListener('change', function () {
      var fd = new FormData();
      fd.append('key', box.getAttribute('data-ui-toggle'));
      fd.append('value', box.checked ? '1' : '0');
      post('/api/ui/toggle', fd)
        .then(function (j) {
          if (!j.ok) {
            box.checked = !box.checked;
            return window.wbToast(j.error || '保存失败', true);
          }
          window.wbToast('已保存，刷新后菜单会变');
        })
        .catch(function (err) {
          box.checked = !box.checked;
          window.wbToast('保存失败：' + err, true);
        });
    });
  });

  /* ---------------- 扫描范围 ---------------- */
  var scopeBox = document.querySelector('[data-scope-toggle]');
  if (scopeBox) {
    scopeBox.addEventListener('change', function () {
      var fd = new FormData();
      fd.append('scope', scopeBox.checked ? 'full' : 'safe');
      post('/api/welcome/scope', fd)
        .then(function (j) {
          if (!j.ok) { scopeBox.checked = !scopeBox.checked; return window.wbToast(j.error || '保存失败', true); }
          window.wbToast(scopeBox.checked ? '已切到无限制' : '已切到保守模式');
        })
        .catch(function (err) { window.wbToast('保存失败：' + err, true); });
    });
  }

  /* ---------------- 活跃度口径 ---------------- */
  var rlSave = document.getElementById('rl-save');
  var rlResult = document.getElementById('rl-result');
  if (rlSave) {
    rlSave.addEventListener('click', function () {
      var fd = new FormData();
      ['active', 'slow', 'stale'].forEach(function (k) {
        var el = document.getElementById('rl-' + k);
        fd.append(k, el ? el.value : '');
      });
      post('/api/settings/rules', fd)
        .then(function (j) { show(rlResult, j.ok ? '已保存，下次扫描生效' : (j.error || '保存失败'), j.ok); })
        .catch(function (err) { show(rlResult, '保存失败：' + err, false); });
    });
  }

  /* ---------------- 根目录 ---------------- */
  rootForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var input = document.getElementById('root-path');
    var fd = new FormData();
    fd.append('path', input ? input.value : '');
    post('/api/root/add', fd)
      .then(function (j) {
        if (!j.ok) return window.wbToast(j.error || '添加失败', true);
        window.wbToast('已添加，去项目页点「重新扫描」');
        setTimeout(function () { location.reload(); }, 500);
      })
      .catch(function (err) { window.wbToast('添加失败：' + err, true); });
  });

  document.addEventListener('click', function (e) {
    var del = e.target.closest('[data-root-del]');
    if (!del) return;
    if (!window.confirm('移除这个根目录？磁盘上的文件不会被删。')) return;
    var fd = new FormData();
    fd.append('path', del.getAttribute('data-root-del'));
    post('/api/root/del', fd)
      .then(function (j) {
        if (!j.ok) return window.wbToast(j.error || '移除失败', true);
        location.reload();
      });
  });

  /* ---------------- 导入导出 ---------------- */
  var cfgPick = document.getElementById('cfg-pick');
  var cfgFile = document.getElementById('cfg-file');
  var cfgResult = document.getElementById('cfg-result');

  if (cfgPick && cfgFile) {
    cfgPick.addEventListener('click', function () { cfgFile.click(); });
    cfgFile.addEventListener('change', function () {
      var file = cfgFile.files && cfgFile.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var fd = new FormData();
        fd.append('payload', String(reader.result || ''));
        post('/api/config/import', fd)
          .then(function (j) {
            if (!j.ok) return show(cfgResult, j.error || '导入失败', false);
            show(cfgResult, '导入完成：服务器 ' + j.servers + ' 台 · 项目 ' + j.projects + ' 个', true);
            setTimeout(function () { location.reload(); }, 900);
          })
          .catch(function (err) { show(cfgResult, '导入失败：' + err, false); });
      };
      reader.readAsText(file, 'utf-8');
      cfgFile.value = '';
    });
  }
})();
