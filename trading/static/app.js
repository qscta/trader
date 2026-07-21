/* ============================================================
   Trend Following 前端 · 欧易单交易所
   - 求索指数用 TradingView lightweight-charts（蜡烛图 + 十字光标 + 拖动缩放）
   ============================================================ */

let equityKlineDays = 120;
let mainRefreshTimer = null;
let _positionSymbols = [];

// lightweight-charts
const _charts = {};   // containerId -> { chart, series, container, tooltipId, klineByTime }

/* ---------------- 基础 ---------------- */
async function authFetch(url, opts = {}) {
    opts.credentials = 'same-origin';
    if (!opts.headers) opts.headers = {};
    const res = await fetch(url, opts);
    if (res.status === 401 && !url.includes('/api/login') && !url.includes('/api/check_auth')) {
        onUnauthorized();
    }
    return res;
}

async function postJSON(path, body) {
    return authFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

function el(id) { return document.getElementById(id); }
function setText(id, txt) { const e = el(id); if (e) e.textContent = txt; }
function fmt(n, d = 2) { if (n == null || isNaN(n)) return '-'; return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
function fmtPct(n) { if (n == null || isNaN(n)) return '-'; return (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'; }
function pnlClass(n) { return n >= 0 ? 'price-up' : 'price-down'; }

function onUnauthorized() {
    if (mainRefreshTimer) { clearInterval(mainRefreshTimer); mainRefreshTimer = null; }
    const o = el('loginOverlay'); if (o) o.style.display = 'flex';
}

function showAlert(message, type) {
    const c = el('alertContainer'); if (!c) return;
    const d = document.createElement('div');
    d.className = 'alert alert-' + (type || 'info');
    d.textContent = message;
    c.appendChild(d);
    setTimeout(() => d.remove(), 4000);
}

/* ---------------- 主题（亮/暗手动切换 + 记忆；首帧由 index.html 内联脚本设定） ---------------- */
function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function chartThemeOptions() {
    const dark = currentTheme() === 'dark';
    return {
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: dark ? '#9aa8c0' : '#5a6678', fontFamily: 'SF Mono, ui-monospace, monospace' },
        grid: { vertLines: { color: dark ? '#1e2740' : '#eef1f6' }, horzLines: { color: dark ? '#1e2740' : '#eef1f6' } },
        rightPriceScale: { borderColor: dark ? '#2a3550' : '#e8ebf1' },
        timeScale: { borderColor: dark ? '#2a3550' : '#e8ebf1', timeVisible: false, secondsVisible: false },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: dark ? '#3d4c6e' : '#b9c1cf', width: 1, style: 2, labelBackgroundColor: '#3b6ef5' },
            horzLine: { color: dark ? '#3d4c6e' : '#b9c1cf', width: 1, style: 2, labelBackgroundColor: '#3b6ef5' } },
    };
}

function syncThemeButton() {
    const btn = el('themeBtn'); if (!btn) return;
    const dark = currentTheme() === 'dark';
    btn.textContent = dark ? '☀️' : '🌙';
    btn.title = dark ? '切换到亮色主题' : '切换到暗色主题';
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('tf-theme', theme); } catch (e) {}
    syncThemeButton();
    Object.values(_charts).forEach(ctx => { try { ctx.chart.applyOptions(chartThemeOptions()); } catch (e) {} });
}

function toggleTheme() { applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }

/* ---------------- 登录 ---------------- */
async function doLogin() {
    const pw = el('loginPassword').value;
    try {
        const res = await authFetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) });
        const data = await res.json();
        if (data.success) {
            el('loginOverlay').style.display = 'none';
            el('loginError').textContent = '';
            bootData();
        } else {
            el('loginError').textContent = data.message || '登录失败';
        }
    } catch (e) {
        el('loginError').textContent = '网络错误';
    }
}
window.doLogin = doLogin;

async function logout() {
    try { await authFetch('/api/logout', { method: 'POST' }); } catch (e) {}
    onUnauthorized();
}

/* ---------------- 刷新调度 ---------------- */
function refreshAll() {
    refreshStatus();
    loadAccountStats();
    loadPositions();
    loadSymbols();
    loadEquityKline();
    loadStrategyParams();
    loadTrades();
    loadLogs();
}

