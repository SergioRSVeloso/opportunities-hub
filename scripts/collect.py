#!/usr/bin/env python3
"""Opportunities Hub daily collector — runs entirely on GitHub, no paid API.

1. Reads every source in scripts/sources.json (RSS/Atom feeds, listing pages, EasyChair CFP lists,
   optionally the ReliefWeb jobs API).
2. Opens each new item's official page and extracts title, organisation, location, deadline,
   event dates, fees and a summary (schema.org JSON-LD first, then page metadata and text).
3. Keeps what is in international relations / political science / related fields, sorts it into
   jobs, internships, PhDs, scholarships, calls for papers and events, and assigns a world region.
   If GitHub Models is available (free with the workflow's GITHUB_TOKEN) a small model double-checks
   relevance and fills gaps; without it, keyword rules do the job.
4. Merges into data/positions.json (keeping each item's "new" date), removes closed items and items
   that disappeared from their source, merges data/manual.json, writes data/meta.json.

Standard library only.  Environment (all optional):
  GITHUB_TOKEN        enables GitHub Models classification (set automatically in Actions)
  USE_AI              "0" to switch GitHub Models off
  GH_MODEL            default "openai/gpt-4.1-mini"
  RELIEFWEB_APPNAME   enables the ReliefWeb source (free appname from reliefweb.int)
  MAX_DETAIL          max official pages opened per run (default 450)
Flags:
  --manual-only       only merge data/manual.json and prune
  --fixtures DIR      read pages from DIR (offline tests): file name = sha1(url)[:16] + ".html"
  --only NAME         run sources whose name contains NAME
"""
import datetime as dt, gzip, hashlib, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
POS_F, META_F, MANUAL_F = (os.path.join(DATA, n) for n in ("positions.json", "meta.json", "manual.json"))
SOURCES_F = os.path.join(ROOT, "scripts", "sources.json")

CATS = ["job", "internship", "phd", "scholarship", "cfp", "event"]
GROUPS = ["EU institution", "EU agency", "UN system", "University", "Think tank / research institute", "Other"]
REGIONS = ["Europe", "China & East Asia", "South & Southeast Asia", "Middle East & North Africa",
           "Sub-Saharan Africa", "Russia & Central Asia", "Americas", "Online / global"]
EVENT_TYPES = ["Conference", "Workshop", "Talk / panel / webinar", "Summer / winter school", "Journal / special issue", "Other"]
FORMATS = ["In person", "Online", "Hybrid"]

TODAY = dt.date.today()
NOW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
import http.cookiejar
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
FIXTURES = None


def log(*a):
    print(*a, flush=True)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj, indent=0):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


