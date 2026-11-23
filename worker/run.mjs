// Runs every 6 hours (GitHub Actions):
//  1. hide deals whose end time has passed
//  2. re-check deals without an end date that are older than 2 days
//  3. look for NEW offers on every brand's website + Instagram
//
// Env: FIREBASE_SERVICE_ACCOUNT (json), GEMINI_API_KEY (optional), GEMINI_MODEL (optional),
//      IG_USER_ID + META_TOKEN (optional, for Instagram Business Discovery)
import fs from 'node:fs';
import { Firestore } from './firestore.mjs';
import { NO_DATE_TTL, IG_MAX_AGE, zonedToUtc, localNow, htmlToText, hash, dealId, regexExtract, stillOnPage, OFFER_RE } from './core.mjs';

const CATS = ['Fashion', 'Food', 'Electronics', 'Grocery', 'Beauty', 'Home', 'Travel', 'Other'];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36';

const cityCache = {};
function citiesOf(cc) {
  if (!cityCache[cc]) { try { cityCache[cc] = JSON.parse(fs.readFileSync(new URL(`../data/cities/${cc}.json`, import.meta.url))); } catch { cityCache[cc] = []; } }
  return cityCache[cc];
}
function tzFor(cc, city) {
  const list = citiesOf(cc);
  const hit = city && city !== 'ALL' && list.find(c => c[0].toLowerCase() === String(city).toLowerCase());
  return (hit || list[0] || [null, 'UTC'])[1];
}

async function getText(url, fetchFn) {
  const r = await fetchFn(url, { headers: { 'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en,ar;q=0.8' }, redirect: 'follow', signal: AbortSignal.timeout(20000) });
  if (!r.ok) return { ok: false, status: r.status, text: '' };
  return { ok: true, status: r.status, text: htmlToText(await r.text()).slice(0, 60000) };
}

async function igPosts(username, env, fetchFn) {
  if (!env.IG_USER_ID || !env.META_TOKEN || !username) return null;
  const fields = `business_discovery.username(${username}){media.limit(12){id,caption,permalink,media_url,thumbnail_url,timestamp}}`;
  const r = await fetchFn(`https://graph.facebook.com/v21.0/${env.IG_USER_ID}?fields=${encodeURIComponent(fields)}&access_token=${env.META_TOKEN}`, { signal: AbortSignal.timeout(30000) });
  const j = await r.json();
  if (!r.ok) throw new Error('Instagram: ' + (j.error?.message || r.status));
  return (j.business_discovery?.media?.data || []).map(m => ({ ...m, time: new Date(m.timestamp) }));
}

