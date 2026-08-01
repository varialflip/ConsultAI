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
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

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
            "patient_name": self.patient_name,
            "patient_ref": self.patient_ref,
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
            # façon dont on stocke fournisseur et modèle séparément.
            "stt_used": " / ".join(p for p in (self.stt_provider, self.stt_model) if p),
            # Brute, celle-ci : l'interface la compare à la langue du gabarit
            # choisi pour décider s'il y a lieu de proposer une retranscription.
            "stt_language": self.stt_language,
            "llm_used": " / ".join(p for p in (self.llm_provider, self.model_used) if p),
            "audio_used": self.audio_used,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_body:
            data.update(
                {
                    "raw_transcript": self.raw_transcript,
                    "generated_markdown": self.generated_markdown,
                    "edited_markdown": self.edited_markdown,
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
**Patient :** {{PATIENT}}
**Numéro de dossier :** {{DOSSIER}}
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
**Patient :** {{PATIENT}}
**Numéro de dossier :** {{DOSSIER}}
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
**Patient :** {{PATIENT}}
**Numéro de dossier :** {{DOSSIER}}
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


# ---------------------------------------------------------------------------
# Groupes et comptes : amorçage
# ---------------------------------------------------------------------------
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
    "templates": [
        ("language", "VARCHAR(8) NOT NULL DEFAULT 'fr'"),
        ("is_locked", "BOOLEAN NOT NULL DEFAULT 0"),
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


def init_db() -> None:
    """Crée les tables si nécessaire puis amorce les gabarits. Appelé au démarrage."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    with SessionLocal() as db:
        # L'ordre compte : on purge les anciens défauts AVANT d'amorcer, pour
        # qu'un nom réutilisé ne fasse pas échouer la contrainte d'unicité.
        purge_legacy_templates(db)
        seed_locked_templates(db)
        seed_editable_templates(db)
        migrate_general_prompt(db)
        seed_groups(db)
        # L'ordre compte : les propriétaires existants d'abord, pour que le
        # compte porteur des données soit celui qui devient administrateur.
        _adopt_legacy_owners(db)
        _adopt_authorized_users(db)
