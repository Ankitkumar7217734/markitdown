"""Hindi-aware PDF to Markdown conversion.

Plugs into MarkItDown as a higher-priority PDF converter. It only claims a PDF
when that PDF actually needs Hindi handling, so English documents keep going
through MarkItDown's built-in converter untouched.

Three problems it solves, in the order they bite:

1. Legacy glyph fonts (Kruti Dev, DevLys, Walkman Chanakya, ...). The bytes in
   the PDF are Latin; Devanagari only appears because of the font. Every
   extractor returns text like "Hkkjr esa" for "भारत में". Fixed by decoding
   each font run with the matching legacy table.
2. Shattered digraphs. MarkItDown's built-in converter prefers pdfplumber's
   word reconstruction, which re-derives spaces from glyph geometry. Legacy
   Devanagari fonts draw matras with negative advances, so pdfplumber splits
   "tks" into "tk s" and the decoder can no longer see the "ks" -> "ो" digraph.
   Fixed by going straight to pdfminer and keeping content-stream order.
3. Image-only pages. Scanned Hindi has no text layer at all, so it needs OCR.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from hinlegacy import decode as _legacy_decode
from markitdown import DocumentConverter, DocumentConverterResult
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

# word_margin/char_margin matter more than usual here: legacy Devanagari fonts
# position combining glyphs with negative advances, and looser values make
# pdfminer inject spaces mid-syllable that break the legacy digraphs.
_LAPARAMS = LAParams(word_margin=0.1, char_margin=2.0, line_margin=0.5)

# Substrings of embedded font names that mean "the bytes are not Unicode".
_LEGACY_FONT_HINTS = (
    "krutidev", "kruti", "devlys", "chanakya", "shusha", "shree", "apsdv",
    "aps-dv", "dvtt", "dvot", "dvb", "yogesh", "agra", "richa", "kundli",
    "millennium", "sanskrit99", "amarujala", "jagran", "naidunia",
)

# hinlegacy ships one table per legacy font, but publishers mix them freely
# (volumeH1.1.pdf tags headings as WalkmanChanakya901Bold while the bytes are
# Kruti Dev). So the font name only decides *whether* to decode; which table to
# use is settled by scoring the decoded text. Ordered best-general-purpose
# first, because _best_table only switches on a decisive win.
_CANDIDATE_TABLES = ("devlys_010", "walkman_chanakya_905", "krutidev_010")

# A later table must beat the incumbent by this much to be chosen. Close scores
# mean the tables agree on the prose and differ only on punctuation, where the
# leading table is the more reliable one.
_TABLE_SWITCH_MARGIN = 1.10

# High-frequency Hindi function words. A correct decode produces many of these;
# a wrong table produces almost none, which makes this a sharp check on whether
# decoding helped at all.
_HINDI_MARKERS = (
    "है", "हैं", "और", "का", "की", "के", "को", "में", "से", "पर", "यह", "वह",
    "नहीं", "कि", "एक", "हो", "था", "थे", "थी", "गया", "किया", "लिए", "साथ",
    "भी", "तो", "ने", "या", "इस", "उस", "जो", "कर", "होता", "अपने", "तथा",
)
_MARKER_RE = re.compile("|".join(rf"\b{w}\b" for w in _HINDI_MARKERS))

# Sequences Unicode Devanagari cannot legally contain. The wrong legacy table
# mangles reph and vowel signs into these, so counting them separates tables
# that the function-word count alone rates almost equally.
_VOWEL_SIGNS = "\u093E-\u094C\u0955-\u0957\u0962\u0963"
_INVALID = re.compile(
    "\u094D{2,}"                                        # stacked viramas
    rf"|\u094D[{_VOWEL_SIGNS}]"                         # virama then vowel sign
    rf"|[{_VOWEL_SIGNS}]{{2,}}"                         # stacked vowel signs
    rf"|(?:^|[\s\u0964\u0965])[{_VOWEL_SIGNS}\u094D]"   # word-initial mark
)

# pdfminer emits this for glyphs the font gives no Unicode mapping for.
# ponytail: dropped rather than recovered. Recovering them means reading the
# font's own encoding table; no sample here needs it (volumeH1.1.pdf has none).
_UNMAPPED_GLYPH = re.compile(r"\(cid:\d+\)")

_CONSONANT = r"[\u0915-\u0939\u0958-\u095F\u0978-\u097F]"
_CLUSTER = rf"{_CONSONANT}\u093C?(?:\u094D{_CONSONANT}\u093C?)*"
# Visual order puts the "i" matra to the left of its cluster; Unicode wants it
# after. Only applied to runs that look visually ordered (see _looks_visual).
_PREBASE_I = re.compile(rf"\u093F({_CLUSTER})")
_MATRAS = "\u093E-\u094D\u0955-\u0957\u0962\u0963"
_WORD_INITIAL_MATRA = re.compile(rf"(?:^|[\s\u0964\u0965.,;:!?()\[\]\"'])([{_MATRAS}])")
_DEVANAGARI_WORD = re.compile(r"[\u0900-\u097F]+")
_DUP_MARK = re.compile(rf"([{_MATRAS}\u0900-\u0903])\1+")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

# "hin" alone recognises Devanagari ~6% better but turns the English half of
# bilingual government documents into Devanagari noise, so both stay enabled.
_OCR_LANG = os.environ.get("HINDI_PDF_OCR_LANG", "hin+eng")
# 300 is tesseract's recommended floor; 200 measurably lost Devanagari here.
_OCR_DPI = int(os.environ.get("HINDI_PDF_OCR_DPI", "300"))
# ponytail: hard cap on OCR'd pages. A 300-page scan at ~1s/page would hang the
# request far past any sane HTTP timeout. Raise HINDI_PDF_OCR_MAX_PAGES for
# batch use, or move OCR to a job queue if this ever needs to be unbounded.
_OCR_MAX_PAGES = int(os.environ.get("HINDI_PDF_OCR_MAX_PAGES", "40"))
# Page-break rules mark where pages end, not where sections end; set
# HINDI_PDF_PAGE_MARKERS=0 for continuous prose with no rules at all.
_PAGE_MARKERS = os.environ.get("HINDI_PDF_PAGE_MARKERS", "1").lower() not in {
    "0",
    "false",
    "no",
    "",
}


def _base_font(name: str) -> str:
    """Strip the subset prefix PDFs prepend, e.g. 'AAAAAB+KrutiDev010'."""
    return name.split("+", 1)[-1] if "+" in name else name


def _is_legacy_font(name: str | None) -> bool:
    if not name:
        return False
    flat = re.sub(r"[^a-z0-9]", "", _base_font(name).lower())
    return any(hint.replace("-", "") in flat for hint in _LEGACY_FONT_HINTS)


def _hindi_score(text: str) -> int:
    """Rate a decode: reward real Hindi words, punish impossible Devanagari."""
    return len(_MARKER_RE.findall(text)) * 3 - len(_INVALID.findall(text)) * 2


def _looks_visual(text: str) -> bool:
    """True when dependent vowel signs start words, which Unicode never does.

    Correctly encoded Devanagari cannot begin a word with a matra, so any real
    rate of word-initial matras means the extractor handed back glyph order.
    """
    words = _DEVANAGARI_WORD.findall(text)
    if len(words) < 12:
        return False
    return len(_WORD_INITIAL_MATRA.findall(text)) > len(words) * 0.03


def repair_unicode_devanagari(text: str) -> str:
    """Normalize Unicode Devanagari, reordering matras only if visually ordered."""
    if _looks_visual(text):
        text = _PREBASE_I.sub("\\1\u093F", text)
    # ा + े / ै are separate glyphs in many fonts but single codepoints in Unicode.
    text = text.replace("\u093E\u0947", "\u094B").replace("\u093E\u0948", "\u094C")
    text = _DUP_MARK.sub(r"\1", text)
    return unicodedata.normalize("NFC", text)


# pdfminer starts a new text line when a glyph sits high enough above the
# baseline, and legacy Devanagari fonts raise anusvara and matras. That splits
# words like "हों" into "हो" + a line starting with the bare mark, ~594 times in
# a 108-page book. These marks must rejoin the previous line with no space.
_LEADING_MARKS = re.compile(r"^[\u0900-\u0903\u093A-\u094D\u0951-\u0957\u0962\u0963]+")
_PAGE_NUMBER = re.compile(r"[0-9\u0966-\u096F]{1,4}")
_SENTENCE_END = re.compile(r"[\u0964\u0965.!?:;\u201d\u2019\"')\]]\s*$")
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _running_heads(pages: list[str]) -> set[str]:
    """Lines repeating at the top or bottom of many pages: running heads.

    Detected by counting rather than by a fixed pattern, because the header
    text differs per document and alternates with the page number on recto
    versus verso pages.
    """
    counts: Counter[str] = Counter()
    for text in pages:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for line in lines[:2] + lines[-2:]:
            # Length and letter checks keep body prose and stray combining
            # marks out of the candidate set.
            if 2 <= len(line) <= 80 and _HAS_LETTER.search(line):
                counts[line] += 1
    threshold = max(3, len(pages) * 0.15)
    return {line for line, count in counts.items() if count >= threshold}


def _paragraphs(text: str, running_heads: set[str]) -> list[str]:
    """Turn one page into paragraphs, unwrapping the PDF's hard line breaks.

    pdfminer separates real paragraphs with a blank line, so blank lines are
    trusted as breaks and every other newline is treated as wrapping.
    """
    found: list[str] = []
    current: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if current:
                found.append(" ".join(current))
                current = []
            continue
        if line in running_heads or _PAGE_NUMBER.fullmatch(line):
            continue
        marks = _LEADING_MARKS.match(line)
        if marks and current:
            current[-1] += marks.group(0)
            line = line[marks.end():].lstrip()
            if not line:
                continue
        current.append(line)
    if current:
        found.append(" ".join(current))
    return found


def assemble_markdown(pages: list[str], page_markers: bool = _PAGE_MARKERS) -> str:
    """Join decoded pages into paragraph-shaped Markdown.

    A page's first paragraph is merged into the previous one when that one did
    not end on sentence-final punctuation. A page-break rule is emitted only
    when no paragraph spans the break, so the rule can never split a sentence
    the way an unconditional one per page did.
    """
    heads = _running_heads(pages)
    out: list[str] = []
    for page_index, text in enumerate(pages):
        pending_rule = page_markers and page_index > 0 and bool(out)
        for index, para in enumerate(_paragraphs(text, heads)):
            # First paragraph of a page may continue the previous page.
            continues_previous = index == 0
            # A raised glyph can also push text into its own box mid-page. A
            # combining mark cannot legally start a paragraph, so finding one
            # here proves the break is an artifact, not a real paragraph.
            marks = _LEADING_MARKS.match(para)
            if marks and out:
                out[-1] += marks.group(0)
                para = para[marks.end():].lstrip()
                if not para:
                    continue
                continues_previous = True
            if continues_previous and out and not _SENTENCE_END.search(out[-1]):
                out[-1] += " " + para
                pending_rule = False
            else:
                if pending_rule:
                    out.append("---")
                    pending_rule = False
                out.append(para)
    return "\n\n".join(out).strip() + "\n"


def _best_table(samples: dict[str, str]) -> dict[str, str]:
    """Choose a legacy table per font name by scoring each candidate's output.

    Per-font rather than per-document because one file can mix encodings, and
    scored rather than keyed off the font name because the name lies: this
    corpus tags Kruti Dev bytes as WalkmanChanakya901Bold.
    """
    chosen: dict[str, str] = {}
    for font, sample in samples.items():
        best_table, best_score = _CANDIDATE_TABLES[0], None
        for table in _CANDIDATE_TABLES:
            try:
                score = _hindi_score(_legacy_decode(sample, table))
            except Exception:
                continue
            if best_score is None:
                best_table, best_score = table, score
            elif score > max(best_score * _TABLE_SWITCH_MARGIN, best_score + 1):
                best_table, best_score = table, score
        chosen[font] = best_table
    return chosen


def _page_runs(data: bytes, maxpages: int = 0) -> list[list[tuple[str | None, str]]]:
    """Extract each page as (font name, text) runs, preserving line structure.

    Runs are what make mixed-encoding documents work: a run tagged
    KrutiDev010 gets decoded while the Verdana run beside it is left alone.
    """
    pages: list[list[tuple[str | None, str]]] = []
    for layout in extract_pages(io.BytesIO(data), laparams=_LAPARAMS, maxpages=maxpages):
        runs: list[tuple[str | None, str]] = []
        for element in layout:
            if not isinstance(element, LTTextContainer):
                continue
            for line in element:
                if not isinstance(line, LTTextLine):
                    continue
                current: str | None = None
                buf: list[str] = []
                for char in line:
                    # LTAnno carries pdfminer's inferred spaces and newlines and
                    # has no font; keep it in the run it interrupts.
                    font = _base_font(char.fontname) if isinstance(char, LTChar) else current
                    if font != current and buf:
                        runs.append((current, _UNMAPPED_GLYPH.sub("", "".join(buf))))
                        buf = []
                    current = font
                    buf.append(char.get_text())
                if buf:
                    runs.append((current, _UNMAPPED_GLYPH.sub("", "".join(buf))))
            runs.append((None, "\n"))
        pages.append(runs)
    return pages


def _render_pages_png(data: bytes, indices: list[int]) -> list[bytes]:
    """Rasterise the given pages, sequentially, from a single document handle.

    Sequential on purpose: pdfium is not thread-safe, and rendering these
    concurrently raised inside the worker threads. MarkItDown catches
    converter exceptions and silently falls through to its built-in PDF
    converter, so that crash surfaced as undecoded output rather than an error.
    Rendering is also the cheap half of OCR, so nothing is gained by
    parallelising it.
    """
    try:
        import pypdfium2
    except ImportError:
        return []
    doc = pypdfium2.PdfDocument(data)
    try:
        rendered = []
        for index in indices:
            image = doc[index].render(scale=_OCR_DPI / 72).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            rendered.append(buf.getvalue())
        return rendered
    finally:
        doc.close()


def ocr_available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        langs = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return all(lang in langs.split() for lang in _OCR_LANG.split("+"))


def _tesseract(png: bytes) -> str:
    try:
        done = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", _OCR_LANG],
            input=png, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace")


def convert_pdf(data: bytes) -> str:
    """Convert a Hindi PDF's bytes to Markdown."""
    pages = _page_runs(data)

    # Pool each legacy font's text across the document before picking its table,
    # so the choice is made on plenty of evidence rather than one short heading.
    samples: dict[str, list[str]] = {}
    for runs in pages:
        for font, text in runs:
            if _is_legacy_font(font) and text.strip():
                samples.setdefault(_base_font(font), []).append(text)
    tables = _best_table(
        {font: "".join(chunks)[:4000] for font, chunks in samples.items()}
    )

    rendered: list[str] = []
    needs_ocr: list[int] = []
    for number, runs in enumerate(pages):
        parts = []
        for font, text in runs:
            table = tables.get(_base_font(font)) if _is_legacy_font(font) else None
            if table:
                try:
                    text = _legacy_decode(text, table)
                except Exception:
                    pass
            parts.append(text)
        page_text = "".join(parts)
        if _DEVANAGARI.search(page_text):
            page_text = repair_unicode_devanagari(page_text)
        page_text = re.sub(r"\n{3,}", "\n\n", page_text).strip()
        if page_text:
            rendered.append(page_text)
        else:
            needs_ocr.append(number)
            rendered.append("")

    if needs_ocr and ocr_available():
        budget = needs_ocr[:_OCR_MAX_PAGES]
        # OCR is a bonus on top of the decode, never a reason to lose it: any
        # failure here would otherwise propagate and make MarkItDown fall back
        # to its built-in converter, discarding all the decoded text above.
        try:
            images = _render_pages_png(data, budget)
            with ThreadPoolExecutor(max_workers=4) as pool:
                texts = list(pool.map(_tesseract, images))
            for number, text in zip(budget, texts):
                if _DEVANAGARI.search(text):
                    text = repair_unicode_devanagari(text)
                rendered[number] = re.sub(r"\n{3,}", "\n\n", text).strip()
            skipped = len(needs_ocr) - len(images)
        except Exception:
            skipped = len(needs_ocr)
        if skipped > 0:
            rendered.append(
                f"<!-- {skipped} image-only page(s) not OCR'd; raise "
                f"HINDI_PDF_OCR_MAX_PAGES (currently {_OCR_MAX_PAGES}) to include them. -->"
            )

    return assemble_markdown(rendered)


