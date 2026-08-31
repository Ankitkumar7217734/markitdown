# File to Markdown Web App

Converts uploaded files to Markdown with MarkItDown, plus a Hindi-aware PDF
path for documents MarkItDown alone cannot read. PDFs, Word, Excel, PowerPoint,
HTML, images, and other formats MarkItDown supports are accepted.

## Install

```bash
pip install -r requirements.txt
brew install tesseract tesseract-lang        # optional, for scanned pages
```

## Run

```bash
python app.py
```

Open http://localhost:5001, upload a file, download the `.md`.

Port 5001 is used locally because macOS AirPlay Receiver occupies 5000. On
Render the process binds to `$PORT` instead.

## Deploy on Render

The repo is set up for a Docker web service so Tesseract (Hindi + English)
is in the image.

1. Push this repository to GitHub (already done if you cloned it).
2. In [Render](https://dashboard.render.com), **New → Blueprint**, connect
   this repo. Render reads `render.yaml`.
3. Or **New → Web Service**, connect the repo, and set:
   - Runtime: **Docker**
   - Dockerfile path: `Dockerfile`
   - Health check path: `/health` (not `/` — a convert on `/` would fail the
     health check, Render would kill the service, and the URL would return
     `Not Found`)
4. Set `SECRET_KEY` in the dashboard if the blueprint did not generate one.

Upload starts a background job and the tab waits on `/jobs/…` until the
Markdown file downloads. That keeps Render's health check alive while a long
Hindi PDF is converting. Free instances are 0.1 CPU / 512 MB and spin down
when idle; the next request waits about a minute for a cold start.

If the public URL shows a black **Not Found** page (`x-render-routing:
no-server`), open the Render dashboard and **Manual Deploy → Deploy latest
commit**. The service was likely killed after a convert exhausted memory.

Local check of the same stack:

```bash
docker build -t markitdown .
docker run --rm -p 5001:10000 -e PORT=10000 markitdown
```

## Hindi support

`hindi_pdf.py` registers a PDF converter ahead of MarkItDown's built-in one. It
claims a PDF only when that PDF needs Hindi handling, so English documents keep
going through the built-in converter unchanged.

It handles three separate failure modes:

**Legacy glyph fonts.** Most pre-Unicode Hindi publishing (Kruti Dev, DevLys,
Walkman Chanakya, Shusha, APS, DV-TT) stores Devanagari as Latin bytes. Every
extractor returns `Hkkjr esa` for `भारत में`. Each font run is decoded with the
matching legacy table, so Latin text sitting beside Hindi is left alone.

The table is chosen per font by scoring the decoded text, not by trusting the
font name — real files mislabel their encoding. Scoring rewards common Hindi
words and penalises sequences Unicode Devanagari cannot contain, such as
stacked viramas.

**Shattered digraphs.** MarkItDown's built-in converter prefers pdfplumber,
which re-derives spaces from glyph geometry. Legacy Devanagari fonts draw
matras with negative advances, so pdfplumber splits `tks` into `tk s` and the
`ks` → `ो` digraph is lost. This path uses pdfminer and keeps content-stream
order.

**Scanned pages.** Pages with no text layer are rendered and passed to
tesseract. Requires `tesseract` with the `hin` language data; without it those
pages are skipped rather than returned empty.

OCR is a fallback rather than the primary engine because it was measured
against the decode path on the same legacy-font page: both scored identically
(198 Hindi markers, zero illegal Devanagari sequences), but decode ran at
0.04s/page against tesseract's 2.88s/page — 5s versus 311s for a 108-page
book — and tesseract additionally injected stray ZWNJ joiners that decode
does not produce.

Output is then reflowed into paragraphs:

- pdfminer starts a new text line whenever a glyph sits high above the
  baseline, and these fonts raise anusvara and matras. That split words across
  line breaks and left the bare mark stranded (594 times in a 108-page book).
  A combining mark can never begin a line or paragraph, so any that does is
  rejoined to the preceding word.
- Running heads and page numbers are dropped. They are found by counting which
  short lines recur at the top or bottom of many pages, not by a fixed pattern,
  because the text differs per document and alternates between recto and verso.
- The PDF's hard line wrapping is undone. Blank lines mark real paragraph
  breaks, so everything else joins into one paragraph.
- A `---` rule is emitted at a page break only when no paragraph spans it, so
  the rule marks where the text actually breaks and can never split a
  sentence. On the 108-page sample this yields 19 rules rather than 107.

Tunable via environment variables: `HINDI_PDF_OCR_LANG` (default `hin+eng`),
`HINDI_PDF_OCR_DPI` (default `300`), `HINDI_PDF_OCR_MAX_PAGES` (default `40`),
`HINDI_PDF_PAGE_MARKERS` (default `1`; set `0` for continuous prose).

## Checks

```bash
python hindi_pdf.py
```

Asserts known Kruti Dev strings decode to the expected Hindi, that Latin fonts
and already-correct Unicode Hindi are never modified, that stranded matras
rejoin their word while real sentence ends still break paragraphs, that a page
rule lands only where no paragraph spans the break, and that page rendering
stays sequential (pdfium is not thread-safe).

## Known limits

- Legacy decoding is character-level, so rare conjuncts and unusual glyph
  positions can still come out wrong. Expect very good, not perfect.
- Reph (`र्`) is not repositioned in already-Unicode PDFs that were extracted in
  visual order, so a word like `निर्वाचन` may remain `निवार्चन`.
- Glyphs a font gives no Unicode mapping for are dropped, not recovered.
- Tables and multi-column layouts are not reconstructed on the Hindi path.
  Reflowing assumes single-column prose and would run columns together.
- Headings are emitted as plain paragraphs, not `#` levels.
- Upload cap is 32 MB. There is no word limit. Text-layer pages are unlimited;
  image-only OCR is capped at 40 pages unless `HINDI_PDF_OCR_MAX_PAGES` is raised.
