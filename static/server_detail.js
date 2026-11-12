/* 服务器详情：单台重连。
   点一下只连这一台，结果就地显示 —— 不用整页刷新，也不用等所有机器跑一遍。
   连不上时的兼容模式降级由后端自动做，这里只负责把结果讲清楚。 */
(function () {
  'use strict';

  // 详情页有结果条；列表页没有，就退化成 toast
  var box = document.getElementById('rc-result');

  function show(kind, title, detail) {
    if (!box) {
      window.wbToast(title + (detail ? '：' + String(detail).split('\n')[0] : ''), kind === 'err');
      return;
    }
    box.hidden = false;
    box.className = 'wb-rc is-' + kind;
    box.innerHTML = '';

    // 结果条出来了，页面上那块旧的错误就别再显示同一段话
    var old = document.getElementById('off-block');
    if (old) old.hidden = true;

    var t = document.createElement('div');
    t.className = 'wb-rc-t';
    t.textContent = title;
    box.appendChild(t);

    if (detail) {
      var d = document.createElement('div');
      d.className = 'wb-rc-d';
      d.textContent = detail;
      box.appendChild(d);
    }
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-reconnect], [data-reconnect-card]');
    if (!btn) return;

    var id = btn.getAttribute('data-reconnect') || btn.getAttribute('data-reconnect-card');
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = '连接中…';
    show('busy', '正在连…', '握手 + 认证 + 跑一次巡检，通常两三秒。');

    var fd = new FormData();
    fd.append('id', id);

    fetch('/api/server/reconnect', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        btn.disabled = false;
        btn.innerHTML = label;

        if (!j.ok) {
          return show('err', '连不上', j.error || '请求失败了');
        }
        if (j.online) {
          show('ok', j.auto_fixed || '连上了',
            j.auto_fixed ? '这台机器只认老加密算法，工作台已经自动切过去并把设置记住了，以后不用再管。' : '数据已更新。');
          setTimeout(function () { location.reload(); }, 1200);
          return;
        }

        // 失败：把原因原样摊开，再给下一步
        var hint = '点右上「编辑」可以改地址 / 端口 / 凭据，改完再点「重新连接」。';
        if (/握手谈不拢|kex/i.test(j.error || '')) {
          // 诊断信息后端已经说清楚了，这里只给"接下来做什么"，别重复
          hint = '下一步：先确认这台机器本身可信，再排查中间链路（防火墙 / 跳板 / 代理）。' +
                 '工作台这边没有别的开关可开了。';
        } else if (/认证失败/.test(j.error || '')) {
          hint = '凭据不对：到「编辑」里换密码或私钥。确认这台机器允许你的登录方式（有的机器禁了密码登录）。';
        } else if (/超时/.test(j.error || '')) {
          hint = '连不上端口：确认机器开机、IP 没变、防火墙放了 22 端口。';
        }
        show('err', '还是连不上', (j.error || '') + '\n\n' + hint);
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.innerHTML = label;
        show('err', '请求失败', String(err));
      });
  });
})();
