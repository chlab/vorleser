#!/usr/bin/env python3
"""
Extract all chapters from an epub and run LLM pause preprocessing on each.

Reads the epub spine order, extracts each chapter as plain text (preserving
paragraph breaks), and writes numbered files into a directory ready for
ebook2audiobook batch conversion.

Usage:
    python3 prepare_book.py <input.epub> [output_dir]

Output directory defaults to ebooks/<book-stem>_chapters/.
Each plain file is named 001_chapter.txt, 002_chapter.txt, etc.

If Ollama is reachable, the pause-processed versions are written into a
separate `paused/` subdirectory (001_chapter_paused.txt, …). Point
ebook2audiobook's --ebooks_dir at that `paused/` subdir so it converts only
the pause-enhanced text — not the plain originals as well.
"""
import re, sys, urllib.request, zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from insert_pauses import MODEL, OLLAMA_URL, Progress, count_blocks, process_text

# ── text extraction ──────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip  = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("p", "h1", "h2", "h3", "h4", "br"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("p", "h1", "h2", "h3", "h4"):
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)

def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.chunks).strip()
    return re.sub(r"\n{3,}", "\n\n", text)

def _ncx_titles(zf: zipfile.ZipFile, opf, opf_dir: str, manifest_all: dict) -> dict:
    """Map content-document stem → chapter title from the NCX table of contents.

    The first navPoint that targets a given file wins, so each chapter gets its
    top-level heading rather than a deeper sub-section anchor (#…) within it.
    """
    NS  = {"opf": "http://www.idpf.org/2007/opf"}
    NCX = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    spine_el = opf.find("opf:spine", NS)
    toc_id   = spine_el.get("toc") if spine_el is not None else None
    ncx_href = manifest_all.get(toc_id) if toc_id else None
    if not ncx_href:
        return {}
    ncx_full = f"{opf_dir}/{ncx_href}" if opf_dir != "." else ncx_href
    titles = {}
    try:
        ncx = ET.fromstring(zf.read(ncx_full))
    except KeyError:
        return {}
    for np in ncx.findall(".//n:navPoint", NCX):
        label   = np.find("n:navLabel/n:text", NCX)
        content = np.find("n:content", NCX)
        if label is None or content is None:
            continue
        stem = Path(content.get("src", "").split("#", 1)[0]).stem
        if stem and stem not in titles:   # first (top-level) navPoint wins
            titles[stem] = (label.text or "").strip()
    return titles

def chapter_subheadings(epub_path: Path) -> dict:
    """Map content-document stem → list of its TOC section headings (the sub
    navPoints that target the same file), in order, EXCLUDING the chapter's own
    top-level title. Used to scaffold chapter summaries so every thread is
    covered."""
    NS  = {"opf": "http://www.idpf.org/2007/opf"}
    NCX = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path  = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
        opf_dir   = str(Path(opf_path).parent)
        opf       = ET.fromstring(zf.read(opf_path))
        manifest_all = {item.get("id"): item.get("href")
                        for item in opf.findall("opf:manifest/opf:item", NS)}
        spine_el = opf.find("opf:spine", NS)
        toc_id   = spine_el.get("toc") if spine_el is not None else None
        ncx_href = manifest_all.get(toc_id) if toc_id else None
        if not ncx_href:
            return {}
        ncx_full = f"{opf_dir}/{ncx_href}" if opf_dir != "." else ncx_href
        try:
            ncx = ET.fromstring(zf.read(ncx_full))
        except KeyError:
            return {}
    sections, seen = {}, set()
    for np in ncx.findall(".//n:navPoint", NCX):
        label   = np.find("n:navLabel/n:text", NCX)
        content = np.find("n:content", NCX)
        if label is None or content is None:
            continue
        stem = Path(content.get("src", "").split("#", 1)[0]).stem
        text = (label.text or "").strip()
        if not stem or not text:
            continue
        if stem not in seen:        # first navPoint for this file is its title
            seen.add(stem)
            sections.setdefault(stem, [])
        else:
            sections.setdefault(stem, []).append(text)
    return sections

def _book_metadata(opf) -> dict:
    """Pull the book title and author from the OPF Dublin Core metadata."""
    DC = "http://purl.org/dc/elements/1.1/"
    title  = next((e.text for e in opf.iter(f"{{{DC}}}title")   if e.text), "")
    author = next((e.text for e in opf.iter(f"{{{DC}}}creator") if e.text), "")
    return {"title": title.strip(), "author": author.strip()}

