
const $ = (s) => document.querySelector(s);
let CFG = {}, TOKEN = '', QUEUE = [], SEQ = 0, RUNNING = false, TOKEN_SET = false,
    URL_TIMER = null, PARSED = null;

/* ---------- 小工具 ---------- */
function fmtSize(n){
  if(n < 1024) return n + ' B';
  if(n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  if(n < 1024*1024*1024) return (n/1024/1024).toFixed(2) + ' MB';
  return (n/1024/1024/1024).toFixed(2) + ' GB';
}
function toast(msg, kind){
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => el.remove(), 3600);
}
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function copyText(text){
  if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
  return Promise.resolve();
}
function pad(n){ return String(n).padStart(2,'0'); }
function previewPath(name, rel){
  const tpl = ($('#o_dir').value.trim() || CFG.targetDir || 'uploads/{date}');
  const d = new Date();
  const stem = name.replace(/\.[^.]+$/, ''), ext = (name.match(/\.([^.]+)$/) || ['',''])[1];
  const map = {
    date: d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()),
    yyyy: String(d.getFullYear()), mm: pad(d.getMonth()+1), dd: pad(d.getDate()),
    time: pad(d.getHours())+pad(d.getMinutes())+pad(d.getSeconds()),
    timestamp: String(Math.floor(d.getTime()/1000)),
    name: stem, ext: ext, filename: name
  };
  let out = tpl.replace(/\{(\w+)\}/g, (m,k) => (k in map ? map[k] : m));
  const dirs = out.split('/').filter(Boolean);
  if(rel && $('#o_keep').checked){
    const parts = rel.split('/').filter(Boolean);
    parts.pop();
    return dirs.concat(parts, [name]).join('/');
  }
  return dirs.concat([name]).join('/');
}

/* ---------- 仓库地址解析（与后端 parse_repo_url 同规则，后端为准） ---------- */
const RESERVED_OWNERS = new Set(['orgs','users','settings','marketplace','topics','collections',
  'sponsors','features','about','pricing','explore','notifications','new','login','apps','site',
  'security','enterprise','search','trending','codespaces','dashboard','pulls','issues','account',
  'organizations','sessions','logout']);
const BRANCH_PATH_HEADS = new Set(['tree','blob','commits','commit','branches','raw','edit']);
const TREE_LIKE = new Set(['tree']);
const FILE_LIKE = new Set(['blob','raw','edit']);
const BRANCH_ONLY = new Set(['commits','commit']);

function parseRepoUrl(text){
  let raw = String(text || '').trim().replace(/^["'<]+/, '').replace(/["'>]+$/, '');
  if(!raw) return null;
  let m = raw.match(/(?:https?|ssh|git):\/\/[^\s]+/);
  if(m) raw = m[0];
  else { m = raw.match(/[^\s/]+@[^\s:]+[:/][^\s]+/); if(m) raw = m[0]; }

  let host = 'github.com', rest = '';
  if(raw.startsWith('git@') || raw.startsWith('ssh://')){
    const scp = raw.match(/^(?:ssh:\/\/)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+)$/);
    if(!scp) return null;
    host = scp[1].toLowerCase(); rest = scp[2];
  } else if(raw.indexOf('://') >= 0){
    let u;
    try { u = new URL(raw); } catch(e){ return null; }
    host = u.hostname.toLowerCase(); rest = u.pathname;
  } else {
    const parts = raw.split('/').filter(Boolean);
    if(!parts.length) return null;
    if(parts[0].indexOf('.') >= 0){ host = parts[0].toLowerCase(); rest = parts.slice(1).join('/'); }
    else { host = 'github.com'; rest = parts.join('/'); }
  }
  const segs = rest.split('/').filter(Boolean).map(s => {
    try { return decodeURIComponent(s); } catch(e){ return s; }
  });
  if(segs.length < 2) return null;
  const owner = segs[0];
  let repo = segs[1];
  if(RESERVED_OWNERS.has(owner.toLowerCase())) return null;
  if(repo.toLowerCase().endsWith('.git')) repo = repo.slice(0, -4);
  if(!repo) return null;
  let branch = null, candidates = [], treePath = '';
  if(segs.length >= 3 && BRANCH_PATH_HEADS.has(segs[2].toLowerCase())){
    const head = segs[2].toLowerCase();
    const tail = segs.slice(3);
    if(TREE_LIKE.has(head) && tail.length){
      for(let i = tail.length; i >= 1; i--) candidates.push(tail.slice(0, i).join('/'));
      treePath = candidates[0];
    } else if(FILE_LIKE.has(head) && tail.length >= 2){
      candidates = [tail.slice(0, -1).join('/')];
    } else if(BRANCH_ONLY.has(head) && tail.length){
      candidates = [tail.join('/')];
    }
    branch = candidates.length ? candidates[0] : null;
  }
  const apiBase = (host === 'github.com' || host === 'www.github.com')
    ? 'https://api.github.com' : ('https://' + host + '/api/v3');
  return {owner: owner, repo: repo, branch: branch, branchCandidates: candidates,
          treePath: treePath, host: host, apiBase: apiBase};
}

function applyRepoUrl(opts){
  opts = opts || {};
  const raw = $('#c_repoUrl').value.trim();
  const hint = $('#urlHint');
  if(!raw){
    hint.textContent = '支持完整链接、带 .git 的、SSH 地址（git@github.com:owner/repo.git）、/tree/分支 链接，以及 owner/repo 简写';
    hint.style.color = '';
    return null;
  }
  const parsed = parseRepoUrl(raw);
  if(!parsed){
    hint.textContent = '✗ 没认出这是仓库地址：至少要有 owner/repo 两段';
    hint.style.color = 'var(--err)';
    PARSED = null;
    return null;
  }
  PARSED = parsed;
  $('#c_owner').value = parsed.owner;
  $('#c_repo').value = parsed.repo;
  if(parsed.branch) $('#c_branch').value = parsed.branch;
  const curApi = $('#c_apiBase').value.trim();
  if(parsed.host !== 'github.com' || !curApi || curApi === 'https://api.github.com'){
    $('#c_apiBase').value = parsed.apiBase;
  }
  const ambiguous = parsed.branchCandidates && parsed.branchCandidates.length > 1;
  hint.textContent = '✓ 已识别 ' + parsed.owner + '/' + parsed.repo
    + (parsed.branch ? ('　分支：' + parsed.branch) : '')
    + (parsed.host !== 'github.com' ? ('　·　' + parsed.host) : '')
    + (ambiguous ? '　（分支名可能带斜杠，加载分支列表会自动校正）' : '');
  hint.style.color = 'var(--ok)';
  updateTarget();
  if(opts.loadBranches) loadBranches(true);
  return parsed;
}

function debouncedRepoUrl(){
  clearTimeout(URL_TIMER);
  URL_TIMER = setTimeout(() => applyRepoUrl({loadBranches: hasToken()}), 400);
}
function hasToken(){
  return !!($('#c_token').value.trim() || TOKEN || TOKEN_SET);
}
/* 没填 Token 时也能看公开仓库，但要把原因说清楚 */
function anonHint(msg){
  return hasToken() ? msg : (msg + '（没填 Token 就只能读公开仓库，私有仓库请先填 Token）');
}

async function loadBranches(silent){
  const owner = $('#c_owner').value.trim();
  const repo = $('#c_repo').value.trim();
  const apiBase = $('#c_apiBase').value.trim();
  const tok = $('#c_token').value.trim() || TOKEN;
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }

  const btn = $('#btnLoadBranches');
  const oldText = btn.textContent;
  btn.disabled = true; btn.textContent = '加载中…';
  try {
    const qs = new URLSearchParams({owner: owner, repo: repo});
    if(apiBase) qs.set('apiBase', apiBase);
    if(PARSED && PARSED.branchCandidates && PARSED.branchCandidates.length){
      PARSED.branchCandidates.forEach(c => qs.append('candidates', c));   // 交给后端做最长前缀匹配
    }
    const res = await fetch('/api/branches?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      if(!silent) toast(anonHint('加载分支失败：' + (res.error || '')), 'err');
      return null;
    }
    const dl = $('#branchOptions');
    dl.innerHTML = res.branches.map(b => '<option value="' + esc(b) + '"></option>').join('');
    const before = $('#c_branch').value.trim();
    const guess = (PARSED && PARSED.branch) || '';
    let note = '', noteKind = 'ok';
    if(res.resolvedBranch){
      $('#c_branch').value = res.resolvedBranch;
      note = '从链接里认出分支：' + res.resolvedBranch;
    } else if(!before){
      $('#c_branch').value = res.defaultBranch || '';
    } else if(res.branches.indexOf(before) < 0){
      if(guess && before === guess){
        $('#c_branch').value = res.defaultBranch || '';
        note = res.defaultBranch
          ? ('链接里的分支在仓库里没找到，已改用默认分支 ' + res.defaultBranch)
          : '链接里的分支在仓库里没找到，已清空分支（留空＝自动用仓库默认分支）';
        noteKind = 'err';
      } else {
        dl.innerHTML += '<option value="' + esc(before) + '"></option>';   // 保住手填的分支
      }
    }
    PARSED = null;
    toast(note || res.message || ('已加载 ' + res.branches.length + ' 个分支'), noteKind);
    updateTarget();
    // 顺手把分支里的文件夹显示出来；链接里带了目录就直接进到那一层
    let wantPath = res.resolvedPath || '';
    if(wantPath){
      const probe = await loadTree(wantPath, true);
      if(probe && probe.empty){
        // 链接可能指向的是一个文件（/tree/…/app.py），退到它所在的文件夹
        wantPath = wantPath.split('/').filter(Boolean).slice(0, -1).join('/');
        await loadTree(wantPath, true);
      }
    } else {
      loadTree('', true);
    }
    if(wantPath){
      $('#o_dir').value = wantPath;
      updateTarget();
      toast('链接里的目录：' + wantPath, 'ok');
    }
    // 顺手把仓库文件列表也拉出来，喂给「从仓库文件夹里挑」和文件管理
    if(hasToken() && !FILES.loaded) loadFiles(true);
    return res;
  } catch(e){
    if(!silent) toast('加载分支失败：' + e, 'err');
    return null;
  } finally {
    btn.disabled = false; btn.textContent = oldText;
  }
}

