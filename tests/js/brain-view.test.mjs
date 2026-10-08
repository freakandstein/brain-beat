// Tes otak 3D untuk panel EEG Channel Map (static/brain3d/brain-view.js): level sensor dari kualitas kanal, penempatan keempat
// elektroda di permukaan otak, putaran, dan gambar (dengan ctx palsu, gaya yang sama dengan tes TypeWave).
//
// Jalankan dari root project:
//     node --test tests/js/*.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { BRAIN } from '../../static/brain3d/brain-data.js';
import { SENSOR_NAMES, SPIN, COLORS, sensorLevels, depthShade, createBrain, stepBrain, drawBrain } from '../../static/brain3d/brain-view.js';

const W = 190;
const H = 155;
const GOOD = { TP9: 1, AF7: 1, AF8: 1, TP10: 1 };

// ctx palsu: mencatat goresan garis, bentuk bulat (arc + fill/stroke), dan teks; koordinat yang tidak hingga atau jauh di luar kanvas dicatat di bad.
function fakeCtx() {
  const rec = { strokes: 0, lineAlphas: [], inkParts: [], shapes: [], texts: [], bad: [] };
  const chk = (...n) => { for (const v of n) if (!Number.isFinite(v) || v < -30 || v > Math.max(W, H) + 30) rec.bad.push(v); };
  let cur = null;
  let segs = 0;
  const ctx = {
    globalAlpha: 1, lineWidth: 1, strokeStyle: '', fillStyle: '', lineJoin: '', lineCap: '', font: '', textAlign: '', textBaseline: '',
    save() {}, restore() {}, closePath() {},
    beginPath() { cur = null; segs = 0; },
    moveTo: (x, y) => chk(x, y),
    lineTo: (x, y) => { chk(x, y); segs++; },
    arc: (x, y, r) => { chk(x, y); if (!(r >= 0)) rec.bad.push(r); cur = { x, y, r }; },
    stroke() {
      if (cur) rec.shapes.push({ op: 'stroke', ...cur, style: ctx.strokeStyle, alpha: ctx.globalAlpha });
      else { rec.strokes++; rec.lineAlphas.push(ctx.globalAlpha); rec.inkParts.push(ctx.globalAlpha * segs); }
    },
    fill() { if (cur) rec.shapes.push({ op: 'fill', ...cur, style: ctx.fillStyle, alpha: ctx.globalAlpha }); },
    fillText: (t, x, y) => { chk(x, y); rec.texts.push({ t, x, y, alpha: ctx.globalAlpha }); },
  };
  return { ctx, rec };
}

function draw(b, o = {}) {
  const { ctx, rec } = fakeCtx();
  const stats = drawBrain(ctx, b, { width: W, height: H, levels: ['good', 'weak', 'poor', 'off'], dim: 1, pulse: 0, ...o });
  return { stats, rec };
}

const maxAlpha = (rec) => Math.max(...rec.lineAlphas);
// Total "tinta" garis = jumlah (alpha x segmen) semua goresan. Alpha tertinggi tidak cocok sebagai ukuran terang: dikuasai celah utama
// dan batang otak yang memang tidak ikut menyala saat riak.
const ink = (rec) => rec.inkParts.reduce((s, v) => s + v, 0);
const at = (yaw) => { const b = createBrain(BRAIN, yaw); stepBrain(b, 0); return b; };

// --- level sensor -----------------------------------------------------------------------------------------------------------
test('level sensor mengikuti ambang peta lama (good >= 0.7, weak >= 0.35) dengan urutan TP9 AF7 AF8 TP10', () => {
  assert.deepEqual(SENSOR_NAMES, ['TP9', 'AF7', 'AF8', 'TP10']);
  assert.deepEqual(sensorLevels({ TP9: 1, AF7: 0.7, AF8: 0.699, TP10: 0.35 }, true), ['good', 'good', 'weak', 'weak']);
  assert.deepEqual(sensorLevels({ TP9: 0.349, AF7: 0, AF8: 0.2 }, true), ['poor', 'poor', 'poor', 'poor']); // TP10 tidak ada = 0
});

