
const API = '';
let funds = [];
let pendingCode = null;
var _recCancelled = false;

function showMsg(text, type) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + type;
  el.style.animation = 'none';
  el.offsetHeight;
  el.style.animation = 'fadeIn 0.2s';
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function htmlEscape(s) {
  if (typeof s !== 'string') return s;
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

async function loadList() {
  const r = await fetch(API + '/api/list');
  const d = await r.json();
  funds = d.funds || [];
  render();
}

function render() {
  const el = document.getElementById('list');
  document.getElementById('count').textContent = funds.length + ' 只';
  if (!funds.length) {
    el.innerHTML = '<div class="empty">暂无监控基金</div>';
    return;
  }
  el.innerHTML = funds.map(f => {
    const name = htmlEscape(f.name || '');
    const code = htmlEscape(f.code || '');
    return `<div class="fund-row">
      <div class="info">
        <span class="name">${name || code}</span>
        <span class="code">${name ? code : ''}</span>
      </div>
      <button class="del-btn" onclick="openModal('${htmlEscape(f.code)}', '${htmlEscape(f.name || '')}')">删除</button>
    </div>`;
  }).join('');
}

async function addFunds() {
  const input = document.getElementById('addInput');
  const raw = input.value.trim();
  if (!raw) return;
  const codes = raw.split(/[\s,，、;；]+/).filter(Boolean);
  const r = await fetch(API + '/api/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ codes }),
  });
  const d = await r.json();
  input.value = '';
  if (d.ok) {
    const parts = [];
    if (d.added.length) parts.push('✅ 添加 ' + d.added.join(', '));
    if (d.skipped.length) parts.push('⏭️ 已存在 ' + d.skipped.join(', '));
    if (d.invalid.length) parts.push('⚠️ 格式错误 ' + d.invalid.join(', '));
    showMsg(parts.join('；') + `（当前 ${d.total} 只）`, 'ok');
    await loadList();
    loadSavedTables();
  } else {
    showMsg('❌ ' + (d.error || '添加失败'), 'err');
  }
}

function openModal(code, name) {
  pendingCode = code;
  document.getElementById('modalCode').textContent = code;
  document.getElementById('modalName').textContent = name;
  document.getElementById('modal').classList.add('show');
}

function closeModal() {
  pendingCode = null;
  document.getElementById('modal').classList.remove('show');
}

async function confirmRemove() {
  if (!pendingCode) return;
  const code = pendingCode;
  closeModal();
  const r = await fetch(API + '/api/remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ codes: [code] }),
  });
  const d = await r.json();
  if (d.ok) {
    showMsg('已移除 ' + code + '（当前 ' + d.total + ' 只）', 'ok');
    await loadList();
    loadSavedTables();
  } else {
    showMsg('❌ ' + (d.error || '移除失败'), 'err');
  }
}

document.getElementById('addInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') addFunds();
});

// 搜索建议（300ms 防抖，至少输入2个字符）
let searchTimer = null;
document.getElementById('addInput').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (!q) { closeSuggestions(); return; }
  if (q.length < 2) return; // 少于2个字符不搜索
  searchTimer = setTimeout(async () => {
    const r = await fetch(API + '/api/search?q=' + encodeURIComponent(q));
    const d = await r.json();
    showSuggestions(d.results || []);
  }, 300);
});

document.getElementById('addInput').addEventListener('blur', () => {
  setTimeout(closeSuggestions, 200);
});

function showSuggestions(results) {
  const el = document.getElementById('suggestions');
  if (!results.length) { el.classList.remove('show'); return; }
  el.innerHTML = results.map(r => `
    <div class="item" data-code="${r.code}" data-name="${r.name.replace(/"/g,'&quot;')}">
      <span class="sname">${r.name}</span>
      <span class="scode">${r.code}</span>
    </div>
  `).join('');
  el.classList.add('show');
  // 点击选中
  el.querySelectorAll('.item').forEach(item => {
    item.addEventListener('click', () => {
      const code = item.dataset.code;
      const name = item.dataset.name;
      document.getElementById('addInput').value = code;
      closeSuggestions();
    });
  });
}

function closeSuggestions() {
  document.getElementById('suggestions').classList.remove('show');
}

loadList();

async function loadFeatures() {
  const r = await fetch(API + '/api/tasks');
  const d = await r.json();
  if (!d.ok) return;
  const el = document.getElementById('features');
  el.innerHTML = d.tasks.map(t => {
    const running = t.running;
    let badgeHtml = '';
    if (running) {
      badgeHtml = '<span class="timer-badge running">▶ 正在运行</span>';
    } else if (t.timer_enabled) {
      badgeHtml = '<span class="timer-badge enabled">◉ 定时器已启用</span>';
    } else {
      badgeHtml = '<span class="timer-badge disabled">○ 定时器未启用</span>';
    }
    const statusClass = running ? 'ok' : 'unknown';
    const statusText = running ? '运行中' : (t.last_result === '0' ? '上次成功' : t.last_result ? '上次失败' : '');
    return `
      <div class="feature-row" data-task-id="${t.id}">
        <div class="feature-icon">${t.icon}</div>
        <div class="feature-body">
          <div class="feature-name">${t.label}</div>
          <div class="feature-desc">${t.desc || ''}</div>
          <div class="feature-info">${statusText}${t.next_run ? ' · 下次 ' + t.next_run : ''}<span class="feature-running"></span></div>
        </div>
        <div class="feature-meta">
          <span class="feature-time">${t.time}</span>${badgeHtml}
          <span class="feature-status ${statusClass}"></span>
          <span class="task-actions">
            <button class="task-btn start-btn" onclick="startTask('${t.id}')" title="启动">▶</button>
            <button class="task-btn stop-btn" onclick="stopTask('${t.id}')" title="停止">⏹</button>
          </span>
        </div>
      </div>
    `;
  }).join('');
}

async function startTask(taskId) {
  const btn = document.querySelector(`[data-task-id="${taskId}"] .start-btn`);
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(API + '/api/task/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_id: taskId}),
    });
    let d;
    try { d = await r.json(); } catch(e) { d = {ok: false, error: '响应解析失败'}; }
    if (d.ok) {
      showMsg('✔ ' + (d.message || '任务已启动'), 'ok');
    } else {
      showMsg('✖ ' + (d.error || '启动失败'), 'fail');
    }
  } catch(e) {
    showMsg('✖ 启动失败: ' + (e.message || e), 'fail');
  }
  if (btn) btn.disabled = false;
  setTimeout(loadFeatures, 2000);
}

async function stopTask(taskId) {
  if (!confirm('确定停止任务「' + taskId + '」？')) return;
  const btn = document.querySelector(`[data-task-id="${taskId}"] .stop-btn`);
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(API + '/api/task/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_id: taskId}),
    });
    let d;
    try { d = await r.json(); } catch(e) { d = {ok: false, error: '响应解析失败'}; }
    if (d.ok) {
      showMsg('✔ ' + (d.message || '任务已停止'), 'ok');
    } else {
      showMsg('✖ ' + (d.error || '停止失败'), 'fail');
    }
  } catch(e) {
    showMsg('✖ 停止失败: ' + (e.message || e), 'fail');
  }
  if (btn) btn.disabled = false;
  setTimeout(loadFeatures, 2000);
}


function updateHeartbeat() {
  fetch(API + '/api/heartbeat?_t=' + Date.now()).then(r => r.json()).then(d => {
    if (!d.ok) return;
    const hb = d.heartbeats || {};
    document.querySelectorAll('.feature-row').forEach(row => {
      const name = row.dataset.taskId;
      const el = row.querySelector('.feature-running');
      if (name && hb[name] && el) {
        el.textContent = '  ▶ 运行中';
        el.style.display = 'inline';
      } else if (el) {
        el.textContent = '';
        el.style.display = 'none';
      }
    });
  });
}

loadFeatures();
updateHeartbeat();
loadSavedTables();
setInterval(updateHeartbeat, 30000);

/** 页面加载时恢复已保存的推荐/自选基金表格 */
async function loadSavedTables() {
  try {
    var rtResp = await fetch(API + '/api/recommend-table' + '?_t=' + Date.now());
    if (rtResp.ok) {
      var rtHtml = await rtResp.text();
      if (rtHtml.indexOf('<tbody>') > 0) {
        var rtEl = document.getElementById('recommendFullTable');
        if (rtEl) { rtEl.innerHTML = rtHtml; document.getElementById('recommendFullTableCard').style.display = ''; }
      }
    }
  } catch(e) {}
  try {
    var ftResp = await fetch(API + '/api/fund-table' + '?_t=' + Date.now());
    if (ftResp.ok) {
      var ftHtml = await ftResp.text();
      if (ftHtml.indexOf('<tbody>') > 0) {
        var ftEl = document.getElementById('fundFullTable');
        if (ftEl) { ftEl.innerHTML = ftHtml; document.getElementById('fundFullTableCard').style.display = ''; setupDragSort(); }
      }
    }
  } catch(e) {}
  // 检查是否有推荐任务正在后台运行，有则恢复进度显示
  try {
    var hbR = await fetch(API + '/api/heartbeat?_t=' + Date.now()).then(function(r){ return r.json(); });
    if (hbR.ok && hbR.alive && hbR.alive.fund_recommend && hbR.heartbeats && hbR.heartbeats.fund_recommend) {
      _resumeRecommendProgress();
    }
  } catch(e) {}
}
/** 页面恢复时接手正在运行的推荐进度 */
function _resumeRecommendProgress() {
  _recCancelled = false;
  var btn = document.getElementById('recFilterBtn');
  var cancelBtn = document.getElementById('recCancelBtn');
  var prog = document.getElementById('recFilterProgress');
  var statusEl = document.getElementById('recFilterStatus');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 推荐中'; }
  if (cancelBtn) cancelBtn.style.display = 'inline-block';
  if (prog) prog.style.width = '5%';
  if (statusEl) statusEl.textContent = '恢复进度...';
  var MAX_WAIT = 1800000;
  var poll = setInterval(async function() {
    if (_recCancelled) { clearInterval(poll); return; }
    try {
      var hb = await fetch(API + '/api/heartbeat?_t=' + Date.now()).then(function(r){ return r.json(); });
      var recData = hb.heartbeats && hb.heartbeats.fund_recommend;
      var recAlive = hb.alive && hb.alive.fund_recommend;
      var recDone = recData && (recData.overall_pct >= 100 || recData.phase === '完成' || recData.phase === '刷新完成');
      if (recAlive && recData && !recDone) {
        var pct = recData.total > 0 ? Math.round(recData.progress / recData.total * 100) : 0;
        var displayPct = recData.overall_pct != null ? Math.round(recData.overall_pct) : pct;
        if (prog) prog.style.width = Math.min(displayPct, 99) + '%';
        var phaseIcon = {'获取排行':'📥','初筛':'📊','限购':'🔒','评分':'🧮','保存':'💾','完成':'✅',
                         '刷新td':'📋','更新涨跌':'📋','涨跌':'📋','重新评分':'📋','检查自选基金':'📋'};
        var icon = phaseIcon[recData.phase] || '⏳';
        var statusParts = [icon];
        if (recData.phase) statusParts.push(recData.phase);
        if (recData.detail) statusParts.push(recData.detail);
        if (recData.elapsed != null) statusParts.push(Math.round(recData.elapsed) + 's');
        if (statusEl) statusEl.textContent = statusParts.join(' | ');
        if (btn) btn.textContent = '⏳ ' + displayPct + '%';
      } else if (!recAlive || recDone) {
        clearInterval(poll);
        if (cancelBtn) cancelBtn.style.display = 'none';
        if (prog) prog.style.width = '100%';
        if (statusEl) statusEl.textContent = '⏳ 刷新实时涨跌...';
        if (btn) btn.textContent = '⏳ 刷新涨跌';
        var _tdStart = Date.now();
        poll = setInterval(async function() {
          if (_recCancelled) { clearInterval(poll); return; }
          try {
            var hb2 = await fetch(API + '/api/heartbeat?_t=' + Date.now()).then(function(r){ return r.json(); });
            var rec2 = hb2.heartbeats && hb2.heartbeats['recommend-td-refresh'];
            var tdAlive = hb2.alive && hb2.alive['recommend-td-refresh'];
            if (tdAlive && rec2) {
              var pct = rec2.total > 0 ? Math.round(rec2.progress / rec2.total * 100) : 0;
              if (prog) prog.style.width = Math.min(pct, 99) + '%';
              if (statusEl) statusEl.textContent = '📋 刷新td ' + (rec2.detail || '') + ' | ' + pct + '%';
              if (btn) btn.textContent = '⏳ ' + pct + '%';
            } else if (!tdAlive) {
              clearInterval(poll);
              if (prog) prog.style.width = '100%';
              if (statusEl) statusEl.textContent = '⏳ 渲染表格...';
              if (btn) btn.textContent = '⏳ 渲染表格';
              var rtPromise = fetch(API + '/api/recommend-table' + '?_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; });
              var ftPromise = fetch(API + '/api/fund-table?fresh=1&_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; });
              try { var _rt = await rtPromise; if (_rt && _rt.indexOf('<tbody>') > 0) { var _re = document.getElementById('recommendFullTable'); if (_re) { _re.innerHTML = _rt; document.getElementById('recommendFullTableCard').style.display = ''; } } else if (_rt) { var _re2 = document.getElementById('recommendFullTable'); if (_re2) { _re2.innerHTML = _rt; document.getElementById('recommendFullTableCard').style.display = ''; } } } catch(e) {}
              try { var _ft = await ftPromise; if (_ft && _ft.indexOf('<tbody>') > 0) { var _fe = document.getElementById('fundFullTable'); if (_fe) { _fe.innerHTML = _ft; document.getElementById('fundFullTableCard').style.display = ''; setupDragSort(); } } else if (_ft) { var _fe2 = document.getElementById('fundFullTable'); if (_fe2) { _fe2.innerHTML = _ft; document.getElementById('fundFullTableCard').style.display = ''; } } } catch(e) {}
              if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
              if (statusEl) statusEl.textContent = '✔ 已恢复（上次推荐已完成）';
              if (prog) prog.style.width = '0%';
              setTimeout(function() { if (prog) prog.style.width = '0%'; if (statusEl && statusEl.textContent.indexOf('已恢复') >= 0) statusEl.textContent = ''; }, 4000);
            }
          } catch(e) {}
        }, 800);
      }
    } catch(e) {}
  }, 800);
}

// ── 自选基金自动刷新 ──
var _autoRefreshTimer = null;
var _autoRefreshCountdown = null;
var _autoRefreshEnabled = false;
var _autoRefreshTicks = 0;
var _autoRefreshInterval = 600; // 默认10分钟（秒） // 距下次刷新的秒数

function toggleAutoRefresh() {
  var btn = document.getElementById('fundAutoRefreshBtn');
  var statusEl = document.getElementById('fundAutoRefreshStatus');
  if (_autoRefreshEnabled) {
    // 关闭
    _autoRefreshEnabled = false;
    if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
    if (_autoRefreshCountdown) { clearInterval(_autoRefreshCountdown); _autoRefreshCountdown = null; }
    if (btn) { btn.style.borderColor = 'rgba(255,255,255,0.1)'; btn.style.color = '#888'; btn.textContent = '🔄 自动刷新'; }
    if (statusEl) statusEl.textContent = '';
    _savePref('fundAutoRefresh', '0');
  } else {
    // 检查时间
    if (!_isTradingTime) {
      if (statusEl) statusEl.textContent = '非交易时间';
      setTimeout(function() { if (statusEl) statusEl.textContent = ''; }, 3000);
      return;
    }
    // 开启
    _autoRefreshEnabled = true;
    _autoRefreshTicks = _autoRefreshInterval;
    if (btn) { btn.style.borderColor = '#42a5f5'; btn.style.color = '#42a5f5'; btn.textContent = '🔄 自动刷新中'; }
    _updateCountdownDisplay();
    _autoRefreshCountdown = setInterval(function() {
      _autoRefreshTicks--;
      if (_autoRefreshTicks <= 0) _autoRefreshTicks = 0;
      _updateCountdownDisplay();
    }, 1000);
    _doAutoRefresh();
    _savePref('fundAutoRefresh', '1');
    _autoRefreshTimer = setInterval(_doAutoRefresh, _autoRefreshInterval * 1000);
  }
}

/** 保存偏好设置到服务端 */
function _savePref(key, val) {
  fetch(API + '/api/prefs').then(function(r){ return r.json(); }).then(function(d){
    var prefs = (d.ok && d.prefs) || {};
    prefs[key] = val;
    fetch(API + '/api/prefs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prefs:prefs})});
  }).catch(function(){});
}
/** 保存刷新间隔选择 */
function _saveRefreshInterval() {
  var sel = document.getElementById('fundRefreshInterval');
  if (!sel) return;
  _autoRefreshInterval = parseInt(sel.value, 10) || 600;
  _savePref('fundRefreshInterval', String(_autoRefreshInterval));
  // 如果正在刷新中，重置倒计时和定时器
  if (_autoRefreshEnabled) {
    _autoRefreshTicks = _autoRefreshInterval;
    _updateCountdownDisplay();
    if (_autoRefreshTimer) {
      clearInterval(_autoRefreshTimer);
      _autoRefreshTimer = setInterval(_doAutoRefresh, _autoRefreshInterval * 1000);
    }
  }
}

function _updateCountdownDisplay() {
  if (!_autoRefreshEnabled) return;
  var statusEl = document.getElementById('fundAutoRefreshStatus');
  var min = Math.floor(_autoRefreshTicks / 60);
  var sec = _autoRefreshTicks % 60;
  if (statusEl) statusEl.textContent = min + '分' + (sec < 10 ? '0' : '') + sec + '秒后刷新';
}

function _doAutoRefresh() {
  if (!_autoRefreshEnabled) return;
  // 收盘后（≥15:00）自动停止自动刷新
  if (new Date().getHours() >= 15) {
    toggleAutoRefresh();
    return;
  }
  _autoRefreshTicks = _autoRefreshInterval;
  var statusEl = document.getElementById('fundAutoRefreshStatus');
  if (statusEl) statusEl.textContent = '刷新中...';
  _updateCountdownDisplay();
  fetch(API + '/api/fund-table?fresh=1&_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; }).then(function(h){
    if (h && h.indexOf('<tbody>') > 0) {
      var ct = document.getElementById('fundFullTable');
      if (ct) { ct.innerHTML = h; setupDragSort(); }
    }
    if (statusEl && _autoRefreshEnabled) _updateCountdownDisplay();
  }).catch(function(){
    if (statusEl && _autoRefreshEnabled) _updateCountdownDisplay();
  });
}

// ── 交易时间判断（统一入口，节省流量）──
var _isTradingTime = false;

// ── 大盘指数看板 ──
var _marketTimer = null;
function loadMarketIndices() {
  // 收盘后（≥15:00）停止大盘定时刷新
  var _nowH = new Date().getHours();
  if (_nowH >= 15 && _marketTimer) {
    clearInterval(_marketTimer);
    _marketTimer = null;
    return;
  }
  fetch(API + '/api/market-indices?_t=' + Date.now()).then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok || !d.indices || !d.indices.length) { document.getElementById('marketBoard').style.display = 'none'; return; }
    document.getElementById('marketBoard').style.display = '';
    var container = document.getElementById('marketIndices');
    var timeEl = document.getElementById('marketTime');
    var html = '';
    for (var i = 0; i < d.indices.length; i++) {
      var idx = d.indices[i];
      var color = idx.change_pct > 0 ? '#ef5350' : (idx.change_pct < 0 ? '#66bb6a' : '#888');
      var sign = idx.change_pct > 0 ? '+' : '';
      var ptsSign = idx.change_points > 0 ? '+' : '';
      html += '<div style="background:rgba(255,255,255,0.03);border-radius:8px;padding:8px 10px;" data-idx-name="' + idx.name + '">'
        + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;">'
        + '<span style="color:#aaa;font-size:12px;">' + idx.name + '</span>'
        + '<span style="font-family:Consolas;font-weight:600;font-size:14px;color:#e0e0e0;">' + idx.price.toFixed(2) + '</span>'
        + '</div>'
        + '<div style="display:flex;align-items:center;gap:8px;">'
        + '<span class="marketSparkline" style="display:inline-block;width:160px;height:48px;flex-shrink:0;"></span>'
        + '<span style="font-family:Consolas;font-size:12px;color:' + color + ';">' + ptsSign + idx.change_points.toFixed(2) + '</span>'
        + '<span style="font-family:Consolas;font-size:12px;font-weight:600;color:' + color + ';">' + sign + idx.change_pct.toFixed(2) + '%</span>'
        + '</div>'
        + '</div>';
    }
    container.innerHTML = html;
    timeEl.textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    // 加载分时折线图
    _renderMarketSparklines();
  }).catch(function(){});
}
/** 渲染大盘分时折线图 */
var _marketTrendData = [];
/** 解析交易时间偏移量：09:30→0, 11:30→120, 13:00→120, 15:00→240 */
function _parseTradingOffset(tStr) {
  var m = tStr.match(/(\d{2}):(\d{2})/);
  if (!m) return 0;
  var mins = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  if (mins < 570) return 0;       // 09:30之前
  if (mins <= 690) return mins - 570; // 09:30-11:30
  if (mins < 780) return 120;     // 11:30-13:00 午休
  return 120 + (mins - 780);      // 13:00-15:00
}
function _renderMarketSparklines() {
  fetch(API + '/api/market-trends?_t=' + Date.now()).then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok || !d.trends) return;
    _marketTrendData = d.trends;
    var containers = document.querySelectorAll('.marketSparkline');
    for (var i = 0; i < d.trends.length; i++) {
      var t = d.trends[i];
      // 优先用 points（含 offset），降级到 closes
      var ptsData = t.points || t.closes.map(function(c, idx){ return {close: c, offset: (t.points && t.points[idx] ? t.points[idx].offset : idx / (t.closes.length-1) * 240)}; });
      var closes = ptsData.map(function(p){ return p.close; });
      if (!closes || closes.length < 2) continue;
      var mn = closes[0], mx = closes[0];
      for (var k = 1; k < closes.length; k++) {
        if (closes[k] < mn) mn = closes[k];
        if (closes[k] > mx) mx = closes[k];
      }
      var range = mx - mn || 1;
      var sw = 160, sh = 48, pad = 2, maxOff = 240;
      var svgPts = [];
      for (var k = 0; k < ptsData.length; k++) {
        var off = ptsData[k].offset !== undefined ? ptsData[k].offset : k / (ptsData.length - 1) * maxOff;
        var x = off / maxOff * (sw - pad * 2) + pad;
        var y = sh - pad - (closes[k] - mn) / range * (sh - pad * 2);
        svgPts.push(x.toFixed(1) + ',' + y.toFixed(1));
      }
      var basePrice = t.pre_close || closes[0];
      var lineColor = closes[closes.length - 1] >= basePrice ? '#ef5350' : '#66bb6a';
      var polyPts = svgPts.join(' ');
      var bgPts = polyPts + ' ' + (sw - pad).toFixed(1) + ',' + (sh - pad).toFixed(1) + ' ' + pad.toFixed(1) + ',' + (sh - pad).toFixed(1);
      var svg = '<svg width="160" height="48" viewBox="0 0 160 48" style="display:block;cursor:pointer;" onclick="showMarketTrend(' + i + ')">'
        + '<polyline fill="none" stroke="' + lineColor + '" stroke-width="2" points="' + polyPts + '"/>'
        + '<polygon fill="' + lineColor + '" fill-opacity="0.08" points="' + bgPts + '"/>'
        + '</svg>';
      if (containers[i]) containers[i].innerHTML = svg;
    }
  }).catch(function(){});
}
/** 大盘分时折线图弹窗 */
function showMarketTrend(idx) {
  var t = _marketTrendData[idx];
  if (!t) return;
  var ptsData = t.points || t.closes.map(function(c, i){ return {close: c, offset: i / (t.closes.length-1) * 240}; });
  var closes = ptsData.map(function(p){ return p.close; });
  if (!closes || closes.length < 2) return;
  var name = t.name;
  var mn = closes[0], mx = closes[0];
  for (var k = 1; k < closes.length; k++) {
    if (closes[k] < mn) mn = closes[k];
    if (closes[k] > mx) mx = closes[k];
  }
  var range = mx - mn || 1;
  var maxOff = 240;
  var w = 480, h = 220, pad = 30, cw = w - pad * 2, ch = h - pad * 2;
  var pts = [];
  for (var i = 0; i < ptsData.length; i++) {
    var off = ptsData[i].offset !== undefined ? ptsData[i].offset : i / (ptsData.length - 1) * maxOff;
    var x = pad + off / maxOff * cw;
    var y = pad + ch - (closes[i] - mn) / range * ch;
    pts.push({x: x, y: y, idx: i, val: closes[i], off: off});
  }
  var basePrice = t.pre_close || closes[0];
  var lineColor = closes[closes.length - 1] >= basePrice ? '#ef5350' : '#66bb6a';
  var polyPts = pts.map(function(p){ return p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' ');
  var svgHtml = '<svg id="mktTrendSvg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="display:block;margin:0 auto;background:transparent;cursor:crosshair;">'
    + '<polyline fill="none" stroke="' + lineColor + '" stroke-width="2" points="' + polyPts + '"/>'
    + '<polygon fill="' + lineColor + '" fill-opacity="0.08" points="' + polyPts + ' ' + (pad+cw) + ',' + (pad+ch) + ' ' + pad + ',' + (pad+ch) + '"/>'
    + '<line id="mktCrosshair" x1="0" y1="0" x2="0" y2="0" stroke="#888" stroke-width="1" stroke-dasharray="4,3" opacity="0"/>'
    + '</svg>';
  var backdrop = document.getElementById('trendBackdrop');
  if (!backdrop) {
    backdrop = document.createElement('div');
    backdrop.id = 'trendBackdrop';
    backdrop.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:99998;display:flex;align-items:center;justify-content:center;';
    backdrop.onclick = function(){ backdrop.style.display = 'none'; };
    document.body.appendChild(backdrop);
  }
  backdrop.style.display = 'flex';
  backdrop.innerHTML = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;max-width:550px;width:90%;" onclick="event.stopPropagation();">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
    + '<span style="color:#e0e0e0;font-size:15px;font-weight:600;">' + name + ' - 今日分时走势</span>'
    + '<span onclick="document.getElementById(\'trendBackdrop\').style.display=\'none\'" style="cursor:pointer;color:#555;font-size:20px;">&times;</span></div>'
    + '<div id="mktTooltip" style="text-align:center;font-size:13px;color:#e0e0e0;min-height:22px;margin-bottom:6px;">\u79fb\u52a8\u9f20\u6807\u67e5\u770b\u8be6\u60c5</div>'
    + svgHtml + '</div>';
  var svg = document.getElementById('mktTrendSvg');
  var crosshair = document.getElementById('mktCrosshair');
  var tooltip = document.getElementById('mktTooltip');
  svg.onmousemove = function(e) {
    var rect = svg.getBoundingClientRect();
    var scaleX = w / rect.width;
    var mouseX = (e.clientX - rect.left) * scaleX;
    var nearest = pts[0];
    for (var k = 1; k < pts.length; k++) {
      if (Math.abs(pts[k].x - mouseX) < Math.abs(nearest.x - mouseX)) nearest = pts[k];
    }
    var openVal = t.pre_close || closes[0];
    var chg = nearest.val - openVal;
    var pct = openVal ? (chg / openVal * 100) : 0;
    var sign = chg >= 0 ? '+' : '';
    var color = chg >= 0 ? '#ef5350' : '#66bb6a';
    // 用offset反推时间
    var _off = nearest.off;
    var _hh, _mm;
    if (_off <= 120) {
      _hh = 9; _mm = 30 + _off;
    } else {
      _hh = 13; _mm = _off - 120;
    }
    var timeStr = ('0' + _hh).slice(-2) + ':' + ('0' + _mm).slice(-2);
    tooltip.innerHTML = '<span style="color:#aaa;">' + timeStr + '</span> '
      + '<span style="font-family:Consolas;color:#e0e0e0;">' + nearest.val.toFixed(2) + '</span> '
      + '<span style="font-family:Consolas;color:' + color + ';font-weight:600;">' + sign + chg.toFixed(2) + '</span> '
      + '<span style="font-family:Consolas;color:' + color + ';">' + sign + pct.toFixed(2) + '%</span>';
    crosshair.setAttribute('x1', nearest.x);
    crosshair.setAttribute('x2', nearest.x);
    crosshair.setAttribute('y1', pad);
    crosshair.setAttribute('y2', pad + ch);
    crosshair.setAttribute('opacity', '0.6');
  };
  svg.onmouseleave = function() {
    crosshair.setAttribute('opacity', '0');
    tooltip.innerHTML = '\u79fb\u52a8\u9f20\u6807\u67e5\u770b\u8be6\u60c5';
  };
}

// ── 大盘看板：页面加载立即拉取行情，再检测交易时间决定是否开启定时刷新 ──
loadMarketIndices();
(function _initMarketTimer() {
  fetch(API + '/api/check-trade-time?_t=' + Date.now()).then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok) return;
    _isTradingTime = d.is_trading === true;
    // 从服务端恢复偏好设置
    _loadPrefs();
    if (_isTradingTime) {
      _marketTimer = setInterval(loadMarketIndices, 60000);
    } else if (d.next_check_seconds > 0) {
      setTimeout(function(){
        _isTradingTime = true;
        loadMarketIndices();
        _marketTimer = setInterval(loadMarketIndices, 60000);
      }, d.next_check_seconds * 1000);
    }
  }).catch(function(){
    // 检测失败则持续重试（大盘定时器依赖这个结果）
    setTimeout(_initMarketTimer, 5000);
  });
})();
/** 从服务端加载偏好设置 */
function _loadPrefs() {
  fetch(API + '/api/prefs').then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok || !d.prefs) return;
    var savedInterval = parseInt(d.prefs.fundRefreshInterval, 10);
    if (savedInterval && [60,120,300,600,1800].indexOf(savedInterval) >= 0) {
      _autoRefreshInterval = savedInterval;
      var sel = document.getElementById('fundRefreshInterval');
      if (sel) sel.value = savedInterval;
    }
    if (_isTradingTime && d.prefs.fundAutoRefresh === '1') toggleAutoRefresh();
  }).catch(function(){});
}

