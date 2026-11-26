"""Discount Finder scraper (simple version).

Every run:
  1. Cleanup: deletes deals whose end time has passed, and deals with no end date
     that are older than 2 days (website deals: not seen for 2 days).
  2. For every brand: scrapes Instagram + Facebook (no login) and the brand's
     offers page, finds discount posts and saves them as live deals.

Run locally:   pip install requests cryptography
               set FIREBASE_SERVICE_ACCOUNT=<path to service-account.json or the JSON itself>
               python scraper/scrape.py
Optional env:  IG_USER_ID + META_TOKEN (official Instagram API, works from any server), GEMINI_API_KEY (better titles / end dates), MAX_MINUTES (default 45), ONLY_COUNTRY=PK
"""
import base64, hashlib, html, json, os, re, sys, time, random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)
TWO_DAYS = timedelta(days=2)
CATS = ['Fashion', 'Food', 'Electronics', 'Grocery', 'Beauty', 'Home', 'Travel', 'Other']
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
OFFER_RE = re.compile(r'(\d{1,2}\s?%\s?(off|discount)?|\bsale\b|\boffer\b|\bdiscount\b|\bbogo\b|buy\s?1\s?get\s?1|'
                      r'black\s?friday|clearance|\bpromo\b|\bdeal\b|خصم|تخفيض|تخفيضات|عرض|عروض|تنزيلات|سيل)', re.I)
PCT_RE = re.compile(r'(up\s?to\s?)?\d{1,2}\s?%(\s?off)?|buy\s?\d\s?get\s?\d|bogo|flat\s?\d+\s?%?\s?off', re.I)
log = lambda *a: print(*a, flush=True)


# ---------------- Firestore (REST, service account) ----------------
class DB:
    def __init__(self, sa):
        self.sa = json.loads(Path(sa).read_text() if not sa.strip().startswith('{') else sa)
        self.base = f"https://firestore.googleapis.com/v1/projects/{self.sa['project_id']}/databases/(default)/documents"
        self.token, self.exp, self.s = None, 0, requests.Session()

    def _auth(self):
        if self.token and time.time() < self.exp - 60:
            return self.token
        b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b'=').decode()
        now = int(time.time())
        unsigned = b64({'alg': 'RS256', 'typ': 'JWT'}) + '.' + b64({
            'iss': self.sa['client_email'], 'scope': 'https://www.googleapis.com/auth/datastore',
            'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600})
        key = serialization.load_pem_private_key(self.sa['private_key'].encode(), password=None)
        sig = base64.urlsafe_b64encode(key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())).rstrip(b'=').decode()
        r = requests.post('https://oauth2.googleapis.com/token', timeout=30, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': unsigned + '.' + sig})
        r.raise_for_status()
        self.token, self.exp = r.json()['access_token'], time.time() + r.json()['expires_in']
        return self.token

    def _call(self, method, url, body=None):
        r = self.s.request(method, url, json=body, timeout=30, headers={'Authorization': 'Bearer ' + self._auth()})
        if r.status_code == 409:
            return None
        if not r.ok:
            raise RuntimeError(f'Firestore {r.status_code}: {r.text[:200]}')
        return r.json() if r.text else {}

    def all(self, coll):
        out, token = [], ''
        while True:
            j = self._call('GET', f'{self.base}/{coll}?pageSize=300' + (f'&pageToken={token}' if token else ''))
            out += [dict(dec({'mapValue': {'fields': d.get('fields', {})}}), id=d['name'].split('/')[-1]) for d in j.get('documents', [])]
            token = j.get('nextPageToken')
            if not token:
                return out

    def create(self, coll, doc_id, data):  # False if it already exists
        return self._call('POST', f'{self.base}/{coll}?documentId={doc_id}', {'fields': enc(data)['mapValue']['fields']}) is not None

    def update(self, coll, doc_id, data, must_exist=False):
        mask = '&'.join('updateMask.fieldPaths=' + k for k in data) + ('&currentDocument.exists=true' if must_exist else '')
        self._call('PATCH', f'{self.base}/{coll}/{doc_id}?{mask}', {'fields': enc(data)['mapValue']['fields']})

    def delete(self, coll, doc_id):
        self._call('DELETE', f'{self.base}/{coll}/{doc_id}')


class CachedDB:
    """Keeps brands/deals/meta in a local file between runs, so Firebase is read in full only once a day.
    Every change is still written to Firebase straight away (and to the file)."""
    COLLS = ('brands', 'deals', 'meta')

    def __init__(self, db, path):
        self.db, self.path, self.data, synced = db, Path(path), {}, None
        try:
            raw = json.loads(self.path.read_text())
            self.data = _unjson(raw['data']); synced = datetime.fromisoformat(raw['synced'])
        except Exception:
            pass
        full = os.environ.get('FULL_SYNC') == '1' or not synced or NOW - synced > timedelta(hours=22) \
            or any(c not in self.data for c in self.COLLS)
        if full:
            self.data = {c: {d['id']: {k: v for k, v in d.items() if k != 'id'} for d in db.all(c)} for c in self.COLLS}
            self.synced = NOW
            log('== Full read from Firebase (once a day); saved to the run cache')
        else:
            self.synced = synced
            log(f'== Using run cache from {synced:%Y-%m-%d %H:%M} UTC (no Firebase reads for brands/deals)')

    def all(self, coll):
        if coll in self.data:
            return [dict(v, id=k) for k, v in self.data[coll].items()]
        return self.db.all(coll)

    def create(self, coll, doc_id, data):
        ok = self.db.create(coll, doc_id, data)
        if ok and coll in self.data:
            self.data[coll][doc_id] = dict(data)
        return ok

    def update(self, coll, doc_id, data):
        try:
            self.db.update(coll, doc_id, data, must_exist=True)
        except RuntimeError as e:
            if 'Firestore 404' in str(e):  # deleted in admin since the last full read
                if coll in self.data: self.data[coll].pop(doc_id, None)
                return
            raise
        if coll in self.data:
            self.data[coll].setdefault(doc_id, {}).update(data)

    def delete(self, coll, doc_id):
        self.db.delete(coll, doc_id)
        if coll in self.data:
            self.data[coll].pop(doc_id, None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'synced': self.synced.isoformat(), 'data': _tojson(self.data)}, separators=(',', ':')))


