(function () {
  if (window.__canvasHookInstalled) return;
  window.__canvasHookInstalled = true;
  window.__canvasLog = [];
  window.__canvasFrame = 0;
  // Hard safety cap: never let the in-page buffer grow unbounded. The capture drains every
  // ~0.3s so this should never trigger in normal use; if it does (draining can't keep up),
  // stop appending and flag it rather than letting a later single drain choke. Tunable live.
  window.__canvasLogCap = 50000;
  window.__canvasLogTruncated = false;
  // Backstop only -- the record-time filter (see record()) should keep normal play well under
  // the hard cap. This defends against an unexpected spike (e.g. a screen full of nameplates)
  // still overrunning the buffer between drains, by proactively dropping frames older than the
  // most recent 5 instead of waiting for the 50,000-record cap. __canvasLog is always appended
  // to in non-decreasing frame order, so the oldest records are always at the front -- pruning
  // is a cheap prefix scan, not a full-array filter.
  var FRAME_RETENTION = 5;
  function pruneOldFrames() {
    var log = window.__canvasLog;
    var cur = window.__canvasFrame;
    var i = 0;
    while (i < log.length && cur - log[i].frame > FRAME_RETENTION - 1) i++;
    if (i > 0) log.splice(0, i);
  }
  function pushRecord(rec) {
    pruneOldFrames();
    if (window.__canvasLog.length >= window.__canvasLogCap) {
      window.__canvasLogTruncated = true;
      return;
    }
    window.__canvasLog.push(rec);
  }

  // canvas_decode.py only ever reads: arc fills (r is not null), health-bar-colored strokes
  // (plus the single stroke immediately following one -- see record()'s "value bar" comment),
  // text within label_radius of a kept health-bar stroke's anchor (mob/player nameplates), and
  // -- as of 2026-08-20 -- UI-scale text regardless of bar-anchor proximity (menu button
  // labels: death panel, start menu; see isUIScale below recordText). Keep these in exact sync
  // with scripts/canvas_decode.py's HEALTHBAR_BG / HEALTHBAR_DAMAGE / HEALTHBAR_SECONDARY /
  // INVENTORY_UI_SCALE_TOL / LABEL_MAX_DX / LABEL_MAX_DY constants. Shared at
  // module scope (not inside patchProto, which runs once per prototype type) so both
  // prototypes note/read the same anchors.
  var HEALTHBAR_COLORS = { "#222222": true, "#DD3434": true, "#42E3F5": true };
  // 名牌位置门: 水平 ±LABEL_MAX_DX, 垂直只往下 LABEL_MAX_DY. 原来是半径 100 的圆 ——
  // florr 把名牌画在怪身体下方, 垂直距离随体型增大, 圆形门会先把大怪的稀有度词挡在
  // 外面(实测 ultra.json: 最大那只的稀有度词落在 100.7px, 名字在 91.9px, 于是"有名字
  // 没稀有度" -> tier 退成 Common -> 神话/究极的锁定和规避全不触发)。这一层是**记录期**
  // 过滤, 丢了就再也拿不回来, 所以这里必须比 Python 侧宽。
  // 跟 canvas_decode.py 的 LABEL_MAX_DX / LABEL_MAX_DY 保持同步(test_canvas_decode.py
  // 里 test_hook_label_gate_matches_python_side 钉着)。
  var LABEL_MAX_DX = 150.0;
  var LABEL_MAX_DY = 700.0;
  var LABEL_MIN_DY = -40.0;
  // scripts/canvas_decode.py's INVENTORY_UI_SCALE_TOL, duplicated here (JS can't import Python
  // constants) -- keep these two values in sync. UI-scale text (menu buttons: death panel's
  // 继续/关闭, start menu's 开始) isn't necessarily near any bar anchor -- confirmed live
  // 2026-08-20 that on some accounts/layouts it isn't, silently dropping the text before
  // canvas_decode.py ever sees it, no matter how the Python-side detector is written. Text at
  // UI CTM scale (~1.0) bypasses the bar-anchor gate entirely below.
  var UI_SCALE_TOL = 0.01;
  var barAnchorFrame = -1;
  var barAnchors = [];
  function noteBarAnchor(m) {
    if (window.__canvasFrame !== barAnchorFrame) {
      barAnchorFrame = window.__canvasFrame;
      barAnchors = [];
    }
    barAnchors.push([m[4], m[5]]);
  }
  function nearABarAnchor(m) {
    if (window.__canvasFrame !== barAnchorFrame) return false;
    for (var i = 0; i < barAnchors.length; i++) {
      var dx = m[4] - barAnchors[i][0], dy = m[5] - barAnchors[i][1];
      if (Math.abs(dx) <= LABEL_MAX_DX && dy >= LABEL_MIN_DY && dy <= LABEL_MAX_DY) return true;
    }
    return false;
  }

  var originalRAF = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (cb) {
    return originalRAF(function (t) {
      window.__canvasFrame++;
      return cb(t);
    });
  };

  // 2D affine matrix [a,b,c,d,e,f]: (x,y) -> (a*x + c*y + e, b*x + d*y + f)
  function mul(m1, m2) {
    return [
      m1[0] * m2[0] + m1[2] * m2[1],
      m1[1] * m2[0] + m1[3] * m2[1],
      m1[0] * m2[2] + m1[2] * m2[3],
      m1[1] * m2[2] + m1[3] * m2[3],
      m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
      m1[1] * m2[4] + m1[3] * m2[5] + m1[5],
    ];
  }
  function apply(m, x, y) {
    return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
  }

  function state(ctx) {
    if (!ctx.__hookState) {
      ctx.__hookState = { m: [1, 0, 0, 1, 0, 0], stack: [], path: [], expectValueStroke: false };
    }
    return ctx.__hookState;
  }

  function patchProto(Proto) {
    if (!Proto || Proto.__canvasHookPatched) return;
    Proto.__canvasHookPatched = true;

    var orig = {};
    ["save", "restore", "translate", "rotate", "scale", "transform", "setTransform",
     "resetTransform", "beginPath", "moveTo", "lineTo", "arc", "bezierCurveTo", "arcTo",
     "rect", "closePath", "fill", "stroke", "fillText", "strokeText"].forEach(function (name) {
      orig[name] = Proto[name];
    });

    Proto.save = function () { var s = state(this); s.stack.push(s.m.slice()); return orig.save.apply(this, arguments); };
    Proto.restore = function () { var s = state(this); if (s.stack.length) s.m = s.stack.pop(); return orig.restore.apply(this, arguments); };
    Proto.translate = function (x, y) { var s = state(this); s.m = mul(s.m, [1, 0, 0, 1, x, y]); return orig.translate.apply(this, arguments); };
    Proto.scale = function (x, y) { var s = state(this); s.m = mul(s.m, [x, 0, 0, y, 0, 0]); return orig.scale.apply(this, arguments); };
    Proto.rotate = function (a) { var s = state(this); var c = Math.cos(a), n = Math.sin(a); s.m = mul(s.m, [c, n, -n, c, 0, 0]); return orig.rotate.apply(this, arguments); };
    Proto.transform = function (a, b, c, d, e, f) { var s = state(this); s.m = mul(s.m, [a, b, c, d, e, f]); return orig.transform.apply(this, arguments); };
    Proto.setTransform = function (a, b, c, d, e, f) {
      var s = state(this);
      if (a && typeof a === "object") { s.m = [a.a, a.b, a.c, a.d, a.e, a.f]; }
      else { s.m = [a, b, c, d, e, f]; }
      return orig.setTransform.apply(this, arguments);
    };
    Proto.resetTransform = function () { state(this).m = [1, 0, 0, 1, 0, 0]; return orig.resetTransform.apply(this, arguments); };

    Proto.beginPath = function () { state(this).path = []; return orig.beginPath.apply(this, arguments); };
    Proto.moveTo = function (x, y) { state(this).path.push(["m", x, y]); return orig.moveTo.apply(this, arguments); };
    Proto.lineTo = function (x, y) { state(this).path.push(["l", x, y]); return orig.lineTo.apply(this, arguments); };
    Proto.arc = function (x, y, r) { state(this).path.push(["a", x, y, r]); return orig.arc.apply(this, arguments); };
    Proto.bezierCurveTo = function (a, b, c, d, x, y) { state(this).path.push(["l", x, y]); return orig.bezierCurveTo.apply(this, arguments); };
    Proto.arcTo = function (x1, y1, x2, y2) { state(this).path.push(["l", x2, y2]); return orig.arcTo.apply(this, arguments); };
    Proto.rect = function (x, y, w, h) { var s = state(this); s.path.push(["m", x, y], ["l", x + w, y], ["l", x + w, y + h], ["l", x, y + h]); return orig.rect.apply(this, arguments); };
    Proto.closePath = function () { return orig.closePath.apply(this, arguments); };

    function record(ctx, op) {
      var s = state(ctx);
      if (!s.path.length) return;
      var xs = [], ys = [], arcR = null, cx = null, cy = null;
      for (var i = 0; i < s.path.length; i++) {
        var p = s.path[i];
        var pt = apply(s.m, p[1], p[2]);
        xs.push(pt[0]); ys.push(pt[1]);
        if (p[0] === "a") {
          cx = pt[0]; cy = pt[1];
          var sxScale = Math.hypot(s.m[0], s.m[1]);
          var syScale = Math.hypot(s.m[2], s.m[3]);
          arcR = p[3] * (sxScale + syScale) / 2;
        }
      }
      if (op === "fill" && arcR === null) return;
      if (op === "stroke") {
        var strokeColor = s.style_strokeStyle != null ? s.style_strokeStyle : null;
        // canvas_decode.py's _bar_blocks reads the remaining-health-bar stroke by POSITION, not
        // colour: it's whatever stroke is drawn immediately after a "#DD3434" (HEALTHBAR_DAMAGE)
        // stroke, and its actual colour is a damage-flash gradient that can be anything. Keep it
        // even though it won't match HEALTHBAR_COLORS.
        var keepAsValueBar = s.expectValueStroke;
        s.expectValueStroke = (strokeColor === "#DD3434");
        if (!HEALTHBAR_COLORS[strokeColor] && !keepAsValueBar) return;
        noteBarAnchor(s.m);
      }
      var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
      var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
      pushRecord({
        frame: window.__canvasFrame,
        op: op,
        x: cx !== null ? cx : (minx + maxx) / 2,
        y: cy !== null ? cy : (miny + maxy) / 2,
        r: arcR,
        bbox: [minx, miny, maxx, maxy],
        n: s.path.length,
        fill: s.style_fillStyle != null ? s.style_fillStyle : null,
        stroke: s.style_strokeStyle != null ? s.style_strokeStyle : null,
        lw: s.style_lineWidth != null ? s.style_lineWidth : null,
        alpha: s.style_globalAlpha == null ? 1 : s.style_globalAlpha,
        m: s.m.slice(),
      });
    }
    Proto.fill = function () { record(this, "fill"); return orig.fill.apply(this, arguments); };
    Proto.stroke = function () { record(this, "stroke"); return orig.stroke.apply(this, arguments); };

    function isUIScale(m) {
      return Math.abs(Math.hypot(m[0], m[1]) - 1.0) < UI_SCALE_TOL;
    }
    function recordText(ctx, text, x, y) {
      var s = state(ctx);
      if (!nearABarAnchor(s.m) && !isUIScale(s.m)) return;
      var pt = apply(s.m, x, y);
      pushRecord({
        frame: window.__canvasFrame, op: "text", text: String(text),
        x: pt[0], y: pt[1],
        fill: s.style_fillStyle != null ? s.style_fillStyle : null,
        alpha: s.style_globalAlpha == null ? 1 : s.style_globalAlpha,
        m: s.m.slice(),
      });
    }
    Proto.fillText = function (text, x, y) { recordText(this, text, x, y); return orig.fillText.apply(this, arguments); };
    Proto.strokeText = function (text, x, y) { recordText(this, text, x, y); return orig.strokeText.apply(this, arguments); };

    // style properties are accessors, not methods — hook via defineProperty, chain to originals
    ["fillStyle", "strokeStyle", "lineWidth", "globalAlpha"].forEach(function (name) {
      var desc = Object.getOwnPropertyDescriptor(Proto, name);
      if (!desc || !desc.set || !desc.get) return;
      Object.defineProperty(Proto, name, {
        get: function () { return desc.get.call(this); },
        set: function (v) { state(this)["style_" + name] = v; return desc.set.call(this, v); },
        configurable: true,
      });
    });
  }

  patchProto(window.CanvasRenderingContext2D && window.CanvasRenderingContext2D.prototype);
  patchProto(window.OffscreenCanvasRenderingContext2D && window.OffscreenCanvasRenderingContext2D.prototype);
})();
