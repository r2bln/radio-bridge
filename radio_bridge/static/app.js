"use strict";
// Клиент radio-bridge: WebSocket /ws, μ-law 8 кГц, PTT удержанием, SSTV. Протокол — docs/SPEC.md §6.

const PROTOCOL_MAJOR = 1;
const $ = (id) => document.getElementById(id);
const SSTV_DURATION_S = { robot36: 38, martin1: 116, scottie1: 111 };  // с тоном VOX

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
  tot: 180, tx: "idle", txStart: 0, rxOpen: false, pressed: false, connected: false, manualClose: false,
  modes: {}, img: null, sstvTotal: 0,
};

const store = {
  get(k, d) { try { return localStorage.getItem("rb." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("rb." + k, v); } catch { /* приватный режим */ } },
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
  closeMic();
  st.mic = await navigator.mediaDevices.getUserMedia({ audio });
  st.micSrc = st.ctx.createMediaStreamSource(st.mic);
  st.micSrc.connect(st.capture);
}

function closeMic() {
  if (st.mic) st.mic.getTracks().forEach((t) => t.stop());
  if (st.micSrc) st.micSrc.disconnect();
  st.mic = st.micSrc = null;
}

async function listMics() {
  const sel = $("mic"), cur = sel.value;
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  sel.innerHTML = '<option value="">по умолчанию</option>';
  for (const d of devs) {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || "микрофон " + sel.length;
    sel.appendChild(o);
  }
  sel.value = cur;
}

async function setupAudio() {
  if (st.ctx) {
    if (!st.mic) await openMic();
    return;
  }
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
  applyMicGain();

  await openMic();
  await listMics();
}

function applyMicGain() {
  const db = Number($("micgain").value);
  $("micgainval").textContent = "+" + db + " dB";
  if (st.capture) st.capture.port.postMessage({ gain: Math.pow(10, db / 20) });
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
  st.manualClose = false;
  setConn("подключение…");
  $("connect").textContent = "Отменить";

  ws.onopen = () => {
    st.connected = true;
    setConn("подключено", "on");
    msg("");
    $("connect").textContent = "Отключиться";
    $("connect").classList.add("danger");
    render();
  };

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
        fillModes(m.sstv_modes || []);
        break;
      case "state":
        if (m.tx !== "idle" && st.tx === "idle") st.txStart = performance.now() - m.tx_elapsed_s * 1000;
        st.tx = m.tx;
        if (m.sstv) st.sstvTotal = m.sstv.total_s;
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
        msg("Сработал TOT (" + fmt(m.tot_s) + ") — передача остановлена");
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
    if (st.manualClose) setConn("отключено");
    else if (e.code === 4001) { setConn("станция занята", "err"); msg("Подключён другой клиент"); }
    else setConn("связь потеряна", "err");
    $("connect").textContent = st.manualClose ? "Подключиться" : "Переподключиться";
    $("connect").classList.remove("danger");
    render();
  };
}

function disconnect() {
  pttUp();
  st.manualClose = true;
  if (st.ws) st.ws.close(1000);
  closeMic();                       // гасим индикатор микрофона в браузере
  if (st.ctx) st.ctx.suspend();
  meter("txbar", "txval", -120);
  meter("rxbar", "rxval", -120);
  $("micwarn").textContent = "";
}

// ---- PTT -----------------------------------------------------------------