_GATE_PAGES = 8


def pdf_needs_hindi_handling(data: bytes) -> bool:
    """True when this PDF is legacy-encoded, visually ordered, or a scan.

    MarkItDown calls this before every conversion, so it reads only the first
    few pages; parsing a 300-page book here cost 15s per upload.
    """
    try:
        sample = _page_runs(data, maxpages=_GATE_PAGES)
    except Exception:
        return False
    if not sample:
        return False

    if any(_is_legacy_font(font) for runs in sample for font, _ in runs):
        return True

    text = "".join(text for runs in sample for _, text in runs)
    if _looks_visual(text):
        return True
    # An all-image PDF yields no text at all. The built-in converter returns
    # empty for these, so OCR is strictly better -- but only claim the file if
    # OCR is actually installed, otherwise we would also return nothing.
    return not text.strip() and ocr_available()


class HindiPdfConverter(DocumentConverter):
    """PDF converter for legacy-encoded, visually ordered, or scanned Hindi."""

    def accepts(self, file_stream, stream_info, **kwargs) -> bool:
        extension = (stream_info.extension or "").lower()
        mimetype = (stream_info.mimetype or "").lower()
        if extension != ".pdf" and not mimetype.startswith(
            ("application/pdf", "application/x-pdf")
        ):
            return False
        position = file_stream.tell()
        try:
            return pdf_needs_hindi_handling(file_stream.read())
        finally:
            file_stream.seek(position)

    def convert(self, file_stream, stream_info, **kwargs) -> DocumentConverterResult:
        return DocumentConverterResult(markdown=convert_pdf(file_stream.read()))


