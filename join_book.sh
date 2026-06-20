#!/usr/bin/env bash
# Join per-chapter .m4b files into a single .m4b audiobook with chapter markers.
#
# ebook2audiobook (--ebooks_dir mode) emits one .m4b per chapter. This stitches
# them — losslessly, via stream copy — into a single audiobook and embeds a
# navigable chapter marker per source file, titled from the filename.
#
# Usage:
#   ./join_book.sh [SRC_DIR] [OUT_FILE] [GLOB]
# Defaults:
#   SRC_DIR  audiobooks/wozu
#   OUT_FILE audiobooks/wozu_das_alles.m4b
#   GLOB     *_paused.m4b   (the pause-enhanced chapters; ignores the plain ones)
set -euo pipefail

VORLESER="/Users/christof.leuenberger/dev/personal/vorleser"
SRC_DIR="${1:-$VORLESER/audiobooks/wozu}"
OUT="${2:-$VORLESER/audiobooks/wozu_das_alles.m4b}"
GLOB="${3:-*_paused.m4b}"

cd "$SRC_DIR"

# Collect chapter files in numeric order. Zero-padded prefixes (001_, 002_…)
# sort lexically, and bash sorts glob expansions, so this is already in order.
shopt -s nullglob
files=( $GLOB )
if (( ${#files[@]} == 0 )); then
  echo "No files matching '$GLOB' in $SRC_DIR" >&2
  exit 1
fi
echo "Joining ${#files[@]} chapters → $OUT"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
concat_list="$work/concat.txt"
meta="$work/chapters.ffmeta"

: > "$concat_list"
printf ';FFMETADATA1\n' > "$meta"

start_ms=0
for f in "${files[@]}"; do
  # concat demuxer entry (absolute path; -safe 0 below)
  printf "file '%s'\n" "$SRC_DIR/$f" >> "$concat_list"

  # per-file duration → milliseconds
  dur=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$f")
  dur_ms=$(awk -v d="$dur" 'BEGIN { printf "%d", d * 1000 }')
  end_ms=$(( start_ms + dur_ms ))

  # chapter title: drop the NNN_ prefix and the _paused suffix
  title="${f%.m4b}"
  title="${title#[0-9][0-9][0-9]_}"
  title="${title%_paused}"

  {
    printf '[CHAPTER]\n'
    printf 'TIMEBASE=1/1000\n'
    printf 'START=%d\n' "$start_ms"
    printf 'END=%d\n'   "$end_ms"
    printf 'title=%s\n' "$title"
  } >> "$meta"

  start_ms=$end_ms
done

# Concatenate (stream copy) and attach chapter metadata.
# -nostdin / </dev/null: ffmpeg must not read the terminal (avoids SIGTTIN if
# this is ever run backgrounded).
ffmpeg -nostdin -hide_banner -loglevel warning -y \
  -f concat -safe 0 -i "$concat_list" \
  -i "$meta" -map_metadata 1 \
  -c copy -movflags +faststart \
  "$OUT" < /dev/null

echo "Done → $OUT"
echo "Total length: $(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$OUT" | awk '{printf "%d:%02d:%02d\n", $1/3600, ($1%3600)/60, $1%60}')"
echo "Chapters embedded: $(ffprobe -v error -show_chapters "$OUT" | grep -c '^\[CHAPTER\]')"