# =========================================================== HTTP
def fetch(url, data=None, headers=None, timeout=25):
    if FIXTURES is not None:
        p = os.path.join(FIXTURES, hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
        raise urllib.error.URLError("no fixture for " + url)
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,*/*;q=0.8",
         "Accept-Language": "en-GB,en;q=0.9", "Accept-Encoding": "gzip, deflate", "Upgrade-Insecure-Requests": "1",
         "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
         "Cache-Control": "no-cache"}
    h.update(headers or {})
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read(4_000_000)
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                cs = r.headers.get_content_charset()
                if not cs:
                    m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
                    cs = m.group(1).decode() if m else "utf-8"
                try:
                    return raw.decode(cs, "replace")
                except LookupError:
                    return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410, 401, 403):
                break
        except Exception as e:
            last = e
        time.sleep(2)
    raise last


# =========================================================== text helpers
def clean(s, n=4000):
    if s is None:
        return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s)))
    return re.sub(r"\s+", " ", s).strip()[:n]


def page_text(doc):
    d = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", " ", doc)
    m = re.search(r"(?is)<(main|article)\b.*?</\1>", d)
    body = m.group(0) if m and len(clean(m.group(0))) > 400 else d
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", body)
    t = html.unescape(re.sub(r"<[^>]+>", " ", body))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()[:20000]


def meta(doc, *names):
    for n in names:
        m = re.search(r'(?is)<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(n), doc) or \
            re.search(r'(?is)<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(n), doc)
        if m and m.group(1).strip():
            return clean(m.group(1), 1500)
    return ""


def jsonld(doc):
    out = []
    for m in re.finditer(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', doc):
        try:
            v = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = v if isinstance(v, list) else [v]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if "@graph" in x:
                    stack += x["@graph"] if isinstance(x["@graph"], list) else [x["@graph"]]
                out.append(x)
            elif isinstance(x, list):
                stack += x
    return out


def ld_type(x):
    t = x.get("@type", "")
    return " ".join(t) if isinstance(t, list) else str(t)


def ld_place(loc):
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if isinstance(loc, str):
        return clean(loc, 160)
    if not isinstance(loc, dict):
        return ""
    if "VirtualLocation" in ld_type(loc):
        return "Online"
    a = loc.get("address", loc)
    if isinstance(a, str):
        return clean(a, 160)
    if isinstance(a, dict):
        country = a.get("addressCountry", "")
        if isinstance(country, dict):
            country = country.get("name", "")
        parts = [a.get("addressLocality", ""), a.get("addressRegion", "") if not a.get("addressLocality") else "", country]
        s = ", ".join(clean(p, 60) for p in parts if p)
        if s:
            return s
    return clean(loc.get("name", ""), 160)


# =========================================================== dates
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PATTERNS = [
    (re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?" + MON + r"\.?,?\s+(20\d\d)\b", re.I), "dmy"),
    (re.compile(r"\b" + MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d\d)\b"), "dmy_num"),
    (re.compile(r"(20\d\d)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    (re.compile(r"\b(20\d\d)[./](\d{1,2})[./](\d{1,2})\b"), "ymd"),
]
YEARLESS = [(re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + MON + r"\b(?!\.?,?\s+20\d\d)", re.I), "dm"),
            (re.compile(r"\b" + MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!,?\s+20\d\d)", re.I), "md"),
            (re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "cn")]


def yearless_dates(s):
    """Dates written without a year (common on event pages): assume the next occurrence."""
    out = []
    for rx, kind in YEARLESS:
        for m in rx.finditer(s):
            g = m.groups()
            try:
                if kind == "dm":
                    mo, d = MONTHS[g[1][:3].lower()], int(g[0])
                elif kind == "md":
                    mo, d = MONTHS[g[0][:3].lower()], int(g[1])
                else:
                    mo, d = int(g[0]), int(g[1])
            except (KeyError, ValueError):
                continue
            x = mk(TODAY.year, mo, d)
            if x and x < TODAY - dt.timedelta(days=2):
                x = mk(TODAY.year + 1, mo, d)
            if x:
                out.append((m.start(), x))
    out.sort()
    return out


def mk(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def find_dates(s):
    out = []
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(s):
            g = m.groups()
            if kind == "ymd":
                d = mk(g[0], g[1], g[2])
            elif kind == "dmy":
                d = mk(g[2], MONTHS[g[1][:3].lower()], g[0])
            elif kind == "mdy":
                d = mk(g[2], MONTHS[g[0][:3].lower()], g[1])
            else:
                d = mk(g[2], g[1], g[0])
            if d:
                out.append((m.start(), d))
    out.sort()
    return out


def iso_date(v):
    if not v:
        return None
    m = re.match(r"(20\d\d)-(\d\d)-(\d\d)", str(v))
    if m:
        d = mk(*m.groups())
        return d.isoformat() if d else None
    ds = find_dates(str(v))
    return ds[0][1].isoformat() if ds else None


DEADLINE_RX = re.compile(r"(?i)(deadline|closing date|closes|close date|apply (?:by|before|until)|applications? (?:are )?(?:due|accepted until|close)|"
                         r"submission(?:s)? (?:deadline|due|by)|abstracts? (?:due|deadline|by|submission)|due date|register by|registration (?:deadline|closes)|"
                         r"last date|valid through|expires?|end date for applications|bewerbungsfrist|date limite)")


def deadline_from_text(t):
    best = None
    for m in DEADLINE_RX.finditer(t):
        window = t[m.end(): m.end() + 90]
        ds = [d for _, d in find_dates(window)]
        if ds:
            d = ds[0]
            if d >= TODAY - dt.timedelta(days=1) and (best is None or d < best):
                best = d
    return best.isoformat() if best else None


EVENT_DATE_RX = re.compile(r"(?i)\b(date|when|time|takes place|will be held|held on|starts?)\b")


def event_date_from_text(t):
    head = t[:2500]
    for m in EVENT_DATE_RX.finditer(head):
        ds = [d for _, d in find_dates(head[m.end(): m.end() + 80]) if d >= TODAY]
        if ds:
            return ds[0].isoformat()
    ds = [d for _, d in find_dates(head) if TODAY <= d <= TODAY + dt.timedelta(days=400)]
    if ds:
        return ds[0].isoformat()
    ds = [d for _, d in yearless_dates(head[:1200]) if d <= TODAY + dt.timedelta(days=200)]
    return ds[0].isoformat() if ds else None


# =========================================================== regions
# multi-word names: split on explicit list instead
_RAW = {
    "Europe": ["united kingdom", "north macedonia", "san marino", "the hague", "den haag", "czech republic"],
    "China & East Asia": ["hong kong", "south korea", "north korea"],
    "South & Southeast Asia": ["sri lanka", "viet nam", "new zealand", "papua new guinea", "timor-leste", "new delhi", "kuala lumpur", "ho chi minh", "phnom penh"],
    "Middle East & North Africa": ["saudi arabia", "united arab emirates", "west bank", "tel aviv", "abu dhabi", "kuwait city"],
    "Sub-Saharan Africa": ["south africa", "sierra leone", "burkina faso", "ivory coast", "côte d'ivoire", "cote d'ivoire", "south sudan", "addis ababa", "cape town", "dar es salaam", "democratic republic of the congo"],
    "Russia & Central Asia": ["russian federation", "saint petersburg", "st petersburg"],
    "Americas": ["united states", "costa rica", "el salvador", "dominican republic", "new york", "los angeles", "san francisco", "new haven", "mexico city", "sao paulo", "são paulo", "rio de janeiro", "buenos aires"],
}
_RAW["Sub-Saharan Africa"] += ["sahel", "horn of africa", "west africa", "east africa", "southern africa", "central africa", "great lakes region", "sub-saharan"]
_RAW["Middle East & North Africa"] += ["middle east", "persian gulf", "gulf states", "maghreb", "levant", "mena", "north africa"]
_RAW["Europe"] += ["balkans", "western balkans", "baltic", "nordic", "eastern europe"]
_RAW["Russia & Central Asia"] += ["central asia", "south caucasus", "caucasus", "eurasia"]
_RAW["Americas"] += ["latin america", "caribbean", "south america", "central america", "north america"]
_RAW["South & Southeast Asia"] += ["south asia", "southeast asia", "south-east asia", "asean", "indo-pacific", "pacific islands"]
_RAW["China & East Asia"] += ["east asia", "korean peninsula", "taiwan strait"]
_SINGLE = {
    "Europe": "albania andorra austria belgium bosnia bulgaria croatia cyprus czechia denmark estonia finland france germany greece hungary iceland ireland italy kosovo latvia liechtenstein lithuania luxembourg malta moldova monaco montenegro netherlands norway poland portugal romania serbia slovakia slovenia spain sweden switzerland ukraine england scotland wales uk belarus brussels london paris berlin geneva vienna rome madrid lisbon amsterdam stockholm oslo copenhagen helsinki warsaw prague budapest athens dublin florence fiesole oxford cambridge edinburgh barcelona milan munich hamburg frankfurt leiden utrecht ghent leuven bruges strasbourg lyon bologna zurich bern lausanne krakow vilnius riga tallinn bucharest sofia zagreb ljubljana belgrade kyiv kiev porto coimbra maastricht groningen nijmegen tilburg rotterdam antwerp konstanz bremen mannheim heidelberg göttingen gottingen sciences-po",
    "China & East Asia": "china prc macau macao taiwan japan korea mongolia beijing shanghai guangzhou shenzhen hangzhou nanjing wuhan xiamen chengdu tianjin tokyo kyoto osaka seoul taipei ulaanbaatar tsinghua fudan",
    "South & Southeast Asia": "india pakistan bangladesh nepal bhutan maldives afghanistan singapore malaysia indonesia thailand vietnam philippines cambodia laos myanmar brunei australia fiji delhi mumbai bangalore bengaluru kolkata chennai islamabad lahore karachi dhaka colombo kathmandu kabul jakarta bangkok hanoi manila canberra sydney melbourne brisbane wellington auckland",
    "Middle East & North Africa": "turkey türkiye turkiye iran iraq syria lebanon jordan israel palestine gaza uae qatar kuwait bahrain oman yemen egypt libya tunisia algeria morocco istanbul ankara tehran baghdad erbil damascus beirut amman jerusalem ramallah riyadh jeddah dubai doha manama muscat cairo tripoli tunis algiers rabat casablanca",
    "Sub-Saharan Africa": "nigeria ghana kenya ethiopia tanzania uganda rwanda senegal mali niger cameroon congo drc sudan somalia zambia zimbabwe mozambique malawi botswana namibia angola madagascar mauritius liberia guinea benin togo chad gabon eritrea djibouti burundi lesotho eswatini gambia lagos abuja accra nairobi johannesburg pretoria durban kampala kigali dakar bamako niamey ouagadougou abidjan yaounde kinshasa khartoum juba mogadishu lusaka harare maputo gaborone windhoek luanda",
    "Russia & Central Asia": "russia kazakhstan uzbekistan kyrgyzstan tajikistan turkmenistan azerbaijan georgia armenia moscow astana almaty tashkent bishkek dushanbe ashgabat baku tbilisi yerevan",
    "Americas": "usa canada mexico brazil argentina chile colombia peru venezuela ecuador bolivia paraguay uruguay panama guatemala honduras nicaragua cuba haiti jamaica trinidad washington boston chicago stanford berkeley princeton philadelphia atlanta austin houston miami seattle denver toronto montreal ottawa vancouver brasilia santiago bogota bogotá lima quito",
}
REGION_TERMS = []
for r, lst in _RAW.items():
    REGION_TERMS += [(t, r) for t in lst]
for r, words in _SINGLE.items():
    REGION_TERMS += [(t, r) for t in words.split()]
REGION_TERMS.sort(key=lambda x: -len(x[0]))  # longest first ("new south wales" before "wales")
ISO2 = {"Europe": "AL AD AT BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO SM RS SK SI ES SE CH UA GB UK BY",
        "China & East Asia": "CN HK MO TW JP KR KP MN", "South & Southeast Asia": "IN PK BD LK NP BT MV AF SG MY ID TH VN PH KH LA MM BN TL AU NZ PG FJ",
        "Middle East & North Africa": "TR IR IQ SY LB JO IL PS SA AE QA KW BH OM YE EG LY TN DZ MA",
        "Sub-Saharan Africa": "NG GH KE ET ZA TZ UG RW SN ML NE CM CD CG SD SS SO ZM ZW MZ MW BW NA AO MG MU SL LR GN BJ TG TD GA ER DJ BI LS SZ GM CI BF",
        "Russia & Central Asia": "RU KZ UZ KG TJ TM AZ GE AM", "Americas": "US CA MX BR AR CL CO PE VE EC BO PY UY CR PA GT HN SV NI CU DO HT JM TT"}
ISO2_REGION = {c: r for r, cs in ISO2.items() for c in cs.split()}
US_STATES = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")


def region_of(*texts):
    for t in texts:
        if not t:
            continue
        low = " " + t.lower() + " "
        if re.search(r"\b(online|virtual|webinar|remote|zoom)\b", low) and len(t) < 40:
            return "Online / global"
        if US_STATES.search(t):
            return "Americas"
        m = re.search(r"(?:^|,\s*)([A-Z]{2})\s*$", t.strip())
        if m and m.group(1) in ISO2_REGION:
            return ISO2_REGION[m.group(1)]
        for term, r in REGION_TERMS:
            if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", low):
                return r
    return ""


# =========================================================== classification rules
RX = lambda p: re.compile(p, re.I)
CORE = RX(r"politic|international (relations|affairs|studies|politics|security|law|development)|\bIR\b|diplomac|foreign (policy|affairs|service)|geopolit|global (governance|affairs|studies|politics)|public policy|governance|\bpeace|conflict|security studies|strategic studies|war studies|european (studies|union|integration)")
RELATED = RX(r"secur|defen[cs]e|military|terroris|counter-?terror|nuclear|arms control|human rights|humanitarian|migration|refugee|asylum|border|development studies|democra|election|authoritarian|middle east|islam|muslim|arab|gulf|china|chinese|asia|asean|indo-pacific|africa|latin america|russia|eurasia|central asia|caucasus|balkan|area studies|regional studies|sociology|social science|political economy|economics of|trade policy|sanction|energy security|climate (policy|governance|security)|international law|human security|united nations|\bUN\b|\bEU\b|\bNATO\b|\bOSCE\b|african union|think tank|policy analys|civil society|transitional justice|genocide|history|anthropolog|media|communication|journalism|law\b|legal studies|global south|decoloni|postcolonial|religio")
ANY_FIELD = RX(r"all (fields|disciplines|subjects|areas)|any (field|discipline|subject)|social sciences?|humanities|multidisciplinary|interdisciplinary")
OFFTOPIC = RX(r"\b(nursing|medicine|medical|clinical|pharmac|chemistry|chemical|physics|engineer|engineering|computer science|software|developer|biolog|biomedical|mathemat|veterinar|dentist|accountant|mechanical|electrical|civil engineering|agricultur|marketing|sales|graphic design|culinary|beauty|fashion|music|football|sports? (scholarship|science))\b")
PREDATORY = RX(r"waset|world academy of science|scientific research publishing|omics|conferencealerts|iser |researchfora|iastem|academicsera|theiier|ocean of conferences|internationalconference(s)?alerts")

CAT_RULES = [
    ("cfp", RX(r"call for (papers|abstracts|proposals|submissions|chapters|panels|contributions|articles)|\bcfp\b|special issue|calls? for participants|paper proposals")),
    ("phd", RX(r"\bph\.?\s?d\b|doctoral|doctorate|dphil|doctoral candidate|doctoral researcher|early[- ]stage researcher|\besr\b|promovend|doktorand")),
    ("internship", RX(r"\bintern(ship)?s?\b|traineeship|trainee|blue book|schuman|\bstage\b|\bstagiaire\b|young professionals? programme|junior professional")),
    ("scholarship", RX(r"scholarship|fellowship|bursary|grant(s)?\b|stipend|funded (master|programme|program|study)|award for|fully funded|visiting (scholar|fellow|researcher) programme")),
    ("event", RX(r"\b(conference|workshop|webinar|seminar|lectures?|panel|symposium|forum|summit|roundtable|round table|book (launch|talk)|briefing|dialogue|summer school|winter school|colloquium|public event|town hall|in conversation)\b")),
]
JOBLIKE = RX(r"\b(vacancy|vacancies|position|post|officer|analyst|lecturer|professor|researcher|research (fellow|associate|assistant)|postdoc|consultant|coordinator|manager|advisor|adviser|specialist|associate|director|assistant|editor|recruit)\b")


def detect_category(title, text, hint=None, is_job=False):
    if hint in CATS:
        return hint
    t = title or ""
    for cat, rx in CAT_RULES:
        if cat == "event" and (is_job or JOBLIKE.search(t)):
            return "job"
        if rx.search(t):
            if cat == "scholarship" and RX(r"postdoc|research fellow|fellow in|lecturer|assistant professor").search(t):
                return "job"
            return cat
    head = (text or "")[:1500]
    if JOBLIKE.search(t):
        return "job"
    for cat, rx in CAT_RULES:
        if rx.search(head):
            return cat
    return "job"


def relevance(title, text, cat):
    """Return 'core', 'related' or None (not relevant)."""
    t = (title or "") + " \n " + (text or "")[:4000]
    if PREDATORY.search(t):
        return None
    if CORE.search(title or ""):
        return "core"
    off = OFFTOPIC.search(title or "")
    core_n = len(CORE.findall(t))
    rel_n = len(RELATED.findall(t))
    if off and core_n < 2:
        return None
    if core_n >= 2:
        return "core"
    if RELATED.search(title or "") or rel_n >= 3 or (core_n and rel_n):
        return "related"
    if cat == "scholarship" and ANY_FIELD.search(t):
        return "related"
    return None


def event_type(title, text):
    t = (title or "") + " " + (text or "")[:600]
    if RX(r"summer school|winter school|spring school|autumn school").search(t):
        return "Summer / winter school"
    if RX(r"special issue|edited volume|journal|book chapters?").search(title or ""):
        return "Journal / special issue"
    if RX(r"workshop").search(title or ""):
        return "Workshop"
    if RX(r"conference|congress|convention|symposium|annual meeting").search(t[:400]):
        return "Conference"
    if RX(r"webinar|lecture|talk|panel|seminar|roundtable|book launch|briefing|discussion|conversation|dialogue|forum").search(t[:400]):
        return "Talk / panel / webinar"
    return "Other"


FREE_RX = RX(r"free (of charge|admission|entry|event|and open|to attend|registration)|free(?:,| and) open to the public|no (registration )?fee|admission (is )?free|attendance is free|is free\b|free\b.{0,20}\bregist|open to the public|no cost to attend|without charge")
PAID_RX = RX(r"registration fee|conference fee|participation fee|ticket(s)? (cost|price)|(€|£|\$|eur|usd|gbp)\s?\d{2,}.{0,30}(fee|registration|ticket)|(fee|registration|ticket).{0,30}(€|£|\$|eur|usd|gbp)\s?\d{2,}")


def cost_of(text, ld_offers=None):
    if isinstance(ld_offers, list):
        ld_offers = ld_offers[0] if ld_offers else None
    if isinstance(ld_offers, dict):
        p = str(ld_offers.get("price", "")).strip()
        if p in ("0", "0.0", "0.00", "free", "Free"):
            return "free"
        if re.match(r"^\d+(\.\d+)?$", p) and float(p) > 0:
            return "paid"
    t = (text or "")[:6000]
    if FREE_RX.search(t):
        return "free"
    if PAID_RX.search(t):
        return "paid"
    return "unknown"


def format_of(location, text, ld_mode=""):
    t = (location or "") + " " + (text or "")[:1500] + " " + str(ld_mode)
    online = bool(RX(r"online|virtual|webinar|zoom|livestream|live stream|webcast|OnlineEventAttendanceMode").search(t))
    person = bool(RX(r"in[- ]person|venue|room\b|hall\b|auditorium|OfflineEventAttendanceMode").search(t))
    if RX(r"hybrid|MixedEventAttendanceMode").search(t) or (online and person):
        return "Hybrid"
    if online and not person:
        return "Online"
    return "In person"


# =========================================================== source adapters
def _anchor_links(doc, base):
    for m in re.finditer(r'(?is)<a\b[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', doc):
        href = urllib.parse.urljoin(base, html.unescape(m.group(1)).strip())
        yield href, clean(m.group(2), 300), clean(doc[m.end(): m.end() + 900], 500)


SKIP_PATH = re.compile(r"/(page|tag|tags|category|categories|author|topics?|search|feed|login|wp-content|wp-json)(/|$)|[?&](page|p)=|\.(pdf|jpg|png|docx?)$", re.I)


def _auto_links(doc, base):
    """No pattern match: take same-site links one level below the listing page."""
    bu = urllib.parse.urlparse(base)
    root = bu.path.rstrip("/")
    seen, out = set(), []
    for href, text, ctx in _anchor_links(doc, base):
        u = urllib.parse.urlparse(href)
        if u.netloc != bu.netloc or SKIP_PATH.search(href) or href in seen:
            continue
        path = u.path.rstrip("/")
        if root and not path.startswith(root + "/"):
            continue
        last = path.rsplit("/", 1)[-1]
        if len(last) < 10 or "-" not in last or len(text) < 12:
            continue
        seen.add(href)
        out.append({"url": href, "title": text, "snippet": ctx})
    return out


def _feed_url(doc, base):
    m = re.search(r'(?is)<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)', doc) or \
        re.search(r'(?is)<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml', doc)
    return urllib.parse.urljoin(base, html.unescape(m.group(1))) if m else None


def adapt_links(src):
    out, seen = [], set()
    pat = re.compile(src["pattern"]) if src.get("pattern") else None
    urls = src.get("urls") or [src["url"]]
    docs = []
    for u in urls:
        doc = fetch(u)
        docs.append((u, doc))
        if not pat:
            continue
        for href, text, ctx in _anchor_links(doc, u):
            if not pat.search(href) or href in seen:
                continue
            if len(text) < 6 or RX(r"^(read more|more|details|register|apply|view|learn more|›|»)$").match(text):
                text = ""
            seen.add(href)
            out.append({"url": href, "title": text, "snippet": ctx})
    if not out:
        # fallback 1: the site's own RSS/Atom feed; fallback 2: links below the listing page
        for u, doc in docs:
            f = _feed_url(doc, u)
            if f:
                try:
                    host = urllib.parse.urlparse(u).netloc
                    out = [c for c in adapt_rss({"url": f}) if urllib.parse.urlparse(c["url"]).netloc == host]
                    if out:
                        src["_note"] = "read via the site's feed"
                        break
                except Exception:
                    pass
        if not out:
            for u, doc in docs:
                out += _auto_links(doc, u)
            if out:
                src["_note"] = "pattern did not match; used nearby links"
    best = {}
    for c in out:
        b = best.get(c["url"])
        if not b or (not b["title"] and c["title"]) or len(c["title"]) > len(b["title"]):
            best[c["url"]] = c
    return list(best.values())


def adapt_rss(src):
    out = []
    for u in src.get("urls") or [src["url"]]:
        try:
            doc = fetch(u)
        except Exception as e:
            if out:
                continue
            raise e
        doc = re.sub(r"^[^<]+", "", doc)
        try:
            root = ET.fromstring(doc.encode("utf-8"))
            items = [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]
            for it in items:
                f = {c.tag.split("}")[-1]: c for c in it}
                title = clean(f["title"].text if "title" in f else "", 300)
                link = ""
                if "link" in f:
                    link = (f["link"].text or f["link"].get("href") or "").strip()
                desc = ""
                for k in ("encoded", "description", "summary", "content"):
                    if k in f and (f[k].text or "").strip():
                        desc = clean(f[k].text, 3000)
                        break
                if link:
                    out.append({"url": link, "title": title, "snippet": desc})
        except ET.ParseError:
            for m in re.finditer(r"(?is)<item>(.*?)</item>", doc):
                b = m.group(1)
                t = re.search(r"(?is)<title>(.*?)</title>", b)
                l = re.search(r"(?is)<link>(.*?)</link>", b)
                d = re.search(r"(?is)<description>(.*?)</description>", b)
                if l:
                    out.append({"url": clean(l.group(1).replace("<![CDATA[", "").replace("]]>", "")), "title": clean((t.group(1) if t else "").replace("<![CDATA[", "").replace("]]>", "")), "snippet": clean((d.group(1) if d else "").replace("<![CDATA[", "").replace("]]>", ""), 3000)})
    return out


def adapt_easychair(src):
    doc = fetch(src["url"])
    out = []
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", doc):
        cells = re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row)
        if len(cells) < 5:
            continue
        a = re.search(r'href=["\']([^"\']*cfp/[^"\']+)["\']', cells[0]) or re.search(r'href=["\']([^"\']*cfp/[^"\']+)["\']', row)
        if not a:
            continue
        url = urllib.parse.urljoin(src["url"], html.unescape(a.group(1)))
        acr, name, loc, dl, start = (clean(c, 300) for c in cells[:5])
        out.append({"url": url, "title": f"{name} ({acr})" if acr and acr not in name else name, "snippet": "",
                    "hint": {"location": loc, "deadline": iso_date(dl), "eventStart": iso_date(start), "deadlineLabel": "Submission deadline",
                             "topics": clean(cells[5], 300) if len(cells) > 5 else ""}})
    return out


def adapt_reliefweb(src):
    app = os.environ.get("RELIEFWEB_APPNAME", "").strip()
    if not app:
        raise RuntimeError("skipped: set the RELIEFWEB_APPNAME variable to enable")
    body = json.dumps({"query": {"value": src.get("query", "political affairs")}, "limit": int(src.get("max", 40)),
                       "sort": ["date.created:desc"], "profile": "full"}).encode()
    doc = fetch("https://api.reliefweb.int/v2/jobs?appname=" + urllib.parse.quote(app), data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    j = json.loads(doc)
    out = []
    for d in j.get("data", []):
        f = d.get("fields", {})
        country = ", ".join(c.get("name", "") for c in f.get("country", [])[:2])
        city = ", ".join(c.get("name", "") for c in f.get("city", [])[:1])
        org = ", ".join(s.get("name", "") for s in f.get("source", [])[:1])
        out.append({"url": f.get("url_alias") or f.get("url"), "title": clean(f.get("title"), 300), "snippet": clean(f.get("body"), 3000),
                    "hint": {"org": org, "location": ", ".join(x for x in (city, country) if x) or "", "deadline": iso_date((f.get("date") or {}).get("closing")),
                             "noDetail": True}})
    return out


ADAPTERS = {"links": adapt_links, "rss": adapt_rss, "easychair": adapt_easychair, "reliefweb": adapt_reliefweb}


# =========================================================== detail extraction
def extract(cand, src):
    """Build a raw item from a candidate (+ its official page)."""
    hint = dict(cand.get("hint") or {})
    url = cand["url"]
    doc, text = "", ""
    if not hint.get("noDetail"):
        try:
            doc = fetch(url)
            text = page_text(doc)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return {"dead": True, "url": url}
        except Exception:
            pass
    text = text or cand.get("snippet", "")
    lds = jsonld(doc) if doc else []
    job = next((x for x in lds if "JobPosting" in ld_type(x)), None)
    ev = next((x for x in lds if re.search(r"Event", ld_type(x))), None)
    title = cand.get("title") or ""
    if job and job.get("title"):
        title = clean(job["title"], 250)
    elif ev and ev.get("name"):
        title = clean(ev["name"], 250)
    if not title or len(title) < 8:
        title = meta(doc, "og:title", "twitter:title") or clean((re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", doc) or re.search(r"(?is)<title>(.*?)</title>", doc) or [None, ""])[1], 250) if doc else title
    title = re.sub(r"\s*[|–—-]\s*(jobs\.ac\.uk|EURAXESS|FindAPhD.*|UNjobs|Times Higher Education.*|THE Unijobs)$", "", title, flags=re.I).strip()

    summary = ""
    for cand_sum in ((job or {}).get("description"), (ev or {}).get("description"), meta(doc, "og:description", "description") if doc else "", cand.get("snippet")):
        c = clean(cand_sum, 900)
        if len(c) > 60:
            summary = c
            break
    if not summary:
        summary = clean(text, 600)
    org = hint.get("org", "")
    if job:
        ho = job.get("hiringOrganization")
        org = org or clean(ho.get("name") if isinstance(ho, dict) else ho, 160)
    if ev:
        oz = ev.get("organizer")
        if isinstance(oz, list):
            oz = oz[0] if oz else None
        org = org or clean(oz.get("name") if isinstance(oz, dict) else oz, 160)
    org = org or meta(doc, "og:site_name") if doc else org
    org = org or src.get("org") or re.sub(r"\s*(events|jobs|–.*|\(.*\))\s*$", "", src["name"], flags=re.I).strip()

    location = hint.get("location") or ""
    if job:
        location = location or ld_place(job.get("jobLocation"))
        if not location and str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            location = "Remote"
    if ev:
        location = location or ld_place(ev.get("location"))
    if not location:
        m = re.search(r"(?i)\b(location|duty station|place of (work|performance)|venue|city|host country)\s*[:\-]\s*([^\n.;|]{3,80})", text)
        if m:
            location = clean(m.group(3), 120)

    deadline = hint.get("deadline") or (iso_date(job.get("validThrough")) if job else None) or deadline_from_text(text) or deadline_from_text(cand.get("snippet", ""))
    ev_start = hint.get("eventStart") or (iso_date(ev.get("startDate")) if ev else None)
    ev_end = iso_date(ev.get("endDate")) if ev else None
    return {"url": url, "title": title, "org": org, "summary": summary, "text": text[:6000], "location": location,
            "deadline": deadline, "eventStart": ev_start, "eventEnd": ev_end,
            "ldOffers": (ev or {}).get("offers"), "ldMode": (ev or {}).get("eventAttendanceMode", ""),
            "deadlineLabel": hint.get("deadlineLabel", ""), "topics": hint.get("topics", ""),
            "contract": clean((job or {}).get("employmentType"), 80).replace("_", " ").lower() if job else "", "isJob": bool(job)}


# =========================================================== GitHub Models (optional, free)
class Classifier:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.on = bool(self.token) and os.environ.get("USE_AI", "1") != "0" and FIXTURES is None
        self.model = os.environ.get("GH_MODEL") or "openai/gpt-4.1-mini"
        self.calls = 0
        self.max_calls = int(os.environ.get("AI_MAX_CALLS", "40"))
        self.note = "keyword rules" if not self.on else f"GitHub Models ({self.model}) + keyword rules"

    def classify(self, raws):
        if not self.on or not raws:
            return {}
        out = {}
        for i in range(0, len(raws), 8):
            if self.calls >= self.max_calls or not self.on:
                break
            batch = raws[i:i + 8]
            lines = []
            for k, r in enumerate(batch):
                lines.append(json.dumps({"n": k, "title": r["title"][:200], "org": r["org"][:100], "location": r["location"][:100],
                                         "text": (r["summary"] + " … " + r["text"][:1400])[:1800]}, ensure_ascii=False))
            prompt = (f"Today is {TODAY.isoformat()}. For each item decide if it is an opportunity for someone in international relations, political science "
                      "or a related field (security/defence, area studies of the Middle East, Islamic world, China, Asia, Africa, Russia, Latin America, migration, human rights, "
                      "international law, peace and conflict, governance, development, political economy, social sciences generally). Scholarships open to all fields count.\n"
                      "Return JSON {\"items\":[{\"n\":0,\"keep\":true,\"field\":\"core|related|other\",\"category\":\"job|internship|phd|scholarship|cfp|event\","
                      "\"region\":\"one of " + "|".join(REGIONS) + "\",\"location\":\"City, Country or Online\",\"deadline\":\"YYYY-MM-DD or null\","
                      "\"eventStart\":\"YYYY-MM-DD or null\",\"eventEnd\":\"YYYY-MM-DD or null\",\"cost\":\"free|paid|unknown\",\"attend\":true,"
                      "\"summary\":\"2 plain sentences\"}]}. keep=false for spam, predatory conferences, closed items, or unrelated fields. "
                      "Use only facts in the text; null if unknown. cost free only if stated.\nItems:\n" + "\n".join(lines))
            body = json.dumps({"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": "You label job and event listings. Reply with JSON only."},
                                            {"role": "user", "content": prompt}]}).encode()
            try:
                req = urllib.request.Request("https://models.github.ai/inference/chat/completions", data=body, headers={
                    "Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    j = json.load(r)
                self.calls += 1
                txt = j["choices"][0]["message"]["content"]
                for x in json.loads(txt).get("items", []):
                    n = x.get("n")
                    if isinstance(n, int) and 0 <= n < len(batch):
                        out[batch[n]["url"]] = x
            except urllib.error.HTTPError as e:
                log(f"   GitHub Models unavailable ({e.code}); continuing with keyword rules")
                self.on = False
                self.note = f"keyword rules (GitHub Models returned {e.code})"
            except Exception as e:
                log(f"   GitHub Models error: {e}")
                self.calls += 1
            time.sleep(4)  # free tier is ~15 requests/minute
        return out


# =========================================================== build item
def slug(text, n=64):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "item"


def norm_url(u):
    u = (u or "").split("#")[0].rstrip("/")
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"[?&](utm_[^=&]+|fbclid|gclid)=[^&]*", "", u)
    return u.lower()


def build(raw, src, ai):
    title = raw["title"]
    if not title or len(title) < 8:
        return None
    hint_cat = src.get("category")
    cat = detect_category(title, raw["text"], hint_cat, raw.get("isJob"))
    field = "core" if src.get("trusted") else relevance(title, raw["summary"] + " " + raw["text"] + " " + raw.get("topics", ""), cat)
    if src.get("trusted") and not CORE.search(title + " " + raw["text"][:2000]):
        field = "related"
    if ai:
        if ai.get("keep") is False and not src.get("trusted"):
            return None
        if ai.get("field") in ("core", "related") and not src.get("trusted"):
            field = ai["field"]
        if ai.get("field") == "other" and not src.get("trusted"):
            return None
        if not hint_cat and ai.get("category") in CATS:
            cat = ai["category"]
    if not field:
        return None
    location = raw["location"] or (ai or {}).get("location") or ""
    region = region_of(location)
    if not region and (ai or {}).get("region") in REGIONS:
        region = ai["region"]
    if not region:
        region = src.get("region") or region_of(title, raw["summary"], raw["text"][:800]) or ""
    deadline = raw["deadline"] or iso_date((ai or {}).get("deadline"))
    it = {
        "title": title[:220], "org": raw["org"][:160], "group": src.get("group") if src.get("group") in GROUPS else "Other",
        "region": region if region in REGIONS else "", "category": cat, "location": location[:160],
        "contract": raw.get("contract", ""), "grade": "", "reference": "", "deadline": deadline, "deadlineTime": "", "deadlineText": "",
        "opens": None, "network": "MSCA doctoral network" if RX(r"marie (skłodowska|sklodowska)|msca|doctoral network").search(title + raw["text"][:3000]) and cat == "phd" else "",
        "field": field, "discipline": "", "summary": clean((ai or {}).get("summary") or raw["summary"], 900),
        "tasks": [], "requirements": [], "howToApply": "See the official page for how to apply.",
        "statusNote": "" if deadline else "Deadline not found automatically: check the official page.",
        "applyUrl": raw["url"], "listingUrl": src.get("url") or (src.get("urls") or [""])[0], "source": src["name"],
        "relevant": True, "tags": [], "fetchedAt": TODAY.isoformat(), "auto": True,
    }
    if cat in ("cfp", "event"):
        start = raw["eventStart"] or iso_date((ai or {}).get("eventStart"))
        if cat == "event" and not start:
            start = event_date_from_text(raw["text"])
        end = raw["eventEnd"] or iso_date((ai or {}).get("eventEnd")) or start
        c = cost_of(raw["text"], raw.get("ldOffers"))
        if c == "unknown" and (ai or {}).get("cost") in ("free", "paid"):
            c = ai["cost"]
        it.update({
            "eventType": "Journal / special issue" if cat == "cfp" and RX(r"special issue|journal|edited volume").search(title) else event_type(title, raw["text"]),
            "eventStart": start, "eventEnd": end, "eventDatesText": "",
            "format": format_of(location, raw["text"], raw.get("ldMode")), "cost": c,
            "costNote": "Free to attend (stated by the organiser)" if c == "free" else ("Fee applies" if c == "paid" else ""),
            "grants": bool(RX(r"travel (grant|bursar|support|funding)|fee waiver|bursar(y|ies)").search(raw["text"][:6000])),
            "attend": cat == "event",
            "deadlineLabel": raw.get("deadlineLabel") or ("Abstracts due" if cat == "cfp" else ""),
        })
        if cat == "event" and not it["deadline"]:
            it["deadline"] = start
            it["deadlineLabel"] = "Event start"
            it["statusNote"] = "" if start else it["statusNote"]
        if it["format"] == "Online" and not it["region"]:
            it["region"] = "Online / global"
    if not it["region"]:
        it["region"] = "Online / global" if RX(r"^(online|remote|virtual)").match(location) else ""
    h = hashlib.sha1(norm_url(raw["url"]).encode()).hexdigest()[:6]
    it["id"] = f"{slug(it['org'] + ' ' + it['title'])}-{h}"
    if is_expired(it, 0):
        return None
    if cat == "event" and not it.get("eventStart") and not it.get("deadline"):
        return None  # an event without any date is not useful
    return it


# =========================================================== merge / prune
def is_expired(it, grace_days):
    limit = TODAY - dt.timedelta(days=grace_days)
    def d(v):
        v = iso_date(v)
        return dt.date.fromisoformat(v) if v else None
    if it.get("category") == "event":
        ref = d(it.get("eventEnd")) or d(it.get("eventStart")) or d(it.get("deadline"))
        return bool(ref and ref < limit)
    dl = d(it.get("deadline"))
    return bool(dl and dl < limit)


def merge(existing, found):
    by_id = {x["id"]: x for x in existing}
    by_url = {norm_url(x.get("applyUrl", "")): x["id"] for x in existing if x.get("applyUrl")}
    added = updated = 0
    for it in found:
        oid = by_url.get(norm_url(it["applyUrl"])) or (it["id"] if it["id"] in by_id else None)
        if oid:
            old = by_id[oid]
            if old.get("manual"):
                continue
            new = dict(old)
            if old.get("auto"):  # our own earlier extraction: refresh fields
                for k, v in it.items():
                    if k != "id" and v not in ("", None, []):
                        new[k] = v
            else:  # a hand-curated item: only refresh "last seen" and fill a missing deadline
                new["fetchedAt"] = it["fetchedAt"]
                if not old.get("deadline") and it.get("deadline"):
                    new["deadline"] = it["deadline"]
            new["addedAt"] = old.get("addedAt") or NOW
            if new != old:
                updated += 1
            by_id[oid] = new
        else:
            it["addedAt"] = NOW
            by_id[it["id"]] = it
            by_url[norm_url(it["applyUrl"])] = it["id"]
            added += 1
    return list(by_id.values()), added, updated


def prune(items, ok_sources):
    keep, dropped = [], 0
    for it in items:
        if it.get("manual"):
            keep.append(it)
            continue
        grace = 3 if it.get("category") == "event" else 14
        if is_expired(it, grace):
            dropped += 1
            continue
        seen = iso_date(it.get("fetchedAt"))
        age = (TODAY - dt.date.fromisoformat(seen)).days if seen else 0
        if it.get("auto") and not iso_date(it.get("deadline")) and it.get("source") in ok_sources and age > 10:
            dropped += 1  # undated and no longer listed by a source that is working
            continue
        if not it.get("auto") and not iso_date(it.get("deadline")) and not iso_date(it.get("eventStart")) and age > 120:
            dropped += 1
            continue
        keep.append(it)
    return keep, dropped


def load_manual():
    out = []
    for raw in load(MANUAL_F, []):
        if not isinstance(raw, dict) or not raw.get("title") or not raw.get("applyUrl") or raw.get("category") not in CATS:
            continue
        it = dict(raw)
        it.setdefault("org", "")
        it.setdefault("group", "Other")
        it.setdefault("field", "core")
        it.setdefault("region", region_of(it.get("location", "")))
        it["manual"] = True
        it["id"] = it.get("id") or "manual-" + slug(it["title"], 60)
        it.setdefault("addedAt", NOW)
        out.append(it)
    return out


# =========================================================== main
def src_note(r):
    if r["note"] and not r["note"].startswith(("read via", "pattern did")):
        return r["note"]
    if not r["listed"]:
        return "page read, but no items recognised"
    bits = [f"{r['listed']} listed"]
    if r["dup"]:
        bits.append(f"{r['dup']} already found via another source")
    if r["dropped"]:
        bits.append(f"{r['dropped']} skipped (closed, no date, or outside the field)")
    if r["note"]:
        bits.append(r["note"])
    return " · ".join(bits) if (r["dup"] or r["dropped"] or r["note"]) else ""


def main():
    global FIXTURES
    args = sys.argv[1:]
    if "--fixtures" in args:
        FIXTURES = args[args.index("--fixtures") + 1]
    only = args[args.index("--only") + 1].lower() if "--only" in args else ""
    manual_only = "--manual-only" in args

    sources = [s for s in load(SOURCES_F, {}).get("sources", []) if not only or only in s["name"].lower()]
    existing = [x for x in load(POS_F, []) if isinstance(x, dict) and x.get("id") and not x.get("manual")]
    known = {norm_url(x.get("applyUrl", "")): x for x in existing}
    report, found, ok_sources = [], [], set()
    max_detail = int(os.environ.get("MAX_DETAIL", "450"))
    ai = Classifier()
    log(f"Collect {NOW} · {len(existing)} existing · {0 if manual_only else len(sources)} sources · {ai.note}")

    if not manual_only:
        # 1) list candidates from every source
        work = []
        def run_src(src):
            try:
                cands = ADAPTERS[src["type"]](src)[: int(src.get("max", 30))]
                return src, cands, ""
            except Exception as e:
                return src, [], str(e)[:160]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for src, cands, err in ex.map(run_src, sources):
                log(f"-> {src['name']}: {len(cands)} listed{(' · ' + err) if err else ''}")
                rep = {"name": src["name"], "listed": len(cands), "found": 0, "note": err or src.get("_note", ""), "dup": 0, "dropped": 0}
                report.append(rep)
                if cands:
                    ok_sources.add(src["name"])
                for c in cands:
                    if not c.get("url"):
                        continue
                    k = known.get(norm_url(c["url"]))
                    if k and k.get("auto") and iso_date(k.get("fetchedAt")) and (TODAY - dt.date.fromisoformat(k["fetchedAt"])).days < 5:
                        k2 = dict(k); k2["fetchedAt"] = TODAY.isoformat()
                        found.append(k2); rep["found"] += 1
                        continue
                    if k and not k.get("auto"):
                        k2 = dict(k); k2["fetchedAt"] = TODAY.isoformat()
                        found.append(k2); rep["found"] += 1
                        continue
                    work.append((src, c, rep))
        # de-duplicate candidates across sources
        uniq, seen = [], set()
        for w in work:
            u = norm_url(w[1]["url"])
            if u not in seen:
                seen.add(u)
                uniq.append(w)
            else:
                w[2]["dup"] += 1
        uniq = uniq[:max_detail]
        log(f"Opening {len(uniq)} new or changed pages…")
        # 2) open official pages
        with ThreadPoolExecutor(max_workers=10) as ex:
            raws = list(ex.map(lambda w: extract(w[1], w[0]), uniq))
        # 3) classify
        pairs = [(w, r) for w, r in zip(uniq, raws) if r and not r.get("dead")]
        need_ai = [r for (w, r) in pairs if not w[0].get("trusted") or not r["deadline"] or not r["location"]]
        labels = ai.classify(need_ai)
        for (src, c, rep), r in pairs:
            it = build(r, src, labels.get(r["url"]))
            if it:
                found.append(it)
                rep["found"] += 1
            else:
                rep["dropped"] += 1

    merged, added, updated = merge(existing, found)
    merged, pruned = prune(merged, ok_sources)
    manual = load_manual()
    mids = {m["id"] for m in manual}
    merged = [x for x in merged if x["id"] not in mids] + manual
    merged.sort(key=lambda x: (x.get("deadline") or "9999-12-31", x.get("title", "")))
    save(POS_F, merged)

    regions = {}
    for x in merged:
        regions[x.get("region") or "Unassigned"] = regions.get(x.get("region") or "Unassigned", 0) + 1
    meta_d = load(META_F, {})
    meta_d.update({"total": len(merged), "byRegion": regions})
    if not manual_only:
        meta_d.update({"lastRefreshed": NOW, "method": ai.note,
                       "summary": f"{added} added, {updated} updated, {pruned} closed or delisted removed; {len(merged)} open",
                       "sources": [{"name": r["name"], "found": r["found"], "note": src_note(r)} for r in report]})
    save(META_F, meta_d, indent=1)
    log(meta_d.get("summary", f"{len(merged)} items"))


if __name__ == "__main__":
    main()
