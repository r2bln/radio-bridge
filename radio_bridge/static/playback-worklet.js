// Приём: кадры 8 кГц (Float32) → джиттер-буфер → ресемплинг до частоты контекста → ФНЧ.

const IN_RATE = 8000;

class Playback extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.target = Math.round(IN_RATE * (o.jitterMs || 120) / 1000);
    this.maxFill = Math.round(IN_RATE * 0.6);
    this.buf = new Float32Array(IN_RATE * 2);
    this.r = 0; this.w = 0; this.fill = 0;
    this.primed = false;
    this.gain = 1;
    this.step = IN_RATE / sampleRate;
    this.pos = 0; this.a = 0; this.b = 0;
    const w = 2 * Math.PI * 3600 / sampleRate, cs = Math.cos(w), al = Math.sin(w) / (2 * 0.7071), a0 = 1 + al;
    this.f = { b0: (1 - cs) / 2 / a0, b1: (1 - cs) / a0, b2: (1 - cs) / 2 / a0, a1: -2 * cs / a0, a2: (1 - al) / a0,
               x1: 0, x2: 0, y1: 0, y2: 0 };
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.gain !== undefined) this.gain = d.gain;
      if (d.reset) { this.fill = 0; this.r = this.w; this.primed = false; }
      if (d.samples) this.push(d.samples);
    };
  }

  push(s) {
    for (let i = 0; i < s.length; i++) {
      this.buf[this.w] = s[i];
      this.w = (this.w + 1) % this.buf.length;
    }
    this.fill += s.length;
    if (this.fill > this.maxFill) {  // сеть догнала пачкой — срезаем задержку до цели
      const drop = this.fill - this.target;
      this.r = (this.r + drop) % this.buf.length;
      this.fill -= drop;
    }
    if (!this.primed && this.fill >= this.target) this.primed = true;
  }

  next() {
    if (!this.primed || this.fill === 0) { this.primed = false; return 0; }
    const v = this.buf[this.r];
    this.r = (this.r + 1) % this.buf.length;
    this.fill--;
    return v;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    const f = this.f;
    for (let i = 0; i < out.length; i++) {
      this.pos += this.step;
      while (this.pos >= 1) { this.a = this.b; this.b = this.next(); this.pos -= 1; }
      const x = this.a + (this.b - this.a) * this.pos;
      const y = f.b0 * x + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
      f.x2 = f.x1; f.x1 = x; f.y2 = f.y1; f.y1 = y;
      out[i] = y * this.gain;
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}

registerProcessor("playback", Playback);