/* ---------- 状态加载 ---------- */
async function loadState(){
  const res = await fetch('/api/state').then(r => r.json());
  CFG = res.config || {};
  TOKEN_SET = !!res.tokenSet;
  $('#verTag').textContent = 'v' + res.version;
  $('#c_owner').value = CFG.owner || '';
  $('#c_repo').value = CFG.repo || '';
  $('#c_branch').value = CFG.branch || '';
  $('#c_targetDir').value = CFG.targetDir || 'uploads/{date}';
  $('#c_commitMessage').value = CFG.commitMessage || 'chore(upload): add {name}';
  $('#c_apiBase').value = CFG.apiBase || 'https://api.github.com';
  $('#c_token').placeholder = TOKEN_SET
    ? '已保存 Token（留空则继续使用）'
    : 'ghp_… 或 github_pat_…';
  $('#o_keep').checked = CFG.keepFolder !== false;
  $('#o_rename').checked = !!CFG.autoRename;
  $('#o_copy').checked = CFG.copyLink !== false;
  const saved = localStorage.getItem('gd_token');
  if(saved){ $('#c_token').value = saved; TOKEN = saved; $('#c_remember').checked = true; }
  if(res.tokenFromEnv){
    $('#c_token').placeholder = '已通过环境变量 GITHUB_TOKEN 提供（留空即可）';
    $('#cfgHint').textContent = 'Token 来自环境变量 GITHUB_TOKEN，不会写入 config.json';
  }
  if(CFG.owner && CFG.repo) $('#c_repoUrl').value = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
  updateTarget();
  renderHistory(res.history || []);
  if(!CFG.owner || !CFG.repo){ $('#cfgBox').open = true; }
  else if(hasToken()) loadBranches(true);
}
function updateTarget(){
  const repo = (CFG.owner && CFG.repo) ? (CFG.owner + '/' + CFG.repo) : '未配置仓库';
  const dir = $('#o_dir').value.trim() || CFG.targetDir || 'uploads/{date}';
  const br = $('#c_branch').value.trim() || '自动（仓库默认分支）';
  $('#targetLine').textContent = '目标：' + repo + ' @ ' + br + '  →  ' + dir + '/';
  const ok = !!(CFG.owner && CFG.repo);
  $('#btnOpenRepo').disabled = !ok;
}

/* ---------- 保存配置 ---------- */
async function saveConfig(extra){
  const body = {
    config: {
      repoUrl: $('#c_repoUrl').value.trim(),
      owner: $('#c_owner').value.trim(),
      repo: $('#c_repo').value.trim(),
      branch: $('#c_branch').value.trim(),
      targetDir: $('#c_targetDir').value.trim() || 'uploads/{date}',
      commitMessage: $('#c_commitMessage').value.trim() || 'chore(upload): add {name}',
      apiBase: $('#c_apiBase').value.trim() || 'https://api.github.com',
      keepFolder: $('#o_keep').checked,
      autoRename: $('#o_rename').checked,
      copyLink: $('#o_copy').checked
    }
  };
  const tok = $('#c_token').value.trim();
  if(tok){
    TOKEN = tok;
    if($('#c_remember').checked){ body.token = tok; localStorage.setItem('gd_token', tok); }
    else { localStorage.removeItem('gd_token'); }
  }
  if(extra && extra.clearToken) body.clearToken = true;
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  }).then(r => r.json());
  if(res.ok){
    CFG = res.config; TOKEN_SET = !!res.tokenSet;
    // 用服务端归一化后的结果回填（地址解析以后端为准）
    $('#c_owner').value = CFG.owner || '';
    $('#c_repo').value = CFG.repo || '';
    $('#c_branch').value = CFG.branch || '';
    $('#c_targetDir').value = CFG.targetDir || '';
    $('#c_apiBase').value = CFG.apiBase || 'https://api.github.com';
    if(CFG.owner && CFG.repo) $('#c_repoUrl').value = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
    updateTarget();
    if(extra && extra.clearToken){ TOKEN = ''; localStorage.removeItem('gd_token'); $('#c_token').value = ''; }
    toast(res.message || '配置已保存', 'ok');
    if(res.tokenSet || tok) loadBranches(true);
  } else {
    toast(res.error || '保存失败', 'err');
    if($('#c_repoUrl').value.trim()){
      $('#urlHint').textContent = '✗ ' + (res.error || '地址无法识别');
      $('#urlHint').style.color = 'var(--err)';
    }
  }
}

