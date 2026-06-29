#!/usr/bin/env bash
# Build a portable EPUB from a book's chapter summaries (the SUMMARY.md written
# by summarize_book.py). Each "## N. Title" heading becomes a navigable chapter,
# so it reads well on iPhone/iPad in Apple Books — AirDrop the file across.
#
# This is an add-on to the summary tool, not part of the audiobook pipeline.
# Requires calibre's `ebook-convert` (brew install --cask calibre).
#
# Usage:
#   ./make_summary_epub.sh [SUMMARY_MD] [OUT_EPUB] [COVER] [TITLES]
# Defaults target the wozu book. Title/author are read from the titles manifest
# (titles.tsv) next to SUMMARY.md when present; otherwise derived from the dir.
set -euo pipefail

VORLESER="/Users/christof.leuenberger/dev/personal/vorleser"
SUMMARY="${1:-$VORLESER/ebooks/wozu_das_alles_chapters/SUMMARY.md}"
src_dir="$(cd "$(dirname "$SUMMARY")" && pwd)"
book="$(basename "$src_dir")"; book="${book%_chapters}"
OUT="${2:-$VORLESER/ebooks/${book}_zusammenfassung.epub}"
COVER="${3:-$src_dir/cover.jpg}"     # cover image; defaults to cover.jpg by SUMMARY.md
TITLES="${4:-$src_dir/titles.tsv}"   # for #TITLE / #ARTIST metadata

command -v ebook-convert >/dev/null 2>&1 || {
  echo "ebook-convert not found — install calibre: brew install --cask calibre" >&2
  exit 1
}
[[ -f "$SUMMARY" ]] || {
  echo "No summary file: $SUMMARY — run summarize_book.py first." >&2
  exit 1
}

# Book title/author from the manifest, if available.
title="$book"; author="Unknown"
if [[ -f "$TITLES" ]]; then
  while IFS=$'\t' read -r key val; do
    case "$key" in
      '#TITLE')  title="$val"  ;;
      '#ARTIST') author="$val" ;;
    esac
  done < "$TITLES"
fi

# Each h2 (the "## N. Title" summary headings) starts a chapter and a TOC entry.
args=( "$SUMMARY" "$OUT"
  --title "$title – Zusammenfassungen"
  --authors "$author"
  --language de
  --chapter '//h:h2' --level1-toc '//h:h2' --chapter-mark pagebreak )
[[ -n "$COVER" && -f "$COVER" ]] && args+=( --cover "$COVER" )

echo "Building “$title – Zusammenfassungen” by $author"
ebook-convert "${args[@]}" >/dev/null
echo "Done → $OUT"
echo "AirDrop it to your iPhone — it opens in Apple Books."
