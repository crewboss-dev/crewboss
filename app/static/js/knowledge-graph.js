// The Knowledge page's GRAPH view (task #550; partials/knowledge_graph.html). Read-only. The server has already worked
// out every link (app/knowledge_graph.py) and put the graph inline as JSON; this lays it out and draws it as SVG.
//   - Layout: a small force simulation (Fruchterman-Reingold: every pair repels, every edge pulls, a weak pull to the
//     middle keeps unlinked islands on screen), run to rest before the first paint. Seeded from a fixed spiral, so the
//     same graph always draws the same way. No library: a few hundred nodes settle in well under a second.
//   - Pan by dragging the background, zoom with the wheel, a two-finger pinch or the -/+/Fit buttons. Labels hide
//     when zoomed far out. A node is a real <a href>: press it (or Tab to it + Enter) to open the entry or task. A
//     press that turned into a drag never navigates.
//   - Colour = the entry's kind (CSS classes kg-k-<kind>, theme tokens, so light/dark follow the page). Archived or
//     superseded entries are grey; tasks are small rings. The housekeeping lens (radio group kg-lens) dims every node
//     outside its set.
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var GRAVITY = 0.6;
  var MIN_K = 0.15, MAX_K = 4, LABEL_MIN_K = 0.55, TASK_LABEL_MIN_K = 1.4, DRAG_PX = 6;

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    for (var a in attrs) if (Object.prototype.hasOwnProperty.call(attrs, a)) node.setAttribute(a, attrs[a]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function truncate(text, n) {
    return text.length > n ? text.slice(0, n - 1).trimEnd() + "…" : text;
  }

  // --- layout -------------------------------------------------------------------------------------------------------

  function layout(nodes, edges) {
    var n = nodes.length, k = 56, i, j;
    var index = {};
    for (i = 0; i < n; i++) {
      index[nodes[i].id] = i;
      // A sunflower spiral: evenly spread, deterministic.
      var r = k * 0.6 * Math.sqrt(i + 1), a = i * 2.39996;
      nodes[i].x = r * Math.cos(a);
      nodes[i].y = r * Math.sin(a);
    }
    var links = [];
    edges.forEach(function (e) {
      if (e.source in index && e.target in index) links.push([index[e.source], index[e.target]]);
    });
    var iterations = n > 400 ? 120 : n > 150 ? 220 : 320;
    var temp = k * Math.sqrt(n) * 0.35;
    var dx = new Float64Array(n), dy = new Float64Array(n);
    for (var it = 0; it < iterations; it++) {
      dx.fill(0); dy.fill(0);
      for (i = 0; i < n; i++) {
        for (j = i + 1; j < n; j++) {
          var ux = nodes[i].x - nodes[j].x, uy = nodes[i].y - nodes[j].y;
          var d2 = ux * ux + uy * uy || 0.01;
          var f = (k * k) / d2;            // repulsion k²/d, applied along the unit vector (u/d)
          dx[i] += ux * f; dy[i] += uy * f;
          dx[j] -= ux * f; dy[j] -= uy * f;
        }
      }
      for (var l = 0; l < links.length; l++) {
        var s = links[l][0], t = links[l][1];
        var vx = nodes[s].x - nodes[t].x, vy = nodes[s].y - nodes[t].y;
        var d = Math.sqrt(vx * vx + vy * vy) || 0.01;
        var g = d / k;                     // attraction d²/k, along the unit vector
        dx[s] -= vx * g; dy[s] -= vy * g;
        dx[t] += vx * g; dy[t] += vy * g;
      }
      for (i = 0; i < n; i++) {
        // Gravity to the middle, a linear spring: strong enough that unlinked nodes rest on the rim of the cluster
        // (radius ~ k·sqrt(n / GRAVITY)) instead of being pushed off into the distance by everyone's repulsion.
        dx[i] -= nodes[i].x * GRAVITY; dy[i] -= nodes[i].y * GRAVITY;
        var m = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1;
        var step = Math.min(m, temp);
        nodes[i].x += (dx[i] / m) * step;
        nodes[i].y += (dy[i] / m) * step;
      }
      temp = Math.max(temp * 0.97, 0.5);
    }
  }

  // --- drawing ------------------------------------------------------------------------------------------------------

  function draw(root, data) {
    var svg = root.querySelector("[data-kg-svg]");
    if (!svg) return;
    var nodes = data.nodes.map(function (n) { return Object.assign({}, n); });
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; n.degree = 0; });
    var edges = data.edges.filter(function (e) { return byId[e.source] && byId[e.target]; });
    edges.forEach(function (e) { byId[e.source].degree++; byId[e.target].degree++; });
    layout(nodes, edges);

    var view = el("g", { "class": "kg-view" }, svg);
    var edgeLayer = el("g", { "class": "kg-edges" }, view);
    var nodeLayer = el("g", { "class": "kg-nodes" }, view);
    var neighbours = {};
    nodes.forEach(function (n) { neighbours[n.id] = {}; });

    edges.forEach(function (e) {
      var s = byId[e.source], t = byId[e.target];
      e.el = el("line", { x1: s.x, y1: s.y, x2: t.x, y2: t.y, "class": "kg-edge kg-edge-" + e.rel }, edgeLayer);
      neighbours[s.id][t.id] = true;
      neighbours[t.id][s.id] = true;
    });

    nodes.forEach(function (n) {
      var isTask = n.type === "task";
      var cls = "kg-node " + (isTask ? "kg-task" + (n.done ? " kg-task-done" : "") : "kg-entry kg-k-" + n.kind);
      if (n.archived || n.superseded) cls += " kg-old";
      if (n.inherited) cls += " kg-inherited";
      n.el = el("a", { href: n.url, "class": cls, "data-node": n.id }, nodeLayer);
      var label = isTask
        ? "Task #" + n.ref + ": " + n.title + " (" + n.status + ")"
        : n.title + " (" + n.kind + (n.archived ? ", archived" : n.superseded ? ", superseded" : "") + ")";
      n.el.setAttribute("aria-label", label);
      el("title", {}, n.el).textContent = label;
      n.r = isTask ? 4 : 6 + Math.min(6, Math.sqrt(n.degree) * 2);
      n.circle = el("circle", { cx: n.x, cy: n.y, r: n.r }, n.el);
      var text = el("text", { x: n.x + n.r + 4, y: n.y + 4, "class": isTask ? "kg-label kg-tlabel" : "kg-label" }, n.el);
      n.text = text;
      text.textContent = isTask ? "#" + n.ref : truncate(n.title, 28);
      // Hover (a mouse only): light up the node's own links and neighbours.
      n.el.addEventListener("pointerenter", function (ev) {
        if (ev.pointerType !== "mouse") return;
        svg.classList.add("kg-hovering");
        n.el.classList.add("kg-hot");
        Object.keys(neighbours[n.id]).forEach(function (id) { byId[id].el.classList.add("kg-hot"); });
        edges.forEach(function (e) { if (e.source === n.id || e.target === n.id) e.el.classList.add("kg-hot"); });
      });
      n.el.addEventListener("pointerleave", function () {
        svg.classList.remove("kg-hovering");
        svg.querySelectorAll(".kg-hot").forEach(function (x) { x.classList.remove("kg-hot"); });
      });
    });

    // --- pan + zoom -------------------------------------------------------------------------------------------------
    var tf = { x: 0, y: 0, k: 1 };
    // Zoomed out, nodes grow in graph units so they stay big enough to see and to press (about 60% of the shrink is
    // given back); zoomed in, they keep their size.
    var drawnScale = 1;
    function resize() {
      var grow = tf.k < 1 ? Math.pow(tf.k, -0.6) : 1;
      if (Math.abs(grow - drawnScale) < 0.02) return;
      drawnScale = grow;
      nodes.forEach(function (n) {
        n.circle.setAttribute("r", n.r * grow);
        n.text.setAttribute("x", n.x + n.r * grow + 4);
      });
    }
    function apply() {
      resize();
      view.setAttribute("transform", "translate(" + tf.x + "," + tf.y + ") scale(" + tf.k + ")");
      svg.classList.toggle("kg-far", tf.k < LABEL_MIN_K);
      svg.classList.toggle("kg-near", tf.k >= TASK_LABEL_MIN_K);
    }
    function zoomAt(px, py, factor) {
      var k = Math.max(MIN_K, Math.min(MAX_K, tf.k * factor));
      tf.x = px - (px - tf.x) * (k / tf.k);
      tf.y = py - (py - tf.y) * (k / tf.k);
      tf.k = k;
      apply();
    }
    function fit() {
      var w = svg.clientWidth || 600, h = svg.clientHeight || 400;
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(function (n) {
        minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
        maxX = Math.max(maxX, n.x + 120); maxY = Math.max(maxY, n.y);   // room for the label to the right
      });
      if (!isFinite(minX)) return;
      var pad = 24;
      var k = Math.min((w - 2 * pad) / Math.max(maxX - minX, 1), (h - 2 * pad) / Math.max(maxY - minY, 1), 1.5);
      tf.k = Math.max(MIN_K, Math.min(MAX_K, k));
      tf.x = (w - (maxX - minX) * tf.k) / 2 - minX * tf.k;
      tf.y = (h - (maxY - minY) * tf.k) / 2 - minY * tf.k;
      apply();
    }
    fit();
    // Refit when the stage gets wider or narrower (rotation, a resized window), never on a height-only resize: a
    // phone fires those as its address bar slides while the page scrolls, and that would undo the owner's zoom.
    var lastWidth = svg.clientWidth;
    window.addEventListener("resize", function () {
      if (svg.clientWidth !== lastWidth) { lastWidth = svg.clientWidth; fit(); }
    });

    function local(ev) {
      var rect = svg.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }

    svg.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var p = local(ev);
      zoomAt(p.x, p.y, Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.05 : 0.0015)));
    }, { passive: false });

    // Pointers are tracked on window (no pointer capture: capture would retarget the click away from the <a>).
    var pointers = {}, count = 0, moved = 0, last = null, pinch = null;
    function centroid() {
      var ids = Object.keys(pointers), x = 0, y = 0;
      ids.forEach(function (id) { x += pointers[id].x; y += pointers[id].y; });
      return { x: x / ids.length, y: y / ids.length };
    }
    function spread() {
      var ids = Object.keys(pointers);
      if (ids.length < 2) return 0;
      var a = pointers[ids[0]], b = pointers[ids[1]];
      return Math.hypot(a.x - b.x, a.y - b.y);
    }
    svg.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0 && ev.pointerType === "mouse") return;
      if (!count) moved = 0;
      pointers[ev.pointerId] = local(ev);
      count = Object.keys(pointers).length;
      last = centroid();
      pinch = count >= 2 ? spread() : null;
    });
    window.addEventListener("pointermove", function (ev) {
      if (!(ev.pointerId in pointers)) return;
      pointers[ev.pointerId] = local(ev);
      var c = centroid();
      moved += Math.hypot(c.x - last.x, c.y - last.y);
      if (moved > DRAG_PX) svg.classList.add("kg-panning");
      tf.x += c.x - last.x;
      tf.y += c.y - last.y;
      if (pinch) {
        var s = spread();
        if (s > 0) { zoomAt(c.x, c.y, s / pinch); pinch = s; moved += DRAG_PX; }
      }
      last = c;
      apply();
    });
    function release(ev) {
      if (!(ev.pointerId in pointers)) return;
      delete pointers[ev.pointerId];
      count = Object.keys(pointers).length;
      if (count) { last = centroid(); pinch = count >= 2 ? spread() : null; }
      else svg.classList.remove("kg-panning");
    }
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    // A press that became a pan or a pinch is not a click on the node under it.
    svg.addEventListener("click", function (ev) {
      if (moved > DRAG_PX) { ev.preventDefault(); ev.stopPropagation(); }
    }, true);
    // Browsers start a native link drag on <a>: that would swallow the pan.
    svg.addEventListener("dragstart", function (ev) { ev.preventDefault(); });

    root.querySelectorAll("[data-kg-zoom]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var how = btn.getAttribute("data-kg-zoom");
        if (how === "fit") return fit();
        zoomAt((svg.clientWidth || 600) / 2, (svg.clientHeight || 400) / 2, how === "in" ? 1.3 : 1 / 1.3);
      });
    });

    // --- housekeeping lens ------------------------------------------------------------------------------------------
    var sets = data.housekeeping || {};
    function lens(name) {
      var keep = name && name !== "all" ? sets[name] || [] : null;
      var on = {};
      if (keep) keep.forEach(function (id) { on[id] = true; });
      svg.classList.toggle("kg-lensed", !!keep);
      nodes.forEach(function (n) { n.el.classList.toggle("kg-lit", !!(keep && on[n.id])); });
    }
    root.querySelectorAll("input[name=kg-lens]").forEach(function (input) {
      input.addEventListener("change", function () { if (input.checked) lens(input.value); });
    });
    var checked = root.querySelector("input[name=kg-lens]:checked");
    lens(checked ? checked.value : "all");
  }

  function init() {
    document.querySelectorAll("[data-knowledge-graph]").forEach(function (root) {
      var src = root.querySelector("[data-kg-data]");
      if (!src) return;
      var data;
      try { data = JSON.parse(src.textContent); } catch (e) { return; }
      draw(root, data);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
