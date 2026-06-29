#!/usr/bin/env python3
"""
Generate a ~150-word abstract and up to 3 noteworthy verbatim quotes per chapter.

Reads the epub (same chapter extraction as prepare_book.py), sends each chapter
to a local Ollama model, and writes one Markdown summary per chapter plus a
combined SUMMARY.md for the whole book.

The model is asked for strict JSON ({"abstract": ..., "quotes": [...]}). Every
returned quote is verified against the source chapter text — anything that is
not actually present (a paraphrase or hallucination) is dropped and reported, so
the quotes can be trusted even from a small model.

Usage:
    python3 summarize_book.py <input.epub> [output_dir] [--force]

Output goes to <output_dir>/summaries/NNN_<name>_summary.md (defaults to
ebooks/<book-stem>_chapters/summaries/), with a combined
<output_dir>/SUMMARY.md. Already-written chapters are skipped unless --force.
"""
import json, math, re, sys, urllib.request, urllib.error
from pathlib import Path

from insert_pauses import MODEL, OLLAMA_URL, Progress
from prepare_book import _epub_chapters, _humanize

# ── tunables ──────────────────────────────────────────────────────────────────
LANG           = "de"    # summary language: "de" (German) or "en" (English)
ABSTRACT_WORDS = 150     # target abstract length
MAX_QUOTES     = 3       # hard cap on quotes per chapter
MIN_QUOTE_WORDS = 6      # reject too-short "quotes" (bare names, single words)
MIN_WORDS      = 150     # skip front-matter / chapters shorter than this
TEMPERATURE    = 0.3
MAX_CTX        = 32768   # KV-cache ceiling; chapters estimated larger are warned

SYSTEM_PROMPT = {
    "de": f"""\
Du bist ein sorgfältiger Lektor. Du erhältst ein Kapitel eines deutschen \
Sachbuchs und erstellst zwei Dinge:

1. "abstract": eine prägnante Zusammenfassung von etwa {ABSTRACT_WORDS} Wörtern, \
die den Kerngedanken und den Argumentationsgang des Kapitels wiedergibt.
2. "quotes": bis zu {MAX_QUOTES} wörtliche Zitate — aber NUR, wenn sie wirklich \
bemerkenswert, prägnant oder zitierwürdig sind. Lieber weniger oder gar keine. \
Eine leere Liste ist ausdrücklich erlaubt.

STRIKTE REGELN:
  • Jedes Zitat muss WÖRTLICH und Zeichen für Zeichen aus dem Text stammen — \
keine Auslassungen, keine Änderungen, keine Zusammensetzung mehrerer Stellen.
  • Ein Zitat ist ein vollständiger, gehaltvoller Satz aus dem Text — niemals \
ein bloßer Name, eine Überschrift oder ein einzelnes Wort.
  • Erfinde nichts. Fasse ausschließlich zusammen, was im Text steht.
  • Antworte NUR mit gültigem JSON in genau diesem Format:
    {{"abstract": "...", "quotes": ["...", "..."]}}
  • Keine Erklärungen, kein Markdown, nur das JSON-Objekt.""",
    "en": f"""\
You are a careful editor. You receive one chapter of a German non-fiction book \
and produce two things:

1. "abstract": a concise ~{ABSTRACT_WORDS}-word summary (in English) capturing \
the chapter's core idea and line of argument.
2. "quotes": up to {MAX_QUOTES} verbatim quotes — but ONLY if they are genuinely \
noteworthy or quotable. Prefer fewer or none; an empty list is allowed. Quote \
the original German text exactly, do not translate the quotes.

STRICT RULES:
  • Every quote must be taken VERBATIM, character for character, from the text — \
no omissions, no edits, no stitching passages together.
  • A quote is a complete, substantive sentence from the text — never a bare \
name, heading, or single word.
  • Invent nothing. Summarize only what the text says.
  • Reply with ONLY valid JSON in exactly this shape:
    {{"abstract": "...", "quotes": ["...", "..."]}}
  • No explanations, no markdown, just the JSON object.""",
}

# ── quote verification ────────────────────────────────────────────────────────

