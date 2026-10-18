// Микрофон → ФВЧ 250 Гц → ФНЧ 3,4 кГц → ресемплинг до 8 кГц → кадры μ-law по 160 байт.
// ФВЧ режет гул, удары по корпусу и дыхание — радиоголосу ниже 300 Гц делать нечего.
// В main thread уходят все кадры (для индикатора); слать ли их в сеть — решает app.js.

const OUT_RATE = 8000;
const FRAME = 160;

function biquadLowpass(fc, fs, q) {
  const w = 2 * Math.PI * fc / fs, cs = Math.cos(w), al = Math.sin(w) / (2 * q);
  const a0 = 1 + al;
  return { b0: (1 - cs) / 2 / a0, b1: (1 - cs) / a0, b2: (1 - cs) / 2 / a0,
           a1: -2 * cs / a0, a2: (1 - al) / a0, x1: 0, x2: 0, y1: 0, y2: 0 };
}

function biquadHighpass(fc, fs, q) {
  const w = 2 * Math.PI * fc / fs, cs = Math.cos(w), al = Math.sin(w) / (2 * q);
  const a0 = 1 + al;
  return { b0: (1 + cs) / 2 / a0, b1: -(1 + cs) / a0, b2: (1 + cs) / 2 / a0,
           a1: -2 * cs / a0, a2: (1 - al) / a0, x1: 0, x2: 0, y1: 0, y2: 0 };
}

function run(f, x) {
  const y = f.b0 * x + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
  f.x2 = f.x1; f.x1 = x; f.y2 = f.y1; f.y1 = y;
  return y;
}

function ulawEncode(s) {
  let p = Math.round(s * 32768);
  p = p > 32767 ? 32767 : p < -32768 ? -32768 : p;
  const sign = p < 0 ? 0x80 : 0;
  const m = Math.min(Math.abs(p), 32635) + 0x84;
  const e = 31 - Math.clz32(m >> 7);
  const mant = (m >> (e + 3)) & 0x0f;
  return ~(sign | (e << 4) | mant) & 0xff;
}

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    // 4-й порядок Баттерворта: два звена с Q 0.541 и 1.307
    this.f1 = biquadLowpass(3400, sampleRate, 0.5412);
    this.f2 = biquadLowpass(3400, sampleRate, 1.3066);
    this.hp = biquadHighpass(250, sampleRate, 0.7071);
    this.peak = 0;    // пик сырого входа за кадр — для индикатора перегруза
    this.raw = 0;     // сумма |x| сырого входа — цифровая тишина = ровно 0
    this.step = sampleRate / OUT_RATE;
    this.pos = 0;
    this.prev = 0;
    this.frame = new Uint8Array(FRAME);
    this.n = 0;
    this.sq = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const a = Math.abs(ch[i]);
      if (a > this.peak) this.peak = a;
      this.raw += a;
      const cur = run(this.f2, run(this.f1, run(this.hp, ch[i])));
      // выходные отсчёты, попадающие между prev (t=-1) и cur (t=0)
      while (this.pos <= 1) {
        const v = this.prev + (cur - this.prev) * this.pos;
        this.frame[this.n++] = ulawEncode(v);
        this.sq += v * v;
        if (this.n === FRAME) {
          const rms = Math.sqrt(this.sq / FRAME);
          this.port.postMessage({ frame: this.frame.buffer, dbfs: rms > 1e-6 ? 20 * Math.log10(rms) : -120,
                                  peak: this.peak, zero: this.raw === 0 }, [this.frame.buffer]);
          this.frame = new Uint8Array(FRAME);
          this.n = 0;
          this.sq = 0;
          this.peak = 0;
          this.raw = 0;
        }
        this.pos += this.step;
      }
      this.pos -= 1;
      this.prev = cur;
    }
    return true;
  }
}

registerProcessor("capture", Capture);