function bootData() {
    refreshAll();
    if (mainRefreshTimer) clearInterval(mainRefreshTimer);
    mainRefreshTimer = setInterval(() => {
        refreshStatus();
        loadAccountStats();
        loadPositions();
    }, 30000);
}

/* 单所版已移除总览（loadOverview / 合并图） */

/* ---------------- 单所：状态 / 权益 ---------------- */
async function refreshStatus() {
    try {
        const res = await authFetch('/api/status');
        const data = await res.json();
        el('statusDot').className = 'status-dot';
        setText('statusText', '运行中 · ' + (data.label || '欧易'));
        setText('symbolCount', data.enabled_symbols ? data.enabled_symbols.length : 0);

        // 止损残留阻断 / 止损状态异常：高风险状态在顶栏常驻显示，不能只靠一条钉钉
        const residues = data.stop_residues || [];
        const pill = el('residuePill');
        if (pill) {
            if (residues.length) {
                setText('residueText', '⚠ 止损残留阻断: ' + residues.join(', '));
                pill.classList.remove('hidden');
            } else {
                pill.classList.add('hidden');
            }
        }
        const anomalies = Object.keys(data.stop_anomalies || {});
        const aPill = el('anomalyPill');
        if (aPill) {
            if (anomalies.length) {
                setText('anomalyText', '⚠ 止损异常待人工: ' + anomalies.join(', '));
                aPill.classList.remove('hidden');
            } else {
                aPill.classList.add('hidden');
            }
        }

        const positions = data.open_positions || {};
        const entries = Object.entries(positions);
        let longCount = 0, shortCount = 0;
        entries.forEach(([_, pos]) => { if (pos.side === 'long') longCount++; else shortCount++; });
        setText('positionCount', entries.length);
        setText('longCountBadge', '多 ' + longCount);
        setText('shortCountBadge', '空 ' + shortCount);

        if (data.last_symbol_update) {
            const d = new Date(data.last_symbol_update);
            setText('lastSymbolUpdate', d.toLocaleDateString('zh-CN'));
            const diffDays = Math.floor((Date.now() - d) / 86400000);
            setText('lastSymbolUpdateDetail', diffDays === 0 ? '今天' : diffDays + ' 天前');
        }
    } catch (e) {
        el('statusDot').className = 'status-dot offline';
        setText('statusText', '离线');
    }
}

function setEquityNA() {
    ['ytdReturn', 'currentEquity', 'peakDrawdown', 'potentialDrawdown', 'daysSincePeak'].forEach(id => setText(id, '-'));
}

async function loadAccountStats() {
    try {
        const res = await authFetch('/api/account_stats');
        if (!res.ok) { setEquityNA(); return; }
        const d = await res.json();
        const ytd = el('ytdReturn');
        ytd.textContent = fmtPct(d.ytd_return);
        ytd.className = 'value ' + (d.ytd_return >= 0 ? 'green' : 'red');
        setText('totalReturnDetail', '累计 ' + fmtPct(d.total_return));
        setText('currentEquity', fmt(d.current_equity));
        setText('peakEquityDetail', '峰值 ' + fmt(d.peak_equity));
        const pd = el('peakDrawdown');
        pd.textContent = '-' + (d.peak_drawdown * 100).toFixed(2) + '%';
        pd.className = 'value red';
        setText('maxDrawdownDetail', '历史最大 -' + (d.max_drawdown * 100).toFixed(2) + '%');
        const pm = el('potentialDrawdown');
        pm.textContent = '-' + (d.potential_max_drawdown * 100).toFixed(2) + '%';
        pm.className = 'value orange';
        setText('potentialDetail', '最低权益 ' + fmt(d.worst_case_equity));
        setText('daysSincePeak', d.days_since_peak);
        setText('longestDrawdownDetail', '历史最长 ' + d.longest_drawdown_days + ' 天');
        // 品种池构成
        if (window._symbolsData) {
            setText('symbolPoolDetail', '双均线 ' + window._symbolsData.length);
        }
    } catch (e) { setEquityNA(); }
}

