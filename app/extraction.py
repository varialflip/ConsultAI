"""
extraction.py — Lecture des documents photographiés par le modèle.
=========================================================================

UN APPEL POUR LE LOT, LE MODÈLE SEGMENTE
----------------------------------------
Le clinicien photographie ce qu'il a sous les yeux pendant la revue du dossier :
liste de médicaments, antécédents, bilan, rapport d'imagerie. Ces pages partent
ensemble, en UN appel, et c'est le modèle qui décide combien de documents
distincts elles contiennent et à quoi chacune appartient. Une heuristique
d'assemblage côté serveur (détection de « page 2 sur 3 », tableau qui continue)
serait moins fiable que la lecture réelle des titres et des en-têtes.

CONTRAT DE COUVERTURE
---------------------
Un appel unique a un mode d'échec redouté : la page oubliée en silence — une
ligne de moins dans une liste de médicaments, et rien ne le signale. Aucune
consigne ne garantit qu'un modèle rende compte de tout. On l'exige donc
explicitement (``coverage`` / ``unreadable``) ET on le VÉRIFIE en code :
chaque entrée envoyée doit apparaître exactement une fois. Une page non
couverte est une erreur détectée, pas une perte silencieuse.

CE QUE LE MODÈLE N'A PAS LE DROIT DE FAIRE
------------------------------------------
Transcrire est permis ; conclure ne l'est pas. « 0 renouvellement » n'est pas
« cessé » — c'est une date de dernière distribution et un compteur, que le
clinicien interprétera. Le modèle restitue le fait imprimé, jamais le statut
qu'il en déduirait. C'est la même règle qu'à la génération : aucune invention.

Les observations (recoupements entre deux documents, contradiction, donnée
surprenante) ont un canal à part, décoché par défaut : le raisonnement reste
visible sans devenir une affirmation clinique dans la note.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app import llm
from app.config import settings
from app.database import Consultation, Document

logger = logging.getLogger(__name__)

#: Types de document reconnus. « other » est le repli : le clinicien peut
#: reclasser dans l'onglet Documents, une erreur de classement ne doit jamais
#: bloquer.
KINDS: Tuple[str, ...] = ("medications", "history", "labs", "imaging", "other")

KIND_LABELS = {
    "medications": ("Médicaments", "Medications"),
    "history": ("Antécédents", "Medical history"),
    "labs": ("Laboratoire", "Laboratory"),
    "imaging": ("Imagerie", "Imaging"),
    "other": ("Autre", "Other"),
}


class ExtractionError(RuntimeError):
    """Échec de lecture : fournisseur sans vision, réponse invalide, couverture
    incomplète. Le message est destiné à l'usager."""


#: Fournisseurs capables de lire un document. Volontairement restrictif : seul
#: Gemini reçoit ici le PDF natif (pages paginées, 258 jetons par page). Un
#: autre fournisseur n'est pas « moins bien » — il est simplement hors du
#: chemin implémenté, et on le dit plutôt que d'envoyer des images à un modèle
#: qui n'en veut pas.
DOCUMENT_CAPABLE_PROVIDERS = ("gemini",)


def capable(provider: Optional[str] = None) -> bool:
    return (provider or llm.active_provider()) in DOCUMENT_CAPABLE_PROVIDERS


