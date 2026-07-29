"""
auth.py — Intégration Pangolin SSO (authentification par en-têtes HTTP).
=========================================================================

MODÈLE DE SÉCURITÉ
------------------
L'application ne gère aucun mot de passe : Pangolin authentifie l'utilisateur
puis réinjecte son identité dans un en-tête HTTP (``Remote-User`` par défaut).

Un en-tête HTTP est trivial à falsifier. La sécurité repose donc sur DEUX
vérifications successives :

  1. L'ADRESSE IP PAIRE (le socket TCP réellement connecté) doit appartenir à
     ``TRUSTED_PROXIES``. Seul Pangolin doit pouvoir joindre le conteneur.
  2. L'identité extraite de l'en-tête doit figurer dans ``AUTHORIZED_USERS``.

⚠️ IMPORTANT — le conteneur démarre volontairement uvicorn avec
``--no-proxy-headers``. Sans cela, uvicorn remplacerait ``request.client.host``
par la valeur de ``X-Forwarded-For``, elle-même falsifiable : la vérification
n° 1 deviendrait inutile. On garde donc l'IP réelle du pair, et l'en-tête
``X-Forwarded-For`` n'est utilisé QUE pour la journalisation.

Corollaire : ne publiez jamais le port du conteneur sur une interface publique.
Le docker-compose fourni le lie à 127.0.0.1 pour cette raison.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from fastapi import HTTPException, Request, status
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
    source_header: str = ""
    is_dev: bool = False

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

        Par défaut (``TEMPLATE_ADMINS=*``) tout utilisateur autorisé est
        administrateur des gabarits — un cabinet de gériatrie compte peu
        d'utilisateurs et ils se font mutuellement confiance. Renseignez la
        variable pour restreindre ce droit à quelques comptes.
        """
        admins = settings.template_admins_normalized
        if not admins or "*" in admins:
            return True
        return self.username.lower() in admins or (self.email or "").lower() in admins

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "label": self.label,
            "initials": self.initials,
            "is_template_admin": self.is_template_admin,
            "is_dev": self.is_dev,
        }


# ---------------------------------------------------------------------------
# Vérifications
# ---------------------------------------------------------------------------
def get_peer_ip(request: Request) -> Optional[str]:
    """IP réellement connectée au socket (et non une valeur d'en-tête)."""
    if request.client is None:
        return None
    return request.client.host


def is_trusted_peer(ip: Optional[str], networks: Optional[Sequence] = None) -> bool:
    """L'IP appartient-elle à l'une des plages de confiance ?"""
    if ip is None:
        return False
    nets = settings.trusted_proxies if networks is None else networks
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        logger.warning("Adresse IP paire illisible : %r", ip)
        return False

    for net in nets:
        # Une IPv4 ne peut pas être comparée à un réseau IPv6. Docker présente
        # parfois les adresses sous forme mappée (::ffff:172.18.0.5) : on les
        # ramène alors à leur équivalent IPv4 avant comparaison.
        if addr.version != net.version:
            if addr.version == 6 and getattr(addr, "ipv4_mapped", None) is not None:
                if addr.ipv4_mapped in net:
                    return True
            continue
        if addr in net:
            return True
    return False


def _header(request: Request, name: str) -> str:
    if not name:
        return ""
    return (request.headers.get(name) or "").strip()


def extract_identity(request: Request) -> tuple[str, str]:
    """
    Retourne (identifiant, nom de l'en-tête utilisé).

    Les en-têtes sont testés dans l'ordre : SSO_HEADER_KEY puis les replis
    déclarés dans SSO_HEADER_FALLBACKS. Cela évite d'avoir à reconfigurer
    l'application si Pangolin change de convention.
    """
    for header_name in settings.sso_headers_in_order:
        value = _header(request, header_name)
        if value:
            return value, header_name
    return "", ""


def is_authorized(username: str, email: str = "") -> bool:
    """
    L'utilisateur figure-t-il dans la liste blanche ?

    La comparaison est insensible à la casse et accepte que la liste blanche
    contienne soit l'identifiant, soit l'adresse courriel — Pangolin peut
    fournir l'un ou l'autre selon le fournisseur d'identité configuré.
    """
    if settings.allow_all_users:
        return True
    allowed = settings.authorized_users_normalized
    if not allowed:
        return False
    candidates = {username.lower()}
    if email:
        candidates.add(email.lower())
    return bool(candidates & allowed)


