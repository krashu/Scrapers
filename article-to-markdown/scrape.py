"""
Scrape an article URL to local markdown with embedded images.

Design choices:
- One BeautifulSoup parse per page; readability gets the serialized form once
- Single shared HTTP session across all URLs (cookies + connection reuse)
- Idempotent: skips already-scraped articles unless --force
- Per-article image deduplication (same URL referenced twice → one download)
- Retries on transient image failures with exponential backoff
- Content-Type and size guards on the article fetch
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

try:
    from curl_cffi import requests as http
except ImportError:
    import requests as http  # type: ignore

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

try:
    from readability import Document
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False


# ---------- configuration ----------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30
IMG_DELAY = 0.3
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.5  # seconds; doubled each attempt

LAZY_ATTRS = (
    "data-src", "data-lazy-src", "data-original", "data-lazy",
    "data-image", "data-img-url", "data-cmsc-src",
)
JUNK_ATTRS = LAZY_ATTRS + ("srcset", "data-srcset", "sizes", "loading")

PLACEHOLDER_RE = re.compile(
    r"^data:|/blank\.gif|/spacer\.gif|1x1\.(png|gif|jpg)|/placeholder",
    re.IGNORECASE,
)

MD_CONVERTER = MarkdownConverter(heading_style="ATX", bullets="-")

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "svg", "avif"}


# ---------- small helpers ----------

def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    last = path.rsplit("/", 1)[-1] or "untitled"
    for ext in (".html", ".htm", ".php", ".aspx"):
        if last.lower().endswith(ext):
            last = last[: -len(ext)]
            break
    return slugify(last)


def is_placeholder(src: str) -> bool:
    return not src or bool(PLACEHOLDER_RE.search(src))


def yaml_quote(s: str) -> str:
    """Safe YAML double-quoted scalar."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def strip_junk_attrs(tag) -> None:
    for attr in JUNK_ATTRS:
        if tag.has_attr(attr):
            del tag[attr]


# ---------- HTTP session ----------

def make_session(browser: str | None):
    try:
        session = http.Session(impersonate="chrome124")
    except TypeError:
        session = http.Session()
    session.headers.update(HEADERS)

    if browser and browser_cookie3:
        try:
            fn = getattr(browser_cookie3, browser, None)
            if fn is None:
                print(f"! unknown browser: {browser}")
                return session
            jar = fn()
            count = 0
            for c in jar:
                session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
                count += 1
            if count:
                print(f"loaded {count} cookies from {browser}")
        except Exception as e:
            print(f"! cookie load failed: {e}")
    return session


