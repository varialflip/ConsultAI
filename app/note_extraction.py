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

from app import llm, runtime_config
from app.drug_lookup import search_drug
from app.note_renderer import OWN_CONTENT_KEY
from app.note_schema import LIST_STYLE_MARKERS, DrugLookup, ElementAValider, ExtractedNote, LayoutSpec
from app.note_validator import ValidationIssue, ValidationResult, validate

logger = logging.getLogger(__name__)

DEFAULT_MAX_REPAIR_ATTEMPTS = 2

# ---------------------------------------------------------------------------
# Vérification de médicament via outil d'appel de fonction (branche
# selfhosted, expérimental) — voir _extract_note_with_dpd_tool ci-dessous.
# ---------------------------------------------------------------------------
#: Plafonds volontairement serrés : cet appel s'insère dans un chemin DÉJÀ
#: bloquant/non diffusé en direct (voir main._generate_json_pipeline) — le
#: médecin attend un seul spinner, pas 8 allers-retours réseau. Au-delà, on
#: force un dernier tour sans outil plutôt que de laisser la boucle continuer.
_DPD_TOOL_MAX_ROUNDS = 2
_DPD_TOOL_MAX_CALLS = 6

_DPD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verifier_medicament_dpd",
        "description": (
            "Vérifie un nom de médicament (marque ou ingrédient actif) "
            "contre la Base de données sur les produits pharmaceutiques de "
            "Santé Canada. À utiliser pour tout médicament dont le nom est "
            "incertain ou reconstruit par homophonie, avant de trancher. Le "
            "résultat ne détermine PAS la décision clinique — un médicament "
            "absent de la base n'est pas forcément une erreur (produit "
            "étranger, composé en pharmacie, retiré du marché)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "terme": {
                    "type": "string",
                    "description": "Nom du médicament tel que retenu, en français.",
                },
                "type": {
                    "type": "string",
                    "enum": ["marque", "ingredient"],
                    "description": (
                        "« marque » pour un nom commercial (ex. Xanax), "
                        "« ingredient » pour la dénomination commune "
                        "internationale (ex. létrozole)."
                    ),
                },
            },
            "required": ["terme", "type"],
        },
    },
}

_DPD_TOOL_GUIDANCE_FR = (
    "\n\nOUTIL DISPONIBLE — verifier_medicament_dpd : pour tout médicament "
    "dont le nom est incertain, mal entendu, ou reconstruit par homophonie "
    "(voir la méthode de correction ci-dessus), appelle cet outil AVANT de "
    "trancher entre l'inscrire dans MÉDICATION ACTUELLE ou le renvoyer en "
    "Éléments à valider. Une absence de résultat n'est pas une preuve "
    "d'erreur — c'est un indice de plus, pas une décision automatique. "
    "Si un passage dicté est ambigu entre UN médicament ou DEUX (par "
    "exemple deux noms enchaînés sans pause claire), appelle l'outil "
    "SÉPARÉMENT pour chaque segment candidat avant de les fusionner — un "
    "segment sans résultat est lui-même un indice qu'il s'agit peut-être "
    "d'un médicament distinct mal orthographié, pas d'un bruit à absorber "
    "dans le segment voisin."
)
_DPD_TOOL_GUIDANCE_EN = (
    "\n\nTOOL AVAILABLE — verifier_medicament_dpd: for any medication whose "
    "name is uncertain, misheard, or reconstructed from a mishearing (see "
    "the correction method above), call this tool BEFORE deciding whether "
    "to record it under CURRENT MEDICATIONS or flag it under Items to "
    "verify. No result found is not proof of an error — it's one more "
    "signal, not an automatic decision. If a dictated passage is ambiguous "
    "between ONE medication or TWO (for example two names run together "
    "with no clear pause), call the tool SEPARATELY for each candidate "
    "segment before merging them — a segment with no result is itself a "
    "signal it might be a distinct, misspelled medication, not noise to "
    "fold into its neighbor."
)


# ---------------------------------------------------------------------------
# Prompt d'extraction — mêmes règles de contenu (consigne générale + gabarit),
# format de sortie différent (JSON, pas markdown).
# ---------------------------------------------------------------------------


