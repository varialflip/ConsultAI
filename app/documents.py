"""
documents.py — Documents photographiés joints à un brouillon.
=========================================================================

Un document, pas une photo
--------------------------
L'unité de travail est le DOCUMENT, jamais la page. Une liste de médicaments
photographiée est un document d'une page ; un rapport d'imagerie de cinq pages
est UN document de cinq pages. C'est aussi l'unité que le modèle segmente, et
celle que l'onglet de revue affiche.

D'où l'assemblage : plusieurs photos prises à la suite sont réunies en un PDF
multipage AVANT l'envoi. Le modèle reçoit alors un document cohérent (ordre des
pages, en-tête, tableau qui continue), et non cinq images indépendantes dont il
doit deviner qu'elles vont ensemble.

POURQUOI DU PYTHON PUR POUR LE PDF
----------------------------------
Une page PDF contenant un JPEG est un objet ``/Subtype /Image`` avec
``/Filter /DCTDecode`` : les octets de la photo y entrent tels quels, sans
ré-encodage ni perte. Le fichier produit tient en une soixantaine de lignes et
n'ajoute AUCUNE dépendance à une image qui doit rester légère (ni Pillow, ni
reportlab). Les dimensions sont lues dans l'en-tête SOF du JPEG.

Le PDF n'est pas un format de travail interne : c'est le format d'entrée du
modèle. On garde donc les JPEG d'origine sur disque (relecture, vignettes,
ré-essai) et le PDF n'est qu'un dérivé, reconstruit à la demande.

ORGANISATION SUR DISQUE
-----------------------
    DOCUMENTS_DIR/<consultation_id>/<uuid>.<ext>

Un document disparaît avec le brouillon, comme l'audio : un seul geste doit
suffire à ne rien laisser derrière.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import uuid
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Consultation, Document, utcnow

logger = logging.getLogger(__name__)

#: Types acceptés. Le SVG est exclu à dessein : servi en ligne, il exécute du
#: script — un document patient n'a aucune raison d'en être un.
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

_IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")

#: Types qu'on accepte de SERVIR en ligne (liste blanche, voir main.py).
SUPPORTED_MIME = tuple(_EXTENSIONS)


def _extension(mime_type: str) -> str:
    base = (mime_type or "").split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, ".bin")


def _directory(consultation_id: int) -> str:
    path = os.path.join(settings.documents_dir, str(consultation_id))
    os.makedirs(path, exist_ok=True)
    return path


def absolute_path(document: Document) -> str:
    return os.path.join(settings.documents_dir, document.filename)


def normalize_mime(raw: str, data: bytes) -> Optional[str]:
    """
    Type MIME RÉEL du fichier, déduit de ses octets.

    Le ``Content-Type`` annoncé par le navigateur n'est qu'une déclaration :
    c'est le contenu qui décide de ce qu'on accepte et de ce qu'on sert
    ensuite. Un fichier inconnu renvoie ``None`` — refusé, jamais rangé en
    « .bin ».
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    # Aucun repli sur le type DÉCLARÉ : un fichier dont les octets ne sont pas
    # reconnus est refusé, quelle que soit l'étiquette que le navigateur lui a
    # collée. C'est le contenu qui décide de ce qu'on accepte — et de ce qu'on
    # resservira ensuite.
    return None


def _jpeg_size(data: bytes) -> Tuple[int, int, int]:
    """
    Largeur, hauteur et nombre de composantes d'un JPEG, lus dans ses marqueurs.

    Un JPEG de navigateur est en YCbCr (3 composantes, ``/DeviceRGB``) ; un
    JPEG en niveaux de gris en compte 1. Se tromper ferait un PDF aux couleurs
    inversées, d'où la lecture réelle plutôt qu'une supposition.
    """
    i = 2
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        i += 2
        # Marqueurs sans charge utile.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > n:
            break
        length = struct.unpack(">H", data[i:i + 2])[0]
        # SOF0..SOF15, sauf DHT (C4), JPG (C8) et DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 3:i + 7])
            components = data[i + 7]
            return width, height, components
        i += length
    raise ValueError("Dimensions JPEG illisibles")


