#!/usr/bin/env python3
"""
make_icons.py — Génère les icônes PWA de ConsultAI.
====================================================

Aucune dépendance externe (ni Pillow, ni ImageMagick, dont le décodeur SVG est
désactivé par la politique de sécurité de DSM) : les PNG sont encodés à la main
avec ``zlib``, et le dessin repose sur des champs de distance signés, ce qui
donne un anticrénelage propre à toutes les tailles.

Utilisation :
    python3 tools/make_icons.py                 # jeu complet, design courant
    python3 tools/make_icons.py --preview       # une vignette par design
    python3 tools/make_icons.py --design onde   # change le design retenu
"""

from __future__ import annotations

import math
import os
import struct
import sys
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(BASE, "..", "app", "static", "icons"))
PREVIEW_DIR = os.path.normpath(os.path.join(BASE, "previews"))

# Design retenu pour le jeu d'icônes final.
# « note-pli » : page repliée portant une onde vocale — dit à la fois la
# dictée et le document clinique, là où une onde seule évoquerait une
# application de musique et un micro seul un simple dictaphone.
DEFAULT_DESIGN = "note-pli"

# Dégradé diagonal, du sarcelle clair au sarcelle profond (palette Tailwind
# teal-600 → teal-800), plus riche qu'un aplat.
GRAD_FROM = (13, 148, 136)    # #0d9488
GRAD_TO = (17, 94, 89)        # #115e59
WHITE = (255, 255, 255)
INK = (15, 118, 110)          # #0f766e — traits sur fond blanc

SUPERSAMPLE = 3


# ---------------------------------------------------------------------------
# Encodage PNG (RGBA 8 bits)
# ---------------------------------------------------------------------------
def write_png(path: str, width: int, height: int, pixels: bytearray) -> None:
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)  # filtre « None »
        rows.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)


# ---------------------------------------------------------------------------
# Primitives (champs de distance signés, coordonnées centrées sur 0)
# ---------------------------------------------------------------------------
def sdf_capsule(x, y, ax, ay, bx, by, radius) -> float:
    """Segment épais à extrémités arrondies — la brique de base ici."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy)) - radius


def sdf_round_rect(x, y, cx, cy, half_w, half_h, radius) -> float:
    qx = abs(x - cx) - (half_w - radius)
    qy = abs(y - cy) - (half_h - radius)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius


def sdf_ring_arc(x, y, radius, thickness, angle_from, angle_to) -> float:
    """
    Portion d'anneau. Les angles sont en degrés, 0° à droite, sens horaire
    vers le bas (repère écran).
    """
    distance = math.hypot(x, y)
    ring = abs(distance - radius) - thickness
    angle = math.degrees(math.atan2(y, x))
    if angle < 0:
        angle += 360.0
    inside = angle_from <= angle <= angle_to
    return ring if inside else 1.0


def union(*values) -> float:
    return min(values)


def subtract(shape, hole) -> float:
    """Retire ``hole`` de ``shape``."""
    return max(shape, -hole)


# ---------------------------------------------------------------------------
# Designs — chacun renvoie (couverture_blanche, couverture_encre)
# ---------------------------------------------------------------------------
# Les deux couvertures sont séparées pour pouvoir dessiner de l'encre sarcelle
# PAR-DESSUS une forme blanche (page + tracé), impossible avec un seul canal.
# ---------------------------------------------------------------------------
def design_note(x, y):
    """Page de note portant une onde vocale — dictée ET document."""
    # Page : rectangle arrondi vertical
    page = sdf_round_rect(x, y, 0.0, 0.0, 0.215, 0.275, 0.045)

    # Onde vocale à l'intérieur : 5 barres verticales arrondies
    bars = [
        (-0.120, 0.055),
        (-0.060, 0.115),
        (0.000, 0.170),
        (0.060, 0.105),
        (0.120, 0.050),
    ]
    wave = 1.0
    for bx, half_h in bars:
        wave = min(wave, sdf_capsule(x, y, bx, -half_h, bx, half_h, 0.021))

    return (1.0 if page <= 0 else 0.0), (1.0 if wave <= 0 else 0.0)


def design_onde(x, y):
    """Onde vocale seule : lecture immédiate, très lisible en petit."""
    bars = [
        (-0.215, 0.075),
        (-0.108, 0.155),
        (0.000, 0.245),
        (0.108, 0.155),
        (0.215, 0.075),
    ]
    wave = 1.0
    for bx, half_h in bars:
        wave = min(wave, sdf_capsule(x, y, bx, -half_h, bx, half_h, 0.036))
    return (1.0 if wave <= 0 else 0.0), 0.0


def design_micro(x, y):
    """Micro avec ondes latérales — variante affinée de l'icône initiale."""
    capsule = sdf_capsule(x, y, 0.0, -0.175, 0.0, 0.010, 0.098)
    cradle = sdf_ring_arc(x, y, 0.205, 0.033, 20, 160)
    stem = sdf_capsule(x, y, 0.0, 0.205, 0.0, 0.285, 0.033)
    base = sdf_capsule(x, y, -0.090, 0.300, 0.090, 0.300, 0.033)
    glyph = union(capsule, cradle, stem, base)

    # Deux arcs sonores de chaque côté, évoquant la voix captée
    left_outer = sdf_ring_arc(x, y, 0.335, 0.026, 155, 205)
    right_outer = sdf_ring_arc(x, y, 0.335, 0.026, -25, 25)
    right_outer = sdf_ring_arc(x, y, 0.335, 0.026, 335, 360) if right_outer > 0 else right_outer
    right_low = sdf_ring_arc(x, y, 0.335, 0.026, 0, 25)
    glyph = union(glyph, left_outer, right_outer, right_low)

    return (1.0 if glyph <= 0 else 0.0), 0.0


