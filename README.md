# Go-FO

**Go**ogle **F**onts **O**ffline — a single-file Python CLI that downloads a Google Fonts stylesheet and everything it references, then rewrites the CSS to point at the local copies.

Useful when you need fonts bundled with your project: air-gapped machines, GDPR-conscious deployments that must not hit `fonts.gstatic.com`, offline demos, or just faster first paint without a third-party round trip.

```
Fetching stylesheets
  ✓ woff2 from fonts.googleapis.com - 12 reference(s)

Downloading 12 font file(s)
  ✓ open-sans-400-normal-latin.woff2 - 17.6 KB
  ✓ open-sans-400-normal-cyrillic.woff2 - 11.2 KB
  ████████████░░░░░░░░░░░░ 7/12 open-sans-700-italic-greek.woff2

✓ 12 font file(s) (184.3 KB) -> fonts/
✓ stylesheet -> fonts/fonts.css
```

## Features

- **No dependencies.** Standard library only — `urllib`, `re`, `argparse`. Nothing to `pip install`.
- **Readable filenames.** Files are named `family-weight-style-subset.ext` instead of Google's opaque hashes.
- **Subset aware.** Handles the `css2` endpoint, keeps `unicode-range` intact, and gives each subset its own file.
- **Format selection.** Ask for `woff2`, `woff`, `ttf`, `eot`, or `svg`; Go-FO sends the matching User-Agent so Google serves the right stylesheet.
- **Subset filtering.** Keep only the scripts you actually ship. Dropping cyrillic, greek and vietnamese from a multi-script family often halves the payload.
- **`font-display` control.** Set `swap` (or any other value) on every rule without having to remember `&display=swap` in the URL.
- **Idempotent.** Files already on disk are left alone, so re-running inside a build script costs nothing. `--force` overrides.
- **Fails soft.** If a font can't be downloaded, that rule keeps its remote URL so your page still renders.
- **Nice terminal output.** Spinner and progress bar when attached to a TTY, plain text when piped or run in CI.

## Requirements

Python 3.8 or newer. That's it.

## Install

```bash
git clone https://github.com/wachirawitc/GoFO.git
cd GoFO
python Go-FO.py --help
```

Or drop `Go-FO.py` anywhere on your `PATH`:

```bash
chmod +x Go-FO.py
mv Go-FO.py ~/.local/bin/go-fo
go-fo "https://fonts.googleapis.com/css2?family=Inter:wght@400;700"
```

## Usage

```bash
python Go-FO.py [options] <stylesheet-url> [<stylesheet-url> ...]
```

Grab the URL from the **Get embed code** panel on [fonts.google.com](https://fonts.google.com) — it's the `href` of the `<link>` tag. Quote it, because it contains `&` and `;`.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--out-dir DIR` | `fonts` | Directory for the font files and the stylesheet. Created if missing. |
| `--out-css NAME` | `fonts.css` | Filename of the generated stylesheet, written inside `--out-dir`. |
| `--formats LIST` | `woff2` | Comma-separated formats to request: `woff2`, `woff`, `ttf`, `eot`, `svg`. |
| `--subset LIST` | all | Comma-separated subsets to keep, e.g. `latin,latin-ext,thai`. |
| `--font-display VALUE` | unset | Force `font-display` on every rule: `auto`, `block`, `swap`, `fallback`, `optional`. |
| `--force` | off | Re-download files that are already in the output directory. |
| `--plain` | off | Disable the spinner and progress bar. |

The argument style of the original `goofoffline` tool (`outDir=…`, `outCss=…`, `formats=…`) also works, so existing scripts can be pointed at Go-FO unchanged.

### Examples

Basic — one family, modern browsers:

```bash
python Go-FO.py "https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;700"
```

Several families at once, into a custom directory:

```bash
python Go-FO.py --out-dir assets/fonts \
  "https://fonts.googleapis.com/css2?family=Inter:wght@400..700" \
  "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500"
```

Latin and Thai only, with `font-display: swap` forced on every rule:

```bash
python Go-FO.py --subset latin,thai --font-display swap \
  "https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;700"
```

Legacy browser support via the older `/css` endpoint:

```bash
python Go-FO.py --formats woff2,woff,ttf \
  "https://fonts.googleapis.com/css?family=Ubuntu:300,400,700"
```

Then reference the stylesheet from your HTML:

```html
<link rel="stylesheet" href="fonts/fonts.css">
```

### Output layout

```
fonts/
├── fonts.css
├── open-sans-400-normal-latin.woff2
├── open-sans-400-normal-cyrillic.woff2
├── open-sans-700-normal-latin.woff2
└── ...
```

The stylesheet lives alongside the fonts, so every `url()` inside it is a bare filename. Move the whole directory anywhere and it keeps working.

## How it works

1. **Fetch.** Google Fonts inspects the `User-Agent` and returns a stylesheet tailored to what that browser supports. Go-FO sends one request per requested format, using an agent string known to trigger it.
2. **Plan.** Every `@font-face` block is parsed for `font-family`, `font-style`, `font-weight`, and the subset comment Google emits (`/* latin */`), which together become the local filename. No network calls happen in this phase, so the total download count is known before the progress bar starts.
3. **Download.** Each unique URL is fetched once, even when several stylesheets reference it.
4. **Rewrite.** The original CSS is reproduced verbatim except for the edits you asked for: `url()` values, an optional `font-display` declaration, and any `@font-face` block removed by `--subset`. Query strings and fragments are preserved — `?#iefix` for IE8 and `#FontName` for SVG fonts — so legacy formats keep working.

## Notes and limitations

- **The `css2` endpoint mostly serves woff2.** Modern Google Fonts largely ignores User-Agent sniffing on `/css2`, so `--formats woff2,woff,ttf` may return the same woff2 stylesheet three times. For older formats use the legacy `/css` endpoint, and note that some newer families have no `ttf`/`eot`/`svg` build at all. woff2 is supported by every browser released since about 2016, so the default is the right choice for almost everyone.
- **Don't delete `unicode-range`.** Google splits a family into per-script files (latin, cyrillic, greek…) and uses `unicode-range` to tell the browser which file to fetch for the characters actually on the page. Go-FO preserves those declarations. If you strip them, the browser treats every file as covering everything, picks the first match, and some scripts silently fall back to a system font. Thai, for example, lives at `U+0E01–0E5B` and ships as its own subset in families like Noto Sans Thai and Sarabun.
- **Subset names are matched exactly.** `latin` and `latin-ext` are two different subsets; pass both if you want both. Blocks Google leaves unlabelled are always kept, so `--subset` never empties out a stylesheet from the legacy `/css` endpoint. If a filter matches nothing, Go-FO prints the subsets the stylesheet actually offers.
- **Fonts have licenses.** Nearly all Google Fonts are under the SIL Open Font License or Apache 2.0, which permit redistribution, but check the individual family before shipping it. Go-FO downloads the font binaries only, not the accompanying license files.
- **Variable fonts** are downloaded as-is. The generated CSS carries whatever `font-variation-settings` and axis ranges Google returned.

## Credits

Inspired by [makovich/google-fonts-offline](https://github.com/makovich/google-fonts-offline), a Node.js tool with the same purpose. Go-FO is an independent implementation written from scratch against the observable behaviour of the Google Fonts API, not a port of that codebase.

## License

MIT — see [LICENSE](LICENSE).
