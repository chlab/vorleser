#!/usr/bin/env python3
"""
Insert natural pause markers into German TTS text via a local Ollama model.

Reads a plain-text chapter file, sends each paragraph to Ollama, and writes
a new file with commas, em-dashes and ellipses added at natural speaking pauses.
Words are never changed — a sanity check falls back to the original paragraph
if the model alters any word.

Usage:
    python3 insert_pauses.py <input.txt> [output.txt]

If output path is omitted, writes <input>_paused.txt alongside the input file.
"""
import json, re, sys, time, urllib.request, urllib.error
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL      = "mistral-nemo"
MIN_WORDS  = 12   # skip very short blocks (titles, single lines)

SYSTEM_PROMPT = """\
Du bereitest einen deutschen Text für die Sprachsynthese vor. Ein Sprecher liest \
den Text vor und braucht an bestimmten Stellen natürliche Pausen.

AUFGABE: Füge an natürlichen Sprechpausen Satzzeichen ein:
  • Komma (,) — kurze Atempause innerhalb eines Satzes
  • Gedankenstrich (—) — rhetorische Pause oder besondere Hervorhebung
  • Auslassungspunkte (...) — nachdenkliche oder dramatische Pause

STRIKTE REGELN:
  1. Verändere KEIN einziges Wort — füge ausschließlich Satzzeichen hinzu oder \
ersetze bestehende durch passendere
  2. Behalte alle Zeilenumbrüche exakt bei
  3. Gib NUR den bearbeiteten Text zurück — keine Erklärungen, keine Kommentare\
"""

def extract_words(text: str) -> list[str]:
    return re.findall(r"[a-zäöüßA-ZÄÖÜ]+", text.lower())

def strip_markdown(text: str) -> str:
    text = re.sub(r"^```[^\n]*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    return text.strip()

def fix_punctuation(text: str) -> str:
    fixes = [
        (r'\.{4,}',     '...'),
        (r'\.\s*—',     ' —'),
        (r'\.,',        '.'),
        (r'—\s*,',      '—'),
        (r',\s*\.',     '.'),
        (r'\.\.\.([\wÄÖÜäöüß])', r'... \1'),
        (r'\s+,',       ','),
    ]
    for pattern, replacement in fixes:
        text = re.sub(pattern, replacement, text)
    return text

def call_ollama(text: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": text},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = json.loads(r.read())["message"]["content"]
    return fix_punctuation(strip_markdown(raw))

def count_blocks(text: str) -> int:
    """Number of non-empty paragraph blocks — the unit of LLM work."""
    return sum(1 for b in text.split("\n\n") if b.strip())


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h: return f"{h}h{m:02d}m"
    if m: return f"{m}m{s:02d}s"
    return f"{s}s"


class Progress:
    """Tracks block-level progress across a whole book for an overall % + ETA."""
    def __init__(self, total_blocks: int):
        self.total  = total_blocks
        self.done   = 0
        self._start = time.monotonic()

    def tick(self) -> None:
        self.done += 1

    def status(self) -> str:
        pct = (self.done / self.total * 100) if self.total else 0.0
        elapsed = time.monotonic() - self._start
        if self.done and elapsed > 0:
            eta = (self.total - self.done) * (elapsed / self.done)
            eta_str = _fmt_dur(eta)
        else:
            eta_str = "—"
        return f"overall {self.done}/{self.total} · {pct:4.1f}% · ETA {eta_str}"


def process_block(block: str, idx: int, total: int, progress: "Progress | None" = None) -> str:
    if progress is not None:
        progress.tick()
    preview = block[:72].replace("\n", " ").replace("\t", " ")
    suffix  = f"   ·  {progress.status()}" if progress is not None else ""
    print(f"  [{idx:2}/{total}] {preview}…{suffix}")

    orig_words = extract_words(block)
    if len(orig_words) < MIN_WORDS:
        print(f"           → skipped (too short)")
        return block

    try:
        result = call_ollama(block)
    except urllib.error.URLError as e:
        print(f"           ✗ Ollama error: {e} — keeping original")
        return block

    result_words = extract_words(result)
    if sorted(orig_words) != sorted(result_words):
        added   = sorted(set(result_words) - set(orig_words))
        removed = sorted(set(orig_words)   - set(result_words))
        print(f"           ⚠ word mismatch — keeping original")
        if added:   print(f"             added:   {added[:6]}")
        if removed: print(f"             removed: {removed[:6]}")
        return block

    print(f"           ✓")
    return result

def process_text(text: str, progress: "Progress | None" = None) -> str:
    """Run pause insertion over a full multi-paragraph text. Returns processed text.

    Pass a shared `Progress` to show book-wide position/ETA across all chapters.
    """
    blocks  = text.split("\n\n")
    total   = len(blocks)
    results = []
    for i, block in enumerate(blocks, 1):
        stripped = block.strip()
        if not stripped:
            results.append("")
            continue
        results.append(process_block(stripped, i, total, progress))
    return "\n\n".join(results)

def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if inp is None or not inp.exists():
        sys.exit(f"Usage: python3 {Path(__file__).name} <input.txt> [output.txt]")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.with_stem(inp.stem + "_paused")

    text = inp.read_text(encoding="utf-8")

    print(f"Model  : {MODEL}")
    print(f"Input  : {inp.name}  ({count_blocks(text)} blocks)")
    print(f"Output : {out.name}\n")

    paused = process_text(text, Progress(count_blocks(text)))
    out.write_text(paused, encoding="utf-8")
    print(f"\nDone → {out}")

if __name__ == "__main__":
    main()
