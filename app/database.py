"""
database.py — Schéma SQLite + amorçage des gabarits par défaut.
================================================================

Deux tables :
  * ``templates``     : gabarits de consultation (instructions + squelette)
  * ``consultations`` : brouillons dictés, sauvegardés automatiquement

SQLite est amplement suffisant ici : un cabinet médical génère quelques
dizaines de documents par jour, et le fichier vit sur un volume Docker
persistant (/data/consultai.db) trivial à sauvegarder avec Hyper Backup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import default_prompts
from app.config import settings
from app.default_templates import (
    EDITABLE_TEMPLATES,
    KEEP_NAMES,
    LOCKED_TEMPLATES,
)

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Horodatage UTC « aware ». Le fuseau d'affichage est géré côté client."""
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """
    Sérialise une date en ISO 8601 explicitement UTC.

    SQLite ne conserve pas le fuseau horaire : les valeurs relues sont naïves.
    Comme on n'écrit jamais que de l'UTC, on rétablit le suffixe « Z » pour que
    ``new Date(...)`` côté navigateur affiche la bonne heure locale.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Moteur SQLAlchemy
# ---------------------------------------------------------------------------
# check_same_thread=False : FastAPI exécute les endpoints synchrones dans un
# pool de threads ; chaque session reste néanmoins confinée à une requête.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    future=True,
    echo=False,
    pool_pre_ping=True,
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    """
    Réglages SQLite appliqués à chaque nouvelle connexion.

    - WAL     : lectures concurrentes pendant l'écriture (l'UI sauvegarde
                automatiquement pendant que l'utilisateur consulte ses brouillons)
    - busy_timeout : évite les « database is locked » sur un NAS lent
    - foreign_keys : SQLite ne les applique pas par défaut (!)
    """
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=8000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------
class Template(Base):
    """
    Gabarit de consultation.

    ``system_instructions`` décrit à Gemini CE QU'IL DOIT FAIRE (le raisonnement
    clinique, ce sur quoi insister), tandis que ``layout_format`` décrit
    EXACTEMENT la forme attendue du document final (les titres Markdown).
    Séparer les deux permet de modifier la mise en page sans toucher aux
    consignes cliniques, et inversement.
    """

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_instructions: Mapped[str] = mapped_column(Text, nullable=False)
    layout_format: Mapped[str] = mapped_column(Text, nullable=False)

    # Vocabulaire spécifique au gabarit, transmis en « phrase hints » à
    # Google STT en plus du lexique clinique global (voir stt.py).
    phrase_hints: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Gabarit préchargé à l'installation : signalé dans l'UI, mais restant
    # entièrement modifiable/supprimable par l'utilisateur.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    #: LANGUE DE TOUTE LA CHAÎNE pour ce gabarit — « fr » ou « en ».
    #:
    #: Ce n'est pas une étiquette d'affichage. Elle décide de la langue des
    #: consignes de base, de la consigne générale employée, du code envoyé au
    #: service de reconnaissance vocale et de la langue de rédaction de la note.
    #: C'est la SEULE source : aucune détection automatique depuis l'audio ni
    #: depuis le texte, qui se tromperait sur une consultation bilingue et
    #: rendrait le résultat imprévisible.
    language: Mapped[str] = mapped_column(String(8), default="fr", nullable=False)

    #: Gabarit protégé : ni modifiable ni supprimable, à dupliquer pour l'adapter.
    #: Le refus est appliqué côté serveur, pas seulement masqué dans l'écran.
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Propriétaire d'un gabarit PERSONNEL — ``None`` = gabarit partagé de
    #: l'équipe. Un gabarit partagé est visible de tous et ne se réécrit qu'avec
    #: le droit ``can_manage_templates`` ; un gabarit personnel (copie ou
    #: création) n'est visible que de son propriétaire, qui le gère seul. La
    #: duplication produit toujours une copie personnelle.
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def to_dict(self, include_body: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_default": self.is_default,
            "is_locked": self.is_locked,
            "owner": self.owner,
            "language": self.language or "fr",
            "sort_order": self.sort_order,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_body:
            data.update(
                {
                    "system_instructions": self.system_instructions,
                    "layout_format": self.layout_format,
                    "phrase_hints": self.phrase_hints,
                }
            )
        return data


class Consultation(Base):
    """
    Brouillon de consultation.

    On conserve séparément la transcription brute, la sortie de Gemini et la
    version corrigée par le médecin : indispensable pour pouvoir régénérer le
    document sans perdre les corrections manuelles, et pour la traçabilité.
    """

    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Propriétaire = identifiant SSO. Chaque médecin ne voit que ses documents.
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    title: Mapped[str] = mapped_column(String(300), default="Consultation sans titre", nullable=False)

    # --- Métadonnées d'identification ------------------------------------
    # Renseignées automatiquement à partir de la dictée (voir llm.extract_metadata) :
    # ce sont elles qui permettent de reconnaître une consultation dans la
    # liste des brouillons, sans avoir à en lire le contenu.
    patient_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    patient_ref: Mapped[str] = mapped_column(String(120), default="", nullable=False)   # n° de dossier
    reason: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    requester: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    accompanied_by: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    consultation_date: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    raw_transcript: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Confiance mot-à-mot du STT (``{norm_phon(mot) → confiance}``), en miroir
    #: de la transcription. Série côté serveur uniquement : elle permet à la
    #: génération de signaler au LLM les mots mal reconnus (sans audio), et
    #: meurt avec le brouillon comme les autres champs internes. Jamais
    #: transmise au navigateur.
    transcript_conf: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    edited_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # brouillon | transcrit | genere | finalise
    status: Mapped[str] = mapped_column(String(30), default="brouillon", nullable=False)
    audio_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Quels moteurs ont réellement produit ce document. Sans cette trace, un
    # brouillon rouvert afficherait la configuration du jour et non celle qui
    # l'a fabriqué — ce qui est exactement l'inverse de ce qu'on veut savoir
    # quand on compare des modèles.
    model_used: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    llm_provider: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    stt_provider: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    stt_model: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    # Langue dans laquelle l'audio a RÉELLEMENT été transcrit. Distincte de
    # celle du gabarit courant : la dictée commence souvent avant que le
    # gabarit soit choisi, et c'est justement l'écart entre les deux qui doit
    # déclencher la proposition de retranscription. La déduire du gabarit ne
    # marcherait pas — il a pu changer depuis, ou être traduit après coup.
    stt_language: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    # Un extrait audio a-t-il accompagné la transcription lors de la DERNIÈRE
    # génération ? Même raison de traçabilité que model_used/llm_provider
    # ci-dessus : un brouillon rouvert doit refléter ce qui l'a produit, pas
    # le réglage courant du panneau admin.
    audio_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # La transcription a-t-elle vraiment nourri la DERNIÈRE génération, ou le
    # STT n'a-t-il tourné que pour l'affichage pendant que l'audio seul
    # alimentait le modèle (contournement du STT, transcription vide) ? Sans
    # ce champ, stt_provider/stt_model — qui suivent le dernier PASSAGE DE
    # DICTÉE, pas la dernière génération — continueraient de s'afficher même
    # quand ils n'ont eu aucune part dans la note. Vrai par défaut : toute
    # consultation antérieure à ce réglage a été générée à partir de sa
    # transcription, sans exception.
    transcript_used: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Jetons et durée RÉELS de la dernière génération, tels que renvoyés par
    # le fournisseur — jamais un nombre que le modèle prétendrait donner dans
    # le corps de la note, qui serait inventé (aucun modèle n'a accès à son
    # propre décompte). Nullable et non 0 : un 0 serait indiscernable d'une
    # mesure réelle de zéro jeton, ce qui n'arrive jamais mais brouillerait
    # « jamais mesuré » (brouillons antérieurs à ce champ) et « mesuré à 0 ».
    usage_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Résultat du « Validation » (audit factuel audio↔note), sérialisé JSON.
    # Nullable : absent = jamais demandé ; présent mais invalide = ignoré
    # côté interface, comme si absent. Suit la note qu'il audite et meurt
    # avec le brouillon, comme elle.
    verification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Rubrique « Corrections et éléments à valider », retirée du corps de la
    # note à la génération et montrée dans l'onglet « Validation ». Même
    # rétention que la note : vit et meurt avec le brouillon.
    corrections_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Liste des médicaments détectés/normalisés (moteur de grounding), sérialisée
    # JSON. Nullable : absent = grounding jamais exécuté ; invalide = ignoré.
    # Alimente la liste pointée de l'onglet « Validation » au rechargement et
    # sert de trace d'audit (corrections déterministes, non-PHI).
    med_grounding_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )

    def to_dict(self, include_body: bool = True) -> dict:
        """
        ``include_body=False`` sert la liste des brouillons : elle n'affiche que
        les métadonnées d'identification, jamais un extrait du document. Un
        aperçu du Markdown y étalerait des renseignements de santé sur un écran
        souvent consulté en présence du patient, sans aider à retrouver la
        bonne consultation.
        """
        data = {
            "id": self.id,
            "title": self.title,
            "reason": self.reason,
            "requester": self.requester,
            "accompanied_by": self.accompanied_by,
            "consultation_date": self.consultation_date,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "status": self.status,
            "audio_seconds": self.audio_seconds,
            "model_used": self.model_used,
            # Chaînes déjà composées : l'interface n'a pas à connaître la
            # façon dont on stocke fournisseur et modèle séparément. Vide si
            # la dernière génération n'a pas consommé la transcription (voir
            # transcript_used) : stt_provider/stt_model datent alors d'un
            # passage de dictée qui n'a eu aucune part dans la note actuelle.
            "stt_used": " / ".join(p for p in (self.stt_provider, self.stt_model) if p)
                if self.transcript_used else "",
            # Brute, celle-ci : l'interface la compare à la langue du gabarit
            # choisi pour décider s'il y a lieu de proposer une retranscription.
            "stt_language": self.stt_language,
            "llm_used": " / ".join(p for p in (self.llm_provider, self.model_used) if p),
            "audio_used": self.audio_used,
            "usage_prompt_tokens": self.usage_prompt_tokens,
            "usage_output_tokens": self.usage_output_tokens,
            "generation_seconds": self.generation_seconds,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_body:
            data.update(
                {
                    "raw_transcript": self.raw_transcript,
                    "generated_markdown": self.generated_markdown,
                    "edited_markdown": self.edited_markdown,
                    "verification_json": self.verification_json,
                    "corrections_markdown": self.corrections_markdown,
                    "med_grounding_json": self.med_grounding_json,
                }
            )
        return data


class Recording(Base):
    """
    Enregistrement audio conservé avec son brouillon.

    L'audio n'est plus détruit à la fin de la dictée : il reste attaché à la
    consultation, et c'est la suppression du brouillon qui l'emporte. Le
    fichier lui-même vit sous ``AUDIO_DIR`` ; la table n'en garde que la
    référence, ce qui permet de tout retrouver — et de tout effacer — sans
    parcourir le disque.

    ``owner`` est dupliqué depuis la consultation : il évite une jointure sur
    chaque lecture du fichier, et le contrôle d'accès est le seul endroit du
    code où une jointure oubliée coûterait cher.
    """

    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # relatif à AUDIO_DIR
    mime_type: Mapped[str] = mapped_column(String(100), default="audio/webm", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="dictee", nullable=False)  # dictee | import

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "consultation_id": self.consultation_id,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "source": self.source,
            "created_at": _iso(self.created_at),
        }


class AppSetting(Base):
    """
    Réglage modifiable depuis le panneau d'administration.

    Ces valeurs **surchargent** celles du fichier ``.env`` : l'environnement
    fournit le défaut au premier démarrage, la base fait ensuite autorité.
    C'est ce qui permet de changer de fournisseur de reconnaissance vocale ou
    de modèle sans reconstruire l'image — sur un NAS, un `docker compose up
    --build` est une opération à ne pas demander entre deux consultations.

    Les clés d'API y sont stockées en clair, comme le reste de la base. Voir
    README § 9 : c'est le fichier ``./data`` qu'il faut protéger.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)


class SchedulerState(Base):
    """
    Dernière exécution de chaque tâche planifiée (sauvegarde quotidienne,
    compactage de l'usage…). Sert à la fois de mémoire pour ``scheduler.py``
    (« la tâche du jour a-t-elle déjà tourné ? ») et d'affichage côté panneau
    admin (« dernière sauvegarde : ... »).
    """

    __tablename__ = "scheduler_state"

    job_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_run_date: Mapped[str] = mapped_column(String(10), default="", nullable=False)  # YYYY-MM-DD, fuseau local
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(10), default="", nullable=False)  # ok | error
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PricingRate(Base):
    """
    Tarif d'un fournisseur/modèle, modifiable depuis le panneau admin.

    ``unit`` fixe l'échelle du tarif pour rester lisible et copiable depuis
    une page de tarifs fournisseur : ``token_input_1m``/``token_output_1m``
    (prix pour 1 million de jetons) ou ``audio_minute`` (prix pour 1 minute
    d'audio) — jamais un prix par jeton unique, dont la précision décimale
    (ex. 0,0000003 $) est source d'erreur de saisie.

    ``model = ""`` sert de tarif par défaut pour tout le fournisseur, utilisé
    quand aucune ligne ne correspond exactement au modèle (les champs modèle
    de ``runtime_config.py`` sont du texte libre, ex. ``gemini_model``).
    """

    __tablename__ = "pricing_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # llm | stt
    unit: Mapped[str] = mapped_column(String(20), nullable=False)  # token_input_1m | token_output_1m | audio_minute
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "model", "kind", "unit", name="uq_pricing_rate"),
    )


class UsageEvent(Base):
    """
    Un appel facturé (génération LLM ou transcription STT), au jeton/à la
    seconde près. Conservé ``USAGE_RAW_RETENTION_DAYS`` jours (voir
    ``app/usage.py``) puis compacté dans ``UsageDaily`` et effacé — sert au
    détail récent (« pourquoi cette consultation a coûté cher »), pas à
    l'historique long terme.

    Le coût est calculé et figé à l'écriture (jamais recalculé depuis
    ``PricingRate`` a posteriori) : corriger un tarif placeholder le mois
    prochain ne doit pas réécrire silencieusement la dépense déjà déclarée le
    mois dernier.
    """

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    consultation_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # llm | stt
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Jetons d'entrée AUDIO, comptés à part du texte : Gemini 2.5 Flash et
    # Qwen Omni facturent l'audio entrant à un tarif distinct du texte, et
    # le ventilent dans la réponse (``prompt_tokens_details``). ``None``
    # chez les fournisseurs qui ne ventilent pas — ``prompt_tokens`` reste
    # alors le total d'entrée, comme avant.
    audio_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Jetons d'entrée servis depuis le cache de préfixe implicite de Gemini
    # (``cached_content_token_count``) : facturés à un tarif réduit, ils sont
    # rangés ici pour que le coût journalisé reflète la remise réelle.
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UsageDaily(Base):
    """
    Cumul quotidien produit par le compactage de ``UsageEvent``. Une ligne
    par (jour, usager, type, fournisseur, modèle) — jamais purgée : c'est la
    base des tableaux de l'onglet admin « Statistiques », et son volume reste
    négligeable (une ligne par combinaison-jour, pas par appel).
    """

    __tablename__ = "usage_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audio_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audio_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("date", "owner", "kind", "provider", "model", name="uq_usage_daily"),
    )


class NotesDaily(Base):
    """
    Nombre de notes réellement produites (consultations passées au statut
    « genere »/« finalise »), cumulé par (jour, usager). Jamais purgée : c'est
    ce qui fait survivre le « nombre de dictées » du panneau admin à la purge
    des dossiers de consultation (rétention). Incrémentée à la PREMIÈRE
    génération d'une consultation — une régénération ne re-compte pas. La
    suppression d'un compte efface ses lignes (voir ``users.delete_user``).
    """

    __tablename__ = "notes_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    notes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("date", "owner", name="uq_notes_daily"),
    )


class Group(Base):
    """
    Groupe d'usagers, porteur de permissions.

    Deux groupes sont livrés et ne peuvent être supprimés (``is_system``) :

    * ``admins`` — accès au panneau d'administration et aux gabarits ;
    * ``users``  — dicter, relire, exporter ses propres consultations.

    Les permissions sont volontairement peu nombreuses. Un modèle de droits fin
    se paie en écrans de configuration que personne ne relit, et le vrai
    cloisonnement de cette application est ailleurs : une consultation
    n'appartient qu'à son auteur, quel que soit son groupe.
    """

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Identifiant technique, en minuscules. Sert aussi à la correspondance avec
    #: les groupes annoncés par le fournisseur d'identité.
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    #: Accès au panneau d'administration, aux réglages et à la gestion des
    #: usagers. C'est « le rôle admin » du cahier des charges.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Droit de créer / modifier / supprimer les gabarits, qui sont partagés.
    can_manage_templates: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: Groupe livré avec l'application : renommable, mais pas supprimable.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_admin": self.is_admin,
            "can_manage_templates": self.can_manage_templates,
            "is_system": self.is_system,
        }


class UserGroup(Base):
    """Appartenance d'un usager à un groupe."""

    __tablename__ = "user_groups"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class User(Base):
    """
    Compte connu de l'application.

    IDENTITÉ : ``subject`` FAIT FOI
    ------------------------------
    Le ``sub`` du fournisseur OIDC est le seul identifiant stable : une adresse
    de courriel change, un nom d'usager peut être réattribué. C'est donc lui qui
    lie un compte à une personne, et il est rempli à la première connexion.

    ``username`` reste néanmoins la clé de propriété des consultations (colonne
    ``Consultation.owner``), pour une raison d'histoire : les consultations
    existaient avant cette table. Le rattachement d'une identité OIDC à un
    compte déjà porteur de données passe donc par ``subject``, puis ``username``,
    puis ``email`` — voir ``users.link_or_create``. Sans cette cascade, une
    installation migrée verrait ses brouillons devenir invisibles du jour où
    l'authentification change de source.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: « sub » OIDC. Vide pour un compte amorcé qui ne s'est jamais connecté.
    subject: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    #: Identité normalisée en minuscules. Clé de propriété des consultations.
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: Adresse de l'avatar annoncée par le fournisseur. Vide = on retombe sur
    #: les initiales, calculées côté application.
    avatar_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    #: Un compte désactivé conserve ses consultations mais ne peut plus entrer.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self, groups: Optional[List["Group"]] = None) -> dict:
        payload = {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "is_active": self.is_active,
            "has_signed_in": bool(self.subject),
            "created_at": _iso(self.created_at),
            "last_login_at": _iso(self.last_login_at),
        }
        if groups is not None:
            payload["groups"] = [g.to_dict() for g in groups]
        return payload


class UserPreference(Base):
    """
    Préférences propres à un usager, indépendantes des réglages d'instance.

    POURQUOI UNE TABLE SÉPARÉE D'``app_settings``
    ---------------------------------------------
    ``app_settings`` décrit l'installation et n'est modifiable que par un
    administrateur. La langue, elle, n'a rien d'administratif : elle regarde la
    personne qui lit l'écran. La mettre dans le panneau d'administration
    obligeait un usager ordinaire à demander à quelqu'un d'autre de changer la
    langue de sa propre interface — ou, s'il y avait accès, à la changer pour
    tout le monde.

    POURQUOI PAS UN TÉMOIN DE SESSION
    ---------------------------------
    Un témoin de session existe (voir ``main.py``, ``SessionMiddleware``),
    mais il expire (``SESSION_MAX_AGE_SECONDS``, 12 h par défaut) et est
    propre à un appareil. La langue, elle, doit survivre à une reconnexion et
    suivre l'usager d'un appareil à l'autre : elle est donc rangée en base,
    sous la clé d'identité de l'usager, indépendamment de tout témoin.

    La clé est l'identifiant normalisé en minuscules (voir
    ``Principal.owner_key``) : le fournisseur d'identité peut renvoyer une casse
    variable d'une session à l'autre, et « Dr.Tremblay@… » ne doit pas perdre le
    réglage de « dr.tremblay@… ».
    """

    __tablename__ = "user_preferences"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: « fr », « en », ou vide pour suivre le défaut de l'installation.
    language: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    #: Thème de couleur : « teal », « blue », … ou vide pour suivre le défaut.
    theme_color: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: « Validation » : audit factuel de la note après génération. Défaut OFF :
    #  coûte un second appel modèle, se déclenche sur demande expresse.
    second_pass: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Dépendance FastAPI
# ---------------------------------------------------------------------------
def get_db() -> Iterator[Session]:
    """Fournit une session SQLAlchemy fermée automatiquement en fin de requête."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ===========================================================================
# GABARITS PAR DÉFAUT
# ===========================================================================
# Rédigés pour la pratique gériatrique québécoise. Ils sont insérés une seule
# fois, au premier démarrage : vos modifications ultérieures ne seront jamais
# écrasées par un redémarrage du conteneur.
# ===========================================================================

_STANDARD_INSTRUCTIONS = """\
Tu structures une ÉVALUATION GÉRIATRIQUE GLOBALE dictée par un gériatre du Québec.

PRIORITÉS CLINIQUES :
1. Dégage clairement le motif de consultation et le demandeur (médecin de famille,
   urgence, CLSC, CHSLD, équipe traitante).
2. Rends l'histoire de la maladie actuelle (HMA) chronologique et narrative :
   début des symptômes, évolution, facteurs déclenchants, traitements déjà essayés.
3. Accorde une attention particulière aux GRANDS SYNDROMES GÉRIATRIQUES, même
   lorsqu'ils sont mentionnés brièvement ou en passant dans la dictée :
   - chutes (nombre, mécanisme, blessures, peur de tomber, aides à la marche)
   - autonomie fonctionnelle : distingue explicitement les AVQ (hygiène, habillage,
     alimentation, transferts, continence) des AVD/AIVQ (finances, médication,
     transport, repas, ménage, téléphone, courses)
   - cognition, humeur (dépression, anxiété), délirium
   - polypharmacie, médicaments potentiellement inappropriés (critères de Beers,
     STOPP/START), anticholinergiques, benzodiazépines
   - dénutrition et perte de poids, sarcopénie, fragilité
   - continence, douleur, sommeil, vision et audition
   - plaies de pression, iatrogénie
4. Documente le milieu de vie et le réseau de soutien : domicile, RPA, RI, CHSLD,
   présence du conjoint ou des proches, services déjà en place (SAD du CLSC,
   popote roulante, aide domestique, centre de jour), épuisement du proche aidant.
5. Rends compte du niveau de soins et des directives médicales anticipées s'ils
   sont abordés, ainsi que du statut d'aptitude, du mandat de protection et de
   la conduite automobile.

RÈGLES DE RÉDACTION :
- Regroupe les informations sous la bonne rubrique même si le médecin les a
  dictées dans le désordre ou y est revenu plus tard dans la dictée.
- Transforme le style télégraphique de la dictée en phrases cliniques complètes,
  sobres et professionnelles, SANS ajouter d'information.
- Le plan doit être présenté en liste numérotée d'actions concrètes.
"""

_STANDARD_LAYOUT = """\
# CONSULTATION EN GÉRIATRIE

**Date de l'évaluation :** {{DATE}}
**Demande de :** {{DEMANDEUR}}

## MOTIF DE CONSULTATION

## HISTOIRE DE LA MALADIE ACTUELLE

## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX

## MÉDICATION ACTUELLE

## ALLERGIES ET INTOLÉRANCES

## HABITUDES DE VIE
Tabac, alcool, cannabis, activité physique.

## HISTOIRE SOCIALE ET MILIEU DE VIE
Scolarité, occupation antérieure, milieu de vie, réseau de soutien, services
en place, proche aidant.

## REVUE DES SYNDROMES GÉRIATRIQUES

### Mobilité et chutes

### Autonomie fonctionnelle
**AVQ :**
**AVD :**

### Cognition

### Humeur et comportement

### Nutrition et poids

### Continence

### Sommeil

### Douleur

### Vision et audition

## EXAMEN PHYSIQUE
Signes vitaux, état général, examen cardiorespiratoire, neurologique,
musculosquelettique, tests de mobilité (TUG, Tinetti, vitesse de marche).

## INVESTIGATIONS
Laboratoires, imagerie et examens complémentaires disponibles ou demandés.

## IMPRESSION DIAGNOSTIQUE
Liste numérotée des problèmes actifs.

## PLAN
Liste numérotée : investigations, ajustements pharmacologiques, interventions
non pharmacologiques, références (physiothérapie, ergothérapie, nutrition,
travail social), services à mettre en place.

## NIVEAU DE SOINS ET DIRECTIVES

## SUIVI
"""

_COGNITIF_INSTRUCTIONS = """\
Tu structures un BILAN COGNITIF réalisé en clinique de mémoire par un gériatre
du Québec.

PRIORITÉS CLINIQUES :
1. HISTOIRE COGNITIVE détaillée : nature du premier symptôme remarqué, date
   approximative du début, mode d'installation (insidieux, brutal, en marches
   d'escalier), vitesse de progression, fluctuations.
2. Distingue systématiquement et explicitement :
   - les DOMAINES ATTEINTS : mémoire épisodique, mémoire de travail, langage
     (manque du mot, paraphasies), fonctions exécutives, praxies, gnosies,
     fonctions visuospatiales, orientation, comportement social
   - l'INFORMATION FOURNIE PAR LE PATIENT de celle fournie par l'HÉTÉRO-ANAMNÈSE
     (conjoint, enfants, proche aidant) — précise toujours la source, car
     l'anosognosie est un élément diagnostique en soi.
3. IMPACT FONCTIONNEL : c'est l'élément qui distingue le trouble neurocognitif
   majeur du trouble léger. Documente précisément les AVD/AIVQ (gestion des
   finances et des comptes, gestion de la médication, conduite automobile,
   utilisation du téléphone et des appareils, préparation des repas, courses)
   puis les AVQ. Cherche les erreurs concrètes rapportées.
4. SYMPTÔMES NEUROPSYCHIATRIQUES ET COMPORTEMENTAUX (SCPD) : apathie, dépression,
   anxiété, irritabilité, agitation, agressivité, idées délirantes (vol,
   jalousie, intrus), hallucinations (précise la modalité), errance, inversion
   du cycle veille-sommeil, désinhibition, trouble comportemental en sommeil
   paradoxal.
5. DRAPEAUX ROUGES ORIENTANT LE DIAGNOSTIC ÉTIOLOGIQUE : parkinsonisme, chutes
   précoces, fluctuations et hallucinations visuelles (corps de Lewy),
   désinhibition et changement de personnalité précoce (dégénérescence
   frontotemporale), évolution en marches d'escalier et facteurs de risque
   vasculaires (vasculaire), troubles de la marche avec incontinence et
   troubles cognitifs (hydrocéphalie à pression normale), causes réversibles
   (B12, TSH, dépression, médicaments, alcool, apnée du sommeil).
6. TESTS OBJECTIFS : rapporte les scores exactement comme dictés, avec le
   maximum et le détail des sous-scores lorsqu'il est fourni (MoCA /30,
   MMSE /30, test de l'horloge, fluence verbale, rappel des 5 mots, MoCA
   ajusté pour la scolarité). Le niveau de scolarité conditionne
   l'interprétation : rapporte-le dans l'histoire sociale, où il appartient,
   et rappelle-le au besoin en commentaire du score.
7. SÉCURITÉ ET PLANIFICATION : aptitude à consentir aux soins et à gérer ses
   biens, mandat de protection, conduite automobile (obligation de déclaration
   à la SAAQ), risques au domicile (cuisinière, errance, armes à feu, gestion
   des médicaments), épuisement du proche aidant, hébergement envisagé
   (RPA, RI, CHSLD) et démarches en cours.

RÈGLES DE RÉDACTION :
- N'inscris JAMAIS un score de test qui n'a pas été dicté ; n'interprète pas un
  score absent.
- Attribue toujours l'information à sa source lorsque c'est cliniquement
  pertinent (« le patient rapporte… », « son épouse rapporte… »).
"""

_COGNITIF_LAYOUT = """\
# BILAN COGNITIF — CLINIQUE DE MÉMOIRE

**Date de l'évaluation :** {{DATE}}
**Demande de :** {{DEMANDEUR}}
**Accompagné de :** {{ACCOMPAGNATEUR}}

## MOTIF DE CONSULTATION

## HISTOIRE COGNITIVE
Début, mode d'installation, évolution, domaines atteints.

### Version du patient

### Hétéro-anamnèse

## IMPACT FONCTIONNEL
**AVD / AIVQ :** finances, médication, conduite, téléphone, repas, courses,
transport, ménage.

**AVQ :** hygiène, habillage, alimentation, transferts, continence.

## SYMPTÔMES NEUROPSYCHIATRIQUES ET COMPORTEMENTAUX

## ÉLÉMENTS D'ORIENTATION ÉTIOLOGIQUE
Symptômes moteurs, fluctuations, hallucinations, éléments vasculaires,
comportementaux, causes potentiellement réversibles.

## ANTÉCÉDENTS PERTINENTS

## MÉDICATION ACTUELLE
Signaler les molécules à charge anticholinergique ou sédative.

## HABITUDES DE VIE ET FACTEURS DE RISQUE

## HISTOIRE SOCIALE ET MILIEU DE VIE
Scolarité (nombre d'années, dernier diplôme), occupation antérieure, milieu de
vie, réseau de soutien, services, proche aidant et son épuisement.

## EXAMEN PHYSIQUE ET NEUROLOGIQUE

## ÉVALUATION COGNITIVE OBJECTIVE
| Test | Score | Commentaire |
|------|-------|-------------|
| MoCA | /30 | |
| MMSE | /30 | |
| Test de l'horloge | | |
| Autres | | |

## INVESTIGATIONS
Bilan sanguin (dont B12, TSH, calcémie), imagerie cérébrale, autres.

## IMPRESSION DIAGNOSTIQUE
Diagnostic syndromique (trouble neurocognitif léger ou majeur), sévérité,
hypothèse étiologique, diagnostics différentiels.

## PLAN
1. Investigations complémentaires
2. Traitement pharmacologique
3. Interventions non pharmacologiques et soutien au proche aidant
4. Références et services

## SÉCURITÉ ET ASPECTS MÉDICOLÉGAUX
Aptitude, mandat de protection, conduite automobile (SAAQ), sécurité au
domicile, hébergement.

## INFORMATION TRANSMISE AU PATIENT ET À LA FAMILLE

## SUIVI
"""

_POLYPHARMACIE_INSTRUCTIONS = """\
Tu structures une RÉVISION DE LA PHARMACOTHÉRAPIE (déprescription) chez une
personne âgée, dictée par un gériatre du Québec.

PRIORITÉS CLINIQUES :
1. Dresse la liste complète et exacte des médicaments dictés : nom, dose, voie,
   posologie, indication et prescripteur lorsque mentionnés. Inclus les produits
   en vente libre, les produits naturels et la médication PRN.
2. Repère et signale explicitement :
   - les médicaments potentiellement inappropriés (critères de Beers, STOPP/START)
   - la charge anticholinergique cumulative
   - les benzodiazépines, les hypnotiques en Z, les antipsychotiques, les
     opioïdes et les anticholinergiques urinaires
   - les cascades médicamenteuses (un médicament traitant l'effet indésirable
     d'un autre)
   - les doublons thérapeutiques et les interactions cliniquement significatives
   - les ajustements requis selon la fonction rénale ou hépatique
   - les médicaments sans indication active ou dont la durée prévue est dépassée
3. Pour CHAQUE médicament faisant l'objet d'une intervention, indique clairement
   la décision (poursuivre, cesser, sevrer, réduire, substituer, débuter),
   la justification clinique et, s'il y a lieu, le schéma de sevrage progressif
   et la surveillance requise.
4. Rends compte des objectifs de soins du patient et de ses préférences, qui
   priment sur l'application mécanique des critères.

RÈGLES DE RÉDACTION :
- N'invente jamais une dose, une molécule ou une indication non dictée : inscris
  « dose non précisée » plutôt que de deviner.
- Le tableau de médication doit rester fidèle à la dictée, sans réordonner les
  molécules de façon à en changer le sens.
"""

_POLYPHARMACIE_LAYOUT = """\
# RÉVISION DE LA PHARMACOTHÉRAPIE

**Date de l'évaluation :** {{DATE}}
**Demande de :** {{DEMANDEUR}}

## MOTIF DE LA RÉVISION

## CONTEXTE CLINIQUE
Problèmes actifs, fonction rénale et hépatique, statut cognitif et fonctionnel,
espérance de vie et objectifs de soins.

## ADHÉSION ET GESTION DE LA MÉDICATION
Pilulier, dosette, aide d'un proche ou du CLSC, pharmacie habituelle.

## MÉDICATION ACTUELLE
| Médicament | Dose et posologie | Indication | Commentaire |
|------------|-------------------|------------|-------------|

## PRODUITS EN VENTE LIBRE ET PRODUITS NATURELS

## PROBLÈMES PHARMACOTHÉRAPEUTIQUES IDENTIFIÉS
Médicaments potentiellement inappropriés, charge anticholinergique, cascades
médicamenteuses, interactions, doublons, ajustements rénaux.

## EFFETS INDÉSIRABLES SUSPECTÉS

## PLAN DE DÉPRESCRIPTION
| Médicament | Décision | Justification | Modalités et surveillance |
|------------|----------|---------------|---------------------------|

## MÉDICAMENTS À DÉBUTER OU À OPTIMISER

## DISCUSSION AVEC LE PATIENT ET LES PROCHES

## SUIVI ET SURVEILLANCE
"""

#: Marqueur d'amorçage des gabarits modifiables, rangé dans ``app_settings``.
#:
#: Ils ne doivent être créés QU'UNE FOIS. Sans ce marqueur, le mécanisme
#: d'amorçage — qui reconnaît un gabarit à son nom — recréerait « Suivi » au
#: prochain démarrage après que le médecin l'a supprimé, ce qui est exactement
#: ce qu'un gabarit « comme les autres » ne doit pas faire.
_EDITABLE_SEEDED_KEY = "editable_defaults_seeded"


def seed_locked_templates(db: Session) -> int:
    """
    Crée ou met à jour les gabarits verrouillés.

    Rafraîchis à chaque démarrage, sans précaution particulière : personne ne
    peut les avoir modifiés — c'est le sens du verrou. Une amélioration apportée
    au module profite donc immédiatement aux installations existantes.
    """
    touches = 0
    for payload in LOCKED_TEMPLATES:
        row = db.scalar(select(Template).where(Template.name == payload["name"]))
        if row is None:
            db.add(Template(**payload))
            touches += 1
            logger.info("Gabarit verrouillé « %s » créé", payload["name"])
            continue
        change = False
        for champ, valeur in payload.items():
            if getattr(row, champ) != valeur:
                setattr(row, champ, valeur)
                change = True
        if change:
            touches += 1
            logger.info("Gabarit verrouillé « %s » mis à jour", payload["name"])
    if touches:
        db.commit()
    return touches


def seed_editable_templates(db: Session) -> int:
    """
    Crée les gabarits modifiables, une seule fois dans la vie de l'installation.

    Le marqueur est posé même si rien n'est créé : sur une installation
    existante, ces gabarits sont déjà là sous leur nom, et il ne faut pas
    réessayer à chaque démarrage.
    """
    marqueur = db.get(AppSetting, _EDITABLE_SEEDED_KEY)
    if marqueur is not None:
        return 0

    existants = {name for name in db.scalars(select(Template.name))}
    crees = 0
    for payload in EDITABLE_TEMPLATES:
        if payload["name"] in existants:
            continue
        db.add(Template(**payload))
        crees += 1
        logger.info("Gabarit modifiable « %s » amorcé", payload["name"])

    db.add(AppSetting(key=_EDITABLE_SEEDED_KEY, value="1", updated_by="amorçage"))
    db.commit()
    return crees


def purge_legacy_templates(db: Session) -> int:
    """
    Supprime les gabarits livrés par une version antérieure.

    MIGRATION DESTRUCTIVE, appliquée aussi aux installations en service. Sont
    supprimés les gabarits marqués ``is_default`` dont le nom ne figure pas
    parmi les quatre retenus — c'est-à-dire « Évaluation gériatrique standard »,
    « Bilan cognitif / Clinique de mémoire », « Révision de la
    pharmacothérapie », et tout autre défaut d'une version encore plus ancienne.

    CE QUI N'EST PAS SUPPRIMÉ, ET POURQUOI
    --------------------------------------
    Un gabarit créé par le médecin (``is_default`` faux) n'est jamais touché,
    même si son nom est inconnu d'ici : ce sont ses gabarits, pas les nôtres.

    LES CONSULTATIONS PASSÉES SONT PRÉSERVÉES
    -----------------------------------------
    ``Consultation.template_name`` est un instantané pris au moment de la
    génération : le libellé du gabarit survit à sa suppression, et l'historique
    reste lisible. Aucune clé étrangère ne relie les deux tables, donc aucune
    suppression en cascade n'est possible.

    Il reste ``template_id``, qui pointerait dans le vide. On le met à NULL —
    l'identifiant d'une ligne disparue n'a aucun sens, et le nom conservé suffit
    à l'affichage.
    """
    condamnes = list(
        db.scalars(
            select(Template).where(
                Template.is_default.is_(True),
                Template.name.notin_(KEEP_NAMES),
            )
        )
    )
    if not condamnes:
        return 0

    for gabarit in condamnes:
        # Les consultations qui le référencent gardent leur nom instantané ; on
        # ne coupe que le lien devenu creux.
        orphelines = (
            db.query(Consultation)
            .filter(Consultation.template_id == gabarit.id)
            .update({"template_id": None}, synchronize_session=False)
        )
        logger.warning(
            "Gabarit livré « %s » supprimé (migration). %d consultation(s) "
            "conservent leur nom de gabarit et perdent seulement le lien.",
            gabarit.name, orphelines,
        )
        db.delete(gabarit)

    db.commit()
    return len(condamnes)


def migrate_general_prompt(db: Session) -> bool:
    """
    Reporte l'ancienne consigne générale unique vers sa version française.

    Il n'y avait qu'une consigne, rédigée en français. Elle devient
    ``general_prompt_fr`` ; la version anglaise part du module livré. L'ancienne
    clé est retirée pour qu'il n'en reste pas deux sources.
    """
    ancienne = db.get(AppSetting, "general_prompt")
    if ancienne is None:
        return False

    if db.get(AppSetting, "general_prompt_fr") is None and ancienne.value.strip():
        db.add(
            AppSetting(
                key="general_prompt_fr",
                value=ancienne.value,
                updated_by=ancienne.updated_by or "migration",
            )
        )
        logger.info(
            "Consigne générale reportée vers « general_prompt_fr » (%d caractères).",
            len(ancienne.value),
        )
    db.delete(ancienne)
    db.commit()
    return True


#: Empreintes des consignes générales LIVRÉES avant l'ajout de la règle
#: « préserver le raisonnement clinique dicté ». Une migration ne doit jamais
#: écraser une consigne que le médecin a personnalisée : tant que l'empreinte
#: en base correspond au texte livré d'origine, on peut le remplacer par le
#: nouveau défaut ; dès qu'elle diffère, c'est que le médecin l'a modifiée et
#: on n'y touche pas. Vérifié le 2026-08-13 contre ``default_prompts`` :
#: la copie en base des deux langues était strictement identique au module.
_OLD_GENERAL_PROMPT_SHA = {
    "general_prompt_fr": "fa1b793cfe032239f8cb68fee9bbfaa5848081eacd9a454f925c7d7e2b864d40",
    "general_prompt_en": "7512fc91a36551efcd30642916307d6dc285fda9f117c8e036bbd2782a81c448",
}


def migrate_general_prompt_keep_reasoning(db: Session) -> int:
    """
    Porte la règle « préserver le raisonnement clinique dicté » dans la consigne
    générale EN BASE.

    La consigne générale est éditable et vit en base (elle surcharge le module
    ``default_prompts``) : corriger le module seul laisserait l'installation en
    service avec l'ancien texte. Cette migration ne remplace la valeur que si
    elle est encore EXACTEMENT le défaut livré d'origine (comparaison par
    empreinte) ; une consigne personnalisée est laissée intacte et signalée au
    journal, le médecin devra alors ajouter la règle depuis le panneau.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration de la règle "
                "« raisonnement clinique » ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : règle « préserver le raisonnement "
            "clinique dicté » ajoutée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant l'ajout de la règle
#: « Éléments à valider prioritaire et jamais ignorée ». Même mécanique que
#: ``_OLD_GENERAL_PROMPT_SHA`` : on ne remplace la valeur en base que si elle
#: est encore EXACTEMENT le défaut livré d'origine, pour ne jamais écraser une
#: consigne personnalisée par le médecin. Vérifié le 2026-08-15 contre
#: ``default_prompts`` : la copie en base des deux langues était strictement
#: identique au module avant l'édition.
_OLD_GENERAL_PROMPT_SHA2 = {
    "general_prompt_fr": "3c635edeaa8daf6f61d4a71d76994409b45da9c34270115d469be9dbaee182f8",
    "general_prompt_en": "a01f7fa88ad7f6ed1fa051d1ed2f3df961beff58058f77b8ed9f9887ec5cf956",
}


def migrate_general_prompt_elements_a_valider(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la règle « Éléments à valider
    obligatoire » et « aucun médicament n'est jamais ignoré ».

    La consigne générale est éditable et vit en base (elle surcharge le module
    ``default_prompts``) : corriger le module seul laisserait l'installation en
    service avec l'ancien texte. Comme pour
    ``migrate_general_prompt_keep_reasoning``, la valeur n'est remplacée que si
    elle est encore EXACTEMENT le défaut livré (comparaison par empreinte) ;
    une consigne personnalisée est laissée intacte et signalée au journal.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA2.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration « Éléments à valider "
                "prioritaire » ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : « Éléments à valider » rendue "
            "obligatoire et règle « aucun médicament ignoré » ajoutée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant la reformulation de la
#: section finale (« correction apportée » / « à confirmer », jamais
#: « Confirmé »). Même mécanique que les migrations précédentes : on ne
#: remplace la valeur en base que si elle est encore EXACTEMENT le défaut
#: livré, pour ne jamais écraser une consigne personnalisée. Vérifié le
#: 2026-08-15 contre ``default_prompts`` : la copie en base des deux langues
#: était strictement identique au module avant l'édition.
_OLD_GENERAL_PROMPT_SHA3 = {
    "general_prompt_fr": "3ee3c9b120a15db38e9ab3bbcb32fd0e16b1382dff28edec786c576921f83086",
    "general_prompt_en": "7f02abbededa37f721e9a673c6384fe380d58dfa982a61979c8cd54892081c54",
}


def migrate_general_prompt_a_confirmer(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la reformulation de la section
    finale : deux mentions possibles — « correction apportée » (correction
    retenue avec confiance) ou « à confirmer » (lecture incertaine) — et
    interdiction d'écrire « Confirmé », contradictoire dans une section
    destinée à la vérification par le clinicien.

    La consigne générale est éditable et vit en base (elle surcharge le module
    ``default_prompts``) : corriger le module seul laisserait l'installation en
    service avec l'ancien texte. Comme pour les migrations précédentes, la
    valeur n'est remplacée que si elle est encore EXACTEMENT le défaut livré
    (comparaison par empreinte) ; une consigne personnalisée est laissée
    intacte et signalée au journal.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA3.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration « à confirmer » "
                "ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : section finale en « correction "
            "apportée » / « à confirmer », plus jamais « Confirmé ».",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant la levée de la contradiction
#: § 4 / § 4.1 (la rubrique « Éléments à valider » devient la seule rubrique
#: supplémentaire autorisée, en toute fin de note). Même mécanique que les
#: migrations précédentes : on ne remplace la valeur en base que si elle est
#: encore EXACTEMENT le défaut livré, pour ne jamais écraser une consigne
#: personnalisée. Vérifié le 2026-08-17 contre ``default_prompts`` : la copie
#: en base des deux langues était strictement identique au module avant l'édition.
_OLD_GENERAL_PROMPT_SHA4 = {
    "general_prompt_fr": "983578c5dc837fe52efcae8d60468f54b5b709855861256008c6e081126c0ccc",
    "general_prompt_en": "a867297c30a77ef3daa9f55e29c9b5886d6860fcd66bbb6eb6ece0eb453ce696",
}


def migrate_general_prompt_final_section(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la rubrique finale « Éléments à
    valider » rendue structurellement obligatoire.

    La section finale était exigée (§ 4.1) mais contredite par la règle
    « n'ajoute aucune rubrique absente du gabarit » (§ 4) : Gemini, qui
    reproduit fidèlement la structure du gabarit, pouvait donc l'omettre. La
    levée de la contradiction déclare « Éléments à valider » comme l'unique
    rubrique supplémentaire autorisée, toujours en toute fin de note. La
    consigne générale vit en base (elle surcharge le module ``default_prompts``)
    : corriger le module seul laisserait l'installation en service avec
    l'ancien texte. Comme pour les migrations précédentes, la valeur n'est
    remplacée que si elle est encore EXACTEMENT le défaut livré (comparaison
    par empreinte) ; une consigne personnalisée est laissée intacte et signalée
    au journal.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA4.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration « rubrique finale » "
                "ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : « Éléments à valider » rendue "
            "structurellement obligatoire (rubrique finale autorisée).",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant l'ajout des règles de
#: structure explicites : HMA et sections narratives en paragraphes (jamais en
#: liste à puces), rubrique ENTIÈRE sans contenu dicté supprimée (jamais
#: remplacée par `[inaudible]`). Même mécanique que les migrations précédentes :
#: on ne remplace la valeur en base que si elle est encore EXACTEMENT le défaut
#: livré, pour ne jamais écraser une consigne personnalisée. Vérifié le
#: 2026-08-17 contre ``default_prompts`` : la copie en base des deux langues
#: était strictement identique au module avant l'édition.
_OLD_GENERAL_PROMPT_SHA5 = {
    "general_prompt_fr": "4fdde800117c3b0de3231c8a99e899be567e43ef9a16133be8cc5024ab284147",
    "general_prompt_en": "ff56c47eed304db86be0c73922008caafdda8b7bbecb8834c201cde08bdd44e9",
}


def migrate_general_prompt_structure(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE les règles de structure explicites.

    L'ajout porte sur : HMA et sections narratives écrites en paragraphes
    courts (jamais en liste à puces), et une rubrique ENTIÈRE sans contenu
    dicté supprimée — jamais remplacée par `[inaudible]` réservé aux passages
    inintelligibles À L'INTÉRIEUR d'une rubrique qui a du contenu. Ces règles
    figurent au § 3 STYLE DE RÉDACTION et au § 1 RÈGLE ABSOLUE.

    La consigne générale est éditable et vit en base (elle surcharge le module
    ``default_prompts``) : corriger le module seul laisserait l'installation en
    service avec l'ancien texte. Comme pour les migrations précédentes, la
    valeur n'est remplacée que si elle est encore EXACTEMENT le défaut livré
    (comparaison par empreinte) ; une consigne personnalisée est laissée
    intacte et signalée au journal.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA5.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration « structure » "
                "ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : règles de structure explicites "
            "(HMA en paragraphes, rubriques vides supprimées).",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales portées par la consolidation de
#: 2026-08-19 (v2.0.0-beta.74), ANNULÉE le même jour : une régression de
#: style a été constatée à la génération, et la consolidation est retirée.
#: Cette migration remet la valeur en base sur le défaut livré (l'ancien
#: texte, de nouveau dans ``default_prompts``) UNIQUEMENT si elle est encore
#: EXACTEMENT le texte consolidé — une consigne personnalisée par le médecin
#: depuis n'est jamais écrasée.
_OLD_GENERAL_PROMPT_SHA6_UNDO = {
    "general_prompt_fr": "b30deb6dc36c8ac4ac23083b9b7d22b522d2764dd823f75e248f583f63494230",
    "general_prompt_en": "b5abf69ac51e1751a912473e0ab6d367f40b62db7d62c14bdafe30f56fe122be",
}


def migrate_general_prompt_undo_consolidation(db: Session) -> int:
    """
    Annule la consolidation de la consigne générale EN BASE.

    La consolidation (v2.0.0-beta.74) a produit une régression de style à la
    génération (voix dictée non respectée) et a été revertée du code. La
    consigne générale est éditable et vit en base : retirer le code seul
    laisserait l'installation en service avec le texte consolidé. La valeur
    n'est remplacée que si elle est encore EXACTEMENT le texte consolidé
    (comparaison par empreinte) ; une consigne personnalisée est laissée
    intacte et signalée au journal.
    """
    import hashlib

    touches = 0
    for cle, consolidee in _OLD_GENERAL_PROMPT_SHA6_UNDO.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != consolidee:
            logger.info(
                "Consigne « %s » personnalisée : annulation de la consolidation "
                "ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » rétablie : consolidation (beta.74) annulée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant l'ajout des règles
#: « aucune omission » et « hospitalisations et séjours » (2026-08-25 : un
#: séjour hospitalier antérieur dicté avait été passé sous silence dans une
#: note de suivi). Deux variantes sont acceptées par langue : le texte du
#: module et la copie en base, qui peut en différer d'un caractère (saut de
#: ligne final absent). Toute autre empreinte = consigne personnalisée par le
#: médecin → on n'y touche pas, comme pour les migrations précédentes.
_OLD_GENERAL_PROMPT_SHA_NO_OMISSION = {
    "general_prompt_fr": (
        "e2f7c2043f365374d29cd8f2ba2be7fcef7fc5180bfbec45a515b58247818d3f",
        "8cf454a7a33eb9e66ffbf55042c514c57c4635ec5e0094035bf61d3d370db01b",
    ),
    "general_prompt_en": (
        "952bf3717947c1fff54ebd750fc9692af21d27a1de2e7ee41971dfe828809b9f",
    ),
}


def migrate_general_prompt_no_omission(db: Session) -> int:
    """
    Porte les règles « aucune omission » et « hospitalisations et séjours »
    dans la consigne générale EN BASE.

    Même mécanique que ``migrate_general_prompt_undo_consolidation`` : la
    valeur n'est remplacée que si elle est encore EXACTEMENT l'un des défauts
    livrés (comparaison par empreinte) ; une consigne personnalisée est
    laissée intacte et signalée au journal — le médecin devra alors ajouter
    les règles depuis le panneau.
    """
    import hashlib

    touches = 0
    for cle, anciennes in _OLD_GENERAL_PROMPT_SHA_NO_OMISSION.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() not in anciennes:
            logger.info(
                "Consigne « %s » personnalisée : migration de la règle "
                "« aucune omission » ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : règles « aucune omission » / "
            "« hospitalisations et séjours » ajoutées.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Ancres de la consigne générale pour la règle « modifications de traitement
#: d'une visite antérieure » (2026-08-26). Contrairement aux migrations
#: précédentes, la consigne générale est ici ÉDITÉE par le médecin : on ne
#: compare pas une empreinte, on vérifie la présence EXACTE de la phrase des
#: hospitalisations et séjours (dont la règle nouvelle est le prolongement)
#: pour y accoler la clause — si la phrase a été modifiée ou supprimée, on
#: laisse la consigne intacte.
_GENERAL_PROMPT_STAYS_ANCHOR_FR = (
    "- Hospitalisations et séjours : chaque hospitalisation, visite ou séjour "
    "institutionnel mentionné (lieu, année, motif) figure dans la note ; les "
    "séjours antérieurs ne sont jamais fusionnés avec le séjour ou la visite "
    "actuelle."
)
_GENERAL_PROMPT_STAYS_ANCHOR_EN = (
    "- Hospitalizations and stays: every hospitalization, visit or "
    "institutional stay mentioned (site, year, reason) appears in the note; "
    "prior stays are never merged with the current stay or visit."
)

_GENERAL_PROMPT_TREATMENT_CLAUSE_FR = (
    "\n- Modifications de traitement d'une visite antérieure : tout "
    "médicament mentionné comme débuté, cessé, renouvelé ou avec une dose "
    "modifiée lors d'une consultation antérieure figure dans la note, dans sa "
    "rubrique (Résumé ou HMA selon le gabarit), distinct du plan de "
    "traitement actuel."
)
_GENERAL_PROMPT_TREATMENT_CLAUSE_EN = (
    "\n- Prior-visit treatment changes: any medication mentioned as started, "
    "stopped, renewed, or with a modified dose during an earlier consultation "
    "appears in the note, in its section (Summary or HPI depending on the "
    "template), distinct from the current treatment plan."
)


def migrate_general_prompt_treatment_stays(db: Session) -> int:
    """
    Ajoute la règle « modifications de traitement d'une visite antérieure »
    à la consigne générale EN BASE (2026-08-26 : « renouvelé l'Exelon et
    diminué le métoclopramide » à la visite précédente avait été passé sous
    silence).

    La consigne générale est éditée par le médecin et vit en base : on ne
    touche à la valeur que si la phrase « hospitalisations et séjours » — dont
    la règle nouvelle est le prolongement direct — y figure EXACTEMENT telle
    que livrée. Une consigne retravaillée est laissée intacte et signalée au
    journal, comme pour les migrations précédentes.
    """
    touches = 0
    for cle, ancre, clause in (
        ("general_prompt_fr", _GENERAL_PROMPT_STAYS_ANCHOR_FR, _GENERAL_PROMPT_TREATMENT_CLAUSE_FR),
        ("general_prompt_en", _GENERAL_PROMPT_STAYS_ANCHOR_EN, _GENERAL_PROMPT_TREATMENT_CLAUSE_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if clause.lstrip("\n") in row.value:
            continue  # déjà en place — idempotent
        if ancre not in row.value:
            logger.info(
                "Consigne « %s » : phrase des hospitalisations introuvable, "
                "règle des modifications de traitement laissée au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancre, ancre + clause)
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » : règle « modifications de traitement d'une "
            "visite antérieure » ajoutée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Ancres de la consigne générale pour la règle « le Plan conserve le « je »
#: dicté » (2026-08-26). Deux versions successives de la même puce du § 3
#: « Impression et Plan » : la consigne est éditable et vit en base, on ne
#: remplace l'ancienne puce que si elle est encore EXACTEMENT le texte livré
#: d'origine, pour ne jamais écraser une instruction personnalisée par le
#: médecin.
_PLAN_FIRST_PERSON_OLD_FR = (
    "Dictées à la première personne, elles se transcrivent telles quelles : "
    "« Je crois qu'il s'agit d'une maladie d'Alzheimer » reste tel quel — "
    "jamais « Maladie d'Alzheimer » ni « Le médecin croit… » ; « Je lui donne "
    "congé de la clinique » reste tel quel — jamais « Congé de la clinique » "
    "ni « Il lui donne congé »."
)
_PLAN_FIRST_PERSON_OLD_EN = (
    'Dictated in the first person, they are transcribed as-is: "I believe '
    'this is Alzheimer\'s disease" stays as-is — never "Alzheimer\'s disease" '
    'nor "The physician believes…" ; "I am discharging her from the clinic" '
    'stays as-is — never "Discharged from the clinic" nor "She is discharged".'
)
_PLAN_FIRST_PERSON_NEW_FR = (
    "Dictées à la première personne, elles se transcrivent telles quelles : "
    "le « je » dicté est TOUJOURS conservé, jamais effacé, jamais réduit à "
    "l'infinitif, au substantif ou à la voix passive. « Je crois qu'il "
    "s'agit d'une maladie d'Alzheimer » reste tel quel — jamais « Maladie "
    "d'Alzheimer » ni « Le médecin croit… » ; « Je lui donne congé de la "
    "clinique » reste tel quel — jamais « Congé de la clinique » ni « Il lui "
    "donne congé ». Dans le Plan : « Je renouvelle son Exelon pour un an » "
    "reste tel quel — jamais « Renouveler son Exelon pour un an », "
    "« Renouvellement de l'Exelon pour un an » ni « Son Exelon est renouvelé "
    "pour un an » ; « Je cesse le Maxeran » reste tel quel — jamais « Cesser "
    "le Maxeran ». Une action dictée sans pronom se transcrit sans pronom : "
    "le Plan respecte strictement la personne grammaticale dictée, sans "
    "normaliser."
)
_PLAN_FIRST_PERSON_NEW_EN = (
    'Dictated in the first person, they are transcribed as-is: the dictated '
    '"I" is ALWAYS kept, never dropped, never reduced to an infinitive, a '
    'noun phrase or the passive voice. "I believe this is Alzheimer\'s '
    'disease" stays as-is — never "Alzheimer\'s disease" nor "The physician '
    'believes…" ; "I am discharging her from the clinic" stays as-is — never '
    '"Discharged from the clinic" nor "She is discharged". In the Plan: "I '
    'am renewing her Exelon for a year" stays as-is — never "Renew her Exelon '
    'for a year", "Renewal of Exelon for a year" nor "Her Exelon is renewed '
    'for a year"; "I am stopping the Maxeran" stays as-is — never "Stop the '
    'Maxeran". An action dictated without a subject is transcribed without '
    "one: the Plan strictly respects the dictated grammatical person, "
    "without normalizing it."
)


def migrate_general_prompt_plan_first_person(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la reformulation de la règle du
    § 3 « Impression et Plan » : le « je » dicté est TOUJOURS conservé dans
    le Plan (2026-08-26 : « Je renouvelle son Exelon pour un an » était
    transcrit « Renouveler son Exelon pour un an »).

    Même mécanique que ``migrate_general_prompt_treatment_stays`` : la
    consigne générale est éditée par le médecin et vit en base, on ne
    remplace l'ancienne puce que si elle est encore EXACTEMENT le texte livré
    d'origine. Une puce retravaillée est laissée intacte et signalée au
    journal.
    """
    touches = 0
    for cle, ancienne, nouvelle in (
        ("general_prompt_fr", _PLAN_FIRST_PERSON_OLD_FR, _PLAN_FIRST_PERSON_NEW_FR),
        ("general_prompt_en", _PLAN_FIRST_PERSON_OLD_EN, _PLAN_FIRST_PERSON_NEW_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if nouvelle in row.value:
            continue  # déjà en place — idempotent
        if ancienne not in row.value:
            logger.info(
                "Consigne « %s » : puce « Impression et Plan » modifiée, "
                "règle du « je » conservé laissée au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancienne, nouvelle)
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » : règle « le Plan conserve le « je » dicté » "
            "appliquée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Ancres de la consigne générale pour la règle « aucune omission dans
#: l'Impression ni le Plan » (2026-08-27). La puce du § 3 « Impression et
#: Plan » « Conserve intégralement le raisonnement clinique dicté » est le
#: point d'ancrage : on y accole la nouvelle puce uniquement si elle y figure
#: encore EXACTEMENT telle que livrée — la consigne est éditée par le médecin
#: et vit en base, une puce retravaillée est laissée intacte.
_IMPRESSION_PLAN_NO_OMISSION_OLD_FR = (
    "  - Conserve **intégralement** le raisonnement clinique dicté — revue des "
    "effets secondaires d'un traitement, cause écartée ou retenue, hypothèse et "
    "ce qui l'appuie — même long, même s'il ressemble à une justification : ne "
    "le résume pas, ne le supprime pas. C'est une donnée clinique au même titre "
    "qu'un diagnostic."
)
_IMPRESSION_PLAN_NO_OMISSION_OLD_EN = (
    '  - Preserve **in full** the dictated clinical reasoning — review of a '
    "treatment's side effects, cause excluded or retained, hypothesis and what "
    "supports it — however long, even if it reads like a justification: do not "
    "summarize it, do not drop it. It is clinical data just like a diagnosis."
)
_IMPRESSION_PLAN_NO_OMISSION_NEW_FR = (
    _IMPRESSION_PLAN_NO_OMISSION_OLD_FR
    + "\n"
    + "  - **Aucune omission dans l'Impression ni le Plan** : chaque "
    "impression, hypothèse ou jugement clinique dicté figure dans "
    "l'Impression, même subjectif, même contradictoire avec un résultat "
    "objectif — « MMSE stable voire amélioré, mais j'ai l'impression qu'il se "
    "détériore au niveau amnésique » conserve les deux faits, le contraste est "
    "le propos, jamais résolu ni réduit au seul résultat. Chaque action ou "
    "recommandation dictée figure dans le Plan sur sa propre ligne numérotée : "
    "délai de suivi (« à revoir dans 6 mois », « retour dans un mois »), "
    "examen ou investigation demandé, référence, renouvellement, cessation, "
    "congé — même brefs, même sans verbe. Un délai de suivi est une décision "
    "clinique : jamais écarté, jamais fusionné dans une autre action, jamais "
    "résumé dans une formulation plus générale."
)
_IMPRESSION_PLAN_NO_OMISSION_NEW_EN = (
    _IMPRESSION_PLAN_NO_OMISSION_OLD_EN
    + "\n"
    + '  - **No omission in the Impression or the Plan** : every dictated '
    "impression, hypothesis or clinical judgment appears in the Impression, "
    "even subjective, even contradicting an objective result — \"MMSE stable "
    "or even improved, but I have the impression he is deteriorating "
    "cognitively\" keeps both facts; the contrast is the point, never resolved "
    "or reduced to the sole result. Every dictated action or recommendation "
    "appears in the Plan on its own numbered line: follow-up interval (\"to be "
    "seen again in 6 months\", \"return in one month\"), requested test or "
    "investigation, referral, renewal, stop, discharge — however brief, even "
    "without a verb. A follow-up interval is a clinical decision: never "
    "dropped, never merged into another action, never summarized into a "
    "broader formulation."
)


def migrate_general_prompt_impression_plan_no_omission(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la règle « aucune omission dans
    l'Impression ni le Plan » (2026-08-27 : l'impression subjective du médecin
    contradictoire avec un résultat objectif — « j'ai l'impression qu'il se
    détériore au niveau amnésique » malgré un MMSE stable — et le délai de
    suivi dicté « à revoir à six mois » avaient été omis de la note).

    Même mécanique que ``migrate_general_prompt_plan_first_person`` : la
    consigne générale est éditée par le médecin et vit en base, la nouvelle
    puce n'est ajoutée que si la puce « Conserve intégralement le raisonnement
    clinique dicté » y figure encore EXACTEMENT telle que livrée. Une puce
    retravaillée est laissée intacte et signalée au journal.
    """
    touches = 0
    for cle, ancienne, nouvelle in (
        ("general_prompt_fr", _IMPRESSION_PLAN_NO_OMISSION_OLD_FR, _IMPRESSION_PLAN_NO_OMISSION_NEW_FR),
        ("general_prompt_en", _IMPRESSION_PLAN_NO_OMISSION_OLD_EN, _IMPRESSION_PLAN_NO_OMISSION_NEW_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if nouvelle in row.value:
            continue  # déjà en place — idempotent
        if ancienne not in row.value:
            logger.info(
                "Consigne « %s » : puce « Impression et Plan » modifiée, "
                "règle « aucune omission dans l'Impression ni le Plan » "
                "laissée au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancienne, nouvelle)
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » : règle « aucune omission dans l'Impression ni "
            "le Plan » appliquée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Règle « jamais de nom de médicament flottant dans le Plan » (2026-08-31,
#: note 13) : un nom de médicament dicté seul, sans verbe d'action, sans dose,
#: sans voie ni aucun élément de posologie (« diclofenac diethylamine » entre
#: deux actions) n'est pas écrit comme une prescription dans le Plan — il passe
#: en « Corrections et éléments à valider ». Ancrage : la puce « Aucune
#: omission dans l'Impression ni le Plan » livrée le 2026-08-27 ; la consigne
#: est éditée par le médecin et vit en base, une puce retravaillée est laissée
#: intacte et signalée au journal.
_IMPRESSION_PLAN_MED_NU_OLD_FR = _IMPRESSION_PLAN_NO_OMISSION_NEW_FR
_IMPRESSION_PLAN_MED_NU_OLD_EN = _IMPRESSION_PLAN_NO_OMISSION_NEW_EN
_IMPRESSION_PLAN_MED_NU_NEW_FR = (
    _IMPRESSION_PLAN_MED_NU_OLD_FR
    + "\n"
    + "  - **Jamais de nom de médicament « flottant » dans le Plan** : un nom "
    "de médicament dicté seul, sans verbe d'action, sans dose, sans voie ni "
    "aucun élément de posologie, n'est jamais écrit comme une ligne de Plan "
    "affirmée — comme s'il s'agissait d'une prescription. Il figure en "
    "« Corrections et éléments à valider » comme mention à confirmer. Une "
    "vraie action dictée avec son médicament (« on commence Zyprexa 2,5 mg "
    "HS ») reste une ligne de Plan."
)
_IMPRESSION_PLAN_MED_NU_NEW_EN = (
    _IMPRESSION_PLAN_MED_NU_OLD_EN
    + "\n"
    + "  - **No floating drug name in the Plan** : a drug name dictated "
    "alone, without an action verb, without a dose, route or any dosing "
    "element, is never written as an asserted Plan line — as if it were a "
    "prescription. It goes to \u201cCorrections and items to verify\u201d as "
    "a mention to confirm. A real dictated action with its medication "
    '("start Zyprexa 2.5 mg HS") stays a Plan line.'
)


def migrate_general_prompt_plan_medicament_nu(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la règle « jamais de nom de
    médicament flottant dans le Plan » (2026-08-31, note 13). Même mécanique
    que les migrations précédentes : la consigne générale est éditée par le
    médecin et vit en base, la nouvelle puce n'est ajoutée que si la puce
    « Aucune omission dans l'Impression ni le Plan » y figure encore
    EXACTEMENT telle que livrée. Une puce retravaillée est laissée intacte.
    """
    touches = 0
    for cle, ancienne, nouvelle in (
        ("general_prompt_fr", _IMPRESSION_PLAN_MED_NU_OLD_FR, _IMPRESSION_PLAN_MED_NU_NEW_FR),
        ("general_prompt_en", _IMPRESSION_PLAN_MED_NU_OLD_EN, _IMPRESSION_PLAN_MED_NU_NEW_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if nouvelle in row.value:
            continue  # déjà en place — idempotent
        if ancienne not in row.value:
            logger.info(
                "Consigne « %s » : puce « Impression et Plan » modifiée, "
                "règle « jamais de nom de médicament flottant dans le Plan » "
                "laissée au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancienne, nouvelle)
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » : règle « jamais de nom de médicament flottant "
            "dans le Plan » appliquée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Ancres de la consigne générale pour le placement des hospitalisations
#: (2026-08-26). La phrase « Hospitalisations et séjours » du § 1 est le
#: point d'ancrage : on y accole la clause de placement uniquement si elle y
#: figure encore EXACTEMENT telle que livrée — la consigne est éditée par le
#: médecin et vit en base, une phrase retravaillée est laissée intacte.
_ANTECEDENTS_PLACEMENT_OLD_FR = (
    "- Hospitalisations et séjours : chaque hospitalisation, visite ou séjour "
    "institutionnel mentionné (lieu, année, motif) figure dans la note ; les "
    "séjours antérieurs ne sont jamais fusionnés avec le séjour ou la visite "
    "actuelle."
)
_ANTECEDENTS_PLACEMENT_OLD_EN = (
    "- Hospitalizations and stays: every hospitalization, visit or "
    "institutional stay mentioned (site, year, reason) appears in the note; "
    "prior stays are never merged with the current stay or visit."
)

_ANTECEDENTS_PLACEMENT_NEW_FR = (
    _ANTECEDENTS_PLACEMENT_OLD_FR
    + " Le placement suit la dictée : une hospitalisation ou un séjour "
    "ANTÉRIEUR dicté pendant l'énumération des antécédents figure dans la "
    "rubrique des antécédents du gabarit, contexte et synthèse dictés compris "
    "— il n'est pas déplacé vers l'HMA, dont le récit ne couvre que le motif "
    "actuel de la consultation."
)
_ANTECEDENTS_PLACEMENT_NEW_EN = (
    _ANTECEDENTS_PLACEMENT_OLD_EN
    + " Placement follows the dictation: a PAST hospitalization or stay "
    "dictated during the past history listing stays in the past history "
    "section of the template, including the dictated context and summary — it "
    "is not moved to the HPI, whose narrative covers only the current reason "
    "for the consultation."
)


#: Règle « le modèle résout les noms de médicaments déformés » (2026-08-31,
#: approche A : « donner des outils au LLM »). Le grounding ne réécrit plus
#: inline que ce qu'il sait déterministiquement ; les noms déformés restants
#: (Monocore, antoloque, pantoloque…) sont laissés au modèle, qui reçoit les
#: candidats sûrs comme hints. Ancrage : la ligne « Liste pointée, nom + dose »
#: du § 4 MÉDICAMENTS livrée dans GENERAL_PROMPT ; la consigne est éditée par
#: le médecin et vit en base — une ligne retravaillée est laissée intacte.
_MEDS_RESOLUTION_OLD_FR = (
    "- Liste pointée, nom + dose, sans titres ni colonnes ; une ligne par "
    "médicament ou par groupe."
)
_MEDS_RESOLUTION_OLD_EN = (
    "- Bulleted list, name + dose, no headings or columns; one line per "
    "medication or group."
)
_MEDS_RESOLUTION_NEW_FR = (
    _MEDS_RESOLUTION_OLD_FR
    + "\n"
    + "- **Les noms déformés par la reconnaissance vocale sont parfois laissés "
    "TELS QUELS dans la transcription** (le moteur automatique de correction "
    "n'intervient que lorsqu'il est sûr). Reconnais le médicament réel à "
    "l'aide de la posologie et du contexte clinique, et écris son nom CORRECT "
    "dans la note. Exemples : « pantoloque 40 » → Pantoloc 40 ; « Monocore "
    "1,25 mg » → Monocor (bisoprolol) 1,25 mg ; « sélexa » → Celexa. Ne "
    "recopie jamais un nom manifestement déformé tel quel. Les « médicaments "
    "détectés » fournis dans le prompt sont des pistes à confirmer avec la "
    "dictée, jamais des vérités à recopier aveuglément — un candidat "
    "incohérent avec la pathologie ou la posologie est écarté (règle des "
    "éléments douteux, § 1)."
)
_MEDS_RESOLUTION_NEW_EN = (
    _MEDS_RESOLUTION_OLD_EN
    + "\n"
    + "- **Drug names deformed by speech recognition are sometimes left AS-IS "
    "in the transcript** (the automatic correction engine only intervenes "
    "when it is sure). Identify the real medication using the dosage and the "
    "clinical context, and write its CORRECT name in the note. Examples: "
    '"pantoloque 40" → Pantoloc 40; "Monocore 1.25 mg" → Monocor (bisoprolol) '
    '1.25 mg; "sélexa" → Celexa. Never copy a clearly deformed name as-is. '
    'The "detected medications" provided in the prompt are leads to be '
    'confirmed against the dictation, never truths to copy blindly — a '
    'candidate inconsistent with the pathology or dosage is set aside '
    "(doubtful-items rule, section 1)."
)


def migrate_general_prompt_meds_resolution(db: Session) -> int:
    """Porte dans la consigne générale EN BASE la règle de résolution des
    noms de médicaments déformés par le modèle (2026-08-31, approche « donner
    des outils au LLM »). Même mécanique que les migrations précédentes : la
    ligne « Liste pointée, nom + dose » (ou « Bulleted list, name + dose »)
    sert d'ancrage et doit y figurer EXACTEMENT ; une ligne retravaillée est
    laissée intacte et signalée au journal."""
    touches = 0
    for cle, ancienne, nouvelle in (
        ("general_prompt_fr", _MEDS_RESOLUTION_OLD_FR, _MEDS_RESOLUTION_NEW_FR),
        ("general_prompt_en", _MEDS_RESOLUTION_OLD_EN, _MEDS_RESOLUTION_NEW_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if nouvelle in row.value:
            continue  # déjà en place — idempotent
        if ancienne not in row.value:
            logger.info(
                "Consigne « %s » : ligne « Liste pointée, nom + dose » "
                "modifiée, règle de résolution des noms de médicaments laissée "
                "au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancienne, nouvelle)
        row.updated_by = "migration"
        touches += 1
        logger.info("Consigne « %s » : règle meds-resolution appliquée.", cle)
    if touches:
        db.commit()
    return touches


def migrate_general_prompt_antecedents_hospitalisation_placement(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE la règle de placement des
    hospitalisations antérieures (2026-08-26 : une hospitalisation dictée
    pendant l'énumération des antécédents — lieu, année, motif et synthèse —
    était déplacée par le modèle vers l'HMA).

    Même mécanique que ``migrate_general_prompt_plan_first_person`` : la
    consigne générale est éditée par le médecin et vit en base, la clause
    n'est ajoutée que si la phrase « Hospitalisations et séjours » y figure
    encore EXACTEMENT telle que livrée. Une phrase retravaillée est laissée
    intacte et signalée au journal.
    """
    touches = 0
    for cle, ancienne, nouvelle in (
        ("general_prompt_fr", _ANTECEDENTS_PLACEMENT_OLD_FR, _ANTECEDENTS_PLACEMENT_NEW_FR),
        ("general_prompt_en", _ANTECEDENTS_PLACEMENT_OLD_EN, _ANTECEDENTS_PLACEMENT_NEW_EN),
    ):
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if nouvelle in row.value:
            continue  # déjà en place — idempotent
        if ancienne not in row.value:
            logger.info(
                "Consigne « %s » : phrase « Hospitalisations et séjours » "
                "modifiée, placement des hospitalisations laissé au panneau.",
                cle,
            )
            continue
        row.value = row.value.replace(ancienne, nouvelle)
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » : règle de placement des hospitalisations "
            "appliquée.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Empreintes des consignes générales LIVRÉES avant l'ajout du § 2.0
#: « l'erreur de reconnaissance est phonétique, pas une faute de frappe »
#: (2026-08-28). Même mécanique que les migrations précédentes : on ne remplace
#: la valeur en base que si elle est encore EXACTEMENT le défaut livré, pour ne
#: jamais écraser une consigne personnalisée. Vérifié le 2026-08-28 contre
#: ``default_prompts`` : la copie en base des deux langues était strictement
#: identique au module avant l'édition.
_OLD_GENERAL_PROMPT_SHA_PHONETIC = {
    "general_prompt_fr": "3540a19fde206b533eb96497ef7a84f50c5da81125495b8e4c9494d280bba045",
    "general_prompt_en": "55d1446373714625d05218895a3bc914e3065e792e4cae3a024313338ca32bec",
}


def migrate_general_prompt_phonetic_origin(db: Session) -> int:
    """
    Porte dans la consigne générale EN BASE le § 2.0 « l'erreur de
    reconnaissance est phonétique, pas une faute de frappe ».

    (2026-08-28 : les modèles traitent la transcription comme un texte tapé et
    cherchent des fautes de frappe, alors que les erreurs de la reconnaissance
    vocale sont phonétiques — homophones souvent parfaitement orthographiés —
    et qu'une correction est un rétablissement du mot dicté, pas un ajout
    d'information.) La consigne générale est éditable et vit en base : corriger
    le module seul laisserait l'installation en service avec l'ancien texte.
    Comme pour les migrations précédentes, la valeur n'est remplacée que si elle
    est encore EXACTEMENT le défaut livré (comparaison par empreinte) ; une
    consigne personnalisée est laissée intacte et signalée au journal.
    """
    import hashlib

    touches = 0
    for cle, ancienne in _OLD_GENERAL_PROMPT_SHA_PHONETIC.items():
        row = db.get(AppSetting, cle)
        if row is None or not row.value.strip():
            continue
        if hashlib.sha256(row.value.encode()).hexdigest() != ancienne:
            logger.info(
                "Consigne « %s » personnalisée : migration du § 2.0 "
                "« erreurs phonétiques » ignorée (laissez-la telle quelle).",
                cle,
            )
            continue
        nouveau = default_prompts.PROMPTS.get("fr" if cle.endswith("_fr") else "en")
        if row.value == nouveau:
            continue
        row.value = nouveau
        row.updated_by = "migration"
        touches += 1
        logger.info(
            "Consigne « %s » mise à jour : § 2.0 « l'erreur de reconnaissance "
            "est phonétique, pas une faute de frappe » ajouté.",
            cle,
        )
    if touches:
        db.commit()
    return touches


#: Ancienne phrase de regroupement des médicaments LIVRÉE dans les gabarits
#: avant la reformulation « indication dictée ou cliniquement évidente ».
#: Même mécanique que les migrations de la consigne générale : on ne remplace
#: la phrase que si elle est encore EXACTEMENT le texte livré d'origine, pour
#: ne jamais écraser une instruction personnalisée par le médecin.
_OLD_MED_GROUPING_SENTENCES = (
    "Regroupe un même traitement en une ligne lorsqu'il sert la même indication (ex. « Metformine 500 mg PO bid, Diamicron MR 90 mg PO die »).",
    'Group a single treatment on one line when it serves the same indication (e.g. "Metformin 500 mg PO BID, Diamicron MR 90 mg PO daily").',
)

#: Phrases de remplacement (une par langue, même ordre que ci-dessus).
_NEW_MED_GROUPING_SENTENCES = (
    "Regroupe sur une même ligne les médicaments qui servent la même indication lorsque celle-ci est dictée ou cliniquement évidente — deux laxatifs, deux opioïdes, deux hypoglycémiants par exemple — en plaçant l'indication entre parenthèses en fin de ligne (« Senokot 1 comprimé PO HS, Lax-A-Day 17 g PO die (constipation) »). En cas de doute sur une indication commune, conserve une ligne par médicament.",
    'Group on a single line the medications that serve the same indication when that indication is dictated or clinically obvious — for example two laxatives, two opioids, two antihyperglycemics — placing the indication in parentheses at the end of the line ("Senokot 1 tablet PO HS, Lax-A-Day 17 g PO daily (constipation)"). When in doubt about a shared indication, keep one medication per line.',
)


def migrate_template_med_grouping(db: Session) -> int:
    """
    Porte dans les gabarits EN BASE la reformulation de la règle de
    regroupement des médicaments (« même indication » → « dictée ou
    cliniquement évidente »).

    Les gabarits verrouillés sont déjà rafraîchis depuis ``default_templates``
    par ``seed_locked_templates`` : cette migration ne concerne donc que les
    copies modifiables (dupliquées avant la reformulation). On ne remplace la
    phrase que si elle est encore EXACTEMENT le texte livré d'origine — une
    instruction personnalisée est laissée intacte et signalée au journal.
    """
    touches = 0
    for row in db.scalars(select(Template)).all():
        inst = row.system_instructions or ""
        for ancienne, nouvelle in zip(
            _OLD_MED_GROUPING_SENTENCES, _NEW_MED_GROUPING_SENTENCES
        ):
            if ancienne in inst:
                row.system_instructions = inst.replace(ancienne, nouvelle)
                touches += 1
                logger.info(
                    "Gabarit « %s » : règle de regroupement des médicaments "
                    "reformulée (indication dictée ou cliniquement évidente).",
                    row.name,
                )
                break
    if touches:
        db.commit()
    return touches


#: Fragment « Résumé » LIVRÉ dans le gabarit « Suivi - Gériatrie » avant
#: l'ajout de la mention explicite des hospitalisations antérieures. Même
#: mécanique que les autres migrations : on ne remplace le fragment que s'il
#: y figure EXACTEMENT tel que livré. Deux casses sont couvertes : la phrase
#: livrée (« Résumé : les faits… ») et la variante retitrée des copies
#: markdown (« **Résumé.** Les faits… »).
_OLD_SUIVI_RESUME_FRAGMENTS = (
    "les faits saillants, les antécédents importants, ce qui est nouveau depuis la dernière visite",
    "Les faits saillants, les antécédents importants, ce qui est nouveau depuis la dernière visite",
)

#: Fragments de remplacement (un par ancien, même ordre).
_NEW_SUIVI_RESUME_FRAGMENTS = (
    "les faits saillants, les antécédents importants — y compris toute "
    "hospitalisation antérieure (lieu, année, motif) —, ce qui est nouveau "
    "depuis la dernière visite",
    "Les faits saillants, les antécédents importants — y compris toute "
    "hospitalisation antérieure (lieu, année, motif) —, ce qui est nouveau "
    "depuis la dernière visite",
)


def migrate_template_suivi_resume_stays(db: Session) -> int:
    """
    Porte dans les copies modifiables du gabarit « Suivi - Gériatrie » la
    mention explicite des hospitalisations antérieures dans la règle du
    Résumé (2026-08-25 : un séjour hospitalier antérieur dicté avait été
    passé sous silence).

    Les gabarits verrouillés sont déjà rafraîchis depuis
    ``default_templates`` par ``seed_locked_templates`` : cette migration ne
    concerne donc que les copies dupliquées avant la reformulation. On ne
    remplace le fragment que s'il y est encore EXACTEMENT le texte livré
    d'origine — une règle du Résumé déjà retravaillée autrement est laissée
    intacte et signalée au journal.
    """
    touches = 0
    for row in db.scalars(select(Template)).all():
        inst = row.system_instructions or ""
        for ancienne, nouvelle in zip(
            _OLD_SUIVI_RESUME_FRAGMENTS, _NEW_SUIVI_RESUME_FRAGMENTS
        ):
            if ancienne in inst:
                row.system_instructions = inst.replace(ancienne, nouvelle)
                touches += 1
                logger.info(
                    "Gabarit « %s » : règle du Résumé enrichie "
                    "(hospitalisation antérieure : lieu, année, motif).",
                    row.name,
                )
                break
    if touches:
        db.commit()
    return touches


#: Fragment « Résumé » LIVRÉ dans le gabarit « Suivi - Gériatrie » avant
#: l'ajout de la mention des modifications de traitement d'une visite
#: antérieure. Même mécanique que les migrations précédentes : on ne remplace
#: le fragment que s'il y figure EXACTEMENT tel que livré. Le fragment est
#: commun aux trois gabarits « Suivi » (Gériatrie, copie GB, FD) — le milieu
#: de la phrase du Résumé ne varie pas entre les casses livrées.
_OLD_SUIVI_RESUME_TREATMENT_FRAGMENT = (
    "ce qui est nouveau depuis la dernière visite, et l'autonomie fonctionnelle antérieure"
)

_NEW_SUIVI_RESUME_TREATMENT_FRAGMENT = (
    "ce qui est nouveau depuis la dernière visite, toute modification du plan de "
    "traitement mentionnée pour une visite antérieure (médicament débuté, cessé, "
    "renouvelé, dose modifiée), et l'autonomie fonctionnelle antérieure"
)


def migrate_template_suivi_resume_treatment_stays(db: Session) -> int:
    """
    Porte dans les copies modifiables du gabarit « Suivi - Gériatrie » la
    mention des modifications de traitement effectuées à une visite antérieure
    dans la règle du Résumé (2026-08-26 : « renouvelé l'Exelon et diminué le
    métoclopramide » à la visite précédente avait été passé sous silence).

    Même mécanique que ``migrate_template_suivi_resume_stays`` : fragment
    remplacé seulement s'il y est EXACTEMENT le texte livré, copie intacte
    sinon (les gabarits verrouillés sont rafraîchis par
    ``seed_locked_templates`` depuis ``default_templates``).
    """
    touches = 0
    for row in db.scalars(select(Template)).all():
        inst = row.system_instructions or ""
        if _OLD_SUIVI_RESUME_TREATMENT_FRAGMENT in inst:
            row.system_instructions = inst.replace(
                _OLD_SUIVI_RESUME_TREATMENT_FRAGMENT,
                _NEW_SUIVI_RESUME_TREATMENT_FRAGMENT,
            )
            touches += 1
            logger.info(
                "Gabarit « %s » : règle du Résumé enrichie (modifications de "
                "traitement d'une visite antérieure).",
                row.name,
            )
    if touches:
        db.commit()
    return touches


#: Phrases « Antécédents » / « Past medical history » LIVRÉES dans les
#: gabarits de consultation avant l'ajout de la mention explicite des
#: hospitalisations antérieures (2026-08-26). Même mécanique que les autres
#: migrations : on ne remplace une phrase que si elle y figure EXACTEMENT
#: telle que livrée.
_ANTECEDENTS_HOSP_OLD = (
    "**Antécédents.** Liste pointée, une ligne par antécédent dicté (médical, chirurgical, familial) — uniquement ce qui est dicté.",
    "**Past medical history.** Bulleted list, one line per dictated item (medical, surgical, family) — only what was dictated.",
    "**Antécédents.** Liste pointée ; antécédents médicaux et chirurgicaux dictés uniquement.",
)

_ANTECEDENTS_HOSP_NEW = (
    "**Antécédents.** Liste pointée, une ligne par antécédent dicté (médical, chirurgical, familial) — uniquement ce qui est dicté. Les hospitalisations et séjours antérieurs dictés (lieu, année, motif et synthèse) figurent dans cette liste, jamais dans l'HMA.",
    "**Past medical history.** Bulleted list, one line per dictated item (medical, surgical, family) — only what was dictated. Past hospitalizations or stays dictated here (site, year, reason and summary) stay in this list, never in the HPI.",
    "**Antécédents.** Liste pointée ; antécédents médicaux et chirurgicaux dictés uniquement, y compris les hospitalisations et séjours antérieurs dictés (lieu, année, motif et synthèse) — ils figurent ici, jamais dans l'HMA.",
)


def migrate_template_antecedents_hospitalisation_placement(db: Session) -> int:
    """
    Porte dans les copies modifiables des gabarits de consultation la mention
    explicite que les hospitalisations antérieures dictées dans les
    antécédents y restent (2026-08-26 : le modèle les déplaçait vers l'HMA).

    Les gabarits verrouillés sont déjà rafraîchis depuis ``default_templates``
    par ``seed_locked_templates`` : cette migration ne concerne donc que les
    copies dupliquées avant la reformulation. On ne remplace la phrase que si
    elle y est encore EXACTEMENT le texte livré d'origine — une règle des
    antécédents déjà retravaillée autrement est laissée intacte et signalée au
    journal.
    """
    touches = 0
    for row in db.scalars(select(Template)).all():
        inst = row.system_instructions or ""
        for ancienne, nouvelle in zip(_ANTECEDENTS_HOSP_OLD, _ANTECEDENTS_HOSP_NEW):
            if nouvelle in inst:
                continue  # déjà en place — idempotent
            if ancienne in inst:
                row.system_instructions = inst.replace(ancienne, nouvelle)
                touches += 1
                logger.info(
                    "Gabarit « %s » : règle des antécédents enrichie "
                    "(hospitalisations antérieures conservées ici, jamais "
                    "dans l'HMA).",
                    row.name,
                )
                break
    if touches:
        db.commit()
    return touches


#: Groupes livrés. « admins » ouvre le panneau d'administration, « users » ne
#: donne accès qu'à ses propres consultations.
_SYSTEM_GROUPS = (
    {
        "name": "admins",
        "description": "Accès complet : réglages, gabarits, comptes et groupes.",
        "is_admin": True,
        "can_manage_templates": True,
    },
    {
        "name": "users",
        "description": "Dicter, relire et exporter ses propres consultations.",
        "is_admin": False,
        "can_manage_templates": False,
    },
)

ADMIN_GROUP = "admins"
DEFAULT_GROUP = "users"


def seed_groups(db: Session) -> None:
    """Crée les groupes livrés s'ils manquent. Ne touche pas à leurs permissions."""
    existants = {name for name in db.scalars(select(Group.name))}
    for payload in _SYSTEM_GROUPS:
        if payload["name"] in existants:
            continue
        db.add(Group(is_system=True, **payload))
    db.commit()


def _adopt_legacy_owners(db: Session) -> int:
    """
    Crée un compte pour chaque propriétaire de consultation antérieur.

    POURQUOI C'EST NÉCESSAIRE
    -------------------------
    Avant l'authentification OIDC, une consultation appartenait à la valeur d'un
    en-tête HTTP (``Remote-User``), qui n'était pas forcément une adresse de
    courriel. Ces chaînes sont toujours dans ``Consultation.owner`` : sans compte
    correspondant, les brouillons existants n'auraient plus de propriétaire
    identifiable et disparaîtraient de l'écran.

    Le premier compte adopté entre dans ``admins`` : une installation migrée doit
    garder quelqu'un capable d'ouvrir le panneau.

    L'adresse de courriel n'est devinée que dans le cas non ambigu — un seul
    propriétaire existant et une seule entrée dans ``AUTHORIZED_USERS``. C'est la
    situation d'une installation à un seul médecin, et elle permet au
    rattachement par courriel de fonctionner à la première connexion. Au-delà,
    on ne devine pas : associer deux personnes par erreur donnerait à l'une les
    consultations de l'autre.
    """
    # Le compte ET le nombre de consultations : ce dernier est journalisé, et
    # compter sur une liste dédoublonnée aurait toujours donné 1.
    from sqlalchemy import func

    par_proprietaire = {
        (owner or "").strip().lower(): total
        for owner, total in db.execute(
            select(Consultation.owner, func.count()).group_by(Consultation.owner)
        )
        if (owner or "").strip()
    }
    proprietaires = sorted(par_proprietaire)
    if not proprietaires:
        return 0

    connus = {u for u in db.scalars(select(User.username))}
    a_creer = [o for o in proprietaires if o not in connus]
    if not a_creer:
        return 0

    courriels = [a.strip().lower() for a in settings.authorized_users if a.strip() != "*"]
    courriel_unique = (
        courriels[0] if len(a_creer) == 1 and len(courriels) == 1 else ""
    )

    admin = db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    premier_compte = not connus

    for index, nom in enumerate(a_creer):
        # Le courriel deviné ne l'est que s'il diffère du nom d'usager : si les
        # deux sont identiques, il n'y a rien à deviner.
        courriel = courriel_unique if courriel_unique and courriel_unique != nom else ""
        # display_name volontairement VIDE : le recopier depuis le nom d'usager
        # en ferait un bouche-trou que la première connexion prendrait pour une
        # valeur voulue. L'affichage retombe de lui-même sur le nom d'usager
        # tant que le fournisseur n'a rien envoyé.
        user = User(username=nom, email=courriel, display_name="")
        db.add(user)
        db.flush()
        if admin is not None and premier_compte and index == 0:
            db.add(UserGroup(user_id=user.id, group_id=admin.id))
        logger.warning(
            "Compte « %s » créé à partir des consultations existantes%s. "
            "Il conserve ses %d brouillon(s).",
            nom,
            f" (courriel deviné : {courriel})" if courriel else "",
            par_proprietaire.get(nom, 0),
        )

    db.commit()
    return len(a_creer)


def _adopt_authorized_users(db: Session) -> int:
    """
    Crée un compte pour chaque entrée d'``AUTHORIZED_USERS`` encore inconnue.

    Filet de démarrage : sans lui, une installation neuve n'aurait aucun compte,
    et avec ``ALLOW_SIGNUP=false`` personne ne pourrait jamais entrer. Le premier
    compte créé entre dans ``admins``.
    """
    entrees = [a.strip().lower() for a in settings.authorized_users if a.strip() and a.strip() != "*"]
    if not entrees:
        return 0

    connus = {u for u in db.scalars(select(User.username))}
    courriels_connus = {e for e in db.scalars(select(User.email)) if e}
    admin = db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    defaut = db.scalar(select(Group).where(Group.name == DEFAULT_GROUP))
    premier = not connus
    crees = 0

    for entree in entrees:
        # Déjà rattaché à un compte adopté plus haut : ne pas dédoubler.
        if entree in connus or entree in courriels_connus:
            continue
        user = User(username=entree, email=entree if "@" in entree else "", display_name=entree)
        db.add(user)
        db.flush()
        groupe = admin if (premier and crees == 0) else defaut
        if groupe is not None:
            db.add(UserGroup(user_id=user.id, group_id=groupe.id))
        logger.info(
            "Compte « %s » amorcé depuis AUTHORIZED_USERS (%s).",
            entree, "admins" if groupe is admin else "users",
        )
        crees += 1

    db.commit()
    return crees


# ---------------------------------------------------------------------------
# Migration légère du schéma
# ---------------------------------------------------------------------------
# ``Base.metadata.create_all`` crée les tables manquantes mais n'ajoute jamais
# une colonne à une table déjà existante. Comme la base vit dans un volume
# Docker qu'on ne veut évidemment pas réinitialiser, les colonnes ajoutées
# après coup sont créées ici à la main. SQLite accepte ALTER TABLE ADD COLUMN,
# ce qui suffit à ce cas : on n'a jamais eu à changer ni à retirer une colonne.
# ---------------------------------------------------------------------------
_ADDED_COLUMNS = {
    "users": [
        ("avatar_url", "VARCHAR(1000) NOT NULL DEFAULT ''"),
    ],
    "user_preferences": [
        ("theme_color", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("second_pass", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    "templates": [
        ("language", "VARCHAR(8) NOT NULL DEFAULT 'fr'"),
        ("is_locked", "BOOLEAN NOT NULL DEFAULT 0"),
        ("owner", "VARCHAR(255)"),
    ],
    "consultations": [
        ("patient_name", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("reason", "VARCHAR(300) NOT NULL DEFAULT ''"),
        ("requester", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("accompanied_by", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("consultation_date", "VARCHAR(40) NOT NULL DEFAULT ''"),
        ("llm_provider", "VARCHAR(40) NOT NULL DEFAULT ''"),
        ("stt_provider", "VARCHAR(40) NOT NULL DEFAULT ''"),
        ("stt_model", "VARCHAR(80) NOT NULL DEFAULT ''"),
        ("stt_language", "VARCHAR(8) NOT NULL DEFAULT ''"),
        ("audio_used", "BOOLEAN NOT NULL DEFAULT 0"),
        ("usage_prompt_tokens", "INTEGER"),
        ("usage_output_tokens", "INTEGER"),
        ("generation_seconds", "FLOAT"),
        ("transcript_used", "BOOLEAN NOT NULL DEFAULT 1"),
        ("verification_json", "TEXT"),
        ("corrections_markdown", "TEXT"),
        ("med_grounding_json", "TEXT"),
        ("transcript_conf", "TEXT"),
    ],
    "usage_events": [
        ("audio_prompt_tokens", "INTEGER"),
        ("cached_tokens", "INTEGER"),
    ],
    "usage_daily": [
        ("audio_prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _add_missing_columns() -> None:
    """Ajoute les colonnes apparues après la création initiale de la base."""
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not existing:
                # Table absente : create_all vient de la créer complète.
                continue
            for name, ddl in columns:
                if name in existing:
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                )
                logger.info("Migration : colonne %s.%s ajoutée", table, name)


def migrate_denominalize(db: Session) -> int:
    """
    Dénominalisation (2026-08-14) : les champs ``patient_name`` et
    ``patient_ref`` (n° de dossier) ne sont plus collectés ni stockés. Les
    valeurs déjà présentes sont effacées ; les colonnes sont conservées (à
    vide) pour ne pas reconstruire la table.
    """
    cleared = db.execute(
        update(Consultation).values(patient_name="", patient_ref="")
    ).rowcount
    if cleared:
        db.commit()
        logger.info(
            "Migration : dénominalisation — %d consultation(s) vidée(s) de "
            "patient_name / patient_ref", cleared,
        )
    return cleared


def migrate_retention_hours(db: Session) -> bool:
    """
    La rétention des dossiers est passée des jours aux heures (2026-08-14) :
    l'ancienne clé ``consultation_retention_days`` n'existe plus dans
    ``runtime_config.SETTINGS``. Sa valeur n'est pas reportée : la politique
    passe volontairement à la nouvelle valeur par défaut (12 h). La ligne est
    simplement retirée pour ne pas laisser de clé orpheline en base.
    """
    ancienne = db.get(AppSetting, "consultation_retention_days")
    if ancienne is None:
        return False
    db.delete(ancienne)
    db.commit()
    logger.info(
        "Migration : rétention passée aux heures — « consultation_retention_days » retiré, défaut 12 h appliqué"
    )
    return True


def init_db() -> None:
    """Crée les tables si nécessaire puis amorce les gabarits. Appelé au démarrage."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    with SessionLocal() as db:
        # L'ordre compte : on purge les anciens défauts AVANT d'amorcer, pour
        # qu'un nom réutilisé ne fasse pas échouer la contrainte d'unicité.
        migrate_retention_hours(db)
        migrate_denominalize(db)
        purge_legacy_templates(db)
        seed_locked_templates(db)
        seed_editable_templates(db)
        migrate_general_prompt(db)
        migrate_general_prompt_keep_reasoning(db)
        migrate_general_prompt_elements_a_valider(db)
        migrate_general_prompt_a_confirmer(db)
        migrate_general_prompt_final_section(db)
        migrate_general_prompt_structure(db)
        migrate_general_prompt_undo_consolidation(db)
        migrate_general_prompt_no_omission(db)
        migrate_general_prompt_treatment_stays(db)
        migrate_general_prompt_plan_first_person(db)
        migrate_general_prompt_impression_plan_no_omission(db)
        migrate_general_prompt_plan_medicament_nu(db)
        migrate_general_prompt_antecedents_hospitalisation_placement(db)
        migrate_general_prompt_meds_resolution(db)
        migrate_general_prompt_phonetic_origin(db)
        migrate_template_med_grouping(db)
        migrate_template_suivi_resume_stays(db)
        migrate_template_suivi_resume_treatment_stays(db)
        migrate_template_antecedents_hospitalisation_placement(db)
        seed_groups(db)
        # Import local : évite un cycle (pricing.py importe PricingRate d'ici).
        from app.pricing import seed_default_rates
        seed_default_rates(db)
        # L'ordre compte : les propriétaires existants d'abord, pour que le
        # compte porteur des données soit celui qui devient administrateur.
        _adopt_legacy_owners(db)
        _adopt_authorized_users(db)