function setupDragSort() {
  var tbody = document.querySelector('#fundFullTable table tbody');
  if (!tbody) return;
  var rows = tbody.querySelectorAll('tr');
  for (var i = 0; i < rows.length; i++) {
    rows[i].draggable = true;
    rows[i].style.cursor = 'grab';
    rows[i].ondragstart = function(e) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', Array.from(tbody.children).indexOf(this));
      this.style.opacity = '0.4';
    };
    rows[i].ondragend = function(e) {
      this.style.opacity = '1';
      document.querySelectorAll('#fundFullTable table tbody tr').forEach(function(r) { r.style.borderBottom = ''; r.style.background = ''; });
    };
    rows[i].ondragover = function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('#fundFullTable table tbody tr').forEach(function(r) { r.style.borderBottom = ''; r.style.background = ''; });
      this.style.borderBottom = '2px solid #42a5f5';
      this.style.background = 'rgba(66,165,245,0.08)';
    };
    rows[i].ondragleave = function(e) {
      this.style.borderBottom = '';
      this.style.background = '';
    };
    rows[i].ondrop = function(e) {
      e.preventDefault();
      this.style.borderBottom = '';
      this.style.background = '';
      var fromIdx = parseInt(e.dataTransfer.getData('text/plain'));
      if (isNaN(fromIdx)) return;
      var allRows = Array.from(tbody.children);
      var toIdx = allRows.indexOf(this);
      if (fromIdx === toIdx) return;
      // 获取重排后的 code 顺序
      var codes = [];
      for (var j = 0; j < allRows.length; j++) {
        var codeCell = allRows[j].querySelector('td:first-child');
        if (codeCell) { codes.push(codeCell.textContent.trim()); }
      }
      // 交换
      var item = codes.splice(fromIdx, 1)[0];
      codes.splice(toIdx, 0, item);
      // 发送到后端保存
      fetch(API + '/api/reorder-funds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({codes: codes}),
      }).then(function(r){ return r.json(); }).then(function(d){
        if (d.ok) { /* 重刷表格以反映新顺序 */ loadFundTable(); }
      });
    };
  }
}

// 加载自选基金表（独立刷新用）
function loadFundTable() {
  var ct = document.getElementById('fundFullTable');
  if (!ct) return;
  ct.innerHTML = '<p style="padding:12px 4px;color:#666;">⏳ 加载中…</p>';
  fetch(API + '/api/fund-table' + '?_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; }).then(function(h){
    if (h && h.indexOf('<tbody>') > 0) { ct.innerHTML = h; document.getElementById('fundFullTableCard').style.display = ''; setupDragSort(); }
    else if (h) { ct.innerHTML = h; document.getElementById('fundFullTableCard').style.display = ''; }
  });
}

async function loadHeartbeat() {
  const r = await fetch(API + '/api/heartbeat?_t=' + Date.now());
  const d = await r.json();
  if (!d.ok) return;
  const hb = d.heartbeats || {};
  // 给每个正在运行的任务的行加一个"运行中"标记
  document.querySelectorAll('.feature-row').forEach(row => {
    const name = row.dataset.taskId;
    if (name && hb[name]) {
      row.querySelector('.feature-running').textContent = '▶ 运行中';
      row.querySelector('.feature-running').style.display = 'inline';
    } else if (name) {
      row.querySelector('.feature-running').textContent = '';
      row.querySelector('.feature-running').style.display = 'none';
    }
  });
}
setInterval(loadFeatures, 30000);




var ALL_DIM_KEYS = ['y1','m3','m1','sharpe','win_rate','profit_ratio','sortino','recovery','sy6','sy3','max_dd','rate','scale','annual_return','institutional','f5','sy2','volatility','calmar','max_loss_days','td'];
var DIM_INFO = {
  'y1': {name:'近1年收益',desc:'最近一年的表现',cat:'perf'},
  'm3': {name:'近3月收益',desc:'近三个月涨跌幅',cat:'perf'},
  'm1': {name:'近1月收益',desc:'近一个月涨跌幅',cat:'perf'},
  'f5': {name:'近一周收益',desc:'近五个交易日涨跌幅',cat:'perf'},
  'sy6': {name:'近6月收益',desc:'近六个月表现',cat:'perf'},
  'sy2': {name:'近2年收益',desc:'近两年精确收益',cat:'perf'},
  'sy3': {name:'近3年收益',desc:'近3年精确计算',cat:'perf'},
  'annual_return': {name:'年化收益率',desc:'基金成立以来年化回报',cat:'perf'},
  'max_dd': {name:'最大回撤',desc:'历史最大跌幅',cat:'risk'},
  'volatility': {name:'波动率',desc:'年化波动率',cat:'risk'},
  'max_loss_days': {name:'最大连跌天数',desc:'历史最长连续下跌天数',cat:'risk'},
  'sharpe': {name:'夏普比率',desc:'每承受 1 份波动能换来的收益',cat:'quality'},
  'sortino': {name:'索提诺比率',desc:'只考虑下跌波动',cat:'quality'},
  'profit_ratio': {name:'盈亏比',desc:'平均盈利/亏损',cat:'quality'},
  'win_rate': {name:'上行胜率',desc:'赚钱天数比例',cat:'quality'},
  'recovery': {name:'修复系数',desc:'总收益/最大回撤',cat:'quality'},
  'calmar': {name:'卡玛比率',desc:'年化收益/最大回撤',cat:'quality'},
  'rate': {name:'费率',desc:'申购费',cat:'other'},
  'scale': {name:'基金规模',desc:'1~50亿最理想',cat:'other'},
  'institutional': {name:'机构持有比例',desc:'专业机构认可度',cat:'other'},
  'td': {name:'当日涨跌',desc:'当日实时涨跌幅',cat:'perf'},
};
_CAT_NAMES = {perf:'📈 收益表现', risk:'🛡️ 风险指标', quality:'⭐ 收益性价比', other:'📋 基金特征'};
_CAT_ORDER = ['perf','risk','quality','other'];


// ── 20 个维度的默认评分曲线断点 ──────────────
// 与 fund_scoring.py 的 _DEFAULT_CURVES 保持一致
_DEFAULT_CURVES = {
  y1:              [[0,0], [20,50], [50,80], [100,100]],
  m3:              [[0,0], [10,50], [30,80], [60,100]],
  m1:              [[0,0], [5,50], [15,80], [30,100]],
  f5:              [[0,0], [5,70], [10,100]],
  sy6:             [[0,10], [20,60], [50,90], [100,100]],
  sy2:             [[0,0], [30,20], [60,40], [100,70], [200,100]],
  sy3:             [[0,0], [30,20], [60,40], [100,70], [200,100]],
  annual_return:   [[0,0], [5,20], [15,60], [30,90], [60,100]],
  sharpe:          [[0,0], [0.5,30], [1,70], [1.5,100]],
  sortino:         [[0,0], [0.5,20], [1,60], [2,100]],
  profit_ratio:    [[0,0], [1,20], [2,100]],
  win_rate:        [[30,10], [50,40], [70,100]],
  recovery:        [[0,0], [5,20], [20,60], [50,100]],
  max_dd:          [[0,90], [16.67,90], [20,86], [50,50], [75,20], [91.67,0]],
  volatility:      [[10,100], [20,80], [40,40], [60,0]],
  calmar:          [[0,0], [0.3,20], [1,60], [3,100]],
  max_loss_days:   [[3,100], [7,80], [15,40], [30,0]],
  rate:            [[0,100], [0.15,80], [0.5,40], [1.5,0]],
  scale:           [[0,0], [1,70], [20,100], [50,70], [100,30]],
  institutional:   [[5,10], [30,50], [60,90]],
  td:              [[-5,0], [-2,40], [0,60], [2,80], [5,100]],
};

// 把后端返回的 curve 转成 JS 断点数组，没有则用默认
function _getCurvePoints(dimKey, serverCurve) {
  if (serverCurve && serverCurve.points && serverCurve.points.length >= 2) {
    return serverCurve.points;
  }
  var def = _DEFAULT_CURVES[dimKey];
  return def ? def.map(function(p){ return [p[0], p[1]]; }) : [[0,0],[100,100]];
}


async function loadDims(customDims) {
  var dims;
  if (customDims) {
    dims = customDims;
  } else {
    const r = await fetch(API + '/api/dims');
    const d = await r.json();
    if (!d.ok || !d.dims) return;
    dims = d.dims;
  }
  const el = document.getElementById('dimsList');
  if (!el) return;
  const allKeys = ['y1','m3','m1','sharpe','win_rate','profit_ratio','sortino','recovery','sy6','sy3','max_dd','rate','scale','annual_return','institutional','f5','sy2','volatility','calmar','max_loss_days'];
  const usedKeys = dims.map(function(x){ return x.key; });
  
  el.innerHTML = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;" id="dimsTable"><thead><tr style="background:#2a2a2a;">'
    + '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;">开关</th>'
    + '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;">维度</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;">权重</th>'
    + '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;">说明</th>'
    + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;">曲线</th>'
    + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;"></th>'
    + '</tr></thead><tbody>'
    + function() {
        var cats = {};
        dims.forEach(function(dim) {
            var cat = dim.category || DIM_INFO[dim.key]?.cat || '';
            if (!cats[cat]) cats[cat] = [];
            cats[cat].push(dim);
        });
        var html = '';
        _CAT_ORDER.forEach(function(cat) {
            var items = cats[cat];
            if (!items || items.length === 0) return;
            html += '<tr style="background:rgba(255,255,255,0.03);"><td colspan="6" style="padding:8px 6px 4px;font-size:12px;color:#888;letter-spacing:1px;border-bottom:none;">' + (_CAT_NAMES[cat] || cat) + '</td></tr>';
            items.forEach(function(dim) {
                const pct = Math.round(dim.weight * 100);
                const pts = _getCurvePoints(dim.key, dim.curve);
                html += '<tr>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><input type="checkbox" ' + (dim.enabled !== false ? 'checked' : '') + ' onchange="markDimsDirty()"></td>'
          + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;">' + htmlEscape(dim.name) + '<br><span style="color:#555;font-size:10px;font-family:Consolas;">' + htmlEscape(dim.key) + '</span></td>'
          + '<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;"><input type="number" min="0" max="100" value="' + pct + '" style="width:50px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:2px 6px;text-align:right;font-family:Consolas;font-size:12px;" onchange="markDimsDirty()">%</td>'
          + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#666;font-size:11px;">' + htmlEscape(dim.desc) + '</td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="toggleCurveEditor(this)" style="background:none;border:none;color:#42a5f5;cursor:pointer;font-size:14px;" title="调整评分曲线">📐</button></td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="removeDim(this)" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:14px;">✖</button></td>'
          + '</tr>'
          + '<tr class="curve-edit-row" style="display:none;background:rgba(255,255,255,0.02);">'
          + '<td colspan="6" style="padding:6px 12px;">'
          + '<div style="font-size:11px;color:#888;margin-bottom:4px;">评分曲线断点（输入值 → 得分）</div>'
          + '<div style="display:flex;gap:12px;align-items:flex-start;">'
          + '<table style="border-collapse:collapse;font-size:11px;" id="curve-tbl-' + dim.key + '">'
          + pts.map(function(p, j) {
            return '<tr><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[0] + '" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + dim.key + '\')"> →</td><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[1] + '" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + dim.key + '\')"></td><td style="padding:2px 4px;">' + (j > 0 ? '<button onclick="this.closest(\'tr\').remove();markDimsDirty();drawCurvePreview(\'' + dim.key + '\')" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button>' : '') + '</td></tr>';
          }).join('')
          + '<tr><td colspan="3" style="padding:2px 4px;"><button onclick="addCurvePoint(this,\'' + dim.key + '\')" style="background:none;border:1px dashed rgba(255,255,255,0.2);border-radius:3px;color:#888;padding:2px 10px;font-size:10px;cursor:pointer;">+ 添加断点</button></td></tr>'
          + '</table>'
          + '<div id="curve-preview-' + dim.key + '" style="flex:1;min-width:0;"></div>'
          + '</div>'
          + '</td></tr>';
            });
        });
        return html;
    }()
    + '</tbody></table></div>'
    + '<div id="addDimArea" style="padding:8px 4px;text-align:center;"></div>';
  renderAddDimBtn();
  updateDimsTotal();
  // 重置按钮为绿色（未修改状态）
  var saveBtn = document.getElementById('dimsSaveBtn');
  if (saveBtn) saveBtn.style.background = 'linear-gradient(135deg,#66bb6a,#43a047)';
}

