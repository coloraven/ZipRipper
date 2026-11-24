# ZipRipper —— 使用 John the Ripper 破解加密文件的密码

<p align="center">
<img src="https://github.com/illsk1lls/ZipRipper/blob/main/.resources/README.png?raw=true"><br>
*<sup>由 John the Ripper 提供破解能力</sup>*
</p>

本仓库提供**两个版本**，功能一致，可按使用习惯任选其一：

| 版本 | 入口 | 适用场景 |
| --- | --- | --- |
| **Python 版**（推荐） | `python zipripper.py` | 网页界面、批量处理、掩码攻击、结果表格化、跨文件复用结果 |
| **CMD 版**（原版） | 双击 `ZipRipper.cmd` | 无需 Python、可打包为完全离线的便携工具 |

两个版本都支持：**ZIP / RAR / 7z / PDF / Microsoft Office**（DOC、DOCM、DOCX、XLS、XLSM、XLSX、PPT、PPTM、PPTX），
并可在支持的情况下使用显卡（OpenCL）加速。

---

## 一、Python 版

### 环境要求

* **Python 3.9 及以上**（由用户自备，仓库不打包 Python 运行时）。
* 首次运行需要联网下载工具；之后全部走缓存，不会重复下载。
* 无需 `pip install`：项目只使用 Python 标准库。

### 快速开始

```bat
python zipripper.py
```

或在 Windows 上双击 `ZipRipper-Py.cmd`。

脚本会自动启动一个**只监听本机**的网页服务，并用默认浏览器打开界面（默认地址
`http://127.0.0.1:8765/`，端口被占用时自动顺延）。

### 功能

* **网页界面**：资源状态、批量选文件、破解设置、任务队列、实时日志、一键下载结果。
* **批量处理**：一次选择多个文件，按设定并发数并行执行；支持取消、重试、移除。
* **掩码攻击**：在界面填写 John 掩码表达式（如 `?u?l?l?l?d?d`），可配合自定义字符集 `--1=`。
* **自定义字典**：可选择本机任意 `.txt` / `.lst`，也支持 `.7z` 压缩字典（自动解包并缓存）；
  内置“下载 rockyou.txt”选项（约 133MB，下载一次后复用）。
* **增量暴力**：使用 John 的 ASCII 增量字符集。
* **结果复用**：按文件内容哈希查历史结果，相同内容的文件（即使路径不同）直接复用密码，
  不再执行耗时的破解流程。
* **显卡加速**：检测到 OpenCL 设备时自动使用 `*-opencl` 格式，并做“格式可用性探测”，
  不可用时自动回退 CPU。

### 结果文件

每成功恢复一个密码，就向**桌面**上的 `password_consumer.csv` **追加**一行（UTF-8-SIG，Excel 可直接打开）：

| 完成时间 | 加密文件完整路径 | 恢复出的密码 | 文件哈希(SHA-256) |
| --- | --- | --- | --- |
| 2026-11-24 14:31:02 | `D:\示例\加密文档.docx` | `password123` | `9c94…` |

