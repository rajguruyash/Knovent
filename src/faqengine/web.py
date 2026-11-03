"""Reading web pages into the knowledge base: one page, or a page and the pages it links to on the same site. Standard library only.

What it does, and what it does not:
  * it reads a page the way a browser would without running JavaScript, so a page that builds itself with scripts looks empty to it (the
    error says so; the page can be saved from a browser and uploaded as a file instead);
  * it identifies itself, obeys the site's robots.txt, waits between pages, and stays on the same site;
  * it never connects to an address on this computer or its network, wherever a link or a redirect points (a page must not be a way
    to read them).
"""
from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

from .sources import read_html, slug

USER_AGENT = "faq-engine-studio/1.0 (reads the pages you point it at)"
MAX_PAGE_BYTES = 5 * 1024 * 1024  # one download
MAX_PAGES = 30  # one request
TIMEOUT = 15.0  # seconds, for each step of a download
PAGE_SECONDS = 30.0  # for a whole download, however slowly it trickles in
CRAWL_SECONDS = 120.0  # for all the pages of one request
DELAY = 0.4  # between two pages of one site
MIN_WORDS = 40  # fewer words than this: the page is empty to a program (JavaScript, a login wall, a picture)
MAX_TEXT_CHARS = 400_000  # of one page
ALLOW_PRIVATE = False  # tests only: lets pages on this computer be read
SKIP_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".json", ".xml", ".zip", ".gz", ".tar", ".mp4", ".mp3", ".avi", ".mov",
                   ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dmg", ".woff", ".woff2", ".ttf", ".rss", ".atom"}
SKIP_SEGMENTS = {"login", "logout", "signin", "signup", "register", "cart", "checkout", "account", "wp-admin", "wp-login.php", "feed"}


class WebError(ValueError):
    """Something a person can understand or fix: shown to them as it is."""


# ---------- never read what is on this computer or its network

def _check_ip(ip: str) -> None:
    if ALLOW_PRIVATE:
        return
    addr = ipaddress.ip_address(ip.split("%")[0])
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        raise WebError("that address is on this computer or its network, which is never read")


class _Http(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        try:
            _check_ip(self.sock.getpeername()[0])  # the address actually connected to, so a name that changes its answer cannot get past this
        except WebError:
            self.close()
            raise


class _Https(http.client.HTTPSConnection):
    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)  # the connection first, checked before anything (not even a TLS hello) is sent
        try:
            _check_ip(self.sock.getpeername()[0])
        except WebError:
            self.close()
            raise
        context = getattr(self, "_context", None) or ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)


class _HttpHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Http, req)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_Https, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise WebError("the page sends the reader somewhere that is not a web page")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HttpHandler, _HttpsHandler, _Redirects)  # no proxies: the real peer is checked


# ---------- addresses

def normalize(url: str) -> str:
    """A clean http(s) address, or a WebError saying what is wrong. "example.com/faq" is taken to mean https://example.com/faq."""
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise WebError("paste the address of a web page")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise WebError("only http and https addresses can be read")
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii")
        port = parts.port
    except (UnicodeError, ValueError):
        raise WebError("that is not a web address") from None
    if not host or "." not in host and not re.fullmatch(r"[0-9a-f:]+", host):
        raise WebError("that is not a web address")
    if parts.username or parts.password:
        raise WebError("addresses with a user name or password in them are not read")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None  # a name: it is checked when it is connected to
    if literal is not None:
        _check_ip(str(literal))  # an address typed as numbers is checked at once
    netloc = f"[{host}]" if ":" in host else host
    if port and not (port == 80 and parts.scheme.lower() == "http" or port == 443 and parts.scheme.lower() == "https"):
        netloc += f":{port}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def _canon(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/') or '/'}{'?' + parts.query if parts.query else ''}"


def _same_site(a: str, b: str) -> bool:
    return urllib.parse.urlsplit(a).netloc.lower().removeprefix("www.") == urllib.parse.urlsplit(b).netloc.lower().removeprefix("www.")


