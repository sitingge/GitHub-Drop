# GitHub Drop · 拖拽即上传 + 仓库文件管理（一个简单轻量的github仓库管理工具）

把文件（或**整个文件夹**）拖进网页，自动上传到你 GitHub 仓库的指定目录 —— 零依赖，只用 Python 标准库。
顺带还能**管仓库里的文件**：列出、删除、重命名、新建文件夹、清空（都带二次确认）。

```
┌──────────────────────────────────────────────────────────┐
│  把文件/文件夹拖到这里  →  uploads/2026-09-30/myproj/... │
│  网页显示进度  →  GitHub 仓库里出现同样的文件夹结构      │
│  下半屏文件管理器  →  删 / 改名 / 新建 / 清空（防手滑）  │
└──────────────────────────────────────────────────────────┘
```

## 快速开始（3 步）

1. **启动**：双击 `start-github-drop.bat`（或命令行 `python github_drop.py`），
   浏览器会自动打开 `http://127.0.0.1:8765/`。
2. **粘贴仓库地址**：直接粘 `https://github.com/你的账号/仓库名`，
   owner / repo 会自动填好；再填 Token，点「加载分支」从下拉里选分支。
3. **拖文件进去 → 点「开始上传」**。

> Token 也可以完全不落盘：不勾选「保存到 config.json」，它就只存在于当前页面内存里。

### 仓库地址怎么粘都行

一个输入框就够了，下面这些写法都能认出来：

| 你粘的 | 识别结果 |
| --- | --- |
| `https://github.com/octocat/hello-world` | octocat / hello-world |
| `https://github.com/octocat/hello-world.git` | 自动去掉 `.git` |
| `octocat/hello-world` | 简写也认 |
| `git@github.com:octocat/hello-world.git` | SSH 地址 |
| `git clone https://github.com/octocat/hello-world.git` | 整段命令也能粘 |
| `https://github.com/octocat/hello-world/tree/dev` | 连分支 `dev` 一起认出来 |
| `https://ghe.example.com/team/asset-repo` | 自动把 API 地址切成 `https://ghe.example.com/api/v3` |
| `https://github.com/settings/profile` | 认出不是仓库地址，直接提示，不会乱填 |

**分支名带斜杠怎么办**：`/tree/feature/x/src/app.py` 这种链接天然有歧义 ——
`feature`、`feature/x`、`feature/x/src` 都可能是分支名。这里的做法是：先把所有前缀当候选，
在用 Token 拉到真实分支列表后，用**最长前缀匹配**自动校正（`feature/x` 存在就选它）。
匹配不上就退回默认分支并提示，不会静默写错。

页面上的分支框是个可输入的下拉框，列出仓库的全部分支（默认分支排第一），也可以手填新分支。
点「测试连接」成功后会自动保存配置并顺带加载一次分支。

### 分支：留空就自动认，绝不假设它叫 main

**仓库的默认分支可以是任何名字**（`save`、`master`、`develop`…），不一定是 `main`。
所以这里的分支框**默认是空的**，空 = 让工具自己去问 GitHub：

1. `GET /repos/{owner}/{repo}` 拿到的 `default_branch` 才是权威答案，先读它；
2. 配置里手填了分支的话，用 `GET /repos/{owner}/{repo}/branches/{name}` **验证它真的存在**；
3. 验证不过（比如配置里写着 `main`，仓库其实叫 `save`）→ 自动改用真实默认分支，
   并在结果里说明原因：「分支 main 在这个仓库里不存在，已自动改用默认分支 save」。

结果就是：无论仓库默认分支叫什么，都不会再出现"一路 404、只告诉你分支不存在"的情况。
分支信息带 5 分钟缓存，不会每点一次就多发一轮请求。

如果连仓库信息都读不到（断网 / 私有仓库没配 Token），宁可不带分支参数交给 GitHub 自己判断，
也不会硬塞一个猜出来的 `main` 把真正的报错盖掉。

> 举例：`https://github.com/sitingge/workboddy-proxy` 的默认分支是 `save`。
> 无论配置里留空还是写死 `main`，工具都会落到 `save` 上并说明原因。

### 分支里的文件夹（目录浏览器）

加载分支后，配置区会自动列出**该分支里的文件夹**：

- 点文件夹名进去，面包屑显示当前位置，随时可以点回上一层或根目录
- 每个文件夹右边有「选它」→ 直接把它设为**本次上传的目标目录**
- 「把当前目录设为默认目标」→ 写进上面的目标目录模板（记得点保存配置）
- 文件夹多的时候用「筛选当前目录…」框，输入关键词即时过滤
- 「在 GitHub 打开」直接跳到网页版对应目录

