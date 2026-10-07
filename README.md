# SiteHealth

A single-file, stdlib-only site health checker for bloggers and AdSense publishers.

No pip install needed. Runs on Python 3.8+.

## What it checks

Basic — HTTPS, viewport, html lang, title, meta description, canonical, Open Graph, JSON-LD, image alt, robots.txt, noindex.

AdSense (auto-detected or forced) — adsbygoogle.js, publisher ID, ad units, ads.txt + Google line matching.

Speed — download time, gzip, HTML size, render-blocking resources, request count, lazy images, image size hints.

Policy risk (heuristic) — thin content, ad density, hidden text, heading hierarchy.

## Modes

- auto (default) — AdSense checks only if AdSense is detected on the page
- general — Pure SEO/a11y/speed, skip all AdSense logic
- adsense — Force AdSense checks (e.g. debugging ads.txt)

## Usage

GUI:
    pythonw SiteHealth.py

CLI:
    python SiteHealth.py https://example.com
    python SiteHealth.py https://example.com --json
    python SiteHealth.py https://example.com --mode general
    python SiteHealth.py https://example.com --mode adsense

## Why no dependencies?

Everything runs on the Python standard library: urllib, gzip, re, html.parser, tkinter, json. Zero install friction, no supply-chain risk.

## Scoring

Speed score (0-100): starts at 100, penalties for slow download, no gzip, oversized HTML, render-blocking resources, missing lazy loading.

Policy risk (LOW/MEDIUM/HIGH): heuristic based on text length, ad density, hidden text. Not a Google verdict. Only applies to article pages with AdSense detected.

## License

MIT - see LICENSE.