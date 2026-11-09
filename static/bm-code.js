/* PikPak 一键导入书签（bookmarklet）源码。
 * 这个文件本身不执行任何采集；控制台把 PK_BOOKMARKLET.toString() 编码成
 * javascript: 链接供用户拖到书签栏。只有用户在 PikPak 页面点书签时才会运行，
 * 此时代码的上下文是 mypikpak.com —— 所以能读到该站点的登录态。
 *
 * 流程：读当前页 localStorage / sessionStorage / cookie / 常见全局状态，
 *      （无果时再异步扫 IndexedDB）→ 提取 refresh_token + sub/email →
 *      打开控制台同源的 /static/bm.html#<payload>，由那个窗口调 /api 完成写入。
 * 凭据只放在 URL fragment（不发送到服务器、不进 Referer），结果页读到后立刻清除。
 */
window.PK_BOOKMARKLET = function (consoleOrigin) {
  var TOKEN_PATTERNS = [
    /\\?"refresh_token\\?"\s*:\s*\\?"([A-Za-z0-9._\-]{20,600})\\?"/g,
    /\\?"(?:refreshToken|refresh|rt)\\?"\s*:\s*\\?"([A-Za-z0-9._\-]{20,600})\\?"/g,
    /(os\.[A-Za-z0-9._\-]{30,600})/g
  ];
  var KEYS_HINT = /(token|auth|user|account|session|login|credential|pp_|pikpak)/i;

  function esc(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  function css(el, o) { for (var k in o) el.style[k] = o[k]; return el; }

  function readStores() {
    var chunks = [], keys = [];
    function dump(store, tag) {
      try {
        for (var i = 0; i < store.length; i++) {
          var k = store.key(i), v = store.getItem(k);
          keys.push(tag + ":" + k);
          chunks.push(tag + "[" + k + "]=" + (v == null ? "" : v));
        }
      } catch (e) {}
    }
    dump(window.localStorage, "localStorage");
    dump(window.sessionStorage, "sessionStorage");
    try { if (document.cookie) { chunks.push("cookie=" + document.cookie); keys.push("cookie"); } } catch (e) {}
    // 少数 SPA 把登录态挂在全局对象上，而不是 storage
    var globals = ["__INITIAL_STATE__", "__NUXT__", "__APP_STATE__", "__USER__", "userInfo"];
    for (var g = 0; g < globals.length; g++) {
      try {
        var v = window[globals[g]];
        if (v) { chunks.push("global[" + globals[g] + "]=" + JSON.stringify(v)); keys.push("global:" + globals[g]); }
      } catch (e) {}
    }
    return { text: chunks.join("\n"), keys: keys };
  }

  function findTokens(txt) {
    var seen = {}, out = [];
    for (var i = 0; i < TOKEN_PATTERNS.length; i++) {
      var re = TOKEN_PATTERNS[i]; re.lastIndex = 0;
      var m;
      while ((m = re.exec(txt)) !== null) {
        var v = m[1];
        if (v && v.length >= 20 && !seen[v]) { seen[v] = 1; out.push(v); }
      }
    }
    return out;
  }

  function identity(txt, rt) {
    var sub = "", mail = "";
    var m = new RegExp('"refresh_token"\\s*:\\s*"' + esc(rt) + '"[^}]{0,400}?"sub"\\s*:\\s*"([A-Za-z0-9_\\-]{4,40})"').exec(txt);
    if (m) sub = m[1];
    if (!sub) {
      var subs = {}, re2 = /\\?"sub\\?"\s*:\s*\\?"([A-Za-z0-9_\-]{6,40})\\?"/g, x;
      while ((x = re2.exec(txt)) !== null) subs[x[1]] = 1;
      var sk = Object.keys(subs); if (sk.length === 1) sub = sk[0];
    }
    var mails = {}, re3 = /\\?"(?:email|mail|account)\\?"\s*:\s*\\?"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\\?"/g, y;
    while ((y = re3.exec(txt)) !== null) mails[y[1]] = 1;
    var mk = Object.keys(mails); if (mk.length === 1) mail = mk[0];
    return { sub: sub, email: mail };
  }

  function build(text, list) {
    return list.map(function (rt) {
      var id = identity(text, rt);
      return { env: "", sub: id.sub, email: id.email, refresh_token: rt };
    });
  }

  function openResult(items) {
    var payload = encodeURIComponent(JSON.stringify({ v: 1, items: items }));
    var url = String(consoleOrigin).replace(/\/+$/, "") + "/static/bm.html#" + payload;
    var w = window.open(url, "_blank");
    if (!w) { alert("导入窗口被浏览器拦截了。请允许本站点弹出窗口后，再点一次书签。"); return false; }
    try { w.focus(); } catch (e) {}
    panel("✓ 已打开导入窗口", [
      "找到 " + items.length + " 条凭证，请在弹出的窗口查看导入结果。",
      "若窗口没出现，可能被放到后台或被拦截，可点下面按钮重开。",
      "控制台：" + consoleOrigin
    ], "重新打开窗口", function () { openResult(items); });
    return true;
  }

  function panel(title, lines, actionLabel, action) {
    var old = document.getElementById("__pk_bm_panel__");
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var box = css(document.createElement("div"), {
      position: "fixed", right: "18px", top: "18px", zIndex: "2147483647",
      width: "340px", maxHeight: "70vh", overflow: "auto", padding: "16px 18px",
      borderRadius: "12px", background: "#111827", color: "#f3f4f6",
      font: "13px/1.6 -apple-system,Segoe UI,Microsoft YaHei,sans-serif",
      boxShadow: "0 12px 40px rgba(0,0,0,.35)", whiteSpace: "pre-wrap", wordBreak: "break-all"
    });
    box.id = "__pk_bm_panel__";
    var h = css(document.createElement("div"), { fontSize: "15px", fontWeight: "700", marginBottom: "8px" });
    h.textContent = title; box.appendChild(h);
    var p = css(document.createElement("div"), { color: "#cbd5e1", fontSize: "12px" });
    p.textContent = (lines || []).join("\n"); box.appendChild(p);
    var bar = css(document.createElement("div"), { marginTop: "12px", display: "flex", gap: "8px" });
    if (actionLabel) {
      var btn = css(document.createElement("button"), {
        flex: "1", padding: "8px", border: "0", borderRadius: "8px",
        background: "#4f46e5", color: "#fff", cursor: "pointer", fontWeight: "600"
      });
      btn.textContent = actionLabel;
      btn.onclick = function () { action(); if (box.parentNode) box.parentNode.removeChild(box); };
      bar.appendChild(btn);
    }
    var close = css(document.createElement("button"), {
      padding: "8px 12px", border: "1px solid #4b5563", borderRadius: "8px",
      background: "transparent", color: "#cbd5e1", cursor: "pointer"
    });
    close.textContent = "关闭";
    close.onclick = function () { if (box.parentNode) box.parentNode.removeChild(box); };
    bar.appendChild(close); box.appendChild(bar);
    (document.body || document.documentElement).appendChild(box);
  }

  function scanIndexedDB(cb) {
    var out = [], done = false;
    function finish() { if (!done) { done = true; clearTimeout(timer); cb(out.join("\n")); } }
    var timer = setTimeout(finish, 2500);
    if (!window.indexedDB || !indexedDB.databases) return finish();
    try {
      indexedDB.databases().then(function (dbs) {
        var names = (dbs || []).map(function (d) { return d.name; }).filter(Boolean).slice(0, 8);
        if (!names.length) return finish();
        var left = names.length;
        names.forEach(function (name) {
          var req;
          try { req = indexedDB.open(name); } catch (e) { if (!--left) finish(); return; }
          req.onerror = function () { if (!--left) finish(); };
          req.onsuccess = function () {
            var db = req.result, stores = [];
            try { stores = Array.prototype.slice.call(db.objectStoreNames); } catch (e) {}
            if (!stores.length) { db.close(); if (!--left) finish(); return; }
            var sl = stores.length;
            stores.forEach(function (sn) {
              try {
                var g = db.transaction(sn, "readonly").objectStore(sn).getAll();
                g.onsuccess = function () {
                  try { out.push(JSON.stringify(g.result)); } catch (e) {}
                  if (!--sl) { db.close(); if (!--left) finish(); }
                };
                g.onerror = function () { if (!--sl) { db.close(); if (!--left) finish(); } };
              } catch (e) { if (!--sl) { db.close(); if (!--left) finish(); } }
            });
          };
        });
      })["catch"](finish);
    } catch (e) { finish(); }
  }

  var s = readStores();
  var list = findTokens(s.text);
  if (list.length) {
    openResult(build(s.text, list));
    return;
  }

  // 同步存储里没有：再异步扫一次 IndexedDB。异步后不能保证 window.open 还被放行，
  // 所以给一个明确的按钮让用户点（这是用户手势，弹窗一定允许）。
  panel("正在查找 PikPak 凭证…", ["已读 localStorage / sessionStorage / cookie，正在检查 IndexedDB。"], null, null);
  scanIndexedDB(function (extra) {
    var all = s.text + "\n" + extra;
    var l2 = findTokens(all);
    if (l2.length) {
      var items = build(all, l2);
      panel("找到 " + l2.length + " 条凭证", ["来源：IndexedDB。点下面按钮完成导入。"],
        "导入到控制台", function () { openResult(items); });
    } else {
      var hint = s.keys.filter(function (k) { return KEYS_HINT.test(k); }).slice(0, 20);
      panel("没有找到 refresh_token", [
        "请确认：① 已登录的是 https://mypikpak.com/drive/login（不是介绍页）；",
        "② 登录已经完成、页面显示网盘内容；③ 用的是同一个浏览器标签。",
        "",
        "本页可见的存储键：" + (hint.length ? hint.join("  ") : "（无相关键）"),
        "",
        "备用方案：控制台「导入 / 采集 Token」→「粘贴导入」，",
        "或 F12 → Application → Local Storage 复制含 refresh_token 的值。"
      ], null, null);
    }
  });
};
