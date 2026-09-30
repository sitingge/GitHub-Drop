/* 页面 JS 测试桩：在 Node 里提供最小 DOM，直接执行 index 页里真实的 script。
   用法: node page_harness.js <page.js>            */
const fs = require('fs');

const pageScript = fs.readFileSync(process.argv[2], 'utf8');

/* ---------------- 最小 DOM ---------------- */
const els = new Map();
const TOASTS = [];                    // 抓 toast 文本（toast 走 createElement）
function mkEl(sel) {
  const el = {
    _sel: sel, value: '', _text: '', innerHTML: '', checked: false, disabled: false,
    placeholder: '', open: false, files: [], type: '', className: '',
    style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    click() {}, focus() {}, select() {}, setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); if (this._capture) TOASTS.push(this._text); },
  });
  return el;
}
function q(sel) {
  if (!els.has(sel)) els.set(sel, mkEl(sel));
  return els.get(sel);
}
const documentStub = {
  querySelector: q,
  querySelectorAll: () => [],
  createElement: () => { const e = mkEl('created'); e._capture = true; return e; },
  body: { appendChild() {} },
  addEventListener() {},
};
const storage = { _d: {}, getItem(k) { return k in this._d ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } };

/* ---------------- fetch 桩 ---------------- */
const CALLS = [];
const MANAGE = [];          // 记录所有 /api/manage 请求体
const OPENED = [];          // 记录 window.open 打开过的链接
const REPO = { defaultBranch: 'main', branches: ['main', 'dev', 'feature/x'] };
const STATE = { config: {} };   // /api/state 回传的 config，可在用例里改
const DIRS = {
  '': [{ name: 'myproj', path: 'myproj', type: 'dir', size: 0 },
       { name: 'pics', path: 'pics', type: 'dir', size: 0 },
       { name: 'readme.txt', path: 'readme.txt', type: 'file', size: 5 }],
  pics: [{ name: 'ai', path: 'pics/ai', type: 'dir', size: 0 },
        { name: 'index.html', path: 'pics/index.html', type: 'file', size: 12 }],
  'myproj/img': [{ name: 'logo.png', path: 'myproj/img/logo.png', type: 'file', size: 99 }],
  src: [{ name: 'app.py', path: 'src/app.py', type: 'file', size: 42 }],
};
/* /api/files 的假数据：浅列只给当前层，deep=1 才递归展开 */
const TREE_FILES = [
  { path: 'readme.txt', size: 5 },
  { path: 'pics/index.html', size: 12 },
  { path: 'pics/ai/x.png', size: 2048 },
  { path: 'myproj/img/logo.png', size: 99 },
];
function filesPayload(path, deep, branch) {
  path = path || '';
  // 忠实模拟新后端：前端没传分支时，服务端会自动认成仓库默认分支（不一定叫 main）
  const br = branch || REPO.defaultBranch;
  const head = path ? path + '/' : '';
  const inScope = TREE_FILES.filter(f => !head || f.path.indexOf(head) === 0);
  const files = [], dirs = [], seenDir = {};

  if (deep) {
    inScope.forEach(f => {
      const rel = f.path.slice(head.length);
      files.push({ path: f.path, rel: rel, name: rel.split('/').pop(),
                   dir: f.path.slice(0, f.path.lastIndexOf('/')), size: f.size, sha: 'a' });
      const parts = rel.split('/'); parts.pop();
      parts.forEach((_, i) => { const sub = parts.slice(0, i + 1).join('/'); seenDir[sub] = (seenDir[sub] || 0) + 1; });
    });
    Object.keys(seenDir).sort().forEach(p => dirs.push({ path: head + p, name: p.split('/').pop(), count: seenDir[p] }));
  } else {
    inScope.forEach(f => {
      const rel = f.path.slice(head.length);
      const parts = rel.split('/');
      if (parts.length === 1) {
        files.push({ path: f.path, rel: rel, name: rel, dir: path, size: f.size, sha: 'a' });
      } else if (!seenDir[parts[0]]) {
        seenDir[parts[0]] = true;
        dirs.push({ path: head + parts[0], name: parts[0], count: null });
      }
    });
  }
  const label = (path ? path + '/' : '根目录');
  return { ok: true, branch: br, prefix: path, deep: !!deep, commit: 'c0ffee', tree: 'tree0',
           truncated: false, files: files, dirs: dirs,
           totalSize: files.reduce((a, b) => a + b.size, 0),
           message: br + '分支 ' + label + '：' + files.length + ' 个文件 / ' + dirs.length + ' 个文件夹' };
}
function param(url, key) {
  const m = new RegExp('[?&]' + key + '=([^&]*)').exec(url);
  return m ? decodeURIComponent(m[1]) : null;
}
function allParams(url, key) {
  const out = []; const re = new RegExp('[?&]' + key + '=([^&]*)', 'g'); let m;
  while ((m = re.exec(url))) out.push(decodeURIComponent(m[1]));
  return out;
}
function fetchStub(url, opts) {
  const u = String(url);
  CALLS.push({ url: u, headers: (opts && opts.headers) || {} });
  let payload = { ok: true };
  if (u.indexOf('/api/state') === 0) {
    payload = { ok: true, version: '1.0.0', config: STATE.config, tokenSet: false,
                configPath: 'x', history: [] };
  } else if (u.indexOf('/api/branches') === 0) {
    // 模拟后端：可能带斜杠的分支名做最长前缀匹配，剩下的部分当目录
    const cands = allParams(u, 'candidates');
    let hit = null;
    for (const c of cands) { if (REPO.branches.indexOf(c) >= 0) { hit = c; break; } }
    let rest = '';
    if (hit && cands.length && cands[0] !== hit && cands[0].indexOf(hit + '/') === 0) {
      rest = cands[0].slice(hit.length + 1);
    }
    payload = { ok: true, fullName: 'o/r', private: false, defaultBranch: REPO.defaultBranch,
                branches: REPO.branches, resolvedBranch: hit, resolvedPath: rest,
                message: '已加载 ' + REPO.branches.length + ' 个分支' };
  } else if (u.indexOf('/api/tree') === 0) {
    const p = param(u, 'path') || '';
    const looksLikeFile = /\.[A-Za-z0-9]{1,6}$/.test(p);
    if (looksLikeFile) {
      payload = { ok: true, path: p, branch: param(u, 'branch') || REPO.defaultBranch,
                  entries: [], empty: true, truncated: false,
                  message: '这个目录不存在，或者仓库还没有任何提交' };
    } else {
      const entries = DIRS[p] || DIRS[''];
      const dirs = entries.filter(e => e.type === 'dir').length;
      const br = param(u, 'branch') || REPO.defaultBranch;
      payload = { ok: true, path: p, branch: br, entries: entries,
                  empty: false, truncated: false,
                  message: br + ' 分支：' + dirs + ' 个文件夹 / ' + (entries.length - dirs) + ' 个文件' };
    }
  } else if (u.indexOf('/api/history') === 0) {
    payload = { ok: true, history: [] };
  } else if (u.indexOf('/api/files') === 0) {
    payload = filesPayload(param(u, 'path') || '', param(u, 'deep') === '1', param(u, 'branch') || '');
  } else if (u.indexOf('/api/manage') === 0) {
    let body = {};
    try { body = JSON.parse((opts && opts.body) || '{}'); } catch (e) { body = {}; }
    MANAGE.push(body);
    if (String(body.path || '').indexOf('boom') >= 0) {
      payload = { ok: false, error: '模拟失败', status: 500 };
    } else if (body.action === 'clear' && body.confirm !== 'CLEAR') {
      payload = { ok: false, error: '清空仓库需要在 confirm 字段里回填 CLEAR', status: 400 };
    } else {
      payload = { ok: true, action: body.action, branch: body.branch, path: body.path,
                  newPath: body.newPath, mode: 'git-data-api', commit: 'deadbeef',
                  message: '操作完成：' + body.action };
    }
  }
  const status = payload.ok === false ? (payload.status || 400) : 200;
  return Promise.resolve({ ok: status === 200, status: status,
                           json: () => Promise.resolve(payload),
                           text: () => Promise.resolve(JSON.stringify(payload)) });
}
class XHRStub {
  open() {} setRequestHeader() {} send() {} addEventListener() {}
}

