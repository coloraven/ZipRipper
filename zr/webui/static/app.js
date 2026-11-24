/* ==========================================================================
   ZipRipper · Python 版 前端脚本
   --------------------------------------------------------------------------
   - 每秒轮询 /api/state，增量更新任务表（不整表重建，避免闪烁）
   - 本地文件浏览器：浏览磁盘、多选文件、选择字典
   - 破解设置保存在 localStorage，刷新页面不丢失
   ========================================================================== */

(function () {
  "use strict";

  /* -------------------------------------------------------------- 常量 */

  const STATUS_TEXT = {
    queued: "排队中",
    fingerprinting: "计算哈希",
    extracting: "提取哈希",
    running: "破解中",
    reused: "已复用",
    done: "已完成",
    not_found: "未找到",
    not_protected: "未加密",
    unsupported: "不支持",
    failed: "失败",
    cancelled: "已取消",
  };

  const STATUS_CLASS = {
    queued: "",
    fingerprinting: "badge-running",
    extracting: "badge-running",
    running: "badge-running",
    reused: "badge-reused",
    done: "badge-done",
    not_found: "badge-warn",
    not_protected: "badge-warn",
    unsupported: "badge-warn",
    failed: "badge-error",
    cancelled: "badge-error",
  };

  const FINISHED = new Set([
    "reused", "done", "not_found", "not_protected", "unsupported", "failed", "cancelled",
  ]);

  const RESOURCE_STATUS_TEXT = {
    missing: "未缓存",
    downloading: "下载中",
    extracting: "解包中",
    ready: "已就绪",
    error: "失败",
  };

  const SETTINGS_KEY = "zipripper.settings.v2";

  /* -------------------------------------------------------------- 全局状态 */

  const ui = {
    state: null,
    rows: new Map(),          // 任务 id → 行对象
    expanded: new Set(),      // 展开日志的任务 id
    browser: {
      mode: "files",          // files | wordlist
      path: "",
      parent: "",
      dirs: [],
      files: [],
      roots: [],
      selection: new Set(),
    },
    polling: true,
  };

  const $ = (id) => document.getElementById(id);

  /* -------------------------------------------------------------- 工具函数 */

  function api(path, options) {
    const opts = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    return fetch(path, opts).then(async (response) => {
      const text = await response.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || ("请求失败：" + response.status));
      }
      return data;
    });
  }

  let toastTimer = null;
  function toast(message, kind) {
    const box = $("toast");
    box.textContent = message;
    box.className = "toast" + (kind ? " is-" + kind : "");
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 4200);
  }

  function formatSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes, index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return value.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  function formatTime(seconds) {
    if (!seconds) return "";
    const d = new Date(seconds * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function shortHash(hash) {
    if (!hash) return "";
    return hash.slice(0, 16) + "…";
  }

  /* -------------------------------------------------------------- 设置表单 */

  function readSettings() {
    const attack = document.querySelector('input[name="attack"]:checked').value;
    const select = $("wordlist-select").value;
    let wordlist = $("wordlist-path").value.trim();
    if (select === "") wordlist = "";
    return {
      attack: attack,
      wordlist: wordlist,
      use_rules: $("use-rules").checked,
      mask: $("mask-input").value.trim(),
      custom_charset: $("charset-input").value.trim(),
      use_gpu: $("use-gpu").checked,
      workers: parseInt($("workers").value, 10) || 1,
      job_timeout: parseInt($("job-timeout").value, 10) || 0,
    };
  }

  function applySettings(settings) {
    if (!settings) return;
    const radio = document.querySelector(`input[name="attack"][value="${settings.attack || "wordlist"}"]`);
    if (radio) radio.checked = true;
    $("wordlist-path").value = settings.wordlist || "";
    if (!settings.wordlist) $("wordlist-select").value = "";
    $("use-rules").checked = settings.use_rules !== false;
    $("mask-input").value = settings.mask || "";
    $("charset-input").value = settings.custom_charset || "";
    $("use-gpu").checked = settings.use_gpu !== false;
    $("workers").value = settings.workers || 1;
    $("job-timeout").value = settings.job_timeout || 0;
    syncModeBlocks();
  }

  function saveSettings() {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(readSettings())); } catch (e) { /* 忽略 */ }
  }

  function syncModeBlocks() {
    const attack = document.querySelector('input[name="attack"]:checked').value;
    $("block-wordlist").hidden = attack !== "wordlist";
    $("block-mask").hidden = attack !== "mask";
    $("block-incremental").hidden = attack !== "incremental";
  }

  function bindSettings() {
    document.querySelectorAll('input[name="attack"]').forEach((radio) => {
      radio.addEventListener("change", () => { syncModeBlocks(); saveSettings(); });
    });
    ["wordlist-path", "mask-input", "charset-input", "workers", "job-timeout"].forEach((id) => {
      $(id).addEventListener("change", saveSettings);
    });
    ["use-rules", "use-gpu"].forEach((id) => {
      $(id).addEventListener("change", saveSettings);
    });

    $("wordlist-select").addEventListener("change", async () => {
      const value = $("wordlist-select").value;
      if (value === "") {
        $("wordlist-path").value = "";
        $("wordlist-hint").textContent = "将使用 John 自带的 password.lst。";
      } else if (value === "__rockyou__") {
        $("wordlist-hint").textContent = "正在下载 rockyou.txt（首次需要一些时间）…";
        try {
          const data = await api("/api/resources", {
            method: "POST",
            body: JSON.stringify({ wordlist: "rockyou" }),
          });
          $("wordlist-path").value = data.path;
          $("wordlist-hint").textContent = "rockyou.txt 已就绪：" + data.path;
        } catch (error) {
          $("wordlist-hint").textContent = "下载失败：" + error.message;
          toast("下载 rockyou 失败：" + error.message, "error");
        }
      } else if (value === "__custom__") {
        openBrowser("wordlist");
      }
      saveSettings();
    });
  }

  /* -------------------------------------------------------------- 顶部信息 */

  function renderHeader(state) {
    const devices = (state.engine && state.engine.opencl_devices) || [];
    const chip = $("chip-engine");
    if (devices.length) {
      chip.textContent = "显卡加速可用：" + devices[0].replace(/^Device #\d+.*?name:\s*/, "");
      chip.className = "chip chip-ok";
      chip.title = devices.join("\n");
    } else {
      chip.textContent = "CPU 模式（未检测到 OpenCL 设备）";
      chip.className = "chip chip-muted";
    }

    const csv = $("chip-csv");
    csv.textContent = "结果文件：" + state.results.csv_path;
    csv.title = state.results.csv_path;

    const supported = (state.engine && state.engine.supported) || [];
    $("supported-hint").textContent =
      "支持：" + supported.map((ext) => ext.replace(".", "").toUpperCase()).join(" / ");
    $("csv-path").textContent = state.results.csv_path + "（已记录 " + state.results.count + " 条）";
  }

  /* -------------------------------------------------------------- 资源卡片 */

  function renderResources(state) {
    const grid = $("resource-grid");
    const resources = state.resources || {};
    grid.innerHTML = "";
    Object.keys(resources).forEach((key) => {
      const res = resources[key];
      const card = document.createElement("div");
      card.className = "resource";

      const statusText = RESOURCE_STATUS_TEXT[res.status] || res.status;
      const barClass = res.status === "ready" ? "bar is-ok" : (res.status === "error" ? "bar is-error" : "bar");
      const percent = res.status === "ready" ? 100 : (res.progress || 0);

      card.innerHTML = `
        <div class="resource-top">
          <span class="resource-title"></span>
          <span class="resource-status"></span>
        </div>
        <div class="${barClass}"><span></span></div>
        <div class="resource-message"></div>`;
      card.querySelector(".resource-title").textContent = res.title;
      card.querySelector(".resource-status").textContent = statusText;
      card.querySelector(".resource-status").className =
        "resource-status " + (res.status === "ready" ? "chip chip-ok" : (res.status === "error" ? "chip chip-danger" : "chip chip-muted"));
      card.querySelector(".bar > span").style.width = percent + "%";
      card.querySelector(".resource-message").textContent =
        res.status === "ready" ? res.path : (res.message || "");
      grid.appendChild(card);
    });
  }

  /* -------------------------------------------------------------- 任务表 */

  function jobRowTemplate(job) {
    const tr = document.createElement("tr");
    tr.dataset.id = job.id;
    tr.innerHTML = `
      <td>
        <div class="file-name"></div>
        <div class="file-path"></div>
        <div class="file-hash"></div>
      </td>
      <td>
        <span class="badge"></span>
        <div class="hint job-message"></div>
      </td>
      <td>
        <div class="progress-cell">
          <div class="bar"><span></span></div>
          <div class="progress-text"></div>
        </div>
      </td>
      <td class="job-password"></td>
      <td><div class="row-actions"></div></td>`;

    const logRow = document.createElement("tr");
    logRow.className = "log-row";
    logRow.innerHTML = `<td colspan="5"><div class="log-box"></div></td>`;

    tr.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      toggleLog(job.id);
    });

    return {
      tr: tr,
      logRow: logRow,
      name: tr.querySelector(".file-name"),
      path: tr.querySelector(".file-path"),
      hash: tr.querySelector(".file-hash"),
      badge: tr.querySelector(".badge"),
      message: tr.querySelector(".job-message"),
      bar: tr.querySelector(".bar > span"),
      barWrap: tr.querySelector(".bar"),
      percent: tr.querySelector(".progress-text"),
      password: tr.querySelector(".job-password"),
      actions: tr.querySelector(".row-actions"),
      logBox: logRow.querySelector(".log-box"),
      status: null,
      actionSignature: null,
    };
  }

  function toggleLog(jobId) {
    if (ui.expanded.has(jobId)) ui.expanded.delete(jobId); else ui.expanded.add(jobId);
    const row = ui.rows.get(jobId);
    if (row) row.logRow.hidden = !ui.expanded.has(jobId);
  }

  function renderActions(row, job) {
    const signature = job.status;
    if (row.actionSignature === signature) return;
    row.actionSignature = signature;
    const actions = row.actions;
    actions.innerHTML = "";

    const addButton = (label, handler, primary) => {
      const button = document.createElement("button");
      button.className = "btn btn-mini" + (primary ? " btn-primary" : "");
      button.textContent = label;
      button.addEventListener("click", handler);
      actions.appendChild(button);
    };

    if (!FINISHED.has(job.status)) {
      addButton("取消", () => postAction("/api/jobs/cancel", job.id));
    } else {
      addButton("重试", () => postAction("/api/jobs/retry", job.id));
      addButton("移除", () => postAction("/api/jobs/remove", job.id));
    }
  }

  async function postAction(path, jobId) {
    try {
      await api(path, { method: "POST", body: JSON.stringify({ id: jobId }) });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function renderJobs(state) {
    const tbody = $("jobs-body");
    const jobs = state.jobs || [];
    const seen = new Set();

    jobs.forEach((job) => {
      seen.add(job.id);
      let row = ui.rows.get(job.id);
      if (!row) {
        row = jobRowTemplate(job);
        ui.rows.set(job.id, row);
        tbody.appendChild(row.tr);
        tbody.appendChild(row.logRow);
      }
      // 追加顺序：若行顺序与后端不一致则重新排列
      if (tbody.children[Array.from(seen).length * 2 - 2] !== row.tr) {
        tbody.appendChild(row.tr);
        tbody.appendChild(row.logRow);
      }

      row.name.textContent = job.name;
      row.path.textContent = job.path;
      row.hash.textContent = job.file_hash ? "SHA-256: " + shortHash(job.file_hash) : "";
      row.hash.title = job.file_hash || "";
      row.badge.textContent = STATUS_TEXT[job.status] || job.status;
      row.badge.className = "badge " + (STATUS_CLASS[job.status] || "");
      row.message.textContent = job.message || "";
      row.bar.style.width = (job.progress || 0) + "%";
      row.barWrap.className = "bar" + (job.status === "done" || job.status === "reused" ? " is-ok" : "");
      row.percent.textContent = (job.progress || 0) + "%";

      if (job.password) {
        row.password.textContent = job.password;
        row.password.className = "job-password pw";
        row.password.title = "点击复制";
        row.password.onclick = (event) => {
          event.stopPropagation();
          copyText(job.password);
        };
      } else {
        row.password.textContent = FINISHED.has(job.status) ? "—" : "…";
        row.password.className = "job-password pw-none";
        row.password.onclick = null;
      }

      renderActions(row, job);

      const logText = (job.log || []).join("\n");
      if (row.logBox.textContent !== logText) {
        row.logBox.textContent = logText;
        row.logBox.scrollTop = row.logBox.scrollHeight;
      }
      row.logRow.hidden = !ui.expanded.has(job.id);
      row.status = job.status;
    });

    // 移除已不在列表中的行
    Array.from(ui.rows.keys()).forEach((id) => {
      if (!seen.has(id)) {
        const row = ui.rows.get(id);
        row.tr.remove();
        row.logRow.remove();
        ui.rows.delete(id);
        ui.expanded.delete(id);
      }
    });

    $("jobs-empty").hidden = jobs.length > 0;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast("已复制密码", "ok")).catch(() => {});
    } else {
      toast("密码：" + text, "ok");
    }
  }

  function renderStats(state) {
    const stats = state.stats || {};
    const parts = [];
    ["queued", "fingerprinting", "extracting", "running", "done", "reused", "not_found", "failed"].forEach((key) => {
      if (stats[key]) parts.push(STATUS_TEXT[key] + " " + stats[key]);
    });
    $("stats-hint").textContent = parts.length ? parts.join(" · ") : "暂无任务";
  }

  /* -------------------------------------------------------------- 轮询 */

  async function poll() {
    if (!ui.polling) return;
    try {
      const state = await api("/api/state");
      ui.state = state;
      renderHeader(state);
      renderResources(state);
      renderJobs(state);
      renderStats(state);
    } catch (error) {
      if (!ui.polling) return;
      $("chip-engine").textContent = "服务连接中断：" + error.message;
      $("chip-engine").className = "chip chip-danger";
    } finally {
      if (ui.polling) setTimeout(poll, 1000);
    }
  }

  /* -------------------------------------------------------------- 添加任务 */

  async function addJobs(paths) {
    if (!paths || !paths.length) {
      toast("请先选择文件", "error");
      return;
    }
    const resultBox = $("add-result");
    try {
      const data = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ paths: paths, settings: readSettings() }),
      });
      const added = (data.added || []).length;
      const skipped = data.skipped || [];
      resultBox.textContent = `已加入 ${added} 个任务` + (skipped.length ? `，跳过 ${skipped.length} 个` : "");
      if (skipped.length) {
        toast("跳过：" + skipped.map((item) => item.reason).join("；"), "error");
      } else {
        toast(`已加入 ${added} 个任务`, "ok");
      }
      $("paths-input").value = "";
    } catch (error) {
      resultBox.textContent = "加入失败：" + error.message;
      toast(error.message, "error");
    }
  }

  /* -------------------------------------------------------------- 文件浏览器 */

  function openBrowser(mode) {
    ui.browser.mode = mode;
    ui.browser.selection = new Set();
    $("browser-title").textContent = mode === "wordlist" ? "选择字典文件" : "选择待破解文件";
    $("modal-browser").hidden = false;
    loadDir("");
  }

  function closeBrowser() {
    $("modal-browser").hidden = true;
  }

  async function loadDir(path) {
    try {
      const data = await api("/api/browse?path=" + encodeURIComponent(path || ""));
      ui.browser.path = data.path;
      ui.browser.parent = data.parent;
      ui.browser.dirs = data.dirs || [];
      ui.browser.files = data.files || [];
      ui.browser.roots = data.roots || [];
      $("browser-path").value = data.path || "（选择磁盘）";
      renderRoots();
      renderBrowser();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function renderRoots() {
    const select = $("browser-roots");
    const roots = ui.browser.roots || [];
    select.innerHTML = "";
    roots.forEach((root) => {
      const option = document.createElement("option");
      option.value = root.path;
      option.textContent = root.name;
      if (root.path === ui.browser.path) option.selected = true;
      select.appendChild(option);
    });
  }

  function renderBrowser() {
    const list = $("browser-list");
    list.innerHTML = "";
    const mode = ui.browser.mode;

    (ui.browser.dirs || []).forEach((dir) => {
      const row = document.createElement("div");
      row.className = "browser-row is-dir";
      row.textContent = "▸ " + dir.name;
      row.title = dir.path;
      row.addEventListener("click", () => loadDir(dir.path));
      list.appendChild(row);
    });

    (ui.browser.files || []).forEach((file) => {
      const selectable = mode === "wordlist"
        ? /\.(txt|lst|7z)$/i.test(file.name)
        : file.supported;
      const row = document.createElement("div");
      row.className = "browser-row" + (selectable ? "" : " is-disabled");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.disabled = !selectable;
      checkbox.checked = ui.browser.selection.has(file.path);
      checkbox.addEventListener("change", () => {
        if (mode === "wordlist") {
          ui.browser.selection.clear();
          if (checkbox.checked) ui.browser.selection.add(file.path);
          renderBrowser();
        } else if (checkbox.checked) {
          ui.browser.selection.add(file.path);
        } else {
          ui.browser.selection.delete(file.path);
        }
        updateSelectionText();
      });

      const label = document.createElement("span");
      label.textContent = file.name;

      const size = document.createElement("span");
      size.className = "size";
      size.textContent = formatSize(file.size);

      row.appendChild(checkbox);
      row.appendChild(label);
      row.appendChild(size);
      list.appendChild(row);
    });

    if (!(ui.browser.dirs || []).length && !(ui.browser.files || []).length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "此目录为空";
      list.appendChild(empty);
    }
    updateSelectionText();
  }

  function updateSelectionText() {
    const count = ui.browser.selection.size;
    $("browser-selection").textContent = ui.browser.mode === "wordlist"
      ? (count ? "已选择字典文件" : "请选择一个字典文件")
      : `已选 ${count} 个文件`;
  }

  /* -------------------------------------------------------------- 事件绑定 */

  function bindEvents() {
    $("btn-browse").addEventListener("click", () => openBrowser("files"));
    $("btn-browser-close").addEventListener("click", closeBrowser);
    $("btn-browser-cancel").addEventListener("click", closeBrowser);
    $("btn-browser-up").addEventListener("click", () => loadDir(ui.browser.parent));
    $("browser-roots").addEventListener("change", (event) => loadDir(event.target.value));

    $("btn-browser-confirm").addEventListener("click", () => {
      const selected = Array.from(ui.browser.selection);
      if (!selected.length) {
        toast("请先选择文件", "error");
        return;
      }
      if (ui.browser.mode === "wordlist") {
        $("wordlist-path").value = selected[0];
        $("wordlist-select").value = "__custom__";
        $("wordlist-hint").textContent = "使用自定义字典：" + selected[0];
        saveSettings();
        closeBrowser();
        return;
      }
      addJobs(selected);
      closeBrowser();
    });

    $("btn-add").addEventListener("click", () => {
      const paths = $("paths-input").value
        .split(/\r?\n/)
        .map((line) => line.trim().replace(/^"|"$/g, ""))
        .filter((line) => line.length > 0);
      addJobs(paths);
    });

    $("btn-pick-wordlist").addEventListener("click", () => openBrowser("wordlist"));

    $("btn-prefetch").addEventListener("click", async () => {
      try {
        await api("/api/resources", { method: "POST", body: JSON.stringify({ keys: "all" }) });
        toast("已开始预取资源，进度见上方卡片", "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    });

    $("btn-clear").addEventListener("click", async () => {
      try {
        const data = await api("/api/jobs/clear", { method: "POST", body: "{}" });
        toast("已清空 " + (data.removed || 0) + " 个任务", "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    });

    $("btn-download-csv").addEventListener("click", () => {
      window.location.href = "/api/results/download";
    });

    $("btn-shutdown").addEventListener("click", async () => {
      if (!confirm("确定要停止 ZipRipper 服务吗？正在运行的任务会被中止（会保存断点）。")) return;
      try {
        await api("/api/shutdown", { method: "POST", body: "{}" });
        ui.polling = false;
        toast("服务已停止，可以关闭此页面", "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("modal-browser").hidden) closeBrowser();
    });
  }

  /* -------------------------------------------------------------- 启动 */

  function boot() {
    bindEvents();
    bindSettings();
    syncModeBlocks();
    try {
      const saved = localStorage.getItem(SETTINGS_KEY);
      if (saved) applySettings(JSON.parse(saved));
    } catch (e) { /* 忽略损坏的本地设置 */ }
    poll();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
