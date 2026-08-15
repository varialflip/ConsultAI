"""
stt.py — Transcription audio via Google Cloud Speech-to-Text (fr-CA).
======================================================================

CHAÎNE DE TRAITEMENT
--------------------
    navigateur (MediaRecorder)  →  ffmpeg  →  OGG/Opus mono 48 kHz  →  Google STT

Pourquoi transcoder systématiquement ?
  * Chrome / Android produisent ``audio/webm;codecs=opus`` — accepté par Google.
  * Safari / iOS (l'iPad du médecin) produisent ``audio/mp4`` (AAC) que
    Google Speech-to-Text v1 NE SAIT PAS décoder. Sans transcodage, toute
    dictée faite sur iPhone/iPad échouerait.
  * Opus à 24 kb/s divise la taille par 5 à 10, ce qui permet de rester sous
    la limite de 10 Mo des requêtes « inline » pour ~50 minutes de dictée.

SIX FOURNISSEURS
----------------
Google Speech-to-Text, Deepgram, AssemblyAI, Soniox, Cohere Transcribe ou
Mistral Voxtral, au choix depuis le panneau d'administration. Tout ce qui
précède l'envoi — transcodage, découpage en tranches, lexique d'adaptation —
leur est commun ; seule la dernière étape change. Voir ``transcribe_payload``.

AssemblyAI est le seul des six à proposer un modèle spécialisé en
terminologie clinique qui couvre le français (« medical-v1 »). Cohere et
Mistral n'offrent aucune adaptation au vocabulaire connue à ce jour.

CHOIX DU MODÈLE (Google)
------------------------
``latest_long`` est le modèle adapté à la parole continue et longue en
français. Le modèle ``medical_conversation`` / ``medical_dictation`` de Google
est **anglophone uniquement** : il n'est pas utilisable ici. La précision
médicale est donc obtenue par l'adaptation du modèle (« phrase hints »)
ci-dessous, complétée par la correction sémantique faite par Gemini.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from app import i18n, preferences, runtime_config
from app.config import settings

logger = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    """Erreur métier de transcription, avec un message affichable à l'écran."""


class _EndpointHttpError(Exception):
    """
    Erreur HTTP d'un point de terminaison OpenAI-compatible.

    Porte le code de statut et le détail renvoyés par le serveur, pour que
    l'appelant puisse décider du comportement (repli sur un autre modèle,
    erreur de clé, etc.) sans avoir à interroger le transport.
    """

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail


# ===========================================================================
# LEXIQUE — adaptation du modèle de reconnaissance
# ===========================================================================
# Ces expressions sont transmises à Google en « speech contexts » : elles
# augmentent fortement la probabilité que le moteur les reconnaisse. On y met
# ce qu'un modèle généraliste français rate systématiquement : les acronymes
# du réseau de la santé québécois, les échelles gériatriques et les molécules.
#
# Limites de l'API : 5 000 expressions max, 100 caractères par expression.
# ===========================================================================

RESEAU_SANTE_QC = [
    "CHSLD", "CLSC", "GMF", "GMF-U", "CISSS", "CIUSSS", "CHU", "CHUM", "CHUS",
    "RPA", "résidence privée pour aînés", "ressource intermédiaire", "RI-RTF",
    "SAD", "soutien à domicile", "SAPA", "soins à domicile",
    "centre de jour", "hôpital de jour", "popote roulante",
    "RAMQ", "SAAQ", "CNESST", "Curateur public", "mandat de protection",
    "DSQ", "dossier santé Québec", "INESSS", "Info-Santé",
    "guichet d'accès", "mécanisme d'accès à l'hébergement", "OEMC",
    "PRISMA-7", "ISO-SMAF", "SMAF", "niveau de soins", "aide médicale à mourir",
    "urgence", "civière", "congé de l'hôpital", "unité de courte durée gériatrique",
    "UCDG", "URFI", "unité transitoire de réadaptation fonctionnelle", "UTRF",
]

SYNDROMES_GERIATRIQUES = [
    "syndrome gériatrique", "évaluation gériatrique globale", "fragilité",
    "sarcopénie", "polypharmacie", "déprescription", "iatrogénie",
    "polymédication", "cascade médicamenteuse", "critères de Beers",
    "STOPP START", "charge anticholinergique",
    "délirium", "état confusionnel aigu", "confusion",
    "trouble neurocognitif majeur", "trouble neurocognitif léger",
    "maladie d'Alzheimer", "démence vasculaire", "démence à corps de Lewy",
    "dégénérescence lobaire frontotemporale", "démence mixte",
    "hydrocéphalie à pression normale", "paralysie supranucléaire progressive",
    "maladie de Parkinson", "parkinsonisme", "tremblement essentiel",
    "chute", "chutes à répétition", "peur de tomber", "syndrome post-chute",
    "trouble de la marche", "instabilité posturale", "hypotension orthostatique",
    "dénutrition", "perte de poids involontaire", "dysphagie", "cachexie",
    "plaie de pression", "ulcère de pression", "escarre",
    "incontinence urinaire", "incontinence fécale", "vessie hyperactive",
    "constipation", "fécalome", "rétention urinaire",
    "presbyacousie", "presbytie", "dégénérescence maculaire", "cataracte",
    "ostéoporose", "fracture de fragilisation", "fracture de hanche",
    "apnée du sommeil", "insomnie", "trouble comportemental en sommeil paradoxal",
    "douleur chronique", "épuisement du proche aidant", "maltraitance",
    "isolement social", "diogène", "autonégligence",
]

AUTONOMIE_ET_ECHELLES = [
    "AVQ", "activités de la vie quotidienne",
    "AVD", "AIVQ", "activités instrumentales de la vie quotidienne",
    "IADL", "ADL", "autonomie fonctionnelle", "perte d'autonomie",
    "MoCA", "MMSE", "mini-mental", "test de l'horloge", "dessin de l'horloge",
    "rappel des cinq mots", "test des cinq mots", "fluence verbale",
    "MoCA 5-minutes", "test de Folstein", "échelle de Reisberg",
    "GDS", "échelle de dépression gériatrique", "PHQ-9", "NPI",
    "inventaire neuropsychiatrique", "échelle de Cornell",
    "CAM", "confusion assessment method", "4AT",
    "TUG", "timed up and go", "test de Tinetti", "vitesse de marche",
    "test de l'appui unipodal", "SPPB", "échelle de Braden", "échelle de Morse",
    "échelle clinique de fragilité", "échelle de Rockwood",
    "grille AGGIR", "MNA", "mini nutritional assessment",
]

SCPD_ET_COGNITION = [
    "SCPD", "symptômes comportementaux et psychologiques de la démence",
    "anosognosie", "hétéro-anamnèse", "proche aidant", "aidant naturel",
    "apathie", "désinhibition", "errance", "agitation", "agressivité",
    "opposition aux soins", "idées délirantes", "délire de vol",
    "hallucinations visuelles", "syndrome crépusculaire", "inversion du cycle",
    "mémoire épisodique", "mémoire de travail", "fonctions exécutives",
    "manque du mot", "paraphasie", "apraxie", "agnosie", "aphasie",
    "fonctions visuospatiales", "désorientation temporospatiale",
    "aptitude à consentir aux soins", "inaptitude", "régime de protection",
]

MEDICAMENTS_COURANTS = [
    "donépézil", "Aricept", "rivastigmine", "Exelon", "galantamine", "Reminyl",
    "mémantine", "Ebixa", "inhibiteur de la cholinestérase",
    "quétiapine", "Seroquel", "rispéridone", "Risperdal", "olanzapine",
    "halopéridol", "trazodone", "mirtazapine", "citalopram", "escitalopram",
    "sertraline", "venlafaxine", "duloxétine", "bupropion",
    "lorazépam", "Ativan", "oxazépam", "clonazépam", "zopiclone", "témazépam",
    "gabapentine", "prégabaline", "amitriptyline", "nortriptyline",
    "lévodopa", "carbidopa", "Sinemet", "pramipexole", "rotigotine",
    "warfarine", "apixaban", "Eliquis", "rivaroxaban", "Xarelto", "dabigatran",
    "métoprolol", "bisoprolol", "périndopril", "ramipril", "amlodipine",
    "furosémide", "hydrochlorothiazide", "spironolactone",
    "metformine", "gliclazide", "empagliflozine", "insuline",
    "atorvastatine", "rosuvastatine", "pantoprazole", "oméprazole",
    "lévothyroxine", "Synthroid", "alendronate", "dénosumab", "Prolia",
    "acétaminophène", "hydromorphone", "morphine", "tramadol", "codéine",
    "tamsulosine", "finastéride", "oxybutynine", "solifénacine", "mirabégron",
    "vitamine D", "calcium", "vitamine B12", "acide folique",
    "dompéridone", "métoclopramide", "docusate", "lactulose", "PEG 3350",
]

INVESTIGATIONS = [
    "formule sanguine complète", "FSC", "ionogramme", "créatinine",
    "clairance de la créatinine", "débit de filtration glomérulaire", "DFG",
    "TSH", "vitamine B12", "acide folique", "albumine", "calcium corrigé",
    "bilan hépatique", "glycémie", "hémoglobine glyquée", "HbA1c",
    "analyse d'urine", "culture d'urine", "SMU-DCA",
    "tomodensitométrie cérébrale", "TDM cérébrale", "scan cérébral",
    "résonance magnétique cérébrale", "IRM cérébrale", "TEP-scan",
    "DaTscan", "SPECT", "électrocardiogramme", "ECG", "Holter",
    "densitométrie osseuse", "ostéodensitométrie",
    "évaluation neuropsychologique", "évaluation en ergothérapie",
    "évaluation en physiothérapie", "évaluation en nutrition",
]

EXPRESSIONS_DICTEE = [
    "motif de consultation", "histoire de la maladie actuelle", "HMA",
    "antécédents personnels", "antécédents familiaux", "revue des systèmes",
    "examen physique", "signes vitaux", "impression diagnostique",
    "diagnostic différentiel", "plan de traitement", "conduite à tenir",
    "à revoir dans", "suivi en externe", "congé", "référence en",
    "milieu de vie", "réseau de soutien", "sans particularité",
    "non contributoire", "dans les limites de la normale",
]

