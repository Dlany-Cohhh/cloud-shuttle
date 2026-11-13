/* 云梭 CloudShuttle · PikPak 下载控制台 v3.0 —— Vue 3 应用（metacubexd 设计系统） */
"use strict";

const THEMES = ["acid","aqua","autumn","black","bumblebee","business","cmyk","coffee",
  "corporate","cupcake","cyberpunk","dark","dim","dracula","emerald","fantasy","forest",
  "garden","halloween","lemonade","light","lofi","luxury","night","nord","pastel","retro",
  "sunset","synthwave","valentine","winter","wireframe"];
const LIGHT_THEMES = new Set(["light","lofi","cupcake","corporate","bumblebee","cmyk","pastel",
  "garden","lemonade","autumn","valentine","winter","wireframe","emerald"]);
/* 仓库版示例地址：部署后请在「设置 → NAS 主页地址」改成自己的导航页 */
const DEFAULT_NAS_HOME = "http://127.0.0.1:8080/";

Vue.createApp({
  data() {
    return {
      /* 导航 */
      page: "overview",
      navs: [
        { id: "overview", label: "概览",     short: "概览", icon: "ti-layout-dashboard" },
        { id: "browse",   label: "云盘预览", short: "预览", icon: "ti-folder-open" },
        { id: "tasks",    label: "下载任务", short: "任务", icon: "ti-download" },
        { id: "files",    label: "本地文件", short: "文件", icon: "ti-folders" },
        { id: "stats",    label: "统计",     short: "统计", icon: "ti-chart-area-line" },
        { id: "accounts", label: "账号",     short: "账号", icon: "ti-users" },
        { id: "settings", label: "设置",     short: "设置", icon: "ti-settings" },
      ],
      collapsed: localStorage.getItem("pk.side") === "1",

      /* 主题 */
      themes: THEMES,
      theme: document.documentElement.dataset.theme || "sunset",
      themeAuto: localStorage.getItem("pk.themeAuto") === "1",
      themeOpen: false,
      swCache: {},

      /* 数据 */
      jobs: [],
      settings: {},
      effectiveProxyUrl: "",
      proxyScope: null,          // v3.74 三条路径各自生效的代理
      proxyCheck: null,          // v3.74 只读连通性测试结果
      proxyChecking: false,
      folderCovers: {},          // v3.74 无封面文件夹的异步补齐结果
      /* v3.57 storages 由后端自动识别（不再写死磁盘总量），每项含 name/totalGB/freeGB/low */
      state: { diskLow: false, diskFree: null, diskTotal: null, storages: [] },
      bootVer: "",                // v3.58 页面加载时的后端版本，用于升级检测
      newVer: "",                 // v3.58 检测到的新版本号（非空 = 提示刷新）
      dirSizes: {},               // v3.60 勾选的文件夹大小统计结果，key = remote:绝对路径
      lfSizes: {},                // v3.64 本地文件夹磁盘占用，key = 绝对路径
      stats: { speed: 0, bytes: 0, transferring: [] },
      hist: [],

      /* 任务页 */
      remotes: [],
      curRemote: "",
      /* v3.37 云盘预览账号下拉：把原先只有 remote 名的 select 换成可搜索的账号列表 */
      acctMenuOpen: false,
      acctQuery: "",
      curPath: "",
      dirs: [],
      dirsLoading: false,
      dirsError: "",
      picked: [],
      mode: "copy",
      filter: "all",
      dlFolder: "",
      localDirs: [],
      dirVolumes: [],             // v3.57 localdirs 返回的存储空间分组
      dlVolume: "",               // v3.57 新任务目标存储空间（空 = 主卷）
      urlVolume: "",              // v3.57 URL 下载目标存储空间
      pickerTarget: "",
      urlForm: { url: "", folder: "" },
      urlAdding: false,
      cleanupMode: false,
      cleanupPicked: [],
      hardDelete: false,          // v3.44 清理模式：彻底删除（不进回收站）
      speedTesting: null,
      speedStage: "",
      speedResult: null,
      speedSeconds: 5,

      /* 统计页 */
      histData: { series: [], daily: [] },
      /* 统计页「账号容量」与账号页「账号列表」是两个不同数据源，必须分开。
         旧版共用 accounts：loadAbout() 把 about 结果（只有 remote 字段）写进来，
         账号页 9 行就全变成无名无类型的空白行 + 未测试。 */
      accounts: [],
      accountsLoading: false,
      ghostAccounts: [],           // v3.51：无 type 的残留空壳段（已从列表过滤）
      purgingGhosts: false,
      accountUsage: [],
      usageLoading: false,
      transfers: [],
      hideNames: localStorage.getItem("pk.hideNames") === "1",
      /* v3.40 云盘文件双视图：list 保持原样，grid 才向后端请求封面元数据 */
      viewMode: localStorage.getItem("pk.view") === "grid" ? "grid" : "list",
      thumbFailed: {},
      /* v3.43 下载过滤：规则存在本机，任务提交时翻译成 rclone 的 _filter */
      dlFilterOpen: false,
      dlFilterOn: localStorage.getItem("pk.dlFilterOn") === "1",
      dlFilter: (() => {
        const base = { skipEmpty: true, minSize: "", maxSize: "", incExt: "", excExt: "", excKw: "" };
        try { return Object.assign(base, JSON.parse(localStorage.getItem("pk.dlFilter") || "{}")); }
        catch (e) { return base; }
      })(),
      /* v3.43 导出 / 查重 */
      expOpen: false,
      scanBusy: "",
      scanData: null,
      dupOpen: false,
      dupBusy: false,
      dupErr: "",
      dupTab: "exact",
      dupScan: null,
      /* v3.44 批量重命名 */
      renameOpen: false,
      renameCtx: "cloud",           // v3.50: cloud = 云盘 / local = NAS 本地文件，共用同一弹窗
      renameScope: "picked",        // picked = 勾选的文件 / all = 当前目录全部文件
      renameMode: "template",       // template / regex / seq
      renameTpl: "{name}{ext}",
      renameFind: "",
      renameRepl: "",
      renameCI: true,
      renamePrefix: "",
      renameSuffix: "",
      renameStart: 1,
      renameStep: 1,
      renamePad: 2,
      renameBusy: false,
      renameResult: null,
      /* v3.44 回收站 */
      trashOpen: false,
      trashBusy: false,
      trashErr: "",
      trashItems: [],
      trashPicked: [],
      /* v3.44 解压：cloud = 云端 API / local = NAS 本地 */
      unzipOpen: false,
      unzipMode: "cloud",
      unzipName: "",
      unzipPwd: "",
      unzipEntries: [],
      unzipInfo: null,
      unzipBusy: false,
      unzipErr: "",
      unzipPremium: false,
      unzipNeedPwd: false,
      unzipDeleteAfter: false,
      unzipTargets: [],
      lfPicked: [],
      quota: [],
      quotaLoading: false,
      quotaOpen: false,
      quotaRemote: "",
      quotaFetchedAt: 0,
      rcloneVer: "",
      consoleVer: "",
      /* v3.54 在线更新 */
      updChecking: false,
      updApplying: false,
      updInfo: null,
      updError: "",
      updState: null,
      updCheckedAt: "",
      health: { allocMB: 0, sysMB: 0, finishedJobs: 0 },

      /* 账号页 */
      providers: [],
      acctForm: { name: "", type: "pikpak", params: {} },
      acctAdding: false,
      purgeBusy: false,           // v3.68 清残留进行中
      testingAll: false,
      testDone: 0,
      testTotal: 0,
      repairing: false,
      moreOpen: false,
      acctEditOpen: false,
      acctEditForm: null,
      acctEditSaving: false,
      importResult: null,
      importOpen: false,
      importTab: "login",
      importTabs: [
        { id: "oneclick", label: "① 一键干净浏览器（推荐）" },
        { id: "login", label: "② 书签一键导入" },
        { id: "paste", label: "③ 粘贴导入" },
      ],
      importText: "",
      importCreate: true,
      importing: false,
      identifying: false,
      /* v3.38：从某个失效账号点「重新登录」时，导入弹窗锁定这个目标账号 */
      relinkTarget: null,

      /* 设置页 */
      defaultNasHomeUrl: DEFAULT_NAS_HOME,
      form: { maxConcurrent: 2, autoStart: "", autoPause: "", diskMinGB: 20, diskQuotaGB: 0,
              nasHomeUrl: DEFAULT_NAS_HOME, proxyUrl: "", pkProxyMode: "follow",
              transfers: 6, checkers: 8, proxy: false, downloadProxy: true,
              advancedMode: false,
              multiThreadStreams: 8, multiThreadCutoffMB: 64, orderBy: "size,descending",
              bwLimitOn: false, bwLimit: "5M", retries: 10, timeout: "1m", idleTimeout: "10m",
              createEmptySrcDirs: true, logLevel: "INFO", logFile: "" },
      saving: false,
      bwSel: "off",
      bwBps: null,

      /* 反馈 */
      toasts: [],
      apiErr: "",
      pollBusy: false,
      pollStatsBusy: false,
      _tid: 0,
      charts: {},

      /* 详情弹窗 */
      detailOpen: false,
      detailId: null,
      detail: null,

      /* v3.41 外部分享链接转存 */
      shareOpen: false,
      shareBusy: false,
      shareErr: "",
      shareInfo: null,
      shareItems: [],
      sharePicked: [],
      sharePath: [],
      shareNextToken: "",
      shareForm: { text: "", passCode: "", remote: "", dstPath: "" },
      /* v3.42 我的分享管理 */
      myShareOpen: false,
      myShareLoading: false,
      myShareErr: "",
      myShares: [],
      myShareLimits: {},
      myShareLog: [],
      myShareAuto: false,
      myShareFilterRemote: "",
      myShareFilterStatus: "OK",
      myShareBusy: "",
      /* 跨账号转移（v3.34 / v3.35 单文件 / v3.39 根目录可挑源 / v3.46 多源批量） */
      xferOpen: false,
      xferBusy: false,
      xferErr: "",
      xferProg: "",
      xferNeedPick: false,
      xferItems: [],                 /* 固定源：行内 ⇄ / 当前文件夹 / 主列表勾选 */
      xferPickSet: [],               /* needPick 模式下的多选 key（d:名 / f:名） */
      xferForm: { srcRemote: "", dstRemote: "", dstPath: "",
                  mode: "copy", method: "share", proxy: true },
      detailLoading: false,

      /* 本地文件页（v3.15）：只读浏览全部存储空间；v3.57 顶层列卷，卷内绝对路径 */
      lfPath: "",
      lfVolumes: [],
      lfDirs: [],
      lfFiles: [],
      lfLoading: false,
      lfError: "",
      lfQuery: "",
      lfSort: "name",
      lfTruncated: false,
      lfExpOpen: false,
      player: null,
      /* v3.45 在线播放：直链签发中（按文件名） / 进度恢复提示文案 */
      cloudPlayBusy: "",
      playResumeHint: "",

      /* 一致性校验（v3.16）：云盘 ↔ 落盘目录/单文件 */
      verifyOpen: false,
      verifyForm: { remote: "", cloudPath: "", localPath: "", mode: "quick", limit: 6 },
      verifyBusy: false,
      verifyResult: null,
      verifyError: "",
      verifyFilter: "diff",
      verifyJobId: "",
      verifyLabels: { match: "一致", missing: "本地缺失", extra: "云端已无",
                      size: "大小不符", content: "内容不符", unknown: "无法判定" },
    };
  },

  computed: {
    runningCount() { return this.jobs.filter(j => j.state === "running").length; },
    /* v3.37 一键清理：已结束（完成 / 出错 / 已暂停）的任务记录数 */
    finishedCount() { return this.jobs.filter(j => j.state !== "running" && j.state !== "queued").length; },
    /* 传输配额：顶部条用当前选中账号，详情弹窗用弹窗里切换的账号 */
    curQuota() { return this.quota.find(q => q.remote === this.curRemote) || null; },
    quotaDetail() { return this.quota.find(q => q.remote === this.quotaRemote) || null; },
    quotaRows() {
      const q = this.quotaDetail;
      if (!q || q.error) return [];
      const rows = [
        { key: "offline", label: "云下载流量", note: "", icon: "ti-cloud-download" },
        { key: "download", label: "下行流量", note: "包括在线播放、查看和下载的传输", icon: "ti-download" },
        { key: "upload", label: "上传流量", note: "", icon: "ti-upload" },
      ];
      if (!q.vip && q.daily && q.daily.total > 0)
        rows.push({ key: "daily", label: "每日限额 · 非会员", note: "每天重置", icon: "ti-clock" });
      return rows.map(r => Object.assign({}, r, { data: q[r.key] || { used: 0, total: 0 } }));
    },
    /* 账号页只渲染有名字的条目：任何来源错误的结构都不会变成一排空白行 */
    namedAccounts() { return this.accounts.filter(a => a && a.name); },
    /* v3.49：侧栏 / 顶栏「返回 NAS 主页」统一取服务端设置；非法值回落到出厂地址 */
    nasHomeUrl() {
      const v = String((this.settings && this.settings.nasHomeUrl) || (this.form && this.form.nasHomeUrl) || DEFAULT_NAS_HOME).trim();
      return /^https?:\/\/[^\s<>"']+$/i.test(v) ? v : DEFAULT_NAS_HOME;
    },
    /* v3.37 云盘预览账号列表：把 remote 名、账号元数据（邮箱/凭证）、配额缓存合到一行 */
    acctItems() {
      const meta = new Map(this.namedAccounts.map(a => [a.name, a]));
      const qs = new Map((this.quota || []).map(q => [q.remote, q]));
      return (this.remotes || []).map(r => {
        const m = meta.get(r) || {};
        const q = qs.get(r);
        return {
          name: r, email: m.email || "", type: m.type || "", cred: m.cred || null,
          daily: q && !q.error && q.daily && q.daily.total > 0 ? q.daily : null,
          vip: q && !q.error ? !!q.vip : null,
          quotaErr: q && q.error ? q.error : "",
        };
      });
    },
    /* v3.45：当前账号类型。只有 PikPak 有免鉴权直链，其它类型点了只会拿到解释 */
    curAcctType() {
      const a = (this.acctItems || []).find(x => x.name === this.curRemote);
      return a ? String(a.type || "") : "";
    },
    acctItemsFiltered() {
      const q = (this.acctQuery || "").trim().toLowerCase();
      if (!q) return this.acctItems;
      return this.acctItems.filter(a => (a.name + " " + a.email).toLowerCase().includes(q));
    },
    curAcctItem() {
      return this.acctItems.find(a => a.name === this.curRemote) ||
             { name: this.curRemote, email: "", type: "" };
    },
    pathSegs() { return this.curPath ? this.curPath.split("/").filter(Boolean) : []; },
    /* 云盘预览：文件表头「全选」状态，随浏览/清理模式切换数据源 */
    allPicked() {
      const names = this.dirFiles.map(f => f.name);
      const bag = this.cleanupMode ? this.cleanupPicked : this.picked;
      return names.length > 0 && names.every(n => bag.includes(n));
    },
    /* v3.60 选中项总大小：文件用列表 size；文件夹异步统计（dirSizes），没统计完的部分不计入 */
    pickedSize() {
      let bytes = 0, dirs = 0, pending = false;
      for (const name of this.picked) {
        const it = (this.dirs || []).find(x => x.name === name);
        if (!it) continue;
        if (it.isdir) {
          dirs++;
          const key = this.dirSizeKey(name);
          const v = this.dirSizes[key];
          /* v3.61：null=统计中（计入 pending 提示），undefined=统计失败（不计 pending，
             旧版会把失败永远显示成"统计中"） */
          if (typeof v === "number") bytes += v;
          else if (v === null) pending = true;
        } else {
          bytes += Math.max(0, Number(it.size) || 0);
        }
      }
      return { bytes, dirs, pending };
    },
    /* 本地文件页：v3.57 顶层是存储空间列表，卷内为绝对路径导航 */
    lfCrumbs() {
      const out = [{ label: "存储空间", path: "" }];
      if (!this.lfPath) return out;
      const vols = this.lfVolumes.length ? this.lfVolumes : (this.state.storages || []);
      const vol = vols.find(v => {
        const root = String(v.console || "").replace(/\/+$/, "");
        return root && (this.lfPath === root || this.lfPath.indexOf(root + "/") === 0);
      });
      if (!vol) {
        /* 不在任何卷内（理论少见）：退回逐段渲染 */
        let cur = "";
        this.lfPath.split("/").filter(Boolean).forEach(seg => {
          cur = cur ? cur + "/" + seg : "/" + seg;
          out.push({ label: seg, path: cur });
        });
        return out;
      }
      /* v3.64：卷感知面包屑——卷根显示卷名，其后才是卷内真实层级。
         旧版把挂载路径段（如 /vol2/download 的 "vol2"）也渲染成目录。 */
      const root = vol.console.replace(/\/+$/, "");
      out.push({ label: vol.name, path: root });
      let cur = root;
      this.lfPath.slice(root.length).split("/").filter(Boolean).forEach(seg => {
        cur = cur + "/" + seg;
        out.push({ label: seg, path: cur });
      });
      return out;
    },
    lfTotalSize() { return this.lfFiles.reduce((a, f) => a + (f.size || 0), 0); },
    lfViewDirs() {
      const q = this.lfQuery.trim().toLowerCase();
      return q ? this.lfDirs.filter(d => d.name.toLowerCase().includes(q)) : this.lfDirs;
    },
    lfViewFiles() {
      const q = this.lfQuery.trim().toLowerCase();
      const a = q ? this.lfFiles.filter(f => f.name.toLowerCase().includes(q)) : [...this.lfFiles];
      if (this.lfSort === "size") a.sort((x, y) => y.size - x.size);
      else if (this.lfSort === "time") a.sort((x, y) => y.mtime - x.mtime);
      else a.sort((x, y) => x.name.localeCompare(y.name, "zh-CN"));
      return a;
    },
    lfEmpty() { return !this.lfViewDirs.length && !this.lfViewFiles.length; },
    /* 一致性校验 */
    verifyJobs() { return (this.jobs || []).filter(j => j.remote && j.dest).slice(0, 40); },
    verifyItems() {
      const list = (this.verifyResult && this.verifyResult.items) || [];
      if (this.verifyFilter === "all") return list;
      if (this.verifyFilter === "diff") return list.filter(i => i.status !== "match");
      return list.filter(i => i.status === this.verifyFilter);
    },
    verifyModeHint() {
      return this.verifyForm.mode === "full"
        ? "完整 SHA256：会把云端文件重新读一遍用于比对（不写磁盘），单文件 ≤300 MiB、合计 ≤1 GiB，流量与时间消耗较大"
        : this.verifyForm.mode === "sample"
          ? "抽样：对前 N 个大小一致的文件，比对头 / 中 / 尾各 32 KB 字节，能发现截断、错版、串文件"
          : "快速：只比对文件名清单与大小，秒级完成，不读取文件内容";
    },
    curProvider() {
      return this.providers.find(p => p.type === this.acctForm.type) || { params: [] };
    },
    pickerFiltered() {
      const val = this.pickerTarget === "dl" ? this.dlFolder
                : this.pickerTarget === "url" ? this.urlForm.folder
                : "";
      const v = (val || "").trim();
      return v ? this.localDirs.filter(d => d.includes(v)) : this.localDirs;
    },
    dirFolders() { return this.dirs.filter(d => d.isdir); },
    dirFiles() { return this.dirs.filter(d => !d.isdir); },
    gridItems() { return this.dirs; },   /* 后端已按「文件夹优先 + 名称」排好序 */
    pikpakRemotes() {
      return this.remotes.filter(r => {
        const a = (this.accounts || []).find(x => x.name === r);
        return !a || !a.type || a.type === "pikpak";
      });
    },
    mySharesView() {
      return this.myShares.filter(r =>
        (!this.myShareFilterRemote || r.remote === this.myShareFilterRemote) &&
        (this.myShareFilterStatus === "ALL" || r.status === this.myShareFilterStatus));
    },
    myShareRemoteOpts() {
      return [...new Set(this.myShares.map(r => r.remote))];
    },
    /* v3.39：根目录工具栏入口用，把当前目录里的文件夹 / 文件列成可转移的源 */
    xferSources() {
      return [
        ...this.dirFolders.map(d => ({ key: "d:" + d.name, name: d.name, isFile: false })),
        ...this.dirFiles.map(f => ({ key: "f:" + f.name, name: f.name, isFile: true })),
      ];
    },
    /* v3.46：本次要转移的源列表（统一出口）。
       needPick 时由弹窗内多选清单（xferPickSet）派生，否则用 openXfer 定好的 xferItems。 */
    xferChosen() {
      if (this.xferNeedPick) {
        return this.xferPickSet.map(k => {
          const isFile = k.slice(0, 2) === "f:";
          const name = k.slice(2);
          return { name, isFile, path: this.fullPath(name) };
        });
      }
      return this.xferItems;
    },
    /* 弹窗顶部那一行「源」的展示文本 */
    xferSrcLabel() {
      const items = this.xferChosen;
      if (!items.length) return "尚未选择";
      const pre = this.xferForm.srcRemote || this.curRemote;
      if (items.length === 1) return pre + ":" + items[0].path;
      const names = items.map(i => i.name);
      const head = names.slice(0, 3).join("、");
      return pre + ":" + (this.curPath ? this.curPath + "/" : "") + head
             + (names.length > 3 ? " 等 " + names.length + " 项" : "");
    },
    /* 目标账号：只留 PikPak（openlist 之类没有原生转存接口）。
       源账号仍保留在列表末尾 —— 后端支持「同账号换目录」，不能因为这次改动砍掉。 */
    xferDstRemotes() {
      const src = this.xferForm.srcRemote || this.curRemote;
      const ok = this.remotes.filter(r => {
        const a = (this.accounts || []).find(x => x.name === r);
        return !a || !a.type || a.type === "pikpak";
      });
      return [...ok.filter(r => r !== src), ...ok.filter(r => r === src)];
    },
    downloadedCount() { return this.dirFiles.filter(f => f.downloaded).length; },
    /* ---------- v3.44 批量重命名 ---------- */
    /* 参与改名的文件：只取当前目录的文件（文件夹改名 rclone 没有接口，
       硬要做得新建目录再逐个搬，风险远大于收益，这里明确不支持） */
    renameAllNames() {
      return this.renameCtx === "local" ? this.lfRenameNames : this.dirFiles.map(f => f.name);
    },
    renamePickedNames() {
      const set = new Set(this.renameAllNames);
      const src = this.renameCtx === "local" ? this.lfPicked : this.picked;
      return src.filter(n => set.has(n));
    },
    renameSources() { return this.renameScope === "picked" ? this.renamePickedNames : this.renameAllNames; },
    /* 三种方式算出的新名字 + 冲突标记。纯前端计算，随手改参数就即时刷新。 */
    renamePlan() {
      /* renameSources 是计算属性，取的是数组本身，不能像方法那样加括号调用 */
      const names = this.renameSources;
      const existing = new Set(this.renameAllNames);
      const srcSet = new Set(names);
      const pad = Math.max(1, Math.min(6, parseInt(this.renamePad, 10) || 2));
      const start = parseInt(this.renameStart, 10) || 1;
      const step = parseInt(this.renameStep, 10) || 1;
      let re = null;
      if (this.renameMode === "regex" && this.renameFind) {
        try { re = new RegExp(this.renameFind, this.renameCI ? "gi" : "g"); } catch (e) { re = null; }
      }
      const rows = names.map((n, i) => {
        const dot = n.lastIndexOf(".");
        const base = dot > 0 ? n.slice(0, dot) : n;
        const ext = dot > 0 ? n.slice(dot) : "";
        const seq = String(start + i * step).padStart(pad, "0");
        let to = n;
        if (this.renameMode === "template") {
          to = String(this.renameTpl || "{name}{ext}")
            .replace(/\{name\}/g, base).replace(/\{n\}/g, n)
            .replace(/\{ext\}/g, ext).replace(/\{i\}/g, seq)
            .replace(/\{idx\}/g, String(i + 1));
        } else if (this.renameMode === "regex") {
          to = re ? n.replace(re, this.renameRepl) : n;
        } else {
          to = String(this.renamePrefix || "") + seq + String(this.renameSuffix || "") + ext;
        }
        to = String(to).trim();
        /* 本地模式额外拦掉隐藏名、传输中的临时名与超长名（与后端同名校验一致） */
        const localBad = this.renameCtx === "local" &&
          (to.startsWith(".") || /\.(part|partial|rclone-partial)$/i.test(to) || to.length > 180);
        return { from: n, to, same: to === n,
                 bad: !to || /[\\/]/.test(to) || to === "." || to === ".." || !!localBad };
      });
      const counts = {};
      rows.forEach(r => { counts[r.to] = (counts[r.to] || 0) + 1; });
      rows.forEach(r => {
        r.dup = !r.same && counts[r.to] > 1;
        r.conflict = !r.same && (r.dup || r.bad ||
          (existing.has(r.to) && !srcSet.has(r.to)));
      });
      return rows;
    },
    renameChanged() { return this.renamePlan.filter(r => !r.same); },
    renameBlocked() { return this.renamePlan.filter(r => r.conflict).length; },
    renamePreview() { return this.renamePlan.slice(0, 200); },
    /* 当前账号是不是会员：云端解压是会员功能，非会员直接引导走本地解压。
       配额还没读到时返回 null（未知），此时不拦按钮，交给服务端如实回答。 */
    curVip() {
      const q = this.curQuota;
      if (!q || q.error) return null;
      return !!q.vip;
    },
    /* ---------- v3.44 回收站 ---------- */
    trashTotal() { return this.trashItems.reduce((a, x) => a + (x.size || 0), 0); },
    trashAllPicked() {
      return this.trashItems.length > 0 && this.trashPicked.length === this.trashItems.length;
    },
    /* ---------- v3.44 解压 ---------- */
    unzipArchiveFiles() { return this.dirFiles.filter(f => f.archive); },
    lfArchives() { return this.lfViewFiles.filter(f => f.archive); },
    lfPickedArchives() {
      const set = new Set(this.lfViewFiles.filter(f => f.archive).map(f => f.name));
      return this.lfPicked.filter(n => set.has(n));
    },
    /* 全选态：模板里当值用（:checked），所以必须放在 computed 而不是 methods，
       否则拿到的永远是函数本身，勾选框会恒为「已选中」。 */
    lfAllArchivesPicked() {
      const names = this.lfArchives.map(f => f.name);
      return names.length > 0 && names.every(n => this.lfPicked.includes(n));
    },
    /* ---------- v3.50 本地批量改名：勾选范围从「仅压缩包」扩展到文件夹 + 所有文件 ---------- */
    lfRenameNames() { return this.lfViewDirs.map(d => d.name).concat(this.lfViewFiles.map(f => f.name)); },
    lfPickedNames() {
      const set = new Set(this.lfRenameNames);
      return this.lfPicked.filter(n => set.has(n));
    },
    lfAllPicked() {
      const names = this.lfRenameNames;
      return names.length > 0 && names.every(n => this.lfPicked.includes(n));
    },
    /* ---------- v3.43 下载过滤 ---------- */
    /* 提交给后端的过滤条件；关闭开关或没有有效规则时为 null（= 完全不加过滤） */
    dlFilterPayload() {
      if (!this.dlFilterOn) return null;
      const f = this.dlFilter || {};
      const out = {};
      if (f.skipEmpty) out.skipEmpty = true;
      ["minSize", "maxSize", "incExt", "excExt", "excKw"].forEach(k => {
        const v = String(f[k] == null ? "" : f[k]).trim();
        if (v) out[k] = v;
      });
      return Object.keys(out).length ? out : null;
    },
    dlFilterCount() {
      const f = this.dlFilterPayload;
      if (!f) return 0;
      let n = 0;
      if (f.skipEmpty) n++;
      ["minSize", "maxSize", "incExt", "excExt", "excKw"].forEach(k => { if (f[k]) n++; });
      return n;
    },
    /* 当前目录按过滤规则预估「会下几个 / 跳过几个」——纯前端算，不发请求 */
    dlFilterPreview() {
      const f = this.dlFilterPayload;
      if (!f) return { pass: this.dirFiles.length, skip: 0, bytes: 0, on: false };
      let pass = 0, skip = 0, bytes = 0;
      this.dirFiles.forEach(it => {
        if (this.filterSkipReason(f, it.name, it.size || 0)) skip++;
        else { pass++; bytes += it.size || 0; }
      });
      return { pass, skip, bytes, on: true };
    },
    dupResult() {
      const files = (this.dupScan && this.dupScan.files) || [];
      const byName = new Map(), bySize = new Map();
      for (const f of files) {
        const name = String(f.p || "").split("/").pop().toLowerCase();
        if (!name) continue;
        if (!byName.has(name)) byName.set(name, []);
        byName.get(name).push(f);
        if (f.s > 0) {
          if (!bySize.has(f.s)) bySize.set(f.s, []);
          bySize.get(f.s).push(f);
        }
      }
      const exact = [], versions = [], sameSize = [];
      byName.forEach((arr, name) => {
        if (arr.length < 2) return;
        const sizes = new Set(arr.map(f => f.s));
        if (sizes.size === 1) exact.push({ name, size: arr[0].s, items: arr });
        else versions.push({ name, size: Math.max.apply(null, arr.map(f => f.s)), items: arr });
      });
      bySize.forEach((arr, size) => {
        if (arr.length < 2 || size < 1048576) return;   // 1 MiB 以下同大小没有参考价值
        const names = new Set(arr.map(f => String(f.p || "").split("/").pop().toLowerCase()));
        if (names.size < 2) return;                     // 同名同大小已归到「完全重复」
        sameSize.push({ name: names.size + " 个不同文件名", size, items: arr });
      });
      const byCount = (a, b) => b.items.length - a.items.length || b.size - a.size;
      return {
        exact: exact.sort(byCount), versions: versions.sort(byCount), sameSize: sameSize.sort(byCount),
        files: files.length, bytes: files.reduce((a, f) => a + (f.s || 0), 0),
      };
    },
    dupGroups() {
      const r = this.dupResult;
      return this.dupTab === "versions" ? r.versions
           : this.dupTab === "samesize" ? r.sameSize : r.exact;
    },
    /* 完全重复里「多出来的那几份」占多少空间（保留一份） */
    dupWaste() {
      return (this.dupResult.exact || []).reduce((a, g) => a + (g.size || 0) * (g.items.length - 1), 0);
    },
    stateCounts() {
      const c = { all: this.jobs.length, running: 0, queued: 0, stopped: 0, error: 0, done: 0 };
      for (const j of this.jobs) c[j.state] = (c[j.state] || 0) + 1;
      return c;
    },
    stateChips() {
      return [
        { id: "all",     label: "全部",   onCls: "bg-base-content/15 text-base-content" },
        { id: "running", label: "下载中", onCls: "bg-info/15 text-info" },
        { id: "queued",  label: "排队中", onCls: "bg-accent/15 text-accent" },
        { id: "stopped", label: "已暂停", onCls: "bg-warning/15 text-warning" },
        { id: "error",   label: "出错",   onCls: "bg-error/15 text-error" },
        { id: "done",    label: "已完成", onCls: "bg-success/15 text-success" },
      ];
    },
    filteredJobs() {
      return this.filter === "all" ? this.jobsView : this.jobsView.filter(j => j.state === this.filter);
    },
    histSeries() { return this.histData.series.map(p => [p[0], p[1] / 1048576]); },
    histPeak() {
      const v = this.histSeries.map(p => p[1]);
      return v.length ? Math.max(...v).toFixed(1) + " MiB/s" : "--";
    },
    histAvg() {
      const v = this.histSeries.map(p => p[1]);
      return v.length ? (v.reduce((a, b) => a + b, 0) / v.length).toFixed(1) + " MiB/s" : "--";
    },
    dailyTotal() {
      const t = this.histData.daily.reduce((a, d) => a + (d.bytes || 0), 0);
      return t ? this.fmt(t) : "--";
    },
    bwText() {
      return this.bwBps == null ? "读取中…" : (this.bwBps < 0 ? "未限速" : this.fmtSpeed(this.bwBps));
    },
    detailJob() { return this.detail && this.detail.job ? this.detail.job : null; },
    detailTitle() {
      const j = this.detailJob;
      return j ? j.remote + ":" + (j.subpath || "(根目录)") : "任务详情";
    },
    diskPct() {
      if (this.state.diskFree == null || this.state.diskTotal == null || this.state.diskTotal <= 0) return 0;
      return Math.max(0, Math.min(100, (1 - this.state.diskFree / this.state.diskTotal) * 100)).toFixed(1);
    },
    statCards() {
      const n = {};
      for (const j of this.jobs) n[j.state] = (n[j.state] || 0) + 1;
      return [
        { label: "总速度",   value: this.fmtSpeed(this.stats.speed),  icon: "ti-activity",     brick: "bg-info/15 text-info" },
        { label: "本次累计", value: this.fmt(this.stats.bytes),       icon: "ti-stack-2",      brick: "bg-secondary/15 text-secondary" },
        { label: "进行中",   value: String(n.running || 0),           icon: "ti-player-play",  brick: "bg-warning/15 text-warning" },
        { label: "排队中",   value: String(n.queued || 0),           icon: "ti-list-numbers", brick: "bg-accent/15 text-accent" },
        { label: "已完成",   value: String(n.done || 0),             icon: "ti-circle-check", brick: "bg-success/15 text-success" },
        { label: "磁盘剩余", value: (this.state.diskFree ?? "--") + " GB", icon: "ti-database",
          brick: this.state.diskLow ? "bg-error/15 text-error" : "bg-success/15 text-success" },
      ];
    },
    stateLabel() {
      return { running: "下载中", done: "已完成", stopped: "已暂停", error: "出错", queued: "排队中" };
    },
    barCls() {
      return { running: "bg-info", done: "bg-success", stopped: "bg-warning", error: "bg-error", queued: "bg-accent/50" };
    },
    jobsView() {
      const PHASE = { share: "建分享", restore: "转存中", move: "归位中", purge: "删源中",
                      scan: "扫描中", copy: "传输中", check: "校验中" };
      let qn = 0;
      return this.jobs.map(j => {
        const xfer = j.kind === "xfer";
        const unzip = j.kind === "unzip";
        const pct = unzip ? (j.state === "done" ? 100 : Math.min(100, Number(j.pctNum) || 0))
                  : xfer ? (j.state === "done" ? 100 : 0)
                         : (j.total > 0 ? Math.min(100, j.bytes / j.total * 100) : 0);
        const nochange = !xfer && !unzip && j.total === 0 && j.state === "done";
        const eta = j.eta == null ? null :
          Math.floor(j.eta / 60) + "分" + String(Math.floor(j.eta % 60)).padStart(2, "0") + "秒";
        const qpos = j.state === "queued" ? ++qn : 0;
        return {
          ...j, pct, nochange, qpos,
          etaStr: eta || "--",
          speedStr: j.speed > 0 ? this.fmtSpeed(j.speed) : "--",
          /* 解压不是「下载中」，状态词单列一套，避免任务卡说错话 */
          stateText: unzip
            ? ({ running: "解压中", done: "已解压", stopped: "已暂停", error: "出错", queued: "排队中" }[j.state] || "")
            : null,
          phaseStr: unzip ? (j.phase || (j.state === "running" ? "解压中" : ""))
                          : (xfer && j.state === "running" ? (PHASE[j.phase] || "") : ""),
          title: unzip
            ? (j.engine === "local"
                ? "解压 " + (j.name || j.subpath || "") + " → " + (j.dest || "同目录")
                : "云端解压 " + j.remote + ":" + (j.subpath || ""))
            : xfer
            ? (j.method === "externalshare"
                ? "分享 " + (j.shareId || j.subpath || "") + " → " +
                  (j.dstRemote || "") + ":" + (j.dstPath || "/")
                : j.remote + ":" + (j.subpath || "/") + " → " +
                  (j.dstRemote || "") + ":" + (j.dstPath || "/"))
            : j.remote + ":" + (j.subpath || "(根目录)"),
        };
      });
    },
    /* 读数单位只在跨量级时变，做成 computed 避免每帧抖动 */
    liveUnit() {
      const v = this.stats.speed || 0;
      return v >= 1048576 ? "MiB/s" : v >= 1024 ? "KB/s" : "B/s";
    },
    /* 60 秒均速（2s 一个采样点 → 30 点窗口），与主曲线同框做趋势参考 */
    liveAvg() {
      const h = this.hist.slice(-30);
      if (!h.length) return "--";
      const m = h.reduce((a, b) => a + b, 0) / h.length;
      return this.fmtSpeed(m * 1048576);
    },
  },

  methods: {
    /* ---------- 基础 ---------- */
    fmt(b) {
      if (b == null) return "--";
      return b > 1073741824 ? (b / 1073741824).toFixed(2) + " GB"
           : b > 1048576    ? (b / 1048576).toFixed(1) + " MB"
                            : (b / 1024).toFixed(0) + " KB";
    },
    /* ---------- v3.60 云端文件夹大小（勾选时按需统计，后端缓存 5 分钟） ---------- */
    dirSizeKey(name) {
      return (this.curRemote || "") + ":" + (this.curPath ? this.curPath + "/" + name : name);
    },
    dirSizeText(name) {
      const v = this.dirSizes[this.dirSizeKey(name)];
      /* v3.73：占位期显示进度，别让人误以为「这个文件夹没有大小数据」 */
      return typeof v === "number" ? this.fmt(v)
           : v === null ? "统计中…" : "--";
    },
    statDirSizes(names, opts) {
      const o = opts || {};
      if (!this.curRemote) return;
      const limit = o.limit || 0;           // >0 时只自动统计前 N 个（其余勾选时补齐）
      const todo = [];
      for (const name of names) {
        const it = (this.dirs || []).find(x => x.name === name && x.isdir);
        if (!it) continue;
        const key = this.dirSizeKey(name);
        /* 已有结果（number）或在统计中（null 占位）都不再入队，避免重复请求 */
        if (typeof this.dirSizes[key] !== "number" && this.dirSizes[key] !== null) {
          todo.push({ name, key, id: it.id || "" });
        }
      }
      if (!todo.length) return;
      const queue = limit > 0 ? todo.slice(0, limit) : todo;
      /* v3.73：先置占位，页面显示「统计中…」而不是 "--" */
      for (const { key } of queue) {
        if (this.dirSizes[key] === undefined) this.dirSizes[key] = null;
      }
      const settle = (key, d) => {
        if (d && typeof d.bytes === "number") { this.dirSizes[key] = d.bytes; return true; }
        return false;
      };
      const drop = key => { if (this.dirSizes[key] === null) delete this.dirSizes[key]; };
      /* v3.73：失败 / 超时的大目录走独立低并发通道，不占住主通道的快任务 */
      const retry = [];
      let ridx = 0, running = 0;
      const pumpRetry = () => {
        while (running < 2 && ridx < retry.length) {
          const job = retry[ridx++];
          running++;
          this.api("dirsize", job.body, 90000)
            .then(d => { if (!settle(job.key, d)) drop(job.key); })
            .catch(() => drop(job.key))
            .finally(() => { running--; pumpRetry(); });
        }
      };
      /* v3.73：主通道并发 5，统计完一个立刻显示一个 */
      let idx = 0;
      const worker = () => {
        if (idx >= queue.length) return;
        const { name, key, id } = queue[idx++];
        /* 带文件夹 id 走 PikPak 原生 API 递归（后端 rclone size 仅兜底） */
        const body = { remote: this.curRemote, id: id || undefined,
                       path: this.curPath ? this.curPath + "/" + name : name };
        this.api("dirsize", body, 150000)
          .then(d => {
            if (!settle(key, d)) throw new Error((d && d.error) || "统计失败");
          })
          .catch(() => {
            /* 后端算完仍会写 5 分钟缓存，稍等后自动重试一次通常秒回 */
            retry.push({ key, body });
            setTimeout(pumpRetry, 800);
          })
          .finally(() => worker());
      };
      for (let i = 0; i < 5; i++) worker();
    },
    /* ---------- v3.64 本地文件页：文件夹磁盘占用（进入目录自动统计，60s 缓存） ---------- */
    lfSizeKey(name) {
      return (this.lfPath || "").replace(/\/+$/, "") + "/" + name;
    },
    lfSizeText(name) {
      const v = this.lfSizes[this.lfSizeKey(name)];
      return typeof v === "number" ? this.fmt(v)
           : v === null ? "统计中…" : "--";
    },
    statLocalSizes() {
      const todo = (this.lfDirs || [])
        .filter(d => !d.volumeRoot)
        .filter(d => {
          const k = this.lfSizeKey(d.name);
          return typeof this.lfSizes[k] !== "number" && this.lfSizes[k] !== null;
        })
        .map(d => ({ name: d.name, key: this.lfSizeKey(d.name) }));
      if (!todo.length) return;
      let idx = 0;
      const worker = () => {
        if (idx >= todo.length) return;
        const { name, key } = todo[idx++];
        this.api("localsize", { path: key }, 60000)
          .then(d => {
            if (d && typeof d.bytes === "number") this.lfSizes[key] = d.bytes;
            else if (this.lfSizes[key] === null) delete this.lfSizes[key];
          })
          .catch(() => {
            if (this.lfSizes[key] === null) delete this.lfSizes[key];
          })
          .finally(() => worker());
      };
      /* v3.73：先置占位（显示「统计中…」），并发提到 5 */
      for (const { key } of todo) {
        if (this.lfSizes[key] === undefined) this.lfSizes[key] = null;
      }
      for (let i = 0; i < 5; i++) worker();
    },
    /* 配额数值可达 TB 级，单独用 TB 显示，避免写成 40960.00 GB */
    fmtQ(b) {
      const v = Number(b) || 0;
      if (v >= 1099511627776) return (v / 1099511627776).toFixed(2) + " TB";
      return this.fmt(v);
    },
    fmtSpeed(b) {
      return b >= 1048576 ? (b / 1048576).toFixed(1) + " MiB/s"
           : b >= 1024    ? (b / 1024).toFixed(0) + " KB/s"
                          : (b || 0).toFixed(0) + " B/s";
    },
    /* timeoutMs 只给轮询类短请求用；verify / speedtest 等长操作不传，保持不限时 */
    async api(action, body, timeoutMs) {
      // 源端（PikPak captcha / TLS 抖动）卡住时不能让请求无限挂着，否则按钮永远转圈、
      // 页面看起来像死了。默认 30s；已知的长操作按 action 放宽，调用方仍可显式覆盖。
      const LONG = { verify: 120000, speedtest: 30000, dirs: 60000, cloudlink: 60000,
                     cloudmv: 60000, cloudrm: 60000, start: 60000, urldl: 60000,
                     accttest: 60000, acctident: 60000, acctimporttext: 60000,
                     acctrepair: 60000, localfiles: 60000, cleanup: 60000, dirsize: 150000, localsize: 60000,
                     scanpath: 240000, quota: 120000,
                     renamebatch: 120000, localrenamebatch: 120000, acctpurge: 60000,
                     trashlist: 90000, trashop: 120000,
                     unzipinfo: 90000, unzip: 120000, localunzip: 60000,
                     playinfo: 60000,
                     updatecheck: 30000, updateapply: 240000,
                     updatestate: 15000, updaterollback: 60000 };
      const ms = timeoutMs || LONG[action] || 30000;
      const ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      const timer = ctl ? setTimeout(() => ctl.abort(), ms) : null;
      try {
        const r = await fetch("/api", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({ action }, body || {})),
          signal: ctl ? ctl.signal : undefined,
        });
        const d = await r.json();
        return d;
      } catch (e) {
        if (ctl && ctl.signal.aborted)
          throw new Error("请求超时（" + Math.round(ms / 1000) + " 秒），源端可能无响应");
        throw e;
      } finally {
        if (timer) clearTimeout(timer);
      }
    },
    toast(msg, type, action) {
      /* v3.38：可带一个动作按钮（凭证失效时直接给「重新登录采集」入口），
         有动作时停留 12 秒，避免用户还没看清 / 没来得及点就消失。 */
      const id = ++this._tid;
      this.toasts.push({ id, msg: this.safeMsg(msg), type: type || "ok", action: action || null });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, action ? 12000 : 3200);
    },
    safeMsg(s) {
      /* 界面兜底：不管错误从哪条后端路径冒出来，都不把完整凭证显示到屏幕上 */
      return String(s == null ? "" : s).replace(
        /(os\.[A-Za-z0-9._\-]{16,}|[A-Za-z0-9_\-]{40,})/g,
        m => m.slice(0, 8) + "…（已隐藏" + (m.length - 12) + "位）…" + m.slice(-4));
    },
    needRelinkText(s) {
      return /refreshed by other|已被别处刷新|invalid refresh token|refresh_token|重新登录|凭证|鉴权|invalid_grant|invalid_token|unauthorized|no username|captcha|失效|过期/i.test(String(s || ""));
    },
    toastError(msg, account) {
      if (this.needRelinkText(msg)) {
        this.toast(msg, "error", { label: "重新登录采集", run: () => this.relinkAccount(account || null) });
      } else {
        this.toast(msg, "error");
      }
    },
    runToastAction(t) {
      this.toasts = this.toasts.filter(x => x.id !== t.id);
      try { if (t.action && typeof t.action.run === "function") t.action.run(); } catch (e) { /* 忽略 */ }
    },

    /* ---------- 轮询 ---------- */
    async refresh() {
      if (document.hidden || this.pollBusy) return;
      this.pollBusy = true;
      try {
        const d = await this.api("jobs", {}, 8000);
        this.jobs = d.jobs || [];
        this.settings = d.settings || {};
        this.effectiveProxyUrl = d.effectiveProxyUrl || "";
        if (d.proxyScope !== undefined) this.proxyScope = d.proxyScope;
        this.state = d.state || this.state;
        /* v3.58：后端升级检测。页面不刷新时 JS 永远是旧代码，新后端 + 旧前端
           混搭会出现"目录不存在"之类的怪状态——版本变了就提示用户刷新。 */
        const ver = String((this.state || {}).version || "");
        if (ver) {
          if (!this.bootVer) this.bootVer = ver;
          else if (ver !== this.bootVer && ver !== this.newVer) this.newVer = ver;
        }
        this.apiErr = "";
      } catch (e) {
        /* 后端不可达横幅：成功时清空、失败时点亮。abort = 8 秒超时，给中文提示 */
        const msg = (e && e.message) || "";
        const aborted = (e && e.name === "AbortError") || /abort/i.test(msg);
        this.apiErr = aborted ? "请求超时（8 秒无响应）" : (msg || "网络错误");
      } finally {
        this.pollBusy = false;
      }
    },
    async refreshStats() {
      if (document.hidden || this.pollStatsBusy) return;
      this.pollStatsBusy = true;
      try {
        const d = await this.api("stats", {}, 8000);
        this.stats = d;
        this.hist.push(d.speed / 1048576);
        if (this.hist.length > 90) this.hist.shift();
        await this.$nextTick();
        this.tweenSpeed(d.speed);
        this.updateSpeedChart();
      } catch (e) {} finally {
        this.pollStatsBusy = false;
      }
    },

    /* ---------- ECharts 图表（§11.14：画线展开动画 / 数据平滑过渡 / 悬停提示） ---------- */
    echColors() {
      if (this._cc && this._ccTheme === this.theme) return this._cc;
      const cs = getComputedStyle(document.documentElement);
      const norm = (v, a) => {
        if (!v) return "rgba(128,128,128," + a + ")";
        /* oklch 等现代色值经 canvas 规整为 rgba，保证 zrender 能解析 */
        const ctx = document.createElement("canvas").getContext("2d");
        ctx.fillStyle = v.trim();
        ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        return "rgba(" + d[0] + "," + d[1] + "," + d[2] + "," + a + ")";
      };
      this._ccTheme = this.theme;
      this._cc = {
        info: norm(cs.getPropertyValue("--color-info"), 1),
        success: norm(cs.getPropertyValue("--color-success"), 1),
        txt: norm(cs.getPropertyValue("--color-base-content"), 1),
        base: norm(cs.getPropertyValue("--color-base-200"), 1),
      };
      return this._cc;
    },
    initChart(name, el, option) {
      if (!window.echarts || !el) return;
      if (this.charts[name]) this.charts[name].dispose();
      const c = echarts.init(el);
      c.setOption(option, true);   // notMerge：重建实例时清干净，避免旧 series 残留叠加
      this.charts[name] = c;
    },
    /* rgba(r,g,b,1) → rgba(r,g,b,a)：同一色相分透明度做发光/描边/渐变 */
    withA(color, a) {
      return String(color).replace(/rgba?\(([^)]+)\)/, (m, p) => {
        const s = p.split(",").map(x => x.trim());
        return "rgba(" + s[0] + "," + s[1] + "," + s[2] + "," + a + ")";
      });
    },
    /* 背景亮度判定：浅色主题里同一套发光参数会显得脏，需要压低阴影、提高描边 */
    isLight() {
      const m = String(this.echColors().base).match(/(\d+)\D+(\d+)\D+(\d+)/);
      if (!m) return false;
      return (0.299 * +m[1] + 0.587 * +m[2] + 0.114 * +m[3]) > 150;
    },
    lineArea(color) {
      return {
        color,
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: color.replace(",1)", ",.28)") },
          { offset: 1, color: color.replace(",1)", ",0)") },
        ]) },
      };
    },
    /* ---------------- 实时流量动效曲线（mihomo 面板风格） ----------------
       四层叠加：光晕底 → 60s 均线 → 横向渐变主线 → 呼吸头部亮点。
       动效来源三处：① 每次轮询数据补间（ECharts 自带 cubicOut）；
                    ② 头部亮点 12fps 呼吸；③ 大号读数 easeOutCubic 滚动。
       性能：轮询只按 series.id 增量喂数据，不重建 option。 */
    speedChartOption() {
      const c = this.echColors();
      const light = this.isLight();   // 浅色主题：收掉外发光，靠实描边立住
      const A = (a) => this.withA(c.info, a);
      const data = this.hist.map(v => +v.toFixed(2));
      return {
        animationDuration: 800,
        animationEasing: "cubicOut",
        grid: { left: 4, right: 12, top: 12, bottom: 2, containLabel: true },
        tooltip: { trigger: "axis", backgroundColor: c.base, borderWidth: 0,
                   textStyle: { color: c.txt, fontSize: 12 },
                   axisPointer: { type: "line", lineStyle: { color: A(.35), type: [4, 4] } },
                   // 四层 series 里只有 main 是真值，其余是装饰，提示只取 main
                   formatter: (ps) => {
                     const arr = Array.isArray(ps) ? ps : [ps];
                     const p = arr.find(x => x.seriesId === "main") || arr[arr.length - 1] || {};
                     const raw = Array.isArray(p.value) ? p.value[1] : p.value;
                     const v = Number(raw || 0);
                     const label = v > 0 ? v.toFixed(2) + " MiB/s" : "暂无传输 · 0 MiB/s";
                     return '<div style="font-size:12px;line-height:1.6;white-space:nowrap">' + label + "</div>";
                   } },
        xAxis: { type: "category", boundaryGap: false, data: this.hist.map((_, i) => i),
                 axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false } },
        yAxis: { type: "value", splitNumber: 4, min: 0,
                 splitLine: { lineStyle: { color: this.withA(c.txt, .07), type: [3, 5] } },
                 axisLabel: { color: c.txt, opacity: .45, fontSize: 10,
                              formatter: (v) => v >= 1 ? (v % 1 ? v.toFixed(1) : v) + "M"
                                             : v > 0 ? Math.round(v * 1024) + "K" : "0" } },
        series: [
          /* 1 光晕底：超粗半透明线 + 阴影模糊，让曲线本身发光 */
          { id: "glow", type: "line", data: data.slice(),
            smooth: .4, showSymbol: false, silent: true, z: 1,
            lineStyle: { width: light ? 7 : 10, color: A(light ? .09 : .13),
                         shadowBlur: light ? 8 : 18, shadowColor: A(light ? .25 : .55) },
            areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: A(light ? .16 : .26) }, { offset: .6, color: A(light ? .05 : .08) },
              { offset: 1, color: A(0) }]) } },
          /* 2 60 秒均线：虚线趋势参考，压在实时线下面不抢戏 */
          { id: "avg", type: "line", data: this.avgSeries(),
            smooth: .4, showSymbol: false, silent: true, z: 2,
            lineStyle: { width: 1.1, color: this.withA(c.txt, .26), type: [4, 4] } },
          /* 3 主曲线：横向渐变描边（左侧淡出、右侧全亮），像一道扫过的光 */
          { id: "main", type: "line", data: data,
            smooth: .4, showSymbol: false, z: 3,
            lineStyle: { width: light ? 2.8 : 2.4, cap: "round",
              color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
                { offset: 0, color: A(.12) }, { offset: .45, color: A(.8) }, { offset: 1, color: A(1) }]) },
            areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: A(light ? .14 : .22) }, { offset: 1, color: A(0) }]) } },
          /* 4 头部亮点：白芯 + 彩边 + 光晕，随轮询前移；呼吸由 startPulse 调 symbolSize */
          { id: "head", type: "scatter", data: this.headPoint(data),
            silent: true, z: 10, symbolSize: 9,
            itemStyle: { color: "#fff", borderColor: c.info, borderWidth: 2.5,
                         shadowBlur: 14, shadowColor: A(.9) } },
        ],
      };
    },
    headPoint(data) {
      const n = data ? data.length : 0;
      return n ? [[n - 1, data[n - 1]]] : [];
    },
    /* 30 点滑动平均（2s/点 → 60 秒窗口），前缀不足窗口时按已有点数平均 */
    avgSeries() {
      const h = this.hist, W = 30, out = [];
      let sum = 0;
      for (let i = 0; i < h.length; i++) {
        sum += h[i];
        if (i >= W) sum -= h[i - W];
        out.push(+(sum / Math.min(i + 1, W)).toFixed(2));
      }
      return out;
    },
    updateSpeedChart(force, retried) {
      const el = this.$refs.chart;
      if (!el || !window.echarts) {
        if (!retried) setTimeout(() => this.updateSpeedChart(force, true), 400);
        return;
      }
      const data = this.hist.map(v => +v.toFixed(2));
      if (force || !this.charts.speed || !this.charts.speed.getDom().isConnected) {
        this.initChart("speed", el, this.speedChartOption());
        this.startPulse();
      } else {
        /* 按 id 增量合并。四条 series 必须都带 id：ECharts 无 id 时按索引对齐，
           一旦顺序或条数变化就会串线（历史上「曲线消失」就是这类问题）。 */
        this.charts.speed.setOption({
          xAxis: { data: this.hist.map((_, i) => i) },
          series: [
            { id: "glow", data: data },
            { id: "avg",  data: this.avgSeries() },
            { id: "main", data: data },
            { id: "head", data: this.headPoint(data) },
          ],
        });
      }
    },
    /* 头部亮点呼吸：只改 symbolSize，80ms 一帧足够顺滑且几乎不耗 CPU */
    startPulse() {
      if (this._pulse) return;
      this._ph = 0;
      this._pulse = setInterval(() => {
        const ch = this.charts.speed;
        if (!ch || document.hidden || this.page !== "overview") return;
        this._ph += 0.42;
        ch.setOption({ series: [{ id: "head", symbolSize: +(8.6 + Math.sin(this._ph) * 2.2).toFixed(1) }] });
      }, 80);
    },
    stopPulse() {
      if (this._pulse) { clearInterval(this._pulse); this._pulse = null; }
    },
    /* 大号读数补间：直接写 DOM textContent，不走响应式，避免 60fps 触发整页重渲染 */
    tweenSpeed(target) {
      const to = Number(target) || 0;
      const from = Number(this._spCur || 0);
      const t0 = performance.now(), dur = 550;
      if (this._spRaf) cancelAnimationFrame(this._spRaf);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / dur);
        const e = 1 - Math.pow(1 - k, 3);
        const v = k < 1 ? from + (to - from) * e : to;
        this._spCur = v;
        const el = this.$refs.speedNum;
        if (el) {
          el.textContent = v >= 1048576 ? (v / 1048576).toFixed(2)
                          : v >= 1024    ? (v / 1024).toFixed(0)
                                         : v.toFixed(0);
        }
        this._spRaf = k < 1 ? requestAnimationFrame(step) : 0;
      };
      this._spRaf = requestAnimationFrame(step);
    },
    histChartOption() {
      const c = this.echColors();
      return {
        animationDuration: 900,
        grid: { left: 6, right: 12, top: 24, bottom: 2, containLabel: true },
        tooltip: { trigger: "axis", backgroundColor: c.base, borderWidth: 0,
                   textStyle: { color: c.txt, fontSize: 12 },
                   valueFormatter: (v) => v + " MiB/s" },
        xAxis: { type: "time", axisLine: { show: false }, axisTick: { show: false },
                 axisLabel: { color: c.txt, opacity: .45, fontSize: 10, formatter: "{HH}:{mm}" },
                 splitLine: { show: false } },
        yAxis: { type: "value", splitNumber: 4,
                 splitLine: { lineStyle: { color: c.txt, opacity: .1 } },
                 axisLabel: { color: c.txt, opacity: .45, fontSize: 10 } },
        series: [{ type: "line", data: this.histSeries.map(p => [p[0] * 1000, +p[1].toFixed(2)]),
                   smooth: .3, showSymbol: false, ...this.lineArea(c.info) }],
      };
    },
    dailyChartOption() {
      const c = this.echColors();
      const days = this.histData.daily;
      return {
        animationDuration: 700,
        animationDelay: (i) => i * 40,
        grid: { left: 6, right: 8, top: 24, bottom: 2, containLabel: true },
        tooltip: { trigger: "axis", backgroundColor: c.base, borderWidth: 0,
                   textStyle: { color: c.txt, fontSize: 12 },
                   valueFormatter: (v) => v + " GB" },
        xAxis: { type: "category", data: days.map(d => d.date.slice(5)),
                 axisLine: { show: false }, axisTick: { show: false },
                 axisLabel: { color: c.txt, opacity: .45, fontSize: 10,
                              interval: Math.max(0, Math.ceil(days.length / 7) - 1) } },
        yAxis: { type: "value", splitNumber: 4,
                 splitLine: { lineStyle: { color: c.txt, opacity: .1 } },
                 axisLabel: { color: c.txt, opacity: .45, fontSize: 10 } },
        series: [{ type: "bar", barMaxWidth: 22,
                   data: days.map(d => +(d.bytes / 1073741824).toFixed(2)),
                   itemStyle: { borderRadius: [4, 4, 0, 0],
                     color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                       { offset: 0, color: c.success.replace(",1)", ",.9)") },
                       { offset: 1, color: c.success.replace(",1)", ",.45)") },
                     ]) } }],
      };
    },
    updateHistChart(force, retried) {
      const el = this.$refs.histChart;
      if (!el || !window.echarts) {
        if (!retried) setTimeout(() => this.updateHistChart(force, true), 400);
        return;
      }
      this.initChart("hist", el, this.histChartOption());
    },
    updateDailyChart(force, retried) {
      const el = this.$refs.dailyChart;
      if (!el || !window.echarts) {
        if (!retried) setTimeout(() => this.updateDailyChart(force, true), 400);
        return;
      }
      this.initChart("daily", el, this.dailyChartOption());
    },
    rebuildCharts() {
      this._cc = null; /* 主题色缓存随切换失效 */
      for (const k of Object.keys(this.charts)) { this.charts[k].dispose(); delete this.charts[k]; }
      this.$nextTick(() => { this.updateSpeedChart(true); this.updateHistChart(true); this.updateDailyChart(true); });
    },
    resizeCharts() {
      for (const k of Object.keys(this.charts)) {
        if (this.charts[k] && this.charts[k].resize) this.charts[k].resize();
      }
    },

    /* ---------- 浏览 / 任务 ---------- */
    async loadRemotes() {
      try {
        const d = await this.api("remotes");
        this.remotes = d.remotes || [];
        if (this.remotes.length) { this.curRemote = this.remotes[0]; this.loadDirs(""); }
      } catch (e) {}
    },
    /* 文件表头全选：只增删本页文件名，不动已勾选的目录名 */
    toggleAll(on) {
      const names = this.dirFiles.map(f => f.name);
      const key = this.cleanupMode ? "cleanupPicked" : "picked";
      const bag = this[key] || [];
      this[key] = on ? [...new Set([...bag, ...names])] : bag.filter(n => !names.includes(n));
    },
    onRemoteChange() {
      this.curPath = ""; this.picked = []; this.loadDirs("");
      this.quotaRemote = this.curRemote;      /* 配额条按当前账号切换，数据已在缓存里 */
    },
    /* v3.37 账号下拉：打开时才拉账号元数据 / 配额，避免每 2 秒轮询多打接口 */
    toggleAcctMenu() {
      this.acctMenuOpen = !this.acctMenuOpen;
      if (!this.acctMenuOpen) return;
      this.acctQuery = "";
      if (!this.accounts.length) this.loadAccounts();
      this.loadQuota(false);
    },
    pickAcct(name) {
      this.acctMenuOpen = false;
      if (!name || name === this.curRemote) return;
      this.curRemote = name;
      this.onRemoteChange();
      /* 新加的账号还没进配额缓存时主动取一次（后端还有 5 分钟缓存，不会打爆） */
      if (!(this.quota || []).some(q => q.remote === name)) this.loadQuota(true);
    },
    reloadDirs() { this.picked = []; this.loadDirs(this.curPath); },

    /* ---------- v3.40 列表 / 缩略图双视图 ---------- */
    setView(m) {
      if ((m !== "list" && m !== "grid") || m === this.viewMode) return;
      this.viewMode = m;
      localStorage.setItem("pk.view", m === "grid" ? "grid" : "");
      this.thumbFailed = {};
      this.loadDirs(this.curPath);   // 只有 grid 会带 thumbs 参数，切回 list 零额外请求
    },
    fullPath(name) { return this.curPath ? this.curPath + "/" + name : name; },
    /* 封面走 NAS 同源代理：用户浏览器不必能直连 PikPak CDN */
    thumbUrl(u) { return "/thumb?u=" + encodeURIComponent(u); },
    onThumbError(name) { this.thumbFailed = { ...this.thumbFailed, [name]: true }; },
    fileIcon(it) {
      if (it.isdir) return "ti-folder";
      const m = String(it.mime || "").toLowerCase();
      const n = String(it.name || "").toLowerCase();
      if (m.startsWith("video/") || /\.(mp4|mkv|avi|mov|wmv|flv|webm|ts|m2ts)$/.test(n)) return "ti-movie";
      if (m.startsWith("image/") || /\.(jpe?g|png|gif|webp|bmp|heic|avif)$/.test(n)) return "ti-photo";
      if (m.startsWith("audio/") || /\.(mp3|flac|wav|m4a|aac|ape)$/.test(n)) return "ti-music";
      if (/\.(zip|rar|7z|tar|gz|bz2)$/.test(n)) return "ti-file-zip";
      if (/\.(txt|md|pdf|docx?|xlsx?|epub|srt|ass|ssa|nfo|json)$/.test(n)) return "ti-file-text";
      return "ti-file";
    },
    fileIconCls(it) {
      if (it.isdir) return "text-warning/60";
      return { "ti-movie": "text-info/60", "ti-photo": "text-success/60",
               "ti-music": "text-accent/60", "ti-file-zip": "text-warning/60",
               "ti-file-text": "text-base-content/35" }[this.fileIcon(it)] || "text-base-content/30";
    },
    shortMime(m) {
      const s = String(m || "");
      if (s.startsWith("video/")) return "视频 · " + s.slice(6).toUpperCase();
      if (s.startsWith("image/")) return "图片 · " + s.slice(6).toUpperCase();
      if (s.startsWith("audio/")) return "音频 · " + s.slice(6).toUpperCase();
      return s.length > 16 ? s.slice(0, 16) + "…" : s;
    },

    async loadDirs(path) {
      // 传入相对文件夹名时自动补上当前父路径。旧版只传 d.name，进入二级目录会
      // 把 "Pack From Shared/kz" 变成 "kz"，后端查不到东西就误报「目录为空」。
      let p = path || "";
      if (p && p.indexOf("/") === -1 && this.curPath &&
          this.dirs.some(d => d.isdir && d.name === p)) {
        p = this.curPath + "/" + p;
      }
      this.curPath = p;
      /* v3.60：换目录即清空上一目录勾选（放在请求前，加载失败也不会残留）。
         旧版保留会导致「根目录勾选文件夹 → 点进该文件夹勾文件 → 下载时两份任务重复」；
         只放成功分支会漏掉「加载失败后 curPath 已变、勾选还在」的边缘场景。 */
      this.picked = [];
      this.cleanupPicked = [];
      this.folderCovers = {};                          // v3.74 换目录清空异步封面
      this.dirsLoading = true;
      this.dirsError = "";
      /* 连点两下目录（或切账号后旧请求才回来）时，晚到的响应会把新目录的内容
         盖成旧目录的 —— 面包屑显示新路径、列表却是旧内容。用序号丢弃过期响应。 */
      const seq = (this._dirsSeq = (this._dirsSeq || 0) + 1);
      try {
        const d = await this.api("dirs", { remote: this.curRemote, path: this.curPath,
                                           thumbs: this.viewMode === "grid" });
        if (seq !== this._dirsSeq) return;                  // 已有更新的请求，丢弃这次结果
        if (d.error) { this.dirs = []; this.dirsError = d.error; }
        else {
          this.dirs = d.dirs || [];
          this.thumbFailed = {};                            // 换目录重置封面失败记录
          if (d.listError) this.dirsError = d.listError;   // 加载失败 ≠ 目录为空
          /* v3.63：进入目录自动统计各子文件夹的总大小（并发 3 渐进填充，最多 40 个；
             超出的仍会在勾选时按需统计）。结果缓存 5 分钟，重复进入不重复请求。 */
          this.statDirSizes((this.dirs || []).filter(x => x.isdir).map(x => x.name), { limit: 40 });
          this.fetchCovers();                              // v3.74 无封面文件夹异步补齐
        }
      } catch (e) {
        if (seq !== this._dirsSeq) return;
        this.dirs = [];
        this.dirsError = "目录加载失败：" + (e.message || e);
      }
      this.dirsLoading = false;
    },
    /* v3.74：PikPak 顶层文件夹常没有 thumbnail_link，单独取内部第一张封面，
       不阻塞 dirs；用 _dirsSeq 丢弃切目录前的过期响应。 */
    async fetchCovers() {
      if (this.viewMode !== "grid" || !this.curRemote) return;
      const missing = (this.dirs || [])
        .filter(x => x.isdir && !x.thumb && x.id && !this.folderCovers[x.name])
        .slice(0, 12)
        .map(x => ({ name: x.name, id: x.id }));
      if (!missing.length) return;
      const seq = this._dirsSeq;
      try {
        const d = await this.api("foldercovers", {
          remote: this.curRemote, path: this.curPath, items: missing }, 90000);
        if (seq !== this._dirsSeq) return;
        if (d && d.covers && Object.keys(d.covers).length)
          this.folderCovers = { ...this.folderCovers, ...d.covers };
      } catch (e) { /* 封面补齐失败：回退文件类型图标，不打断浏览 */ }
    },
    /* v3.74：只读连通性测试，不带任何账号凭证、不写配置 */
    async testProxy() {
      this.proxyChecking = true;
      this.proxyCheck = null;
      try {
        this.proxyCheck = await this.api("proxycheck", {}, 60000);
      } catch (e) {
        this.proxyCheck = { ok: false, hint: "测试失败：" + (e.message || e), items: [] };
      }
      this.proxyChecking = false;
    },
    setMode(m) {
      if (m === this.mode) return;
      if (m === "move") {
        const ok = window.confirm(
          "即将切换为「move · 下完删云端」\n\n" +
          "· 下载完成后会删除云端对应的文件/文件夹，不可恢复！\n" +
          "· 若本地目标已存在相同文件，rclone 会直接删除云端源而不传输。\n\n" +
          "确定要继续吗？（copy · 保留云端 为默认安全选项）"
        );
        if (!ok) return;
      }
      this.mode = m;
    },
    async startJob(name) {
      try {
        const d = await this.api("start", {
          remote: this.curRemote,
          path: this.curPath ? this.curPath + "/" + name : name,
          mode: this.mode,
          folder: this.dlFolder,
          volume: this.dlVolume || undefined,   // v3.57 目标存储空间（空 = 主卷）
          proxy: this.dlProxy(),
          filter: this.dlFilterPayload || undefined,
        });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast("已加入队列：" + name, "ok");
      } catch (e) { this.toast("启动失败：" + e.message, "error"); }
      this.refresh();
    },
    async startFileJob(name) {
      /* 单文件下载：rclone 会把它放进 保存目录/<文件名> */
      try {
        const d = await this.api("start", {
          remote: this.curRemote,
          path: this.curPath ? this.curPath + "/" + name : name,
          mode: "copy",
          folder: this.dlFolder,
          volume: this.dlVolume || undefined,
          proxy: this.dlProxy(),
        });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast("已加入队列（单文件）：" + name, "ok");
      } catch (e) { this.toast("启动失败：" + e.message, "error"); }
      this.refresh();
    },
    async batchStart() {
      const names = [...this.picked];
      let ok = 0, err = 0;
      const errMsgs = [];
      for (const n of names) {
        try {
          const d = await this.api("start", {
            remote: this.curRemote,
            path: this.curPath ? this.curPath + "/" + n : n,
            mode: this.mode,
            folder: this.dlFolder,
            volume: this.dlVolume || undefined,
            proxy: this.dlProxy(),
            filter: this.dlFilterPayload || undefined,
          });
          if (d.error) { err++; errMsgs.push(n + "：" + d.error); }
          else ok++;
        } catch (e) { err++; errMsgs.push(n + "：" + e.message); }
      }
      this.picked = [];
      const detail = errMsgs.length ? "：" + errMsgs.slice(0, 2).join("；").slice(0, 220) +
        (errMsgs.length > 2 ? "（另有 " + (errMsgs.length - 2) + " 个）" : "") : "";
      this.toast("已提交 " + ok + " 个任务" + (err ? "，" + err + " 个失败" + detail : ""),
                 err && !ok ? "error" : "ok");
      this.refresh();
    },
    /* ---------- v3.43 下载过滤（提交给 rclone 的 _filter） ---------- */
    sizeToBytes(s) {
      const m = /^(\d+(?:\.\d+)?)\s*([bkmgt]?)$/i.exec(String(s == null ? "" : s).trim());
      if (!m) return null;
      const mul = { "": 1, b: 1, k: 1024, m: 1048576, g: 1073741824, t: 1099511627776 };
      return Math.round(parseFloat(m[1]) * (mul[(m[2] || "").toLowerCase()] || 1));
    },
    ruleList(v) {
      return String(v == null ? "" : v).split(/[,，、;；\n]+/)
        .map(x => x.trim().toLowerCase()).filter(Boolean);
    },
    /* 返回跳过原因；空字符串 = 这个文件会下载。与后端 rclone 规则保持同一套语义 */
    filterSkipReason(f, name, size) {
      if (!f) return "";
      const n = String(name || "").toLowerCase();
      const i = n.lastIndexOf(".");
      const ext = i > 0 ? n.slice(i + 1) : "";
      if (f.skipEmpty && !size) return "0 字节";
      const mn = this.sizeToBytes(f.minSize);
      if (mn != null && size < mn) return "小于 " + f.minSize;
      const mx = this.sizeToBytes(f.maxSize);
      if (mx != null && size > mx) return "大于 " + f.maxSize;
      const inc = this.ruleList(f.incExt);
      if (inc.length && inc.indexOf(ext) === -1) return "扩展名不在白名单";
      if (this.ruleList(f.excExt).indexOf(ext) !== -1) return "扩展名被排除";
      if (this.ruleList(f.excKw).some(k => n.indexOf(k) !== -1)) return "命中排除关键词";
      return "";
    },
    dlFilterText(f) {
      const x = f || this.dlFilterPayload;
      if (!x) return "未启用";
      const bits = [];
      if (x.skipEmpty) bits.push("跳过 0 字节");
      if (x.minSize) bits.push("≥" + x.minSize);
      if (x.maxSize) bits.push("≤" + x.maxSize);
      if (x.incExt) bits.push("仅 " + x.incExt);
      if (x.excExt) bits.push("排除 " + x.excExt);
      if (x.excKw) bits.push("排除「" + x.excKw + "」");
      return bits.join(" · ") || "未启用";
    },
    openDlFilter() { this.dlFilterOpen = true; },
    saveDlFilter() {
      try {
        localStorage.setItem("pk.dlFilter", JSON.stringify(this.dlFilter));
        localStorage.setItem("pk.dlFilterOn", this.dlFilterOn ? "1" : "0");
      } catch (e) { /* 隐私模式下 localStorage 可能不可写，忽略 */ }
      this.dlFilterOpen = false;
      this.toast(this.dlFilterPayload ? "下载过滤已启用：" + this.dlFilterText() : "下载过滤已关闭", "ok");
    },
    resetDlFilter() {
      this.dlFilter = { skipEmpty: true, minSize: "", maxSize: "", incExt: "", excExt: "", excKw: "" };
      this.dlFilterOn = true;
      this.toast("已恢复默认（只跳过 0 字节文件）", "ok");
    },

    /* ---------- v3.43 导出 / 查重 ---------- */
    saveText(text, filename, mime) {
      const url = URL.createObjectURL(new Blob([text], { type: mime || "text/plain;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    },
    csvText(rows) {
      return "\uFEFF" + rows.map(a => a.map(x =>
        '"' + String(x == null ? "" : x).replace(/"/g, '""') + '"').join(",")).join("\r\n");
    },
    exportTag() {
      const dir = this.curPath ? this.curPath.split("/").pop() : (this.curRemote || "cloud");
      const t = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      return "-" + dir.replace(/[\\/:*?"<>|]/g, "_").slice(0, 40) + "-" + t;
    },
    /* 递归扫描（含子目录）按目录缓存 60 秒：导出与查重复用同一份结果 */
    async loadScan() {
      const key = (this.curRemote || "") + ":" + (this.curPath || "");
      if (this.scanData && this.scanData.key === key && Date.now() - this.scanData.ts < 60000)
        return this.scanData;
      const d = await this.api("scanpath", { remote: this.curRemote, path: this.curPath, depth: 6 }, 240000);
      if (d.error) throw new Error(d.error);
      const pack = { key, ts: Date.now(), files: d.files || [], dirs: d.dirs || [],
                     truncated: !!d.truncated, elapsed: d.elapsed || 0 };
      this.scanData = pack;
      return pack;
    },
    async exportCloudCsv(deep) {
      if (deep) this.scanBusy = "csv";
      let entries = [];
      try {
        if (deep) {
          const s = await this.loadScan();
          const pre = this.curPath ? this.curPath + "/" : "";   // CSV 里统一用「相对账号根」的路径
          entries = s.files.map(f => ({ path: pre + f.p, size: f.s, mtime: f.m, isdir: false }))
            .concat(s.dirs.map(p => ({ path: pre + p, size: 0, mtime: "", isdir: true })));
          if (s.truncated) this.toast("目录过大，清单已截断到 6000 个文件", "warning");
        } else {
          entries = this.dirs.map(it => ({ path: this.fullPath(it.name), size: it.size || 0,
                                           mtime: it.mtime || "", isdir: !!it.isdir,
                                           downloaded: !!it.downloaded }));
        }
      } catch (e) {
        this.toast("导出失败：" + (e.message || e), "error");
        return;
      } finally { this.scanBusy = ""; }
      if (!entries.length) { this.toast("当前目录没有可导出的条目", "error"); return; }
      const rows = [["类型", "路径", "大小(字节)", "大小", "修改时间", "已下载"]];
      entries.forEach(e => rows.push([
        e.isdir ? "文件夹" : "文件", e.path, e.size, this.fmt(e.size),
        e.mtime, e.downloaded === undefined ? "" : (e.downloaded ? "是" : "否"),
      ]));
      this.saveText(this.csvText(rows),
        (deep ? "云盘完整清单" : "云盘清单") + this.exportTag() + ".csv", "text/csv;charset=utf-8");
      this.toast("已导出 " + entries.length + " 条到 CSV", "ok");
    },
    async exportCloudTree(deep) {
      if (deep) this.scanBusy = "tree";
      let entries = [];
      try {
        if (deep) {
          const s = await this.loadScan();
          entries = s.files.map(f => ({ path: f.p, size: f.s, isdir: false }))
            .concat(s.dirs.map(p => ({ path: p, size: 0, isdir: true })));
          if (s.truncated) this.toast("目录过大，目录树已截断到 6000 个文件", "warning");
        } else {
          entries = this.dirs.map(it => ({ path: this.fullPath(it.name), size: it.size || 0,
                                           isdir: !!it.isdir }));
        }
      } catch (e) {
        this.toast("导出失败：" + (e.message || e), "error");
        return;
      } finally { this.scanBusy = ""; }
      if (!entries.length) { this.toast("当前目录没有可导出的条目", "error"); return; }
      // 深度扫描的路径本来就相对当前目录；当前目录即时导出的是完整路径，这里剥掉前缀
      const base = this.curPath ? this.curPath + "/" : "";
      const rel = entries.map(e => ({
        path: deep ? e.path
          : (base && e.path.indexOf(base) === 0 ? e.path.slice(base.length) : e.path),
        size: e.size, isdir: e.isdir,
      }));
      this.saveText(this.buildTreeText(rel),
        (deep ? "目录树-完整" : "目录树") + this.exportTag() + ".txt");
      this.toast("目录树已导出（" + rel.length + " 个条目）", "ok");
    },
    buildTreeText(entries) {
      const rootName = (this.curPath ? this.curPath.split("/").pop() : this.curRemote) || "root";
      const root = { dirs: new Map(), files: [] };
      entries.forEach(e => {
        const parts = String(e.path || "").split("/").filter(Boolean);
        if (!parts.length) return;
        let node = root;
        for (let i = 0; i < parts.length - 1; i++) {
          if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], { dirs: new Map(), files: [] });
          node = node.dirs.get(parts[i]);
        }
        const leaf = parts[parts.length - 1];
        if (e.isdir) {
          if (!node.dirs.has(leaf)) node.dirs.set(leaf, { dirs: new Map(), files: [] });
        } else node.files.push({ name: leaf, size: e.size || 0 });
      });
      const out = [rootName + "/"];
      const walk = (node, prefix) => {
        const ds = [...node.dirs.keys()].sort((a, b) => a.localeCompare(b, "zh-CN"));
        const fs = node.files.slice().sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
        const items = ds.map(n => ({ n, dir: true }))
          .concat(fs.map(f => ({ n: f.name, dir: false, size: f.size })));
        items.forEach((it, i) => {
          const last = i === items.length - 1;
          out.push(prefix + (last ? "└─ " : "├─ ") + it.n + (it.dir ? "/" : "") +
                   (it.dir ? "" : "  [" + this.fmt(it.size) + "]"));
          if (it.dir) walk(node.dirs.get(it.n), prefix + (last ? "    " : "│   "));
        });
      };
      walk(root, "");
      return out.join("\n") + "\n";
    },
    openDup() {
      this.dupOpen = true;
      this.dupErr = "";
      this.dupTab = "exact";
      this.runDupScan();
    },
    closeDup() { this.dupOpen = false; this.dupBusy = false; },
    async runDupScan() {
      this.dupBusy = true;
      this.dupErr = "";
      this.dupScan = null;
      try {
        this.dupScan = await this.loadScan();
      } catch (e) {
        this.dupErr = e.message || String(e);
      } finally { this.dupBusy = false; }
    },
    exportDupCsv() {
      const r = this.dupResult;
      const rows = [["类型", "分组名", "字节", "重复份数", "路径"]];
      const put = (type, groups) => groups.forEach(g => g.items.forEach((f, i) => rows.push([
        i === 0 ? type : "", i === 0 ? g.name : "", f.s, g.items.length, f.p,
      ])));
      put("完全重复", r.exact);
      put("同名不同大小", r.versions);
      put("同大小不同名", r.sameSize);
      if (rows.length === 1) { this.toast("没有重复项，无需导出", "warning"); return; }
      this.saveText(this.csvText(rows), "查重报告" + this.exportTag() + ".csv", "text/csv;charset=utf-8");
      this.toast("查重报告已导出", "ok");
    },
    async stopAll() {
      try {
        const d = await this.api("jobs");
        for (const j of d.jobs || []) {
          if (j.state === "running" || j.state === "queued") await this.api("stop", { id: j.id });
        }
        this.toast("已暂停全部任务", "ok");
      } catch (e) { this.toast("操作失败", "error"); }
      this.refresh();
    },
    async resumeAll() {
      try {
        const d = await this.api("jobs");
        for (const j of d.jobs || []) {
          if (j.state === "stopped" || j.state === "error") await this.api("resume", { id: j.id });
        }
        this.toast("已继续全部任务", "ok");
      } catch (e) { this.toast("操作失败", "error"); }
      this.refresh();
    },
    async jobOp(act, id) {
      if (act === "remove" && !window.confirm("删除该任务记录？（不影响已下载的文件）")) return;
      try {
        const d = await this.api(act, { id });
        if (d.error) this.toast(d.error, "error");
      } catch (e) { this.toast("操作失败", "error"); }
      this.refresh();
    },

    async retryAllFailed() {
      const errs = this.jobs.filter(j => j.state === "error");
      for (const j of errs) await this.jobOp("resume", j.id);
      if (errs.length) this.toast("已重试 " + errs.length + " 个失败任务", "ok");
    },
    /* v3.37 一键清理：批量删掉已结束的任务记录 + 清空统计页已完成传输水位 */
    async clearFinished() {
      if (!this.finishedCount) { this.toast("没有可清理的历史记录", "warning"); return; }
      if (!confirm("一键清理 " + this.finishedCount + " 条已结束的任务记录，并清空统计页的已完成传输记录？\n\n· 不会影响已下载到 NAS 的文件\n· 不会动下载中 / 排队中的任务\n· 之后新完成的文件仍会正常显示")) return;
      try {
        const d = await this.api("clearfinished");
        if (d.error) { this.toast(d.error, "error"); return; }
        this.transfers = [];
        this.toast("已清理 " + (d.removed ?? this.finishedCount) + " 条历史记录", "ok");
      } catch (e) { this.toast("清理失败：" + e.message, "error"); }
      this.refresh();
    },

    /* ---------- URL 直链下载 ---------- */
    async urlDl() {
      if (!this.urlForm.url || this.urlAdding) return;
      this.urlAdding = true;
      try {
        const d = await this.api("urldl", {
          url: this.urlForm.url,
          folder: this.urlForm.folder || "URL下载",
          volume: this.urlVolume || undefined,
        });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast("已提交 URL 下载任务", "ok");
        this.urlForm.url = "";
      } catch (e) { this.toast("提交失败：" + e.message, "error"); }
      this.urlAdding = false;
      this.refresh();
    },

    /* ---------- 云端文件操作 ---------- */
    copyText(t) {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(t).catch(() => {});
        return;
      }
      const ta = document.createElement("textarea");
      ta.value = t;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      ta.remove();
    },
    async cloudSpeedTest(name) {
      if (this.speedTesting) return;
      const remote = this.curRemote;
      const path = this.curPath ? this.curPath + "/" + name : name;
      const r = { name: name, direct: null, proxy: null, seconds: this.speedSeconds };
      this.speedTesting = name;
      this.speedResult = null;
      try {
        for (const [phase, label] of [["direct", "直连"], ["proxy", "经 mihomo 代理"]]) {
          this.speedStage = label + " " + this.speedSeconds + " 秒…";
          const d = await this.api("speedtest", { remote: remote, path: path, phase: phase }, 20000);
          if (d.error) { r.error = "(" + label + ") " + d.error; break; }
          r[phase] = d[phase];
          if (d.seconds) r.seconds = d.seconds;
        }
        if (!r.error && r.direct != null && r.proxy != null) {
          r.better = r.proxy > r.direct ? "代理更快" : (r.direct > r.proxy ? "直连更快" : "持平");
          this.toast("测速完成：直连 " + r.direct + " · 代理 " + r.proxy + " MiB/s —— " + r.better, "ok");
        } else if (!r.error) {
          r.error = "测速未完成，请重试";
        }
        if (r.error) this.toast("测速失败：" + r.error, "error");
      } catch (e) {
        r.error = e.message;
        this.toast("测速失败：" + e.message, "error");
      }
      this.speedResult = r;
      this.speedTesting = null;
      this.speedStage = "";
    },
    async batchCleanup() {
      const n = this.cleanupPicked.length;
      if (!n) return;
      /* v3.44：PikPak 的普通删除只是进回收站，「彻底删除」走 batchDelete 不进回收站 */
      const hard = !!(this.hardDelete && this.trashSupported());
      const tail = hard
        ? "，且不经过回收站直接彻底删除，无法恢复！"
        : (this.trashSupported()
            ? "。删除后进 PikPak 回收站，可在「回收站」里恢复或彻底删除。"
            : "，此操作不可恢复！");
      if (!window.confirm("确认删除云端 " + n + " 项" + tail)) return;
      let ok = 0, fail = 0;
      for (const name of this.cleanupPicked) {
        const isfile = this.dirFiles.some(x => x.name === name);
        const path = this.curPath ? this.curPath + "/" + name : name;
        try {
          const d = await this.api("cloudrm", { remote: this.curRemote, path, isfile, hard }, 90000);
          d.error ? fail++ : ok++;
        } catch (e) { fail++; }
      }
      this.cleanupPicked = [];
      this.scanData = null;                 // 删了东西，导出/查重的缓存必须作废
      this.toast("云端清理完成：成功 " + ok + " 个" + (fail ? "，失败 " + fail + " 个" : ""), fail && !ok ? "error" : "ok");
      this.loadDirs(this.curPath);
    },
    async cloudShare(name) {
      try {
        const d = await this.api("cloudlink", {
          remote: this.curRemote,
          path: this.curPath ? this.curPath + "/" + name : name,
        });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.copyText(d.url);
        this.toast("分享链接已复制（" + (d.via === "proxy" ? "经 mihomo 代理" : "直连") + "）", "ok");
      } catch (e) { this.toast("获取失败：" + e.message, "error"); }
    },
    async cloudRm(name, isfile) {
      const kind = isfile ? "云端文件" : "云端文件夹（含全部内容）";
      /* PikPak 的删除实际是「移入回收站」，把话说准：用户才知道还能找回/彻底删 */
      const hint = this.trashSupported()
        ? "删除后进 PikPak 回收站，可在「回收站」里恢复或彻底删除。"
        : "此操作不可恢复！";
      if (!window.confirm("确认删除" + kind + "「" + name + "」？" + hint)) return;
      try {
        const d = await this.api("cloudrm", { remote: this.curRemote, path: this.curPath ? this.curPath + "/" + name : name, isfile });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.scanData = null;
        this.toast("已删除：" + name, "ok");
      } catch (e) { this.toast("删除失败：" + e.message, "error"); }
      this.loadDirs(this.curPath);
    },
    async cloudRename(name) {
      const nn = window.prompt("重命名云端文件", name);
      if (!nn || nn === name) return;
      try {
        const d = await this.api("cloudmv", {
          remote: this.curRemote,
          path: this.curPath ? this.curPath + "/" + name : name,
          newname: nn,
        });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.scanData = null;
        this.toast("已重命名：" + name + " → " + nn, "ok");
      } catch (e) { this.toast("重命名失败：" + e.message, "error"); }
      this.loadDirs(this.curPath);
    },

    /* ---------- v3.44 批量重命名；v3.50 起云盘 / NAS 本地共用同一弹窗；v3.52 本地执行后保持窗口 ---------- */
    openRename(ctx) {
      const local = ctx === "local";
      const all = local ? this.lfRenameNames : this.dirFiles.map(f => f.name);
      if (!all.length) { this.toast("当前目录没有可改名的条目", "warn"); return; }
      this.renameCtx = local ? "local" : "cloud";
      /* 勾了条目就默认改勾选的，没勾就默认整个列表，少点一次操作 */
      const set = new Set(all);
      const src = local ? this.lfPicked : this.picked;
      this.renameScope = src.some(n => set.has(n)) ? "picked" : "all";
      this.renameMode = "template";
      this.renameResult = null;
      this.renameBusy = false;
      if (!String(this.renameTpl || "").trim()) this.renameTpl = "{name}{ext}";
      this.renameOpen = true;
    },
    closeRename() { this.renameOpen = false; this.renameCtx = "cloud"; },
    renameInsertToken(tok) {
      /* 光标处插入更符合直觉，但 input 没有 ref 时退回追加，行为始终可预期 */
      this.renameTpl = (this.renameTpl || "") + tok;
    },
    async renameApply() {
      const local = this.renameCtx === "local";
      const plan = this.renameChanged;
      if (!plan.length) { this.toast("按当前规则没有名字会变，改一下规则试试", "warn"); return; }
      if (this.renameBlocked) {
        this.toast("有 " + this.renameBlocked + " 项名字冲突（已存在或重复），先改规则", "error");
        return;
      }
      this.renameBusy = true;
      try {
        const items = plan.map(r => ({ from: r.from, to: r.to }));
        const d = local
          ? await this.api("localrenamebatch", { path: this.lfPath, items }, 120000)
          : await this.api("renamebatch", {
              remote: this.curRemote, path: this.curPath, items }, 120000);
        if (d.error) { this.toast(d.error, "error"); this.renameBusy = false; return; }
        const fail = (d.failed || []).length;
        this.renameResult = { ok: d.ok || 0, failed: d.failed || [] };
        const msg = (local ? "已在 NAS 上重命名 " : "已重命名 ") + (d.ok || 0) + " 项" +
          (fail ? "，失败 " + fail + " 项" : "");
        if (local) {
          this.lfPicked = [];
          /* v3.52：本地改名后弹窗保持打开，方便一轮接一轮地改。
             选中已清空，作用范围回到「当前列表全部」，预览按刷新后的新文件名重算 */
          this.renameScope = "all";
        } else {
          this.scanData = null;
          this.picked = [];
        }
        this.toast(msg, fail && !(d.ok || 0) ? "error" : "ok");
        /* v3.52：本地文件执行完不关窗；云盘沿用「全部成功即关闭」的旧行为 */
        if (!fail && !local) this.closeRename();
        if (local) await this.loadLocalFiles(this.lfPath);
        else this.loadDirs(this.curPath);
      } catch (e) {
        this.toast("重命名失败：" + e.message, "error");
      }
      this.renameBusy = false;
    },

    /* ---------- v3.56：本地文件 / 文件夹单项重命名 ---------- */
    /* 与云盘页 cloudRename 对称；后端复用 localrenamebatch 的冲突、越界、软链接校验与失败回滚 */
    async localRenameOne(name) {
      if (!name) return;
      const nn = window.prompt("重命名 NAS 本地文件或文件夹", name);
      if (nn == null) return;
      const to = String(nn).trim();
      if (!to || to === name) return;
      if (/[\\/]/.test(to)) { this.toast("新名字不能包含路径分隔符", "error"); return; }
      try {
        const d = await this.api("localrenamebatch", {
          path: this.lfPath, items: [{ from: name, to }]
        }, 120000);
        if (d.error) { this.toast(d.error, "error"); return; }
        if ((d.failed || []).length) {
          this.toast(d.failed[0].error || "重命名失败", "error");
          return;
        }
        this.lfPicked = (this.lfPicked || []).filter(x => x !== name);
        this.toast("已重命名：" + name + " → " + to, "ok");
        await this.loadLocalFiles(this.lfPath);
      } catch (e) { this.toast("重命名失败：" + e.message, "error"); }
    },

    /* ---------- v3.44 回收站 ---------- */
    trashSupported() {
      /* 只有 PikPak 有服务端回收站；其它网盘（openlist 等）删了就是删了 */
      const a = this.acctItems.find(x => x.name === this.curRemote);
      return !!a && a.type === "pikpak";
    },
    async openTrash() {
      if (!this.trashSupported()) {
        this.toast("只有 PikPak 账号有回收站", "warn");
        return;
      }
      this.trashOpen = true;
      this.trashItems = [];
      this.trashPicked = [];
      this.trashErr = "";
      await this.loadTrash();
    },
    closeTrash() { this.trashOpen = false; },
    async loadTrash() {
      this.trashBusy = true;
      this.trashErr = "";
      try {
        const d = await this.api("trashlist", { remote: this.curRemote }, 90000);
        if (d.error) this.trashErr = d.error;
        else this.trashItems = d.items || [];
      } catch (e) { this.trashErr = e.message; }
      this.trashBusy = false;
    },
    toggleAllTrash(on) { this.trashPicked = on ? this.trashItems.map(x => x.id) : []; },
    async trashOp(op, one) {
      /* 三种粒度：单条（one）/ 选中 / 全部清空（既不传 ids 也不传 one） */
      let ids, label;
      if (one) { ids = [one.id]; label = "「" + one.name + "」"; }
      else if (this.trashPicked.length) { ids = this.trashPicked.slice(); label = "选中的 " + ids.length + " 项"; }
      else { ids = null; label = "回收站全部 " + this.trashItems.length + " 项（" + this.fmt(this.trashTotal) + "）"; }

      if (op === "delete") {
        if (!one && !this.trashPicked.length && !this.trashItems.length) {
          this.toast("回收站是空的", "warn");
          return;
        }
        if (!window.confirm("确认彻底删除" + label + "？彻底删除后无法恢复，空间会立即释放。")) return;
      } else {
        if (!ids) { this.toast("先勾选要恢复的条目", "warn"); return; }
        if (!window.confirm("把" + label + "恢复到删除前的目录？")) return;
      }

      this.trashBusy = true;
      try {
        const d = await this.api("trashop", {
          remote: this.curRemote, op,
          ids: ids,
          all: (!ids && op === "delete") ? true : undefined,
        }, 120000);
        if (d.error) { this.toast(d.error, "error"); this.trashBusy = false; return; }
        this.toast(d.text || "完成", "ok");
        this.trashPicked = [];
        this.scanData = null;
        await this.loadTrash();
        this.loadDirs(this.curPath);
      } catch (e) { this.toast("操作失败：" + e.message, "error"); }
      this.trashBusy = false;
    },
    async emptyTrash() {
      if (!this.trashItems.length) { this.toast("回收站是空的", "warn"); return; }
      this.trashPicked = [];
      await this.trashOp("delete", null);
    },

    /* ---------- v3.44 解压（云端 / 本地共用一套弹窗）---------- */
    openUnzip(name) {
      this.unzipMode = "cloud";
      this.unzipName = name;
      this.unzipTargets = [this.curPath ? this.curPath + "/" + name : name];
      this.unzipPwd = "";
      this.unzipEntries = [];
      this.unzipInfo = null;
      this.unzipErr = "";
      this.unzipPremium = false;
      this.unzipNeedPwd = false;
      this.unzipOpen = true;
      this.loadUnzipList();
    },
    openLocalUnzip(names) {
      const list = Array.isArray(names) ? names : [names];
      if (!list.length) { this.toast("先勾选要解压的压缩包", "warn"); return; }
      this.unzipMode = "local";
      this.unzipName = list.length === 1 ? list[0] : (list.length + " 个压缩包");
      this.unzipTargets = list.map(n => (this.lfPath ? this.lfPath + "/" + n : n));
      this.unzipPwd = "";
      this.unzipEntries = [];
      this.unzipInfo = null;
      this.unzipErr = "";
      this.unzipPremium = false;
      this.unzipDeleteAfter = false;
      this.unzipOpen = true;
    },
    closeUnzip() { this.unzipOpen = false; },
    async loadUnzipList() {
      if (this.unzipMode !== "cloud") return;
      this.unzipBusy = true;
      this.unzipErr = "";
      try {
        const d = await this.api("unzipinfo", {
          remote: this.curRemote, path: this.unzipTargets[0], password: this.unzipPwd,
        }, 90000);
        if (d.error) {
          this.unzipErr = d.error;
          this.unzipNeedPwd = !!d.needPassword;
          this.unzipPremium = !!d.premium;
          this.unzipEntries = [];
        } else {
          this.unzipInfo = d;
          this.unzipEntries = d.entries || [];
        }
      } catch (e) { this.unzipErr = e.message; }
      this.unzipBusy = false;
    },
    async startUnzip() {
      if (this.unzipMode === "local") return this.startLocalUnzip();
      this.unzipBusy = true;
      try {
        const d = await this.api("unzip", {
          remote: this.curRemote, path: this.unzipTargets[0], password: this.unzipPwd,
        }, 120000);
        if (d.error) {
          this.unzipErr = d.error;
          this.unzipPremium = !!d.premium;
          this.unzipNeedPwd = !!d.needPassword;
          this.toast(d.error, "error");
          this.unzipBusy = false;
          return;
        }
        this.unzipOpen = false;
        this.toast("已提交云端解压，进度见「下载任务」", "ok", { label: "去任务页", run: () => this.go("tasks") });
      } catch (e) { this.unzipErr = e.message; this.toast("解压失败：" + e.message, "error"); }
      this.unzipBusy = false;
    },
    async startLocalUnzip() {
      const list = this.unzipTargets.slice();
      if (!list.length) return;
      this.unzipBusy = true;
      let ok = 0, fail = 0, firstErr = "";
      for (const rel of list) {
        try {
          const d = await this.api("localunzip", {
            path: rel, password: this.unzipPwd, deleteAfter: this.unzipDeleteAfter,
          }, 60000);
          if (d.error) { fail++; firstErr = firstErr || d.error; } else ok++;
        } catch (e) { fail++; firstErr = firstErr || e.message; }
      }
      this.unzipBusy = false;
      if (ok) {
        this.unzipOpen = false;
        this.lfPicked = [];
        this.toast("已提交 " + ok + " 个解压任务" + (fail ? "，" + fail + " 个失败" : ""),
                   fail && !ok ? "error" : "ok", { label: "去任务页", run: () => this.go("tasks") });
        this.reloadLocalFiles();
      } else {
        this.unzipErr = firstErr || "解压提交失败";
        this.toast(this.unzipErr, "error");
      }
    },
    /* 非会员走这条路：先把压缩包下载到 NAS，再到本地文件页解压 */
    async downloadForUnzip() {
      const name = this.unzipName;
      this.unzipOpen = false;
      await this.startFileJob(name);
    },

    /* ---------- 账号页 ---------- */
    async loadProviders() {
      try {
        const d = await this.api("providers");
        this.providers = d.providers || [];
        if (!this.providers.some(p => p.type === this.acctForm.type)) {
          this.acctForm.type = this.providers.length ? this.providers[0].type : "pikpak";
        }
      } catch (e) {}
    },
    async loadAccounts() {
      this.accountsLoading = true;
      try {
        const d = await this.api("accounts");
        /* 原地复用已有对象：单行「测试」/「全部验证」进行中刷新列表时，
           结果不会因数组被整体替换而丢失，也不会把 ok 重置成「未测试」。 */
        const old = new Map(this.accounts.filter(a => a && a.name).map(a => [a.name, a]));
        const next = (d.accounts || []).map(a => {
          const prev = old.get(a.name);
          if (prev) { prev.type = a.type; prev.email = a.email || ""; prev.cred = a.cred || null; return prev; }
          return { name: a.name, type: a.type, email: a.email || "", cred: a.cred || null,
                   ok: null, testing: false, error: "" };
        });
        this.accounts.splice(0, this.accounts.length, ...next);
        this.ghostAccounts = d.ghosts || [];
      } catch (e) { /* 保留现有列表，避免一次网络抖动把整页清空 */ }
      this.accountsLoading = false;
    },
    /* v3.51：清理 rclone 里没有 type 的残留空壳段（两个 rcd 都清），防止改名后旧名复活 */
    async purgeGhosts() {
      if (this.purgingGhosts) return;
      this.purgingGhosts = true;
      try {
        const d = await this.api("acctpurge");
        if (d.error) { this.toast(d.error, "error"); return; }
        const n = (d.removed || []).length;
        this.toast(n ? "已清理 " + n + " 个残留配置段" : "没有需要清理的残留", "ok");
        this.ghostAccounts = [];
        this.moreOpen = false;
        await this.loadAccounts();
      } catch (e) { this.toast("清理失败：" + e.message, "error"); }
      this.purgingGhosts = false;
    },
    async acctTest(a) {
      if (!a || !a.name || a.testing) return;
      a.testing = true;
      try {
        const d = await this.api("accttest", { name: a.name });
        a.ok = !!d.ok;
        a.relink = !!d.relink;                 /* v3.38：后端判定「只能重新登录」 */
        if (d.ok) { a.used = d.used; a.total = d.total; a.free = d.free; a.error = ""; }
        else a.error = d.error || "连接失败";
      } catch (e) { a.ok = false; a.error = e.message; a.relink = this.needRelinkText(e.message); }
      a.testing = false;
      /* 测试会把 lastOk / lastErr 写进身份表，拉一次列表让凭证徽标同步 */
      await this.loadAccounts();
    },
    credLabel(c) {
      if (!c) return "";
      return { ok: "凭证正常", warn: "凭证待更新", dead: "需重新登录", unknown: "未验证" }[c.state] || "未验证";
    },
    credClass(c) {
      if (!c) return "bg-base-content/8 text-base-content/50";
      if (c.state === "ok") return "bg-success/12 text-success";
      if (c.state === "dead") return "bg-error/12 text-error";
      if (c.state === "warn") return "bg-warning/15 text-warning";
      return "bg-base-content/8 text-base-content/50";
    },
    credClick(c, a) {
      /* 失效 / 待更新的账号，点徽标直接进入「为该账号重新登录」流程 */
      if (c && (c.state === "dead" || c.state === "warn")) {
        if (a) this.relinkAccount(a); else this.openTokenImport();
      }
    },
    /* v3.48：以前只有「已判死」的账号才显示这个按钮，而判死依赖身份表的 lastErr。
       下载任务里报的 captcha / no username 往往是历史快照、不会写进身份表，
       于是越需要重登、按钮越不出现（用户按提示去找却找不到）。
       现在所有 PikPak 账号常驻入口，重新采集本来就是无害的维护动作。 */
    showRelink(a) {
      return !!(a && a.type === "pikpak");
    },
    /* 凭证异常时才用主色高亮，平时保持次要按钮，避免 11 个号一屏全蓝 */
    relinkUrgent(a) {
      return !!(a && (a.relink || (a.cred && (a.cred.state === "dead" || a.cred.state === "warn"))));
    },
    acctErrorText(a) {
      const e = this.safeMsg((a && a.error) || "");
      if (/no username/i.test(e)) return "no username · 凭证缺失，请点「重新登录」重新采集";
      return e;
    },
    /* v3.68 双进程对账清理：空壳段 + 代理侧多出的残留段（账号删除后复活的根源） */
    async purgeResidual() {
      if (this.purgeBusy) return;
      this.purgeBusy = true;
      try {
        const d = await this.api("acctpurge", {}, 60000);
        if (d.error) { this.toast(d.error, "error"); return; }
        const n = (d.removed || []).length + (d.resynced || []).length;
        this.toast(n ? "已清理 " + n + " 个残留段：" + [...(d.removed || []), ...(d.resynced || [])].join("、") : "没有发现残留段", "ok");
        this.loadAccounts();
      } catch (e) { this.toast("清理失败：" + e.message, "error"); }
      this.purgeBusy = false;
    },
    async acctAdd() {
      const f = this.acctForm;
      if (this.acctAdding) return;
      /* v3.70：账号名留空不再静默失败（旧版直接 return，看起来像"点了没反应"）。
         WebDAV 可根据地址自动生成名字（rclone 账号名不允许点号，替换为连字符）。 */
      if (!f.name) {
        let auto = "";
        if (f.params && f.params.url) {
          try {
            const u = new URL(f.params.url);
            auto = "webdav-" + u.hostname.replace(/\./g, "-") +
                   (u.port && u.port !== "80" ? "-" + u.port : "");
          } catch (e) { /* url 非法时走下面的必填提示 */ }
        }
        if (!auto) { this.toast("请先填写账号名（或先填好 WebDAV 地址，可自动命名）", "error"); return; }
        f.name = auto;
        this.toast("账号名已自动生成为 " + auto + "，可在添加后用「编辑」改名", "ok");
      }
      const missing = (this.curProvider.params || []).filter(p => p.required && !f.params[p.name]);
      if (missing.length) { this.toast("请填写：" + missing.map(p => p.label).join("、"), "error"); return; }
      this.acctAdding = true;
      try {
        const d = await this.api("acctadd", { name: f.name, type: f.type, params: { ...f.params } });
        if (d.error) { this.toast(d.error, "error"); this.acctAdding = false; return; }
        this.toast("账号 " + f.name + " 添加成功，已验证连接 ✓", "ok");
        this.acctForm = { name: "", type: f.type, params: {} };
        await this.loadAccounts();
        this.acctAdding = false;
      } catch (e) { this.toast("添加失败：" + e.message, "error"); this.acctAdding = false; }
    },
    async acctDel(a) {
      if (!window.confirm("确认从控制台删除账号「" + a.name + "」？\n\n· 云端文件不受影响（仍在该 PikPak 账号内）\n· 其 refresh_token 将不再被自动续期，请自行保管")) return;
      try {
        const d = await this.api("acctdel", { name: a.name });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast("已删除账号 " + a.name, "ok");
        this.accounts = this.accounts.filter(x => x.name !== a.name);
      } catch (e) { this.toast("删除失败：" + e.message, "error"); }
    },
    async openAcctEdit(a) {
      try {
        const d = await this.api("acctget", { name: a.name });
        if (d.error) { this.toast(d.error, "error"); return; }
        const values = {};
        for (const f of d.fields || []) values[f.name] = "";
        this.acctEditForm = {
          name: d.name, newName: d.name, type: d.type, fields: d.fields || [],
          email: d.email || "", sub: d.sub || "",
          values, clearToken: d.type === "pikpak",
        };
        this.acctEditOpen = true;
      } catch (e) { this.toast("读取账号配置失败：" + e.message, "error"); }
    },
    closeAcctEdit() { this.acctEditOpen = false; this.acctEditForm = null; },
    openPikPakLogin(silent) {
      window.open("https://mypikpak.com/drive/login", "_blank", "noopener,noreferrer");
      if (!silent) this.toast("已打开 PikPak 登录页；登录完成后点书签栏的「PikPak 一键导入」即可", "ok");
    },
    /* v3.38：refresh_token 被别处刷新 / 失效时，服务端没有任何办法自行恢复，
       必须由人在浏览器里重新登录拿一份新的。这里把这个入口放到出错的地方：
       打开登录页 + 打开采集弹窗并锁定目标账号（采集后按 sub 自动覆盖原账号）。 */
    relinkAccount(a) {
      const t = a || {};
      this.relinkTarget = { name: t.name || "", email: t.email || "" };
      this.importResult = null;
      this.importText = "";
      this.importTab = "oneclick";   /* v3.65：重登默认走一键干净浏览器 */
      this.importOpen = true;        /* v3.66：先看弹窗选项，登录页由弹窗内按钮打开 */
      return
    },
    async copyRelinkEmail() {
      const e = this.relinkTarget && this.relinkTarget.email;
      if (!e) { this.toast("这个账号没有登记邮箱，请按账号名自行确认登录目标", "warning"); return; }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(e);
        } else {
          /* 控制台是局域网 http，非安全上下文，clipboard API 不可用 */
          const ta = document.createElement("textarea");
          ta.value = e; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          const ok = document.execCommand("copy");
          document.body.removeChild(ta);
          if (!ok) throw new Error("copy failed");
        }
        this.toast("已复制登录邮箱：" + e, "ok");
      } catch (err) { this.toast("复制失败，请手动选中邮箱复制", "error"); }
    },
    // 书签链接按当前访问地址现场生成：换 NAS 地址 / 换端口都不用重新做书签
    bmHref() {
      const fn = window.PK_BOOKMARKLET;
      if (typeof fn !== "function") return "javascript:void 0";
      const call = "(" + fn.toString() + ")(" + JSON.stringify(location.origin) + ")";
      return "javascript:" + encodeURIComponent(call);
    },
    bmHint() {
      this.toast("请按住这个按钮拖到浏览器书签栏（只做一次）；之后在 PikPak 登录页点它", "ok");
    },
    async saveAcctEdit(renew) {
      const f = this.acctEditForm;
      if (!f || this.acctEditSaving) return;
      const params = {};
      for (const k of Object.keys(f.values)) {
        const v = (f.values[k] || "").trim();
        if (v) params[k] = v;
      }
      const newName = (f.newName || "").trim();
      const renamed = !!newName && newName !== f.name;
      if (!Object.keys(params).length && !renew && !renamed) {
        this.toast("没有填写任何要修改的内容", "error"); return;
      }
      if (renamed && !/^[\w\u4e00-\u9fff.\-]{1,64}$/.test(newName)) {
        this.toast("账号名只能用中英文、数字、_ - .，最长 64 字符", "error"); return;
      }
      this.acctEditSaving = true;
      try {
        let name = f.name;
        if (renamed) {
          const r = await this.api("acctrename", { name: f.name, newName });
          if (r.error) { this.toast(r.error, "error"); this.acctEditSaving = false; return; }
          name = r.name || newName;
        }
        if (Object.keys(params).length || renew) {
          const d = await this.api("acctedit", {
            name, params, refresh: !!renew,
            clearToken: !!(f.clearToken && params.refresh_token),
          });
          if (d.error) {
            /* v3.38：换发失败（refresh_token 被别处刷新）时，报错里直接给重登入口 */
            this.toastError(d.error, { name, email: (f && f.email) || "" });
            this.acctEditSaving = false; return;
          }
          this.toast("账号 " + name + " 已更新并通过连接验证", "ok");
        } else {
          this.toast("账号已重命名为 " + name, "ok");
        }
        this.closeAcctEdit();
        await this.loadAccounts();
      } catch (e) {
        this.toastError("保存失败：" + e.message,
          this.acctEditForm ? { name: this.acctEditForm.name, email: this.acctEditForm.email || "" } : null);
      }
      this.acctEditSaving = false;
    },
    async repairAccounts() {
      if (this.repairing) return;
      if (!window.confirm("一键修复会影响所有 PikPak 账号：\n\n· token 为空的，用现有 refresh_token 重新换发\n· 被 PikPak 风控（captcha 报错）的，自动更换 device_id\n\n继续？")) return;
      this.repairing = true;
      try {
        const d = await this.api("acctrepair", {});
        if (d.error) { this.toast(d.error, "error"); return; }
        const bad = (d.results || []).filter(r => !r.ok);
        this.toast("修复完成：" + d.okCount + "/" + d.total + " 个可用" +
          (bad.length ? "；失败：" + bad.map(r => r.name + "（" + r.msg + "）").join("；") : ""),
          bad.length ? "error" : "ok");
        await this.loadAccounts();
      } catch (e) { this.toast("修复失败：" + e.message, "error"); }
      this.repairing = false;
    },

    /* ---------- Token 导入 / 采集 ---------- */
    openTokenImport() {
      this.importOpen = true; this.importResult = null;
      this.relinkTarget = null; this.importTab = "oneclick";   /* 普通导入不锁定账号 */
    },
    closeTokenImport() { this.importOpen = false; this.relinkTarget = null; },

    /* 统一的结果汇总 + toast */
    summarizeImport(d, label) {
      this.importResult = d;
      const rs = d.results || [];
      const created = rs.filter(x => x.ok && x.action === "新建").length;
      const updated = rs.filter(x => x.ok && x.action === "更新").length;
      const skipped = rs.filter(x => x.action === "跳过").length;
      const bad = rs.filter(x => !x.ok);
      const summary = label + "：新建 " + created + " · 更新 " + updated + " · 已正常 " + skipped +
        (bad.length ? "；失败 " + bad.length + " 个：" +
          bad.map(x => (x.target || x.env || "?") + "（" + (x.msg || "") + "）").join("；") : "");
      /* v3.38：导入结果里出现「凭证失效」时，汇总 toast 也带上重登入口 */
      if (bad.length && this.relinkTarget && this.needRelinkText(bad.map(x => x.msg || "").join(" "))) {
        const t = { ...this.relinkTarget };
        this.toast(summary, "error", { label: "重新登录采集", run: () => this.relinkAccount(t) });
      } else {
        this.toast(summary, bad.length ? "error" : "ok");
      }
    },

    async doImportText() {
      if (this.importing) return;
      const text = this.importText || "";
      if (!text.trim()) return;
      this.importing = true;
      try {
        /* v3.38：定向重登时把目标账号一起交给后端，且禁止自动新建 */
        const d = await this.api("acctimporttext", {
          text,
          create: this.relinkTarget ? false : this.importCreate,
          target: this.relinkTarget ? this.relinkTarget.name : "",
        });
        if (d.error) { this.toastError(d.error, this.relinkTarget); this.importing = false; return; }
        this.summarizeImport(d, "导入完成");
        this.importOpen = false;
        this.importText = "";
        this.relinkTarget = null;
        await this.loadAccounts();
      } catch (e) { this.toast("导入失败：" + e.message, "error"); }
      this.importing = false;
    },

    async identifyAccounts() {
      this.moreOpen = false;
      if (this.identifying) return;
      this.identifying = true;
      try {
        const d = await this.api("acctident", {});
        if (d.error) { this.toast(d.error, "error"); }
        else {
          const bad = (d.results || []).filter(r => !r.ok);
          this.toast("身份识别：" + d.okCount + "/" + d.total + " 个已登记到 NAS" +
            (bad.length ? "；失败：" + bad.map(r => r.name + "（" + r.msg + "）").join("；") : ""),
            bad.length ? "error" : "ok");
        }
      } catch (e) { this.toast("识别失败：" + e.message, "error"); }
      this.identifying = false;
    },

    /* 低频批量动作：合并原来的「全部验证」+「一键修复」。
       acctrepair 内部已逐号真连接验证（pikpak_verify）并自愈 device_id，
       随后再跑一次全量验证，顺带把每行的容量信息刷新出来。 */
    async checkAndRepair() {
      this.moreOpen = false;
      if (this.repairing || this.testingAll) return;
      const list = [...this.namedAccounts];
      if (!list.length) return;
      this.repairing = true;
      try {
        const d = await this.api("acctrepair", {});
        if (d.error) {
          this.toast(d.error, "error");
        } else {
          await this.testAllAccounts(true);
          const ok = d.okCount || 0, total = d.total || list.length;
          this.toast("检查并修复完成：" + ok + "/" + total + " 个可用" +
            (ok === total ? "" : "，异常账号见各行提示"),
            ok === total ? "ok" : "error");
        }
      } catch (e) { this.toast("检查并修复失败：" + e.message, "error"); }
      finally { this.repairing = false; }
    },
    /* v3.49：账号页「一键测试全部」直接调用本方法（silent=false）；
       检查并修复仍以 silent=true 复用它，行为不变，只是多了进度计数。 */
    async testAllAccounts(silent) {
      if (this.testingAll) return;
      const list = [...this.namedAccounts];
      if (!list.length) return;
      this.testingAll = true;
      this.testTotal = list.length;
      this.testDone = 0;
      let idx = 0;
      const worker = async () => {
        while (idx < list.length) {
          const a = list[idx++];
          try { await this.acctTest(a); }
          finally { this.testDone++; }
        }
      };
      try { await Promise.all([worker(), worker(), worker()]); }
      finally { this.testingAll = false; }
      /* 每行测试都会写身份表，最后再拉一次列表让凭证徽标同步 */
      await this.loadAccounts();
      const ok = list.filter(a => a.ok === true).length;
      if (!silent) {
        this.toast("一键测试完成：" + ok + "/" + list.length + " 个正常" +
          (ok === list.length ? "" : "，异常账号见各行提示"),
          ok === list.length ? "ok" : "error");
      }
    },
    async loadHealth() {
      try {
        const d = await this.api("health");
        if (!d.error) this.health = d;
      } catch (e) {}
    },

    /* ---------- 本地目录（保存目录弹窗点选） ---------- */
    async loadLocalDirs() {
      try {
        const d = await this.api("localdirs");
        /* v3.57：后端返回分组 volumes（每个存储空间一组）；点选列表仍按当前选中卷展开 */
        this.dirVolumes = d.volumes || [];
        const cur = this.dirVolumes.find(v => v.root === (this.dlVolume || (this.dirVolumes[0] || {}).root));
        this.localDirs = cur ? cur.dirs : (d.dirs || []);
      } catch (e) {}
    },
    switchDlVolume(root) {
      this.dlVolume = root;
      if (this.urlForm) this.urlVolume = root;
      this.loadLocalDirs();
    },
    openPicker(t) {
      if (this.pickerTarget !== t) this.loadLocalDirs();
      this.pickerTarget = t;
    },
    pickDir(d) {
      if (this.pickerTarget === "dl") this.dlFolder = d;
      else if (this.pickerTarget === "url") this.urlForm.folder = d;
      this.pickerTarget = "";
    },

    /* ---------- 统计页 ---------- */
    async loadHistory() {
      try {
        const d = await this.api("history");
        this.histData = d;
        await this.$nextTick();
        this.updateHistChart();
        this.updateDailyChart();
      } catch (e) {}
    },
    async loadAbout() {
      if (!this.remotes.length) return;
      this.usageLoading = true;
      try {
        const d = await this.api("about", { remotes: this.remotes });
        this.accountUsage = d.accounts || [];
      } catch (e) { /* 保留上一次容量数据 */ }
      this.usageLoading = false;
    },
    async loadTransfers() {
      try {
        const d = await this.api("transfers");
        this.transfers = d.list || [];
      } catch (e) {}
    },
    /* 隐私开关：统计页两张卡片的文件名一起打码，状态持久化到本机浏览器 */
    toggleHideNames() {
      this.hideNames = !this.hideNames;
      localStorage.setItem("pk.hideNames", this.hideNames ? "1" : "");
    },
    async clearTransfers() {
      try {
        const d = await this.api("cleartransfers");
        if (d.error) { this.toast(d.error, "error"); return; }
        this.transfers = [];
        this.toast("已完成传输记录已清空", "ok");
        await this.loadTransfers();
      } catch (e) { this.toast("清空失败：" + e.message, "error"); }
    },
    /* 传输配额：官方私有接口，后端按账号缓存 5 分钟；前端再节流 60 秒，
       不跟随 2 秒轮询打接口。切账号/点刷新/开详情时才取。 */
    async loadQuota(force = false) {
      if (this.quotaLoading) return;
      if (!force && Date.now() - this.quotaFetchedAt < 60000) return;
      const remotes = this.remotes.length ? this.remotes : (this.curRemote ? [this.curRemote] : []);
      if (!remotes.length) return;
      this.quotaLoading = true;
      try {
        const d = await this.api("quota", { remotes, force: !!force }, 120000);
        this.quota = d.accounts || [];
        this.quotaFetchedAt = Date.now();
        if (!this.quotaRemote) this.quotaRemote = this.curRemote;
      } catch (e) {
        this.quota = remotes.map(r => ({ remote: r, error: (e && e.message) || "读取失败" }));
      }
      this.quotaLoading = false;
    },
    quotaPct(it) { return it && it.total > 0 ? Math.min(100, it.used / it.total * 100) : 0; },
    quotaBar(it) {
      const p = this.quotaPct(it);
      return p >= 90 ? "bg-error" : p >= 70 ? "bg-warning" : "bg-primary";
    },
    openQuota() {
      this.quotaRemote = this.curRemote;
      this.quotaOpen = true;
      this.loadQuota(false);
    },
    loadStatsPage() { this.loadHistory(); this.loadAbout(); this.loadTransfers(); this.loadHealth(); },
    acctPct(a) { return a.total ? Math.min(100, a.used / a.total * 100).toFixed(1) : 0; },
    acctBar(a) {
      const p = a.total ? a.used / a.total : 0;
      return p >= .9 ? "bg-error" : p >= .7 ? "bg-warning" : "bg-success";
    },
    /* ---------- 限速 ---------- */
    async loadBw() {
      try {
        const d = await this.api("bwlimit");
        if (!d.error) this.bwBps = d.bytesPerSecond;
      } catch (e) {}
    },
    async applyBw() {
      try {
        const d = await this.api("setbwlimit", { rate: this.bwSel });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.bwBps = d.bytesPerSecond;
        this.toast("限速已应用：" + (this.bwSel === "off" ? "不限速" : this.bwSel), "ok");
      } catch (e) { this.toast("设置失败：" + e.message, "error"); }
    },

    /* ---------- 设置 ---------- */
    fillForm() {
      const s = this.settings || {};
      this.form = {
        maxConcurrent: s.maxConcurrent ?? 2,
        autoStart: s.autoStart || "",
        autoPause: s.autoPause || "",
        diskMinGB: s.diskMinGB ?? 20,
        diskQuotaGB: s.diskQuotaGB ?? 0,   // v3.57 存储空间配额（0 = 跟随实际容量）
        nasHomeUrl: s.nasHomeUrl || DEFAULT_NAS_HOME,
        proxyUrl: s.proxyUrl || "",
        pkProxyMode: s.pkProxyMode || "follow",
        transfers: s.transfers ?? 6,
        checkers: s.checkers ?? 8,
        proxy: !!s.proxy,
        downloadProxy: s.downloadProxy ?? true,
        advancedMode: !!s.advancedMode,
        multiThreadStreams: s.multiThreadStreams ?? 8,
        multiThreadCutoffMB: s.multiThreadCutoffMB ?? 64,
        orderBy: s.orderBy ?? "size,descending",
        bwLimitOn: !!s.bwLimitOn,
        bwLimit: s.bwLimit || "5M",
        retries: s.retries ?? 10,
        timeout: s.timeout || "1m",
        idleTimeout: s.idleTimeout || "10m",
        createEmptySrcDirs: s.createEmptySrcDirs ?? true,
        logLevel: s.logLevel || "INFO",
        logFile: s.logFile || "",
      };
    },
    async resetSettings() {
      if (!confirm("确定把所有设置恢复为默认值？\n（不影响任务记录、账号和已下载文件）")) return;
      this.saving = true;
      try {
        const d = await this.api("resetsettings");
        if (!d.ok) { this.toast("恢复失败", "error"); return; }
        this.settings = d.settings;
        this.effectiveProxyUrl = d.effectiveProxyUrl || "";
        if (d.proxyScope !== undefined) this.proxyScope = d.proxyScope;
        this.fillForm();
        this.toast("已恢复全部默认设置", "ok");
      } catch (e) { this.toast("恢复失败：" + e.message, "error"); }
      this.saving = false;
      this.refresh();
    },
    toggleProxy(v) {
      this.form.proxy = !!v;
      this.saveSettings();
    },
    toggleDownloadProxy(v) {
      this.form.downloadProxy = !!v;
      this.saveSettings();
    },
    /* 新任务默认走哪条路：以服务端设置为准，未进过设置页时回落到表单默认值 */
    dlProxy() {
      return !!(this.settings.downloadProxy ?? this.form.downloadProxy ?? true);
    },
    async reproxyJob(id, proxy) {
      try {
        const d = await this.api("jobreproxy", { id, proxy });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast(proxy ? "已切换为代理下载并重启任务" : "已切换为直连下载并重启任务", "ok");
        if (this.detailOpen) this.loadDetail(true);
      } catch (e) { this.toast("切换失败：" + e.message, "error"); }
      this.refresh();
    },
    async saveSettings() {
      if (!/^https?:\/\/\S+$/i.test(String(this.form.nasHomeUrl || "").trim())) {
        this.toast("NAS 主页地址须以 http:// 或 https:// 开头，且不能含空格", "error");
        return;
      }
      const q = Number(this.form.diskQuotaGB);
      if (!isFinite(q) || q < 0) {
        this.toast("存储空间配额须为不小于 0 的数字（0 = 不限制，跟随实际容量）", "error");
        return;
      }
      this.form.diskQuotaGB = q;
      const pu = String(this.form.proxyUrl || "").trim();
      if (pu && !/^(?:(?:https?|socks5h?):\/\/\S+|off|none|direct|disabled)$/i.test(pu)) {
        this.toast("代理地址须以 http(s):// 或 socks5:// 开头，或填 off 关闭代理", "error");
        return;
      }
      this.saving = true;
      try {
        const d = await this.api("savesettings", { ...this.form });
        if (d.ok) {
          this.toast("已保存 ✓（定时状态已重新武装）" + (d.proxyNote ? " · " + d.proxyNote : ""), "ok");
          this.settings = d.settings;
          this.effectiveProxyUrl = d.effectiveProxyUrl || "";
          if (d.proxyScope !== undefined) this.proxyScope = d.proxyScope;
        } else this.toast("保存失败", "error");
      } catch (e) { this.toast("保存失败：" + e.message, "error"); }
      this.saving = false;
      this.refresh();
    },

    /* ---------- v3.42 我的分享管理 / 提取上限自动取消 ---------- */
    myShareKey(r) { return r.remote + "|" + r.shareId; },
    openMyShares() {
      this.myShareOpen = true;
      this.myShareErr = "";
      this.loadMyShares();
    },
    closeMyShares() { if (!this.myShareBusy) this.myShareOpen = false; },
    async loadMyShares(force) {
      if (this.myShareLoading) return;
      this.myShareLoading = true;
      this.myShareErr = "";
      try {
        const d = await this.api("myshares", { force: !!force }, 120000);
        if (d.error) { this.myShareErr = d.error; return; }
        const lim = d.limits || {};
        this.myShares = (d.shares || []).map(r => ({
          ...r,
          limitDraft: lim[this.myShareKey(r)] ? String(lim[this.myShareKey(r)].limit) : "" }));
        this.myShareLimits = lim;
        this.myShareLog = d.log || [];
        this.myShareAuto = !!d.autoCancel;
        if (d.errors && d.errors.length)
          this.myShareErr = "部分账号读取失败：" + d.errors.join("；");
      } catch (e) { this.myShareErr = (e && e.message) || "读取失败"; }
      finally { this.myShareLoading = false; }
    },
    async toggleShareAuto() {
      try {
        const d = await this.api("savesettings", { shareAutoCancel: !!this.myShareAuto });
        if (d.error) { this.myShareErr = d.error; return; }
        this.toast(this.myShareAuto ? "已开启：提取次数达上限自动取消分享" : "已关闭自动取消", "ok");
      } catch (e) {
        this.myShareErr = (e && e.message) || "保存失败";
        this.myShareAuto = !this.myShareAuto;
      }
    },
    async saveMyShareLimit(r) {
      const k = this.myShareKey(r);
      this.myShareBusy = k;
      this.myShareErr = "";
      try {
        const n = Math.max(0, Math.min(100000, parseInt(r.limitDraft, 10) || 0));
        const d = await this.api("sharelimit", { remote: r.remote, shareId: r.shareId, limit: n });
        if (d.error) { this.myShareErr = d.error; return; }
        this.myShareLimits = d.limits || {};
        r.limitDraft = n ? String(n) : "";
        this.toast(n ? ("已设上限 " + n + " 次，达到后自动取消") : "已清除上限", "ok");
      } catch (e) { this.myShareErr = (e && e.message) || "保存失败"; }
      finally { this.myShareBusy = ""; }
    },
    async delMyShare(r) {
      if (!window.confirm("确认取消这条分享链接？\n\n取消后链接立即失效，云端文件本身不受影响。")) return;
      const k = this.myShareKey(r);
      this.myShareBusy = k;
      this.myShareErr = "";
      try {
        const d = await this.api("sharedel", { remote: r.remote, shareId: r.shareId });
        if (d.error) { this.myShareErr = d.error; return; }
        this.myShares = this.myShares.filter(x => this.myShareKey(x) !== k);
        delete this.myShareLimits[k];
        this.toast("已取消分享", "ok");
      } catch (e) { this.myShareErr = (e && e.message) || "取消失败"; }
      finally { this.myShareBusy = ""; }
    },
    async copyShareUrl(r) {
      const u = r.url || ("https://mypikpak.com/s/" + r.shareId);
      try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(u);
        else {
          const ta = document.createElement("textarea");
          ta.value = u; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          document.execCommand("copy"); document.body.removeChild(ta);
        }
        this.toast("分享链接已复制", "ok");
      } catch (e) { this.toast("复制失败：" + e.message, "error"); }
    },

    /* ---------- v3.41 外部分享链接：解析 / 透视 / 转存 ---------- */
    async openShareSave() {
      if (!this.accounts.length) await this.loadAccounts();
      const bad = (this.accounts || []).find(a => a.name === this.curRemote &&
                                                 a.type && a.type !== "pikpak");
      this.shareOpen = true;
      this.shareErr = "";
      this.shareInfo = null;
      this.shareItems = [];
      this.sharePicked = [];
      this.sharePath = [];
      this.shareNextToken = "";
      this.shareForm = { text: "", passCode: "",
                         remote: bad ? (this.pikpakRemotes[0] || this.curRemote) : this.curRemote,
                         dstPath: bad ? "" : (this.curPath || "") };
    },
    closeShareSave() {
      if (this.shareBusy) return;
      this.shareOpen = false;
      this.shareErr = "";
    },
    async parseShare(parentId, crumbName, keepPath) {
      if (this.shareBusy) return;
      this.shareBusy = true;
      this.shareErr = "";
      try {
        const d = await this.api("shareparse", {
          text: this.shareForm.text,
          passCode: this.shareForm.passCode,
          remote: this.shareForm.remote,
          parentId: parentId || "",
          /* 进子目录时复用已拿到的 token，避免重复用提取码换 token */
          passCodeToken: parentId && this.shareInfo ? this.shareInfo.passCodeToken : "",
          title: this.shareInfo ? this.shareInfo.title : "",
        }, 60000);
        if (d.error) { this.shareErr = d.error; return; }
        this.shareInfo = d.info || null;
        this.shareItems = d.items || [];
        this.sharePicked = this.shareItems.map(x => x.id).filter(Boolean);
        this.shareNextToken = d.nextPageToken || "";
        if (!keepPath) {
          if (parentId) this.sharePath.push({ id: parentId, name: crumbName || "子目录" });
          else this.sharePath = [];
        }
      } catch (e) { this.shareErr = (e && e.message) || "解析失败"; }
      finally { this.shareBusy = false; }
    },
    enterShareFolder(it) {
      if (it && it.isdir && it.id) this.parseShare(it.id, it.name);
    },
    jumpShareCrumb(i) {
      const id = i < 0 ? "" : this.sharePath[i].id;
      this.sharePath = i < 0 ? [] : this.sharePath.slice(0, i + 1);
      this.parseShare(id, "", true);
    },
    toggleShareAll(on) {
      this.sharePicked = on ? this.shareItems.map(x => x.id).filter(Boolean) : [];
    },
    async startShareSave() {
      if (this.shareBusy) return;
      if (!this.shareInfo) { this.shareErr = "请先解析分享链接"; return; }
      if (!this.shareForm.remote) { this.shareErr = "请选择目标账号"; return; }
      if (!this.sharePicked.length) { this.shareErr = "请至少勾选一个文件或文件夹"; return; }
      this.shareBusy = true;
      this.shareErr = "";
      try {
        const d = await this.api("sharesave", {
          remote: this.shareForm.remote,
          shareId: this.shareInfo.shareId,
          passCodeToken: this.shareInfo.passCodeToken,
          fileIds: this.sharePicked,
          /* 在分享子目录里勾选时，必须把层级 id 一起交给 PikPak */
          ancestorIds: this.sharePath.map(p => p.id),
          dstPath: this.shareForm.dstPath || "",
          title: this.shareInfo.title || "",
        }, 120000);
        if (d.error) { this.shareErr = d.error; return; }
        this.shareOpen = false;
        this.toast("已提交分享转存 → " + this.shareForm.remote + ":" +
                   (this.shareForm.dstPath || "/"), "ok");
        this.go("tasks");
      } catch (e) { this.shareErr = (e && e.message) || "提交失败"; }
      finally { this.shareBusy = false; }
    },

    /* ---------- 跨账号转移弹窗（v3.34 / v3.35 单文件 / v3.39 根目录可挑源 / v3.46 多源批量） ---------- */
    async openXfer(path, isFile, pickMode) {
      const p = String(path || "").replace(/^\/+|\/+$/g, "");
      if (!pickMode && !p) {
        this.toast("请先选中要转移的文件夹或文件", "warning");
        return;
      }
      /* 先拿到账号类型再打开弹窗，否则 openlist 这类非 PikPak 目标会短暂混进下拉 */
      if (!this.accounts.length) await this.loadAccounts();
      const others = this.remotes.filter(r => {
        if (r === this.curRemote) return false;
        const a = (this.accounts || []).find(x => x.name === r);
        return !a || !a.type || a.type === "pikpak";
      });

      /* 源怎么定（v3.46）：入口不同，path 的含义也不同 ——
         · 工具栏按钮（pickMode=true）传进来的 path 其实就是当前目录，不能当「单源」，
           否则在子目录里勾选多项会被它盖掉。此时按：勾选优先 → 当前文件夹 → 弹窗清单。
         · 列表行 ⇄（pickMode 假）传的是精确路径，就那一个。 */
      let items = [];
      let needPick = false;
      if (pickMode) {
        if (this.picked.length) {
          items = this.picked.map(n => ({
            name: n, path: this.fullPath(n),
            isFile: this.dirFiles.some(f => f.name === n),
          }));
        } else if (this.curPath) {
          items = [{ name: this.curPath.split("/").pop(), path: this.curPath, isFile: false }];
        } else {
          needPick = true;
        }
      } else {
        items = [{ name: p.split("/").pop(), path: p, isFile: !!isFile }];
      }

      this.xferErr = "";
      this.xferBusy = false;
      this.xferProg = "";
      this.xferNeedPick = needPick;
      this.xferItems = items;
      this.xferPickSet = items.map(i => (i.isFile ? "f:" : "d:") + i.name);
      this.xferForm = {
        srcRemote: this.curRemote,
        dstRemote: others[0] || "",
        dstPath: "",
        mode: this.mode === "move" ? "move" : "copy",
        method: "share",
        proxy: this.settings.downloadProxy !== false,
      };
      this.xferOpen = true;
    },
    closeXfer() {
      if (this.xferBusy) return;
      this.xferOpen = false;
      this.xferErr = "";
      this.xferProg = "";
      this.xferNeedPick = false;
      this.xferItems = [];
      this.xferPickSet = [];
    },
    async startXfer() {
      if (this.xferBusy) return;
      const f = this.xferForm;
      const items = this.xferChosen;
      if (!items.length) { this.xferErr = "请至少选择一个要转移的文件夹或文件"; return; }
      if (!f.dstRemote) { this.xferErr = "请选择目标账号"; return; }
      const dstPath = String(f.dstPath || "").trim().replace(/^\/+|\/+$/g, "");
      if (f.dstRemote === f.srcRemote && !dstPath) {
        this.xferErr = "同账号转移需要填写一个不同的目标目录（后端支持同账号搬目录）"; return;
      }
      if (dstPath.split("/").includes("..")) {
        this.xferErr = "目标目录不能包含 .."; return;
      }
      /* v3.46：提前拦掉「目标落进 PikPak 暂存区」的必然失败组合。
         转存落点固定是 <目标账号>:Pack From Shared/...；若目标顶层也叫这个名字，
         目标就成了源的父目录 → rclone 判 overlapping；且根上本来就有同名暂存目录。 */
      const topSeg = dstPath ? dstPath.split("/")[0] : "";
      for (const it of items) {
        if ((topSeg || it.name) === "Pack From Shared") {
          this.xferErr = "「Pack From Shared」是 PikPak 的转存暂存目录，不能当目标根，也不能把这个同名文件夹放到根目录。"
            + "请改选一个子目录作为目标，或只选择它里面的内容再转移。";
          return;
        }
      }
      this.xferBusy = true;
      this.xferErr = "";
      const results = [];
      try {
        for (let i = 0; i < items.length; i++) {
          const it = items[i];
          this.xferProg = items.length > 1 ? (i + 1) + "/" + items.length : "";
          try {
            const d = await this.api("xferstart", {
              srcRemote: f.srcRemote, path: it.path, dstRemote: f.dstRemote,
              dstPath, mode: f.mode, method: f.method, proxy: !!f.proxy,
            }, 120000);
            results.push(d.error ? { name: it.name, ok: false, err: d.error }
                                 : { name: it.name, ok: true });
          } catch (e) {
            results.push({ name: it.name, ok: false, err: (e && e.message) || "提交失败" });
          }
        }
        const bad = results.filter(r => !r.ok);
        if (!bad.length) {
          this.xferOpen = false;
          this.toast(items.length > 1
            ? "已开始 " + items.length + " 个转移任务 → " + f.dstRemote + ":" + (dstPath || "/")
            : "已开始转移 → " + f.dstRemote + ":" + (dstPath || "/"));
          this.go("tasks");
        } else if (bad.length === results.length) {
          this.xferErr = "全部提交失败：" + bad.map(b => b.name + "（" + b.err + "）").join("；").slice(0, 320);
        } else {
          this.xferOpen = false;
          this.toast("已提交 " + (results.length - bad.length) + "/" + results.length
            + " 个，失败：" + bad.map(b => b.name).join("、"), "error");
          this.go("tasks");
        }
      } finally {
        this.xferBusy = false;
        this.xferProg = "";
      }
    },

    /* ---------- 任务详情弹窗 ---------- */
    async openDetail(id) {
      this.detailId = id;
      this.detailOpen = true;
      this.detail = null;
      await this.loadDetail();
    },
    async loadDetail(silent) {
      if (!this.detailOpen || this.detailId == null) return;
      if (!silent) this.detailLoading = true;
      try {
        const d = await this.api("jobdetail", { id: this.detailId });
        if (this.detailOpen) this.detail = d;
      } catch (e) {}
      this.detailLoading = false;
    },
    closeDetail() {
      this.detailOpen = false;
      this.detail = null;
      this.detailId = null;
    },
    /* v3.42：详情弹窗「磁盘文件」→ 本地文件页，并定位到该任务的下载目录。
       目录若已被移动/删除，自动退到上一级，避免只看到一句冷冰冰的「不存在」。 */
    async gotoLocalFromDetail() {
      const j = this.detail && this.detail.job;
      if (!j) return;
      const p = this.destToLocal(j.dest);
      this.closeDetail();
      if (this.player) this.closePlayer();
      this.page = "files";
      this.themeOpen = false;
      this.pickerTarget = "";
      await this.loadLocalFiles(p || "");
      if (this.lfError && p) {
        const segs = p.split("/").filter(Boolean).slice(0, -1);
        const parent = p.startsWith("/") && segs.length ? "/" + segs.join("/") : segs.join("/");
        await this.loadLocalFiles(parent);
        if (!this.lfError) this.toast("原下载目录已不在 NAS，已打开上一级目录", "warning");
      }
    },
    async cleanupJob(dest) {
      if (!window.confirm("确认删除该任务在磁盘上的全部下载文件？此操作不可恢复。")) return;
      try {
        const d = await this.api("cleanup", { dest });
        if (d.error) { this.toast(d.error, "error"); return; }
        this.toast("已清理，释放 " + this.fmt(d.freed), "ok");
        this.loadDetail(true);
        this.refresh();
      } catch (e) { this.toast("清理失败：" + e.message, "error"); }
    },

    /* 页面切换 cross-fade（§9.3）—— JS 钩子驱动，定时器完成，
       避免 rAF 被节流时 transitionend 永不触发导致页面卡空 */
    pageLeave(el, done) {
      el.style.transition = "opacity 200ms var(--ease-soft)";
      el.style.opacity = "0";
      setTimeout(done, 210);
    },
    pageEnter(el, done) {
      el.style.opacity = "0";
      el.style.transition = "opacity 200ms var(--ease-soft)";
      setTimeout(() => {
        el.style.opacity = "1";
        setTimeout(done, 210);
      }, 30);
    },

    /* ---------- 本地文件页（v3.15，只读） ---------- */
    lfUrl(f, dl) {
      const segs = (this.lfPath ? this.lfPath.split("/").filter(Boolean) : []).concat(f.name);
      return "/media/" + segs.map(s => encodeURIComponent(s)).join("/") + (dl ? "?dl=1" : "");
    },
    fmtTime(ts) {
      if (!ts) return "--";
      return new Date(ts * 1000).toLocaleString("zh-CN",
        { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    },
    /* v3.44：PikPak 的 created_time 是 ISO 字符串（不是秒级时间戳），别喂给 fmtTime */
    fmtIso(s) {
      if (!s) return "--";
      const d = new Date(s);
      if (isNaN(d.getTime())) return String(s).slice(0, 16).replace("T", " ");
      return d.toLocaleString("zh-CN",
        { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    },
    async loadLocalFiles(path) {
      /* v3.58：请求序号防竞态——快速连点 / 上一级与子目录并发时，
         晚到的旧响应不得覆盖新目录（旧版会让错误横幅与正确列表同屏） */
      const seq = (this._lfSeq = (this._lfSeq || 0) + 1);
      this.lfLoading = true;
      this.lfError = "";
      try {
        const d = await this.api("localfiles", { path: path || "" });
        if (seq !== this._lfSeq) return;
        if (d.error) {
          this.lfError = d.error;
          /* v3.64：失败不再保留上一目录的列表（旧版错误横幅与旧内容同屏），
             并自动回退一次——先试所属卷根，再不行回顶层（_lfFalling 防循环） */
          this.lfDirs = [];
          this.lfFiles = [];
          this.lfPicked = [];
          if (path && !this._lfFalling) {
            this._lfFalling = true;
            const vol = (this.state.storages || []).find(v => {
              const root = String(v.console || "").replace(/\/+$/, "");
              return root && (path === root || path.indexOf(root + "/") === 0);
            });
            const target = vol ? vol.console.replace(/\/+$/, "") : "";
            const from = path;
            this.loadLocalFiles(target).finally(() => { this._lfFalling = false; });
            this.toast("目录不可访问（" + d.error + "），已回到 " + (vol ? vol.name : "存储空间顶层"), "warning");
            void from;
            return;
          }
        } else {
          this.lfPath = d.path || "";
          this.lfVolumes = d.volumes || this.state.storages || [];
          this.lfDirs = d.dirs || [];
          this.lfFiles = d.files || [];
          this.lfTruncated = !!d.truncated;
          this.lfQuery = "";
          this.lfPicked = [];          // v3.44 换目录后旧的勾选不再对应任何文件
          /* v3.64：进入目录自动统计各子文件夹占用的磁盘大小（并发 3 渐进填充，
             结果缓存到 lfSizes，重复进入不重复扫描） */
          this.statLocalSizes();
        }
      } catch (e) {
        if (seq !== this._lfSeq) return;
        this.lfError = "读取失败：" + e.message;
      } finally {
        if (seq === this._lfSeq) this.lfLoading = false;
      }
    },
    reloadNow() { location.reload(); },   // v3.58 升级提示横幅用
    /* 控制台挂载点 → 卷显示名（面包屑/提示用） */
    volumeNameOf(consolePath) {
      const v = (this.lfVolumes.length ? this.lfVolumes : this.state.storages)
        .find(v => v.console === consolePath);
      return v ? v.name : "";
    },
    reloadLocalFiles() { return this.loadLocalFiles(this.lfPath); },
    goLf(path) { return this.loadLocalFiles(path || ""); },
    openLfDir(name) {
      /* v3.57：顶层卷条目带 volumeRoot，点进去直接用该卷的绝对路径 */
      const d = this.lfDirs.find(x => x.name === name);
      if (d && d.volumeRoot) return this.loadLocalFiles(d.volumeRoot);
      this.loadLocalFiles((this.lfPath ? this.lfPath.replace(/\/$/, "") + "/" : "") + name);
    },
    lfUp() {
      /* 卷根的上一级 = 顶层存储空间列表 */
      if (this.volumeNameOf(this.lfPath)) return this.loadLocalFiles("");
      const p = this.lfPath.split("/").filter(Boolean);
      p.pop();
      this.loadLocalFiles(p.length ? "/" + p.join("/") : "");
    },
    /* v3.44 本地压缩包勾选：只对压缩包生效，勾选状态在切换目录时自动清空 */
    lfToggleAllArchives(on) {
      /* v3.50：只增删压缩包，别把为「批量改名」勾的普通文件/文件夹清掉 */
      const arc = new Set(this.lfArchives.map(f => f.name));
      const keep = this.lfPicked.filter(n => !arc.has(n));
      this.lfPicked = on ? keep.concat([...arc]) : keep;
    },
    /* v3.50：全选 / 取消当前列表全部条目（文件夹 + 文件），供批量改名用 */
    lfToggleAll(on) {
      this.lfPicked = on ? [...this.lfRenameNames] : [];
    },
    lfUnzipPicked() {
      const list = this.lfPickedArchives;
      if (!list.length) { this.toast("先勾选要解压的压缩包", "warn"); return; }
      this.openLocalUnzip(list);
    },
    /* v3.43 本地文件导出：M3U 用控制台的 /media 绝对地址，VLC / PotPlayer 可直接播放 */
    exportLocalM3u() {
      const list = this.lfFiles.filter(f => f.kind === "video" || f.kind === "audio");
      if (!list.length) { this.toast("当前目录没有音视频文件，无法生成播放列表", "error"); return; }
      const base = location.origin;
      const lines = ["#EXTM3U"];
      list.forEach(f => {
        lines.push("#EXTINF:-1," + f.name);
        lines.push(base + this.lfUrl(f));
      });
      const dir = this.lfPath ? this.lfPath.split("/").pop() : "download";
      this.saveText(lines.join("\r\n") + "\r\n",
        "播放列表-" + dir.replace(/[\\/:*?"<>|]/g, "_") + ".m3u", "audio/x-mpegurl");
      this.toast("已导出 " + list.length + " 个音视频到 M3U（可直接用 VLC 打开）", "ok");
    },
    exportLocalCsv() {
      if (!this.lfDirs.length && !this.lfFiles.length) { this.toast("当前目录是空的", "error"); return; }
      const rows = [["类型", "名称", "大小(字节)", "大小", "修改时间"]];
      this.lfDirs.forEach(d => rows.push(["文件夹", d.name, "", "", this.fmtTime(d.mtime)]));
      this.lfFiles.forEach(f => rows.push(["文件", f.name, f.size, this.fmt(f.size), this.fmtTime(f.mtime)]));
      const dir = this.lfPath ? this.lfPath.split("/").pop() : "download";
      const t = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      this.saveText(this.csvText(rows),
        "本地清单-" + dir.replace(/[\\/:*?"<>|]/g, "_") + "-" + t + ".csv", "text/csv;charset=utf-8");
      this.toast("已导出 " + (rows.length - 1) + " 条到 CSV", "ok");
    },
    openPlayer(f) {
      if (!f.playable) {
        this.toast("浏览器不支持直接播放此格式，请点「下载」", "error");
        return;
      }
      this.player = { name: f.name, kind: f.kind, url: this.lfUrl(f),
                      dl: this.lfUrl(f, true), size: f.size };
    },
    closePlayer() {
      const v = this.$refs.playerVideo;
      const a = this.$refs.playerAudio;
      /* v3.45：关窗时把进度落下来。timeupdate 5 秒才写一次，最后一小段会丢 */
      const p = this.player;
      if (p && p.cloud) {
        const el = v || a;
        if (el && el.currentTime) this.playProgressSet(p.remote, p.path, el.currentTime, el.duration);
      }
      if (v) { try { v.pause(); v.removeAttribute("src"); v.load(); } catch (e) {} }
      if (a) { try { a.pause(); } catch (e) {} }
      this.player = null;
      this.playResumeHint = "";
      this._playResumeAt = 0;
    },

    /* ══════════ v3.45 云端在线播放 ══════════
       链路：后端 GET /drive/v1/files/{id} 拿 web_content_link（自带 expire 签名、
       免 token、支持 Range），浏览器 <video> 直接拉 CDN。实测 1163 KB/s；
       经 NAS 中转只有 45 KB/s（PikPak）/ 79 KB/s（webdav），所以坚决不中转。 */
    playBtnTitle() {
      if (this.curAcctType && this.curAcctType !== "pikpak")
        return "该网盘没有免鉴权的播放直链（只能经 NAS 中转，实测不足 100 KB/s），点击可看具体原因";
      return "在线播放（直连 CDN，可拖动进度）";
    },
    fmtBitrate(bps) {
      const n = Number(bps) || 0;
      if (!n) return "";
      return n >= 1e6 ? (n / 1e6).toFixed(1) + " Mbps" : Math.round(n / 1e3) + " kbps";
    },
    fmtClock(s) {
      const t = Math.max(0, Math.floor(Number(s) || 0));
      const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
      const p2 = n => (n < 10 ? "0" : "") + n;
      return h ? h + ":" + p2(m) + ":" + p2(sec) : m + ":" + p2(sec);
    },

    /* ---- 进度记忆：localStorage，键 = pkplay:<remote>:<path> ---- */
    playKey(remote, path) { return "pkplay:" + remote + ":" + path; },
    playProgressGet(remote, path) {
      try {
        const raw = localStorage.getItem(this.playKey(remote, path));
        if (!raw) return null;
        const o = JSON.parse(raw);
        return o && o.t > 0 ? o : null;
      } catch (e) { return null; }        /* 隐私模式禁存储，静默降级 */
    },
    playProgressSet(remote, path, t, dur) {
      if (!remote || !path) return;
      try {
        const k = this.playKey(remote, path);
        /* 太靠前（<5s）或快看完（差 12s 内）都不记，否则下次点开像没播过/直接跳结尾 */
        if (!isFinite(t) || !isFinite(dur) || dur <= 0 || t < 5 || t > dur - 12) {
          localStorage.removeItem(k);
          return;
        }
        localStorage.setItem(k, JSON.stringify({ t: Math.floor(t), d: Math.floor(dur), at: Date.now() }));
      } catch (e) { /* 配额满 / 禁用存储：忽略，播放本身不受影响 */ }
    },
    playProgressClear(remote, path) {
      try { localStorage.removeItem(this.playKey(remote, path)); } catch (e) {}
    },

    async cloudPlay(f) {
      if (!f || !f.playable) {
        this.toast("这个格式浏览器不能直接播，请下载后再看", "error");
        return;
      }
      const path = this.curPath ? this.curPath + "/" + f.name : f.name;
      this.cloudPlayBusy = f.name;
      try {
        const d = await this.api("playinfo", { remote: this.curRemote, path: path });
        if (!d || d.error) { this.toast((d && d.error) || "取不到播放地址", "error"); return; }
        if (!d.url) {
          this.toast(d.error || "这个账号不支持在线播放", "error");
          return;
        }
        /* HEVC 命中时仍然打开：Edge 装了 HEVC 扩展是能播的，只是先把话说清 */
        if (!d.codecOk) this.toast(d.hint || "该视频编码浏览器多半解不了", "error");
        this.player = {
          name: f.name, kind: d.kind || "video", url: d.url,
          dl: "", size: f.size, cloud: true, direct: true,
          remote: this.curRemote, path: path, meta: d,
        };
        this.playResumeHint = "";
        this._playRetried = false;
      } catch (e) {
        this.toast("取播放地址失败：" + (e && e.message ? e.message : e), "error");
      } finally {
        this.cloudPlayBusy = "";
      }
    },
    downloadFromPlayer() {
      const p = this.player;
      if (!p || !p.cloud) return;
      this.startFileJob(p.name);
    },

    /* ---- 播放器事件 ---- */
    onPlayerMeta() {
      const el = this.$refs.playerVideo || this.$refs.playerAudio;
      const p = this.player;
      if (!el || !p || !p.cloud) return;
      const dur = el.duration;
      let target = this._playResumeAt || 0;
      this._playResumeAt = 0;
      if (!target) {
        const rec = this.playProgressGet(p.remote, p.path);
        if (rec) target = rec.t;
      }
      if (!target || !isFinite(dur) || dur <= 0) return;
      if (target < 5 || target > dur - 12) return;
      try { el.currentTime = target; } catch (e) { return; }
      this.playResumeHint = this.fmtClock(target);
      clearTimeout(this._resumeTimer);
      this._resumeTimer = setTimeout(() => { this.playResumeHint = ""; }, 5000);
    },
    onPlayerTime() {
      const el = this.$refs.playerVideo || this.$refs.playerAudio;
      const p = this.player;
      if (!el || !p || !p.cloud) return;
      const now = Date.now();
      if (now - (this._playTick || 0) < 5000) return;   /* timeupdate 一秒能来 4 次 */
      this._playTick = now;
      this.playProgressSet(p.remote, p.path, el.currentTime, el.duration);
    },
    onPlayerEnd() {
      const p = this.player;
      if (p && p.cloud) this.playProgressClear(p.remote, p.path);
    },
    async onPlayerError() {
      const p = this.player;
      const el = this.$refs.playerVideo || this.$refs.playerAudio;
      if (!p || !p.cloud) return;
      /* 直链 24 小时过期：重新签发一次续播。只重试一次，避免坏文件死循环 */
      if (this._playRetried) {
        this.toast("播放失败：编码不受支持或链接失效，请下载后播放", "error");
        return;
      }
      this._playRetried = true;
      const at = el ? el.currentTime : 0;
      try {
        const d = await this.api("playinfo", { remote: p.remote, path: p.path });
        if (!d || d.error || !d.url) {
          this.toast((d && d.error) || "重新获取播放地址失败", "error");
          return;
        }
        this._playResumeAt = at > 5 ? at : 0;
        p.url = d.url;
        p.meta = d;
        this.toast("播放链接已刷新");
      } catch (e) {
        this.toast("重新获取播放地址失败", "error");
      }
    },

    /* ---------- 一致性校验（v3.16）：云盘 ↔ 落盘内容 ---------- */
    destToLocal(dest) {
      /* rclone 侧任务目标 → 本地文件页路径。
         v3.57：按后端识别的卷映射换算成控制台绝对路径（支持任一存储空间）；
         识别信息还没回来时回退旧约定（剥 /data 前缀 = 相对主卷）。 */
      const s = String(dest || "").trim();
      const vols = this.state.storages || [];
      const hit = vols.filter(v => s === v.rclone || s.indexOf(v.rclone + "/") === 0)
        .sort((a, b) => b.rclone.length - a.rclone.length)[0];
      if (hit) {
        const tail = s.slice(hit.rclone.length).replace(/^\//, "").replace(/\/$/, "");
        return hit.console.replace(/\/$/, "") + (tail ? "/" + tail : "");
      }
      if (s.indexOf("/data/") === 0) return s.slice(6).replace(/\/$/, "");
      return s === "/data" ? "" : s;
    },
    async loadVerifyRemotes() {
      try {
        const d = await this.api("remotes");
        this.remotes = d.remotes || [];
        if (!this.verifyForm.remote && this.remotes.length) this.verifyForm.remote = this.remotes[0];
      } catch (e) { /* 轮询失败由 refresh() 统一提示，这里不弹横幅 */ }
    },
    /* 本地路径 → 反查它属于哪个下载任务，自动带出云端账号与路径。
       输入兼容两种写法：相对主卷（kz/0501.mp4）与绝对路径（/vol2/download/kz/...）。
       例：kz/0501.mp4 命中 dest=/data/kz 的任务 → pikpik-合集:Pack From Shared/kz/0501.mp4 */
    guessCloudForLocal(localPath) {
      const raw = String(localPath || "");
      const lp = raw.replace(/^\/+|\/+$/g, "");
      if (!lp) return null;
      for (const j of (this.jobs || [])) {
        const d = this.destToLocal(j.dest);
        if (!d) continue;
        /* 绝对路径（任务目标）与相对主卷两种口径都比对一次 */
        const relOf = raw.startsWith("/") && raw.indexOf(d + "/") === 0
          ? raw.slice(d.length + 1).replace(/\/+$/, "") : "";
        const cand = [lp, relOf].filter(Boolean);
        for (const c of cand) {
          const dd = d.replace(/^\/+|\/+$/g, "");
          if (c === dd) return { remote: j.remote, cloudPath: j.subpath || "" };
          if (c.indexOf(dd + "/") === 0) {
            const rest = c.slice(dd.length + 1);
            const base = String(j.subpath || "").replace(/\/$/, "");
            return { remote: j.remote, cloudPath: base ? base + "/" + rest : rest };
          }
        }
      }
      return null;
    },
    openVerify(opts) {
      opts = opts || {};
      if (this.player) this.closePlayer();
      if (this.detailOpen) this.closeDetail();
      this.verifyError = "";
      this.verifyResult = null;
      this.verifyFilter = "diff";
      this.verifyJobId = "";
      this.verifyForm.remote = opts.remote || this.curRemote || this.remotes[0] || "";
      this.verifyForm.cloudPath = opts.cloudPath !== undefined ? opts.cloudPath : (this.curPath || "");
      this.verifyForm.localPath = opts.localPath !== undefined ? opts.localPath : this.lfPath;
      if (opts.remote === undefined && opts.cloudPath === undefined) {
        const g = this.guessCloudForLocal(this.verifyForm.localPath);
        if (g) {
          this.verifyForm.remote = g.remote;
          this.verifyForm.cloudPath = g.cloudPath;
        }
      }
      this.verifyOpen = true;
      if (!this.remotes.length) this.loadVerifyRemotes();
      this.refresh();          /* jobs 供「从下载任务导入」 */
    },
    closeVerify() { this.verifyOpen = false; },
    openVerifyFromJob(job) {
      if (!job) return;
      this.openVerify({ remote: job.remote, cloudPath: job.subpath || "",
                        localPath: this.destToLocal(job.dest) });
    },
    applyVerifyJob() {
      const j = this.verifyJobs.find(x => String(x.id) === String(this.verifyJobId));
      if (!j) return;
      this.verifyForm.remote = j.remote;
      this.verifyForm.cloudPath = j.subpath || "";
      this.verifyForm.localPath = this.destToLocal(j.dest);
      this.verifyResult = null;
      this.verifyError = "";
      this.toast("已填入该任务的云端与目标路径");
    },
    useBrowsePath() {
      this.verifyForm.remote = this.curRemote || this.verifyForm.remote;
      this.verifyForm.cloudPath = this.curPath || "";
      this.toast("已填入云盘预览的当前目录");
    },
    async runVerify() {
      if (this.verifyBusy) return;
      if (!this.verifyForm.remote) { this.verifyError = "请选择云端账号"; return; }
      if (this.verifyForm.mode === "full" && !window.confirm(
          "完整校验会从云盘重新读取所选文件的全部内容用于比对（不写入磁盘），可能消耗较多流量和时间。继续吗？")) return;
      this.verifyBusy = true;
      this.verifyError = "";
      this.verifyResult = null;
      try {
        const d = await this.api("verify", {
          remote: this.verifyForm.remote,
          cloudPath: (this.verifyForm.cloudPath || "").trim(),
          localPath: (this.verifyForm.localPath || "").trim(),
          mode: this.verifyForm.mode,
          limit: Number(this.verifyForm.limit) || 6,
        });
        if (d.error) this.verifyError = d.error;
        else {
          this.verifyResult = d;
          this.verifyFilter = d.ok ? "all" : "diff";
          this.toast(d.ok ? "校验通过：云端与本地一致" : "校验完成：发现差异");
        }
      } catch (e) {
        this.verifyError = "请求失败：" + (e && e.message ? e.message : e);
      } finally {
        this.verifyBusy = false;
      }
    },
    exportVerifyCsv() {
      const r = this.verifyResult;
      if (!r) return;
      const rows = [["状态", "相对路径", "云端字节", "本地字节", "说明"]];
      (r.items || []).forEach(i => rows.push([
        this.verifyLabels[i.status] || i.status, i.path,
        i.cloudSize == null ? "" : i.cloudSize,
        i.localSize == null ? "" : i.localSize, i.note || "",
      ]));
      const csv = "\uFEFF" + rows.map(a => a.map(x =>
        '"' + String(x == null ? "" : x).replace(/"/g, '""') + '"').join(",")).join("\r\n");
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      const a = document.createElement("a");
      const tag = (r.localRoot || "download").split("/").filter(Boolean).pop() || "download";
      a.href = url;
      a.download = "一致性校验-" + tag + "-" +
        new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      this.toast("校验报告已导出");
    },

    /* ---------- 版本与在线更新（v3.54） ---------- */
    async loadUpdateState() {
      try {
        const d = await this.api("updatestate");
        if (d && d.state) this.updState = d.state;
        if (d && d.current) this.consoleVer = d.current;
      } catch (e) { /* 离线 / 旧后端时静默 */ }
    },
    async checkUpdate(manual) {
      if (this.updChecking || this.updApplying) return;
      this.updChecking = true; this.updError = "";
      try {
        const d = await this.api("updatecheck", { force: manual !== false }, 30000);
        if (!d || d.error || !d.ok) {
          this.updError = (d && d.error) || "检查失败";
          this.updInfo = null;
        } else {
          this.updInfo = d;
          this.updCheckedAt = d.checkedAt || "";
          if (manual !== false)
            this.toast(d.hasUpdate ? ("发现新版本 " + d.latest) : "已是最新版本",
                       d.hasUpdate ? "warning" : "ok");
        }
      } catch (e) {
        this.updError = "检查更新失败：" + (e.message || e);
      } finally { this.updChecking = false; }
    },
    async applyUpdate() {
      if (!this.updInfo || !this.updInfo.hasUpdate || this.updApplying) return;
      const ver = this.updInfo.latest;
      if (!window.confirm("即将下载并安装 " + ver + "。\n\n程序文件会先备份再替换，设置、任务、账号与已下载文件不受影响；安装完成后控制台会自动重启，页面可能短暂中断。是否继续？")) return;
      this.updApplying = true; this.updError = "";
      try {
        const d = await this.api("updateapply", {}, 240000);
        if (d.error) { this.updError = d.error; this.toast(d.error, "error"); }
        else {
          this.toast(d.message || ("已更新到 " + ver), "ok");
          await this.loadUpdateState();
          this.updInfo = null;   /* 重启前收起按钮，避免重复点击 */
          if (d.restart) setTimeout(() => this.loadUpdateState(), 8000);
        }
      } catch (e) {
        this.updError = "更新失败：" + (e.message || e);
        this.toast(this.updError, "error");
      } finally { this.updApplying = false; }
    },
    async rollbackUpdate() {
      if (this.updApplying) return;
      if (!window.confirm("确定回滚到上一次更新前的版本吗？\n设置、任务记录与账号凭证不在替换范围内，不会丢失。")) return;
      this.updApplying = true; this.updError = "";
      try {
        const d = await this.api("updaterollback", {}, 60000);
        if (d.error) { this.updError = d.error; this.toast(d.error, "error"); }
        else { this.toast(d.message || "已回滚", "ok"); await this.loadUpdateState(); }
      } catch (e) {
        this.updError = "回滚失败：" + (e.message || e);
        this.toast(this.updError, "error");
      } finally { this.updApplying = false; }
    },

    /* ---------- 导航 / 主题 ---------- */
    go(p) {
      if (this.player) this.closePlayer();
      this.page = p;
      this.themeOpen = false;
      this.pickerTarget = "";
      if (p === "settings") { this.fillForm(); this.loadUpdateState(); }
      if (p === "stats") this.loadStatsPage();
      if (p === "accounts") {
        this.loadProviders();
        this.loadAccounts();  /* 不再自动验证：由「全部验证」或每行「测试」手动触发 */
      }
      if (p === "browse") {
        /* 只在首次进入时取账号列表；之后保持用户已选的账号和目录路径，
           避免每次从「下载任务」切回来都跳回根目录 / 第一个账号。 */
        if (!this.remotes.length) this.loadRemotes().then(() => this.loadQuota(true));
        else { this.loadDirs(this.curPath); this.loadQuota(false); }
        this.loadLocalDirs();   /* 保存目录 picker 的数据源 */
      }
      if (p === "tasks") {
        this.loadLocalDirs();
      }
      if (p === "files") this.loadLocalFiles(this.lfPath);
    },
    toggleSide() {
      this.collapsed = !this.collapsed;
      localStorage.setItem("pk.side", this.collapsed ? "1" : "");
    },
    applyTheme(t) {
      this.theme = t;
      document.documentElement.dataset.theme = t;
      document.documentElement.style.colorScheme = LIGHT_THEMES.has(t) ? "light" : "dark";
      localStorage.setItem("pk.theme", t);
      this.$nextTick(() => this.rebuildCharts());
    },
    setAuto(v) {
      this.themeAuto = v;
      localStorage.setItem("pk.themeAuto", v ? "1" : "");
      if (v) this.applyTheme(this._mq && this._mq.matches ? "nord" : "sunset");
    },
    probeSwatches() {
      const out = {};
      document.querySelectorAll("[data-probe]").forEach(el => {
        out[el.dataset.probe] = [...el.querySelectorAll("span")].map(s => getComputedStyle(s).backgroundColor);
      });
      this.swCache = out;
    },
    sw(t, i) {
      if (!this.swCache[t]) this.probeSwatches();
      return (this.swCache[t] || [])[i] || "transparent";
    },
    onKey(e) {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "Escape") {
        if (this.verifyOpen) this.closeVerify();
        if (this.player) this.closePlayer();
        if (this.detailOpen) this.closeDetail();
        this.pickerTarget = "";
        this.themeOpen = false;
        this.acctMenuOpen = false;
        return;
      }
      if (e.key === "g") { this._gbuf = Date.now(); return; }
      if (this._gbuf && Date.now() - this._gbuf < 1000) {
        if (e.key === "o") this.go("overview");
        else if (e.key === "b") this.go("browse");
        else if (e.key === "t") this.go("tasks");
        else if (e.key === "f") this.go("files");
        else if (e.key === "s") this.go("settings");
        else if (e.key === "a") this.go("accounts");
        this._gbuf = 0;
      }
    },
  },

  watch: {
    themeOpen(v) { if (v) this.probeSwatches(); },
    /* v3.60 勾选变化时统计新勾入文件夹的大小（文件夹行的"—"与下载按钮总大小共用） */
    picked(names) { this.statDirSizes(names || []); },
  },

  mounted() {
    window.__vm = this; /* 调试句柄 */
    /* 昼夜自动切换（§5.3：昼 nord / 夜 sunset） */
    this._mq = window.matchMedia("(prefers-color-scheme: light)");
    this._mq.addEventListener("change", () => {
      if (this.themeAuto) this.applyTheme(this._mq.matches ? "nord" : "sunset");
    });
    this._onResize = () => {
      this.resizeCharts();
    };
    window.addEventListener("resize", this._onResize);
    window.addEventListener("keydown", e => this.onKey(e));
    // 书签导入结果窗口与控制台同源，用 storage 事件自动刷新账号列表
    this._onBmDone = e => {
      if (e.key === "__pk_bm_done__" && e.newValue) {
        this.loadAccounts();
        this.relinkTarget = null;
        this.importOpen = false;      /* v3.38：重登采集成功后自动收起弹窗 */
        this.toast("书签导入完成，账号列表已刷新", "ok");
      }
    };
    window.addEventListener("storage", this._onBmDone);
    /* 点击页面其它位置关闭账号下拉（下拉自身带 data-acct-drop 标记） */
    this._onDocClick = e => {
      if (!e.target || !e.target.closest || !e.target.closest("[data-acct-drop]"))
        this.acctMenuOpen = false;
    };
    document.addEventListener("click", this._onDocClick);
    this.$nextTick(() => this.probeSwatches());

    this.loadRemotes();
    this.loadProviders();
    this.refresh();
    this.refreshStats();
    this.loadBw();
    this.api("version").then(d => {
      if (d && d.version) this.rcloneVer = d.version;
      if (d && d.consoleVersion) this.consoleVer = d.consoleVersion;
    }).catch(() => {});
    this._timer = setInterval(() => {
      this.refresh();
      this.refreshStats();
      if (this.detailOpen) this.loadDetail(true);
    }, 2000);
    this._slow = setInterval(() => {
      if (this.page === "stats") { this.loadHistory(); this.loadHealth(); }
    }, 30000);
  },

  beforeUnmount() {
    clearInterval(this._timer);
    clearInterval(this._slow);
    this.stopPulse();
    if (this._spRaf) cancelAnimationFrame(this._spRaf);
    if (this._onResize) window.removeEventListener("resize", this._onResize);
    if (this._onBmDone) window.removeEventListener("storage", this._onBmDone);
    if (this._onDocClick) document.removeEventListener("click", this._onDocClick);
  },
}).mount("#app");