/* ---------------- 求索指数图（lightweight-charts） ---------------- */
function ensureKlineChart(containerId, tooltipId) {
    let ctx = _charts[containerId];
    if (ctx) return ctx;
    const container = el(containerId);
    if (!container || typeof LightweightCharts === 'undefined') return null;
    try {
        const chart = LightweightCharts.createChart(container, Object.assign({
            width: container.clientWidth, height: 380,
            handleScroll: true, handleScale: true,
        }, chartThemeOptions()));
        const series = chart.addCandlestickSeries({
            upColor: '#15a05a', downColor: '#e5484d',
            borderUpColor: '#15a05a', borderDownColor: '#e5484d',
            wickUpColor: '#15a05a', wickDownColor: '#e5484d',
        });
        ctx = { chart, series, container, tooltipId, klineByTime: {} };
        chart.subscribeCrosshairMove((p) => onKlineCrosshair(p, ctx));
        new ResizeObserver(() => {
            chart.applyOptions({ width: container.clientWidth });
            // 等浏览器完成本次布局后再适配视野，避免用旧宽度计算导致蜡烛挤在一侧
            requestAnimationFrame(() => chart.timeScale().fitContent());
        }).observe(container);
        _charts[containerId] = ctx;
        return ctx;
    } catch (e) {
        console.error('图表初始化失败', e);
        return null;
    }
}

function onKlineCrosshair(param, ctx) {
    const tip = el(ctx.tooltipId);
    if (!tip) return;
    if (!param.point || !param.time || !param.seriesData.has(ctx.series)) {
        tip.style.opacity = 0; return;
    }
    const d = param.seriesData.get(ctx.series);
    const dateStr = (typeof param.time === 'string') ? param.time
        : (param.time.year + '-' + String(param.time.month).padStart(2, '0') + '-' + String(param.time.day).padStart(2, '0'));
    const full = ctx.klineByTime[dateStr] || {};
    const prevClose = (full.prevClose != null) ? full.prevClose : d.open;
    const chg = d.close - prevClose;                              // 涨跌额（相对前一根收盘）
    const chgPct = prevClose ? (chg / prevClose) * 100 : 0;       // 涨幅
    const ampPct = prevClose ? ((d.high - d.low) / prevClose) * 100 : 0; // 振幅
    const col = chg >= 0 ? '#7ee2a8' : '#ff9b9b';
    const sign = chg >= 0 ? '+' : '';
    tip.innerHTML = `<div class="tt-date">${dateStr}</div>` +
        `<div class="tt-row"><span class="tt-k">开</span><span>${fmt(d.open)}</span></div>` +
        `<div class="tt-row"><span class="tt-k">高</span><span>${fmt(d.high)}</span></div>` +
        `<div class="tt-row"><span class="tt-k">低</span><span>${fmt(d.low)}</span></div>` +
        `<div class="tt-row"><span class="tt-k">收</span><span style="color:${col}">${fmt(d.close)}</span></div>` +
        `<div class="tt-row"><span class="tt-k">涨跌</span><span style="color:${col}">${sign}${fmt(chg)}</span></div>` +
        `<div class="tt-row"><span class="tt-k">涨幅</span><span style="color:${col}">${sign}${chgPct.toFixed(2)}%</span></div>` +
        `<div class="tt-row"><span class="tt-k">振幅</span><span style="color:#c7cedb">${ampPct.toFixed(2)}%</span></div>`;
    tip.style.opacity = 1;
    let x = param.point.x + 18, y = param.point.y + 12;
    if (x + 150 > ctx.container.clientWidth) x = param.point.x - 165;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
}

function renderKline(ctx, payload, summaryId) {
    const summary = el(summaryId);
    const candles = (payload && payload.candles) || [];
    if (!ctx) return;
    if (!candles.length) {
        ctx.series.setData([]);
        if (summary) summary.innerHTML = '<span class="muted">暂无求索指数数据，等待权益采样积累</span>';
        return;
    }
    const data = candles.map(c => ({ time: c.date, open: +c.open, high: +c.high, low: +c.low, close: +c.close }));
    // 缓存每根（含前收）供悬停计算涨跌/涨幅/振幅
    ctx.klineByTime = {};
    let prevClose = null;
    data.forEach(c => {
        c.prevClose = (prevClose == null) ? c.open : prevClose;
        ctx.klineByTime[c.time] = c;
        prevClose = c.close;
    });
    ctx.series.setData(data);
    ctx.chart.timeScale().fitContent();

    if (!summary) return;
    const base = payload.base_index || 1853;
    const latest = data[data.length - 1].close;
    let hi = -Infinity, lo = Infinity;
    data.forEach(c => { hi = Math.max(hi, c.high); lo = Math.min(lo, c.low); });
    const chgPct = (latest / base - 1) * 100;
    summary.innerHTML =
        `<div class="eks-item"><div class="eks-label">最新指数</div><div class="eks-val">${fmt(latest)}</div></div>` +
        `<div class="eks-item"><div class="eks-label">较基准</div><div class="eks-val ${chgPct>=0?'green':'red'}">${fmtPct(chgPct)}</div></div>` +
        `<div class="eks-item"><div class="eks-label">区间最高</div><div class="eks-val">${fmt(hi)}</div></div>` +
        `<div class="eks-item"><div class="eks-label">区间最低</div><div class="eks-val red">${fmt(lo)}</div></div>`;
}

