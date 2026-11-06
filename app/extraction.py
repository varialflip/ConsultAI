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

## 5. MISE EN PAGE — NE PAS RECOPIER LES PUCES
Aucun ``text`` ne commence par une puce, un glyphe de liste ou un numéro \
d'énumération (``•``, ``◇``, ``-``, ``1.``, ``a)``) : ces marques appartiennent \
à la page, pas à l'énoncé. Et une phrase qui se poursuit à la ligne suivante \
est prise entière — on ne coupe jamais un item au milieu.

## 6. CE QUE TU N'AS PAS LE DROIT DE FAIRE
Tu n'es pas clinicien. Tu ne déduis aucun statut : « 0 renouvellement » n'est \
PAS « cessé », une date de dernière distribution n'est PAS une date d'arrêt. \
Tu restitues le fait imprimé ; l'interprétation appartient au médecin.

## 7. CHAMPS PAR TYPE

**medications** — un item par ligne de la liste :
``text`` = la ligne telle qu'imprimée, VERBATIM (préfixe fabricant compris, \
majuscules comprises) ; c'est la provenance, elle est conservée telle quelle. \
``fields`` = {``short_name``, ``dose``, ``route``, ``frequency``, ``molecule`` \
si la molécule est certaine, ``strength``, ``form``, ``directions``, ``din``, \
``last_dispensed``, ``renewals``, ``status_printed`` (seulement si le statut \
est IMPRIMÉ : « cessé le… », « suspendu »)}.

**``line`` — la ligne finale, que TU écris.** C'est elle qui paraît dans la \
note. Format exact : ``Nom court dose voie fréquence``, nombre collé à \
l'unité, abréviations en minuscules, ``prn`` en dernier :
``PEG 3350 17g po die`` · ``Acétaminophène 650mg po q6h prn`` · \
``Quétiapine 12,5mg po hs prn`` · ``Hydromorphone 0,5mg po q4h prn``.
La dose de ``line`` est la dose ADMINISTRÉE par prise (quantité × force), pas \
la force du comprimé. Cette ligne est affichée TELLE QUELLE et reste éditable : \
le médecin la relit, tu n'as pas à la conformer à un format d'ordinateur.

