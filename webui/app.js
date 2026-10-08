/* 安卓相册备份 · 本地管理页脚本
   纯原生 JS，无任何外部依赖（离线可用）。 */

(function () {
  'use strict';

  var TOKEN = new URLSearchParams(location.search).get('t') || '';
  var state = { tier: '可放心删', tiers: [], selected: new Set(), ready: false };

  /* ---------------- 工具 ---------------- */
  function $(id) { return document.getElementById(id); }

  function api(path, opts) {
    opts = opts || {};
    var sep = path.indexOf('?') >= 0 ? '&' : '?';
    var url = path + sep + 't=' + encodeURIComponent(TOKEN);
    opts.headers = Object.assign({ 'X-Token': TOKEN }, opts.headers || {});
    return fetch(url, opts).then(function (r) {
      var ct = r.headers.get('content-type') || '';
      if (ct.indexOf('application/json') >= 0) {
        return r.json().then(function (j) {
          if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
          return j;
        });
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r;
    });
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function setConn(ok, text) {
    $('connDot').className = 'dot ' + (ok ? 'on' : 'off');
    $('connText').textContent = text;
  }

  /* ---------------- 标签切换 ---------------- */
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (b) { b.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      $('panel-' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'cleanup') loadSummary();
    });
  });

  /* ---------------- 进度 ---------------- */
  function renderProgress(p) {
    if (!p) return;
    $('stage').textContent = p.stage || '准备中';
    $('barFill').style.width = (p.percent || 0) + '%';
    $('percent').textContent = (p.percent || 0).toFixed(1) + '%';
    $('bytes').textContent = (p.done_human || '0 B') + ' / ' + (p.total_human || '0 B');
    $('speed').textContent = p.speed > 0 ? (p.speed_human || '') : '—';
    $('currentFile').textContent = p.current_name
      ? p.current_name + (p.current_size ? '  (' + p.current_done + ' / ' + p.current_size + ' 字节)' : '')
      : (p.message || '—');
    $('stTotal').textContent = p.total_files || 0;
    $('stDone').textContent = p.done_files || 0;
    $('stSkip').textContent = p.skipped_files || 0;
    $('stFail').textContent = p.failed_files || 0;
    $('stElapsed').textContent = p.elapsed_human || '—';
    $('stEta').textContent = p.eta_human || '—';
  }

  function connectWS() {
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var ws = new WebSocket(proto + '://' + location.host + '/ws/progress?t=' + encodeURIComponent(TOKEN));
    ws.onopen = function () { setConn(true, '已连接'); };
    ws.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.ok && msg.progress) renderProgress(msg.progress);
      } catch (e) { /* 忽略单条异常消息 */ }
    };
    ws.onclose = function () {
      setConn(false, '已断开，重连中');
      setTimeout(connectWS, 2000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  /* ---------------- 状态概要 ---------------- */
  function loadState() {
    api('/api/state').then(function (j) {
      setConn(true, '已连接');
      var s = j.state || {};
      var bits = [];
      if (s.device) bits.push('设备：' + s.device);
      if (s.channel) bits.push('通道：' + s.channel);
      if (s.backup_root) bits.push('备份目录：' + s.backup_root);
      $('subtitle').textContent = bits.join('   ·   ') || '就绪';
      if (s.restore_dir) $('restoreDir').textContent = s.restore_dir;
      $('footInfo').textContent = '版本 ' + (j.version || '') +
        (s.backup_root ? '   ·   ' + s.backup_root : '');
      if (j.progress) renderProgress(j.progress);
    }).catch(function (e) {
      setConn(false, '无法连接');
      $('subtitle').textContent = '无法连接服务：' + e.message;
    });
  }

  /* ---------------- 清理清单 ---------------- */
  function loadSummary() {
    api('/api/summary').then(function (j) {
      state.ready = !!j.ready;
      state.tiers = j.tiers || [];
      renderTiers();
      if (state.ready) loadTier(state.tier);
      else $('cleanupBody').innerHTML = '<tr><td colspan="7" class="empty">备份尚未完成，暂无清单</td></tr>';
    }).catch(function (e) {
      $('cleanupBody').innerHTML = '<tr><td colspan="7" class="empty">' + esc(e.message) + '</td></tr>';
    });
  }

  function renderTiers() {
    var box = $('tierbar');
    if (!state.tiers.length) { box.innerHTML = ''; return; }
    box.innerHTML = state.tiers.map(function (t) {
      return '<button class="tier' + (t.name === state.tier ? ' active' : '') +
        '" data-tier="' + esc(t.name) + '">' +
        '<div class="tn">' + esc(t.name) + '</div>' +
        '<div class="tc">' + t.count + ' 个 · ' + esc(t.size_human) + '</div></button>';
    }).join('');
    box.querySelectorAll('.tier').forEach(function (b) {
      b.addEventListener('click', function () {
        state.tier = b.dataset.tier;
        state.selected.clear();
        updateSelInfo();
        renderTiers();
        loadTier(state.tier);
      });
    });
  }

  function loadTier(tier) {
    var kw = $('search').value.trim();
    api('/api/cleanup?tier=' + encodeURIComponent(tier) + '&limit=1000&keyword=' + encodeURIComponent(kw))
      .then(function (j) {
        if (!j.ready) {
          $('cleanupBody').innerHTML = '<tr><td colspan="7" class="empty">暂无清单</td></tr>';
          return;
        }
        if (!j.items.length) {
          $('cleanupBody').innerHTML = '<tr><td colspan="7" class="empty">该分档下没有文件</td></tr>';
          return;
        }
        $('cleanupBody').innerHTML = j.items.map(function (it) {
          var key = it.phone_path;
          return '<tr>' +
            '<td><input type="checkbox" class="rowsel" data-key="' + esc(key) + '"' +
              (state.selected.has(key) ? ' checked' : '') + '></td>' +
            '<td>' + esc(it.filename) + '</td>' +
            '<td class="c-num">' + esc(it.size_human) + '</td>' +
            '<td>' + esc(it.taken_at || '未知') + '</td>' +
            '<td class="c-hide"><span class="tag">' + esc(it.app_tag) + '</span></td>' +
            '<td class="path">' + esc(it.phone_path) + '</td>' +
            '<td class="c-hide muted">' + esc(it.note || '') + '</td>' +
            '</tr>';
        }).join('');
        $('cleanupBody').querySelectorAll('.rowsel').forEach(function (cb) {
          cb.addEventListener('change', function () {
            if (cb.checked) state.selected.add(cb.dataset.key);
            else state.selected.delete(cb.dataset.key);
            updateSelInfo();
          });
        });
        updateSelInfo();
      }).catch(function (e) {
        $('cleanupBody').innerHTML = '<tr><td colspan="7" class="empty">' + esc(e.message) + '</td></tr>';
      });
  }

  function updateSelInfo() {
    $('selInfo').textContent = '已选 ' + state.selected.size + ' 个';
  }

  $('selAll').addEventListener('change', function () {
    var on = $('selAll').checked;
    $('cleanupBody').querySelectorAll('.rowsel').forEach(function (cb) {
      cb.checked = on;
      if (on) state.selected.add(cb.dataset.key); else state.selected.delete(cb.dataset.key);
    });
    updateSelInfo();
  });

  var searchTimer = null;
  $('search').addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { loadTier(state.tier); }, 300);
  });

  function exportAs(fmt) {
    var url = '/api/cleanup/export?fmt=' + fmt + '&t=' + encodeURIComponent(TOKEN) +
      '&tier=' + encodeURIComponent(state.tier);
    window.open(url, '_blank');
  }
  $('btnCsv').addEventListener('click', function () { exportAs('csv'); });
  $('btnTxt').addEventListener('click', function () { exportAs('txt'); });

  /* ---------------- 反向下载 ---------------- */
  function selectedPaths() { return Array.from(state.selected); }

  function logLine(text) {
    var el = $('restoreLog');
    el.textContent += '\n' + text;
    el.scrollTop = el.scrollHeight;
  }

  $('btnZip').addEventListener('click', function () {
    if (!state.selected.size) { alert('请先在上面的表格里勾选文件'); return; }
    var paths = selectedPaths();
    var url = '/api/download/zip?t=' + encodeURIComponent(TOKEN) +
      '&paths=' + encodeURIComponent(paths.join('\n'));
    $('restoreLog').textContent = '正在打包 ' + paths.length + ' 个文件…';
    fetch(url, { headers: { 'X-Token': TOKEN } }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) { throw new Error(j.error || j.detail || ('HTTP ' + r.status)); });
      }
      return r.blob();
    }).then(function (blob) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = '恢复包_' + new Date().toISOString().slice(0, 10) + '.zip';
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 3000);
      $('restoreLog').textContent = '已开始下载：' + a.download;
    }).catch(function (e) { $('restoreLog').textContent = '打包失败：' + e.message; });
  });

  $('btnPush').addEventListener('click', function () {
    if (!state.selected.size) { alert('请先在上面的表格里勾选文件'); return; }
    $('restoreLog').textContent = '正在推送到手机…';
    api('/api/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: selectedPaths() })
    }).then(function (j) {
      (j.results || []).forEach(function (r) {
        logLine((r.ok ? '✔ ' : '✘ ') + r.remote_path + (r.detail ? '  ' + r.detail : ''));
      });
      logLine('完成。');
    }).catch(function (e) { logLine('失败：' + e.message); });
  });

  $('btnRefresh').addEventListener('click', function () {
    api('/api/refresh_media', { method: 'POST' }).then(function (j) {
      logLine(j.message || '已触发媒体库刷新');
    }).catch(function (e) { logLine('刷新失败：' + e.message); });
  });

  /* ---------------- 启动 ---------------- */
  loadState();
  connectWS();
  setInterval(loadState, 15000);
  loadSummary();
})();