async function loadEquityKline() {
    const ctx = ensureKlineChart('equityChart', 'equityTooltip');
    if (!ctx) { el('equityChart').innerHTML = '<div class="chart-empty">图表库加载失败（缺 static/lwc.js）</div>'; return; }
    try {
        const res = await authFetch('/api/equity_ohlc?days=' + equityKlineDays);
        renderKline(ctx, await res.json(), 'equityKlineSummary');
    } catch (e) {
        el('equityKlineSummary').innerHTML = '<span class="muted">求索指数加载失败</span>';
    }
}

function setEquityKlineRange(days) {
    equityKlineDays = days;
    document.querySelectorAll('#equityKlineToolbar .equity-kline-range-btn').forEach(b => {
        b.classList.toggle('active', +b.getAttribute('data-days') === days);
    });
    loadEquityKline();
}
window.setEquityKlineRange = setEquityKlineRange;

/* ---------------- 持仓 ---------------- */
async function loadPositions() {
    const box = el('positionsList');
    try {
        const res = await authFetch('/api/positions');
        const positions = await res.json();
        const entries = Object.entries(positions || {});
        _positionSymbols = entries.map(([s]) => s);
        if (!entries.length) { box.innerHTML = '<div class="empty">当前无持仓</div>'; return; }
        let totalPnl = 0;
        let totalNotional = 0;
        let totalStopRisk = 0;
        let rows = entries.map(([symbol, p]) => {
            const sideBadge = p.side === 'long' ? '<span class="badge badge-long">多</span>' : '<span class="badge badge-short">空</span>';
            const cur = p.current_price != null ? fmt(p.current_price, 4) : '-';
            const pnl = p.unrealized_pnl != null ? `<span class="${pnlClass(p.unrealized_pnl)}">${p.unrealized_pnl>=0?'+':''}${fmt(p.unrealized_pnl)}</span>` : '-';
            const days = p.holding_days != null ? p.holding_days + ' 天' : '-';
            const entry = Number(p.entry_price);
            const size = Number(p.position_size);
            const mark = p.current_price != null ? Number(p.current_price) : entry;
            const stop = Number(p.stop_loss_price);
            if (!isNaN(p.unrealized_pnl)) totalPnl += Number(p.unrealized_pnl);
            if (!isNaN(mark) && !isNaN(size)) totalNotional += Math.abs(mark * size);
            if (!isNaN(entry) && !isNaN(size) && !isNaN(stop)) {
                totalStopRisk += Math.max(0, (p.side === 'long' ? entry - stop : stop - entry) * size);
            }
            return `<tr>
                <td><span class="symbol-name">${symbol}</span></td>
                <td>${sideBadge}</td>
                <td class="number-cell">${fmt(p.entry_price, 4)}</td>
                <td class="number-cell">${cur}</td>
                <td class="number-cell">${pnl}</td>
                <td>${days}</td>
                <td class="center-cell"><button class="btn-close-pos" data-action="close" data-symbol="${symbol}" data-side="${p.side}" data-size="${p.position_size}">平仓</button></td>
            </tr>`;
        }).join('');
        const pnlSign = totalPnl >= 0 ? '+' : '';
        const summary = `<div class="position-summary">
            <div class="position-summary-item"><span class="position-summary-label">浮动盈亏</span><span class="position-summary-value ${totalPnl >= 0 ? 'green' : 'red'}">${pnlSign}${fmt(totalPnl)}U</span></div>
            <div class="position-summary-item"><span class="position-summary-label">名义价值</span><span class="position-summary-value">${fmt(totalNotional)}U</span></div>
            <div class="position-summary-item"><span class="position-summary-label">止损风险</span><span class="position-summary-value red">${fmt(totalStopRisk)}U</span></div>
        </div>`;
        box.innerHTML = `<div class="positions-table-wrap"><table class="data"><thead><tr>
            <th>交易对</th><th>方向</th><th class="number-cell">入场价</th><th class="number-cell">现价</th>
            <th class="number-cell">浮动盈亏</th><th>持仓</th><th class="center-cell">操作</th></tr></thead><tbody>${rows}</tbody></table></div>${summary}`;
    } catch (e) {
        box.innerHTML = '<div class="empty">持仓加载失败</div>';
    }
}

