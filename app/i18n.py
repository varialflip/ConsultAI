"""
i18n.py — Textes de l'interface, en français et en anglais.
===========================================================================

POURQUOI UN SEUL DICTIONNAIRE À DEUX COLONNES
---------------------------------------------
Chaque entrée est un couple ``(français, anglais)`` sous une même clé. Deux
dictionnaires séparés auraient dérivé l'un de l'autre à la première
modification : une clé ajoutée d'un côté et oubliée de l'autre ne se voit pas
avant qu'un écran ne s'affiche vide. Ici, une entrée incomplète est une erreur
de syntaxe.

CE QUI EST TRADUIT, ET CE QUI NE L'EST PAS
------------------------------------------
Traduit : tout ce que l'usager lit — l'interface, les messages d'erreur
renvoyés au navigateur, les libellés du panneau d'administration, et les
consignes de base envoyées au modèle de langage (voir ``llm.py``).

Non traduit, volontairement :

* **Le code et ses commentaires**, qui restent en français : ils s'adressent
  à qui maintient l'application, pas à qui l'utilise.
* **Les gabarits de note**, qui appartiennent au médecin. Ils vivent en base,
  dans la langue où il les a écrits. Changer la langue de l'interface ne
  réécrit pas ses gabarits — voir la remarque sur ``document_language``.
* **Les champs de substitution** (``{{DATE}}``, ``{{DEMANDEUR}}``…), qui font
  partie du contrat des gabarits existants. Les traduire casserait les
  gabarits déjà écrits. (``{{PATIENT}}`` et ``{{DOSSIER}}`` sont conservés par
  compatibilité, mais ne sont plus alimentés : la ligne qui les porte est
  retirée de la note — l'identité du patient n'est plus collectée.)
* **Les noms propres** : ConsultAI, Deepgram, Soniox, Pangolin, Markdown, PDF.

LA LANGUE N'EST PAS QU'UN HABILLAGE
-----------------------------------
Elle traverse toute la chaîne : l'interface, le code de langue envoyé au
service de reconnaissance vocale, le lexique d'adaptation (propre au
français), et la langue dans laquelle le modèle rédige la note. Les
correspondances sont ici, en un seul endroit, plutôt que dispersées dans
``stt.py`` et ``llm.py``.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple

#: Langues offertes. L'ordre est celui du menu d'identité, en haut à droite.
LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("fr", "Français"),
    ("en", "English"),
)

DEFAULT_LANGUAGE = "fr"

_INDEX = {"fr": 0, "en": 1}


def normalize(language: str) -> str:
    """
    Ramène n'importe quelle étiquette de langue à « fr » ou « en ».

    Tolérante par conception : ``fr-CA``, ``FR``, ``en_US`` et une valeur vide
    doivent tous donner une langue utilisable. Une langue inconnue retombe sur
    le français plutôt que de faire échouer l'affichage.
    """
    code = str(language or "").strip().lower().replace("_", "-")
    if not code:
        return DEFAULT_LANGUAGE
    racine = code.split("-", 1)[0]
    return racine if racine in _INDEX else DEFAULT_LANGUAGE


# ===========================================================================
# Chaîne de traitement : ce que la langue change en dehors de l'écran
# ===========================================================================

#: Code de langue par service de reconnaissance vocale.
#:
#: Chaque service a sa propre convention : Google veut une étiquette complète
#: avec la région, AssemblyAI et Soniox se contentent de la racine. Le
#: québécois n'existe pas comme dialecte distinct chez AssemblyAI — « fr »
#: couvre le français canadien, c'est documenté chez eux.
STT_LANGUAGE_CODES: Dict[str, Dict[str, str]] = {
    "fr": {
        "google": "fr-CA",
        "deepgram": "fr-CA",
        "assemblyai": "fr",
        "soniox": "fr",
        # Cohere et Mistral attendent de l'ISO-639-1 strict : « fr-CA » serait
        # refusé.
        "cohere": "fr",
        "mistral": "fr",
        "openai": "fr",
        "custom": "fr",
        "modulate": "fr",
        "openrouter": "fr",
    },
    "en": {
        "google": "en-CA",
        "deepgram": "en-CA",
        "assemblyai": "en",
        "soniox": "en",
        "cohere": "en",
        "mistral": "en",
        "openai": "en",
        "custom": "en",
        "modulate": "en",
        "openrouter": "en",
    },
}


def stt_language_code(language: str, provider: str) -> str:
    """Code de langue attendu par ``provider`` pour la langue demandée."""
    table = STT_LANGUAGE_CODES.get(normalize(language), STT_LANGUAGE_CODES["fr"])
    return table.get(provider, table["google"])


#: Nom de la langue de rédaction, tel qu'écrit dans la consigne au modèle.
#:
#: « français québécois » plutôt que « français » : la terminologie du réseau
#: de la santé et les unités attendues en diffèrent, et le modèle en tient
#: compte. Même logique pour l'anglais canadien.
DOCUMENT_LANGUAGE = {
    "fr": "français québécois",
    "en": "Canadian English",
}


def document_language(language: str) -> str:
    return DOCUMENT_LANGUAGE[normalize(language)]


def uses_french_lexicon(language: str) -> bool:
    """
    Le lexique d'adaptation intégré doit-il être envoyé au service vocal ?

    Ce lexique est une liste de termes **français** (voir ``stt.py``). Envoyé
    pendant une dictée en anglais, il ne peut rien améliorer et pousse le
    moteur vers des mots qui ne seront pas prononcés : on ne l'envoie donc que
    pour le français. Le vocabulaire additionnel du gabarit, lui, reste
    toujours transmis — il est écrit par le médecin, qui sait dans quelle
    langue il dicte.
    """
    return normalize(language) == "fr"


# ===========================================================================
# Textes de l'interface
# ===========================================================================
# Clé → (français, anglais).
#
# Les clés sont préfixées par zone d'écran. Les valeurs peuvent contenir des
# champs entre accolades simples ({n}, {name}…) : ils sont remplis par ``t()``
# côté serveur et par ``T()`` côté navigateur, avec la même convention.
# ===========================================================================

_STRINGS: Dict[str, Tuple[str, str]] = {
    # --- Identité de l'application ----------------------------------------
    # Le sous-titre a longtemps annoncé une spécialité. L'application n'en a
    # pas : ce qui est propre à une pratique vit dans les gabarits et dans la
    # consigne générale, pas dans le titre.
    "app.subtitle": ("Dictée clinique assistée", "Assisted clinical dictation"),
    "app.description": (
        "Dictée et mise en forme de consultations cliniques.",
        "Clinical consultation dictation and formatting.",
    ),

    # --- En-tête ------------------------------------------------------------
    "header.new": ("Nouvelle", "New"),
    "header.new_title": ("Nouvelle consultation", "New consultation"),
    "header.drafts": ("Brouillons", "Drafts"),
    "header.drafts_title": ("Mes brouillons", "My drafts"),
    "header.templates": ("Gabarits", "Templates"),
    "header.templates_title": ("Gérer les gabarits", "Manage templates"),
    "header.settings": ("Réglages", "Settings"),
    "header.settings_title": ("Panneau d'administration", "Administration panel"),

    # --- Identité et déconnexion -------------------------------------------
    # La langue se choisit ici, dans le menu de l'usager, et non dans le
    # panneau d'administration : elle regarde la personne qui lit l'écran, pas
    # l'installation.
    "identity.language": ("Langue", "Language"),
    "identity.theme": ("Thème", "Theme"),
    "identity.language_saved": (
        "Langue changée. L'écran se recharge…",
        "Language changed. Reloading…",
    ),
    "identity.language_failed": (
        "Changement de langue impossible : {error}",
        "Could not change the language: {error}",
    ),
    "identity.logout": ("Se déconnecter", "Sign out"),
    "identity.logout_busy": (
        "Terminez ou arrêtez la dictée en cours avant de vous déconnecter.",
        "Finish or stop the current dictation before signing out.",
    ),
    "identity.logout_progress": (
        "Déconnexion du fournisseur d'identité…",
        "Signing out of the identity provider…",
    ),

    # --- Barre d'outils et enregistrement ----------------------------------
    "toolbar.template_aria": ("Gabarit de consultation", "Consultation template"),
    "toolbar.loading": ("Chargement…", "Loading…"),
    "rec.record": ("Enregistrer", "Record"),
    "rec.recording": ("Enregistrement…", "Recording…"),
    "rec.paused": ("En pause", "Paused"),
    "rec.pause": ("Pause", "Pause"),
    "rec.resume": ("Reprendre", "Resume"),
    "rec.pause_title": ("Pause / Reprendre", "Pause / Resume"),
    "rec.finish_title": ("Terminer et transcrire", "Finish and transcribe"),
    "rec.abort_title": ("Arrêter sans envoyer", "Stop without sending"),
    "rec.import_title": ("Importer un fichier audio", "Import an audio file"),
    "rec.level_aria": ("Niveau sonore du micro", "Microphone level"),
    "dictaphone.close_label": ("Quitter", "Close"),
    "dictaphone.toggle_title": (
        "Mode retourné (grand bouton)",
        "Flip mode (big button)",
    ),
    "dictaphone.exit_title": (
        "Quitter le mode retourné",
        "Exit flip mode",
    ),
    "dictaphone.mode_group_aria": (
        "Mode de conversation",
        "Recording mode",
    ),
    "dictaphone.mode_tap": ("Toucher", "Tap"),
    "dictaphone.mode_ptt": ("Maintenir", "Hold"),
    "dictaphone.tap_hint": (
        "Touchez pour enregistrer, touchez encore pour mettre en pause",
        "Tap to record, tap again to pause",
    ),
    "dictaphone.hold_hint": (
        "Maintenez pour enregistrer, relâchez pour mettre en pause",
        "Hold to record, release to pause",
    ),
    "dictaphone.transcript_placeholder": (
        "La transcription apparaîtra ici…",
        "The transcription will appear here…",
    ),

    # --- Détails ------------------------------------------------------------
    "details.summary": ("Détails", "Details"),
    "debug.title": ("Informations techniques", "Technical details"),
    "debug.llm": ("Mise en forme : {value}", "Formatting: {value}"),
    "debug.stt": ("Transcription : {value}", "Transcription: {value}"),
    "debug.audio": ("Audio joint à la mise en forme", "Audio attached to formatting"),
    "debug.tokens": (
        "Jetons — entrée : {in_tokens} · sortie : {out_tokens}",
        "Tokens — input: {in_tokens} · output: {out_tokens}",
    ),
    "debug.duration": (
        "Durée de la mise en forme : {seconds} s",
        "Formatting duration: {seconds} s",
    ),
    "debug.compute": (
        "Pré-traitement déterministe : {deterministic} s · modèle (1er jeton) : "
        "{ttft} s · total : {total} s",
        "Deterministic pre-processing: {deterministic} s · model (first token): "
        "{ttft} s · total: {total} s",
    ),
    "debug.tokens_unavailable": (
        "Jetons : non disponibles après réouverture (valables seulement "
        "juste après la génération).",
        "Tokens: unavailable after reopening (only available right after "
        "generating).",
    ),
    "debug.truncated": (
        "Réponse tronquée (limite de longueur atteinte).",
        "Response truncated (length limit reached).",
    ),
    "details.hint": (
        "métadonnées, consigne ponctuelle, enregistrements",
        "metadata, one-off instruction, recordings",
    ),
    "meta.date": ("Date de la consultation", "Consultation date"),
    "meta.reason": ("Raison de consultation", "Reason for consultation"),
    "meta.requester": ("Demande de", "Requested by"),
    "meta.accompanied": ("Accompagné de", "Accompanied by"),
    "meta.reason_ph": ("Ex. Douleur thoracique", "e.g. Chest pain"),
    "meta.requester_ph": ("Dr X, clinique, urgence…", "Dr. X, clinic, emergency…"),
    "meta.accompanied_ph": ("Conjointe, fille…", "Spouse, daughter…"),
    "ctx.label": (
        "Consigne ponctuelle pour cette dictée (facultatif)",
        "One-off instruction for this dictation (optional)",
    ),
    "ctx.placeholder": (
        "Ex. insister sur le plan de suivi",
        "e.g. emphasize the follow-up plan",
    ),
    "recordings.title": ("Enregistrements", "Recordings"),
    "recordings.note": (
        "Supprimer le brouillon efface aussi ses enregistrements.",
        "Deleting the draft also erases its recordings.",
    ),
    "recordings.download": ("Télécharger", "Download"),
    "recordings.discard": ("Supprimer", "Delete"),
    "recordings.source_import": ("Fichier importé", "Imported file"),
    "recordings.source_dictation": ("Dictée", "Dictation"),
    "recordings.confirm_delete": (
        "Supprimer définitivement cet enregistrement audio ?",
        "Permanently delete this audio recording?",
    ),
    "recordings.deleted": ("Enregistrement supprimé.", "Recording deleted."),
    "recordings.load_failed": ("Enregistrements non chargés :", "Recordings not loaded:"),

    # --- Panneaux -----------------------------------------------------------
    "pane.dictation": ("Dictée", "Dictation"),
    "pane.note": ("Note structurée", "Structured note"),
    "pane.raw": ("Transcription brute", "Raw transcript"),
    "secondpass.toggle": ("Validation", "Validation"),
    "secondpass.tab": ("Validation", "Validation"),
    "secondpass.corrections": (
        "Corrections et éléments à valider",
        "Corrections and items to verify",
    ),
    "secondpass.title": (
        "Validation : après chaque note, comparer la note à la référence "
        "(l'audio si le fournisseur le reçoit, sinon la transcription) et "
        "signaler les écarts certains (omissions / éléments non dictés).",
        "Validation: after each note, compare it against the reference "
        "(the audio when the provider receives it, otherwise the transcript) "
        "and flag certain discrepancies (omissions / non-dictated items).",
    ),
    "secondpass.pending": (
        "Vérification en cours — comparaison de la note à la référence…",
        "Verification in progress — comparing the note against the reference…",
    ),
    "secondpass.empty": (
        "Rien à signaler : aucun écart certain entre la note et la référence.",
        "Nothing to report: no certain discrepancy between note and reference.",
    ),
    "secondpass.summary": ("Audit factuel de la note", "Factual audit of the note"),
    "secondpass.audit_title": (
        "Validation - 2e passe",
        "Validation - 2nd pass",
    ),
    "secondpass.omissions": (
        "Dans la source, mais absents de la note",
        "In the source, but missing from the note",
    ),
    "secondpass.inventions": (
        "Dans la note, mais absents de la source",
        "In the note, but absent from the source",
    ),
    "secondpass.confidence": ("Confiance de l'audit", "Audit confidence"),
    "secondpass.unavailable": (
        "Indisponible : le fournisseur actif ne transmet pas d'audio.",
        "Unavailable: the active provider does not send audio.",
    ),
    "secondpass.save_error": (
        "Préférence « Validation » non enregistrée.",
        "Could not save the \u201cValidation\u201d preference.",
    ),
    "medgrounding.title": (
        "Médicaments normalisés",
        "Normalized medications",
    ),
    "medgrounding.empty": (
        "En attente des médicaments …",
        "Waiting for medications …",
    ),
    "medgrounding.none": (
        "Aucun médicament détecté.",
        "No medication detected.",
    ),
    "medgrounding.status": (
        "médicaments : {count}",
        "medications: {count}",
    ),
    "medgrounding.confirm": (
        "à confirmer",
        "to confirm",
    ),
    "pane.clear": ("Supprimer", "Delete"),
    "pane.clear_title": ("Supprimer la consultation", "Delete the consultation"),
    "pane.generate": ("Mettre en forme", "Format"),
    "transcript.placeholder": (
        "Appuyez sur « Enregistrer » et dictez la consultation.\n\n"
        "Astuce : vous pouvez dicter en plusieurs fois, chaque nouvelle "
        "dictée s'ajoute à la suite.",
        "Press “Record” and dictate the consultation.\n\n"
        "Tip: you can dictate in several passes — each new dictation "
        "is appended.",
    ),
    # Affiché pendant l'enregistrement, le temps que la première tranche
    # revienne du service vocal : la zone est sinon vide et muable.
    "transcript.placeholder_recording": (
        "Dictée en cours — la transcription s'affiche ici au fil de la parole.",
        "Dictation in progress — the transcript appears here as you speak.",
    ),
    "note.preview": ("Aperçu", "Preview"),
    "note.write": ("Écrire", "Write"),
    "note.edit": ("Éditer", "Edit"),
    "note.copy": ("Copier", "Copy"),
    "note.empty": (
        "La note structurée apparaîtra ici après la mise en forme.",
        "The structured note will appear here after formatting.",
    ),
    "note.thinking": ("Raisonnement du modèle…", "Model reasoning…"),

    # --- Politique de confidentialité (FAQ) -------------------------------
    "privacy.title": ("Politique de confidentialité", "Privacy policy"),
    "privacy.footer_link": ("Politique de confidentialité", "Privacy policy"),
    "privacy.close": ("Fermer", "Close"),
    "privacy.q0_title": ("Est-ce un scribe IA ?", "Is this an AI scribe?"),
    "privacy.q0_body": (
        "Non. ConsultAI n'est pas un « scribe IA » au sens du Collège des "
        "médecins du Québec : l'application n'est pas faite pour "
        "l'enregistrement d'une conversation entre un médecin et ses patients. "
        "C'est un outil de dictée que le clinicien utilise après la "
        "consultation, seul — le dialogue avec le patient ne fait pas partie "
        "de l'usage prévu.",
        "No. ConsultAI is not an \"AI scribe\" within the meaning of the "
        "Collège des médecins du Québec: the application is not made to record "
        "a conversation between a physician and their patients. It is a "
        "dictation tool the clinician uses after the consultation, on their "
        "own — the dialogue with the patient is not part of its intended use.",
    ),
    "privacy.q1_title": ("Où sont traitées les données ?", "Where is the data processed?"),
    "privacy.q1_body": (
        "La reconnaissance vocale peut être effectuée au Québec, sur le serveur "
        "local : l'audio n'en sort alors jamais. Depuis 2026-08-16, la "
        "mise en forme de la note repose sur Google Gemini via Vertex AI, "
        "région northamerica-northeast1 (Montréal) : l'audio de la dictée "
        "est envoyé directement au modèle multimodal et les requêtes restent "
        "dans cette région. La transcription locale reste configurée en secours.",
        "Speech recognition may be performed in Québec, on the local server: "
        "the audio then never leaves it. Since 2026-08-16, note formatting "
        "relies on Google Gemini via Vertex AI, region "
        "northamerica-northeast1 (Montréal): dictation audio is sent "
        "directly to that multimodal model and requests stay in that region. "
        "Local transcription remains configured as a fallback.",
    ),
    "privacy.q2_title": ("Quelles données sont collectées ?", "What data is collected?"),
    "privacy.q2_body": (
        "L'application collecte l'enregistrement audio, la transcription, la "
        "note générée et des métadonnées non identifiantes (date, raison de "
        "consultation, demandeur, accompagnateur). Le nom et le numéro de "
        "dossier du patient ne sont ni collectés ni conservés.",
        "The application collects the audio recording, the transcript, the "
        "generated note and non-identifying metadata (date, reason for "
        "consultation, requester, companion). The patient's name and record "
        "number are neither collected nor kept.",
    ),
    "privacy.q3_title": ("Où sont-elles stockées ?", "Where are they stored?"),
    "privacy.q3_body": (
        "Les données sont stockées sur un serveur sécurisé hébergé au Québec, "
        "chiffré au repos. Aucun transit par un service tiers de diffusion "
        "(CDN).",
        "The data is stored on a secure server hosted in Québec, encrypted at "
        "rest. No transit through a third-party content delivery network "
        "(CDN).",
    ),
    "privacy.q4_title": (
        "Combien de temps sont-elles conservées ?",
        "How long are they kept?",
    ),
    "privacy.q4_body": (
        "La rétention est de 12 heures par défaut, réglable dans "
        "l'administration. Une dictée jamais conclue est supprimée selon la "
        "même rétention. La suppression d'un brouillon efface définitivement "
        "la transcription, la note et l'audio.",
        "Retention is 12 hours by default, adjustable in the administration. "
        "An abandoned dictation is removed under the same retention. Deleting "
        "a draft permanently erases the transcript, the note and the audio.",
    ),
    "privacy.q5_title": (
        "Les sauvegardes contiennent-elles des données cliniques ?",
        "Do backups contain clinical data?",
    ),
    "privacy.q5_body": (
        "Les archives de sauvegarde sont sanitisées : elles ne renferment ni "
        "audio ni données de patients (configuration, comptes et gabarits "
        "seulement).",
        "Backup archives are sanitized: they contain neither audio nor patient "
        "data (configuration, accounts and templates only).",
    ),
    "privacy.q6_title": ("Qui a accès aux données ?", "Who has access to the data?"),
"privacy.q6_body": (
        "L'accès est réservé aux cliniciens autorisés (connexion sécurisée, "
        "comptes nominatifs et groupes de permissions). Le fournisseur de "
        "traitement au Québec est lié par une entente de service couvrant les "
        "renseignements de santé. La supervision de sécurité n'analyse que des "
        "journaux techniques, jamais le contenu des consultations. La mise en "
        "forme passe par Google Gemini via Vertex AI, région "
        "northamerica-northeast1 (Montréal). Les informations ne sont jamais "
        "utilisées pour entraîner des modèles (l'addendum de politique cloud de "
        "Google pour les renseignements de santé est consenti).",
        "Access is reserved for authorized clinicians (secure sign-in, named "
        "accounts and permission groups). The Québec-based processing provider "
        "is bound by a service agreement covering health information. Security "
        "monitoring analyzes only technical logs, never consultation content. "
        "Formatting relies on Google Gemini via Vertex AI, region "
        "northamerica-northeast1 (Montréal). Information is never used to "
        "train models (Google's cloud policy addendum for health information "
        "is in force).",
    ),
    "privacy.q7_title": ("Quels cookies ?", "Which cookies?"),
    "privacy.q7_body": (
        "Un seul cookie de session, utilisé pour la connexion. Aucun cookie de "
        "tracking, aucune publicité ni statistique tierce. Le seul stockage "
        "local utilisé (mémoriser le dernier gabarit choisi) reste dans votre "
        "navigateur et n'est jamais transmis.",
        "A single session cookie, used for signing in. No tracking cookies, "
        "no advertising and no third-party analytics. The only local storage "
        "used (remembering the last chosen template) stays in your browser "
        "and is never transmitted.",
    ),
    "privacy.q8_title": ("Quelles garanties de sécurité ?", "What security guarantees?"),
    "privacy.q8_body": (
        "Connexions chiffrées, protection contre les accès malveillants, "
        "accès nominatifs et traçables, et conformité à la Loi 25 du Québec "
        "(évaluation des facteurs relatifs à la vie privée documentée). Tout "
        "incident de confidentialité présentant un risque est signalé à la "
        "Commission d'accès à l'information et aux personnes concernées, et "
        "fait l'objet d'un registre.",
        "Encrypted connections, protection against malicious access, named "
        "and traceable access, and compliance with Québec's Law 25 "
        "(documented privacy impact assessment). Any confidentiality incident "
        "posing a risk is reported to the Commission d'accès à l'information "
        "and to the individuals concerned, and is recorded in an incident "
        "register.",
    ),
    "privacy.q9_title": ("Quels sont vos droits ?", "What are your rights?"),
    "privacy.q9_body": (
        "Les résidents du Québec peuvent demander l'accès, la rectification ou "
        "la suppression de leurs renseignements personnels, ainsi que leur "
        "portabilité dans un format structuré, en s'adressant au responsable "
        "de la protection des renseignements personnels de l'application. Ils "
        "peuvent également obtenir des explications sur la façon dont la "
        "dictée est traitée et sur toute décision automatisée reposant sur ces "
        "renseignements.",
        "Québec residents may request access to, rectification of, or "
        "deletion of their personal information, as well as its portability "
        "in a structured format, by contacting the application's privacy "
        "officer. They may also obtain explanations about how dictation is "
        "processed and about any automated decision based on this information.",
    ),
    "note.engine_dictation": ("dictée {engine}", "dictation {engine}"),
    "note.engine_note": ("note {engine}", "note {engine}"),
    "note.engine_stt_title": ("Transcription : {engine}", "Transcription: {engine}"),
    "note.engine_llm_title": ("Mise en forme : {engine}", "Formatting: {engine}"),
    "note.engine_audio": ("+ audio", "+ audio"),
    "transcript.bypass_notice": (
        "Guide — la note se génère à partir de l'audio, texte en soutien",
        "Guide — the note is generated from the audio, text as support",
    ),
    "transcript.bypass_notice_title": (
        "Le STT continue de tourner pour l'affichage ; la note se génère à "
        "partir de l'audio, avec ce texte en guide pour ne rien omettre "
        "(l'audio reste la source autoritaire).",
        "Speech recognition keeps running for display; the note is generated "
        "from the audio, with this text as a guide so nothing is missed (the "
        "audio remains the authoritative source).",
    ),
    "note.engine_audio_title": (
        "Un extrait audio de la dictée a été joint à la transcription pour "
        "cette mise en forme.",
        "An audio excerpt of the dictation was attached to the transcript "
        "for this formatting pass.",
    ),

    # --- Copie et export ----------------------------------------------------
    "copy.rich": ("Mise en forme", "Formatted"),
    "copy.rich_short": ("Forme", "Rich"),
    "copy.rich_title": (
        "Copie mise en forme : titres et tableaux conservés. Pour Word, ou un "
        "DME qui accepte le HTML.",
        "Formatted copy: headings and tables preserved. For Word, or an EMR "
        "that accepts HTML.",
    ),
    "copy.plain": ("Texte simple", "Plain text"),
    "copy.plain_short": ("Texte", "Text"),
    "copy.plain_title": (
        "Copie en texte seul, sections soulignées et tableaux alignés en "
        "ASCII. Pour un DME qui n'accepte pas le HTML.",
        "Plain-text copy: underlined sections and ASCII-aligned tables. For "
        "an EMR that does not accept HTML.",
    ),
    "copy.markdown": ("Markdown", "Markdown"),
    "copy.markdown_title": (
        "Copier le Markdown brut, tel qu'il est dans l'éditeur",
        "Copy the raw Markdown, exactly as in the editor",
    ),
    "copy.pdf_title": ("Imprimer ou enregistrer en PDF", "Print or save as PDF"),
    "copy.nothing": ("Aucune note à copier.", "No note to copy."),
    "copy.rich_done": ("Note copiée avec sa mise en forme.", "Note copied with formatting."),
    "copy.plain_done": (
        "Texte simple copié — prêt pour le DME.",
        "Plain text copied — ready for the EMR.",
    ),
    "copy.markdown_done": ("Markdown copié.", "Markdown copied."),
    "copy.failed": ("Copie impossible : {error}", "Copy failed: {error}"),
    "pdf.nothing": ("Aucune note à imprimer.", "No note to print."),
    "pdf.footer": (
        "Document produit avec assistance à la dictée et relu par le clinicien.",
        "Document produced with dictation assistance and reviewed by the clinician.",
    ),
    "pdf.footer_printed": ("Imprimé le {date}.", "Printed on {date}."),

    # --- Dictée : état du téléversement ------------------------------------
    "dictation.streaming": (
        "Dictée envoyée au serveur au fur et à mesure.",
        "Dictation is being sent to the server as you speak.",
    ),
    "dictation.local_only": (
        "Enregistrement conservé dans le navigateur — le serveur est injoignable.",
        "Recording kept in the browser — the server is unreachable.",
    ),
    "dictation.retrying": (
        "Envoi interrompu — {count} fragment(s) (~{seconds} s) en attente, "
        "nouvelle tentative en cours. Rien n'est perdu.",
        "Upload interrupted — {count} chunk(s) (~{seconds} s) pending, "
        "retrying. Nothing is lost.",
    ),
    "dictation.sending": (
        "Envoi en cours — {count} fragment(s) en attente.",
        "Uploading — {count} chunk(s) pending.",
    ),
    "dictation.drain_failed": (
        "{count} fragment(s) n'ont pas pu être envoyés. La dictée reste "
        "conservée dans le navigateur : réessayez depuis la bannière.",
        "{count} chunk(s) could not be sent. The dictation is still kept in "
        "the browser: retry from the banner.",
    ),
    "dictation.too_short": (
        "Enregistrement trop court — rien à transcrire.",
        "Recording too short — nothing to transcribe.",
    ),
    "dictation.finishing": (
        "Transcription des dernières secondes…",
        "Transcribing the last few seconds…",
    ),
    "dictation.finished": (
        "Dictée terminée — {count} caractères transcrits.",
        "Dictation finished — {count} characters transcribed.",
    ),
    "dictation.no_speech": (
        "Aucune parole n'a été détectée dans cette dictée.",
        "No speech was detected in this dictation.",
    ),

    # --- Changement de gabarit et retranscription -------------------------
    # La dictée démarre souvent avant le choix du gabarit : le texte déjà
    # transcrit peut donc l'avoir été dans la mauvaise langue.
    "retranscribe.confirm": (
        "Ce gabarit est en {nouvelle}, alors que la dictée a été transcrite "
        "en {ancienne}.\n\nRenvoyer l'enregistrement au service vocal pour le "
        "retranscrire en {nouvelle} ?\n\nLa transcription actuelle sera "
        "REMPLACÉE. La note déjà mise en forme, elle, n'est pas touchée.",
        "This template is in {nouvelle}, but the dictation was transcribed in "
        "{ancienne}.\n\nSend the recording back to the speech service to "
        "re-transcribe it in {nouvelle}?\n\nThe current transcript will be "
        "REPLACED. The formatted note itself is left untouched.",
    ),
    "retranscribe.action": ("Retranscrire", "Re-transcribe"),
    "retranscribe.action_title": (
        "Renvoyer l'enregistrement au service vocal, dans la langue du gabarit "
        "choisi et avec le service configuré actuellement. Utile après un "
        "changement de service ou de gabarit.",
        "Send the recording back to the speech service, in the selected "
        "template's language and with the currently configured service. Useful "
        "after switching service or template.",
    ),
    # Déclenchement manuel : aucun écart de langue ne le motive, c'est un choix
    # délibéré — d'où un avertissement plus net sur ce qu'on perd.
    "retranscribe.confirm_manual": (
        "Renvoyer l'enregistrement au service vocal ?\n\nIl sera retranscrit "
        "en {langue}, avec le service vocal configuré actuellement.\n\nLa "
        "transcription actuelle sera REMPLACÉE, y compris les corrections que "
        "vous y auriez faites à la main. La note déjà mise en forme, elle, "
        "n'est pas touchée.",
        "Send the recording back to the speech service?\n\nIt will be "
        "re-transcribed in {langue}, using the currently configured speech "
        "service.\n\nThe current transcript will be REPLACED, including any "
        "manual corrections you made to it. The formatted note itself is left "
        "untouched.",
    ),
    "retranscribe.running": (
        "Retranscription en {langue}…",
        "Re-transcribing in {langue}…",
    ),
    "retranscribe.done": (
        "Retranscrit en {langue} — {count} caractères.",
        "Re-transcribed in {langue} — {count} characters.",
    ),
    # Certains enregistrements n'ont rien donné — un faux départ muet, par
    # exemple. Le texte est donc incomplet, et le taire serait pire que tout.
    "retranscribe.done_partial": (
        "Retranscrit en {langue} — {count} caractères. "
        "{used} enregistrement(s) sur {total} ont donné du texte ; "
        "les autres étaient muets ou illisibles.",
        "Re-transcribed in {langue} — {count} characters. "
        "{used} of {total} recordings produced text; "
        "the others were silent or unreadable.",
    ),
    "retranscribe.failed": (
        "Retranscription impossible : {error}",
        "Re-transcription failed: {error}",
    ),
    # Pendant la dictée, l'audio n'est pas encore attaché au brouillon : on ne
    # peut pas repartir de zéro, seulement corriger la suite.
    "retranscribe.during_dictation": (
        "Gabarit changé pour {nouvelle}. Les tranches à venir suivront cette "
        "langue ; celles déjà transcrites restent en {ancienne}. Vous pourrez "
        "tout retranscrire une fois la dictée terminée.",
        "Template switched to {nouvelle}. Upcoming slices will follow that "
        "language; those already transcribed stay in {ancienne}. You can "
        "re-transcribe everything once the dictation is finished.",
    ),
    "dictation.confirm_abort_transcribed": (
        "Arrêter sans envoyer ?\n\nL'enregistrement sera supprimé du serveur "
        "et du navigateur. Le texte déjà transcrit reste dans la "
        "transcription — videz-la si vous ne le voulez pas.",
        "Stop without sending?\n\nThe recording will be deleted from the "
        "server and the browser. Text already transcribed stays in the "
        "transcript — clear it if you do not want it.",
    ),
    "dictation.confirm_abort": (
        "Arrêter sans envoyer ?\n\nL'enregistrement sera supprimé, sans "
        "transcription.",
        "Stop without sending?\n\nThe recording will be deleted, with no "
        "transcription.",
    ),
    "dictation.aborted": (
        "Dictée arrêtée, enregistrement supprimé.",
        "Dictation stopped, recording deleted.",
    ),
    "dictation.cancel_failed": (
        "Annulation côté serveur impossible :",
        "Server-side cancellation failed:",
    ),
    "dictation.server_unreachable": (
        "Serveur injoignable : la dictée est enregistrée dans le navigateur "
        "et sera envoyée à la fin. ({error})",
        "Server unreachable: the dictation is stored in the browser and will "
        "be sent at the end. ({error})",
    ),
    "dictation.stt_unavailable": (
        "Service de reconnaissance vocale indisponible : le texte n'arrive "
        "plus. La dictée est enregistrée dans le navigateur.",
        "Speech recognition service unavailable: no more text is arriving. The "
        "dictation is stored in the browser.",
    ),

    # --- Dictée : micro et erreurs matérielles -----------------------------
    "mic.insecure": (
        "Le micro n'est pas accessible. Le navigateur ne l'autorise que sur une "
        "page servie en HTTPS : ouvrez l'application par son adresse publique, "
        "et non par l'adresse locale du serveur.",
        "The microphone is not available. Browsers only allow it on a page "
        "served over HTTPS: open the application through its public address, "
        "not the server's local address.",
    ),
    "mic.denied": (
        "Accès au micro refusé. Autorisez le microphone dans les réglages du "
        "navigateur.",
        "Microphone access denied. Allow the microphone in your browser "
        "settings.",
    ),
    "mic.unavailable": ("Micro indisponible : {error}", "Microphone unavailable: {error}"),
    "mic.ios_standalone": (
        " Sur iPhone/iPad, si le problème persiste dans l'application "
        "installée, ouvrez ConsultAI directement dans Safari pour dicter.",
        " On iPhone/iPad, if the problem persists in the installed app, open "
        "ConsultAI directly in Safari to dictate.",
    ),
    "mic.recorder_failed": (
        "Enregistrement impossible sur ce navigateur : {error}",
        "Recording is not possible in this browser: {error}",
    ),
    "mic.recorder_error": (
        "Erreur d'enregistrement : {error}",
        "Recording error: {error}",
    ),

    # --- Récupération d'une dictée interrompue -----------------------------
    "recovery.no_chunks": (
        "Aucun fragment audio conservé pour cette dictée.",
        "No audio chunk kept for this dictation.",
    ),
    "recovery.uploading": (
        "Envoi de la dictée conservée — {current}/{total}…",
        "Sending the stored dictation — {current}/{total}…",
    ),

    # --- Transcription et mise en forme ------------------------------------
    "transcribe.busy": (
        "Transcription en cours ({size} Mo)…",
        "Transcribing ({size} MB)…",
    ),
    "transcribe.busy_short": ("Transcription…", "Transcribing…"),
    "transcribe.done": (
        "Transcription terminée ({count} caractères, confiance {confidence} %).",
        "Transcription complete ({count} characters, {confidence} % confidence).",
    ),
    "transcript.characters": ("{count} caractères", "{count} characters"),
    "transcript.audio": ("{duration} d'audio", "{duration} of audio"),
    "generate.empty": (
        "La transcription est vide : dictez ou collez un texte d'abord.",
        "The transcript is empty: dictate or paste text first.",
    ),
    "generate.confirm_overwrite": (
        "La note affichée a été modifiée depuis la dernière mise en forme. "
        "Régénérer remplace ces modifications par la nouvelle note — "
        "continuer ?",
        "The displayed note has been edited since the last formatting pass. "
        "Regenerating replaces those edits with the new note — continue?",
    ),
    "generate.no_template": (
        "Sélectionnez un gabarit de consultation.",
        "Select a consultation template.",
    ),
    "generate.preparing": (
        "Préparation…",
        "Preparing…",
    ),
    "generate.sending": (
        "Envoi au modèle…",
        "Sending to model…",
    ),
    "generate.processing": (
        "Traitement en cours…",
        "Processing…",
    ),
    "generate.streaming": (
        "La note se génère…",
        "Generating the note…",
    ),
    "generate.validating": (
        "Validation en cours…",
        "Validating…",
    ),
    "generate.truncated": (
        "La note a été tronquée : le modèle a atteint sa limite de longueur. "
        "Augmentez GEMINI_MAX_OUTPUT_TOKENS ou dictez en deux parties.",
        "The note was truncated: the model reached its length limit. Increase "
        "GEMINI_MAX_OUTPUT_TOKENS or dictate in two parts.",
    ),
    "generate.done": (
        "Note générée avec {model}. Relisez-la avant utilisation.",
        "Note generated with {model}. Review it before use.",
    ),

    # --- Sauvegarde ---------------------------------------------------------
    "save.saving": ("Sauvegarde…", "Saving…"),
    "save.saved_at": ("Enregistré à {time}", "Saved at {time}"),
    "save.failed": ("Échec de la sauvegarde", "Save failed"),
    "save.loaded_at": ("Chargé — {date}", "Loaded — {date}"),

    # --- Éditeur Markdown ---------------------------------------------------
    "markdown.hint": (
        "Markdown — # titre, ## sous-titre, **gras**, - liste, | tableau |",
        "Markdown — # heading, ## subheading, **bold**, - list, | table |",
    ),
    "markdown.nothing_to_preview": (
        "Rien à prévisualiser pour le moment.",
        "Nothing to preview yet.",
    ),

    # --- Gabarits -----------------------------------------------------------
    "tpl.modal_title": ("Gabarits", "Templates"),
    "tpl.new": ("Nouveau", "New"),
    "tpl.back": ("Retour à la liste", "Back to the list"),
    "tpl.list_hint": (
        "Touchez un gabarit pour le modifier.",
        "Tap a template to edit it.",
    ),
    "tpl.name": ("Nom du gabarit", "Template name"),
    "tpl.name_ph": ("Ex. Consultation externe", "e.g. Outpatient consultation"),
    "tpl.order": ("Ordre", "Order"),
    "tpl.language": ("Langue", "Language"),
    "tpl.language_help": (
        "Décide de toute la chaîne : consignes, service vocal, langue de la note.",
        "Drives the whole chain: instructions, speech service, note language.",
    ),
    "tpl.locked_title": (
        "Ce gabarit est protégé.",
        "This template is protected.",
    ),
    "tpl.locked_help": (
        "Il ne peut être ni modifié ni supprimé : c'est un point de départ "
        "garanti de l'installation. Dupliquez-le pour en obtenir une copie "
        "entièrement modifiable, indépendante de l'original.",
        "It cannot be edited or deleted: it is a guaranteed starting point for "
        "this installation. Duplicate it to obtain a fully editable copy, "
        "independent of the original.",
    ),
    "tpl.duplicate_to_edit": (
        "Dupliquer pour personnaliser",
        "Duplicate to customize",
    ),
    "tpl.locked_badge": ("protégé", "protected"),
    "tpl.shared_badge": ("Partagé", "Shared"),
    "tpl.personal_badge": ("Personnel", "Personal"),
    "tpl.readonly_title": (
        "Gabarit partagé — lecture seule",
        "Shared template — read-only",
    ),
    "tpl.readonly_help": (
        "Ce gabarit appartient à l'équipe : seuls les administrateurs peuvent "
        "le réécrire. Dupliquez-le pour obtenir une copie personnelle que vous "
        "pouvez modifier à votre façon.",
        "This template belongs to the team: only administrators can edit it. "
        "Duplicate it to get a personal copy you can adjust to your own style.",
    ),
    "tpl.description": ("Description", "Description"),
    "tpl.description_ph": (
        "Affichée sous le menu de sélection",
        "Shown below the selection menu",
    ),
    "tpl.instructions": ("Instructions cliniques", "Clinical instructions"),
    "tpl.instructions_help": (
        "Ce sur quoi le modèle doit se concentrer : éléments à rechercher, "
        "distinctions à faire, éléments à ne jamais omettre.",
        "What the model should focus on: what to look for, distinctions to "
        "draw, things never to omit.",
    ),
    "tpl.layout": ("Mise en page (squelette Markdown)", "Layout (Markdown skeleton)"),
    "tpl.layout_help": (
        "Les titres <code class=\"bg-slate-100 px-1 rounded\">#</code> et "
        "<code class=\"bg-slate-100 px-1 rounded\">##</code> définissent la "
        "structure exacte du document.",
        "The <code class=\"bg-slate-100 px-1 rounded\">#</code> and "
        "<code class=\"bg-slate-100 px-1 rounded\">##</code> headings define "
        "the exact structure of the document.",
    ),
    # Les noms de champs ne sont PAS traduits : les gabarits déjà écrits s'en
    # servent, les renommer les casserait silencieusement.
    "tpl.layout_fields": ("Champs disponibles :", "Available fields:"),
    "tpl.layout_fields_note": (
        "Une ligne dont le champ reste inconnu est simplement retirée du document.",
        "A line whose field remains unknown is simply removed from the document.",
    ),
    "tpl.hints": ("Vocabulaire additionnel", "Additional vocabulary"),
    "tpl.hints_help_fr": (
        "Termes séparés par des virgules, ajoutés au lexique clinique "
        "francophone déjà intégré.",
        "Comma-separated terms, added to the built-in French clinical lexicon.",
    ),
    "tpl.hints_help_en": (
        "Termes séparés par des virgules. Le lexique intégré étant "
        "francophone, il n'est pas envoyé en mode anglais : ce champ est le "
        "seul vocabulaire transmis au moteur.",
        "Comma-separated terms. The built-in lexicon is French, so it is not "
        "sent in English mode: this field is the only vocabulary passed to "
        "the engine.",
    ),
    "tpl.save": ("Enregistrer", "Save"),
    "tpl.duplicate": ("Dupliquer", "Duplicate"),
    "tpl.duplicate_title": (
        "Créer une copie modifiable de ce gabarit",
        "Create an editable copy of this template",
    ),
    "tpl.delete": ("Supprimer", "Delete"),
    "tpl.no_description": ("Sans description", "No description"),
    "tpl.preloaded": ("Préchargé", "Preloaded"),
    "tpl.none": ("Aucun gabarit", "No template"),
    "tpl.none_option": ("Aucun gabarit — créez-en un", "No template — create one"),
    "tpl.required_fields": (
        "Le nom, les instructions et la mise en page sont obligatoires.",
        "Name, instructions and layout are required.",
    ),
    "tpl.saving": ("Enregistrement…", "Saving…"),
    "tpl.updated": ("Gabarit mis à jour.", "Template updated."),
    "tpl.created": ("Gabarit créé.", "Template created."),
    "tpl.open_first": (
        "Ouvrez d'abord le gabarit à dupliquer.",
        "Open the template to duplicate first.",
    ),
    "tpl.duplicating": ("Duplication…", "Duplicating…"),
    "tpl.duplicated": ("Copie créée : « {name} ».", "Copy created: “{name}”."),
    "tpl.confirm_delete": (
        "Supprimer définitivement « {name} » ?",
        "Permanently delete “{name}”?",
    ),
    "tpl.this_one": ("ce gabarit", "this template"),
    "tpl.deleted": ("Gabarit supprimé.", "Template deleted."),
    "tpl.default_layout": (
        "# TITRE DU DOCUMENT\n\n## MOTIF DE CONSULTATION\n\n"
        "## HISTOIRE DE LA MALADIE ACTUELLE\n\n## EXAMEN PHYSIQUE\n\n"
        "## IMPRESSION\n\n## PLAN\n",
        "# DOCUMENT TITLE\n\n## REASON FOR CONSULTATION\n\n"
        "## HISTORY OF PRESENT ILLNESS\n\n## PHYSICAL EXAMINATION\n\n"
        "## IMPRESSION\n\n## PLAN\n",
    ),

    # --- Brouillons ---------------------------------------------------------
    "drafts.title": ("Mes brouillons", "My drafts"),
    "drafts.retention_notice": (
        "Les dossiers de plus de {hours} heure(s) sans modification sont supprimés automatiquement.",
        "Records unmodified for more than {hours} hour(s) are deleted automatically.",
    ),
    "drafts.loading": ("Chargement…", "Loading…"),
    "drafts.none": ("Aucun brouillon enregistré.", "No saved draft."),
    "drafts.unnamed_patient": ("Patient non identifié", "Unidentified patient"),
    "drafts.no_reason": (
        "Raison de consultation non précisée",
        "Reason for consultation not specified",
    ),
    # Statut affiché dans la liste des brouillons. Les valeurs en base sont
    # sans accent (« genere », « finalise ») : on affiche le libellé, jamais la
    # valeur brute.
    "status.brouillon": ("Brouillon", "Draft"),
    "status.transcrit": ("Transcrit", "Transcribed"),
    "status.genere": ("Générée", "Generated"),
    "status.finalise": ("Finalisée", "Finalized"),
    "status.error": ("Erreur", "Error"),
    # Dictée abandonnée par un onglet mort : le brouillon garde son audio,
    # mais la transcription n'a pas été conclue — signalé en rouge pâle.
    "status.abandonnee": ("Abandonnée", "Abandoned"),
    "drafts.confirm_delete": (
        "Supprimer définitivement ce brouillon ?",
        "Permanently delete this draft?",
    ),
    "drafts.deleted": ("Brouillon supprimé.", "Draft deleted."),
    "drafts.busy_open": (
        "Terminez ou arrêtez la dictée en cours avant d'ouvrir un brouillon.",
        "Finish or stop the current dictation before opening a draft.",
    ),
    "drafts.loaded": ("Brouillon « {title} » chargé.", "Draft “{title}” loaded."),
    "drafts.busy_new": (
        "Terminez ou arrêtez la dictée en cours avant de changer de consultation.",
        "Finish or stop the current dictation before switching consultation.",
    ),
    "drafts.confirm_new": (
        "Commencer une nouvelle consultation ? Le brouillon courant reste "
        "accessible dans « Mes brouillons ».",
        "Start a new consultation? The current draft stays available under "
        "“My drafts”.",
    ),
    "drafts.delete": ("Supprimer", "Delete"),
    "drafts.unknown_date": ("Date inconnue", "Unknown date"),
    "drafts.today": ("Aujourd'hui", "Today"),
    "drafts.yesterday": ("Hier", "Yesterday"),
    "drafts.default_title": ("Consultation", "Consultation"),
    # Toast montré à chaque chargement tant qu'un brouillon « abandonnée »
    # existe, et à la détection en direct d'une dictée interrompue.
    "drafts.abandoned_toast": (
        "Une dictée abandonnée a laissé un brouillon — voyez la liste des brouillons.",
        "An abandoned dictation left a draft — see the drafts list.",
    ),
    "drafts.view": ("Voir", "View"),

    # --- Synchronisation en direct (autre onglet, autre appareil) ----------
    "sync.reload": ("Recharger", "Reload"),
    "sync.conflict_transcript": (
        "Une nouvelle dictée est arrivée d'un autre appareil, mais cet onglet "
        "a des modifications non enregistrées. Recharger affichera la version "
        "la plus récente et perdra ces modifications.",
        "New dictation arrived from another device, but this tab has unsaved "
        "changes. Reloading will show the latest version and lose those "
        "changes.",
    ),
    "sync.conflict_generic": (
        "Cette consultation a été modifiée depuis un autre appareil, mais cet "
        "onglet a des modifications non enregistrées. Recharger affichera la "
        "version la plus récente et perdra ces modifications.",
        "This consultation was changed from another device, but this tab has "
        "unsaved changes. Reloading will show the latest version and lose "
        "those changes.",
    ),
    "sync.consultation_deleted": (
        "Cette consultation a été supprimée depuis un autre appareil.",
        "This consultation was deleted from another device.",
    ),
    "sync.dictation_started": (
        "Dictée commencée sur « {title} » depuis un autre appareil.",
        "Dictation started on “{title}” from another device.",
    ),
    "sync.follow": ("Suivre", "Follow"),

    # --- Panneau d'administration : chrome ---------------------------------
    "admin.title": ("Réglages", "Settings"),
    "admin.badge": ("Administrateur", "Administrator"),
    "admin.loading": ("Chargement…", "Loading…"),

    # --- Une phrase par onglet, affichée au-dessus de son contenu ------------
    # L'ancien bandeau expliquait la surcharge du .env en haut de TOUS les
    # onglets, y compris ceux où elle ne s'applique pas. Chaque onglet dit
    # maintenant ce qu'il fait, et seuls ceux qui portent des réglages
    # mentionnent le .env. Les onglets suivent les flux de travail :
    # Dictée (la voix devient texte), Note (le texte devient note), Comptes
    # et accès (qui entre), Données et sauvegarde (ce qu'on garde), Statistiques.
    "admin.intro.group.dictation": (
        "Comment la voix devient texte : le service de transcription sous son "
        "propre sous-onglet, puis ce qui s'applique à tous — retrait des "
        "pauses et temps réel.",
        "How voice becomes text: the transcription service under its own "
        "sub-tab, then what applies to all — pause trimming and realtime.",
    ),
    "admin.intro.group.note": (
        "Comment la transcription devient note : le modèle de langage sous son "
        "propre sous-onglet, la consigne générale qui complète les gabarits, "
        "et l'affichage du raisonnement.",
        "How the transcript becomes a note: the language model under its own "
        "sub-tab, the general instruction complementing templates, and the "
        "reasoning display.",
    ),
    "admin.intro.group.access": (
        "Qui peut entrer, avec quels droits, et quels attributs du fournisseur "
        "d'identité sont lus pour le nom et l'avatar.",
        "Who may sign in, with which rights, and which identity-provider "
        "attributes are read for the name and avatar.",
    ),
    "admin.intro.group.data": (
        "Combien de temps les consultations restent sur le serveur, combien de "
        "sauvegardes sont conservées, et comment restaurer.",
        "How long consultations stay on the server, how many backups are "
        "kept, and how to restore.",
    ),
    "admin.intro.group.stats": (
        "Jetons et minutes d'audio consommés par usager, par modèle et par "
        "période, avec le coût estimé.",
        "Tokens and audio minutes consumed per user, model and time period, "
        "with the estimated cost.",
    ),
    "admin.env_note": (
        "Ces réglages sont enregistrés en base et surchargent le fichier "
        "<code>.env</code> : effet immédiat, sans reconstruction. Vider un champ "
        "le remet à la valeur du <code>.env</code>.",
        "These settings are stored in the database and override the "
        "<code>.env</code> file: effective immediately, with no rebuild. "
        "Clearing a field resets it to the <code>.env</code> value.",
    ),
    "admin.advanced": ("Avancé", "Advanced"),
    "admin.search.placeholder": ("Rechercher un réglage…", "Search a setting…"),
    "admin.search.none": ("Aucun réglage ne correspond.", "No matching setting."),

    # --- Panneau : comptes et groupes ---------------------------------------
    "people.users_title": ("Comptes", "Accounts"),
    "people.groups_title": ("Groupes", "Groups"),
    "people.you": ("vous", "you"),
    "people.never_signed_in": ("jamais connecté", "never signed in"),
    "people.last_login": ("dernière connexion {date}", "last sign-in {date}"),
    "people.consultations": (
        "{count} consultation(s)", "{count} consultation(s)",
    ),
    "people.active": ("Actif", "Active"),
    "people.disabled": ("Désactivé", "Disabled"),
    "people.deactivate": ("Désactiver", "Deactivate"),
    "people.reactivate": ("Réactiver", "Reactivate"),
    "people.delete_user": ("Supprimer", "Delete"),
    "people.user_deleted": ("Compte et toutes ses données supprimés.", "Account and all its data deleted."),
    "people.confirm_delete_user": (
        "Supprimer définitivement « {name} » ? Toutes ses consultations, "
        "transcriptions, notes, enregistrements audio et son historique "
        "d'usage seront effacés. Action irréversible.",
        "Permanently delete “{name}”? All their consultations, transcripts, "
        "notes, audio recordings and usage history will be erased. This "
        "cannot be undone.",
    ),
    "people.saved": ("Compte mis à jour.", "Account updated."),
    "people.no_users": (
        "Aucun compte. Le premier usager qui se connectera deviendra "
        "administrateur.",
        "No accounts yet. The first user to sign in will become an "
        "administrator.",
    ),
    "people.disabled_warning": (
        "Un compte désactivé conserve ses consultations mais ne peut plus se "
        "connecter.",
        "A disabled account keeps its consultations but can no longer sign in.",
    ),
    "people.perm_admin": ("Administration", "Administration"),
    "people.perm_templates": ("Gabarits", "Templates"),
    "people.members": ("{count} membre(s)", "{count} member(s)"),
    "people.new_group": ("Nouveau groupe", "New group"),
    "people.group_name_ph": ("nom-du-groupe", "group-name"),
    "people.group_desc_ph": ("À quoi sert ce groupe", "What this group is for"),
    "people.create": ("Créer", "Create"),
    "people.delete_group": ("Supprimer", "Delete"),
    "people.group_created": ("Groupe créé.", "Group created."),
    "people.group_saved": ("Groupe mis à jour.", "Group updated."),
    "people.group_deleted": ("Groupe supprimé.", "Group deleted."),
    "people.confirm_delete_group": (
        "Supprimer le groupe « {name} » ? Ses membres le perdent, leurs "
        "consultations ne sont pas touchées.",
        "Delete the group “{name}”? Its members lose it; their consultations "
        "are untouched.",
    ),
    "people.provider_groups_note": (
        "Les groupes annoncés par le fournisseur d'identité sont ajoutés "
        "automatiquement s'ils portent le même nom. Ils ne sont jamais retirés "
        "automatiquement : le retrait se fait ici.",
        "Groups announced by the identity provider are added automatically "
        "when the names match. They are never removed automatically: removal "
        "is done here.",
    ),
    "admin.save": ("Enregistrer", "Save"),
    "admin.list_models": ("Modèles disponibles", "Available models"),
    "admin.provider_active": ("service actif", "active service"),
    "admin.provider_use": ("Utiliser ce service", "Use this service"),
    "admin.provider_staged": (
        "Sera activé à l'enregistrement.",
        "Will be activated on save.",
    ),
    "admin.provider_env_only": (
        "Ce service n'a aucun réglage dans ce panneau : il se configure dans le "
        "fichier <code>.env</code> et, pour Google, par le compte de service "
        "monté dans le conteneur.",
        "This service has no setting in this panel: it is configured in the "
        "<code>.env</code> file and, for Google, through the service account "
        "mounted in the container.",
    ),
    "admin.provider_no_key": (
        "Aucune clé enregistrée pour ce service : il refusera les requêtes tant "
        "qu'elle n'est pas renseignée.",
        "No key stored for this service: it will refuse requests until one is "
        "provided.",
    ),
    "admin.from_panel": ("panneau", "panel"),
    "admin.from_env": (".env", ".env"),
    "admin.secret_configured": (
        "Clé en place ({hint}) — laisser vide pour la conserver",
        "Key in place ({hint}) — leave empty to keep it",
    ),
    "admin.secret_missing": ("Aucune clé enregistrée", "No key stored"),
    "admin.secret_clear": ("Effacer", "Clear"),
    "admin.secret_clear_title": ("Effacer la clé enregistrée", "Erase the stored key"),
    "admin.secret_will_clear": (
        "Sera effacée à l'enregistrement",
        "Will be erased on save",
    ),
    "admin.unsaved": ("Modifications non enregistrées", "Unsaved changes"),
    "admin.nothing_to_save": ("Rien à enregistrer.", "Nothing to save."),
    "admin.saving": ("Enregistrement…", "Saving…"),
    "admin.saved_count": (
        "{count} réglage(s) enregistré(s).",
        "{count} setting(s) saved.",
    ),
    "admin.applied": (
        "Réglages appliqués — ils prennent effet immédiatement.",
        "Settings applied — they take effect immediately.",
    ),
    "admin.querying": ("Interrogation du fournisseur…", "Querying the provider…"),
    "admin.models_listed": (
        "{count} modèle(s) — proposés dans le champ « Modèle ».",
        "{count} model(s) — offered in the “Model” field.",
    ),
    "admin.fast_model_missing": (
        "Le modèle rapide « {model} » ne figure pas dans les modèles "
        "accessibles à cette clé. La relecture des métadonnées échouera.",
        "The fast model “{model}” is not among the models available to this "
        "key. Metadata extraction will fail.",
    ),
    "admin.model_missing": (
        "Attention : « {model} » ne figure pas dans les modèles accessibles à "
        "cette clé. La mise en forme échouera.",
        "Warning: “{model}” is not among the models available to this key. "
        "Formatting will fail.",
    ),
    "admin.stt_model_missing": (
        "Attention : « {model} » ne figure pas dans les modèles accessibles à "
        "cette clé. La transcription échouera.",
        "Warning: “{model}” is not among the models available to this key. "
        "Transcription will fail.",
    ),
    "admin.stt_models_unsupported": (
        "Ce fournisseur n'expose pas de liste de modèles : saisissez le nom "
        "du modèle à la main.",
        "This provider does not expose a model list: type the model name "
        "manually.",
    ),

    # --- Panneau d'administration : onglet Sauvegarde -----------------------
    "admin.backup.loading": ("Chargement…", "Loading…"),
    "admin.backup.empty": ("Aucune sauvegarde pour l'instant.", "No backup yet."),
    "admin.backup.now": ("Sauvegarder maintenant", "Backup now"),
    "admin.backup.creating": ("Sauvegarde en cours…", "Backing up…"),
    "admin.backup.download": ("Télécharger", "Download"),
    "admin.backup.delete": ("Supprimer", "Delete"),
    "admin.backup.delete_confirm": (
        "Supprimer définitivement cette sauvegarde ?",
        "Permanently delete this backup?",
    ),
    "admin.backup.restore": ("Restaurer", "Restore"),
    "admin.backup.restore_confirm": (
        "Restaurer cette sauvegarde va REMPLACER toutes les consultations et "
        "tous les enregistrements audio actuels par leur contenu. Une "
        "sauvegarde de l'état actuel sera prise avant, mais cette action reste "
        "lourde de conséquences. Continuer ?",
        "Restoring this backup will REPLACE all current consultations and "
        "audio recordings with its content. A safety snapshot of the current "
        "state will be taken first, but this remains a serious action. "
        "Continue?",
    ),
    "admin.backup.upload_restore": (
        "Restaurer depuis un fichier…",
        "Restore from a file…",
    ),
    "admin.backup.kind.scheduled": ("automatique", "automatic"),
    "admin.backup.kind.manual": ("manuelle", "manual"),
    "admin.backup.kind.pre_restore": ("sécurité pré-restauration", "pre-restore safety"),
    "admin.backup.last_run": (
        "Dernière sauvegarde automatique : {at}",
        "Last automatic backup: {at}",
    ),
    "admin.backup.last_run_never": (
        "Aucune sauvegarde automatique n'a encore tourné.",
        "The automatic backup hasn't run yet.",
    ),
    "admin.backup.last_run_error": (
        "La dernière sauvegarde automatique a échoué : {error}",
        "The last automatic backup failed: {error}",
    ),
    "admin.backup.restart_required": (
        "Restauration terminée. Redémarrez le conteneur ConsultAI maintenant "
        "pour continuer — jusque-là, toute écriture est bloquée.",
        "Restore complete. Restart the ConsultAI container now to continue — "
        "until then, all writes are blocked.",
    ),
    "admin.backup.retention_help_count": (
        "{count} sauvegarde(s) conservée(s) actuellement.",
        "{count} backup(s) currently kept.",
    ),

    # --- Panneau d'administration : onglet Statistiques ---------------------
    "admin.stats.loading": ("Chargement…", "Loading…"),
    "admin.stats.empty": ("Aucune donnée sur cette période.", "No data for this period."),
    "admin.stats.overview_title": (
        "Notes et coût par usager",
        "Notes and cost per user",
    ),
    "admin.stats.notes_short": ("notes", "notes"),
    "admin.stats.date_from": ("Du", "From"),
    "admin.stats.date_to": ("Au", "To"),
    "admin.stats.owner_all": ("Tous les usagers", "All users"),
    "admin.stats.total_cost": ("Coût total estimé", "Estimated total cost"),
    "admin.stats.log_title": ("Journal des générations", "Generation log"),
    "admin.stats.log_prev": ("Précédent", "Previous"),
    "admin.stats.log_next": ("Suivant", "Next"),
    "admin.stats.log_page": (
        "Page {page} / {pages} · {total} entrée(s)",
        "Page {page} / {pages} · {total} entries",
    ),
    "admin.stats.log_retention_note": (
        "Le détail par génération ne couvre que les 45 derniers jours ; au-delà, seuls les totaux quotidiens subsistent.",
        "Per-generation detail only covers the last 45 days; beyond that, only daily totals remain.",
    ),
    "admin.stats.breakdown_title": (
        "Détail par fournisseur / modèle",
        "Breakdown by provider / model",
    ),
    "admin.stats.col_date": ("Date", "Date"),
    "admin.stats.col_owner": ("Usager", "User"),
    "admin.stats.col_provider": ("Fournisseur", "Provider"),
    "admin.stats.col_model": ("Modèle", "Model"),
    "admin.stats.col_kind": ("Type", "Kind"),
    "admin.stats.col_consultation": ("Consultation", "Consultation"),
    "admin.stats.col_usage": ("Usage", "Usage"),
    "admin.stats.col_events": ("Générations", "Generations"),
    "admin.stats.segments": ("segments", "segments"),
    "admin.stats.col_tokens": ("Jetons (texte+♪audio/sortie)", "Tokens (text+♪audio/output)"),
    "admin.stats.cached": ("en cache", "cached"),
    "admin.stats.col_audio": ("Audio", "Audio"),
    "admin.stats.col_cost": ("Coût", "Cost"),
    "admin.stats.col_compute": ("Pré-traitement · 1er jeton", "Pre-processing · first token"),
    "admin.stats.kind.llm": ("Modèle de langage", "Language model"),
    "admin.stats.kind.stt": ("Reconnaissance vocale", "Speech recognition"),
    "admin.stats.pricing_title": ("Tarifs", "Rates"),
    "admin.stats.pricing_all": ("Tous", "All"),
    "admin.stats.pricing_add": ("Ajouter un tarif", "Add a rate"),
    "admin.stats.pricing_provider": ("Fournisseur", "Provider"),
    "admin.stats.pricing_model": ("Modèle (vide = défaut du fournisseur)", "Model (empty = provider default)"),
    "admin.stats.pricing_unit": ("Unité", "Unit"),
    "admin.stats.pricing_rate": ("Tarif ($)", "Rate ($)"),
    "admin.stats.pricing_delete_confirm": (
        "Supprimer ce tarif ?",
        "Delete this rate?",
    ),
    "admin.stats.unit.token_input_1m": ("$ / 1M jetons entrée", "$ / 1M input tokens"),
    "admin.stats.unit.token_output_1m": ("$ / 1M jetons sortie", "$ / 1M output tokens"),
    "admin.stats.unit.token_audio_input_1m": (
        "$ / 1M jetons audio entrée",
        "$ / 1M input audio tokens",
    ),
    "admin.stats.unit.audio_minute": ("$ / minute d'audio", "$ / audio minute"),

    # --- Menu identité : usage personnel -------------------------------------
    "identity.usage.month": ("Mon usage — {month}", "My usage — {month}"),
    "identity.usage.loading": ("Chargement…", "Loading…"),
    "identity.usage.empty": ("Aucune activité.", "No activity."),
    "identity.usage.tokens": ("{count} jetons", "{count} tokens"),
    "identity.usage.audio_minutes": ("{count} min d'audio", "{count} audio min"),
    "identity.usage.cost": ("≈ {amount} $ estimé", "≈ US${amount} estimated"),

    # --- Panneau d'administration : groupes de réglages --------------------
    # Onglets organisés par flux de travail — voir runtime_config.GROUPS.
    "group.interface": ("Interface", "Interface"),
    "group.dictation": ("Dictée", "Dictation"),
    "group.note": ("Note", "Note"),
    "group.access": ("Comptes et accès", "Accounts and access"),
    "group.data": ("Données et sauvegarde", "Data and backups"),
    "group.stats": ("Statistiques", "Statistics"),

    # --- Sous-titres internes à un onglet (Setting.section) ------------------
    "sect.silence": ("Retrait des pauses", "Pause trimming"),
    "sect.realtime": ("Temps réel", "Realtime"),
    "sect.prompts": ("Consigne générale", "General instruction"),
    "sect.med_grounding": ("Correction des médicaments", "Medication correction"),

    # --- Textes partagés entre fournisseurs de modèle de langage -------------
    # Les fabriques _cap_* de runtime_config.py instancient ces capacités pour
    # chaque fournisseur : valeurs propres à chacun en base, textes écrits une
    # seule fois ici. Un fournisseur qui s'écarte du commun garde son propre
    # set.<clé> (point de terminaison personnalisé, notamment).
    "set.cap.model.label": ("Modèle", "Model"),
    "set.cap.model.help": (
        "Le bouton « Modèles disponibles » interroge le fournisseur avec la "
        "clé configurée et affiche ce à quoi ce compte a réellement droit.",
        "The “Available models” button queries the provider with the "
        "configured key and shows what this account actually has access to.",
    ),
    "set.cap.model_fast.label": (
        "Modèle rapide (métadonnées)",
        "Fast model (metadata)",
    ),
    "set.cap.model_fast.help": (
        "Utilisé pour la seule relecture des métadonnées, une tâche triviale "
        "payée au jeton. Laisser vide pour employer le modèle principal.",
        "Used only to re-read metadata, a trivial task paid by the token. "
        "Leave empty to use the main model.",
    ),
    "set.cap.verify_model.label": (
        "Modèle de 2e passe (Validation)",
        "2nd pass model (Validation)",
    ),
    "set.cap.verify_model.help": (
        "Modèle qui audite la note contre l'audio (ou la transcription) dans "
        "le panneau « Validation » après génération. Laisser vide pour "
        "employer le même modèle que la mise en forme.",
        "Model that audits the note against the audio (or the transcript) in "
        "the “Validation” panel after generation. Leave empty to use the same "
        "model as the formatting.",
    ),
    "set.cap.temperature.label": ("Température", "Temperature"),
    "set.cap.temperature.help": (
        "0 = déterministe. Au-delà de 0,4 le modèle commence à broder, ce qui "
        "n'a pas sa place dans une note clinique. Les modèles les plus "
        "récents ne l'acceptent plus : le réglage est alors ignoré, la note "
        "est produite quand même.",
        "0 = deterministic. Above 0.4 the model starts embellishing, which "
        "has no place in a clinical note. The most recent models no longer "
        "accept it: the setting is then ignored and the note is produced "
        "anyway.",
    ),
    "set.cap.send_audio.label": (
        "Joindre aussi l'audio (silences plafonnés)",
        "Also attach audio (pauses capped)",
    ),
    "set.cap.send_audio.help": (
        "Envoie l'extrait audio en plus de la transcription : le modèle peut "
        "trancher un terme mal reconnu (nom propre, terme médical) en "
        "l'écoutant. Ajoute un coût et quelques secondes par note.",
        "Sends the audio clip alongside the transcript: the model can "
        "resolve a poorly recognized term (proper noun, medical term) by "
        "listening to it. Adds cost and a few seconds per note.",
    ),
    "set.cap.send_audio_max_minutes.label": (
        "Durée maximale envoyée (minutes)",
        "Maximum duration sent (minutes)",
    ),
    "set.cap.send_audio_max_minutes.help": (
        "Au-delà de cette durée d'audio (après retrait des silences), rien "
        "n'est joint — la note se génère comme avant, sur la seule "
        "transcription. Protège la latence et le coût sur une très longue "
        "dictée.",
        "Beyond this much audio (after silence trimming), nothing is "
        "attached — the note is generated as before, from the transcript "
        "alone. Protects latency and cost on a very long dictation.",
    ),
    "set.cap.bypass_stt.label": (
        "Ignorer la reconnaissance vocale (audio direct)",
        "Skip speech recognition (direct audio)",
    ),
    "set.cap.bypass_stt.help": (
        "L'audio part directement au modèle, sans passer par le service de "
        "reconnaissance vocale — économise son coût et sa latence. La note "
        "peut alors se générer sans transcription, dès qu'un enregistrement "
        "existe.",
        "Audio goes straight to the model, without passing through speech "
        "recognition — saves its cost and latency. The note can then be "
        "generated without a transcript, as soon as a recording exists.",
    ),
    "set.cap.keep_transcript.label": (
        "Conserver une transcription pendant l'enregistrement",
        "Keep a transcript during recording",
    ),
    "set.cap.keep_transcript.help": (
        "Sans effet si l'option ci-dessus est désactivée. Activée : la "
        "reconnaissance vocale continue de tourner pendant la dictée (texte "
        "visible et modifiable) ET cette transcription accompagne la note comme "
        "guide : l'audio reste la source autoritaire, mais le texte en soutien "
        "réduit les omissions (éléments dictés absents de la transcription, "
        "mal transcrits ou oubliés d'un audio seul). Désactivée (par défaut) : "
        "aucun appel au service vocal "
        "pendant l'enregistrement, économie maximale.",
        "No effect if the option above is off. On: speech recognition keeps "
        "running during dictation (visible, editable text) AND that transcript "
        "accompanies the note as a guide: the audio stays the authoritative "
        "source, but the supporting text reduces omissions (dictated items "
        "absent from the transcript, mis-transcribed, or missed from audio "
        "alone). Off (default): no call to the speech service during "
        "recording, maximum savings.",
    ),

    # --- Panneau d'administration : réglages -------------------------------
    "set.allow_signup.label": (
        "Inscription automatique",
        "Automatic sign-up",
    ),
    "set.allow_signup.help": (
        "Activée : tout compte authentifié par le fournisseur d'identité est "
        "créé et autorisé sans intervention. Désactivée : seuls les comptes "
        "déjà présents peuvent entrer, les autres sont refusés. À ne laisser "
        "activée que si l'inscription est fermée chez le fournisseur — sinon "
        "quiconque peut s'y créer un compte entre ici.",
        "Enabled: any account authenticated by the identity provider is created "
        "and authorized with no intervention. Disabled: only existing accounts "
        "may enter, others are refused. Leave this enabled only if sign-up is "
        "closed at the provider — otherwise anyone who can register there gets "
        "in here.",
    ),
    "set.backup_retention_count.label": (
        "Sauvegardes conservées",
        "Backups kept",
    ),
    "set.backup_retention_count.help": (
        "Nombre de sauvegardes gardées avant suppression automatique des plus "
        "anciennes — sauvegardes quotidiennes, exports manuels et sauvegardes "
        "de sécurité pré-restauration confondus. La rotation privilégie la "
        "couverture temporelle : ~50 % d'instantanés quotidiens (un par jour), "
        "~25 % hebdomadaires (un par semaine) et ~25 % mensuels (un par mois). "
        "0 désactive la rotation : tout s'accumule.",
        "Number of backups kept before the oldest are automatically deleted — "
        "daily backups, manual exports and pre-restore safety snapshots all "
        "counted together. Rotation favors time coverage: ~50% daily snapshots "
        "(one per day), ~25% weekly (one per week) and ~25% monthly (one per "
        "month). 0 disables rotation: everything accumulates.",
    ),
    "set.consultation_retention_hours.label": (
        "Purger les dossiers après (heures)",
        "Purge records after (hours)",
    ),
    "set.consultation_retention_hours.help": (
        "Un dossier (brouillon, transcription, note et audio) dont la dernière "
        "modification dépasse ce délai est supprimé définitivement, sans "
        "récupération possible. Le compte repart à zéro à chaque ouverture ou "
        "modification. Laisser à 0 pour désactiver la purge.",
        "A record (draft, transcript, note and audio) whose last modification "
        "exceeds this delay is permanently deleted, with no recovery possible. "
        "The countdown restarts each time it is opened or edited. Leave at 0 "
        "to disable the purge.",
    ),
    "set.oidc_name_claim.label": (
        "Attribut du nom affiché",
        "Display-name attribute",
    ),
    "set.oidc_name_claim.help": (
        "Quel attribut du fournisseur d'identité sert de nom affiché : "
        "« name » (nom complet), « preferred_username » (nom d'usager), "
        "« nickname », « given_name »… S'il est absent de la réponse, "
        "l'application essaie les autres dans cet ordre, puis le courriel. "
        "Le nom d'usager, lui, ne change pas : c'est la clé de propriété des "
        "consultations.",
        "Which identity-provider attribute is used as the display name: “name” "
        "(full name), “preferred_username”, “nickname”, “given_name”… If it is "
        "absent from the response, the application tries the others in that "
        "order, then the email. The username itself never changes: it is the "
        "ownership key of consultations.",
    ),
    "set.oidc_picture_claim.label": (
        "Attribut de l'avatar",
        "Avatar attribute",
    ),
    "set.oidc_picture_claim.help": (
        "Propriété portant l'adresse de la photo, « picture » chez la plupart "
        "des fournisseurs. Si elle est absente — ou si son adresse n'est pas en "
        "https — la pastille affiche les initiales. L'avatar est rafraîchi à "
        "chaque connexion.",
        "The property carrying the photo URL, “picture” with most providers. If "
        "it is absent — or if the URL is not https — the badge shows initials "
        "instead. The avatar is refreshed on each sign-in.",
    ),
    "set.stt_provider.label": (
        "Service de reconnaissance vocale",
        "Speech recognition service",
    ),
    "set.stt_provider.help": (
        "Le découpage de la dictée en tranches est identique dans tous les "
        "cas : seul l'envoi final change.",
        "Dictation is split into segments the same way in every case: only "
        "the final upload differs.",
    ),
    "set.deepgram_api_key.label": ("Clé API Deepgram", "Deepgram API key"),
    "set.deepgram_api_key.help": (
        "console.deepgram.com → API Keys. Requise si Deepgram est sélectionné.",
        "console.deepgram.com → API Keys. Required if Deepgram is selected.",
    ),
    "set.deepgram_model.label": ("Modèle Deepgram", "Deepgram model"),
    "set.deepgram_model.help": (
        "nova-2 pour le français canadien : c'est la dernière génération où "
        "l'adaptation par mots-clés fonctionne hors anglais. nova-3 est plus "
        "récent mais ignore les mots-clés dans les autres langues.",
        "nova-2 for French: it is the last generation where keyword "
        "adaptation works outside English. nova-3 is newer but ignores "
        "keywords in other languages.",
    ),
    "set.deepgram_language.label": ("Langue Deepgram", "Deepgram language"),
    "set.deepgram_language.help": (
        "Laisser vide pour suivre la langue de l'application. Une valeur "
        "inscrite ici l'emporte et survit au changement de langue.",
        "Leave empty to follow the application language. A value entered here "
        "takes precedence and survives a language change.",
    ),
    "set.assemblyai_api_key.label": ("Clé API AssemblyAI", "AssemblyAI API key"),
    "set.assemblyai_api_key.help": (
        "assemblyai.com → Dashboard → API Keys.",
        "assemblyai.com → Dashboard → API Keys.",
    ),
    "set.assemblyai_model.label": ("Modèle AssemblyAI", "AssemblyAI model"),
    "set.assemblyai_model.help": (
        "universal-3-5-pro (défaut) ou universal-2. Le premier reconnaît "
        "explicitement le français québécois et accepte 1000 termes "
        "d'adaptation, contre 200 pour le second.",
        "universal-3-5-pro (default) or universal-2. The former explicitly "
        "recognizes Quebec French and accepts 1000 adaptation terms, versus "
        "200 for the latter.",
    ),
    "set.assemblyai_language.label": ("Langue AssemblyAI", "AssemblyAI language"),
    "set.assemblyai_language.help": (
        "Laisser vide pour suivre la langue de l'application. Inscrire "
        "« auto » pour la détection automatique. « fr » couvre le français "
        "québécois : AssemblyAI ne demande pas de code de dialecte.",
        "Leave empty to follow the application language. Enter “auto” for "
        "automatic detection. “fr” covers Quebec French: AssemblyAI does not "
        "ask for a dialect code.",
    ),
    "set.assemblyai_medical.label": (
        "Mode médical AssemblyAI",
        "AssemblyAI medical mode",
    ),
    "set.assemblyai_medical.help": (
        "Module « medical-v1 » : améliore les noms de médicaments, de "
        "procédures, les diagnostics et les posologies. Le français en fait "
        "partie. Facturé en supplément (~0,15 $US/h) ; sur une langue non "
        "prise en charge, l'option est simplement ignorée, sans frais.",
        "The “medical-v1” module: improves drug names, procedures, diagnoses "
        "and dosages. French is supported. Billed as an extra (~US$0.15/h); "
        "on an unsupported language the option is simply ignored, at no cost.",
    ),
    "set.stt_trim_silence.label": (
        "Retirer les longues pauses",
        "Trim long pauses",
    ),
    "set.stt_trim_silence.help": (
        "Plafonne les pauses au-delà de « Pause conservée » dans la copie "
        "envoyée — de quoi réduire la durée facturée (services à la durée) "
        "et les jetons audio du modèle. Bascule GLOBALE : s'applique à toutes "
        "les pistes et à tous les fournisseurs (reconnaissance vocale et audio "
        "joint au modèle de langage). Seule la copie envoyée est raccourcie : "
        "l'enregistrement conservé avec le brouillon reste intact, et la durée "
        "affichée reste celle de la dictée réelle.",
        "Caps pauses beyond “Pause kept” in the copy that is sent — reducing "
        "billed duration (duration-billed services) and model audio tokens. "
        "GLOBAL toggle: applies to every track and provider (speech "
        "recognition and the audio attached to the language model). Only the "
        "sent copy is shortened: the recording kept with the draft stays "
        "intact, and the displayed duration remains that of the actual "
        "dictation.",
    ),
    "set.stt_silence_keep_seconds.label": (
        "Pause conservée (secondes)",
        "Pause kept (seconds)",
    ),
    "set.dictation_grounding.label": (
        "Correction des médicaments",
        "Medication correction",
    ),
    "set.dictation_grounding.help": (
        "Pendant la dictée, l'application réécoute les mots coupés en fin de "
        "segment (stabilisation par l'arrière, quelques secondes de latence) "
        "et corrige les noms de médicaments déformés par la reconnaissance "
        "vocale contre la base canadienne BDP livrée dans l'image. Une liste "
        "pointée des médicaments (nom + posologie) s'affiche sous le transcrit "
        "et dans l'onglet Validation. Coûte des appels STT en arrière-plan — "
        "prévu pour un point de terminaison custom auto-hébergé (sans limite "
        "de taux).",
        "During dictation, the app re-listens to words cut off at segment "
        "boundaries (rear stabilization, a few seconds of latency) and "
        "corrects drug names garbled by speech recognition against the "
        "Canadian DPD database shipped in the image. A bullet list of "
        "medications (name + dosage) appears below the transcript and in the "
        "Validation tab. Costs background STT calls — intended for a "
        "self-hosted custom endpoint (no rate limit).",
    ),
    "set.stt_silence_keep_seconds.help": (
        "Toute pause plus courte est gardée telle quelle ; les plus longues "
        "sont ramenées à cette durée. Ne pas descendre à 0 : les moteurs se "
        "servent des pauses pour placer la ponctuation et séparer les "
        "phrases — sur une liste de médicaments, cela compte.",
        "Any shorter pause is kept as is; longer ones are reduced to this "
        "duration. Do not go down to 0: engines use pauses to place "
        "punctuation and separate sentences — on a medication list, that "
        "matters.",
    ),
    "set.soniox_api_key.label": ("Clé API Soniox", "Soniox API key"),
    "set.soniox_api_key.help": (
        "console.soniox.com → API Keys.",
        "console.soniox.com → API Keys.",
    ),
    "set.soniox_model.label": ("Modèle Soniox", "Soniox model"),
    "set.soniox_model.help": (
        "Modèle asynchrone (fichier). Le tarif annoncé est d'environ "
        "0,10 $US/h, soit le quart d'AssemblyAI avec ses modules.",
        "Asynchronous (file) model. The published rate is about US$0.10/h, a "
        "quarter of AssemblyAI with its add-ons.",
    ),
    "set.soniox_language.label": ("Langue Soniox", "Soniox language"),
    "set.soniox_language.help": (
        "Laisser vide pour suivre la langue de l'application. Inscrire "
        "« auto » pour la détection automatique : Soniox est multilingue par "
        "conception, ce qui convient à une consultation qui alterne deux "
        "langues.",
        "Leave empty to follow the application language. Enter “auto” for "
        "automatic detection: Soniox is multilingual by design, which suits a "
        "consultation that alternates between two languages.",
    ),
    "set.soniox_send_context.label": (
        "Contexte de transcription Soniox",
        "Soniox transcription context",
    ),
    "set.soniox_send_context.help": (
        "Envoie le vocabulaire médical et le contexte de domaine à Soniox "
        "pour améliorer la précision de la transcription. Désactiver réduit "
        "les tokens texte facturés.",
        "Sends medical vocabulary and domain context to Soniox to improve "
        "transcription accuracy. Disabling reduces billed text tokens.",
    ),
    "set.cohere_api_key.label": ("Clé API Cohere", "Cohere API key"),
    "set.cohere_api_key.help": (
        "dashboard.cohere.com → API Keys. Une clé d'ESSAI est limitée à "
        "5 requêtes par minute, toutes dictées confondues ; une clé de "
        "production se négocie avec Cohere.",
        "dashboard.cohere.com → API Keys. A TRIAL key is limited to 5 requests "
        "per minute across all dictations; a production key must be arranged "
        "with Cohere.",
    ),
    "set.cohere_model.label": ("Modèle Cohere", "Cohere model"),
    "set.cohere_model.help": (
        "cohere-transcribe-03-2026 au moment de l'intégration. Modèle de "
        "reconnaissance vocale dédié, 14 langues dont le français.",
        "cohere-transcribe-03-2026 at the time of integration. A dedicated "
        "speech recognition model covering 14 languages, French included.",
    ),
    "set.cohere_language.label": ("Langue Cohere", "Cohere language"),
    "set.cohere_language.help": (
        "Laisser vide pour suivre la langue du gabarit. Cohere n'accepte que "
        "de l'ISO-639-1 : « fr », « en » — jamais « fr-CA ».",
        "Leave empty to follow the template language. Cohere only accepts "
        "ISO-639-1: “fr”, “en” — never “fr-CA”.",
    ),

    "set.mistral_api_key.label": ("Clé API Mistral", "Mistral API key"),
    "set.mistral_api_key.help": (
        "console.mistral.ai → API Keys. Cette même clé sert au modèle de "
        "langage, sous l'onglet Modèle de langage → Mistral AI.",
        "console.mistral.ai → API Keys. This same key is used by the language "
        "model, under the Language model tab → Mistral AI.",
    ),
    "set.mistral_model.label": ("Modèle Voxtral", "Voxtral model"),
    "set.mistral_model.help": (
        "voxtral-mini-latest au moment de l'intégration ; voxtral-small-latest "
        "est plus précis mais plus coûteux.",
        "voxtral-mini-latest at the time of integration; voxtral-small-latest "
        "is more accurate but more expensive.",
    ),
    "set.mistral_language.label": ("Langue Mistral", "Mistral language"),
    "set.mistral_language.help": (
        "Laisser vide pour suivre la langue du gabarit. Mistral n'accepte que "
        "de l'ISO-639-1 : « fr », « en » — jamais « fr-CA ».",
        "Leave empty to follow the template language. Mistral only accepts "
        "ISO-639-1: “fr”, “en” — never “fr-CA”.",
    ),
    "set.mistral_realtime_model.label": (
        "Modèle Voxtral temps réel", "Voxtral realtime model",
    ),
    "set.mistral_realtime_model.help": (
        "Modèle de TRANSCRIPTION STREAMING (deltas pendant la parole), utilisé "
        "en mode « sse » uniquement. Réservé à la transcription en direct.",
        "Streaming transcription model (deltas while speaking), used in “sse” "
        "mode only. Intended for live transcription.",
    ),
    "set.mistral_realtime_delay_ms.label": (
        "Délai de contexte (ms)", "Context delay (ms)",
    ),
    "set.mistral_realtime_delay_ms.help": (
        "Le modèle attend ce temps avant de transcrire, pour rassembler le "
        "contexte des paroles précédentes (target_streaming_delay_ms). Plus "
        "élevé = plus précis mais texte plus tardif ; 0 = défaut du service.",
        "The model waits this long before transcribing, to gather context "
        "from previous speech (target_streaming_delay_ms). Higher = more "
        "accurate but later text; 0 = service default.",
    ),

    "set.stt_realtime_mode.label": ("Temps réel de la dictée", "Realtime dictation"),
    "set.stt_realtime_mode.help": (
        "Le texte apparaît pendant la dictée, par-dessus le batch fiable "
        "inchangé. « désactivé » : comportement historique (l'audio ne quitte "
        "jamais la machine). « énoncé » : le VAD du navigateur signale la fin "
        "de chaque phrase, transcription immédiate au silence — compatible avec "
        "tous les fournisseurs, dont le Parakeet local. « streaming » : deltas "
        "chez Mistral Voxtral realtime pendant la parole (l'audio part alors "
        "chez Mistral). « streaming » n'est applicable qu'à Mistral ; « énoncé » "
        "est inapplicable à Cohere (5 requêtes/min).",
        "Text appears while dictating, on top of the unchanged reliable batch. "
        "“Disabled”: legacy behavior (audio never leaves the machine). "
        "“Utterance”: the browser VAD signals the end of each sentence, "
        "immediate transcription at silence — works with every provider, "
        "including the local Parakeet. “Streaming”: Mistral Voxtral realtime "
        "deltas while speaking (audio then goes to Mistral). “Streaming” only "
        "applies to Mistral; “Utterance” cannot apply to Cohere (5 req/min).",
    ),
    "set.stt_realtime_mode.off": ("désactivé", "disabled"),
    "set.stt_realtime_mode.vad": ("énoncé (VAD)", "utterance (VAD)"),
    "set.stt_realtime_mode.sse": ("streaming (Mistral)", "streaming (Mistral)"),

    "set.stt_vad_sensitivity.label": (
        "Sensibilité du détecteur de parole", "Speech detector sensitivity",
    ),
    "set.stt_vad_sensitivity.help": (
        "Seuil d'énergie du micro au-dessus duquel une voix est reconnue. "
        "« élevée » convient à un micro doux ou à un local bruyant, « faible » "
        "à un casque ; un seuil trop bas laisse passer les bruits de fond, trop "
        "haut coupe les attaques de mots. Ce réglage ne touche jamais à "
        "l'audio enregistré, il ne pilote que la transcription.",
        "Microphone energy threshold above which speech is detected. “High” "
        "suits a quiet mic or a noisy room, “low” a headset; too low lets "
        "background noise through, too high clips word onsets. This never "
        "touches the recorded audio, it only gates transcription.",
    ),
    "set.stt_vad_sensitivity.low": ("faible", "low"),
    "set.stt_vad_sensitivity.medium": ("moyenne", "medium"),
    "set.stt_vad_sensitivity.high": ("élevée", "high"),
    "set.stt_vad_speech_ms.label": (
        "Parole reconnue après (ms)", "Speech recognized after (ms)",
    ),
    "set.stt_vad_speech_ms.help": (
        "Durée de signal au-dessus du seuil avant de considérer qu'une voix "
        "parle. Un court bruit (clavier, papier) ne déclenche rien.",
        "How long the signal must stay above threshold before speech is "
        "assumed. A brief noise (keyboard, paper) triggers nothing.",
    ),
    "set.stt_vad_silence_ms.label": (
        "Fin d'énoncé après (ms)", "Utterance end after (ms)",
    ),
    "set.stt_vad_silence_ms.help": (
        "Pause au-dessous du seuil qui marque la fin de la phrase : le texte "
        "apparaît alors. Une valeur trop haute retarde le texte, trop basse "
        "coupe au milieu d'une hésitation.",
        "Silence below threshold that marks the end of the sentence — the text "
        "then appears. Too high delays the text, too low cuts mid-hesitation.",
    ),
    "set.stt_vad_finish_sweep.label": (
        "Re-transcrire les trous à la fin", "Re-transcribe gaps on finish",
    ),
    "set.stt_vad_finish_sweep.help": (
        "Au « Terminer », re-parcourt l'audio brut complet et re-transcrit les "
        "passages de parole manqués (VAD trop strict, tranche échouée). "
        "L'audio ayant été conservé en entier, rien n'est perdu.",
        "On “Finish”, rescans the full raw audio and re-transcribes any missed "
        "speech (over-strict VAD, failed segment). Since the full audio is "
        "kept, nothing is lost.",
    ),

    # Pas de clé propre : voir set.openai_api_key, sous Modèle de langage,
    # dont le champ est répété ici (voir app.js, PROVIDER_KEY_FIELD).
    "set.openai_stt_model.label": ("Modèle OpenAI", "OpenAI model"),
    "set.openai_stt_model.help": (
        "whisper-1 au moment de l'intégration ; gpt-4o-transcribe est plus "
        "récent et plus précis sur certains accents.",
        "whisper-1 at the time of integration; gpt-4o-transcribe is newer and "
        "more accurate on some accents.",
    ),
    "set.openai_stt_language.label": ("Langue OpenAI", "OpenAI language"),
    "set.openai_stt_language.help": (
        "Laisser vide pour suivre la langue du gabarit. OpenAI n'accepte que "
        "de l'ISO-639-1 : « fr », « en » — jamais « fr-CA ».",
        "Leave empty to follow the template language. OpenAI only accepts "
        "ISO-639-1: “fr”, “en” — never “fr-CA”.",
    ),

    "set.modulate_api_key.label": ("Clé API Modulate", "Modulate API key"),
    "set.modulate_api_key.help": (
        "Clé « X-API-Key » de la plateforme Modulate (Velma STT), créée dans "
        "l'onglet API Keys du tableau de bord.",
        "“X-API-Key” from the Modulate platform (Velma STT), created in the "
        "API Keys tab of the dashboard.",
    ),
    "set.modulate_model.label": ("Modèle Modulate", "Modulate model"),
    "set.modulate_model.help": (
        "Dernier segment de l'URL de l'endpoint : « velma-2-stt-batch » "
        "(multilingue, vocabulaire personnalisé) par défaut ; "
        "« velma-2-stt-batch-multilingual-vfast » (plus rapide, sans "
        "vocabulaire) ; « velma-2-stt-batch-english-vfast » (anglais seul).",
        "Last segment of the endpoint URL: “velma-2-stt-batch” (multilingual, "
        "custom vocabulary) by default; “velma-2-stt-batch-multilingual-vfast” "
        "(faster, no vocabulary); “velma-2-stt-batch-english-vfast” "
        "(English only).",
    ),
    "set.modulate_language.label": ("Langue Modulate", "Modulate language"),
    "set.modulate_language.help": (
        "Laisser vide pour suivre la langue du gabarit, « auto » pour laisser "
        "Modulate détecter. Modulate accepte de l'ISO-639-1 : « fr », « en ».",
        "Leave empty to follow the template language, “auto” to let Modulate "
        "detect. Modulate accepts ISO-639-1: “fr”, “en”.",
    ),

    "set.custom_stt_api_key.label": ("Clé API", "API key"),
    "set.custom_stt_api_key.help": (
        "Selon le service : laisser vide si le point de terminaison n'exige "
        "aucune authentification.",
        "Depending on the service: leave empty if the endpoint requires no "
        "authentication.",
    ),
    "set.custom_stt_base_url.label": ("Adresse de base", "Base URL"),
    "set.custom_stt_base_url.help": (
        "Adresse compatible OpenAI, jusqu'au préfixe de version inclus (ex. "
        "« https://exemple.tld/v1 »). « /audio/transcriptions » y est ajouté "
        "automatiquement.",
        "OpenAI-compatible address, including the version prefix (e.g. "
        "“https://example.tld/v1”). “/audio/transcriptions” is appended "
        "automatically.",
    ),
    "set.custom_stt_model.label": ("Modèle", "Model"),
    "set.custom_stt_model.help": (
        "Nom du modèle tel qu'attendu par ce point de terminaison, par "
        "exemple « whisper-1 ».",
        "Model name as expected by this endpoint, e.g. “whisper-1”.",
    ),
    "set.custom_stt_language.label": ("Langue", "Language"),
    "set.custom_stt_language.help": (
        "Laisser vide pour suivre la langue du gabarit, ou inscrire « auto » "
        "si le service détecte la langue lui-même.",
        "Leave empty to follow the template language, or enter “auto” if the "
        "service detects the language itself.",
    ),
    "set.custom_stt_fallback_model.label": ("Modèle de repli", "Fallback model"),
    "set.custom_stt_fallback_model.help": (
        "Modèle utilisé quand l'endpoint principal échoue (erreur HTTP 5xx) ou "
        "que la dictée dépasse le seuil de durée ci-dessous. Laisser vide pour "
        "désactiver le repli. Ex. « Systran/faster-whisper-small ».",
        "Model used when the main endpoint fails (HTTP 5xx) or when the "
        "dictation exceeds the duration threshold below. Leave empty to disable "
        "the fallback. E.g. “Systran/faster-whisper-small”.",
    ),
    "set.custom_stt_fallback_base_url.label": ("Adresse de repli", "Fallback base URL"),
    "set.custom_stt_fallback_base_url.help": (
        "Adresse du point de terminaison de repli, même format que l'adresse "
        "principale. Vide = même adresse que le principal.",
        "Fallback endpoint address, same format as the main address. Empty = "
        "same address as the main one.",
    ),
    "set.custom_stt_max_seconds.label": ("Repli au-delà de (secondes)", "Fallback beyond (seconds)"),
    "set.custom_stt_max_seconds.help": (
        "Au-delà de cette durée, la dictée part directement au modèle de repli "
        "(utile si l'endpoint principal plafonne en longueur d'audio). Vide = "
        "pas de routage par durée, seul le repli sur erreur 5xx s'applique.",
        "Beyond this duration, the dictation is sent straight to the fallback "
        "model (useful when the main endpoint caps audio length). Empty = no "
        "duration-based routing; only the 5xx fallback applies.",
    ),
    "set.custom_stt_chunk_seconds.label": ("Tranches d'audio (secondes)", "Audio chunks (seconds)"),
    "set.custom_stt_chunk_seconds.help": (
        "Découpe l'audio en tranches de cette durée (ex. 60) avant l'envoi, "
        "en coupant de préférence dans un silence : chaque tranche part au "
        "modèle principal, même au-delà de sa limite d'audio en une passe "
        "(ex. Parakeet/ONNX plafonnant vers 6-7 min). Vide (défaut) = envoi en "
        "un bloc, le seuil de durée ci-dessus s'applique alors.",
        "Splits the audio into chunks of this length (e.g. 60) before sending, "
        "cutting preferably in a silence: each chunk goes to the main model, "
        "even beyond its one-pass audio limit (e.g. Parakeet/ONNX capping "
        "around 6-7 min). Empty (default) = send in one block, then the "
        "duration threshold above applies.",
    ),

    # Avertissement affiché dans le panneau quand Cohere est sélectionné.
    "admin.cohere_warning": (
        "Cohere est limité à 5 requêtes par minute. La dictée envoie une "
        "tranche toutes les 10 secondes et par usager : une dictée passe, deux "
        "passent à peine, trois dépassent. Déconseillé pour des dictées "
        "simultanées. L'application étale les envois et réessaie, mais une "
        "tranche peut être retardée.",
        "Cohere is rate-limited to 5 requests per minute. Dictation sends one "
        "segment every 10 seconds per user: one dictation fits, two barely, "
        "three exceed it. Not recommended for concurrent dictation sessions. "
        "The application spaces out requests and retries, but a segment may be "
        "delayed.",
    ),
    "admin.cohere_no_vocab": (
        "Cohere n'offre aucune adaptation au vocabulaire : ni mots-clés, ni "
        "contexte. Le lexique clinique et le vocabulaire des gabarits ne lui "
        "sont pas transmis — les noms de molécules et les acronymes sont "
        "précisément ce qui s'y transcrit le moins bien.",
        "Cohere offers no vocabulary adaptation: no keywords, no context. The "
        "clinical lexicon and template vocabulary are not sent to it — drug "
        "names and acronyms are exactly what it transcribes least well.",
    ),
    "set.llm_provider.label": ("Fournisseur", "Provider"),
    "set.llm_provider.help": ("", ""),

    # --- Réglages « modèle » : textes partagés -------------------------------
    # Les libellés et textes d'aide de modèle / modèle rapide / température /
    # audio joint / contournement du STT sont communs à tous les fournisseurs :
    # ils vivent sous set.cap.* ci-dessus, instanciés par les fabriques
    # _cap_* de runtime_config.py. Ne restent ici que les clés propres à un
    # fournisseur (Gemini thinking, budget Cohere, point de terminaison
    # personnalisé…).
    "set.gemini_api_key.label": ("Clé API Google Gemini", "Google Gemini API key"),
    "set.gemini_api_key.help": ("", ""),
    "set.gemini_thinking.label": (
        "Raisonnement (thinking)",
        "Thinking",
    ),
    "set.gemini_thinking.help": (
        "Désactivé (par défaut) : raisonnement coupé (budget 0). Pris en charge "
        "par gemini-2.5-flash ; gemini-2.5-pro le refuse — un message le "
        "signale alors, passez sur « Oui » avec un budget de 128. Activé : le "
        "modèle réfléchit avant de répondre ; le champ « Budget de "
        "raisonnement » ci-dessous fixe l'ampleur.",
        "Off (default): reasoning cut (budget 0). Supported by "
        "gemini-2.5-flash; gemini-2.5-pro rejects it — you will be told, "
        "then switch to “On” with a budget of 128. On: the model thinks "
        "before answering; the “Thinking budget” field below sets how much.",
    ),
    "set.gemini_thinking_budget.label": (
        "Budget de raisonnement (thinking)",
        "Thinking budget",
    ),
    "set.gemini_thinking_budget.help": (
        "Prise en compte si « Raisonnement » est activé. Jetons de raisonnement "
        "alloués au modèle avant de répondre. Minimum accepté par "
        "gemini-2.5-pro sur Vertex : 128 (0 et 1-127 sont refusés). Vide = 128.",
        "Used when “Thinking” is on. Reasoning tokens allocated to the model "
        "before answering. Minimum accepted by gemini-2.5-pro on Vertex: 128 "
        "(0 and 1-127 are rejected). Empty = 128.",
    ),
    "set.show_thinking_admin.label": (
        "Montrer le raisonnement — administrateurs",
        "Show reasoning — administrators",
    ),
    "set.show_thinking_admin.help": (
        "Affiche le raisonnement (thinking) du modèle dans la fenêtre de note "
        "pendant la génération, pour les administrateurs. La pensée défile "
        "puis s'efface dès que le texte de la note commence ; elle n'est "
        "jamais enregistrée. Hors fonction si le modèle ne produit pas de "
        "raisonnement.",
        "Shows the model's reasoning (thinking) in the note window during "
        "generation, for administrators. The thinking streams then is cleared "
        "as soon as the note text starts; it is never saved. No-op when the "
        "model produces no reasoning.",
    ),
    "set.show_thinking_users.label": (
        "Montrer le raisonnement — utilisateurs",
        "Show reasoning — users",
    ),
    "set.show_thinking_users.help": (
        "Même affichage transitoire du raisonnement (thinking), pour les "
        "utilisateurs non administrateurs. Désactivé par défaut : la pensée "
        "du modèle peut contenir des brouillons non relus.",
        "Same transient reasoning (thinking) display, for non-admin users. Off "
        "by default: the model's thinking may contain unrevised drafts.",
    ),
    "set.anthropic_api_key.label": ("Clé API Anthropic", "Anthropic API key"),
    "set.anthropic_api_key.help": ("", ""),

    "set.openai_api_key.label": ("Clé API OpenAI", "OpenAI API key"),
    "set.openai_api_key.help": ("", ""),

    # Cohere et Mistral n'ont pas de clé propre côté Note : elle se règle sous
    # Dictée (cohere_api_key / mistral_api_key), d'où le champ est répété via
    # ``also_in``. Leurs textes modèle/température sont ceux de set.cap.*.
    "set.cohere_llm_thinking_budget.label": (
        "Budget de raisonnement (jetons)",
        "Thinking budget (tokens)",
    ),
    "set.cohere_llm_thinking_budget.help": (
        "Budget de jetons accordé au raisonnement du modèle (thinking."
        "token_budget) lors de la mise en forme. Les modèles command-a "
        "raisonnent ; un budget trop bas produit une note vide « MAX_TOKENS » "
        "constatée en production. 0 = défaut du modèle. Doit rester sous le "
        "budget de sortie. Non envoyé à la relecture des métadonnées (tâche "
        "mécanique).",
        "Token budget given to the model's thinking (thinking.token_budget) "
        "when formatting the note. command-a models reason; a budget too low "
        "produced an empty 'MAX_TOKENS' note seen in production. 0 = model "
        "default. Must stay below the output budget. Not sent to the "
        "metadata re-read (a mechanical task).",
    ),

    "set.qwen_omni_api_key.label": ("Clé API Qwen Omni", "Qwen Omni API key"),
    "set.qwen_omni_api_key.help": (
        "Clé du compte Alibaba Cloud DashScope.",
        "Alibaba Cloud DashScope account key.",
    ),
    "set.qwen_omni_base_url.label": ("Adresse de base", "Base URL"),
    "set.qwen_omni_base_url.help": (
        "Adresse du mode compatible OpenAI de DashScope, jusqu'au préfixe de "
        "version inclus. Diffère selon la région du compte (internationale "
        "ou Chine continentale) — voir la documentation DashScope.",
        "Address of DashScope's OpenAI-compatible mode, including the "
        "version prefix. Differs by account region (international or "
        "mainland China) — see the DashScope documentation.",
    ),

    "set.custom_llm_api_key.label": ("Clé API", "API key"),
    "set.custom_llm_api_key.help": (
        "Selon le service : laisser vide si le point de terminaison n'exige "
        "aucune authentification.",
        "Depending on the service: leave empty if the endpoint requires no "
        "authentication.",
    ),
    "set.custom_llm_base_url.label": ("Adresse de base", "Base URL"),
    "set.custom_llm_base_url.help": (
        "Adresse compatible OpenAI, jusqu'au préfixe de version inclus (ex. "
        "« https://exemple.tld/v1 »).",
        "OpenAI-compatible address, including the version prefix (e.g. "
        "“https://example.tld/v1”).",
    ),
    "set.custom_llm_model.label": ("Modèle", "Model"),
    "set.custom_llm_model.help": (
        "Nom du modèle tel qu'attendu par ce point de terminaison.",
        "Model name as expected by this endpoint.",
    ),
    "set.custom_llm_model_fast.label": (
        "Modèle rapide (métadonnées)",
        "Fast model (metadata)",
    ),
    "set.custom_llm_model_fast.help": (
        "Utilisé pour la seule relecture des métadonnées, une tâche triviale "
        "payée au jeton. Laisser vide pour employer le modèle principal.",
        "Used only to re-read metadata, a trivial task paid by the token. "
        "Leave empty to use the main model.",
    ),
    "set.custom_llm_temperature.label": ("Température", "Temperature"),
    "set.custom_llm_temperature.help": (
        "0 = déterministe. Au-delà de 0,4 le modèle commence à broder, ce qui "
        "n'a pas sa place dans une note clinique. Certains points de "
        "terminaison ignorent ce réglage : la note est produite quand même.",
        "0 = deterministic. Above 0.4 the model starts embellishing, which "
        "has no place in a clinical note. Some endpoints ignore this setting: "
        "the note is produced anyway.",
    ),
    "set.custom_llm_max_tokens.label": ("Budget de sortie (jetons)", "Output budget (tokens)"),
    "set.custom_llm_max_tokens.help": (
        "Plafond de jetons de sortie pour ce fournisseur (raisonnement + "
        "texte). Les modèles à raisonnement (ex. DeepSeek) consomment une "
        "large part du budget dans leur pensée : un budget trop bas produit "
        "une réponse vide (« motif : length »). 32768 par défaut, en sus du "
        "réglage propre à Gemini.",
        "Output token cap for this provider (reasoning + text). Reasoning "
        "models (e.g. DeepSeek) spend a large share of the budget on "
        "thinking: a too-small budget yields an empty reply ('finish_reason: "
        "length'). Default 32768, in addition to the Gemini-specific setting.",
    ),
    "set.custom_llm_reasoning_effort.label": ("Raisonnement", "Reasoning"),
    "set.custom_llm_reasoning_effort.help": (
        "Effort de raisonnement demandé aux modèles à raisonnement (DeepSeek, "
        "etc.) : « Automatique » = paramètre non envoyé, le modèle fait son "
        "choix ; « Aucun » = raisonnement totalement désactivé (plus rapide, "
        "moins cher). Les autres valeurs envoient le réglage au point de "
        "terminaison (OpenRouter `reasoning.effort`) ; tous les modèles ne "
        "l'honorent pas (sur DeepSeek v4, « Faible »/« Minimal » augmentent la "
        "pensée). Si la note sort vide (raisonnement saturant le budget), "
        "relevez le Budget de sortie plutôt que d'augmenter l'effort.",
        "Reasoning effort requested from models that support it (DeepSeek, "
        "etc.): 'Automatic' = parameter not sent, the model decides; 'None' = "
        "reasoning fully disabled (faster, cheaper). Other values forward the "
        "setting to the endpoint (OpenRouter `reasoning.effort`); not every "
        "model honors it (on DeepSeek v4, 'Low'/'Minimal' increase thinking). "
        "If the note comes back empty (reasoning saturating the budget), raise "
        "the Output budget rather than increasing effort.",
    ),
    "set.openrouter_api_key.label": ("Clé API OpenRouter", "OpenRouter API key"),
    "set.openrouter_api_key.help": (
        "openrouter.ai → Keys. Une seule clé pour les deux usages : la note et "
        "la transcription STT passent par le même compte.",
        "openrouter.ai → Keys. One key for both uses: note and STT "
        "transcription go through the same account.",
    ),
    "set.openrouter_llm_max_tokens.label": ("Budget de sortie (jetons)", "Output budget (tokens)"),
    "set.openrouter_llm_max_tokens.help": (
        "Plafond de jetons de sortie pour OpenRouter (raisonnement + texte). "
        "Inkling raisonne : un budget trop bas produit une note vide (« motif "
        ": length »). 32768 par défaut.",
        "Output token cap for OpenRouter (reasoning + text). Inkling reasons: "
        "a too-small budget yields an empty note ('finish_reason: length'). "
        "Default 32768.",
    ),
    "set.openrouter_llm_reasoning_effort.label": ("Raisonnement", "Reasoning"),
    "set.openrouter_llm_reasoning_effort.help": (
        "Effort de raisonnement demandé aux modèles à raisonnement (Inkling, "
        "etc.) : « Automatique » = paramètre non envoyé ; « Aucun » = "
        "raisonnement désactivé (plus rapide, moins cher). Tous les modèles "
        "n'honorent pas `reasoning.effort`.",
        "Reasoning effort requested from reasoning models (Inkling, etc.): "
        "'Automatic' = parameter not sent; 'None' = reasoning disabled "
        "(faster, cheaper). Not every model honors `reasoning.effort`.",
    ),
    "set.openrouter_send_audio_format.label": (
        "Format audio envoyé",
        "Sent audio format",
    ),
    "set.openrouter_send_audio_format.help": (
        "Format de l'extrait joint au modèle. OGG/Opus (défaut) est le format "
        "natif de l'application et le plus léger ; WAV convient aux modèles "
        "qui le refusent.",
        "Format of the clip attached to the model. OGG/Opus (default) is the "
        "app's native format and the lightest; WAV suits models that reject it.",
    ),
    "set.openrouter_stt_model.label": ("Modèle OpenRouter", "OpenRouter model"),
    "set.openrouter_stt_model.help": (
        "Modèle multimodal (audio → texte) interrogé en « chat » pour la "
        "transcription : OpenRouter ne sert pas inkling-small derrière "
        "« /audio/transcriptions ». Le modèle doit comprendre le français.",
        "Multimodal model (audio → text) queried via 'chat' for transcription: "
        "OpenRouter does not serve inkling-small behind '/audio/transcriptions'. "
        "The model must understand French.",
    ),
    "set.openrouter_stt_language.label": ("Langue OpenRouter", "OpenRouter language"),
    "set.openrouter_stt_language.help": (
        "Laisser vide pour suivre la langue de l'application. Inscrire « auto » "
        "pour la détection automatique ; « fr » / « en » pour forcer.",
        "Leave empty to follow the application language. Enter “auto” for "
        "automatic detection; “fr”/“en” to force.",
    ),
    "set.custom_send_audio.label": (
        "Joindre aussi l'audio (silences plafonnés)",
        "Also attach audio (pauses capped)",
    ),
    "set.custom_send_audio.help": (
        "Envoie l'extrait audio en plus de la transcription : utile si le "
        "point de terminaison (ex. OpenRouter) expose un modèle multimodal — "
        "le modèle peut trancher un terme mal reconnu (nom propre, terme "
        "médical) en l'écoutant. Sans effet si le modèle ne gère pas l'audio : "
        "la note se génère alors comme avant. Ajoute un coût et quelques "
        "secondes par note.",
        "Sends the audio clip alongside the transcript: useful if the "
        "endpoint (e.g. OpenRouter) exposes a multimodal model — the model "
        "can resolve a poorly recognized term (proper noun, medical term) by "
        "listening to it. No effect if the model does not handle audio: the "
        "note is generated as before. Adds cost and a few seconds per note.",
    ),
    "set.custom_send_audio_max_minutes.label": (
        "Durée maximale envoyée (minutes)",
        "Maximum duration sent (minutes)",
    ),
    "set.custom_send_audio_max_minutes.help": (
        "Au-delà de cette durée d'audio (après retrait des silences), rien "
        "n'est joint — la note se génère comme avant, sur la seule "
        "transcription. Protège la latence et le coût sur une très longue "
        "dictée.",
        "Beyond this much audio (after silence trimming), nothing is "
        "attached — the note is generated as before, from the transcript "
        "alone. Protects latency and cost on a very long dictation.",
    ),
    "set.custom_send_audio_format.label": (
        "Format audio envoyé",
        "Audio format sent",
    ),
    "set.custom_send_audio_format.help": (
        "Format du fichier audio joint au modèle. OGG/Opus (défaut) est le "
        "plus léger et convient à Gemini et Qwen. Certains modèles d'un point "
        "de terminaison personnalisé n'acceptent que MP3 ou WAV "
        "(ex. Mistral Voxtral via OpenRouter) : choisissez alors mp3 ou wav.",
        "Format of the audio file attached to the model. OGG/Opus (default) "
        "is the lightest and suits Gemini and Qwen. Some models behind a "
        "custom endpoint only accept MP3 or WAV (e.g. Mistral Voxtral via "
        "OpenRouter): pick mp3 or wav then.",
    ),
    "set.custom_bypass_stt.label": (
        "Ignorer la reconnaissance vocale (audio direct)",
        "Skip speech recognition (direct audio)",
    ),
    "set.custom_bypass_stt.help": (
        "L'audio part directement au modèle multimodal, sans passer par le "
        "service de reconnaissance vocale — économise son coût et sa "
        "latence. La note peut alors se générer sans transcription, dès "
        "qu'un enregistrement existe. Sans effet si le point de terminaison "
        "n'accepte pas l'audio.",
        "Audio goes straight to the multimodal model, without passing "
        "through speech recognition — saves its cost and latency. The note "
        "can then be generated without a transcript, as soon as a recording "
        "exists. No effect if the endpoint does not accept audio.",
    ),
    "set.custom_bypass_stt_keep_transcript.label": (
        "Conserver une transcription pendant l'enregistrement",
        "Keep a transcript during recording",
    ),
    "set.custom_bypass_stt_keep_transcript.help": (
        "Sans effet si l'option ci-dessus est désactivée. Activée : la "
        "reconnaissance vocale continue de tourner pendant la dictée (texte "
        "visible et modifiable), mais la note se génère quand même à partir "
        "de l'audio. Désactivée (par défaut) : aucun appel au service vocal "
        "pendant l'enregistrement, économie maximale.",
        "No effect if the option above is off. On: speech recognition keeps "
        "running during dictation (visible, editable text), but the note is "
        "still generated from the audio. Off (default): no call to the "
        "speech service during recording, maximum savings.",
    ),

    "provider.custom_endpoint": (
        "Point de terminaison personnalisé",
        "Custom endpoint",
    ),
    "provider.openrouter": (
        "OpenRouter",
        "OpenRouter",
    ),

    "set.general_prompt.label": ("Consigne générale", "General instruction"),
    "set.general_prompt.help": (
        "Ajoutée aux consignes de TOUS les gabarits et appliquée quel que "
        "soit le modèle choisi. Elle passe après celles du gabarit : en cas "
        "de contradiction, c'est elle qui l'emporte. C'est ici que se met ce "
        "qui est propre à votre pratique — une spécialité, un vocabulaire, "
        "des habitudes de rédaction.",
        "Added to the instructions of ALL templates and applied whichever "
        "model is chosen. It comes after the template's own: in case of "
        "conflict, it wins. This is where anything specific to your practice "
        "belongs — a specialty, a vocabulary, writing habits.",
    ),
    "set.general_prompt.placeholder": (
        "Ex. : Utiliser systématiquement le vouvoiement. Ne jamais abréger "
        "les noms de médicaments.",
        "e.g.: Always spell out drug names. Never abbreviate diagnoses.",
    ),
    # Une consigne par langue : les libellés se distinguent, l'aide est partagée.
    "set.general_prompt_fr.label": (
        "Consigne générale (gabarits en français)",
        "General instruction (French templates)",
    ),
    "set.general_prompt_en.label": (
        "Consigne générale (gabarits en anglais)",
        "General instruction (English templates)",
    ),
    "choice.on": ("Activé", "Enabled"),
    "choice.off": ("Désactivé", "Disabled"),
    "choice.reasoning_auto": ("Automatique", "Automatic"),
    "choice.reasoning_none": ("Aucun", "None"),
    "choice.reasoning_minimal": ("Minimal", "Minimal"),
    "choice.reasoning_low": ("Faible", "Low"),
    "choice.reasoning_medium": ("Moyen", "Medium"),
    "choice.reasoning_high": ("Élevé", "High"),

    # --- Chargement et diagnostic ------------------------------------------
    "app.load_failed": ("Chargement impossible : {error}", "Loading failed: {error}"),
    "toast.dismiss": ("Fermer", "Dismiss"),
    "net.unreachable": (
        "Serveur injoignable. Vérifiez votre connexion réseau.",
        "Server unreachable. Check your network connection.",
    ),
    "net.http_error": ("Erreur {status}", "Error {status}"),
    "pwa.updated": (
        "Mise à jour installée — rechargez pour l'appliquer.",
        "Update installed — reload to apply it.",
    ),
    "pwa.insecure": (
        "L'installation est impossible en {protocol} : une PWA exige HTTPS. "
        "Passez par l'adresse publique de l'application plutôt que par l'IP du "
        "serveur.",
        "Installation is not possible over {protocol}: a PWA requires HTTPS. "
        "Use the application's public address rather than the server's IP.",
    ),
    # États du diagnostic d'installation. Le rapport complet est un outil de
    # développeur (console), mais cet état-ci ressort dans une notification
    # visible : il est donc traduit, contrairement au reste du rapport.
    "pwa.state_unchecked": ("non vérifié", "not checked"),
    "pwa.state_sso": (
        "intercepté par le SSO (HTML reçu au lieu de JSON)",
        "intercepted by the SSO (HTML received instead of JSON)",
    ),
    "pwa.state_unreadable": ("illisible ({error})", "unreadable ({error})"),
    "pwa.manifest_blocked": (
        "Manifeste inaccessible ({state}). Ces ressources sont pourtant "
        "publiques dans l'application : c'est donc le reverse proxy qui les "
        "intercepte. Il doit laisser passer /static/manifest.webmanifest, "
        "/sw.js et /static/icons/ sans authentifier.",
        "Manifest unreachable ({state}). These resources are public within the "
        "application, so the reverse proxy is intercepting them. It must let "
        "/static/manifest.webmanifest, /sw.js and /static/icons/ through "
        "without authenticating.",
    ),

    # --- Messages du serveur ------------------------------------------------
    "err.consultation_not_found": ("Consultation introuvable.", "Consultation not found."),
    "err.template_not_found": ("Gabarit introuvable.", "Template not found."),
    "err.template_exists": (
        "Un gabarit nommé « {name} » existe déjà.",
        "A template named “{name}” already exists.",
    ),
    "err.template_locked": (
        "Ce gabarit est protégé : il ne peut être ni modifié ni supprimé. "
        "Dupliquez-le pour en obtenir une copie modifiable.",
        "This template is protected: it cannot be edited or deleted. Duplicate "
        "it to obtain an editable copy.",
    ),
    "err.template_rights": (
        "Vous n'avez pas le droit de modifier ce gabarit : un gabarit partagé "
        "ne se réécrit que par un administrateur, un gabarit personnel que par "
        "son propriétaire.",
        "You do not have permission to modify this template: a shared template "
        "is only editable by an administrator, a personal one only by its owner.",
    ),
    "err.template_last": (
        "Impossible de supprimer le dernier gabarit : l'application en exige "
        "au moins un.",
        "Cannot delete the last template: the application requires at least "
        "one.",
    ),
    "err.audio_empty": ("Fichier audio vide.", "Empty audio file."),
    "err.chunk_empty": ("Fragment audio vide.", "Empty audio chunk."),
    "err.transcription": ("Erreur de transcription : {error}", "Transcription error: {error}"),
    "err.generation": ("Erreur de génération : {error}", "Generation error: {error}"),
    "err.recording_not_found": ("Enregistrement introuvable.", "Recording not found."),
    "err.retranscribe_no_audio": (
        "Aucun enregistrement conservé pour cette consultation : "
        "il n'y a rien à retranscrire.",
        "No recording kept for this consultation: there is nothing to re-transcribe.",
    ),
    "err.retranscribe_empty": (
        "La nouvelle transcription n'a rien produit. La transcription "
        "existante est conservée.",
        "The new transcription produced nothing. The existing transcript is kept.",
    ),
    "err.setting_rejected": (
        "Valeur refusée pour « {label} » : {value}",
        "Value rejected for “{label}”: {value}",
    ),
    "err.setting_number": (
        "« {label} » doit être un nombre.",
        "“{label}” must be a number.",
    ),
    "err.recording_gone": (
        "Le fichier audio de cet enregistrement n'est plus sur le disque.",
        "The audio file for this recording is no longer on disk.",
    ),

    "err.group_system": (
        "Ce groupe est livré avec l'application et ne peut pas être supprimé.",
        "This group ships with the application and cannot be deleted.",
    ),
    "err.group_exists": (
        "Un groupe porte déjà ce nom.",
        "A group with this name already exists.",
    ),
    "err.group_name_required": ("Le nom est obligatoire.", "The name is required."),
    "err.group_not_found": ("Groupe introuvable.", "Group not found."),
    "err.user_not_found": ("Compte introuvable.", "Account not found."),
    "err.pricing_duplicate": (
        "Un tarif existe déjà pour ce fournisseur/modèle/type/unité.",
        "A rate already exists for this provider/model/kind/unit.",
    ),
    "err.pricing_not_found": ("Tarif introuvable.", "Rate not found."),
    "err.backup_not_found": ("Sauvegarde introuvable.", "Backup not found."),
    "err.restart_required": (
        "Une restauration vient d'avoir lieu : redémarrez le conteneur "
        "ConsultAI avant de continuer.",
        "A restore just took place: restart the ConsultAI container before "
        "continuing.",
    ),
    "err.unknown_language": (
        "Langue inconnue : {language}",
        "Unknown language: {language}",
    ),
    "denied.unauthenticated": ("Non authentifié.", "Not authenticated."),
    "denied.account_disabled": (
        "Votre compte a été désactivé. Contactez l'administrateur.",
        "Your account has been disabled. Contact your administrator.",
    ),
    "denied.not_system_admin": (
        "Réservé aux administrateurs.",
        "Administrators only.",
    ),
    "denied.signup_closed": (
        "Le compte « {username} » n'est pas autorisé sur cette installation. "
        "L'inscription automatique est désactivée : un administrateur doit "
        "créer le compte.",
        "The account “{username}” is not authorized on this installation. "
        "Automatic sign-up is disabled: an administrator must create the "
        "account.",
    ),
    "denied.last_admin": (
        "Impossible : ce serait le dernier administrateur actif. "
        "Nommez-en un autre d'abord.",
        "Not possible: this would remove the last active administrator. "
        "Appoint another one first.",
    ),
    "denied.not_admin": (
        "Seuls les administrateurs peuvent modifier les gabarits.",
        "Only administrators may modify templates.",
    ),

    # --- Flux de connexion ---------------------------------------------------
    "auth.error_title": ("Connexion impossible", "Sign-in failed"),
    "auth.retry": ("Réessayer la connexion", "Try signing in again"),
    "auth.not_configured": (
        "La connexion n'est pas configurée sur cette installation : "
        "OIDC_PROVIDER_URL, OIDC_CLIENT_ID et OIDC_CLIENT_SECRET doivent être "
        "renseignés dans le fichier .env.",
        "Sign-in is not configured on this installation: OIDC_PROVIDER_URL, "
        "OIDC_CLIENT_ID and OIDC_CLIENT_SECRET must be set in the .env file.",
    ),
    "auth.provider_refused": (
        "Le fournisseur d'identité a refusé la connexion : {detail}",
        "The identity provider refused the sign-in: {detail}",
    ),

    # --- Page de connexion (rendue par /auth/login) -------------------------
    "auth.welcome_title": ("Connexion", "Sign in"),
    "auth.welcome_subtitle": (
        "Choisissez la durée de session pour cet appareil.",
        "Choose how long to stay signed in on this device.",
    ),
    "auth.option_short_title": ("Usage ponctuel", "Temporary use"),
    "auth.option_short_desc": (
        "Session de {heures} h d'inactivité. Pour un poste partagé ou public.",
        "Expires after {heures} h of inactivity. For a shared or public computer.",
    ),
    "auth.stay_logged_in": (
        "Rester connecté {jours} jours",
        "Keep me signed in for {jours} days",
    ),
    "auth.option_long_desc": (
        "Session de {jours} jours. Sur votre appareil personnel.",
        "Lasts {jours} days. On your personal device.",
    ),
    "auth.sign_in_with": ("Continuer avec {sso}", "Continue with {sso}"),
    "auth.session_note": (
        "La session expire après la durée choisie sans activité.",
        "The session expires after the chosen period of inactivity.",
    ),
    "auth.changelog": ("Nouveautés (7 derniers jours)", "What's new (last 7 days)"),
    "auth.changelog_empty": (
        "Aucune nouveauté cette semaine.",
        "Nothing new this week.",
    ),

    # --- Page 403 (rendue par le middleware) --------------------------------
    "denied.title": ("Accès refusé", "Access denied"),
    "denied.heading": ("403 — Accès refusé", "403 — Access denied"),
    "denied.footer": (
        "ConsultAI — accès contrôlé par {sso}.",
        "ConsultAI — access controlled by {sso}.",
    ),
}


def t(key: str, language: str = DEFAULT_LANGUAGE, /, **fields) -> str:
    """
    Texte pour la langue demandée, champs entre accolades remplis.

    Une clé absente est renvoyée telle quelle plutôt que de lever : un libellé
    qui s'affiche en clair est un défaut visible et réparable, une exception au
    milieu du rendu d'une page ne l'est pas.

    ``key`` et ``language`` sont **positionnels seulement** (le ``/``). Sans
    cela, un texte comportant un champ ``{language}`` ou ``{key}`` — il en
    existe — provoquait un ``TypeError`` : « got multiple values for argument
    'language' ». Le nom d'un champ de traduction ne doit pas pouvoir entrer en
    collision avec la signature de la fonction qui le remplit.
    """
    couple = _STRINGS.get(key)
    if couple is None:
        return key
    texte = couple[_INDEX[normalize(language)]]
    if not fields:
        return texte
    try:
        return texte.format(**fields)
    except (KeyError, IndexError):
        # Champ manquant : le texte brut vaut mieux qu'une page en erreur.
        return texte


def catalog(language: str = DEFAULT_LANGUAGE) -> Dict[str, str]:
    """
    Toutes les chaînes de la langue demandée, prêtes à être envoyées au
    navigateur.

    Le catalogue est servi en entier plutôt que filtré : il pèse quelques
    kilo-octets, il est inclus dans la page, et trier ce qui sert côté client
    de ce qui sert côté serveur créerait surtout des occasions d'oublier une
    clé.
    """
    index = _INDEX[normalize(language)]
    return {key: couple[index] for key, couple in _STRINGS.items()}


def missing_keys() -> Iterable[str]:
    """Clés dont une des deux langues est vide — utilisé par les tests."""
    for key, couple in _STRINGS.items():
        if not couple[0] or not couple[1]:
            # Une chaîne vide est légitime pour un « help » sans texte : on ne
            # signale que les entrées à moitié remplies.
            if bool(couple[0]) != bool(couple[1]):
                yield key
