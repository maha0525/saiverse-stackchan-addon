#!/usr/bin/env python3
"""Generate a placeholder raw RGB565 avatar set for end-to-end testing.

Phase 4.5 (saiverse-stackchan-addon). Used to exercise the
``load_avatar_set`` MCP tool + gateway HTTP staging + firmware
AvatarSetFetcher + AvatarSet::Load loop before the real Phase 4.5-d
image generation pipeline is in place.

Each frame is 160×120 RGB565 LE, matching ``AvatarSet::kImageBytes``
(38,400 bytes / frame). Total payload size:

- layered mode: 14 × 38,400 = 537,600 bytes (face×6 + eyes×3 + mouth×5)
- matrix  mode: 90 × 38,400 = 3,456,000 bytes (face×6 × eyes×3 × mouth×5)

Usage:
    python generate_test_avatar_set.py --mode layered \
        --output /tmp/test_layered.bin
    python generate_test_avatar_set.py --mode matrix  \
        --output /tmp/test_matrix.bin

The output file is passed to the ``load_avatar_set`` MCP tool's
``archive_path`` argument; the gateway streams it to the device.

Frames are colour-coded so the LCD readout makes it obvious which slot
is being shown:

- layered face : grey / yellow / purple / blue / orange / pink
- layered eyes : green / yellow-green / brown
- layered mouth: 5 shades of red
- matrix       : average of the corresponding face/eyes/mouth colours,
                 with the slot label printed in the centre
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
EYES = ["open", "half", "closed"]
MOUTHS = ["closed", "half", "open", "e", "u"]

FACE_COLORS: dict[str, tuple[int, int, int]] = {
    "idle":        (150, 150, 150),
    "happy":       (255, 200,   0),
    "thinking":    (180, 120, 220),
    "sad":         ( 80, 120, 200),
    "surprised":   (255, 140,   0),
    "embarrassed": (255, 150, 180),
}
EYES_COLORS: dict[str, tuple[int, int, int]] = {
    "open":   ( 40, 200,  40),
    "half":   (180, 200,  40),
    "closed": (120,  80,  40),
}
MOUTH_COLORS: dict[str, tuple[int, int, int]] = {
    "closed": (200,  40,  40),
    "half":   (200,  80,  60),
    "open":   (200, 100,  80),
    "e":      (220, 100, 100),
    "u":      (220, 120, 120),
}

W = 160
H = 120
FRAME_BYTES = W * H * 2  # RGB565 LE


def rgb888_to_rgb565_bytes(im: Image.Image) -> bytes:
    """Pack a 160×120 RGB image into little-endian RGB565 bytes (LVGL native)."""
    if im.size != (W, H):
        im = im.resize((W, H))
    if im.mode != "RGB":
        im = im.convert("RGB")
    px = im.tobytes()
    out = bytearray(W * H * 2)
    j = 0
    for i in range(0, len(px), 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[j]     = v & 0xFF
        out[j + 1] = (v >> 8) & 0xFF
        j += 2
    return bytes(out)


def _make_frame(label: str, color: tuple[int, int, int]) -> bytes:
    im = Image.new("RGB", (W, H), color)
    draw = ImageDraw.Draw(im)
    # Inset border so the frame edge is visible against the LCD background.
    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=(0, 0, 0))
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((W - tw) // 2, (H - th) // 2), label, fill=(0, 0, 0), font=font)
    return rgb888_to_rgb565_bytes(im)


def generate_layered() -> bytes:
    buf = bytearray()
    for f in FACES:
        buf += _make_frame(f"face/{f}", FACE_COLORS[f])
    for e in EYES:
        buf += _make_frame(f"eyes/{e}", EYES_COLORS[e])
    for m in MOUTHS:
        buf += _make_frame(f"mouth/{m}", MOUTH_COLORS[m])
    assert len(buf) == 14 * FRAME_BYTES, f"layered size mismatch: {len(buf)}"
    return bytes(buf)


def generate_matrix() -> bytes:
    buf = bytearray()
    for f in FACES:
        for e in EYES:
            for m in MOUTHS:
                fc, ec, mc = FACE_COLORS[f], EYES_COLORS[e], MOUTH_COLORS[m]
                color = (
                    (fc[0] + ec[0] + mc[0]) // 3,
                    (fc[1] + ec[1] + mc[1]) // 3,
                    (fc[2] + ec[2] + mc[2]) // 3,
                )
                buf += _make_frame(f"{f[:3]}/{e[:3]}/{m[:3]}", color)
    assert len(buf) == 90 * FRAME_BYTES, f"matrix size mismatch: {len(buf)}"
    return bytes(buf)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["layered", "matrix"], required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    payload = generate_layered() if args.mode == "layered" else generate_matrix()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(f"Wrote {args.mode} avatar set: {args.output} ({len(payload):,} bytes)")


if __name__ == "__main__":
    main()
