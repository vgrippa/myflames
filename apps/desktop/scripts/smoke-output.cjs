'use strict';
// Scrubs URLs and launch tokens from child process output before it can reach
// a public CI log. Complete lines are scrubbed as they arrive, so a secret can
// never straddle the boundary of the bounded window; the trailing partial line
// is scrubbed when the stream ends or when the window is read.

const URL_PATTERN = /https?:\/\/\S+/gi;
const LAUNCH_PATTERN = /#launch=[A-Za-z0-9_-]+/g;

function scrub(text) {
  return String(text).replace(URL_PATTERN, '[private URL]').replace(LAUNCH_PATTERN, '#launch=[private]');
}

class OutputCollector {
  constructor(limit) {
    this.limit = limit;
    this.window = '';
    this.partial = '';
  }

  push(chunk) {
    const lines = (this.partial + chunk.toString()).split('\n');
    this.partial = lines.pop();
    for (const line of lines) this._append(scrub(line) + '\n');
  }

  read() {
    return (this.window + (this.partial ? scrub(this.partial) : '')).slice(-this.limit);
  }

  end() {
    if (this.partial) {
      this._append(scrub(this.partial));
      this.partial = '';
    }
    return this.read();
  }

  _append(text) {
    this.window = (this.window + text).slice(-this.limit);
  }
}

module.exports = { OutputCollector, scrub };