_PROMPT_FR = """\
Tu lis des documents photographiés fournis par un médecin pendant la revue de \
dossier : listes de médicaments, listes d'antécédents, bilans de laboratoire, \
rapports d'imagerie.

Tu reçois %(count)d entrée(s), numérotées de 1 à %(count)d dans l'ordre où \
elles ont été fournies. Une entrée peut être une photographie ou un PDF de \
plusieurs pages.

## 1. SEGMENTATION
Décide combien de DOCUMENTS distincts contiennent ces entrées — c'est toi qui \
tranches. Une liste de médicaments sur deux pages est UN document ; un bilan \
puis un rapport d'imagerie sont DEUX documents. Attribue chaque entrée à un \
document et donne dans ``pages`` les numéros de PAGE dans l'ordre de lecture : \
pour un PDF, le numéro de page DANS LE FICHIER (1 = première page) ; pour une \
simple photo, son numéro d'entrée.

## 2. COUVERTURE — OBLIGATOIRE
Chaque entrée reçue doit apparaître EXACTEMENT UNE FOIS, soit dans \
``coverage``, soit dans ``unreadable``. Une entrée que tu n'as pas lue doit \
être déclarée dans ``unreadable`` avec sa raison (« page tournée », \
« floue », « vide »). Ne l'omets jamais en silence.

## 3. EN-TÊTES ADMINISTRATIFS — NE PAS EXTRAIRE
Le nom du patient, la date de naissance, le NAS, l'adresse, le numéro de \
dossier et les coordonnées ne sont PAS du contenu clinique : ne les extrais \
pas comme élément, ne les recopie nulle part. L'application ne collecte pas \
l'identité du patient.

## 4. TRANSCRIPTION FIDÈLE
Transcris ce qui est IMPRIMÉ, sans corriger, sans compléter, sans traduire. \
Un passage illisible s'écrit exactement ``[illisible]``. Ne complète JAMAIS \
une posologie, une date, un chiffre ou un nom manquant.

## 5. CE QUE TU N'AS PAS LE DROIT DE FAIRE
Tu n'es pas clinicien. Tu ne déduis aucun statut : « 0 renouvellement » n'est \
PAS « cessé », une date de dernière distribution n'est PAS une date d'arrêt. \
Tu restitues le fait imprimé ; l'interprétation appartient au médecin.

## 6. CHAMPS PAR TYPE

**medications** — un item par ligne de la liste :
``text`` (la ligne telle qu'imprimée, préfixe fabricant compris), \
``fields`` = {``molecule`` si la molécule est certaine d'après ce qui est \
imprimé, sinon vide, ``strength``, ``form``, ``directions``, ``din``, \
``last_dispensed``, ``renewals``, ``status_printed`` (seulement si le statut \
est IMPRIMÉ : « cessé le… », « suspendu »)}.

**history** — un item par problème, intervention ou hospitalisation :
``text``, ``fields`` = {``year`` ou ``date`` si imprimé, ``category`` \
(``condition`` | ``surgery`` | ``hospitalization`` | ``allergy`` | ``family``)}.

**labs** — un item par analyte : ``text``, ``fields`` = {``analyte``, \
``value``, ``unit``, ``reference``, ``flag``, ``date``}.

**imaging** — UN seul item par rapport, ``text`` = le corps du rapport \
(conclusion comprise), ``fields`` = {``study``, ``date``}.

**other** — un item par bloc de texte cohérent.

## 7. OBSERVATIONS
Dans ``observations``, uniquement des recoupements FACTUELS entre documents \
(« la date du bilan précède celle du rapport », « deux posologies différentes \
pour la même molécule »). Jamais de diagnostic, jamais de conduite à tenir.

## 8. RÉPONSE
Un seul objet JSON, aucun texte autour, aucune clôture de code :
{
  "documents": [{"id": "d1", "kind": "medications|history|labs|imaging|other",
                 "pages": [1, 2], "title": "…", "confidence": 0.0}],
  "items": [{"document_id": "d1", "kind": "…", "text": "…", "fields": {},
             "source_page": 1}],
  "coverage": [{"input": 1, "document_id": "d1", "note": ""}],
  "unreadable": [{"input": 3, "reason": "…"}],
  "observations": [{"text": "…", "pages": [1, 2]}]
}
"""

