// Builds data/cities/<CC>.json from a GeoNames dump (cities5000.txt).
// Each file: [[cityName, timezone, population], ...] largest first.
// Usage: node scripts/build-cities.mjs path/to/cities5000.txt
import fs from 'node:fs';
import readline from 'node:readline';

const src = process.argv[2];
if (!src) { console.error('usage: build-cities.mjs <cities5000.txt>'); process.exit(1); }
const outDir = new URL('../data/cities/', import.meta.url);
fs.mkdirSync(outDir, { recursive: true });

const byCountry = new Map();
const rl = readline.createInterface({ input: fs.createReadStream(src), crlfDelay: Infinity });
for await (const line of rl) {
  const f = line.split('\t');
  if (f.length < 18) continue;
  const name = f[2] || f[1], cc = f[8], pop = Number(f[14]) || 0, tz = f[17];
  if (!name || !cc || !tz) continue;
  if (!byCountry.has(cc)) byCountry.set(cc, new Map());
  const m = byCountry.get(cc);
  const prev = m.get(name);
  if (!prev || prev[2] < pop) m.set(name, [name, tz, pop]);
}

let total = 0;
for (const [cc, m] of byCountry) {
  const list = [...m.values()].sort((a, b) => b[2] - a[2]);
  total += list.length;
  fs.writeFileSync(new URL(`${cc}.json`, outDir), JSON.stringify(list));
}
console.log(`countries: ${byCountry.size}, cities: ${total}`);
