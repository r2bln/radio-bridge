"use strict";
// Клиент radio-bridge: WebSocket /ws, μ-law 8 кГц, PTT удержанием. Протокол — docs/SPEC.md §6.

const PROTOCOL_MAJOR = 1;
const FRAME = 160;
const $ = (id) => document.getElementById(id);

const ULAW = (() => {
  const t = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xff, sign = u & 0x80, e = (u >> 4) & 7, m = u & 0x0f;
    const mag = (((m << 3) + 0x84) << e) - 0x84;
    t[i] = (sign ? -mag : mag) / 32768;
  }
  return t;
})();

const st = {
  ctx: null, ws: null, mic: null, micSrc: null, capture: null, playback: null,
  tot: 180, tx: "idle", txStart: 0, rxOpen: false, pressed: false, connected: false,
};

function setConn(text, cls) {
  $("conn").textContent = text;
  $("dot").className = "dot" + (cls ? " " + cls : "");
}

function msg(text) { $("msg").textContent = text || ""; }

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function meter(bar, val, dbfs) {
  const pct = Math.max(0, Math.min(100, (dbfs + 70) / 70 * 100));
  $(bar).style.width = pct + "%";
  $(val).textContent = dbfs <= -119 ? "—" : dbfs.toFixed(0) + " dB";
}

// ---- аудио ---------------------------------------------------------------

async function openMic() {
  const deviceId = $("mic").value;
  const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 };
  if (deviceId) audio.deviceId = { exact: deviceId };
  if (st.mic) st.mic.getTracks().forEach((t) => t.stop());
  if (st.micSrc) st.micSrc.disconnect();
  st.mic = await navigator.mediaDevices.getUserMedia({ audio });
  st.micSrc = st.ctx.createMediaStreamSource(st.mic);
  st.micSrc.connect(st.capture);
}

async function listMics() {
  const sel = $("mic"), cur = sel.value;
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  sel.innerHTML = '<option value="">по умолчанию</option>';
  for (const d of devs) {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || "микрофон " + (sel.length);
    sel.appendChild(o);
  }
  sel.value = cur;
}

async function setupAudio() {
  if (st.ctx) return;
  if (!window.isSecureContext || !navigator.mediaDevices) {
    throw new Error("Браузер не даёт микрофон: нужна страница по https:// (или localhost)");
  }
  st.ctx = new AudioContext({ latencyHint: "interactive" });
  await st.ctx.audioWorklet.addModule("/static/capture-worklet.js");
  await st.ctx.audioWorklet.addModule("/static/playback-worklet.js");

  st.capture = new AudioWorkletNode(st.ctx, "capture", { numberOfOutputs: 1 });
  const sink = st.ctx.createGain();
  sink.gain.value = 0;  // захвату нужен путь до destination, чтобы process() вызывался
  st.capture.connect(sink).connect(st.ctx.destination);
  st.capture.port.onmessage = (e) => {
    meter("txbar", "txval", e.data.dbfs);
    micHealth(e.data);
    if (st.pressed && st.ws && st.ws.readyState === WebSocket.OPEN && (st.tx === "wake" || st.tx === "voice")) {
      st.ws.send(e.data.frame);
    }
  };

  st.playback = new AudioWorkletNode(st.ctx, "playback", {
    numberOfInputs: 0, outputChannelCount: [2], processorOptions: { jitterMs: 120 },
  });
  st.playback.connect(st.ctx.destination);
  st.playback.port.postMessage({ gain: Number($("vol").value) });

  await openMic();
  await listMics();
}

// Окно 50 кадров (1 с): цифровая тишина = микрофон отвалился, частый клиппинг = убавить уровень.
const mh = { zero: 0, clip: 0, n: 0 };
function micHealth(d) {
  mh.zero += d.zero ? 1 : 0;
  mh.clip += d.peak > 0.98 ? 1 : 0;
  if (++mh.n < 50) return;
  let text = "";
  if (mh.zero === mh.n) text = "Микрофон отдаёт цифровую тишину — проверьте устройство (или перезапустите браузер)";
  else if (mh.clip >= 5) text = "Микрофон перегружен — убавьте уровень входа в системе";
  $("micwarn").textContent = text;
  mh.zero = mh.clip = mh.n = 0;
}