/* ---------- 仓库目录浏览器 ---------- */
let TREE = {path: '', entries: [], branch: '', loaded: false};

async function loadTree(path, silent){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  // 留空就交给服务端自己认（它会用仓库的真实默认分支），别在这里替它填 main
  const branch = $('#c_branch').value.trim();
  const apiBase = $('#c_apiBase').value.trim();
  const tok = $('#c_token').value.trim() || TOKEN;
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }

  const qs = new URLSearchParams({owner: owner, repo: repo, branch: branch, path: path || ''});
  if(apiBase) qs.set('apiBase', apiBase);
  $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">…</span><span class="nm">加载中…</span></div>';
  try {
    const res = await fetch('/api/tree?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">✗</span><span class="nm" style="color:#cf222e">'
        + esc(res.error || '加载失败') + '</span></div>';
      if(!silent) toast(anonHint('加载目录失败：' + (res.error || '')), 'err');
      return null;
    }
    TREE = {path: res.path || '', entries: res.entries || [], branch: res.branch || branch,
            loaded: true, res: res};
    $('#treeFilter').value = '';
    renderTree();
    return res;
  } catch(e){
    $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">✗</span><span class="nm" style="color:#cf222e">'
      + esc(String(e)) + '</span></div>';
    if(!silent) toast('加载目录失败：' + e, 'err');
    return null;
  }
}

function renderTree(){
  const res = TREE.res || {};
  const parts = (TREE.path || '').split('/').filter(Boolean);
  let crumb = '<a data-go="">仓库根目录</a>';
  let acc = '';
  parts.forEach(p => {
    acc = acc ? acc + '/' + p : p;
    crumb += '　/　<a data-go="' + esc(acc) + '">' + esc(p) + '</a>';
  });
  $('#treeCrumbs').innerHTML = '<span>分支 <b>' + esc(TREE.branch) + '</b>：</span>' + crumb;

  const kw = ($('#treeFilter').value || '').trim().toLowerCase();
  const all = TREE.entries || [];
  const list = kw ? all.filter(e => String(e.name).toLowerCase().indexOf(kw) >= 0) : all;

  const rows = list.map(e => {
    if(e.type === 'dir'){
      return '<div class="row-i dir" data-enter="' + esc(e.path) + '">'
        + '<span class="ico2">📁</span><span class="nm">' + esc(e.name) + '</span>'
        + '<button class="btnmini" data-pick="' + esc(e.path) + '">选它</button></div>';
    }
    return '<div class="row-i file"><span class="ico2">📄</span><span class="nm">' + esc(e.name)
      + '</span><span class="cnt">' + fmtSize(e.size || 0) + '</span></div>';
  });
  if(!rows.length){
    rows.push('<div class="row-i"><span class="ico2">∅</span><span class="nm">'
      + esc(kw ? ('没有匹配「' + kw + '」的条目') : (res.message || '这里还没有文件夹')) + '</span></div>');
  }
  $('#treeList').innerHTML = rows.join('');
  $('#treeList').querySelectorAll('[data-enter]').forEach(el => {
    el.onclick = () => enterDir(el.getAttribute('data-enter'));
  });
  $('#treeList').querySelectorAll('[data-pick]').forEach(el => {
    el.onclick = (ev) => { ev.stopPropagation(); pickDir(el.getAttribute('data-pick')); };
  });
  $('#treeCrumbs').querySelectorAll('[data-go]').forEach(el => {
    el.onclick = () => loadTree(el.getAttribute('data-go'));
  });
  $('#treeHint').textContent = kw
    ? ('筛选出 ' + list.length + ' / ' + all.length + ' 条')
    : (res.message || '');
}

function enterDir(path){ return loadTree(path); }

function pickDir(path){
  $('#o_dir').value = path || '';
  updateTarget();
  toast(path ? ('本次上传到 ' + path + '/') : '已切回默认目录', 'ok');
}

function setTreeAsDefault(){
  $('#c_targetDir').value = TREE.path || '';
  updateTarget();
  toast('默认目标目录已设为 ' + (TREE.path ? TREE.path + '/' : '仓库根目录') + '，记得点「保存配置」', 'ok');
}

function ghWebHost(){
  const api = ($('#c_apiBase').value.trim() || CFG.apiBase || 'https://api.github.com');
  try {
    const h = new URL(api).hostname;
    return (h === 'api.github.com') ? 'github.com' : h;
  } catch(e){ return 'github.com'; }
}

/* 页面自己在拼 GitHub 链接时需要具体分支名：优先用服务端算出来的那个 */
function liveBranch(){
  return ($('#c_branch').value.trim() || FILES.branch || TREE.branch || CFG.branch || '').trim();
}

function openTreeOnGitHub(){
  if(!(CFG.owner && CFG.repo)){ toast('先粘贴仓库地址', 'err'); return; }
  const branch = liveBranch();
  const host = 'https://' + ghWebHost() + '/' + CFG.owner + '/' + CFG.repo;
  // 分支还没认出来时退到仓库首页，总比拼一个错分支的 404 链接好
  const url = branch ? (host + '/tree/' + encodeURI(branch) + (TREE.path ? '/' + encodeURI(TREE.path) : '')) : host;
  window.open(url, '_blank');
}

