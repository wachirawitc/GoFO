#!/usr/bin/env python3
"""
Go-FO.py - Google Fonts Offline: download Google Fonts for local use.

Usage:
    python Go-FO.py "https://fonts.googleapis.com/css?family=Open+Sans"
    python Go-FO.py --out-dir tmp --out-css gf.css \
        "https://fonts.googleapis.com/css2?family=Ubuntu:ital,wght@1,300"

The legacy goofoffline argument style is also accepted:
    python Go-FO.py outDir=tmp outCss=gf.css "https://..."

Every font file is written into out-dir together with a CSS file whose
@font-face rules point at the local copies sitting next to it.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import itertools
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

# Google Fonts sniffs the User-Agent and serves a different CSS per format.
# Note: the /css2 endpoint returns woff2 for almost every agent nowadays;
# the legacy /css endpoint still honours the sniffing for older formats.
USER_AGENTS = {
    "woff2": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "woff": "Mozilla/5.0 (Windows NT 6.3; rv:34.0) Gecko/20100101 Firefox/34.0",
    "ttf": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_6_8) AppleWebKit/534.57.2 "
           "(KHTML, like Gecko) Version/5.1.7 Safari/534.57.2",
    "eot": "Mozilla/5.0 (compatible; MSIE 8.0; Windows NT 6.1; Trident/4.0)",
    "svg": "Mozilla/4.0 (iPad; CPU OS 4_0_1 like Mac OS X) AppleWebKit/534.46 "
           "(KHTML, like Gecko) Version/4.1 Mobile/9A405 Safari/7534.48.3",
}

FONT_FACE_RE = re.compile(
    r"(?:/\*\s*(?P<subset>[^*/]+?)\s*\*/\s*)?"       # subset name comment, if any
    r"@font-face\s*\{(?P<body>[^}]*)\}",
    re.IGNORECASE,
)
IMPORT_RE = re.compile(
    r"@import\s+url\(\s*['\"]?(?P<url>[^'\")]+)['\"]?\s*\)\s*;",
    re.IGNORECASE,
)
URL_RE = re.compile(r"url\(\s*(?P<quote>['\"]?)(?P<url>[^'\")]+)(?P=quote)\s*\)")
EXT_RE = re.compile(r"\.(woff2|woff|ttf|otf|eot|svg)\b", re.IGNORECASE)
FONT_DISPLAY_RE = re.compile(r"font-display\s*:\s*([^;]+)", re.IGNORECASE)
UNNAMED_SUBSET_RE = re.compile(r"^\[\d+\]$")
FONT_DISPLAY_VALUES = ("auto", "block", "swap", "fallback", "optional")
AMP_RE = re.compile(r"&(?:amp;|#0*38;|#[xX]0*26;)")

DEFAULT_RETRIES = 3
DEFAULT_CONCURRENCY = 6
IMPORT_DEPTH_LIMIT = 5


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

class Term:
    """Small wrapper around stdout: colours, transient lines, capability checks."""

    def __init__(self, stream=sys.stdout, animate: bool = True):
        self.stream = stream
        self.tty = bool(getattr(stream, "isatty", lambda: False)()) \
            and os.environ.get("TERM") != "dumb"
        self.animate = animate and self.tty
        self.color = self.tty and "NO_COLOR" not in os.environ
        self.unicode = self._probe_unicode()
        self._lock = threading.Lock()

    def _probe_unicode(self) -> bool:
        encoding = getattr(self.stream, "encoding", None) or "ascii"
        try:
            "\u28cf\u2588\u2713".encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
        return True

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def dim(self, text): return self._paint(text, "2")
    def bold(self, text): return self._paint(text, "1")
    def cyan(self, text): return self._paint(text, "36")
    def green(self, text): return self._paint(text, "32")
    def yellow(self, text): return self._paint(text, "33")
    def red(self, text): return self._paint(text, "31")

    @property
    def width(self) -> int:
        return shutil.get_terminal_size((80, 24)).columns

    def transient(self, text: str) -> None:
        """Draw a line that the next write will overwrite."""
        if not self.animate:
            return
        with self._lock:
            self.stream.write("\r\033[2K" + text)
            self.stream.flush()

    def clear(self) -> None:
        if self.animate:
            with self._lock:
                self.stream.write("\r\033[2K")
                self.stream.flush()

    def line(self, text: str = "") -> None:
        with self._lock:
            if self.animate:
                self.stream.write("\r\033[2K")
            self.stream.write(text + "\n")
            self.stream.flush()


class Spinner:
    """Animated spinner for work of unknown duration."""

    BRAILLE = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
    ASCII = "|/-\\"

    def __init__(self, term: Term, label: str):
        self.term = term
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self.term.animate:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            self.term.line(self.term.dim("  ... " + self.label))
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.term.clear()
        return False

    def _spin(self) -> None:
        frames = self.BRAILLE if self.term.unicode else self.ASCII
        for frame in itertools.cycle(frames):
            if self._stop.is_set():
                return
            self.term.transient(f"  {self.term.cyan(frame)} {self.label}")
            time.sleep(0.08)


class ProgressBar:
    """Determinate progress bar for the download phase."""

    def __init__(self, term: Term, total: int, width: int = 24):
        self.term = term
        self.total = max(total, 1)
        self.width = width
        self.done = 0

    def _bar(self) -> str:
        filled = int(round(self.width * self.done / self.total))
        full, empty = ("\u2588", "\u2591") if self.term.unicode else ("#", "-")
        return full * filled + empty * (self.width - filled)

    def show(self, label: str = "") -> None:
        if not self.term.animate:
            return
        head = f"  {self.term.cyan(self._bar())} {self.done}/{self.total} "
        budget = max(self.term.width - len(self._bar()) - 12, 10)
        self.term.transient(head + self.term.dim(truncate(label, budget)))

    def advance(self, label: str = "") -> None:
        self.done += 1
        self.show(label)

    def finish(self) -> None:
        self.term.clear()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def human_size(num: int) -> str:
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} MB"


# --------------------------------------------------------------------------
# networking
# --------------------------------------------------------------------------

def http_get(url: str, user_agent: str, retries: int = DEFAULT_RETRIES) -> bytes:
    """Fetch *url* with retries and exponential backoff.

    On transient errors the request is retried up to *retries* times with
    a delay of 1 s, 2 s, 4 s, … between attempts.  Permanent HTTP errors
    (4xx) are raised immediately.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if 400 <= err.code < 500:
                raise                       # not retryable
            last_err = err
        except (urllib.error.URLError, OSError) as err:
            last_err = err
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))  # 1 s, 2 s, 4 s, …
    raise last_err  # type: ignore[misc]