def assemble_pdf(images: Sequence[bytes]) -> bytes:
    """
    Réunit des JPEG en un PDF multipage (une page par image).

    Les octets JPEG sont embarqués tels quels en ``DCTDecode`` : aucune
    ré-encodage, donc aucune perte sur un texte petit. La page prend la taille
    de l'image en points (1 px = 1 pt) : le modèle s'en moque, et cela évite
    tout calcul de mise en page.
    """
    if not images:
        raise ValueError("Aucune image à assembler")

    objects: List[bytes] = []      # corps des objets, index = numéro - 1
    pages_refs: List[int] = []

    # 1 = catalogue, 2 = arbre des pages ; les pages suivent par blocs de 3.
    objects.append(b"")            # réservé (catalogue)
    objects.append(b"")            # réservé (pages)

    for data in images:
        width, height, components = _jpeg_size(data)
        color = b"/DeviceGray" if components == 1 else b"/DeviceRGB"
        page_no = len(objects) + 1          # objet /Page
        content_no = page_no + 1            # objet /Contents
        image_no = page_no + 2              # objet /XObject

        contenu = (
            f"q\n{width} 0 0 {height} 0 0 cm\n/Im0 Do\nQ\n".encode("ascii")
        )
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
            + f"{width} {height}".encode("ascii")
            + b"] /Resources << /XObject << /Im0 "
            + f"{image_no} 0 R".encode("ascii")
            + b" >> >> /Contents "
            + f"{content_no} 0 R".encode("ascii")
            + b" >>"
        )
        objects.append(
            b"<< /Length " + str(len(contenu)).encode("ascii")
            + b" >>\nstream\n" + contenu + b"endstream"
        )
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width "
            + str(width).encode("ascii")
            + b" /Height " + str(height).encode("ascii")
            + b" /ColorSpace " + color
            + b" /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(data)).encode("ascii")
            + b" >>\nstream\n" + data + b"\nendstream"
        )
        pages_refs.append(page_no)

    kids = b" ".join(f"{n} 0 R".encode("ascii") for n in pages_refs)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count "
        + str(len(pages_refs)).encode("ascii") + b" >>"
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _register(
    db: Session,
    consultation: Consultation,
    relative: str,
    mime_type: str,
    size_bytes: int,
    pages: int,
    kind: str = "other",
    title: str = "",
) -> Document:
    document = Document(
        consultation_id=consultation.id,
        owner=consultation.owner,
        filename=relative,
        mime_type=(mime_type or "image/jpeg")[:100],
        size_bytes=size_bytes,
        pages=max(1, int(pages or 1)),
        kind=(kind or "other")[:20],
        title=(title or "")[:200],
    )
    db.add(document)
    # Le brouillon vient de changer de contenu : sans ce repère, la purge de
    # rétention (heures depuis ``updated_at``) pourrait emporter un dossier
    # dont les documents sont le seul contenu — et laisser leurs fichiers
    # orphelins sur le disque.
    consultation.updated_at = utcnow()
    db.commit()
    db.refresh(document)
    logger.info(
        "Document %s conservé avec la consultation %s (%.1f Mo, %s page(s), %s)",
        document.id, consultation.id, size_bytes / 1048576, document.pages, mime_type,
    )
    return document


def store_bytes(
    db: Session,
    consultation: Consultation,
    data: bytes,
    mime_type: str,
    pages: int = 1,
) -> Document:
    """Écrit un document reçu en mémoire (photo redimensionnée ou PDF)."""
    name = f"{uuid.uuid4().hex}{_extension(mime_type)}"
    destination = os.path.join(_directory(consultation.id), name)
    with open(destination, "wb") as handle:
        handle.write(data)
    return _register(
        db, consultation, os.path.join(str(consultation.id), name),
        mime_type, len(data), pages,
    )


def store_images_as_document(
    db: Session,
    consultation: Consultation,
    images: Sequence[bytes],
) -> Document:
    """
    Range des photos comme UN document : la première reste l'image de
    référence, l'ensemble devient un PDF multipage.

    C'est la voie normale quand le clinicien photographie un rapport page par
    page : sans cet assemblage, le modèle recevrait cinq images indépendantes
    et devrait deviner qu'elles vont ensemble.
    """
    if len(images) == 1:
        return store_bytes(db, consultation, images[0], "image/jpeg", pages=1)
    pdf = assemble_pdf(images)
    return store_bytes(db, consultation, pdf, "application/pdf", pages=len(images))


def replace_file(document: Document, data: bytes, mime_type: str, pages: int) -> None:
    """Réécrit le fichier d'un document déjà en base (relecture d'une page)."""
    path = absolute_path(document)
    with open(path, "wb") as handle:
        handle.write(data)
    document.mime_type = (mime_type or document.mime_type)[:100]
    document.size_bytes = len(data)
    document.pages = max(1, int(pages or 1))


def for_consultation(db: Session, consultation_id: int) -> List[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.consultation_id == consultation_id)
            .order_by(Document.created_at, Document.id)
        )
    )


def delete_one(db: Session, document: Document) -> None:
    try:
        os.remove(absolute_path(document))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Document %s : fichier non supprimé — %s", document.id, exc)
    db.delete(document)
    db.commit()


def delete_for_consultation(db: Session, consultation_id: int) -> int:
    """Appelée à la suppression d'un brouillon : rien ne doit survivre."""
    rows = for_consultation(db, consultation_id)
    for document in rows:
        try:
            os.remove(absolute_path(document))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Document %s : fichier non supprimé — %s", document.id, exc)
        db.delete(document)
    shutil.rmtree(
        os.path.join(settings.documents_dir, str(consultation_id)), ignore_errors=True
    )
    if rows:
        logger.info("Consultation %s : %d document(s) supprimé(s)", consultation_id, len(rows))
    return len(rows)


def purge_orphans(db: Session) -> int:
    """
    Efface les fichiers sans ligne correspondante.

    La purge de rétention supprime la ligne ET les fichiers ; ce nettoyage-ci
    couvre le cas inverse (fichier écrit puis transaction perdue), qui laisse
    sinon un document patient invisible à toute suppression.
    """
    removed = 0
    root = settings.documents_dir
    if not os.path.isdir(root):
        return 0
    known = {document.filename for document in db.scalars(select(Document))}
    for entry in os.listdir(root):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            relative = os.path.join(entry, name)
            if relative in known:
                continue
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
        try:
            os.rmdir(folder)  # échoue si non vide, c'est voulu
        except OSError:
            pass
    if removed:
        logger.info("Purge : %d fichier(s) de document orphelin(s) supprimé(s)", removed)
    return removed