Les champs suivants documentent la lecture (provenance, contrôle par le \
médecin) :
- ``short_name`` : le nom court qu'un pharmacien écrirait (``POLYETHYLENE-\
GLYCOL-3350`` → ``PEG 3350``, ``ACETAMINOPHEN`` → ``Acétaminophène``).
- ``quantity`` : le nombre d'UNITÉS par prise, tel que la posologie le dit \
(``2`` pour « 2 comprimés », ``0.5`` pour « 1/2 comprimé », ``1`` pour un \
comprimé seul). Une poudre ou un liquide : ``1``.
- ``dose`` : la dose ADMINISTRÉE par prise, en unité de masse, nombre collé à \
l'unité (``650mg``, ``12.5mg``, ``17g``). C'est ``quantity × strength`` : \
``ACETAMINOPHENE 325MG, 2 comprimés`` → ``dose`` = ``650mg``, pas ``325mg`` \
(c'est la force du comprimé, elle va dans ``strength``). « 1/2 comprimé de \
1 mg » → ``0.5mg``. « 17 grammes » de poudre → ``17g``. Si aucune masse \
n'est imprimée, laisse vide.
- ``route`` : ``po``, ``sc``, ``im``, ``iv``, ``inh``, ``opht``, ``top``.
- ``frequency`` : ``die``, ``bid``, ``tid``, ``qid``, ``hs``, ``qsem``, \
``q6h``, ``q4-6h``, ``prn``. « 1 fois par jour » → ``die``, « aux 6 heures » \
→ ``q6h``, « si besoin » → ``prn``.

Un médicament qui apparaît sur DEUX pages (liste qui continue, ou photo qui \
recouvre la précédente) est UN seul item, jamais deux. Range sa page de \
première apparition.

Exemples de ligne attendue : ``PEG 3350 17g po die``, \
``Acétaminophène 650mg po q6h prn`` (deux comprimés de 325 mg), \
``Magnésium-gluconate 1g po tid``.

**history** — un item par problème de santé, intervention, hospitalisation ou \
allergie : ``text`` = l'énoncé COMPLET tel qu'imprimé, en une phrase, SANS \
puce ni glyphe (``•``, ``◇``, ``-``, ``1.``) et sans numéro de liste ; si la \
phrase continue à la ligne suivante, prends la suite plutôt que de la couper \
au milieu. ``fields`` = {``year`` (l'année imprimée, MÊME si elle figure aussi \
dans le texte), ``category`` (``condition`` | ``surgery`` | ``hospitalization`` \
| ``allergy`` | ``family``)}.

Un rapport de consultation contient plusieurs rubriques : ne retiens comme \
antécédents que ce qui est un problème de santé, une intervention ou une \
hospitalisation. N'extrais PAS comme antécédent un identifiant de dossier \
(``ID:``, MRN, numéro), un titre de rubrique, ni les données de vie \
quotidienne (AVQ, AVD, habitudes, situation de vie) : celles-là vont en \
``other``. Un constat d'examen rattaché à un problème (« HTA, 220 à la \
présentation, bien contrôlée ensuite ») reste UN seul item : c'est la même \
histoire. Même règle quand un problème est cité deux fois (une fois nu, une \
fois détaillé) : UN seul item, celui qui en dit le plus — jamais « HTA » à \
côté de « HTA, 220 à la présentation ».

**labs** — un item par analyte : ``text``, ``fields`` = {``analyte``, \
``value``, ``unit``, ``reference``, ``flag``, ``date``, ``pertinence``}. \
``pertinence`` vaut ``pertinent``, ``routine`` ou ``anormal``.

Tu n'es pas clinicien et tu ne poses aucun diagnostic — mais tu sais ce qu'est \
un bilan usuel, et une note de consultation ne recopie pas un panneau entier. \
``pertinent`` : ce qu'un médecin citerait — hémoglobine, plaquettes, VGM, \
globules blancs et différentiel d'une formule sanguine ; créatinine et DFGe ; \
HbA1c ; INR ; TSH ; tout résultat marqué par le laboratoire. ``anormal`` : \
valeur hors des références imprimées, même sans drapeau. ``routine`` : le \
reste du panneau — sodium et potassium normaux, chlore, bicarbonates, \
albumine normale, bilirubine, TIBC et paramètres du fer, sérologies négatives, \
toute valeur normale qu'un médecin ne citerait pas. Une formule sanguine \
normale s'écrit en une ligne, pas en trente.

Si le tableau donne PLUSIEURS colonnes datées, crée UN item par analyte ET par \
date : ``date`` porte la date de la colonne, jamais celle du prélèvement \
global — deux valeurs d'une même analyte à deux dates sont deux faits, pas un.

**imaging** — UN seul item par rapport : ``text`` = la CONCLUSION du rapport \
telle qu'imprimée (2 à 4 lignes, c'est ce qui entre dans la note), \
``fields`` = {``study``, ``date``, ``body`` = le texte intégral des résultats, \
verbatim}.

**other** — un item par bloc de texte cohérent.

## 8. OBSERVATIONS
Dans ``observations``, uniquement des recoupements FACTUELS entre documents \
(« la date du bilan précède celle du rapport », « deux posologies différentes \
pour la même molécule »). Jamais de diagnostic, jamais de conduite à tenir.

## 9. RÉPONSE
Un seul objet JSON, aucun texte autour, aucune clôture de code, AUCUNE
indentation ni saut de ligne (JSON compact, sur une seule ligne) :
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

## 5. LAYOUT — DO NOT COPY BULLETS
No ``text`` starts with a bullet, a list glyph or an enumeration number \
(``•``, ``◇``, ``-``, ``1.``, ``a)``): those marks belong to the page, not to \
the statement. And a sentence continuing on the next line is taken whole — an \
item is never cut mid-sentence.

## 6. WHAT YOU MUST NOT DO
You are not a clinician. Infer no status: "0 renewals" is NOT "stopped", a \
last-dispensed date is NOT a stop date. Report the printed fact; \
interpretation belongs to the physician.

## 7. FIELDS BY TYPE

**medications** — one item per list line:
``text`` = the line as printed, VERBATIM (manufacturer prefix included, \
capitalisation included); that is the provenance, kept unchanged. ``fields`` = \
{``short_name``, ``dose``, ``route``, ``frequency``, ``molecule`` if certain, \
``strength``, ``form``, ``directions``, ``din``, ``last_dispensed``, \
``renewals``, ``status_printed`` (only if PRINTED: "stopped on…", "on hold")}.

**``line`` — the final line, written by YOU.** It is what appears in the note. \
Exact format: ``Short name dose route frequency``, number glued to the unit, \
abbreviations lowercase, ``prn`` last:
``PEG 3350 17g po die`` · ``Acetaminophen 650mg po q6h prn`` · \
``Quetiapine 12.5mg po hs prn`` · ``Hydromorphone 0.5mg po q4h prn``.
The dose in ``line`` is the ADMINISTERED dose per intake (quantity × \
strength), not the tablet strength. That line is shown AS IS and stays \
editable: the physician reviews it, you do not have to fit it to a computer \
format.

The following fields document the reading (provenance, the physician's \
check):
- ``short_name``: the short name a pharmacist would write \
(``POLYETHYLENE-GLYCOL-3350`` → ``PEG 3350``).
- ``quantity``: the number of UNITS per intake as the directions state it \
(``2`` for "2 tablets", ``0.5`` for "1/2 tablet", ``1`` for a single tablet). \
A powder or a liquid: ``1``.
- ``dose``: the ADMINISTERED dose per intake, in a mass unit, number glued to \
the unit (``650mg``, ``12.5mg``, ``17g``). It is ``quantity × strength``: \
``ACETAMINOPHENE 325MG, 2 tablets`` → ``dose`` = ``650mg``, not ``325mg`` \
(that is the tablet strength, which belongs in ``strength``). "1/2 tablet of \
1 mg" → ``0.5mg``. "17 grams" of powder → ``17g``. Leave empty when no mass \
is printed.
- ``route``: ``po``, ``sc``, ``im``, ``iv``, ``inh``, ``opht``, ``top``.
- ``frequency``: ``die``, ``bid``, ``tid``, ``qid``, ``hs``, ``qsem``, \
``q6h``, ``q4-6h``, ``prn``.

A medication appearing on TWO pages (a list that continues, or a photograph \
overlapping the previous one) is ONE item, never two. Give its first page.

Expected line examples: ``PEG 3350 17g po die``, \
``Acetaminophen 650mg po q6h prn`` (two 325 mg tablets), \
``Magnesium gluconate 1g po tid``.

**history** — one item per health problem, procedure, hospitalization or \
allergy: ``text`` = the COMPLETE statement as printed, as a sentence, WITHOUT \
bullet or glyph (``•``, ``◇``, ``-``, ``1.``) and without list numbering; if \
the sentence continues on the next line, take the continuation rather than \
cutting it mid-sentence. ``fields`` = {``year`` (the printed year, EVEN if it \
also appears in the text), ``category`` (``condition`` | ``surgery`` | \
``hospitalization`` | ``allergy`` | ``family``)}.

A consultation report contains several headings: keep as history only what is \
a health problem, a procedure or a hospitalization. Do NOT extract as history \
a record identifier (``ID:``, MRN, number), a section heading, or daily-life \
data (ADL, IADL, habits, living situation): those go to ``other``. An \
examination finding attached to a problem ("HTN, 220 on presentation, well \
controlled afterwards") stays ONE item: it is the same story. Same rule when \
a problem is stated twice (once bare, once detailed): ONE item, the fuller \
one — never "HTN" next to "HTN, 220 on presentation".

**labs** — one item per analyte: ``text``, ``fields`` = {``analyte``, \
``value``, ``unit``, ``reference``, ``flag``, ``date``, ``pertinence``}. \
``pertinence`` is ``pertinent``, ``routine`` or ``anormal``.

You are not a clinician and you make no diagnosis — but you know what a \
routine panel is, and a consultation note does not copy a whole panel. \
``pertinent``: what a physician would cite — haemoglobin, platelets, MCV, \
white cells and differential from a blood count; creatinine and eGFR; HbA1c; \
INR; TSH; anything the laboratory flagged. ``anormal``: outside the printed \
reference range even without a flag. ``routine``: the rest of the panel — \
normal sodium and potassium, chloride, bicarbonate, normal albumin, \
bilirubin, TIBC and iron studies, negative serologies, any normal value a \
physician would not cite. A normal blood count is one line, not thirty.

If the table gives SEVERAL dated columns, create ONE item per analyte AND per \
date: ``date`` carries the column's date, never the overall collection date — \
two values of one analyte at two dates are two facts, not one.

**imaging** — ONE item per report: ``text`` = the report's CONCLUSION as \
printed (2 to 4 lines, this is what goes into the note), ``fields`` = \
{``study``, ``date``, ``body`` = the full findings text, verbatim}.

**other** — one item per coherent block of text.

## 8. OBSERVATIONS
In ``observations``, only FACTUAL cross-references between documents ("the lab \
date precedes the report date", "two different dosages for the same molecule"). \
Never a diagnosis, never a recommendation.

## 9. RESPONSE
A single JSON object, no surrounding text, no code fence, NO indentation or
line break (compact JSON, on a single line):
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


#: Clés de ``fields``, toutes déclarées : le schéma de réponse impose une
#: forme fixe, et c'est ce qui empêche le modèle de produire un JSON invalide
#: (observé : un caractère non échappé au milieu d'un objet de 12 000 signes,
#: une lecture perdue sur trois). Un champ absent est simplement omis.
_CHAMPS_SCHEMA = (
    "line", "molecule", "short_name", "strength", "form", "directions", "dose", "quantity",
    "route", "frequency", "din", "last_dispensed",
    "renewals", "status_printed", "as_printed",
    "analyte", "value", "unit", "reference", "flag", "date", "pertinence",
    "study", "body", "year", "category", "partie",
)

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "documents": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": list(KINDS)},
                    "pages": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "title": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["id", "kind", "pages"],
            },
        },
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "document_id": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": list(KINDS)},
                    "text": {"type": "STRING"},
                    "fields": {
                        "type": "OBJECT",
                        "properties": {cle: {"type": "STRING"} for cle in _CHAMPS_SCHEMA},
                    },
                    "source_page": {"type": "INTEGER"},
                },
                "required": ["document_id", "kind", "text"],
            },
        },
        "coverage": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "input": {"type": "INTEGER"},
                    "document_id": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["input"],
            },
        },
        "unreadable": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "input": {"type": "INTEGER"},
                    "reason": {"type": "STRING"},
                },
                "required": ["input"],
            },
        },
        "observations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "pages": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["documents", "items", "coverage"],
}