def _tojson(v):
    if isinstance(v, datetime): return {'$t': v.isoformat()}
    if isinstance(v, dict): return {k: _tojson(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [_tojson(x) for x in v]
    return v


def _unjson(v):
    if isinstance(v, dict):
        if set(v) == {'$t'}: return datetime.fromisoformat(v['$t'])
        return {k: _unjson(x) for k, x in v.items()}
    if isinstance(v, list): return [_unjson(x) for x in v]
    return v


def enc(v):
    if v is None: return {'nullValue': None}
    if isinstance(v, datetime): return {'timestampValue': v.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if isinstance(v, bool): return {'booleanValue': v}
    if isinstance(v, int): return {'integerValue': str(v)}
    if isinstance(v, float): return {'doubleValue': v}
    if isinstance(v, (list, tuple)): return {'arrayValue': {'values': [enc(x) for x in v]}}
    if isinstance(v, dict): return {'mapValue': {'fields': {k: enc(x) for k, x in v.items()}}}
    return {'stringValue': str(v)}


def dec(v):
    if 'timestampValue' in v: return datetime.fromisoformat(v['timestampValue'].replace('Z', '+00:00'))
    if 'integerValue' in v: return int(v['integerValue'])
    for k in ('stringValue', 'doubleValue', 'booleanValue'):
        if k in v: return v[k]
    if 'arrayValue' in v: return [dec(x) for x in v['arrayValue'].get('values', [])]
    if 'mapValue' in v: return {k: dec(x) for k, x in v['mapValue'].get('fields', {}).items()}
    return None


# ---------------- helpers ----------------
_tz_cache = {}
def country_tz(cc):
    if cc not in _tz_cache:
        try: _tz_cache[cc] = json.loads((ROOT / f'data/cities/{cc}.json').read_text())[0][1]
        except Exception: _tz_cache[cc] = 'UTC'
    return _tz_cache[cc]


def deal_id(*parts):
    return hashlib.sha1('|'.join(map(str, parts)).encode()).hexdigest()[:24]


def html_to_text(h):
    h = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', h)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', h))).strip()


# a real discount: "-30%", "30% off", "up to 50% off", "save 20%", "Buy 1 Get 1", "flat 500 off"
PCT_CTX = re.compile(r'(?:(?:up\s?to|upto|save|flat|extra|get)\s{0,3}(\d{1,2})\s?%(?![a-z0-9])'
                     r'|(?<![\d.,])-\s?(\d{1,2})\s?%(?![a-z0-9])'
                     r'|(?<![\d.,%])(\d{1,2})\s?%\s?(?:off|discount|sale|خصم|تخفيض))', re.I)
BOGO_RE = re.compile(r'buy\s?(\d)\s?get\s?(\d)(\s?free)?|\bbogo\b|1\s?\+\s?1\s?free', re.I)
FLAT_RE = re.compile(r'flat\s?(rs\.?|pkr|kwd|kd|aed|sar|qar|\$|₹|£|€)?\s?\d[\d,]*\s?(rs\.?|pkr|kwd|kd|aed|sar)?\s?off', re.I)
EVENT_RE = re.compile(r'black\s?friday|white\s?friday|yellow\s?friday|cyber\s?monday|singles.?\s?day|11\.11|12\.12|'
                      r'mega\s?sale|flash\s?sale|clearance(\s?sale)?|end\s?of\s?season\s?sale|eoss|year.?end\s?sale|'
                      r'(summer|winter|eid|ramadan|national\s?day|independence\s?day|new\s?year|christmas|festive)\s?sale', re.I)
MIN_PCT, MAX_PCT = 5, 90


def summarize(text):
    """Turns a page/caption into ONE clean offer, e.g. Black Friday Sale - UP TO 70% OFF. None if no real offer."""
    pcts = [int(next(g for g in m.groups() if g)) for m in PCT_CTX.finditer(text)]
    pcts = [x for x in pcts if MIN_PCT <= x <= MAX_PCT]
    bogo, flat = BOGO_RE.search(text), FLAT_RE.search(text)
    ev = EVENT_RE.search(text)
    event = re.sub(r'\s+', ' ', ev.group(0)).title() if ev else ''
    if event and not event.lower().endswith('sale') and re.search(r'friday|monday|day$|11\.11|12\.12', event, re.I):
        event += ' Sale'
    if pcts:
        top = max(pcts)
        disc = f'UP TO {top}% OFF' if len(set(pcts)) > 1 or re.search(r'up\s?to|upto', text, re.I) else f'{top}% OFF'
    elif bogo:
        disc = f'BUY {bogo.group(1) or 1} GET {bogo.group(2) or 1} FREE'
    elif flat:
        disc = re.sub(r'\s+', ' ', flat.group(0)).upper()
    elif event:
        disc = 'SALE'
    else:
        return None
    label = event or ('Offer' if bogo and not pcts else 'Sale')
    title = f'{label} - {disc.title().replace("Off", "OFF")}' if disc != 'SALE' else event
    desc = offer_line(text, pcts and max(pcts), bogo or flat or ev)
    if not desc and len(pcts) >= 3:  # product grid with -% badges: describe what is on sale
        lo, hi = min(pcts), max(pcts)
        desc = f"{len(pcts)}+ products on sale, {lo}% to {hi}% off" if lo != hi else f"{len(pcts)}+ products at {hi}% off"
    return {'title': title, 'discount': disc, 'description': desc, 'ends': find_end_date(text)}