function updateDimsTotal() {
  var total = 0;
  document.querySelectorAll('#dimsTable tbody tr').forEach(function(row) {
    var cb = row.querySelector('input[type=checkbox]');
    var inp = row.querySelector('td:nth-child(3) input[type=number]');
    if (cb && inp && cb.checked) total += parseInt(inp.value) || 0;
  });
  var el = document.getElementById('dimsTotal');
  if (!el) {
    el = document.createElement('div');
    el.id = 'dimsTotal';
    el.style.cssText = 'text-align:right;padding:4px 4px 8px;font-size:12px;';
    document.getElementById('addDimArea').before(el);
  }
  var color = total === 100 ? '#66bb6a' : '#ef5350';
  el.innerHTML = '合计: <span style="font-weight:600;color:' + color + ';">' + total + '%</span>';
}
function renderAddDimBtn() {
  const area = document.getElementById('addDimArea');
  if (!area) return;
  const rows = document.querySelectorAll('#dimsTable tbody tr');
  const used = [];
  rows.forEach(function(r){ const s=r.querySelector('td:nth-child(2) span'); if(s) used.push(s.textContent); });
  const avail = ALL_DIM_KEYS.filter(function(k){ return used.indexOf(k) < 0; });
  if (avail.length === 0) {
    area.innerHTML = '<span style="font-size:11px;color:#555;">已包含所有维度</span>';
    return;
  }
  area.innerHTML = '<select id="addDimSelect" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.15);border-radius:4px;color:#ccc;padding:4px 8px;font-size:12px;">'
    + avail.map(function(k){ return '<option value="' + k + '">' + DIM_INFO[k].name + ' (' + k + ')</option>'; }).join('')
    + '</select> '
    + '<button onclick="doAddDim()" style="background:rgba(255,255,255,0.05);border:1px dashed rgba(255,255,255,0.2);border-radius:6px;color:#888;padding:6px 16px;font-size:12px;cursor:pointer;">+ 添加</button>';
}

function doAddDim() {
  const sel = document.getElementById('addDimSelect');
  if (!sel) return;
  const key = sel.value;
  const info = DIM_INFO[key];
  if (!info) return;
  const tbody = document.querySelector('#dimsTable tbody');
  const tr = document.createElement('tr');
  const pts = _getCurvePoints(key, null);
  tr.innerHTML = '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><input type="checkbox" checked onchange="markDimsDirty()"></td>'
    + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;">' + htmlEscape(info.name) + '<br><span style="color:#555;font-size:10px;font-family:Consolas;">' + htmlEscape(key) + '</span></td>'
    + '<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;"><input type="number" min="0" max="100" value="5" style="width:50px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:2px 6px;text-align:right;font-family:Consolas;font-size:12px;" onchange="markDimsDirty()">%</td>'
    + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#666;font-size:11px;">' + htmlEscape(info.desc) + '</td>'
    + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="toggleCurveEditor(this)" style="background:none;border:none;color:#42a5f5;cursor:pointer;font-size:14px;" title="调整评分曲线">📐</button></td>'
    + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="removeDim(this)" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:14px;">✖</button></td>';
  tbody.appendChild(tr);
  // 添加曲线编辑子行
  var curveSubRow = document.createElement('tr');
  curveSubRow.className = 'curve-edit-row';
  curveSubRow.style.cssText = 'display:none;background:rgba(255,255,255,0.02);';
  var pts2 = _getCurvePoints(key, null);
  curveSubRow.innerHTML = '<td colspan="6" style="padding:6px 12px;">'
    + '<div style="font-size:11px;color:#888;margin-bottom:4px;">评分曲线断点（输入值 → 得分）</div>'
    + '<div style="display:flex;gap:12px;align-items:flex-start;">'
    + '<table style="border-collapse:collapse;font-size:11px;" id="curve-tbl-' + key + '">'
    + pts2.map(function(p, j) {
      return '<tr><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[0] + '" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + key + '\')"> →</td><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[1] + '" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + key + '\')"></td><td style="padding:2px 4px;">' + (j > 0 ? '<button onclick="this.closest(\'tr\').remove();markDimsDirty();drawCurvePreview(\'' + key + '\')" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button>' : '') + '</td></tr>';
    }).join('')
    + '<tr><td colspan="3" style="padding:2px 4px;"><button onclick="addCurvePoint(this,\'' + key + '\')" style="background:none;border:1px dashed rgba(255,255,255,0.2);border-radius:3px;color:#888;padding:2px 10px;font-size:10px;cursor:pointer;">+ 添加断点</button></td></tr>'
    + '</table>'
    + '<div id="curve-preview-' + key + '" style="flex:1;min-width:0;"></div>'
    + '</div>'
    + '</td></tr>';
  tbody.appendChild(curveSubRow);
  renderAddDimBtn();
  updateDimsTotal();
  markDimsDirty();
}
function removeDim(btn) {
  var row = btn.closest('tr');
  var subRow = row.nextElementSibling;
  if (subRow && subRow.classList.contains('curve-edit-row')) subRow.remove();
  row.remove();
  markDimsDirty();
  renderAddDimBtn();
}

function markDimsDirty() {
  updateDimsTotal();
  document.getElementById('dimsSaveBtn').style.background = 'linear-gradient(135deg,#ef5350,#e53935)';
}
function toggleCurveEditor(btn) {
  var row = btn.closest('tr');
  var curveRow = row.nextElementSibling;
  if (curveRow && curveRow.classList.contains('curve-edit-row')) {
    var showing = curveRow.style.display === 'none';
    curveRow.style.display = showing ? 'table-row' : 'none';
    if (showing) {
      // 获取曲线 key
      var keyEl = row.querySelector('td:nth-child(2) span');
      if (keyEl) drawCurvePreview(keyEl.textContent);
    }
  }
}
// 曲线图表状态注册表（避免重复绑定事件）
_curveChartState = {};
_curveDragInited = false;
_curveDragData = null;
// 全局级拖拽事件（只绑一次）
document.addEventListener('mousemove', function(evt) {
  if (!_curveDragData) return;
  var st = _curveChartState[_curveDragData.key];
  if (!st) return;
  var rect = st.svg.getBoundingClientRect();
  var scX = st.W / rect.width, scY = st.H / rect.height;
  var mx = (evt.clientX - rect.left) * scX;
  var my = (evt.clientY - rect.top) * scY;
  var idx = _curveDragData.idx;
  function rx2(px) { return Math.round((st.xMin + (px - st.pad.l) / (st.W - st.pad.l - st.pad.r) * (st.xMax - st.xMin)) * 100) / 100; }
  function ry2(py) { return Math.max(0, Math.min(100, Math.round((st.yMax - (py - st.pad.t) / (st.H - st.pad.t - st.pad.b) * (st.yMax - st.yMin)) * 100) / 100)); }
  var newX = rx2(mx);
  var newY = ry2(my);
  if (idx > 0) { var pv = st.pts[idx-1][0]; if (newX < pv) newX = pv; }
  if (idx < st.pts.length - 1) { var nv = st.pts[idx+1][0]; if (newX > nv) newX = nv; }
  st.pts[idx] = [newX, newY];
  if (st.inputsList && st.inputsList[idx]) {
    st.inputsList[idx].xInp.value = newX;
    st.inputsList[idx].yInp.value = newY;
  }
  markDimsDirty();
  // 拖拽重绘（不重建DOM）
  var pathD = st.pts.map(function(p, i) { return (i === 0 ? 'M' : 'L') + st.sx(p[0]).toFixed(1) + ',' + st.sy(p[1]).toFixed(1); }).join(' ');
  st.path.setAttribute('d', pathD);
  st.circles.forEach(function(c, i) {
    if (i < st.pts.length) { c.setAttribute('cx', st.sx(st.pts[i][0]).toFixed(1)); c.setAttribute('cy', st.sy(st.pts[i][1]).toFixed(1)); }
  });
  st.labels.forEach(function(t, i) {
    if (i < st.pts.length) { t.setAttribute('x', st.sx(st.pts[i][0]).toFixed(1)); t.textContent = st.pts[i][0]; }
  });
});
document.addEventListener('mouseup', function() {
  if (_curveDragData) {
    var st = _curveChartState[_curveDragData.key];
    if (st && st.svg) st.svg.style.cursor = '';
    _curveDragData = null;
  }
});

function addCurvePoint(btn, key) {
  var tbl = document.getElementById('curve-tbl-' + key);
  if (!tbl) return;
  var rows = tbl.querySelectorAll('tr');
  var lastVal = 100, lastScore = 100;
  for (var i = rows.length - 1; i >= 0; i--) {
    var inputs = rows[i].querySelectorAll('input[type=number]');
    if (inputs.length >= 2) {
      lastVal = parseFloat(inputs[0].value) + 10;
      lastScore = parseFloat(inputs[1].value);
      break;
    }
  }
  var newRow = document.createElement('tr');
  newRow.innerHTML = '<td style="padding:2px 4px;"><input type="number" step="any" value="' + lastVal + '" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + key + '\')"> →</td>'
    + '<td style="padding:2px 4px;"><input type="number" step="any" value="' + lastScore + '" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markDimsDirty();drawCurvePreview(\'' + key + '\')"></td>'
    + '<td style="padding:2px 4px;"><button onclick="this.closest(\'tr\').remove();markDimsDirty();drawCurvePreview(\'' + key + '\')" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button></td>';
  tbl.querySelector('tr:last-child').before(newRow);
  markDimsDirty();
  drawCurvePreview(key);
}

function drawCurvePreview(key) {
  var container = document.getElementById('curve-preview-' + key);
  if (!container) return;
  var tbl = document.getElementById('curve-tbl-' + key);
  if (!tbl) return;
  // 读取断点数据
  var pts = [];
  var inputsList = [];
  var rows = tbl.querySelectorAll('tr');
  for (var i = 0; i < rows.length; i++) {
    var inputs = rows[i].querySelectorAll('input[type=number]');
    if (inputs.length >= 2) {
      var x = parseFloat(inputs[0].value);
      var y = parseFloat(inputs[1].value);
      if (!isNaN(x) && !isNaN(y)) { pts.push([x, y]); inputsList.push({xInp:inputs[0], yInp:inputs[1]}); }
    }
  }
  if (pts.length < 2) { container.innerHTML = ''; return; }
  var W = 560, H = 200, pad = {t:14, r:12, b:26, l:42};
  var xMin = pts[0][0], xMax = pts[pts.length - 1][0];
  var yMin = 0, yMax = 100;
  if (xMax === xMin) xMax = xMin + 1;
  function sv(v, lo, hi, a, b) { return a + (v - lo) / (hi - lo) * (b - a); }
  function sx(v) { return sv(v, xMin, xMax, pad.l, W - pad.r); }
  function sy(v) { return sv(v, yMax, yMin, pad.t, H - pad.b); }
  function rx(px) { return Math.round((xMin + (px - pad.l) / (W - pad.l - pad.r) * (xMax - xMin)) * 100) / 100; }
  function ry(py) { return Math.max(0, Math.min(100, Math.round((yMax - (py - pad.t) / (H - pad.t - pad.b) * (yMax - yMin)) * 100) / 100)); }
  var svgId = 'curve-svg-' + key;

  // 创建/重建完整 SVG
  var pathD = pts.map(function(p, i) { return (i === 0 ? 'M' : 'L') + sx(p[0]).toFixed(1) + ',' + sy(p[1]).toFixed(1); }).join(' ');
  var gridHtml = '';
  for (var g = 0; g <= 100; g += 20) {
    var gy = sy(g);
    gridHtml += '<line x1="' + pad.l + '" y1="' + gy + '" x2="' + (W - pad.r) + '" y2="' + gy + '" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>';
  }
  var dotsHtml = pts.map(function(p, i) {
    return '<circle data-idx="' + i + '" cx="' + sx(p[0]).toFixed(1) + '" cy="' + sy(p[1]).toFixed(1) + '" r="7" fill="#42a5f5" stroke="#fff" stroke-width="2" style="cursor:grab;"/>';
  }).join('');
  var labelsHtml = pts.map(function(p) {
    return '<text class="clbl" x="' + sx(p[0]).toFixed(1) + '" y="' + (H - 3) + '" text-anchor="middle" fill="#aaa" font-size="9" font-family="Consolas">' + p[0] + '</text>';
  }).join('');
  container.innerHTML = '<svg id="' + svgId + '" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" style="background:rgba(0,0,0,0.3);border-radius:6px;width:100%;">'
    + '<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="none"/>'
    + gridHtml
    + '<path class="cpath" d="' + pathD + '" fill="none" stroke="#42a5f5" stroke-width="2.5" stroke-linejoin="round"/>'
    + dotsHtml
    + '<text x="' + (W - 4) + '" y="14" text-anchor="end" fill="#888" font-size="9" font-family="Consolas">100</text>'
    + '<text x="' + (W - 4) + '" y="' + (H - 6) + '" text-anchor="end" fill="#888" font-size="9" font-family="Consolas">0</text>'
    + labelsHtml
    + '</svg>';

  // 缓存 DOM 引用用于拖拽更新
  var svgEl = document.getElementById(svgId);
  var pathEl = svgEl.querySelector('.cpath');
  var circleEls = svgEl.querySelectorAll('circle');
  var labelEls = svgEl.querySelectorAll('.clbl');
  _curveChartState[key] = {
    svg: svgEl, path: pathEl, circles: circleEls, labels: labelEls,
    pts: pts, inputsList: inputsList,
    W:W, H:H, pad:pad, xMin:xMin, xMax:xMax, yMin:yMin, yMax:yMax,
    sx: sx, sy: sy,
  };

  // mousedown：每个 SVG 独立绑定
  if (!svgEl._dragBound) {
    svgEl._dragBound = true;
    svgEl.addEventListener('mousedown', function(evt) {
      var t = evt.target;
      if (t.tagName === 'circle' && t.hasAttribute('data-idx')) {
        _curveDragData = {idx: parseInt(t.getAttribute('data-idx')), key: key};
        this.style.cursor = 'grabbing';
        evt.preventDefault();
      }
    });
  }
}


/** 刷新推荐表和自选表，通过 progFill/progPct/progText 显示td刷新进度 */
async function _refreshTablesWithProgress(progFill, progPct, progText, onDone) {
  try {
    var poll = setInterval(function() {
      fetch(API + '/api/heartbeat?_t=' + Date.now()).then(function(r){ return r.json(); }).then(function(hb) {
        if (!hb.ok) return;
        var recHb = hb.heartbeats && hb.heartbeats['recommend-td-refresh'];
        if (recHb) {
          var p = recHb.total > 0 ? Math.round(recHb.progress / recHb.total * 100) : 0;
          if (progFill) progFill.style.width = Math.min(p, 99) + '%';
          if (progPct) progPct.textContent = p + '%';
          if (progText) progText.textContent = '📋 刷新td ' + (recHb.detail || '') + ' | ' + p + '%';
        }
      }).catch(function(){});
    }, 800);
    var rtR = await fetch(API + '/api/recommend-table' + '?_t=' + Date.now());
    if (rtR.ok) { var rtH = await rtR.text(); if (rtH.indexOf('<tbody>') > 0) { var rtE = document.getElementById('recommendFullTable'); if (rtE) { rtE.innerHTML = rtH; document.getElementById('recommendFullTableCard').style.display = ''; } } else if (rtH) { var rtE2 = document.getElementById('recommendFullTable'); if (rtE2) { rtE2.innerHTML = rtH; document.getElementById('recommendFullTableCard').style.display = ''; } } }
    var ftR = await fetch(API + '/api/fund-table' + '?_t=' + Date.now());
    if (ftR.ok) { var ftH = await ftR.text(); if (ftH.indexOf('<tbody>') > 0) { var ftE = document.getElementById('fundFullTable'); if (ftE) { ftE.innerHTML = ftH; document.getElementById('fundFullTableCard').style.display = ''; setupDragSort(); } } else if (ftH) { var ftE2 = document.getElementById('fundFullTable'); if (ftE2) { ftE2.innerHTML = ftH; document.getElementById('fundFullTableCard').style.display = ''; } } }
    clearInterval(poll);
  } catch(e) {}
  if (onDone) onDone();
}


async function saveDims() {
  const btn = document.getElementById('dimsSaveBtn');
  const progArea = document.getElementById('saveProgressArea');
  const progFill = document.getElementById('saveProgressFill');
  const progPct = document.getElementById('saveProgressPct');
  const progText = document.getElementById('saveProgressText');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 保存中'; }
  if (progArea) progArea.style.display = '';
  if (progFill) progFill.style.width = '30%';
  if (progPct) progPct.textContent = '30%';
  if (progText) progText.textContent = '正在保存...';
  const rows = document.querySelectorAll('#dimsTable tbody tr');
  const dims = [];
  rows.forEach(function(row) {
    var cb = row.querySelector('input[type=checkbox]');
    if (!cb) return;
    var num = row.querySelector('input[type=number]');
    var key = row.querySelector('td:nth-child(2) span').textContent;
    var name = row.querySelector('td:nth-child(2)').textContent.replace(key,'').trim();
    if (name.endsWith('\n')) name = name.slice(0,-1).trim();
    const desc = row.querySelectorAll('td')[3].textContent;
    var curvePoints = null;
    var curveRow = row.nextElementSibling;
    if (curveRow && curveRow.classList.contains('curve-edit-row')) {
      var inpRows = curveRow.querySelectorAll('table tr');
      var pts = [];
      for (var j = 0; j < inpRows.length; j++) {
        var inputs = inpRows[j].querySelectorAll('input[type=number]');
        if (inputs.length >= 2) {
          var x = parseFloat(inputs[0].value);
          var y = parseFloat(inputs[1].value);
          if (!isNaN(x) && !isNaN(y)) pts.push([x, y]);
        }
      }
      if (pts.length >= 2) curvePoints = {points: pts};
    }
    var cat = DIM_INFO[key]?.cat || '';
    dims.push({name: name, key: key, weight: parseInt(num.value) / 100, enabled: cb.checked, desc: desc, curve: curvePoints, category: cat});
  });
  try {
    if (progFill) progFill.style.width = '60%';
    if (progPct) progPct.textContent = '60%';
    if (progText) progText.textContent = '正在保存...';
    const controller = new AbortController();
    const timeout = setTimeout(function() { controller.abort(); }, 30000);
    const r = await fetch(API + '/api/dims', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dims: dims}),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    const d = await r.json();
    if (progFill) progFill.style.width = '100%';
    if (progPct) progPct.textContent = '100%';
    if (progText) progText.textContent = '保存完成';
    if (d.ok) {
      showMsg('✔ 权重已保存', 'ok');
      if (progText) progText.textContent = '✔ 保存完成，刷新表格...';
      if (btn) { btn.textContent = '⏳ 刷新表格'; }
      // 权重变更后刷新评分表格（缓存已被服务端清除）
      _refreshTablesWithProgress(progFill, progPct, progText, function() {
        if (progFill) progFill.style.width = '100%';
        if (progPct) progPct.textContent = '100%';
        if (progText) progText.textContent = '✔ 刷新完成';
        if (btn) { btn.disabled = false; btn.textContent = '💾 保存权重'; btn.style.background = 'linear-gradient(135deg,#66bb6a,#43a047)'; }
        try { loadDims(); } catch(e) {}
        setTimeout(function() {
          if (progArea) progArea.style.display = 'none';
          if (progFill) progFill.style.width = '0%';
          if (progPct) progPct.textContent = '0%';
        }, 2000);
      });
    } else {
      if (progFill) progFill.style.width = '0%';
      if (progPct) progPct.textContent = '0%';
      if (progText) progText.textContent = '';
      if (btn) { btn.disabled = false; btn.textContent = '💾 保存权重'; btn.style.background = 'linear-gradient(135deg,#66bb6a,#43a047)'; }
      showMsg('✖ ' + (d.error || '保存失败'), 'fail');
    }
  } catch(e) {
    if (progFill) progFill.style.width = '0%';
    if (progPct) progPct.textContent = '0%';
    if (progText) progText.textContent = '';
    if (btn) { btn.disabled = false; btn.textContent = '💾 保存权重'; btn.style.background = 'linear-gradient(135deg,#66bb6a,#43a047)'; }
    if (progArea) progArea.style.display = 'none';
    showMsg('✖ 保存失败' + (e.name === 'AbortError' ? '（超时）' : ''), 'fail');
  }
}

