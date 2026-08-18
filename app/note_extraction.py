"""
note_extraction.py — transcription -> ExtractedNote (JSON), puis réparation ciblée.
===================================================================================

Remplace le principe de l'ancien ``llm.generate_note`` (un seul appel qui
rend directement le markdown final) par : un appel qui rend du JSON structuré
(``app.llm.complete(..., json_mode=True)``, déjà utilisé pour
``extract_metadata`` — voir llm.py), validé par ``note_validator``, réparé au
besoin par des appels CIBLÉS (juste le champ en cause + la règle violée +
l'extrait de transcription concerné — jamais une régénération complète), et
seulement alors passé à ``note_renderer`` pour produire le markdown.

Ce module ne décide PAS quel modèle utiliser ni comment ce markdown est
affiché/streamé au médecin — ça reste la responsabilité de l'appelant
(``main.py`` aujourd'hui utilise ``llm.generate_note_stream`` ; le
branchement de ce nouveau chemin est une décision séparée, notamment parce
que le streaming actuel envoie des deltas de markdown au fil de l'eau, ce
qu'un JSON structuré ne permet pas de faire sans y repenser).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app import llm
from app.note_schema import ElementAValider, ExtractedNote, LayoutSpec
from app.note_validator import ValidationIssue, ValidationResult, validate

logger = logging.getLogger(__name__)

DEFAULT_MAX_REPAIR_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Prompt d'extraction — mêmes règles de contenu (consigne générale + gabarit),
# format de sortie différent (JSON, pas markdown).
# ---------------------------------------------------------------------------


def _section_skeleton(layout: LayoutSpec, label: str) -> object:
    child_headings = [e for e in layout.entries if e.kind == "heading" and e.parent == label]
    child_fields = [e for e in layout.entries if e.kind == "bold_field" and e.parent == label]
    if child_headings:
        return {c.label: _section_skeleton(layout, c.label) for c in child_headings}
    if child_fields:
        return {f.label: "<valeur dictée, ou omettre la clé si non dictée>" for f in child_fields}
    return "<contenu de la rubrique, prose ou liste selon la consigne — omettre la clé entière si rien n'a été dicté>"


def build_expected_json_skeleton(layout: LayoutSpec) -> dict:
    top_headings = [e for e in layout.entries if e.kind == "heading" and e.parent is None]
    skeleton = {
        "header_fields": {label: "<valeur dictée, ou omettre la clé>" for label in layout.top_level_fields()},
        "sections": {h.label: _section_skeleton(layout, h.label) for h in top_headings},
        "elements_a_valider": [
            {"kind": "item", "terme_dicte": "...", "correction": "... (absent/null si à confirmer)"},
            {"kind": "group", "texte_groupe": "ex. « 5 dates approximatives non confirmées » — seulement si plus de 8 items individuels"},
        ],
        "grounded_fields": [
            {
                "field": "identifiant court du champ, ex. dose_pregabaline",
                "value": "valeur retenue, ou null si aucune valeur fiable",
                "source_span": "extrait EXACT de la transcription dont cette valeur est tirée",
                "note": "",
            }
        ],
    }
    return skeleton


_JSON_FORMAT_INSTRUCTIONS_FR = """\

---

# FORMAT DE SORTIE — JSON STRUCTURÉ (et non du markdown)

Toutes les règles ci-dessus s'appliquent SANS EXCEPTION — elles décrivent le
contenu clinique attendu. Ce qui change, c'est uniquement l'encodage : tu ne
rends plus le rapport en markdown, tu rends un unique objet JSON valide, de
cette forme exacte (les clés absentes valent « rien à dire » — ne mets jamais
une chaîne vide, une valeur devinée ou un texte de remplissage à la place) :

```json
{skeleton}
```

Règles spécifiques à cet encodage :

- ``sections`` : une clé par rubrique du gabarit (mêmes intitulés, même
  imbrication que ci-dessus). Omets entièrement une clé si la dictée n'a
  fourni aucun contenu pour cette rubrique — ne mets ni chaîne vide, ni null,
  ni texte de remplissage.
- ``elements_a_valider`` : un item par ligne de la future rubrique Éléments à
  valider. Un item à ``correction`` renseignée = lecture retenue avec
  confiance ; ``correction`` absente/null = à confirmer. N'écris jamais la
  mise en forme finale (« → », « **correction apportée :** ») toi-même — le
  code s'en charge à partir de ces champs structurés.