# Lexique global : dédoublonné, préservant l'ordre d'insertion.
DEFAULT_PHRASE_HINTS: List[str] = list(
    dict.fromkeys(
        RESEAU_SANTE_QC
        + SYNDROMES_GERIATRIQUES
        + AUTONOMIE_ET_ECHELLES
        + SCPD_ET_COGNITION
        + MEDICAMENTS_COURANTS
        + INVESTIGATIONS
        + EXPRESSIONS_DICTEE
    )
)

# Limites imposées par l'API Speech-to-Text v1.
_MAX_PHRASES = 5000
_MAX_PHRASE_CHARS = 100
# Au-delà, l'appel « inline » dépasse la taille de requête autorisée.
_INLINE_LIMIT_BYTES = 10 * 1024 * 1024


def build_phrase_hints(extra: Optional[str] = None) -> List[str]:
    """
    Combine le lexique global et le vocabulaire propre au gabarit choisi.

    ``extra`` accepte une liste séparée par des virgules ou des sauts de ligne
    (champ « Vocabulaire » de l'éditeur de gabarits).

    Le lexique global est francophone : comme pour ``_termes_prioritaires``, il
    n'est pas envoyé en mode anglais, où il ne ferait que biaiser le moteur
    vers des mots absents de la dictée.
    """
    phrases = (
        list(DEFAULT_PHRASE_HINTS)
        if i18n.uses_french_lexicon(preferences.document_language())
        else []
    )
    if extra:
        for chunk in extra.replace("\n", ",").split(","):
            phrase = chunk.strip()
            if phrase:
                phrases.append(phrase)

    cleaned, seen = [], set()
    for phrase in phrases:
        phrase = phrase.strip()[:_MAX_PHRASE_CHARS]
        key = phrase.lower()
        if phrase and key not in seen:
            seen.add(key)
            cleaned.append(phrase)
    return cleaned[:_MAX_PHRASES]


# ===========================================================================
# Prétraitement audio (ffmpeg)
# ===========================================================================
@dataclass
class AudioPayload:
    """Audio normalisé, prêt à être envoyé à Google."""

    content: bytes
    encoding_name: str  # nom de l'enum RecognitionConfig.AudioEncoding
    sample_rate: int
    #: Durée **réelle** de l'audio d'origine. C'est elle qui fait avancer le
    #: curseur de découpage et qui alimente ``audio_seconds`` : elle ne doit
    #: jamais refléter un raccourcissement (voir ``sent_seconds``).
    duration_seconds: float
    transcoded: bool
    # Une tranche de dictée peut légitimement ne contenir aucune parole (le
    # médecin réfléchit, examine le patient) : ce n'est alors pas une erreur.
    allow_silence: bool = False
    #: Durée de ``content`` lorsqu'elle diffère de la durée réelle — cas du
    #: retrait des silences. ``None`` signifie « identique ». C'est cette
    #: durée-là qui est facturée et qui décide du mode de reconnaissance.
    #: Volontairement ``Optional`` et non 0 : une tranche sans aucune parole
    #: se réduit légitimement à zéro seconde, ce qu'un 0-sentinelle rendrait
    #: indistinguable de « pas de filtrage ».
    sent_seconds: Optional[float] = None

    @property
    def effective_seconds(self) -> float:
        """Durée de ce qui part réellement chez le fournisseur."""
        return self.duration_seconds if self.sent_seconds is None else self.sent_seconds


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_duration(path: str) -> float:
    """
    Durée de l'audio via ffprobe ; 0 si indéterminable (non bloquant).

    Les WebM produits par MediaRecorder (muxeur « live ») n'écrivent aucune
    durée dans leurs métadonnées : ffprobe renvoie alors « N/A » au niveau
    format ET stream, alors que le fichier se décode parfaitement. On retombe
    sur un décodage complet (``ffmpeg … -f null -``) pour lire la dernière
    horloge ``time=`` — quelques secondes pour un fichier long, uniquement
    quand la durée manque.
    """
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        pass
    return _probe_duration_decodage(path)


def _probe_duration_decodage(path: str) -> float:
    """Durée mesurée en décodant l'audio jusqu'au bout (repli WebM « live »)."""
    if not shutil.which("ffmpeg"):
        return 0.0
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.SubprocessError:
        return 0.0
    if result.returncode != 0:
        return 0.0
    horloges = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not horloges:
        return 0.0
    heures, minutes, secondes = horloges[-1]
    try:
        return int(heures) * 3600 + int(minutes) * 60 + float(secondes)
    except ValueError:
        return 0.0


