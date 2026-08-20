/* ═══════════════════════════════════════════════════════════════════════
   Portal Pi · Luxury Dashboard — app.js
   ═══════════════════════════════════════════════════════════════════════ */

// ─── AUTH LAYER ─────────────────────────────────────────────────────────
let _authMode = null;
let _authToken = null;

function authToken() { return _authToken; }
function authHeaders() {
    const t = authToken();
    if (t && t !== 'local-mode-no-auth') return { 'Authorization': `Bearer ${t}` };
    return {};
}

async function authFetch(url, opts = {}) {
    opts.headers = { ...opts.headers || {}, ...authHeaders() };
    let r = await fetch(url, opts);
    if (r.status === 401 && _authMode && _authMode.supabase) {
        const refreshed = await tryRefreshToken();
        if (refreshed) {
            opts.headers = { ...opts.headers || {}, ...authHeaders() };
            r = await fetch(url, opts);
        } else { showLogin(); throw new Error('Sesión expirada'); }
    }
    return r;
}

async function tryRefreshToken() {
    const rt = localStorage.getItem('pp_refresh_token');
    if (!rt) return false;
    try {
        const r = await fetch('/api/auth/refresh', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: rt })
        });
        if (r.ok) {
            const data = await r.json();
            _authToken = data.access_token;
            localStorage.setItem('pp_access_token', _authToken);
            if (data.refresh_token) localStorage.setItem('pp_refresh_token', data.refresh_token);
            return true;
        }
    } catch {}
    return false;
}

async function checkAuthMode() {
    try { const r = await fetch('/api/auth/mode'); _authMode = await r.json(); }
    catch { _authMode = { supabase: false, auth_required: false }; }
    _authToken = localStorage.getItem('pp_access_token') || null;
    if (!_authMode.supabase) { enterDashboard(); return; }
    if (_authToken && _authToken !== 'local-mode-no-auth') {
        try {
            const r = await authFetch('/api/auth/me');
            if (r.ok) { enterDashboard(await r.json()); return; }
        } catch {}
    }
    showLogin();
}

function showLogin() {
    $('login-page').style.display = 'flex';
    $('dashboard-app').style.display = 'none';
    $('local-mode-hint').style.display = 'none';
}

function enterDashboard(user) {
    $('login-page').style.display = 'none';
    $('dashboard-app').style.display = '';
    if (_authMode && !_authMode.supabase) {
        $('local-mode-hint').style.display = '';
        $('user-badge').style.display = 'none';
    } else if (user) {
        $('user-badge').style.display = 'flex';
        $('user-email').textContent = user.email || '';
    }
    initDashboard();
}

function switchLoginTab(tab) {
    $('login-tab-btn').classList.toggle('active', tab === 'login');
    $('register-tab-btn').classList.toggle('active', tab === 'register');
    $('login-form').style.display = tab === 'login' ? '' : 'none';
    $('register-form').style.display = tab === 'register' ? '' : 'none';
    $('login-error').style.display = 'none';
}

async function handleLogin(e) {
    e.preventDefault();
    const isRegister = e.target.id === 'register-form';
    const email = isRegister ? $('register-email').value : $('login-email').value;
    const password = isRegister ? $('register-password').value : $('login-password').value;
    const displayName = isRegister ? $('register-display').value : '';
    const endpoint = isRegister ? '/api/auth/register' : '/api/auth/login';
    const body = isRegister ? { email, password, display_name: displayName } : { email, password };
    try {
        const r = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await r.json();
        if (!r.ok) { $('login-error').textContent = data.detail || 'Error'; $('login-error').style.display = ''; return; }
        _authToken = data.access_token;
        localStorage.setItem('pp_access_token', _authToken);
        if (data.refresh_token) localStorage.setItem('pp_refresh_token', data.refresh_token);
        enterDashboard(data.user || { email });
    } catch { $('login-error').textContent = 'Error de conexión'; $('login-error').style.display = ''; }
}

function handleLogout() {
    _authToken = null;
    localStorage.removeItem('pp_access_token');
    localStorage.removeItem('pp_refresh_token');
    showLogin();
}

// ─── API LAYER ──────────────────────────────────────────────────────────
const API = {
    async get(path) {
        const r = await authFetch('/api' + path);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        try { return JSON.parse(text); } catch { throw new Error(`No JSON: ${text.substring(0, 200)}`); }
    },
    async post(path, body = {}) {
        const r = await authFetch('/api' + path, {
            method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        try { return JSON.parse(text); } catch { throw new Error(`No JSON: ${text.substring(0, 200)}`); }
    },
    async del(path) {
        const r = await authFetch('/api' + path, { method: 'DELETE', headers: authHeaders() });
        if (r.status === 204) return { status: 'ok' };
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        try { return JSON.parse(text); } catch { throw new Error(`No JSON: ${text.substring(0, 200)}`); }
    }
};

// ─── UTILITIES ──────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
function showToast(msg, type = '') {
    const t = $('toast');
    t.textContent = msg; t.className = 'toast show ' + type;
    setTimeout(() => t.className = 'toast', 3500);
}
function escHtml(s) { if (s == null) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function escAttr(s) { return escHtml(s).replace(/\\/g,'\\\\'); }
function truncate(s, n = 80) { if (!s) return ''; return s.length > n ? s.slice(0, n) + '…' : s; }
function formatDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'short' }); }
    catch { return iso; }
}
function tagHtml(text, cls = 'tag-blue') { return `<span class="tag ${cls}">${escHtml(text)}</span>`; }
function confidenceTag(val) {
    if (val == null) return '';
    const n = Number(val);
    const cls = n >= 0.8 ? 'tag-green' : n >= 0.5 ? 'tag-orange' : 'tag-red';
    return tagHtml(n.toFixed(2), cls);
}

// ─── TAB SWITCHING ─────────────────────────────────────────────────────
let currentTab = 'news';

function clearAllPollers() {
    if (_pipelinePolling) { clearInterval(_pipelinePolling); _pipelinePolling = null; }
    if (_maPipelinePolling) { clearInterval(_maPipelinePolling); _maPipelinePolling = null; }
    if (_orchPipelinePolling) { clearInterval(_orchPipelinePolling); _orchPipelinePolling = null; }
    if (_ingestPolling) { clearInterval(_ingestPolling); _ingestPolling = null; }
    if (_schedulerPolling) { clearInterval(_schedulerPolling); _schedulerPolling = null; }
}

function switchTab(tab) {
    if (currentTab !== tab) {
        if (_ingestPolling) { clearInterval(_ingestPolling); _ingestPolling = null; }
        if (_schedulerPolling) { clearInterval(_schedulerPolling); _schedulerPolling = null; }
    }
    currentTab = tab;
    document.querySelectorAll('.nav-item').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
    loadTab(tab);
}