/* ---------- 队列 ---------- */
function addItems(items, empties){
  empties = empties || [];
  if(!items.length && !empties.length) return;
  // 拖入文件夹时自动开启「保留文件夹结构」
  if(items.some(it => (it.rel || '').indexOf('/') >= 0) && !$('#o_keep').checked){
    $('#o_keep').checked = true;
    toast('检测到文件夹，已自动开启「保留文件夹结构」', 'ok');
  }
  let n = 0, f = 0;
  for(const it of items){
    const dup = QUEUE.some(q => q.file.name === it.file.name && q.file.size === it.file.size && q.rel === it.rel);
    if(dup) continue;
    QUEUE.push({id: ++SEQ, file: it.file, rel: it.rel || '', status: 'wait', pct: 0, result: null, error: ''});
    n++;
  }
  // 空目录：Git 不保存空目录，用一个 0 字节 .gitkeep 占位
  if($('#o_gitkeep').checked){
    for(const rel of empties){
      const keep = rel.replace(/\/+$/, '') + '/.gitkeep';
      if(QUEUE.some(q => q.rel === keep)) continue;
      let fileObj;
      try { fileObj = new File([''], '.gitkeep', {type: 'text/plain'}); }
      catch(e){ fileObj = new Blob([''], {type: 'text/plain'}); fileObj.name = '.gitkeep'; }
      QUEUE.push({id: ++SEQ, file: fileObj, rel: keep, status: 'wait', pct: 0, result: null, error: ''});
      n++; f++;
    }
  }
  renderQueue();
  if(n) toast('已加入 ' + (n - f) + ' 个文件' + (f ? (' · ' + f + ' 个空文件夹占位') : ''), 'ok');
}
function renderQueue(){
  const box = $('#list');
  if(!QUEUE.length){ box.innerHTML = '<div class="empty">还没有待上传的文件</div>'; $('#queueInfo').textContent = ''; return; }
  box.innerHTML = QUEUE.map(q => {
    const chip = {wait:['wait','等待'], up:['up','上传中'], ok:['ok','完成'], err:['err','失败']}[q.status];
    const links = q.result ? ('<div class="links">' +
      (q.result.urls.raw ? '<a href="' + q.result.urls.raw + '" target="_blank">直链</a>' : '') +
      (q.result.urls.html ? '<a href="' + q.result.urls.html + '" target="_blank">GitHub</a>' : '') +
      (q.result.urls.cdn ? '<a href="' + q.result.urls.cdn + '" target="_blank">CDN</a>' : '') +
      (q.result.urls.raw ? '<button data-copy="' + esc(q.result.urls.raw) + '">复制直链</button>' : '') + '</div>') : '';
    return '<div class="item">' +
      '<div class="top"><div><div class="nm">' + esc(q.rel || q.file.name) + '</div>' +
      '<div class="meta">' + fmtSize(q.file.size) + ' · → ' + esc(previewPath(q.file.name, q.rel)) +
      (q.result ? ' · ' + q.result.seconds + 's · ' + (q.result.mode === 'git-data-api' ? '大文件通道' : 'Contents API') : '') +
      (q.error ? ' · <span style="color:#cf222e">' + esc(q.error) + '</span>' : '') +
      '</div></div><span class="chip ' + chip[0] + '">' + chip[1] + '</span></div>' +
      '<div class="bar"><i style="width:' + Math.round(q.pct*100) + '%' +
      (q.status === 'ok' ? ';background:#1a7f37' : q.status === 'err' ? ';background:#cf222e' : '') + '"></i></div>' +
      links + '</div>';
  }).join('');
  box.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
    copyText(b.getAttribute('data-copy')).then(() => toast('链接已复制', 'ok'));
  });
  const done = QUEUE.filter(q => q.status === 'ok').length;
  const fail = QUEUE.filter(q => q.status === 'err').length;
  $('#queueInfo').textContent = QUEUE.length + ' 个文件' + (done ? ' · 成功 ' + done : '') + (fail ? ' · 失败 ' + fail : '');
}

function postFile(item, onProgress){
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(item.file.name));
    if(item.rel && $('#o_keep').checked) xhr.setRequestHeader('X-Rel-Path', encodeURIComponent(item.rel));
    const dir = $('#o_dir').value.trim();
    if(dir) xhr.setRequestHeader('X-Target-Dir', encodeURIComponent(dir));
    xhr.setRequestHeader('X-Keep-Rel', $('#o_keep').checked ? '1' : '0');
    const tok = $('#c_token').value.trim() || TOKEN;
    if(tok) xhr.setRequestHeader('X-GitHub-Token', tok);
    xhr.upload.onprogress = e => { if(e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch(e){ data = {ok:false, error:'服务端返回异常'}; }
      if(xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
      else reject(data.error || ('HTTP ' + xhr.status));
    };
    xhr.onerror = () => reject('网络错误：无法连接本地服务');
    xhr.ontimeout = () => reject('上传超时');
    xhr.send(item.file);
  });
}

async function startUpload(){
  if(RUNNING) return;
  const pending = QUEUE.filter(q => q.status === 'wait' || q.status === 'err');
  if(!pending.length){ toast('没有待上传的文件', 'err'); return; }
  if(!(($('#c_owner').value.trim()) && ($('#c_repo').value.trim()))){ toast('请先填写 owner / repo', 'err'); $('#cfgBox').open = true; return; }
  if(!(($('#c_token').value.trim()) || TOKEN || TOKEN_SET)){ toast('请先填写 GitHub Token', 'err'); $('#cfgBox').open = true; return; }
  RUNNING = true;
  $('#btnStart').disabled = true; $('#btnStart').textContent = '上传中…';
  let lastLink = '';
  for(const item of pending){
    item.status = 'up'; item.pct = 0; item.error = '';
    renderQueue();
    try {
      const res = await postFile(item, p => { item.pct = p; renderQueue(); });
      item.status = 'ok'; item.pct = 1; item.result = res;
      lastLink = res.urls.raw;
    } catch(err){
      item.status = 'err'; item.pct = 1; item.error = String(err);
    }
    renderQueue();
  }
  RUNNING = false;
  $('#btnStart').disabled = false; $('#btnStart').textContent = '开始上传';
  const fail = QUEUE.filter(q => q.status === 'err').length;
  toast(fail ? ('上传结束，' + fail + ' 个失败') : '全部上传完成', fail ? 'err' : 'ok');
  if(!fail && lastLink && $('#o_copy').checked) copyText(lastLink).then(() => toast('已复制最后一个文件的直链', 'ok'));
  loadHistory();
}

/* ---------- 文件夹拖拽 ---------- */
function readAllEntries(reader){
  return new Promise(resolve => {
    const acc = [];
    const step = () => reader.readEntries(batch => {
      if(!batch.length) return resolve(acc);
      acc.push(...batch); step();
    }, () => resolve(acc));
    step();
  });
}
function entryFile(entry){ return new Promise((res, rej) => entry.file(res, rej)); }
async function walkEntry(entry, prefix, out, empties){
  if(entry.isFile){
    try { const f = await entryFile(entry); out.push({file: f, rel: prefix + entry.name}); } catch(e){}
  } else if(entry.isDirectory){
    const kids = await readAllEntries(entry.createReader());
    const here = prefix + entry.name + '/';
    if(!kids.length){ empties.push(here); return; }
    for(const it of kids) await walkEntry(it, here, out, empties);
  }
}
async function itemsFromDataTransfer(dt){
  const out = [], empties = [];
  const items = dt.items ? Array.from(dt.items) : [];
  const entries = items.map(i => i.webkitGetAsEntry ? i.webkitGetAsEntry() : null).filter(Boolean);
  if(entries.length){
    for(const e of entries) await walkEntry(e, '', out, empties);
    if(out.length || empties.length) return {items: out, empties: empties};
  }
  for(const f of Array.from(dt.files || [])) out.push({file: f, rel: f.webkitRelativePath || ''});
  return {items: out, empties: empties};
}