test('tanpa koneksi atau tanpa data kualitas semua sensor off', () => {
  assert.deepEqual(sensorLevels(GOOD, false), ['off', 'off', 'off', 'off']);
  assert.deepEqual(sensorLevels(null, true), ['off', 'off', 'off', 'off']);
  assert.deepEqual(sensorLevels(undefined, true), ['off', 'off', 'off', 'off']);
});

// --- terang menurut kedalaman -------------------------------------------------------------------------------------------------
test('depthShade: makin dekat makin terang, sisi belakang lebih redup tetapi tetap tampak, nilai di luar rentang dijepit', () => {
  let prev = -1;
  for (let t = 0; t <= 1.0001; t += 0.1) { const v = depthShade(t, 1); assert.ok(v >= prev, `turun di t=${t}`); prev = v; }
  assert.equal(depthShade(1, 1), 1);
  for (const t of [0, 0.5, 1]) {
    assert.ok(depthShade(t, 0) < depthShade(t, 1), `belakang harus lebih redup di t=${t}`);
    assert.ok(depthShade(t, 0) > 0, 'sisi belakang tetap tampak');
  }
  assert.equal(depthShade(-5, 1), depthShade(0, 1));
  assert.equal(depthShade(5, 1), depthShade(1, 1));
});

// --- putaran ------------------------------------------------------------------------------------------------------------------
test('otak berputar dengan kecepatan tetap, sekitar 20 detik per putaran', () => {
  const period = (2 * Math.PI) / SPIN;
  assert.ok(period > 19 && period < 21, `periode ${period.toFixed(2)} detik`);
  const b = createBrain(BRAIN, 0);
  stepBrain(b, 1);
  assert.ok(Math.abs(b.yaw - SPIN) < 1e-9);
  stepBrain(b, period - 1);
  assert.ok(b.yaw < 1e-6 || Math.abs(b.yaw - 2 * Math.PI) < 1e-6, `yaw ${b.yaw} setelah satu putaran penuh`);
});

// --- penempatan sensor --------------------------------------------------------------------------------------------------------
test('keempat elektroda di sisi yang benar: AF di dahi kiri dan kanan, TP di samping bawah dan lebih ke belakang', () => {
  const b = createBrain();
  assert.equal(new Set(b.sensor).size, 4, 'empat titik berbeda');
  const [tp9, af7, af8, tp10] = Array.from(b.sensor, (i) => ({ x: b.x[i], y: b.y[i], z: b.z[i] }));
  assert.ok(af7.x < 0 && tp9.x < 0, 'AF7 dan TP9 di kiri');
  assert.ok(af8.x > 0 && tp10.x > 0, 'AF8 dan TP10 di kanan');
  assert.ok(af7.y > 0 && af8.y > 0, 'AF di depan');
  assert.ok(tp9.y < af7.y && tp10.y < af8.y, 'TP lebih ke belakang daripada AF');
  assert.ok(tp9.z < af7.z && tp10.z < af8.z, 'TP lebih rendah daripada AF');
  assert.ok(Math.abs(tp9.x) > Math.abs(af7.x) && Math.abs(tp10.x) > Math.abs(af8.x), 'TP lebih ke samping');
});

test('sensor hanya menghadap kamera saat berada di sisi dekat: TP9 di yaw 0, TP10 setelah setengah putaran', () => {
  const near = at(0); // yaw 0: kamera di sisi kiri otak
  assert.ok(near.sdf[0] > 0.3, `TP9 menghadap: ${near.sdf[0]}`);
  assert.ok(near.sdf[3] < -0.3, `TP10 membelakangi: ${near.sdf[3]}`);
  const far = at(Math.PI);
  assert.ok(far.sdf[3] > 0.3, `TP10 menghadap: ${far.sdf[3]}`);
  assert.ok(far.sdf[0] < -0.3, `TP9 membelakangi: ${far.sdf[0]}`);
});

// --- gambar -------------------------------------------------------------------------------------------------------------------
test('gambar: semua koordinat hingga dan di dalam kanvas di setiap sudut putar', () => {
  for (let yaw = 0; yaw < 6.3; yaw += 0.4) {
    const { rec } = draw(at(yaw));
    assert.deepEqual(rec.bad, [], `koordinat tidak valid pada yaw ${yaw.toFixed(1)}`);
  }
});

