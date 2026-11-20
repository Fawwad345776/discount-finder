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
const UA = 'Mozilla/5.0 (compatible; DiscountFinderBot/1.0; +https://fawwad345776.github.io/discount-finder/)';

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
  const r = await fetchFn(url, { headers: { 'User-Agent': UA, 'Accept-Language': 'en,ar;q=0.8' }, redirect: 'follow', signal: AbortSignal.timeout(20000) });
  if (!r.ok) return { ok: false, status: r.status, text: '' };
  return { ok: true, status: r.status, text: htmlToText(await r.text()).slice(0, 60000) };
}

async function igPosts(username, env, fetchFn) {
  if (!env.IG_USER_ID || !env.META_TOKEN || !username) return null;
  const fields = `business_discovery.username(${username}){media.limit(12){id,caption,permalink,media_url,thumbnail_url,timestamp}}`;
  const r = await fetchFn(`https://graph.facebook.com/v21.0/${env.IG_USER_ID}?fields=${encodeURIComponent(fields)}&access_token=${env.META_TOKEN}`);
  const j = await r.json();
  if (!r.ok) throw new Error('Instagram: ' + (j.error?.message || r.status));
  return (j.business_discovery?.media?.data || []).map(m => ({ ...m, time: new Date(m.timestamp) }));
}

async function aiExtract(text, brand, tz, env, fetchFn) {
  if (!env.GEMINI_API_KEY) return regexExtract(text);
  const cities = citiesOf(brand.country).slice(0, 40).map(c => c[0]);
  const prompt = `You read promotional text from the brand "${brand.name}" (country code ${brand.country}).
Local date/time now: ${localNow(tz)} (${tz}). Brand serves cities: ${(brand.cities || ['ALL']).join(', ')}. Known cities: ${cities.join(', ')}.
Return ONLY real, current or upcoming discounts/sales/offers for customers. Ignore old/ended offers, job posts, generic marketing.
JSON: {"deals":[{"title":"short English title","discount":"e.g. 50% OFF / Buy 1 Get 1","description":"one short line or empty",
"category":"one of ${CATS.join('|')}","city":"a city name from the list or ALL","starts":"YYYY-MM-DDTHH:mm or null",
"ends":"YYYY-MM-DDTHH:mm or null (if only a date is given use 23:59)","confidence":0.0-1.0}]}
Text:
${text.slice(0, 15000)}`;
  const model = env.GEMINI_MODEL || 'gemini-2.5-flash';
  const r = await fetchFn(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${env.GEMINI_API_KEY}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { responseMimeType: 'application/json', temperature: 0.1 } }) });
  const j = await r.json();
  if (!r.ok) { console.warn('  AI error, using fallback:', j.error?.message); return regexExtract(text); }
  try { return JSON.parse(j.candidates[0].content.parts[0].text).deals || []; } catch { return regexExtract(text); }
}

async function saveDeals(db, brand, found, source, now, stats) {
  for (const f of found) {
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
      status: (f.confidence ?? 0) >= 0.7 ? 'active' : 'pending', lastChecked: now, createdAt: now };
    const created = await db.createWithId('deals', dealId(brand.id, source.id || source.url, deal.title), deal);
    if (created) { stats.added++; console.log(`  + ${deal.status.toUpperCase()} ${deal.brand}: ${deal.title} (${deal.discount}) ${expiresAt ? 'ends ' + expiresAt.toISOString() : 'no end date'}`); }
  }
}

export async function main({ db, env = process.env, fetchFn = fetch, now = new Date() }) {
  const stats = { expired: 0, rechecked: 0, hidden: 0, toReview: 0, added: 0, errors: 0 };
  let brands = (await db.query('brands', [], 5000));
  if (!brands.length) { // first run: load the starter brand list
    try {
      const seed = JSON.parse(fs.readFileSync(new URL('../data/brands-seed.json', import.meta.url)));
      for (const b of seed) await db.createWithId('brands', hash(b.country + b.name).slice(0, 20), { ...b, active: true, createdAt: now });
      console.log(`== Seeded ${seed.length} starter brands`);
      brands = await db.query('brands', [], 5000);
    } catch (e) { console.warn('seed skipped:', e.message); }
  }
  brands = brands.filter(b => b.active !== false);
  const brandById = Object.fromEntries(brands.map(b => [b.id, b]));
  const igCache = {};
  const ig = async (u) => (u in igCache ? igCache[u] : (igCache[u] = await igPosts(u, env, fetchFn).catch(e => (console.warn('  ' + e.message), null))));

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
  console.log(`== Scanning ${brands.length} brands`);
  for (const b of brands) {
    const tz = tzFor(b.country, (b.cities || [])[0]);
    const patch = { lastRun: now };
    try {
      if (b.offersUrl) {
        const page = await getText(b.offersUrl, fetchFn);
        const h = page.ok ? hash(page.text) : null;
        if (page.ok && h !== b.lastHash && OFFER_RE.test(page.text)) {
          console.log(`- ${b.name}: website changed`);
          await saveDeals(db, b, await aiExtract(page.text, b, tz, env, fetchFn), { type: 'website', url: b.offersUrl }, now, stats);
        }
        if (h) patch.lastHash = h;
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