_PROMPT_EN = """\
You are reading documents photographed by a physician during chart review: \
medication lists, medical history lists, lab reports, imaging reports.

You receive %(count)d input(s), numbered 1 to %(count)d in the order supplied. \
An input may be a photograph or a multi-page PDF.

## 1. SEGMENTATION
Decide how many distinct DOCUMENTS these inputs contain — that is your call. \
A medication list spanning two pages is ONE document; a lab report followed by \
an imaging report are TWO documents. Assign each input to a document and list \
in ``pages`` the PAGE numbers in reading order: for a PDF, the page number \
INSIDE THE FILE (1 = first page); for a plain photograph, its input number.

## 2. COVERAGE — MANDATORY
Every input must appear EXACTLY ONCE, either in ``coverage`` or in \
``unreadable``. An input you did not read must be declared in ``unreadable`` \
with a reason ("page upside down", "blurry", "blank"). Never drop one silently.

## 3. ADMINISTRATIVE HEADERS — DO NOT EXTRACT
The patient's name, date of birth, health insurance number, address, record \
number and contact details are NOT clinical content: do not extract them as \
items and do not copy them anywhere. The application does not collect patient \
identity.

## 4. FAITHFUL TRANSCRIPTION
Transcribe what is PRINTED, without correcting, completing or translating. An \
illegible passage is written exactly ``[illisible]``. Never fill in a missing \
dosage, date, number or name.

## 5. WHAT YOU MUST NOT DO
You are not a clinician. Infer no status: "0 renewals" is NOT "stopped", a \
last-dispensed date is NOT a stop date. Report the printed fact; \
interpretation belongs to the physician.

## 6. FIELDS BY TYPE

**medications** — one item per list line:
``text`` (the line as printed, manufacturer prefix included), ``fields`` = \
{``molecule`` if certain from what is printed, else empty, ``strength``, \
``form``, ``directions``, ``din``, ``last_dispensed``, ``renewals``, \
``status_printed`` (only if the status is PRINTED: "stopped on…", "on hold")}.

**history** — one item per condition, procedure or hospitalization: ``text``, \
``fields`` = {``year`` or ``date`` if printed, ``category`` (``condition`` | \
``surgery`` | ``hospitalization`` | ``allergy`` | ``family``)}.

**labs** — one item per analyte: ``text``, ``fields`` = {``analyte``, \
``value``, ``unit``, ``reference``, ``flag``, ``date``}.

**imaging** — ONE item per report, ``text`` = the report body (impression \
included), ``fields`` = {``study``, ``date``}.

**other** — one item per coherent block of text.

## 7. OBSERVATIONS
In ``observations``, only FACTUAL cross-references between documents ("the lab \
date precedes the report date", "two different dosages for the same molecule"). \
Never a diagnosis, never a recommendation.

## 8. RESPONSE
A single JSON object, no surrounding text, no code fence:
{
  "documents": [{"id": "d1", "kind": "medications|history|labs|imaging|other",
                 "pages": [1, 2], "title": "…", "confidence": 0.0}],
  "items": [{"document_id": "d1", "kind": "…", "text": "…", "fields": {},
             "source_page": 1}],
  "coverage": [{"input": 1, "document_id": "d1", "note": ""}],
  "unreadable": [{"input": 3, "reason": "…"}],
  "observations": [{"text": "…", "pages": [1, 2]}]
}
"""


def _prompt(count: int, language: str) -> str:
    modele = _PROMPT_EN if (language or "fr").lower().startswith("en") else _PROMPT_FR
    return modele % {"count": count}


def _parse(raw: str) -> dict:
    texte = (raw or "").strip()
    texte = re.sub(r"^```(?:json)?\s*", "", texte, flags=re.IGNORECASE)
    texte = re.sub(r"\s*```$", "", texte)
    try:
        return json.loads(texte)
    except ValueError:
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut >= 0 and fin > debut:
            try:
                return json.loads(texte[debut:fin + 1])
            except ValueError:
                pass
    raise ExtractionError(
        "Réponse illisible du modèle (JSON attendu). Rien n'a été enregistré : "
        "réessayez l'envoi."
    )