- ``grounded_fields`` : pour CHAQUE valeur critique que tu affirmes dans
  ``sections`` (médicament, dose, date, nom propre, diagnostic, chiffre,
  résultat), ajoute une entrée ici avec ``source_span`` = l'extrait EXACT
  (mots identiques) de la transcription qui justifie cette valeur. C'est ce
  qui permet de vérifier mécaniquement que tu n'inventes rien — un champ
  sans ``source_span`` correspondant sera rejeté.
- N'inclus AUCUN texte hors de l'objet JSON — pas de phrase d'introduction,
  pas de bloc ```markdown, uniquement le JSON.
"""

_JSON_FORMAT_INSTRUCTIONS_EN = """\

---

# OUTPUT FORMAT — STRUCTURED JSON (not markdown)

All the rules above still apply WITHOUT EXCEPTION — they describe the
expected clinical content. Only the encoding changes: you no longer render
the report as markdown, you render a single valid JSON object, in exactly
this shape (a missing key means "nothing to say" — never put an empty
string, a guessed value, or filler text instead):

```json
{skeleton}
```

Rules specific to this encoding:

- ``sections``: one key per template section (same headings, same nesting as
  above). Omit a key entirely if the dictation provided no content for that
  section — never an empty string, null, or filler text.
- ``elements_a_valider``: one item per future line of the Items to Confirm
  section. An item with ``correction`` filled = a confidently resolved
  reading; missing/null ``correction`` = unconfirmed. Never write the final
  formatting yourself — code handles that from these structured fields.
- ``grounded_fields``: for EVERY critical value you assert in ``sections``
  (medication, dose, date, proper name, diagnosis, number, result), add an
  entry here with ``source_span`` = the EXACT excerpt (identical wording)
  from the transcript that justifies it. This is what lets the value be
  mechanically checked — a value with no matching ``source_span`` will be
  rejected.
- Include NO text outside the JSON object — no introduction, no ```markdown
  fence, only the JSON.
