/* 到期页专用交互：只在 /expiries 加载。
   弹窗开合复用 app.js 的通用 data-mbx-open / data-mbx-close。 */
(function () {
  'use strict';

  var exSave = document.getElementById('ex-save');
  if (!exSave) return;

  function show(el, text, good) {
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
    el.className = 'wb-test-result ' + (good ? 'ok' : 'err');
  }

  function val(id, fallback) {
    var el = document.getElementById(id);
    return el && el.value !== undefined ? el.value : (fallback || '');
  }

  /* ---------------- 添加到期项 ---------------- */
  var exResult = document.getElementById('ex-result');
  exSave.addEventListener('click', function () {
    var fd = new FormData();
    fd.append('kind', val('ex-kind', 'manual'));
    fd.append('name', val('ex-name'));
    fd.append('expire', val('ex-expire'));
    fd.append('note', val('ex-note'));
    var renew = document.getElementById('ex-renew');
    if (renew && renew.checked) fd.append('renew', '1');

    fetch('/api/expiry/add', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) return show(exResult, j.error || '保存失败', false);
        window.wbToast('已添加');
        setTimeout(function () { location.reload(); }, 400);
      })
      .catch(function (err) { show(exResult, '保存失败：' + err, false); });
  });

  /* ---------------- 删除到期项 ---------------- */
  document.addEventListener('click', function (e) {
    var del = e.target.closest('[data-exp-del]');
    if (del) {
      if (!window.confirm('删掉这条到期项？')) return;
      var fd = new FormData();
      fd.append('id', del.getAttribute('data-exp-del'));
      fetch('/api/expiry/del', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) return window.wbToast(j.error || '删除失败', true);
          location.reload();
        });
      return;
    }

    var pd = e.target.closest('[data-prov-del]');
    if (pd) {
      if (!window.confirm('删掉这组云厂商配置？它拉到的到期项也会一起删。')) return;
      var fd2 = new FormData();
      fd2.append('id', pd.getAttribute('data-prov-del'));
      fetch('/api/expiry/provider/del', { method: 'POST', body: fd2 })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) return window.wbToast(j.error || '删除失败', true);
          location.reload();
        });
      return;
    }

    var tg = e.target.closest('[data-prov-toggle]');
    if (tg) {
      var fd3 = new FormData();
      fd3.append('id', tg.getAttribute('data-prov-toggle'));
      fetch('/api/expiry/provider/toggle', { method: 'POST', body: fd3 })
        .then(function (r) { return r.json(); })
        .then(function (j) { if (j.ok) location.reload(); });
    }
  });

  /* ---------------- 云厂商：测试解析 / 保存 ---------------- */
  var pvResult = document.getElementById('pv-result');

  function pvForm() {
    var fd = new FormData();
    fd.append('kind', val('pv-kind'));
    fd.append('cfg', val('pv-cfg', '{}'));
    return fd;
  }

  var pvTest = document.getElementById('pv-test');
  if (pvTest) {
    pvTest.addEventListener('click', function () {
      show(pvResult, '正在拉取…', true);
      fetch('/api/expiry/provider/test', { method: 'POST', body: pvForm() })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) return show(pvResult, j.error || '解析失败', false);
          var lines = (j.preview || []).map(function (it) {
            return '· ' + (it.name || '(无名)') + '   ' + (it.expire || '无到期日');
          });
          show(pvResult, '解析到 ' + j.count + ' 条，前 ' + (j.preview || []).length + ' 条：\n' + lines.join('\n'), true);
        })
        .catch(function (err) { show(pvResult, '解析失败：' + err, false); });
    });
  }

  var pvSave = document.getElementById('pv-save');
  if (pvSave) {
    pvSave.addEventListener('click', function () {
      var fd = pvForm();
      fd.append('name', val('pv-name'));
      fetch('/api/expiry/provider/add', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) return show(pvResult, j.error || '保存失败', false);
          window.wbToast('已保存，下次刷新就会拉');
          setTimeout(function () { location.reload(); }, 500);
        });
    });
  }

  /* ---------------- 通知配置 ---------------- */
  var ntResult = document.getElementById('nt-result');

  function ntForm() {
    var fd = new FormData();
    [
      ['smtp_host', 'nt-host'], ['smtp_port', 'nt-port'], ['smtp_user', 'nt-user'],
      ['smtp_pass', 'nt-pass'], ['mail_to', 'nt-to'], ['webhook_type', 'nt-type'],
      ['webhook_url', 'nt-url'], ['webhook_secret', 'nt-secret']
    ].forEach(function (pair) { fd.append(pair[0], val(pair[1])); });
    var tls = document.getElementById('nt-tls');
    if (tls && tls.checked) fd.append('smtp_tls', '1');
    return fd;
  }

  var ntSave = document.getElementById('nt-save');
  if (ntSave) {
    ntSave.addEventListener('click', function () {
      fetch('/api/expiry/notify', { method: 'POST', body: ntForm() })
        .then(function (r) { return r.json(); })
        .then(function (j) { show(ntResult, j.ok ? '已保存' : (j.error || '保存失败'), j.ok); });
    });
  }

  var ntTest = document.getElementById('nt-test');
  if (ntTest) {
    ntTest.addEventListener('click', function () {
      show(ntResult, '正在发送…', true);
      fetch('/api/expiry/notify', { method: 'POST', body: ntForm() })
        .then(function () { return fetch('/api/expiry/notify/test', { method: 'POST' }); })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var detail = j.results
            ? Object.keys(j.results).map(function (k) {
                return k + '：' + (j.results[k].ok ? '成功' : j.results[k].detail);
              }).join('\n')
            : (j.detail || '');
          show(ntResult, (j.ok ? '发送成功\n' : '发送失败\n') + detail, !!j.ok);
        })
        .catch(function (err) { show(ntResult, '发送失败：' + err, false); });
    });
  }
})();
