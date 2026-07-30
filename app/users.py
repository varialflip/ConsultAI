"""
users.py — Comptes, groupes et permissions.
===========================================================================

RÈGLES D'ENTRÉE, DANS L'ORDRE OÙ ELLES S'APPLIQUENT
---------------------------------------------------
1. **Compte déjà connu** → il entre, sauf s'il est désactivé.
2. **Compte inconnu et aucun compte n'existe** → il entre et devient
   administrateur. C'est l'amorçage : sans cette exception, une installation
   neuve avec ``ALLOW_SIGNUP=false`` n'aurait personne pour ouvrir le panneau et
   serait définitivement inutilisable.
3. **Compte inconnu, d'autres comptes existent, ``ALLOW_SIGNUP=true``** → il est
   créé dans le groupe ``users``.
4. **Compte inconnu, ``ALLOW_SIGNUP=false``** → refusé.

Le point 2 mérite d'être vu pour ce qu'il est : **quiconque se connecte le
premier devient administrateur**. C'est acceptable parce que le fournisseur
d'identité a déjà authentifié la personne, et que sur une installation neuve il
n'y a rien à voler. Cela suppose en revanche que l'inscription chez le
fournisseur soit fermée : ouverte, le premier venu prendrait l'installation.
L'avertissement de démarrage le dit lorsque ``ALLOW_SIGNUP=true``.

RATTACHEMENT D'UNE IDENTITÉ À UN COMPTE EXISTANT
------------------------------------------------
Le ``sub`` du fournisseur fait foi, mais il n'est connu qu'à partir de la
première connexion. Un compte amorcé — depuis les anciennes consultations ou
depuis ``AUTHORIZED_USERS`` — n'en a pas encore. La recherche se fait donc en
cascade : ``sub``, puis nom d'usager, puis courriel. Le premier qui correspond
se voit attribuer le ``sub``, une fois pour toutes.

Sans cette cascade, la migration depuis l'authentification par en-têtes ferait
disparaître les brouillons existants : ils appartiennent à une chaîne de
caractères (``Consultation.owner``) qu'un compte fraîchement créé ne porterait
pas.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import runtime_config
from app.config import settings
from app.database import (
    ADMIN_GROUP,
    DEFAULT_GROUP,
    Consultation,
    Group,
    User,
    UserGroup,
    utcnow,
)

logger = logging.getLogger(__name__)


class SignupRefused(RuntimeError):
    """L'usager est authentifié mais n'a pas le droit d'entrer."""


class AccountDisabled(RuntimeError):
    """Le compte existe mais a été désactivé."""


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
def groups_of(db: Session, user_id: int) -> List[Group]:
    return list(
        db.scalars(
            select(Group)
            .join(UserGroup, UserGroup.group_id == Group.id)
            .where(UserGroup.user_id == user_id)
            .order_by(Group.name)
        )
    )


def permissions_of(groups: Sequence[Group]) -> Dict[str, bool]:
    """Union des permissions des groupes : le droit le plus large gagne."""
    return {
        "is_admin": any(g.is_admin for g in groups),
        "can_manage_templates": any(
            g.is_admin or g.can_manage_templates for g in groups
        ),
    }


def count_users(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(User)) or 0)


def allow_signup() -> bool:
    """Réglage effectif : panneau d'administration, sinon ``.env``."""
    return runtime_config.value("allow_signup").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------
def _find_existing(db: Session, subject: str, username: str, email: str) -> Optional[User]:
    """Cascade de rattachement : ``sub``, puis nom d'usager, puis courriel."""
    if subject:
        trouve = db.scalar(select(User).where(User.subject == subject))
        if trouve is not None:
            return trouve
    if username:
        trouve = db.scalar(select(User).where(User.username == username))
        if trouve is not None:
            return trouve
    if email:
        # Un compte déjà rattaché à une AUTRE identité ne doit pas être capturé
        # par simple homonymie de courriel.
        trouve = db.scalar(
            select(User).where(User.email == email, User.subject == "")
        )
        if trouve is not None:
            return trouve
    return None


def _assign_group(db: Session, user: User, group_name: str) -> None:
    groupe = db.scalar(select(Group).where(Group.name == group_name))
    if groupe is None:
        logger.error("Groupe « %s » introuvable : appartenance non attribuée", group_name)
        return
    existe = db.scalar(
        select(UserGroup).where(
            UserGroup.user_id == user.id, UserGroup.group_id == groupe.id
        )
    )
    if existe is None:
        db.add(UserGroup(user_id=user.id, group_id=groupe.id))


