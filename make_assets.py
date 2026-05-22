from pathlib import Path
import json
import math

import imageio
import numpy as np
from PIL import Image, ImageFilter


ROOT = Path(__file__).resolve().parent
VIDEO = Path(
    r"C:\Users\向鹏\Downloads\jimeng-2026-05-21-6263-图一的猫咪按照图二的说明，头部上下左右转一圈，身体不动，固定镜头，不要缩放镜头，....mp4"
)

# Calibrated from contact_sheet.jpg. Angles are screen-space degrees:
# 0 = right, 90 = down, 180 = left, 270 = up. These anchors are dense
# observations from the actual video, then expanded to 5-degree keys below.
ANGLE_ANCHORS = [
    {"angle": 0, "sourceFrame": 204, "label": "right"},
    {"angle": 18, "sourceFrame": 213, "label": "right"},
    {"angle": 32, "sourceFrame": 225, "label": "right-down"},
    {"angle": 48, "sourceFrame": 234, "label": "right-down"},
    {"angle": 66, "sourceFrame": 246, "label": "down"},
    {"angle": 90, "sourceFrame": 276, "label": "down"},
    {"angle": 112, "sourceFrame": 297, "label": "down"},
    {"angle": 132, "sourceFrame": 318, "label": "left-down"},
    {"angle": 150, "sourceFrame": 336, "label": "left-down"},
    {"angle": 180, "sourceFrame": 348, "label": "left"},
    {"angle": 205, "sourceFrame": 369, "label": "left-up"},
    {"angle": 225, "sourceFrame": 387, "label": "left-up"},
    {"angle": 246, "sourceFrame": 408, "label": "up"},
    {"angle": 270, "sourceFrame": 420, "label": "up"},
    {"angle": 292, "sourceFrame": 81, "label": "right-up"},
    {"angle": 315, "sourceFrame": 123, "label": "right-up"},
    {"angle": 338, "sourceFrame": 162, "label": "right-up"},
    {"angle": 360, "sourceFrame": 204, "label": "right"},
]
CENTER_FRAME = 0
FRAME_SIZE = 760
ATLAS_COLS = 12
ANGLE_STEP = 5


def build_angle_keys():
    anchors = sorted(ANGLE_ANCHORS, key=lambda item: item["angle"])
    keys = []
    for angle in range(0, 360, ANGLE_STEP):
        prev_item = anchors[0]
        next_item = anchors[-1]
        for idx in range(len(anchors) - 1):
            if anchors[idx]["angle"] <= angle <= anchors[idx + 1]["angle"]:
                prev_item, next_item = anchors[idx], anchors[idx + 1]
                break
        span = max(1, next_item["angle"] - prev_item["angle"])
        t = (angle - prev_item["angle"]) / span
        frame_delta = next_item["sourceFrame"] - prev_item["sourceFrame"]
        if abs(frame_delta) > 150:
            source_frame = prev_item["sourceFrame"] if t < 0.5 else next_item["sourceFrame"]
        else:
            source_frame = round(prev_item["sourceFrame"] + frame_delta * t)
        label = prev_item["label"] if t < 0.5 else next_item["label"]
        keys.append({"angle": angle, "sourceFrame": source_frame, "label": label})
    return keys


ANGLE_KEYS = build_angle_keys()


def circular_distance(a, b):
    return abs((a - b + 180) % 360 - 180)


def read_frames(frame_numbers):
    needed = set(frame_numbers)
    found = {}
    reader = imageio.get_reader(str(VIDEO), "ffmpeg")
    try:
        for idx, frame in enumerate(reader):
            if idx in needed:
                found[idx] = frame[..., :3].copy()
                if len(found) == len(needed):
                    break
    finally:
        reader.close()
    missing = sorted(needed - set(found))
    if missing:
        raise RuntimeError(f"Missing frames: {missing}")
    return found