function pttDown() {
  if (st.pressed || !st.connected || st.tx !== "idle") return;
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

// ---- SSTV ----------------------------------------------------------------

function fillModes(list) {
  const sel = $("sstvmode"), want = store.get("sstvmode", "robot36");
  sel.innerHTML = "";
  st.modes = {};
  for (const m of list) {
    st.modes[m.name] = m;
    const o = document.createElement("option");
    o.value = m.name;
    o.textContent = m.title + " · " + fmt(SSTV_DURATION_S[m.name] || 0);
    sel.appendChild(o);
  }
  if (st.modes[want]) sel.value = want;
  drawPreview();
}

function currentMode() { return st.modes[$("sstvmode").value]; }

function drawPreview() {
  const m = currentMode() || { width: 320, height: 240 };
  const c = $("preview"), g = c.getContext("2d");
  c.width = m.width;
  c.height = m.height;
  g.fillStyle = "#000";
  g.fillRect(0, 0, c.width, c.height);
  if (st.img) {  // обрезка «cover»: заполнить кадр целиком, лишнее по краям срезать
    const k = Math.max(c.width / st.img.width, c.height / st.img.height);
    const w = st.img.width * k, h = st.img.height * k;
    g.imageSmoothingQuality = "high";
    g.drawImage(st.img, (c.width - w) / 2, (c.height - h) / 2, w, h);
  }
  const text = $("caption").value.trim();
  if (text) {
    const size = Math.round(c.height * 0.1);
    g.font = "bold " + size + "px system-ui, sans-serif";
    g.textBaseline = "bottom";
    g.lineWidth = Math.max(2, size / 6);
    g.strokeStyle = "#000";
    g.fillStyle = "#fff";
    g.strokeText(text, size * 0.4, c.height - size * 0.3);
    g.fillText(text, size * 0.4, c.height - size * 0.3);
  }
  render();
}

function loadImage(blob) {
  if (!blob || !blob.type.startsWith("image/")) return;
  createImageBitmap(blob)
    .then((b) => { st.img = b; $("droptext").textContent = "Другая картинка: Ctrl+V, перетащите или"; drawPreview(); })
    .catch(() => msg("Не удалось открыть картинку"));
}

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function sendSstv() {
  const m = currentMode();
  if (!m || !st.img || !st.connected || st.tx !== "idle") return;
  if (!confirm("Передать картинку в эфир?\n" + m.title + ", примерно " + fmt(SSTV_DURATION_S[m.name]))) return;
  const px = $("preview").getContext("2d").getImageData(0, 0, m.width, m.height).data;
  const rgb = new Uint8Array(m.width * m.height * 3);
  for (let i = 0, j = 0; i < px.length; i += 4) { rgb[j++] = px[i]; rgb[j++] = px[i + 1]; rgb[j++] = px[i + 2]; }
  st.ws.send(JSON.stringify({ type: "sstv", mode: m.name, rgb: toBase64(rgb) }));
}

// ---- отрисовка -----------------------------------------------------------

function render() {
  const b = $("ptt");
  b.disabled = !st.connected || st.tx === "sstv";
  b.className = st.tx === "idle" ? "" : st.tx;
  b.textContent = { idle: st.pressed ? "…" : "PTT", wake: "VOX…", voice: "ПЕРЕДАЧА", locked: "СТОП",
                    sstv: "SSTV" }[st.tx];
  const badge = $("rxbadge");
  badge.className = "badge" + (st.rxOpen ? " rx" : "");
  badge.textContent = st.rxOpen ? "приём: сигнал" : "приём: тишина";
  const sending = st.tx === "sstv";
  $("sstvsend").disabled = !(st.connected && st.img && st.tx === "idle" && currentMode());
  $("sstvsend").textContent = sending ? "Идёт передача…" : "Отправить в эфир";
  $("sstvstop").hidden = !sending;
  $("sstvprog").hidden = !sending;
}

function tick() {
  const active = st.tx !== "idle";
  const el = active ? (performance.now() - st.txStart) / 1000 : 0;
  $("timer").innerHTML = fmt(el) + " <small>/ " + fmt(st.tot) + (active ? " · осталось " + fmt(st.tot - el) : "") + "</small>";
  if (st.tx === "sstv" && st.sstvTotal) $("sstvbar").style.width = Math.min(100, el / st.sstvTotal * 100) + "%";
  requestAnimationFrame(tick);
}

// ---- обвязка -------------------------------------------------------------

$("connect").onclick = async () => {
  if (st.ws && st.ws.readyState !== WebSocket.CLOSED) { disconnect(); return; }
  try {
    await setupAudio();
    await st.ctx.resume();
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

// Пробел — PTT везде, кроме полей ввода; preventDefault не даёт ему «нажать» кнопку в фокусе.
const typing = (e) => e.target.closest("select, input, textarea");
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || typing(e)) return;
  e.preventDefault();
  if (!e.repeat) pttDown();
});
document.addEventListener("keyup", (e) => {
  if (e.code !== "Space") return;
  if (!typing(e)) e.preventDefault();
  pttUp();
});
window.addEventListener("blur", pttUp);
document.addEventListener("visibilitychange", () => { if (document.hidden) pttUp(); });
window.addEventListener("pagehide", pttUp);

$("vol").oninput = (e) => st.playback && st.playback.port.postMessage({ gain: Number(e.target.value) });
$("mic").onchange = () => st.mic && openMic().catch((err) => msg(err.message));

$("micgain").value = store.get("micgain", "0");
$("micgain").oninput = () => { store.set("micgain", $("micgain").value); applyMicGain(); };
applyMicGain();
$("caption").value = store.get("caption", "");
$("caption").oninput = () => { store.set("caption", $("caption").value); drawPreview(); };
$("sstvmode").onchange = () => { store.set("sstvmode", $("sstvmode").value); drawPreview(); };
$("file").onchange = (e) => { loadImage(e.target.files[0]); e.target.value = ""; };  // сброс — чтобы тот же файл можно было выбрать снова
$("preview").onclick = () => $("file").click();
$("sstvsend").onclick = sendSstv;
$("sstvstop").onclick = () => st.ws && st.ws.send(JSON.stringify({ type: "sstv_stop" }));
document.addEventListener("paste", (e) => {
  for (const item of e.clipboardData.items) if (item.type.startsWith("image/")) loadImage(item.getAsFile());
});
const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  loadImage(e.dataTransfer.files[0]);
});

drawPreview();
requestAnimationFrame(tick);
