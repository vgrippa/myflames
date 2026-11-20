'use strict';
// Timing bookkeeping for the packaged desktop smoke (smokeWindow in main.cjs).
// Pure and dependency-free so node:test covers it; the packaged app includes
// it through electron-builder.json "files".

// Records the phase the smoke is in and how long each completed phase took.
// Phases repeat (ready, cookie and session run again after the reload), so
// times accumulate per name in first-use order. onPhase, when given, fires
// once per phase change with the name and the milliseconds since construction,
// for the smoke's stdout progress lines.
class SmokePhases {
  constructor(now = Date.now, onPhase = null) {
    this._now = now;
    this._onPhase = onPhase;
    this._order = [];
    this._elapsed = new Map();
    this._current = null;
    this._started = 0;
    this._origin = this._now();
  }
  begin(name) {
    if (name === this._current) return;
    this.end();
    this._current = name;
    this._started = this._now();
    if (this._onPhase) this._onPhase(name, this._started - this._origin);
  }
  end() {
    if (this._current === null) return;
    if (!this._elapsed.has(this._current)) {
      this._elapsed.set(this._current, 0);
      this._order.push(this._current);
    }
    this._elapsed.set(this._current, this._elapsed.get(this._current) + this._now() - this._started);
    this._current = null;
  }
  elapsed(name) {
    return this._elapsed.get(name) || 0;
  }
  currentElapsed() {
    return this._current === null ? 0 : this._now() - this._started;
  }
  // "ready 840 ms, cookie 15 ms": the success line's timings.
  format() {
    return this._order.map(name => `${name} ${this._elapsed.get(name)} ms`).join(', ');
  }
  // A deadline failure names the phase the smoke was in and lists the
  // completed phases with their milliseconds.
  describeTimeout(budgetMs) {
    const budget = `${Math.round(budgetMs / 1000)} s readiness budget ran out`;
    const completed = this._order.length
      ? 'completed phases: ' + this.format()
      : 'no phase had completed';
    return this._current === null
      ? `The ${budget} between phases; ${completed}.`
      : `The ${budget} in phase "${this._current}" (${this.currentElapsed()} ms into it); ${completed}.`;
  }
}

module.exports = { SmokePhases };
