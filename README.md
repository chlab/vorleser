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

The `.onnx` and `.onnx.json` files go in the repo root or anywhere convenient. See [Packaging a custom Piper model](#packaging-a-custom-piper-model) below.

### Voice reference sample (optional)

For XTTS voice cloning, place a clean WAV sample of your target speaker in `voices/`. Aim for 30–60 seconds of varied, natural speech at 22050Hz mono. Multiple clips can be stitched together with:

```bash
ffmpeg -i "concat:clip1.wav|clip2.wav|clip3.wav" -ac 1 -ar 22050 voices/speaker.wav
```

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

Extracts all chapters into `ebooks/mybook_chapters/`. If Ollama is running, also writes `_paused` versions with LLM-inserted pause markers. These are the files you'll feed to ebook2audiobook.

### 2. Convert a single chapter (for testing)

```bash
E2A=../ebook2audiobook
mkdir -p audiobooks/test

$E2A/python_env/bin/python3 $E2A/app.py \
  --headless \
  --ebook ebooks/mybook_chapters/004_chapter4_paused.txt \
  --language deu \
  --tts_engine PIPER \
  --custom_model mymodel.zip \
  --output_dir audiobooks/test
```

### 3. Convert the full book

Pass an `--ebooks_dir` instead of `--ebook` to convert all chapters:

```bash
E2A=../ebook2audiobook
mkdir -p audiobooks/mybook

$E2A/python_env/bin/python3 $E2A/app.py \
  --headless \
  --ebooks_dir ebooks/mybook_chapters \
  --language deu \
  --tts_engine PIPER \
  --custom_model mymodel.zip \
  --output_dir audiobooks/mybook
```

The individual chapter `.m4b` files can then be joined with ffmpeg or imported directly into an audiobook player that handles folders.

## Packaging a custom Piper model

ebook2audiobook expects a zip containing exactly these files:

```
model.onnx        ← your .onnx file, renamed
config.onnx.json  ← your .onnx.json file, renamed
ref.wav           ← short voice sample (used internally, does not affect synthesis)
```

Build it:

```python
import zipfile, wave, struct, math, json

# tweak length_scale for speaking rate (1.0 = default, 1.1 = ~10% slower)
with open("de_DE-thorsten-medium.onnx.json") as f:
    cfg = json.load(f)
cfg["inference"]["length_scale"] = 1.1
with open("de_DE-thorsten-medium.onnx.json", "w") as f:
    json.dump(cfg, f)

# minimal ref.wav — replace with a real voice sample for better results
sr, n = 22050, 22050
with wave.open("ref.wav", "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(struct.pack(f"<{n}h", *[int(8000*math.sin(2*math.pi*440*t/sr)) for t in range(n)]))

with zipfile.ZipFile("mymodel.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.write("de_DE-thorsten-medium.onnx", "model.onnx")
    z.write("de_DE-thorsten-medium.onnx.json", "config.onnx.json")
    z.write("ref.wav", "ref.wav")
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
