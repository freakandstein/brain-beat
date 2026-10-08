// Otak 3D untuk panel "EEG Channel Map" di dashboard: garis tipis otak yang berputar pelan, digambar tembus pandang (garis di
// belakang tetap tampak, hanya lebih redup), dengan empat elektroda Muse (TP9, AF7, AF8, TP10) sebagai titik di permukaannya,
// berwarna menurut kualitas kontak.
//
// Cara menggambar, proyeksi, dan penempatan sensor diambil dari TypeWave (src/render/brain.js; project yang sama pemiliknya, MIT),
// dipangkas: tanpa percikan ketikan, kelompok gelombang, dan agitasi lipatan. Ini penunjuk kontak dan hiasan, bukan peta aktivitas
// otak: posisi titik hanya perkiraan (lipatan terdekat di arah sensor), bukan anatomi persis.
//
// Bagian murni (sensorLevels, depthShade, createBrain, stepBrain, drawBrain) tidak menyentuh DOM, jadi bisa dites di Node
// (tests/js/brain-view.test.mjs). createBrainView di paling bawah adalah perekat ke <canvas> dan requestAnimationFrame.
import { BRAIN } from './brain-data.js';
import { decodeBrain } from './brain-decode.js';
import { PITCH, viewOf, perspK, projectInto } from './orbit.js';

const TAU = Math.PI * 2;

export const SENSOR_NAMES = ['TP9', 'AF7', 'AF8', 'TP10'];
export const SPIN = 0.32; // rad/detik, tetap (sekitar 19,6 detik per putaran) dan tidak mengikuti data
export const COLORS = { good: '#5eead4', weak: '#fbbf75', poor: '#fb7185', off: '#1c3b35' }; // sama dengan legenda di dashboard
const GOOD_AT = 0.7; // ambang kualitas kanal: sama dengan peta SVG yang digantikan
const WEAK_AT = 0.35;
const YAW0 = 0.55;
const INK = '#8fe9d8';
const DIM_OFF = 0.55; // otak lebih redup saat Muse belum terhubung
const PULSE_S = 0.9; // lama riak mental command

// Arah sensor dari pusat otak (x kiri-kanan, y depan, z atas), urutan TP9, AF7, AF8, TP10: dahi kiri dan kanan, dan samping bawah di belakang telinga.
const SENSOR_DIRS = [[-0.95, -0.10, -0.35], [-0.50, 0.82, 0.30], [0.50, 0.82, 0.30], [0.95, -0.10, -0.35]];

// Goresan dikelompokkan menurut tingkat alpha (0..NA, alpha = (tingkat / NA)^2) dan kelas lebar, supaya seluruh garis digambar dengan
// ratusan bukan ribuan stroke() per frame.
const NA = 24;
const WC = 4;
const NBK = (NA + 1) * WC;
// Menghadap kamera berubah halus dari belakang (0) ke depan (1) di sekitar tepi otak, supaya garis yang berputar melewati tepi tidak melompat terang.
const FACE_LO = -0.12;
const FACE_HI = 0.06;
const DP = { far: 0.12, curve: 1.5, back: 0.4, gain: 1.3, range: 1.25 }; // terang menurut kedalaman (nilai tuning TypeWave)
const ALPHA = { fold: 0.42, fixed: 0.75, mul: 0.8, lit: 0.3 }; // terang dasar lipatan, celah utama dan batang otak, pengali umum, tambahan saat riak
const LINE_W = [0.8, 0.85, 1.15, 1.25]; // lebar garis px: sisi belakang, depan jauh, depan dekat, celah utama
const FIT = 0.94; // otak mengisi sebagian kanvas; sisanya ruang untuk titik dan label di tepi

const clamp = (v, lo = 0, hi = 1) => (v < lo ? lo : v > hi ? hi : v);
const smoothstep = (a, b, x) => { const t = clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); };

// Kualitas kanal (0..1, dari server) -> level titik: 'good' | 'weak' | 'poor', atau 'off' bila Muse tidak terhubung / belum ada data.
export function sensorLevels(quality, connected) {
  if (!connected || quality == null) return SENSOR_NAMES.map(() => 'off');
  return SENSOR_NAMES.map((name) => { const q = quality[name] ?? 0; return q >= GOOD_AT ? 'good' : q >= WEAK_AT ? 'weak' : 'poor'; });
}

// Terang relatif menurut kedalaman: t 0 (terjauh) .. 1 (terdekat); face 0 (membelakangi kamera) .. 1 (menghadap kamera).
export function depthShade(t, face) {
  const u = clamp(t);
  const v = 1 - (1 - Math.pow(u, DP.curve)) * (1 - DP.far);
  return v * (DP.back + (1 - DP.back) * face);
}