def _worth_following(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return not (any(path.endswith(x) for x in SKIP_EXTENSIONS) or any(seg in SKIP_SEGMENTS for seg in path.split("/")))


# ---------- downloading

def _problem(code: int, host: str) -> str:
    return {401: f"{host} needs a login to show this page", 403: f"{host} does not let automated readers see this page", 404: "that page was not found (check the address)",
            410: "that page is gone", 429: f"{host} says too many requests: try again in a few minutes"}.get(code, f"{host} answered with an error ({code})")


def _decode(body: bytes, charset: str | None) -> str:
    sniffed = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    for enc in (charset, sniffed.group(1).decode("ascii") if sniffed else None, "utf-8"):
        if enc:
            try:
                return body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
    return body.decode("latin-1", errors="replace")


def fetch(url: str, accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8") -> tuple[str, str, str]:
    """(final address, content type, text) of one download."""
    host = urllib.parse.urlsplit(url).hostname or url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as r:
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
                raise WebError(f"that page is larger than {MAX_PAGE_BYTES // 2**20} MB")
            chunks, size, started = [], 0, time.monotonic()
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise WebError(f"that page is larger than {MAX_PAGE_BYTES // 2**20} MB")
                if time.monotonic() - started > PAGE_SECONDS:
                    raise WebError(f"{host} is too slow to read")
                chunks.append(chunk)
            return r.geturl(), (r.headers.get_content_type() or "").lower(), _decode(b"".join(chunks), r.headers.get_content_charset())
    except urllib.error.HTTPError as e:
        raise WebError(_problem(e.code, host)) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise WebError(f"{host} has a security certificate problem, so it was not read") from None
        raise WebError(f"could not reach {host}: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise WebError(f"{host} did not answer in {int(TIMEOUT)} seconds") from None
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise WebError(f"could not read {host}: {type(e).__name__}") from None


def _allowed(url: str, cache: dict) -> bool:
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    robots = cache.get(origin)
    if robots is None:
        robots = urllib.robotparser.RobotFileParser()
        try:
            robots.parse(fetch(origin + "/robots.txt", "text/plain")[2].splitlines())
        except WebError:
            robots.parse([])  # a site without a robots.txt disallows nothing
        cache[origin] = robots
    return robots.can_fetch(USER_AGENT, url)


# ---------- pages

def _clean(lines: list[str]) -> list[str]:
    """Drop what menus and page furniture leave behind: short fragments with no sentence ending and no number ("next |", "Home"). Headings stay,
    and so do short lines that carry a fact (hours, prices, phone numbers)."""
    out = []
    for line in lines:
        line = line.replace("¶", "").strip()
        if line.startswith("# "):
            if line[2:].strip():
                out.append(line)
        elif len(line.split()) >= 4 or line[-1:] in ".!?:;)\"'”’" or any(c.isdigit() for c in line):
            out.append(line)
    return out


@dataclass
class Page:
    url: str  # where it was read, after any redirects
    title: str
    text: str  # headings as "# " lines, paragraphs between blank lines
    links: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())


def read_page(url: str, robots: dict | None = None) -> Page:
    url = normalize(url)
    host = urllib.parse.urlsplit(url).hostname
    if not _allowed(url, robots if robots is not None else {}):
        raise WebError(f"{host}'s robots.txt asks automated readers to stay away from this page. Save the page from your browser and upload it as a file instead")
    final, ctype, body = fetch(url)
    final = normalize(final)
    if ctype in ("text/html", "application/xhtml+xml"):
        page = read_html(body)
        title = " ".join(page.title.split()) or urllib.parse.urlsplit(final).path.strip("/").replace("-", " ") or host
        text = "\n\n".join(_clean(page.body_lines()))[:MAX_TEXT_CHARS]
        links = []
        for href in page.links:
            absolute = urllib.parse.urljoin(final, href.strip())
            if urllib.parse.urlsplit(absolute).scheme.lower() in ("http", "https"):
                links.append(urllib.parse.urldefrag(absolute)[0])
    elif ctype == "text/plain":
        title, text, links = urllib.parse.urlsplit(final).path.strip("/") or host, "\n\n".join(x.strip() for x in re.split(r"\n\s*\n", body) if x.strip())[:MAX_TEXT_CHARS], []
    else:
        raise WebError(f"that link is {ctype or 'not a web page'}, not a web page. Download it and upload it as a file instead")
    out = Page(final, title, text, links)
    if out.words < MIN_WORDS:
        raise WebError(f"{host} showed only {out.words} words of text. The page probably builds itself with JavaScript (this reader cannot run it) or sits behind a login. "
                       "Save the page from your browser (HTML or PDF) and upload it as a file instead")
    return out


def read_site(url: str, pages: int = 1) -> tuple[list[Page], list[str]]:
    """The page at `url` and, when `pages` > 1, the pages it links to on the same site, in the order the links appear, up to `pages` in all.

    Returns (pages, notes). The first page must be readable (its problem is raised); a later page that is not is skipped, with a note."""
    try:
        wanted = max(1, min(int(pages), MAX_PAGES))
    except (TypeError, ValueError):
        raise WebError("pages must be a number from 1 to 30") from None
    start = normalize(url)
    robots: dict = {}
    queue, seen, texts, out, notes = [start], set(), set(), [], []
    began = time.monotonic()
    while queue and len(out) < wanted:
        if time.monotonic() - began > CRAWL_SECONDS:
            notes.append(f"stopped after {int(CRAWL_SECONDS)} seconds with {len(out)} pages")
            break
        nxt = queue.pop(0)
        if _canon(nxt) in seen:
            continue
        seen.add(_canon(nxt))
        try:
            page = read_page(nxt, robots)
        except WebError as e:
            if not out:
                raise
            notes.append(f"{nxt}: {e}")
            continue
        seen.add(_canon(page.url))
        digest = hashlib.sha1(page.text.encode()).hexdigest()
        if digest in texts:
            continue  # the same words under another address
        texts.add(digest)
        out.append(page)
        queue += [x for x in page.links if _same_site(x, start) and _worth_following(x) and _canon(x) not in seen]
        if queue and len(out) < wanted:
            time.sleep(DELAY)
    return out, notes


# ---------- keeping a page in a project

def page_filename(url: str) -> str:
    """The stored file's name: the same address always gives the same name, so adding it again replaces (refreshes) the copy."""
    parts = urllib.parse.urlsplit(url)
    return f"web-{slug(parts.netloc.removeprefix('www.') + parts.path)[:40].strip('-')}-{hashlib.sha1(_canon(url).encode()).hexdigest()[:6]}.html"


def page_html(page: Page) -> bytes:
    """A small, clean HTML copy (no scripts, menus or tracking) that remembers where it came from; sources.py reads it back as passages."""
    esc = lambda t: html.escape(t, quote=True)  # noqa: E731
    body = "".join(f"<h2>{esc(line[2:])}</h2>" if line.startswith("# ") else f"<p>{esc(line)}</p>" for line in page.text.split("\n\n") if line.strip())
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="source-url" content="{esc(page.url)}"><title>{esc(page.title)}</title></head>'
            f"<body><h1>{esc(page.title)}</h1>{body}</body></html>").encode("utf-8")