def prepare_audio(raw: bytes, content_type: str = "") -> AudioPayload:
    """
    Normalise n'importe quel enregistrement du navigateur en OGG/Opus mono.

    Si ffmpeg est absent de l'image (ne devrait pas arriver), on tente un envoi
    direct lorsque le format est déjà compatible, et on échoue explicitement
    sinon plutôt que de laisser Google renvoyer une erreur incompréhensible.
    """
    if not raw:
        raise TranscriptionError("Aucune donnée audio reçue.")

    if len(raw) > settings.max_audio_bytes:
        raise TranscriptionError(
            f"Fichier audio trop volumineux ({len(raw) / 1048576:.1f} Mo). "
            f"Limite : {settings.max_audio_mb} Mo."
        )

    if not _ffmpeg_available():
        logger.error("ffmpeg introuvable — transcodage impossible")
        lowered = (content_type or "").lower()
        if "webm" in lowered:
            return AudioPayload(raw, "WEBM_OPUS", 48000, 0, False)
        if "ogg" in lowered:
            return AudioPayload(raw, "OGG_OPUS", 48000, 0, False)
        raise TranscriptionError(
            "ffmpeg est absent du conteneur et le format audio reçu "
            f"({content_type or 'inconnu'}) n'est pas lisible directement par "
            "Google. Reconstruisez l'image Docker."
        )

    workdir = tempfile.mkdtemp(prefix="consultai-audio-")
    src = os.path.join(workdir, "source")
    dst = os.path.join(workdir, "normalise.ogg")
    try:
        with open(src, "wb") as handle:
            handle.write(raw)

        # On mesure sur la sortie normalisée, jamais sur la source : un WebM
        # produit par MediaRecorder n'annonce aucune durée dans son entête, et
        # ffprobe y renvoie 0. Cette valeur nulle désactivait silencieusement
        # le retrait des silences sur les fichiers importés et faussait
        # audio_seconds — mesuré sur un enregistrement réel.
        duration = 0.0

        # -ac 1        : mono (la reconnaissance ne tire aucun bénéfice du stéréo)
        # -ar 48000    : Opus fonctionne nativement à 48 kHz ; on annonce la même
        #                valeur à Google (sample_rate_hertz obligatoire en OGG_OPUS)
        # -b:a 24k     : largement suffisant pour de la voix, ~10 Mo pour 55 min
        # -application voip : profil optimisé pour la parole
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", src,
            "-vn", "-map_metadata", "-1",
            "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-f", "ogg", "-y", dst,
        ]
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=900)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace")[-800:]
            logger.error("Échec du transcodage ffmpeg : %s", stderr)
            raise TranscriptionError(
                "Le fichier audio n'a pas pu être décodé. Format reçu : "
                f"{content_type or 'inconnu'}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError("Le transcodage audio a dépassé le délai maximal.") from exc

        with open(dst, "rb") as handle:
            content = handle.read()
        duration = probe_duration(dst) or probe_duration(src)

        logger.info(
            "Audio normalisé : %.1f Mo → %.1f Mo (%s s, source %s)",
            len(raw) / 1048576, len(content) / 1048576, round(duration, 1) or "?",
            content_type or "?",
        )
        payload = AudioPayload(content, "OGG_OPUS", 48000, duration, True)
        return _apply_silence_trim(payload, dst)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# Client Google Speech-to-Text
# ===========================================================================
_speech_client = None


def get_speech_client():
    """Client Speech-to-Text mis en cache (l'instanciation coûte cher)."""
    global _speech_client
    if _speech_client is not None:
        return _speech_client

    try:
        from google.cloud import speech
    except ImportError as exc:  # pragma: no cover
        raise TranscriptionError(
            "La bibliothèque google-cloud-speech n'est pas installée."
        ) from exc

    client_options = None
    if settings.stt_api_endpoint:
        # Point de terminaison régional, ex. « eu-speech.googleapis.com ».
        from google.api_core.client_options import ClientOptions

        client_options = ClientOptions(api_endpoint=settings.stt_api_endpoint)

    try:
        _speech_client = speech.SpeechClient(client_options=client_options)
    except Exception as exc:
        raise TranscriptionError(
            "Impossible de s'authentifier auprès de Google Cloud. Vérifiez "
            "GOOGLE_APPLICATION_CREDENTIALS et le montage du dossier ./secrets. "
            f"({exc})"
        ) from exc
    return _speech_client


def _upload_to_gcs(content: bytes) -> str:
    """Dépose l'audio dans le bucket configuré et retourne son URI gs://."""
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(settings.stt_gcs_bucket)
    blob_name = f"consultai/{uuid.uuid4().hex}.ogg"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(content, content_type="audio/ogg")
    logger.info("Audio téléversé vers gs://%s/%s", settings.stt_gcs_bucket, blob_name)
    return f"gs://{settings.stt_gcs_bucket}/{blob_name}"


def _delete_from_gcs(uri: str) -> None:
    """Supprime l'objet temporaire — les données de santé ne doivent pas traîner."""
    try:
        from google.cloud import storage

        _, _, rest = uri.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        storage.Client().bucket(bucket_name).blob(blob_name).delete()
        logger.info("Objet GCS temporaire supprimé : %s", uri)
    except Exception as exc:  # ne doit jamais faire échouer la transcription
        logger.warning("Suppression de %s impossible : %s", uri, exc)


def transcribe(
    raw_audio: bytes,
    content_type: str = "",
    extra_phrase_hints: Optional[str] = None,
    boost: float = 15.0,
) -> dict:
    """
    Transcrit un enregistrement complet (import de fichier, dictée en un bloc).

    Retourne ``{"transcript", "confidence", "duration_seconds", "segments"}``.
    """
    return transcribe_payload(prepare_audio(raw_audio, content_type), extra_phrase_hints, boost)


# Au-delà de cette durée, la reconnaissance synchrone est refusée par Google.
# On garde une marge : la durée mesurée par ffprobe et celle calculée par
# Google diffèrent de quelques dixièmes de seconde.
_SYNC_LIMIT_SECONDS = 55.0

#: En deçà, une tranche filtrée ne contient plus que la pause conservée :
#: aucune parole, donc aucun appel à facturer.
_MIN_SPEECH_SECONDS = 0.7


def transcribe_payload(
    payload: AudioPayload,
    extra_phrase_hints: Optional[str] = None,
    boost: float = 15.0,
) -> dict:
    """
    Envoie un audio déjà normalisé au service configuré.

    Le choix du fournisseur se fait ici et nulle part ailleurs : tout ce qui
    précède — transcodage, découpage en tranches, lexique — est commun, et
    tout ce qui suit reçoit le même dictionnaire de résultat.
    """
    # Après retrait des silences, une tranche peut ne plus contenir que la
    # pause conservée : il n'y a rien à reconnaître, et l'appel serait
    # facturé pour rien. Réservé aux tranches de dictée — sur un fichier
    # importé, l'absence de parole reste une erreur à signaler.
    if payload.allow_silence and payload.effective_seconds < _MIN_SPEECH_SECONDS:
        logger.info(
            "Tranche de %.1f s sans parole après retrait des silences : aucun appel au service",
            payload.duration_seconds,
        )
        return {
            "transcript": "",
            "confidence": 0.0,
            "duration_seconds": int(round(payload.duration_seconds)),
            "segments": 0,
            "provider": "",
            "model": "",
        }

    provider = runtime_config.value("stt_provider")
    if provider == "deepgram":
        return _transcribe_deepgram(payload, extra_phrase_hints)
    if provider == "assemblyai":
        return _transcribe_assemblyai(payload, extra_phrase_hints)
    if provider == "soniox":
        return _transcribe_soniox(payload, extra_phrase_hints)
    if provider == "cohere":
        return _transcribe_cohere(payload, extra_phrase_hints)
    if provider == "mistral":
        return _transcribe_mistral(payload, extra_phrase_hints)
    if provider == "openai":
        return _transcribe_openai(payload, extra_phrase_hints)
    if provider == "custom":
        return _transcribe_custom(payload, extra_phrase_hints)
    return _transcribe_google(payload, extra_phrase_hints, boost)


def _transcribe_google(
    payload: AudioPayload,
    extra_phrase_hints: Optional[str] = None,
    boost: float = 15.0,
) -> dict:
    """
    Google Speech-to-Text v1.

    Deux modes selon la durée. En dessous d'une minute — le cas d'une tranche
    de dictée — la reconnaissance synchrone répond en deux ou trois secondes,
    ce qui permet d'afficher le texte pendant que le médecin parle encore.
    Au-dessus, seule ``long_running_recognize`` est acceptée par l'API.
    """
    from google.cloud import speech

    client = get_speech_client()
    phrases = build_phrase_hints(extra_phrase_hints)

    config_kwargs = dict(
        encoding=getattr(speech.RecognitionConfig.AudioEncoding, payload.encoding_name),
        sample_rate_hertz=payload.sample_rate,
        audio_channel_count=1,
        language_code=runtime_config.stt_language("google"),
        enable_automatic_punctuation=True,
        profanity_filter=False,
        max_alternatives=1,
        model=settings.stt_model,
        use_enhanced=settings.stt_use_enhanced,
        # Adaptation du modèle : c'est ce qui fait la différence entre
        # « CHSLD » et « chez elle de » sur une dictée québécoise.
        speech_contexts=[speech.SpeechContext(phrases=phrases, boost=boost)],
    )
    config = speech.RecognitionConfig(**config_kwargs)

    # --- Choix du mode de transmission ------------------------------------
    # C'est la durée réellement envoyée qui compte : après retrait des
    # silences, une tranche de 45 s peut tenir dans la limite synchrone.
    duration = payload.duration_seconds or 0.0
    sent = payload.effective_seconds or 0.0
    synchronous = 0 < sent <= _SYNC_LIMIT_SECONDS and len(payload.content) <= _INLINE_LIMIT_BYTES

    gcs_uri: Optional[str] = None
    if len(payload.content) > _INLINE_LIMIT_BYTES:
        if not settings.stt_gcs_bucket:
            raise TranscriptionError(
                f"Enregistrement trop long pour un envoi direct "
                f"({len(payload.content) / 1048576:.1f} Mo, limite 10 Mo). "
                "Configurez STT_GCS_BUCKET ou découpez la dictée en plusieurs parties."
            )
        gcs_uri = _upload_to_gcs(payload.content)
        audio = speech.RecognitionAudio(uri=gcs_uri)
    else:
        audio = speech.RecognitionAudio(content=payload.content)

    # Délai généreux : Google traite environ 1 minute d'audio en quelques
    # secondes, mais un NAS derrière une connexion lente doit d'abord téléverser.
    timeout = max(300, int(sent * 3))

    try:
        logger.info(
            "Envoi à Google STT (%s) : %.2f Mo, %s s facturées, modèle %s, %d expressions d'adaptation",
            "synchrone" if synchronous else "asynchrone",
            len(payload.content) / 1048576, round(sent, 1) or "?",
            settings.stt_model, len(phrases),
        )
        if synchronous:
            response = client.recognize(config=config, audio=audio, timeout=120)
        else:
            operation = client.long_running_recognize(config=config, audio=audio)
            response = operation.result(timeout=timeout)
    except TranscriptionError:
        raise
    except Exception as exc:
        logger.exception("Échec de l'appel Speech-to-Text")
        raise TranscriptionError(f"Erreur Google Speech-to-Text : {exc}") from exc
    finally:
        if gcs_uri:
            _delete_from_gcs(gcs_uri)

    segments: List[str] = []
    confidences: List[float] = []
    for result in response.results:
        if not result.alternatives:
            continue
        alternative = result.alternatives[0]
        text = (alternative.transcript or "").strip()
        if text:
            segments.append(text)
            confidences.append(alternative.confidence or 0.0)

    transcript = " ".join(segments).strip()
    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        "confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "duration_seconds": int(round(duration)),
        "segments": len(segments),
        "provider": "google",
        "model": settings.stt_model,
    }


# ===========================================================================
# Deepgram
# ===========================================================================
#
# API « pre-recorded » : un simple POST du fichier audio, réponse JSON. On
# l'appelle en HTTP direct plutôt que par le SDK — c'est une seule requête,
# et cela évite d'embarquer une dépendance de plus dans une image qui tourne
# sur un NAS.
#
# L'audio envoyé est le même OGG/Opus que pour Google : le transcodage et le
# découpage en tranches sont communs aux deux fournisseurs.

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

#: Termes d'adaptation envoyés aux services qui plafonnent leur nombre
#: (AssemblyAI « keyterms », Soniox « context.terms »). Google, dont la
#: limite est de 5 000 expressions, reçoit le lexique complet.
#:
#: MESURÉ, PAS SUPPOSÉ. Sur une dictée réelle de 15 minutes, envoyer les ~318
#: expressions du lexique complet a fait **disparaître 43 mots** de la
#: transcription — dont un accident de voiture et une apnée du sommeil non
#: traitée. Le décodeur, saturé de termes à privilégier, saute du contenu.
#: La même dictée avec la liste ci-dessous : longueur identique à la
#: référence, aucune suppression, et tous les gains conservés
#: (« moco » → MoCA, « mis » → MMSE, « quétzapine » → quétiapine).
#:
#: Le tarif étant forfaitaire (0,05 $/h quelle que soit la taille), une liste
#: courte ne coûte pas moins — elle est simplement plus sûre. N'allongez
#: cette liste qu'en revérifiant qu'aucun passage ne disparaît.
LEXIQUE_PRIORITAIRE = [
    # Réseau et institutions du Québec — inconnus de tout modèle généraliste.
    "CHSLD", "CLSC", "GMF", "GMF-U", "CIUSSS", "CISSS", "RPA", "SAD", "SAPA",
    "RAMQ", "OEMC", "UCDG", "URFI", "DSQ", "niveau de soins", "proche aidant",
    # Échelles gériatriques — c'est là que le gain mesuré est le plus net.
    "MoCA", "MMSE", "GDS", "TUG", "AVQ", "AVD", "AIVQ", "CAM", "NPI", "SCPD",
    "MNA", "SMAF", "PRISMA-7",
    # Syndromes et actes dont la forme française prête à confusion.
    "délirium", "trouble neurocognitif majeur", "polypharmacie",
    "déprescription", "fragilité", "hypotension orthostatique",
    "créatinine", "TEP", "tomodensitométrie",
    # Molécules les plus souvent mal entendues en gériatrie.
    "donépézil", "rivastigmine", "mémantine", "quétiapine", "apixaban",
    "périndopril", "zopiclone",
]

#: Plafond volontairement bas : au-delà, les suppressions réapparaissent.
_ASSEMBLYAI_MAX_KEYTERMS = 60


#: Deepgram passe ses options en paramètres d'URL : au-delà de quelques
#: centaines de termes, l'URL devient déraisonnable. Le lexique complet fait
#: plus de 300 entrées, on n'en garde que le début — les acronymes du réseau
#: québécois, qui sont ce qu'un modèle généraliste rate le plus.
_DEEPGRAM_MAX_KEYWORDS = 120


def _deepgram_keyword_param(model: str) -> str:
    """
    Nom du paramètre d'adaptation selon la génération du modèle.

    ``keywords`` n'existe plus sur nova-3, qui lui substitue ``keyterm`` —
    mais ``keyterm`` est réservé à l'anglais. En français, l'adaptation n'est
    donc réellement disponible que sur nova-2 : c'est la raison pour laquelle
    le panneau recommande ce modèle.
    """
    return "keyterm" if model.startswith("nova-3") else "keywords"


