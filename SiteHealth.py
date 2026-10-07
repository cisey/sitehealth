# sitehealth.pyw — Site Health Checker (AdSense-aware)
# Single-file, stdlib-only. Python 3.8+.
#
# Usage (GUI):  pythonw sitehealth.pyw
# Usage (CLI):  python sitehealth.pyw https://example.com [--json] [--mode auto|general|adsense]
#
# Modes:
#   auto     (default) — AdSense checks only if AdSense is detected
#   general            — SEO/a11y/speed only, skip all AdSense checks
#   adsense            — always run AdSense checks (ads.txt, publisher ID, ad density)

import gzip
import json
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import urllib.request
import urllib.parse
import urllib.error
import re
from html.parser import HTMLParser

APP_NAME = "SiteHealth"
APP_VERSION = "1.0.0"
UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
      "Chrome/124 Mobile Safari/537.36")

COUNTERS = ("mc.yandex.", "facebook.com/tr", "google-analytics.com",
            "googletagmanager.com", "doubleclick.net", "bat.bing.com")
HIDDEN_1 = re.compile(r'(?:text-indent|left|right)\s*:\s*-\s*9{3,}px', re.I)
HIDDEN_2 = re.compile(r'font-size\s*:\s*0\s*(?:px|pt|em|rem|%)?\s*(?:;|$)', re.I)
JSONLD_RE = re.compile(
    r'<script[^>]*\btype\s*=\s*["\']application/ld\+json["\'][^>]*>',
    re.I)
ADSENSE_SRC_OK = ("https://pagead2.googlesyndication.com",
                  "//pagead2.googlesyndication.com")


# ───────────────────────── fetch ─────────────────────────
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
    })
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        elapsed = time.perf_counter() - t0
        hdr = r.headers
        final = r.geturl()
    enc = (hdr.get("Content-Encoding") or "").strip().lower()
    size_compressed = len(raw)
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    charset = hdr.get_content_charset() or "utf-8"
    try:
        html = raw.decode(charset, "replace")
    except LookupError:
        html = raw.decode("utf-8", "replace")
    return {
        "html": html, "final": final, "hdr": hdr, "elapsed": elapsed,
        "size": len(raw), "size_compressed": size_compressed,
        "gzip": "gzip" in enc,
    }


# ───────────────────────── HTML parser ─────────────────────────
class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []          # (tag, attrs, raw, in_head)
        self.in_head = False
        self.title = ""
        self._in_title = False
        self._skip = 0
        self.words = 0
        self.h1 = 0
        self.h2 = 0
        self.img_missing_alt = 0
        self.lang = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        raw = self.get_starttag_text() or ""
        if tag == "head":
            self.in_head = True
        if tag == "html":
            self.lang = a.get("lang", "")
        self.tags.append((tag, a, raw, self.in_head))
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "h1":
            self.h1 += 1
        if tag == "h2":
            self.h2 += 1
        if tag == "img" and not a.get("alt", "").strip():
            self.img_missing_alt += 1

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, d):
        if self._in_title:
            self.title += d
        if not self._skip:
            self.words += len(re.findall(r"\w+", d))


# ───────────────────────── helpers ─────────────────────────
def _is_lazy(attrs):
    """Image tag looks lazy-loaded by any modern technique."""
    if attrs.get("loading", "").lower() == "lazy":
        return True
    if "data-src" in attrs or "data-lazy-src" in attrs or "data-original" in attrs:
        return True
    cls = attrs.get("class", "").lower()
    if "lazyload" in cls or "lazy-load" in cls or "lazy" in cls.split():
        return True
    return False


def _has_size_info(attrs):
    """Image declares size via width/height, srcset, or inline aspect-ratio."""
    if "width" in attrs and "height" in attrs:
        return True
    if "srcset" in attrs and attrs.get("srcset", "").strip():
        return True
    style = attrs.get("style", "")
    if "aspect-ratio" in style or "width" in style:
        return True
    return False