/* ---------- 历史 ---------- */
function renderHistory(items){
  if(!items.length){ $('#histWrap').innerHTML = '<div class="empty">暂无记录</div>'; return; }
  const rows = items.map(it =>
    '<tr><td>' + esc(it.time || '') + '</td><td>' + esc(it.path) + '</td>' +
    '<td>' + fmtSize(it.size || 0) + '</td>' +
    '<td><a href="' + ((it.urls && it.urls.raw) || '#') + '" target="_blank">直链</a>' +
    ' · <a href="' + ((it.urls && it.urls.html) || '#') + '" target="_blank">GitHub</a></td></tr>').join('');
  $('#histWrap').innerHTML = '<table class="hist" style="width:100%;border-collapse:collapse">' +
    '<tr><th>时间</th><th>仓库路径</th><th>大小</th><th>链接</th></tr>' + rows + '</table>';
}
async function loadHistory(){
  const res = await fetch('/api/history').then(r => r.json()).catch(() => ({history: []}));
  renderHistory(res.history || []);
}

/* ---------- 弹窗（确认 / 输入） ---------- */
let MODAL_CTX = null;
function openModal(opts){
  const fields = opts.fields || [];
  let body = '';
  if(opts.warn) body += '<div class="warnbox">' + opts.warn + '</div>';
  if(opts.html) body += opts.html;
  fields.forEach((f, i) => {
    body += '<label class="f">' + esc(f.label || f.name) + '</label>'
      + '<input type="text" id="m_in' + i + '" value="' + esc(f.value || '') + '"'
      + ' placeholder="' + esc(f.placeholder || '') + '" autocomplete="off">';
  });
  $('#modalTitle').textContent = opts.title || '确认';
  $('#modalBody').innerHTML = body;
  const ok = $('#modalOk');
  ok.textContent = opts.okText || '确定';
  ok.className = opts.danger ? 'danger' : 'primary';
  ok.disabled = false;
  $('#modal').classList.add('on');
  fields.forEach((f, i) => {
    const el = document.querySelector('#m_in' + i);
    if(el) el.value = f.value || '';
  });
  const first = document.querySelector('#m_in0');
  if(first && first.focus) setTimeout(() => first.focus(), 30);
  return new Promise(resolve => { MODAL_CTX = {fields: fields, resolve: resolve}; });
}
function modalOk(){
  if(!MODAL_CTX) return;
  const ctx = MODAL_CTX;
  const vals = ctx.fields.map((f, i) => {
    const el = document.querySelector('#m_in' + i);
    return el ? String(el.value || '').trim() : '';
  });
  MODAL_CTX = null;
  $('#modal').classList.remove('on');
  ctx.resolve(vals);
}
function modalCancel(){
  if(!MODAL_CTX){ $('#modal').classList.remove('on'); return; }
  const ctx = MODAL_CTX;
  MODAL_CTX = null;
  $('#modal').classList.remove('on');
  ctx.resolve(null);
}
/* 确认框：确定 -> []，取消 -> null */
function askConfirm(opts){ return openModal(opts); }

/* ---------- 仓库文件管理 ---------- */
let FILES = {loaded: false, files: [], dirs: [], branch: '', path: '', truncated: false,
             deep: false, res: null};
let SEL = new Set();

function curBranch(){ return $('#c_branch').value.trim(); }   // 空 = 让服务端自动用仓库默认分支

async function loadFiles(silent, path, deep){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }
  if(path === undefined) path = FILES.path || '';
  if(deep === undefined) deep = $('#fileDeep').checked;

  const tok = $('#c_token').value.trim() || TOKEN;
  const btn = $('#btnFilesLoad'), old = btn.textContent;
  btn.disabled = true; btn.textContent = deep ? '扫描中…' : '加载中…';
  $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">…</span><span class="nm">'
    + (deep ? '正在把子文件夹也一起列出来，仓库大时会慢一点…' : '加载中…') + '</span></div>';
  try {
    const qs = new URLSearchParams({owner: owner, repo: repo, branch: curBranch(), path: path || ''});
    if(deep) qs.set('deep', '1');
    const apiBase = $('#c_apiBase').value.trim();
    if(apiBase) qs.set('apiBase', apiBase);
    const res = await fetch('/api/files?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      FILES = {loaded: false, files: [], dirs: [], branch: '', path: '', truncated: false,
               deep: false, res: null};
      SEL = new Set();
      $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">✗</span>'
        + '<span class="nm" style="color:var(--err)">' + esc(res.error || '加载失败') + '</span></div>';
      if(!silent) toast(anonHint('加载文件失败：' + (res.error || '')), 'err');
      return null;
    }
    FILES = {loaded: true, files: res.files || [], dirs: res.dirs || [],
             branch: res.branch || curBranch(), path: res.prefix || '',
             truncated: !!res.truncated, deep: !!res.deep, res: res};
    SEL = new Set();
    if(!deep) $('#fileFilter').value = '';
    fillDirPicker();
    renderFiles();
    if(!silent) toast(res.message || ('已列出 ' + FILES.files.length + ' 个文件'), 'ok');
    return res;
  } catch(e){
    $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">✗</span>'
      + '<span class="nm" style="color:var(--err)">' + esc(String(e)) + '</span></div>';
    if(!silent) toast('加载文件失败：' + e, 'err');
    return null;
  } finally { btn.disabled = false; btn.textContent = old; }
}

/* 把仓库里已有的文件夹塞进「本次传到哪儿」的下拉，挑一个就填进输入框 */
function fillDirPicker(){
  const sel = $('#o_dirPick');
  if(!sel) return;
  let html = '<option value="__pick__">— 从仓库文件夹里挑 —</option>'
    + '<option value="__default__">（用默认目标模板）</option>'
    + '<option value="">（仓库根目录）</option>';
  (FILES.dirs || []).forEach(d => {
    const n = (d.count === null || d.count === undefined) ? '' : '（' + d.count + '）';
    html += '<option value="' + esc(d.path) + '">' + esc(d.path) + '/' + n + '</option>';
  });
  sel.innerHTML = html;
  sel.value = '__pick__';
}

function onDirPickChange(){
  const v = $('#o_dirPick').value;
  if(v === '__pick__') return;
  if(v === '__default__') $('#o_dir').value = '';
  else $('#o_dir').value = v;
  updateTarget(); renderQueue();
  const d = $('#o_dir').value.trim();
  toast(d ? ('本次上传到 ' + d + '/') : '已切回默认目标目录', 'ok');
  $('#o_dirPick').value = '__pick__';
}

function fileEnterDir(path){ return loadFiles(false, path, $('#fileDeep').checked); }

function fileCrumbs(){
  const parts = (FILES.path || '').split('/').filter(Boolean);
  let crumb = '<a data-fgo="">仓库根目录</a>';
  let acc = '';
  parts.forEach(p => {
    acc = acc ? acc + '/' + p : p;
    crumb += '　/　<a data-fgo="' + esc(acc) + '">' + esc(p) + '</a>';
  });
  $('#fileCrumbs').innerHTML = '<span>分支 <b>' + esc(FILES.branch || curBranch()) + '</b>：</span>' + crumb;
  $('#fileCrumbs').querySelectorAll('[data-fgo]').forEach(el => {
    el.onclick = () => fileEnterDir(el.getAttribute('data-fgo'));
  });
}

