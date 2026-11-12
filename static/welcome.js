/* 新手向导（welcome.html）专用。类名 wz- 前缀，避免和主样式打架。 */
(function () {
  'use strict';

  var root = document.querySelector('.wz');
  if (!root) return;

  var picked = [];          // 已选目录（绝对路径）
  var curPath = '';         // 当前浏览的目录
  var sshRoutes = [];       // SSH config 候选
  var knownHosts = [];      // known_hosts 候选
  var projHosts = [];       // 项目文档里提到的

  function $(id) { return document.getElementById(id); }

  function show(el, text, good) {
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
    el.className = 'wb-test-result ' + (good ? 'ok' : 'err');
  }

  function goto(step) {
    root.querySelectorAll('.wz-pane').forEach(function (p) {
      p.classList.toggle('on', p.getAttribute('data-pane') === String(step));
    });
    root.querySelectorAll('#wz-steps li').forEach(function (li) {
      var n = Number(li.getAttribute('data-step'));
      li.classList.toggle('on', n === step);
      li.classList.toggle('done', n < step);
    });
    window.scrollTo(0, 0);
  }

  root.querySelectorAll('[data-goto]').forEach(function (b) {
    b.addEventListener('click', function () { goto(b.getAttribute('data-goto')); });
  });

  /* -------------------------------------------------- 跳过 / 完成 */
  root.querySelectorAll('[data-skip]').forEach(function (b) {
    b.addEventListener('click', function () {
      fetch('/api/welcome/done', { method: 'POST' })
        .then(function () { location.href = '/projects'; });
    });
  });

  /* -------------------------------------------------- 目录浏览 */
  function renderPicked() {
    var list = $('wz-picked');
    var count = $('wz-count');
    if (!list) return;
    list.innerHTML = '';
    picked.forEach(function (p) {
      var li = document.createElement('li');
      var span = document.createElement('span');
      span.className = 'wb-mono wb-fs1';
      span.textContent = p;
      var btn = document.createElement('button');
      btn.className = 'wb-iconbtn';
      btn.type = 'button';
      btn.textContent = '✕';
      btn.addEventListener('click', function () {
        picked = picked.filter(function (x) { return x !== p; });
        renderPicked();
      });
      li.appendChild(span);
      li.appendChild(btn);
      list.appendChild(li);
    });
    if (count) count.textContent = picked.length;
  }

  function addPicked(path) {
    if (picked.indexOf(path) === -1) picked.push(path);
    renderPicked();
  }

  function loadDrives() {
    fetch('/api/fs/drives').then(function (r) { return r.json(); }).then(function (j) {
      var box = $('wz-drives');
      if (!box || !j.ok) return;
      box.innerHTML = '';
      j.drives.forEach(function (d) {
        var b = document.createElement('button');
        b.className = 'wb-chip';
        b.type = 'button';
        b.textContent = d;
        b.addEventListener('click', function () { loadPath(d); });
        box.appendChild(b);
      });
      if (j.drives.length) loadPath(j.drives[0]);
    });
  }

  function loadPath(path) {
    fetch('/api/fs/list?path=' + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { window.wbToast(j.error || '读不了这个目录', true); return; }
        curPath = j.path;
        $('wz-path').textContent = j.path;
        var box = $('wz-dirs');
        box.innerHTML = '';

        var self = document.createElement('button');
        self.className = 'wb-btn sm';
        self.type = 'button';
        self.textContent = '就选这个目录';
        self.addEventListener('click', function () { addPicked(curPath); });
        box.appendChild(self);

        if (!j.dirs.length) {
          var empty = document.createElement('div');
          empty.className = 'wb-fs1 wb-mut2 wb-mt2';
          empty.textContent = '这层没有子目录了。';
          box.appendChild(empty);
          return;
        }

        j.dirs.forEach(function (d) {
          var row = document.createElement('div');
          // 系统目录沉到最后并标出来 —— 不隐藏（诚实），但别挡着常用目录
          row.className = 'wz-dir' + (d.system ? ' is-system' : '');

          var cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.setAttribute('aria-label', '选中 ' + d.name);
          cb.addEventListener('change', function () {
            if (cb.checked) addPicked(d.path);
            else { picked = picked.filter(function (x) { return x !== d.path; }); renderPicked(); }
          });

          var name = document.createElement('button');
          name.className = 'wz-dir-name';
          name.type = 'button';
          name.textContent = d.name;
          if (d.system) {
            var mark = document.createElement('span');
            mark.className = 'wz-sys';
            mark.textContent = '系统';
            name.appendChild(mark);
          }
          name.addEventListener('click', function () { loadPath(d.path); });

          row.appendChild(cb);
          row.appendChild(name);
          box.appendChild(row);
        });
      });
  }

  if ($('wz-up')) {
    $('wz-up').addEventListener('click', function () {
      fetch('/api/fs/list?path=' + encodeURIComponent(curPath))
        .then(function (r) { return r.json(); })
        .then(function (j) { if (j.ok) loadPath(j.parent); });
    });
  }

  /* ---------------------------------------------- 第一步：提交并扫描 */
  var rootsNext = $('wz-roots-next');
  if (rootsNext) {
    rootsNext.addEventListener('click', function () {
      var fd = new FormData();
      picked.forEach(function (p) { fd.append('paths', p); });
      var text = $('wz-text');
      if (text && text.value.trim()) fd.append('paths_text', text.value);

      if (!picked.length && !(text && text.value.trim())) {
        return show($('wz-roots-result'), '先选一个文件夹，或者直接粘路径进去', false);
      }

      rootsNext.disabled = true;
      show($('wz-roots-result'), '正在扫描，第一次会慢一点…', true);
      fetch('/api/welcome/roots', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          rootsNext.disabled = false;
          if (!j.ok) return show($('wz-roots-result'), j.error || '没加进去', false);
          show($('wz-roots-result'), '扫到 ' + j.scanned + ' 个项目', true);
          setTimeout(function () { goto(2); }, 500);
        })
        .catch(function (err) {
          rootsNext.disabled = false;
          show($('wz-roots-result'), '出错了：' + err, false);
        });
    });
  }

  /* ------------------------------------------------ 第二步：扫描本机 */
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  var scanBtn = $('wz-scan');
  if (scanBtn) {
    scanBtn.addEventListener('click', function () {
      var scopeEl = document.querySelector('input[name="wz-scope"]:checked');
      var scope = scopeEl ? scopeEl.value : 'full';
      var fd = new FormData();
      fd.append('scope', scope);

      scanBtn.disabled = true;
      fetch('/api/welcome/discover', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          scanBtn.disabled = false;
          if (!j.ok) return window.wbToast('扫描失败', true);
          $('wz-found').hidden = false;
          var tag = document.createElement('div');
          tag.className = 'wb-fs1 wb-mut2';
          tag.style.marginTop = '8px';
          tag.textContent = '当前范围：' + (j.scope === 'safe' ? '保守（只看文档）' : '无限制（含配置文件）');
          sshRoutes = j.ssh.config || [];
          knownHosts = j.ssh.known_hosts || [];
          projHosts = j.project_hosts || [];
          renderFound();
          if (tag) $('wz-found').appendChild(tag);
        })
        .catch(function (err) { scanBtn.disabled = false; window.wbToast('扫描失败：' + err, true); });
    });
  }

  function renderFound() {
    var total = 0;

    var secSsh = $('wz-sec-ssh');
    if (sshRoutes.length) {
      secSsh.hidden = false;
      $('wz-n-ssh').textContent = '（' + sshRoutes.length + ' 条）';
      var body = $('wz-ssh-body');
      body.innerHTML = '';
      sshRoutes.forEach(function (h, idx) {
        var tr = el('tr');
        var td1 = el('td');
        var cb = el('input');
        cb.type = 'checkbox';
        cb.setAttribute('data-ssh', String(idx));
        td1.appendChild(cb);
        var td2 = el('td', 'wb-mono wb-fs1', h.user + '@' + h.host + ':' + h.port);
        var td3 = el('td', 'wb-fs1 wb-mut2', h.alias + (h.key ? ' · 密钥 ' + h.key.split(/[\\/]/).pop() : ''));
        tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
        body.appendChild(tr);
        total++;
      });
    } else {
      secSsh.hidden = true;
    }

    var secKnown = $('wz-sec-known');
    if (knownHosts.length) {
      secKnown.hidden = false;
      var kb = $('wz-known-body');
      kb.innerHTML = '';
      knownHosts.forEach(function (h) {
        var label = el('label', 'wz-cand');
        var cb = el('input');
        cb.type = 'checkbox';
        cb.setAttribute('data-known', h);
        label.appendChild(cb);
        label.appendChild(document.createTextNode(h));
        kb.appendChild(label);
        total++;
      });
    } else {
      secKnown.hidden = true;
    }

    var secProj = $('wz-sec-proj');
    if (projHosts.length) {
      secProj.hidden = false;
      var pb = $('wz-proj-body');
      pb.innerHTML = '';
      projHosts.forEach(function (h, idx) {
        var label = el('label', 'wz-cand');
        var cb = el('input');
        cb.type = 'checkbox';
        cb.setAttribute('data-proj', String(idx));
        label.appendChild(cb);
        label.appendChild(document.createTextNode(h.host));
        label.title = '来自 ' + h.from;
        pb.appendChild(label);
        total++;
      });
    } else {
      secProj.hidden = true;
    }

    $('wz-none').hidden = total > 0;
  }

  /* ------------------------------------------------ 第二步：导入 */
  var srvNext = $('wz-srv-next');
  if (srvNext) {
    srvNext.addEventListener('click', function () {
      var items = [];

      document.querySelectorAll('[data-ssh]').forEach(function (cb) {
        if (!cb.checked) return;
        var h = sshRoutes[Number(cb.getAttribute('data-ssh'))];
        if (h) items.push({ name: h.alias || h.host, host: h.host, port: h.port, user: h.user, key: h.key });
      });

      document.querySelectorAll('[data-known]').forEach(function (cb) {
        if (!cb.checked) return;
        var host = cb.getAttribute('data-known');
        items.push({ name: host, host: host, port: 22, user: 'root', key: '' });
      });

      document.querySelectorAll('[data-proj]').forEach(function (cb) {
        if (!cb.checked) return;
        var h = projHosts[Number(cb.getAttribute('data-proj'))];
        if (h) items.push({ name: h.host, host: h.host, port: h.port || 22, user: h.user || 'root', key: '' });
      });

      if (!items.length) {
        // 没勾也没关系，直接进下一步
        goto(3);
        return;
      }

      var fd = new FormData();
      fd.append('items', JSON.stringify(items));
      srvNext.disabled = true;
      fetch('/api/welcome/servers', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          srvNext.disabled = false;
          if (!j.ok) return show($('wz-srv-result'), j.error || '导入失败', false);
          show($('wz-srv-result'), '导入 ' + j.created + ' 台' + (j.skipped ? '（跳过 ' + j.skipped + ' 台）' : ''), true);
          var sum = $('wz-summary');
          if (sum) sum.textContent = '项目档案里能看到你的项目了；服务器导入了 ' + j.created + ' 台，从「服务器和云资源」里点「刷新全部」验证连接。';
          setTimeout(function () { goto(3); }, 600);
        })
        .catch(function (err) {
          srvNext.disabled = false;
          show($('wz-srv-result'), '导入失败：' + err, false);
        });
    });
  }

  loadDrives();
})();
