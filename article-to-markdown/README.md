# scrape.py

A small CLI for converting public web articles into local Markdown with
embedded images. Built for personal study notes — point it at a URL,
get a `.md` file with images saved alongside.

## What it does

- Fetches an article and extracts the main content (no nav, sidebar, or footer)
- Downloads images locally and rewrites the HTML to point at them
- Converts the result to clean Markdown with YAML frontmatter
- Skips already-scraped articles on re-runs (idempotent)
- Optionally pulls cookies from your installed browser so the script
  inherits any session you already have

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

## Usage

```bash
# Single URL
python scrape.py https://blog.cloudflare.com/some-post/

# Multiple URLs
python scrape.py https://example.com/a https://example.com/b

# From a file (one URL per line; blank lines and # comments are ignored)
python scrape.py --urls-file urls.txt

# Custom output folder
python scrape.py <url> --output ./obsidian-vault/eng-blogs

# Re-scrape something that's already there
python scrape.py <url> --force
```

`urls.txt` example:

```
# Cloudflare deep dives
https://blog.cloudflare.com/some-post/
https://blog.cloudflare.com/another-post/

# Stripe engineering
https://stripe.com/blog/some-article/
```

## Options

| Flag | Default | Notes |
|---|---|---|
| `--output` | `./articles` | Where markdown + `images/` land |
| `--browser` | `chrome` | Cookie source. `chrome`/`firefox`/`edge`/`brave`/`chromium`/`opera`/`vivaldi`/`safari`, or `none` to skip cookies |
| `--selector` | (auto) | CSS selector override when auto-extraction picks the wrong container |
| `--force` | off | Re-scrape URLs whose markdown already exists |
| `--debug` | off | Save raw HTML next to the markdown for inspection |

## Output

```
articles/
├── ubers-rate-limiting-system.md
├── another-post.md
└── images/
    ├── ubers-rate-limiting-system-01.png
    ├── ubers-rate-limiting-system-02.png
    └── another-post-01.jpg
```

Each markdown file starts with YAML frontmatter that Obsidian and most
static-site generators understand:

```yaml
---
title: "Article Title"
source: https://example.com/post/
scraped: 2026-05-04
---
```

## Browser cookies on Windows

Cookies are useful when a site's CDN blocks plain Python clients but
lets your real browser through. You don't always need them — most
public blogs work fine with `--browser none`.

A few Windows-specific gotchas:

- **Close Chrome before running.** Chrome locks its cookie database
  while it's open. Firefox doesn't have this problem.
- **Chrome v127+ App-Bound Encryption** can fail with
  *"Unable to get key for cookie decryption"* even with the latest
  `browser_cookie3`. Workarounds: use `--browser firefox`, or run from
  an Administrator PowerShell.
- If neither works, `--browser none` is fine for the vast majority of
  public sites.

## When extraction misses content

The script uses [readability-lxml](https://github.com/buriy/python-readability)
(the algorithm behind Firefox Reader Mode) with structural fallbacks.
It handles most blogs out of the box, but if you see a near-empty
output:

1. Run with `--debug` and open the saved `<slug>.raw.html`. If the
   article text is missing from that file too, the page is rendered by
   JavaScript and this script can't help — you'd need a headless
   browser tool like Playwright.
2. If the text *is* in the raw HTML, find a class wrapping the article
   body in your browser's DevTools and pass it explicitly:

   ```bash
   python scrape.py <url> --selector "div.post-content"
   ```

The console output reports text length and image count after each URL,
which is a quick way to spot extraction problems:

```
  extracted 42894 chars, 12 <img> tags
  downloaded 12 images
```

## Limits

- **JavaScript-rendered sites** won't work. Single-page apps that fetch
  content client-side leave nothing useful in the static HTML.
- **CDN-level IP blocks** aren't solved by this script. Cookies and TLS
  fingerprint mimicry cover most cases, but a hard IP block needs a VPN
  or a different machine.
- **Paywalled or login-required content** is out of scope. The script
  will faithfully send your browser cookies, so if your browser session
  is logged in you'll get whatever your account can see — but don't use
  this to bypass paywalls.
- **Polite by default**: 0.3s delay between image requests, retries on
  transient failures. If you're scraping a lot from one host, leave
  these in place.