def design_pouls(x, y):
    """Page de note portant un tracé de pouls — accent clinique."""
    page = sdf_round_rect(x, y, 0.0, 0.0, 0.215, 0.275, 0.045)

    # Tracé ECG : ligne brisée, segments épais joints
    points = [
        (-0.150, 0.020), (-0.075, 0.020), (-0.045, -0.105),
        (0.000, 0.110), (0.045, -0.055), (0.080, 0.020), (0.150, 0.020),
    ]
    trace = 1.0
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        trace = min(trace, sdf_capsule(x, y, ax, ay, bx, by, 0.022))

    return (1.0 if page <= 0 else 0.0), (1.0 if trace <= 0 else 0.0)


_SQRT2 = math.sqrt(2.0)


def _fold_threshold(half_w, half_h, fold):
    """Position de la diagonale du pli, exprimée en (x - y)."""
    return (half_w - fold) + (half_h - fold)


def _page_with_fold(x, y, half_w, half_h, fold):
    """
    Page à coin supérieur droit replié.

    Le coin est retiré en soustrayant un demi-plan diagonal du rectangle
    arrondi. En repère écran, y est négatif vers le haut : le coin supérieur
    droit est donc la zone où (x - y) est maximal.
    """
    page = sdf_round_rect(x, y, 0.0, 0.0, half_w, half_h, 0.042)
    # Distance signée à la diagonale, positive du côté à retirer
    cut = ((x - y) - _fold_threshold(half_w, half_h, fold)) / _SQRT2
    return max(page, cut)


def _fold_crease(x, y, half_w, half_h, fold, thickness=0.017):
    """Trait marquant le pli, limité à l'intérieur de la page."""
    band = abs((x - y) - _fold_threshold(half_w, half_h, fold)) / _SQRT2 - thickness
    page = sdf_round_rect(x, y, 0.0, 0.0, half_w, half_h, 0.042)
    return max(band, page)


def design_dictee(x, y):
    """
    Page de note repliée, deux lignes de texte et l'onde vocale dessous.

    C'est la synthèse de ce que fait l'application : une dictée qui devient
    une note clinique structurée.
    """
    half_w, half_h, fold = 0.205, 0.285, 0.088
    page = _page_with_fold(x, y, half_w, half_h, fold)
    crease = _fold_crease(x, y, half_w, half_h, fold)

    # Deux lignes de texte en haut
    line1 = sdf_capsule(x, y, -0.115, -0.170, 0.020, -0.170, 0.020)
    line2 = sdf_capsule(x, y, -0.115, -0.100, 0.075, -0.100, 0.020)

    # Onde vocale dans la moitié basse
    bars = [
        (-0.120, 0.045),
        (-0.060, 0.095),
        (0.000, 0.140),
        (0.060, 0.085),
        (0.120, 0.040),
    ]
    wave = 1.0
    for bx, half_bar in bars:
        wave = min(wave, sdf_capsule(x, y, bx, 0.075 - half_bar, bx, 0.075 + half_bar, 0.020))

    ink = union(line1, line2, wave, crease)
    return (1.0 if page <= 0 else 0.0), (1.0 if ink <= 0 else 0.0)


