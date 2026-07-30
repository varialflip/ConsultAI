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
* **Les champs de substitution** (``{{PATIENT}}``, ``{{DOSSIER}}``…), qui font
  partie du contrat des gabarits existants. Les traduire casserait les
  gabarits déjà écrits.
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
    },
    "en": {
        "google": "en-CA",
        "deepgram": "en-CA",
        "assemblyai": "en",
        "soniox": "en",
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
    "identity.language_saved": (
        "Langue changée. L'écran se recharge…",
        "Language changed. Reloading…",
    ),
    "identity.language_failed": (
        "Changement de langue impossible : {error}",
        "Could not change the language: {error}",
    ),
    "identity.logout": ("Se déconnecter", "Sign out"),
    "identity.pangolin_logout": ("Fermer la session Pangolin", "Close the Pangolin session"),
    "identity.logout_busy": (
        "Terminez ou arrêtez la dictée en cours avant de vous déconnecter.",
        "Finish or stop the current dictation before signing out.",
    ),
    "identity.logout_unconfigured": (
        "LOGOUT_OIDC_URL n'est pas configurée : aucune déconnexion possible.",
        "LOGOUT_OIDC_URL is not configured: signing out is not possible.",
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

    # --- Détails ------------------------------------------------------------
    "details.summary": ("Détails", "Details"),
    "details.hint": (
        "métadonnées, consigne ponctuelle, enregistrements",
        "metadata, one-off instruction, recordings",
    ),
    "meta.name": ("Nom du patient", "Patient name"),
    "meta.record": ("Numéro de dossier", "Record number"),
    "meta.date": ("Date de la consultation", "Consultation date"),
    "meta.reason": ("Raison de consultation", "Reason for consultation"),
    "meta.requester": ("Demande de", "Requested by"),
    "meta.accompanied": ("Accompagné de", "Accompanied by"),
    "meta.recognized_ph": ("Reconnu dans la dictée", "Recognized from the dictation"),
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
    "pane.clear": ("Vider", "Clear"),
    "pane.clear_title": ("Vider la transcription", "Clear the transcript"),
    "pane.generate": ("Mettre en forme", "Format"),
    "transcript.placeholder": (
        "Appuyez sur « Enregistrer » et dictez la consultation, ou collez ici "
        "un texte existant.\n\nAstuce : vous pouvez dicter en plusieurs fois, "
        "chaque nouvelle dictée s'ajoute à la suite.",
        "Press “Record” and dictate the consultation, or paste existing text "
        "here.\n\nTip: you can dictate in several passes — each new dictation "
        "is appended.",
    ),
    "note.preview": ("Aperçu", "Preview"),
    "note.write": ("Écrire", "Write"),
    "note.edit": ("Éditer", "Edit"),
    "note.copy": ("Copier", "Copy"),
    "note.empty": (
        "La note structurée apparaîtra ici après la mise en forme.",
        "The structured note will appear here after formatting.",
    ),
    "note.engine_dictation": ("dictée {engine}", "dictation {engine}"),
    "note.engine_note": ("note {engine}", "note {engine}"),
    "note.engine_stt_title": ("Transcription : {engine}", "Transcription: {engine}"),
    "note.engine_llm_title": ("Mise en forme : {engine}", "Formatting: {engine}"),

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
    "pdf.footer_patient": ("Patient : {patient}. ", "Patient: {patient}. "),
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

    # --- Dictée : micro et erreurs matérielles -----------------------------
    "mic.insecure": (
        "Le micro n'est pas accessible. L'application doit être servie en "
        "HTTPS (c'est le cas via Pangolin) pour que le navigateur autorise "
        "l'enregistrement.",
        "The microphone is not available. The application must be served over "
        "HTTPS (which it is through Pangolin) for the browser to allow "
        "recording.",
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
    "recovery.title": ("Dictée interrompue retrouvée", "Interrupted dictation found"),
    "recovery.resume": ("Reprendre et transcrire", "Resume and transcribe"),
    "recovery.download": ("Télécharger l'audio", "Download the audio"),
    "recovery.discard": ("Supprimer", "Delete"),
    "recovery.unnamed": ("Consultation sans nom", "Unnamed consultation"),
    "recovery.received": (
        "{duration} reçues par le serveur",
        "{duration} received by the server",
    ),
    "recovery.not_received": ("non reçue par le serveur", "not received by the server"),
    "recovery.nothing_local": (
        "Aucun fragment local à télécharger.",
        "No local chunk to download.",
    ),
    "recovery.confirm_discard": (
        "Supprimer définitivement cet enregistrement ?",
        "Permanently delete this recording?",
    ),
    "recovery.delete_failed": (
        "Suppression côté serveur impossible :",
        "Server-side deletion failed:",
    ),
    "recovery.deleted": ("Enregistrement supprimé.", "Recording deleted."),
    "recovery.resuming": (
        "Reprise de la dictée interrompue…",
        "Resuming the interrupted dictation…",
    ),
    "recovery.resumed": (
        "Dictée récupérée — {count} tranche(s) transcrite(s).",
        "Dictation recovered — {count} segment(s) transcribed.",
    ),
    "recovery.resume_failed": ("Reprise impossible : {error}", "Resume failed: {error}"),
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
        "Transcription en cours ({size} Mo)… Cela peut prendre une minute.",
        "Transcribing ({size} MB)… This may take a minute.",
    ),
    "transcribe.busy_short": ("Transcription…", "Transcribing…"),
    "transcribe.done": (
        "Transcription terminée ({count} caractères, confiance {confidence} %).",
        "Transcription complete ({count} characters, {confidence} % confidence).",
    ),
    "transcript.characters": ("{count} caractères", "{count} characters"),
    "transcript.audio": ("{duration} d'audio", "{duration} of audio"),
    "transcript.confirm_clear": ("Vider la transcription ?", "Clear the transcript?"),
    "generate.empty": (
        "La transcription est vide : dictez ou collez un texte d'abord.",
        "The transcript is empty: dictate or paste text first.",
    ),
    "generate.no_template": (
        "Sélectionnez un gabarit de consultation.",
        "Select a consultation template.",
    ),
    "generate.busy": (
        "Mise en forme avec le gabarit « {name} »…",
        "Formatting with the “{name}” template…",
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
    "tpl.readonly": ("Lecture seule", "Read-only"),
    "tpl.new": ("Nouveau", "New"),
    "tpl.back": ("Retour à la liste", "Back to the list"),
    "tpl.list_hint": (
        "Touchez un gabarit pour le modifier.",
        "Tap a template to edit it.",
    ),
    "tpl.name": ("Nom du gabarit", "Template name"),
    "tpl.name_ph": ("Ex. Consultation externe", "e.g. Outpatient consultation"),
    "tpl.order": ("Ordre", "Order"),
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
    "drafts.loading": ("Chargement…", "Loading…"),
    "drafts.none": ("Aucun brouillon enregistré.", "No saved draft."),
    "drafts.unnamed_patient": ("Patient non identifié", "Unidentified patient"),
    "drafts.no_reason": (
        "Raison de consultation non précisée",
        "Reason for consultation not specified",
    ),
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

    # --- Panneau d'administration : chrome ---------------------------------
    "admin.title": ("Réglages", "Settings"),
    "admin.badge": ("Administrateur", "Administrator"),
    "admin.intro": (
        "Ces réglages sont enregistrés en base et <strong>surchargent le "
        "fichier <code>.env</code></strong> : ils prennent effet "
        "immédiatement, sans reconstruire l'image. Vider un champ le remet à "
        "la valeur du <code>.env</code>.",
        "These settings are stored in the database and <strong>override the "
        "<code>.env</code> file</strong>: they take effect immediately, with "
        "no image rebuild. Clearing a field resets it to the "
        "<code>.env</code> value.",
    ),
    "admin.intro_access": (
        "Ce qui gouverne l'accès — usagers autorisés, plages de proxy de "
        "confiance — reste dans le <code>.env</code>, hors d'atteinte du "
        "navigateur.",
        "What governs access — authorized users, trusted proxy ranges — stays "
        "in <code>.env</code>, out of reach of the browser.",
    ),
    "admin.loading": ("Chargement…", "Loading…"),
    "admin.save": ("Enregistrer", "Save"),
    "admin.list_models": ("Modèles disponibles", "Available models"),
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
    "admin.model_missing": (
        "Attention : « {model} » ne figure pas dans les modèles accessibles à "
        "cette clé. La mise en forme échouera.",
        "Warning: “{model}” is not among the models available to this key. "
        "Formatting will fail.",
    ),

    # --- Panneau d'administration : groupes de réglages --------------------
    "group.interface": ("Interface", "Interface"),
    "group.stt": ("Reconnaissance vocale", "Speech recognition"),
    "group.llm": ("Modèle de langage", "Language model"),
    "group.prompts": ("Consignes", "Instructions"),

    # --- Panneau d'administration : réglages -------------------------------
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
        "Les services facturent à la durée d'audio. Seule la copie envoyée "
        "est raccourcie : l'enregistrement conservé avec le brouillon reste "
        "intact, et la durée affichée reste celle de la dictée réelle.",
        "Services bill by audio duration. Only the copy that is sent is "
        "shortened: the recording kept with the draft stays intact, and the "
        "displayed duration remains that of the actual dictation.",
    ),
    "set.stt_silence_keep_seconds.label": (
        "Pause conservée (secondes)",
        "Pause kept (seconds)",
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
    "set.llm_provider.label": ("Fournisseur", "Provider"),
    "set.llm_provider.help": ("", ""),
    "set.llm_model.label": ("Modèle", "Model"),
    "set.llm_model.help": (
        "Le bouton « Modèles disponibles » interroge le fournisseur avec la "
        "clé configurée et affiche ce à quoi ce compte a réellement droit.",
        "The “Available models” button queries the provider with the "
        "configured key and shows what this account actually has access to.",
    ),
    "set.llm_model_fast.label": (
        "Modèle rapide (métadonnées)",
        "Fast model (metadata)",
    ),
    "set.llm_model_fast.help": (
        "Utilisé pour la seule relecture des métadonnées, une tâche triviale "
        "payée au jeton. Laisser vide pour employer le modèle principal.",
        "Used only to re-read metadata, a trivial task paid by the token. "
        "Leave empty to use the main model.",
    ),
    "set.llm_temperature.label": ("Température", "Temperature"),
    "set.llm_temperature.help": (
        "0 = déterministe. Au-delà de 0,4 le modèle commence à broder, ce qui "
        "n'a pas sa place dans une note clinique. Les modèles les plus "
        "récents ne l'acceptent plus : le réglage est alors ignoré, la note "
        "est produite quand même.",
        "0 = deterministic. Above 0.4 the model starts embellishing, which "
        "has no place in a clinical note. The most recent models no longer "
        "accept it: the setting is then ignored and the note is produced "
        "anyway.",
    ),
    "set.gemini_api_key.label": ("Clé API Google Gemini", "Google Gemini API key"),
    "set.gemini_api_key.help": ("", ""),
    "set.anthropic_api_key.label": ("Clé API Anthropic", "Anthropic API key"),
    "set.anthropic_api_key.help": ("", ""),
    "set.openai_api_key.label": ("Clé API OpenAI", "OpenAI API key"),
    "set.openai_api_key.help": ("", ""),
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
    "choice.on": ("Activé", "Enabled"),
    "choice.off": ("Désactivé", "Disabled"),

    # --- Chargement et diagnostic ------------------------------------------
    "app.load_failed": ("Chargement impossible : {error}", "Loading failed: {error}"),
    "app.busy_default": ("Traitement en cours…", "Working…"),
    "app.dont_close": ("Ne fermez pas cette fenêtre.", "Do not close this window."),
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
        "Passez par l'adresse Pangolin plutôt que par l'IP du NAS.",
        "Installation is not possible over {protocol}: a PWA requires HTTPS. "
        "Use the Pangolin address rather than the NAS IP.",
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
        "Manifeste inaccessible ({state}). Autorisez "
        "/static/manifest.webmanifest, /sw.js et /static/icons/ sans "
        "authentification dans Pangolin.",
        "Manifest unreachable ({state}). Allow "
        "/static/manifest.webmanifest, /sw.js and /static/icons/ without "
        "authentication in Pangolin.",
    ),

    # --- Messages du serveur ------------------------------------------------
    "err.consultation_not_found": ("Consultation introuvable.", "Consultation not found."),
    "err.template_not_found": ("Gabarit introuvable.", "Template not found."),
    "err.template_exists": (
        "Un gabarit nommé « {name} » existe déjà.",
        "A template named “{name}” already exists.",
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

    # --- Refus d'accès ------------------------------------------------------
    # Ces messages nomment des réglages du .env (TRUSTED_PROXIES,
    # AUTHORIZED_USERS) : les noms de variables ne se traduisent pas, c'est ce
    # qu'il faut aller chercher dans le fichier.
    "denied.proxy": (
        "Accès refusé : requête reçue en dehors du proxy de confiance. "
        "Passez par Pangolin ou ajustez TRUSTED_PROXIES.",
        "Access denied: request received outside the trusted proxy. Go through "
        "Pangolin or adjust TRUSTED_PROXIES.",
    ),
    "denied.no_identity": (
        "Accès refusé : aucune identité transmise par le SSO. En-têtes "
        "attendus : {headers}.",
        "Access denied: no identity passed by the SSO. Expected headers: "
        "{headers}.",
    ),
    "denied.not_authorized": (
        "Accès refusé : le compte « {username} » ne figure pas dans "
        "AUTHORIZED_USERS. Contactez l'administrateur.",
        "Access denied: the account “{username}” is not listed in "
        "AUTHORIZED_USERS. Contact your administrator.",
    ),
    "err.unknown_language": (
        "Langue inconnue : {language}",
        "Unknown language: {language}",
    ),
    "denied.unauthenticated": ("Non authentifié.", "Not authenticated."),
    "denied.not_admin": (
        "Seuls les administrateurs peuvent modifier les gabarits.",
        "Only administrators may modify templates.",
    ),

    # --- Page 403 (rendue par le middleware) --------------------------------
    "denied.title": ("Accès refusé", "Access denied"),
    "denied.heading": ("403 — Accès refusé", "403 — Access denied"),
    "denied.footer": (
        "ConsultAI — accès contrôlé par Pangolin SSO.",
        "ConsultAI — access controlled by Pangolin SSO.",
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