def _prompt(count: int, language: str) -> str:
    from app import runtime_config

    anglais = (language or "fr").lower().startswith("en")
    modele = _PROMPT_EN if anglais else _PROMPT_FR
    base = modele % {"count": count}

    # Choix de lecture du médecin, dits une fois dans le panneau : la même
    # phrase revient à chaque bilan (« je ne regarde que Hb, Plt, VGM, GB et
    # le différentiel »). Sans cela, il décocherait TIBC tous les jours.
    toujours = (runtime_config.value("labs_always_include") or "").strip()
    jamais = (runtime_config.value("labs_always_exclude") or "").strip()
    if not (toujours or jamais):
        return base
    lignes = []
    if toujours:
        lignes.append(
            "The physician ALWAYS keeps these, even when normal: " + toujours
            if anglais else
            "Le médecin retient TOUJOURS ces paramètres, même normaux : " + toujours
        )
    if jamais:
        lignes.append(
            "The physician NEVER keeps these by default (mark them `routine`): " + jamais
            if anglais else
            "Le médecin ne retient JAMAIS ces paramètres par défaut (marque-les "
            "`routine`) : " + jamais
        )
    titre = "PHYSICIAN'S READING HABITS" if anglais else "HABITUDES DE LECTURE DU MÉDECIN"
    return base + "\n\n## " + titre + "\n" + "\n".join("- " + l for l in lignes) + "\n"


