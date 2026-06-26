#!/usr/bin/env bash
# Join per-chapter .m4b files into a single .m4b audiobook with chapter markers.
#
# ebook2audiobook (--ebooks_dir mode) emits one .m4b per chapter. This stitches
# them — losslessly, via stream copy — into a single audiobook and embeds a
# navigable chapter marker per source file, titled from the filename.
#
# The book cover is embedded by default — pulled from the first chapter's
# embedded art (ebook2audiobook puts the cover in every chapter .m4b), or pass
# your own image as the 4th argument to override.
#
# Chapter titles and book-level metadata come from a titles manifest
# (titles.tsv, written by prepare_book.py) when one is available — see TITLES
# below. Without it, titles fall back to the filename (NNN_<name>_paused).
#
# Usage:
#   ./join_book.sh [SRC_DIR] [OUT_FILE] [GLOB] [COVER] [TITLES]
# Defaults:
#   SRC_DIR  audiobooks/wozu
#   OUT_FILE audiobooks/wozu_das_alles.m4b
#   GLOB     *_paused.m4b   (the pause-enhanced chapters; ignores the plain ones)
#   COVER    (none)         (override; default cover comes from the first chapter)
#   TITLES   $SRC_DIR/titles.tsv if present, else filename-derived titles.
#            The manifest also selects/orders chapters: only prefixes listed in
#            it are joined, so deleting a line drops that chapter.
set -euo pipefail

VORLESER="/Users/christof.leuenberger/dev/personal/vorleser"
SRC_DIR="${1:-$VORLESER/audiobooks/wozu}"
OUT="${2:-$VORLESER/audiobooks/wozu_das_alles.m4b}"
GLOB="${3:-*_paused.m4b}"
COVER="${4:-}"   # optional override; otherwise pulled from the first chapter
TITLES="${5:-$SRC_DIR/titles.tsv}"

cd "$SRC_DIR"
shopt -s nullglob

# ffmetadata escaping: =, ;, #, \ and newlines are special and must be escaped.
esc() { printf '%s' "$1" | sed 's/[\\=;#]/\\&/g'; }