_QUOTE_CHARS = "\"'„“”‚‘’«»"

def _norm_char(c: str) -> str:
    """Single-char normalization shared by quote and source so offsets line up:
    lowercase, dashes→'-', ellipsis→'.', quote marks dropped (→'')."""
    c = c.lower()
    if c in "—–": return "-"
    if c == "…":  return "."
    if c in _QUOTE_CHARS: return ""
    return c

def _norm_quote(s: str) -> str:
    out, prev_space = [], False
    for ch in s:
        c = _norm_char(ch)
        if not c:
            continue
        if c.isspace():
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(c)
            prev_space = False
    return "".join(out).strip()

def _build_norm(src: str):
    """Normalized source string + a map from each normalized-char position back to
    the original source index, so a match can be projected onto the real text."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(src):
        c = _norm_char(ch)
        if not c:
            continue
        if c.isspace():
            if prev_space:
                continue
            out.append(" "); idx.append(i); prev_space = True
        else:
            out.append(c); idx.append(i); prev_space = False
    return "".join(out), idx

def _sentence_spans(src: str):
    """(start, end) char spans for each sentence, splitting on . ! ? … plus any
    trailing quotes/brackets."""
    spans, start = [], 0
    for m in re.finditer(r"[.!?…]+[\s»\"”'’\)\]]*", src):
        spans.append((start, m.end())); start = m.end()
    if start < len(src):
        spans.append((start, len(src)))
    return spans

def _verbatim_range(qnorm: str, src_norm: str):
    """Longest leading run of the (normalized) quote present in the source —
    strips trailing attributions/fragments the model appends. Returns the
    normalized [start, end) or None."""
    words = [w for w in qnorm.split(" ") if w]
    for end in range(len(words), MIN_QUOTE_WORDS - 1, -1):
        cand = " ".join(words[:end])
        pos = src_norm.find(cand)
        if pos != -1:
            return pos, pos + len(cand)
    return None

def verify_quotes(quotes, source: str):
    """Keep only quotes verifiably present in the source, each expanded to whole
    sentences and emitted from the original text (real casing/punctuation).
    Returns (kept, dropped); dropped are bare names, fragments, or
    paraphrases/hallucinations."""
    src_norm, idx = _build_norm(source)
    sents = _sentence_spans(source)
    kept, dropped = [], []
    for q in quotes:
        if not isinstance(q, str) or not q.strip():
            continue
        rng = _verbatim_range(_norm_quote(q), src_norm)
        if rng is None:
            dropped.append(q.strip()); continue
        o_start, o_end = idx[rng[0]], idx[rng[1] - 1] + 1
        # expand to the sentence(s) the match falls in → complete-sentence quote
        lo = next((a for a, b in sents if a <= o_start < b), o_start)
        hi = next((b for a, b in reversed(sents) if a < o_end <= b), o_end)
        text = re.sub(r"\s+", " ", source[lo:hi]).strip().strip(_QUOTE_CHARS).strip()
        if len(text.split()) >= MIN_QUOTE_WORDS and text not in kept:
            kept.append(text)
        else:
            dropped.append(q.strip())
    return kept[:MAX_QUOTES], dropped

# ── ollama ────────────────────────────────────────────────────────────────────

def _estimate_ctx(text: str) -> tuple[int, int]:
    """Pick a num_ctx large enough for the chapter + prompt + output. German runs
    ~3 chars/token; add headroom and round to a 4096 boundary. Returns
    (num_ctx, estimated_tokens)."""
    need = len(text) // 3 + 1536        # input tokens + output/prompt overhead
    ctx  = max(8192, math.ceil(need / 4096) * 4096)
    return min(ctx, MAX_CTX), need

def call_ollama(title: str, text: str, num_ctx: int) -> dict:
    payload = {
        "model": MODEL,
        "format": "json",
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT[LANG]},
            {"role": "user",   "content": f"Titel: {title}\n\n{text}"},
        ],
        "options": {"temperature": TEMPERATURE, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        content = json.loads(r.read())["message"]["content"]
    return json.loads(content)

def summarize_chapter(title: str, text: str):
    """Return (abstract, kept_quotes, dropped_quotes) or None on failure."""
    num_ctx, est = _estimate_ctx(text)
    if est > MAX_CTX:
        print(f"           ⚠ ~{est} tokens exceeds num_ctx {MAX_CTX} — may truncate")
    for attempt in (1, 2):
        try:
            data = call_ollama(title, text, num_ctx)
        except urllib.error.URLError as e:
            print(f"           ✗ Ollama error: {e}")
            return None
        except json.JSONDecodeError:
            print(f"           ⚠ invalid JSON (attempt {attempt})")
            continue
        abstract = (data.get("abstract") or "").strip()
        quotes   = data.get("quotes") or []
        if not isinstance(quotes, list):
            quotes = []
        if abstract:
            kept, dropped = verify_quotes(quotes, text)
            return abstract, kept, dropped
        print(f"           ⚠ empty abstract (attempt {attempt})")
    return None

# ── markdown ──────────────────────────────────────────────────────────────────

def chapter_markdown(num: int, title: str, abstract: str, quotes) -> str:
    out = [f"## {num}. {title}", "", abstract]
    for q in quotes:
        out += ["", f"> {q}"]
    return "\n".join(out) + "\n"

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if not args:
        sys.exit(f"Usage: python3 {Path(__file__).name} <input.epub> [output_dir] [--force]")

    epub_path  = Path(args[0])
    output_dir = Path(args[1]) if len(args) > 1 else \
                 Path("ebooks") / (epub_path.stem + "_chapters")
    summ_dir = output_dir / "summaries"
    summ_dir.mkdir(parents=True, exist_ok=True)

    try:
        urllib.request.urlopen(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
    except urllib.error.URLError:
        sys.exit("Ollama not reachable at "
                 f"{OLLAMA_URL} — start it with `ollama serve` and pull {MODEL}.")

    chapters, book_meta = _epub_chapters(epub_path)
    todo = [(i, name, text, title)
            for i, (name, text, title) in enumerate(chapters, 1)
            if len(re.findall(r"\w+", text)) >= MIN_WORDS]
    skipped = len(chapters) - len(todo)

    print(f"Model    : {MODEL}  ({LANG} summaries, ≤{MAX_QUOTES} quotes/chapter)")
    print(f"Chapters : {len(chapters)} found, {len(todo)} to summarize"
          f"{f', {skipped} skipped (<{MIN_WORDS} words)' if skipped else ''}\n")

    progress = Progress(len(todo))
    sections = []
    for i, name, text, title in todo:
        label   = title or _humanize(name)
        md_path = summ_dir / f"{i:03}_{name}_summary.md"

        if md_path.exists() and not force:
            progress.tick()
            sections.append(md_path.read_text(encoding="utf-8"))
            print(f"[{i:03}] {label} — already done, skipping   ·  {progress.status()}")
            continue

        progress.tick()
        print(f"[{i:03}] {label} — summarizing…   ·  {progress.status()}")
        result = summarize_chapter(label, text)
        if result is None:
            print(f"      ✗ failed — leaving for a later --force run")
            continue
        abstract, kept, dropped = result
        if dropped:
            print(f"      ⚠ dropped {len(dropped)} unverifiable quote(s)")
            for d in dropped:
                print(f"        · {d[:70]}…")
        print(f"      ✓ {len(abstract.split())} words, {len(kept)} quote(s)")

        md = chapter_markdown(i, label, abstract, kept)
        md_path.write_text(md, encoding="utf-8")
        sections.append(md)

    # combined book summary
    header = [f"# {book_meta.get('title') or epub_path.stem}"]
    if book_meta.get("author"):
        header.append(f"*{book_meta['author']}*")
    combined = "\n".join(header) + "\n\n" + "\n".join(sections)
    combined_path = output_dir / "SUMMARY.md"
    combined_path.write_text(combined, encoding="utf-8")

    print(f"\nDone → {summ_dir}")
    print(f"Combined summary → {combined_path}")

if __name__ == "__main__":
    main()