def _normalize(payload: dict, count: int) -> dict:
    """
    Valide la réponse et la complète avec l'état de revue.

    La validation est volontairement stricte sur la couverture — c'est le seul
    point où une erreur du modèle coûte cher et ne se voit pas — et souple sur
    le reste : un ``kind`` inconnu devient ``other``, un item orphelin est
    rattaché au premier document plutôt que perdu.
    """
    documents = payload.get("documents") or []
    items = payload.get("items") or []
    coverage = payload.get("coverage") or []
    unreadable = payload.get("unreadable") or []
    observations = payload.get("observations") or []

    if not isinstance(documents, list) or not documents:
        raise ExtractionError(
            "Le modèle n'a reconnu aucun document dans les pages envoyées."
        )

    ids = []
    for index, doc in enumerate(documents):
        identifiant = str(doc.get("id") or f"d{index + 1}")
        kind = str(doc.get("kind") or "other").lower()
        if kind not in KINDS:
            kind = "other"
        pages = doc.get("pages") or []
        pages = [int(p) for p in pages if isinstance(p, (int, float)) or str(p).isdigit()]
        pages = [p for p in pages if 1 <= p <= count] or list(range(1, count + 1))
        doc["id"] = identifiant
        doc["kind"] = kind
        doc["pages"] = pages
        doc["title"] = str(doc.get("title") or "")[:200]
        try:
            doc["confidence"] = float(doc.get("confidence") or 0.0)
        except (TypeError, ValueError):
            doc["confidence"] = 0.0
        ids.append(identifiant)

    # Couverture : chaque entrée doit être vue exactement une fois.
    vues = []
    for row in coverage:
        if not isinstance(row, dict):
            continue
        try:
            entree = int(row.get("input"))
        except (TypeError, ValueError):
            continue
        if 1 <= entree <= count and entree not in vues:
            vues.append(entree)
            ids_couverts = row.get("document_id")
            if isinstance(ids_couverts, list):
                row["document_id"] = ids_couverts[0] if ids_couverts else ""
    illisibles = []
    for row in unreadable:
        if not isinstance(row, dict):
            continue
        try:
            entree = int(row.get("input"))
        except (TypeError, ValueError):
            continue
        if 1 <= entree <= count and entree not in vues:
            vues.append(entree)
            illisibles.append({"input": entree, "reason": str(row.get("reason") or "")[:300]})

    manquantes = [n for n in range(1, count + 1) if n not in vues]
    if manquantes:
        raise ExtractionError(
            "Lecture incomplète : la ou les entrées "
            + ", ".join(str(n) for n in manquantes)
            + " n'ont pas été rendues par le modèle. Rien n'a été enregistré — "
            "renvoyez le lot, ou relisez ces pages seules."
        )

    propres = []
    for item in items:
        if not isinstance(item, dict):
            continue
        texte = str(item.get("text") or "").strip()
        if not texte:
            continue
        kind = str(item.get("kind") or "other").lower()
        if kind not in KINDS:
            kind = "other"
        doc_id = str(item.get("document_id") or "")
        if doc_id not in ids:
            doc_id = ids[0]
        champs = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        try:
            page = int(item.get("source_page") or 0)
        except (TypeError, ValueError):
            page = 0
        champs = dict(champs)
        if kind == "medications" and champs.get("molecule"):
            # La ligne de la note porte la MOLÉCULE, pas le préfixe du
            # distributeur : « APO-RAMIPRIL » est une marque de fabricant, la
            # molécule est ramipril. La ligne imprimée reste dans
            # ``as_printed`` — c'est la provenance, et le clinicien la retrouve
            # dans le document.
            molecule = str(champs["molecule"]).strip()
            if molecule.isupper():
                molecule = molecule.capitalize()
            morceaux = [molecule, str(champs.get("strength") or "").strip()]
            ligne = " ".join(m for m in morceaux if m).strip()
            directions = str(champs.get("directions") or "").strip()
            if directions:
                ligne = f"{ligne} — {directions}"
            # Statut IMPRIMÉ seulement (« cessé le… »). Jamais déduit : une
            # ligne sans renouvellement n'est pas une ligne arrêtée. Le fait
            # figure tel quel, entre parenthèses, et le clinicien tranche.
            statut = str(champs.get("status_printed") or "").strip()
            if statut and ligne:
                ligne = f"{ligne} ({statut})"
            if ligne:
                champs["as_printed"] = texte
                texte = ligne

        propres.append({
            "document_id": doc_id,
            "kind": kind,
            "text": texte,
            "fields": champs,
            "source_page": page,
            # État de revue — persistant, jamais dans le DOM seul : rouvrir un
            # brouillon ne doit pas décocher ce que le médecin a arbitré.
            # Un bloc que le modèle n'a pas su classer (« other ») est
            # proposé DÉCOCHÉ : c'est exactement là que tombent les en-têtes
            # administratifs et les restes de mise en page, et rien d'incertain
            # ne doit entrer dans la note sans un geste.
            "included": kind != "other",
            "edited": False,
        })

    # Les pages d'un document sont déduites des lignes réellement lues : le
    # modèle se trompe parfois dans le récapitulatif ``documents[].pages``
    # alors que chaque ligne porte sa bonne page. Le faisceau des lignes fait
    # foi, et c'est lui que l'onglet affiche.
    for doc in documents:
        vues_pages = sorted({
            item["source_page"] for item in propres
            if item["document_id"] == doc["id"] and item["source_page"]
        })
        if vues_pages:
            doc["pages"] = vues_pages

    if not propres:
        raise ExtractionError(
            "Le modèle n'a extrait aucun élément exploitable des documents."
        )

    observations_propres = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        texte = str(obs.get("text") or "").strip()
        if not texte:
            continue
        pages = obs.get("pages") or []
        pages = [int(p) for p in pages if isinstance(p, (int, float)) or str(p).isdigit()]
        observations_propres.append({
            "text": texte,
            "pages": [p for p in pages if 1 <= p <= count],
            # Une observation est une lecture du modèle, pas un fait du
            # document : décochée par défaut.
            "included": False,
            "edited": False,
        })

    return {
        "documents": documents,
        "items": propres,
        "coverage": coverage,
        "unreadable": illisibles,
        "observations": observations_propres,
    }