function renderFiles(){
  if(!FILES.loaded){
    $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">—</span>'
      + '<span class="nm">点「加载文件列表」把仓库里的文件列出来</span></div>';
    $('#fileCrumbs').textContent = '还没加载 —— 点右边的「加载文件列表」';
    $('#filesInfo').textContent = '';
    updateSelInfo();
    return;
  }
  fileCrumbs();

  const kw = ($('#fileFilter').value || '').trim().toLowerCase();
  const onlyDirs = $('#fileOnlyDirs').checked;
  const hit = (s) => !kw || String(s).toLowerCase().indexOf(kw) >= 0;
  const dList = (FILES.dirs || []).filter(d => hit(d.path));
  // 浅列时文件都在当前层；深列时带上相对路径
  const fList = onlyDirs ? [] : (FILES.files || []).filter(f => hit(FILES.deep ? f.path : f.name));

  const rows = [];
  if(FILES.deep){
    rows.push('<div class="mgridhead"><label><input type="checkbox" id="fileSelAll"> 全选（按当前筛选）</label>'
      + '<span style="flex:1"></span><span>' + fList.length + ' 文件 / ' + dList.length + ' 文件夹</span></div>');
  } else {
    rows.push('<div class="mgridhead"><label><input type="checkbox" id="fileSelAll"> 全选</label>'
      + '<span style="flex:1"></span><span>当前目录 ' + fList.length + ' 文件 / '
      + dList.length + ' 子文件夹</span></div>');
  }

  dList.forEach(d => {
    const n = (d.count === null || d.count === undefined) ? '' : (' · ' + d.count + ' 项');
    rows.push('<div class="frow" data-path="' + esc(d.path) + '">'
      + '<span class="ico2">📁</span>'
      + '<span class="nm" style="cursor:pointer" data-enter="' + esc(d.path) + '">'
      + esc(FILES.deep ? d.path : d.name) + '/<span class="mtag dir">文件夹' + n + '</span></span>'
      + '<span class="ops">'
      + (FILES.deep ? '' : '<button class="btnmini" data-act="enter" data-path="' + esc(d.path) + '">进入</button>')
      + '<button class="btnmini" data-act="target" data-path="' + esc(d.path) + '">上传到此</button>'
      + '<button class="btnmini" data-act="rename" data-kind="dir" data-path="' + esc(d.path) + '">重命名</button>'
      + '<button class="btnmini danger" data-act="del_dir" data-path="' + esc(d.path) + '">删除</button>'
      + '</span></div>');
  });

  fList.forEach(f => {
    const label = FILES.deep ? f.path : f.name;
    rows.push('<div class="frow" data-path="' + esc(f.path) + '">'
      + '<input type="checkbox" class="fchk" data-path="' + esc(f.path) + '"'
      + (SEL.has(f.path) ? ' checked' : '') + '>'
      + '<span class="ico2">📄</span>'
      + '<span class="nm">' + esc(label) + '</span>'
      + '<span class="cnt">' + fmtSize(f.size || 0) + '</span>'
      + '<span class="ops">'
      + '<button class="btnmini" data-act="rename" data-kind="file" data-path="' + esc(f.path) + '">重命名</button>'
      + '<button class="btnmini danger" data-act="del_file" data-path="' + esc(f.path) + '">删除</button>'
      + '</span></div>');
  });

  if(!dList.length && !fList.length){
    rows.push('<div class="row-i"><span class="ico2">∅</span><span class="nm">'
      + esc(kw ? ('没有匹配「' + kw + '」的内容')
               : (FILES.path ? '这个文件夹是空的' : '这个分支里还没有文件')) + '</span></div>');
  }
  $('#filesList').innerHTML = rows.join('');
  $('#filesInfo').textContent = (FILES.res && FILES.res.message) || '';

  const box = $('#filesList');
  box.querySelectorAll('[data-act]').forEach(el => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      rowAction(el.getAttribute('data-act'), el.getAttribute('data-path'), el.getAttribute('data-kind'));
    };
  });
  box.querySelectorAll('[data-enter]').forEach(el => {
    el.onclick = () => fileEnterDir(el.getAttribute('data-enter'));
  });
  box.querySelectorAll('.fchk').forEach(el => {
    el.onchange = () => {
      const p = el.getAttribute('data-path');
      if(el.checked) SEL.add(p); else SEL.delete(p);
      updateSelInfo();
    };
  });
  const sa = $('#fileSelAll');
  if(sa) sa.onchange = () => {
    const paths = fList.map(f => f.path);
    box.querySelectorAll('.fchk').forEach(el => { el.checked = sa.checked; });
    paths.forEach(p => { if(sa.checked) SEL.add(p); else SEL.delete(p); });
    updateSelInfo();
  };
  updateSelInfo();
}

function updateSelInfo(){
  const n = SEL.size;
  const del = $('#btnDelFiles'), ren = $('#btnRename');
  if(del) del.disabled = n === 0;
  if(ren) ren.disabled = n !== 1;
  $('#manageHint').textContent = n ? ('已勾选 ' + n + ' 个文件') : '';
}

function selectedFiles(){ return Array.from(SEL); }

async function manageAction(action, extra, successMsg, silent){
  const tok = $('#c_token').value.trim() || TOKEN;
  const body = Object.assign({action: action, branch: curBranch()}, extra || {});
  const res = await fetch('/api/manage', {
    method: 'POST',
    headers: Object.assign({'Content-Type': 'application/json'},
                           tok ? {'X-GitHub-Token': tok} : {}),
    body: JSON.stringify(body),
  }).then(r => r.json());
  if(!res.ok){
    const msg = res.error || '操作失败';
    if(!silent) toast(msg, 'err');
    const err = new Error(msg); err.status = res.status; err.res = res;
    throw err;
  }
  if(!silent) toast(successMsg || res.message || '操作完成', 'ok');
  return res;
}

function rowAction(act, path, kind){
  if(act === 'enter') return loadFiles(false, path, false);
  if(act === 'target'){
    $('#o_dir').value = path || '';
    updateTarget(); renderQueue();
    toast(path ? ('本次上传到 ' + path + '/') : '已切回默认（仓库根目录）', 'ok');
    return;
  }
  if(act === 'rename') return askRename(path, kind || 'file');
  if(act === 'del_file') return askDeleteFile(path);
  if(act === 'del_dir') return askDeleteDir(path);
}