async function resetDims() {
  if (!confirm('确定重置为默认权重配置？')) return;
  function _defCurve(k) { var c = _DEFAULT_CURVES[k]; return c ? {points: c.map(function(p){return [p[0],p[1]]})} : null; }
  const defaults = [
    {name:'近3月收益',key:'m3',weight:0.12,enabled:true,desc:'近三个月涨跌幅，中期趋势',curve:_defCurve('m3'),category:'perf'},
    {name:'近1月收益',key:'m1',weight:0.15,enabled:true,desc:'近一个月涨跌幅，捕捉短期动量',curve:_defCurve('m1'),category:'perf'},
    {name:'近1年收益',key:'y1',weight:0.09,enabled:true,desc:'最近一年的表现，反映基金近期赚钱能力',curve:_defCurve('y1'),category:'perf'},
    {name:'近一周收益',key:'f5',weight:0.03,enabled:true,desc:'近五个交易日涨跌幅，捕捉短期动量',curve:_defCurve('f5'),category:'perf'},
    {name:'近6月收益',key:'sy6',weight:0.06,enabled:true,desc:'近六个月表现，补充近1年的中短期维度',curve:_defCurve('sy6'),category:'perf'},
    {name:'近2年收益',key:'sy2',weight:0.05,enabled:true,desc:'近两年精确收益，填补中期维度',curve:_defCurve('sy2'),category:'perf'},
    {name:'近3年收益',key:'sy3',weight:0.07,enabled:true,desc:'从净值数据取级750个交易日精确计算，看穿越牛熊能力',curve:_defCurve('sy3'),category:'perf'},
    {name:'年化收益率',key:'annual_return',weight:0.04,enabled:true,desc:'基金成立以来年化回报',curve:_defCurve('annual_return'),category:'perf'},
    {name:'最大回撤',key:'max_dd',weight:0.10,enabled:true,desc:'历史最大跌幅',curve:_defCurve('max_dd'),category:'risk'},
    {name:'波动率',key:'volatility',weight:0.03,enabled:true,desc:'年化波动率，衡量基金震荡幅度',curve:_defCurve('volatility'),category:'risk'},
    {name:'最大连跌天数',key:'max_loss_days',weight:0.02,enabled:true,desc:'历史最长连续下跌天数',curve:_defCurve('max_loss_days'),category:'risk'},
    {name:'夏普比率',key:'sharpe',weight:0.06,enabled:true,desc:'每承受 1 份波动能换来多少额外收益',curve:_defCurve('sharpe'),category:'quality'},
    {name:'索提诺比率',key:'sortino',weight:0.06,enabled:true,desc:'只考虑下跌波动，更贴近真实风险感受',curve:_defCurve('sortino'),category:'quality'},
    {name:'盈亏比',key:'profit_ratio',weight:0.07,enabled:true,desc:'平均盈利÷平均亏损，>1说明赚比亏多',curve:_defCurve('profit_ratio'),category:'quality'},
    {name:'上行胜率',key:'win_rate',weight:0.07,enabled:true,desc:'赚钱天数占总交易天数的比例',curve:_defCurve('win_rate'),category:'quality'},
    {name:'修复系数',key:'recovery',weight:0.04,enabled:true,desc:'总收益÷最大回撤，衡量跌下去能不能涨回来',curve:_defCurve('recovery'),category:'quality'},
    {name:'卡玛比率',key:'calmar',weight:0.03,enabled:true,desc:'年化收益/最大回撤，衡量收益/风险比',curve:_defCurve('calmar'),category:'quality'},
    {name:'费率',key:'rate',weight:0.03,enabled:true,desc:'申购费越低越好',curve:_defCurve('rate'),category:'other'},
    {name:'基金规模',key:'scale',weight:0.02,enabled:true,desc:'1~50亿最理想，太小不灵活、太大难操作',curve:_defCurve('scale'),category:'other'},
    {name:'机构持有比例',key:'institutional',weight:0.02,enabled:true,desc:'专业机构认可度，小幅参考',curve:_defCurve('institutional'),category:'other'},
    {name:'当日涨跌',key:'td',weight:0.03,enabled:true,desc:'当日实时涨跌幅，捕捉盘中动量',curve:_defCurve('td'),category:'perf'},
  ];
  const r = await fetch(API + '/api/dims', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dims: defaults}),
  });
  const d = await r.json();
  if (d.ok) {
    showMsg('✔ 已重置为默认配置', 'ok');
    loadDims();
  } else {
    showMsg('✖ ' + (d.error || '重置失败'), 'fail');
  }
}

async function calibrateCurves() {
  if (!confirm('基于当前推荐数据自动校准评分曲线，确定继续？')) return;
  var btn = document.getElementById('calibrateBtn');
  var progArea = document.getElementById('saveProgressArea');
  var progFill = document.getElementById('saveProgressFill');
  var progPct = document.getElementById('saveProgressPct');
  var progText = document.getElementById('saveProgressText');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 校准中'; }
  if (progArea) progArea.style.display = '';
  if (progFill) progFill.style.width = '30%';
  if (progPct) progPct.textContent = '30%';
  if (progText) progText.textContent = '正在校准...';
  try {
    const r = await fetch(API + '/api/dims/calibrate', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    const d = await r.json();
    if (d.ok) {
      showMsg('✔ ' + (d.message || '校准完成'), 'ok');
      if (progFill) progFill.style.width = '100%';
      if (progPct) progPct.textContent = '100%';
      if (progText) progText.textContent = '校准完成，刷新表格...';
      if (btn) btn.textContent = '⏳ 刷新表格';
      // 刷新评分维度显示
      try { loadDims(); } catch(e) {}
      // 刷新表格（缓存已被服务端清除）
      _refreshTablesWithProgress(progFill, progPct, progText, function() {
        if (progFill) progFill.style.width = '100%';
        if (progPct) progPct.textContent = '100%';
        if (progText) progText.textContent = '✔ 刷新完成';
        if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
        setTimeout(function() {
          if (progArea) progArea.style.display = 'none';
          if (progFill) progFill.style.width = '0%';
          if (progPct) progPct.textContent = '0%';
        }, 2000);
      });
    } else {
      if (progFill) progFill.style.width = '0%';
      if (progPct) progPct.textContent = '0%';
      if (progText) progText.textContent = '';
      if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
      showMsg('✖ ' + (d.error || '校准失败'), 'fail');
    }
  } catch(e) {
    if (progFill) progFill.style.width = '0%';
    if (progPct) progPct.textContent = '0%';
    if (progText) progText.textContent = '';
    if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
    showMsg('✖ 校准失败: ' + (e.message || e), 'fail');
  }
}

loadDims();
loadPresets();

// ── 预设管理 ──────────────────────────────────
async function loadPresets() {
  try {
    const r = await fetch(API + '/api/dims-presets');
    const d = await r.json();
    if (!d.ok) return;
    const sel = document.getElementById('presetSelect');
    if (!sel) return;
    sel.innerHTML = Object.keys(d.presets).map(function(name) {
      return '<option value="' + htmlEscape(name) + '">' + htmlEscape(name) + '</option>';
    }).join('');
    if (d.current && d.presets[d.current]) sel.value = d.current;
  } catch(e) {}
}

async function loadPreset() {
  const sel = document.getElementById('presetSelect');
  if (!sel) return;
  const name = sel.value;
  if (!name) return;
  if (!confirm('加载预设将替换当前所有维度配置，确定继续？')) return;
  try {
    const r = await fetch(API + '/api/dims-presets');
    const d = await r.json();
    if (!d.ok) return;
    const preset = d.presets[name];
    if (!preset || !preset.dims) return;
    loadDims(preset.dims);
    // 自动保存到后端
    const saveR = await fetch(API + '/api/dims', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dims: preset.dims}),
    });
    const saveD = await saveR.json();
    if (saveD.ok) {
      showMsg('✔ 已加载并保存预设「' + name + '」', 'ok');
      const btn = document.getElementById('dimsSaveBtn');
      if (btn) btn.style.background = 'linear-gradient(135deg,#66bb6a,#43a047)';
    } else {
      showMsg('✔ 已加载预设，但自动保存失败: ' + (saveD.error || ''), 'fail');
    }
  } catch(e) {}
}

async function savePreset() {
  const sel = document.getElementById('presetSelect');
  if (!sel) return;
  const name = sel.value;
  if (!name || name === '系统默认') {
    showMsg('系统默认预设不可覆盖', 'fail');
    return;
  }
  try {
    const r = await fetch(API + '/api/dims-presets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save', name: name}),
    });
    const d = await r.json();
    if (d.ok) {
      showMsg('✔ 预设已覆盖保存', 'ok');
      loadPresets();
    } else {
      showMsg('✖ ' + (d.error || '保存失败'), 'fail');
    }
  } catch(e) { showMsg('✖ 保存失败', 'fail'); }
}

async function saveAsPreset() {
  const name = prompt('请输入新预设名称：');
  if (!name || !name.trim()) return;
  try {
    const r = await fetch(API + '/api/dims-presets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save_as', name: name.trim()}),
    });
    const d = await r.json();
    if (d.ok) {
      showMsg('✔ 预设已保存', 'ok');
      loadPresets();
    } else {
      showMsg('✖ ' + (d.error || '保存失败'), 'fail');
    }
  } catch(e) { showMsg('✖ 保存失败', 'fail'); }
}

async function deletePreset() {
  const sel = document.getElementById('presetSelect');
  if (!sel) return;
  const name = sel.value;
  if (!name || name === '系统默认') {
    showMsg('系统默认预设不可删除', 'fail');
    return;
  }
  if (!confirm('确定删除预设「' + name + '」？')) return;
  try {
    const r = await fetch(API + '/api/dims-presets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'delete', name: name}),
    });
    const d = await r.json();
    if (d.ok) {
      showMsg('✔ 预设已删除', 'ok');
      loadPresets();
    } else {
      showMsg('✖ ' + (d.error || '删除失败'), 'fail');
    }
  } catch(e) { showMsg('✖ 删除失败', 'fail'); }
}

// 加载全市场筛选配置参数
loadRecommendConfig();

async function loadRecommendConfig() {
  try {
    const r = await fetch(API + '/api/recommend-config');
    const d = await r.json();
    if (d.ok && d.config) {
      var el1 = document.getElementById('recTopN');
      if (el1) el1.value = d.config.top_n || 200;
      var el4 = document.getElementById('recShowTop');
      if (el4) el4.value = d.config.show_top || 20;
      var el5 = document.getElementById('recSkipMissingPerf');
      if (el5) el5.checked = d.config.skip_missing_perf !== false;
      var el6 = document.getElementById('recSkipLimited');
      if (el6) el6.checked = d.config.skip_limited === true;
      var el7 = document.getElementById('recRankSort');
      if (el7 && d.config.rank_sort) el7.value = d.config.rank_sort;
      // 加载筛选条件
      var condDiv = document.getElementById('filterConditions');
      if (condDiv) {
        condDiv.innerHTML = '';
        var conditions = d.config.filter_conditions || [];
        if (conditions.length === 0) {
          conditions = [{field: 'y1', op: 'gte', value: 20}];
        }
        conditions.forEach(function(c) { addFilterCondition(c.field, c.op, c.value); });
      }
    }
  } catch(e) {}
}
loadMonitorConfig();
async function loadMonitorConfig() {
  try {
    const r = await fetch(API + '/api/monitor-config');
    const d = await r.json();
    if (!d.ok || !d.config) return;
    var map = {
      mc_alert_drop_once: 'alert_drop_once', mc_alert_jump_once: 'alert_jump_once',
      mc_alert_accum_drop: 'alert_accum_drop', mc_accum_jump: 'accum_jump',
      mc_stock_drop_red: 'stock_alert_drop_red', mc_stock_jump_red: 'stock_alert_jump_red',
      mc_stock_accum_drop_red: 'stock_alert_accum_drop_red', mc_stock_accum_jump_red: 'stock_alert_accum_jump_red',
    };
    for (var elId in map) {
      var el = document.getElementById(elId);
      if (el) el.value = d.config[map[elId]] !== undefined ? d.config[map[elId]] : el.value;
    }
    var pollEl = document.getElementById('mc_poll_interval');
    if (pollEl) pollEl.value = String(d.config.poll_interval_seconds || 600);
  } catch(e) {}
}
async function saveMonitorConfig() {
  var btn = document.querySelector('#monitorConfigArea button');
  if (btn) btn.disabled = true;
  var statusEl = document.getElementById('mcSaveStatus');
  if (statusEl) statusEl.textContent = '保存中...';
  try {
    var body = {
      alert_drop_once: parseFloat(document.getElementById('mc_alert_drop_once').value) || -3,
      alert_jump_once: parseFloat(document.getElementById('mc_alert_jump_once').value) || 5,
      alert_accum_drop: parseFloat(document.getElementById('mc_alert_accum_drop').value) || -7,
      accum_jump: parseFloat(document.getElementById('mc_accum_jump').value) || 10,
      stock_alert_drop_red: parseFloat(document.getElementById('mc_stock_drop_red').value) || -5,
      stock_alert_jump_red: parseFloat(document.getElementById('mc_stock_jump_red').value) || 7,
      stock_alert_accum_drop_red: parseFloat(document.getElementById('mc_stock_accum_drop_red').value) || -10,
      stock_alert_accum_jump_red: parseFloat(document.getElementById('mc_stock_accum_jump_red').value) || 12,
      poll_interval_seconds: parseInt(document.getElementById('mc_poll_interval').value) || 600,
    };
    const r = await fetch(API + '/api/monitor-config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) {
      if (statusEl) { statusEl.textContent = '✔ 已保存'; statusEl.style.color = '#66bb6a'; }
      setTimeout(function() { if (statusEl) statusEl.textContent = ''; }, 3000);
    } else {
      if (statusEl) { statusEl.textContent = '✖ ' + (d.error || '保存失败'); statusEl.style.color = '#ef5350'; }
    }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '✖ 保存失败'; statusEl.style.color = '#ef5350'; }
  }
  if (btn) btn.disabled = false;
}
function addFilterCondition(field, op, value) {
  var condDiv = document.getElementById('filterConditions');
  if (!condDiv) return;
  var fieldOpts = {y1:'近1年收益',sy6:'近6月收益',m3:'近3月收益',m1:'近1月收益',sy2:'近2年收益',sy3:'近3年收益'};
  var html = '<div style="display:flex;gap:4px;align-items:center;margin-top:4px;">';
  html += '<select class="condField" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:4px 8px;font-family:Consolas;">';
  for (var k in fieldOpts) {
    html += '<option value="' + k + '"' + (k === field ? ' selected' : '') + '>' + fieldOpts[k] + '</option>';
  }
  html += '</select>';
  html += '<select class="condOp" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:4px 8px;font-family:Consolas;">';
  html += '<option value="gte"' + (op === 'gte' ? ' selected' : '') + '>≥</option>';
  html += '<option value="lte"' + (op === 'lte' ? ' selected' : '') + '>≤</option>';
  html += '</select>';
  html += '<input type="number" class="condVal" value="' + (value || 0) + '" min="0" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:4px 8px;font-family:Consolas;">';
  html += '<span style="color:#888;font-size:12px;">%</span>';
  html += '<button onclick="this.parentElement.remove()" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:16px;padding:2px 6px;" title="删除条件">✕</button>';
  html += '</div>';
  condDiv.insertAdjacentHTML('beforeend', html);
}

function cancelRecommend() {
  var btn = document.getElementById('recFilterBtn');
  var cancelBtn = document.getElementById('recCancelBtn');
  var prog = document.getElementById('recFilterProgress');
  var statusEl = document.getElementById('recFilterStatus');
  _recCancelled = true;
  if (cancelBtn) cancelBtn.style.display = 'none';
  if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
  if (prog) prog.style.width = '0%';
  if (statusEl) statusEl.textContent = '✖ 已取消';
  fetch(API + '/api/recommend/stop', { method: 'POST' }).catch(function(){});
}

