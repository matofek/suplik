#!/usr/bin/env python3
"""
Skenuje priečinky galeria/<KOD>/ pre 11 odborov ŠUP Košice,
generuje optimalizované WebP náhľady do galeria/thumbs/<KOD>/
a vytvára galeria/manifest.json zoradený od najnovších prác.

Možno spúšťať automaticky cez GitHub Actions aj lokálne:
    python3 scripts/build_manifest.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
GALLERY_DIR = ROOT / "galeria"
THUMBS_DIR = GALLERY_DIR / "thumbs"
MANIFEST_PATH = GALLERY_DIR / "manifest.json"

DEPARTMENTS = [
    "ANI", "DDA", "DEX", "DIM", "FOD", "GDI",
    "GPD", "KRM", "ODE", "PRV", "SCE",
]

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_MAX_WIDTH = 800
THUMB_QUALITY = 82
NEW_BADGE_DAYS = 30


def get_git_timestamp(file_path: Path) -> int:
    """Vráti UNIX timestamp posledného commitu súboru (fallback na mtime)."""
    try:
        rel = file_path.relative_to(ROOT).as_posix()
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct", "--", rel],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    return int(file_path.stat().st_mtime)


def parse_filename(filename: str, fallback_year: str) -> dict[str, str]:
    """
    Podporované konvencie:
    1) 2026_Ema-Kovacova_Plagat-k-vystave.jpg -> year, author, title
    2) Ema-Kovacova_Plagat-k-vystave.jpg      -> fallback_year, author, title
    3) Plagat-k-vystave.jpg                   -> fallback_year, "", title
    """
    stem = Path(filename).stem
    parts = [p for p in stem.split("_") if p]

    if len(parts) >= 3 and re.fullmatch(r"\d{4}", parts[0]):
        return {
            "year": parts[0],
            "author": parts[1].replace("-", " ").strip(),
            "title": " ".join(parts[2:]).replace("-", " ").strip(),
        }
    if len(parts) == 2:
        return {
            "year": fallback_year,
            "author": parts[0].replace("-", " ").strip(),
            "title": parts[1].replace("-", " ").strip(),
        }
    return {
        "year": fallback_year,
        "author": "",
        "title": re.sub(r"[-_]+", " ", stem).strip() or "Bez názvu",
    }


def make_thumbnail(src_path: Path, thumb_path: Path) -> tuple[int, int]:
    """Prečíta rozmery po EXIF orientácii a vygeneruje/aktualizuje WebP náhľad."""
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        width, height = img.size

        needs_build = (
            not thumb_path.exists()
            or thumb_path.stat().st_mtime < src_path.stat().st_mtime
        )

        if needs_build:
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            thumb = img.copy()
            if thumb.width > THUMB_MAX_WIDTH:
                new_h = max(1, round(thumb.height * (THUMB_MAX_WIDTH / thumb.width)))
                thumb = thumb.resize((THUMB_MAX_WIDTH, new_h), Image.Resampling.LANCZOS)

            if thumb.mode not in ("RGB", "RGBA"):
                thumb = thumb.convert("RGBA" if "A" in thumb.getbands() else "RGB")

            thumb.save(thumb_path, format="WEBP", quality=THUMB_QUALITY, method=6)

        return width, height


def encode_rel_url(path: Path) -> str:
    rel_parts = path.relative_to(ROOT).parts
    return "./" + "/".join(quote(part) for part in rel_parts)


def build_manifest() -> dict:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    departments_data: dict[str, list[dict]] = {}

    for code in DEPARTMENTS:
        dept_dir = GALLERY_DIR / code
        dept_thumb_dir = THUMBS_DIR / code
        items: list[dict] = []
        valid_thumbs: set[Path] = set()

        if dept_dir.is_dir():
            for entry in sorted(dept_dir.iterdir()):
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                if entry.suffix.lower() not in ALLOWED_EXTS:
                    continue

                added_at = get_git_timestamp(entry)
                fallback_year = str(datetime.fromtimestamp(added_at, tz=timezone.utc).year)
                meta = parse_filename(entry.name, fallback_year)

                thumb_name = f"{entry.stem}.webp"
                thumb_path = dept_thumb_dir / thumb_name

                try:
                    width, height = make_thumbnail(entry, thumb_path)
                    valid_thumbs.add(thumb_path.resolve())
                    thumb_url = encode_rel_url(thumb_path)
                except Exception as exc:
                    print(f"[WARN] Nepodarilo sa spracovať {entry}: {exc}")
                    width, height = 800, 800
                    thumb_url = encode_rel_url(entry)

                items.append(
                    {
                        "id": f"{code}-{entry.stem}",
                        "filename": entry.name,
                        "title": meta["title"],
                        "author": meta["author"],
                        "year": meta["year"],
                        "width": width,
                        "height": height,
                        "addedAt": added_at,
                        "thumbUrl": thumb_url,
                        "fullUrl": encode_rel_url(entry),
                    }
                )

        # Zoradenie od najnovších (pri rovnakom čase commitu podľa názvu zostupne)
        items.sort(key=lambda x: (x["addedAt"], x["filename"].lower()), reverse=True)

        # Označenie najnovších prác (mladšie než NEW_BADGE_DAYS, max prvé 3)
        for idx, item in enumerate(items):
            item["isNew"] = idx < 3 and (now_ts - item["addedAt"]) <= NEW_BADGE_DAYS * 86400

        departments_data[code] = items

        # Upratanie náhľadov, ktorých originál už v priečinku neexistuje
        if dept_thumb_dir.is_dir():
            for thumb_file in dept_thumb_dir.iterdir():
                if thumb_file.is_file() and thumb_file.resolve() not in valid_thumbs:
                    thumb_file.unlink()
            if not any(dept_thumb_dir.iterdir()):
                dept_thumb_dir.rmdir()

    # Ak nezostali žiadne náhľady, uprac prázdny priečinok thumbs
    if THUMBS_DIR.is_dir() and not any(THUMBS_DIR.iterdir()):
        THUMBS_DIR.rmdir()

    manifest = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "departments": departments_data,
    }

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    total = sum(len(v) for v in departments_data.values())
    print(f"Manifest vygenerovaný: {MANIFEST_PATH} ({total} prác)")
    return manifest


if __name__ == "__main__":
    build_manifest()