MONTHS = {m: i + 1 for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}
END_WORDS = r'(?:till|until|untill|upto|up\s?to|ends?|ending|valid\s(?:till|until|through|thru)|offer\s(?:ends|valid\still)|last\sday|expires?|through|thru)'
MON = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?'
END_RES = [
    re.compile(END_WORDS + r'[:\s]+(?:on\s)?(?:[a-z]+day,?\s)?(\d{1,2})(?:st|nd|rd|th)?\s(?:of\s)?' + MON + r'(?:,?\s(\d{4}))?', re.I),   # till 30th Nov 2026
    re.compile(END_WORDS + r'[:\s]+(?:on\s)?(?:[a-z]+day,?\s)?' + MON + r'\s(\d{1,2})(?:st|nd|rd|th)?(?:,?\s(\d{4}))?', re.I),            # ends November 30, 2026
    re.compile(END_WORDS + r'[:\s]+(?:on\s)?(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?', re.I),                                      # till 30/11/2026
]


def find_end_date(text):
    """End date written on the page ("till 30 Nov", "ends November 30", "valid until 30/11"). ISO date or None."""
    today = NOW.date()
    for i, rx in enumerate(END_RES):
        for m in rx.finditer(text):
            try:
                if i == 0:
                    d, mon, y = int(m.group(1)), MONTHS[m.group(2)[:3].lower()], m.group(3)
                elif i == 1:
                    mon, d, y = MONTHS[m.group(1)[:3].lower()], int(m.group(2)), m.group(3)
                else:
                    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
                    d, mon = (a, b)                               # tried both ways below (30/11 and 11/30)
                year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
                options = [(d, mon)] if i < 2 else [(a, b), (b, a)]
                fits = set()
                for dd, mm in options:
                    try:
                        end = datetime(year, mm, dd).date()
                    except ValueError:
                        continue
                    if not y and end < today - timedelta(days=60):
                        end = end.replace(year=year + 1)          # "till 5 Jan" seen in December
                    # a recently passed date is kept: the deal is then skipped as expired
                    if today - timedelta(days=60) <= end <= today + timedelta(days=120):
                        fits.add(end)
                if len(fits) == 1:
                    return fits.pop().isoformat()
            except (ValueError, KeyError):
                continue
    return None


PRICE_LINE = re.compile(r'(regular|sale|unit|original)\s?price|add to (basket|cart|bag)|view all|out of stock|'
                        r'(rs\.?|pkr|aed|sar|kwd|qar|usd|inr|₹|\$|£|€)\s?\d[\d,]*(\.\d+)?|\d[\d,]*(\.\d+)?\s?(rs|pkr|aed|sar|kwd|qar)\b', re.I)


def offer_line(text, top_pct, other_match):
    """The sentence on the page that explains the offer, e.g. 'Get 15% off on domestic flights with HBL cards'."""
    parts = [p.strip(' -|•·') for p in re.split(r'(?<=[.!?])\s+|\s{2,}|[|•·\n]', text)]
    if top_pct:
        hit = re.compile(rf'(?<!\d){top_pct}\s?%')
    elif other_match:
        hit = re.compile(re.escape(other_match.group(0)), re.I)
    else:
        return ''
    good = [p for p in parts if hit.search(p) and 15 <= len(p) <= 180 and len(re.findall(r'[A-Za-z]{3,}', p)) >= 3
            and not PRICE_LINE.search(p) and p.count('%') <= 2]  # product cards ("Sale price PKR 3,534 Regular price") are not offer lines
    if not good:
        return ''
    best = re.sub(r'\s+', ' ', min(good, key=lambda p: abs(len(p) - 80)))  # a full but short sentence
    # start at the offer itself ("Get 15% off ..."), dropping menu/button words before it
    m = hit.search(best)
    starts = [x.start() for x in re.finditer(r'\b(get|enjoy|save|flat|avail|buy|up\s?to|upto|extra|shop|order|use code|sale)\b', best, re.I)
              if x.start() <= m.start() and m.start() - x.start() <= 60]
    if starts:
        best = best[starts[-1] if best[starts[-1]:m.start()].strip() == '' else starts[0]:]
    elif m.start() > 60:
        best = best[m.start() - 40:].split(' ', 1)[-1]
    return best[:1].upper() + best[1:160]


def find_offers(text):
    one = summarize(text)
    return [one] if one else []