# --------------------------------------------------------------------------
# css parsing
# --------------------------------------------------------------------------

def prop(body: str, name: str, default: str = "") -> str:
    """Pull a single declaration out of an @font-face body."""
    m = re.search(rf"{name}\s*:\s*([^;]+)", body, re.IGNORECASE)
    return m.group(1).strip().strip("'\"") if m else default


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "-", text.strip())
    return re.sub(r"-{2,}", "-", text).strip("-").lower() or "font"


def guess_ext(url: str, fmt_hint: str) -> str:
    m = EXT_RE.search(urlparse(url).path)
    if m:
        return m.group(1).lower()
    hint = (fmt_hint or "").lower()
    for name in ("woff2", "woff", "truetype", "opentype", "embedded-opentype", "svg"):
        if name in hint:
            return {"truetype": "ttf", "opentype": "otf",
                    "embedded-opentype": "eot"}.get(name, name)
    return "bin"


def build_filename(family: str, style: str, weight: str, subset: str, ext: str) -> str:
    parts = [slugify(family), slugify(weight or "400"), slugify(style or "normal")]
    if subset:
        parts.append(slugify(subset))
    return "-".join(parts) + "." + ext


def unique_name(name: str, taken) -> str:
    base, dot, suffix = name.rpartition(".")
    candidate, n = name, 1
    while candidate in taken:
        candidate = f"{base}-{n}{dot}{suffix}"
        n += 1
    return candidate


def hashed_name(name: str, data: bytes, length: int = 8) -> str:
    """Insert a short content hash before the file extension.

    ``inter-400-normal-latin.woff2`` becomes
    ``inter-400-normal-latin.a3f2c1b8.woff2``.
    """
    digest = hashlib.sha256(data).hexdigest()[:length]
    base, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}.{digest}"
    return f"{base}.{digest}.{ext}"


def url_suffix(url: str) -> str:
    """Keep ?#iefix (IE8) and #FontName (SVG) attached to the rewritten path."""
    if "?" in url:
        return "?" + url.split("?", 1)[1]
    if "#" in url:
        return "#" + url.split("#", 1)[1]
    return ""


def normalize_url(url: str) -> str:
    """Undo the HTML escaping of URLs copied straight out of a <link> tag.

    Google Fonts embed snippets separate parameters with '&', which becomes
    '&amp;' inside an HTML attribute. Left as-is, the API reads the extra
    families as unknown 'amp;family' parameters and silently ignores them.
    """
    return AMP_RE.sub("&", url.strip())