def _instruction_hint(layout: LayoutSpec, label: str) -> str:
    """Lignes d'exemple/consigne du gabarit entre accolades (ex.
    « {{Phrase résumé}} », « 1. {{Problème 1}} ») trouvées SOUS cette
    rubrique — jamais reproduites telles quelles (voir note_schema.parse_layout,
    kind="instruction"), mais utiles comme indice de forme à transmettre au
    modèle plutôt que d'être purement et simplement jetées. Les marqueurs de
    style de liste (LIST_STYLE_MARKERS) sont exclus : ce ne sont pas des
    exemples de contenu, et LayoutSpec.list_style s'en charge séparément
    (note_renderer numérote lui-même — le modèle n'a pas à le savoir pour
    encoder correctement, voir la note ci-dessous)."""
    lines = [
        e.label for e in layout.entries
        if e.kind == "instruction" and e.parent == label and e.label not in LIST_STYLE_MARKERS
    ]
    return " ".join(lines)


def _list_style_nudge(layout: LayoutSpec, label: str) -> str:
    """Rappel explicite d'encodage en tableau JSON — SEULEMENT quand le
    gabarit porte un marqueur {{...}} explicite pour cette rubrique (voir
    LayoutSpec.explicit_list_style) : jamais par défaut, sans quoi la
    moindre rubrique retomberait sur « bulleted » (défaut de rendu) et
    recevrait la consigne à tort. Le numérotage/les puces eux-mêmes restent
    toujours faits par note_renderer, jamais par le modèle — ça évite qu'un
    item sur deux perde son marqueur ou que la numérotation reparte à 1."""
    style = layout.explicit_list_style(label)
    if style == "numbered":
        return (
            " — cette rubrique attend une liste NUMÉROTÉE : encode-la comme "
            "un tableau JSON de chaînes, un item distinct par élément (le "
            "code ajoute les numéros, n'écris pas « 1. », « 2. » toi-même)."
        )
    if style == "bulleted":
        return (
            " — cette rubrique attend une liste À PUCES : encode-la comme "
            "un tableau JSON de chaînes, un item distinct par élément (le "
            "code ajoute les puces, n'écris pas « - » toi-même)."
        )
    return ""


def _leaf_placeholder(layout: LayoutSpec, label: str) -> str:
    base = "<contenu de la rubrique, prose ou liste selon la consigne — omettre la clé entière si rien n'a été dicté>"
    hint = _instruction_hint(layout, label)
    if hint:
        base = f"{base} (forme attendue suggérée par le gabarit : {hint})"
    return base + _list_style_nudge(layout, label)


def _section_skeleton(layout: LayoutSpec, label: str) -> object:
    child_headings = [e for e in layout.entries if e.kind == "heading" and e.parent == label]
    child_fields = [e for e in layout.entries if e.kind == "bold_field" and e.parent == label]
    if child_headings or child_fields:
        skeleton = {}
        # Cette rubrique peut avoir SON PROPRE contenu direct EN PLUS de ses
        # sous-rubriques/champs (ex. MÉDICATION ACTUELLE contient sa liste de
        # médicaments ET une sous-rubrique ALLERGIES) — voir note_renderer.OWN_CONTENT_KEY.
        skeleton[OWN_CONTENT_KEY] = (
            "<contenu dicté DIRECTEMENT sous cette rubrique (pas sous une "
            "sous-rubrique ci-dessous) — omettre la clé entière si rien n'a "
            "été dicté à ce niveau>" + _list_style_nudge(layout, label)
        )
        for c in child_headings:
            skeleton[c.label] = _section_skeleton(layout, c.label)
        for f in child_fields:
            skeleton[f.label] = "<valeur dictée, ou omettre la clé si non dictée>"
        return skeleton
    return _leaf_placeholder(layout, label)


