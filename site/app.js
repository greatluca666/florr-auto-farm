// 两件事: ①「它能做什么」的功能探索器(悬停/点按切换演示, 无人操作时每 8 秒自动轮播一次,
// 一旦用户碰过就不再自动轮播); ② 读 /latest.json(由服务器上的 deploy/mirror/mirror_sync.py
// 生成)填下载按钮、版本信息和更新记录. 读不到时按钮保持 HTML 里写死的 GitHub Releases 链接.
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  // ---- 功能探索器 ----
  var buttons = Array.prototype.slice.call(document.querySelectorAll("#feat-list .feat-item"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("#feature-stage .stage-body"));
  var order = buttons.map(function (b) { return b.dataset.demo; });
  var touched = false;
  var cursor = 0;

  function show(demo) {
    buttons.forEach(function (b) {
      var active = b.dataset.demo === demo;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    panels.forEach(function (p) {
      p.classList.toggle("is-on", p.dataset.demo === demo);
    });
    cursor = order.indexOf(demo);
  }

  buttons.forEach(function (b) {
    var demo = b.dataset.demo;
    function pick() { touched = true; show(demo); }
    // 跟原设计一致: 悬停、聚焦、点击都算"碰过", 停止自动轮播 —— 浏览的人主动看哪项就停在哪项.
    b.addEventListener("mouseenter", pick);
    b.addEventListener("focus", pick);
    b.addEventListener("click", pick);
  });

  if (order.length) {
    setInterval(function () {
      if (touched) return;
      cursor = (cursor + 1) % order.length;
      show(order[cursor]);
    }, 8000);
  }

  // ---- 下载 / 更新记录 ----
  var MB = 1024 * 1024;
  var LONG_NOTES = 240; // 更新说明超过这个字符数就折叠, 点「展开」才看全文

  function formatSize(bytes) {
    return (bytes / MB).toFixed(1) + " MB";
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var mm = String(d.getMonth() + 1).padStart(2, "0");
    var dd = String(d.getDate()).padStart(2, "0");
    return d.getFullYear() + "-" + mm + "-" + dd;
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Release 正文里用到的行内写法(粗体/行内代码/链接) -> 安全的行内 HTML. 输入先转义,
  // 再在转义后的文本上替换成固定的标签, 不会有原始 HTML 混进来.
  function inlineMd(text) {
    var html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, label, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener">' + label + "</a>";
    });
    return html;
  }

  // GitHub Release 正文是 markdown(标题/列表/粗体/行内代码/链接/段落这个子集) -> 真正的
  // 富文本节点, 不再当纯文本塞进 textContent —— 否则 "## 本版更新" 这种标题符号会原样显示.
  function renderMarkdown(md) {
    var frag = document.createDocumentFragment();
    var lines = (md || "").replace(/\r\n/g, "\n").split("\n");
    var i = 0;
    var para = [];

    function flushPara() {
      if (!para.length) return;
      var p = document.createElement("p");
      p.innerHTML = inlineMd(para.join(" "));
      frag.appendChild(p);
      para = [];
    }

    while (i < lines.length) {
      var line = lines[i];
      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      var bullet = /^[-*]\s+(.*)$/.exec(line);
      if (heading) {
        flushPara();
        var h = document.createElement("h" + Math.min(heading[1].length + 2, 6));
        h.innerHTML = inlineMd(heading[2]);
        frag.appendChild(h);
        i++;
      } else if (bullet) {
        flushPara();
        var ul = document.createElement("ul");
        while (i < lines.length && (bullet = /^[-*]\s+(.*)$/.exec(lines[i]))) {
          var li = document.createElement("li");
          li.innerHTML = inlineMd(bullet[1]);
          ul.appendChild(li);
          i++;
        }
        frag.appendChild(ul);
      } else if (!line.trim()) {
        flushPara();
        i++;
      } else {
        para.push(line.trim());
        i++;
      }
    }
    flushPara();
    if (!frag.childNodes.length) {
      var empty = document.createElement("p");
      empty.textContent = "(这个版本没有写更新说明)";
      frag.appendChild(empty);
    }
    return frag;
  }

  // 官网下载计数(服务器上的统计服务): 只发包名; 发不出去也不影响下载本身
  function countDownload(url) {
    try {
      var file = String(url).split("/").pop();
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/t", JSON.stringify({ v: 1, type: "dl", file: file }));
      }
    } catch (e) { /* 统计失败不管 */ }
  }

  function renderDownload(m) {
    var w = m.win64;
    var label = "v" + m.version;
    [$("dl-btn"), $("dl-btn-2")].forEach(function (btn) {
      if (!btn) return;
      btn.href = w.url;
      btn.removeAttribute("target");
      btn.removeAttribute("rel");
      btn.addEventListener("click", function () { countDownload(w.url); });
    });
    if ($("dl-btn")) $("dl-btn").textContent = "下载 Windows 版 " + label;
    if ($("dl-btn-2")) $("dl-btn-2").textContent = "↓ 下载最新版本 " + label;
    var parts = [label, formatSize(w.size)];
    var date = formatDate(m.published_at);
    if (date) parts.push(date + " 发布");
    if ($("dl-meta")) $("dl-meta").textContent = parts.join(" · ");
    if ($("footer-sha")) $("footer-sha").textContent = label + " SHA-256: " + w.sha256;
  }

  function renderHistory(m) {
    var list = $("changelog-list");
    if (!list) return;
    list.textContent = "";
    (m.history || []).forEach(function (h) {
      var li = document.createElement("li");
      li.className = "changelog-row";

      var head = document.createElement("div");
      head.className = "changelog-version mono";
      var when = formatDate(h.published_at);
      head.textContent = "v" + h.version + (when ? " · " + when : "");
      li.appendChild(head);

      var notes = document.createElement("div");
      notes.className = "changelog-notes";
      notes.appendChild(renderMarkdown(h.notes));

      // 短的更新说明直接展开显示; 长的默认折叠, 点「展开」才看全文 —— 免得列表被一条
      // 长说明撑爆, 又不丢内容.
      if ((h.notes || "").length > LONG_NOTES) {
        var details = document.createElement("details");
        details.className = "changelog-details";
        var summary = document.createElement("summary");
        summary.className = "changelog-summary";
        var closed = document.createElement("span");
        closed.className = "cs-closed";
        closed.textContent = "展开更新说明 ▾";
        var open = document.createElement("span");
        open.className = "cs-open";
        open.textContent = "收起 ▴";
        summary.appendChild(closed);
        summary.appendChild(open);
        details.appendChild(summary);
        details.appendChild(notes);
        li.appendChild(details);
      } else {
        li.appendChild(notes);
      }

      // 每个版本自己的一键下载, 跟 GitHub Release 页面每条 Release 自带下载链接一样;
      // 服务器还没把这个版本的下载信息写进 latest.json 时(镜像脚本刚升级、还没跑过一轮
      // 同步)就不显示, 不给一个点了没反应的按钮.
      if (h.win64 && h.win64.url) {
        var dl = document.createElement("a");
        dl.className = "changelog-dl";
        dl.href = h.win64.url;
        dl.textContent = "↓ 下载 v" + h.version + " · " + formatSize(h.win64.size);
        dl.addEventListener("click", countDownload.bind(null, h.win64.url));
        li.appendChild(dl);
      }

      list.appendChild(li);
    });
    if (!list.children.length && $("changelog-empty")) $("changelog-empty").hidden = false;
  }

  function fail() {
    if ($("dl-meta")) $("dl-meta").textContent = "暂时读不到最新版本信息, 请用下面的 GitHub 链接下载";
    if ($("changelog-empty")) $("changelog-empty").hidden = false;
  }

  fetch("/latest.json", { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (m) {
      if (!m || m.schema !== 1 || !m.win64) throw new Error("latest.json 格式不对");
      renderDownload(m);
      renderHistory(m);
    })
    .catch(fail);
})();