def resolve_imports(css: str, base_url: str, user_agent: str,
                    term: Term, depth: int = 0) -> str:
    """Recursively fetch ``@import url(...)`` rules and inline them.

    The imported CSS replaces the ``@import`` statement so the rest of the
    pipeline sees a single flat stylesheet.  A *depth* guard prevents
    infinite loops or excessively deep chains.
    """
    if depth >= IMPORT_DEPTH_LIMIT:
        return css

    tick = "\u2713" if term.unicode else "+"
    cross = "\u2717" if term.unicode else "x"

    def _replace(match):
        import_url = match.group("url")
        if not urlparse(import_url).scheme:
            import_url = urljoin(base_url, import_url)
        try:
            imported = http_get(import_url, user_agent).decode("utf-8", "replace")
            term.line(f"  {term.green(tick)} @import {term.dim(import_url)}")
        except urllib.error.URLError as err:
            term.line(f"  {term.red(cross)} @import {term.dim(f'{import_url} - {err}')}")
            return match.group(0)  # keep the original @import on failure
        # Recurse in case the imported sheet has its own @imports.
        return resolve_imports(imported, import_url, user_agent, term, depth + 1)

    return IMPORT_RE.sub(_replace, css)


def plan_css(css: str, names: dict, order: list,
             subsets=None, font_display: str = "", base_url: str = "") -> tuple:
    """Plan every edit for one stylesheet. No network calls happen here.

    Returns (replacements, subsets_seen). A replacement is a tuple
    (start, end, kind, payload) where kind is:
      "url"  - swap a remote URL for a local filename
      "drop" - delete a whole @font-face block (subset filtered out)
      "text" - insert or overwrite a declaration
    """
    replacements: list = []
    seen: list = []

    for face in FONT_FACE_RE.finditer(css):
        body = face.group("body")
        subset = (face.group("subset") or "").strip()
        # css2 sometimes labels variable-font ranges "[0]", "[1]" instead of a
        # script name; those are not subsets a user could filter on.
        named = bool(subset) and not UNNAMED_SUBSET_RE.match(subset)
        if named and subset not in seen:
            seen.append(subset)

        # The filter only applies to blocks Google actually labelled, so an
        # unlabelled stylesheet is never emptied out by accident.
        if subsets and named and subset.lower() not in subsets:
            replacements.append((face.start(), face.end(), "drop", None))
            continue

        family = prop(body, "font-family", "font")
        style = prop(body, "font-style", "normal")
        weight = prop(body, "font-weight", "400")
        base = face.start("body")

        if font_display:
            current = FONT_DISPLAY_RE.search(body)
            if current:
                replacements.append((base + current.start(1),
                                     base + current.end(1),
                                     "text", font_display))
            else:
                replacements.append((base, base, "text",
                                     f"\n  font-display: {font_display};"))

        for ref in URL_RE.finditer(body):
            url = ref.group("url")
            if url.startswith("data:"):
                continue
            # Resolve relative URLs (e.g. ./files/font.woff2) against the
            # stylesheet's own URL so downloads work for any CSS source.
            if base_url and not urlparse(url).scheme:
                url = urljoin(base_url, url)
            hint = body[ref.end():ref.end() + 80].split(";")[0]
            if url not in names:
                ext = guess_ext(url, hint)
                names[url] = unique_name(
                    build_filename(family, style, weight, subset, ext),
                    set(names.values()),
                )
                order.append(url)
            replacements.append((base + ref.start(), base + ref.end(),
                                 "url", (url, url_suffix(url))))

    replacements.sort(key=lambda r: (r[0], r[1]))
    return replacements, seen