def _parse(raw: str, motif: str = "") -> dict:
    texte = (raw or "").strip()
    if not texte:
        raise ExtractionError(
            "Le modèle n'a rien renvoyé pour ce document"
            + (f" (motif : {motif})" if motif and motif != "STOP" else "")
            + ". Renvoyez-le, ou relisez-le page par page."
        )
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


#: Marques d'un résultat à retenir, telles qu'IMPRIMÉES par le laboratoire.
#: On ne juge pas la valeur : on lit le drapeau que le laboratoire a posé.
_FLAGS_ANORMAUX = {
    "h", "l", "hh", "ll", "haut", "bas", "eleve", "élevé", "bas", "a", "abn",
    "anormal", "critique", "crit", "high", "low", "↑", "↓", "▲", "▼", "+", "*",
}


#: Groupes d'analytes et leurs noms usuels. Sert à reconnaître ce que le
#: médecin a écrit au panneau (« na », « creat ») dans ce que la page imprime
#: (« Sodium », « Créatinine »). Volontairement petit : c'est une correspondance
#: de confort, pas une base de laboratoire. Un terme inconnu retombe sur une
#: comparaison de texte directe.
_GROUPES_ANALYTES = {
    "globules blancs": ("globules blancs", "leucocytes", "wbc", "gb"),
    "hémoglobine": ("hémoglobine", "hemoglobine", "hb", "hgb"),
    "vgm": ("vgm", "mcv", "volume globulaire moyen"),
    "plaquettes": ("plaquettes", "plt", "thrombocytes"),
    "sodium": ("sodium", "natrémie", "natremie", "na"),
    "potassium": ("potassium", "kaliémie", "kaliemie", "k"),
    "créatinine": ("créatinine", "creatinine", "creat", "clairance"),
    "calcium": ("calcium", "calcémie", "calcemie", "ca"),
    "albumine": ("albumine", "albu", "alb"),
    "tsh": ("tsh", "thyréostimuline", "thyreostimuline"),
    "b12": ("b12", "vitamine b12", "cobalamine"),
    "syphilis": ("syphilis", "rpr", "vdrl", "tppa", "tpha"),
    "vih": ("vih", "hiv", "vih-1", "vih 1"),
}