// Bagian data yang tidak berubah (dibuka dari brain-data.js sekali, dipakai bersama semua instans).
const STATIC = new WeakMap();
function staticOf(data) {
  let s = STATIC.get(data);
  if (s) return s;
  const D = decodeBrain(data);
  const { n, lines } = D;
  let foldEnd = 0; // lipatan korteks (jenis 0) ada di awal data
  for (let li = 0; li < lines; li++) if (D.kind[li] === 0) foldEnd = D.start[li] + D.len[li];

  // Titik sensor: lipatan yang arahnya dari pusat otak (dinormalkan per sumbu) paling dekat dengan arah sensor dan yang normalnya menghadap ke luar ke arah itu.
  const sensor = new Int32Array(4).fill(-1);
  let mx = 0; let my = 0; let mz = 0; let hx = 1e-6; let hy = 1e-6; let hz = 1e-6;
  for (let i = 0; i < foldEnd; i++) { mx += D.x[i]; my += D.y[i]; mz += D.z[i]; }
  mx /= foldEnd || 1; my /= foldEnd || 1; mz /= foldEnd || 1;
  for (let i = 0; i < foldEnd; i++) { hx = Math.max(hx, Math.abs(D.x[i] - mx)); hy = Math.max(hy, Math.abs(D.y[i] - my)); hz = Math.max(hz, Math.abs(D.z[i] - mz)); }
  SENSOR_DIRS.forEach((t, k) => {
    const tl = Math.hypot(t[0], t[1], t[2]);
    let best = -Infinity;
    for (let i = 0; i < foldEnd; i++) {
      const vx = (D.x[i] - mx) / hx; const vy = (D.y[i] - my) / hy; const vz = (D.z[i] - mz) / hz;
      const vl = Math.hypot(vx, vy, vz) || 1;
      const score = (vx * t[0] + vy * t[1] + vz * t[2]) / (vl * tl) + 0.5 * (D.nx[i] * t[0] + D.ny[i] * t[1] + D.nz[i] * t[2]) / tl;
      if (score > best) { best = score; sensor[k] = i; }
    }
  });

  // Segmen: titik i ke i+1 pada garis yang sama; segCont = 1 bila segmen sebelumnya satu garis, supaya jalur disambung tanpa moveTo.
  const nSeg = n - lines;
  const segA = new Int32Array(nSeg);
  const segLine = new Uint16Array(nSeg);
  const segCont = new Uint8Array(nSeg);
  let q = 0;
  for (let li = 0; li < lines; li++) {
    for (let i = 0; i < D.len[li] - 1; i++) { segA[q] = D.start[li] + i; segLine[q] = li; segCont[q] = i > 0 ? 1 : 0; q++; }
  }
  s = { D, sensor, nSeg, segA, segLine, segCont };
  STATIC.set(data, s);
  return s;
}

export function createBrain(data = BRAIN, yaw = YAW0) {
  const st = staticOf(data);
  const { D } = st;
  const { n } = D;
  const b = {
    data, st, n, nSeg: st.nSeg, yaw, view: viewOf(yaw, PITCH), kp: perspK(data.unit),
    kind: D.kind, x: D.x, y: D.y, z: D.z, nx: D.nx, ny: D.ny, nz: D.nz,
    dx: new Float32Array(n), dy: new Float32Array(n), dd: new Float32Array(n), df: new Float32Array(n), // kotak desain, kedalaman, menghadap
    sx: new Float32Array(n), sy: new Float32Array(n), // piksel
    sensor: st.sensor, sdx: new Float32Array(4), sdy: new Float32Array(4), sdd: new Float32Array(4), sdf: new Float32Array(4),
    bucketOf: new Uint8Array(st.nSeg), cnt: new Int32Array(NBK + 1), at: new Int32Array(NBK), order: new Int32Array(st.nSeg),
  };
  project(b);
  return b;
}

// Titik model -> kotak desain, kedalaman, dan menghadap kamera (dikalikan normal permukaan), pada sudut b.yaw.
function project(b) {
  const v = viewOf(b.yaw, PITCH, b.view);
  const cx = b.data.w / 2;
  const cy = b.data.h / 2;
  for (let i = 0; i < b.n; i++) {
    projectInto(v, b.kp, b.x[i], b.y[i], b.z[i], b.dx, b.dy, b.dd, i);
    b.dx[i] = cx + b.dx[i];
    b.dy[i] = cy - b.dy[i];
    b.df[i] = -(b.nx[i] * v.cy - b.ny[i] * v.sy) * v.cp + b.nz[i] * v.sp;
  }
  for (let k = 0; k < 4; k++) {
    const i = b.sensor[k];
    projectInto(v, b.kp, b.x[i], b.y[i], b.z[i], b.sdx, b.sdy, b.sdd, k);
    b.sdx[k] = cx + b.sdx[k];
    b.sdy[k] = cy - b.sdy[k];
    b.sdf[k] = -(b.nx[i] * v.cy - b.ny[i] * v.sy) * v.cp + b.nz[i] * v.sp;
  }
}