def apply_replacements(css: str, replacements: list, names: dict, failed: set) -> str:
    out, cursor = [], 0
    for start, end, kind, payload in replacements:
        if kind == "url":
            url, suffix = payload
            if url in failed:
                continue  # leave the remote URL in place so the CSS still works
            text = f"url('{names[url]}{suffix}')"
        elif kind == "drop":
            text = ""
        else:
            text = payload
        out.append(css[cursor:start])
        out.append(text)
        cursor = end
    out.append(css[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


# --------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------

def _download_one(url: str, dest: str, user_agent: str):
    """Download a single font file. Returns (url, data_bytes, error)."""
    try:
        data = http_get(url.split("#")[0], user_agent)
        with open(dest, "wb") as fh:
            fh.write(data)
        return url, data, None
    except (urllib.error.URLError, OSError) as err:
        return url, None, err


def run(urls, out_dir: str, out_css: str, formats, term: Term,
        subsets=None, font_display: str = "", force: bool = False,
        concurrency: int = DEFAULT_CONCURRENCY,
        hashed: bool = False) -> int:
    os.makedirs(out_dir, exist_ok=True)
    names: dict[str, str] = {}
    order: list[str] = []
    documents: list[tuple[str, list]] = []
    subsets_seen: list = []
    tick = "\u2713" if term.unicode else "+"
    cross = "\u2717" if term.unicode else "x"
    dot = "\u00b7" if term.unicode else "."

    term.line(term.bold("Fetching stylesheets"))
    for raw_url in urls:
        url = normalize_url(raw_url)
        if url != raw_url.strip():
            term.line(f"  {term.yellow('!')} un-escaped &amp; in URL "
                      f"{term.dim('- looks like it was copied from HTML')}")
        host = urlparse(url).netloc or url
        for fmt in formats:
            agent = USER_AGENTS.get(fmt)
            if agent is None:
                term.line(f"  {term.yellow(cross)} unknown format: {fmt}")
                continue

            label = f"{fmt} from {host}"
            with Spinner(term, label):
                try:
                    css = http_get(url, agent).decode("utf-8", "replace")
                    error = None
                except urllib.error.URLError as err:
                    css, error = "", err

            if error is not None:
                term.line(f"  {term.red(cross)} {label} {term.dim(f'- {error}')}")
                continue

            # Inline any @import rules before planning.
            css = resolve_imports(css, url, agent, term)

            replacements, seen = plan_css(css, names, order, subsets, font_display,
                                           base_url=url)
            for name in seen:
                if name not in subsets_seen:
                    subsets_seen.append(name)

            refs = sum(1 for r in replacements if r[2] == "url")
            dropped = sum(1 for r in replacements if r[2] == "drop")
            if not refs:
                reason = ("every @font-face was filtered out"
                          if dropped else "no @font-face found")
                term.line(f"  {term.yellow('-')} {label} {term.dim(f'- {reason}')}")
                continue

            documents.append((css, replacements))
            note = f"- {refs} reference(s)"
            if dropped:
                note += f", {dropped} block(s) filtered"
            term.line(f"  {term.green(tick)} {label} {term.dim(note)}")

    if not order:
        term.line(term.red("Nothing to download. Check the URL and try again."))
        if subsets and subsets_seen:
            term.line(term.dim("Subsets offered by this stylesheet: "
                               + ", ".join(subsets_seen)))
        return 1

    term.line()
    term.line(term.bold(f"Downloading {len(order)} font file(s)"))
    bar = ProgressBar(term, len(order))
    bar.show("starting")
    failed: set[str] = set()
    total_bytes = 0
    reused = 0

    # Separate already-present files from those that need downloading.
    to_download: list[tuple[str, str]] = []  # (url, dest)
    for url in order:
        filename = names[url]
        dest = os.path.join(out_dir, filename)

        # When --hashed, a prior run may have renamed the file to include a
        # hash.  Look for an existing file matching the pattern name.HASH.ext.
        existing_hashed = None
        if hashed and not os.path.exists(dest):
            base, _sep, ext = filename.rpartition(".")
            if _sep:
                pattern = os.path.join(out_dir, f"{base}.*.{ext}")
                matches = glob.glob(pattern)
                if matches:
                    existing_hashed = matches[0]

        if (os.path.exists(dest) or existing_hashed) and not force:
            reused += 1
            reuse_path = existing_hashed or dest
            size = os.path.getsize(reuse_path)
            total_bytes += size
            if hashed:
                if existing_hashed:
                    # Already hashed from a prior run — read its name directly.
                    names[url] = os.path.basename(existing_hashed)
                    filename = names[url]
                else:
                    with open(dest, "rb") as fh:
                        data = fh.read()
                    new_name = hashed_name(filename, data)
                    new_dest = os.path.join(out_dir, new_name)
                    if new_dest != dest:
                        os.replace(dest, new_dest)
                    names[url] = new_name
                    filename = new_name
            bar.advance(filename)
            term.line(f"  {term.dim(dot)} {filename} "
                      f"{term.dim('- already present, skipped')}")
            bar.show(filename)
        else:
            to_download.append((url, dest))

    # Download new files concurrently.
    workers = min(concurrency, len(to_download)) if to_download else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_one, url, dest, USER_AGENTS["woff2"]): url
            for url, dest in to_download
        }
        for future in as_completed(futures):
            url = futures[future]
            filename = names[url]
            _url, data, err = future.result()
            if err is not None:
                failed.add(url)
                bar.advance(filename)
                term.line(f"  {term.red(cross)} {filename} {term.dim(f'- {err}')}")
            else:
                if hashed:
                    new_name = hashed_name(filename, data)
                    new_dest = os.path.join(out_dir, new_name)
                    old_dest = os.path.join(out_dir, filename)
                    if new_dest != old_dest:
                        os.replace(old_dest, new_dest)
                    names[url] = new_name
                    filename = new_name
                total_bytes += len(data)
                bar.advance(filename)
                term.line(f"  {term.green(tick)} {filename} "
                          f"{term.dim(f'- {human_size(len(data))}')}")
            bar.show(filename)

    bar.finish()

    css_path = os.path.join(out_dir, out_css)
    with open(css_path, "w", encoding="utf-8") as fh:
        body = "\n".join(
            apply_replacements(css, reps, names, failed).strip()
            for css, reps in documents
        )
        fh.write(body + "\n")

    saved = len(order) - len(failed)
    detail = human_size(total_bytes)
    if reused:
        detail += f", {reused} reused"
    term.line()
    term.line(f"{term.green(tick)} {term.bold(f'{saved} font file(s)')} "
              f"{term.dim(f'({detail})')} -> {out_dir}/")
    term.line(f"{term.green(tick)} stylesheet -> {css_path}")
    if failed:
        term.line(term.yellow(
            f"{cross} {len(failed)} download(s) failed; those rules still point "
            f"at the remote URL."))
    return 0


