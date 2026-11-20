// Minimal Firestore REST client using a service account (no npm packages).
import crypto from 'node:crypto';

export class Firestore {
  constructor(sa) {
    this.sa = typeof sa === 'string' ? JSON.parse(sa) : sa;
    this.base = `https://firestore.googleapis.com/v1/projects/${this.sa.project_id}/databases/(default)/documents`;
    this.token = null; this.exp = 0;
  }
  async auth() {
    if (this.token && Date.now() < this.exp - 60e3) return this.token;
    const now = Math.floor(Date.now() / 1000);
    const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64url');
    const unsigned = b64({ alg: 'RS256', typ: 'JWT' }) + '.' + b64({
      iss: this.sa.client_email, scope: 'https://www.googleapis.com/auth/datastore',
      aud: 'https://oauth2.googleapis.com/token', iat: now, exp: now + 3600 });
    const sig = crypto.createSign('RSA-SHA256').update(unsigned).sign(this.sa.private_key, 'base64url');
    const r = await fetch('https://oauth2.googleapis.com/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion: unsigned + '.' + sig }) });
    const j = await r.json();
    if (!r.ok) throw new Error('Google auth failed: ' + JSON.stringify(j));
    this.token = j.access_token; this.exp = Date.now() + j.expires_in * 1000;
    return this.token;
  }
  async call(url, opt = {}) {
    const r = await fetch(url, { ...opt, headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + await this.auth() } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(j.error?.message || 'HTTP ' + r.status); e.status = r.status; throw e; }
    return j;
  }
  async query(collection, where = [], limit = 2000) {
    const filters = where.map(([f, op, v]) => ({ fieldFilter: { field: { fieldPath: f }, op, value: enc(v) } }));
    const q = { from: [{ collectionId: collection }], limit };
    if (filters.length === 1) q.where = filters[0]; else if (filters.length) q.where = { compositeFilter: { op: 'AND', filters } };
    const res = await this.call(`${this.base}:runQuery`, { method: 'POST', body: JSON.stringify({ structuredQuery: q }) });
    return res.filter(x => x.document).map(x => decDoc(x.document));
  }
  // returns false if the id already exists (no duplicates)
  async createWithId(collection, id, data) {
    try { await this.call(`${this.base}/${collection}?documentId=${id}`, { method: 'POST', body: JSON.stringify({ fields: enc(data).mapValue.fields }) }); return true; }
    catch (e) { if (e.status === 409) return false; throw e; }
  }
  async remove(collection, id) { await this.call(`${this.base}/${collection}/${id}`, { method: 'DELETE' }); }
  async update(collection, id, data) {
    const mask = Object.keys(data).map(k => 'updateMask.fieldPaths=' + encodeURIComponent(k)).join('&');
    await this.call(`${this.base}/${collection}/${id}?${mask}`, { method: 'PATCH', body: JSON.stringify({ fields: enc(data).mapValue.fields }) });
  }
}

export function enc(v) {
  if (v === null || v === undefined) return { nullValue: null };
  if (v instanceof Date) return { timestampValue: v.toISOString() };
  if (Array.isArray(v)) return { arrayValue: { values: v.map(enc) } };
  if (typeof v === 'boolean') return { booleanValue: v };
  if (typeof v === 'number') return Number.isInteger(v) ? { integerValue: String(v) } : { doubleValue: v };
  if (typeof v === 'object') return { mapValue: { fields: Object.fromEntries(Object.entries(v).map(([k, x]) => [k, enc(x)])) } };
  return { stringValue: String(v) };
}
export function dec(v) {
  if (!v) return null;
  if ('stringValue' in v) return v.stringValue;
  if ('integerValue' in v) return Number(v.integerValue);
  if ('doubleValue' in v) return v.doubleValue;
  if ('booleanValue' in v) return v.booleanValue;
  if ('timestampValue' in v) return new Date(v.timestampValue);
  if ('nullValue' in v) return null;
  if ('arrayValue' in v) return (v.arrayValue.values || []).map(dec);
  if ('mapValue' in v) { const o = {}; for (const k in v.mapValue.fields || {}) o[k] = dec(v.mapValue.fields[k]); return o; }
  return null;
}
export function decDoc(d) { const o = dec({ mapValue: { fields: d.fields || {} } }); o.id = d.name.split('/').pop(); return o; }