let aiModels = null;
async function pickModel(env, fetchFn, avoid) {
  const r = await fetchFn(`https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key=${env.GEMINI_API_KEY}`, { signal: AbortSignal.timeout(20000) });
  const j = await r.json();
  const ver = n => (n.match(/gemini-(\d+(?:\.\d+)?)/) || [0, 0])[1] * 1;
  const ok = (j.models || []).filter(m => (m.supportedGenerationMethods || []).includes('generateContent'))
    .map(m => m.name.replace('models/', ''))
    .filter(n => /^gemini-[\d.]+-flash(-lite)?$/.test(n) && n !== avoid)
    .sort((a, b) => ver(b) - ver(a) || a.length - b.length);
  return ok;
}
// returns { deals, via: 'ai' | 'regex' | 'failed' }
async function aiExtract(text, brand, tz, env, fetchFn) {
  if (!env.GEMINI_API_KEY) return { deals: regexExtract(text), via: 'regex' };
  const cities = citiesOf(brand.country).slice(0, 40).map(c => c[0]);
  const prompt = `You read promotional text from the brand "${brand.name}" (country code ${brand.country}).
Local date/time now: ${localNow(tz)} (${tz}). Brand serves cities: ${(brand.cities || ['ALL']).join(', ')}. Known cities: ${cities.join(', ')}.
Return ONLY real, current or upcoming discounts/sales/offers for customers. Ignore old/ended offers, job posts, generic marketing.
JSON: {"deals":[{"title":"short English title","discount":"e.g. 50% OFF / Buy 1 Get 1","description":"one short line or empty",
"category":"one of ${CATS.join('|')}","city":"a city name from the list or ALL","starts":"YYYY-MM-DDTHH:mm or null",
"ends":"YYYY-MM-DDTHH:mm or null (if only a date is given use 23:59)","confidence":0.0-1.0}]}
Text:
${text.slice(0, 15000)}`;
  const body = JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { responseMimeType: 'application/json', temperature: 0.1 } });
  if (!aiModels) aiModels = [env.GEMINI_MODEL || 'gemini-3.8-flash', ...(await pickModel(env, fetchFn, env.GEMINI_MODEL || 'gemini-3.8-flash').catch(() => []))].slice(0, 4);
  for (const model of aiModels) {
    for (let attempt = 0; attempt < 3; attempt++) {
      let r;
      try { r = await fetchFn(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${env.GEMINI_API_KEY}`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, signal: AbortSignal.timeout(90000) }); }
      catch (e) { console.warn(`  AI network (${model}): ${e.message}`); break; }
      const j = await r.json().catch(() => ({}));
      if (r.ok) {
        if (aiModels[0] !== model) aiModels = [model, ...aiModels.filter(m => m !== model)]; // remember the one that works
        try { return { deals: JSON.parse(j.candidates[0].content.parts[0].text).deals || [], via: 'ai' }; }
        catch { return { deals: [], via: 'ai' }; }
      }
      const msg = j.error?.message || String(r.status);
      if (r.status === 503 || r.status === 429 || /high demand|overloaded|rate/i.test(msg)) {
        console.warn(`  AI busy (${model}), retry ${attempt + 1}/3`);
        await new Promise(res => setTimeout(res, env.AI_WAIT_MS ? +env.AI_WAIT_MS : 8000 * (attempt + 1)));
        continue;
      }
      console.warn(`  AI error (${model}): ${msg}`); break; // model missing/other -> try next model
    }
  }
  return { deals: [], via: 'failed' }; // AI key set but AI failed: save nothing, retry next run
}

async function saveDeals(db, brand, res, source, now, stats) {
  for (const f of res.deals) {
    if (!f || !f.title) continue;
    const city = f.city && f.city !== 'null' ? f.city : 'ALL';
    const tz = tzFor(brand.country, city);
    const expiresAt = zonedToUtc(f.ends, tz), startsAt = zonedToUtc(f.starts, tz);
    if (expiresAt && expiresAt <= now) continue; // already over
    const deal = {
      brand: brand.name, brandId: brand.id, title: String(f.title).slice(0, 140), discount: f.discount || '', description: f.description || '',
      category: CATS.includes(f.category) ? f.category : (brand.category || 'Other'),
      country: brand.country, city, tz, startsAt, expiresAt,
      sourceType: source.type, sourceUrl: source.url, sourceId: source.id || null, image: source.image || '',
      status: (f.confidence ?? 0) >= 0.7 ? 'active' : 'pending', extractor: res.via, lastChecked: now, createdAt: now };
    const created = await db.createWithId('deals', dealId(brand.id, source.id || source.url, deal.title), deal);
    if (created) { stats.added++; console.log(`  + ${deal.status.toUpperCase()} ${deal.brand}: ${deal.title} (${deal.discount}) ${expiresAt ? 'ends ' + expiresAt.toISOString() : 'no end date'}`); }
  }
}

export async function main({ db, env = process.env, fetchFn = fetch, now = new Date() }) {
  aiModels = null;
  const stats = { expired: 0, rechecked: 0, hidden: 0, toReview: 0, added: 0, errors: 0 };
  let brands = (await db.query('brands', [], 5000));
  // Sync starter brand list: add brands from data/brands-seed.json that were never seeded before.
  // Seeded ids are remembered in meta/seed, so a brand deleted in admin is not added back.
  try {
    const seed = JSON.parse(fs.readFileSync(new URL('../data/brands-seed.json', import.meta.url)));
    const meta = (await db.query('meta', [], 50)).find(m => m.id === 'seed');
    const done = new Set(meta?.ids || (meta ? [] : brands.map(b => b.id)));
    let added = 0;
    for (const b of seed) {
      const id = hash(b.country + b.name).slice(0, 20);
      if (done.has(id)) continue;
      if (await db.createWithId('brands', id, { ...b, active: true, createdAt: now })) added++;
      done.add(id);
    }
    const ids = [...done];
    if (meta) await db.update('meta', 'seed', { ids, at: now }); else await db.createWithId('meta', 'seed', { ids, at: now });
    if (added) { console.log(`== Seeded ${added} new starter brands`); brands = await db.query('brands', [], 5000); }
  } catch (e) { console.warn('seed skipped:', e.message); }
  brands = brands.filter(b => b.active !== false);
  const brandById = Object.fromEntries(brands.map(b => [b.id, b]));
  const igCache = {};
  const ig = async (u) => (u in igCache ? igCache[u] : (igCache[u] = await igPosts(u, env, fetchFn).catch(e => (console.warn('  ' + e.message), null))));

  // 0: once AI is available, remove low-quality pending deals made by the regex fallback
  if (env.GEMINI_API_KEY) {
    for (const d of await db.query('deals', [['status', 'EQUAL', 'pending']], 5000))
      if (d.extractor !== 'ai' && d.sourceType !== 'manual') { await db.remove('deals', d.id); stats.cleaned = (stats.cleaned || 0) + 1; }
  }

  // 1 + 2: maintenance of live deals
  console.log('== Checking live deals');
  for (const d of await db.query('deals', [['status', 'EQUAL', 'active']], 10000)) {
    try {
      if (d.expiresAt) {
        if (d.expiresAt <= now) { await db.update('deals', d.id, { status: 'expired' }); stats.expired++; }
        continue;
      }
      if (d.lastChecked && now - d.lastChecked < NO_DATE_TTL) continue;
      stats.rechecked++;
      if (d.sourceType === 'website' && d.sourceUrl) {
        const page = await getText(d.sourceUrl, fetchFn);
        if (page.ok && stillOnPage(page.text, d)) await db.update('deals', d.id, { lastChecked: now });
        else { await db.update('deals', d.id, { status: 'ended' }); stats.hidden++; }
      } else if (d.sourceType === 'instagram') {
        const posts = await ig(brandById[d.brandId]?.instagram);
        const post = posts?.find(p => p.id === d.sourceId);
        if (post && now - post.time < IG_MAX_AGE) await db.update('deals', d.id, { lastChecked: now });
        else { await db.update('deals', d.id, { status: 'pending' }); stats.toReview++; }
      } else { await db.update('deals', d.id, { status: 'pending' }); stats.toReview++; } // manual deals -> admin confirms
    } catch (e) { stats.errors++; console.warn('  !', d.id, e.message); }
  }

  // 3: discover new offers
  // least recently scanned first, so with many brands every brand gets its turn across runs
  const lr = b => (b.lastRun ? new Date(b.lastRun).getTime() : 0) || 0;
  brands.sort((a, b) => lr(a) - lr(b));
  console.log(`== Scanning ${brands.length} brands`);
  const deadline = Date.now() + 20 * 60e3; // stay well inside the 30-min job limit
  for (const b of brands) {
    if (Date.now() > deadline) { console.log('== Time budget reached, rest next run'); break; }
    const tz = tzFor(b.country, (b.cities || [])[0]);
    const patch = { lastRun: now };
    try {
      if (b.offersUrl) {
        const page = await getText(b.offersUrl, fetchFn);
        const h = page.ok ? hash(page.text) : null;
        const want = env.GEMINI_API_KEY ? 'ai' : 'regex';
        if (page.ok && (h !== b.lastHash || b.lastExtractor !== want) && OFFER_RE.test(page.text)) {
          console.log(`- ${b.name}: checking website`);
          const res = await aiExtract(page.text, b, tz, env, fetchFn);
          await saveDeals(db, b, res, { type: 'website', url: b.offersUrl }, now, stats);
          if (res.via !== 'failed') { patch.lastHash = h; patch.lastExtractor = res.via; }
        } else if (h) patch.lastHash = h;
        if (!page.ok) console.log(`- ${b.name}: website returned ${page.status}`);
      }
      if (b.instagram) {
        const posts = await ig(b.instagram);
        if (posts) {
          const since = b.lastIgTime || new Date(now - 7 * 24 * 3600e3);
          for (const p of posts.filter(p => p.time > since && OFFER_RE.test(p.caption || ''))) {
            console.log(`- ${b.name}: new Instagram post`);
            await saveDeals(db, b, await aiExtract(`Posted ${p.timestamp}\n${p.caption}`, b, tz, env, fetchFn),
              { type: 'instagram', url: p.permalink, id: p.id, image: p.media_url || p.thumbnail_url || '' }, now, stats);
          }
          if (posts.length) patch.lastIgTime = new Date(Math.max(...posts.map(p => p.time)));
        }
      }
    } catch (e) { stats.errors++; console.warn(`  ! ${b.name}: ${e.message}`); }
    await db.update('brands', b.id, patch);
  }
  console.log('== Done', JSON.stringify(stats));
  return stats;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  if (!process.env.FIREBASE_SERVICE_ACCOUNT) { console.error('Missing FIREBASE_SERVICE_ACCOUNT secret'); process.exit(1); }
  main({ db: new Firestore(process.env.FIREBASE_SERVICE_ACCOUNT) }).catch(e => { console.error(e); process.exit(1); });
}