def _transcribe_deepgram(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    api_key = runtime_config.value("deepgram_api_key")
    if not api_key:
        raise TranscriptionError(
            "Deepgram est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Reconnaissance vocale."
        )

    model = runtime_config.value("deepgram_model") or "nova-2"
    params = [
        ("model", model),
        ("language", runtime_config.stt_language("deepgram")),
        ("punctuate", "true"),
        ("smart_format", "true"),
    ]
    keyword_param = _deepgram_keyword_param(model)
    for phrase in build_phrase_hints(extra_phrase_hints)[:_DEEPGRAM_MAX_KEYWORDS]:
        params.append((keyword_param, phrase))

    url = f"{_DEEPGRAM_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        data=payload.content,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/ogg"},
        method="POST",
    )

    logger.info(
        "Envoi à Deepgram : %.2f Mo, %s s facturées, modèle %s, %d termes d'adaptation",
        len(payload.content) / 1048576, round(payload.effective_seconds, 1) or "?",
        model, sum(1 for key, _ in params if key == keyword_param),
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:400]
        logger.error("Deepgram a refusé la requête (%s) : %s", exc.code, detail)
        if exc.code in (401, 403):
            raise TranscriptionError(
                "Deepgram refuse la clé API. Vérifiez-la dans le panneau "
                "d'administration."
            ) from exc
        if exc.code == 400:
            raise TranscriptionError(
                f"Deepgram refuse la requête : {detail or 'paramètre invalide'}. "
                f"Le modèle « {model} » accepte-t-il cette langue ?"
            ) from exc
        raise TranscriptionError(f"Erreur Deepgram ({exc.code}) : {detail}") from exc
    except Exception as exc:
        logger.exception("Échec de l'appel Deepgram")
        raise TranscriptionError(f"Erreur Deepgram : {exc}") from exc

    channels = (body.get("results") or {}).get("channels") or []
    alternatives = (channels[0].get("alternatives") or []) if channels else []
    best = alternatives[0] if alternatives else {}
    transcript = (best.get("transcript") or "").strip()

    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    # Deepgram renvoie la durée qu'il a lui-même mesurée ; on garde la nôtre,
    # mesurée sur le fichier, car c'est elle qui fait avancer le curseur de
    # découpage et les deux ne doivent pas diverger.
    return {
        "transcript": transcript,
        "confidence": round(float(best.get("confidence") or 0.0), 3),
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "deepgram",
        "model": model,
    }


# ===========================================================================
# AssemblyAI
# ===========================================================================
#
# Contrairement aux deux autres, l'API n'est pas synchrone : on téléverse le
# fichier, on crée une tâche, puis on interroge son état jusqu'à ce qu'elle
# soit terminée. Trois allers-retours au lieu d'un, quelques secondes de plus
# par tranche — ce qui reste invisible puisque le découpage tourne en tâche de
# fond pendant que le médecin continue de parler.
#
# INTÉRÊT POUR CET USAGE
# ----------------------
# Le module « medical-v1 » (Medical Mode) est entraîné sur la terminologie
# clinique — médicaments, procédures, diagnostics, posologies — et **prend en
# charge le français**. C'est exactement ce que les deux autres services ratent
# le plus souvent ici, et ce que le lexique de ce fichier ne rattrape qu'en
# partie. Il est facturé en supplément ; sur une langue non prise en charge,
# AssemblyAI ignore l'option et le signale, sans frais.

_ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"

#: Une expression d'adaptation ne peut dépasser six mots.
_ASSEMBLYAI_MAX_WORDS = 6

#: Cadence et plafond d'attente. Une tranche d'une dizaine de secondes revient
#: en quelques secondes ; le plafond n'existe que pour ne pas bloquer un thread
#: indéfiniment si la tâche reste coincée.
_ASSEMBLYAI_POLL_SECONDS = 1.5
_ASSEMBLYAI_TIMEOUT_SECONDS = 240


def _assemblyai_request(path: str, api_key: str, data=None, content_type: str = "application/json"):
    import json as _json
    import urllib.error
    import urllib.request

    headers = {"authorization": api_key}
    body = data
    if data is not None and content_type == "application/json":
        body = _json.dumps(data).encode("utf-8")
        headers["content-type"] = "application/json"
    elif data is not None:
        headers["content-type"] = content_type

    request = urllib.request.Request(
        f"{_ASSEMBLYAI_BASE}{path}", data=body, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:400]
        logger.error("AssemblyAI a refusé la requête (%s) : %s", exc.code, detail)
        if exc.code in (401, 403):
            raise TranscriptionError(
                "AssemblyAI refuse la clé API. Vérifiez-la dans le panneau "
                "d'administration."
            ) from exc
        raise TranscriptionError(f"Erreur AssemblyAI ({exc.code}) : {detail}") from exc
    except Exception as exc:
        logger.exception("Échec de l'appel AssemblyAI")
        raise TranscriptionError(f"Erreur AssemblyAI : {exc}") from exc


def _termes_prioritaires(extra_phrase_hints: Optional[str], limite: int) -> List[str]:
    """
    Noyau validé, puis vocabulaire propre au gabarit.

    L'ordre importe : c'est le vocabulaire du gabarit qui justifie qu'un
    gabarit puisse en ajouter, et la troncature ne doit pas l'évincer. Les
    expressions de plus de six mots sont écartées — les API les refusent, et ce
    sont de toute façon des phrases entières que l'adaptation n'aide pas.
    """
    # Le noyau intégré est une liste de termes FRANÇAIS. En mode anglais il ne
    # peut rien améliorer et pousserait le moteur vers des mots qui ne seront
    # pas prononcés : on ne l'envoie alors pas. Le vocabulaire du gabarit, lui,
    # part toujours — c'est le médecin qui l'écrit, il sait dans quelle langue
    # il dicte.
    noyau = (
        list(LEXIQUE_PRIORITAIRE)
        if i18n.uses_french_lexicon(preferences.document_language())
        else []
    )

    vus, termes = set(), []
    for phrase in noyau + _extra_phrases(extra_phrase_hints):
        cle = phrase.lower()
        if cle in vus or len(phrase.split()) > _ASSEMBLYAI_MAX_WORDS:
            continue
        vus.add(cle)
        termes.append(phrase)
    return termes[:limite]


def _assemblyai_keyterms(model: str, extra_phrase_hints: Optional[str]) -> List[str]:
    """
    Lexique adapté aux contraintes d'AssemblyAI.

    Les expressions de plus de six mots sont refusées par l'API : plutôt que de
    faire échouer la requête entière, on les écarte — ce sont de toute façon
    des phrases entières, que l'adaptation n'aide pas.
    """
    return _termes_prioritaires(extra_phrase_hints, _ASSEMBLYAI_MAX_KEYTERMS)


def _extra_phrases(extra: Optional[str]) -> List[str]:
    """Vocabulaire additionnel d'un gabarit, découpé comme dans le lexique."""
    if not extra:
        return []
    return [p.strip() for p in extra.replace("\n", ",").split(",") if p.strip()]


def _transcribe_assemblyai(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    import time

    api_key = runtime_config.value("assemblyai_api_key")
    if not api_key:
        raise TranscriptionError(
            "AssemblyAI est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Reconnaissance vocale."
        )

    model = runtime_config.value("assemblyai_model") or "universal-3-5-pro"
    medical = runtime_config.value("assemblyai_medical") != "false"
    keyterms = _assemblyai_keyterms(model, extra_phrase_hints)

    logger.info(
        "Envoi à AssemblyAI : %.2f Mo, %s s facturées, modèle %s%s, %d termes d'adaptation",
        len(payload.content) / 1048576, round(payload.effective_seconds, 1) or "?",
        model, " + mode médical" if medical else "", len(keyterms),
    )

    # 1. Téléversement du fichier.
    upload = _assemblyai_request(
        "/upload", api_key, payload.content, content_type="application/octet-stream"
    )
    audio_url = upload.get("upload_url")
    if not audio_url:
        raise TranscriptionError("AssemblyAI n'a pas retourné d'URL de téléversement.")

    # 2. Création de la tâche.
    request_body = {
        "audio_url": audio_url,
        "speech_models": [model],
        "punctuate": True,
        "format_text": True,
    }
    language = runtime_config.stt_language("assemblyai")
    if language:
        request_body["language_code"] = language
    else:
        # Sans code de langue, l'API exige la détection explicite.
        request_body["language_detection"] = True
    if keyterms:
        request_body["keyterms_prompt"] = keyterms
    if medical:
        request_body["domain"] = "medical-v1"

    job = _assemblyai_request("/transcript", api_key, request_body)
    job_id = job.get("id")
    if not job_id:
        raise TranscriptionError("AssemblyAI n'a pas retourné d'identifiant de tâche.")

    # 3. Attente du résultat.
    deadline = time.monotonic() + _ASSEMBLYAI_TIMEOUT_SECONDS
    while True:
        status = job.get("status")
        if status == "completed":
            break
        if status == "error":
            raise TranscriptionError(
                f"AssemblyAI : {job.get('error') or 'échec de la transcription'}"
            )
        if time.monotonic() > deadline:
            raise TranscriptionError(
                f"AssemblyAI n'a pas terminé la transcription en "
                f"{_ASSEMBLYAI_TIMEOUT_SECONDS} s (tâche {job_id})."
            )
        time.sleep(_ASSEMBLYAI_POLL_SECONDS)
        job = _assemblyai_request(f"/transcript/{job_id}", api_key)

    # Le mode médical est ignoré sans erreur sur une langue non prise en
    # charge : l'avertissement est le seul moyen de s'en apercevoir, et il vaut
    # mieux le voir dans les journaux que de croire payer pour un module inactif.
    for warning in (job.get("warnings") or []):
        logger.warning("AssemblyAI : %s", warning if isinstance(warning, str)
                       else warning.get("message", warning))

    transcript = (job.get("text") or "").strip()
    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        "confidence": round(float(job.get("confidence") or 0.0), 3),
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "assemblyai",
        "model": model + (" + medical" if medical else ""),
    }