旧版三列 CSV 会被自动补齐哈希列并重写表头，历史数据不会丢失。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--host` | 监听地址，默认 `127.0.0.1`（仅本机） |
| `--port` | 首选端口，默认 `8765`，被占用时自动顺延 |
| `--no-browser` | 启动后不自动打开浏览器 |
| `--cache-dir` | 自定义缓存目录（默认 `%LOCALAPPDATA%\ZipRipper`） |
| `--refresh` | 忽略缓存，强制重新下载工具资源 |
| `--workers` | 默认并发任务数（1–8） |
| `--prefetch` | 预下载全部工具资源后退出 |
| `--check` | 只检查环境并打印报告后退出 |

### 缓存与资源

所有外部工具与字典都保存在缓存目录，**只下载一次**：

```
%LOCALAPPDATA%\ZipRipper\
├─ bin\7zr.exe                7-Zip 独立解包器
├─ downloads\                 原始压缩包与 .part 断点文件（支持续传）
├─ tools\JtR\                 John the Ripper（john.exe、*2john、john.conf）
├─ tools\JtR\7za.exe          7-Zip 控制台
├─ tools\perl\                Strawberry Perl 便携版（7z2john.pl / pdf2john.pl 需要）
├─ wordlists\                 可选字典
├─ jobs\<哈希>\               每个文件内容的 John 工作目录（断点续跑）
└─ logs\zipripper.log         运行日志
```

每个已安装资源目录内都有 `.zr-resource.json` 清单，记录来源与安装时间；
清单缺失或来源变化时会自动重建，缓存损坏不会导致程序失效。

### 目录结构

```
zipripper.py          命令行入口（启动网页界面）
ZipRipper-Py.cmd      Windows 双击启动器
zr/                   核心包（详见 zr/README.md）
├─ config.py          常量、路径、设置对象
├─ hashing.py         文件内容哈希
├─ resources.py       工具/字典的缓存与下载
├─ hashers.py         格式 → John 提取器映射
├─ engine.py          John 调用封装
├─ jobs.py            批量队列与并发
├─ results.py         桌面 CSV 读写与哈希复用
└─ webui/             本地网页界面（详见 zr/webui/README.md）
```

### 常见问题

* **Office 破解很慢**：Office 2010/2013 的哈希使用 10 万次迭代（spin count），
  比 ZIP/RAR 慢得多，建议使用显卡或缩小掩码范围。
* **7z / PDF 无法破解**：`7z2john.pl` 与 `pdf2john.pl` 都需要 Perl，程序会自动下载便携版 Perl（约 98MB），只需一次。
* **PDF 依赖**：本项目使用 John 自带的 `pdf2john.pl`（ExifTool 内核），**不需要** `pip install pyhanko`。
* **没有显卡**：自动使用 CPU 模式；可在界面关闭“显卡加速”以避免探测开销。
* **端口被占用**：程序会自动尝试后续端口，控制台会打印实际地址。

---

## 二、CMD 版（原版）

双击 `ZipRipper.cmd`，点击 Start，选择受密码保护的文件，等待结果。
找到密码后会弹窗提示，并把密码保存到桌面的 `ZipRipper-Passwords.txt`。

* **在线模式**：仅需脚本本身与网络，运行时会自动获取 John、7-Zip、便携 Perl。
* **离线模式**：脚本同目录存在 `zr-offline.txt` 时自动启用；点击 John 帽子上的
  “JtR”字母可生成该文件，之后即可断网使用或打包分发。
* **断点续跑**：正常中断（按 `q` 或点一次红叉）会保存进度；任务以文件 MD5 作为
  标识存于 `%AppData%\ZR-InProgress\`，同名不同内容的文件互不影响。
* **字典选项**：点击 John 的嘴可选择内置替代字典、自定义字典或暴力破解模式
  （选择一个空的 .TXT 作为字典即可进入暴力破解）。
* **显卡加速**：支持 nVidia GeForce / Quadro / A 系列 / RTX Ada，AMD Radeon RX / Radeon Pro
  （实验性），以及 Intel ARC（实验性）。
* **Office 支持**：通过 John 的 `office2john.py` 实现；选中 Office 文件时会自动下载
  约 11MB 的便携 Python（在线模式）。离线模式需重新生成 `zr-offline.txt` 以内置该资源，
  若缺失则回退到本机已安装的 Python。

---

## 致谢

* John the Ripper —— <https://github.com/openwall/john>
* cyclone_hk 字典（由 Weakpass 托管）—— <https://github.com/cyclone-github/wordlist>
* 7-Zip —— <https://www.7-zip.org/>
* Strawberry Perl 便携版 —— <https://strawberryperl.com/releases.html>
* Python（便携/嵌入版）—— <https://www.python.org/downloads/windows/>

有疑问请前往 <https://github.com/illsk1lls/ZipRipper/discussions/categories/q-a> 讨论区。