// Satu langkah waktu: putar dengan kecepatan tetap lalu proyeksikan ulang.
export function stepBrain(b, dt) {
  b.yaw = (b.yaw + SPIN * dt) % TAU;
  project(b);
}

// Gambar satu frame ke ctx 2D. o: { width, height (px CSS), levels (empat level sensor), dim (1 = penuh), pulse (0..1, riak), ink, labels }.
// Mengembalikan { drawn, sensors: [{ name, level, face, x, y }] } untuk tes dan diagnostik.
export function drawBrain(ctx, b, o) {
  const W = o.width;
  const H = o.height;
  const dim = o.dim ?? 1;
  const pulse = o.pulse ?? 0;
  const ink = o.ink ?? INK;
  const levels = o.levels ?? sensorLevels(null, false);
  const k = Math.min(W, H) / 155; // skala UI: 1 pada tinggi kanvas 155 px
  const sc = Math.min(W / b.data.w, H / b.data.h) * FIT;
  const x0 = (W - b.data.w * sc) / 2;
  const y0 = (H - b.data.h * sc) / 2;
  const { sx, sy, dd, df } = b;
  for (let i = 0; i < b.n; i++) { sx[i] = x0 + b.dx[i] * sc; sy[i] = y0 + b.dy[i] * sc; }

  const mul = ALPHA.mul * dim;
  const foldA = clamp(ALPHA.fold * mul);
  const fixedA = clamp(ALPHA.fixed * mul);
  const litA = ALPHA.lit * mul;
  const { segA, segLine, segCont } = b.st;
  const { cnt, bucketOf, order, at } = b;
  const half = DP.range * b.data.unit;
  const inv = 1 / (2 * half);

  // Tiap segmen: tingkat alpha dari jenis garisnya, riak, dan kedalamannya; kelas lebar dari sisi dan kedalaman. Lalu diurutkan ke goresan.
  cnt.fill(0);
  for (let s = 0; s < b.nSeg; s++) {
    const a = segA[s];
    const kind = b.kind[segLine[s]];
    const face = smoothstep(FACE_LO, FACE_HI, (df[a] + df[a + 1]) * 0.5);
    const t = clamp(((dd[a] + dd[a + 1]) * 0.5 + half) * inv);
    const lit = kind === 0 || kind === 2; // lipatan dan otak kecil ikut menyala saat riak; celah utama dan batang otak tetap
    const base = kind === 1 || kind === 3 ? fixedA : foldA;
    const level = Math.round(Math.sqrt(clamp((base + (lit ? pulse * litA : 0)) * DP.gain * depthShade(t, face))) * NA);
    if (level === 0) { bucketOf[s] = 255; continue; }
    let wc = face < 0.5 ? 0 : kind === 1 ? 3 : t >= 0.7 ? 2 : 1;
    if (lit && pulse > 0.4 && wc === 1) wc = 2; // garis yang menyala lebih tebal
    const bk = level * WC + wc;
    bucketOf[s] = bk;
    cnt[bk + 1]++;
  }
  for (let q = 0; q < NBK; q++) cnt[q + 1] += cnt[q];
  for (let q = 0; q < NBK; q++) at[q] = cnt[q];
  for (let s = 0; s < b.nSeg; s++) { const bk = bucketOf[s]; if (bk !== 255) order[at[bk]++] = s; }

  ctx.strokeStyle = ink;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  let drawn = 0;
  for (let bk = 0; bk < NBK; bk++) { // dari yang paling redup ke yang paling terang, supaya yang terang menimpa
    const from = cnt[bk];
    const to = cnt[bk + 1];
    if (to === from) continue;
    ctx.beginPath();
    let prev = -2;
    for (let q = from; q < to; q++) {
      const s = order[q];
      const a = segA[s];
      if (prev === s - 1 && segCont[s]) ctx.lineTo(sx[a + 1], sy[a + 1]);
      else { ctx.moveTo(sx[a], sy[a]); ctx.lineTo(sx[a + 1], sy[a + 1]); }
      prev = s;
    }
    ctx.globalAlpha = (Math.floor(bk / WC) / NA) ** 2;
    ctx.lineWidth = Math.max(0.8, LINE_W[bk % WC] * k);
    ctx.stroke();
    drawn += to - from;
  }

  if (pulse > 0) { // riak mental command: cincin yang melebar dari tengah dan memudar
    const e = 1 - (1 - (1 - pulse)) ** 2;
    ctx.globalAlpha = pulse * 0.9;
    ctx.strokeStyle = COLORS.good;
    ctx.lineWidth = 0.5 + 2 * pulse * k;
    ctx.beginPath();
    ctx.arc(W / 2, H / 2, (0.05 + 0.42 * e) * W, 0, TAU);
    ctx.stroke();
  }

  // Titik sensor: selalu tampak (juga di sisi belakang, lebih kecil dan redup), di atas garis. poor = cincin, off = bulatan gelap berbingkai.
  const sensors = [];
  const rBack = 3 * k;
  const rFront = 4.6 * k;
  for (let j = 0; j < 4; j++) {
    const face = smoothstep(FACE_LO, FACE_HI, b.sdf[j]);
    const x = x0 + b.sdx[j] * sc;
    const y = y0 + b.sdy[j] * sc;
    const r = rBack + (rFront - rBack) * face;
    const a = (0.6 + 0.4 * face) * (0.5 + 0.5 * dim);
    const level = levels[j];
    ctx.globalAlpha = a * 0.6; // alas gelap supaya titik terbaca di atas garis
    ctx.fillStyle = '#021916';
    ctx.beginPath(); ctx.arc(x, y, r + 2 * k, 0, TAU); ctx.fill();
    ctx.globalAlpha = a;
    ctx.beginPath(); ctx.arc(x, y, r, 0, TAU);
    if (level === 'poor') { ctx.strokeStyle = COLORS.poor; ctx.lineWidth = Math.max(1.5, r * 0.34); ctx.stroke(); }
    else {
      ctx.fillStyle = COLORS[level];
      ctx.fill();
      if (level === 'off') { ctx.strokeStyle = '#3f7a6e'; ctx.lineWidth = 1; ctx.stroke(); }
    }
    if (o.labels !== false && face >= 0.5) { // label hanya di sisi dekat; di sisi jauh titiknya berpindah-pindah sehingga label hanya mengganggu
      const right = x < W * 0.62;
      ctx.globalAlpha = a * 0.95;
      ctx.fillStyle = '#c9f5ec';
      ctx.font = `${(8 * k).toFixed(1)}px ui-monospace, Menlo, monospace`;
      ctx.textAlign = right ? 'left' : 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(SENSOR_NAMES[j], right ? x + r + 3 * k : x - r - 3 * k, y);
    }
    sensors.push({ name: SENSOR_NAMES[j], level, face, x, y });
  }
  ctx.globalAlpha = 1;
  return { drawn, sensors };
}

