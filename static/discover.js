/* 一键发现：扫描 → 候选列表 → 勾选导入。
   设计原则：系统能做的都做掉（扫、探、试连、填好），
   只有「补密码」和「填云账号」留给人在页面上看一眼。 */
(function () {
  'use strict';

  var box = document.getElementById('mbx-discover');
  if (!box) return;

  var busy = document.getElementById('dsc-busy');
  var body = document.getElementById('dsc-body');
  var listEl = document.getElementById('dsc-list');
  var statEl = document.getElementById('dsc-stat');
  var hintEl = document.getElementById('dsc-hint');
  var warnEl = document.getElementById('dsc-warn');
  var cloudEl = document.getElementById('dsc-cloud');
  var selEl = document.getElementById('dsc-sel');
  var importBtn = document.getElementById('dsc-import');
  var rescanBtn = document.getElementById('dsc-rescan');

  var items = [];

  var STATUS = {
    ready: { label: '可直接导入', cls: 'b-ok', pick: true },
    alive: { label: '能连上', cls: 'b-warn', pick: false },
    exists: { label: '已在列表', cls: 'b-mut', pick: false },
    dead: { label: '连不上', cls: 'b-err', pick: false }
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function pickDefault(it) {
    // 端口通 + 来自本机 SSH 线索 = 大概率是自己的机器，默认勾上
    if (it.status === 'ready') return true;
    if (it.status === 'alive' && (it.source === 'ssh_config' || it.source === 'known_hosts')) return true;
    return false;
  }

  function render() {
    listEl.innerHTML = '';
    items.forEach(function (it, i) {
      var st = STATUS[it.status] || STATUS.dead;
      var row = document.createElement('label');
      row.className = 'dsc-row' + (it.status === 'exists' ? ' is-exists' : '');

      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.setAttribute('data-i', String(i));
      cb.checked = it._pick;
      cb.disabled = it.status === 'exists';
      row.appendChild(cb);

      var g = document.createElement('div');
      g.className = 'dsc-g';

      var n = document.createElement('div');
      n.className = 'dsc-n';
      n.innerHTML = esc(it.name || it.host) +
        ' <span class="wb-bdg ' + st.cls + '">' + st.label + '</span>' +
        (it.source === 'cloud' ? ' <span class="wb-bdg b-brand">云账号</span>' : '');

      var m = document.createElement('div');
      m.className = 'dsc-m';
      var bits = [esc(it.user) + '@' + esc(it.host) + ':' + it.port];
      if (it.detail) bits.push(esc(it.detail));
      if (it.status === 'alive' && it.auth === 'password') bits.push('要补密码');
      if (it.why) bits.push(esc(it.why));
      if (it.note) bits.push(esc(it.note));
      m.textContent = bits.filter(Boolean).join(' · ');

      g.appendChild(n);
      g.appendChild(m);
      row.appendChild(g);
      listEl.appendChild(row);
    });
    updateSel();
  }

  function updateSel() {
    var n = items.filter(function (it) { return it._pick; }).length;
    selEl.textContent = n ? '已选 ' + n + ' 台' : '还没勾';
    importBtn.disabled = n === 0;
    importBtn.textContent = n ? '导入 ' + n + ' 台' : '导入';
  }

  function showStats(st) {
    var parts = [
      ['可直接导入', st.ready, 'b-ok'],
      ['要补密码', st.alive, 'b-warn'],
      ['已在列表', st.exists, 'b-mut'],
      ['连不上', st.dead, 'b-err']
    ];
    statEl.innerHTML = parts.map(function (p) {
      return '<span class="dsc-pill"><b>' + p[1] + '</b>' + p[0] + '</span>';
    }).join('');
  }

  function run() {
    busy.hidden = false;
    body.hidden = true;
    importBtn.disabled = true;
    selEl.textContent = '';

    fetch('/api/servers/discover', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        busy.hidden = true;
        body.hidden = false;
        if (!j.ok) {
          listEl.innerHTML = '<div class="dsc-empty">' + esc(j.error || '扫描失败') + '</div>';
          return;
        }
        items = (j.items || []).map(function (it) {
          it._pick = pickDefault(it);
          return it;
        });
        showStats(j.stats || { ready: 0, alive: 0, exists: 0, dead: 0 });

        if (!items.length) {
          listEl.innerHTML = '<div class="dsc-empty">没扫到任何线索 —— 手动添加一台吧。</div>';
        } else {
          render();
        }

        // 有能直接导入的就把提示压下去，没有才提示补密码
        if ((j.stats || {}).ready) {
          hintEl.hidden = false;
          hintEl.innerHTML = '这 ' + j.stats.ready + ' 台已经用本机密钥验证过，勾上就能用，不用填任何东西。';
        } else if (items.length) {
          hintEl.hidden = false;
          hintEl.innerHTML = '扫到的都能连上，但没有一台能用本机密钥直接登录 —— 导入后补一下密码就行。';
        } else {
          hintEl.hidden = true;
        }

        if (j.warnings && j.warnings.length) {
          warnEl.hidden = false;
          warnEl.innerHTML = '<b>有些线索没取到：</b><br>' + j.warnings.map(esc).join('<br>');
        } else {
          warnEl.hidden = true;
        }

        if (!(j.stats || {}).cloud) {
          cloudEl.hidden = false;
          cloudEl.innerHTML =
            '<b>想把云上的机器也自动列出来？</b>' +
            '去<a href="/expiries">到期提醒</a>填一次腾讯云 / 阿里云 / 华为云的 AK/SK（只有第一次要填），' +
            '之后这里会连云上的实例一起列出，还能顺带看到哪个快到期。';
        } else {
          cloudEl.hidden = true;
        }
      })
      .catch(function (err) {
        busy.hidden = true;
        body.hidden = false;
        listEl.innerHTML = '<div class="dsc-empty">扫描失败：' + esc(err) + '</div>';
      });
  }

  // 每次打开弹窗都重新扫一遍 —— 目录和 SSH 配置随时会变
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-mbx-open="mbx-discover"]')) run();
  });

  rescanBtn.addEventListener('click', run);

  listEl.addEventListener('change', function (e) {
    var cb = e.target.closest('input[type="checkbox"]');
    if (!cb) return;
    var i = Number(cb.getAttribute('data-i'));
    if (items[i]) items[i]._pick = cb.checked;
    updateSel();
  });

  importBtn.addEventListener('click', function () {
    var chosen = items.filter(function (it) { return it._pick; }).map(function (it) {
      return {
        name: it.name, host: it.host, port: it.port,
        user: it.user, auth: it.auth, secret: it.secret, note: it.note,
        verified: !!it.verified   // 后端据此决定能不能覆盖已存在的旧凭据
      };
    });
    if (!chosen.length) return;

    var fd = new FormData();
    fd.append('items', JSON.stringify(chosen));
    importBtn.disabled = true;
    importBtn.textContent = '导入中…';

    fetch('/api/servers/import', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          importBtn.disabled = false;
          updateSel();
          return window.wbToast(j.error || '导入失败', true);
        }
        var msg = '导入 ' + j.added + ' 台';
        if (j.updated) msg += '，更新凭据 ' + j.updated + ' 台';
        if (j.skipped) msg += '，跳过 ' + j.skipped + ' 台';
        window.wbToast(msg);
        setTimeout(function () { location.reload(); }, 600);
      })
      .catch(function (err) {
        importBtn.disabled = false;
        updateSel();
        window.wbToast('导入失败：' + err, true);
      });
  });
})();
