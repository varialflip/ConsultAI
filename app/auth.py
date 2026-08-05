"""
auth.py — Authentification par OpenID Connect.
=========================================================================

MODÈLE DE SÉCURITÉ
------------------
L'application authentifie **elle-même**, par OpenID Connect (voir ``oidc.py``).
Elle ne gère aucun mot de passe : le fournisseur d'identité le fait, et
l'application ne conserve de la session qu'un témoin signé.

    navigateur ──► /auth/login ──► fournisseur ──► /auth/callback ──► témoin
                                                        │
                                                  compte en base
                                                  (app/users.py)

Le reverse proxy n'est plus qu'un relais. C'est un changement de nature par
rapport à la version précédente, qui se fiait à un en-tête ``Remote-User``
injecté par Pangolin :

* ``TRUSTED_PROXIES`` n'est **plus un contrôle de sécurité**. Le port du
  conteneur ne doit pas pour autant être exposé — un attaquant qui l'atteint ne
  peut rien forger, mais il n'a aucune raison d'y accéder ;
* le proxy doit **relayer sans authentifier**. Un proxy qui retire l'en-tête
  ``Cookie`` — Pangolin le fait quand il authentifie lui-même — rend toute
  connexion impossible : le flux aboutit chez le fournisseur puis retombe sur
  une session vide, indéfiniment ;
* l'en-tête ``X-Forwarded-For`` ne sert qu'à la journalisation, et uvicorn
  démarre toujours avec ``--no-proxy-headers`` pour ne pas laisser un appelant
  réécrire l'adresse qu'on journalise.

CE QUE LE TÉMOIN CONTIENT
-------------------------
L'identité, pas les droits. Les groupes et permissions sont relus en base à
chaque requête : sans cela, retirer l'administration à quelqu'un n'aurait
d'effet qu'à l'expiration de son témoin, soit jusqu'à douze heures plus tard.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from fastapi import HTTPException, Request, status
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import i18n, preferences
from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Initiales affichées dans la pastille de l'en-tête
# ---------------------------------------------------------------------------
# Tout ce qui n'est pas une lettre sépare deux mots : « frederick.duong »,
# « frederick_duong » et « Frederick Duong » donnent donc tous « FD ».
_NAME_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Titres de civilité écartés avant le calcul : dans un cabinet médical ils
# précèdent presque tous les noms, et « Dr Duong » doit donner « D » puis
# « DU », jamais « DD ».
_HONORIFICS = {"dr", "dre", "drs", "pr", "pre", "m", "mme", "mlle", "me", "st", "ste"}


def _initials_from(text: str) -> str:
    """
    Deux lettres tirées d'un libellé de personne, ou une chaîne vide.

    Un nom composé de plusieurs mots donne l'initiale du premier et celle du
    dernier (« Frederick Duong » → « FD ») ; un mot isolé retombe sur ses deux
    premières lettres (« frederick » → « FR »), faute de mieux.
    """
    words = [w for w in _NAME_WORD.findall(text or "") if w]
    # On n'écarte les titres que s'il reste quelque chose derrière : un
    # utilisateur nommé « dr » tout court doit garder ses initiales.
    without_titles = [w for w in words if w.lower().rstrip(".") not in _HONORIFICS]
    if without_titles:
        words = without_titles
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


# ---------------------------------------------------------------------------
# Identité de l'utilisateur courant
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """Utilisateur authentifié pour la requête en cours."""

    username: str
    email: str = ""
    display_name: str = ""
    #: D'où vient l'identité. « oidc », ou « (AUTH_DISABLED) » en développement.
    source_header: str = "oidc"
    is_dev: bool = False
    user_id: Optional[int] = None
    #: Avatar annoncé par le fournisseur. Vide = la pastille affiche les
    #: initiales, ce qui est le cas le plus fréquent.
    avatar_url: str = ""
    #: Noms des groupes, et permissions qui en découlent. Relus en base à chaque
    #: requête : un droit retiré prend effet immédiatement.
    groups: Tuple[str, ...] = ()
    is_admin: bool = False
    can_manage_templates: bool = False

    @property
    def label(self) -> str:
        """Libellé le plus lisible disponible, pour l'affichage dans l'UI."""
        return self.display_name or self.email or self.username

    @property
    def initials(self) -> str:
        """
        Deux lettres identifiant l'utilisateur dans la pastille de l'en-tête.

        On essaie les sources de la plus parlante à la moins parlante : le nom
        affiché, puis la partie locale du courriel, puis l'identifiant. La
        première qui donne un résultat gagne — « Frederick Duong » produit
        « FD », là où découper bêtement le libellé donnait « FR ».
        """
        local_part = self.email.split("@", 1)[0] if self.email else ""
        for source in (self.display_name, local_part, self.username.split("@", 1)[0]):
            initials = _initials_from(source)
            if initials:
                return initials
        return "?"

    @property
    def owner_key(self) -> str:
        """
        Clé de propriété des consultations, normalisée en minuscules.

        Le fournisseur d'identité peut renvoyer une casse variable
        (« Dr.Tremblay@… » puis « dr.tremblay@… ») : sans normalisation, le
        médecin perdrait l'accès à ses propres brouillons.
        """
        return self.username.lower()

    @property
    def is_template_admin(self) -> bool:
        """
        Droit de créer / modifier / supprimer des gabarits.

        Vient désormais des groupes et non plus de ``TEMPLATE_ADMINS`` : les
        gabarits sont partagés, et le droit de les réécrire se donne compte par
        compte depuis le panneau. Un administrateur l'a toujours.
        """
        return self.is_admin or self.can_manage_templates

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "label": self.label,
            "initials": self.initials,
            "avatar_url": self.avatar_url,
            "is_template_admin": self.is_template_admin,
            "is_admin": self.is_admin,
            "groups": list(self.groups),
            "is_dev": self.is_dev,
        }


# ---------------------------------------------------------------------------
# Vérifications
# ---------------------------------------------------------------------------
def _header(request: Request, name: str) -> str:
    if not name:
        return ""
    return (request.headers.get(name) or "").strip()


SESSION_KEY = "consultai_user"


def session_identity(request: Request) -> dict:
    """Contenu de la session, ou dictionnaire vide si aucune."""
    try:
        return dict(request.session.get(SESSION_KEY) or {})
    except AssertionError:
        # SessionMiddleware absent : ne devrait pas arriver, mais mieux vaut
        # une absence d'identité qu'une exception au milieu du middleware.
        logger.error("SessionMiddleware non installé : aucune session lisible")
        return {}


def store_identity(request: Request, payload: dict) -> None:
    request.session[SESSION_KEY] = payload


def clear_identity(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def _principal_from_db(identity: dict) -> Optional[Principal]:
    """
    Construit le Principal en relisant les droits en base.

    Les groupes ne sont **pas** pris dans le témoin : ils y seraient figés
    jusqu'à son expiration, et retirer l'administration à quelqu'un ne prendrait
    effet que douze heures plus tard. Le témoin ne porte que l'identité.
    """
    from app.database import SessionLocal, User
    from app import users as users_service

    subject = str(identity.get("sub") or "")
    username = str(identity.get("username") or "").lower()
    if not (subject or username):
        return None

    with SessionLocal() as db:
        user = None
        if subject:
            user = db.query(User).filter(User.subject == subject).one_or_none()
        if user is None and username:
            user = db.query(User).filter(User.username == username).one_or_none()
        if user is None or not user.is_active:
            return None

        groupes = users_service.groups_of(db, user.id)
        droits = users_service.permissions_of(groupes)
        return Principal(
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            user_id=user.id,
            groups=tuple(g.name for g in groupes),
            is_admin=droits["is_admin"],
            can_manage_templates=droits["can_manage_templates"],
        )


def authenticate(request: Request) -> Principal:
    """
    Authentifie la requête depuis la session, ou lève une ``HTTPException``.

    Renvoie **401** et non 403 : il existe désormais un mécanisme de défi côté
    application — ``/auth/login``. Le middleware traduit ce 401 en redirection
    pour une navigation, et le laisse tel quel pour un appel d'API, que le
    navigateur ne doit pas suivre silencieusement.
    """
    if settings.auth_disabled:
        return Principal(
            username=settings.dev_user,
            email=settings.dev_user if "@" in settings.dev_user else "",
            display_name="Mode développement",
            source_header="(AUTH_DISABLED)",
            is_dev=True,
            groups=("admins",),
            is_admin=True,
            can_manage_templates=True,
        )

    identity = session_identity(request)
    if not identity:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_denied("denied.unauthenticated"),
        )

    principal = _principal_from_db(identity)
    if principal is None:
        # Compte supprimé ou désactivé depuis l'ouverture de la session : le
        # témoin ne doit pas continuer à ouvrir la porte.
        clear_identity(request)
        logger.warning(
            "Session refusée : compte « %s » inconnu ou désactivé",
            identity.get("username"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_denied("denied.account_disabled"),
        )
    return principal


# ---------------------------------------------------------------------------
# Middleware ASGI
# ---------------------------------------------------------------------------
# Implémenté en ASGI « pur » plutôt qu'avec BaseHTTPMiddleware : ce dernier
# met la requête en tampon, ce qui pose problème pour les téléversements audio
# de plusieurs dizaines de mégaoctets.
# ---------------------------------------------------------------------------
def _denied(key: str, **fields) -> str:
    """
    Message de refus, dans la langue du ``.env``.

    Volontairement pas la langue de la base : ces messages répondent à des
    requêtes non authentifiées, et rien de ce que dit un appelant refusé ne
    doit déclencher une lecture de la base.
    """
    return i18n.t(key, i18n.normalize(settings.app_language), **fields)


_ERROR_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1.5rem}}
 .card{{max-width:34rem;background:#1e293b;border:1px solid #334155;border-radius:.75rem;padding:2rem}}
 h1{{margin:0 0 .75rem;font-size:1.25rem;color:#f87171}}
 p{{margin:0 0 .75rem;line-height:1.6;font-size:.95rem;color:#cbd5e1}}
 code{{background:#0f172a;padding:.15rem .4rem;border-radius:.25rem;font-size:.85em}}
</style></head>
<body><div class="card">
  <h1>{heading}</h1>
  <p>{detail}</p>
  <p style="color:#64748b;font-size:.8rem">{footer}</p>
</div></body></html>"""