链接里带的目录也会自动认出来：粘贴
`https://github.com/octocat/my-assets/tree/main/pics`，
分支选 `main`、目录自动进 `pics`、本次目标直接设为 `pics`。
如果链接其实指向一个文件（`/tree/main/pics/a.png`），会自动退到它所在的文件夹。

**公开仓库不填 Token 也能浏览**（走匿名只读），私有仓库才必须填 Token。

## 仓库文件管理（删除 / 重命名 / 新建 / 清空）

页面下半部分那张卡片就是文件管理器，**操作的是仓库里的真实文件**（会真的提交）：

| 操作 | 说明 |
| --- | --- |
| 列出文件 | 默认**只列当前这一层**，一个请求秒回；勾上「连子文件夹一起列」才递归扫描整棵子树 |
| 进文件夹 | 点文件夹名进去，面包屑随时点回上一层；「上传到此」把它设成本次上传目标 |
| 删除文件 | 勾选后点「删除选中的文件」，可多选批量删（逐个删，失败会单独报出来） |
| 重命名 | 文件名右边「重命名」；含 `/` 表示顺便移动到别的目录。文件夹也能改名（整棵子树一起搬） |
| 新建文件夹 | 填路径即可，会用 `.gitkeep` 占位（Git 不保存空目录） |
| 删除文件夹 | 文件夹行右边的「删除」，或「删除整个文件夹…」删当前所在目录 |
| 清空仓库 | 删掉该分支上的**全部**顶层内容 |

安全设计：

- **删除、清空全都不可撤销**，每次动手前都弹窗二次确认
- 删文件夹要**手打一遍完整路径**，清空仓库要**输入 `CLEAR`** —— 防手滑
- 重命名的目标位置已存在同名内容时直接拒绝（409），绝不静默覆盖
- 路径里的 `..` 会被清洗掉，管理接口同样挡路径穿越
- 删除/重命名都基于当前 commit 生成一棵新 tree 提一次 commit，**不会强推**（`force: false`）

底层用 Git Data API 实现：`sha: null` 的条目即删除，改名则是「同一棵 tree 里删旧名 + 加新名」，
所以一次操作只留一个 commit，历史干净、可回滚。

### 大仓库 / 网络不稳时的兜底

一次性拉整棵 git tree（`?recursive=1`）在文件多的仓库上会有几百 KB ~ 几 MB，
某些网络环境下会**读到一半被断开**（`Content-Length` 有 517230，实际只收到 495947）。

所以列表有两条腿：

1. 先试「一次性递归 tree」——正常网络下一个请求就够（响应里 `mode=recursive`）；
2. 失败或被 GitHub 截断时，自动退回**逐目录扫描**：每层只取一个小响应，同层并发 8 路
   （响应里 `mode=walk`）。这条路慢一些，但基本不会整份作废。

递归这条路失败后会按仓库记 10 分钟冷却，接下来直接走扫描，不每次都白等一次超时。

## Token 怎么申请

- **Fine-grained PAT**（推荐）：Settings → Developer settings → Personal access tokens → Fine-grained tokens
  权限只需 **Contents: Read and write**（仓库选你要上传的那个）。
- **经典 PAT**：勾选 **repo** 权限即可。

点页面右上角「测试连接」可以立刻验证 owner / repo / 分支 / Token 是否都对。

## 目录规则

目标目录支持模板，上传时实时替换：

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `{date}` | 日期 | `2026-09-30` |
| `{yyyy}` `{mm}` `{dd}` | 年 / 月 / 日 | `2026` `09` `30` |
| `{time}` | 时分秒 | `214741` |
| `{timestamp}` | 10 位时间戳 | `1790776061` |
| `{name}` `{ext}` `{filename}` | 主名 / 扩展名 / 全名 | `report` `pdf` `report.pdf` |

最终路径 = **目标目录** + **相对路径** + **文件名**。

例：目标目录 `assets/{yyyy}{mm}{dd}`，拖入文件夹 `myproj`（内含 `img/logo.png`）
→ 仓库里得到 `assets/20260930/myproj/img/logo.png`，文件夹结构完整复刻。

页面上的开关：