def sync_provider_groups(db: Session, user: User, provider_groups: Sequence[str]) -> None:
    """
    Reporte les groupes annoncés par le fournisseur sur ceux de l'application.

    Correspondance par nom, et **uniquement additive** : un groupe absent de la
    réponse du fournisseur n'est pas retiré. C'est délibéré. Beaucoup de
    fournisseurs n'envoient la revendication que si le client la demande et que
    l'usager appartient à au moins un groupe ; la traiter comme la vérité
    complète ferait perdre ses droits à un administrateur le jour où la
    revendication manque — y compris le dernier, ce qui verrouillerait
    l'installation.

    Le retrait d'un droit se fait donc explicitement, depuis la gestion des
    comptes.
    """
    if not provider_groups:
        return
    connus = {g.name for g in db.scalars(select(Group))}
    for nom in provider_groups:
        if nom in connus:
            _assign_group(db, user, nom)
            logger.info("Groupe « %s » accordé à %s par le fournisseur", nom, user.username)


def link_or_create(
    db: Session,
    subject: str,
    username: str,
    email: str,
    display_name: str,
    provider_groups: Sequence[str] = (),
) -> Tuple[User, List[Group]]:
    """
    Retourne le compte correspondant à cette identité, en le créant s'il y a lieu.

    Lève ``SignupRefused`` ou ``AccountDisabled`` — l'appelant les traduit en
    page lisible.
    """
    subject = (subject or "").strip()
    username = (username or "").strip().lower()
    email = (email or "").strip().lower()

    user = _find_existing(db, subject, username, email)
    premier = count_users(db) == 0

    if user is None:
        if not premier and not allow_signup():
            raise SignupRefused(username or email or subject)

        user = User(
            subject=subject,
            username=username or email or subject,
            email=email,
            display_name=display_name or username or email,
        )
        db.add(user)
        db.flush()
        # Amorçage : le tout premier compte prend l'administration, sans quoi
        # personne ne pourrait jamais ouvrir le panneau.
        _assign_group(db, user, ADMIN_GROUP if premier else DEFAULT_GROUP)
        logger.info(
            "Compte créé : %s (%s)",
            user.username, "administrateur — premier compte" if premier else "users",
        )
    else:
        if not user.is_active:
            raise AccountDisabled(user.username)

        # Rattachement définitif de l'identité du fournisseur.
        if subject and not user.subject:
            user.subject = subject
            logger.info(
                "Compte « %s » rattaché à l'identité du fournisseur", user.username
            )
        # On complète ce qui manque, sans écraser ce que l'administrateur a
        # éventuellement corrigé à la main.
        if email and not user.email:
            user.email = email
        if display_name and not user.display_name:
            user.display_name = display_name
        # Un compte amorcé sans groupe entrerait sans aucun droit.
        if not groups_of(db, user.id):
            _assign_group(db, user, ADMIN_GROUP if count_users(db) == 1 else DEFAULT_GROUP)

    sync_provider_groups(db, user, provider_groups)
    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    return user, groups_of(db, user.id)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
def list_users(db: Session) -> List[dict]:
    """Comptes, avec leurs groupes et le nombre de consultations qu'ils portent."""
    compte_par_owner = {
        owner: total
        for owner, total in db.execute(
            select(Consultation.owner, func.count()).group_by(Consultation.owner)
        )
    }
    sortie = []
    for user in db.scalars(select(User).order_by(User.username)):
        payload = user.to_dict(groups_of(db, user.id))
        payload["consultation_count"] = compte_par_owner.get(user.username, 0)
        sortie.append(payload)
    return sortie


def list_groups(db: Session) -> List[dict]:
    membres = {
        gid: total
        for gid, total in db.execute(
            select(UserGroup.group_id, func.count()).group_by(UserGroup.group_id)
        )
    }
    sortie = []
    for groupe in db.scalars(select(Group).order_by(Group.is_admin.desc(), Group.name)):
        payload = groupe.to_dict()
        payload["member_count"] = membres.get(groupe.id, 0)
        sortie.append(payload)
    return sortie


def _admin_count(db: Session, exclude_user_id: Optional[int] = None) -> int:
    """Nombre de comptes actifs disposant de l'administration."""
    requete = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .join(Group, Group.id == UserGroup.group_id)
        .where(Group.is_admin.is_(True), User.is_active.is_(True))
    )
    if exclude_user_id is not None:
        requete = requete.where(User.id != exclude_user_id)
    return int(db.scalar(requete) or 0)