// ---- сеть ----------------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.binaryType = "arraybuffer";
  st.ws = ws;
  setConn("подключение…");

  ws.onopen = () => { st.connected = true; setConn("подключено", "on"); msg(""); render(); };

  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      const u = new Uint8Array(e.data);
      const s = new Float32Array(u.length);
      for (let i = 0; i < u.length; i++) s[i] = ULAW[u[i]];
      st.playback.port.postMessage({ samples: s }, [s.buffer]);
      return;
    }
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    switch (m.type) {
      case "hello":
        if (Math.floor(m.version) !== PROTOCOL_MAJOR) {
          msg("Несовместимая версия протокола сервера: " + m.version);
          ws.close();
        }
        st.tot = m.tot_s;
        break;
      case "state":
        if ((m.tx === "wake" || m.tx === "voice" || m.tx === "locked") && st.tx === "idle") {
          st.txStart = performance.now() - m.tx_elapsed_s * 1000;
        }
        st.tx = m.tx;
        if (m.rx_open && !st.rxOpen) st.playback.port.postMessage({ reset: true });
        st.rxOpen = m.rx_open;
        if (m.tx === "locked") msg("Передача остановлена — отпустите PTT");
        else if (m.tx !== "idle") msg("");
        render();
        break;
      case "level":
        meter("rxbar", "rxval", m.rx_dbfs);
        break;
      case "tot_expired":
        msg("Сработал TOT (" + fmt(m.tot_s) + ") — отпустите PTT");
        break;
      case "error":
        msg(m.message || m.code);
        break;
    }
  };

  ws.onclose = (e) => {
    st.connected = false;
    st.tx = "idle";
    st.pressed = false;
    if (e.code === 4001) { setConn("станция занята", "err"); msg("Подключён другой клиент"); }
    else setConn("отключено", "err");
    $("connect").hidden = false;
    $("connect").textContent = "Переподключиться";
    render();
  };
}

// ---- PTT -----------------------------------------------------------------

function pttDown() {
  if (st.pressed || !st.connected || st.tx === "locked") return;
  st.pressed = true;
  st.ws.send(JSON.stringify({ type: "ptt", state: "down" }));
  render();
}

function pttUp() {
  if (!st.pressed) return;
  st.pressed = false;
  if (st.ws && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify({ type: "ptt", state: "up" }));
  render();
}

function render() {
  const b = $("ptt");
  b.disabled = !st.connected;
  b.className = st.tx === "idle" ? "" : st.tx;
  b.textContent = { idle: st.pressed ? "…" : "PTT", wake: "VOX…", voice: "ПЕРЕДАЧА", locked: "СТОП" }[st.tx];
  const badge = $("rxbadge");
  badge.className = "badge" + (st.rxOpen ? " rx" : "");
  badge.textContent = st.rxOpen ? "приём: сигнал" : "приём: тишина";
}

function tick() {
  const active = st.tx !== "idle";
  const el = active ? (performance.now() - st.txStart) / 1000 : 0;
  $("timer").innerHTML = fmt(el) + " <small>/ " + fmt(st.tot) + (active ? " · осталось " + fmt(st.tot - el) : "") + "</small>";
  requestAnimationFrame(tick);
}

// ---- обвязка -------------------------------------------------------------

$("connect").onclick = async () => {
  try {
    await setupAudio();
    await st.ctx.resume();
    $("connect").hidden = true;
    connect();
  } catch (err) {
    msg(err.message || String(err));
  }
};

const ptt = $("ptt");
ptt.addEventListener("pointerdown", (e) => { ptt.setPointerCapture(e.pointerId); pttDown(); });
ptt.addEventListener("pointerup", pttUp);
ptt.addEventListener("pointercancel", pttUp);
ptt.addEventListener("lostpointercapture", pttUp);
ptt.addEventListener("contextmenu", (e) => e.preventDefault());

document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || e.target.closest("select, input")) return;
  e.preventDefault();
  pttDown();
});
document.addEventListener("keyup", (e) => {
  if (e.code !== "Space") return;
  e.preventDefault();
  pttUp();
});
window.addEventListener("blur", pttUp);
document.addEventListener("visibilitychange", () => { if (document.hidden) pttUp(); });
window.addEventListener("pagehide", pttUp);

$("vol").oninput = (e) => st.playback && st.playback.port.postMessage({ gain: Number(e.target.value) });
$("mic").onchange = () => st.ctx && openMic().catch((err) => msg(err.message));

render();
requestAnimationFrame(tick);