async function runRecommendFromFilter() {
  var btn = document.getElementById('recFilterBtn');
  var prog = document.getElementById('recFilterProgress');
  var statusEl = document.getElementById('recFilterStatus');
  if (!btn) return;
  try {
    var topN = parseInt(document.getElementById('recTopN').value) || 200;
    var showTop = parseInt(document.getElementById('recShowTop').value) || 20;
    var skipMissing = document.getElementById('recSkipMissingPerf') ? document.getElementById('recSkipMissingPerf').checked : true;
    var skipLimited = document.getElementById('recSkipLimited') ? document.getElementById('recSkipLimited').checked : false;
    var rankSortEl = document.getElementById('recRankSort');
    var rankSort = rankSortEl ? rankSortEl.value : '1n';
    // 收集筛选条件
    var condDiv = document.getElementById('filterConditions');
    var conditions = [];
    if (condDiv) {
      var rows = condDiv.querySelectorAll('div');
      rows.forEach(function(row) {
        var fieldEl = row.querySelector('.condField');
        var opEl = row.querySelector('.condOp');
        var valEl = row.querySelector('.condVal');
        if (fieldEl && opEl && valEl) {
          conditions.push({field: fieldEl.value, op: opEl.value, value: parseFloat(valEl.value) || 0});
        }
      });
    }
    var cfgResp = await fetch(API + '/api/recommend-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({top_n: topN, filter_conditions: conditions, show_top: showTop, skip_missing_perf: skipMissing, skip_limited: skipLimited, rank_sort: rankSort}),
    });
    var cfgData = await cfgResp.json();
    if (!cfgData.ok) {
      if (statusEl) statusEl.textContent = '⚠ 筛选参数保存失败';
    }
  } catch(e) {
    if (statusEl) statusEl.textContent = '⚠ 筛选参数保存失败';
  }
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 推荐中'; }
  var cancelBtn = document.getElementById('recCancelBtn');
  if (cancelBtn) cancelBtn.style.display = 'inline-block';
  _recCancelled = false;
  if (prog) prog.style.width = '5%';
  if (statusEl) statusEl.textContent = '启动中...';
  try {
    const r = await fetch(API + '/api/recommend', { method: 'POST' });
    if (!r.ok) {
      // HTTP 错误（非 200），直接显示状态码
      if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
      if (prog) prog.style.width = '0%';
      if (statusEl) statusEl.textContent = '✖ 服务错误: HTTP ' + r.status;
      return;
    }
    const d = await r.json();
    if (!d.ok) {
      if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
      if (prog) prog.style.width = '0%';
      if (statusEl) statusEl.textContent = '✖ ' + (d.error || '启动失败');
      return;
    }
    var startTime = Date.now();
    var MAX_WAIT = 1800000; // 30分钟超时保护
    var poll = setInterval(async function() {
      if (_recCancelled) { clearInterval(poll); return; }
      try {
        var hb = await fetch(API + '/api/heartbeat?_t=' + Date.now()).then(function(r){ return r.json(); });
        var recData = hb.heartbeats && hb.heartbeats.fund_recommend;
        var recAlive = hb.alive && hb.alive.fund_recommend;
        // 如果心跳显示已完成，即使 alive 也视为结束
        var recDone = recData && (recData.overall_pct >= 100 || recData.phase === '完成' || recData.phase === '刷新完成');
        // 检测错误
        var recFailed = recData && (recData.phase === '失败' || recData.error);
        if (recFailed) {
          clearInterval(poll);
          var errMsg = recData.error || '推荐过程出错';
          if (prog) prog.style.width = '0%';
          if (statusEl) { statusEl.textContent = '✖ ' + errMsg; statusEl.style.color = '#ef5350'; }
          if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
          if (cancelBtn) cancelBtn.style.display = 'none';
          showMsg('✖ ' + errMsg, 'fail');
          setTimeout(function(){ if(statusEl) statusEl.style.color = ''; }, 8000);
          return;
        }
        if (recAlive && recData && !recDone) {
          var pct = recData.total > 0 ? Math.round(recData.progress / recData.total * 100) : 0;
          // 优先使用全局进度 overall_pct，避免阶段切换时进度回退
          var displayPct = recData.overall_pct != null ? Math.round(recData.overall_pct) : pct;
          if (prog) prog.style.width = Math.min(displayPct, 99) + '%';
          // 构建详细状态字符串
          var phaseIcon = {'获取排行':'📥','初筛':'📊','限购':'🔒','评分':'🧮','保存':'💾','完成':'✅',
                           '刷新td':'📋','更新涨跌':'📋','涨跌':'📋','重新评分':'📋','检查自选基金':'📋'};
          var icon = phaseIcon[recData.phase] || '⏳';
          var statusParts = [icon];
          if (recData.phase) statusParts.push(recData.phase);
          if (recData.detail) statusParts.push(recData.detail);
          if (recData.elapsed != null) statusParts.push(Math.round(recData.elapsed) + 's');
          if (statusEl) statusEl.textContent = statusParts.join(' | ');
          if (btn) btn.textContent = '⏳ ' + displayPct + '%';
        } else if (!recAlive || recDone) {
          clearInterval(poll);
          // 如果进程结束但没到完成阶段且没有错误标记，可能是异常退出
          if (!recDone && recData && recData.overall_pct != null && recData.overall_pct < 100) {
            var errMsg = recData.error || '推荐进程意外退出（阶段：' + (recData.phase || '?') + '）';
            if (prog) prog.style.width = '0%';
            if (statusEl) { statusEl.textContent = '✖ ' + errMsg; statusEl.style.color = '#ef5350'; }
            if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
            if (cancelBtn) cancelBtn.style.display = 'none';
            showMsg('✖ ' + errMsg, 'fail');
            setTimeout(function(){ if(statusEl) statusEl.style.color = ''; }, 8000);
            return;
          }
          // 正常完成：渲染表格（td刷新已在推荐进程内部完成）
          if (cancelBtn) cancelBtn.style.display = 'none';
          if (prog) prog.style.width = '100%';
          if (statusEl) statusEl.textContent = '⏳ 渲染表格...';
          if (btn) btn.textContent = '⏳ 渲染表格';
          // 等一小会儿让 _supplement_self_selected 完成
          await new Promise(function(r){ setTimeout(r, 1500); });
          var rtPromise = fetch(API + '/api/recommend-table' + '?_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; });
          var ftPromise = fetch(API + '/api/fund-table?fresh=1&_t=' + Date.now()).then(function(r){ return r.ok ? r.text() : null; });
          var _tableOk = true;
          try { var _rt = await rtPromise; if (_rt && _rt.indexOf('<tbody>') > 0) { var _re = document.getElementById('recommendFullTable'); if (_re) { _re.innerHTML = _rt; document.getElementById('recommendFullTableCard').style.display = ''; } } else if (_rt) { var _re2 = document.getElementById('recommendFullTable'); if (_re2) { _re2.innerHTML = _rt; document.getElementById('recommendFullTableCard').style.display = ''; } } } catch(e) { _tableOk = false; }
          try { var _ft = await ftPromise; if (_ft && _ft.indexOf('<tbody>') > 0) { var _fe = document.getElementById('fundFullTable'); if (_fe) { _fe.innerHTML = _ft; document.getElementById('fundFullTableCard').style.display = ''; setupDragSort(); } } else if (_ft) { var _fe2 = document.getElementById('fundFullTable'); if (_fe2) { _fe2.innerHTML = _ft; document.getElementById('fundFullTableCard').style.display = ''; } } } catch(e) { _tableOk = false; }
          // 获取超时统计
          var _timeoutInfo = '';
          try { var _r = await fetch(API + '/api/recommend?_t=' + Date.now()).then(function(r){ return r.json(); }); if (_r && _r.timeout_count > 0) { _timeoutInfo = ' ⚠' + _r.timeout_count + '次超时'; } } catch(e) {}
          if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
          if (statusEl) statusEl.textContent = (_tableOk ? '✔ 刷新完成' : '✔ 刷新完成（部分失败）') + _timeoutInfo;
          if (prog) prog.style.width = '0%';
          setTimeout(function() { if (prog) prog.style.width = '0%'; if (statusEl && statusEl.textContent.indexOf('刷新完成') >= 0) statusEl.textContent = ''; }, 4000);
          return;
      } catch(e) {
        // 轮询异常：不立即放弃，继续下一次轮询
        if (Date.now() - startTime > 60000 && statusEl) {
          statusEl.textContent = '⚠ 连接异常，后台任务可能仍在运行...';
        }
      }
      if (Date.now() - startTime > MAX_WAIT) {
        clearInterval(poll);
        if (prog) prog.style.width = '0%';
        if (statusEl) statusEl.textContent = '⏰ 超时';
        if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
      }
    }, 2000);
  } catch(e) {
    if (prog) prog.style.width = '0%';
    if (statusEl) statusEl.textContent = '启动失败';
    if (btn) { btn.disabled = false; btn.textContent = '▶ 运行推荐'; }
    showMsg('✖ 启动失败: ' + (e.message || e), 'fail');
  }
}

function showScoreDetail(items) {
  // 按权重降序排列
  items = items.slice().sort(function(a,b){ return b[2] - a[2]; });
  var html = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;margin:8px 0;font-size:12px;color:#ccc;max-height:80vh;overflow-y:auto;">'
    + '<div style="font-size:13px;font-weight:600;color:#e0e0e0;margin-bottom:10px;">\u7ef4\u5ea6\u8bc4\u5206\u660e\u7ec6</div>'
    + '<div style="margin-bottom:10px;padding:8px;background:rgba(0,0,0,0.3);border-radius:6px;">';
  var maxContrib = 0;
  items.forEach(function(item){ var c = item[2] * item[1]; if (c > maxContrib) maxContrib = c; });
  var total = 0, weightSum = 0;
  items.forEach(function(item){
    var name = item[0], score = item[1], weight = item[2], value = item[3];
    var contrib = (value === null || value === undefined) ? 50 * weight : score * weight;
    total += contrib; weightSum += weight;
    var barPct = maxContrib > 0 ? (contrib / maxContrib * 100) : 0;
    var scoreColor = score >= 80 ? '#66bb6a' : score >= 40 ? '#ffa726' : '#ef5350';
    var barColor = score >= 80 ? 'rgba(102,187,106,0.4)' : score >= 40 ? 'rgba(255,167,38,0.4)' : 'rgba(239,83,80,0.4)';
    var valStr = value !== null && value !== undefined ? (typeof value === 'number' ? value.toFixed(2) : value) : '-';
    var barHtml = '<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
      + '<div style="flex:0 0 80px;text-align:right;color:#888;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help;" title="' + (item[4] || name) + '">' + name + '</div>'
      + '<div style="flex:1;height:14px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;position:relative;">'
      + '<div style="width:' + barPct.toFixed(0) + '%;height:100%;background:' + barColor + ';border-radius:3px;"></div>'
      + '</div>'
      + '<div style="flex:0 0 28px;text-align:right;font-family:Consolas;font-size:10px;font-weight:600;color:' + scoreColor + ';">' + score.toFixed(0) + '</div>'
      + '<div style="flex:0 0 28px;text-align:right;font-family:Consolas;font-size:10px;color:#888;">' + (weight * 100).toFixed(0) + '%</div>'
      + '<div style="flex:0 0 36px;text-align:right;font-family:Consolas;font-size:10px;color:#888;">' + contrib.toFixed(1) + '</div>'
      + '</div>';
    html += barHtml;
  });
  var normTotal = weightSum > 0 ? (total / weightSum).toFixed(1) : '0.0';
  html += '</div>'
    + '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
    + '<thead><tr style="background:#2a2a2a;"><th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #444;">\u7ef4\u5ea6</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">\u503c</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">\u5f97\u5206</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">\u6743\u91cd</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">\u8d21\u732e</th></tr></thead><tbody>';
  items.forEach(function(item){
    var name = item[0], score = item[1], weight = item[2], value = item[3];
    var isNull = (value === null || value === undefined);
    var contrib = isNull ? 50 * weight : score * weight;
    var valStr = isNull ? '-' : (typeof value === 'number' ? value.toFixed(2) : value);
    var scoreColor = isNull ? '#ffa726' : (score >= 80 ? '#66bb6a' : score >= 40 ? '#ffa726' : '#ef5350');
    html += '<tr><td style="padding:3px 6px;border-bottom:1px solid #333;color:' + (isNull ? '#888' : '#e0e0e0') + ';cursor:help;" title="' + (item[4] || name) + '">' + name + (isNull ? ' <span style="color:#555;">(中性)</span>' : '') + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (isNull ? '#555' : '#888') + ';">' + valStr + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + scoreColor + ';">' + (isNull ? '50.0' : score.toFixed(1)) + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (weight * 100).toFixed(0) + '%</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + contrib.toFixed(1) + '</td></tr>';
  });
  html += '<tr><td style="padding:4px 6px;border-top:1px solid #555;color:#888;" colspan="2">\u5408\u8ba1</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;font-weight:600;color:#66bb6a;">' + normTotal + '</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;color:#888;">' + (weightSum * 100).toFixed(0) + '%</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;color:#888;">' + total.toFixed(1) + '</td></tr>'
    + '</tbody></table>'
    + '<button id="__closeScoreBtn" style="margin-top:10px;background:#333;border:1px solid #555;border-radius:4px;color:#ccc;padding:4px 12px;cursor:pointer;">\u5173\u95ed</button></div>';
  var el = document.getElementById("scoreDetailModal");
  if (!el) {
    el = document.createElement("div");
    el.id = "scoreDetailModal";
    document.body.appendChild(el);
  }
  el.innerHTML = html;
  var btn = document.getElementById("__closeScoreBtn");
  if (btn) btn.onclick = function(){ el.style.display = "none"; };
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9999;max-width:520px;width:90%;max-height:90vh;overflow-y:auto;";
}

function showHoldings(code, name) {
  var backdrop = document.getElementById("holdingsBackdrop");
  if (!backdrop) {
    backdrop = document.createElement("div");
    backdrop.id = "holdingsBackdrop";
    document.body.appendChild(backdrop);
  }
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:9998;background:rgba(0,0,0,0.5);";
  backdrop.onclick = function(){ backdrop.style.display='none'; var m=document.getElementById('holdingsModal'); if(m)m.style.display='none'; };

  var el = document.getElementById("holdingsModal");
  if (!el) {
    el = document.createElement("div");
    el.id = "holdingsModal";
    document.body.appendChild(el);
  }
  el.innerHTML = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;font-size:12px;color:#ccc;max-width:1200px;overflow-x:auto;">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
    + '<div><span style="font-size:13px;font-weight:600;color:#e0e0e0;">\ud83d\udcc8 ' + name + '</span> <span style="color:#666;font-size:11px;">' + code + '</span> <span id="hldReportPeriod" style="color:#555;font-size:10px;"></span></div>'
    + '<div><span onclick="showHldDimsEditor()" style="cursor:pointer;color:#888;font-size:11px;margin-right:12px;border:1px solid #444;padding:2px 8px;border-radius:4px;">⚙ 评分设置</span>'
    + '<span onclick="this.closest(\'#holdingsModal\').style.display=\'none\';document.getElementById(\'holdingsBackdrop\').style.display=\'none\'" style="cursor:pointer;color:#555;font-size:16px;">&times;</span></div></div>'
    + '<div id="holdingsContent" style="text-align:center;padding:20px;color:#888;">\u23f3 \u52a0\u8f7d\u4e2d...</div>'
    + '</div>';
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9999;width:98%;max-width:1200px;overflow-x:auto;";
  el.onclick = function(e){ e.stopPropagation(); };

  window._hldFundCode = code;
  window._hldFundName = name;
  fetch(API + '/api/holdings?code=' + code)
    .then(function(r){ return r.json(); })
    .then(function(d){
      var content = document.getElementById('holdingsContent');
      // 显示报告期
      var rpEl = document.getElementById('hldReportPeriod');
      if (rpEl && d.report && d.report.quarter) {
        rpEl.textContent = '📋 ' + d.report.quarter + (d.report.date ? ' · ' + d.report.date : '');
      }
      if (!d.ok || !d.holdings || d.holdings.length === 0) {
        content.innerHTML = '<span style="color:#666;">暂无持仓数据</span>';
        return;
      }
      // 存储当前持仓数据供评分明细使用
      window._hldHoldingsData = d.holdings;
      var tbl = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;white-space:nowrap;">'
        + '<thead><tr style="background:#2a2a2a;"><th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #444;position:sticky;left:0;z-index:3;background:#2a2a2a;">股票</th>'
        + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #444;">代码</th>'
        + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #444;" title="综合评分(0-100)，基于ROE/毛利率/负债率等12个维度加权计算。可点击设置按钮调整维度权重。">评分</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">占比</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">实时涨跌</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="当前股价(元)。实时交易价格。">当前价</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="今日开盘价(元)。">今开</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="振幅(%)=(最高-最低)÷昨收。反映日内价格波动剧烈程度。">振幅</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="市盈率(PE)=股价÷每股收益。衡量股价相对于盈利是否合理，PE越高说明市场对该公司未来越乐观，但也可能意味着估值泡沫。横向同行业对比更有意义。">PE</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="市净率PB=股价÷每股净资产。衡量股价相对于净资产是否合理，适用于金融/周期股，PB<1可能破净。与PE互补使用。">市净率PB</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近1周涨跌幅(%)。股票过去5个交易日的价格表现。">近1周涨跌</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="PEG=市盈率÷净利润增速。核心看股价匹配业绩增长速度。PEG≈1估值合理，PEG＜1低估，PEG＞2泡沫风险大。仅适用于净利润连续稳定增长的企业，亏损/周期股不适用。">PEG</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="股息率=股息发放率×每股收益÷股价。衡量每年现金分红回报，适合长期吃股息的保守投资者。数据来自最新财报，部分股票可能无数据。">股息率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="市销率(PS)=总市值÷营业收入。不看利润只看营收规模，适用于亏损/微利/周期底部企业。PS越低说明营收相对市值越便宜，需同行业对比。">PS(市销率)</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="净资产收益率(ROE)=净利润÷净资产。巴菲特最看重的指标，衡量公司为股东创造回报的能力。>15%优秀，>20%卓越。">ROE</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="成本费用利润率(%)=利润总额÷成本费用总额。衡量每付出1元成本能创造多少利润，越高说明成本控制越好。>100%优秀。">成本费用利润率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="销售净利率=净利润÷营收。衡量每元营收能产生多少净利润，>15%优秀。">销售净利率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="主营业务利润率=主营业务利润÷营收。反映核心业务的盈利水平。">主营利润率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="主营业务收入增长率(%)。与净利润增长率对照看，若营收增长但利润不增，说明增收不增利、利润质量堪忧。">营收增长率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="净利润增长率(%)。净利润同比增速，衡量盈利增长动力。>10%良好，与营收增长率对照判断成长质量。">净利润增长率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="资产负债率=总负债÷总资产。衡量财务杠杆风险，<50%稳健，50-70%正常，>70%偏高需警惕。">资产负债率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="速动比率=(流动资产-存货)÷流动负债。衡量短期偿债能力，>1安全，<1需警惕流动性风险。">速动比率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="流动比率=流动资产÷流动负债。比速动比率更宽松的偿债指标，>2较安全。">流动比率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="每股经营性现金流(元)。衡量公司真实现金造血能力，比每股收益(EPS)更真实。每股现金流>EPS说明利润质量高。">每股经营现金流</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="经营现金流÷净利润。衡量利润的现金支撑质量。>1说明利润有真实现金流保障，>0.5可接受，<0.5需警惕。">现金流/净利润</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="每股净资产(元)。每股含多少净资产，是PB(市净率)的估值基础。净资产越高说明家底越厚。">每股净资产</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="每股未分配利润(元)。公司累积的可分配利润，越高说明分红潜力越大。">每股未分配利润</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="每股资本公积金(元)。公司资本储备，越高说明高送转潜力越大。">每股资本公积</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="换手率(%)=成交量÷流通股本。反映股票交易活跃度，换手率太低说明流动性差。">换手率</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="量比=当前成交量÷过去5日均量。量比>1放量，<1缩量，>2明显放量、可能有行情启动。">量比</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="总市值(亿元)。公司整体市场估值，值越大说明公司规模越大。">总市值</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="52周最高价(元)。过去一年内的最高成交价。">52周最高</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="52周最低价(元)。过去一年内的最低成交价。">52周最低</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="52周相对位置(%)=当前价在52周高低区间内的位置。100%≈最高价，0%≈最低价。>70%偏高位，<30%偏低位。">52周位置</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近1月收益率(%)。过去约22个交易日的价格表现。">近1月收益</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近3月收益率(%)。过去约66个交易日的价格表现。">近3月收益</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近1年收益率(%)。过去约252个交易日的价格表现。">近1年收益</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近1年最大回撤(%)。过去一年内从最高点到最低点的最大跌幅，衡量下行风险。">近1年回撤</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="近2年最大回撤(%)。过去两年内从最高点到最低点的最大跌幅。">近2年回撤</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="流通市值(亿元)。可在二级市场自由交易的市值部分，区分大小盘风格。">流通市值</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="总资产(亿元)。公司全部资产规模，体量越大抗风险能力越强。">总资产</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="总资产利润率ROA(%)=净利润÷总资产。衡量全部资产的盈利效率，与ROE互补。">ROA</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="净资产增长率(%)。衡量公司净资产的扩张速度，越高说明成长性越好。">净资产增长率</th>'
        + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #444;" title="股票上市日期，越长说明公司经营历史越久。">上市日期</th>'
        + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #444;" title="公司所属行业/机构类型。">行业</th>'
        + '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #444;max-width:140px;" title="公司主营业务简介。">主营业务</th>'
        + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;" title="注册资本(元)。公司注册时的资本总额。">注册资本</th>'
        + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #444;" title="公司成立日期。">成立日期</th></tr></thead><tbody>';
      d.holdings.forEach(function(h){
        var chgStr = '';
        var chgColor = '#888';
        if (h.chg !== null && h.chg !== undefined) {
          chgStr = (h.chg > 0 ? '+' : '') + h.chg.toFixed(2) + '%';
          chgColor = h.chg > 0 ? '#ef5350' : '#66bb6a';
        } else {
          chgStr = '-';
        }
        var stockFullCode = (h.m || 'sz') + h.c;
        var peStr = '-';
        if (h.pe !== null && h.pe !== undefined && h.pe > 0) {
          peStr = h.pe.toFixed(2);
        } else if (h.pe !== null && h.pe !== undefined && h.pe < 0) {
          peStr = '<span style="color:#ef5350;">亏损</span>';
        }
        var growthStr = '-';
        var growthColor = '#888';
        if (h.ret_1w !== null && h.ret_1w !== undefined) {
          growthStr = (h.ret_1w > 0 ? '+' : '') + h.ret_1w.toFixed(2) + '%';
          growthColor = h.ret_1w > 0 ? '#ef5350' : '#66bb6a';
        }
        var pegStr = '-';
        var pegColor = '#888';
        if (h.peg !== null && h.peg !== undefined && h.peg > 0) {
          pegStr = h.peg.toFixed(2);
          pegColor = h.peg < 1 ? '#66bb6a' : (h.peg <= 2 ? '#ffa726' : '#ef5350');
        }
        // 数据季度标记：圆点颜色直接按季度分
        var dotColor = '#888';
        var dotTitle = '无财报数据';
        var fglQuarter = '';
        if (h.fgl_date) {
          var fglM = parseInt(h.fgl_date.substring(5, 7), 10);
          var fglY = parseInt(h.fgl_date.substring(0, 4), 10);
          if (fglM <= 3) { fglQuarter = 'Q1'; dotColor = '#4dd0e1'; }
          else if (fglM <= 6) { fglQuarter = 'Q2'; dotColor = '#66bb6a'; }
          else if (fglM <= 9) { fglQuarter = 'Q3'; dotColor = '#ffa726'; }
          else { fglQuarter = 'Q4'; dotColor = '#ab47bc'; }
          dotTitle = fglY + fglQuarter + ' 财报';
        }
        tbl += '<tr><td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;max-width:120px;overflow:hidden;text-overflow:ellipsis;position:sticky;left:0;z-index:1;background:#1a1a1a;">'
        + '<span style="cursor:help;color:' + dotColor + ';font-size:12px;" title="' + dotTitle + '">●</span>'
        + ' <span onclick="event.stopPropagation();showStockInfo(\'' + htmlEscape(h.n || '') + '\',\'' + stockFullCode + '\')" style="cursor:pointer;border-bottom:1px dashed rgba(255,255,255,0.25);" title="' + dotTitle + '\nK线:最新交易日\n行情:今日实时\n上市：' + (h.listing_date || '-') + '\n行业：' + (h.industry || '-') + '\n点击查看详情">' + htmlEscape(h.n || '') + '</span></td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + htmlEscape(h.c || '') + '</td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;font-family:Consolas;font-weight:600;cursor:pointer;color:' + (h.hld_score != null ? (h.hld_score >= 80 ? '#66bb6a' : h.hld_score >= 60 ? '#42a5f5' : h.hld_score >= 40 ? '#ffa726' : '#ef5350') : '#888') + ';" title="财报:' + (h.fgl_date || '无') + ' 点击查看评分明细" onclick="event.stopPropagation();showHldScoreDetail(\'' + htmlEscape(h.n || '') + '\',\'' + htmlEscape(h.c || '') + '\',this)">' + (h.hld_score != null ? h.hld_score.toFixed(1) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#66bb6a;">' + (h.p || 0).toFixed(2) + '%</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + chgColor + ';">' + chgStr + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.price != null ? h.price.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.open != null ? h.open.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.amplitude != null ? (h.amplitude >= 5 ? '#ef5350' : h.amplitude >= 2 ? '#ffa726' : '#888') : '#888') + ';">' + (h.amplitude != null ? h.amplitude.toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + peStr + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.pb != null ? (h.pb < 10 ? '#66bb6a' : h.pb < 30 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.pb != null ? h.pb.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + growthColor + ';">' + growthStr + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + pegColor + ';">' + pegStr + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.dividend_yield ? (h.dividend_yield * 100).toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.ps ? h.ps.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.roe != null ? (h.roe >= 15 ? '#66bb6a' : h.roe >= 5 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.roe != null ? h.roe.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.cost_profit_margin != null ? (h.cost_profit_margin >= 100 ? '#66bb6a' : h.cost_profit_margin >= 50 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.cost_profit_margin != null ? h.cost_profit_margin.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.net_profit_margin != null ? (h.net_profit_margin >= 15 ? '#66bb6a' : h.net_profit_margin >= 5 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.net_profit_margin != null ? h.net_profit_margin.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.main_biz_margin != null ? (h.main_biz_margin >= 30 ? '#66bb6a' : h.main_biz_margin >= 15 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.main_biz_margin != null ? h.main_biz_margin.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.rev_growth != null ? (h.rev_growth >= 10 ? '#ef5350' : h.rev_growth >= 0 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.rev_growth != null ? (h.rev_growth > 0 ? '+' : '') + h.rev_growth.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.net_profit_growth != null ? (h.net_profit_growth >= 10 ? '#ef5350' : h.net_profit_growth >= 0 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.net_profit_growth != null ? (h.net_profit_growth > 0 ? '+' : '') + h.net_profit_growth.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.debt_ratio != null ? (h.debt_ratio < 50 ? '#66bb6a' : h.debt_ratio <= 70 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.debt_ratio != null ? h.debt_ratio.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.quick_ratio != null ? (h.quick_ratio >= 1 ? '#66bb6a' : h.quick_ratio >= 0.5 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.quick_ratio != null ? h.quick_ratio.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.current_ratio != null ? (h.current_ratio >= 2 ? '#66bb6a' : h.current_ratio >= 1 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.current_ratio != null ? h.current_ratio.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.cf_ps != null ? '#888' : '#888') + ';">' + (h.cf_ps != null ? h.cf_ps.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.cashflow_to_profit != null ? (h.cashflow_to_profit >= 1 ? '#66bb6a' : h.cashflow_to_profit >= 0.5 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.cashflow_to_profit != null ? h.cashflow_to_profit.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.nav_ps != null ? h.nav_ps.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.retained_ps != null ? h.retained_ps.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.capital_reserve_ps != null ? h.capital_reserve_ps.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.turnover != null ? (h.turnover >= 5 ? '#ef5350' : h.turnover >= 1 ? '#ffa726' : '#888') : '#888') + ';">' + (h.turnover != null ? h.turnover.toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.vol_ratio != null ? (h.vol_ratio >= 2 ? '#ef5350' : h.vol_ratio >= 0.8 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.vol_ratio != null ? h.vol_ratio.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.mkt_cap != null ? h.mkt_cap.toFixed(1) + '亿' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.wk_high != null ? h.wk_high.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.wk_low != null ? h.wk_low.toFixed(2) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.wk_position != null ? (h.wk_position >= 70 ? '#ef5350' : h.wk_position >= 30 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.wk_position != null ? h.wk_position.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.ret_1m != null ? (h.ret_1m >= 0 ? '#ef5350' : '#66bb6a') : '#888') + ';">' + (h.ret_1m != null ? (h.ret_1m > 0 ? '+' : '') + h.ret_1m.toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.ret_3m != null ? (h.ret_3m >= 0 ? '#ef5350' : '#66bb6a') : '#888') + ';">' + (h.ret_3m != null ? (h.ret_3m > 0 ? '+' : '') + h.ret_3m.toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.ret_1y != null ? (h.ret_1y >= 0 ? '#ef5350' : '#66bb6a') : '#888') + ';">' + (h.ret_1y != null ? (h.ret_1y > 0 ? '+' : '') + h.ret_1y.toFixed(2) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.mdd_1y != null ? (h.mdd_1y >= 30 ? '#ef5350' : h.mdd_1y >= 15 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.mdd_1y != null ? '-' + h.mdd_1y.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.mdd_2y != null ? (h.mdd_2y >= 30 ? '#ef5350' : h.mdd_2y >= 15 ? '#ffa726' : '#66bb6a') : '#888') + ';">' + (h.mdd_2y != null ? '-' + h.mdd_2y.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.float_mkt_cap != null ? h.float_mkt_cap.toFixed(1) + '亿' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.total_assets != null ? h.total_assets.toFixed(1) + '亿' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.roa != null ? (h.roa >= 10 ? '#66bb6a' : h.roa >= 5 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.roa != null ? h.roa.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + (h.net_asset_growth != null ? (h.net_asset_growth >= 10 ? '#66bb6a' : h.net_asset_growth >= 0 ? '#ffa726' : '#ef5350') : '#888') + ';">' + (h.net_asset_growth != null ? h.net_asset_growth.toFixed(1) + '%' : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.listing_date || '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;color:#888;font-size:11px;">' + (h.industry || '-') + '</td>'
          + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#888;font-size:11px;max-width:140px;overflow:hidden;text-overflow:ellipsis;" title="' + htmlEscape(h.main_biz || '') + '">' + htmlEscape((h.main_biz || '') ? (h.main_biz.length > 30 ? h.main_biz.substring(0, 28) + '…' : h.main_biz) : '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.reg_capital || '-') + '</td>'
          + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + (h.establish_date || '-') + '</td></tr>';
      });
      tbl += '</tbody></table></div>';
      content.innerHTML = tbl;
      // 列拖动排序（带localStorage持久化）
      (function(){
        var tblEl = content.querySelector('table');
        if (!tblEl) return;
        var thead = tblEl.querySelector('thead');
        var tbody = tblEl.querySelector('tbody');
        // 获取当前列名列表
        function _getColNames() {
          return Array.from(thead.querySelectorAll('tr:first-child th')).map(function(t){ return t.textContent; });
        }
        // 按保存的顺序重排列
        function _applySavedOrder(saved) {
          if (!saved || !saved.length) return;
          var cur = _getColNames();
          var order = saved.filter(function(n){ return cur.indexOf(n) >= 0; });
          if (order.length < 2) return;
          var headerRow = thead.querySelector('tr:first-child');
          // 收集所有 th 按保存顺序排序后重新追加
          var allThs = Array.from(headerRow.querySelectorAll('th'));
          allThs.sort(function(a, b){
            return order.indexOf(a.textContent) - order.indexOf(b.textContent);
          });
          allThs.forEach(function(th){ headerRow.appendChild(th); });
          // 数据行按新表头顺序重排
          Array.from(tbody.querySelectorAll('tr')).forEach(function(tr){
            var tds = tr.querySelectorAll('td');
            if (tds.length < order.length) return;
            var sortedTds = [];
            Array.from(headerRow.querySelectorAll('th')).forEach(function(th){
              var origIdx = cur.indexOf(th.textContent);
              if (origIdx >= 0 && origIdx < tds.length) sortedTds.push(tds[origIdx]);
            });
            sortedTds.forEach(function(td){ tr.appendChild(td); });
          });
        }
        // 从服务端加载顺序
        if (window._hldColOrder && window._hldColOrder.length) {
          _applySavedOrder(window._hldColOrder);
        }
        fetch(API + '/api/holdings-col-order').then(function(r){ return r.json(); }).then(function(d){
          if (d.ok && d.order && d.order.length) {
            window._hldColOrder = d.order;
            _applySavedOrder(d.order);
          }
        }).catch(function(){});
        var ths = thead.querySelectorAll('tr:first-child th');
        // 获取th的当前索引
        function _thIdx(t) {
          var all = thead.querySelectorAll('tr:first-child th');
          for (var i = 0; i < all.length; i++) { if (all[i] === t) return i; }
          return -1;
        }
        // 保存当前顺序到服务端
        function _saveOrder() {
          var names = _getColNames();
          window._hldColOrder = names;
          fetch(API + '/api/holdings-col-order', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order:names})});
        }
        var dragSrcEl = null;
        Array.from(ths).forEach(function(th){
          th.draggable = true;
          th.style.cursor = 'grab';
          var origTitle = th.title || '';
          th.title = (origTitle ? origTitle + '\n' : '') + '拖动调整顺序，双击移至最前';
          th.addEventListener('dragstart', function(e){
            dragSrcEl = th;
            e.dataTransfer.effectAllowed = 'move';
            th.style.opacity = '0.5';
          });
          th.addEventListener('dragend', function(){
            th.style.opacity = '';
            Array.from(thead.querySelectorAll('th')).forEach(function(t){ t.style.borderLeft = ''; });
          });
          th.addEventListener('dragover', function(e){
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            Array.from(thead.querySelectorAll('th')).forEach(function(t){ t.style.borderLeft = ''; });
            th.style.borderLeft = '2px solid #888';
          });
          th.addEventListener('dragleave', function(){
            th.style.borderLeft = '';
          });
          th.addEventListener('drop', function(e){
            e.preventDefault();
            th.style.borderLeft = '';
            if (!dragSrcEl || dragSrcEl === th) return;
            var srcIdx = _thIdx(dragSrcEl);
            var tgtIdx = _thIdx(th);
            if (srcIdx < 0 || tgtIdx < 0 || srcIdx === tgtIdx) return;
            var headerRow = thead.querySelector('tr:first-child');
            if (tgtIdx > srcIdx) {
              headerRow.insertBefore(dragSrcEl, th.nextSibling);
            } else {
              headerRow.insertBefore(dragSrcEl, th);
            }
            Array.from(tbody.querySelectorAll('tr')).forEach(function(tr){
              var tds = tr.querySelectorAll('td');
              if (tds.length <= Math.max(srcIdx, tgtIdx)) return;
              var srcTd = tds[srcIdx];
              var tgtTd = tds[tgtIdx];
              if (tgtIdx > srcIdx) {
                tr.insertBefore(srcTd, tgtTd.nextSibling);
              } else {
                tr.insertBefore(srcTd, tgtTd);
              }
            });
            _saveOrder();
            dragSrcEl = null;
          });
          // 双击列头将该列移至最前
          th.addEventListener('dblclick', function(e){
            e.stopPropagation();
            var curIdx = _thIdx(th);
            if (curIdx <= 0) return;
            var headerRow = thead.querySelector('tr:first-child');
            headerRow.insertBefore(th, headerRow.firstChild);
            Array.from(tbody.querySelectorAll('tr')).forEach(function(tr){
              var tds = tr.querySelectorAll('td');
              if (tds.length <= curIdx) return;
              tr.insertBefore(tds[curIdx], tds[0]);
            });
            _saveOrder();
          });
        });
      })();
    })
    .catch(function(){
      var content = document.getElementById('holdingsContent');
      if (content) content.innerHTML = '<span style="color:#ef5350;">加载失败</span>';
    });
}