// ─── TABLE BUILDER ──────────────────────────────────────────────────────
function buildTable(columns, rows, opts = {}) {
    if (!rows || rows.length === 0) return `<div class="empty">${opts.empty || 'Sin datos'}</div>`;
    let html = '<div class="data-table-wrap"><table><thead><tr>';
    columns.forEach(([, label]) => html += `<th>${escHtml(label)}</th>`);
    html += '</tr></thead><tbody>';
    rows.forEach(row => {
        html += '<tr>';
        columns.forEach(([key]) => {
            let val = row[key];
            if (opts.render && opts.render[key]) val = opts.render[key](val, row);
            else val = escHtml(truncate(val));
            html += `<td${opts.fullKeys && opts.fullKeys.includes(key) ? ' class="full"' : ''}>${val}</td>`;
        });
        html += '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
}

// ─── COLLAPSIBLE ────────────────────────────────────────────────────────
function toggleCollapsible(id) {
    const el = $(id);
    if (!el) return;
    const body = el.querySelector('.collapsible-body');
    const icon = el.querySelector('.collapsible-icon');
    const isOpen = el.classList.contains('open');
    if (isOpen) {
        body.style.display = 'none';
        if (icon) icon.textContent = '▸';
        el.classList.remove('open');
    } else {
        body.style.display = '';
        if (icon) icon.textContent = '▾';
        el.classList.add('open');
    }
}

// ─── CHAT TOGGLE ────────────────────────────────────────────────────────
function toggleChat() {
    const panel = $('chat-panel');
    const btn = $('chat-toggle');
    panel.classList.toggle('collapsed');
    btn.textContent = panel.classList.contains('collapsed') ? '▸' : '◂';
}

// ─── REFRESH / TOPBAR ───────────────────────────────────────────────────
async function refreshAll() {
    try {
        const status = await API.get('/status');
        updateTopbar(status);
        loadTab(currentTab);
        showToast('Datos actualizados', 'success');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function updateTopbar(status) {
    const pill = $('status-pill');
    pill.textContent = '● ' + (status.global_status || 'UNKNOWN');
    pill.className = 'status-pill ' + (status.global_status === 'COMPLETED' ? 'ok' : status.global_status === 'ERROR' ? 'err' : '');
    $('stage-badge').textContent = status.pipeline_stage || 'IDLE';
    const llmBadge = $('llm-badge');
    if (llmBadge) {
        const llm = status.llm || {};
        const active = llm.active_provider;
        const provs = llm.providers || {};
        const anyKey = Object.values(provs).some(p => p.has_key);
        if (active && provs[active]) {
            llmBadge.textContent = '🤖 ' + provs[active].name;
            llmBadge.className = 'llm-badge';
        } else if (anyKey) {
            llmBadge.textContent = '🤖 Sin test';
            llmBadge.className = 'llm-badge';
        } else {
            llmBadge.textContent = '🤖 Sin LLM';
            llmBadge.className = 'llm-badge no-llm';
        }
    }
}

// ─── INIT ───────────────────────────────────────────────────────────────
let _dashboardInitialized = false;

async function initDashboard() {
    if (_dashboardInitialized) return;
    _dashboardInitialized = true;
    try {
        const status = await API.get('/status');
        updateTopbar(status);
    } catch { showToast('No se pudo conectar al servidor', 'error'); }
    loadTab('news');
    connectChatWs();
}

document.addEventListener('DOMContentLoaded', () => checkAuthMode());

// ═══════════════════════════════════════════════════════════════════════
// TAB LOADERS
// ═══════════════════════════════════════════════════════════════════════

function loadTab(tab) {
    switch(tab) {
        case 'news': loadNews(); break;
        case 'pipeline': loadPipeline(); break;
        case 'data': switchDataSub('entities'); break;
        case 'reports': loadReports(); break;
        case 'config': loadConfig(); break;
    }
}

// ─── TAB: NEWS ──────────────────────────────────────────────────────────
let _rawNewsFilterAccessible = true;
let _rawNewsCache = null;

async function loadNews() {
    try {
        const articles = await API.get('/raw_news?limit=100');
        _rawNewsCache = articles;
        renderNews(articles);
    } catch { $('news-grid').innerHTML = '<div class="empty">Error cargando artículos</div>'; }
}

// Event delegation for clickable elements
document.addEventListener('click', (e) => {
    // News cards — click on card (not on a link) opens detail
    const card = e.target.closest('.news-card');
    if (card && !e.target.closest('a')) {
        const filename = card.dataset.filename;
        if (filename) { showRawDetail(filename); return; }
    }
    // Report cards
    const repCard = e.target.closest('.report-card');
    if (repCard) {
        const filename = repCard.dataset.filename;
        if (filename) { openReport(filename); return; }
    }
    // Feed toggles
    const feedToggle = e.target.closest('.feed-toggle');
    if (feedToggle) {
        const name = feedToggle.dataset.feedName;
        if (name) { toggleFeed(name); return; }
    }
    // Credential save
    const credSave = e.target.closest('.cred-save');
    if (credSave) { saveCred(credSave.dataset.provider); return; }
    // Credential delete
    const credDel = e.target.closest('.cred-delete');
    if (credDel) { deleteCred(credDel.dataset.provider); return; }
});

function renderNews(articles) {
    // Stats
    const accessible = articles.filter(a => a.link_type === 'direct');
    const indirect = articles.filter(a => a.link_type === 'indirect');
    const noLink = articles.filter(a => a.link_type === 'none' || !a.link_type);

    let statsHtml = `<span class="news-stat green">📰 ${articles.length} artículos</span>`;
    if (accessible.length) statsHtml += `<span class="news-stat green">✅ ${accessible.length} con fuente</span>`;
    if (indirect.length) statsHtml += `<span class="news-stat orange">⚠️ ${indirect.length} indirectos</span>`;
    if (noLink.length) statsHtml += `<span class="news-stat red">🚫 ${noLink.length} sin enlace</span>`;
    $('news-stats').innerHTML = statsHtml;

    const display = _rawNewsFilterAccessible ? accessible : [...accessible, ...indirect, ...noLink];

    if (display.length === 0) {
        $('news-grid').innerHTML = '<div class="empty">No hay artículos. Pulsa ⬇ Ingestar para descargar noticias.</div>';
        return;
    }

    let html = '';
    for (const art of display) {
        const title = art.title || art.filename.replace(/\.txt$/, '').replace(/_/g, ' ');
        const body = art.body || '';
        const linkType = art.link_type || 'none';
        const effectiveLink = art.effective_link || art.link || '';

        // Title is a link to the source when available
        let titleHtml;
        if (effectiveLink && linkType === 'direct') {
            titleHtml = `<a href="${escHtml(effectiveLink)}" target="_blank" rel="noopener noreferrer" class="news-card-title-link">${escHtml(truncate(title, 120))}</a>`;
        } else {
            titleHtml = `<div class="news-card-title">${escHtml(truncate(title, 120))}</div>`;
        }

        // Footer link
        let linkHtml;
        if (linkType === 'direct') linkHtml = `<a href="${escHtml(effectiveLink)}" target="_blank" rel="noopener noreferrer" class="news-card-link">🔍 Abrir fuente</a>`;
        else if (linkType === 'indirect') linkHtml = `<a href="${escHtml(effectiveLink)}" target="_blank" rel="noopener noreferrer" class="news-card-link indirect">⚠️ Indirecto</a>`;
        else linkHtml = `<span class="news-card-link none">🚫 Sin enlace</span>`;

        html += `<div class="news-card" data-filename="${escAttr(art.filename)}">
            <div class="news-card-source">${escHtml(art.source || 'Desconocido')}</div>
            ${titleHtml}
            ${body ? `<div class="news-card-body">${escHtml(truncate(body, 180))}</div>` : ''}
            <div class="news-card-footer">
                ${art.category ? tagHtml(art.category, 'tag-purple') : ''}
                ${art.published ? `<span class="news-card-meta">${escHtml(formatDate(art.published))}</span>` : ''}
                ${linkHtml}
            </div>
        </div>`;
    }
    $('news-grid').innerHTML = html;
}

function toggleRawFilter() {
    const cb = $('filter-accessible');
    if (cb) _rawNewsFilterAccessible = cb.checked;
    if (_rawNewsCache) renderNews(_rawNewsCache);
}

async function showRawDetail(filename) {
    try {
        const result = await API.get('/raw_news/' + encodeURIComponent(filename));
        let html = '';
        const title = result.title || filename.replace(/\.txt$/, '').replace(/_/g, ' ');
        $('raw-detail-title').textContent = title;

        html += '<div class="raw-detail-actions">';
        if (result.link_type === 'direct' && result.effective_link) {
            html += `<a href="${escHtml(result.effective_link)}" target="_blank" rel="noopener noreferrer" class="btn-accent btn-sm raw-detail-link-btn">🔍 Abrir fuente</a>`;
        } else if (result.link_type === 'indirect' && result.effective_link) {
            html += `<a href="${escHtml(result.effective_link)}" target="_blank" rel="noopener noreferrer" class="btn-outline btn-sm">⚠️ Enlace indirecto</a>`;
        } else {
            html += `<span class="news-card-link none">🚫 Sin enlace</span>`;
        }
        html += '</div>';

        if (result.body) {
            html += '<div class="raw-detail-body"><div class="raw-detail-body-label">📝 Resumen</div>';
            html += `<div class="raw-detail-body-text">${escHtml(result.body)}</div></div>`;
        }
        if (result.content) {
            html += '<details class="raw-detail-raw-section"><summary>📄 Ver contenido raw</summary>';
            html += `<pre class="raw-detail-raw-content">${escHtml(result.content)}</pre></details>`;
        }

        $('raw-detail-content').innerHTML = html;
        $('raw-detail').style.display = 'block';
        $('news-grid').style.display = 'none';
        $('news-stats').style.display = 'none';
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function closeRawDetail() {
    $('raw-detail').style.display = 'none';
    $('news-grid').style.display = '';
    $('news-stats').style.display = '';
}

// ─── INGEST ────────────────────────────────────────────────────────────
let _ingestPolling = null;

async function runIngest(feedName = null) {
    if (_pipelineRunning) { showToast('Pipeline en curso, espera...', 'success'); return; }
    _setPipelineButtons(true);
    try {
        const body = feedName ? { feed_name: feedName } : {};
        const result = await API.post('/ingest', body);
        if (result.status === 'started' || result.status === 'already_running') {
            showToast('Ingesta iniciada...', 'success');
            pollIngestStatus();
        } else { showToast('Error: ' + (result.message || ''), 'error'); }
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function pollIngestStatus() {
    if (_ingestPolling) clearInterval(_ingestPolling);
    _ingestPolling = setInterval(async () => {
        try {
            const s = await API.get('/ingest/status');
            if (!s.running) {
                clearInterval(_ingestPolling); _ingestPolling = null;
                _setPipelineButtons(false);
                if (s.error) showToast('Ingesta falló: ' + s.error, 'error');
                else if (s.results) {
                    const total_new = s.results.reduce((a, r) => a + (r.articles_new || 0), 0);
                    showToast(`Ingesta OK: ${total_new} nuevos`, 'success');
                }
                if (currentTab === 'news') loadNews();
                updateTopbarFromApi();
            }
        } catch { clearInterval(_ingestPolling); _ingestPolling = null; _setPipelineButtons(false); }
    }, 1500);
}

async function updateTopbarFromApi() {
    try { const status = await API.get('/status'); updateTopbar(status); } catch {}
}

// ═══════════════════════════════════════════════════════════════════════
// TAB: PIPELINE
// ═══════════════════════════════════════════════════════════════════════

let _pipelinePolling = null;
let _maPipelinePolling = null;
let _orchPipelinePolling = null;

async function loadPipeline() {
    // LLM info
    try {
        const llm = await API.get('/llm/config');
        if (llm.status === 'ok') llmData = llm.config;
    } catch {}

    // Stats
    try {
        const status = await API.get('/status');
        const db = status.db_counts || {};
        $('pipeline-hero-stats').innerHTML = `
            <div class="stat-card"><div class="stat-val" style="color:var(--accent)">${db.raw_news || 0}</div><div class="stat-label">Artículos</div></div>
            <div class="stat-card"><div class="stat-val" style="color:var(--purple)">${db.entities || 0}</div><div class="stat-label">Entidades</div></div>
            <div class="stat-card"><div class="stat-val">${db.syntheses || 0}</div><div class="stat-label">Síntesis</div></div>
        `;
    } catch {}

    // Pipeline status
    try {
        const ps = await API.get('/pipeline/status');
        if (ps.running) {
            highlightPipelineStep(ps.step);
            $('pipeline-bar').style.display = 'block';
            if (!_pipelinePolling) pollPipelineStatus();
        } else { clearPipelineSteps(); _setPipelineButtons(false); }
    } catch {}

    await loadSchedulerStatus();
}

function highlightPipelineStep(step) {
    const stepMap = { 'ingest': 'ps-ingest', 'extract_entities': 'ps-extract', 'classify_topic': 'ps-classify', 'synthesize_news': 'ps-synthesize', 'generate_action_items': 'ps-actions' };
    clearPipelineSteps();
    const el = stepMap[step];
    if (el) { const elem = $(el); if (elem) elem.classList.add('active'); }
}

function clearPipelineSteps() { document.querySelectorAll('.pf-step').forEach(s => s.classList.remove('active', 'done', 'error')); }

let _pipelineRunning = false;

function _setPipelineButtons(disabled) {
    _pipelineRunning = disabled;
    ['btn-pipeline', 'btn-ingest'].forEach(id => { const el = $(id); if (el) el.disabled = disabled; });
}

async function runPipeline() {
    if (_pipelineRunning) { showToast('Pipeline ya en curso', 'success'); return; }
    _setPipelineButtons(true);
    try {
        const result = await API.post('/pipeline/run');
        if (result.status === 'started') {
            showToast('Pipeline iniciado...', 'success');
            $('pipeline-bar').style.display = 'block';
            $('pipeline-label').textContent = 'Ejecutando pipeline...';
            $('pipeline-fill').style.width = '10%';
            pollPipelineStatus();
        } else if (result.status === 'already_running') { pollPipelineStatus(); }
        else { showToast('Error: ' + (result.message || ''), 'error'); _setPipelineButtons(false); }
    } catch (e) { showToast('Error: ' + e.message, 'error'); _setPipelineButtons(false); }
}

function pollPipelineStatus() {
    if (_pipelinePolling) clearInterval(_pipelinePolling);
    _pipelinePolling = setInterval(async () => {
        try {
            const ps = await API.get('/pipeline/status');
            const progressMap = { 'starting': 10, 'extract_entities': 25, 'classify_topic': 50, 'synthesize_news': 75, 'generate_action_items': 90, 'done': 100 };
            const pct = progressMap[ps.step] || 30;
            $('pipeline-fill').style.width = pct + '%';
            $('pipeline-label').textContent = ps.step ? 'Paso: ' + ps.step.replace(/_/g, ' ') : 'Procesando...';
            highlightPipelineStep(ps.step);
            if (!ps.running) {
                clearInterval(_pipelinePolling); _pipelinePolling = null;
                $('pipeline-fill').style.width = '100%';
                if (ps.results) {
                    ps.results.forEach(r => {
                        const stepMap = { 'extract_entities': 'ps-extract', 'classify_topic': 'ps-classify', 'synthesize_news': 'ps-synthesize', 'generate_action_items': 'ps-actions' };
                        const el = stepMap[r.step]; if (el) { const elem = $(el); if (elem) elem.classList.add(r.status === 'ok' ? 'done' : 'error'); }
                    });
                    const okCount = ps.results.filter(r => r.status === 'ok').length;
                    showToast(`Pipeline: ${okCount}/${ps.results.length} pasos OK`, 'success');
                } else if (ps.error) showToast('Pipeline falló: ' + ps.error, 'error');
                setTimeout(() => { $('pipeline-bar').style.display = 'none'; }, 3000);
                _setPipelineButtons(false); updateTopbarFromApi(); loadPipeline();
            }
        } catch { clearInterval(_pipelinePolling); _pipelinePolling = null; _setPipelineButtons(false); }
    }, 2000);
}

// ─── Multi-Agent ────────────────────────────────────────────────────────
async function runMultiAgent() {
    if (_pipelineRunning) { showToast('Pipeline en curso', 'success'); return; }
    _setPipelineButtons(true);
    try {
        const result = await API.post('/pipeline/multi-agent');
        if (result.status === 'started') {
            showToast('Multi-agente iniciado...', 'success');
            $('pipeline-bar').style.display = 'block';
            $('pipeline-label').textContent = 'Multi-agente...';
            $('pipeline-fill').style.width = '10%';
            pollMultiAgent();
        } else if (result.status === 'already_running') pollMultiAgent();
        else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function pollMultiAgent() {
    if (_maPipelinePolling) clearInterval(_maPipelinePolling);
    _maPipelinePolling = setInterval(async () => {
        try {
            const ps = await API.get('/pipeline/multi-agent/status');
            const pct = { 'extractor': 25, 'analyst': 50, 'critic': 75, 'synthesizer': 90, 'done': 100 }[ps.step] || 30;
            $('pipeline-fill').style.width = pct + '%';
            $('pipeline-label').textContent = ps.step ? 'Multi-agente: ' + ps.step : 'Procesando...';
            if (!ps.running) {
                clearInterval(_maPipelinePolling); _maPipelinePolling = null;
                $('pipeline-fill').style.width = '100%';
                if (ps.results) {
                    const steps = Object.keys(ps.results).filter(k => !['summary','log','timeline','timeline_error'].includes(k));
                    const okCount = steps.filter(k => ps.results[k]?.status === 'ok').length;
                    showToast(`Multi-agente: ${okCount}/${steps.length} OK`, 'success');
                } else if (ps.error) showToast('Multi-agente falló: ' + ps.error, 'error');
                setTimeout(() => { $('pipeline-bar').style.display = 'none'; }, 3000);
                _setPipelineButtons(false); updateTopbarFromApi(); loadPipeline();
            }
        } catch { clearInterval(_maPipelinePolling); _maPipelinePolling = null; _setPipelineButtons(false); }
    }, 3000);
}

// ─── Orchestrated ────────────────────────────────────────────────────────
async function runOrchestrated() {
    if (_pipelineRunning) { showToast('Pipeline en curso', 'success'); return; }
    _setPipelineButtons(true);
    try {
        const result = await API.post('/pipeline/orchestrated');
        if (result.status === 'started') {
            showToast('Orquestado iniciado', 'success');
            $('pipeline-bar').style.display = 'block';
            $('pipeline-label').textContent = 'Orquestado...';
            $('pipeline-fill').style.width = '10%';
            pollOrchestrated();
        } else if (result.status === 'already_running') pollOrchestrated();
        else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function pollOrchestrated() {
    if (_orchPipelinePolling) clearInterval(_orchPipelinePolling);
    _orchPipelinePolling = setInterval(async () => {
        try {
            const ps = await API.get('/pipeline/orchestrated/status');
            const pct = { 'preparing': 15, 'planning': 30, 'researcher': 50, 'critic': 70, 'judge': 85, 'saving': 95, 'done': 100 }[ps.step] || 40;
            $('pipeline-fill').style.width = pct + '%';
            $('pipeline-label').textContent = ps.step ? 'Orquestado: ' + ps.step.replace(/_/g, ' ') : 'Procesando...';
            if (!ps.running) {
                clearInterval(_orchPipelinePolling); _orchPipelinePolling = null;
                $('pipeline-fill').style.width = '100%';
                if (ps.results) {
                    const r = ps.results;
                    const icon = r.status === 'accepted' ? '✅' : r.status === 'rejected' ? '❌' : '⚠️';
                    const score = ((r.quality?.score || 0) * 100).toFixed(0);
                    showToast(`${icon} Orquestado ${r.status} — calidad: ${score}%`, r.status === 'accepted' ? 'success' : 'error');
                } else if (ps.error) showToast('Orquestado falló: ' + ps.error, 'error');
                setTimeout(() => { $('pipeline-bar').style.display = 'none'; }, 4000);
                _setPipelineButtons(false); updateTopbarFromApi(); loadPipeline();
            }
        } catch { clearInterval(_orchPipelinePolling); _orchPipelinePolling = null; _setPipelineButtons(false); }
    }, 3000);
}

// ─── LLM Test ───────────────────────────────────────────────────────────
async function testLLM() {
    showToast('Testeando proveedores...', 'success');
    try {
        const result = await API.post('/llm/test');
        if (result.status === 'ok' && result.providers) {
            const ok = result.providers.filter(p => p.status === 'ok').length;
            showToast(`${ok} OK de ${result.providers.length}`, ok > 0 ? 'success' : 'error');
            loadPipeline();
        }
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

// ─── SCHEDULER ──────────────────────────────────────────────────────────
let _schedulerPolling = null;

async function loadSchedulerStatus() {
    try {
        const s = await API.get('/scheduler/status');
        $('sched-status')?.replaceWith?.(null); // old compat
        const statsEl = $('scheduler-stats');
        if (!statsEl) return;
        const running = s.running;
        statsEl.innerHTML = `
            <div class="stat-card"><div class="stat-val" style="color:${running ? 'var(--green)' : 'var(--text-dim)'}">${running ? '● ON' : '○ OFF'}</div><div class="stat-label">Estado</div></div>
            <div class="stat-card"><div class="stat-val">${s.ingest_interval_min || 30}</div><div class="stat-label">Min intervalo</div></div>
            <div class="stat-card"><div class="stat-val">${s.total_runs || 0}</div><div class="stat-label">Ejecuciones</div></div>
            <div class="stat-card"><div class="stat-val" style="font-size:14px">${s.next_run_at ? formatDate(s.next_run_at) : '—'}</div><div class="stat-label">Próxima</div></div>
        `;
        $('sched-interval-input').value = s.ingest_interval_min || 30;
        $('sched-auto-pipeline').checked = s.auto_pipeline !== false;
        $('sched-auto-start').checked = s.enabled || false;
        $('btn-sched-start').style.display = running ? 'none' : 'inline-block';
        $('btn-sched-stop').style.display = running ? 'inline-block' : 'none';

        if (s.recent_runs && s.recent_runs.length > 0) {
            $('sched-history').innerHTML = buildTable(
                [['started_at','Inicio'],['status','Estado'],['ingest_result','Ingesta'],['pipeline_result','Pipeline']],
                s.recent_runs.slice().reverse(),
                { render: {
                    status: v => tagHtml(v, v === 'ok' ? 'tag-green' : v === 'error' ? 'tag-red' : 'tag-orange'),
                    ingest_result: v => v ? tagHtml(`${v.total_new || 0} nuevos`, 'tag-blue') : '—',
                    pipeline_result: v => v ? (v.status === 'ok' ? tagHtml('✓ OK', 'tag-green') : tagHtml('✗ Error', 'tag-red')) : '—',
                    started_at: v => formatDate(v)
                }}
            );
        } else { $('sched-history').innerHTML = '<div class="empty">Sin ejecuciones</div>'; }
    } catch {}
}

async function schedulerStart() {
    const intervalMin = parseInt($('sched-interval-input').value) || 30;
    const autoPipeline = $('sched-auto-pipeline').checked;
    const autoStart = $('sched-auto-start').checked;
    try {
        await API.post('/scheduler/settings', { ingest_interval_min: intervalMin, auto_pipeline: autoPipeline, enabled: autoStart });
    } catch (e) { showToast('Error guardando config', 'error'); return; }
    try {
        const result = await API.post('/scheduler/start');
        if (result.status === 'started' || result.status === 'already_running') {
            showToast('Scheduler iniciado — cada ' + intervalMin + ' min', 'success');
            pollScheduler();
        } else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

async function schedulerStop() {
    try { await API.post('/scheduler/stop'); showToast('Scheduler detenido', 'success'); loadSchedulerStatus(); if (_schedulerPolling) { clearInterval(_schedulerPolling); _schedulerPolling = null; } }
    catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function pollScheduler() {
    if (_schedulerPolling) clearInterval(_schedulerPolling);
    _schedulerPolling = setInterval(async () => { await loadSchedulerStatus(); }, 5000);
}

// ═══════════════════════════════════════════════════════════════════════
// TAB: DATA
// ═══════════════════════════════════════════════════════════════════════

function switchDataSub(sub) {
    document.querySelectorAll('#tab-data .sub-tab').forEach(b => b.classList.toggle('active', b.dataset.sub === sub));
    loadDataSub(sub);
}

const DATA_SUBTABS = {
    entities: { cols: [['type','Tipo'],['name','Nombre'],['confidence','Confianza'],['source_file','Fuente'],['created_at','Fecha']], api: '/entities?limit=100', empty: 'No hay entidades. Ejecuta el pipeline.' },
    relations: { cols: [['subject','Sujeto'],['predicate','Relación'],['object','Objeto'],['source_file','Fuente'],['created_at','Fecha']], api: '/relations?limit=100', empty: 'No hay relaciones. Ejecuta el pipeline.' },
    syntheses: { cols: [['executive_summary','Resumen'],['priority','Prioridad'],['created_at','Fecha']], api: '/syntheses?limit=50', empty: 'No hay síntesis. Ejecuta el pipeline.' },
    classifications: { cols: [['primary_category','Categoría'],['secondary_tags','Tags'],['justification','Justificación'],['created_at','Fecha']], api: '/classifications?limit=100', empty: 'No hay clasificaciones.' },
    actions: { cols: [['description','Descripción'],['priority','Prioridad'],['owner','Owner'],['created_at','Fecha']], api: '/action_items?limit=100', empty: 'No hay acciones.' }
};

const typeEmoji = { 'PERSON':'👤','ORGANIZATION':'🏢','LOCATION':'📍','TECHNOLOGY':'💻','EVENT':'📅','CONCEPT':'💡','NEWS_ITEM':'📰' };

async function loadDataSub(sub) {
    const cfg = DATA_SUBTABS[sub];
    if (!cfg) return;
    try {
        const data = await API.get(cfg.api);
        if (!data || data.length === 0) { $('data-content').innerHTML = `<div class="empty">${cfg.empty}</div>`; return; }

        if (sub === 'entities') {
            $('data-content').innerHTML = buildTable(
                [['type','Tipo'],['name','Nombre'],['confidence','Confianza'],['source_file','Fuente'],['created_at','Fecha']],
                data,
                { render: { type: v => `${typeEmoji[v] || '📌'} ${tagHtml(v || 'OTRO', 'tag-blue')}`, name: v => `<strong>${escHtml(v)}</strong>`, confidence: v => confidenceTag(v), source_file: v => escHtml(truncate(v, 50)), created_at: v => formatDate(v) }, empty: cfg.empty }
            );
        } else if (sub === 'relations') {
            $('data-content').innerHTML = buildTable(
                [['subject','Sujeto'],['predicate','Relación'],['object','Objeto'],['source_file','Fuente'],['created_at','Fecha']],
                data,
                { render: { subject: v => `<strong>${escHtml(v)}</strong>`, predicate: v => tagHtml(v, 'tag-purple'), object: v => `<strong>${escHtml(v)}</strong>`, source_file: v => escHtml(truncate(v, 50)), created_at: v => formatDate(v) }, empty: cfg.empty }
            );
        } else if (sub === 'syntheses') {
            let html = '<div class="synthesis-cards">';
            for (const s of data) {
                const prioEmoji = s.priority === 'ALTA' ? '🔴' : s.priority === 'MEDIA' ? '🟡' : '🟢';
                html += `<div class="synthesis-card"><div class="synthesis-priority">${prioEmoji} ${escHtml(s.priority || '—')}</div><div class="synthesis-text">${escHtml(s.executive_summary || 'Sin resumen')}</div>`;
                try { const trends = typeof s.trends === 'string' ? JSON.parse(s.trends) : (s.trends || []); if (trends.length) html += '<div class="synthesis-trends">' + trends.map(t => tagHtml(t, 'tag-purple')).join(' ') + '</div>'; } catch {}
                html += `<div class="synthesis-date">${formatDate(s.created_at)}</div></div>`;
            }
            $('data-content').innerHTML = html + '</div>';
        } else if (sub === 'classifications') {
            let html = '<div class="class-cards">';
            for (const c of data) {
                html += `<div class="class-card"><div class="class-cat">${tagHtml(c.primary_category || '—', 'tag-purple')}</div>`;
                try { const tags = typeof c.secondary_tags === 'string' ? JSON.parse(c.secondary_tags) : (c.secondary_tags || []); if (tags.length) html += '<div class="class-tags">' + tags.map(t => tagHtml(t, 'tag-blue')).join(' ') + '</div>'; } catch {}
                if (c.justification) html += `<div class="class-justify">${escHtml(c.justification)}</div>`;
                html += `<div class="class-date">${formatDate(c.created_at)}</div></div>`;
            }
            $('data-content').innerHTML = html + '</div>';
        } else if (sub === 'actions') {
            let html = '<div class="action-list">';
            for (const a of data) {
                const prioEmoji = a.priority === 'ALTA' ? '🔴' : a.priority === 'MEDIA' ? '🟡' : '🟢';
                html += `<div class="action-item"><span class="action-prio">${prioEmoji}</span><span class="action-desc">${escHtml(a.description)}</span><span class="action-meta">${a.owner ? escHtml(a.owner) : ''}</span></div>`;
            }
            $('data-content').innerHTML = html + '</div>';
        }
    } catch { $('data-content').innerHTML = '<div class="empty">Error cargando datos</div>'; }
}

// ═══════════════════════════════════════════════════════════════════════
// TAB: REPORTS
// ═══════════════════════════════════════════════════════════════════════

async function loadReports() {
    try {
        const reports = await API.get('/reports');
        if (!reports || reports.length === 0) { $('reports-list').innerHTML = '<div class="empty">No hay informes aún. Ejecuta el pipeline.</div>'; return; }
        let html = '<div class="report-cards">';
        for (const r of reports) {
            const dateLabel = r.date || formatDate(r.modified);
            const size = r.size_bytes ? (r.size_bytes > 1024 ? (r.size_bytes / 1024).toFixed(1) + ' KB' : r.size_bytes + ' B') : '';
            html += `<div class="report-card" data-filename="${escAttr(r.filename)}">
                <div class="report-card-icon">📄</div>
                <div class="report-card-date">${escHtml(dateLabel)}</div>
                <div class="report-card-size">${escHtml(size)}</div></div>`;
        }
        $('reports-list').innerHTML = html + '</div>';
    } catch { $('reports-list').innerHTML = '<div class="empty">Error cargando informes</div>'; }
}

async function openReport(filename) {
    try {
        const result = await API.get('/reports/' + encodeURIComponent(filename));
        if (!result.content) { showToast('Informe vacío', 'error'); return; }
        $('report-title').textContent = filename;
        $('report-content').innerHTML = markdownToHtml(result.content);
        $('reports-list').style.display = 'none';
        $('report-viewer').style.display = 'block';
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function closeReport() { $('reports-list').style.display = ''; $('report-viewer').style.display = 'none'; }

async function generateReport() {
    showToast('Generando informe...', 'success');
    try {
        const result = await API.post('/reports/generate');
        if (result.status === 'ok') { showToast('Informe generado', 'success'); loadReports(); }
        else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

function markdownToHtml(md) {
    let html = escHtml(md);
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    html = html.replace(/^---$/gm, '<hr>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    html = '<p>' + html + '</p>';
    html = html.replace(/<p><\/p>/g, '');
    html = html.replace(/<p>(<h[1-6]>)/g, '$1');
    html = html.replace(/(<\/h[1-6]>)<\/p>/g, '$1');
    html = html.replace(/<p>(<hr>)<\/p>/g, '$1');
    html = html.replace(/<p>(<ul>)/g, '$1');
    html = html.replace(/(<\/ul>)<\/p>/g, '$1');
    html = html.replace(/<p>(<blockquote>)/g, '$1');
    html = html.replace(/(<\/blockquote>)<\/p>/g, '$1');
    return html;
}

// ═══════════════════════════════════════════════════════════════════════
// TAB: CONFIG
// ═══════════════════════════════════════════════════════════════════════

const PROVIDER_ICONS = {
    groq: '⚡', nvidia: '🟢', gemini_flash: '💎', cerebras: '🔷',
    modal: '🌀', openrouter_free: '🔀', together: '🤝',
    openai: '🧠', anthropic: '🎭', deepseek: '🐉',
};

async function loadConfig() {
    loadLLMProviders();
    loadRouterStatus();
    loadFeeds();
    loadState();
}

// ─── LLM Providers ──────────────────────────────────────────────────────
async function loadLLMProviders() {
    try {
        const llm = await API.get('/llm/config');
        if (llm.status !== 'ok') {
            $('llm-providers-list').innerHTML = '<div class="empty">Error cargando configuración LLM</div>';
            return;
        }
        renderLLMProviders(llm.config);
    } catch {
        $('llm-providers-list').innerHTML = '<div class="empty">No se pudo conectar al servidor</div>';
    }
}

function renderLLMProviders(config) {
    const container = $('llm-providers-list');
    if (!container) return;

    const providers = config.providers || {};
    const activeProvider = config.active_provider;
    const fallbackOrder = config.fallback_order || [];

    // Summary stats
    const totalProviders = Object.keys(providers).length;
    const withKeys = Object.values(providers).filter(p => p.has_key).length;

    let html = `<div style="padding:12px 20px 0; display:flex; gap:12px; margin-bottom:4px">
        <span class="tag tag-blue">${totalProviders} proveedores</span>
        <span class="tag tag-green">${withKeys} con API key</span>
        <span class="tag tag-purple">Orden: ${fallbackOrder.slice(0, 3).join(' → ')}…</span>
    </div>`;

    html += '<div class="provider-grid">';

    for (const [key, prov] of Object.entries(providers)) {
        const icon = PROVIDER_ICONS[key] || '🤖';
        const isActive = activeProvider === key;
        const hasKey = prov.has_key;
        const cardClass = isActive ? 'is-active' : hasKey ? 'has-key' : 'no-key';
        const keyStatusClass = hasKey ? 'has-key' : 'no-key';
        const keyStatusLabel = hasKey
            ? `● Key configurada (${prov.api_key_status || '•••'})`
            : '○ Sin API key';
        const inputClass = hasKey ? 'has-key' : '';

        html += `<div class="provider-card ${cardClass}">
            <div class="provider-icon">${icon}</div>
            <div class="provider-info">
                <div class="provider-name">${escHtml(prov.name)}${isActive ? ' <span class="tag tag-green" style="font-size:10px">ACTIVO</span>' : ''}</div>
                <div class="provider-model">${escHtml(prov.model)}</div>
                <div class="provider-key-status ${keyStatusClass}">${keyStatusLabel}</div>
            </div>
            <div class="provider-actions">
                <input type="password" id="key-${escHtml(key)}" class="provider-key-input ${inputClass}" placeholder="${hasKey ? '••••••••' : 'Pega tu API key…'}">
                <button class="btn-accent btn-sm cred-save" data-provider="${escAttr(key)}">Guardar</button>
                ${hasKey ? `<button class="btn-ghost btn-sm cred-delete" data-provider="${escAttr(key)}" title="Eliminar key">✕</button>` : ''}
            </div>
        </div>`;
    }

    html += '</div>';
    container.innerHTML = html;
}

async function saveCred(provider) {
    const input = $('key-' + provider); if (!input) return;
    const api_key = input.value.trim();
    if (!api_key) { showToast('Introduce una API key', 'error'); return; }
    try {
        const result = await API.post('/llm/credentials', { provider, api_key });
        if (result.status === 'ok') { showToast(`Key para '${provider}' guardada`, 'success'); input.value = ''; loadLLMProviders(); }
        else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

async function deleteCred(provider) {
    if (!confirm(`¿Eliminar la API key de '${provider}'?`)) return;
    try {
        const result = await API.del('/llm/credentials/' + provider);
        if (result.status === 'ok') { showToast(`Key de '${provider}' eliminada`, 'success'); loadLLMProviders(); }
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

// ─── Feeds ──────────────────────────────────────────────────────────────
async function loadFeeds() {
    try {
        const feeds = await API.get('/feeds');
        $('feeds-table').innerHTML = buildTable(
            [['name','Nombre'],['url','URL'],['category','Categoría'],['enabled','Estado']],
            feeds,
            { empty: 'No hay feeds. Añade uno.',
              render: {
                name: v => `<strong>${escHtml(v)}</strong>`,
                url: v => `<a href="${escHtml(v)}" target="_blank">${escHtml(truncate(v, 50))}</a>`,
                category: v => tagHtml(v, 'tag-purple'),
                enabled: (v, row) => {
                    const cls = v ? 'tag-green' : 'tag-red'; const label = v ? 'Activo' : 'Inactivo';
                    return `<span class="tag ${cls} feed-toggle" style="cursor:pointer" data-feed-name="${escAttr(row.name)}">${label}</span>`;
                }
              }
            }
        );
    } catch { $('feeds-table').innerHTML = '<div class="empty">Error cargando feeds</div>'; }
}

function showAddFeed() { $('add-feed-form').style.display = 'flex'; $('btn-add-feed').style.display = 'none'; }
function hideAddFeed() { $('add-feed-form').style.display = 'none'; $('btn-add-feed').style.display = ''; }

async function addFeed() {
    const name = $('feed-name').value.trim();
    const url = $('feed-url').value.trim();
    const category = $('feed-category').value;
    if (!name || !url) { showToast('Nombre y URL requeridos', 'error'); return; }
    try {
        const result = await API.post('/feeds/add', { name, url, category });
        if (result.status === 'ok') { showToast(`Feed "${name}" añadido`, 'success'); $('feed-name').value = ''; $('feed-url').value = ''; hideAddFeed(); loadFeeds(); }
        else showToast('Error: ' + (result.message || ''), 'error');
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

async function toggleFeed(name) {
    try {
        const result = await API.post('/feeds/toggle', { name });
        if (result.status === 'ok') { showToast(`Feed "${name}" ${result.enabled ? 'activado' : 'desactivado'}`, 'success'); loadFeeds(); }
    } catch (e) { showToast('Error: ' + e.message, 'error'); }
}

// ─── Router Status ──────────────────────────────────────────────────────
async function loadRouterStatus() {
    try {
        const result = await API.get('/llm/router/status');
        const container = $('router-content');
        if (!container) return;
        if (result.status !== 'ok') { container.innerHTML = '<div class="empty">Router no disponible</div>'; return; }
        const router = result.router || {};
        const targets = router.targets || {};
        const breakers = router.breakers || {};
        let html = '<div class="router-providers">';
        for (const [name, info] of Object.entries(targets)) {
            const state = info.circuit_state || 'closed';
            const cls = state === 'closed' ? '' : state === 'half_open' ? ' half-open' : ' open';
            const stateLabel = state === 'closed' ? 'OK' : state === 'half_open' ? 'Semi' : 'Caído';
            const stateColor = state === 'closed' ? 'var(--green)' : state === 'half_open' ? 'var(--orange)' : 'var(--red)';
            html += `<div class="provider-chip${cls}"><span class="provider-name">${escHtml(name)}</span><span class="provider-state" style="color:${stateColor}">${stateLabel} · score: ${(info.score || 0).toFixed(2)}</span></div>`;
        }
        html += '</div>';
        container.innerHTML = html;
    } catch { if ($('router-content')) $('router-content').innerHTML = '<div class="empty">Error cargando router</div>'; }
}

// ─── Logs ───────────────────────────────────────────────────────────────
function switchLogSub(sub) {
    document.querySelectorAll('[data-logsub]').forEach(b => b.classList.toggle('active', b.dataset.logsub === sub));
    loadLogSub(sub);
}

async function loadLogSub(sub) {
    try {
        const endpoints = { orchestrator: '/logs/orchestrator?lines=100', ingester: '/logs/ingester?lines=100', llm: '/logs/llm?lines=100', scheduler: '/logs/scheduler?lines=100' };
        const lines = await API.get(endpoints[sub] || endpoints.orchestrator);
        if (Array.isArray(lines) && lines.length > 0) {
            $('log-content').innerHTML = lines.map(l => {
                if (l.includes('[ERROR]') || l.includes('[CRITICAL]')) return `<span style="color:var(--red)">${l}</span>`;
                if (l.includes('[WARN]')) return `<span style="color:var(--orange)">${l}</span>`;
                if (l.includes('[INFO]')) return `<span style="color:var(--green)">${l}</span>`;
                return l;
            }).join('\n');
        } else { $('log-content').textContent = 'No hay entradas.'; }
    } catch (e) { $('log-content').textContent = 'Error: ' + e.message; }
}

// ─── State ──────────────────────────────────────────────────────────────
async function loadState() {
    try { const state = await API.get('/state'); $('state-content').textContent = JSON.stringify(state, null, 2); }
    catch (e) { $('state-content').textContent = 'Error: ' + e.message; }
}

// ═══════════════════════════════════════════════════════════════════════
// CHAT AI — WebSocket Streaming
// ═══════════════════════════════════════════════════════════════════════

let _chatWs = null;
let _chatStreamingMsg = null;
let _chatFullText = '';
let _chatReconnectTimer = null;

function connectChatWs() {
    const statusEl = $('chat-ws-status');
    if (statusEl) { statusEl.textContent = '○ Conectando...'; statusEl.className = 'chat-ws-status connecting'; }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try { _chatWs = new WebSocket(`${protocol}//${location.host}/ws/chat`); }
    catch { if (statusEl) { statusEl.textContent = '○ Error'; statusEl.className = 'chat-ws-status'; } return; }

    _chatWs.onopen = () => {
        if (statusEl) { statusEl.textContent = '● Conectado'; statusEl.className = 'chat-ws-status connected'; }
        loadChatProviders();
    };

    _chatWs.onclose = () => {
        if (statusEl) { statusEl.textContent = '○ Desconectado'; statusEl.className = 'chat-ws-status'; }
        if (_chatReconnectTimer) clearTimeout(_chatReconnectTimer);
        _chatReconnectTimer = setTimeout(() => { _chatReconnectTimer = null; connectChatWs(); }, 4000);
    };

    _chatWs.onerror = () => {
        if (statusEl) { statusEl.textContent = '○ Error'; statusEl.className = 'chat-ws-status'; }
    };

    _chatWs.onmessage = (event) => {
        try { handleChatMessage(JSON.parse(event.data)); } catch (e) { console.error('Chat WS parse error:', e); }
    };
}

async function loadChatProviders() {
    try {
        const llmCfg = await API.get('/llm/config');
        const select = $('chat-provider');
        if (select && llmCfg.status === 'ok') {
            select.innerHTML = '';
            for (const [name, cfg] of Object.entries(llmCfg.config.providers || {})) {
                if (cfg.has_key) { const opt = document.createElement('option'); opt.value = name; opt.textContent = cfg.name || name; select.appendChild(opt); }
            }
        }
    } catch {}
}

function handleChatMessage(data) {
    const messagesEl = $('chat-messages');
    if (!messagesEl) return;

    switch (data.type) {
        case 'token':
            if (!_chatStreamingMsg) { _chatStreamingMsg = createChatMsg('assistant', '', true); _chatFullText = ''; }
            _chatFullText += data.content;
            const textEl = _chatStreamingMsg.querySelector('.chat-msg-text');
            if (textEl) textEl.textContent = _chatFullText;
            messagesEl.scrollTop = messagesEl.scrollHeight;
            break;

        case 'done':
            if (_chatStreamingMsg) {
                _chatStreamingMsg.classList.remove('streaming');
                const roleEl = _chatStreamingMsg.querySelector('.chat-msg-role');
                if (roleEl) roleEl.textContent = `🤖 ${data.provider || 'AI'} ${data.transport === 'websocket' ? '⚡' : '📡'}`;
            }
            _chatStreamingMsg = null; _chatFullText = '';
            const sendBtn = $('chat-send-btn'); const input = $('chat-input');
            if (sendBtn) sendBtn.disabled = false; if (input) input.disabled = false;
            break;

        case 'error':
            if (_chatStreamingMsg) {
                _chatStreamingMsg.classList.remove('streaming');
                _chatStreamingMsg.querySelector('.chat-msg-text').textContent = _chatFullText + '\n\n❌ ' + (data.message || 'Error');
            } else {
                const errDiv = document.createElement('div'); errDiv.className = 'chat-msg-error'; errDiv.textContent = '❌ ' + (data.message || 'Error');
                messagesEl.appendChild(errDiv);
            }
            _chatStreamingMsg = null; _chatFullText = '';
            const sb = $('chat-send-btn'); const inp = $('chat-input');
            if (sb) sb.disabled = false; if (inp) inp.disabled = false;
            messagesEl.scrollTop = messagesEl.scrollHeight;
            break;

        case 'test_result':
            showToast(data.status === 'ok' ? `✅ ${data.provider} OK` : `❌ ${data.provider} falló`, data.status === 'ok' ? 'success' : 'error');
            break;

        case 'search_results':
            displaySearchResults(data.query, data.results, data.elapsed_sec);
            break;
    }
}

function createChatMsg(role, text, streaming = false) {
    const messagesEl = $('chat-messages');
    if (!messagesEl) return null;
    const div = document.createElement('div');
    div.className = `chat-msg ${role}${streaming ? ' streaming' : ''}`;
    const roleEl = document.createElement('div'); roleEl.className = 'chat-msg-role';
    roleEl.textContent = { 'user': '👤 Tú', 'assistant': '🤖 AI', 'system': '🤖 Sistema' }[role] || role;
    div.appendChild(roleEl);
    const textEl = document.createElement('div'); textEl.className = 'chat-msg-text'; textEl.textContent = text;
    div.appendChild(textEl);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

function sendChat() {
    const input = $('chat-input'); if (!input) return;
    const message = input.value.trim(); if (!message) return;

    if (message.toLowerCase().startsWith('/buscar ') || message.toLowerCase().startsWith('/search ')) {
        const query = message.replace(/^\/\/?(buscar|search)\s*/i, '').trim();
        if (query) { createChatMsg('user', message); input.value = ''; doWebSearch(query); return; }
    }

    const provider = $('chat-provider') ? $('chat-provider').value : 'modal';
    createChatMsg('user', message); input.value = '';
    const sendBtn = $('chat-send-btn'); if (sendBtn) sendBtn.disabled = true; input.disabled = true;

    if (_chatWs && _chatWs.readyState === WebSocket.OPEN) {
        _chatWs.send(JSON.stringify({ action: 'chat', message, provider }));
    } else {
        sendChatRest(message, provider);
    }
}

async function sendChatRest(message, provider) {
    try {
        const result = await API.post('/chat', { message, provider });
        if (result.status === 'ok') createChatMsg('assistant', result.response);
        else { const errDiv = document.createElement('div'); errDiv.className = 'chat-msg-error'; errDiv.textContent = '❌ ' + (result.message || 'Error'); $('chat-messages').appendChild(errDiv); }
    } catch (e) { const errDiv = document.createElement('div'); errDiv.className = 'chat-msg-error'; errDiv.textContent = '❌ ' + e.message; $('chat-messages').appendChild(errDiv); }
    const sb = $('chat-send-btn'); const inp = $('chat-input');
    if (sb) sb.disabled = false; if (inp) inp.disabled = false;
}

function testChatConnection() {
    const provider = $('chat-provider') ? $('chat-provider').value : 'modal';
    if (_chatWs && _chatWs.readyState === WebSocket.OPEN) {
        _chatWs.send(JSON.stringify({ action: 'test', provider })); showToast('Testeando...', 'success');
    } else showToast('WebSocket no conectado', 'error');
}

function clearChat() {
    const messagesEl = $('chat-messages');
    if (messagesEl) messagesEl.innerHTML = '<div class="chat-msg system"><div class="chat-msg-text">Chat limpiado. Escribe un mensaje.</div></div>';
    _chatStreamingMsg = null; _chatFullText = '';
}

// ─── Web Search in Chat ────────────────────────────────────────────────
function webSearchChat() {
    const input = $('chat-input'); if (!input) return;
    const message = input.value.trim();
    if (!message) { showToast('Escribe un término', 'error'); return; }
    const query = message.replace(/^\/\/?(buscar|search)\s*/i, '').trim() || message;
    input.value = ''; createChatMsg('user', `🔍 Buscar: ${query}`); doWebSearch(query);
}

async function doWebSearch(query) {
    if (_chatWs && _chatWs.readyState === WebSocket.OPEN) {
        _chatWs.send(JSON.stringify({ action: 'search', query, message: query })); return;
    }
    try {
        const result = await API.post('/web-search', { query });
        if (result.status === 'ok') displaySearchResults(query, result.results, result.elapsed_sec);
        else { const errDiv = document.createElement('div'); errDiv.className = 'chat-msg-error'; errDiv.textContent = '❌ ' + (result.message || 'Error'); $('chat-messages').appendChild(errDiv); }
    } catch (e) { const errDiv = document.createElement('div'); errDiv.className = 'chat-msg-error'; errDiv.textContent = '❌ ' + e.message; $('chat-messages').appendChild(errDiv); }
}

function displaySearchResults(query, results, elapsed) {
    const messagesEl = $('chat-messages'); if (!messagesEl) return;
    const div = document.createElement('div'); div.className = 'chat-msg assistant';
    const roleEl = document.createElement('div'); roleEl.className = 'chat-msg-role';
    roleEl.textContent = `🔍 Web Search — ${results ? results.length : 0} resultados (${elapsed || 0}s)`;
    div.appendChild(roleEl);
    const textEl = document.createElement('div'); textEl.className = 'chat-msg-text chat-search-results';
    if (!results || results.length === 0) {
        textEl.innerHTML = `<em>No se encontraron resultados para "${escHtml(query)}"</em>`;
    } else {
        let html = `<div style="font-weight:600;margin-bottom:8px">Resultados para "${escHtml(query)}"</div>`;
        for (const r of results) {
            html += `<div class="chat-search-item"><div class="chat-search-title"><a href="${escHtml(r.url)}" target="_blank" rel="noopener noreferrer">${escHtml(r.title)}</a>${r.source ? `<span class="chat-search-source">${escHtml(r.source)}</span>` : ''}${r.date ? `<span class="chat-search-date">${escHtml(r.date)}</span>` : ''}</div><div class="chat-search-snippet">${escHtml(truncate(r.snippet, 200))}</div></div>`;
        }
        textEl.innerHTML = html;
    }
    div.appendChild(textEl); messagesEl.appendChild(div); messagesEl.scrollTop = messagesEl.scrollHeight;
}
