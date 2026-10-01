(function () {
  'use strict';
  var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-node-id]'));
  var known = Object.create(null);
  nodes.forEach(function (node) {
    var id = node.getAttribute('data-node-id');
    if (!id) return;
    known[id] = true;
    node.setAttribute('aria-pressed', 'false');
    if (node.tagName.toLowerCase() !== 'button') {
      node.setAttribute('tabindex', '0');
      node.setAttribute('role', 'button');
      if (!node.hasAttribute('aria-label')) {
        node.setAttribute('aria-label', node.getAttribute('data-label') || node.textContent.trim().slice(0, 180) || 'Select operator');
      }
    }
  });
  function select(id, notify) {
    if (typeof id !== 'string' || !known[id]) return;
    nodes.forEach(function (node) {
      var active = node.getAttribute('data-node-id') === id;
      node.classList.toggle('is-selected', active);
      node.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (notify && window.parent !== window) {
      window.parent.postMessage({ type: 'myflames-node', node_id: id }, '*');
    }
  }
  var pointerStart = null;
  var pointerMoved = false;
  document.addEventListener('pointerdown', function (event) {
    pointerStart = {x: event.clientX, y: event.clientY};
    pointerMoved = false;
  }, true);
  document.addEventListener('pointermove', function (event) {
    if (pointerStart && event.buttons && Math.abs(event.clientX - pointerStart.x) + Math.abs(event.clientY - pointerStart.y) > 4) pointerMoved = true;
  }, true);
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || pointerMoved) return;
    var node = event.target.closest('[data-node-id]');
    if (node) select(node.getAttribute('data-node-id'), true);
  }, true);
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var node = event.target.closest('[data-node-id]');
    if (!node || node.tagName.toLowerCase() === 'button') return;
    event.preventDefault();
    select(node.getAttribute('data-node-id'), true);
  });
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || !event.data || event.data.type !== 'myflames-select') return;
    select(event.data.node_id, false);
  });
  select(document.body.getAttribute('data-selected'), false);
})();