/** 持仓评分设置弹窗 */
var _hldDimsData = null;
/** 锁定为绝对标准的维度（自动校准不改变曲线） */
var _HLD_LOCKED_KEYS = {roe:1, debt_ratio:1, quick_ratio:1, mdd_1y:1, pe:1, net_profit_margin:1};
/** 维度分类映射 */
var _HLD_DIM_CAT = {
  roe:'盈利能力', main_biz_margin:'盈利能力', net_profit_margin:'盈利能力',
  cost_profit_margin:'盈利能力', roa:'盈利能力',
  rev_growth:'成长性', net_profit_growth:'成长性',
  debt_ratio:'偿债/风险', quick_ratio:'偿债/风险', mdd_1y:'偿债/风险',
  cf_ps:'现金流', cashflow_to_profit:'现金流',
  pe:'估值', pb:'估值',
  ret_1m:'市场表现', ret_3m:'市场表现', ret_1y:'市场表现', wk_position:'市场表现'
};
var _HLD_CAT_ORDER = ['盈利能力','成长性','偿债/风险','现金流','估值','市场表现'];
/** 维度数据源分类：财报(季报) vs 行情(日频) vs K线(日频) */
var _HLD_DATA_SRC = {
  roe:'财报', main_biz_margin:'财报', net_profit_margin:'财报',
  cost_profit_margin:'财报', roa:'财报',
  rev_growth:'财报', net_profit_growth:'财报',
  debt_ratio:'财报', quick_ratio:'财报',
  cf_ps:'财报', cashflow_to_profit:'财报',
  mdd_1y:'K线', wk_position:'K线',
  ret_1m:'K线', ret_3m:'K线', ret_1y:'K线',
  pe:'行情', pb:'行情'
};
var _HLD_SRC_COLORS = { '财报':'#42a5f5', 'K线':'#ffa726', '行情':'#66bb6a' };
var _HLD_SRC_TIPS = { '财报':'基于最新季度财报数据，滞后1-3个月', 'K线':'基于近252个交易日K线数据，最新交易日', '行情':'基于当日实时行情数据' };
/** 持仓评分维度详细解释 */
var _HLD_DIM_DESC = {
  roe:'【绝对标准】净资产收益率。净利润÷净资产，巴菲特最看重的指标。>15%优秀，>20%卓越。越高越好。自动校准不改变此曲线。',
  main_biz_margin:'主营业务利润率。主营业务利润÷营收，反映核心业务盈利水平。越高说明主业竞争力越强。',
  net_profit_margin:'销售净利率。净利润÷营收，每元营收能赚多少利润。>15%优秀。越高越好。',
  debt_ratio:'【绝对标准】资产负债率。总负债÷总资产，衡量财务杠杆风险。<50%稳健，50-70%正常，>70%偏高。越低越好。自动校准不改变此曲线。',
  rev_growth:'营收增长率。主营业务收入同比增速，衡量公司成长性。>10%优秀。越高越好。',
  quick_ratio:'【绝对标准】速动比率。(流动资产-存货)÷流动负债，衡量短期偿债能力。>1安全，<0.5警惕。越高越安全。自动校准不改变此曲线。',
  cf_ps:'每股经营性现金流。经营现金流÷总股本，比EPS更真实的现金造血能力。越高越好。',
  mdd_1y:'【绝对标准】近1年最大回撤。过去1年内从最高到最低的最大跌幅。<15%优秀，>30%需警惕。越低越好。自动校准不改变此曲线。',
  ret_1m:'近1月收益率。过去22个交易日的价格表现，短期动量指标。越高越好。',
  ret_3m:'近3月收益率。过去66个交易日的价格表现，中期动量指标。越高越好。',
  ret_1y:'近1年收益率。过去252个交易日的价格表现，长期动量指标。越高越好。',
  pe:'【绝对标准】市盈率PE。股价÷每股收益，最常用的估值指标。同行业对比更有意义。自动校准不改变此曲线。',
  wk_position:'52周相对位置。当前价在52周高低区间内的位置%。>70%偏高位，<30%偏低位。',
  pb:'市净率PB。股价÷每股净资产。与PE互补，适用于金融/周期股，PB<1可能破净。',
  roa:'总资产利润率ROA。净利润÷总资产。衡量全部资产的盈利效率，与ROE互补。越高越好。',
  gross_margin:'销售毛利率。(营收-营业成本)÷营收。衡量产品定价权和护城河。注意：新浪数据源可能无数据。',
  net_profit_growth:'净利润增长率。净利润同比增速，衡量盈利增长动力。>10%良好。越高越好。',
  cost_profit_margin:'成本费用利润率。利润总额÷成本费用总额。每元成本能创造多少利润，>100%优秀。越高越好。',
  cashflow_to_profit:'经营现金流÷净利润。衡量利润的现金支撑质量。>1说明利润有真实现金流保障，<0.5需警惕。越高越好。',
  main_biz_cost_ratio:'主营业务成本率。主营业务成本÷营收，越低说明毛利率越高。越低越好。',
  turnover:'换手率。成交量÷流通股本，反映交易活跃度。过高可能见顶，过低可能无人问津。',
  vol_ratio:'量比。当前成交量÷5日均量。>2明显放量，<0.5明显缩量。',
  amplitude:'振幅。(最高-最低)÷昨收。反映日内波动程度，过高说明多空分歧大。',
  float_mkt_cap:'流通市值。可在二级市场自由交易的市值，区分大小盘风格。',
  p:'持仓占比。该股票占基金净值的比例，越高说明基金经理越看好。',
  ret_1w:'近1周涨跌幅。过去5个交易日的价格表现，超短期动量。越高越好。',
  turnover_amount:'成交额。当日总成交金额，衡量流动性。',
  volume:'成交量。当日总成交手数，衡量交易活跃度。',
  limit_up:'涨停价。当日最大可成交价(昨收×1.1/1.2)。盘中可能无数据。',
  limit_down:'跌停价。当日最小可成交价(昨收×0.9/0.8)。',
  nav_ps:'每股净资产。净资产÷总股本，是PB的估值基础。越高家底越厚。',
  retained_ps:'每股未分配利润。公司累积可分配利润，越高分红潜力越大。',
  capital_reserve_ps:'每股资本公积金。公司资本储备，越高转增股本潜力越大。',
  total_assets:'总资产。公司全部资产规模，体量越大抗风险能力越强。',
  current_ratio:'流动比率。流动资产÷流动负债，比速动比率更宽松的偿债指标。>2安全。',
  net_asset_growth:'净资产增长率。净资产同比增速，衡量股东权益增长。越高越好。',
  mdd_2y:'近2年最大回撤。过去2年内最大跌幅，更长周期的风险衡量。越低越好。'
};
/** 持仓评分字段中文名映射 */
var _HLD_FIELD_NAMES = {
  p:'占比%', ret_1w:'近1周涨跌%', pb:'市净率PB', turnover:'换手率%',
  vol_ratio:'量比', float_mkt_cap:'流通市值(亿)', open:'今开(元)',
  amplitude:'振幅%', nav_ps:'每股净资产(元)',
  total_assets:'总资产(亿)', current_ratio:'流动比率',
  net_asset_growth:'净资产增长率%', capital_reserve_ps:'每股资本公积(元)',
  roa:'总资产利润率%', ret_1m:'近1月收益%', ret_3m:'近3月收益%',
  mdd_2y:'近2年回撤%', gross_margin:'销售毛利率%',
  retained_ps:'每股未分配利润(元)',
  main_biz_margin:'主营业务利润率%',
  net_profit_growth:'净利润增长率%', turnover_amount:'成交额(万元)',
  volume:'成交量(手)', limit_up:'涨停价', limit_down:'跌停价',
  main_biz_cost_ratio:'主营业务成本率%', total_asset_growth:'总资产增长率%',
  cash_ratio:'现金比率%', cost_profit_margin:'成本费用利润率%',
  cashflow_to_profit:'经营现金流/净利润'
};
var _HLD_FIELD_DESC = {
  p:'该股票占基金净值比例', ret_1w:'过去5个交易日涨跌幅', pb:'股价÷每股净资产',
  turnover:'成交量÷流通股本', vol_ratio:'当前量÷5日均量',
  float_mkt_cap:'可自由交易市值', open:'今日开盘价',
  amplitude:'(最高-最低)÷昨收',
  nav_ps:'净资产÷总股本',
  retained_ps:'累计可分配利润', total_assets:'公司全部资产规模',
  current_ratio:'流动资产÷流动负债', net_asset_growth:'净资产同比增速',
  capital_reserve_ps:'资本储备金', roa:'净利润÷总资产',
  ret_1m:'近22个交易日涨幅', ret_3m:'近66个交易日涨幅',
  mdd_2y:'近2年最大回撤', gross_margin:'(营收-成本)÷营收',
  main_biz_margin:'主营业务利润÷营收',
  net_profit_growth:'净利润同比增速，用于PEG计算',
  turnover_amount:'当日成交额(万元)', volume:'当日成交量(手)',
  limit_up:'当日涨停价', limit_down:'当日跌停价',
  main_biz_cost_ratio:'主营业务成本÷营收，越低利润越高',
  total_asset_growth:'总资产同比增速，衡量扩张速度',
  cash_ratio:'(现金+等价物)÷流动负债，最保守偿债指标',
  cost_profit_margin:'利润÷成本费用，衡量成本控制',
  cashflow_to_profit:'经营现金流÷净利润，>1利润质量高'
};
function showHldDimsEditor() {
  var backdrop = document.getElementById("hldDimBackdrop");
  if (!backdrop) {
    backdrop = document.createElement("div"); backdrop.id = "hldDimBackdrop";
    document.body.appendChild(backdrop);
  }
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:99998;background:rgba(0,0,0,0.5);";
  backdrop.onclick = function(){ backdrop.style.display='none'; var m=document.getElementById('hldDimModal'); if(m)m.style.display='none'; };
  var el = document.getElementById("hldDimModal");
  if (!el) { el = document.createElement("div"); el.id = "hldDimModal"; document.body.appendChild(el); }
  fetch(API + '/api/holdings-dims').then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok || !d.dims) return;
    _hldDimsData = d.dims;
    // 按分类排序
    var sorted = d.dims.slice().sort(function(a, b){
      var ca = _HLD_DIM_CAT[a.key] || '';
      var cb = _HLD_DIM_CAT[b.key] || '';
      var ia = _HLD_CAT_ORDER.indexOf(ca);
      var ib = _HLD_CAT_ORDER.indexOf(cb);
      if (ia !== ib) return ia - ib;
      return (a.name || a.key).localeCompare(b.name || b.key);
    });
    var html = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;font-size:12px;color:#ccc;max-width:600px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
      + '<span style="font-size:14px;font-weight:600;color:#e0e0e0;">📊 持仓评分设置</span>'
      + '<span onclick="closeHldDimEditor()" style="cursor:pointer;color:#555;font-size:16px;">&times;</span></div>'
      + '<div style="font-size:11px;color:#666;margin-bottom:8px;">调整各维度权重，分数越高代表该股票在该维度表现越好。</div>'
      + '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;"><thead><tr style="background:#2a2a2a;">'
      + '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;">维度</th>'
      + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;">权重</th>'
      + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;">曲线</th>'
      + '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;"></th></tr></thead><tbody>';
    var lastCat = '';
    sorted.forEach(function(dim, i){
      var cat = _HLD_DIM_CAT[dim.key] || '';
      if (cat && cat !== lastCat) {
        html += '<tr style="background:rgba(255,255,255,0.03);"><td colspan="4" style="padding:4px 8px;color:#888;font-size:10px;font-weight:600;letter-spacing:1px;border-bottom:1px solid #333;">▸ ' + htmlEscape(cat) + '</td></tr>';
        lastCat = cat;
      }
      var pct = dim.w;
      html += '<tr>'
        + '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;cursor:help;" title="' + htmlEscape(_HLD_DIM_DESC[dim.key] || '') + '">' + htmlEscape(dim.name) + (_HLD_LOCKED_KEYS[dim.key] ? ' <span style="color:#888;font-size:9px;">🔒</span>' : '') + '<br><span style="color:#555;font-size:10px;">' + htmlEscape(dim.key) + '</span></td>'
        + '<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;"><input type="number" min="0" max="100" value="' + pct + '" style="width:50px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:2px 6px;text-align:right;font-family:Consolas;font-size:12px;" onchange="markHldDimsDirty();updateHldTotalWeight()">%</td>'
        + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="toggleHldCurve(this,' + i + ')" style="background:none;border:none;color:#42a5f5;cursor:pointer;font-size:14px;">📐</button></td>'
        + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="removeHldDim(this)" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:14px;">✖</button></td>'
        + '</tr>'
        + '<tr class="hld-curve-row" style="display:none;background:rgba(255,255,255,0.02);" data-idx="' + i + '">'
        + '<td colspan="3" style="padding:6px 12px;">'
        + '<div style="font-size:11px;color:#888;margin-bottom:4px;">评分曲线（值 → 得分）</div>'
        + '<table style="border-collapse:collapse;font-size:11px;">'
        + dim.curve.map(function(p, j){
          return '<tr><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[0] + '" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"> →</td>'
            + '<td style="padding:2px 4px;"><input type="number" step="any" value="' + p[1] + '" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"></td>'
            + '<td style="padding:2px 4px;">' + (j > 0 ? '<button onclick="this.closest(\'tr\').remove();markHldDimsDirty()" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button>' : '') + '</td></tr>';
        }).join('')
        + '<tr><td colspan="3" style="padding:2px 4px;"><button onclick="addHldCurvePoint(this,' + i + ')" style="background:none;border:1px dashed rgba(255,255,255,0.2);border-radius:3px;color:#888;padding:2px 10px;font-size:10px;cursor:pointer;">+ 添加断点</button></td></tr>'
        + '</table></td></tr>';
    });
    html += '</tbody></table></div>'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;">'
      + '<div><button onclick="addHldDim()" style="background:none;border:1px dashed rgba(255,255,255,0.2);border-radius:4px;color:#888;padding:4px 14px;font-size:11px;cursor:pointer;">+ 添加维度</button></div>'
      + '<div id="hldTotalWeight" style="font-size:11px;color:#666;">总权重: <span id="hldTotalWeightVal">0</span></div></div>'
      + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px;padding-top:8px;border-top:1px solid #333;">'
      + '<button onclick="calibrateHldDims()" style="background:linear-gradient(135deg,#ffa726,#f57c00);border:none;color:#fff;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">📐 自动校准</button>'
      + '<button onclick="saveHldDims()" style="background:#42a5f5;border:none;color:#fff;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">💾 保存</button>'
      + '<button onclick="resetHldDims()" style="background:rgba(255,255,255,0.05);border:1px solid #444;color:#888;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">↺ 重置</button>'
      + '<button onclick="closeHldDimEditor()" style="background:rgba(255,255,255,0.05);border:1px solid #444;color:#888;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">取消</button>'
      + '</div></div>';
    el.innerHTML = html;
    updateHldTotalWeight();
  });
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:99999;width:95%;max-width:620px;max-height:80vh;overflow-y:auto;";
  el.onclick = function(e){ e.stopPropagation(); };
}
function closeHldDimEditor() {
  document.getElementById('hldDimModal').style.display = 'none';
  document.getElementById('hldDimBackdrop').style.display = 'none';
}
function removeHldDim(btn) {
  var tr = btn.closest('tr');
  if (tr) {
    var name = tr.querySelector('td:first-child')?.textContent.trim() || '';
    if (confirm('确定删除维度「' + name + '」？')) {
      tr.remove();
      var curveRow = tr.nextElementSibling;
      if (curveRow && curveRow.classList.contains('hld-curve-row')) curveRow.remove();
      markHldDimsDirty(); updateHldTotalWeight();
    }
  }
}
function addHldDim() {
  // 从当前持仓数据中提取可用的数值列
  var data = window._hldHoldingsData;
  if (!data || !data.length) { alert('请先打开基金持仓弹窗'); return; }
  // 获取已有的维度key
  var existingKeys = (_hldDimsData || []).map(function(d){ return d.key; });
  // 找出可用字段（排除非数值字段和已有维度）
  var skipKeys = {n:'n',c:'c',m:'m',chg:'chg',peg:'peg',dividend_yield:'dividend_yield',
                   listing_date:'listing_date',industry:'industry',main_biz:'main_biz',
                   reg_capital:'reg_capital',establish_date:'establish_date',
                   hld_score:'hld_score',hld_dim_scores:'hld_dim_scores',
                   wk_high:'wk_high',wk_low:'wk_low',ps:'ps',price:'price',mkt_cap:'mkt_cap'};
  var first = data[0];
  var available = [];
  // 字段中文名映射
  var FIELD_NAMES = {
    p:'占比%', ret_1w:'近1周涨跌%', pb:'市净率PB', turnover:'换手率%',
    vol_ratio:'量比', float_mkt_cap:'流通市值(亿)', open:'今开(元)',
    amplitude:'振幅%', nav_ps:'每股净资产(元)',
    total_assets:'总资产(亿)', current_ratio:'流动比率',
    net_asset_growth:'净资产增长率%', capital_reserve_ps:'每股资本公积(元)',
    roa:'总资产利润率%', ret_1m:'近1月收益%', ret_3m:'近3月收益%',
    mdd_2y:'近2年回撤%', gross_margin:'销售毛利率%',
    retained_ps:'每股未分配利润(元)',
    establish_date:'成立日期', listing_date:'上市日期',
    main_biz_margin:'主营业务利润率%',
    net_profit_growth:'净利润增长率%', turnover_amount:'成交额(万元)',
    volume:'成交量(手)', limit_up:'涨停价', limit_down:'跌停价',
    main_biz_cost_ratio:'主营业务成本率%', total_asset_growth:'总资产增长率%',
    cash_ratio:'现金比率%', cost_profit_margin:'成本费用利润率%',
    cashflow_to_profit:'经营现金流/净利润'
  };
  var FIELD_DESC = {
    p:'该股票占基金净值比例', ret_1w:'过去5个交易日涨跌幅', pb:'股价÷每股净资产',
    turnover:'成交量÷流通股本', vol_ratio:'当前量÷5日均量',
    float_mkt_cap:'可自由交易市值', open:'今日开盘价',
    amplitude:'(最高-最低)÷昨收',
    nav_ps:'净资产÷总股本',
    retained_ps:'累计可分配利润', total_assets:'公司全部资产规模',
    current_ratio:'流动资产÷流动负债', net_asset_growth:'净资产同比增速',
    capital_reserve_ps:'资本储备金', roa:'净利润÷总资产',
    ret_1m:'近22个交易日涨幅', ret_3m:'近66个交易日涨幅',
    mdd_2y:'近2年最大回撤', gross_margin:'(营收-成本)÷营收',
    main_biz_margin:'主营业务利润÷营收',
    net_profit_growth:'净利润同比增速', turnover_amount:'当日成交额(万元)',
    volume:'当日成交量(手)', limit_up:'当日涨停价', limit_down:'当日跌停价',
    main_biz_cost_ratio:'主营业务成本÷营收', total_asset_growth:'总资产同比增速',
    cash_ratio:'(现金+等价物)÷流动负债', cost_profit_margin:'利润÷成本费用',
    cashflow_to_profit:'经营现金流÷净利润'
  };
  for (var key in first) {
    if (skipKeys[key]) continue;
    if (existingKeys.indexOf(key) >= 0) continue;
    if (typeof first[key] === 'number') available.push(key);
  }
  if (!available.length) { alert('没有可添加的维度了'); return; }
  // 显示选择框
  var selectHtml = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;font-size:12px;color:#ccc;max-width:400px;">'
    + '<div style="font-size:13px;font-weight:600;color:#e0e0e0;margin-bottom:10px;">选择要添加的维度</div>'
    + '<select id="hldNewDimSelect" style="width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:6px 8px;font-size:12px;margin-bottom:6px;">'
    + available.map(function(k){
        var label = _HLD_FIELD_NAMES[k] || k.replace(/_/g,' ');
        var desc = _HLD_FIELD_DESC[k] || '';
        return '<option value="' + k + '">' + label + (desc ? ' — ' + desc : '') + '</option>';
      }).join('')
    + '</select>'
    + '<select id="hldNewDimCat" style="width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:6px 8px;font-size:12px;margin-bottom:10px;">'
    + _HLD_CAT_ORDER.map(function(c){ return '<option value="' + c + '">' + c + '</option>'; }).join('')
    + '<option value="">其他</option></select>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;">'
    + '<button onclick="confirmAddHldDim()" style="background:#42a5f5;border:none;color:#fff;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">确认添加</button>'
    + '<button onclick="this.closest(\'[id]\').style.display=\'none\'" style="background:rgba(255,255,255,0.05);border:1px solid #444;color:#888;padding:4px 16px;border-radius:4px;cursor:pointer;font-size:12px;">取消</button>'
    + '</div></div>';
  var backdrop = document.getElementById("hldAddDimBackdrop");
  if (!backdrop) { backdrop = document.createElement("div"); backdrop.id = "hldAddDimBackdrop"; document.body.appendChild(backdrop); }
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;background:rgba(0,0,0,0.5);";
  backdrop.onclick = function(){ backdrop.style.display='none'; var m=document.querySelector('[id="hldAddDimModal"]'); if(m)m.style.display='none'; };
  var el = document.getElementById("hldAddDimModal");
  if (!el) { el = document.createElement("div"); el.id = "hldAddDimModal"; document.body.appendChild(el); }
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:100000;";
  el.innerHTML = selectHtml;
}
function confirmAddHldDim() {
  var sel = document.getElementById('hldNewDimSelect');
  if (!sel) return;
  var key = sel.value;
  if (!key) return;
  // 生成默认曲线
  var data = window._hldHoldingsData;
  var vals = [];
  data.forEach(function(h){ var v = h[key]; if (typeof v === 'number') vals.push(v); });
  vals.sort(function(a,b){ return a - b; });
  var curve = [];
  if (vals.length >= 2) {
    var mn = vals[0], mx = vals[vals.length-1];
    for (var i = 0; i <= 5; i++) {
      var x = mn + (mx - mn) * i / 5;
      curve.push([Math.round(x*100)/100, Math.round(i*20)]);
    }
  } else {
    curve = [[0,0],[100,100]];
  }
  // 添加到编辑器
  var tbl = document.querySelector('#hldDimModal table tbody');
  if (!tbl) return;
  // 读取分类并更新映射
  var catSel = document.getElementById('hldNewDimCat');
  var cat = catSel ? catSel.value : '';
  if (cat) _HLD_DIM_CAT[key] = cat;
  // 找到分类插入位置
  var insertBefore = null;
  if (cat) {
    var rows = tbl.querySelectorAll('tr:not(.hld-curve-row)');
    var foundCat = false;
    for (var ri = 0; ri < rows.length; ri++) {
      var td = rows[ri].querySelector('td[colspan]');
      if (td) {
        var txt = td.textContent.trim();
        if (txt === '▸ ' + cat) { foundCat = true; continue; }
        if (foundCat && txt.startsWith('▸ ')) { insertBefore = rows[ri]; break; }
      }
    }
    if (!foundCat) {
      // 当前分类没有分类行，找到正确位置插入
      var catIdx = _HLD_CAT_ORDER.indexOf(cat);
      for (var ri2 = 0; ri2 < rows.length; ri2++) {
        var td2 = rows[ri2].querySelector('td[colspan]');
        if (td2) {
          var txt2 = td2.textContent.trim();
          if (txt2.startsWith('▸ ')) {
            var existingCatIdx = _HLD_CAT_ORDER.indexOf(txt2.replace('▸ ',''));
            if (existingCatIdx > catIdx) { insertBefore = rows[ri2]; break; }
          }
        }
      }
    }
  }
  var idx = Date.now(); // 唯一ID
  var label = _HLD_FIELD_NAMES[key] || key.replace(/_/g,' ');
  var isLocked = _HLD_LOCKED_KEYS[key];
  // 创建主行（仅TD）
  var tr = document.createElement('tr');
  tr.innerHTML = '<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;cursor:help;" title="' + htmlEscape(_HLD_DIM_DESC[key] || '') + '">' + htmlEscape(label) + (isLocked ? ' <span style="color:#666;font-size:9px;">🔒</span>' : '') + '<br><span style="color:#555;font-size:10px;">' + key + '</span></td>'
    + '<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;"><input type="number" min="0" max="100" value="5" style="width:50px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:2px 6px;text-align:right;font-family:Consolas;font-size:12px;" onchange="markHldDimsDirty();updateHldTotalWeight()">%</td>'
    + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="toggleHldCurve(this,' + idx + ')" style="background:none;border:none;color:#42a5f5;cursor:pointer;font-size:14px;">📐</button></td>'
    + '<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;"><button onclick="removeHldDim(this)" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:14px;">✖</button></td>';
  // 创建曲线行
  var cr = document.createElement('tr');
  cr.className = 'hld-curve-row';
  cr.style.display = 'none';
  cr.style.background = 'rgba(255,255,255,0.02)';
  cr.setAttribute('data-idx', idx);
  var curveHtml = '<td colspan="4" style="padding:6px 12px;">'
    + '<div style="font-size:11px;color:#888;margin-bottom:4px;">评分曲线（值 → 得分）</div>'
    + '<table style="border-collapse:collapse;font-size:11px;">';
  curve.forEach(function(p, j){
    curveHtml += '<tr><td style="padding:2px 4px;"><input type="number" step="any" value="' + p[0] + '" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"> →</td>'
      + '<td style="padding:2px 4px;"><input type="number" step="any" value="' + p[1] + '" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"></td>'
      + '<td style="padding:2px 4px;">' + (j > 0 ? '<button onclick="this.closest(\'tr\').remove();markHldDimsDirty()" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button>' : '') + '</td></tr>';
  });
  curveHtml += '<tr><td colspan="3" style="padding:2px 4px;"><button onclick="addHldCurvePoint(this,' + idx + ')" style="background:none;border:1px dashed rgba(255,255,255,0.2);border-radius:3px;color:#888;padding:2px 10px;font-size:10px;cursor:pointer;">+ 添加断点</button></td></tr>'
    + '</table></td>';
  cr.innerHTML = curveHtml;
  // 如果分类行不存在且选定了分类，创建分类行并插入
  if (cat && !foundCat) {
    var catTr = document.createElement('tr');
    catTr.style.background = 'rgba(255,255,255,0.03)';
    catTr.innerHTML = '<td colspan="4" style="padding:4px 8px;color:#888;font-size:10px;font-weight:600;letter-spacing:1px;border-bottom:1px solid #333;">▸ ' + cat + '</td>';
    if (insertBefore) {
      tbl.insertBefore(catTr, insertBefore);
    } else {
      tbl.appendChild(catTr);
    }
  }
  // 重新查找插入位置（因刚可能插入了分类行）
  if (cat && !insertBefore) {
    var insertAfter = tbl.querySelector('tr:last-child');
    tbl.appendChild(tr);
    tbl.appendChild(cr);
  } else if (insertBefore) {
    tbl.insertBefore(tr, insertBefore);
    tbl.insertBefore(cr, insertBefore);
  } else {
    tbl.appendChild(tr);
    tbl.appendChild(cr);
  }
  // 关闭添加弹窗
  var backdrop = document.getElementById('hldAddDimBackdrop');
  if (backdrop) backdrop.style.display = 'none';
  var modal = document.getElementById('hldAddDimModal');
  if (modal) modal.style.display = 'none';
  // 更新数据
  _hldDimsData.push({key: key, name: label, w: 5, curve: curve});
  markHldDimsDirty();
  updateHldTotalWeight();
}
function toggleHldCurve(btn, idx) {
  var row = btn.closest('tr').nextElementSibling;
  if (row && row.classList.contains('hld-curve-row') && parseInt(row.dataset.idx) === idx) {
    row.style.display = row.style.display === 'none' ? '' : 'none';
  }
}
function addHldCurvePoint(btn, idx) {
  var tbl = btn.closest('table');
  var lastRow = tbl.querySelector('tr:last-child');
  var newRow = document.createElement('tr');
  newRow.innerHTML = '<td style="padding:2px 4px;"><input type="number" step="any" value="0" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"> →</td>'
    + '<td style="padding:2px 4px;"><input type="number" step="any" value="0" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:3px;color:#e0e0e0;padding:2px 4px;font-family:Consolas;font-size:11px;" onchange="markHldDimsDirty()"></td>'
    + '<td style="padding:2px 4px;"><button onclick="this.closest(\'tr\').remove();markHldDimsDirty()" style="background:none;border:none;color:#ef5350;cursor:pointer;font-size:12px;">✖</button></td>';
  lastRow.parentNode.insertBefore(newRow, lastRow);
}
function markHldDimsDirty() { _hldDimsDirty = true; }
/** 实时更新总权重显示 */
function updateHldTotalWeight() {
  var total = 0;
  var modal = document.getElementById('hldDimModal');
  if (modal) {
    var tbody = modal.querySelector('table > tbody');
    if (tbody) {
      // 只取 tbody 的直接子 tr（排除嵌套曲线表内的 tr）
      Array.prototype.forEach.call(tbody.children, function(tr){
        if (tr.tagName !== 'TR' || tr.classList.contains('hld-curve-row')) return;
        var td = tr.querySelector('td:nth-child(2)');
        if (td) {
          var inp = td.querySelector('input[type="number"]');
          if (inp) total += parseInt(inp.value) || 0;
        }
      });
    }
  }
  var el = document.getElementById('hldTotalWeightVal');
  if (el) {
    el.textContent = total;
    var container = el.parentElement;
    if (container) {
      if (total === 100) {
        container.style.color = '#66bb6a';
      } else if (total > 100) {
        container.style.color = '#ef5350';
      } else {
        container.style.color = '#ffa726';
      }
    }
  }
}
var _hldDimsDirty = false;
function _readHldDimsFromUI() {
  var tbl = document.getElementById('hldDimModal').querySelector('table');
  var rows = tbl.querySelectorAll('tbody tr:not(.hld-curve-row)');
  var dims = [];
  rows.forEach(function(tr){
    var nameEl = tr.querySelector('td:first-child');
    if (!nameEl) return;
    var key = nameEl.textContent.trim();
    // Actually key is in the span
    var keySpan = nameEl.querySelector('span');
    if (!keySpan) return;
    var dimKey = keySpan.textContent.trim();
    var wInput = tr.querySelector('input[type="number"]');
    var w = wInput ? parseInt(wInput.value) || 1 : 1;
    // Read curve from the hidden row
    var curveRow = tr.nextElementSibling;
    var curve = [];
    if (curveRow && curveRow.classList.contains('hld-curve-row')) {
      var inputs = curveRow.querySelectorAll('tr:not(:last-child)');
      inputs.forEach(function(cr){
        var ins = cr.querySelectorAll('input');
        if (ins.length >= 2) curve.push([parseFloat(ins[0].value)||0, parseFloat(ins[1].value)||0]);
      });
    }
    var orig = _hldDimsData.find(function(d){ return d.key === dimKey; });
    dims.push({key: dimKey, name: orig ? orig.name : dimKey, w: w, curve: curve.length >= 2 ? curve : (orig ? orig.curve : [[0,0],[100,100]])});
  });
  return dims;
}
function saveHldDims() {
  var dims = _readHldDimsFromUI();
  fetch(API + '/api/holdings-dims/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({dims:dims})})
    .then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) { closeHldDimEditor(); _refreshHoldingsContent(); }
      else alert('保存失败: ' + (d.error||''));
    });
}
function resetHldDims() {
  if (!confirm('确定重置为默认设置？')) return;
  fetch(API + '/api/holdings-dims/reset', {method:'POST'})
    .then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) { closeHldDimEditor(); _refreshHoldingsContent(); }
    });
}
/** 刷新持仓弹窗评分数据（无需重新打开弹窗） */
function _refreshHoldingsContent() {
  var code = window._hldFundCode;
  var name = window._hldFundName;
  if (!code || !name) return;
  var content = document.getElementById('holdingsContent');
  if (content) content.innerHTML = '<span style="color:#888;">⏳ 刷新评分...</span>';
  showHoldings(code, name);
}
function calibrateHldDims() {
  // 从持仓弹窗中获取当前基金代码
  var modal = document.getElementById('holdingsModal');
  var code = '';
  if (modal) {
    var m = modal.textContent.match(/\b\d{6}\b/);
    if (m) code = m[0];
  }
  if (!code) { alert('请先打开基金持仓弹窗'); return; }
  if (!confirm('基于当前基金持仓数据自动校准评分曲线，确定继续？')) return;
  var btn = document.querySelector('#hldDimModal button:first-child');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 校准中...'; }
  fetch(API + '/api/holdings-dims/calibrate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:code})})
    .then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) {
        if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
        closeHldDimEditor();
        _refreshHoldingsContent();
      } else {
        if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
        alert('校准失败: ' + (d.error||''));
      }
    }).catch(function(e){
      if (btn) { btn.disabled = false; btn.textContent = '📐 自动校准'; }
      alert('校准失败: ' + (e.message||e));
    });
}