"""


def build_system_prompt(template_system_instructions: str, general_prompt: str, layout: LayoutSpec, language: str = "fr") -> str:
    skeleton = json.dumps(build_expected_json_skeleton(layout), ensure_ascii=False, indent=2)
    suffix = (_JSON_FORMAT_INSTRUCTIONS_EN if language == "en" else _JSON_FORMAT_INSTRUCTIONS_FR).format(skeleton=skeleton)
    return f"{template_system_instructions.strip()}\n\n{general_prompt.strip()}{suffix}"


# ---------------------------------------------------------------------------
# Appel + parsing (même stratégie de repli que _parse_metadata_json)
# ---------------------------------------------------------------------------


def _parse_json_completion(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start:end + 1])
        raise


def extract_note(
    transcript: str,
    layout: LayoutSpec,
    template_system_instructions: str,
    general_prompt: str,
    *,
    model: str,
    language: str = "fr",
    temperature: float = 0.15,
    max_tokens: int = 8192,
) -> ExtractedNote:
    system = build_system_prompt(template_system_instructions, general_prompt, layout, language)
    label = "TRANSCRIPTION" if language != "en" else "TRANSCRIPT"
    user = f"{label} :\n<<<DICTEE\n{transcript.strip()}\nDICTEE>>>"
    result = llm.complete(system, user, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=True)
    payload = _parse_json_completion(result.text)
    return ExtractedNote.from_dict(payload)


# ---------------------------------------------------------------------------
# Réparation ciblée — un appel par problème, jamais une régénération.
# ---------------------------------------------------------------------------


_INDEX_RE = re.compile(r"^(.*)\[(\d+)\]$")


def _get_path(note: ExtractedNote, path: str) -> object:
    m = _INDEX_RE.match(path)
    if m:
        container = _get_path(note, m.group(1))
        idx = int(m.group(2))
        return container[idx] if isinstance(container, list) and 0 <= idx < len(container) else None
    parts = path.split(".")
    if parts[0] == "header_fields":
        return note.header_fields.get(parts[1])
    container = note.sections
    for part in parts[1:]:
        if not isinstance(container, dict):
            return None
        container = container.get(part)
    return container


def _set_path(note: ExtractedNote, path: str, value: Optional[str]) -> None:
    m = _INDEX_RE.match(path)
    if m:
        container = _get_path(note, m.group(1))
        idx = int(m.group(2))
        if isinstance(container, list) and 0 <= idx < len(container):
            if value:
                container[idx] = value
            else:
                container.pop(idx)
        return
    parts = path.split(".")
    if parts[0] == "header_fields":
        if value:
            note.header_fields[parts[1]] = value
        else:
            note.header_fields.pop(parts[1], None)
        return
    container = note.sections
    for part in parts[1:-1]:
        nxt = container.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            container[part] = nxt
        container = nxt
    if value:
        container[parts[-1]] = value
    else:
        container.pop(parts[-1], None)


# NB : "grounding_mismatch"/"grounding_missing_span" ne sont PAS ici — un
# médicament/dose mal ancré n'est jamais renvoyé au modèle pour "correction"
# ciblée (§6 : ne jamais auto-corriger silencieusement une valeur clinique
# incertaine). Le repli déterministe ci-dessous (ajout à Éléments à valider)
# s'applique dès le premier passage, sans consommer de tentative de
# réparation.
_REPAIRABLE_CODES = {"placeholder_leftover", "html_markup"}


def _repair_one(issue: ValidationIssue, note: ExtractedNote, transcript: str, *, model: str) -> bool:
    """Tente un correctif ciblé pour un seul problème. Retourne True si un
    correctif a été appliqué (pas nécessairement suffisant — la revalidation
    tranche)."""
    if issue.code not in _REPAIRABLE_CODES:
        return False
    current_value = _get_path(note, issue.path)
    excerpt = transcript.strip()[:4000]
    system = (
        "Tu corriges UN SEUL champ d'une note clinique structurée, déjà extraite. "
        "Ne régénère rien d'autre. Réponds avec UNIQUEMENT un objet JSON "
        '{"new_value": "<valeur corrigée>"} — new_value peut être null si le champ '
        "doit être retiré faute de valeur fiable."
    )
    user = (
        f"RÈGLE VIOLÉE : {issue.message}\n\n"
        f"VALEUR ACTUELLE : {current_value!r}\n\n"
        f"EXTRAIT DE TRANSCRIPTION (référence, ne pas dépasser) :\n<<<\n{excerpt}\n>>>"
    )
    try:
        result = llm.complete(system, user, model=model, temperature=0.0, max_tokens=512, json_mode=True)
        payload = _parse_json_completion(result.text)
    except Exception as exc:  # noqa: BLE001 — une réparation ratée n'est pas fatale
        logger.warning("Réparation ciblée impossible pour %s : %s", issue.path, exc)
        return False
    if not isinstance(payload, dict) or "new_value" not in payload:
        return False
    _set_path(note, issue.path, payload.get("new_value"))
    return True


def validate_and_repair(
    note: ExtractedNote,
    layout: LayoutSpec,
    transcript: str,
    *,
    model: str,
    language: str = "fr",
    max_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
) -> ValidationResult:
    """Valide, répare ce qui peut l'être (plafonné), et applique les replis
    déterministes décrits dans le brief pour ce qui ne peut pas l'être.
    Mute ``note``. Retourne le dernier ``ValidationResult`` (ce qui reste,
    après réparation/repli, est la responsabilité de l'appelant — voir
    ``ValidationResult.blocked``)."""
    result = validate(note, layout, transcript, language)

    for attempt in range(max_attempts):
        repairable = [i for i in result.needs_repair if i.code in _REPAIRABLE_CODES]
        if not repairable:
            break
        for issue in repairable:
            _repair_one(issue, note, transcript, model=model)
        result = validate(note, layout, transcript, language)

    # Replis déterministes pour ce qui reste après le plafond de tentatives —
    # jamais de nouvelle boucle, jamais un passage silencieux.
    for issue in result.needs_repair:
        if issue.code == "grounding_mismatch" or issue.code == "grounding_missing_span":
            idx = int(issue.path.split("[")[1].rstrip("]"))
            gf = note.grounded_fields[idx]
            note.elements_a_valider.append(
                ElementAValider(kind="item", terme_dicte=gf.field or (gf.value or ""), correction=None)
            )
            # Signalé via Éléments à valider — évite que la revalidation ne
            # re-déclenche indéfiniment le même problème de grounding.
            gf.value = None
        elif issue.code in ("placeholder_leftover", "html_markup"):
            _set_path(note, issue.path, None)

    return validate(note, layout, transcript, language)