# Build parallel `files`/`titles` arrays.
#   - With a manifest: walk it in order, resolving each NNN prefix to its file
#     and using the manifest title. Chapters absent from the manifest are
#     skipped; book metadata comes from #TITLE / #ARTIST header lines.
#   - Without one: every GLOB match, titled from its filename.
files=() ; titles=()
BOOK_TITLE="" ; BOOK_ARTIST=""
if [[ -f "$TITLES" ]]; then
  echo "Using titles manifest: $TITLES"
  while IFS=$'\t' read -r key val || [[ -n "$key" ]]; do
    [[ -z "$key" ]] && continue
    case "$key" in
      '#TITLE')  BOOK_TITLE="$val"  ;;
      '#ARTIST') BOOK_ARTIST="$val" ;;
      '#'*)      ;;                      # other comment — ignore
      *)
        cand=( "${key}"_*"${GLOB#\*}" )
        if (( ${#cand[@]} == 0 )); then
          echo "  warn: no file for prefix '$key' (matching ${key}_*${GLOB#\*}) — skipping" >&2
          continue
        fi
        files+=( "${cand[0]}" ) ; titles+=( "$val" )
        ;;
    esac
  done < "$TITLES"
else
  echo "No titles manifest at '$TITLES' — falling back to filename-derived titles." >&2
  echo "  (Generate one with: python3 prepare_book.py --titles-only <book.epub>," >&2
  echo "   then pass its path as the 5th argument.)" >&2
  # Zero-padded prefixes sort lexically and bash sorts glob expansions, so this
  # is already in chapter order.
  for f in $GLOB; do
    t="${f%.m4b}" ; t="${t#[0-9][0-9][0-9]_}" ; t="${t%_paused}"
    files+=( "$f" ) ; titles+=( "$t" )
  done
fi

if (( ${#files[@]} == 0 )); then
  echo "No chapters to join (glob '$GLOB', manifest '${TITLES}')" >&2
  exit 1
fi
echo "Joining ${#files[@]} chapters → $OUT"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
concat_list="$work/concat.txt"
meta="$work/chapters.ffmeta"

: > "$concat_list"
{
  printf ';FFMETADATA1\n'
  [[ -n "$BOOK_TITLE"  ]] && { printf 'title=%s\n'        "$(esc "$BOOK_TITLE")"
                               printf 'album=%s\n'        "$(esc "$BOOK_TITLE")"; }
  [[ -n "$BOOK_ARTIST" ]] && { printf 'artist=%s\n'       "$(esc "$BOOK_ARTIST")"
                               printf 'album_artist=%s\n' "$(esc "$BOOK_ARTIST")"; }
  printf 'genre=Audiobook\n'
} > "$meta"

start_ms=0
for i in "${!files[@]}"; do
  f="${files[$i]}"
  # concat demuxer entry (absolute path; -safe 0 below)
  printf "file '%s'\n" "$SRC_DIR/$f" >> "$concat_list"

  # per-file duration → milliseconds
  dur=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$f")
  dur_ms=$(awk -v d="$dur" 'BEGIN { printf "%d", d * 1000 }')
  end_ms=$(( start_ms + dur_ms ))

  {
    printf '[CHAPTER]\n'
    printf 'TIMEBASE=1/1000\n'
    printf 'START=%d\n' "$start_ms"
    printf 'END=%d\n'   "$end_ms"
    printf 'title=%s\n' "$(esc "${titles[$i]}")"
  } >> "$meta"

  start_ms=$end_ms
done

# Cover art: use the override if given, else pull the embedded cover from the
# first chapter (ebook2audiobook embeds the book cover in every chapter .m4b).
cover="$work/cover.jpg"
if [[ -n "$COVER" && -f "$COVER" ]]; then
  cp "$COVER" "$cover"
elif ! ffmpeg -nostdin -v error -y -i "$SRC_DIR/${files[0]}" -map 0:v:0 -frames:v 1 -c copy "$cover" </dev/null; then
  cover=""   # first chapter has no cover stream — proceed without one
fi

# Concatenate (stream copy) and attach chapter + book metadata — audio only.
# -map 0:a takes ONLY the audio: the per-chapter files also carry a bin_data
# and an mjpeg stream, and we deliberately do NOT re-embed the cover as a video
# track here. Apple Books, Finder and most players (BookPlayer included) read
# cover art from the MP4 `covr` metadata atom, not a video stream, so the cover
# is added separately below with AtomicParsley.
# -nostdin / </dev/null: ffmpeg must not read the terminal (avoids SIGTTIN if
# this is ever run backgrounded).
echo "Joining audio + chapters…"
ffmpeg -nostdin -hide_banner -loglevel warning -y \
  -f concat -safe 0 -i "$concat_list" \
  -i "$meta" -map_metadata 1 \
  -map 0:a -c:a copy -movflags +faststart \
  "$OUT" < /dev/null

# Embed the cover into the `covr` atom so Apple software and audiobook apps
# actually display it. AtomicParsley rewrites the file in place (--overWrite).
if [[ -n "$cover" ]]; then
  if command -v AtomicParsley >/dev/null 2>&1; then
    echo "Embedding cover (covr atom) from ${COVER:-first chapter}"
    AtomicParsley "$OUT" --artwork "$cover" --overWrite >/dev/null
  else
    echo "AtomicParsley not found — skipping cover art." >&2
    echo "  Install it for cover support:  brew install atomicparsley" >&2
  fi
else
  echo "No cover found — audio only."
fi

echo "Done → $OUT"
echo "Total length: $(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$OUT" | awk '{printf "%d:%02d:%02d\n", $1/3600, ($1%3600)/60, $1%60}')"
echo "Chapters embedded: $(ffprobe -v error -show_chapters "$OUT" | grep -c '^\[CHAPTER\]')"