/** 持仓评分明细弹窗 */
function showHldScoreDetail(stockName, stockCode, tdEl) {
  var data = window._hldHoldingsData;
  if (!data) return;
  var h = null;
  for (var i = 0; i < data.length; i++) {
    if (data[i].c === stockCode) { h = data[i]; break; }
  }
  if (!h || !h.hld_dim_scores) return;
  var dims = h.hld_dim_scores.sort(function(a,b){ return b.w - a.w; });
  var html = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;margin:8px 0;font-size:12px;color:#ccc;max-height:80vh;overflow-y:auto;">'
    + '<div style="font-size:13px;font-weight:600;color:#e0e0e0;margin-bottom:6px;">📊 ' + htmlEscape(stockName) + ' <span style="color:#666;font-size:11px;">' + stockCode + '</span> 评分明细</div>'
    + '<div style="font-size:11px;color:#666;margin-bottom:4px;">总分: <span style="font-weight:600;color:' + (h.hld_score >= 80 ? '#66bb6a' : h.hld_score >= 60 ? '#42a5f5' : h.hld_score >= 40 ? '#ffa726' : '#ef5350') + ';">' + h.hld_score + '</span></div>'
    + '<div style="font-size:10px;color:#555;margin-bottom:6px;border-bottom:1px solid #333;padding-bottom:6px;">'
    + '📅 <span style="color:#4dd0e1;">●</span>Q1 <span style="color:#66bb6a;">●</span>Q2 <span style="color:#ffa726;">●</span>Q3 <span style="color:#ab47bc;">●</span>Q4'
    + ' — 财报数据按季度着色，K线/行情为近期数据。仅供参考。'
    + '</div>'
    + '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
    + '<thead><tr style="background:#2a2a2a;"><th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #444;">维度</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">实际值</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">得分</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">权重</th>'
    + '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #444;">贡献</th></tr></thead><tbody>';
  // 先算总权重
  var totalW = 0;
  dims.forEach(function(d){ totalW += d.w; });
  var totalContrib = 0;
  dims.forEach(function(d){
    var val = d.v;
    var score = d.s;
    var isNull = (val === null || val === undefined);
    // 贡献 = 得分 × 权重 ÷ 总权重，使贡献之和 = 总分
    var rawScore = isNull ? 50 : score;
    var contrib = totalW > 0 ? rawScore * d.w / totalW : 0;
    totalContrib += contrib;
    var valStr = isNull ? '-' : (typeof val === 'number' ? (Math.abs(val) >= 100 ? val.toFixed(1) : val.toFixed(2)) : val);
    var scoreColor = isNull ? '#ffa726' : (score >= 80 ? '#66bb6a' : score >= 40 ? '#ffa726' : '#ef5350');
    var src = _HLD_DATA_SRC[d.k] || '';
    var srcColor = '#888';
    var srcTip = '';
    var srcLabel = '';
    if (src === '财报' && h.fgl_date) {
      var _fglM = parseInt(h.fgl_date.substring(5, 7), 10);
      var _fglY = parseInt(h.fgl_date.substring(0, 4), 10);
      var _q;
      if (_fglM <= 3) { _q = 'Q1'; srcColor = '#4dd0e1'; }
      else if (_fglM <= 6) { _q = 'Q2'; srcColor = '#66bb6a'; }
      else if (_fglM <= 9) { _q = 'Q3'; srcColor = '#ffa726'; }
      else { _q = 'Q4'; srcColor = '#ab47bc'; }
      srcLabel = _q;
      srcTip = _fglY + _q + ' 财报';
    } else if (src) {
      srcColor = _HLD_SRC_COLORS[src] || '#888';
      srcLabel = src;
      srcTip = _HLD_SRC_TIPS[src] || '';
    }
    html += '<tr><td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;cursor:help;" title="' + htmlEscape(_HLD_DIM_DESC[d.k] || '') + '">' + htmlEscape(d.n) + (isNull ? ' <span style="color:#555;">(中性)</span>' : '') + (src ? ' <span style="color:' + srcColor + ';font-size:8px;border:1px solid ' + srcColor + ';border-radius:2px;padding:0 3px;cursor:help;" title="' + htmlEscape(srcTip) + '">' + srcLabel + '</span>' : '') + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + valStr + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:' + scoreColor + ';">' + (isNull ? '50.0' : score.toFixed(1)) + '</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + d.w + '%</td>'
      + '<td style="padding:3px 6px;text-align:right;border-bottom:1px solid #333;font-family:Consolas;color:#888;">' + contrib.toFixed(1) + '</td></tr>';
  });
  var avgScore = totalW > 0 ? totalContrib.toFixed(1) : '0.0';
  html += '<tr><td style="padding:4px 6px;border-top:1px solid #555;color:#888;" colspan="2">合计</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;font-weight:600;color:#66bb6a;">' + avgScore + '</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;color:#888;">' + totalW + '%</td>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;color:#888;">' + totalContrib.toFixed(1) + '</td></tr>'
    + '<td style="padding:4px 6px;text-align:right;border-top:1px solid #555;font-family:Consolas;color:#888;">' + totalContrib.toFixed(1) + '</td></tr>'
    + '</tbody></table>'
    + '<button onclick="this.closest(\'#hldScoreModal\').style.display=\'none\';document.getElementById(\'hldScoreBackdrop\').style.display=\'none\'" style="margin-top:10px;background:#333;border:1px solid #555;border-radius:4px;color:#ccc;padding:4px 12px;cursor:pointer;">关闭</button></div>';
  var backdrop = document.getElementById("hldScoreBackdrop");
  if (!backdrop) { backdrop = document.createElement("div"); backdrop.id = "hldScoreBackdrop"; document.body.appendChild(backdrop); }
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:99998;background:rgba(0,0,0,0.5);";
  backdrop.onclick = function(){ backdrop.style.display='none'; var m=document.getElementById('hldScoreModal'); if(m)m.style.display='none'; };
  var el = document.getElementById("hldScoreModal");
  if (!el) { el = document.createElement("div"); el.id = "hldScoreModal"; document.body.appendChild(el); }
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:99999;width:95%;max-width:500px;";
  el.onclick = function(e){ e.stopPropagation(); };
  el.innerHTML = html;
}