- **保留文件夹结构**：关掉则所有文件平铺到目标目录（拖文件夹时默认自动打开）
- **空文件夹放 .gitkeep**：Git 本身不保存空目录，勾上会用 0 字节 `.gitkeep` 占位
- **同名自动重命名**：默认是**覆盖**同名文件；勾上后改为 `note.md` → `note-1.md` → `note-2.md`
- **完成后自动复制链接**：只复制最后一个文件，避免刷屏

## 上传方式（自动选择）

| 体积 | 走的接口 | 说明 |
| --- | --- | --- |
| ≤ 20 MB | Contents API（`PUT /contents`） | 自动带 `sha` 覆盖旧文件，遇 409 并发冲突会重新取 sha 重试 |
| > 20 MB | Git Data API（blob → tree → commit → 更新 ref） | 绕开 Contents API 的体积限制，上限仍是 GitHub 单文件 100 MB |

阈值可在 `config.json` 里改：`largeFileThresholdMB`（设为 0 = 强制走 Git Data 通道）。

每个文件传完都会给出三种链接，点一下即复制：

- **直链**：`raw.githubusercontent.com/...`（私有仓库的直链需要带 Token 才能访问）
- **GitHub**：网页预览地址
- **CDN**：`cdn.jsdelivr.net/gh/owner/repo@branch/...`，公开仓库可直接嵌网页

## 配置文件 `config.json`

首次运行自动生成。

```json
{
  "owner": "",
  "repo": "",
  "branch": "",
  "token": "",
  "targetDir": "uploads/{date}",
  "apiBase": "https://api.github.com",
  "commitMessage": "chore(upload): add {name}",
  "sanitizeNames": false,
  "autoRename": false,
  "keepFolder": true,
  "largeFileThresholdMB": 20,
  "copyLink": true
}
```

- `branch`：**留空 = 自动用仓库的真实默认分支**（推荐）。填了就按填的来，
  但会先验证这个分支在这个仓库里是否真的存在，不存在会自动改回默认分支并提示
- `apiBase`：GitHub Enterprise 或自建镜像改这里
- `commitMessage`：提交信息模板，支持 `{name}`
- `sanitizeNames`：把文件名里的空格换成连字符
- 上传记录写在同目录的 `upload-history.jsonl`，页面上「最近上传」可查

## 维护提醒：改启动脚本前必看

`start-github-drop.bat` **只能包含 ASCII 字符，且行尾必须是 CRLF**。

Windows 的 cmd 是按系统代码页（中文系统为 GBK/936）逐行读批处理的，文件里只要出现
UTF-8 编码的中文，字节就会错位，后面的行被切碎成
`'b_drop.py" ' 不是内部或外部命令` 这种碎片报错，整个脚本失效（本项目已经踩过一次）。
所以：中文提示统一交给 Python 打印（它跟随控制台编码输出，不会乱码），批处理里只写英文。

改完执行一次自检：

```bash
python tests/check_bat.py
```

会检查 ASCII-only / CRLF / 无 BOM，并实际用 `--help` 跑一遍验证标签跳转和参数透传。

## 自测（4 个脚本，全都不联网）

```bash
python tests/run_tests.py                  # 全部上传用例（121 个断言）-> tests/result.txt
python tests/run_tests.py check_page.py    # 页面前端逻辑（24 个断言）
python tests/run_tests.py check_bat.py     # 启动脚本编码/执行性（8 个断言）
python tests/run_tests.py check_server.py  # 真起服务探活（13 个断言）
```

> 本机 PowerShell 不回显输出，所以统一把结果写进 `tests/*.txt` 看。

- `test_upload_flow.py`：起一个假的 GitHub API，覆盖目录模板、文件夹结构复刻、`.gitkeep` 占位、
  409 冲突重试、同名改名、Git Data 大文件通道、401 鉴权、仓库地址解析、分支最长前缀匹配，
  以及**仓库文件管理**（列出 / 删除 / 重命名 / 新建 / 清空）、**断流兜底**
  （假 API 会故意把响应体截短，验证会不会自动退回逐目录扫描）和**端口占用探测**
  （断言重复启动时不会绑到已在服务的端口上）。
- `check_page.py`：把页面里真实的 `<script>` 抽出来，用 Node + 最小 DOM 桩直接执行，
  验证「粘贴地址 → 自动填字段 → 拉分支 → 选中分支 → 进文件夹 / 改配置」和文件管理区的
  二次确认、批量删除、路径复核等前端链路。
- `check_server.py` / `check_bat.py` / `jscheck.py`：本地服务细节、启动脚本自检（ASCII + CRLF）、页面 JS 语法。
- `check_privacy.py`：隐私自检，确认目录里没有 Token、仓库名、本机用户名等残留（见文末「清空个人信息」）。