/* ---------------- 品种池 ---------------- */
async function loadSymbols() {
    const box = el('symbolsList');
    try {
        const res = await authFetch('/api/symbols');
        const symbols = await res.json();
        window._symbolsData = symbols;
        // 品种池构成直接在此更新，不依赖 loadAccountStats 的返回时序
        setText('symbolPoolDetail', symbols.length ? ('双均线 ' + symbols.length) : '');
        if (!symbols.length) { box.innerHTML = '<div class="empty">品种池为空，添加一个交易对</div>'; return; }
        let rows = symbols.map(s => {
            const stratBadge = '<span class="badge badge-ma">双均线</span>';
            const stateBadge = s.enabled ? '<span class="badge badge-on">启用</span>' : '<span class="badge badge-off">禁用</span>';
            const holding = s.has_open_position ? '<span class="badge badge-holding">持仓中</span>' : '';
            return `<tr>
                <td><span class="symbol-name">${s.name}</span>${holding}</td>
                <td>${stratBadge}</td>
                <td class="number-cell" data-action="risk" data-symbol="${s.name}" data-risk="${s.risk_per_trade}" style="cursor:pointer">${(s.risk_per_trade*100).toFixed(1)}%</td>
                <td><span style="cursor:pointer" data-action="toggle" data-symbol="${s.name}" data-enabled="${s.enabled?1:0}">${stateBadge}</span></td>
                <td class="center-cell"><button class="btn btn-sm btn-danger" data-action="del" data-symbol="${s.name}">删除</button></td>
            </tr>`;
        }).join('');
        box.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
            <th>交易对</th><th>策略</th><th class="number-cell">风险</th><th>状态</th><th class="center-cell">操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } catch (e) {
        box.innerHTML = '<div class="empty">品种池加载失败</div>';
    }
}

async function addSymbol() {
    const name = el('symbolName').value.trim().toUpperCase();
    if (!name) { showAlert('请输入交易对', 'error'); return; }
    const risk = parseFloat(el('riskPerTrade').value) / 100;
    const strategy = el('symbolStrategy').value;
    try {
        const res = await postJSON('/api/symbols', { name, risk_per_trade: risk, strategy });
        const data = await res.json();
        if (res.ok) { showAlert(data.message || '已添加', 'success'); el('symbolName').value = ''; loadSymbols(); refreshStatus(); }
        else showAlert(data.error || '添加失败', 'error');
    } catch (e) { showAlert('网络错误', 'error'); }
}

async function deleteSymbol(symbol) {
    if (!confirm(`确认从【欧易】删除交易对 ${symbol}？\n删除后将从品种池移除，不再新开仓；如已有持仓，系统会保留并核对现有止损，只管理到当前仓结束。`)) return;
    try {
        const res = await authFetch('/api/symbols/' + symbol, { method: 'DELETE' });
        const data = await res.json();
        if (res.ok) { showAlert('已删除', 'success'); loadSymbols(); refreshStatus(); }
        else showAlert(data.error || '删除失败', 'error');
    } catch (e) { showAlert('网络错误', 'error'); }
}

async function toggleSymbol(symbol, enabled) {
    try {
        const res = await authFetch('/api/symbols/' + symbol, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !enabled }) });
        if (res.ok) { loadSymbols(); refreshStatus(); } else showAlert('更新失败', 'error');
    } catch (e) { showAlert('网络错误', 'error'); }
}

async function updateSymbolRisk(symbol, currentRisk) {
    const v = prompt('输入新的风险度 (%)', (currentRisk * 100).toFixed(1));
    if (v == null) return;
    const risk = parseFloat(v) / 100;
    if (isNaN(risk) || risk <= 0) { showAlert('风险度无效', 'error'); return; }
    try {
        const res = await authFetch('/api/symbols/' + symbol, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ risk_per_trade: risk }) });
        if (res.ok) { showAlert('已更新', 'success'); loadSymbols(); } else showAlert('更新失败', 'error');
    } catch (e) { showAlert('网络错误', 'error'); }
}