def _is_article_page(html, path, og_type, ld_json_present):
    """Heuristic: URL date pattern, og:type=article, long slug,
    or JSON-LD Article/BlogPosting/NewsArticle."""
    if re.search(r'/\d{4}/\d{1,2}/(?:\d{1,2}/)?[^/]+', path):
        return True
    if og_type == "article":
        return True
    last = path.rstrip("/").split("/")[-1]
    if last.count("-") >= 4:
        return True
    if ld_json_present and re.search(
            r'"@type"\s*:\s*\[?\s*"(?:Article|BlogPosting|NewsArticle)"', html):
        return True
    return False


# ───────────────────────── checks ─────────────────────────
def check(url, emit, mode="auto"):
    """
    emit(level, text) — level in {ok,bad,warn,info,section,
                                   meta_speed, meta_is_article, meta_adsense}
    """
    try:
        r = fetch(url)
    except Exception as e:
        emit("bad", "✖ Could not open page: " + str(e))
        emit("meta_speed", "0")
        emit("meta_is_article", "0")
        emit("meta_adsense", "0")
        return

    html = r["html"]
    p = urllib.parse.urlparse(r["final"])
    base = p.scheme + "://" + p.netloc
    is_blogger = "blogspot.com" in p.netloc

    P = Page()
    try:
        P.feed(html)
        P.close()
    except Exception:
        pass

    tags = P.tags
    metas = [a for t, a, _, _ in tags if t == "meta"]
    links = [a for t, a, _, _ in tags if t == "link"]
    imgs = [a for t, a, _, _ in tags if t == "img"]

    def meta(name, key="name"):
        for a in metas:
            if a.get(key, "").lower() == name:
                return a.get("content", "")
        return None

    og_type = (meta("og:type", "property") or "").lower()
    ld_present = bool(JSONLD_RE.search(html))
    is_article = _is_article_page(html, p.path, og_type, ld_present)

    # AdSense detection (multiple signals)
    srcs = [a["src"] for t, a, _, _ in tags
            if t == "script" and "adsbygoogle" in a.get("src", "")]
    pubs = sorted(set(re.findall(r"ca-pub-\d{10,}", html)))
    ins_units = [
        a for t, a, _, _ in tags
        if t == "ins" and (
            "adsbygoogle" in a.get("class", "").lower()
            or "data-ad-client" in a
            or "data-ad-slot" in a
        )
    ]
    ins = len(ins_units)
    has_adsense = bool(srcs or pubs or ins)

    # Mode resolution
    effective_mode = mode
    if mode == "auto":
        effective_mode = "adsense" if has_adsense else "general"

    emit("meta_is_article", "1" if is_article else "0")
    emit("meta_adsense", "1" if has_adsense else "0")

    if r["final"] != url:
        emit("info", "→ Redirected: " + r["final"])
    if is_article:
        emit("info", "Page type: article page")
    else:
        emit("info", "Page type: looks like a home/list page. "
                     "Scan an article URL for content checks.")
    if mode == "auto":
        emit("info", f"Mode: auto → {effective_mode}")
    else:
        emit("info", f"Mode: {effective_mode}")

    # ═══════════ BASIC ═══════════
    emit("section", "── BASIC ──")

    if p.scheme == "https":
        emit("ok", "HTTPS in use")
    else:
        emit("bad", "Site is not HTTPS")

    vp = meta("viewport")
    if vp is None:
        emit("bad", "No viewport meta tag (broken on mobile)")
    elif "device-width" in vp.lower():
        emit("ok", "viewport correct (width=device-width)")
    else:
        emit("bad", "viewport has fixed width: " + vp)

    if any(a.get(k, "").startswith("[http")
           for t, a, _, _ in tags for k in ("src", "href")):
        emit("bad", 'Broken markdown-style link: src="[url](url)"')

    if P.lang:
        emit("ok", f"html lang attribute present: {P.lang}")
    else:
        emit("warn", "html lang attribute missing (a11y/SEO)")

    # ── AdSense section (only in adsense mode) ──
    if effective_mode == "adsense":
        if not srcs:
            emit("warn", "adsbygoogle.js not found (no AdSense script). "
                         "Fine if the site doesn't use AdSense.")
        else:
            broken = [x for x in srcs
                      if not x.startswith(ADSENSE_SRC_OK)
                      or "](" in x or "[" in x]
            if broken:
                for x in broken:
                    emit("bad", "AdSense script URL broken: " + x[:90])
            else:
                emit("ok", "AdSense script URL looks fine")

        if pubs:
            emit("ok", "Publisher ID found: " + ", ".join(pubs))

        if srcs and ins == 0:
            emit("warn", "No ad unit (<ins>). Harmless if auto ads are on "
                         "(they get injected after load).")
        elif ins:
            emit("ok", f"{ins} ad unit(s) found on page")
    elif has_adsense:
        # general mode but AdSense detected — notify user
        emit("info", "AdSense detected but mode is 'general' — skipping "
                     "AdSense-specific checks.")

    # GA4
    ids = sorted(set(re.findall(r"G-[A-Z0-9]{6,}", html)))
    if ids and "googletagmanager.com" in html:
        emit("ok", "GA4 tag present: " + ", ".join(ids))
    elif "googletagmanager.com" in html:
        emit("warn", "Google Tag Manager present but no GA4 ID (G-...) found")
    else:
        emit("warn", "No GA4 / Google tag found (another analytics may be used)")

    rob = meta("robots")
    if rob and "noindex" in rob.lower():
        emit("bad", "Page is 'noindex' — hidden from Google")
    else:
        emit("ok", "No noindex")

    title = re.sub(r"\s+", " ", P.title).strip()
    if title:
        emit("ok", "<title> present")
        if len(title) > 70:
            emit("warn", f"Title too long ({len(title)} chars, ~60 recommended): "
                         f"{title[:60]}...")
    else:
        emit("bad", "<title> missing")

    if meta("description"):
        emit("ok", "meta description present")
    else:
        emit("warn", "meta description missing")

    # Open Graph
    og_props = set()
    for a in metas:
        prop = a.get("property", "").lower()
        if prop.startswith("og:"):
            og_props.add(prop)
    if og_props:
        emit("ok", f"Open Graph tags present ({len(og_props)})")
    else:
        emit("warn", "No Open Graph tags (bad social previews)")

    # JSON-LD
    if ld_present:
        emit("ok", "JSON-LD structured data present")
    else:
        emit("warn", "No JSON-LD structured data")

    # Alt text
    if imgs:
        if P.img_missing_alt == 0:
            emit("ok", "All images have alt text")
        elif P.img_missing_alt <= len(imgs) * 0.3:
            emit("warn", f"{P.img_missing_alt}/{len(imgs)} images missing alt text")
        else:
            emit("bad", f"{P.img_missing_alt}/{len(imgs)} images missing alt text (a11y/SEO)")
    else:
        emit("info", "No images found on page")

    # ── ads.txt (only in adsense mode) ──
    if effective_mode == "adsense":
        if is_blogger:
            emit("ok", "Blogger subdomain: ads.txt usually not required")
        else:
            try:
                txt = fetch(base + "/ads.txt", timeout=10)["html"]
                if "<html" in txt[:500].lower():
                    emit("bad", "ads.txt missing (URL returns an HTML page)")
                elif "google.com" not in txt:
                    emit("bad", "ads.txt present but no google.com line")
                elif pubs and pubs[0].replace("ca-", "") in txt:
                    emit("ok", "ads.txt present with matching publisher ID")
                elif pubs:
                    emit("warn", f"ads.txt has google.com but publisher ID "
                                 f"{pubs[0]} not found")
                else:
                    emit("warn", "ads.txt has google.com line but no "
                                 "ca-pub-* ID found on page to match against")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    emit("bad", "ads.txt not found (404)")
                else:
                    emit("warn", f"ads.txt could not be read (HTTP {e.code})")
            except Exception:
                emit("warn", "ads.txt could not be read (connection error, retry)")

    # ═══════════ SPEED ═══════════
    emit("section", "── SPEED ──")
    speed = 100

    times = [r["elapsed"]]
    for _ in range(2):
        try:
            times.append(fetch(r["final"])["elapsed"])
        except Exception:
            pass
    median = sorted(times)[len(times) // 2]
    detail = ", ".join(f"{x:.2f}" for x in times)
    label = (f"Page download time (median {median:.2f}s; samples: {detail}; "
             f"from your connection, rough estimate)")
    if median < 1.0:
        emit("ok", label)
    elif median < 2.5:
        emit("warn", label)
        speed -= 3
    else:
        emit("warn", label)
        speed -= 8

    kb = r["size"] / 1024
    if r["gzip"]:
        emit("ok", f"Compression on: HTML {r['size_compressed']/1024:.0f} KB "
                   f"→ {kb:.0f} KB")
    else:
        emit("bad", "Server does not send gzip (wasted traffic)")
        speed -= 15
    if kb > 300:
        emit("bad", f"HTML too large ({kb:.0f} KB)")
        speed -= 15
    elif kb > 150:
        emit("warn", f"HTML large ({kb:.0f} KB)")
        speed -= 8

    cc = (r["hdr"].get("Cache-Control") or "").lower()
    emit("info", "Page cache setting: " + (cc if cc else "unspecified") +
                 " (usually fine for HTML)")

    rb = [a for t, a, _, h in tags
          if h and t == "script" and a.get("src") and "async" not in a
          and "defer" not in a and a.get("type", "").lower() != "module"]
    rb += [a for t, a, _, h in tags
           if h and t == "link" and "stylesheet" in a.get("rel", "").lower()]
    if len(rb) > 4:
        emit("warn", f"{len(rb)} render-blocking resources "
                     f"(scripts/CSS blocking head) — consider async/defer")
        speed -= min(15, (len(rb) - 4) * 3)
    else:
        emit("ok", f"Few render-blocking resources ({len(rb)})")

    requests = (sum(1 for t, a, _, _ in tags if t == "script" and a.get("src"))
                + sum(1 for a in links if "stylesheet" in a.get("rel", "").lower())
                + len(imgs)
                + sum(1 for t, _, _, _ in tags if t == "iframe"))
    if requests > 60:
        emit("bad", f"High request count (~{requests})")
        speed -= 10
    elif requests > 35:
        emit("warn", f"Request count a bit high (~{requests})")
        speed -= 5
    else:
        emit("ok", f"Request count reasonable (~{requests})")

    n = len(imgs)
    if n < 4:
        emit("info", f"Few images ({n}), skipping lazy/size checks")
    else:
        rest = imgs[1:]
        lazy = sum(1 for a in rest if _is_lazy(a))
        if lazy >= len(rest) * 0.5:
            emit("ok", f"{lazy}/{len(rest)} images lazy-loaded (first excluded)")
        elif lazy > 0:
            emit("warn", f"Only {lazy}/{len(rest)} images lazy-loaded")
            speed -= 5
        else:
            emit("warn", f"No images lazy-loaded except the first ({len(rest)} images)")
            speed -= 8
        sized = sum(1 for a in imgs if _has_size_info(a))
        if sized < n * 0.5:
            emit("warn", "Most images lack width/height/srcset (may cause "
                         "layout shift)")
            speed -= 5
        else:
            emit("ok", f"{sized}/{n} images declare size (width/height/srcset)")

    # ═══════════ POLICY RISK ═══════════
    if effective_mode == "adsense":
        emit("section", "── POLICY RISK (heuristic, not a verdict) ──")

        words = P.words
        if is_article:
            if words >= 500:
                emit("ok", f"Text length looks sufficient (~{words} words; "
                           f"menus/sidebar text included)")
            elif words >= 250:
                emit("warn", f"Text a bit short (~{words} words; menu/sidebar "
                             f"text included)")
            else:
                emit("bad", f"Very little text (~{words} words) — 'thin content' risk")

            if ins > 0:
                ratio = words / ins
                if ratio < 150:
                    emit("bad", f"High ad density: {ins} ads, ~{words} words "
                                f"(~{ratio:.0f} words per ad)")
                elif ratio < 300:
                    emit("warn", f"Ad density borderline: ~{ratio:.0f} words per ad")
                else:
                    emit("ok", f"Ad density reasonable (~{ratio:.0f} words per ad)")
            else:
                emit("info", "Ad density not computed (no <ins> ad unit visible)")
        else:
            emit("info", "Text length and ad density not measured on a "
                         "home/list page. Scan an article URL.")

        # Hidden text
        hidden, skipped = [], set()
        for t, a, raw, _ in tags:
            st = a.get("style", "")
            if not st or not (HIDDEN_1.search(st) or HIDDEN_2.search(st)):
                continue
            src = a.get("src", "").lower()
            if t == "img" and any(x in src for x in COUNTERS):
                u = "https:" + src if src.startswith("//") else src
                skipped.add(urllib.parse.urlparse(u).netloc or src[:30])
            else:
                snippet = re.sub(r"\s+", " ", raw)
                if len(snippet) > 170:
                    snippet = snippet[:170] + "..."
                hidden.append(snippet)
        if skipped:
            emit("info", "Tracking pixel images not counted as hidden text (" +
                         ", ".join(sorted(skipped)) + ")")
        if hidden:
            emit("warn", f"{len(hidden)} tag(s) show hidden-text technique "
                         f"(-9999px or font-size:0); can be legitimate. First hit:\n    "
                         + hidden[0])
    else:
        emit("section", "── POLICY RISK ──")
        emit("info", "Skipped — site does not use AdSense (general mode). "
                     "Use --mode adsense to force.")

    # ── Structure / SEO (always) ──
    if P.h1 == 1:
        emit("ok", "One <h1> heading")
    elif P.h1 == 0:
        emit("warn", "No <h1> (heading hierarchy looks incomplete)")
    else:
        emit("info", f"{P.h1} <h1> tags (theme-related, usually harmless)")

    if P.h2 == 0:
        if is_article and P.h1 >= 1:
            emit("warn", "No <h2> subheadings (SEO/structure)")
        elif not is_article:
            emit("info", "No <h2> on this page (home/list page, normal)")

    if any("canonical" in a.get("rel", "").lower() for a in links):
        emit("ok", "canonical tag present")
    else:
        emit("warn", "No canonical tag (duplicate-content risk)")

    try:
        rt = fetch(base + "/robots.txt", timeout=10)["html"]
        lines = [l.strip().lower() for l in rt.replace("\r", "").split("\n")]
        all_agents = False
        blocked = False
        for l in lines:
            if l.startswith("user-agent:"):
                all_agents = "*" in l
            elif l.startswith("disallow:") and all_agents:
                if l[len("disallow:"):].strip() == "/":
                    blocked = True
        if blocked:
            emit("bad", "robots.txt blocks the whole site (Disallow: /)")
        else:
            emit("ok", "robots.txt does not block the site")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            emit("info", "robots.txt missing (nothing blocked either)")
        else:
            emit("warn", f"robots.txt could not be read (HTTP {e.code})")
    except Exception:
        emit("warn", "robots.txt could not be read (connection error, retry)")

    emit("meta_speed", str(max(0, min(100, speed))))


# ───────────────────────── scoring wrapper ─────────────────────────
def run_check(url, mode="auto"):
    """Runs checks, returns list of (level, text) with score entries appended."""
    findings = []

    def emit(level, text):
        findings.append((level, text))

    check(url, emit, mode=mode)

    # Extract meta channels
    is_article = False
    has_adsense = False
    speed = 100
    cleaned = []
    for lvl, txt in findings:
        if lvl == "meta_is_article":
            is_article = txt == "1"
            continue
        if lvl == "meta_adsense":
            has_adsense = txt == "1"
            continue
        if lvl == "meta_speed":
            speed = int(txt)
            continue
        cleaned.append((lvl, txt))
    findings = cleaned

    # Policy score
    policy_start = next((i for i, (t, m) in enumerate(findings)
                         if t == "section" and "POLICY" in m), None)
    policy = None
    if policy_start is not None and has_adsense:
        policy_slice = findings[policy_start + 1:]
        pbad = sum(1 for t, _ in policy_slice if t == "bad")
        pwarn = sum(1 for t, _ in policy_slice if t == "warn")
        if pbad >= 2 or (pbad and pwarn >= 2):
            policy = "HIGH"
        elif pbad or pwarn >= 2:
            policy = "MEDIUM"
        else:
            policy = "LOW"

    # Speed score line
    if speed >= 80:
        findings.append(("score_green", f"★ SPEED SCORE: {speed}/100"))
    elif speed >= 50:
        findings.append(("score_yellow", f"★ SPEED SCORE: {speed}/100"))
    else:
        findings.append(("score_red", f"★ SPEED SCORE: {speed}/100"))

    # Policy score line
    if not has_adsense:
        findings.append(("score_gray",
                         "★ POLICY RISK: N/A — no AdSense detected"))
    elif not is_article:
        findings.append(("score_gray",
                         "★ POLICY RISK: NOT MEASURED — home/list page, "
                         "scan an article URL"))
    else:
        color = {"HIGH": "score_red", "MEDIUM": "score_yellow",
                 "LOW": "score_green"}[policy]
        findings.append((color, f"★ POLICY RISK: {policy} (heuristic)"))

    return findings


# ───────────────────────── CLI ─────────────────────────
def cli(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = [a for a in argv[1:] if a.startswith("--")]

    if not args:
        print(f"{APP_NAME} {APP_VERSION}")
        print(f"Usage: {argv[0]} <url> [--json] [--mode auto|general|adsense]")
        return 1

    url = args[0]
    if not url.startswith("http"):
        url = "https://" + url

    mode = "auto"
    if "--mode" in flags:
        try:
            idx = argv.index("--mode")
            mode = argv[idx + 1]
            if mode not in ("auto", "general", "adsense"):
                mode = "auto"
        except (ValueError, IndexError):
            mode = "auto"
    if "--general" in flags:
        mode = "general"
    if "--adsense" in flags:
        mode = "adsense"

    findings = run_check(url, mode=mode)

    if "--json" in flags:
        out = {
            "url": url,
            "version": APP_VERSION,
            "mode": mode,
            "findings": [{"level": t, "text": m} for t, m in findings],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for t, m in findings:
            if t in ("section", "score_green", "score_yellow",
                     "score_red", "score_gray"):
                print()
            print(m)
        errs = sum(1 for t, _ in findings if t == "bad")
        warns = sum(1 for t, _ in findings if t == "warn")
        print(f"\nSUMMARY: {errs} error(s), {warns} warning(s)")
    return 0


# ───────────────────────── GUI ─────────────────────────
def build_gui():
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("860x840")

    # Use 'clam' theme for consistent ttk look across platforms
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    top = tk.Frame(root)
    top.pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(top, text="Site URL:").pack(side="left")
    entry = tk.Entry(top, font=("Segoe UI", 11))
    entry.pack(side="left", fill="x", expand=True, padx=8)
    btn = tk.Button(top, text="Check")
    btn.pack(side="left")

    mode_row = tk.Frame(root)
    mode_row.pack(fill="x", padx=10, pady=(0, 6))
    tk.Label(mode_row, text="Mode:").pack(side="left")

    mode_var = tk.StringVar(value="auto")
    for label, value in [("Auto", "auto"), ("General", "general"),
                         ("AdSense", "adsense")]:
        ttk.Radiobutton(mode_row, text=label, variable=mode_var,
                        value=value).pack(side="left", padx=4)

    state = {"last_findings": None, "dark": False}

    def start():
        url = entry.get().strip()
        if not url:
            return
        if not url.startswith("http"):
            url = "https://" + url
        mode = mode_var.get()
        btn.config(state="disabled", text="Checking...")
        box.config(state="normal")
        box.delete("1.0", "end")
        box.insert("end", f"Scanning: {url}  (mode={mode})\n")
        box.config(state="disabled")

        def worker():
            try:
                findings = run_check(url, mode=mode)
            except Exception as e:
                findings = [("bad", "✖ Unexpected error: " + str(e))]
            root.after(0, lambda: show(findings))

        threading.Thread(target=worker, daemon=True).start()

    def show(findings):
        state["last_findings"] = findings
        box.config(state="normal")
        box.delete("1.0", "end")
        for lvl, txt in findings:
            if lvl in ("section", "score_green", "score_yellow",
                       "score_red", "score_gray"):
                box.insert("end", "\n")
            box.insert("end", txt + "\n", lvl)
        errs = sum(1 for t, _ in findings if t == "bad")
        warns = sum(1 for t, _ in findings if t == "warn")
        box.insert("end", f"\nSUMMARY: {errs} error(s), {warns} warning(s)\n",
                   "title")
        box.config(state="disabled")
        btn.config(state="normal", text="Check")

    def copy_report():
        if not state["last_findings"]:
            return
        text = "\n".join(m for _, m in state["last_findings"])
        root.clipboard_clear()
        root.clipboard_append(text)
        messagebox.showinfo(APP_NAME, "Report copied to clipboard.")

    def save_report():
        if not state["last_findings"]:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("JSON", "*.json")],
            initialfile="sitehealth_report.txt",
        )
        if not path:
            return
        if path.endswith(".json"):
            data = {"findings": [{"level": t, "text": m}
                                 for t, m in state["last_findings"]]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                for _, m in state["last_findings"]:
                    f.write(m + "\n")
        messagebox.showinfo(APP_NAME, "Saved: " + path)

    def toggle_theme():
        state["dark"] = not state["dark"]
        bg = "#1e1e1e" if state["dark"] else "SystemButtonFace"
        fg = "#e0e0e0" if state["dark"] else "SystemButtonText"
        root.configure(bg=bg)
        for w in (top, mode_row, bar):
            w.configure(bg=bg)
            for child in w.winfo_children():
                try:
                    child.configure(bg=bg, fg=fg)
                except tk.TclError:
                    pass
        if state["dark"]:
            box.configure(bg="#1e1e1e", fg="#e0e0e0",
                          insertbackground="#e0e0e0")
            for tag, color in [("ok", "#7cffa0"), ("bad", "#ff7c7c"),
                               ("warn", "#ffc97c"), ("info", "#a0a0a0"),
                               ("section", "#ffffff")]:
                box.tag_config(tag, foreground=color)
        else:
            box.configure(bg="white", fg="black", insertbackground="black")
            box.tag_config("ok", foreground="#1a7f37")
            box.tag_config("bad", foreground="#c62828")
            box.tag_config("warn", foreground="#b26a00")
            box.tag_config("info", foreground="#666666")
            box.tag_config("section", foreground="#333333")

    btn.config(command=start)
    entry.bind("<Return>", lambda e: start())

    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10)
    tk.Button(bar, text="Copy report", command=copy_report).pack(side="left")
    tk.Button(bar, text="Save report", command=save_report).pack(side="left", padx=6)
    tk.Button(bar, text="Toggle theme", command=toggle_theme).pack(side="left")

    box = scrolledtext.ScrolledText(root, font=("Segoe UI", 10), wrap="word",
                                    spacing1=1, spacing3=1)
    box.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    box.tag_config("ok", foreground="#1a7f37")
    box.tag_config("bad", foreground="#c62828")
    box.tag_config("warn", foreground="#b26a00")
    box.tag_config("info", foreground="#666666")
    box.tag_config("section", font=("Segoe UI", 11, "bold"), foreground="#333333")
    box.tag_config("score_green", font=("Segoe UI", 12, "bold"),
                   foreground="#1a7f37")
    box.tag_config("score_yellow", font=("Segoe UI", 12, "bold"),
                   foreground="#b26a00")
    box.tag_config("score_red", font=("Segoe UI", 12, "bold"),
                   foreground="#c62828")
    box.tag_config("score_gray", font=("Segoe UI", 12, "bold"),
                   foreground="#666666")
    box.tag_config("title", font=("Segoe UI", 11, "bold"))
    box.config(state="disabled")

    entry.focus()
    root.mainloop()


# ───────────────────────── main ─────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        sys.exit(cli(sys.argv))
    build_gui()