def _sans_accent(texte: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", texte or "")
        if unicodedata.category(c) != "Mn"
    ).lower()


def _groupes_demandes(liste: str) -> set:
    """Traduit « na, k, creat » en groupes canoniques."""
    groupes = set()
    for morceau in (liste or "").replace(";", ",").split(","):
        terme = _sans_accent(morceau).strip()
        if not terme:
            continue
        trouve = None
        for groupe, alias in _GROUPES_ANALYTES.items():
            if any(_sans_accent(a) == terme for a in alias):
                trouve = groupe
                break
        groupes.add(trouve or terme)
    return groupes


def _analytes_retenus(analyte: str, liste: str) -> bool:
    """L'analyte figure-t-il dans la liste (nom complet, abréviation ou alias) ?"""
    demandes = _groupes_demandes(liste)
    if not demandes:
        return False
    nom = _sans_accent(analyte).strip()
    if not nom:
        return False
    for groupe in demandes:
        if groupe in _GROUPES_ANALYTES:
            if any(_sans_accent(a) in nom for a in _GROUPES_ANALYTES[groupe]):
                return True
        elif groupe and groupe in nom:
            return True
    return False


#: Fréquences : la phrase imprimée → le jeton maison. Table de CODE, pas de
#: prompt : « 1 fois par jour » doit donner `die` à chaque fois, jamais `qd`
#: ni `DIE`. L'ordre compte (du plus spécifique au plus général).
# ---------------------------------------------------------------------------
# Ligne de médicament : celle du modèle, sinon celle de la page
# ---------------------------------------------------------------------------
# Il y avait ici une table de fréquences, une table de voies, un moule à quatre
# emplacements, une composition de repli et un contrôle arithmétique. Tout cela
# a été RETIRÉ (2026-11-06) : sur le terrain, ce filet a remplacé une ligne
# juste (« Trazodone 25-50mg po hs ») par une ligne fausse (« 50mg », la force
# du comprimé) parce que le moule ne connaissait pas les intervalles. Une
# vérification qui réécrit une dose est plus dangereuse que pas de vérification
# du tout.
#
# La règle tient en trois lignes : le modèle écrit la ligne, on l'affiche telle
# quelle, et si elle manque on montre la ligne IMPRIMÉE plutôt que d'en
# fabriquer une. Le médecin est le validateur — il édite le texte dans l'onglet.


