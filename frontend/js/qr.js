// A small QR code generator (byte mode, error correction level L, versions 1 to 12: up to 367 bytes). No network, no dependency.
// Follows the QR standard (ISO/IEC 18004): Reed-Solomon over GF(256), the eight masks, and the penalty rules to pick one.
const ECC_PER_BLOCK = [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24];      // level L, by version
const NUM_BLOCKS = [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4];
const MAX_VERSION = 12;

const rawModules = (v) => {
  let r = (16 * v + 128) * v + 64;
  if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; }
  return r;
};
const dataCodewords = (v) => Math.floor(rawModules(v) / 8) - ECC_PER_BLOCK[v] * NUM_BLOCKS[v];
const bit = (x, i) => ((x >>> i) & 1) !== 0;

function gfMul(x, y) { let z = 0; for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; } return z; }
function rsDivisor(degree) {
  const r = new Array(degree).fill(0); r[degree - 1] = 1; let root = 1;
  for (let i = 0; i < degree; i++) { for (let j = 0; j < degree; j++) { r[j] = gfMul(r[j], root); if (j + 1 < degree) r[j] ^= r[j + 1]; } root = gfMul(root, 2); }
  return r;
}
function rsRemainder(data, divisor) {
  const r = new Array(divisor.length).fill(0);
  for (const b of data) { const f = b ^ r.shift(); r.push(0); divisor.forEach((c, i) => { r[i] ^= gfMul(c, f); }); }
  return r;
}

function alignPositions(v) {
  if (v === 1) return [];
  const n = Math.floor(v / 7) + 2, size = v * 4 + 17, step = Math.ceil((v * 4 + 4) / (n * 2 - 2)) * 2, out = [6];
  for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
  return out;
}

function addEcc(data, v) {
  const nb = NUM_BLOCKS[v], len = ECC_PER_BLOCK[v], raw = Math.floor(rawModules(v) / 8), shortCount = nb - (raw % nb), shortLen = Math.floor(raw / nb);
  const blocks = [], div = rsDivisor(len);
  for (let i = 0, k = 0; i < nb; i++) {
    const dl = shortLen - len + (i < shortCount ? 0 : 1), dat = data.slice(k, k + dl); k += dl;
    const ecc = rsRemainder(dat, div); if (i < shortCount) dat.push(0); blocks.push(dat.concat(ecc));
  }
  const out = [];
  for (let i = 0; i < blocks[0].length; i++) blocks.forEach((b, j) => { if (i !== shortLen - len || j >= shortCount) out.push(b[i]); });
  return out;
}

function penalty(m, size) {
  let p = 0;
  for (let pass = 0; pass < 2; pass++) {   // rows, then columns
    for (let a = 0; a < size; a++) {
      let run = 1, prev = pass ? m[0][a] : m[a][0];
      const seq = [];
      for (let b = 0; b < size; b++) {
        const c = pass ? m[b][a] : m[a][b]; seq.push(c);
        if (b > 0) { if (c === prev) { run++; if (run === 5) p += 3; else if (run > 5) p++; } else { run = 1; prev = c; } }
      }
      for (let b = 0; b + 10 < size; b++) {   // finder-like 1:1:3:1:1 with a light margin
        const s = seq.slice(b, b + 11).map((x) => (x ? 1 : 0)).join('');
        if (s.startsWith('10111010000') || s.endsWith('00001011101')) p += 40;
      }
    }
  }
  for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) { const c = m[y][x]; if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) p += 3; }
  let dark = 0; for (const row of m) for (const c of row) if (c) dark++;
  const total = size * size, k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
  return p + Math.max(0, k) * 10;
}