def green_screen_alpha(rgb):
    arr = rgb.astype(np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    # Learn the green screen from the corners. This keeps the key specific to
    # the actual plate instead of removing arbitrary light or gray pixels.
    h, w = rgb.shape[:2]
    corner = max(48, min(h, w) // 12)
    samples = np.concatenate(
        [
            rgb[:corner, :corner].reshape(-1, 3),
            rgb[:corner, -corner:].reshape(-1, 3),
            rgb[-corner:, :corner].reshape(-1, 3),
            rgb[-corner:, -corner:].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    bg = np.median(samples, axis=0)
    bg_dist = np.linalg.norm(arr - bg, axis=2)
    chroma = arr / np.maximum(arr.sum(axis=2, keepdims=True), 1)
    bg_chroma = bg / max(float(bg.sum()), 1.0)
    chroma_dist = np.linalg.norm(chroma - bg_chroma, axis=2)

    saturation_guard = (g - np.maximum(r, b))
    green_dominant = (g > 32) & (saturation_guard > 5)
    bg_like = green_dominant & ((bg_dist < 92) | (chroma_dist < 0.13))

    alpha = np.full((h, w), 255, dtype=np.uint8)
    alpha[bg_like] = 0

    # Spill and antialiasing zone. The low-saturation guard prevents white and
    # gray subject regions from being keyed out.
    soft = green_dominant & ~bg_like & ((bg_dist < 174) | (chroma_dist < 0.2))
    softness = np.maximum((bg_dist[soft] - 92) / 82, (chroma_dist[soft] - 0.13) / 0.07)
    alpha[soft] = np.clip(softness * 255, 0, 255).astype(np.uint8)

    pil_alpha = Image.fromarray(alpha, "L")
    pil_alpha = pil_alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MedianFilter(3)).filter(
        ImageFilter.GaussianBlur(0.55)
    )
    alpha = np.array(pil_alpha)

    # Desaturate green spill only where pixels are already semitransparent.
    out = rgb.copy().astype(np.float32)
    spill = (alpha > 0) & (alpha < 245) & green_dominant
    out[..., 1][spill] = np.minimum(out[..., 1][spill], (out[..., 0][spill] + out[..., 2][spill]) * 0.58)
    return np.dstack([np.clip(out, 0, 255).astype(np.uint8), alpha])


def subject_bbox(rgba):
    alpha = rgba[..., 3]
    ys, xs = np.where(alpha > 12)
    if len(xs) == 0:
        return (0, 0, rgba.shape[1], rgba.shape[0])
    pad = 28
    return (
        max(0, int(xs.min()) - pad),
        max(0, int(ys.min()) - pad),
        min(rgba.shape[1], int(xs.max()) + pad + 1),
        min(rgba.shape[0], int(ys.max()) + pad + 1),
    )


def fit_to_canvas(rgba, bbox):
    img = Image.fromarray(rgba, "RGBA").crop(bbox)
    img.thumbnail((FRAME_SIZE, FRAME_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (FRAME_SIZE, FRAME_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((FRAME_SIZE - img.width) // 2, FRAME_SIZE - img.height))
    return canvas


def main():
    source_frames = [CENTER_FRAME] + [item["sourceFrame"] for item in ANGLE_KEYS]
    raw = read_frames(source_frames)
    keyed = {frame: green_screen_alpha(arr) for frame, arr in raw.items()}

    boxes = [subject_bbox(img) for img in keyed.values()]
    bbox = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )

    sprite_frames = []
    frame_meta = []
    center_img = fit_to_canvas(keyed[CENTER_FRAME], bbox)
    center_img.save(ROOT / "frame_front.webp", "WEBP", quality=96, method=6, lossless=False)

    for idx, item in enumerate(ANGLE_KEYS):
        sprite_frames.append(fit_to_canvas(keyed[item["sourceFrame"]], bbox))
        frame_meta.append({**item, "spriteIndex": idx})

    rows = math.ceil(len(sprite_frames) / ATLAS_COLS)
    sprite = Image.new(
        "RGBA",
        (FRAME_SIZE * ATLAS_COLS, FRAME_SIZE * rows),
        (0, 0, 0, 0),
    )
    for idx, frame in enumerate(sprite_frames):
        x = (idx % ATLAS_COLS) * FRAME_SIZE
        y = (idx // ATLAS_COLS) * FRAME_SIZE
        sprite.alpha_composite(frame, (x, y))
    sprite.save(ROOT / "sprite.webp", "WEBP", quality=96, method=6, lossless=False)
    for row in range(rows):
        row_img = sprite.crop((0, row * FRAME_SIZE, FRAME_SIZE * ATLAS_COLS, (row + 1) * FRAME_SIZE))
        row_img.save(
            ROOT / f"sprite-row-{row}.webp",
            "WEBP",
            quality=96,
            method=6,
            lossless=False,
        )

    (ROOT / "asset-meta.json").write_text(
        json.dumps(
            {
                "frameSize": FRAME_SIZE,
                "atlasCols": ATLAS_COLS,
                "angleStep": ANGLE_STEP,
                "centerFrame": CENTER_FRAME,
                "angleKeys": frame_meta,
                "bbox": bbox,
                "note": "ANGLE_KEYS were calibrated from contact_sheet.jpg; mapping is nearest-key, not linear frame/angle sampling.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote sprite.webp, frame_front.webp, asset-meta.json")


if __name__ == "__main__":
    main()