# ===========================================================================
# Soniox
# ===========================================================================
#
# Trois particularités par rapport aux autres fournisseurs :
#
#   * le téléversement est un vrai multipart/form-data, et non un corps binaire
#     brut — d'où le montage manuel de la requête ci-dessous ;
#   * le transcript revient **en jetons** et non en texte. Chaque jeton porte
#     déjà son espacement : on les concatène sans séparateur, sans quoi chaque
#     mot se retrouverait précédé d'une espace en trop ;
#   * la suppression du fichier téléversé est documentée, et on l'appelle —
#     comme on supprime l'objet GCS temporaire du chemin Google. Un
#     enregistrement de consultation n'a pas à séjourner chez un tiers plus
#     longtemps que la transcription ne l'exige.
#
# Soniox est multilingue par conception (une même requête couvre 60+ langues),
# ce qui en fait le seul des quatre à traiter sans réglage une consultation qui
# alterne français et anglais.

_SONIOX_BASE = "https://api.soniox.com"
_SONIOX_MAX_TERMS = 60
_SONIOX_POLL_SECONDS = 1.5
_SONIOX_TIMEOUT_SECONDS = 240

#: Indice de domaine passé en texte libre. Court à dessein : c'est un contexte,
#: pas une consigne — un moteur de reconnaissance vocale ne raisonne pas.
#:
#: Aucune spécialité n'y est nommée : elle varierait d'un utilisateur à l'autre
#: alors que ce texte est figé dans l'image. Le vocabulaire précis passe par les
#: « terms », qui viennent eux du lexique et du gabarit.
_SONIOX_CONTEXTES = {
    "fr": (
        "Consultation médicale au Québec. Vocabulaire clinique, posologies, "
        "échelles cliniques et acronymes du réseau de la santé québécois."
    ),
    "en": (
        "Medical consultation. Clinical vocabulary, drug dosages, clinical "
        "scales and health-system acronyms."
    ),
}


def _soniox_request(path: str, api_key: str, data=None, method: Optional[str] = None,
                    content_type: str = "application/json"):
    import json as _json
    import urllib.error
    import urllib.request

    headers = {"Authorization": f"Bearer {api_key}"}
    body = data
    if data is not None and content_type == "application/json":
        body = _json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(
        f"{_SONIOX_BASE}{path}", data=body, headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
            return _json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:400]
        logger.error("Soniox a refusé la requête (%s) : %s", exc.code, detail)
        if exc.code in (401, 403):
            raise TranscriptionError(
                "Soniox refuse la clé API. Vérifiez-la dans le panneau "
                "d'administration."
            ) from exc
        raise TranscriptionError(f"Erreur Soniox ({exc.code}) : {detail}") from exc
    except TranscriptionError:
        raise
    except Exception as exc:
        logger.exception("Échec de l'appel Soniox")
        raise TranscriptionError(f"Erreur Soniox : {exc}") from exc


