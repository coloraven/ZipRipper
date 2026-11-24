# `zr/webui` —— 本地网页界面

只依赖 Python 标准库（`http.server`），无需安装 Flask/Django，也无需构建前端。
运行 `python zipripper.py` 后会自动用默认浏览器打开界面。

## 组成

| 文件 | 说明 |
| --- | --- |
| [`server.py`](server.py) | HTTP 服务与全部 JSON 接口，含目录浏览与 CSV 下载 |
| [`static/index.html`](static/index.html) | 单页结构：资源、添加文件、破解设置、任务队列、结果 |
| [`static/app.js`](static/app.js) | 轮询状态、增量渲染任务表、文件浏览器、设置持久化 |
| [`static/style.css`](static/style.css) | 深色中性主题、响应式布局 |

## 页面区块

1. **工具资源**：显示 John / 7-Zip / Perl / 可选字典的缓存状态与下载进度，
   可一键“预取全部资源”。
2. **添加待破解文件**：通过内置的本机文件浏览器批量勾选，或直接粘贴完整路径。
3. **破解设置**：攻击模式（字典 / 掩码 / 增量暴力）、字典选择（默认 /
   rockyou / 自定义文件）、规则开关、掩码表达式、自定义字符集、显卡开关、
   并发数与单任务超时。设置保存在浏览器 `localStorage`，刷新不丢失。
4. **任务队列**：文件名、完整路径、文件哈希、状态、进度条、恢复出的密码
   （点击可复制）、取消/重试/移除按钮；点击行可展开实时日志。
5. **结果文件**：显示桌面 CSV 路径与已记录条数，并提供下载按钮。

## 接口一览

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 主页面 |
| `/static/*` | GET | 静态资源（仅允许白名单扩展名） |
| `/api/state` | GET | 任务、资源、统计、引擎能力、默认设置的快照 |
| `/api/browse?path=` | GET | 列出目录内容；`path` 为空时返回磁盘根节点 |
| `/api/roots` | GET | 磁盘根节点列表 |
| `/api/jobs` | POST | 批量添加任务：`{"paths": [...], "settings": {...}}` |
| `/api/jobs/cancel` | POST | 取消任务：`{"id": "..."}` |
| `/api/jobs/retry` | POST | 重试任务 |
| `/api/jobs/remove` | POST | 移除已结束任务（并删除其工作目录） |
| `/api/jobs/clear` | POST | 清空所有已结束任务 |
| `/api/resources` | POST | 预取资源 `{"keys": ["jtr"]}` / `{"keys":"all"}`；或下载字典 `{"wordlist":"rockyou"}` |
| `/api/results/download` | GET | 下载桌面 CSV |
| `/api/shutdown` | POST | 关闭服务 |

## 状态轮询

前端每秒请求一次 `/api/state`，并根据任务 `id` 增量更新表格（只更新变化的单元格），
避免整表重绘造成闪烁。任务日志只携带最后 30 行，控制响应体积。

## 安全说明

* 服务默认只绑定 `127.0.0.1`，不对外网开放。
* 目录浏览会跳过隐藏目录与系统目录（`$Recycle.Bin`、`System Volume Information` 等）。
* 静态文件只允许 `.html/.js/.css/.svg/.png/.ico/.json`，且必须位于 `static/` 内。

## 二次开发提示

* 想改界面文案：直接编辑 `static/index.html` 与 `static/app.js` 中的中文字符串。
* 想新增接口：在 `server.py` 的 `Handler.do_GET` / `do_POST` 中加分支，
  并在 `static/app.js` 中用 `api()` 调用。
* 想让界面实时推送（SSE/WebSocket）而不是轮询：可在 `Handler` 中新增
  `text/event-stream` 分支，前端用 `EventSource` 替换 `poll()`。
