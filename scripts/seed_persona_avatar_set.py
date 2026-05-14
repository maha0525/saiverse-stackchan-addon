#!/usr/bin/env python3
"""Seed a persona's avatar set into addon storage for Phase 4.5-c testing.

Generates a color-coded raw RGB565 placeholder set (= same idea as
``generate_test_avatar_set.py``) and writes it into the location the
``avatar_loader`` server_hook expects:

    ~/.saiverse/addons/saiverse-stackchan-addon/avatar_sets/<persona_id>/<set_name>/
        avatar.bin       # raw RGB565 payload (537,600 or 3,456,000 bytes)
        manifest.json    # {"mode": "...", "checksum": "sha256:..."}

Multi-persona test cycle (Phase 4.5-c):
    python seed_persona_avatar_set.py --persona-id air_city_a   --mode layered
    python seed_persona_avatar_set.py --persona-id elyth_city_a --mode layered --tint-hue 180

The ``--tint-hue`` option rotates all colours in HSV so persona A and B can
be told apart by eye on the LCD without needing real art. Then憑依 A → see
one palette, 憑依 B → see the other.

The seeded files live under ``~/.saiverse/`` outside the repo, so seeding
is a per-machine setup step.
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

# Reuse the frame palette + generators from generate_test_avatar_set.py
# without forcing a package layout on the addon scripts/ dir.
_SCRIPTS_DIR = Path(__file__).parent
_GEN_PATH = _SCRIPTS_DIR / "generate_test_avatar_set.py"
_spec = importlib.util.spec_from_file_location(
    "_stackchan_test_avatar_gen", str(_GEN_PATH)
)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"failed to load generator at {_GEN_PATH}")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)


def _rotate_hue(
    rgb: tuple[int, int, int], hue_offset_deg: float
) -> tuple[int, int, int]:
    """Rotate an (R, G, B) colour by ``hue_offset_deg`` degrees in HSV."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h = (h + hue_offset_deg / 360.0) % 1.0
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


# Override the base generator's neutral-grey idle ((150, 150, 150), sat=0)
# with a low-saturation cool grey so that the ``--tint-hue`` rotation
# actually changes the visible colour. HSV hue rotation is a no-op on a
# zero-saturation pixel, which made two seeded personas indistinguishable
# on the idle face. (150, 165, 195) has hue ≈ 213° (cool blue-grey), sat
# ≈ 23 %, which still reads as "calm idle" but rotates to a warm beige
# at 180°.
_IDLE_BASELINE_RGB = (150, 165, 195)


def _generate_tinted(mode: str, tint_hue_deg: float) -> bytes:
    """Re-run the layered / matrix generator with hue-rotated palettes.

    Also patches the idle baseline so that idle is hue-rotatable even at
    ``tint_hue_deg=0`` (= explicit cool-grey vs. the generator's default
    pure-grey). See the ``_IDLE_BASELINE_RGB`` comment for why.
    """
    # The generators read FACE_COLORS / EYES_COLORS / MOUTH_COLORS from
    # the module's globals, so monkey-patch them for the duration of this
    # call and restore afterwards.
    saved = {
        "FACE_COLORS": dict(_gen.FACE_COLORS),
        "EYES_COLORS": dict(_gen.EYES_COLORS),
        "MOUTH_COLORS": dict(_gen.MOUTH_COLORS),
    }
    try:
        _gen.FACE_COLORS["idle"] = _IDLE_BASELINE_RGB
        if tint_hue_deg != 0.0:
            for table_name in ("FACE_COLORS", "EYES_COLORS", "MOUTH_COLORS"):
                table = getattr(_gen, table_name)
                for key, rgb in list(table.items()):
                    table[key] = _rotate_hue(rgb, tint_hue_deg)
        if mode == "layered":
            return _gen.generate_layered()
        return _gen.generate_matrix()
    finally:
        for table_name, original in saved.items():
            table = getattr(_gen, table_name)
            table.clear()
            table.update(original)


def _addon_storage_dir() -> Path:
    """Resolve ``~/.saiverse/addons/saiverse-stackchan-addon/`` without
    importing SAIVerse internals (the script may be run outside the
    SAIVerse Python environment).
    """
    home = Path.home() / ".saiverse"
    # Honour SAIVERSE_HOME like data_paths.py does.
    import os

    override = os.environ.get("SAIVERSE_HOME")
    if override:
        home = Path(override)
    return home / "addons" / "saiverse-stackchan-addon"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--persona-id", required=True,
        help="Persona ID (= AIID column in saiverse.db). The seeded set "
             "will auto-load when this persona enters the Vessel Building.",
    )
    parser.add_argument(
        "--mode", choices=["layered", "matrix"], default="layered",
        help="Avatar set mode (default: layered).",
    )
    parser.add_argument(
        "--set-name", default="default",
        help="Set name under the persona's avatar_sets/ directory (default: 'default'). "
             "Phase 4.5-c only loads 'default'; future multi-set support will use this.",
    )
    parser.add_argument(
        "--tint-hue", type=float, default=0.0,
        help="Rotate the test palette by this many degrees in HSV so "
             "different personas' sets are visually distinguishable on the "
             "LCD. 0 = no change; 120/180/240 are useful for 2-3 personas.",
    )
    args = parser.parse_args()

    payload = _generate_tinted(args.mode, args.tint_hue)
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()

    set_dir = _addon_storage_dir() / "avatar_sets" / args.persona_id / args.set_name
    set_dir.mkdir(parents=True, exist_ok=True)
    bin_path = set_dir / "avatar.bin"
    manifest_path = set_dir / "manifest.json"

    bin_path.write_bytes(payload)
    manifest = {
        "mode": args.mode,
        "checksum": checksum,
        "source": "seed_persona_avatar_set",
        "tint_hue_deg": args.tint_hue,
        "byte_count": len(payload),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"Seeded persona={args.persona_id} mode={args.mode} "
        f"set={args.set_name} tint={args.tint_hue}deg"
    )
    print(f"  bin:      {bin_path} ({len(payload):,} bytes)")
    print(f"  manifest: {manifest_path}")
    print(f"  checksum: {checksum}")


if __name__ == "__main__":
    main()