## 常见问题

- **端口被占用**：会自动顺延到 8766、8767…，并在控制台明确告诉你「端口 8765 上已经有实例在运行，
  本次改用 8766」；也可 `python github_drop.py --port 9000`
  （注意：**不会**出现两个实例同时监听同一端口的情况 —— Windows 的 `SO_REUSEADDR` 本来允许
  "第二个实例偷偷绑上同一端口、把新标签页抢走"，那样进度/历史会看起来"不见了"，现已从启动探测上堵掉）
- **重复启动会不会顶掉原来的窗口？** 不会。第二次启动只会换一个新端口，原来那个实例和它上面的上传
  完全不受影响。
- **文件超过 100 MB**：GitHub 硬限制，只能改用 Git LFS 或 Release 附件
- **私有仓库的直链打不开**：属正常现象，raw 链接需要带 Token；用 GitHub 页面链接或 CDN（仅公开仓库）
- **「加载分支」报 401/404**：Token 不对或没权限（需要 Contents 读写）；404 通常是 owner/repo 拼错
- **只有本机能访问**：默认只监听 `127.0.0.1`，Token 不会暴露到局域网
- **想换端口/不打开浏览器**：`python github_drop.py --no-browser --port 9000 --config my.json`
- **想同时开两个**：`python github_drop.py --port 8766` —— 端口会自动顺延，两个页面互不干扰
- **「连子文件夹一起列」很慢**：正常现象，仓库文件多时要一层层扫；平时用默认的单层列表就够快
- **列表提示「已逐目录扫完」**：说明网络把一次性的大响应掐断了，已自动换成稳的路子，结果不受影响

## Token 怎么存最安全

三种方式，按安全性从高到低：

1. **环境变量**（推荐）：设好 `GITHUB_TOKEN` 再启动，页面会显示「已通过环境变量提供」，
   Token **不会**被写进 `config.json`：
   ```bat
   set GITHUB_TOKEN=ghp_xxxx
   start-github-drop.bat
   ```
2. **只在页面内存里**：填了 Token 但**不勾**「保存到 config.json」，
   它只活在当前页面内存（勾了「保存」则会存进浏览器的 localStorage）。
3. **存进 config.json**：最方便，但 Token 是**明文**躺在磁盘上的。

不管用哪种，都请注意：

- 整个 `github-drop` 目录**不要分享、不要传到任何仓库**（本项目自带的 `.gitignore`
  已经忽略了 `config.json` 和 `upload-history.jsonl`，但不能防手动复制）
- 一旦这个文件夹外流过（发给别人、传上网盘、提交进仓库），**立刻去 GitHub 撤销并重建 Token**
- 权限给到最小：fine-grained PAT 只需 **Contents: Read and write**，不要用带 `repo` 全权限的经典 Token

## 安全提示

- 服务只监听本机回环地址，不对外网开放
- Token 只用于调用 GitHub API，不会发往任何第三方
- 上传到仓库的内容是**公开可见**的（除非仓库是私有的），别传密钥

## 清空个人信息 / 重新初始化

这个目录里**只有两个文件会存私人信息**：

| 文件 | 里面有什么 | 清掉的影响 |
|---|---|---|
| `config.json` | owner / repo / branch / targetDir / apiBase，以及（如果勾了保存）**明文 Token** | 没有影响，下次启动自动用默认值重建 |
| `upload-history.jsonl` | 上传过的文件名、仓库路径、生成的外链 | 只是「最近上传」列表清空，仓库里的文件不受影响 |

手工重新初始化（等价于首次运行的干净状态）：

```bat
del config.json upload-history.jsonl
```

或者直接删掉这两个文件即可 —— 服务启动时发现 `config.json` 不存在，会写入一份默认配置
（注意：**正在运行的实例**已经把配置和历史读进内存了，删文件后要重启实例才会看到变化）。

交付 / 分享这个目录之前，建议先跑一遍自检：

```bat
python tests\check_privacy.py
```

它会扫 GitHub Token 形态、`config.json` 里的仓库信息、上传历史、本机用户名与主目录路径，
并把结果分成**阻断项**（源码 / 文档 / 配置里的残留，必须清掉）和**提示项**
（测试跑出来的产物，含本机路径属正常，交付前删掉即可）。退出码 0 表示没有阻断项。
要额外查某个仓库名或 GitHub ID，可以 `python tests\check_privacy.py --term 某个词`。