test('gambar: garis dikelompokkan jadi goresan (bukan satu per segmen) dan ribuan segmen tergambar', () => {
  const { stats, rec } = draw(at(0.55));
  assert.ok(stats.drawn > 2000, `segmen tergambar: ${stats.drawn}`);
  assert.ok(rec.strokes >= 5 && rec.strokes <= 130, `goresan: ${rec.strokes}`);
});

test('titik sensor: warna menurut level, poor berupa cincin, posisi sama dengan yang dilaporkan', () => {
  const { stats, rec } = draw(at(0.55));
  assert.deepEqual(stats.sensors.map((s) => s.name), SENSOR_NAMES);
  assert.deepEqual(stats.sensors.map((s) => s.level), ['good', 'weak', 'poor', 'off']);
  const find = (op, style, s) => rec.shapes.find((p) => p.op === op && p.style === style && Math.abs(p.x - s.x) < 0.5 && Math.abs(p.y - s.y) < 0.5);
  assert.ok(find('fill', COLORS.good, stats.sensors[0]), 'TP9 good = bulatan hijau');
  assert.ok(find('fill', COLORS.weak, stats.sensors[1]), 'AF7 weak = bulatan kuning');
  assert.ok(find('stroke', COLORS.poor, stats.sensors[2]), 'AF8 poor = cincin merah');
  assert.ok(!find('fill', COLORS.poor, stats.sensors[2]), 'poor tidak diisi');
  assert.ok(find('fill', COLORS.off, stats.sensors[3]), 'TP10 off = bulatan gelap');
  for (const p of rec.shapes) assert.ok(p.r > 0 && p.r < 12, `radius titik ${p.r}`);
});

test('label kanal hanya untuk sensor yang menghadap kamera', () => {
  const left = draw(at(0)).rec.texts.map((t) => t.t);
  assert.ok(left.includes('TP9'), 'yaw 0: TP9 menghadap kamera');
  assert.ok(!left.includes('TP10'), 'yaw 0: TP10 di sisi jauh tanpa label');
  const right = draw(at(Math.PI)).rec.texts.map((t) => t.t);
  assert.ok(right.includes('TP10'), 'yaw π: TP10 menghadap kamera');
  assert.ok(!right.includes('TP9'), 'yaw π: TP9 di sisi jauh tanpa label');
  for (const t of [...left, ...right]) assert.ok(SENSOR_NAMES.includes(t), `label tak dikenal: ${t}`);
});

test('riak mental command: tanpa pulse tidak ada cincin besar; dengan pulse ada cincin dan garis lebih terang', () => {
  const big = (rec) => rec.shapes.filter((p) => p.op === 'stroke' && p.r > 0.15 * W);
  const idle = draw(at(0.55), { pulse: 0 });
  assert.equal(big(idle.rec).length, 0, 'tanpa pulse tidak ada cincin');
  const fired = draw(at(0.55), { pulse: 0.6 });
  assert.equal(big(fired.rec).length, 1, 'satu cincin riak');
  assert.ok(big(fired.rec)[0].r < 0.5 * W, 'cincin tidak melebihi kanvas');
  assert.ok(ink(fired.rec) > ink(idle.rec) * 1.05, `garis lebih terang saat pulse: ${ink(fired.rec).toFixed(0)} vs ${ink(idle.rec).toFixed(0)}`);
});

test('redup saat tidak terhubung: dim < 1 menurunkan terang garis dan titik', () => {
  const full = draw(at(0.55), { dim: 1 });
  const dim = draw(at(0.55), { dim: 0.55 });
  assert.ok(maxAlpha(dim.rec) < maxAlpha(full.rec), 'garis lebih redup');
  const dots = (rec) => Math.max(...rec.shapes.filter((p) => p.op === 'fill' && p.style === COLORS.good).map((p) => p.alpha));
  assert.ok(dots(dim.rec) < dots(full.rec), 'titik lebih redup');
});
