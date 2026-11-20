// Pure helpers shared by the worker (no network) — unit-testable.
import crypto from 'node:crypto';

export const NO_DATE_TTL = 48 * 3600 * 1000;      // no end date -> re-confirm every 2 days
export const IG_MAX_AGE = 14 * 24 * 3600 * 1000;  // IG post older than this without end date -> human check

function tzOffset(date, tz) {
  const p = new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
    .formatToParts(date).reduce((a, x) => (a[x.type] = x.value, a), {});
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) - Math.floor(date.getTime() / 1000) * 1000;
}
// "2026-11-30T23:59" (local time in tz) -> Date
export function zonedToUtc(local, tz) {
  if (!local) return null;
  const m = String(local).match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!m) return null;
  const [, y, mo, d, hh = '23', mm = '59'] = m;
  const guess = Date.UTC(+y, +mo - 1, +d, +hh, +mm);
  let ts = guess - tzOffset(new Date(guess), tz);
  ts = guess - tzOffset(new Date(ts), tz);
  return new Date(ts);
}
export function localNow(tz, now = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(now).replace(' ', 'T');
}

export function isVisible(d, now = new Date()) {
  if (d.status !== 'active') return false;
  if (d.startsAt && d.startsAt > now) return false;
  if (d.expiresAt) return d.expiresAt > now;
  return !!d.lastChecked && (now - d.lastChecked) <= NO_DATE_TTL;
}

export const OFFER_RE = /(\d{1,2}\s?%\s?(off|discount)?|\bsale\b|\boffer\b|\bdiscount\b|\bbogo\b|buy\s?1\s?get\s?1|black\s?friday|clearance|promo|deal|خصم|تخفيض|تخفيضات|عرض|عروض|تنزيلات|سيل)/i;
export const PCT_RE = /(up\s?to\s?)?\d{1,2}\s?%(\s?off)?/i;

export function htmlToText(html) {
  return String(html)
    .replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>|<noscript[\s\S]*?<\/noscript>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"')
    .replace(/\s+/g, ' ').trim();
}
export const hash = s => crypto.createHash('sha1').update(String(s)).digest('hex');
export const dealId = (brandId, source, title) => hash(`${brandId}|${source}|${String(title).toLowerCase().trim()}`).slice(0, 24);

// Cheap fallback when no AI key: one "pending" deal for a human to confirm.
export function regexExtract(text) {
  if (!OFFER_RE.test(text)) return [];
  const sentence = (text.split(/(?<=[.!?\n])\s+/).find(s => OFFER_RE.test(s)) || text).slice(0, 120).trim();
  const pct = (text.match(PCT_RE) || [])[0];
  return [{ title: sentence, discount: pct ? pct.toUpperCase().replace(/\s+/g, ' ') : 'Offer', description: '', category: null, city: 'ALL', starts: null, ends: null, confidence: 0.3 }];
}

// Is the offer still on the page?
export function stillOnPage(text, deal) {
  const t = text.toLowerCase();
  const pct = String(deal.discount || '').match(/\d{1,2}\s?%/);
  if (pct && t.includes(pct[0].replace(/\s/g, '').toLowerCase())) return true;
  const words = String(deal.title || '').toLowerCase().split(/\W+/).filter(w => w.length > 3);
  const hits = words.filter(w => t.includes(w)).length;
  return OFFER_RE.test(text) && (words.length === 0 || hits / words.length >= 0.5);
}