def update_user(
    db: Session,
    user_id: int,
    *,
    group_ids: Optional[List[int]] = None,
    is_active: Optional[bool] = None,
    display_name: Optional[str] = None,
) -> dict:
    """
    Modifie un compte.

    Refuse de retirer le dernier administrateur actif. Ce n'est pas une
    politesse : l'administration ne se rend que depuis le panneau, et une
    installation sans administrateur ne peut plus être réparée depuis le
    navigateur — il faudrait éditer la base à la main sur le NAS.
    """
    user = db.get(User, user_id)
    if user is None:
        raise ValueError("compte introuvable")

    if display_name is not None:
        user.display_name = display_name.strip()

    if is_active is not None and is_active != user.is_active:
        if not is_active and _admin_count(db, exclude_user_id=user.id) == 0 \
                and permissions_of(groups_of(db, user.id))["is_admin"]:
            raise ValueError("dernier administrateur")
        user.is_active = is_active

    if group_ids is not None:
        demandes = set(group_ids)
        valides = {
            g.id for g in db.scalars(select(Group).where(Group.id.in_(demandes)))
        } if demandes else set()

        futur_admin = bool(
            db.scalar(
                select(func.count())
                .select_from(Group)
                .where(Group.id.in_(valides), Group.is_admin.is_(True))
            )
        ) if valides else False

        if not futur_admin and user.is_active \
                and permissions_of(groups_of(db, user.id))["is_admin"] \
                and _admin_count(db, exclude_user_id=user.id) == 0:
            raise ValueError("dernier administrateur")

        db.query(UserGroup).filter(UserGroup.user_id == user.id).delete()
        for gid in sorted(valides):
            db.add(UserGroup(user_id=user.id, group_id=gid))

    db.commit()
    db.refresh(user)
    logger.info("Compte « %s » modifié", user.username)
    return user.to_dict(groups_of(db, user.id))


def create_group(db: Session, name: str, description: str = "",
                 is_admin: bool = False, can_manage_templates: bool = False) -> dict:
    slug = (name or "").strip().lower()
    if not slug:
        raise ValueError("nom vide")
    if db.scalar(select(Group).where(Group.name == slug)) is not None:
        raise ValueError("nom déjà pris")
    groupe = Group(
        name=slug,
        description=(description or "").strip(),
        is_admin=bool(is_admin),
        can_manage_templates=bool(can_manage_templates),
    )
    db.add(groupe)
    db.commit()
    db.refresh(groupe)
    logger.info("Groupe « %s » créé", slug)
    return groupe.to_dict()


def update_group(db: Session, group_id: int, **champs) -> dict:
    groupe = db.get(Group, group_id)
    if groupe is None:
        raise ValueError("groupe introuvable")

    if "description" in champs and champs["description"] is not None:
        groupe.description = str(champs["description"]).strip()
    if "can_manage_templates" in champs and champs["can_manage_templates"] is not None:
        groupe.can_manage_templates = bool(champs["can_manage_templates"])
    if "is_admin" in champs and champs["is_admin"] is not None:
        nouveau = bool(champs["is_admin"])
        if not nouveau and groupe.is_admin:
            # Retirer l'administration à ce groupe laisserait-il l'installation
            # sans personne pour la gouverner ?
            restants = _admin_count(db)
            membres_admin = int(
                db.scalar(
                    select(func.count())
                    .select_from(UserGroup)
                    .join(User, User.id == UserGroup.user_id)
                    .where(UserGroup.group_id == groupe.id, User.is_active.is_(True))
                ) or 0
            )
            if membres_admin and restants <= membres_admin:
                raise ValueError("dernier administrateur")
        groupe.is_admin = nouveau

    db.commit()
    db.refresh(groupe)
    logger.info("Groupe « %s » modifié", groupe.name)
    return groupe.to_dict()


def delete_group(db: Session, group_id: int) -> None:
    groupe = db.get(Group, group_id)
    if groupe is None:
        raise ValueError("groupe introuvable")
    if groupe.is_system:
        raise ValueError("groupe système")
    if groupe.is_admin and _admin_count(db) <= int(
        db.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.group_id == groupe.id)
        ) or 0
    ):
        raise ValueError("dernier administrateur")
    db.query(UserGroup).filter(UserGroup.group_id == groupe.id).delete()
    db.delete(groupe)
    db.commit()
    logger.info("Groupe « %s » supprimé", groupe.name)
