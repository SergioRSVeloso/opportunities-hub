# Opportunities Hub

A global listing of opportunities in international relations, political science and related fields (security and defence, area studies of the Middle East, Islamic world, China, Asia, Africa, Russia and Latin America, migration, human rights, international law, peace and conflict):

- **Jobs**, **Internships**, **PhD positions**, **Scholarships**
- **Calls for papers** and **Conferences & events** (with free / paid, online / in-person and "attend without a paper" filters)
- A **By region** view, plus region filters on every page: Europe · China & East Asia · South & Southeast Asia · Middle East & North Africa · Sub-Saharan Africa · Russia & Central Asia · Americas · Online / global
- A page per position with the official link, deadline countdown, calendar link and a fit check
- **My profile** (your base CV, with PDF / Word upload) and one-click **tailored CV, cover letter, motivation letter, abstract or bio** for any position, downloadable as .docx or .md
- **My applications** tracker with statuses and notes
- Installable on your phone as an app (PWA)

It is a static site on **GitHub Pages**. A **GitHub Action** collects new listings every morning, entirely on GitHub, with **no API key and no cost**.

---

## How it works on GitHub

| Feature | How it runs |
|---|---|
| Listings | `data/positions.json`, rewritten every morning by `scripts/collect.py` in a GitHub Action |
| Daily collection | The script reads about 60 sources listed in `scripts/sources.json`: academic job boards (jobs.ac.uk, Times Higher Education, HigherEdJobs, EURAXESS, FindAPhD), UN jobs, scholarship and fellowship feeds, CfP lists (EasyChair, WikiCFP, H-Net), and the event pages of think tanks and institutes in every region (Washington, Europe, Singapore, India, Australia, Beijing, Shanghai, Doha, Pretoria and more). It opens each new item's official page and reads the deadline, place, dates and fee (from the page's structured data or text), keeps what belongs to IR and related fields, sorts it into the six categories and a world region, removes closed items, and republishes the site. |
| Sorting | Keyword rules, plus GitHub's free built-in AI (**GitHub Models**, via the Action's own token, no key needed) to double-check relevance and fill gaps. If GitHub Models is unavailable, keyword rules alone are used. |
| Your profile, saved list, documents | Stored **in your browser only** (localStorage). Nothing personal is ever committed. Use *My profile → Export backup* to save a copy or move it to your phone (*Import backup*). |
| Tailored CVs and letters, CV reading, fit check | Free by default: each document button gives you a ready-made prompt: paste it into the Claude app (free with your account), then *Paste result* puts the answer back for editing and .docx download. If you prefer one-click writing inside the site, add an Anthropic API key under *My profile → AI writing* (a few cents per document; the key stays on your device). |
| Hand-added items | Add them to `data/manual.json` (see below); they are merged on the next push. |

> **Privacy:** the site is public (anyone with the link can see the listings), but your profile, documents and any key never leave your device.

---

## Setup (about 5 minutes, no keys needed)

### 1. Create the repository
1. Sign in to GitHub → **New repository** → name it e.g. `opportunities-hub` → **Public** → Create.
2. Click **uploading an existing file**, drag in **everything inside this folder** (including the hidden `.github` folder and `.nojekyll`; on macOS press <kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>.</kbd> in Finder to see hidden files), and **Commit changes**.
   *With git:* `git init && git add . && git commit -m "Opportunities Hub" && git branch -M main && git remote add origin https://github.com/<you>/opportunities-hub.git && git push -u origin main`

> If the uploader skips the `.github` folder: **Add file → Create new file**, name it `.github/workflows/refresh.yml`, and paste in that file's contents.

### 2. Allow the Action to save and publish
- **Settings → Actions → General → Workflow permissions → Read and write permissions** → Save.
- **Settings → Pages → Build and deployment → Source: GitHub Actions.**

### 3. Run the first collection
**Actions** tab → *Collect and publish* → **Run workflow**. It takes 5–15 minutes. Your site is then live at
`https://<your-username>.github.io/opportunities-hub/`

It already contains 175 hand-checked open items, so it works before the first run too. From then on it runs **every day at 06:31 UTC** by itself, and republishes whenever you change a file.

