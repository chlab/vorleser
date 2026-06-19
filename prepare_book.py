#!/usr/bin/env python3
"""
Extract all chapters from an epub and run LLM pause preprocessing on each.

Reads the epub spine order, extracts each chapter as plain text (preserving
paragraph breaks), and writes numbered files into a directory ready for
ebook2audiobook batch conversion.

Usage:
    python3 prepare_book.py <input.epub> [output_dir]

Output directory defaults to ebooks/<book-stem>_chapters/.
Each file is named 001_chapter.txt, 002_chapter.txt, etc.
Paused versions (_paused suffix) are written alongside if Ollama is reachable.
"""
import re, sys, urllib.request, zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from insert_pauses import MODEL, OLLAMA_URL, process_text

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

def _epub_chapters(epub_path: Path) -> list[tuple[str, str]]:
    """Return list of (name, text) for each spine document that has real content."""
    NS = {"opf": "http://www.idpf.org/2007/opf"}
    chapters = []
    with zipfile.ZipFile(epub_path) as zf:
        # find OPF
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path  = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
        opf_dir   = str(Path(opf_path).parent)
        opf       = ET.fromstring(zf.read(opf_path))

        manifest  = {
            item.get("id"): item.get("href")
            for item in opf.findall("opf:manifest/opf:item", NS)
            if item.get("media-type") in ("application/xhtml+xml", "text/html")
        }
        spine = [ref.get("idref") for ref in opf.findall("opf:spine/opf:itemref", NS)]

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
            chapters.append((Path(href).stem, text))
    return chapters

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        sys.exit(f"Usage: python3 {Path(__file__).name} <input.epub> [output_dir]")

    epub_path  = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else \
                 Path("ebooks") / (epub_path.stem + "_chapters")
    output_dir.mkdir(parents=True, exist_ok=True)

    # probe Ollama availability once
    try:
        urllib.request.urlopen(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
        ollama_ok = True
        print(f"Ollama reachable — will write _paused versions (model: {MODEL})\n")
    except urllib.error.URLError:
        ollama_ok = False
        print("Ollama not reachable — writing plain text only (run insert_pauses.py later)\n")

    chapters = _epub_chapters(epub_path)
    print(f"Found {len(chapters)} chapters in {epub_path.name}\n")

    for i, (name, text) in enumerate(chapters, 1):
        prefix = f"{i:03}"
        plain_path = output_dir / f"{prefix}_{name}.txt"
        plain_path.write_text(text, encoding="utf-8")

        if ollama_ok:
            print(f"[{i}/{len(chapters)}] {name} — inserting pauses…")
            paused = process_text(text)
            paused_path = output_dir / f"{prefix}_{name}_paused.txt"
            paused_path.write_text(paused, encoding="utf-8")
        else:
            print(f"[{i}/{len(chapters)}] {name} — saved")

    print(f"\nDone → {output_dir}")

if __name__ == "__main__":
    main()
