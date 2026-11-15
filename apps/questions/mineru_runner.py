"""Standalone MinerU runner — executed by the MinerU virtualenv, not Django.

This script is intentionally free of any Django import. The Django app invokes it
as a subprocess using the interpreter configured in ``settings.MINERU_PYTHON``
(a separate, heavy virtualenv), so MinerU's large dependency tree never enters
the web application's environment.

It parses one PDF **entirely locally** with MinerU's torch/ONNX pipeline (no
cloud, no ``--remote`` service, telemetry disabled) and writes:

    <out>/content_list.json   ordered blocks: text (Markdown+LaTeX), equation,
                              table (HTML), image (saved to images/ and
                              referenced by relative path)
    <out>/images/*.jpg        extracted figure crops

Usage:
    python mineru_runner.py --pdf INPUT.pdf --out OUTDIR [--effort medium]
                            [--pages all] [--lang ...]

Effort ``flash``/``medium`` use the local torch+ONNX pipeline (layout, formula
recognition, OCR, tables). ``high``/``xhigh`` would require a VLM and are not
used here, so no ``llama.cpp``/VLM component is loaded.
"""
import argparse
import base64
import json
import multiprocessing
import os
import sys


def _save_images(content, out_dir):
    """Write base64 image payloads to files and rewrite img_path to a relative path."""
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    index = 0
    for item in content:
        if item.get("type") not in ("image", "figure"):
            continue
        path = item.get("img_path") or ""
        if not path.startswith("data:"):
            continue
        try:
            header, payload = path.split(",", 1)
            ext = "png" if "png" in header else "jpg"
            raw = base64.b64decode(payload)
        except Exception:
            item["img_path"] = ""
            continue
        name = f"img_{index}.{ext}"
        with open(os.path.join(images_dir, name), "wb") as handle:
            handle.write(raw)
        item["img_path"] = f"images/{name}"
        index += 1
    return index


def run(args):
    # Force local-only, quiet, no telemetry. Model source is configurable; it only
    # affects where *model weights* are fetched from, never the user's document.
    os.environ.setdefault("MINERU_TELEMETRY_DISABLED", "1")
    os.environ.setdefault("MINERU_MODEL_SOURCE", args.model_source)

    from mineru.backend.analyze import doc_analyze
    from mineru.render.content_list import render_content_list

    with open(args.pdf, "rb") as handle:
        data = handle.read()

    middle_json, _model_json = doc_analyze(
        data, effort=args.effort, parse_mode="auto", image_analysis=True
    )
    content = render_content_list(middle_json)
    os.makedirs(args.out, exist_ok=True)
    image_count = _save_images(content, args.out)
    with open(os.path.join(args.out, "content_list.json"), "w", encoding="utf-8") as handle:
        json.dump(content, handle, ensure_ascii=False)
    # A machine-readable summary line the caller can parse from stdout.
    print(json.dumps({"ok": True, "items": len(content), "images": image_count}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Local MinerU PDF parser (no cloud).")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--effort", default="medium", choices=["flash", "medium"])
    parser.add_argument("--pages", default="all")
    parser.add_argument("--model-source", default=os.environ.get("MINERU_MODEL_SOURCE", "huggingface"))
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:  # pragma: no cover - exercised via subprocess
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