/* ---------------- 即时开仓 / 平仓 ---------------- */
async function instantOpen() {
    const name = el('instantSymbolName').value.trim().toUpperCase();
    if (!name) { showAlert('请输入交易对', 'error'); return; }
    const risk = parseFloat(el('instantRiskPerTrade').value) / 100;
    const strategy = el('instantStrategy').value;
    const out = el('instantResult');
    out.className = 'result-line'; out.textContent = '检测信号并开仓中...';
    try {
        const res = await postJSON('/api/instant_open', { name, risk_per_trade: risk, strategy });
        const data = await res.json();
        if (res.ok) {
            out.className = 'result-line ok';
            out.textContent = data.message || '开仓成功';
            loadPositions(); loadSymbols(); refreshStatus();
        } else {
            out.className = 'result-line err';
            out.textContent = data.error || '开仓失败';
        }
    } catch (e) { out.className = 'result-line err'; out.textContent = '网络错误'; }
}

async function closePosition(symbol, side, size) {
    const dir = side === 'long' ? '做多' : (side === 'short' ? '做空' : (side || '-'));
    const input = prompt(`⚠️ 平仓确认（真实下单，不可撤销）\n交易所：欧易\n交易对：${symbol}\n方向：${dir}\n数量：${size != null ? size : '-'}\n\n确认无误请输入交易对名 “${symbol}” 以继续：`);
    if (input == null) return;
    if (input.trim().toUpperCase() !== symbol.toUpperCase()) { showAlert('输入与交易对不匹配，已取消平仓', 'error'); return; }
    try {
        const res = await postJSON('/api/close_position', { name: symbol });
        const data = await res.json();
        if (res.ok) { showAlert(symbol + ' 平仓成功', 'success'); loadPositions(); refreshStatus(); }
        else showAlert(data.error || '平仓失败', 'error');
    } catch (e) { showAlert('网络错误', 'error'); }
}

/* ---------------- 策略参数 / 资金同步 ---------------- */
async function loadStrategyParams() {
    try {
        const res = await authFetch('/api/strategy_params');
        const p = await res.json();
        el('paramMaShort').value = p.ma_short_period ?? '';
        el('paramMaLong').value = p.ma_long_period ?? '';
        el('paramMaStop').value = p.ma_stop_period ?? '';
        el('paramDefaultRisk').value = p.default_risk_per_trade != null ? (p.default_risk_per_trade * 100).toFixed(1) : '';
    } catch (e) {}
}