async function askRename(path, kind){
  const isDir = kind === 'dir';
  const parts = String(path).split('/');
  const base = parts.pop() || '';
  const parent = parts.join('/');
  const vals = await openModal({
    title: '重命名' + (isDir ? '文件夹' : '文件'),
    html: '<p>原路径：<b>' + esc(path) + '</b>' + (isDir ? '/' : '') + '</p>',
    fields: [{name: 'newName',
              label: isDir ? '新文件夹名（含 / 表示顺便换位置）' : '新文件名（含 / 表示顺便移动）',
              value: base, placeholder: base}],
    okText: '重命名',
  });
  if(!vals) return;
  const newName = (vals[0] || '').replace(/^\/+|\/+$/g, '');
  if(!newName){ toast('新名字不能为空', 'err'); return; }
  const dst = newName.indexOf('/') >= 0 ? newName : (parent ? parent + '/' + newName : newName);
  if(dst === path){ toast('新名字和原来一样，没有改动', 'err'); return; }
  await manageAction('rename', {path: path, newPath: dst, kind: kind},
                     '已重命名：' + path + ' → ' + dst);
  // 改名的正是当前所在目录（或它的父级）就回到上一层，免得列表对不上
  if(FILES.path === path || FILES.path.indexOf(path + '/') === 0){
    FILES.path = path.split('/').slice(0, -1).join('/');
  }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteFile(path){
  const ok = await askConfirm({
    title: '删除文件',
    warn: '⚠️ 删除后虽然提交历史里还能翻到，但仓库里就没这个文件了。',
    html: '<p>要删除的文件：<b>' + esc(path) + '</b></p>',
    okText: '确认删除', danger: true,
  });
  if(!ok) return;
  try {
    await manageAction('delete_file', {path: path}, '已删除文件：' + path);
  } catch(e) { /* 已经提示过 */ }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteDir(path){
  const info = (FILES.dirs || []).find(d => d.path === path);
  const n = (info && info.count !== null && info.count !== undefined) ? (info.count + ' 个') : '全部';
  const ok = await askConfirm({
    title: '删除文件夹',
    warn: '⚠️ 会把这个文件夹下的 <b>' + n + '</b> 文件全部删掉，无法撤销。<br>'
      + 'Git 不保存空目录，内容删完目录本身也就没了。',
    html: '<p>要删除的文件夹：<b>' + esc(path) + '/</b></p>',
    fields: [{name: 'confirmPath', label: '防误删：把文件夹路径再完整打一遍',
              value: '', placeholder: path}],
    okText: '确认删除整个文件夹', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '') !== path){
    toast('路径打得不一致，已取消删除（需完整输入 ' + path + '）', 'err');
    return;
  }
  try {
    const res = await manageAction('delete_dir', {path: path}, null);
    toast('已删除文件夹 ' + path + '（' + ((res.deleted || []).length) + ' 个文件）', 'ok');
  } catch(e) { /* 已经提示过 */ }
  // 刚删掉的正是当前所在目录（或它的父级）就回根目录
  if(FILES.path === path || FILES.path.indexOf(path + '/') === 0) FILES.path = '';
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteDirHere(){
  if(!FILES.loaded){ toast('先加载文件列表', 'err'); return; }
  if(!FILES.path){ toast('已经在仓库根目录了。要删某个子文件夹，点它那一行的「删除」', 'err'); return; }
  const target = FILES.path;
  let n = FILES.files.length;
  if(!FILES.deep){
    // 浅列时不知道里面有多少，先诚实地说「不确定」
    n = null;
  }
  const ok = await askConfirm({
    title: '删除当前文件夹',
    warn: '⚠️ 会删掉 <b>' + esc(target) + '/</b> 里面的'
      + (n === null ? '所有文件（会先扫一遍再删，可能稍慢）' : (' <b>' + n + ' 个</b>文件'))
      + '，无法撤销。',
    html: '<p>要删除的文件夹：<b>' + esc(target) + '/</b></p>',
    fields: [{name: 'confirmPath', label: '防误删：把文件夹路径再完整打一遍',
              value: '', placeholder: target}],
    okText: '确认删除整个文件夹', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '') !== target){
    toast('路径打得不一致，已取消删除（需完整输入 ' + target + '）', 'err');
    return;
  }
  const btn = $('#btnDelDir'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '删除中…';
  try {
    const res = await manageAction('delete_dir', {path: target}, null, true);
    toast('已删除文件夹 ' + target + '（' + ((res.deleted || []).length) + ' 个文件）', 'ok');
    FILES.path = '';
  } catch(e){
    toast('删除失败：' + ((e && e.message) || e), 'err');
  }
  btn.disabled = false; btn.textContent = old;
  await loadFiles(true, '', $('#fileDeep').checked);
  loadHistory();
}

async function askDeleteSelected(){
  const sel = selectedFiles();
  if(!sel.length){ toast('先在列表里勾选要删除的文件', 'err'); return; }
  const list = sel.slice(0, 12).map(p => '<br>· ' + esc(p)).join('');
  const more = sel.length > 12 ? ('<br>… 还有 ' + (sel.length - 12) + ' 个') : '';
  const ok = await askConfirm({
    title: '删除选中的 ' + sel.length + ' 个文件',
    warn: '⚠️ 删除后无法撤销。',
    html: '<p>将删除：' + list + more + '</p>',
    okText: '确认删除这 ' + sel.length + ' 个', danger: true,
  });
  if(!ok) return;
  let done = 0; const failed = [];
  const btn = $('#btnDelFiles'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '删除中…';
  for(const p of sel){
    try { await manageAction('delete_file', {path: p}, null, true); done++; }
    catch(e){ failed.push(p + '：' + ((e && e.message) || e)); }
  }
  btn.disabled = false; btn.textContent = old;
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
  if(failed.length) toast('删除了 ' + done + ' 个，失败 ' + failed.length + ' 个：' + failed[0], 'err');
  else toast('已删除 ' + done + ' 个文件', 'ok');
  loadHistory();
}

async function askRenameSelected(){
  const sel = selectedFiles();
  if(sel.length !== 1){ toast('重命名一次只针对一个文件，请刚好勾选 1 个', 'err'); return; }
  await askRename(sel[0], 'file');
}

async function askMkdir(){
  const vals = await openModal({
    title: '新建文件夹',
    html: '<p>Git 存不了空目录，所以会放一个 <b>.gitkeep</b> 占位，这样子目录才留得住。</p>',
    fields: [{name: 'path', label: '文件夹路径（可带层级）', value: '', placeholder: 'assets/2026/秋'}],
    okText: '新建',
  });
  if(!vals) return;
  const p = (vals[0] || '').replace(/^\/+|\/+$/g, '');
  if(!p){ toast('路径不能为空', 'err'); return; }
  try {
    await manageAction('mkdir', {path: p}, '已新建文件夹：' + p);
  } catch(e) { /* 已经提示过 */ }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askClearRepo(){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  if(!owner || !repo){ toast('先在配置里填好仓库地址', 'err'); return; }
  const ok = await askConfirm({
    title: '清空整个仓库',
    warn: '⚠️⚠️ 这会删掉 <b>' + esc(owner + '/' + repo) + '</b> 里 <b>' + esc(curBranch() || liveBranch() || '默认分支')
      + '</b> 分支上的<b>全部文件和文件夹</b>，仓库会变成空的。',
    html: '<p>真要继续的话，请把 <b>CLEAR</b> 打进下面的框里：</p>',
    fields: [{name: 'confirm', label: '输入 CLEAR 确认', value: '', placeholder: 'CLEAR'}],
    okText: '我确定，清空仓库', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '').toUpperCase() !== 'CLEAR'){
    toast('没有输入 CLEAR，已取消', 'err');
    return;
  }
  try {
    const res = await manageAction('clear', {confirm: 'CLEAR'}, null);
    toast(res.message || '已清空仓库', 'ok');
  } catch(e) { /* 已经提示过 */ }
  FILES.path = '';
  await loadFiles(true, '', $('#fileDeep').checked);
  loadHistory();
}

/* ---------- 事件绑定 ---------- */
const drop = $('#drop');
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); e.stopPropagation(); drop.classList.add('hot');
}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); e.stopPropagation();
  if(ev === 'dragleave' && e.relatedTarget && drop.contains(e.relatedTarget)) return;
  drop.classList.remove('hot');
}));
drop.addEventListener('drop', async e => {
  const r = await itemsFromDataTransfer(e.dataTransfer);
  addItems(r.items, r.empties);
});
drop.addEventListener('click', () => $('#fileInput').click());
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', async e => {
  e.preventDefault();
  if(!e.dataTransfer) return;
  const r = await itemsFromDataTransfer(e.dataTransfer);
  addItems(r.items, r.empties);
});
$('#fileInput').addEventListener('change', e => {
  const files = Array.from(e.target.files || []);
  addItems(files.map(f => ({file: f, rel: f.webkitRelativePath || ''})));
  e.target.value = '';
});
$('#dirInput').addEventListener('change', e => {
  const files = Array.from(e.target.files || []);
  addItems(files.map(f => ({file: f, rel: f.webkitRelativePath || f.name})));
  e.target.value = '';
});
$('#btnPick').addEventListener('click', () => $('#fileInput').click());
$('#btnPickDir').addEventListener('click', () => $('#dirInput').click());
$('#c_repoUrl').addEventListener('input', debouncedRepoUrl);
$('#c_repoUrl').addEventListener('keydown', e => {
  if(e.key === 'Enter'){ clearTimeout(URL_TIMER); applyRepoUrl({loadBranches: hasToken()}); }
});
$('#btnLoadBranches').addEventListener('click', () => loadBranches(false));
$('#c_branch').addEventListener('input', updateTarget);
$('#c_branch').addEventListener('change', () => {
  if(TREE.loaded) loadTree(TREE.path, true);
  if(FILES.loaded) loadFiles(true);
});
$('#btnTreeRoot').addEventListener('click', () => loadTree(''));
$('#btnTreeReload').addEventListener('click', () => loadTree(TREE.path));
$('#treeFilter').addEventListener('input', renderTree);
$('#btnTreeAsDefault').addEventListener('click', setTreeAsDefault);
$('#btnTreeOpen').addEventListener('click', openTreeOnGitHub);
$('#btnBrowseDir').addEventListener('click', () => {
  $('#cfgBox').open = true;
  loadTree(TREE.loaded ? TREE.path : '', false);
  const box = $('#treeList');
  if(box && box.scrollIntoView) box.scrollIntoView({block: 'center'});
});
$('#c_owner').addEventListener('blur', () => { if(hasToken() && $('#c_repo').value.trim()) loadBranches(true); });
$('#c_repo').addEventListener('blur', () => { if(hasToken() && $('#c_owner').value.trim()) loadBranches(true); });
$('#btnStart').addEventListener('click', startUpload);
$('#btnClear').addEventListener('click', () => { QUEUE = []; renderQueue(); });
$('#btnSave').addEventListener('click', () => saveConfig());
$('#btnClearToken').addEventListener('click', () => saveConfig({clearToken: true}));
$('#o_dir').addEventListener('input', () => { updateTarget(); renderQueue(); });
$('#o_keep').addEventListener('change', () => renderQueue());
$('#btnOpenRepo').addEventListener('click', () => {
  const dir = ($('#o_dir').value.trim() || CFG.targetDir || '').replace(/\{(\w+)\}/g, (m,k) => {
    const d = new Date();
    const map = {date: d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()),
      yyyy: String(d.getFullYear()), mm: pad(d.getMonth()+1), dd: pad(d.getDate()),
      time: pad(d.getHours())+pad(d.getMinutes())+pad(d.getSeconds())};
    return (k in map) ? map[k] : m;
  }).split('/').filter(Boolean).slice(0,1).join('/');
  const base = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
  const br = liveBranch();
  // 分支还没认出来时退到仓库首页，别拼一个 /tree/main/... 的死链
  window.open(br ? (base + '/tree/' + encodeURI(br) + '/' + dir) : base, '_blank');
});
$('#btnTest').addEventListener('click', async () => {
  $('#btnTest').disabled = true; $('#btnTest').textContent = '测试中…';
  try {
    const tok = $('#c_token').value.trim() || TOKEN;
    const qs = new URLSearchParams({owner: $('#c_owner').value.trim(), repo: $('#c_repo').value.trim()});
    const ab = $('#c_apiBase').value.trim();
    if(ab) qs.set('apiBase', ab);
    const res = await fetch('/api/test?' + qs.toString(),
      {method:'POST', headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(res.ok){
      toast(res.message, 'ok');
      if(res.defaultBranch && !$('#c_branch').value.trim()) $('#c_branch').value = res.defaultBranch;
      await saveConfig();            // 连接成功顺手存下来，省得再点一次
    } else {
      toast('连接失败：' + (res.error || ''), 'err');
    }
  } catch(e){ toast('连接失败', 'err'); }
  $('#btnTest').disabled = false; $('#btnTest').textContent = '测试连接';
});

/* ---------- 仓库文件管理：绑定 ---------- */
$('#btnFilesLoad').addEventListener('click', () => loadFiles(false, FILES.path || '', $('#fileDeep').checked));
$('#fileFilter').addEventListener('input', renderFiles);
$('#fileOnlyDirs').addEventListener('change', renderFiles);
$('#fileDeep').addEventListener('change', () => {
  if(FILES.loaded) loadFiles(false, FILES.path || '', $('#fileDeep').checked);
});
$('#btnDelFiles').addEventListener('click', askDeleteSelected);
$('#btnRename').addEventListener('click', askRenameSelected);
$('#btnMkdir').addEventListener('click', askMkdir);
$('#btnDelDir').addEventListener('click', askDeleteDirHere);
$('#btnClearRepo').addEventListener('click', askClearRepo);
$('#o_dirPick').addEventListener('change', onDirPickChange);
$('#modalOk').addEventListener('click', modalOk);
$('#modalCancel').addEventListener('click', modalCancel);
$('#modal').addEventListener('click', e => { if(e.target === $('#modal')) modalCancel(); });
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && MODAL_CTX) modalCancel();
  else if(e.key === 'Enter' && MODAL_CTX) modalOk();
});
updateSelInfo();

loadState().catch(e => toast('无法连接本地服务：' + e, 'err'));