// Perekat ke <canvas>: loop requestAnimationFrame (dibatasi fps), ukuran piksel mengikuti ukuran CSS dan devicePixelRatio.
// setContact(quality, connected) dipanggil tiap state_update; pulse() dari riak mental command; stats() untuk diagnostik.
export function createBrainView(canvas, { fps = 30, ink = INK } = {}) {
  const ctx = canvas.getContext('2d');
  const brain = createBrain();
  let levels = sensorLevels(null, false);
  let dim = DIM_OFF;
  let pulse = 0;
  let last = 0;
  let raf = 0;
  let stats = null;
  let cssW = 0;
  let cssH = 0;

  // Satuan gambar = piksel layout (clientWidth/Height). Backing store mengikuti ukuran yang benar-benar tampil di layar (termasuk CSS zoom
  // atau transform scale, mis. browser source OBS yang diskalakan) dikali devicePixelRatio, supaya tetap tajam dan tidak diperbesar paksa.
  function resize() {
    cssW = canvas.clientWidth || canvas.width;
    cssH = canvas.clientHeight || canvas.height;
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round((r.width || cssW) * dpr));
    const h = Math.max(1, Math.round((r.height || cssH) * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.setTransform(w / cssW, 0, 0, h / cssH, 0, 0);
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (last && now - last < 1000 / fps - 2) return;
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
    last = now;
    resize();
    stepBrain(brain, dt);
    pulse = Math.max(0, pulse - dt / PULSE_S);
    ctx.clearRect(0, 0, cssW, cssH);
    stats = drawBrain(ctx, brain, { width: cssW, height: cssH, levels, dim, pulse, ink });
  }
  raf = requestAnimationFrame(frame);

  return {
    setContact(quality, connected) {
      levels = sensorLevels(quality, connected);
      dim = connected && quality != null ? 1 : DIM_OFF;
    },
    pulse() { pulse = 1; },
    stats() { return stats && { drawn: stats.drawn, sensors: stats.sensors, pulse, dim, yaw: brain.yaw }; },
    destroy() { cancelAnimationFrame(raf); },
  };
}