/** Returns { size, modules } where modules[y][x] is true for a dark square. Throws if the text does not fit. */
export function qrMatrix(text) {
  const bytes = Array.from(new TextEncoder().encode(text));
  let v = 1;
  for (; v <= MAX_VERSION; v++) { const cc = v < 10 ? 8 : 16; if (4 + cc + bytes.length * 8 <= dataCodewords(v) * 8) break; }
  if (v > MAX_VERSION) throw new Error('That text is too long for the QR code.');
  const bits = [];
  const put = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
  put(0b0100, 4); put(bytes.length, v < 10 ? 8 : 16); bytes.forEach((b) => put(b, 8));
  const cap = dataCodewords(v) * 8;
  put(0, Math.min(4, cap - bits.length)); while (bits.length % 8) bits.push(0);
  for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) put(pad, 8);
  const data = []; for (let i = 0; i < bits.length; i += 8) { let b = 0; for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j]; data.push(b); }
  const all = addEcc(data, v), size = v * 4 + 17;
  const m = Array.from({ length: size }, () => new Array(size).fill(false)), fn = Array.from({ length: size }, () => new Array(size).fill(false));
  const setFn = (x, y, dark) => { m[y][x] = dark; fn[y][x] = true; };
  for (let i = 0; i < size; i++) { setFn(6, i, i % 2 === 0); setFn(i, 6, i % 2 === 0); }
  const finder = (cx, cy) => { for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) { const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy; if (x >= 0 && x < size && y >= 0 && y < size) setFn(x, y, d !== 2 && d !== 4); } };
  finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
  const ap = alignPositions(v);
  ap.forEach((ax, i) => ap.forEach((ay, j) => { if ((i === 0 && j === 0) || (i === 0 && j === ap.length - 1) || (i === ap.length - 1 && j === 0)) return; for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setFn(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1); }));
  const drawFormat = (mask) => {
    const d0 = (1 << 3) | mask;   // level L = 01
    let rem = d0; for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const f = ((d0 << 10) | rem) ^ 0x5412;
    for (let i = 0; i <= 5; i++) setFn(8, i, bit(f, i)); setFn(8, 7, bit(f, 6)); setFn(8, 8, bit(f, 7)); setFn(7, 8, bit(f, 8)); for (let i = 9; i < 15; i++) setFn(14 - i, 8, bit(f, i));
    for (let i = 0; i < 8; i++) setFn(size - 1 - i, 8, bit(f, i)); for (let i = 8; i < 15; i++) setFn(8, size - 15 + i, bit(f, i));
    setFn(8, size - 8, true);
  };
  drawFormat(0);
  if (v >= 7) { let rem = v; for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25); const vb = (v << 12) | rem; for (let i = 0; i < 18; i++) { const c = bit(vb, i), a = size - 11 + (i % 3), b = Math.floor(i / 3); setFn(a, b, c); setFn(b, a, c); } }
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
      const x = right - j, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
      if (!fn[y][x] && i < all.length * 8) { m[y][x] = bit(all[i >>> 3], 7 - (i & 7)); i++; }
    }
  }
  const applyMask = (mask) => { for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    let inv; switch (mask) { case 0: inv = (x + y) % 2 === 0; break; case 1: inv = y % 2 === 0; break; case 2: inv = x % 3 === 0; break; case 3: inv = (x + y) % 3 === 0; break; case 4: inv = (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; break; case 5: inv = ((x * y) % 2) + ((x * y) % 3) === 0; break; case 6: inv = (((x * y) % 2) + ((x * y) % 3)) % 2 === 0; break; default: inv = (((x + y) % 2) + ((x * y) % 3)) % 2 === 0; }
    if (!fn[y][x] && inv) m[y][x] = !m[y][x]; } };
  let best = 0, bestP = Infinity;
  for (let mask = 0; mask < 8; mask++) { applyMask(mask); drawFormat(mask); const p = penalty(m, size); if (p < bestP) { bestP = p; best = mask; } applyMask(mask); }
  applyMask(best); drawFormat(best);
  return { size, modules: m, version: v };
}

/** An SVG string for the text. `border` is in modules (the standard asks for 4). */
export function qrSvg(text, { border = 4, dark = '#0b2b1c', light = '#ffffff' } = {}) {
  const { size, modules } = qrMatrix(text), n = size + border * 2, cells = [];
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (modules[y][x]) cells.push(`M${x + border},${y + border}h1v1h-1z`);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges" role="img" aria-label="QR code"><rect width="${n}" height="${n}" fill="${light}"/><path d="${cells.join('')}" fill="${dark}"/></svg>`;
}