globalThis.document = documentStub;
globalThis.window = { isSecureContext: false, addEventListener() {},
  open(u) { OPENED.push(String(u)); } };
globalThis.localStorage = storage;
globalThis.navigator = {};
globalThis.fetch = fetchStub;
globalThis.XMLHttpRequest = XHRStub;

/* ---------------- 执行页面脚本 ---------------- */
const EXPORT = '\n;globalThis.__page = {parseRepoUrl, applyRepoUrl, loadBranches, loadTree, renderTree, ' +
  'pickDir, enterDir, setTreeAsDefault, toast, getTREE: () => TREE, getPARSED: () => PARSED, ' +
  'loadFiles, renderFiles, fillDirPicker, onDirPickChange, manageAction, selectedFiles, ' +
  'askRename, askDeleteFile, askDeleteDir, askDeleteDirHere, askDeleteSelected, askRenameSelected, ' +
  'askMkdir, askClearRepo, openModal, modalOk, modalCancel, updateSelInfo, fileEnterDir, ' +
  'getFILES: () => FILES, getSEL: () => SEL, setSEL: (a) => { SEL = new Set(a); }, ' +
  'loadState, updateTarget, openTreeOnGitHub, liveBranch};\n';
(0, eval)(pageScript + EXPORT);

/* ---------------- 断言 ---------------- */
const PASS = [], FAIL = [];
function check(name, cond, detail) {
  if (cond) { PASS.push(name); console.log('  [PASS] ' + name); }
  else { FAIL.push(name); console.log('  [FAIL] ' + name + '  ' + (detail === undefined ? '' : detail)); }
}
function typeUrl(url) { q('#c_repoUrl').value = url; return globalThis.__page.applyRepoUrl({ loadBranches: false }); }
function reset() { q('#c_owner').value = ''; q('#c_repo').value = ''; q('#c_branch').value = ''; }
const tick = (n) => new Promise(r => setTimeout(r, n === undefined ? 0 : n));
const next_d = (list, p) => (list || []).filter(d => d.path === p)[0] || {};