def _ligne_medicament(champs: dict) -> str:
    """La ligne du modèle si elle existe, sinon la ligne imprimée, telle quelle."""
    proposee = re.sub(r"\s+", " ", str(champs.get("line") or "")).strip()
    if proposee:
        return proposee
    return re.sub(r"\s+", " ", str(champs.get("as_printed") or "")).strip()


def _inclus_par_defaut(kind: str, champs: dict) -> bool:
    """
    Ce qui entre dans la note SANS geste du clinicien.

    Le principe : la note ne recopie pas tout ce qui est lisible. Un bilan de
    trente analytes dont vingt-huit sont normaux n'a pas sa place dans une
    consultation — mais un résultat marqué n'a pas non plus le droit de
    disparaître, et décocher TIBC à chaque fois serait une corvée.

    Deux garde-fous, dans cet ordre : le DRAPEAU imprimé par le laboratoire
    (H, L, ↑, ↓…) force l'inclusion ; sinon c'est le jugement de pertinence du
    modèle qui décide (``pertinence`` : pertinent / routine / anormal). Le
    clinicien voit tout, coche et décoche d'un clic.

    Ce qui est décoché reste visible, éditable, et se coche d'un clic — ou de
    « Tout cocher ». Rien n'est perdu ; tout n'est pas versé.
    """
    if kind == "labs":
        # Le drapeau imprimé l'emporte toujours : un résultat que le
        # laboratoire a marqué ne peut pas être écarté par un jugement.
        from app import runtime_config

        analyte = str(champs.get("analyte") or "").strip()
        # 1. Ce que le médecin a dit vouloir voir, TOUJOURS : sa liste passe
        #    avant le jugement du modèle. C'est elle qui évite de recocher
        #    sodium et TSH à chaque bilan.
        if _analytes_retenus(analyte, runtime_config.value("labs_always_include") or ""):
            return True
        # 2. Drapeau imprimé par le laboratoire : un résultat marqué ne peut
        #    pas être écarté.
        flag = str(champs.get("flag") or "").strip().lower()
        if flag in _FLAGS_ANORMAUX:
            return True
        # 3. Ce qu'il ne veut jamais voir : visible et cochable, jamais versé.
        if _analytes_retenus(analyte, runtime_config.value("labs_always_exclude") or ""):
            return False
        # Liste BLANCHE, pas liste noire : sans jugement de pertinence, on
        # n'a pas d'avis, et le défaut ne doit pas être de tout verser. La
        # valeur reste visible et se coche d'un clic.
        return str(champs.get("pertinence") or "").strip().lower() in (
            "pertinent", "anormal",
        )
    if kind == "other":
        # Là où tombent les en-têtes, les mentions administratives et les
        # restes de mise en page : jamais versé sans un geste.
        return False
    return True


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

    # Couverture : on la SIGNALE, on ne jette plus la lecture. Une page
    # oubliée doit se voir — pas coûter l'extraction entière du lot.
    manquantes = [n for n in range(1, count + 1) if n not in vues]
    if manquantes:
        logger.warning(
            "Extraction : entrée(s) %s non rendue(s) par le modèle",
            ", ".join(str(n) for n in manquantes),
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
        if kind == "medications":
            # La ligne du modèle, telle quelle. La ligne imprimée reste dans
            # ``as_printed`` — c'est la provenance, et le texte affiché reste
            # éditable : le médecin est le validateur.
            champs.setdefault("as_printed", texte)
            texte = _ligne_medicament(champs) or texte

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
            "included": _inclus_par_defaut(kind, champs),
            "edited": False,
        })

    # Imagerie : la conclusion entre, le corps du rapport accompagne — décoché.
    # Coller un compte rendu entier dans une note de consultation la noierait ;
    # le texte intégral reste à un clic, jamais perdu.
    enrichis = []
    for item in propres:
        enrichis.append(item)
        if item["kind"] != "imaging":
            continue
        corps = str(item["fields"].get("body") or "").strip()
        if len(corps) < 80 or corps == item["text"]:
            continue
        enrichis.append({
            "document_id": item["document_id"],
            "kind": "imaging",
            "text": corps,
            "fields": {"partie": "corps"},
            "source_page": item["source_page"],
            "included": False,
            "edited": False,
        })
    propres = enrichis

    # Doublon exact : la même ligne vue sur deux pages (liste qui continue,
    # photo qui recouvre la précédente). Texte identique = même médicament ;
    # on n'en garde qu'un, en réunissant les pages pour ne rien perdre. Un
    # doublon qui diffère (dose, forme, DIN) n'est PAS touché : c'est peut-être
    # deux forces différentes, et ce n'est pas à l'application de trancher.
    vus: dict = {}
    uniques = []
    for item in propres:
        if item["kind"] != "medications":
            uniques.append(item)
            continue
        cle = re.sub(r"\s+", " ", _sans_accent(item["text"])).strip()
        if cle in vus:
            garde = vus[cle]
            for page in (item.get("source_page"), garde.get("source_page")):
                if page and page not in garde.setdefault("source_pages", []):
                    garde["source_pages"].append(page)
            garde["source_pages"].sort()
            continue
        vus[cle] = item
        uniques.append(item)
    propres = uniques

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
        # Pages non rendues : affichées dans l'onglet, jamais bloquantes.
        "missing": manquantes,
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
    # Modèle RAPIDE pour la lecture : le modèle de rédaction (gemini-2.5-pro
    # sur l'instance) mettait 72 s sur un dossier de quatre pages, contre 15 s
    # ici, pour une lecture identique. Le réglage « Modèle rapide » du panneau
    # fait foi ; vide = le défaut livré.
    modele = (
        (llm.raw_fast_model("gemini") or "").strip()
        or settings.gemini_document_model
    )

    from google.genai import types

    client = llm.get_client("gemini")
    parts: List[object] = [
        types.Part.from_bytes(data=data, mime_type=mime) for data, mime in blobs
    ]
    # Budget de sortie PROPRE à l'extraction : une liste de médicaments de
    # douze lignes plus un bilan et un rapport produisent un JSON de plusieurs
    # milliers de jetons, et le plafond de la note (8 192) le tronquait — un
    # JSON coupé est illisible, donc une lecture perdue. Le raisonnement est
    # par ailleurs borné : lire une page est une tâche mécanique, le budget de
    # réflexion du panneau est réglé pour la rédaction clinique.
    plafond = max(int(settings.gemini_max_output_tokens), 16384)
    config_kwargs = dict(
        temperature=0.1,
        max_output_tokens=plafond,
        safety_settings=llm._safety_settings(),
        response_mime_type="application/json",
        # Sortie CONTRAINTE : le modèle ne peut plus produire un JSON
        # syntaxiquement invalide. C'était le vrai mode d'échec — pas la
        # troncature.
        response_schema=_SCHEMA,
    )
    try:
        # Réflexion BORNÉE, pas coupée — mais courtement bornée. Mesuré sur
        # le scan DSQ réel et le dossier de quatre pages :
        #   0 jeton    : 5,1 s / 39,1 s — et « HbAlc » au lieu de « HbA1c »
        #   256 jetons : 6,8 s / 21,8 s — tout juste
        #   1 024      : 11,8 s / 30,4 s — pas mieux
        # Un peu de réflexion paie (fidélité des unités, cas tordus) ; audelà
        # de 256, on paie sans rien gagner. Le panneau garde la main pour la
        # rédaction de la note, pas pour la lecture.
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=min(llm._gemini_thinking_budget(), 256)
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
    candidats = getattr(response, "candidates", None) or []
    motif = str(getattr(candidats[0], "finish_reason", "") or "") if candidats else ""
    if "MAX_TOKENS" in motif:
        raise ExtractionError(
            "Lecture interrompue : le document produit plus de contenu que le "
            "modèle ne peut en rendre d'un coup. Envoyez-le en deux fois (les "
            "pages d'un même rapport peuvent rester ensemble)."
        )
    payload = _parse(getattr(response, "text", None) or "", motif)
    result = _normalize(payload, len(blobs))
    result["model"] = modele
    result["usage"] = usage
    result["version"] = 1
    return result
