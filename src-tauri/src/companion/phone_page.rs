//! 手机端伴侣页面（S8）。
//!
//! 一个**自包含**的 HTML 字符串：无外部 CSS/JS/字体/CDN（手机可能没有外网），
//! 断网也能渲染。服务端只在 token 校验通过后才下发它。
//!
//! # 只读纪律（有测试钉住）
//!
//! 这份页面里**没有任何上行能力**：不 `fetch`、不建 `XMLHttpRequest`、
//! 不调用 `ws.send`（源码级 grep 断言）。它只 `onmessage` 然后渲染。
//! 二屏不跑第二份检索——同一个问题发两次检索是 P7 就钉死的纪律。

/// 手机端页面。扫码后由 `GET /?token=…` 返回。
pub const PAGE: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>InterviewCopilot 伴侣屏</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0 12px 24px;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
          "Microsoft YaHei", sans-serif;
    background: #f6f7f9; color: #111827;
  }
  header {
    position: sticky; top: 0; z-index: 1;
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; padding: 12px 0 8px; background: #f6f7f9;
    border-bottom: 1px solid #e5e7eb;
  }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .status { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #4b5563; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; }
  .dot.on { background: #16a34a; }
  .dot.bad { background: #dc2626; }
  ol { list-style: none; margin: 12px 0 0; padding: 0; }
  li {
    background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
    padding: 12px 14px; margin-bottom: 10px;
  }
  .q { margin: 0 0 8px; font-size: 15px; color: #6b7280; }
  .a { margin: 0; font-size: 18px; line-height: 1.6; white-space: pre-wrap; }
  .a.miss { color: #b45309; }
  .meta { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
  .tag { font-size: 12px; padding: 1px 8px; border-radius: 999px; background: #eef2ff; color: #4338ca; }
  .tag.err { background: #fee2e2; color: #b91c1c; }
  .empty { margin-top: 48px; text-align: center; color: #9ca3af; }
  .note { margin-top: 20px; font-size: 12px; color: #9ca3af; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>InterviewCopilot 伴侣屏</h1>
  <span class="status"><i id="dot" class="dot"></i><span id="status">连接中…</span></span>
</header>
<main>
  <ol id="list"></ol>
  <p id="empty" class="empty">等待主屏提词…</p>
</main>
<p class="note">本页为只读镜像：只显示主屏推送的卡片，不能操作主屏，也不做任何检索。</p>
<script>
(function () {
  var PROTOCOL_VERSION = 1;
  var dot = document.getElementById('dot');
  var statusEl = document.getElementById('status');
  var listEl = document.getElementById('list');
  var emptyEl = document.getElementById('empty');
  var ws = null;
  var retry = 0;
  var stopped = false;      // 被服务端吊销/二维码失效后不再重连
  var timer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    dot.className = 'dot' + (kind ? ' ' + kind : '');
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c];
    });
  }

  function render(cards) {
    if (!cards || cards.length === 0) {
      listEl.innerHTML = '';
      emptyEl.style.display = '';
      return;
    }
    emptyEl.style.display = 'none';
    listEl.innerHTML = cards.map(function (c) {
      var miss = c.kind === 'fail_closed';
      var a = miss ? '知识库未命中' : esc(c.answer);
      var tags = '';
      if (c.source) tags += '<span class="tag">' + esc(c.source) + '</span>';
      if (c.reused) tags += '<span class="tag">复用</span>';
      if (c.kind === 'error') tags += '<span class="tag err">检索失败</span>';
      return '<li><p class="q">' + esc(c.question) + '</p>' +
             '<p class="a' + (miss ? ' miss' : '') + '">' + a + '</p>' +
             (tags ? '<div class="meta">' + tags + '</div>' : '') + '</li>';
    }).join('');
  }

  function connect() {
    if (stopped || (ws && ws.readyState === 1)) return;
    var token = new URLSearchParams(location.search).get('token');
    if (!token) {
      stopped = true;
      setStatus('缺少 token，请重新扫码', 'bad');
      return;
    }
    var url = 'ws://' + location.host + '/ws?token=' + encodeURIComponent(token);
    setStatus('连接中…');
    ws = new WebSocket(url);

    ws.onopen = function () { retry = 0; setStatus('已连接', 'on'); };

    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'cards') render(msg.cards);
      else if (msg.type === 'welcome' && msg.protocolVersion !== PROTOCOL_VERSION) {
        setStatus('版本不一致，请更新主程序', 'bad');
      } else if (msg.type === 'revoked') {
        stopped = true;
        setStatus('已被主屏断开，请重新扫码', 'bad');
      }
    };

    ws.onclose = function (ev) {
      if (stopped) return;
      // 4001 = 服务端吊销（换 token / 停止服务 / 被顶掉），不再重连。
      if (ev.code === 4001) {
        stopped = true;
        setStatus('已被主屏断开，请重新扫码', 'bad');
        return;
      }
      retry += 1;
      if (retry > 5) { setStatus('连接已断开', 'bad'); return; }
      setStatus('连接断开，' + retry + ' 秒后重试…', 'bad');
      timer = setTimeout(connect, retry * 1000);
    };

    ws.onerror = function () { /* 状态由 onclose 统一处理 */ };
  }

  window.addEventListener('pagehide', function () {
    stopped = true;
    if (timer) clearTimeout(timer);
  });

  connect();
})();
</script>
</body>
</html>
"##;
