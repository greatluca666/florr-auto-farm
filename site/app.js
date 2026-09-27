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

  function renderDownload(m) {
    var w = m.win64;
    var label = "v" + m.version;
    [$("dl-btn"), $("dl-btn-2")].forEach(function (btn) {
      if (!btn) return;
      btn.href = w.url;
      btn.removeAttribute("target");
      btn.removeAttribute("rel");
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
      var body = document.createElement("div");
      body.className = "changelog-notes";
      body.textContent = h.notes || "(这个版本没有写更新说明)"; // Release 正文是 markdown 原文, 按纯文本显示
      li.appendChild(head);
      li.appendChild(body);
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
