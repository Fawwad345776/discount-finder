// Shared browser helpers: Firestore REST, Firebase Auth REST, time-zone maths, visibility rules.
(function () {
  const C = window.DF_CONFIG || {};
  const DEMO = !C.apiKey || !C.projectId;
  const BASE = `https://firestore.googleapis.com/v1/projects/${C.projectId}/databases/(default)/documents`;
  const NO_DATE_TTL = 48 * 3600 * 1000; // deals without an end date must be re-confirmed every 2 days

  // ---------- Firestore value encoding ----------
  function enc(v) {
    if (v === null || v === undefined) return { nullValue: null };
    if (v instanceof Date) return { timestampValue: v.toISOString() };
    if (Array.isArray(v)) return { arrayValue: { values: v.map(enc) } };
    if (typeof v === 'boolean') return { booleanValue: v };
    if (typeof v === 'number') return Number.isInteger(v) ? { integerValue: String(v) } : { doubleValue: v };
    if (typeof v === 'object') return { mapValue: { fields: Object.fromEntries(Object.entries(v).map(([k, x]) => [k, enc(x)])) } };
    return { stringValue: String(v) };
  }
  function dec(v) {
    if (!v) return null;
    if ('stringValue' in v) return v.stringValue;
    if ('integerValue' in v) return Number(v.integerValue);
    if ('doubleValue' in v) return v.doubleValue;
    if ('booleanValue' in v) return v.booleanValue;
    if ('timestampValue' in v) return new Date(v.timestampValue);
    if ('nullValue' in v) return null;
    if ('arrayValue' in v) return (v.arrayValue.values || []).map(dec);
    if ('mapValue' in v) return decFields(v.mapValue.fields || {});
    return null;
  }
  function decFields(f) { const o = {}; for (const k in f) o[k] = dec(f[k]); return o; }
  function decDoc(d) { const o = decFields(d.fields || {}); o.id = d.name.split('/').pop(); return o; }

  // ---------- Auth (admin only) ----------
  let idToken = null;
  try { idToken = sessionStorage.getItem('df-token'); } catch (e) {}
  async function signIn(email, password) {
    const r = await fetch(`https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${C.apiKey}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password, returnSecureToken: true }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error?.message || 'Login failed');
    idToken = j.idToken; try { sessionStorage.setItem('df-token', idToken); } catch (e) {}
    return j;
  }
  function signOut() { idToken = null; try { sessionStorage.removeItem('df-token'); } catch (e) {} }
  const authed = () => !!idToken;

  async function call(url, opt = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (idToken) headers.Authorization = 'Bearer ' + idToken;
    const r = await fetch(url, { ...opt, headers });
    const j = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) { if (r.status === 401) signOut(); throw new Error(j.error?.message || ('HTTP ' + r.status)); }
    return j;
  }
  const keyQ = () => (idToken ? '' : `?key=${C.apiKey}`);

  // where: [[field, op, value], ...]  op: EQUAL, IN, ...
  async function query(collection, where = [], limit = 500) {
    const filters = where.map(([field, op, value]) => ({ fieldFilter: { field: { fieldPath: field }, op, value: enc(value) } }));
    const structuredQuery = { from: [{ collectionId: collection }], limit };
    if (filters.length === 1) structuredQuery.where = filters[0];
    else if (filters.length > 1) structuredQuery.where = { compositeFilter: { op: 'AND', filters } };
    const res = await call(`${BASE}:runQuery${keyQ()}`, { method: 'POST', body: JSON.stringify({ structuredQuery }) });
    return res.filter(x => x.document).map(x => decDoc(x.document));
  }
  async function create(collection, data) {
    const d = await call(`${BASE}/${collection}`, { method: 'POST', body: JSON.stringify({ fields: enc(data).mapValue.fields }) });
    return decDoc(d);
  }
  async function update(collection, id, data) {
    const mask = Object.keys(data).map(k => 'updateMask.fieldPaths=' + encodeURIComponent(k)).join('&');
    const d = await call(`${BASE}/${collection}/${id}?${mask}`, { method: 'PATCH', body: JSON.stringify({ fields: enc(data).mapValue.fields }) });
    return decDoc(d);
  }
  async function remove(collection, id) { await call(`${BASE}/${collection}/${id}`, { method: 'DELETE' }); }

  // ---------- Time zones ----------
  // offset (ms) of tz at instant `date`
  function tzOffset(date, tz) {
    const p = new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      .formatToParts(date).reduce((a, x) => (a[x.type] = x.value, a), {});
    const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
    return asUTC - Math.floor(date.getTime() / 1000) * 1000;
  }
  // "2026-11-30T23:59" in "Asia/Kuwait" -> real Date
  function zonedToUtc(local, tz) {
    const [d, t = '00:00'] = local.split('T');
    const [y, m, day] = d.split('-').map(Number), [hh, mm] = t.split(':').map(Number);
    const guess = Date.UTC(y, m - 1, day, hh, mm);
    let ts = guess - tzOffset(new Date(guess), tz);
    ts = guess - tzOffset(new Date(ts), tz); // second pass handles DST edges
    return new Date(ts);
  }
  function fmtInTz(date, tz) {
    return new Intl.DateTimeFormat('en-GB', { timeZone: tz || 'UTC', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true }).format(date);
  }
  function timeLeft(date, now = new Date()) {
    let s = Math.max(0, (date - now) / 1000);
    const d = Math.floor(s / 86400); s -= d * 86400; const h = Math.floor(s / 3600); s -= h * 3600; const m = Math.floor(s / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }

  // A deal is shown only if: active, already started, and
  //  - it has an end date that has not passed (judged on the real instant, i.e. the city's own clock), or
  //  - it has no end date but was confirmed within the last 2 days.
  function isVisible(d, now = new Date()) {
    if (d.status !== 'active') return false;
    if (d.startsAt && d.startsAt > now) return false;
    if (d.expiresAt) return d.expiresAt > now;
    return !!d.lastChecked && (now - d.lastChecked) <= NO_DATE_TTL;
  }

  window.DF = { C, DEMO, query, create, update, remove, signIn, signOut, authed, zonedToUtc, fmtInTz, timeLeft, isVisible, NO_DATE_TTL };
})();