class AuthMiddleware:
    """
    Protège l'ensemble de l'application, y compris les fichiers statiques.

    ``public_paths`` liste les chemins accessibles sans authentification : la
    sonde de santé, les ressources d'installation de la PWA, et **les trois
    routes du flux de connexion** — qui doivent évidemment rester atteignables
    sans être déjà connecté.
    """

    def __init__(
        self,
        app: ASGIApp,
        public_paths: Iterable[str] = (),
        public_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.public_paths = set(public_paths)
        self.public_prefixes = tuple(public_prefixes)

    def _is_public(self, path: str) -> bool:
        """
        Le chemin est-il accessible sans authentification ?

        NORMALISER AVANT DE COMPARER, sinon ``public_prefixes`` se contourne :
        ``scope["path"]`` arrive tel quel, segments « .. » compris, si bien que
        « /static/icons/../app.js » satisfaisait ``startswith("/static/icons/")``
        et traversait le middleware sans authentification. La portée réelle
        était limitée — ``StaticFiles`` refuse de sortir de son dossier, et le
        routeur compare le chemin LITTÉRAL, donc aucune route protégée n'était
        atteignable — mais le contrat annoncé (« protège tout sauf ces
        chemins ») n'était pas tenu, et il le serait d'autant moins le jour où
        un fichier sensible atterrirait sous ``/static/``.

        Normaliser ne peut pas ouvrir l'accès à une route protégée : pour
        atteindre « /api/me », le chemin littéral doit déjà être « /api/me »,
        qui se normalise en lui-même et reste donc refusé.
        """
        # « / » forcé en tête puis normalisation : normpath conserve un double
        # slash initial (« //x » reste « //x », comportement POSIX), ce qui
        # rendrait la comparaison dépendante d'un détail d'écriture.
        normalise = posixpath.normpath("/" + path.lstrip("/"))
        return (
            normalise in self.public_paths
            or normalise.startswith(self.public_prefixes)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_public(path):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        try:
            principal = authenticate(request)
        except HTTPException as exc:
            await self._deny(request, exc, scope, receive, send)
            return

        # Mis à disposition des endpoints via `request.state.principal`.
        scope.setdefault("state", {})["principal"] = principal

        # Langue de CET usager, fixée pour toute la durée de la requête.
        #
        # Ici plutôt que dans une dépendance FastAPI : une dépendance ne
        # s'applique qu'aux routes qui la déclarent, et il suffirait d'en
        # oublier une pour qu'elle réponde dans la langue de quelqu'un d'autre.
        # Ce point-ci couvre par construction tout ce qui est authentifié.
        #
        # Le contexte est recopié par asyncio.create_task et par
        # run_in_threadpool : la passe de découpage d'une dictée, qui survit à
        # la réponse HTTP, garde donc cette langue.
        preferences.bind_language(preferences.language_for(principal.owner_key))
        preferences.bind_theme(preferences.theme_for(principal.owner_key))

        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(
        request: Request, exc: HTTPException, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """
        Réponse adaptée à la nature de l'appel.

        Une **navigation** non authentifiée part vers la page de connexion : c'est
        le comportement attendu d'une application qui authentifie elle-même, et
        afficher un 403 obligerait l'usager à deviner qu'il doit visiter
        ``/auth/login``.

        Un **appel d'API** reçoit du JSON, jamais une redirection. Le client
        suivrait la redirection en arrière-plan et recevrait du HTML là où il
        attend du JSON, ce qui se manifesterait par une erreur d'analyse
        incompréhensible plutôt que par « votre session a expiré ».
        """
        accepts_html = "text/html" in (request.headers.get("accept") or "")
        wants_api = request.url.path.startswith("/api/")

        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and accepts_html
            and not wants_api
            and request.method == "GET"
        ):
            from urllib.parse import quote

            cible = request.url.path
            if request.url.query:
                cible = f"{cible}?{request.url.query}"
            response = RedirectResponse(
                f"/auth/login?next={quote(cible, safe='')}", status_code=302
            )
            await response(scope, receive, send)
            return

        if accepts_html and not wants_api:
            # Langue prise dans le .env et non dans la base : cette page est la
            # réponse à une requête REFUSÉE. Elle doit pouvoir s'afficher même
            # si la base est indisponible, et surtout ne rien interroger sur
            # ordre d'un appelant non authentifié.
            langue = i18n.normalize(settings.app_language)
            response = HTMLResponse(
                _ERROR_PAGE.format(
                    lang=langue,
                    title=i18n.t("denied.title", langue),
                    heading=i18n.t("denied.heading", langue),
                    footer=i18n.t("denied.footer", langue, sso=settings.sso_label),
                    detail=exc.detail,
                ),
                status_code=exc.status_code,
            )
        else:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Dépendances FastAPI
# ---------------------------------------------------------------------------
def current_user(request: Request) -> Principal:
    """Injecte l'utilisateur déjà authentifié par le middleware."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        # Ne devrait jamais arriver : le middleware couvre toutes les routes
        # non publiques. Filet de sécurité en cas d'erreur de configuration.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_denied("denied.unauthenticated"))
    return principal


def require_template_admin(request: Request) -> Principal:
    """Dépendance pour les routes de modification des gabarits."""
    principal = current_user(request)
    if not principal.is_template_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_denied("denied.not_admin"),
        )
    return principal


def require_admin(request: Request) -> Principal:
    """
    Dépendance des routes d'administration : réglages, comptes, groupes.

    Distincte de ``require_template_admin`` : écrire un gabarit et changer les
    droits d'autrui ne sont pas le même pouvoir. Un groupe peut accorder le
    premier sans le second.
    """
    principal = current_user(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_denied("denied.not_system_admin"),
        )
    return principal