def extract_documents(
    db: Session,
    consultation: Consultation,
    documents: Sequence[Document],
    language: str = "fr",
) -> Dict[str, object]:
    """
    Lit les documents en UN appel et renvoie la réponse structurée validée.

    Best-effort assumé côté appelant : ce qui a déjà été enregistré reste, et
    un échec ne doit jamais emporter le brouillon.
    """
    if not documents:
        raise ExtractionError("Aucun document à lire.")
    provider = llm.active_provider()
    if not capable(provider):
        raise ExtractionError(
            "Le fournisseur de modèle actif (« %s ») ne lit pas les documents. "
            "Choisissez Gemini (Vertex) dans le panneau pour utiliser les "
            "photos et les PDF." % provider
        )

    blobs: List[Tuple[bytes, str]] = []
    for doc in documents:
        try:
            with open(f"{settings.documents_dir}/{doc.filename}", "rb") as handle:
                blobs.append((handle.read(), doc.mime_type))
        except OSError as exc:
            raise ExtractionError(
                f"Document {doc.id} illisible sur le disque ({exc})."
            ) from exc

    prompt = _prompt(len(blobs), language)
    modele = llm.active_model()

    from google.genai import types

    client = llm.get_client("gemini")
    parts: List[object] = [
        types.Part.from_bytes(data=data, mime_type=mime) for data, mime in blobs
    ]
    config_kwargs = dict(
        temperature=0.1,
        max_output_tokens=settings.gemini_max_output_tokens,
        safety_settings=llm._safety_settings(),
        response_mime_type="application/json",
    )
    try:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=llm._gemini_thinking_budget()
        )
    except Exception:  # pragma: no cover - très défensif
        pass

    logger.info(
        "Extraction : %d entrée(s), modèle %s, %s",
        len(blobs), modele,
        ", ".join(f"{d.id}:{d.mime_type}" for d in documents),
    )
    try:
        response = client.models.generate_content(
            model=modele,
            contents=parts + [prompt],
            config=types.GenerateContentConfig(**config_kwargs),
        )
    except Exception as exc:
        logger.exception("Extraction refusée par le fournisseur")
        raise ExtractionError(
            "Le modèle n'a pas pu lire les documents : %s" % exc
        ) from exc

    usage = llm._gemini_usage(getattr(response, "usage_metadata", None))
    payload = _parse(getattr(response, "text", None) or "")
    result = _normalize(payload, len(blobs))
    result["model"] = modele
    result["usage"] = usage
    result["version"] = 1
    return result
