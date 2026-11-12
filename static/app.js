/* Supersu 工作台 · 前端交互
   原则：不引入框架，事件全部委托，页面切换不刷新。 */
(function () {
  'use strict';

  /* ---------------------------------------------------------------- toast */
  var toastEl = document.getElementById('wb-toast');
  var toastTimer = null;

  window.wbToast = function (msg, isErr) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.toggle('err', !!isErr);
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove('show');
    }, 2600);
  };

  /* ------------------------------------------------------------- 弹窗开合 */
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-mbx-open]');
    if (opener) {
      var box = document.getElementById(opener.getAttribute('data-mbx-open'));
      if (box) box.hidden = false;
      return;
    }

    var closer = e.target.closest('[data-mbx-close]');
    if (closer) {
      var m = closer.closest('.wb-mbx');
      if (m) m.hidden = true;
      return;
    }

    // 点遮罩关闭
    if (e.target.classList && e.target.classList.contains('wb-mbx')) {
      e.target.hidden = true;
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('.wb-mbx:not([hidden])').forEach(function (el) {
        el.hidden = true;
      });
    }
  });

  /* --------------------------------------------- 还没接上的控件给个说明 */
  var q = document.getElementById('wb-q');
  if (q && q.hasAttribute('data-hint')) {
    q.addEventListener('click', function () {
      window.wbToast(q.getAttribute('data-hint'));
    });
    q.addEventListener('focus', function () {
      q.blur();
    });
  }

  /* ------------------------------------------- 窄屏换短占位，文字别被截断
     断点跟 CSS 对齐：1080px 以下输入框只有 150px，放不下「搜索项目 / 服务器…」。 */
  if (q) {
    var qPh = q.getAttribute('placeholder') || '';
    if (qPh) {
      var mqNarrow = window.matchMedia('(max-width: 1080px)');
      var syncPh = function () {
        q.setAttribute('placeholder', mqNarrow.matches ? '搜索…' : qPh);
      };
      syncPh();
      if (mqNarrow.addEventListener) mqNarrow.addEventListener('change', syncPh);
      else if (mqNarrow.addListener) mqNarrow.addListener(syncPh);
    }
  }

  /* ------------------------------------------------------------ 截图位
     只在项目详情页存在 #wb-shot 时启用：Ctrl+V 粘贴 / 选文件 / 删除。 */
  var shot = document.getElementById('wb-shot');
  if (shot) {
    var rel = shot.getAttribute('data-rel') || '';
    var picker = document.getElementById('wb-shot-file');

    function post(url, form) {
      return fetch(url, { method: 'POST', body: form }).then(function (r) {
        return r.json().catch(function () {
          return { ok: false, error: '服务返回的不是合法 JSON（HTTP ' + r.status + '）' };
        });
      });
    }

    function save(dataUrl) {
      var fd = new FormData();
      fd.append('rel', rel);
      fd.append('data', dataUrl);
      window.wbToast('正在保存截图…');
      post('/api/project/preview', fd)
        .then(function (j) {
          if (!j.ok) return window.wbToast(j.error || '保存失败', true);
          window.wbToast('截图已保存');
          setTimeout(function () { location.reload(); }, 450);
        })
        .catch(function (err) { window.wbToast('保存失败：' + err, true); });
    }

    function saveFile(file) {
      if (!file) return;
      if (!/^image\/(png|jpe?g|webp)$/i.test(file.type)) {
        return window.wbToast('只认 PNG / JPG / WebP 图片', true);
      }
      if (file.size > 8 * 1024 * 1024) {
        return window.wbToast('图片超过 8MB，先压缩一下', true);
      }
      var fr = new FileReader();
      fr.onload = function () { save(fr.result); };
      fr.onerror = function () { window.wbToast('读取文件失败', true); };
      fr.readAsDataURL(file);
    }

    document.addEventListener('paste', function (e) {
      var items = (e.clipboardData && e.clipboardData.items) || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].type && items[i].type.indexOf('image') === 0) {
          e.preventDefault();
          saveFile(items[i].getAsFile());
          return;
        }
      }
    });

    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-shot-pick]') && picker) {
        picker.click();
        return;
      }
      var del = e.target.closest('[data-shot-del]');
      if (del) {
        if (!window.confirm('删掉这张截图？文件会从项目目录里移除。')) return;
        var fd = new FormData();
        fd.append('rel', rel);
        post('/api/project/preview/delete', fd)
          .then(function (j) {
            if (!j.ok) return window.wbToast(j.error || '删除失败', true);
            window.wbToast('已删除');
            setTimeout(function () { location.reload(); }, 400);
          })
          .catch(function (err) { window.wbToast('删除失败：' + err, true); });
      }
    });

    if (picker) {
      picker.addEventListener('change', function () {
        saveFile(picker.files && picker.files[0]);
        picker.value = '';
      });
    }
  }

  /* ------------------------------------------------------------ 批量选择 */
  var checks = Array.prototype.slice.call(document.querySelectorAll('.wb-check'));
  var checkAll = document.getElementById('wb-check-all');
  var bulk = document.getElementById('wb-bulk');

  function selectedPaths() {
    return checks.filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
  }

  function refreshBulk() {
    var n = selectedPaths().length;
    if (bulk) {
      bulk.hidden = n === 0;
      var lbl = bulk.querySelector('.wb-bulk-n b');
      if (lbl) lbl.textContent = n;
    }
    if (checkAll) checkAll.checked = n > 0 && n === checks.length;
  }

  if (checks.length) {
    checks.forEach(function (c) { c.addEventListener('change', refreshBulk); });
    if (checkAll) {
      checkAll.addEventListener('change', function () {
        checks.forEach(function (c) { c.checked = checkAll.checked; });
        refreshBulk();
      });
    }
    refreshBulk();

    if (bulk) {
      bulk.addEventListener('click', function (e) {
        if (e.target.id === 'wb-bulk-clear') {
          checks.forEach(function (c) { c.checked = false; });
          refreshBulk();
          return;
        }
        var btn = e.target.closest('[data-bulk]');
        if (!btn) return;
        var paths = selectedPaths();
        if (!paths.length) return;
        var action = btn.getAttribute('data-bulk');
        if (action === 'archive' && !window.confirm('归档 ' + paths.length + ' 个项目？归档后不再出现在「活跃」里，随时可取消。')) {
          return;
        }
        var fd = new FormData();
        fd.append('action', action);
        paths.forEach(function (p) { fd.append('paths', p); });
        fetch('/api/projects/bulk', { method: 'POST', body: fd })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) return window.wbToast(j.error || '操作失败', true);
            window.wbToast('已处理 ' + j.count + ' 项');
            setTimeout(function () { location.reload(); }, 420);
          })
          .catch(function (err) { window.wbToast('操作失败：' + err, true); });
      });
    }
  }

  /* -------------------------------------------------------- 服务器弹窗 */
  var srvBox = document.getElementById('mbx-server');
  if (srvBox) {
    var f = {};
    ['id', 'name', 'host', 'port', 'user', 'auth', 'secret', 'passphrase', 'jump', 'grp', 'note', 'am', 'ad', 'al']
      .forEach(function (k) {
        var key = 'srv-' + k;
        f[k] = document.getElementById(key);
      });
    var am = document.getElementById('srv-am');
    var ad = document.getElementById('srv-ad');
    var al = document.getElementById('srv-al');
    var legacy = document.getElementById('srv-legacy');
    var secretHint = document.getElementById('srv-secret-hint');
    var title = document.getElementById('srv-title');
    var result = document.getElementById('srv-test-result');

    function showResult(text, good) {
      if (!result) return;
      result.hidden = false;
      result.textContent = text;
      result.className = 'wb-test-result ' + (good ? 'ok' : 'err');
    }

    function fill(data, mode) {
      var edit = mode === 'edit';
      if (title) title.textContent = edit ? '编辑服务器' : '添加服务器';
      f.id.value = data.id || '';
      f.name.value = data.name || '';
      f.host.value = data.host || '';
      f.port.value = data.port || 22;
      f.user.value = data.user || 'root';
      f.auth.value = data.auth || 'password';
      f.grp.value = data.grp || '';
      f.note.value = data.note || '';
      f.jump.value = data.jump || '';
      f.secret.value = '';
      f.passphrase.value = '';
      if (am) am.value = data.alert_mem || 85;
      if (ad) ad.value = data.alert_disk || 90;
      if (al) al.value = data.alert_load || 4.0;
      if (legacy) legacy.checked = false;
      if (secretHint) secretHint.textContent = edit ? '（留空 = 不改）' : '';
      if (result) result.hidden = true;
      if (f.name) f.name.focus();
    }

    document.addEventListener('click', function (e) {
      var opener = e.target.closest('[data-mbx-open="mbx-server"]');
      if (!opener) return;
      var mode = opener.getAttribute('data-mode') || 'add';
      var card = opener.closest('[data-srv]');
      var data = {};
      if (mode === 'edit' && card) {
        try { data = JSON.parse(card.getAttribute('data-srv')); } catch (err) { data = {}; }
      }
      fill(data, mode);
    });

    function payload() {
      var fd = new FormData();
      ['id', 'name', 'host', 'port', 'user', 'auth', 'secret', 'passphrase', 'jump', 'grp', 'note']
        .forEach(function (k) { fd.append(k, (f[k] && f[k].value) || ''); });
      fd.append('alert_mem', (am && am.value) || 85);
      fd.append('alert_disk', (ad && ad.value) || 90);
      fd.append('alert_load', (al && al.value) || 4.0);
      if (legacy && legacy.checked) fd.append('legacy_kex', '1');
      return fd;
    }

    var testBtn = document.getElementById('srv-test');
    if (testBtn) {
      testBtn.addEventListener('click', function () {
        showResult('正在连接…', true);
        fetch('/api/server/test', { method: 'POST', body: payload() })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) return showResult(j.error || '连接失败', false);
            showResult('连接成功，主机名：' + j.hostname, true);
          })
          .catch(function (err) { showResult('测试失败：' + err, false); });
      });
    }

    var saveBtn = document.getElementById('srv-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        saveBtn.disabled = true;
        fetch('/api/server/save', { method: 'POST', body: payload() })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            saveBtn.disabled = false;
            if (!j.ok) {
              showResult(j.error || '保存失败', false);
              return;
            }
            window.wbToast('已保存');
            setTimeout(function () { location.reload(); }, 400);
          })
          .catch(function (err) {
            saveBtn.disabled = false;
            showResult('保存失败：' + err, false);
          });
      });
    }
  }

  /* -------------------------------------------------------- 详情页编辑 */
  var edSave = document.getElementById('ed-save');
  if (edSave) {
    edSave.addEventListener('click', function () {
      var rel = edSave.getAttribute('data-rel') || '';
      var pairs = [
        ['name', (document.getElementById('ed-name') || {}).value || ''],
        ['status', (document.getElementById('ed-status') || {}).value || ''],
        ['note', (document.getElementById('ed-note') || {}).value || '']
      ];
      var jobs = pairs.map(function (kv) {
        var fd = new FormData();
        fd.append('rel', rel);
        fd.append('field', kv[0]);
        fd.append('value', kv[1]);
        return fetch('/api/project/update', { method: 'POST', body: fd }).then(function (r) { return r.json(); });
      });
      Promise.all(jobs)
        .then(function (list) {
          var bad = list.filter(function (j) { return !j.ok; });
          if (bad.length) return window.wbToast(bad[0].error || '保存失败', true);
          window.wbToast('已保存');
          setTimeout(function () { location.reload(); }, 420);
        })
        .catch(function (err) { window.wbToast('保存失败：' + err, true); });
    });
  }

  /* ------------------------------------------------------ 打开项目目录 */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-open]');
    if (!btn) return;
    var fd = new FormData();
    fd.append('rel', btn.getAttribute('data-open'));
    fetch('/api/open', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (!j.ok) window.wbToast(j.error || '打不开', true); })
      .catch(function (err) { window.wbToast('打不开：' + err, true); });
  });

  /* -------------------------------------------------- 删除服务器
     连不上的死机器往往还挂着项目关联，不给强删就得先去逐个解绑，
     结果灰条永远清不掉。所以有关联时二次确认，确认后连关联一起断。 */
  function delServer(id, name, force) {
    var fd = new FormData();
    fd.append('id', id);
    if (force) fd.append('force', '1');
    fetch('/api/server/del', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.ok) {
          window.wbToast('已删除「' + name + '」');
          setTimeout(function () { location.reload(); }, 420);
          return;
        }
        if (j.need_confirm) {
          if (window.confirm(j.error + '\n\n继续删除？采集记录和项目关联会一起清掉。')) {
            delServer(id, name, true);
          }
          return;
        }
        window.wbToast(j.error || '删不掉', true);
      })
      .catch(function (err) { window.wbToast('删不掉：' + err, true); });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-del-srv]');
    if (!btn) return;
    var id = btn.getAttribute('data-del-srv');
    var name = btn.getAttribute('data-srv-name') || '';
    if (!window.confirm('删除「' + name + '」？这个动作不可撤销。')) return;
    delServer(id, name, false);
  });

  /* ------------------------------------------- 服务器详情页：勾选关联项目
     跟项目页的 chip 是同一条关联的两头，哪边点都算数。 */
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-toggle-projs]');
    if (!t) return;
    var box = t.parentElement.querySelector('.wb-projpicker');
    if (box) box.hidden = !box.hidden;
  });

  document.addEventListener('change', function (e) {
    var cb = e.target.closest('[data-link-proj]');
    if (!cb) return;
    var fd = new FormData();
    fd.append('id', cb.getAttribute('data-sid'));
    fd.append('path', cb.getAttribute('data-link-proj'));
    fd.append('on', cb.checked ? '1' : '0');
    fetch('/api/server/link', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          cb.checked = !cb.checked;
          return window.wbToast(j.error || '改不了', true);
        }
        window.wbToast(cb.checked ? '已关联' : '已解除关联');
        setTimeout(function () { location.reload(); }, 420);
      })
      .catch(function (err) {
        cb.checked = !cb.checked;
        window.wbToast('改不了：' + err, true);
      });
  });

  /* ------------------------------------------- 重复目录：从清单移除
     只把 state 标成 dropped（不显示），**绝不删磁盘文件** —— 删目录不可逆。 */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-ignore-path]');
    if (!btn) return;
    var path = btn.getAttribute('data-ignore-path');
    if (!window.confirm('从清单里移除这条？\n\n只影响这个工作台的显示，磁盘上的文件一个都不会动。')) return;
    var fd = new FormData();
    fd.append('action', 'ignore');
    fd.append('paths', path);
    fetch('/api/projects/bulk', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) return window.wbToast(j.error || '没移除成功', true);
        window.wbToast('已从清单移除（文件没动）');
        setTimeout(function () { location.reload(); }, 420);
      })
      .catch(function (err) { window.wbToast('没移除成功：' + err, true); });
  });

  /* ------------------------------------------------------ 刷新
     顶栏那颗按钮干两种活：服务器页刷 SSH 巡检，其它页重扫项目目录（data-act
     由模板按当前页渲染）。以前固定扫项目，在服务器页点它跟服务器毫无关系。 */

  function wbBusy(btn, text) {
    if (!btn || btn.disabled) return null;
    var label = btn.querySelector('span') || btn;
    var state = { btn: btn, label: label, old: label.textContent };
    btn.disabled = true;
    btn.classList.add('is-busy');
    label.textContent = text;
    return state;
  }

  function wbIdle(state) {
    if (!state) return;
    state.btn.disabled = false;
    state.btn.classList.remove('is-busy');
    state.label.textContent = state.old;
  }

  /* 巡检是同步的：真连 SSH，4 台实测约 7 秒，离线机器更久。这期间不给反馈，
     用户只会觉得「点了没反应」——原来那条表单就是这么被当成无效的。 */
  function wbRefreshServers() {
    var state = wbBusy(document.getElementById('wb-refresh'), '刷新中…');
    fetch('/api/servers/refresh', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          wbIdle(state);
          return window.wbToast(j.error || '刷新失败', true);
        }
        window.wbToast('已刷新：' + j.online + '/' + j.total + ' 台在线，正在更新页面…');
        // 1.2s 再刷：太快会把刚弹出来的提示直接冲掉，等于白提示
        setTimeout(function () { location.reload(); }, 1200);
      })
      .catch(function (err) {
        wbIdle(state);
        window.wbToast('刷新失败：' + err, true);
      });
  }

  /* 顶栏那颗按钮 = 刷新「当前页」的数据，data-act 由模板按 active 渲染。
     页面工具条里不再放第二颗刷新（服务器页、到期页、项目页原来都各有一颗重复的）。 */
  function wbRefreshProjects() {
    var state = wbBusy(document.getElementById('wb-refresh'), '扫描中…');
    fetch('/api/refresh-all', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          wbIdle(state);
          return window.wbToast(j.error || '扫描失败', true);
        }
        window.wbToast('已重扫 ' + j.scanned + ' 个项目，正在刷新页面…');
        setTimeout(function () { location.reload(); }, 500);
      })
      .catch(function (err) {
        wbIdle(state);
        window.wbToast('扫描失败：' + err, true);
      });
  }

  /* 到期刷新要真去查 RDAP / 云 API，比列表刷新慢得多，同样得先给反馈。 */
  function wbRefreshExpiries() {
    var state = wbBusy(document.getElementById('wb-refresh'), '刷新中…');
    fetch('/api/expiry/refresh', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          wbIdle(state);
          return window.wbToast(j.error || '刷新失败', true);
        }
        var ds = j.domain_ssl || {};
        var pv = j.providers || {};
        window.wbToast('已刷新：证书 ' + (ds.ok || 0) + '/' + (ds.total || 0)
          + '，云实例 ' + (pv.instances || 0) + ' 台，正在更新页面…');
        setTimeout(function () { location.reload(); }, 1200);
      })
      .catch(function (err) {
        wbIdle(state);
        window.wbToast('刷新失败：' + err, true);
      });
  }

  var refreshBtn = document.getElementById('wb-refresh');
  if (refreshBtn) {
    var refreshAct = refreshBtn.getAttribute('data-act') || 'projects';
    refreshBtn.addEventListener('click', function () {
      if (refreshBtn.disabled) return;
      if (refreshAct === 'servers') {
        wbRefreshServers();
      } else if (refreshAct === 'expiries') {
        wbRefreshExpiries();
      } else {
        wbRefreshProjects();
      }
    });
  }

  /* -------------------------------------------------- 单个项目重扫
     改完 README 或刚提交完，只想刷新这一个，不该等整盘扫完。 */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-rescan]');
    if (!btn) return;
    var rel = btn.getAttribute('data-rescan');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.disabled = true;
    var fd = new FormData();
    fd.append('rel', rel);
    fetch('/api/project/rescan', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          window.wbToast(j.error || '重扫失败', true);
          return;
        }
        // 只刷这一张卡的数据：整页 reload 会丢掉滚动位置和筛选状态
        btn.classList.remove('is-busy');
        btn.disabled = false;
        var scope = btn.closest('[data-card-scope]');
        if (scope) scope.classList.remove('is-stale');
        window.wbToast('已重扫「' + (j.name || rel) + '」');
        setTimeout(function () { location.reload(); }, 420);
      })
      .catch(function (err) {
        window.wbToast('重扫失败：' + err, true);
        btn.classList.remove('is-busy');
        btn.disabled = false;
      });
  });

  /* -------------------------------------------------- 部署服务器 chip
     开合已交给通用弹窗（data-mbx-open / data-mbx-close / Esc / 点遮罩），
     下面只负责「点关联 / 取消关联」发请求，事件委托到 document，弹窗内同样生效。 */
  document.addEventListener('click', function (e) {
    // chip 里的 ↗ 是「去服务器详情」，别让它被当成切换关联
    var go = e.target.closest('[data-go-server]');
    if (go) {
      e.preventDefault();
      e.stopPropagation();
      location.href = '/server/' + go.getAttribute('data-go-server');
      return;
    }

    var local = e.target.closest('[data-local]');
    if (local) {
      var lf = new FormData();
      lf.append('rel', local.getAttribute('data-rel'));
      lf.append('on', local.getAttribute('data-local'));
      fetch('/api/project/local', { method: 'POST', body: lf })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) return window.wbToast(j.error || '改不了', true);
          location.reload();
        })
        .catch(function (err) { window.wbToast('改不了：' + err, true); });
      return;
    }

    var chip = e.target.closest('[data-link-srv]');
    if (!chip) return;
    var fd = new FormData();
    fd.append('rel', chip.getAttribute('data-rel'));
    fd.append('sid', chip.getAttribute('data-link-srv'));
    fd.append('on', chip.getAttribute('data-on'));
    fetch('/api/project/server', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) return window.wbToast(j.error || '改不了', true);
        location.reload();
      })
      .catch(function (err) { window.wbToast('改不了：' + err, true); });
  });
})();
