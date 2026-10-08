// Tes data otak 3D (static/brain3d): data, decoder, dan proyeksi dibawa apa adanya dari TypeWave, jadi yang dikunci di sini
// adalah kontraknya — data utuh dan konsisten, dan otak muat di kotak desain di SEMUA sudut putar (kalau tidak, otak yang
// berputar akan terpotong di tepi kanvas).
//
// Jalankan dari root project:
//     node --test tests/js/*.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { BRAIN } from '../../static/brain3d/brain-data.js';
import { decodeBrain } from '../../static/brain3d/brain-decode.js';
import { PITCH, viewOf, perspK, projectInto } from '../../static/brain3d/orbit.js';

test('data otak terbuka utuh: jumlah titik cocok dan semua nilai hingga', () => {
  const D = decodeBrain(BRAIN);
  assert.ok(D.n > 5000, `titik: ${D.n}`);
  assert.equal(D.lines, BRAIN.kinds.length);
  assert.equal(D.len.reduce((s, v) => s + v, 0), D.n);
  for (const arr of [D.x, D.y, D.z, D.nx, D.ny, D.nz]) assert.ok(arr.every(Number.isFinite));
});

test('keempat jenis garis ada: lipatan 0, celah utama 1, otak kecil 2, batang otak 3', () => {
  assert.deepEqual([...new Set(BRAIN.kinds)].sort(), [0, 1, 2, 3]);
});

test('data yang tidak konsisten ditolak dengan pesan yang jelas', () => {
  assert.throws(() => decodeBrain({ ...BRAIN, nrm: BRAIN.nrm.slice(0, 8) }), /tidak konsisten/);
});

test('otak muat di kotak desain di setiap sudut putar', () => {
  const D = decodeBrain(BRAIN);
  const X = new Float64Array(D.n);
  const Y = new Float64Array(D.n);
  const Z = new Float64Array(D.n);
  const kp = perspK(BRAIN.unit);
  const tol = 0.01; // 1% dari sisi kotak
  for (let yaw = 0; yaw < 6.3; yaw += 0.5) {
    const v = viewOf(yaw, PITCH);
    for (let i = 0; i < D.n; i++) projectInto(v, kp, D.x[i], D.y[i], D.z[i], X, Y, Z, i);
    for (let i = 0; i < D.n; i++) {
      const px = BRAIN.w / 2 + X[i];
      const py = BRAIN.h / 2 - Y[i];
      assert.ok(px >= -tol * BRAIN.w && px <= BRAIN.w * (1 + tol), `x=${px.toFixed(1)} di luar kotak pada yaw ${yaw}`);
      assert.ok(py >= -tol * BRAIN.h && py <= BRAIN.h * (1 + tol), `y=${py.toFixed(1)} di luar kotak pada yaw ${yaw}`);
    }
  }
});
