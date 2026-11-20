# Discount Finder: Offers & Deals

Live discounts, sales and offers by **country → city**, for every country.

- `index.html` – public site (GitHub Pages)
- `admin.html` – admin panel: add deals, confirm pending deals, manage brands
- `worker/` – runs every 6 hours via GitHub Actions:
  1. hides deals whose end time has passed (using the city's own time zone)
  2. deals without an end date are re-checked every 2 days; if no longer found they are hidden
  3. scans each brand's offers page and Instagram for new deals (AI extraction with Gemini, regex fallback)
- `data/countries.json` – all countries; `data/cities/<CC>.json` – cities (built from GeoNames by the *Build world cities list* workflow)

## Setup
1. Create a Firebase project, enable **Firestore** and **Authentication → Email/Password**, add your admin user.
2. Put `apiKey`, `projectId`, `adminEmail` in `config.js`.
3. Paste `firestore.rules` (with your email) into Firestore → Rules.
4. GitHub → Settings → Secrets → Actions:
   - `FIREBASE_SERVICE_ACCOUNT` – service account JSON (Firebase → Project settings → Service accounts → Generate new private key)
   - `GEMINI_API_KEY` – optional, from Google AI Studio
   - `IG_USER_ID`, `META_TOKEN` – optional, Instagram Business Discovery

Visibility rule: a deal shows only if `status == active`, it has started, and either its end time is in the future, or (no end time) it was confirmed in the last 48 hours.

City data © GeoNames, CC BY 4.0.
