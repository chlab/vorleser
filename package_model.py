#!/usr/bin/env python3
"""
Package a Piper .onnx model into the zip format expected by ebook2audiobook.

Usage:
    python3 package_model.py <model.onnx> [config.onnx.json] [output.zip]

    model.onnx        path to the Piper model file
    config.onnx.json  defaults to <model>.onnx.json alongside the model
    output.zip        defaults to <model-stem>.zip in the current directory

The length_scale in the config is set to 1.1 (slightly slower than default).
Edit LENGTH_SCALE below to adjust speaking rate.
"""
import argparse, json, sys, zipfile
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Package a Piper model for ebook2audiobook")
    parser.add_argument("model",        type=Path, help=".onnx model file")
    parser.add_argument("config",       type=Path, nargs="?", help=".onnx.json config (default: <model>.onnx.json)")
    parser.add_argument("output",       type=Path, nargs="?", help="output zip (default: <model-stem>.zip)")
    parser.add_argument("--length-scale", type=float, default=1.1,
                        help="Piper speaking rate multiplier — higher is slower (default: 1.1)")
    args = parser.parse_args()

    model_path  = args.model
    config_path = args.config  if args.config  else model_path.with_suffix(".onnx.json")
    zip_path    = args.output  if args.output  else Path(model_path.stem).with_suffix(".zip")

    if not model_path.exists():
        sys.exit(f"Model not found: {model_path}")
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path} (pass it explicitly as second argument)")

    ref_wav = Path(__file__).parent / "ref.wav"
    if not ref_wav.exists():
        sys.exit("ref.wav not found — it should be in the same directory as this script")

    cfg = json.loads(config_path.read_text())
    cfg.setdefault("inference", {})["length_scale"] = args.length_scale
    config_patched = config_path.read_bytes()  # re-read below after patching
    config_patched = json.dumps(cfg, ensure_ascii=False, indent=2).encode()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(model_path, "model.onnx")
        z.writestr("config.onnx.json", config_patched)
        z.write(ref_wav, "ref.wav")

    print(f"Written: {zip_path}  ({zip_path.stat().st_size // 1024 // 1024} MB)")
    print(f"  model        : {model_path.name}")
    print(f"  config       : {config_path.name}  (length_scale={args.length_scale})")
    print(f"  ref.wav      : {ref_wav.name}  (placeholder)")

if __name__ == "__main__":
    main()
