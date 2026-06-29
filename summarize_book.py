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

from insert_pauses import OLLAMA_URL, Progress
from prepare_book import _epub_chapters, _humanize, chapter_subheadings

# ── tunables ──────────────────────────────────────────────────────────────────
MODEL           = "qwen3:14b"  # summary model (separate from the pause model);
                               # qwen3 follows length/structure better than nemo
LANG            = "de"   # summary language: "de" (German) or "en" (English)
WORDS_LOW       = 300    # target summary length (thesis + bullets), lower bound
WORDS_HIGH      = 400    # …upper bound
POINTS_LOW      = 6      # number of bullet points
POINTS_HIGH     = 10
MAX_QUOTES      = 3      # hard cap on quotes per chapter
MIN_QUOTE_WORDS = 6      # reject too-short "quotes" (bare names, single words)
MIN_WORDS       = 150    # skip front-matter / chapters shorter than this
TEMPERATURE     = 0.2
MAX_CTX         = 32768  # KV-cache ceiling; chapters estimated larger are warned

SYSTEM_PROMPT = {
    "de": f"""\
Du bist ein sorgfältiger Lektor. Du fasst ein Kapitel eines deutschen Sachbuchs \
so zusammen, dass jemand, der NUR deine Zusammenfassung liest, den INHALT des \
Kapitels tatsächlich kennt — die konkreten Gedanken, Argumente, Beispiele und \
Schlussfolgerungen, nicht nur die behandelten Themen.

Erstelle:
1. "intro": die zentrale These oder Antwort des Kapitels in EINEM Satz — eine \
inhaltliche Aussage über die Sache selbst, KEINE Beschreibung des Kapitels (also \
nicht „Das Kapitel …", nicht „Es geht um …").
2. "points": {POINTS_LOW}–{POINTS_HIGH} Stichpunkte, je 1–2 Sätze. Jeder Punkt \
nennt einen konkreten Inhalt: eine These mit ihrer Begründung, eine \
Unterscheidung, ein Beispiel, eine Zahl, einen Namen oder eine Schlussfolgerung \
— mit den eigentlichen Aussagen. Decke die verschiedenen Abschnitte des Kapitels \
ab und wiederhole keinen Gedanken. Zusammen {WORDS_LOW}–{WORDS_HIGH} Wörter; \
lieber mehr konkrete Punkte als wenige vage.
3. "quotes": bis zu {MAX_QUOTES} wörtliche Zitate — nur wenn wirklich \
bemerkenswert. Leere Liste erlaubt.

VERBOTEN sind inhaltsleere Meta-Sätze. Schreibe NICHT „erörtert Möglichkeiten, \
wie Arbeit neu gedacht werden kann" — sondern NENNE diese Möglichkeiten konkret. \
Kein „behandelt", „diskutiert", „geht ein auf" ohne dass das WAS unmittelbar \
folgt. Ein Stichpunkt ist niemals nur eine Themen-Überschrift.

STRIKTE REGELN:
  • Erfinde nichts. Gib ausschließlich wieder, was im Text steht.
  • Jedes Zitat WÖRTLICH und Zeichen für Zeichen aus dem Text — ein vollständiger \
Satz, niemals ein bloßer Name oder eine Überschrift.
  • Antworte NUR mit gültigem JSON in genau diesem Format:
    {{"intro": "...", "points": ["...", "..."], "quotes": ["..."]}}
  • Keine weiteren Erklärungen, kein Markdown außerhalb der Strings.""",
    "en": f"""\
You are a careful editor. You summarize a chapter of a German non-fiction book so \
that someone who reads ONLY your summary actually knows the chapter's CONTENT — \
the concrete ideas, arguments, examples and conclusions, not just the topics it \
covers. Write the summary in English.

Produce:
1. "intro": the chapter's central thesis or answer in ONE sentence — a substantive \
claim about the subject itself, NOT a description of the chapter (so not "The \
chapter …", not "It is about …").
2. "points": {POINTS_LOW}–{POINTS_HIGH} bullet points, 1–2 sentences each. Each \
states concrete content: a thesis with its reasoning, a distinction, an example, \
a figure, a name, or a conclusion — with the actual claims. Cover the chapter's \
different sections and never repeat a point. Together {WORDS_LOW}–{WORDS_HIGH} \
words; prefer more concrete points over a few vague ones.
3. "quotes": up to {MAX_QUOTES} verbatim quotes — only if genuinely noteworthy; \
an empty list is allowed. Quote the original German exactly; do not translate.

FORBIDDEN: empty meta-sentences. Do NOT write "explores ways to rethink work" — \
NAME those ways concretely. No "discusses", "addresses", "looks at" without WHAT \
immediately following. A bullet is never just a topic label.

STRICT RULES:
  • Invent nothing. Convey only what the text says.
  • Every quote VERBATIM, character for character, from the text — a complete \
sentence, never a bare name or heading.
  • Reply with ONLY valid JSON in exactly this shape:
    {{"intro": "...", "points": ["...", "..."], "quotes": ["..."]}}
  • No other explanations, no markdown outside the strings.""",
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
        text = source[lo:hi]
        text = re.sub(r"\[\d+\]", "", text)            # drop footnote markers
        text = re.sub(r"\s+", " ", text).strip().strip(_QUOTE_CHARS).strip()
        if len(text.split()) >= MIN_QUOTE_WORDS and text not in kept:
            kept.append(text)
        else:
            dropped.append(q.strip())
    return kept[:MAX_QUOTES], dropped

# ── ollama ────────────────────────────────────────────────────────────────────

def _dedupe(points: list) -> list:
    """Drop near-duplicate bullets (mistral-nemo sometimes restates a point)."""
    out, seen = [], []
    for p in points:
        ws = set(re.findall(r"\w+", p.lower()))
        if ws and any(len(ws & s) / len(ws | s) > 0.6 for s in seen):
            continue
        out.append(p); seen.append(ws)
    return out

def _estimate_ctx(text: str) -> tuple[int, int]:
    """Pick a num_ctx large enough for the chapter + prompt + output. German runs
    ~3 chars/token; add headroom and round to a 4096 boundary. Returns
    (num_ctx, estimated_tokens)."""
    need = len(text) // 3 + 1536        # input tokens + output/prompt overhead
    ctx  = max(8192, math.ceil(need / 4096) * 4096)
    return min(ctx, MAX_CTX), need

def call_ollama(title: str, text: str, sections: list, num_ctx: int) -> dict:
    user = f"Titel: {title}\n"
    if sections:
        user += "\nGliederung des Kapitels (Abschnitte):\n" \
                + "\n".join(f"  - {s}" for s in sections) \
                + "\nStelle sicher, dass die Zusammenfassung die wesentlichen " \
                  "dieser Abschnitte inhaltlich abdeckt.\n"
    user += f"\nKapiteltext:\n{text}"
    payload = {
        "model": MODEL,
        "format": "json",
        "stream": False,
        "think": False,          # qwen3 reasoning off → clean, fast JSON
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT[LANG]},
            {"role": "user",   "content": user},
        ],
        "options": {"temperature": TEMPERATURE, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        content = json.loads(r.read())["message"]["content"]
    return json.loads(content)

def summarize_chapter(title: str, text: str, sections: list | None = None):
    """Return (intro, points, kept_quotes, dropped_quotes) or None on failure."""
    sections = sections or []
    num_ctx, est = _estimate_ctx(text)
    if est > MAX_CTX:
        print(f"           ⚠ ~{est} tokens exceeds num_ctx {MAX_CTX} — may truncate")
    for attempt in (1, 2):
        try:
            data = call_ollama(title, text, sections, num_ctx)
        except urllib.error.URLError as e:
            print(f"           ✗ Ollama error: {e}")
            return None
        except json.JSONDecodeError:
            print(f"           ⚠ invalid JSON (attempt {attempt})")
            continue
        clean  = lambda s: re.sub(r"\s+", " ", re.sub(r"\[\d+\]", "", s)).strip()
        intro  = clean(data.get("intro") or "")
        points = _dedupe([clean(p) for p in (data.get("points") or [])
                          if isinstance(p, str) and p.strip()])
        quotes = data.get("quotes") or []
        if not isinstance(quotes, list):
            quotes = []
        if intro or points:
            kept, dropped = verify_quotes(quotes, text)
            return intro, points, kept, dropped
        print(f"           ⚠ empty summary (attempt {attempt})")
    return None

# ── markdown ──────────────────────────────────────────────────────────────────

def chapter_markdown(num: int, title: str, intro: str, points, quotes) -> str:
    out = [f"## {num}. {title}", ""]
    if intro:
        out += [intro, ""]
    out += [f"- {p}" for p in points]
    for q in quotes:
        out += ["", f"> {q}"]
    return "\n".join(out).rstrip() + "\n"

# ── main ──────────────────────────────────────────────────────────────────────

def _matches(tok: str, i: int, name: str, title: str) -> bool:
    """A --from/--to token matches a chapter by its number (33 / 033), its file
    stem (chapter28), or a case-insensitive substring of its title (philia)."""
    t = tok.lower()
    return t in (str(i), f"{i:03}", name.lower()) or t in (title or "").lower()

def main():
    argv, force, from_tok, to_tok, positional = sys.argv[1:], False, None, None, []
    it = iter(argv)
    for a in it:
        if   a == "--force": force = True
        elif a == "--from":  from_tok = next(it, None)
        elif a == "--to":    to_tok   = next(it, None)
        elif a.startswith("--"): pass            # unknown flag — ignore
        else: positional.append(a)
    if not positional:
        sys.exit(f"Usage: python3 {Path(__file__).name} <input.epub> [output_dir] "
                 f"[--from CH] [--to CH] [--force]\n"
                 f"  --from/--to select a chapter range by number, stem or title text.")

    epub_path  = Path(positional[0])
    output_dir = Path(positional[1]) if len(positional) > 1 else \
                 Path("ebooks") / (epub_path.stem + "_chapters")
    summ_dir = output_dir / "summaries"
    summ_dir.mkdir(parents=True, exist_ok=True)

    try:
        urllib.request.urlopen(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
    except urllib.error.URLError:
        sys.exit("Ollama not reachable at "
                 f"{OLLAMA_URL} — start it with `ollama serve` and pull {MODEL}.")

    chapters, book_meta = _epub_chapters(epub_path)
    subheadings = chapter_subheadings(epub_path)
    todo = [(i, name, text, title)
            for i, (name, text, title) in enumerate(chapters, 1)
            if len(re.findall(r"\w+", text)) >= MIN_WORDS]

    lo, hi = 1, len(chapters)
    if from_tok:
        m = [i for i, n, _, ti in todo if _matches(from_tok, i, n, ti)]
        if not m: sys.exit(f"--from '{from_tok}' matched no chapter")
        lo = m[0]
    if to_tok:
        m = [i for i, n, _, ti in todo if _matches(to_tok, i, n, ti)]
        if not m: sys.exit(f"--to '{to_tok}' matched no chapter")
        hi = m[-1]
    todo = [c for c in todo if lo <= c[0] <= hi]
    skipped = len(chapters) - len(todo)

    print(f"Model    : {MODEL}  ({LANG} summaries, {WORDS_LOW}–{WORDS_HIGH} words, "
          f"≤{MAX_QUOTES} quotes/chapter)")
    print(f"Chapters : {len(chapters)} found, {len(todo)} to summarize"
          f"{f', {skipped} skipped (<{MIN_WORDS} words)' if skipped else ''}\n")

    progress = Progress(len(todo))
    rendered = []
    for i, name, text, title in todo:
        label   = title or _humanize(name)
        md_path = summ_dir / f"{i:03}_{name}_summary.md"

        if md_path.exists() and not force:
            progress.tick()
            rendered.append(md_path.read_text(encoding="utf-8"))
            print(f"[{i:03}] {label} — already done, skipping   ·  {progress.status()}")
            continue

        progress.tick()
        print(f"[{i:03}] {label} — summarizing…   ·  {progress.status()}")
        result = summarize_chapter(label, text, subheadings.get(name, []))
        if result is None:
            print(f"      ✗ failed — leaving for a later --force run")
            continue
        intro, points, kept, dropped = result
        if dropped:
            print(f"      ⚠ dropped {len(dropped)} unverifiable quote(s)")
            for d in dropped:
                print(f"        · {d[:70]}…")
        words = len((intro + " " + " ".join(points)).split())
        print(f"      ✓ {words} words, {len(points)} points, {len(kept)} quote(s)")

        md = chapter_markdown(i, label, intro, points, kept)
        md_path.write_text(md, encoding="utf-8")
        rendered.append(md)

    # combined book summary
    header = [f"# {book_meta.get('title') or epub_path.stem}"]
    if book_meta.get("author"):
        header.append(f"*{book_meta['author']}*")
    combined = "\n".join(header) + "\n\n" + "\n".join(rendered)
    combined_path = output_dir / "SUMMARY.md"
    combined_path.write_text(combined, encoding="utf-8")

    print(f"\nDone → {summ_dir}")
    print(f"Combined summary → {combined_path}")

if __name__ == "__main__":
    main()