def load_urls_from_file(path: str) -> list[str]:
    """Read stylesheet URLs from a file, one per line.

    Blank lines and lines starting with ``#`` are ignored.
    """
    urls: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def parse_args(argv):
    """Accept both --out-dir style and the original outDir=... style."""
    legacy, rest = {}, []
    for arg in argv:
        m = re.fullmatch(r"(outDir|outCss|formats)=(.*)", arg)
        if m:
            legacy[m.group(1)] = m.group(2)
        else:
            rest.append(arg)

    p = argparse.ArgumentParser(
        description="Download Google Fonts for offline use.")
    p.add_argument("urls", nargs="*", help="Google Fonts stylesheet URL(s)")
    p.add_argument("--out-dir", default=legacy.get("outDir", "fonts"),
                   help="output directory (default: fonts)")
    p.add_argument("--out-css", default=legacy.get("outCss", "fonts.css"),
                   help="stylesheet filename (default: fonts.css)")
    p.add_argument("--formats", default=legacy.get("formats", "woff2"),
                   help="comma-separated formats, e.g. woff2,woff,ttf")
    p.add_argument("--font-display", metavar="VALUE", default="",
                   choices=("",) + FONT_DISPLAY_VALUES,
                   help="set font-display in every rule: "
                        + ", ".join(FONT_DISPLAY_VALUES))
    p.add_argument("--subset", default="", metavar="LIST",
                   help="comma-separated subsets to keep, e.g. latin,latin-ext,thai "
                        "(default: keep all)")
    p.add_argument("--force", action="store_true",
                   help="re-download files that are already in the output directory")
    p.add_argument("--plain", action="store_true",
                   help="disable spinner and progress bar")
    p.add_argument("--from-file", metavar="FILE",
                   help="read stylesheet URLs from FILE (one per line, "
                        "# comments and blank lines ignored)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   metavar="N",
                   help=f"number of parallel downloads (default: {DEFAULT_CONCURRENCY})")
    p.add_argument("--hashed", action="store_true",
                   help="append a content hash to each font filename for "
                        "cache-busting (e.g. font-400-normal.a3f2c1b8.woff2)")
    args = p.parse_args(rest)
    args.formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    args.subset = {s.strip().lower() for s in args.subset.split(",") if s.strip()}

    # Merge URLs from --from-file with positional URLs.
    if args.from_file:
        try:
            file_urls = load_urls_from_file(args.from_file)
        except OSError as err:
            p.error(f"cannot read URL file: {err}")
        args.urls = (args.urls or []) + file_urls

    if not args.urls:
        p.error("no URLs given (pass them as arguments or via --from-file)")

    return args


def main():
    args = parse_args(sys.argv[1:])
    term = Term(animate=not args.plain)
    try:
        sys.exit(run(args.urls, args.out_dir, args.out_css, args.formats, term,
                     subsets=args.subset, font_display=args.font_display,
                     force=args.force, concurrency=args.concurrency,
                     hashed=args.hashed))
    except KeyboardInterrupt:
        term.clear()
        term.line("Aborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