def fetch_html(session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ! fetch failed: {e}")
        return None

    ct = resp.headers.get("content-type", "").lower()
    if ct and not any(t in ct for t in ("text/html", "application/xhtml", "text/xml")):
        print(f"  ! unexpected content-type: {ct}")
        return None
    if len(resp.content) > MAX_PAGE_BYTES:
        print(f"  ! page too large ({len(resp.content)} bytes)")
        return None
    return resp.text


def fetch_image(session, url: str):
    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = session.get(url, timeout=TIMEOUT,
                               headers={"Referer": url})
            resp.raise_for_status()
            if len(resp.content) > MAX_IMAGE_BYTES:
                raise ValueError(f"image too large ({len(resp.content)} bytes)")
            return resp
        except Exception as e:
            last_exc = e
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


# ---------- HTML pre-processing ----------

def largest_srcset_url(srcset: str) -> str | None:
    best_url, best_score = None, -1.0
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        toks = part.rsplit(None, 1)
        if len(toks) == 2 and re.match(r"^\d+(\.\d+)?[wx]$", toks[1]):
            url = toks[0]
            score = float(re.match(r"(\d+(?:\.\d+)?)", toks[1]).group(1))
        else:
            url, score = part, 0.0
        if score > best_score:
            best_url, best_score = url, score
    return best_url


def unwrap_noscript(soup) -> None:
    """Some sites tuck the real <img> inside <noscript> for crawlers."""
    for ns in list(soup.find_all("noscript")):
        try:
            inner = BeautifulSoup(ns.decode_contents(), "html.parser")
            ns.replace_with(inner)
        except Exception:
            pass


def resolve_lazy_images(soup) -> None:
    """Mutate the soup so every <img> has a real src in plain `src`."""
    unwrap_noscript(soup)

    for img in soup.find_all("img"):
        if not is_placeholder(img.get("src", "")):
            continue
        new_src = None
        for attr in LAZY_ATTRS:
            val = img.get(attr)
            if val and not is_placeholder(val):
                new_src = val
                break
        if not new_src:
            for attr in ("srcset", "data-srcset"):
                ss = img.get(attr)
                if ss:
                    cand = largest_srcset_url(ss)
                    if cand and not is_placeholder(cand):
                        new_src = cand
                        break
        if new_src:
            img["src"] = new_src

    for picture in soup.find_all("picture"):
        img = picture.find("img")
        if not img or not is_placeholder(img.get("src", "")):
            continue
        for source in picture.find_all("source"):
            ss = source.get("srcset") or source.get("data-srcset")
            if ss:
                cand = largest_srcset_url(ss)
                if cand:
                    img["src"] = cand
                    break


# ---------- title + content extraction ----------

def extract_title(soup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    if soup.title:
        return soup.title.get_text(strip=True)
    return "untitled"


def clean_node(node):
    for tag in node.find_all(["script", "style", "nav", "aside",
                              "footer", "form", "iframe"]):
        tag.decompose()
    return node


def _score(node) -> tuple[int, int]:
    return (len(node.find_all("img")), len(node.get_text(strip=True)))


def extract_content(soup, html: str, selector: str | None):
    """Find the article body. Order: explicit selector → readability → structural."""
    if selector:
        node = soup.select_one(selector)
        if node:
            return clean_node(node)
        print(f"  ! selector {selector!r} not found, falling back")

    readability_node = None
    if HAS_READABILITY:
        try:
            doc = Document(html)
            readability_node = BeautifulSoup(
                doc.summary(html_partial=True), "html.parser"
            )
        except Exception as e:
            print(f"  ! readability failed: {e}")

    if readability_node and _score(readability_node)[0] > 0:
        return clean_node(readability_node)

    candidates: list[tuple[int, int, object]] = []
    for sel in ("article", "main", "[role='main']",
                "div.post-content", "div.entry-content",
                "div.article-body", "div.post-body", "div.blog-post",
                "div[class*='content']", "div[class*='article']"):
        for node in soup.select(sel):
            imgs, text = _score(node)
            if imgs > 0 and text > 500:
                candidates.append((imgs, text, node))

    if candidates:
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        imgs, text, chosen = candidates[0]
        print(f"  · structural container: {imgs} imgs, {text} chars")
        return clean_node(chosen)

    if readability_node and _score(readability_node)[1] > 0:
        print("  · readability (text only, no images)")
        return clean_node(readability_node)

    divs = [d for d in soup.find_all("div") if d.get_text(strip=True)]
    if divs:
        return clean_node(max(divs, key=lambda n: len(n.get_text())))
    return clean_node(soup.find("body") or soup)


# ---------- image localization ----------

def image_ext(url: str, content_type: str) -> str:
    name = Path(urlparse(url).path).name
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower().split("?")[0]
        if ext in IMAGE_EXTS:
            return "jpg" if ext == "jpeg" else ext
    ct = content_type.lower()
    for token, ext in (("png", "png"), ("webp", "webp"), ("gif", "gif"),
                       ("svg", "svg"), ("avif", "avif"), ("jpeg", "jpg")):
        if token in ct:
            return ext
    return "jpg"


def localize_images(node, base_url: str, images_dir: Path,
                    slug: str, session) -> tuple[int, int]:
    """Returns (downloaded, failed). Dedupes by absolute URL within the article."""
    seen: dict[str, str] = {}
    counter = 1
    failed = 0

    images_dir.mkdir(parents=True, exist_ok=True)

    for img in node.find_all("img"):
        src = img.get("src")
        if is_placeholder(src):
            img.decompose()
            continue
        absolute = urljoin(base_url, src)

        if absolute in seen:
            img["src"] = f"images/{seen[absolute]}"
            strip_junk_attrs(img)
            continue

        try:
            resp = fetch_image(session, absolute)
        except Exception as e:
            print(f"    ! image failed: {absolute} ({e})")
            failed += 1
            continue

        ext = image_ext(absolute, resp.headers.get("content-type", ""))
        filename = f"{slug}-{counter:02d}.{ext}"
        (images_dir / filename).write_bytes(resp.content)
        img["src"] = f"images/{filename}"
        strip_junk_attrs(img)
        seen[absolute] = filename
        counter += 1
        time.sleep(IMG_DELAY)

    return len(seen), failed


# ---------- main per-URL flow ----------

def scrape(url: str, output_dir: Path, session, selector: str | None,
           debug: bool, force: bool) -> None:
    print(f"\n→ {url}")
    slug = slug_from_url(url)
    md_path = output_dir / f"{slug}.md"

    if md_path.exists() and not force:
        print(f"  · skip (exists): {md_path.name}    [--force to re-scrape]")
        return

    html = fetch_html(session, url)
    if html is None:
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    if debug:
        (output_dir / f"{slug}.raw.html").write_text(html, encoding="utf-8")
        print("  · raw HTML saved")

    soup = BeautifulSoup(html, "html.parser")
    resolve_lazy_images(soup)
    title = extract_title(soup)
    print(f"  title: {title}")
    print(f"  slug:  {slug}")

    # readability needs HTML; serialize the (now-fixed) soup once
    fixed_html = str(soup)
    node = extract_content(soup, fixed_html, selector)

    text_len = len(node.get_text(strip=True))
    img_count = len(node.find_all("img"))
    print(f"  extracted {text_len} chars, {img_count} <img> tags")

    saved, failed = localize_images(node, url, output_dir / "images", slug, session)
    summary = f"  downloaded {saved} images"
    if failed:
        summary += f" ({failed} failed)"
    print(summary)

    # Avoid duplicate H1: only add our own if the article body has none
    has_own_h1 = node.find("h1") is not None
    body_md = MD_CONVERTER.convert_soup(node)
    body_md = re.sub(r"\n{3,}", "\n\n", body_md).strip()

    frontmatter = (
        "---\n"
        f"title: {yaml_quote(title)}\n"
        f"source: {url}\n"
        f"scraped: {time.strftime('%Y-%m-%d')}\n"
        "---\n\n"
    )
    heading = "" if has_own_h1 else f"# {title}\n\n"
    md_path.write_text(
        frontmatter + heading + f"[Original article]({url})\n\n" + body_md,
        encoding="utf-8",
    )
    print(f"  ✓ {md_path}")


# ---------- CLI ----------

def main() -> None:
    p = argparse.ArgumentParser(description="Scrape an article URL to markdown")
    p.add_argument("urls", nargs="*", help="article URL(s)")
    p.add_argument("--urls-file", type=Path, default=None,
                   help="text file with URLs, one per line (# comments allowed)")
    p.add_argument("--output", type=Path, default=Path("./articles"))
    p.add_argument("--browser", default="chrome",
                   help="cookie source (chrome/firefox/edge/...); 'none' to skip")
    p.add_argument("--selector", default=None,
                   help="CSS selector override for article body")
    p.add_argument("--force", action="store_true",
                   help="re-scrape URLs whose markdown already exists")
    p.add_argument("--debug", action="store_true",
                   help="save raw HTML alongside the markdown")
    args = p.parse_args()

    if not HAS_READABILITY:
        print("note: readability-lxml not installed; using heuristic extraction.")
        print("      install: pip install readability-lxml lxml_html_clean\n")

    browser = None if args.browser.lower() == "none" else args.browser.lower()
    if browser and browser_cookie3 is None:
        print("note: browser_cookie3 not installed; running without cookies.\n")
        browser = None

    urls = list(args.urls)
    if args.urls_file:
        try:
            for ln in args.urls_file.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    urls.append(ln)
        except Exception as e:
            p.error(f"could not read {args.urls_file}: {e}")

    if not urls:
        p.error("provide at least one URL or use --urls-file")

    session = make_session(browser)  # one session for the whole batch
    print(f"queued {len(urls)} URL(s)\n")

    for url in urls:
        try:
            scrape(url, args.output, session, args.selector, args.debug, args.force)
        except KeyboardInterrupt:
            print("\ninterrupted")
            break


if __name__ == "__main__":
    main()
