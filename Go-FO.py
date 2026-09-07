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
import itertools
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

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
URL_RE = re.compile(r"url\(\s*(?P<quote>['\"]?)(?P<url>[^'\")]+)(?P=quote)\s*\)")
EXT_RE = re.compile(r"\.(woff2|woff|ttf|otf|eot|svg)\b", re.IGNORECASE)


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
        self.stream.write("\r\033[2K" + text)
        self.stream.flush()

    def clear(self) -> None:
        if self.animate:
            self.stream.write("\r\033[2K")
            self.stream.flush()

    def line(self, text: str = "") -> None:
        self.clear()
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
# css parsing
# --------------------------------------------------------------------------

def http_get(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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


def url_suffix(url: str) -> str:
    """Keep ?#iefix (IE8) and #FontName (SVG) attached to the rewritten path."""
    if "?" in url:
        return "?" + url.split("?", 1)[1]
    if "#" in url:
        return "#" + url.split("#", 1)[1]
    return ""


def plan_css(css: str, names: dict, order: list) -> list:
    """Assign a local filename to every remote font URL. No network here."""
    replacements = []
    for face in FONT_FACE_RE.finditer(css):
        body = face.group("body")
        subset = face.group("subset") or ""
        family = prop(body, "font-family", "font")
        style = prop(body, "font-style", "normal")
        weight = prop(body, "font-weight", "400")

        for ref in URL_RE.finditer(body):
            url = ref.group("url")
            if url.startswith("data:"):
                continue
            hint = body[ref.end():ref.end() + 80].split(";")[0]
            if url not in names:
                ext = guess_ext(url, hint)
                names[url] = unique_name(
                    build_filename(family, style, weight, subset, ext),
                    set(names.values()),
                )
                order.append(url)
            start = face.start("body") + ref.start()
            end = face.start("body") + ref.end()
            replacements.append((start, end, url, url_suffix(url)))
    return replacements


def apply_replacements(css: str, replacements: list, names: dict, failed: set) -> str:
    out, cursor = [], 0
    for start, end, url, suffix in replacements:
        if url in failed:
            continue  # leave the remote URL in place so the CSS still works
        out.append(css[cursor:start])
        out.append(f"url('{names[url]}{suffix}')")
        cursor = end
    out.append(css[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------

def run(urls, out_dir: str, out_css: str, formats, term: Term) -> int:
    os.makedirs(out_dir, exist_ok=True)
    names: dict[str, str] = {}
    order: list[str] = []
    documents: list[tuple[str, list]] = []
    tick = "\u2713" if term.unicode else "+"
    cross = "\u2717" if term.unicode else "x"

    term.line(term.bold("Fetching stylesheets"))
    for url in urls:
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

            replacements = plan_css(css, names, order)
            if not replacements:
                term.line(f"  {term.yellow('-')} {label} {term.dim('- no @font-face found')}")
                continue

            documents.append((css, replacements))
            term.line(f"  {term.green(tick)} {label} "
                      f"{term.dim(f'- {len(replacements)} reference(s)')}")

    if not order:
        term.line(term.red("Nothing to download. Check the URL and try again."))
        return 1

    term.line()
    term.line(term.bold(f"Downloading {len(order)} font file(s)"))
    bar = ProgressBar(term, len(order))
    bar.show("starting")
    failed: set[str] = set()
    total_bytes = 0

    for url in order:
        filename = names[url]
        bar.show(filename)
        try:
            data = http_get(url.split("#")[0], USER_AGENTS["woff2"])
        except urllib.error.URLError as err:
            failed.add(url)
            bar.advance(filename)
            term.line(f"  {term.red(cross)} {filename} {term.dim(f'- {err}')}")
            bar.show(filename)
            continue

        with open(os.path.join(out_dir, filename), "wb") as fh:
            fh.write(data)
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
    term.line()
    term.line(f"{term.green(tick)} {term.bold(f'{saved} font file(s)')} "
              f"{term.dim(f'({human_size(total_bytes)})')} -> {out_dir}/")
    term.line(f"{term.green(tick)} stylesheet -> {css_path}")
    if failed:
        term.line(term.yellow(
            f"{cross} {len(failed)} download(s) failed; those rules still point "
            f"at the remote URL."))
    return 0


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
    p.add_argument("urls", nargs="+", help="Google Fonts stylesheet URL(s)")
    p.add_argument("--out-dir", default=legacy.get("outDir", "fonts"),
                   help="output directory (default: fonts)")
    p.add_argument("--out-css", default=legacy.get("outCss", "fonts.css"),
                   help="stylesheet filename (default: fonts.css)")
    p.add_argument("--formats", default=legacy.get("formats", "woff2"),
                   help="comma-separated formats, e.g. woff2,woff,ttf")
    p.add_argument("--plain", action="store_true",
                   help="disable spinner and progress bar")
    args = p.parse_args(rest)
    args.formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    return args


def main():
    args = parse_args(sys.argv[1:])
    term = Term(animate=not args.plain)
    try:
        sys.exit(run(args.urls, args.out_dir, args.out_css, args.formats, term))
    except KeyboardInterrupt:
        term.clear()
        term.line("Aborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