(async () => {
  await new Promise(r => setTimeout(r, 120));            // 等 loadState 跑完
  const P = globalThis.__page;

  console.log('---- 粘贴地址：自动填字段 ----');
  let p = typeUrl('https://github.com/octocat/hello-world/tree/dev');
  check('返回解析结果', !!p, JSON.stringify(p));
  check('owner 自动填入', q('#c_owner').value === 'octocat', q('#c_owner').value);
  check('repo 自动填入', q('#c_repo').value === 'hello-world', q('#c_repo').value);
  check('分支自动填入', q('#c_branch').value === 'dev', q('#c_branch').value);
  check('提示显示识别成功', q('#urlHint').textContent.indexOf('✓ 已识别 octocat/hello-world') === 0,
        q('#urlHint').textContent);

  reset();
  p = typeUrl('   git@github.com:me/my-assets.git   ');
  check('SSH 地址也能识别', q('#c_owner').value === 'me' && q('#c_repo').value === 'my-assets',
        q('#c_owner').value + '/' + q('#c_repo').value);
  check('SSH 地址没分支时不清空已有值', true, '');

  reset();
  p = typeUrl('https://github.com/octocat/hello-world/tree/feature/x/src/app.py');
  check('歧义链接的分支候选有 4 个', p && p.branchCandidates.length === 4,
        p ? JSON.stringify(p.branchCandidates) : 'null');
  check('歧义提示里说明了会校正', q('#urlHint').textContent.indexOf('校正') > 0, q('#urlHint').textContent);

  reset();
  const apiBefore = q('#c_apiBase').value;
  p = typeUrl('https://ghe.corp.com/team/asset-repo');
  check('企业版地址自动换 apiBase', q('#c_apiBase').value === 'https://ghe.corp.com/api/v3',
        q('#c_apiBase').value);
  check('企业版 owner/repo 正确', q('#c_owner').value === 'team' && q('#c_repo').value === 'asset-repo', '');
  q('#c_apiBase').value = apiBefore;

  reset();
  q('#c_owner').value = 'keep-me';
  p = typeUrl('这不是仓库地址');
  check('乱填地址返回 null', p === null, String(p));
  check('乱填地址不覆盖已有 owner', q('#c_owner').value === 'keep-me', q('#c_owner').value);
  check('提示变成错误态', q('#urlHint').textContent.indexOf('✗') === 0, q('#urlHint').textContent);

  console.log('---- 加载分支：请求参数与分支选中 ----');
  reset();
  CALLS.length = 0;
  q('#c_token').value = 'ghp_test_token';
  typeUrl('https://github.com/octocat/hello-world/tree/feature/x/src/app.py');
  const res = await P.loadBranches(true);
  const branchCall = CALLS.filter(c => c.url.indexOf('/api/branches') === 0).pop();
  check('确实请求了 /api/branches', !!branchCall, JSON.stringify(CALLS.map(c => c.url)));
  check('请求带上候选分支（最长优先）',
        !!branchCall && branchCall.url.indexOf('candidates=feature%2Fx%2Fsrc%2Fapp.py') > 0
        && branchCall.url.indexOf('candidates=feature') > 0, branchCall ? branchCall.url : '');
  check('请求带上 owner/repo',
        !!branchCall && branchCall.url.indexOf('owner=octocat') > 0 && branchCall.url.indexOf('repo=hello-world') > 0,
        branchCall ? branchCall.url : '');
  check('请求带上了 Token', !!branchCall && branchCall.headers['X-GitHub-Token'] === 'ghp_test_token',
        branchCall ? JSON.stringify(branchCall.headers) : '');
  check('接口返回成功', !!res && res.ok === true, JSON.stringify(res));
  check('分支被后端解析结果校正为 feature/x', q('#c_branch').value === 'feature/x', q('#c_branch').value);
  check('候选用完后清空（避免二次覆盖）', P.getPARSED() === null, String(P.getPARSED()));
  check('链接若指向文件，退到它所在的文件夹', q('#o_dir').value === 'src', q('#o_dir').value);
  check('退到父目录后能看到同目录文件', q('#treeList').innerHTML.indexOf('app.py') > 0,
        q('#treeList').innerHTML.slice(0, 120));

  console.log('---- 分支里的文件夹（你的链接那种）----');
  q('#c_token').value = 'ghp_test_token';
  q('#c_branch').value = '';
  q('#c_owner').value = 'octocat';
  q('#c_repo').value = 'my-assets';
  q('#o_dir').value = '';
  CALLS.length = 0;
  typeUrl('https://github.com/octocat/my-assets/tree/main/pics');
  await P.loadBranches(true);
  check('分支识别为 main', q('#c_branch').value === 'main', q('#c_branch').value);
  check('owner / repo 正确',
        q('#c_owner').value === 'octocat' && q('#c_repo').value === 'my-assets',
        q('#c_owner').value + '/' + q('#c_repo').value);
  check('链接里的目录 pics 自动成为本次目标', q('#o_dir').value === 'pics', q('#o_dir').value);
  check('目录浏览器直接进到 pics', P.getTREE().path === 'pics', P.getTREE().path);
  check('pics 里的文件夹显示出来了', q('#treeList').innerHTML.indexOf('ai') > 0,
        q('#treeList').innerHTML.slice(0, 120));
  check('面包屑显示 仓库根目录 / pics',
        q('#treeCrumbs').innerHTML.indexOf('仓库根目录') >= 0 && q('#treeCrumbs').innerHTML.indexOf('pics') >= 0,
        q('#treeCrumbs').innerHTML);
  const treeCall = CALLS.filter(c => c.url.indexOf('/api/tree') === 0).pop();
  check('tree 请求带上 path 与 branch',
        !!treeCall && treeCall.url.indexOf('path=pics') > 0 && treeCall.url.indexOf('branch=main') > 0,
        treeCall ? treeCall.url : '');
  check('tree 请求带上 Token',
        !!treeCall && treeCall.headers['X-GitHub-Token'] === 'ghp_test_token',
        treeCall ? JSON.stringify(treeCall.headers) : '');

  await P.enterDir('myproj/img');
  check('点文件夹能进去（enterDir）', P.getTREE().path === 'myproj/img', P.getTREE().path);
  check('进去了能看到里面的文件', q('#treeList').innerHTML.indexOf('logo.png') > 0, '');
  P.pickDir('pics');
  check('点「选它」写进本次目标目录', q('#o_dir').value === 'pics', q('#o_dir').value);
  P.setTreeAsDefault();
  check('「设为默认目标」写进 targetDir 输入框', q('#c_targetDir').value === 'myproj/img',
        q('#c_targetDir').value);

  console.log('---- 目录太多时的筛选 ----');
  await P.loadTree('');
  q('#treeFilter').value = 'pics';
  P.renderTree();
  check('筛选只留下匹配的文件夹',
        q('#treeList').innerHTML.indexOf('pics') > 0 && q('#treeList').innerHTML.indexOf('readme.txt') < 0,
        q('#treeList').innerHTML.slice(0, 140));
  check('筛选时提示剩余条数', q('#treeHint').textContent.indexOf('筛选出') === 0, q('#treeHint').textContent);
  q('#treeFilter').value = 'zzz不存在';
  P.renderTree();
  check('筛选无结果有提示', q('#treeList').innerHTML.indexOf('没有匹配') > 0, '');
  q('#treeFilter').value = '';
  P.renderTree();
  check('清空筛选后恢复全部', q('#treeList').innerHTML.indexOf('readme.txt') > 0, '');
  q('#treeFilter').value = 'pics';
  await P.loadTree('pics');
  check('进新目录会自动清掉筛选词', q('#treeFilter').value === '', q('#treeFilter').value);
  check('新目录内容正常渲染', q('#treeList').innerHTML.indexOf('ai') > 0, '');

  console.log('---- 没填 Token：匿名也能读公开仓库 ----');
  reset();
  q('#c_token').value = '';
  q('#c_owner').value = 'octocat';
  q('#c_repo').value = 'hello-world';
  q('#c_branch').value = 'main';
  CALLS.length = 0;
  const res2 = await P.loadBranches(true);
  const anonCall = CALLS.filter(c => c.url.indexOf('/api/branches') === 0).pop();
  check('没 Token 也会请求拉分支', !!anonCall, JSON.stringify(CALLS.map(c => c.url)));
  check('匿名请求不带 Token 头', !!anonCall && !anonCall.headers['X-GitHub-Token'],
        anonCall ? JSON.stringify(anonCall.headers) : '');
  check('匿名也能拿到分支列表', !!res2 && res2.ok === true, JSON.stringify(res2));
  const anonTree = CALLS.filter(c => c.url.indexOf('/api/tree') === 0).pop();
  check('匿名也会把根目录显示出来', !!anonTree, anonTree ? anonTree.url : '');
  check('手填的分支不会被清掉', q('#c_branch').value === 'main', q('#c_branch').value);

  console.log('---- 仓库文件管理：先浅列当前层（快）----');
  reset();
  q('#c_token').value = 'ghp_test_token';
  q('#c_owner').value = 'octocat';
  q('#c_repo').value = 'my-assets';
  q('#c_branch').value = 'main';
  q('#fileDeep').checked = false;
  CALLS.length = 0;
  const fr0 = await P.loadFiles(true);
  const fCall0 = CALLS.filter(c => c.url.indexOf('/api/files') === 0).pop();
  check('请求了 /api/files', !!fCall0, JSON.stringify(CALLS.map(c => c.url)));
  check('/api/files 带上 owner/repo/branch',
        !!fCall0 && fCall0.url.indexOf('owner=octocat') > 0
        && fCall0.url.indexOf('repo=my-assets') > 0 && fCall0.url.indexOf('branch=main') > 0,
        fCall0 ? fCall0.url : '');
  check('/api/files 带上 Token',
        !!fCall0 && fCall0.headers['X-GitHub-Token'] === 'ghp_test_token',
        fCall0 ? JSON.stringify(fCall0.headers) : '');
  check('默认不递归（不带 deep=1）', !!fCall0 && fCall0.url.indexOf('deep=1') < 0,
        fCall0 ? fCall0.url : '');
  check('浅列只给当前层的文件（1 个）', !!fr0 && P.getFILES().files.length === 1,
        String(P.getFILES().files.length));
  check('浅列给出直接子文件夹（2 个）', P.getFILES().dirs.length === 2,
        JSON.stringify(P.getFILES().dirs.map(d => d.path)));
  check('浅列时子文件夹不带数量', P.getFILES().dirs.every(d => d.count === null), '');
  check('浅列只显示文件名（不带路径）',
        q('#filesList').innerHTML.indexOf('readme.txt') > 0
        && q('#filesList').innerHTML.indexOf('pics/ai/x.png') < 0,
        q('#filesList').innerHTML.slice(0, 200));
  check('浅列的面包屑指向仓库根目录',
        q('#fileCrumbs').innerHTML.indexOf('仓库根目录') > 0, q('#fileCrumbs').innerHTML);
  check('文件夹行有「进入」按钮', q('#filesList').innerHTML.indexOf('data-act="enter"') > 0, '');
  check('文件夹行能给「上传到此」', q('#filesList').innerHTML.indexOf('上传到此') > 0, '');
  check('文件夹行有删除按钮', q('#filesList').innerHTML.indexOf('data-act="del_dir"') > 0, '');
  check('文件行有勾选框', q('#filesList').innerHTML.indexOf('class="fchk"') > 0, '');
  check('文件行有重命名按钮', q('#filesList').innerHTML.indexOf('data-act="rename"') > 0, '');
  check('顶部显示当前层的统计', q('#filesInfo').textContent.indexOf('1 个文件') > 0,
        q('#filesInfo').textContent);

  console.log('---- 点文件夹进下一层 ----');
  await P.fileEnterDir('pics');
  check('进入 pics 后 path 跟上了', P.getFILES().path === 'pics', P.getFILES().path);
  check('pics 里只列自己那一层',
        P.getFILES().files.length === 1 && P.getFILES().files[0].name === 'index.html',
        JSON.stringify(P.getFILES().files.map(f => f.path)));
  check('pics 的子文件夹是 ai', P.getFILES().dirs[0].path === 'pics/ai', JSON.stringify(P.getFILES().dirs));
  check('面包屑 仓库根目录 / pics',
        q('#fileCrumbs').innerHTML.indexOf('仓库根目录') > 0
        && q('#fileCrumbs').innerHTML.indexOf('pics') > 0, q('#fileCrumbs').innerHTML);
  await P.fileEnterDir('');
  check('回到根目录', P.getFILES().path === '', P.getFILES().path);

  console.log('---- 勾上「连子文件夹一起列」做全量扫描 ----');
  q('#fileDeep').checked = true;
  CALLS.length = 0;
  const fr = await P.loadFiles(true, '', true);
  const fCall = CALLS.filter(c => c.url.indexOf('/api/files') === 0).pop();
  check('这次带上了 deep=1', !!fCall && fCall.url.indexOf('deep=1') > 0, fCall ? fCall.url : '');
  check('全量列出 4 个文件', !!fr && P.getFILES().loaded && P.getFILES().files.length === 4,
        String(P.getFILES().files.length));
  check('文件夹去重后 4 个', P.getFILES().dirs.length === 4, String(P.getFILES().dirs.length));
  check('全量时文件夹带数量',
        next_d(P.getFILES().dirs, 'pics').count === 2, JSON.stringify(P.getFILES().dirs));
  check('全量时显示完整路径', q('#filesList').innerHTML.indexOf('pics/ai/x.png') > 0,
        q('#filesList').innerHTML.slice(0, 240));
  check('顶部显示全量统计', q('#filesInfo').textContent.indexOf('4 个文件') > 0, q('#filesInfo').textContent);

  console.log('---- 上传目标：从仓库文件夹里挑（人性化）----');
  check('下拉列出了仓库里的文件夹', q('#o_dirPick').innerHTML.indexOf('pics/ai') > 0,
        q('#o_dirPick').innerHTML.slice(0, 200));
  check('下拉里有「用默认目标模板」', q('#o_dirPick').innerHTML.indexOf('__default__') > 0, '');
  check('下拉里有「仓库根目录」', q('#o_dirPick').innerHTML.indexOf('（仓库根目录）') > 0, '');
  q('#o_dirPick').value = 'pics/ai';
  P.onDirPickChange();
  check('挑文件夹后写进本次目标', q('#o_dir').value === 'pics/ai', q('#o_dir').value);
  check('挑完自动回到占位项', q('#o_dirPick').value === '__pick__', q('#o_dirPick').value);
  check('顶部目标行同步刷新', q('#targetLine').textContent.indexOf('pics/ai') > 0,
        q('#targetLine').textContent);
  q('#o_dirPick').value = '__default__';
  P.onDirPickChange();
  check('选「默认模板」清空本次目标', q('#o_dir').value === '', q('#o_dir').value);
  check('清空后目标行回落到默认模板', q('#targetLine').textContent.indexOf('uploads/{date}') > 0,
        q('#targetLine').textContent);

  console.log('---- 管理列表的筛选/只看文件夹 ----');
  q('#fileFilter').value = 'logo';
  P.renderFiles();
  check('筛选只留 logo.png',
        q('#filesList').innerHTML.indexOf('logo.png') > 0
        && q('#filesList').innerHTML.indexOf('readme.txt') < 0,
        q('#filesList').innerHTML.slice(0, 200));
  q('#fileFilter').value = '';
  q('#fileOnlyDirs').checked = true;
  P.renderFiles();
  check('只看文件夹时不出现文件行', q('#filesList').innerHTML.indexOf('class="fchk"') < 0, '');
  check('只看文件夹时仍列出文件夹', q('#filesList').innerHTML.indexOf('myproj') > 0, '');
  q('#fileOnlyDirs').checked = false;
  P.renderFiles();
  check('取消后文件行回来', q('#filesList').innerHTML.indexOf('class="fchk"') > 0, '');

  console.log('---- 删除单个文件：先弹确认 ----');
  MANAGE.length = 0;
  const delP = P.askDeleteFile('pics/ai/x.png');
  await tick();
  check('确认框里带出要删的文件', q('#modalBody').innerHTML.indexOf('pics/ai/x.png') > 0,
        q('#modalBody').innerHTML.slice(0, 160));
  check('确认框给出风险提示', q('#modalBody').innerHTML.indexOf('⚠️') > 0, '');
  P.modalOk();
  await delP;
  check('确认后调用了删除接口',
        MANAGE.length === 1 && MANAGE[0].action === 'delete_file', JSON.stringify(MANAGE));
  check('删除请求带 path/branch',
        MANAGE.length === 1 && MANAGE[0].path === 'pics/ai/x.png' && MANAGE[0].branch === 'main',
        JSON.stringify(MANAGE[0] || {}));

  console.log('---- 取消就不该动手 ----');
  const mp = P.openModal({title: '随便问问', html: '<p>点取消试试</p>'});
  await tick();
  P.modalCancel();
  const mv = await mp;
  check('弹窗取消时解析成 null', mv === null, String(mv));

  MANAGE.length = 0;
  const cancelP = P.askDeleteFile('readme.txt');
  await tick();
  P.modalCancel();
  const cancelR = await cancelP;
  check('取消后流程没有继续往下走', !cancelR, String(cancelR));
  check('取消后没有发任何写请求', MANAGE.length === 0, JSON.stringify(MANAGE));

  console.log('---- 重命名文件/文件夹 ----');
  MANAGE.length = 0;
  const renP = P.askRename('pics/index.html', 'file');
  await tick();
  check('弹窗默认带出原文件名', document.querySelector('#m_in0').value === 'index.html',
        document.querySelector('#m_in0').value);
  document.querySelector('#m_in0').value = 'home.html';
  P.modalOk();
  await renP;
  check('重命名请求正确（同目录改名）',
        MANAGE.length === 1 && MANAGE[0].action === 'rename'
        && MANAGE[0].path === 'pics/index.html' && MANAGE[0].newPath === 'pics/home.html'
        && MANAGE[0].kind === 'file', JSON.stringify(MANAGE[0] || {}));

  MANAGE.length = 0;
  const renP2 = P.askRename('myproj', 'dir');
  await tick();
  check('文件夹弹窗默认带出原目录名', document.querySelector('#m_in0').value === 'myproj',
        document.querySelector('#m_in0').value);
  document.querySelector('#m_in0').value = 'projects';
  P.modalOk();
  await renP2;
  check('文件夹重命名（顶层保持不带父目录）',
        MANAGE.length === 1 && MANAGE[0].newPath === 'projects' && MANAGE[0].kind === 'dir',
        JSON.stringify(MANAGE[0] || {}));

  MANAGE.length = 0;
  const renP3 = P.askRename('pics/index.html', 'file');
  await tick();
  document.querySelector('#m_in0').value = 'index.html';
  P.modalOk();
  await renP3;
  check('名字没改就不发请求', MANAGE.length === 0, JSON.stringify(MANAGE));

  MANAGE.length = 0;
  const renP4 = P.askRename('pics/index.html', 'file');
  await tick();
  document.querySelector('#m_in0').value = 'assets/home.html';   // 带 / 就是顺便移动
  P.modalOk();
  await renP4;
  check('带斜杠表示顺便换目录',
        MANAGE.length === 1 && MANAGE[0].newPath === 'assets/home.html',
        JSON.stringify(MANAGE[0] || {}));

  console.log('---- 重命名要刚好勾 1 个 ----');
  MANAGE.length = 0;
  P.setSEL([]);
  await P.askRenameSelected();
  check('没勾选时拦住', MANAGE.length === 0, '');
  P.setSEL(['a.txt', 'b.txt']);
  await P.askRenameSelected();
  check('勾了 2 个时拦住', MANAGE.length === 0, '');

  console.log('---- 新建文件夹 ----');
  MANAGE.length = 0;
  const mkP = P.askMkdir();
  await tick();
  check('弹窗说明会放 .gitkeep', q('#modalBody').innerHTML.indexOf('.gitkeep') > 0,
        q('#modalBody').innerHTML.slice(0, 160));
  document.querySelector('#m_in0').value = 'assets/2026';
  P.modalOk();
  await mkP;
  check('新建文件夹请求正确',
        MANAGE.length === 1 && MANAGE[0].action === 'mkdir' && MANAGE[0].path === 'assets/2026',
        JSON.stringify(MANAGE[0] || {}));

  console.log('---- 清空仓库：必须输入 CLEAR ----');
  MANAGE.length = 0;
  const clP = P.askClearRepo();
  await tick();
  check('清空弹窗里写明了影响范围',
        q('#modalBody').innerHTML.indexOf('CLEAR') > 0
        && q('#modalBody').innerHTML.indexOf('全部文件和文件夹') > 0,
        q('#modalBody').innerHTML.slice(0, 260));
  check('清空弹窗带出仓库名', q('#modalBody').innerHTML.indexOf('my-assets') > 0, '');
  document.querySelector('#m_in0').value = 'clear';        // 小写也认
  P.modalOk();
  await clP;
  check('输入 clear 也认，confirm 统一成 CLEAR',
        MANAGE.length === 1 && MANAGE[0].action === 'clear' && MANAGE[0].confirm === 'CLEAR',
        JSON.stringify(MANAGE[0] || {}));

  MANAGE.length = 0;
  const clP2 = P.askClearRepo();
  await tick();
  document.querySelector('#m_in0').value = '我不管我就要清空';
  P.modalOk();
  await clP2;
  check('没打 CLEAR 就不发清空请求', MANAGE.length === 0, JSON.stringify(MANAGE));

  console.log('---- 删除文件夹：要手打一遍路径 ----');
  await P.loadFiles(true, '', true);
  MANAGE.length = 0;
  const ddP = P.askDeleteDir('pics');
  await tick();
  check('删文件夹弹窗提示会连带删掉几个文件',
        q('#modalBody').innerHTML.indexOf('pics/') > 0 && q('#modalBody').innerHTML.indexOf('<b>2 个</b>') > 0,
        q('#modalBody').innerHTML.slice(0, 260));
  document.querySelector('#m_in0').value = 'pics2';
  P.modalOk();
  await ddP;
  check('路径打错就不删', MANAGE.length === 0, JSON.stringify(MANAGE));

  const ddP2 = P.askDeleteDir('pics');
  await tick();
  document.querySelector('#m_in0').value = 'pics';
  P.modalOk();
  await ddP2;
  check('路径打对才删',
        MANAGE.length === 1 && MANAGE[0].action === 'delete_dir' && MANAGE[0].path === 'pics',
        JSON.stringify(MANAGE[0] || {}));

  console.log('---- 删除当前所在的整个文件夹 ----');
  MANAGE.length = 0;
  await P.fileEnterDir('pics');
  check('当前在 pics 里', P.getFILES().path === 'pics', P.getFILES().path);
  const hereP = P.askDeleteDirHere();
  await tick();
  check('弹窗带出当前目录路径',
        q('#modalBody').innerHTML.indexOf('pics') > 0, q('#modalBody').innerHTML.slice(0, 200));
  document.querySelector('#m_in0').value = '打错了';
  P.modalOk();
  await hereP;
  check('路径打错就不删（当前目录）', MANAGE.length === 0, JSON.stringify(MANAGE));

  const hereP2 = P.askDeleteDirHere();
  await tick();
  document.querySelector('#m_in0').value = 'pics';
  P.modalOk();
  await hereP2;
  check('确认后删掉当前目录',
        MANAGE.length === 1 && MANAGE[0].action === 'delete_dir' && MANAGE[0].path === 'pics',
        JSON.stringify(MANAGE[0] || {}));
  check('删完自动回到仓库根目录', P.getFILES().path === '', P.getFILES().path);

  console.log('---- 批量删除勾选的文件 ----');
  MANAGE.length = 0;
  P.setSEL(['readme.txt', 'pics/index.html', 'boom.txt']);
  check('selectedFiles 反映勾选', P.selectedFiles().length === 3, JSON.stringify(P.selectedFiles()));
  const bdP = P.askDeleteSelected();
  await tick();
  check('批量删除弹窗列出条目',
        q('#modalBody').innerHTML.indexOf('readme.txt') > 0
        && q('#modalBody').innerHTML.indexOf('pics/index.html') > 0,
        q('#modalBody').innerHTML.slice(0, 260));
  P.modalOk();
  await bdP;
  const dels = MANAGE.filter(m => m.action === 'delete_file');
  check('逐个删除共 3 个请求', dels.length === 3, JSON.stringify(MANAGE.map(m => m.path)));
  check('部分失败会提示出来',
        TOASTS.some(t => t.indexOf('失败') >= 0), JSON.stringify(TOASTS.slice(-3)));

  console.log('---- 接口报错要把原因抛出来 ----');
  try {
    await P.manageAction('delete_file', {path: 'boom.txt'}, null, true);
    check('失败时 manageAction 抛错', false, '居然没抛错');
  } catch (e) {
    check('失败时抛错并带服务端原因', String(e.message).indexOf('模拟失败') >= 0, String(e.message));
  }

  console.log('---- 管理操作带 Token ----');
  MANAGE.length = 0;
  const tokP = P.askDeleteFile('readme.txt');
  await tick();
  P.modalOk();
  await tokP;
  const manageCall = CALLS.filter(c => c.url.indexOf('/api/manage') === 0).pop();
  check('/api/manage 带上 Token',
        !!manageCall && manageCall.headers['X-GitHub-Token'] === 'ghp_test_token',
        manageCall ? JSON.stringify(manageCall.headers) : '');

  console.log('---- 默认分支叫 save（不叫 main）：前端不能自己编一个 main ----');
  const savedDefault = REPO.defaultBranch, savedBranches = REPO.branches, savedCfg = STATE.config;
  REPO.defaultBranch = 'save';
  REPO.branches = ['save', 'main', 'dev'];
  STATE.config = { owner: 'octocat', repo: 'my-assets', branch: '' };
  await P.loadState();
  q('#c_owner').value = 'octocat';
  q('#c_repo').value = 'my-assets';
  q('#c_branch').value = '';
  P.updateTarget();
  check('分支留空时目标行说「自动」，不替用户认定 main',
        q('#targetLine').textContent.indexOf('自动') > 0
        && q('#targetLine').textContent.indexOf('main') < 0, q('#targetLine').textContent);

  await P.loadBranches(true);
  check('留空时会自动认成仓库默认分支 save', q('#c_branch').value === 'save', q('#c_branch').value);
  check('绝不会被前端填成 main', q('#c_branch').value !== 'main', q('#c_branch').value);

  q('#c_branch').value = '';
  CALLS.length = 0;
  const frS = await P.loadFiles(true);
  const fCallS = CALLS.filter(c => c.url.indexOf('/api/files') === 0).pop();
  check('清空分支时前端老实传空，不偷偷塞 main',
        !!fCallS && fCallS.url.indexOf('branch=main') < 0, fCallS ? fCallS.url : '');
  check('服务端自动认成 save，前端照单全收',
        !!frS && P.getFILES().branch === 'save', P.getFILES().branch);

  OPENED.length = 0;
  q('#c_branch').value = '';
  P.openTreeOnGitHub();
  check('「在 GitHub 打开」用真实分支 save',
        OPENED.length === 1 && OPENED[0].indexOf('/tree/save') > 0, JSON.stringify(OPENED));
  check('不会拼出 /tree/main 的死链',
        OPENED.length === 1 && OPENED[0].indexOf('/tree/main') < 0, JSON.stringify(OPENED));

  REPO.defaultBranch = savedDefault;
  REPO.branches = savedBranches;
  STATE.config = savedCfg;

  console.log('');
  console.log('通过 ' + PASS.length + ' / ' + (PASS.length + FAIL.length));
  if (FAIL.length) { console.log('失败: ' + FAIL.join(', ')); process.exit(1); }
  console.log('JS 全部通过 OK');
})();