function showStockInfo(name, fullCode) {
  var backdrop = document.getElementById("stockInfoBackdrop");
  if (!backdrop) {
    backdrop = document.createElement("div");
    backdrop.id = "stockInfoBackdrop";
    document.body.appendChild(backdrop);
  }
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:99998;background:rgba(0,0,0,0.5);";
  backdrop.onclick = function(){ backdrop.style.display='none'; var m=document.getElementById('stockInfoModal'); if(m)m.style.display='none'; };

  var el = document.getElementById("stockInfoModal");
  if (!el) {
    el = document.createElement("div");
    el.id = "stockInfoModal";
    document.body.appendChild(el);
  }
  el.innerHTML = '<div style="background:#1a1a2e;border:1px solid rgba(255,255,255,0.08);border-radius:12px;box-shadow:0 12px 48px rgba(0,0,0,0.6);max-height:85vh;display:flex;flex-direction:column;">'
    + '<div style="flex-shrink:0;padding:20px 20px 10px;border-bottom:1px solid rgba(255,255,255,0.06);display:flex;justify-content:space-between;align-items:center;background:#1a1a2e;border-radius:12px 12px 0 0;">'
    + '<div><span style="font-size:16px;font-weight:700;color:#e0e0e0;">' + htmlEscape(name) + '</span>'
    + ' <span style="font-family:Consolas;font-size:12px;color:#666;">' + fullCode + '</span></div>'
    + '<span onclick="var b=document.getElementById(\'stockInfoBackdrop\');var m=document.getElementById(\'stockInfoModal\');if(b)b.style.display=\'none\';if(m)m.style.display=\'none\';" style="cursor:pointer;color:#555;font-size:20px;line-height:1;">&times;</span></div>'
    + '<div id="stockInfoContent" style="flex:1;overflow-y:auto;padding:14px 20px 10px;text-align:center;color:#888;font-size:13px;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,0.08) transparent;">\u23f3 \u52a0\u8f7d\u4e2d...</div>'
    + '<div style="flex-shrink:0;padding:6px 20px 14px;font-size:11px;color:#444;text-align:center;border-top:1px solid rgba(255,255,255,0.03);">点击背景关闭</div>'
    + '</div>';
  el.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:99999;width:90%;max-width:420px;";
  el.onclick = function(e){ e.stopPropagation(); };

  // 并行获取行情和公司资料
  var codeNum = fullCode.replace(/[a-z]/g, '');
  var marketPro = Promise.all([
    fetch(API + '/api/stock-info?code=' + encodeURIComponent(fullCode)).then(function(r){ return r.json(); }),
    fetch(API + '/api/stock-profile?code=' + codeNum).then(function(r){ return r.json(); }).catch(function(){ return {ok:false}; }),
  ]);

  marketPro.then(function(results){
    var content = document.getElementById('stockInfoContent');
    var d = results[0], pd = results[1];
    if (!d.ok || !d.data) {
      content.innerHTML = '<span style="color:#666;">暂无数据</span>';
      return;
    }
    var s = d.data, p = pd.ok && pd.data ? pd.data : null;

    function _fmt(v, unit, dec) {
      if (v === null || v === undefined || v === '') return '-';
      if (dec !== undefined && typeof v === 'number') return v.toFixed(dec) + (unit||'');
      return v + (unit||'');
    }
    function _chgClass(v) { return v > 0 ? '#ef5350' : v < 0 ? '#66bb6a' : '#bbb'; }
    function _sec(t) { return '<div style="font-size:11px;color:#888;font-weight:600;letter-spacing:0.5px;padding:8px 0 4px;border-top:1px solid rgba(255,255,255,0.05);margin-top:4px;">' + t + '</div>'; }
    function _row(l, v, s) { return '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;border-bottom:1px solid rgba(255,255,255,0.02);"><span style="color:#888;">' + l + '</span><span style="font-family:Consolas;text-align:right;' + (s||'color:#ccc;') + '">' + v + '</span></div>'; }
    function _row2(l1,v1,l2,v2) { return '<div style="display:flex;padding:3px 0;font-size:12px;border-bottom:1px solid rgba(255,255,255,0.02);"><span style="flex:1;display:flex;justify-content:space-between;padding-right:6px;"><span style="color:#888;">' + l1 + '</span><span style="font-family:Consolas;color:#ccc;text-align:right;">' + v1 + '</span></span><span style="flex:1;display:flex;justify-content:space-between;padding-left:6px;"><span style="color:#888;">' + l2 + '</span><span style="font-family:Consolas;color:#ccc;text-align:right;">' + v2 + '</span></span></div>'; }

    var chgColor = _chgClass(s.change_pct);
    var html = '';

    // ── 公司概况 ──
    if (p && p.main_business) {
      html += _sec('🏢 公司概况')
        + '<div style="padding:0 0 6px;font-size:12px;color:#ccc;line-height:1.5;">' + htmlEscape(p.main_business) + '</div>'
        + _row2('上市', _fmt(p.listing_date), '成立', _fmt(p.establish_date))
        + _row2('发行价', _fmt(p.issue_price), '注册资本', (p.registered_capital||'').replace(/万元.*/,'').replace(/(\d+)/g,function(m){return (parseInt(m)/10000).toFixed(1)+'亿';}))
        + _row2('组织', _fmt(p.organization_form), '市场', _fmt(p.listing_market));
      if (p.website) {
        var site = p.website.replace(/https?:\/\//,'').replace(/\/$/,'');
        html += _row('网址', '<span style="color:#42a5f5;">' + htmlEscape(site) + '</span>', '');
      }
    }

    // ── 财务指标 ──
    if (p && p.finances) {
      var f = p.finances;
      html += _sec('📋 财务指标')
        + _row2('每股收益', _fmt(f.eps, '', 2), '每股净资产', _fmt(f.bps, '', 2))
        + _row2('ROE', _fmt(f.roe, '%', 2), '主营利润率', _fmt(f.profit_margin, '%', 2))
        + _row2('净利增长', '<span style="color:' + _chgClass(f.net_profit_growth||0) + ';">' + (f.net_profit_growth>0?'+':'') + _fmt(f.net_profit_growth, '%', 2) + '</span>',
                '营收增长', '<span style="color:' + _chgClass(f.revenue_growth||0) + ';">' + (f.revenue_growth>0?'+':'') + _fmt(f.revenue_growth, '%', 2) + '</span>')
        + _row2('资产负债率', _fmt(f.debt_ratio, '%', 2), '流动/速动', _fmt(f.current_ratio, '', 2) + ' / ' + _fmt(f.quick_ratio, '', 2));
    }

    // ── 前五大股东 ──
    if (p && p.shareholders && p.shareholders.length) {
      html += _sec('👥 前十大股东' + (p.fund_count ? (' · ' + p.fund_count + '家基金持有') : ''));
      p.shareholders.forEach(function(sh, i){
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;font-size:12px;border-bottom:1px solid rgba(255,255,255,0.02);">'
          + '<span style="display:inline-flex;align-items:center;gap:6px;"><span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;background:rgba(255,255,255,0.05);color:#888;font-size:10px;font-weight:600;">' + (i+1) + '</span>'
          + '<span style="color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px;">' + htmlEscape(sh.name) + '</span></span>'
          + '<span style="font-family:Consolas;font-weight:600;color:#e0e0e0;">' + _fmt(sh.pct, '%') + '</span></div>';
      });
    }

    // ── 价格区 ──
    html += _sec('')
      + '<div style="text-align:center;padding:8px 0 6px;">'
      + '<span style="font-size:28px;font-weight:700;color:#e0e0e0;">' + _fmt(s.price, '', 2) + '</span>'
      + ' <span style="font-size:16px;color:' + chgColor + ';font-weight:600;">' + (s.change_pct > 0 ? '+' : '') + _fmt(s.change_pct, '%', 2) + '</span>'
      + ' <span style="font-size:13px;color:' + chgColor + ';">' + (s.change_amt > 0 ? '+' : '') + _fmt(s.change_amt, '', 2) + '</span>'
      + '</div>';

    // ── 今日行情 ──
    html += _sec('📊 今日行情')
      + _row2('最高', _fmt(s.high, '', 2), '最低', _fmt(s.low, '', 2))
      + _row2('今开', _fmt(s.open, '', 2), '昨收', _fmt(s.prev_close, '', 2))
      + _row('振幅', _fmt(s.amplitude, '%', 2))
    // ── 成交量 ──
    + _sec('📈 成交量')
      + _row2('成交量', _fmt(s.volume, '手'), '成交额', _fmt(s.amount, '万'))
      + _row2('外盘', '<span style="color:#66bb6a;">' + _fmt(s.buy_volume, '手') + '</span>',
              '内盘', '<span style="color:#ef5350;">' + _fmt(s.sell_volume, '手') + '</span>')
      + _row('换手率', _fmt(s.turnover_rate, '%', 2))
    // ── 估值与市值 ──
    + _sec('💰 估值与市值')
      + _row2('市盈率(PE)', _fmt(s.pe, '', 2), '总市值', _fmt(s.market_cap, '亿'))
      + _row('流通市值', _fmt(s.float_market_cap, '亿'))
    // ── 区间表现 ──
    + _sec('📅 区间表现')
      + _row2('52周最高', _fmt(s.high_52w, '', 2), '52周最低', _fmt(s.low_52w, '', 2))
      + _row('60日涨跌', '<span style="color:' + _chgClass(s.chg_60d) + ';">' + (s.chg_60d > 0 ? '+' : '') + _fmt(s.chg_60d, '%', 2) + '</span>', '');

    content.innerHTML = html;
  }).catch(function(){
    var content = document.getElementById('stockInfoContent');
    if (content) content.innerHTML = '<span style="color:#ef5350;">加载失败</span>';
  });
}

// 走势图放大弹窗（鼠标移动自动定位最近数据点）
function showTrendChart(el) {
  var raw = el.getAttribute('data-trend');
  if (!raw) return;
  var data;
  try { data = JSON.parse(raw); } catch(e) { return; }
  var dates = data.d, dayVals = data.v;
  if (!dates || !dayVals || dayVals.length < 2) return;
  var name = el.getAttribute('data-name') || '走势图';
  var cumVals = [], cum = 0;
  for (var vi = 0; vi < dayVals.length; vi++) { cum += dayVals[vi]; cumVals.push(cum); }
  var minV = Math.min.apply(null, cumVals), maxV = Math.max.apply(null, cumVals), range = maxV - minV || 1;
  var w = 400, h = 200, pad = 30, cw = w - pad * 2, ch = h - pad * 2;
  var pts = [];
  for (var i = 0; i < cumVals.length; i++) {
    var x = pad + i / (cumVals.length - 1) * cw;
    var y = pad + ch - (cumVals[i] - minV) / range * ch;
    pts.push({x: x, y: y, idx: i, date: dates[i], val: dayVals[i], cum: cumVals[i]});
  }
  var lineColor = cumVals[cumVals.length-1] >= cumVals[0] ? '#ef5350' : '#4caf50';
  var polyPts = pts.map(function(p){ return p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' ');
  var svgHtml = '<svg id="trendSvg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="display:block;margin:0 auto;background:transparent;cursor:crosshair;">';
  svgHtml += '<polyline fill="none" stroke="' + lineColor + '" stroke-width="2" points="' + polyPts + '"/>';
  svgHtml += '<polygon fill="' + lineColor + '" fill-opacity="0.08" points="' + polyPts + ' ' + (pad+cw) + ',' + (pad+ch) + ' ' + pad + ',' + (pad+ch) + '"/>';
  svgHtml += '<line id="trendCrosshair" x1="0" y1="0" x2="0" y2="0" stroke="#888" stroke-width="1" stroke-dasharray="4,3" opacity="0"/>';
  svgHtml += '</svg>';
  // 创建弹窗
  var backdrop = document.getElementById('trendBackdrop');
  if (!backdrop) {
    backdrop = document.createElement('div');
    backdrop.id = 'trendBackdrop';
    backdrop.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:99998;display:flex;align-items:center;justify-content:center;';
    backdrop.onclick = function(){ backdrop.style.display = 'none'; };
    document.body.appendChild(backdrop);
  }
  backdrop.style.display = 'flex';
  backdrop.innerHTML = '<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;max-width:500px;width:90%;" onclick="event.stopPropagation();">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
    + '<span style="color:#e0e0e0;font-size:14px;font-weight:600;">' + name + '</span>'
    + '<span onclick="document.getElementById(\'trendBackdrop\').style.display=\'none\'" style="cursor:pointer;color:#555;font-size:20px;">&times;</span></div>'
    + '<div id="trendTooltip" style="text-align:center;font-size:13px;color:#e0e0e0;min-height:22px;margin-bottom:6px;">\u79fb\u52a8\u9f20\u6807\u67e5\u770b</div>'
    + svgHtml + '</div>';
  // 鼠标移动事件
  var svg = document.getElementById('trendSvg');
  var crosshair = document.getElementById('trendCrosshair');
  var tooltip = document.getElementById('trendTooltip');
  svg.onmousemove = function(e) {
    var rect = svg.getBoundingClientRect();
    var scaleX = w / rect.width;
    var mouseX = (e.clientX - rect.left) * scaleX;
    // 找最近的X坐标数据点
    var nearest = pts[0];
    for (var k = 1; k < pts.length; k++) {
      if (Math.abs(pts[k].x - mouseX) < Math.abs(nearest.x - mouseX)) nearest = pts[k];
    }
    var sign = nearest.val >= 0 ? '+' : '';
    var dayColor = nearest.val >= 0 ? '#ef5350' : '#66bb6a';
    tooltip.innerHTML = '<span style="color:#aaa;">' + nearest.date + '</span> <span style="color:' + dayColor + ';font-weight:600;">' + sign + nearest.val.toFixed(2) + '%</span> <span style="color:#666;font-size:11px;margin-left:8px;">\u7d2f\u8ba1' + (nearest.cum >= 0 ? '+' : '') + nearest.cum.toFixed(2) + '%</span>';
    crosshair.setAttribute('x1', nearest.x);
    crosshair.setAttribute('x2', nearest.x);
    crosshair.setAttribute('y1', pad);
    crosshair.setAttribute('y2', pad + ch);
    crosshair.setAttribute('opacity', '0.6');
  };
  svg.onmouseleave = function() {
    crosshair.setAttribute('opacity', '0');
    tooltip.innerHTML = '\u79fb\u52a8\u9f20\u6807\u67e5\u770b';
  };
}

