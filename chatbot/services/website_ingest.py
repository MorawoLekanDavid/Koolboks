"""Fetches web pages and extracts clean, readable text so they can be ingested
into the knowledge base as regular KBDocument rows — reusing the exact same
draft -> Go Live review workflow a manually uploaded file already goes
through, nothing new for admins to learn or trust blindly."""
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from chatbot.config import log
from chatbot.database import get_db
from chatbot.models import KBDocument

_SKIP_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]
_USER_AGENT = "Mozilla/5.0 (compatible; KoolbuyBot/1.0; +https://koolbuystore.com)"


def _extract_text_from_html(html: str) -> tuple[str, str]:
    """Returns (title, clean_text) from already-fetched HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_SKIP_TAGS):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    text = soup.get_text(separator="\n")
    # HTML->text always leaves a wall of blank lines behind — collapse it down
    # to something worth feeding a prompt.
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    return title, text.strip()


async def fetch_page_text(url: str) -> tuple[str, str]:
    """Returns (title, clean_text) for a single page, or ("", "") on failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
        if resp.status_code != 200:
            log.warning(f"Website ingest: {url} returned {resp.status_code}")
            return "", ""
        return _extract_text_from_html(resp.text)
    except Exception as e:
        log.warning(f"Website ingest failed for {url}: {e}")
        return "", ""


def _upsert_kb_document(url: str, title: str, text: str, created_by: str) -> KBDocument:
    """Saves a page's content as a draft KB document, matching the exact status
    a manual file upload gets. If this URL was already ingested before: a live
    version gets marked pending_trash (removed only when the admin next clicks
    Go Live, so the old content keeps serving until the new draft is reviewed
    and approved — no gap); an un-published draft from a prior sync is just
    replaced outright, since nothing was ever live from it."""
    db = get_db()
    try:
        existing = db.query(KBDocument).filter(KBDocument.name == url).first()
        if existing:
            if existing.status == "live":
                existing.status = "pending_trash"
            elif existing.status == "draft":
                db.delete(existing)
                db.flush()
        doc = KBDocument(
            name=url,
            content=f"{title}\n\n{text}" if title else text,
            file_type="url",
            file_size=len(text),
            status="draft",
            created_by=created_by,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc
    finally:
        db.close()


async def ingest_url(url: str, created_by: str = "web-sync") -> KBDocument:
    """Fetch one page and save/update it as a draft KB document."""
    title, text = await fetch_page_text(url)
    if not text:
        raise ValueError(f"Could not extract any readable content from {url}")
    return _upsert_kb_document(url, title, text, created_by)


def _same_domain_links(base_url: str, html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    links = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"]).split("#")[0]
        parsed = urlparse(full)
        if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
            links.add(full)
    return sorted(links)


async def crawl_site(start_url: str, max_pages: int = 20, created_by: str = "web-sync") -> list:
    """Starting from `start_url`, follows same-domain links breadth-first up to
    `max_pages` pages, ingesting each as a draft KB document. Bounded and
    same-domain by design — this pulls in a business's own site content, not a
    general-purpose web crawler."""
    visited = set()
    queue = [start_url]
    results = []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
                if resp.status_code != 200:
                    continue
                title, text = _extract_text_from_html(resp.text)
                if text:
                    doc = _upsert_kb_document(url, title, text, created_by)
                    results.append({"url": url, "id": doc.id, "title": title})
                for link in _same_domain_links(url, resp.text):
                    if link not in visited and len(visited) + len(queue) < max_pages:
                        queue.append(link)
            except Exception as e:
                log.warning(f"Crawl error at {url}: {e}")
    return results