### 4. Install it on your phone
- **Android (Chrome):** menu ⋮ → **Install app** / **Add to Home screen**.
- **iPhone (Safari):** Share → **Add to Home Screen**.

Move your profile with *Export backup* on the computer and *Import backup* on the phone.

---

## Checking and improving the sources

After each run, the site's **Sources** page shows how many items every source gave and any error. Websites change their layout, and some block automated visitors, so a few sources will show 0 now and then. To fix or extend them, edit `scripts/sources.json`:

```json
{"name": "Example Institute events", "type": "links", "url": "https://example.org/events",
 "pattern": "^https://example\\.org/event/[^/?#]{8,}", "category": "event",
 "group": "Think tank / research institute", "region": "Middle East & North Africa", "trusted": true, "max": 15}
```

- `type`: `rss` (any RSS/Atom feed: most WordPress sites have one at `/feed/`), `links` (a listing page; `pattern` is a regular expression that the item links match), `easychair` (an EasyChair CFP topic page), `reliefweb` (see below).
- `trusted: true` means everything from that source is in the field; otherwise the relevance filter decides.
- `category` forces a category; otherwise it is detected from the title and text.
- `region` is used when a page gives no location.

**Optional extra source (free):** ReliefWeb's job API covers UN and NGO posts in political affairs, peacebuilding and governance worldwide. Request a free *appname* at reliefweb.int (API section), then add it under **Settings → Secrets and variables → Actions → Variables** as `RELIEFWEB_APPNAME`.

**Optional settings** (same Variables page): `USE_AI` = `0` turns GitHub Models off; `GH_MODEL` picks another GitHub Models model (default `openai/gpt-4.1-mini`).

To run only some sources, use **Run workflow** and type part of their name (e.g. `EURAXESS`).

### What automatic collection cannot do as well as a human
- Some sites (especially many Chinese institutions, EU and UN career portals built as web apps, and AcademicTransfer) don't show their listings to automated readers. Recurring programmes from those (CSC scholarships, Schwarzman, Yenching, EPSO traineeships, UN YPP and so on) are best added once a year to `data/manual.json`.
- Deadlines are read from the page; when none is found the item says *"Deadline not found automatically"*.
- Events are marked **free** only when the page says so.

### Adding an item by hand
Edit `data/manual.json` on GitHub:

```json
[
  {
    "title": "Research Fellow, Indo-Pacific Security",
    "org": "Example Institute",
    "category": "job",
    "group": "Think tank / research institute",
    "region": "South & Southeast Asia",
    "location": "Singapore",
    "deadline": "2027-01-31",
    "applyUrl": "https://example.org/vacancy",
    "summary": "Two-year post on maritime security."
  }
]
```

`category` is one of `job`, `internship`, `phd`, `scholarship`, `cfp`, `event`. Hand-added items stay until you remove them.

---

## Files

```
index.html                 the whole site (no build step)
manifest.webmanifest, sw.js, icons/   phone app install + offline support
data/positions.json        listings (updated by the Action)
data/meta.json             last refresh time and per-pass report (shown on Sources)
data/manual.json           your hand-added items
scripts/collect.py         daily collector (Python standard library only)
scripts/sources.json       the sources it reads (edit freely)
sources/*.csv              reference lists of EU bodies, UN entities, universities, Chinese, Asian, MENA, African and American institutions and portals
.github/workflows/refresh.yml   daily schedule + publish to GitHub Pages
```

### Run the collector on your own computer (optional)
```
python3 scripts/collect.py                 # all sources (keyword rules only, unless GITHUB_TOKEN is set)
python3 scripts/collect.py --only EURAXESS
python3 -m http.server 8000                # then open http://localhost:8000
```

### Troubleshooting
- **Action fails at "Commit updated data"** → step 2, *Read and write permissions*.
- **Action fails at "deploy"** → step 2, Pages source must be *GitHub Actions*.
- **A source shows 0 items** → open its URL in a browser; if the layout changed, adjust `pattern` in `scripts/sources.json`, or remove the source.
- **Phone shows old listings** → pull to refresh; the app fetches fresh listings whenever it is online.
