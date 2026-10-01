// Workbench-style SVG interaction; scoped to its SVG for embedded reports.
(function () {
  var script = document.currentScript;
  var root = script && script.closest('svg.wb-plan');
  if (!root) {
    var candidates = document.querySelectorAll('svg.wb-plan');
    root = candidates[candidates.length - 1];
  }
  if (!root || root.dataset.wired) return;
  root.dataset.wired = 'true';
  var viewport = root.querySelector('.wb-viewport');
  var detail = root.querySelector('.wb-details');
  var nodes = Array.prototype.slice.call(root.querySelectorAll('.wb-node'));
  var width = Number(viewport.getAttribute('width'));
  var height = Number(viewport.getAttribute('height'));
  var graphWidth = Number(viewport.dataset.graphWidth);
  var graphHeight = Number(viewport.dataset.graphHeight);
  var box = {x: 0, y: 0, w: width, h: height};
  var drag = null;
  var moved = false;
  var selected = null;
  var wheelFrame = null;
  var wheelDelta = 0;
  var wheelPoint = null;
  var wheelTime = null;
  var zoomIn = root.querySelector('[data-action="in"]');
  var zoomOut = root.querySelector('[data-action="out"]');
  var zoomLevel = root.querySelector('[data-action="reset"]');
  var minimum = width / 5;
  var maximum = Math.max(width * 8, graphWidth * 2, graphHeight * width / height * 2);

  function stopWheel() {
    if (wheelFrame !== null) window.cancelAnimationFrame(wheelFrame);
    wheelFrame = null;
    wheelDelta = 0;
    wheelTime = null;
  }

  function paint() {
    viewport.setAttribute('viewBox', [box.x, box.y, box.w, box.h].join(' '));
    var percent = Math.round(width / box.w * 100) + '%';
    zoomLevel.textContent = percent;
    zoomLevel.setAttribute('aria-label', 'Zoom ' + percent + '. Reset to 100%');
    zoomLevel.setAttribute('title', 'Current zoom: ' + percent + '. Click to reset to 100%');
    zoomIn.disabled = box.w <= minimum + .00001;
    zoomOut.disabled = box.w >= maximum - .00001;
  }
  function fit() {
    stopWheel();
    var ratio = Math.max((graphWidth + 56) / width, (graphHeight + 56) / height);
    box.w = width * ratio;
    box.h = height * ratio;
    box.x = (graphWidth - box.w) / 2;
    box.y = (graphHeight - box.h) / 2;
    paint();
  }
  function animateWheel(time) {
    // A bounded logarithmic target avoids a backlog from macOS momentum events.
    // Limit elapsed time too, so returning to a background tab cannot jump.
    var elapsed = wheelTime === null ? 16 : Math.min(32, Math.max(1, time - wheelTime));
    wheelTime = time;
    var step = wheelDelta * (1 - Math.exp(-elapsed / 55));
    step = Math.max(-elapsed * .0006, Math.min(elapsed * .0006, step));
    wheelDelta -= step;
    var matrix = viewport.getScreenCTM();
    if (!matrix) { stopWheel(); return; }
    var point = viewport.createSVGPoint();
    point.x = wheelPoint.x;
    point.y = wheelPoint.y;
    point = point.matrixTransform(matrix.inverse());
    zoom(Math.exp(step), (point.x - box.x) / box.w, (point.y - box.y) / box.h);
    if (Math.abs(wheelDelta) > .00001) {
      wheelFrame = window.requestAnimationFrame(animateWheel);
    } else {
      stopWheel();
    }
  }
  function zoom(factor, px, py) {
    var next = Math.min(maximum, Math.max(minimum, box.w * factor));
    var ratio = next / box.w;
    box.x += box.w * px * (1 - ratio);
    box.y += box.h * py * (1 - ratio);
    box.w = next;
    box.h *= ratio;
    paint();
  }
  function choose(node, notify) {
    if (selected) selected.classList.remove('selected');
    selected = node;
    node.classList.add('selected');
    detail.textContent = node.dataset.details;
    if (notify !== false && window.parent !== window) {
      window.parent.postMessage({type: 'myflames-node', node_id: node.dataset.nodeId}, '*');
    }
  }
  nodes.forEach(function (node) {
    node.addEventListener('click', function (event) {
      if (!moved) choose(node); else event.preventDefault();
    });
    node.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault(); choose(node);
      }
    });
  });
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || !event.data || event.data.type !== 'myflames-select') return;
    var node = nodes.find(function (item) { return item.dataset.nodeId === event.data.node_id; });
    if (node) choose(node, false);
  });
  root.querySelectorAll('[data-action]').forEach(function (button) {
    var feedbackTimer;
    button.addEventListener('click', function () {
      if (button.disabled) return;
      window.clearTimeout(feedbackTimer);
      button.classList.add('wb-activated');
      feedbackTimer = window.setTimeout(function () {
        button.classList.remove('wb-activated');
      }, 450);
      stopWheel();
      switch (button.dataset.action) {
        case 'fit': fit(); break;
        case 'in': zoom(.8, .5, .5); break;
        case 'out': zoom(1.25, .5, .5); break;
        case 'reset':
          box = {x: (graphWidth - width) / 2, y: -28, w: width, h: height};
          paint(); break;
      }
    });
  });
  var search = root.querySelector('input[type="search"]');
  search.addEventListener('input', function () {
    var query = search.value.toLowerCase().trim();
    var matches = 0;
    nodes.forEach(function (node) {
      var match = !query || node.dataset.details.toLowerCase().indexOf(query) !== -1;
      node.classList.toggle('dim', !match);
      if (match) matches++;
    });
    root.querySelector('.wb-search-status').textContent = query ? matches + ' / ' + nodes.length : '';
  });
  viewport.addEventListener('wheel', function (event) {
    // Pixel deltas from a Magic Mouse/trackpad must not be treated as full
    // wheel notches. Normalize line/page devices, then scale actual distance.
    if (!isFinite(event.deltaY) || Math.abs(event.deltaY) < .01 ||
        Math.abs(event.deltaX) > Math.abs(event.deltaY) || drag) return;
    event.preventDefault();
    // Nested SVG bounds include the oversized pan background. Use the visible
    // viewport height, converted by its parent's screen scale, for page units.
    var parentMatrix = root.getScreenCTM();
    var pageHeight = height * (parentMatrix ? Math.abs(parentMatrix.d) : 1);
    var unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? pageHeight : 1;
    var delta = Math.max(-100, Math.min(100, event.deltaY * unit)) * .001;
    // Reversing a gesture should respond immediately, without old momentum.
    if (wheelDelta * delta < 0) wheelDelta = 0;
    wheelDelta = Math.max(-.16, Math.min(.16, wheelDelta + delta));
    wheelPoint = {x: event.clientX, y: event.clientY};
    if (wheelFrame === null) wheelFrame = window.requestAnimationFrame(animateWheel);
  }, {passive: false});
  viewport.addEventListener('pointerdown', function (event) {
    if (event.button !== 0) return;
    stopWheel();
    moved = false;
    drag = {x: event.clientX, y: event.clientY, bx: box.x, by: box.y};
  });
  viewport.addEventListener('pointermove', function (event) {
    if (!drag) return;
    var dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) {
      moved = true;
      viewport.setPointerCapture(event.pointerId);
      var matrix = viewport.getScreenCTM();
      box.x = drag.bx - dx / matrix.a;
      box.y = drag.by - dy / matrix.d;
      paint();
    }
  });
  function release(event) {
    drag = null;
    if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
  }
  viewport.addEventListener('pointerup', release);
  viewport.addEventListener('pointercancel', release);
  viewport.addEventListener('pointerleave', function () { if (!moved) drag = null; });
  viewport.addEventListener('dblclick', fit);
  fit();
})();