def register(md) -> None:
    """Register this converter ahead of MarkItDown's built-in PDF converter."""
    md.register_converter(HindiPdfConverter(), priority=-10.0)


if __name__ == "__main__":
    # Runnable check: real Kruti Dev strings lifted from volumeH1.1.pdf, plus
    # the guards that keep English and correct Unicode Hindi from being touched.
    legacy_pairs = [
        ("Hkkjr esa tkfrizFkk", "भारत में जातिप्रथा"),
        ("lajpuk] mRifÙk vkSj fodkl", "संरचना, उत्पत्ति और विकास"),
        ("9 ebZ] 1916 dks", "9 मई, 1916 को"),
        ("dksyafc;k ;wfuoflZVh] U;w;kdZ] vejhdk", "कोलंबिया यूनिवर्सिटी, न्यूयार्क, अमरीका"),
        ("MkW- ,-,- xksYMuokbtj xks\"Bh esa", "डॉ. ए.ए. गोल्डनवाइजर गोष्ठी में"),
        ("ckcklkgsc MkW- vEcsMdj laiw.kZ ok³~e;", "बाबासाहेब डॉ. अम्बेडकर संपूर्ण वाङ्मय"),
        ("ij ifBr ys[k", "पर पठित लेख"),
        ("bafM;u ,aVhDosjh", "इंडियन एंटीक्वेरी"),
    ]
    picked = _best_table({"KrutiDev010": " ".join(s for s, _ in legacy_pairs)})["KrutiDev010"]
    assert picked == "devlys_010", f"expected devlys_010 for Kruti Dev bytes, got {picked}"
    for source, expected in legacy_pairs:
        got = repair_unicode_devanagari(_legacy_decode(source, picked))
        assert got == expected, f"{source!r}\n  got  {got!r}\n  want {expected!r}"

    # The buggy krutidev_010 table must lose: it emits stacked viramas
    # ("संपूर््ण"), which the invalid-sequence penalty is there to catch.
    bad = _legacy_decode("ckcklkgsc MkW- vEcsMdj laiw.kZ ok³~e;", "krutidev_010")
    assert _hindi_score(bad) < _hindi_score(
        _legacy_decode("ckcklkgsc MkW- vEcsMdj laiw.kZ ok³~e;", "devlys_010")
    ), "penalty failed to demote the table that produces invalid Devanagari"

    # Latin font runs must never be handed to the legacy decoder.
    assert not _is_legacy_font("AAAAAG+TimesNewRomanPSMT")
    assert not _is_legacy_font("Verdana")
    assert not _is_legacy_font(None)
    assert _is_legacy_font("AAAAAB+KrutiDev010")
    assert _is_legacy_font("AAAAAC+WalkmanChanakya901Bold")
    assert _is_legacy_font("AAAAAT+Walkman-Chanakya905Normal")
    assert _is_legacy_font("ZWIFXX+DVOT-YogeshERoll")

    # Correct Unicode Hindi must survive untouched; "किताब" has an "i" matra
    # followed by a consonant, which a blind reorder would corrupt to "कति ब".
    correct = (
        "यह एक किताब है और वह दिल्ली में रहता है। शिक्षा का अधिकार सबको "
        "मिला है, जो इस देश के लिए एक बड़ी बात थी और सरकार ने भी यह माना।"
    )
    assert repair_unicode_devanagari(correct) == correct, repair_unicode_devanagari(correct)
    assert not _looks_visual(correct)

    # Visually ordered text (as extracted from the E-Epic card) is detected.
    visual = (
        "भारत िनवार्चन आयोग नाम अंिकत कमर िपता का नाम राके श बाबू "
        "िदल्ली िशक्षा िवभाग िनयम िकताब िहसाब िजला िसतंबर िमला"
    )
    assert _looks_visual(visual), "should detect visual order"
    assert "िन" not in repair_unicode_devanagari(visual).split()[1]

    # A matra stranded on its own line must rejoin the previous word with no
    # space, and hard-wrapped lines must become one paragraph.
    split_word = "फिर वे कहते हैं कि लोगो\nं ने यह माना\nऔर आगे बढ़े।"
    assert _paragraphs(split_word, set()) == [
        "फिर वे कहते हैं कि लोगों ने यह माना और आगे बढ़े।"
    ], _paragraphs(split_word, set())

    # Running heads and bare page numbers are dropped, and a paragraph broken
    # across a page boundary is rejoined rather than left as two paragraphs.
    book = [
        "भारत में जातिप्रथा\n7\n\nयह पहला वाक्य है और यह\nदूसरी पंक्ति है जो चलती",
        "8\nभारत में जातिप्रथा\n\nरहती है और यहां समाप्त होती है।",
        "भारत में जातिप्रथा\n9\n\nअब एक नया अनुच्छेद यहां से शुरू होता है।",
    ]
    joined = assemble_markdown(book, page_markers=False)
    assert "भारत में जातिप्रथा" not in joined, joined
    assert "\n7" not in joined and "\n8" not in joined, joined
    assert "जो चलती रहती है" in joined, joined
    assert len(joined.strip().split("\n\n")) == 2, joined

    # A real sentence end must still start a new paragraph.
    assert (
        len(
            assemble_markdown(
                ["पहला वाक्य।", "दूसरा वाक्य।"], page_markers=False
            ).strip().split("\n\n")
        )
        == 2
    )

    # Page rules go only where no paragraph spans the break: pages 1-2 share a
    # sentence, so the rule belongs before page 3 and nowhere else.
    ruled = assemble_markdown(book, page_markers=True)
    assert ruled.count("\n---\n") == 1, ruled
    assert "चलती रहती है" in ruled.split("---")[0], ruled
    assert "अब एक नया अनुच्छेद" in ruled.split("---")[1], ruled
    # A rule must never be the first or last block, nor sit next to another.
    blocks = [b for b in ruled.strip().split("\n\n") if b]
    assert blocks[0] != "---" and blocks[-1] != "---", blocks
    assert "---" not in assemble_markdown(["एकल पृष्ठ।"], page_markers=True)

    # A mark pushed into its own block mid-page still rejoins, and no output
    # paragraph may ever begin with a combining mark.
    boxed = ["वे कहते हैं कि लोगो\n\nं ने यह भी माना था।"]
    assert assemble_markdown(boxed).strip() == "वे कहते हैं कि लोगों ने यह भी माना था।"
    for para in assemble_markdown(book + boxed).split("\n\n"):
        assert not _LEADING_MARKS.match(para), f"paragraph starts with a mark: {para!r}"

    # End-to-end on a synthetic blank page. This drives the image-only branch,
    # which is where rendering used to run concurrently and raise PdfiumError.
    # convert_pdf must never propagate: MarkItDown turns a raised converter
    # into a silent fallback to its own garbled output.
    import pypdfium2

    blank = pypdfium2.PdfDocument.new()
    blank.new_page(595, 842)
    buf = io.BytesIO()
    blank.save(buf)
    blank.close()
    assert convert_pdf(buf.getvalue()) is not None

    # Rendering several pages at once must stay sequential and succeed.
    many = pypdfium2.PdfDocument.new()
    for _ in range(6):
        many.new_page(595, 842)
    buf = io.BytesIO()
    many.save(buf)
    many.close()
    images = _render_pages_png(buf.getvalue(), list(range(6)))
    assert len(images) == 6 and all(images), "page rendering regressed"

    print(f"all checks passed (selected table: {picked})")