def design_note_pli(x, y):
    """Page repliée portant uniquement l'onde vocale — la version épurée."""
    half_w, half_h, fold = 0.205, 0.285, 0.088
    page = _page_with_fold(x, y, half_w, half_h, fold)
    crease = _fold_crease(x, y, half_w, half_h, fold)

    bars = [
        (-0.120, 0.055),
        (-0.060, 0.115),
        (0.000, 0.170),
        (0.060, 0.105),
        (0.120, 0.050),
    ]
    wave = 1.0
    for bx, half_bar in bars:
        wave = min(wave, sdf_capsule(x, y, bx, -half_bar, bx, half_bar, 0.021))

    ink = union(wave, crease)
    return (1.0 if page <= 0 else 0.0), (1.0 if ink <= 0 else 0.0)


DESIGNS = {
    "note": design_note,
    "note-pli": design_note_pli,
    "dictee": design_dictee,
    "onde": design_onde,
    "micro": design_micro,
    "pouls": design_pouls,
}


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def render(size: int, design: str, maskable: bool = False) -> bytearray:
    """
    ``maskable`` : fond carré plein bord à bord et glyphe réduit, pour rester
    dans la zone sûre centrale de 80 % qu'Android peut rogner en cercle.
    """
    glyph_fn = DESIGNS[design]
    pixels = bytearray(size * size * 4)
    samples = SUPERSAMPLE * SUPERSAMPLE
    step = 1.0 / (size * SUPERSAMPLE)

    scale = 0.74 if maskable else 1.0
    corner_radius = 0.0 if maskable else 0.225

    for py in range(size):
        for px in range(size):
            bg_acc = white_acc = ink_acc = 0.0

            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    u = (px * SUPERSAMPLE + sx + 0.5) * step
                    v = (py * SUPERSAMPLE + sy + 0.5) * step

                    if maskable:
                        inside_bg = 1.0
                    else:
                        inside_bg = 1.0 if sdf_round_rect(u, v, 0.5, 0.5, 0.5, 0.5, corner_radius) <= 0 else 0.0
                    bg_acc += inside_bg
                    if not inside_bg:
                        continue

                    gx = (u - 0.5) / scale
                    gy = (v - 0.5) / scale
                    white_cov, ink_cov = glyph_fn(gx, gy)
                    white_acc += white_cov
                    ink_acc += ink_cov

            bg_alpha = bg_acc / samples
            white_alpha = white_acc / samples
            ink_alpha = ink_acc / samples

            # Fond : dégradé diagonal
            ratio = (px + py) / max(1, 2 * (size - 1))
            colour = [round(GRAD_FROM[i] + (GRAD_TO[i] - GRAD_FROM[i]) * ratio) for i in range(3)]

            # Composition : blanc sur le fond, puis encre sur le blanc
            colour = [round(colour[i] + (WHITE[i] - colour[i]) * white_alpha) for i in range(3)]
            colour = [round(colour[i] + (INK[i] - colour[i]) * ink_alpha) for i in range(3)]

            offset = (py * size + px) * 4
            pixels[offset:offset + 3] = bytes(colour)
            pixels[offset + 3] = round(bg_alpha * 255)

    return pixels


def build_previews() -> None:
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    for name in DESIGNS:
        path = os.path.join(PREVIEW_DIR, f"apercu-{name}.png")
        write_png(path, 192, 192, render(192, name))
        print(f"  aperçu « {name} » → {path}")


def build_all(design: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    # Le favicon utilise volontairement un motif plus gras : à 32 px, les
    # barres de l'onde dans la page ne feraient qu'un pixel de large et le
    # dessin virerait à la tache blanche. Simplifier aux petites tailles est
    # la pratique courante en conception d'icônes.
    targets = [
        ("icon-192.png", 192, False, design),
        ("icon-512.png", 512, False, design),
        ("icon-maskable-512.png", 512, True, design),
        ("apple-touch-icon.png", 180, True, design),   # iOS arrondit lui-même
        ("favicon-32.png", 32, False, "onde"),
    ]
    for filename, size, maskable, design_used in targets:
        path = os.path.join(OUT_DIR, filename)
        write_png(path, size, size, render(size, design_used, maskable))
        suffix = "" if design_used == design else f"  (motif « {design_used} »)"
        print(f"  {filename:<26} {size}×{size}  {os.path.getsize(path):>7} octets{suffix}")


def main() -> None:
    args = sys.argv[1:]
    if "--preview" in args:
        build_previews()
        return

    design = DEFAULT_DESIGN
    if "--design" in args:
        design = args[args.index("--design") + 1]
    if design not in DESIGNS:
        sys.exit(f"Design inconnu « {design} ». Disponibles : {', '.join(DESIGNS)}")

    print(f"Design : {design}")
    build_all(design)


if __name__ == "__main__":
    main()