def _multipart_body(field: str, filename: str, content: bytes) -> Tuple[bytes, str]:
    """Corps multipart/form-data à un seul fichier."""
    frontiere = f"----consultai{uuid.uuid4().hex}"
    corps = (
        f"--{frontiere}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{frontiere}--\r\n".encode()
    return corps, f"multipart/form-data; boundary={frontiere}"


def _multipart_body_fields(
    fields: dict, file_field: str, filename: str, content: bytes
) -> Tuple[bytes, str]:
    """
    Corps multipart/form-data : des champs texte PUIS un fichier.

    Distinct de ``_multipart_body``, qui n'envoie qu'un fichier : Cohere attend
    « model » et « language » dans le même formulaire que l'audio.
    """
    frontiere = f"----consultai{uuid.uuid4().hex}"
    morceaux = []
    for nom, valeur in fields.items():
        morceaux.append(
            (
                f"--{frontiere}\r\n"
                f'Content-Disposition: form-data; name="{nom}"\r\n\r\n'
                f"{valeur}\r\n"
            ).encode()
        )
    morceaux.append(
        (
            f"--{frontiere}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    morceaux.append(content)
    morceaux.append(f"\r\n--{frontiere}--\r\n".encode())
    return b"".join(morceaux), f"multipart/form-data; boundary={frontiere}"


def _transcribe_soniox(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    import time

    api_key = runtime_config.value("soniox_api_key")
    if not api_key:
        raise TranscriptionError(
            "Soniox est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Reconnaissance vocale."
        )

    model = runtime_config.value("soniox_model") or "stt-async-v5"
    langue = runtime_config.stt_language("soniox")
    envoyer_contexte = runtime_config.value("soniox_send_context") != "false"
    termes = _termes_prioritaires(extra_phrase_hints, _SONIOX_MAX_TERMS) if envoyer_contexte else []

    logger.info(
        "Envoi à Soniox : %.2f Mo, %s s facturées, modèle %s, %d termes de contexte%s",
        len(payload.content) / 1048576, round(payload.effective_seconds, 1) or "?",
        model, len(termes),
        " (contexte désactivé)" if not envoyer_contexte else "",
    )

    corps, ctype = _multipart_body("file", "dictee.ogg", payload.content)
    fichier = _soniox_request("/v1/files", api_key, corps, content_type=ctype)
    file_id = fichier.get("id")
    if not file_id:
        raise TranscriptionError("Soniox n'a pas retourné d'identifiant de fichier.")

    try:
        requete: dict = {
            "model": model,
            "file_id": file_id,
        }
        if envoyer_contexte:
            requete["context"] = {
                "text": _SONIOX_CONTEXTES[i18n.normalize(preferences.document_language())],
                "terms": termes,
            }
        if langue:
            requete["language_hints"] = [langue]
        else:
            requete["enable_language_identification"] = True

        tache = _soniox_request("/v1/transcriptions", api_key, requete)
        tache_id = tache.get("id")
        if not tache_id:
            raise TranscriptionError("Soniox n'a pas retourné d'identifiant de tâche.")

        limite = time.monotonic() + _SONIOX_TIMEOUT_SECONDS
        while True:
            etat = _soniox_request(f"/v1/transcriptions/{tache_id}", api_key)
            statut = etat.get("status")
            if statut == "completed":
                break
            if statut == "error":
                raise TranscriptionError(
                    f"Soniox : {etat.get('error_message') or 'échec de la transcription'}"
                )
            if time.monotonic() > limite:
                raise TranscriptionError(
                    f"Soniox n'a pas terminé la transcription en "
                    f"{_SONIOX_TIMEOUT_SECONDS} s (tâche {tache_id})."
                )
            time.sleep(_SONIOX_POLL_SECONDS)

        resultat = _soniox_request(f"/v1/transcriptions/{tache_id}/transcript", api_key)
    finally:
        # Au mieux : un échec de suppression ne doit pas faire perdre le texte,
        # mais il doit se voir dans les journaux.
        try:
            _soniox_request(f"/v1/files/{file_id}", api_key, method="DELETE")
        except TranscriptionError as exc:
            logger.warning("Fichier Soniox %s non supprimé : %s", file_id, exc)

    jetons = resultat.get("tokens") or []
    transcript = "".join(j.get("text") or "" for j in jetons).strip()
    transcript = re.sub(r"[ \t]{2,}", " ", transcript)

    confiances = [
        j["confidence"] for j in jetons
        if isinstance(j.get("confidence"), (int, float))
    ]

    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        "confidence": round(sum(confiances) / len(confiances), 3) if confiances else 0.0,
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "soniox",
        "model": model,
    }


# ===========================================================================
# Découpage d'une dictée en cours (mode segments)
# ===========================================================================
#
# Le navigateur téléverse l'enregistrement par fragments de quelques secondes
# que le serveur concatène dans un seul fichier. Ce fichier est un conteneur
# tronqué — parfaitement lisible par ffmpeg, mais pas par Google. On en extrait
# donc des tranches complètes et autonomes, converties en OGG/Opus.
#
# Le point de coupe n'est pas fixé à la seconde près : couper au milieu d'un
# mot le rend inintelligible des deux côtés de la frontière, et une note
# médicale ne peut pas se permettre « …le patient prend du war / farine ».
# On cherche donc un silence autour de la durée visée.


def _run_ffmpeg(args: Sequence[str], timeout: int = 300) -> str:
    """Lance ffmpeg et retourne son journal (stderr) ; lève en cas d'échec."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args],
        capture_output=True, timeout=timeout, check=False,
    )
    stderr = (result.stderr or b"").decode("utf-8", "replace")
    if result.returncode != 0:
        raise TranscriptionError(
            f"ffmpeg a échoué (code {result.returncode}) : {stderr[-400:]}"
        )
    return stderr


_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")


def find_cut_point(path: str, start: float, target: float, low: float, high: float) -> float:
    """
    Choisit où couper la tranche qui commence à ``start``.

    Retourne un décalage **relatif à start**, compris entre ``low`` et
    ``high``. On privilégie le milieu du silence le plus proche de ``target`` ;
    faute de silence exploitable on coupe à ``target``, en acceptant qu'un mot
    soit tronché — c'est le comportement de repli, pas le cas courant : une
    dictée comporte une pause toutes les quelques secondes.
    """
    if not _ffmpeg_available():
        return target
    try:
        log = _run_ffmpeg(
            [
                "-loglevel", "info", "-ss", f"{start:.3f}", "-t", f"{high:.3f}",
                "-i", path, "-vn",
                "-af", "silencedetect=noise=-32dB:duration=0.30",
                "-f", "null", "-",
            ],
            timeout=300,
        )
    except (TranscriptionError, subprocess.SubprocessError):
        return target

    # silencedetect émet « silence_start: x » puis « silence_end: y » ; sur un
    # silence encore ouvert à la fin de la fenêtre, le « end » manque.
    events = [(kind, float(value)) for kind, value in _SILENCE_RE.findall(log)]
    best, best_distance = target, None
    pending: Optional[float] = None
    for kind, value in events:
        if kind == "start":
            pending = value
            continue
        if pending is None:
            continue
        middle = (pending + value) / 2
        pending = None
        if not low <= middle <= high:
            continue
        distance = abs(middle - target)
        if best_distance is None or distance < best_distance:
            best, best_distance = middle, distance
    return best


# ---------------------------------------------------------------------------
# Retrait des longues pauses
# ---------------------------------------------------------------------------
#
# Google, Deepgram et AssemblyAI facturent tous à la durée d'audio : les
# minutes de silence d'une consultation — le médecin examine le patient, le
# patient cherche ses mots — sont payées plein tarif pour rien.
#
# CE QUI EST RETIRÉ, ET CE QUI NE L'EST PAS
# -----------------------------------------
# On ne supprime pas le silence, on le **plafonne**. Toute pause plus courte
# que ``stt_silence_keep_seconds`` est conservée intacte ; les plus longues
# sont ramenées à cette durée. La raison n'est pas esthétique : les moteurs de
# reconnaissance se servent des pauses pour placer la ponctuation et les
# frontières de phrases. Tout supprimer transforme « arrête le lisinopril.
# Débute l'amlodipine » en une seule phrase — sur une liste de médicaments,
# ce n'est pas un détail de mise en forme.
#
# LA DÉRIVE À NE PAS INTRODUIRE
# -----------------------------
# La durée mesurée sur le fichier raccourci ne doit JAMAIS servir de durée de
# la tranche : le curseur de découpage avancerait moins que l'audio consommé,
# la tranche suivante repartirait trop tôt, et une partie de la dictée serait
# transcrite deux fois — texte dupliqué au milieu de la note, sans la moindre
# erreur visible. D'où deux mesures distinctes : ``duration_seconds`` (réelle)
# et ``sent_seconds`` (raccourcie).


def compress_silence(source_path: str) -> Optional[Tuple[bytes, float]]:
    """
    Produit une copie de ``source_path`` dont les pauses sont plafonnées.

    Retourne ``(contenu, durée)`` — une durée nulle signifiant « aucune
    parole » — ou ``None`` si le filtrage a échoué, auquel cas l'appelant
    envoie l'audio d'origine plutôt que de perdre la tranche.

    Les deux extrémités sont traitées : ``stop_*`` plafonne les pauses au fil
    de l'audio, ``start_*`` celle qui précède la première parole. Sans ce
    second volet, une tranche entièrement muette ressortait intacte — le
    plafonnement en cours de flux ne s'amorce qu'après une première parole.
    On emploie ``start_silence`` et non ``start_duration`` : le premier borne
    le silence conservé, le second exigerait une durée minimale de parole
    avant de cesser de couper, et rognerait l'attaque d'un mot court.
    """
    if runtime_config.value("stt_trim_silence") == "false" or not _ffmpeg_available():
        return None

    if runtime_config.value("stt_provider") == "custom":
        # Endpoints personnalisés (ex. Parakeet/ONNX local) : pas de
        # facturation à la durée — la concaténation des paroles n'économise
        # rien — et le modèle y est sensible (le plafonnement des pauses
        # coupe les attaques de mots et fait mélanger les langues à un
        # modèle multilingue). On envoie l'audio tel quel.
        return None

    keep = max(0.0, runtime_config.value_float(
        "stt_silence_keep_seconds", settings.stt_silence_keep_seconds
    ))
    seuil = f"{settings.stt_silence_threshold_db}dB"
    workdir = tempfile.mkdtemp(prefix="consultai-trim-")
    dst = os.path.join(workdir, "trimmed.ogg")
    try:
        _run_ffmpeg([
            "-loglevel", "error", "-i", source_path, "-vn",
            "-af", (
                f"silenceremove=start_periods=1:start_silence={keep:.3f}"
                f":start_threshold={seuil}"
                f":stop_periods=-1:stop_duration={keep:.3f}"
                f":stop_threshold={seuil}"
            ),
            "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-f", "ogg", "-y", dst,
        ])
    except (TranscriptionError, subprocess.SubprocessError, OSError) as exc:
        # Un filtre qui échoue ne doit pas faire perdre la tranche : on
        # renonce à l'économie et on envoie l'audio d'origine.
        logger.warning("Retrait des silences impossible, audio envoyé tel quel : %s", exc)
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    try:
        with open(dst, "rb") as handle:
            content = handle.read()
        # Une sortie vide n'est pas une panne : c'est le résultat exact d'une
        # tranche où personne n'a parlé.
        return content, probe_duration(dst)
    except OSError:
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _apply_silence_trim(payload: AudioPayload, source_path: str) -> AudioPayload:
    """Remplace le contenu du payload par sa version raccourcie, si utile."""
    trimmed = compress_silence(source_path)
    if trimmed is None:
        return payload

    content, duration = trimmed

    # Sur un fichier importé, l'absence de parole doit rester une erreur
    # explicite venant du fournisseur : on lui envoie l'audio d'origine.
    if duration < _MIN_SPEECH_SECONDS and not payload.allow_silence:
        return payload

    if duration >= payload.duration_seconds - 0.05:
        return payload  # rien de significatif à gagner

    payload.content = content
    payload.sent_seconds = duration
    logger.info(
        "Silences retirés : %.1f s → %.1f s envoyées (%.0f %% de moins)",
        payload.duration_seconds, duration,
        100 * (1 - duration / payload.duration_seconds),
    )
    return payload


def extract_segment(path: str, start: float, length: float) -> AudioPayload:
    """
    Extrait ``length`` secondes à partir de ``start`` et les normalise en
    OGG/Opus. La durée retournée est celle mesurée sur le fichier produit,
    et non celle demandée : c'est elle qui fait avancer le curseur de lecture,
    et un écart cumulé de quelques dixièmes finirait par sauter un mot.
    """
    if not _ffmpeg_available():
        raise TranscriptionError("ffmpeg est absent du conteneur.")

    workdir = tempfile.mkdtemp(prefix="consultai-segment-")
    dst = os.path.join(workdir, "segment.ogg")
    try:
        _run_ffmpeg([
            "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path,
            "-vn", "-map_metadata", "-1",
            "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-f", "ogg", "-y", dst,
        ])
        with open(dst, "rb") as handle:
            content = handle.read()
        duration = probe_duration(dst)
        if not content or duration <= 0:
            raise TranscriptionError("Tranche audio vide.")
        # Le silence est attendu au milieu d'une consultation : une tranche
        # muette ne doit pas interrompre la dictée.
        payload = AudioPayload(content, "OGG_OPUS", 48000, duration, True, allow_silence=True)
        # La durée mesurée ci-dessus est celle qui fait avancer le curseur ;
        # le raccourcissement ne touche que la copie envoyée.
        return _apply_silence_trim(payload, dst)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# Cohere Transcribe
# ===========================================================================
# Contrat vérifié sur la documentation courante :
#   POST https://api.cohere.com/v2/audio/transcriptions
#   Authorization: Bearer <clé>
#   multipart/form-data : model, language (ISO-639-1), file
#   réponse : {"text": "..."}
#
# LA CONTRAINTE QUI GOUVERNE CE CODE : 5 REQUÊTES / MINUTE
# --------------------------------------------------------
# C'est la limite des clés d'essai — une clé de production se négocie avec
# Cohere. Or la dictée par tranches envoie une requête toutes les ~10 s et par
# usager : un seul médecin tient (6/min), deux tiennent à peine (12/min), trois
# dépassent. Ce service n'est donc pas adapté à un usage simultané, et
# l'application le dit dans le panneau au lieu de laisser découvrir des
# tranches perdues au milieu d'une consultation.
#
# Deux protections, dans cet ordre :
#   1. un ÉTALEMENT préventif — on ne laisse pas partir plus de 5 requêtes par
#      minute, en faisant patienter la suivante. Mieux vaut une tranche en
#      retard qu'une tranche refusée ;
#   2. une REPRISE sur 429, en respectant « Retry-After » quand il est fourni.
#
# Le découpage de la dictée tourne dans un fil d'exécution séparé (voir
# main._schedule_dictation_processing) : y patienter ne bloque pas la boucle
# asyncio ni la réception des fragments suivants.
# ===========================================================================
_COHERE_URL = "https://api.cohere.com/v2/audio/transcriptions"
_COHERE_MODEL_DEFAUT = "cohere-transcribe-03-2026"
#: Documenté : 25 Mo maximum par fichier.
_COHERE_MAX_BYTES = 25 * 1024 * 1024
#: Fenêtre d'étalement. Volontairement un cran sous la limite annoncée : une
#: requête refusée coûte plus cher qu'une requête retardée.
_COHERE_MAX_PAR_FENETRE = 4
_COHERE_FENETRE_SECONDES = 60.0
_COHERE_TENTATIVES = 3

_cohere_envois: List[float] = []
_cohere_verrou = threading.Lock()


def _cohere_attendre_son_tour() -> None:
    """
    Étale les envois pour rester sous la limite du fournisseur.

    Bloquant à dessein, et sans danger : l'appelant est un fil du pool, pas la
    boucle asyncio. Le verrou n'est pas tenu pendant l'attente — le garder
    sérialiserait tous les appels au lieu de les étaler.
    """
    while True:
        with _cohere_verrou:
            maintenant = time.monotonic()
            # On ne garde que les envois de la dernière minute : la liste ne
            # peut donc pas croître, et l'historique reste exact.
            recents = [t for t in _cohere_envois if maintenant - t < _COHERE_FENETRE_SECONDES]
            _cohere_envois[:] = recents
            if len(recents) < _COHERE_MAX_PAR_FENETRE:
                _cohere_envois.append(maintenant)
                return
            attente = _COHERE_FENETRE_SECONDES - (maintenant - recents[0]) + 0.05

        logger.info(
            "Cohere : limite de %d requêtes/minute atteinte, attente de %.1f s "
            "avant l'envoi de la tranche.",
            _COHERE_MAX_PAR_FENETRE, attente,
        )
        time.sleep(max(0.1, min(attente, _COHERE_FENETRE_SECONDES)))


def _transcribe_cohere(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    """
    Transcription par Cohere Transcribe.

    ``extra_phrase_hints`` est ignoré : l'API n'offre aucune adaptation au
    vocabulaire — ni mots-clés, ni contexte. C'est la contrepartie de sa
    simplicité, et cela vaut d'être su pour une dictée clinique, où les noms de
    molécules et les acronymes sont précisément ce qui se transcrit mal.
    """
    import json as _json
    import urllib.error
    import urllib.request

    api_key = runtime_config.value("cohere_api_key")
    if not api_key:
        raise TranscriptionError(
            "Cohere est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Reconnaissance vocale."
        )

    if len(payload.content) > _COHERE_MAX_BYTES:
        raise TranscriptionError(
            f"Tranche de {len(payload.content) / 1048576:.1f} Mo : Cohere refuse "
            "au-delà de 25 Mo. Réduisez DICTATION_SEGMENT_SECONDS."
        )

    model = runtime_config.value("cohere_model") or _COHERE_MODEL_DEFAUT
    # ISO-639-1 : « fr », « en ». Le code régional de Google (« fr-CA ») serait
    # refusé, d'où une entrée propre dans la table de correspondance.
    langue = runtime_config.stt_language("cohere") or "en"

    logger.info(
        "Envoi à Cohere : %.2f Mo, %s s facturées, modèle %s, langue %s",
        len(payload.content) / 1048576,
        round(payload.effective_seconds, 1) or "?", model, langue,
    )

    corps, ctype = _multipart_body_fields(
        {"model": model, "language": langue},
        "file", "dictee.ogg", payload.content,
    )

    derniere_erreur = ""
    for tentative in range(1, _COHERE_TENTATIVES + 1):
        _cohere_attendre_son_tour()
        requete = urllib.request.Request(
            _COHERE_URL,
            data=corps,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": ctype},
            method="POST",
        )
        try:
            with urllib.request.urlopen(requete, timeout=240) as reponse:
                data = _json.loads(reponse.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429 and tentative < _COHERE_TENTATIVES:
                # « Retry-After » quand il est fourni, sinon un recul qui
                # double : la fenêtre du fournisseur est glissante, réessayer
                # trop tôt ne fait que consommer une tentative.
                entete = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    pause = float(entete) if entete else 0.0
                except (TypeError, ValueError):
                    pause = 0.0
                pause = pause or min(60.0, 5.0 * (2 ** (tentative - 1)))
                logger.warning(
                    "Cohere a refusé la tranche (429, limite de débit). "
                    "Nouvelle tentative dans %.0f s (%d/%d).",
                    pause, tentative, _COHERE_TENTATIVES,
                )
                time.sleep(pause)
                derniere_erreur = detail
                continue
            if exc.code == 429:
                raise TranscriptionError(
                    "Cohere a refusé la tranche : limite de 5 requêtes par "
                    "minute atteinte malgré les tentatives. Ce service ne "
                    "convient pas à plusieurs dictées simultanées — changez de "
                    "fournisseur dans le panneau d'administration."
                ) from exc
            raise TranscriptionError(f"Erreur Cohere ({exc.code}) : {detail}") from exc
        except Exception as exc:
            logger.exception("Échec de l'appel Cohere")
            raise TranscriptionError(f"Erreur Cohere : {exc}") from exc
    else:
        raise TranscriptionError(
            f"Cohere injoignable après {_COHERE_TENTATIVES} tentatives. "
            f"{derniere_erreur}"
        )

    texte = str(data.get("text") or "").strip()
    return {
        "transcript": texte,
        # L'API ne renvoie aucun indice de confiance : on n'en invente pas.
        # 0.0 signifie « non fourni », comme pour les autres services muets.
        "confidence": 0.0,
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if texte else 0,
        "provider": "cohere",
        "model": model,
    }


# ===========================================================================
# Mistral Voxtral (transcription audio)
# ===========================================================================
# Contrat documenté sur docs.mistral.ai :
#   POST https://api.mistral.ai/v1/audio/transcriptions
#   Authorization: Bearer <clé>
#   multipart/form-data : file, model, language (ISO-639-1, optionnel)
#   réponse : {"text": "...", ...}
#
# Comme Cohere, aucune adaptation au vocabulaire n'est documentée pour ce
# service : ``extra_phrase_hints`` n'est donc pas transmis.
# ===========================================================================
_MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"
_MISTRAL_MODEL_DEFAUT = "voxtral-mini-latest"


def _transcribe_mistral(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    """
    Transcription par Mistral Voxtral.

    ``extra_phrase_hints`` est ignoré : aucune adaptation au vocabulaire n'est
    documentée pour ce service, contrairement à Google ou Deepgram.
    """
    import json as _json
    import urllib.error
    import urllib.request

    api_key = runtime_config.value("mistral_api_key")
    if not api_key:
        raise TranscriptionError(
            "Mistral est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Reconnaissance vocale."
        )

    model = runtime_config.value("mistral_model") or _MISTRAL_MODEL_DEFAUT
    langue = runtime_config.stt_language("mistral")

    logger.info(
        "Envoi à Mistral Voxtral : %.2f Mo, %s s facturées, modèle %s, langue %s",
        len(payload.content) / 1048576,
        round(payload.effective_seconds, 1) or "?", model, langue or "auto",
    )

    champs = {"model": model}
    if langue:
        champs["language"] = langue
    corps, ctype = _multipart_body_fields(champs, "file", "dictee.ogg", payload.content)

    requete = urllib.request.Request(
        _MISTRAL_URL,
        data=corps,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": ctype},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=240) as reponse:
            data = _json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        logger.error("Mistral a refusé la requête (%s) : %s", exc.code, detail)
        if exc.code in (401, 403):
            raise TranscriptionError(
                "Mistral refuse la clé API. Vérifiez-la dans le panneau "
                "d'administration."
            ) from exc
        raise TranscriptionError(f"Erreur Mistral ({exc.code}) : {detail}") from exc
    except Exception as exc:
        logger.exception("Échec de l'appel Mistral")
        raise TranscriptionError(f"Erreur Mistral : {exc}") from exc

    transcript = str(data.get("text") or "").strip()
    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        # L'API ne renvoie aucun indice de confiance : on n'en invente pas.
        "confidence": 0.0,
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "mistral",
        "model": model,
    }


# ===========================================================================
# OpenAI (Whisper / gpt-4o-transcribe)
# ===========================================================================
#
#   POST https://api.openai.com/v1/audio/transcriptions
#   Authorization: Bearer <clé>
#   multipart/form-data : file, model, language (ISO-639-1, optionnel)
#   réponse : {"text": "...", ...}
#
# Aucune clé propre à ce réglage : « openai_api_key », déjà utilisée pour le
# modèle de langage (voir llm.py), sert aussi ici — même compte, comme Cohere
# et Mistral, mais dans l'autre sens puisque la clé LLM existait déjà.
# ===========================================================================
_OPENAI_STT_BASE = "https://api.openai.com/v1"
_OPENAI_STT_MODEL_DEFAUT = "whisper-1"


def _transcribe_openai(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    """
    Transcription par OpenAI.

    ``extra_phrase_hints`` est ignoré : aucune adaptation au vocabulaire n'est
    documentée pour ce service, comme pour Cohere et Mistral.
    """
    api_key = runtime_config.value("openai_api_key")
    if not api_key:
        raise TranscriptionError(
            "OpenAI est sélectionné mais aucune clé API n'est renseignée. "
            "Panneau d'administration → Modèle de langage."
        )

    model = runtime_config.value("openai_stt_model") or _OPENAI_STT_MODEL_DEFAUT
    langue = runtime_config.stt_language("openai")

    logger.info(
        "Envoi à OpenAI : %.2f Mo, %s s facturées, modèle %s, langue %s",
        len(payload.content) / 1048576,
        round(payload.effective_seconds, 1) or "?", model, langue or "auto",
    )

    try:
        data = _post_openai_compatible(
            _OPENAI_STT_BASE, api_key, model, langue, payload, libelle="OpenAI",
        )
    except _EndpointHttpError as exc:
        logger.error("OpenAI a refusé la requête (%s) : %s", exc.code, exc.detail)
        if exc.code in (401, 403):
            raise TranscriptionError(
                "OpenAI refuse la clé API. Vérifiez-la dans le panneau "
                "d'administration."
            ) from exc
        raise TranscriptionError(f"Erreur OpenAI ({exc.code}) : {exc.detail}") from exc

    transcript = str(data.get("text") or "").strip()
    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        "confidence": 0.0,
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "openai",
        "model": model,
    }


# ===========================================================================
# Point de terminaison personnalisé, compatible OpenAI
# ===========================================================================
#
# Même contrat que ci-dessus (``POST {base_url}/audio/transcriptions``, même
# forme multipart), mais l'adresse ET la clé sont propres à ce réglage : rien
# ne garantit qu'un serveur auto-hébergé ou un service tiers partage un compte
# avec un autre fournisseur déjà configuré.
#
# Ce bloc fournit la primitive ``_post_openai_compatible`` — réutilisable par
# n'importe quel endpoint compatible OpenAI à l'avenir — et le schéma de repli
# propre à l'endpoint personnalisé : découpage optionnel en tranches
# (``custom_stt_chunk_seconds``), routage optionnel par durée
# (``custom_stt_max_seconds``), puis retry sur erreur HTTP 5xx vers un modèle
# de secours configurable.
# ===========================================================================


def _post_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    langue: str,
    payload: AudioPayload,
    timeout: int = 240,
    libelle: str = "du point de terminaison personnalisé",
) -> dict:
    """
    POST multipart vers ``{base_url}/audio/transcriptions`` (compatible OpenAI).

    Lève ``_EndpointHttpError`` si le serveur répond en erreur HTTP, ou
    ``TranscriptionError`` (message affichable) en cas d'échec de transport.
    """
    import json as _json
    import urllib.error
    import urllib.request

    champs = {"model": model}
    if langue:
        champs["language"] = langue
    corps, ctype = _multipart_body_fields(champs, "file", "dictee.ogg", payload.content)

    headers = {"Content-Type": ctype}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    requete = urllib.request.Request(
        f"{base_url}/audio/transcriptions", data=corps, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            return _json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        logger.error(
            "L'endpoint OpenAI-compatible (%s) a refusé la requête (%s) : %s",
            base_url, exc.code, detail,
        )
        raise _EndpointHttpError(exc.code, detail) from exc
    except Exception as exc:
        logger.exception("Échec de l'appel à l'endpoint OpenAI-compatible (%s)", base_url)
        raise TranscriptionError(f"Erreur {libelle} : {exc}") from exc


def _fallback_custom_target(base_url: str) -> Tuple[str, str]:
    """
    Cible de repli de l'endpoint personnalisé : (modèle, base_url).

    ``custom_stt_fallback_base_url`` vide → on réutilise l'adresse du modèle
    principal. ``custom_stt_fallback_model`` vide → pas de repli configuré.
    """
    fb_model = runtime_config.value("custom_stt_fallback_model").strip()
    fb_url = runtime_config.value("custom_stt_fallback_base_url").strip().rstrip("/")
    return fb_model, fb_url or base_url


def _parse_seuil_secondes(valeur: str) -> float:
    """
    Lit un seuil de durée configuré ; une valeur invalide désactive le routage.
    """
    try:
        return float(valeur)
    except ValueError:
        logger.warning("Réglage custom_stt_max_seconds invalide, ignoré : %r", valeur)
        return float("inf")


def _post_custom_avec_repli(
    base_url: str,
    api_key: str,
    model: str,
    fb_model: str,
    fb_url: str,
    langue: str,
    payload: AudioPayload,
) -> Tuple[dict, str]:
    """
    POST une tranche à l'endpoint personnalisé, avec repli sur erreur 5xx.

    Retourne ``(réponse, modèle_utilisé)``. En cas d'erreur HTTP 5xx du modèle
    principal, on retente une fois avec le modèle de secours. Lève
    ``TranscriptionError`` si l'endpoint refuse définitivement (401/403, ou 5xx
    sans repli disponible) — l'appelant décide alors d'abandonner ou de sauter
    la tranche.
    """
    try:
        data = _post_openai_compatible(base_url, api_key, model, langue, payload)
        return data, model
    except _EndpointHttpError as exc:
        if exc.code >= 500 and fb_model and fb_model != model:
            logger.warning(
                "STT custom : repli %s → %s après erreur HTTP %s (%s)",
                model, fb_model, exc.code, base_url,
            )
            try:
                data = _post_openai_compatible(fb_url, api_key, fb_model, langue, payload)
                return data, fb_model
            except _EndpointHttpError as exc2:
                if exc2.code in (401, 403):
                    raise TranscriptionError(
                        "Le point de terminaison personnalisé refuse la clé API. "
                        "Vérifiez-la dans le panneau d'administration."
                    ) from exc2
                raise TranscriptionError(
                    f"Erreur du point de terminaison personnalisé ({exc2.code}) : {exc2.detail}"
                ) from exc2
        if exc.code in (401, 403):
            raise TranscriptionError(
                "Le point de terminaison personnalisé refuse la clé API. "
                "Vérifiez-la dans le panneau d'administration."
            ) from exc
        raise TranscriptionError(
            f"Erreur du point de terminaison personnalisé ({exc.code}) : {exc.detail}"
        ) from exc


def _transcribe_custom_chunked(
    base_url: str,
    api_key: str,
    model: str,
    langue: str,
    fb_model: str,
    fb_url: str,
    payload: AudioPayload,
    chunk_seconds: float,
) -> dict:
    """
    Découpe ``payload`` en tranches d'au plus ``chunk_seconds`` et transcrit
    chacune au modèle principal, en coupant de préférence dans un silence.

    Raison d'être : un endpoint comme Parakeet/ONNX plafonne autour de 6-7 min
    d'audio en une passe (HTTP 500 au-delà). En découpant — plutôt qu'en
    routant le fichier entier vers le modèle de repli — on garde le modèle
    principal sur toute la durée. Le repli 5xx s'applique tranche par tranche ;
    une tranche qui échoue est sautée (le texte partiel est conservé), comme la
    retranscription écarte un enregistrement muet.
    """
    if not _ffmpeg_available():
        raise TranscriptionError("ffmpeg est absent du conteneur.")

    # Fenêtre de coupe : on cherche un silence autour de la durée visée pour ne
    # pas trancher un mot en deux — réutilise la logique de la dictée.
    low = chunk_seconds * 0.75
    high = chunk_seconds * 1.25

    workdir = tempfile.mkdtemp(prefix="consultai-chunk-")
    src = os.path.join(workdir, "source.ogg")
    try:
        with open(src, "wb") as handle:
            handle.write(payload.content)

        duree = payload.effective_seconds or 0.0
        logger.info(
            "STT custom : dictée de %.0f s > %s s → découpage en tranches "
            "(%.2f Mo, modèle %s, langue %s)",
            duree, chunk_seconds, len(payload.content) / 1048576,
            model, langue or "auto",
        )

        morceaux: List[str] = []
        modele_final = model
        dernier_refus = ""
        start = 0.0
        while start < duree - 0.05:
            restant = duree - start
            # Dernier morceau : on le prend entier, sans coupe donc sans risque.
            coupe = (
                restant if restant <= high
                else find_cut_point(src, start, chunk_seconds, low, high)
            )
            try:
                seg = extract_segment(src, start, coupe)
            except TranscriptionError as exc:
                # Fin de fichier atteinte : rien de plus à découper.
                logger.debug("STT custom : plus de tranche extractible (%s)", exc)
                break
            if seg.duration_seconds <= 0:
                break
            debut = start
            start += seg.duration_seconds

            try:
                data, modele_utilise = _post_custom_avec_repli(
                    base_url, api_key, model, fb_model, fb_url, langue, seg,
                )
            except TranscriptionError as exc:
                dernier_refus = str(exc)
                logger.warning(
                    "STT custom : tranche [%.0f-%.0f s] écartée — %s",
                    debut, start, exc,
                )
                continue

            texte = str(data.get("text") or "").strip()
            if texte:
                morceaux.append(texte)
                modele_final = modele_utilise
            logger.info(
                "STT custom : tranche de %.0f s transcrite (%d caractères, "
                "curseur %.0f s)",
                seg.duration_seconds, len(texte), start,
            )

        if not morceaux:
            raise TranscriptionError(
                dernier_refus
                or "Aucune parole n'a été détectée. Vérifiez le micro et le "
                   "volume de l'enregistrement, puis réessayez."
            )

        return {
            "transcript": "\n".join(morceaux),
            "confidence": 0.0,
            "duration_seconds": int(round(payload.duration_seconds)),
            "segments": len(morceaux),
            "provider": "custom",
            "model": modele_final,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _transcribe_custom(payload: AudioPayload, extra_phrase_hints: Optional[str] = None) -> dict:
    """
    Transcription par un point de terminaison personnalisé, compatible OpenAI.

    Découpage optionnel en tranches (``custom_stt_chunk_seconds``) : au-delà de
    la durée configurée, l'audio est découpé et chaque tranche part au modèle
    principal — utile quand l'endpoint plafonne en longueur d'audio par passe
    (ex. Parakeet/ONNX limité à ~6-7 min). Le découpage prime sur le routage
    par durée. Sans découpage, un routage optionnel par durée
    (``custom_stt_max_seconds``) envoie directement au modèle de repli les
    dictées trop longues. En cas d'erreur HTTP 5xx, on retente une fois avec le
    modèle de repli.
    """
    base_url = runtime_config.value("custom_stt_base_url").strip().rstrip("/")
    if not base_url:
        raise TranscriptionError(
            "Point de terminaison personnalisé sélectionné mais aucune adresse "
            "n'est renseignée. Panneau d'administration → Reconnaissance vocale."
        )
    api_key = runtime_config.value("custom_stt_api_key")
    model = runtime_config.value("custom_stt_model") or _OPENAI_STT_MODEL_DEFAUT
    langue = runtime_config.stt_language("custom")

    fb_model, fb_url = _fallback_custom_target(base_url)

    # --- Découpage optionnel en tranches ----------------------------------
    chunk_seconds = _parse_seuil_secondes(
        runtime_config.value("custom_stt_chunk_seconds")
    )
    if (
        0 < chunk_seconds < float("inf")
        and payload.effective_seconds
        and payload.effective_seconds > chunk_seconds
    ):
        return _transcribe_custom_chunked(
            base_url, api_key, model, langue, fb_model, fb_url, payload, chunk_seconds,
        )

    # --- Routage optionnel par durée (repli direct sur fichier long) -----
    maxsec = runtime_config.value("custom_stt_max_seconds").strip()
    if maxsec and fb_model and payload.effective_seconds:
        if payload.effective_seconds > _parse_seuil_secondes(maxsec):
            logger.info(
                "STT custom : dictée de %.0f s > seuil %s s → envoi direct au "
                "modèle de repli %s",
                payload.effective_seconds, maxsec, fb_model,
            )
            model, base_url = fb_model, fb_url

    logger.info(
        "Envoi au point de terminaison personnalisé (%s) : %.2f Mo, %s s "
        "facturées, modèle %s, langue %s",
        base_url, len(payload.content) / 1048576,
        round(payload.effective_seconds, 1) or "?", model, langue or "auto",
    )

    data, model = _post_custom_avec_repli(base_url, api_key, model, fb_model, fb_url, langue, payload)

    transcript = str(data.get("text") or "").strip()
    if not transcript and not payload.allow_silence:
        raise TranscriptionError(
            "Aucune parole n'a été détectée. Vérifiez le micro et le volume "
            "de l'enregistrement, puis réessayez."
        )

    return {
        "transcript": transcript,
        "confidence": 0.0,
        "duration_seconds": int(round(payload.duration_seconds)),
        "segments": 1 if transcript else 0,
        "provider": "custom",
        "model": model,
    }
