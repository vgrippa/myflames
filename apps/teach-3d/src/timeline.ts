// A seekable timeline with the teach toolbar contract (play, pause, resume,
// speed, scrubber, reset, marks). Unlike _anim.py's queue, every step sets
// absolute state from its progress t, so seeking is: reset the stage, then
// apply every step that has started. Scrubbing, pausing and reduced motion
// all fall out of that one function.

export type Run = (t: number) => void;

interface Step {
  at: number;
  dur: number;
  run: Run;
}

export interface Mark {
  label: string;
  at: number;
}

export const ease = {
  inOutCubic: (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  outCubic: (t: number) => 1 - Math.pow(1 - t, 3),
  inOutQuad: (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
};

export class Timeline {
  private steps: Step[] = [];
  private cursor = 0;
  private lastAt = 0;
  readonly marks: Mark[] = [];

  get total(): number {
    return Math.max(this.cursor, ...this.steps.map((s) => s.at + s.dur));
  }

  /** Append a step after everything added so far. */
  add(dur: number, run: Run): this {
    this.steps.push({ at: this.cursor, dur, run });
    this.lastAt = this.cursor;
    this.cursor += dur;
    return this;
  }

  /** Run alongside the previous step, starting `offset` ms after it. */
  with(dur: number, run: Run, offset = 0): this {
    const at = this.lastAt + offset;
    this.steps.push({ at, dur, run });
    this.cursor = Math.max(this.cursor, at + dur);
    return this;
  }

  /** A step that happens at one instant (t is always 1 once reached). */
  call(run: () => void): this {
    return this.add(0, () => run());
  }

  delay(ms: number): this {
    this.cursor += ms;
    return this;
  }

  mark(label: string): this {
    this.marks.push({ label, at: this.cursor });
    return this;
  }

  /** Apply the state at time `time` (call after resetting the stage). */
  apply(time: number): void {
    for (const step of this.steps) {
      if (time < step.at) continue;
      step.run(step.dur <= 0 ? 1 : Math.min(1, (time - step.at) / step.dur));
    }
  }
}

export interface PlayerHooks {
  /** Draw the state at `time`: reset the stage, then timeline.apply(time). */
  render(time: number): void;
  /** Play state changed (for the toolbar). */
  changed(): void;
}

/** Virtual clock driving a Timeline: speed-aware, pausable, seekable. */
export class Player {
  time = 0;
  playing = false;
  paused = false;
  speed = 1;
  private timeline: Timeline = new Timeline();
  private frame = 0;
  private last = 0;

  constructor(private hooks: PlayerHooks, private reducedMotion: () => boolean) {}

  load(timeline: Timeline): void {
    this.stop();
    this.timeline = timeline;
    this.time = 0;
    this.hooks.render(0);
    this.hooks.changed();
  }

  get total(): number {
    return this.timeline.total;
  }

  get marks(): Mark[] {
    return this.timeline.marks;
  }

  get ended(): boolean {
    return this.time >= this.total && this.total > 0;
  }

  play(): void {
    if (this.ended) this.time = 0;
    if (this.reducedMotion()) {
      this.seek(this.total);
      return;
    }
    this.playing = true;
    this.paused = false;
    this.last = performance.now();
    this.loop();
    this.hooks.changed();
  }

  pause(): void {
    if (!this.playing) return;
    this.paused = true;
    this.hooks.changed();
  }

  resume(): void {
    if (!this.playing) return this.play();
    this.paused = false;
    this.last = performance.now();
    this.hooks.changed();
  }

  /** Jump to `time` and stay paused there (scrubber, marks, reduced motion). */
  seek(time: number): void {
    this.time = Math.max(0, Math.min(this.total, time));
    if (this.playing) this.paused = true;
    this.hooks.render(this.time);
    if (this.ended) this.playing = false;
    this.hooks.changed();
  }

  reset(): void {
    this.stop();
    this.time = 0;
    this.hooks.render(0);
    this.hooks.changed();
  }

  private stop(): void {
    this.playing = false;
    this.paused = false;
    cancelAnimationFrame(this.frame);
  }

  private loop = (): void => {
    cancelAnimationFrame(this.frame);
    const tick = (now: number) => {
      if (!this.playing) return;
      // The loop keeps running while paused so resume picks up at once.
      if (!this.paused) {
        this.time = Math.min(this.total, this.time + (now - this.last) * this.speed);
        this.hooks.render(this.time);
        if (this.time >= this.total) {
          this.playing = false;
          this.hooks.changed();
          return;
        }
      }
      this.last = now;
      this.hooks.changed();
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  };
}
