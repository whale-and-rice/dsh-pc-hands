"""RapidOCR adapter: the good Chinese OCR we actually want.

Why this exists: Windows' built-in OCR cannot read WeChat's list rows at all
(tried cropping, grayscale, thresholding, 2x/3x/4x upscaling — every variant
returned zero lines, while RapidOCR reads them at confidence 1.00). It is also
the difference between "scan the screen by clicking blindly and checking the
title each time" and "click the row once".

Model: PP-OCR (det + rec) exported to ONNX, runs on CPU via onnxruntime.
~4 s for a 1440x1080 screenshot; a cropped region is much faster.

API kept deliberately tiny and shaped like the Windows OCR output so the
callers in pc.py do not care which engine produced the words.
"""

from __future__ import annotations

import os

_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def run(image: str, region=None) -> dict:
    """Return {"ok", "words":[{t,x,y,w,h}], "lines":[...]} in screen pixels.

    Coordinates are stable regardless of `region` because the crop offset is
    added back — callers never have to know they were looking at a crop.
    """
    if not available():
        return {"ok": False, "error": "rapidocr_onnxruntime not installed", "words": [], "lines": []}

    src = image
    x0 = y0 = 0
    if region:
        from PIL import Image
        import tempfile
        x0, y0, x1, y1 = region
        crop = Image.open(image).convert("RGB").crop((x0, y0, x1, y1))
        src = os.path.join(tempfile.gettempdir(), "dsh-rapid-crop.png")
        crop.save(src)

    try:
        result, _elapse = _engine()(src)
    except Exception as exc:  # engine/model problem must not kill a task
        return {"ok": False, "error": str(exc), "words": [], "lines": []}

    words, lines = [], []
    for item in (result or []):
        box, text = item[0], item[1]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x = int(min(xs)) + x0
        y = int(min(ys)) + y0
        w = int(max(xs) - min(xs))
        h = int(max(ys) - min(ys))
        words.append({"t": text, "x": x, "y": y, "w": w, "h": h})
        lines.append(text)

    return {"ok": True, "engine": "rapidocr", "image": image, "words": words, "lines": lines}