def _epub_chapters(epub_path: Path) -> tuple[list[tuple[str, str, str]], dict]:
    """Return (chapters, book_meta).

    chapters is a list of (name, text, title) for each spine document with real
    content; title is the chapter's TOC heading (empty if the TOC has none).
    """
    NS = {"opf": "http://www.idpf.org/2007/opf"}
    chapters = []
    with zipfile.ZipFile(epub_path) as zf:
        # find OPF
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path  = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
        opf_dir   = str(Path(opf_path).parent)
        opf       = ET.fromstring(zf.read(opf_path))

        manifest_all = {
            item.get("id"): item.get("href")
            for item in opf.findall("opf:manifest/opf:item", NS)
        }
        manifest  = {
            item.get("id"): item.get("href")
            for item in opf.findall("opf:manifest/opf:item", NS)
            if item.get("media-type") in ("application/xhtml+xml", "text/html")
        }
        spine = [ref.get("idref") for ref in opf.findall("opf:spine/opf:itemref", NS)]

        toc_titles = _ncx_titles(zf, opf, opf_dir, manifest_all)
        book_meta  = _book_metadata(opf)

        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = f"{opf_dir}/{href}" if opf_dir != "." else href
            try:
                html = zf.read(full).decode("utf-8", errors="replace")
            except KeyError:
                continue
            text = _html_to_text(html)
            words = len(re.findall(r"\w+", text))
            if words < 30:          # skip covers, blank pages, endnotes
                continue
            stem = Path(href).stem
            chapters.append((stem, text, toc_titles.get(stem, "")))
    return chapters, book_meta

def _humanize(stem: str) -> str:
    """Best-effort readable title for a chapter the TOC didn't name."""
    s = re.sub(r"^\d+[-_]", "", stem)          # drop a leading 20- / 001_ prefix
    s = s.replace("-", " ").replace("_", " ").strip()
    return s or stem

def _write_titles(path: Path, chapters: list[tuple[str, str, str]], book_meta: dict) -> None:
    """Write the chapter-title manifest consumed by join_book.sh.

    Format: tab-separated. `#TITLE`/`#ARTIST` header lines carry book metadata;
    each remaining line is `NNN<TAB>title`. join_book.sh joins only the chapters
    listed here, in this order — delete a line to drop that chapter, or edit the
    title text to rename it.
    """
    lines = []
    if book_meta.get("title"):
        lines.append(f"#TITLE\t{book_meta['title']}")
    if book_meta.get("author"):
        lines.append(f"#ARTIST\t{book_meta['author']}")
    for i, (name, _text, title) in enumerate(chapters, 1):
        lines.append(f"{i:03}\t{title or _humanize(name)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    args = [a for a in sys.argv[1:] if a != "--titles-only"]
    titles_only = "--titles-only" in sys.argv
    if not args:
        sys.exit(f"Usage: python3 {Path(__file__).name} [--titles-only] <input.epub> [output_dir]")

    epub_path  = Path(args[0])
    output_dir = Path(args[1]) if len(args) > 1 else \
                 Path("ebooks") / (epub_path.stem + "_chapters")
    output_dir.mkdir(parents=True, exist_ok=True)
    paused_dir = output_dir / "paused"   # pause-processed files live here, alone

    # --titles-only just (re)builds the chapter manifest from the epub TOC, with
    # no text extraction or LLM work — handy to recover titles for a book whose
    # audio was already generated.
    if titles_only:
        chapters, book_meta = _epub_chapters(epub_path)
        titles_path = output_dir / "titles.tsv"
        _write_titles(titles_path, chapters, book_meta)
        print(f"Wrote {len(chapters)} chapter titles → {titles_path}")
        return

    # probe Ollama availability once
    try:
        urllib.request.urlopen(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
        ollama_ok = True
        print(f"Ollama reachable — will write _paused versions (model: {MODEL})\n")
    except urllib.error.URLError:
        ollama_ok = False
        print("Ollama not reachable — writing plain text only (run insert_pauses.py later)\n")

    chapters, book_meta = _epub_chapters(epub_path)
    titles_path = output_dir / "titles.tsv"
    _write_titles(titles_path, chapters, book_meta)
    print(f"Chapter title manifest → {titles_path}\n")

    total_blocks = sum(count_blocks(text) for _, text, _ in chapters) if ollama_ok else 0
    progress = Progress(total_blocks)
    if ollama_ok:
        paused_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(chapters)} chapters in {epub_path.name}"
          + (f" ({total_blocks} blocks to process)\n" if ollama_ok else "\n"))

    for i, (name, text, _title) in enumerate(chapters, 1):
        prefix = f"{i:03}"
        plain_path = output_dir / f"{prefix}_{name}.txt"
        plain_path.write_text(text, encoding="utf-8")

        if ollama_ok:
            print(f"[{i}/{len(chapters)}] {name} — inserting pauses…")
            paused = process_text(text, progress)
            paused_path = paused_dir / f"{prefix}_{name}_paused.txt"
            paused_path.write_text(paused, encoding="utf-8")
        else:
            print(f"[{i}/{len(chapters)}] {name} — saved")

    print(f"\nDone → {output_dir}")
    if ollama_ok:
        print(f"Pause-enhanced chapters (feed these to ebook2audiobook):\n  {paused_dir}")

if __name__ == "__main__":
    main()