def ai_extract(text, brand):
    """Gemini (optional): returns [{'title','discount','description','ends'}]; None if no key / failed."""
    key = os.environ.get('GEMINI_API_KEY')
    if not key or AI_DOWN[0]:
        return None
    prompt = (f"Today is {NOW.date()}. From this {brand['name']} ({brand['country']}) text, list the brand's CURRENT sale campaigns/offers "
              '(max 3), e.g. "Black Friday Sale up to 70% off", "Buy 1 Get 1 on Zinger". Do NOT list single products with prices, '
              'policies, cashback terms or menus. JSON: {"deals":[{"title":"short English title","discount":"e.g. UP TO 30% OFF",'
              '"description":"one short line","ends":"YYYY-MM-DD or null"}]}. Empty list if none.\nText:\n' + text[:12000])
    for model in [os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')]:
        try:
            r = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}', timeout=60,
                              json={'contents': [{'parts': [{'text': prompt}]}],
                                    'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.1}})
            if r.ok:
                return json.loads(r.json()['candidates'][0]['content']['parts'][0]['text']).get('deals', [])
            if r.status_code == 429:
                AI_DOWN[0] = True
                log('== Gemini quota used up, deals use plain text for the rest of this run')
        except Exception as e:
            log('   AI error', model, e)
    return None


AI_DOWN = [False]


def extract(text, brand):
    res = ai_extract(text, brand)
    if res is None:
        return find_offers(text)
    # keep only AI deals that carry a real discount
    return [f for f in res if f and f.get('title') and summarize(f"{f.get('discount', '')} {f.get('title', '')}")][:3]


def offer_key(brand_id, discount):
    """Same brand + same discount = same offer (from website, Instagram or Facebook)."""
    return brand_id + '|' + re.sub(r'[^A-Z0-9]', '', str(discount).upper().replace('UPTO', 'UP TO').replace(' ', ''))


def parse_end(ends, tz):
    if not ends or ends == 'null':
        return None
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(str(ends)[:10])
        return d.replace(hour=23, minute=59, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    except Exception:
        return None


# ---------------- scrapers (no login) ----------------
S = requests.Session()
S.headers.update({'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})


GRAPH = 'https://graph.facebook.com/v25.0'


class IGBlocked(RuntimeError):
    pass


def scrape_instagram(username):
    """Official Instagram API (Business Discovery) when IG_USER_ID + META_TOKEN are set, else public no-login scrape."""
    uid, token = os.environ.get('IG_USER_ID'), os.environ.get('META_TOKEN')
    if not (uid and token):
        return scrape_instagram_public(username)
    fields = f'business_discovery.username({username}){{media.limit(12){{id,caption,permalink,media_url,thumbnail_url,timestamp}}}}'
    r = requests.get(f'{GRAPH}/{uid}', params={'fields': fields, 'access_token': token}, timeout=30)
    j = r.json() if r.text else {}
    if 'error' in j:
        err = j['error']
        msg = f"Instagram API: {err.get('message', '')[:120]} (code {err.get('code')})"
        if err.get('code') in (4, 17, 32, 190, 613) or err.get('type') == 'OAuthException' and err.get('code') == 190:
            raise IGBlocked(msg)  # rate limit or bad/expired token -> stop for this run
        raise RuntimeError(msg)   # e.g. that brand is not a business account / wrong username
    out = []
    for m in j.get('business_discovery', {}).get('media', {}).get('data', []):
        out.append({'id': m['id'], 'text': m.get('caption', ''), 'url': m.get('permalink', ''),
                    'image': m.get('media_url') or m.get('thumbnail_url') or '',
                    'time': datetime.fromisoformat(m['timestamp'].replace('+0000', '+00:00'))})
    return out


def scrape_instagram_public(username):
    """Public profile, last ~12 posts: [{'id','text','url','image','time'}]"""
    r = S.get(f'https://i.instagram.com/api/v1/users/web_profile_info/?username={username}', timeout=20,
              headers={'x-ig-app-id': '936619743392459', 'Referer': 'https://www.instagram.com/'})
    if r.status_code != 200:
        raise RuntimeError(f'Instagram blocked ({r.status_code})')
    edges = r.json()['data']['user']['edge_owner_to_timeline_media']['edges']
    posts = []
    for e in edges:
        n = e['node']
        cap = (n.get('edge_media_to_caption', {}).get('edges') or [{}])[0].get('node', {}).get('text', '')
        posts.append({'id': n['id'], 'text': cap, 'url': f"https://www.instagram.com/p/{n['shortcode']}/",
                      'image': n.get('display_url', ''), 'time': datetime.fromtimestamp(n['taken_at_timestamp'], timezone.utc)})
    return posts


def scrape_facebook(page):
    """Public page via mbasic (works only while Facebook allows logged-out views)."""
    r = S.get(f'https://mbasic.facebook.com/{page}', timeout=20, allow_redirects=True)
    if r.status_code != 200 or 'login' in r.url:
        raise RuntimeError('Facebook asks for login')
    posts = []
    for block in re.findall(r'(?s)<article.*?</article>|<div[^>]+data-ft=.*?</footer>', r.text)[:10]:
        text = html_to_text(block)
        link = re.search(r'href="(/story\.php\?[^"]+|/[^"/]+/posts/[^"]+)"', block)
        url = 'https://www.facebook.com' + html.unescape(link.group(1)) if link else f'https://www.facebook.com/{page}'
        posts.append({'id': deal_id(page, text[:80]), 'text': text, 'url': url, 'image': '', 'time': None})
    if not posts:
        raise RuntimeError('Facebook: no public posts visible')
    return posts


def scrape_website(url):
    r = S.get(url, timeout=25)
    r.raise_for_status()
    img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r.text)
    return html_to_text(r.text), (img.group(1) if img else ''), find_instagram(r.text), find_offers_page(r.text, r.url)


OFFER_LINK = re.compile(r'offer|deal|promo|discount|sale|coupon|voucher|black-?friday|عروض|خصم', re.I)


def find_offers_page(page_html, base):
    """Link to the site's own offers/deals page (same website), so 'View offer' opens the right page."""
    from urllib.parse import urljoin, urlparse
    host = urlparse(base).netloc.replace('www.', '')
    best = None
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', page_html, re.I | re.S):
        href, label = m.group(1), re.sub(r'<[^>]+>', ' ', m.group(2))
        url = urljoin(base, href)
        u = urlparse(url)
        if u.scheme not in ('http', 'https') or u.netloc.replace('www.', '') != host or u.path in ('', '/'):
            continue
        path_hit, label_hit = OFFER_LINK.search(u.path), OFFER_LINK.search(label)
        if path_hit or (label_hit and len(label.strip()) < 40):
            score = (2 if path_hit else 0) + (1 if label_hit else 0) - u.path.count('/') * 0.1
            if not best or score > best[0]:
                best = (score, url)
    return best[1] if best else ''


IG_SKIP = {'p', 'reel', 'reels', 'explore', 'accounts', 'stories', 'tv', 'about', 'developer', 'legal', 'direct', 'instagram'}


def find_instagram(page_html):
    """Brand's own Instagram username from the links on its website (footer icons etc.)."""
    for m in re.finditer(r'instagram\.com/([A-Za-z0-9_.]{2,30})', page_html):
        u = m.group(1).strip('.').lower()
        if u not in IG_SKIP:
            return u
    return ''


# ---------------- main ----------------
VERSION = 2  # deals made by older scraper versions are removed once (they had duplicates and junk)
SEEN = {}    # offer_key -> deal id, filled by cleanup()


def save(db, brand, items, source_type, source_url, source_id, image, posted, stats):
    tz = country_tz(brand['country'])
    for f in items or []:
        if not f or not f.get('title'):
            continue
        expires = parse_end(f.get('ends'), tz)
        if expires and expires <= NOW:
            continue
        if source_type == 'website':
            # one website deal per brand: if the % changes on the site, the same deal is updated (no doubling)
            key = brand['id'] + '|web'
            if key in SEEN:
                touch(db, SEEN[key], {'title': str(f['title'])[:140], 'discount': f.get('discount') or '', 'expiresAt': expires,
                                      'description': f.get('description') or '', 'sourceUrl': source_url})
                continue
        else:
            key = offer_key(brand['id'], f.get('discount') or f['title'])
        if key in SEEN:  # this brand already has this offer -> no doubling, just keep it alive
            touch(db, SEEN[key])
            continue
        did = deal_id(key)
        deal = {'brand': brand['name'], 'brandId': brand['id'], 'title': str(f['title'])[:140],
                'discount': f.get('discount') or '', 'description': f.get('description') or '',
                'category': brand.get('category') if brand.get('category') in CATS else 'Other',
                'country': brand['country'], 'city': (brand.get('cities') or ['ALL'])[0], 'tz': tz,
                'startsAt': None, 'expiresAt': expires, 'sourceType': source_type, 'sourceUrl': source_url,
                'sourceId': source_id, 'image': image or '', 'status': 'active', 'extractor': 'python', 'v': VERSION,
                'lastChecked': NOW, 'createdAt': posted or NOW}
        if db.create('deals', did, deal):
            stats['added'] += 1
            log(f"   + {brand['name']}: {deal['title'][:70]}")
        else:
            touch(db, did)
        SEEN[key] = did
        SEEN_INFO.setdefault(did, (NOW, deal['title'], deal['discount'], expires, deal['description'], deal['sourceUrl']))


SEEN_INFO = {}             # deal id -> (lastChecked, title, discount, expiresAt)
REFRESH = timedelta(hours=12)  # still-running deals are re-confirmed at most every 12h (saves Firebase writes)


def touch(db, did, fields=None):
    """Update an existing deal only if something changed or it was not re-confirmed for 12h."""
    seen, *old = SEEN_INFO.get(did, (None, None, None, None, None, None))
    keys = ('title', 'discount', 'expiresAt', 'description', 'sourceUrl')
    changed = fields and any(k in fields and fields[k] != o for k, o in zip(keys, old))
    if not changed and seen and NOW - seen < REFRESH:
        return
    db.update('deals', did, dict(fields or {}, lastChecked=NOW))
    SEEN_INFO[did] = (NOW, *[(fields or {}).get(k, o) for k, o in zip(keys, old)])


def cleanup(db, stats):
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    deals = sorted(db.all('deals'), key=lambda d: d.get('lastChecked') or epoch, reverse=True)  # newest first
    for d in sorted(deals, key=lambda d: d.get('expiresAt') is None):  # deals with an end date win
        manual = d.get('sourceType') == 'manual'
        if manual and not d.get('expiresAt'):
            continue  # your own deals without end date stay until you delete them
        exp, seen, made = d.get('expiresAt'), d.get('lastChecked'), d.get('createdAt')
        old = (exp and exp <= NOW) or (not exp and (
            (d.get('sourceType') == 'website' and (not seen or NOW - seen > TWO_DAYS)) or
            (d.get('sourceType') != 'website' and (not made or NOW - made > TWO_DAYS))))
        junk = not manual and d.get('v') != VERSION
        if d.get('sourceType') == 'website':
            key = d.get('brandId', '') + '|web'
        else:
            key = offer_key(d.get('brandId', ''), d.get('discount') or d.get('title', ''))
        dup = not manual and key in SEEN
        if old or junk or dup:
            db.delete('deals', d['id'])
            stats['deleted'] += 1
        elif not manual:
            SEEN[key] = d['id']
            SEEN_INFO[d['id']] = (seen, d.get('title'), d.get('discount'), exp, d.get('description'), d.get('sourceUrl'))


def sync_seed(db):
    """Add brands from data/brands-seed.json that were never added before (deleted ones stay deleted)."""
    try:
        seed = json.loads((ROOT / 'data/brands-seed.json').read_text())
        meta = next((m for m in db.all('meta') if m['id'] == 'seed'), None)
        done = set(meta.get('ids') or []) if meta else {b['id'] for b in db.all('brands')}
        added = 0
        for b in seed:
            bid = hashlib.sha1((b['country'] + b['name']).encode()).hexdigest()[:20]
            if bid not in done:
                added += db.create('brands', bid, dict(b, active=True, createdAt=NOW))
                done.add(bid)
        if meta: db.update('meta', 'seed', {'ids': sorted(done), 'at': NOW})
        else: db.create('meta', 'seed', {'ids': sorted(done), 'at': NOW})
        if added: log(f'== Added {added} new brands from brands-seed.json')
    except Exception as e:
        log('seed skipped:', e)


LIVE_FIELDS = ['brand', 'title', 'discount', 'description', 'category', 'country', 'city', 'tz', 'startsAt', 'expiresAt',
               'sourceType', 'sourceUrl', 'image', 'status', 'lastChecked']


def is_visible(d):
    if d.get('status') != 'active': return False
    if d.get('startsAt') and d['startsAt'] > NOW: return False
    if d.get('expiresAt'): return d['expiresAt'] > NOW
    return bool(d.get('lastChecked')) and NOW - d['lastChecked'] <= TWO_DAYS


def export_live(db):
    """Writes data/live/<CC>.json (live deals per country) + index.json, so the website reads files, not Firebase."""
    out = ROOT / 'data' / 'live'
    out.mkdir(parents=True, exist_ok=True)
    by = {}
    for d in db.all('deals'):
        if d.get('sourceType') == 'manual' or not is_visible(d):
            continue  # manual deals are read live from Firebase by the site (so they show instantly)
        row = {'id': d['id']}
        for k in LIVE_FIELDS:
            v = d.get(k)
            row[k] = v.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if isinstance(v, datetime) else v
        by.setdefault(d.get('country') or 'XX', []).append(row)
    for f in out.glob('*.json'):
        if f.stem != 'index' and f.stem not in by:
            f.unlink()
    for cc, rows in by.items():
        (out / f'{cc}.json').write_text(json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    (out / 'index.json').write_text(json.dumps({'updatedAt': NOW.isoformat().replace('+00:00', 'Z'),
                                                'counts': {cc: len(r) for cc, r in sorted(by.items())}}, indent=1))
    log(f'== Exported {sum(map(len, by.values()))} live deals for {len(by)} countries to data/live/')


AI_FAILS = {}


def gemini_json(prompt, timeout=90):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        return None
    for model in [os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')]:
        try:
            r = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}', timeout=timeout,
                              json={'contents': [{'parts': [{'text': prompt}]}],
                                    'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2}})
            if r.ok:
                return json.loads(r.json()['candidates'][0]['content']['parts'][0]['text'])
            AI_FAILS[f'{model} {r.status_code}'] = AI_FAILS.get(f'{model} {r.status_code}', 0) + 1
            if AI_FAILS[f'{model} {r.status_code}'] <= 2:
                log('   AI failed', model, r.status_code, r.text[:200].replace('\n', ' '))
        except Exception as e:
            log('   AI error', model, e)
    return None


def domain(url):
    return re.sub(r'^www\.', '', (url or '').split('//')[-1].split('/')[0].lower())


FOCUS = [c for c in os.environ.get('FOCUS_COUNTRIES', 'PK,IN,US,TR,KW').split(',') if c]  # main markets
FOCUS_TARGET = int(os.environ.get('FOCUS_TARGET', 200))  # brands per main market before others get most of the time
NEXT_WAVE = ['AE', 'SA', 'QA', 'GB', 'BD', 'OM', 'BH', 'EG', 'JO', 'ID', 'MY', 'PH', 'NG', 'KE', 'ZA', 'CA', 'AU', 'DE', 'BR', 'MX']


def discovery_order(countries, count, last, dry, per_run):
    """Which countries the AI searches this run.
    1) Main markets below FOCUS_TARGET brands get slots first (fewest brands first); a slot freed by a
       finished main market goes straight to the next country (UAE, Saudi, ...).
    2) After that: 1 slot per run stays with the main markets (each gets a turn about twice a day),
       the other slots go to the next wave, then the rest of the world (oldest-checked first).
    A country where the AI found nothing new rests for 24h (no wasted free AI quota)."""
    day_ago = (NOW - timedelta(hours=24)).isoformat()
    rested = lambda cc: dry.get(cc, '') < day_ago
    by_code = {c['code']: c for c in countries}
    focus = [cc for cc in FOCUS if cc in by_code and rested(cc)]
    short = sorted([cc for cc in focus if count.get(cc, 0) < FOCUS_TARGET], key=lambda cc: count.get(cc, 0))
    pick = short[:per_run]
    if len(pick) < per_run:
        done_focus = sorted([cc for cc in focus if cc not in pick], key=lambda cc: last.get(cc, ''))
        if not short and done_focus:
            pick.append(done_focus[0])  # main markets keep getting new brands
        wave = {cc: i for i, cc in enumerate(NEXT_WAVE)}
        others = [c['code'] for c in countries if c['code'] not in FOCUS and rested(c['code'])]
        others.sort(key=lambda cc: (last.get(cc, ''), wave.get(cc, 999), -count.get(cc, 0)))
        pick += others[:per_run - len(pick)]
    return [by_code[cc] for cc in pick]


def discover_brands(db, brands):
    """Finds new popular brands for a few countries per run (AI suggests, website must really open), adds them."""
    per_run = int(os.environ.get('DISCOVER_COUNTRIES', 5))
    if per_run <= 0 or not os.environ.get('GEMINI_API_KEY'):
        return []
    countries = json.loads((ROOT / 'data/countries.json').read_text())
    meta = next((m for m in db.all('meta') if m['id'] == 'discover'), None)
    last = (meta or {}).get('last') or {}
    dry = (meta or {}).get('dry') or {}
    count = {}
    for b in brands:
        if b.get('active') is not False:
            count[b.get('country')] = count.get(b.get('country'), 0) + 1
    order = discovery_order(countries, count, last, dry, per_run)
    log('== Brand search this run: ' + ', '.join(f"{c['code']}({count.get(c['code'], 0)})" for c in order))
    added = []
    from concurrent.futures import ThreadPoolExecutor
    for c in order:
        cc = c['code']
        mine = [b for b in brands if b.get('country') == cc]
        names = {b['name'].lower() for b in mine}
        doms = {domain(b.get('offersUrl')) for b in mine}
        res = gemini_json(
            f"List up to 25 popular brands in {c['name']} ({cc}) whose discounts/sales people look for: restaurants & food chains, "
            'food delivery apps, fashion, supermarkets, electronics, beauty, home, travel. Only real brands that operate in this country, '
            'with their OFFICIAL website for this country. Skip these, already have them: ' + ', '.join(sorted(names))[:8000] + '. '
            'JSON: {"brands":[{"name":"","category":"Food|Fashion|Grocery|Electronics|Beauty|Home|Travel|Other",'
            '"website":"https://...","instagram":"username or empty"}]}')
        if res is None:  # AI unavailable (e.g. daily quota used up) -> try this country again next run
            log(f"== Discover {cc}: AI not available, will retry next run")
            if AI_FAILS and any(k.endswith(' 429') for k in AI_FAILS):
                break  # quota is over for today, no point asking for more countries
            continue
        last[cc] = NOW.isoformat()
        cand = []
        for x in (res or {}).get('brands', [])[:25]:
            n, w = str(x.get('name', '')).strip(), str(x.get('website', '')).strip()
            if not n or not w.startswith('http') or n.lower() in names or domain(w) in doms:
                continue
            names.add(n.lower()); doms.add(domain(w))
            cand.append({'country': cc, 'name': n[:60], 'category': x.get('category') if x.get('category') in CATS else 'Other',
                         'offersUrl': w, 'instagram': re.sub(r'[^A-Za-z0-9_.]', '', str(x.get('instagram') or ''))[:30].lower(),
                         'cities': ['ALL']})

        def ok(b):  # the website must really open (filters made-up brands)
            try:
                r = S.get(b['offersUrl'], timeout=20)
                return r.status_code < 400 or r.status_code in (403, 429)  # 403/429 = site exists but blocks bots
            except Exception:
                return False
        with ThreadPoolExecutor(10) as pool:
            good = [b for b, g in zip(cand, pool.map(ok, cand)) if g]
        for b in good:
            bid = hashlib.sha1((b['country'] + b['name']).encode()).hexdigest()[:20]
            if db.create('brands', bid, dict(b, active=True, source='auto', createdAt=NOW)):
                added.append(dict(b, id=bid))
        if good:
            dry.pop(cc, None)
        else:
            dry[cc] = NOW.isoformat()  # nothing new -> rest this country for 24h
        count[cc] = count.get(cc, 0) + len(good)
        log(f"== Discover {cc}: AI suggested {len(cand)}, website OK {len(good)}, added {len(good)}")
    if meta: db.update('meta', 'discover', {'last': last, 'dry': dry})
    else: db.create('meta', 'discover', {'last': last, 'dry': dry})
    return added


def main():
    sa = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if not sa and (Path(__file__).parent / 'service-account.json').exists():
        sa = str(Path(__file__).parent / 'service-account.json')
    if not sa:
        sys.exit('Set FIREBASE_SERVICE_ACCOUNT (path to the JSON file or the JSON text)')
    db = DB(sa)
    if os.environ.get('STATE_FILE'):
        db = CachedDB(db, os.environ['STATE_FILE'])
    stats = {'added': 0, 'deleted': 0, 'errors': 0}
    cleanup(db, stats)
    log(f"== Deleted {stats['deleted']} old/expired deals")

    sync_seed(db)
    brands, same = [], {}
    for b in sorted(db.all('brands'), key=lambda b: b.get('createdAt') or NOW):
        if b.get('active') is False:
            continue
        k = (b.get('country'), (b.get('offersUrl') or '').rstrip('/').lower() or (b.get('instagram') or '').lower() or b['name'].lower())
        if k in same:  # same brand added twice -> turn the copy off so its offers are not doubled
            db.update('brands', b['id'], {'active': False})
            log(f"== Duplicate brand turned off: {b['name']} (same as {same[k]})")
            continue
        same[k] = b['name']
        brands.append(b)
    try:
        new = discover_brands(db, brands)
        brands += new
        if new: log(f'== Discovered {len(new)} new brands')
    except Exception as e:
        log('discover skipped:', e)
    if os.environ.get('ONLY_COUNTRY'):
        brands = [b for b in brands if b.get('country') == os.environ['ONLY_COUNTRY']]
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    brands.sort(key=lambda b: b.get('lastRun') or epoch)  # least recently scanned first
    deadline = time.time() + 60 * float(os.environ.get('MAX_MINUTES', 45))
    log(f'== Scanning {len(brands)} brands')

    # websites: fetch 10 at a time (much faster than one by one)
    from concurrent.futures import ThreadPoolExecutor
    def fetch(b):
        try: return b['id'], scrape_website(b['offersUrl']), None
        except Exception as e: return b['id'], None, e
    only_social = os.environ.get('ONLY_SOCIAL') == '1'  # laptop: Instagram/Facebook only, GitHub does websites
    with ThreadPoolExecutor(10) as pool:
        pages = {} if only_social else {i: (r, e) for i, r, e in pool.map(fetch, [b for b in brands if b.get('offersUrl')])}
    log(f'== Fetched {sum(1 for r, e in pages.values() if r)} of {len(pages)} websites')

    blocked = {'IG': 0, 'FB': 0}  # after 5 blocks in a row we stop trying that site for this run (it is blocking us)
    for b in brands:
        if time.time() > deadline:
            log('== Time up, the rest next run')
            break
        log(f"-- {b['country']} {b['name']}")
        upd = {}
        ig, fb = b.get('instagram'), b.get('facebook') or b.get('instagram')
        if os.environ.get('SKIP_FACEBOOK') == '1':
            fb = None
        if ig and blocked['IG'] < 5:
            try:
                for p in scrape_instagram(ig):
                    if p['time'] < NOW - TWO_DAYS or not OFFER_RE.search(p['text']):
                        continue  # only fresh discount posts
                    save(db, b, extract(p['text'], b), 'instagram', p['url'], p['id'], p['image'], p['time'], stats)
                blocked['IG'] = 0
            except IGBlocked as e:
                stats['errors'] += 1; blocked['IG'] = 5; log('   IG:', e); log('== Instagram API limit/token problem, skipping Instagram for this run')
            except Exception as e:
                stats['errors'] += 1; log('   IG:', e)
                if not (os.environ.get('IG_USER_ID') and os.environ.get('META_TOKEN')):
                    blocked['IG'] += 1  # no-login scraping: count blocks
                if blocked['IG'] == 5: log('== Instagram is blocking this server, skipping it for this run')
            time.sleep(1 if os.environ.get('META_TOKEN') else random.uniform(3, 6))  # be gentle, avoids blocks
        if fb and blocked['FB'] < 5:
            try:
                for p in scrape_facebook(fb):
                    if OFFER_RE.search(p['text']):
                        save(db, b, extract(p['text'], b), 'facebook', p['url'], p['id'], '', None, stats)
                blocked['FB'] = 0
            except Exception as e:
                stats['errors'] += 1; blocked['FB'] += 1; log('   FB:', e)
                if blocked['FB'] == 5: log('== Facebook is blocking this server, skipping it for this run')
            time.sleep(random.uniform(2, 4))
        if b['id'] in pages:
            res, err = pages[b['id']]
            if res:
                if res[2] and not b.get('instagram'):  # learned the brand's Instagram from its website
                    b['instagram'] = res[2]; upd['instagram'] = res[2]
                    log(f"   found Instagram: @{res[2]}")
                # AI only when the offer on the page changed; otherwise reuse last time's result (saves Gemini quota)
                _sm = summarize(res[0]) or {}
                sig = 'v3' + hashlib.sha1(json.dumps([_sm.get('title'), _sm.get('discount'), _sm.get('ends')]).encode()).hexdigest()[:16]
                ai_ready = os.environ.get('GEMINI_API_KEY') and not AI_DOWN[0]
                if sig == b.get('pageSig') and isinstance(b.get('lastOffers'), list) and (b.get('lastOffersAI') or not ai_ready):
                    items = b['lastOffers']; stats['ai_skipped'] = stats.get('ai_skipped', 0) + 1
                else:
                    ai = ai_extract(res[0], b)
                    items = find_offers(res[0]) if ai is None else \
                        [f for f in ai if f and f.get('title') and summarize(f"{f.get('discount', '')} {f.get('title', '')}")][:3]
                    upd['pageSig'] = sig; upd['lastOffersAI'] = ai is not None
                    upd['lastOffers'] = [{k: str(f.get(k) or '') for k in ('title', 'discount', 'description', 'ends')} for f in items or [] if f]
                    b.update(upd)
                link = res[3] or b['offersUrl'] if len(res) > 3 else b['offersUrl']
                try: save(db, b, items, 'website', link, None, res[1], None, stats)
                except Exception as e: stats['errors'] += 1; log('   SAVE:', e)
            else:
                stats['errors'] += 1; log('   WEB:', err)
        last = b.get('lastRun')
        if upd or not last or NOW - last > timedelta(hours=6):  # fewer Firebase writes
            upd['lastRun'] = NOW
            db.update('brands', b['id'], upd)

    export_live(db)
    if isinstance(db, CachedDB):
        db.save()
    if AI_FAILS: log('== AI failures:', json.dumps(AI_FAILS))
    log('== Done', json.dumps(stats))


if __name__ == '__main__':
    main()
