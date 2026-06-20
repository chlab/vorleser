# vorleser

Convert German ebooks to audiobooks using local TTS and LLM-assisted prosody preprocessing.

Built on top of [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) with a preprocessing pipeline that uses a local LLM (via Ollama) to insert natural pause markers before synthesis, producing more natural-sounding narration.

## How it works

1. **`prepare_book.py`** extracts each chapter from an epub and runs it through a local LLM (Mistral NeMo via Ollama) to insert commas, em-dashes and ellipses at natural speaking pauses
2. **ebook2audiobook** converts the preprocessed text to audio using Piper TTS with a German voice model
3. Output is a `.m4b` audiobook with chapter metadata

## Prerequisites

### System tools

Install via Homebrew:

```bash
brew install ffmpeg sox espeak-ng ollama
brew install --cask calibre
```

### Ollama model

```bash
ollama pull mistral-nemo
```

Mistral NeMo (12B) is recommended for German — it was explicitly trained with German as a target language and has a 128k context window. Requires ~7GB RAM.

### Piper voice model

Download a German Piper model. The [Thorsten-Voice](https://huggingface.co/Thorsten-Voice) models work well:

- `de_DE-thorsten-medium` — neutral, natural-sounding

All Piper voices are published in the [`rhasspy/piper-voices`](https://huggingface.co/rhasspy/piper-voices) repo. Each voice needs **two** files — the model (`.onnx`) and its config (`.onnx.json`). These are public, so no login or `huggingface-cli` is needed:

```bash
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium"
curl -L -o de_DE-thorsten-medium.onnx      "$BASE/de_DE-thorsten-medium.onnx"
curl -L -o de_DE-thorsten-medium.onnx.json "$BASE/de_DE-thorsten-medium.onnx.json"
```

Other quality tiers live alongside it — swap `medium` for `low` (smaller/faster) or `high` (larger/slower) in the path. Browse [`de/de_DE/thorsten/`](https://huggingface.co/rhasspy/piper-voices/tree/main/de/de_DE/thorsten) for the full list.

The `.onnx` and `.onnx.json` files go in the repo root or anywhere convenient. See [Packaging a custom Piper model](#packaging-a-custom-piper-model) below.

## Setup

```bash
git clone https://github.com/yourname/vorleser.git
cd vorleser
chmod +x setup.sh
./setup.sh
```

This clones ebook2audiobook into the sibling directory, applies the vorleser patches, and runs the bootstrap installer. Takes a while on first run.

## Usage

### 1. Prepare chapters

```bash
python3 prepare_book.py ebooks/mybook.epub
```

Extracts all chapters into `ebooks/mybook_chapters/` (plain text). If Ollama is running, it also writes the LLM pause-processed versions into a **`paused/` subdirectory** — `ebooks/mybook_chapters/paused/`. Those are the files you feed to ebook2audiobook.

> **Why a subdirectory:** ebook2audiobook's `--ebooks_dir` converts *every* `.txt` it finds. If the plain and `_paused` files share one directory, it converts both — doubling the work and output. Keeping the paused files in their own subdir means you point `--ebooks_dir` there and convert only the pause-enhanced text.

> **Important:** ebook2audiobook's `app.py` reads `VERSION.txt` (and other files) with paths relative to the current directory, so it **must be run from inside the ebook2audiobook directory** — running it from the vorleser root fails with `FileNotFoundError: VERSION.txt`. The commands below `cd` into `$E2A` first and pass **absolute paths** for the ebook, model and output so they still resolve. `$VORLESER` is this repo's root.

### 2. Convert a single chapter (for testing)

```bash
VORLESER="$(pwd)"            # run this from the vorleser repo root
E2A=../ebook2audiobook
mkdir -p "$VORLESER/audiobooks/test"

cd "$E2A" && ./python_env/bin/python3 app.py \
  --headless \
  --ebook "$VORLESER/ebooks/mybook_chapters/paused/004_chapter4_paused.txt" \
  --language deu \
  --tts_engine PIPER \
  --custom_model "$VORLESER/mymodel.zip" \
  --output_dir "$VORLESER/audiobooks/test"
```

### 3. Convert the full book

Pass an `--ebooks_dir` instead of `--ebook` to convert all chapters:

```bash
VORLESER="$(pwd)"            # run this from the vorleser repo root
E2A=../ebook2audiobook
mkdir -p "$VORLESER/audiobooks/mybook"

cd "$E2A" && ./python_env/bin/python3 app.py \
  --headless \
  --ebooks_dir "$VORLESER/ebooks/mybook_chapters/paused" \
  --language deu \
  --tts_engine PIPER \
  --custom_model "$VORLESER/mymodel.zip" \
  --output_dir "$VORLESER/audiobooks/mybook"
```

> Run this **in the foreground** in an interactive terminal. ebook2audiobook
> shells out to `ffmpeg`/`calibre`, which read the controlling terminal; if the
> job is backgrounded, those reads raise `SIGTTIN` and the whole process
> **freezes** (visible as process state `T` in `ps`). It may also prompt
> interactively (e.g. "conversion already exists — [s]kip/[y]es"), which needs
> a real terminal. Piper is fast, so a foreground run is fine. See
> [Troubleshooting](#troubleshooting) if you must run it unattended.

### 4. Join chapters into one audiobook

`--ebooks_dir` produces one `.m4b` per chapter. To stitch them into a single
`.m4b` with chapter markers (lossless stream-copy):

```bash
./join_book.sh "$VORLESER/audiobooks/mybook" "$VORLESER/audiobooks/mybook.m4b"
```

Or just keep the folder of numbered `.m4b` files — most audiobook players treat
it as one book with per-file chapters.

## Packaging a custom Piper model

ebook2audiobook expects a zip containing exactly these files:

```
model.onnx        ← your .onnx file, renamed
config.onnx.json  ← your .onnx.json file, renamed
ref.wav           ← placeholder only; Piper ignores it entirely
```

Piper is not a voice-cloning system — `ref.wav` is only there to pass ebook2audiobook's zip validation. A placeholder `ref.wav` is already included in this repo.

Build the zip with the included helper:

```bash
python3 package_model.py de_DE-thorsten-medium.onnx
```

This patches `length_scale` to 1.1 (slightly slower than default) and writes `de_DE-thorsten-medium.zip`. Override with `--length-scale` if needed:

```bash
python3 package_model.py de_DE-thorsten-medium.onnx --length-scale 1.2
```

## Patches applied to ebook2audiobook

`core.patch` fixes three issues in ebook2audiobook:

1. **Custom model caching** — extracted models are stored in a shared `__custom_models/` directory instead of session-specific directories, so the model is only unzipped once
2. **Cache hit handling** — correctly points `session['custom_model']` to the cached path on subsequent runs
3. **`UnboundLocalError` bugfix** — fixes a crash when zip validation fails (`f` was used but not defined in that scope)
4. **Zip preservation** — stops ebook2audiobook from deleting the `.zip` after extraction

## Tips

- **Speaking rate**: adjust `length_scale` in the Piper config JSON before zipping (`1.1` is a good starting point — slightly slower than default without sounding sluggish)
- **Pause quality**: the LLM preprocessing helps most with long philosophical or literary sentences; short conversational text benefits less
- **Hardware**: for full-book conversion with XTTS, prefer a machine with active cooling — MacBook Airs throttle significantly under sustained load. A Mac with M-series Pro/Max chip is strongly recommended
- **Ollama**: make sure `ollama serve` is running before calling `prepare_book.py`

## Troubleshooting

**`FileNotFoundError: VERSION.txt` when running `app.py`**
ebook2audiobook reads `VERSION.txt` (and other files) relative to the current
directory. Run it from *inside* the ebook2audiobook directory and pass absolute
paths for `--ebook`/`--ebooks_dir`, `--custom_model` and `--output_dir` (the
Usage commands already do this).

**`SSL: CERTIFICATE_VERIFY_FAILED` during conversion**
The conda env bakes its absolute install path into OpenSSL's cert location. If
you **moved the ebook2audiobook directory** after the bootstrap installer ran,
that path no longer exists and every HTTPS download fails. Fixes:
- Symlink the old path to the new one so the baked path resolves:
  `ln -s /new/path/ebook2audiobook /old/path/ebook2audiobook`, or
- recreate the conda env in place (re-run the bootstrap), or
- per-run: `export SSL_CERT_FILE="$(./python_env/bin/python3 -m certifi)"` and
  `export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"`.
Conda environments are not relocatable — avoid moving the directory after setup.

**Conversion freezes (process state `T` in `ps`), no progress for ages**
`app.py` shells out to `ffmpeg`/`calibre`, which read the controlling terminal.
A **backgrounded** job doing so receives `SIGTTIN` and the whole process group
stops. Run conversions in the **foreground**. If you must run unattended,
redirect stdin from `/dev/null` *and* avoid the resume path's interactive prompt
(`--session` triggers a "[s]kip/[y]es" `input()` that then fails on EOF) — i.e.
start fresh rather than resuming, or pre-answer the prompt.

**Interrupted conversion**
Headless mode prints a session id and supports `--session <id>` to resume — but
note the resume prompt above. For a foreground rerun it's often simplest to just
restart and answer `[y]es` to overwrite (Piper is fast).