async function saveStrategyParams() {
    const out = el('strategyParamResult');
    const body = {
        ma_short_period: parseInt(el('paramMaShort').value),
        ma_long_period: parseInt(el('paramMaLong').value),
        ma_stop_period: parseInt(el('paramMaStop').value),
        default_risk_per_trade: parseFloat(el('paramDefaultRisk').value) / 100,
    };
    try {
        const res = await authFetch('/api/strategy_params', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await res.json();
        if (res.ok) { out.className = 'result-line ok'; out.textContent = '策略参数已保存'; }
        else { out.className = 'result-line err'; out.textContent = data.error || '保存失败'; }
    } catch (e) { out.className = 'result-line err'; out.textContent = '网络错误'; }
}

async function syncEquity() {
    const flowInput = el('syncFlowAmount');
    const raw = flowInput ? flowInput.value.trim() : '';
    const body = {};
    let tip = '锚定方式：最近指数值（须在入金/出金后 5 分钟内点击才准确）';
    if (raw !== '') {
        const v = parseFloat(raw);
        if (isNaN(v)) { showAlert('净变动金额无效', 'error'); return; }
        body.flow_amount = v;
        tip = `净变动 ${v >= 0 ? '+' : ''}${v} USDT，按变动前权益精确锚定（不受点击时间影响）`;
    }
    if (!confirm(`确认把当前权益设为新基准？（仅在入金/出金后使用）\n${tip}`)) return;
    const out = el('syncResult');
    out.className = 'result-line'; out.textContent = '同步中...';
    try {
        const res = await postJSON('/api/equity_sync', body);
        const data = await res.json();
        if (res.ok) {
            out.className = 'result-line ok'; out.textContent = data.message || '已同步';
            if (flowInput) flowInput.value = '';
            loadAccountStats(); loadEquityKline();
        }
        else { out.className = 'result-line err'; out.textContent = data.error || '同步失败'; }
    } catch (e) { out.className = 'result-line err'; out.textContent = '网络错误'; }
}

/* ---------------- 历史交易 / 日志 ---------------- */
async function loadTrades() {
    const box = el('tradesList');
    try {
        const [tr, sr] = await Promise.all([
            authFetch('/api/trades'),
            authFetch('/api/trades_summary'),
        ]);
        const trades = await tr.json();
        const summary = await sr.json();
        if (summary && summary.total) {
            setText('tradesSummary', `共 ${summary.total} 笔 · 胜率 ${summary.win_rate}% · 净盈亏 ${fmt(summary.total_pnl)}U · 盈亏比 ${summary.profit_factor ?? '-'}`);
        } else setText('tradesSummary', '暂无成交');
        if (!trades || !trades.length) { box.innerHTML = '<div class="empty">暂无历史交易</div>'; return; }
        const rows = trades.slice().reverse().map(t => {
            const side = t.side === 'long' ? '<span class="badge badge-long">多</span>' : '<span class="badge badge-short">空</span>';
            const closeTime = (t.close_time || '').replace('T', ' ').slice(0, 16);
            const pnl = `<span class="${pnlClass(t.pnl)}">${t.pnl>=0?'+':''}${fmt(t.pnl)}</span>`;
            return `<tr>
                <td>${closeTime}</td>
                <td><span class="symbol-name">${t.symbol}</span></td>
                <td>${side}</td>
                <td class="number-cell">${fmt(t.entry_price, 4)}</td>
                <td class="number-cell">${fmt(t.exit_price, 4)}</td>
                <td class="number-cell">${pnl}</td>
                <td class="number-cell">${fmtPct(t.pnl_percent)}</td>
            </tr>`;
        }).join('');
        box.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
            <th>平仓时间</th><th>交易对</th><th>方向</th><th class="number-cell">入场</th><th class="number-cell">出场</th>
            <th class="number-cell">净盈亏</th><th class="number-cell">收益率</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } catch (e) {
        box.innerHTML = '<div class="empty">历史交易加载失败</div>';
    }
}

async function loadLogs() {
    const panel = el('logsPanel');
    try {
        const res = await authFetch('/api/logs?lines=80');
        const data = await res.json();
        if (data.logs && data.logs.length) {
            panel.textContent = data.logs.join('');
        } else panel.textContent = '暂无日志';
    } catch (e) { panel.textContent = '日志加载失败'; }
}

/* ---------------- 事件绑定 / 初始化 ---------------- */
function bindEvents() {
    el('logoutBtn').addEventListener('click', logout);
    el('themeBtn').addEventListener('click', toggleTheme);
    el('addSymbolBtn').addEventListener('click', addSymbol);
    el('instantOpenBtn').addEventListener('click', instantOpen);
    el('saveParamsBtn').addEventListener('click', saveStrategyParams);
    el('syncEquityBtn').addEventListener('click', syncEquity);
    el('refreshLogsBtn').addEventListener('click', loadLogs);

    el('equityKlineToolbar').addEventListener('click', e => {
        const b = e.target.closest('.equity-kline-range-btn');
        if (b) setEquityKlineRange(+b.getAttribute('data-days'));
    });

    // 持仓 / 品种 表格事件委托
    document.addEventListener('click', e => {
        const t = e.target.closest('[data-action]');
        if (!t) return;
        const action = t.getAttribute('data-action');
        const symbol = t.getAttribute('data-symbol');
        if (action === 'close') closePosition(symbol, t.getAttribute('data-side'), t.getAttribute('data-size'));
        else if (action === 'del') deleteSymbol(symbol);
        else if (action === 'toggle') toggleSymbol(symbol, t.getAttribute('data-enabled') === '1');
        else if (action === 'risk') updateSymbolRisk(symbol, parseFloat(t.getAttribute('data-risk')));
    });
}

async function init() {
    bindEvents();
    syncThemeButton();
    try {
        const res = await authFetch('/api/check_auth');
        if (res.ok) { el('loginOverlay').style.display = 'none'; bootData(); }
        else { el('loginOverlay').style.display = 'flex'; }
    } catch (e) { el('loginOverlay').style.display = 'flex'; }
}

document.addEventListener('DOMContentLoaded', init);