def authenticate(request: Request) -> Principal:
    """
    Authentifie la requête ou lève une ``HTTPException``.

    Codes retournés :
      * 403 — pair non fiable, en-tête absent, ou utilisateur non autorisé.
        On ne renvoie jamais 401 : il n'y a pas de mécanisme de défi côté
        application, c'est Pangolin qui gère la connexion.
    """
    # -- Mode développement : contournement explicite --------------------
    if settings.auth_disabled:
        return Principal(
            username=settings.dev_user,
            email=settings.dev_user if "@" in settings.dev_user else "",
            display_name="Mode développement",
            source_header="(AUTH_DISABLED)",
            is_dev=True,
        )

    peer_ip = get_peer_ip(request)

    # -- Vérification 1 : provenance réseau ------------------------------
    if not is_trusted_peer(peer_ip):
        logger.warning(
            "Requête refusée : IP paire %s hors de TRUSTED_PROXIES (%s %s)",
            peer_ip, request.method, request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Accès refusé : requête reçue en dehors du proxy de confiance. "
                "Passez par Pangolin ou ajustez TRUSTED_PROXIES."
            ),
        )

    # -- Vérification 2 : identité ---------------------------------------
    username, source_header = extract_identity(request)
    if not username:
        logger.warning(
            "Requête refusée : aucun en-tête d'identité parmi %s (IP %s)",
            settings.sso_headers_in_order, peer_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Accès refusé : aucune identité transmise par le SSO. "
                f"En-têtes attendus : {', '.join(settings.sso_headers_in_order)}."
            ),
        )

    email = _header(request, settings.sso_email_header)
    display_name = _header(request, settings.sso_name_header)

    if not is_authorized(username, email):
        logger.warning("Requête refusée : utilisateur « %s » non autorisé", username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Accès refusé : le compte « {username} » ne figure pas dans "
                "AUTHORIZED_USERS. Contactez l'administrateur."
            ),
        )

    return Principal(
        username=username,
        email=email,
        display_name=display_name,
        source_header=source_header,
    )


# ---------------------------------------------------------------------------
# Middleware ASGI
# ---------------------------------------------------------------------------
# Implémenté en ASGI « pur » plutôt qu'avec BaseHTTPMiddleware : ce dernier
# met la requête en tampon, ce qui pose problème pour les téléversements audio
# de plusieurs dizaines de mégaoctets.
# ---------------------------------------------------------------------------
_ERROR_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accès refusé</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1.5rem}}
 .card{{max-width:34rem;background:#1e293b;border:1px solid #334155;border-radius:.75rem;padding:2rem}}
 h1{{margin:0 0 .75rem;font-size:1.25rem;color:#f87171}}
 p{{margin:0 0 .75rem;line-height:1.6;font-size:.95rem;color:#cbd5e1}}
 code{{background:#0f172a;padding:.15rem .4rem;border-radius:.25rem;font-size:.85em}}
</style></head>
<body><div class="card">
  <h1>403 — Accès refusé</h1>
  <p>{detail}</p>
  <p style="color:#64748b;font-size:.8rem">ConsultAI — accès contrôlé par Pangolin SSO.</p>
</div></body></html>"""


class SSOAuthMiddleware:
    """
    Protège l'ensemble de l'application, y compris les fichiers statiques.

    ``public_paths`` liste les chemins accessibles sans authentification
    (sonde de santé du conteneur uniquement — elle ne divulgue aucune donnée
    clinique).
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
        return path in self.public_paths or path.startswith(self.public_prefixes)

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
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(
        request: Request, exc: HTTPException, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Réponse HTML pour une navigation, JSON pour un appel d'API."""
        accepts_html = "text/html" in (request.headers.get("accept") or "")
        wants_api = request.url.path.startswith("/api/")

        if accepts_html and not wants_api:
            response = HTMLResponse(
                _ERROR_PAGE.format(detail=exc.detail), status_code=exc.status_code
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Non authentifié.")
    return principal


def require_template_admin(request: Request) -> Principal:
    """Dépendance pour les routes de modification des gabarits."""
    principal = current_user(request)
    if not principal.is_template_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Seuls les administrateurs peuvent modifier les gabarits.",
        )
    return principal