def build_expected_json_skeleton(layout: LayoutSpec) -> dict:
    top_headings = [e for e in layout.entries if e.kind == "heading" and e.parent is None]
    skeleton = {
        "header_fields": {label: "<valeur dictée, ou omettre la clé>" for label in layout.top_level_fields()},
        "sections": {h.label: _section_skeleton(layout, h.label) for h in top_headings},
        "elements_a_valider": [
            {
                "kind": "item", "terme_dicte": "...",
                "correction": "la lecture corrigée elle-même (ex. « Prégabaline 150 mg ») — "
                "OMETS CETTE CLÉ ENTIÈREMENT si incertain, n'écris JAMAIS le mot "
                "« à confirmer » ni « unconfirmed » comme valeur de correction",
            },
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
- MISE EN FORME À L'INTÉRIEUR D'UNE VALEUR DE ``sections`` : si la rubrique
  contient plusieurs éléments distincts (Impression, Plan, examen
  physique...), ENCODE-LA COMME UN TABLEAU JSON DE CHAÎNES, un item par
  élément — jamais un seul bloc de texte continu. N'écris JAMAIS toi-même
  un numéro ou une puce au début d'un item (pas de « 1. », pas de « - ») :
  le code s'en charge à l'affichage, à partir du gabarit. Une rubrique
  purement narrative (paragraphe suivi, pas une liste d'éléments distincts)
  reste une simple chaîne de texte.
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
- FORMATTING INSIDE A ``sections`` VALUE: if a section holds several
  distinct items (Impression, Plan, physical exam...), ENCODE IT AS A JSON
  ARRAY OF STRINGS, one item per element — never one continuous block of
  text. NEVER write a number or bullet yourself at the start of an item
  (no "1.", no "-"): the code adds it at render time, from the template.
  A purely narrative section (a running paragraph, not a list of distinct
  items) stays a plain string.
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


def _dpd_kind(type_arg: str) -> str:
    return "ingredient" if str(type_arg or "").strip().lower().startswith("ingred") else "brand"


def _add_usage(total: dict, delta: dict) -> None:
    """Additionne deux ``usage`` (voir ``llm.Completion``/``ToolCompletion``)
    — plusieurs tours d'appel d'outils consomment chacun des jetons, et rien
    d'autre ne les additionne pour l'appelant. ``None`` traité comme 0 (un
    fournisseur qui ne rapporte pas un compteur ne doit pas empêcher
    d'additionner ceux qu'il rapporte)."""
    for cle in ("prompt_tokens", "output_tokens", "total_tokens"):
        valeur = delta.get(cle)
        if valeur is None:
            continue
        total[cle] = (total.get(cle) or 0) + valeur


def _extract_note_with_dpd_tool(
    system: str, user: str, *, model: str, temperature: float, max_tokens: int,
    provider: str, language: str, usage_out: Optional[dict] = None,
) -> ExtractedNote:
    """Variante de ``extract_note`` qui donne au modèle un outil d'appel de
    fonction (``verifier_medicament_dpd``) pendant l'extraction — voir
    ``app.drug_lookup`` et le réglage ``note_lookup_dpd``. Réservé à Mistral
    (voir ``llm.complete_with_tools``) : seul fournisseur, aujourd'hui, dont
    l'appel d'outils est câblé dans ce dépôt.

    Boucle bornée (``_DPD_TOOL_MAX_ROUNDS``/``_DPD_TOOL_MAX_CALLS``) : le
    modèle peut ignorer l'outil complètement (extraction inchangée), l'appeler
    plusieurs fois, ou ne jamais rendre de contenu final dans le budget
    imparti — dans ce dernier cas, un tour de repli SANS outil force une
    réponse plutôt que de laisser la boucle s'éterniser."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    drug_lookups: list[DrugLookup] = []
    calls_used = 0
    final_text = ""

    for _ in range(_DPD_TOOL_MAX_ROUNDS):
        tools = [_DPD_TOOL_SCHEMA] if calls_used < _DPD_TOOL_MAX_CALLS else None
        result = llm.complete_with_tools(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, messages=messages, json_mode=True, provider=provider,
        )
        if usage_out is not None:
            _add_usage(usage_out, result.usage)
        if not result.tool_calls:
            final_text = result.text
            break

        messages.append(result.raw_message)
        for appel in result.tool_calls:
            if calls_used >= _DPD_TOOL_MAX_CALLS:
                # Budget épuisé EN COURS de tour : chaque tool_call de ce
                # message assistant exige quand même une réponse appariée
                # (protocole Mistral), sinon le tour suivant est malformé.
                messages.append({
                    "role": "tool", "tool_call_id": appel.id,
                    "content": json.dumps({"erreur": "budget de vérifications épuisé"}),
                })
                continue
            calls_used += 1
            try:
                arguments = json.loads(appel.arguments_raw)
            except (json.JSONDecodeError, TypeError):
                messages.append({
                    "role": "tool", "tool_call_id": appel.id,
                    "content": json.dumps({"erreur": "arguments invalides"}),
                })
                continue
            terme = str(arguments.get("terme") or "").strip()
            if not terme:
                lookup = None
            else:
                lookup = search_drug(terme, kind=_dpd_kind(arguments.get("type", "")), language=language)
            if lookup is None:
                messages.append({
                    "role": "tool", "tool_call_id": appel.id,
                    "content": json.dumps({"erreur": "terme manquant"}),
                })
                continue
            drug_lookups.append(lookup)
            messages.append({
                "role": "tool", "tool_call_id": appel.id,
                "content": json.dumps({
                    "trouve": lookup.found,
                    "nom_correspondant": lookup.matched_name,
                    "din": lookup.din,
                }, ensure_ascii=False),
            })
    else:
        # Budget de tours épuisé sans contenu final : un dernier appel SANS
        # outil force une réponse plutôt que d'abandonner la génération.
        result = llm.complete_with_tools(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens,
            tools=None, messages=messages, json_mode=True, provider=provider,
        )
        if usage_out is not None:
            _add_usage(usage_out, result.usage)
        final_text = result.text

    payload = _parse_json_completion(final_text)
    note = ExtractedNote.from_dict(payload)
    note.drug_lookups = drug_lookups
    return note


def extract_note(
    transcript: str,
    layout: LayoutSpec,
    template_system_instructions: str,
    general_prompt: str,
    *,
    model: str,
    language: str = "fr",
    provider: Optional[str] = None,
    temperature: float = 0.15,
    max_tokens: int = 8192,
    usage_out: Optional[dict] = None,
) -> ExtractedNote:
    """``usage_out``, si fourni, est MUTÉ pour y accumuler les jetons
    consommés (voir ``llm.Completion.usage``) — jamais dans la valeur de
    retour, pour ne pas changer le type de retour pour les appelants qui ne
    s'y intéressent pas (dont les tests existants)."""
    system = build_system_prompt(template_system_instructions, general_prompt, layout, language)
    label = "TRANSCRIPTION" if language != "en" else "TRANSCRIPT"
    user = f"{label} :\n<<<DICTEE\n{transcript.strip()}\nDICTEE>>>"

    resolved_provider = provider or llm.active_provider()
    if resolved_provider == "mistral" and runtime_config.value("note_lookup_dpd") == "true":
        system += _DPD_TOOL_GUIDANCE_EN if language == "en" else _DPD_TOOL_GUIDANCE_FR
        return _extract_note_with_dpd_tool(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens,
            provider=resolved_provider, language=language, usage_out=usage_out,
        )

    result = llm.complete(
        system, user, model=model, temperature=temperature, max_tokens=max_tokens,
        json_mode=True, provider=provider,
    )
    if usage_out is not None:
        _add_usage(usage_out, result.usage)
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
_REPAIRABLE_CODES = {"placeholder_leftover", "html_markup", "cramped_numbered_list"}


def _repair_one(issue: ValidationIssue, note: ExtractedNote, transcript: str, *, model: str, provider: Optional[str] = None) -> bool:
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
        result = llm.complete(system, user, model=model, temperature=0.0, max_tokens=512, json_mode=True, provider=provider)
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
    provider: Optional[str] = None,
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
            _repair_one(issue, note, transcript, model=model, provider=provider)
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
