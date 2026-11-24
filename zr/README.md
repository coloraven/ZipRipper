# `zr` —— ZipRipper Python 版核心包

本目录是 Python 版的全部业务逻辑，入口脚本是同级的 [`zipripper.py`](../zipripper.py)。
代码按“单一职责”拆分，每个模块只做一件事，方便阅读与二次开发。

## 目录与模块

| 模块 | 职责 | 关键类 / 函数 |
| --- | --- | --- |
| [`config.py`](config.py) | 全局常量、缓存与桌面路径、设置对象 | `Settings`、`cache_root()`、`csv_path()` |
| [`hashing.py`](hashing.py) | 计算文件内容哈希（SHA-256），支持进度与取消 | `file_hash()`、`new_hasher()` |
| [`resources.py`](resources.py) | 下载 / 解包 / 校验外部工具与字典，缓存优先 | `ResourceManager`、`Resource`、`CATALOG` |
| [`hashers.py`](hashers.py) | 扩展名 → John 提取器映射、输出解析、OpenCL 格式推导 | `HashTool`、`run_extractor()`、`classify_hash()` |
| [`engine.py`](engine.py) | 调用 John：提取哈希、破解、进度上报、取消、断点续跑 | `JohnEngine`、`AttackResult` |
| [`jobs.py`](jobs.py) | 批量队列、并发工作线程、取消/重试、跨文件结果复用 | `JobManager`、`Job`、`JobStatus` |
| [`results.py`](results.py) | 桌面 CSV 的追加写入与按哈希查询、旧格式迁移 | `ResultStore` |
| [`webui/`](webui/) | 本地网页界面（标准库 HTTP + 单页前端） | `App`、`serve()` |

## 一次任务的完整数据流

```
用户选择文件
      │
      ▼
JobManager.add()  ──► 入队（同一批次共享 Settings）
      │
      ▼
工作线程 _execute()
      │
      ├─① hashing.file_hash()        计算文件内容 SHA-256
      │
      ├─② results.lookup_with_time() 命中历史结果？
      │        └─ 是 → 直接复用密码，追加 CSV，状态 = reused（不再跑 John）
      │
      ├─③ engine.prepare()           按需准备 John / 7-Zip / Perl（有缓存则秒过）
      │
      ├─④ engine.extract()           调用 zip2john / rar2john / 7z2john / pdf2john / office2john
      │
      ├─⑤ engine.attack()            字典 / 掩码 / 增量模式跑 John，实时汇报进度
      │
      └─⑥ results.append()           成功则追加一行 CSV
```

## 结果文件

结果追加写入**桌面**上的 `password_consumer.csv`，四列：

| 列 | 说明 |
| --- | --- |
| 完成时间 | 任务结束的本地时间（`YYYY-MM-DD HH:MM:SS`） |
| 加密文件完整路径 | 被处理文件的绝对路径 |
| 恢复出的密码 | John 恢复出的明文密码 |
| 文件哈希(SHA-256) | 文件内容的 SHA-256 |

**跨文件复用**：文件被复制、改名或移动到别的目录后内容哈希不变，程序会先用
哈希查询 CSV，命中则直接复用历史密码并跳过耗时的哈希提取与破解流程。
若桌面上的 CSV 是旧版三列格式，程序会自动补齐哈希列并重写表头。

## 缓存布局

所有外部资源都放在缓存目录（默认 `%LOCALAPPDATA%\ZipRipper`，可用环境变量
`ZR_CACHE_DIR` 或 `--cache-dir` 覆盖）：

```
ZipRipper/
├─ bin/                 7zr.exe（独立解包器）
├─ downloads/           下载的原始压缩包与 .part 断点文件
├─ tools/
│  ├─ JtR/              John the Ripper（run/john.exe、*2john 脚本、john.conf）
│  ├─ JtR/7za.exe       7-Zip 控制台
│  └─ perl/             Strawberry Perl 便携版（7z2john.pl / pdf2john.pl 依赖）
├─ wordlists/           可选字典（rockyou.txt、解包后的 .7z 字典）
├─ jobs/<哈希>/         每个文件内容对应的任务目录（pwhash、john.pot、.rec 断点）
└─ logs/zipripper.log   运行日志
```

每个已安装资源目录内都有 `.zr-resource.json` 清单，记录来源 URL 与安装时间；
清单缺失或 URL 变化时会自动重建，因此缓存损坏不会导致程序失效。

## 扩展新格式

1. 在 [`hashers.py`](hashers.py) 的 `TOOLS` 中新增一个 `HashTool`（扩展名、
   脚本名、解释器、所需资源、哈希家族）；
2. 如果该家族支持显卡加速，在 `OPENCL_FORMATS` 中登记对应的 OpenCL 格式名；
3. 如需新的工具资源，在 [`resources.py`](resources.py) 的 `CATALOG` 中登记；
4. 前端会自动从 `/api/state` 的 `engine.supported` 读取受支持扩展名，无需改页面。

> 提示：PDF 使用 John 自带的 `pdf2john.pl`（基于 ExifTool），不依赖 `pyhanko`；
> 该脚本输出的登录名是带盘符的完整路径，`hashers.normalize_login()` 会把它
> 规范化为文件名，否则 John 会因为登录名中的冒号而报 “No password hashes loaded”。

## 设计约定

* 所有对外文案、日志、异常提示均为中文。
* 不使用第三方依赖，仅依赖 Python 标准库（3.9+）。
* 每个模块、类、公开函数都有中文文档字符串，参数与返回值均有说明。
* 线程安全：任务队列与 CSV 写入都带锁；相同内容（哈希一致）的任务会串行执行，
  避免多个进程同时写同一个任务目录。
