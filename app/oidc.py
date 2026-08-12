"""
oidc.py — Connexion par OpenID Connect.
===========================================================================

CE QUE FAIT CE MODULE, ET CE QU'IL DÉLÈGUE
------------------------------------------
Il enveloppe ``authlib`` : découverte du fournisseur, flux « authorization
code » avec PKCE, et validation du jeton d'identité.

La validation cryptographique n'est **volontairement pas** écrite ici. Vérifier
une signature JWT à la main — récupérer le JWKS, choisir la bonne clé, contrôler
l'algorithme sans se laisser imposer ``none`` ou une confusion HS256/RS256 —
c'est du code de sécurité qu'on n'improvise pas. ``authlib`` le fait, il est
maintenu, et c'est la seule dépendance ajoutée pour cela.

Ce qui reste à notre charge et qui compte tout autant :

* ``state`` — lie le retour du fournisseur à la session qui a initié le flux.
  Sans lui, un tiers peut faire aboutir *sa* connexion dans le navigateur de la
  victime (« login CSRF »). Géré par authlib, qui le range dans la session.
* ``nonce`` — lie le jeton d'identité à cette requête-ci, contre le rejeu.
* ``PKCE`` — inutile en théorie pour un client confidentiel, mais gratuit et
  il ferme l'interception du code d'autorisation.
* la portée de retour : on ne redirige qu'à l'intérieur de l'application, jamais
  vers une adresse arbitraire fournie par l'appelant (« open redirect »).

POURQUOI LE PROXY NE DÉCIDE PLUS
--------------------------------
L'application authentifiait par en-tête HTTP, en se fiant à un reverse proxy.
Elle authentifie maintenant elle-même : le proxy n'est plus qu'un relais. Deux
conséquences à ne pas perdre de vue :

* ``TRUSTED_PROXIES`` n'est plus un contrôle de sécurité ;
* la session vit dans un témoin signé, et ce témoin doit atteindre le serveur.
  Un proxy qui retire l'en-tête ``Cookie`` — Pangolin le fait quand il
  authentifie lui-même — rend toute connexion impossible : le flux aboutit chez
  le fournisseur puis retombe sur une session vide, en boucle.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, List, NamedTuple, Tuple
from urllib.parse import urlencode, urlparse

from app.config import settings

logger = logging.getLogger(__name__)


class OidcError(RuntimeError):
    """Échec du flux, avec un message affichable."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_oauth = None


def _client(provider_url: str, client_id: str, client_secret: str):
    """
    Client authlib du fournisseur demandé.

    Un client par fournisseur : « consultai » (fournisseur historique) et
    « consultai_alt » (domaine d'accès alternatif) le cas échéant. Construit
    tardivement et non au chargement du module : l'import de ce dernier ne doit
    pas échouer sur une installation qui n'a pas encore renseigné sa
    configuration OIDC — sinon l'application ne démarre plus et même
    ``/healthz``, qui sert à diagnostiquer cela, devient inatteignable.
    """
    global _oauth

    if not settings.oidc_configured:
        raise OidcError(
            "La connexion n'est pas configurée : renseignez OIDC_PROVIDER_URL, "
            "OIDC_CLIENT_ID et OIDC_CLIENT_SECRET."
        )

    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()

    name = "consultai_alt" if provider_url != settings.oidc_provider_url else "consultai"
    client = _oauth.create_client(name)
    if client is None:
        _oauth.register(
            name=name,
            server_metadata_url=f"{provider_url}/.well-known/openid-configuration",
            client_id=client_id,
            client_secret=client_secret,
            client_kwargs={
                "scope": " ".join(settings.oidc_scopes_effective),
                # PKCE : gratuit, et referme l'interception du code.
                "code_challenge_method": "S256",
            },
        )
        client = _oauth.create_client(name)
    return client


def reset_client() -> None:
    """Oublie les clients mémorisés (après un changement de configuration)."""
    global _oauth
    _oauth = None


def _host_of(url: str) -> str:
    """Nom d'hôte (minuscules) d'une URL, ou chaîne vide."""
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _endpoints_for(host: str) -> Tuple[str, str, str, str, str]:
    """
    (provider_url, client_id, client_secret, redirect_uri, base_url) pour
    l'hôte demandé.

    Le fournisseur alternatif (OIDC_ALT_*) n'est choisi que lorsqu'il est
    configuré ET que l'hôte correspond à son adresse de retour ; sinon on
    retombe sur le fournisseur unique historique — c'est le comportement
    strict de l'application avant l'introduction du second domaine.
    """
    alt_host = _host_of(settings.oidc_alt_redirect_uri)
    if alt_host and host and host.lower() == alt_host:
        return (
            settings.oidc_alt_provider_url,
            settings.oidc_alt_client_id,
            settings.oidc_alt_client_secret,
            settings.oidc_alt_redirect_uri,
            settings.alt_base_url,
        )
    return (
        settings.oidc_provider_url,
        settings.oidc_client_id,
        settings.oidc_client_secret,
        settings.effective_redirect_uri,
        settings.base_url,
    )


def base_url_for(host: str) -> str:
    """Base de l'application pour l'hôte demandé (fournisseur alternatif ou non)."""
    return _endpoints_for(host)[4]


# ---------------------------------------------------------------------------
# Flux
# ---------------------------------------------------------------------------
async def authorization_redirect(request):
    """Réponse de redirection vers la page de connexion du fournisseur."""
    provider, cid, secret, redirect_uri, _ = _endpoints_for(request.url.hostname)
    client = _client(provider, cid, secret)
    if not redirect_uri:
        raise OidcError(
            "Aucune adresse de retour : renseignez BASE_URL ou OIDC_REDIRECT_URI."
        )
    # authlib range state, nonce et le verifier PKCE dans la session.
    return await client.authorize_redirect(request, redirect_uri)


class Identity(NamedTuple):
    """
    Résultat d'une connexion réussie.

    Le jeton d'identité **brut** accompagne les revendications décodées, et ce
    n'est pas une commodité : il est exigé comme ``id_token_hint`` à la
    déconnexion. Sans lui, le fournisseur ignore ``post_logout_redirect_uri`` et
    l'usager reste sur la page de déconnexion du fournisseur au lieu de revenir
    à l'application.
    """

    claims: Dict[str, Any]
    id_token: str


async def fetch_identity(request) -> Identity:
    """
    Termine le flux et retourne les revendications validées.

    Lève ``OidcError`` avec un message affichable : cette fonction répond à un
    retour de navigateur, l'usager doit lire quelque chose d'utile plutôt qu'une
    trace d'exception.
    """
    provider, cid, secret, redirect_uri, _ = _endpoints_for(request.url.hostname)
    client = _client(provider, cid, secret)
    try:
        token = await client.authorize_access_token(request)
    except Exception as exc:
        # Cas courants : « state » absent (témoin perdu, donc session vide),
        # code expiré, ou secret client erroné.
        logger.warning("Échange du code OIDC refusé : %s", exc)
        raise OidcError(_diagnose(exc, redirect_uri)) from exc

    claims: Dict[str, Any] = dict(token.get("userinfo") or {})

    # Les groupes ne figurent pas toujours dans le jeton d'identité : certains
    # fournisseurs ne les exposent qu'au point « userinfo ». On complète, sans
    # jamais écraser ce que le jeton signé disait déjà.
    if settings.oidc_groups_claim not in claims:
        try:
            info = await client.userinfo(token=token)
            for cle, valeur in dict(info).items():
                claims.setdefault(cle, valeur)
        except Exception as exc:
            logger.info("Point « userinfo » non exploitable : %s", exc)

    if not claims.get("sub"):
        raise OidcError(
            "Le fournisseur n'a renvoyé aucun identifiant « sub » : impossible "
            "de reconnaître le compte."
        )

    # Le jeton d'identité BRUT, tel que reçu. Il servira de « id_token_hint » à
    # la déconnexion — c'est la seule pièce qui permette au fournisseur
    # d'accepter notre adresse de retour.
    id_token = str(token.get("id_token") or "")
    if not id_token:
        logger.warning(
            "Le fournisseur n'a pas renvoyé de jeton d'identité : la "
            "déconnexion ne pourra pas revenir à l'application."
        )
    return Identity(claims, id_token)


def _diagnose(exc: Exception, redirect_uri: str = "") -> str:
    """Traduit les échecs les plus fréquents en indication actionnable."""
    texte = str(exc).lower()
    if "state" in texte:
        return (
            "Connexion expirée ou témoin de session absent. Si cela se répète, "
            "le proxy retire l'en-tête « Cookie » : il doit relayer sans "
            "authentifier."
        )
    if "redirect_uri" in texte or "redirect uri" in texte:
        return (
            "Adresse de retour refusée par le fournisseur. Elle doit être "
            "déclarée à l'identique chez lui : "
            f"{redirect_uri or settings.effective_redirect_uri}"
        )
    if "client" in texte and ("secret" in texte or "auth" in texte):
        return "Identifiant ou secret client refusé par le fournisseur."
    return f"Connexion refusée par le fournisseur : {exc}"


# ---------------------------------------------------------------------------
# Lecture des revendications
# ---------------------------------------------------------------------------
def username_from(claims: Dict[str, Any]) -> str:
    """
    Nom d'usager retenu, en minuscules.

    L'ordre a des conséquences : c'est cette valeur qui sert de clé de propriété
    aux consultations. ``preferred_username`` d'abord, parce que c'est ce que
    portait l'ancien en-tête du proxy et que les consultations déjà en base y
    sont rattachées ; le courriel ensuite ; le ``sub`` en dernier recours, qui
    est stable mais illisible.
    """
    for cle in ("preferred_username", "email", "sub"):
        valeur = str(claims.get(cle) or "").strip().lower()
        if valeur:
            return valeur
    return ""


#: Revendications essayées après celle configurée, pour le nom affiché.
#:
#: L'ordre va du plus humain au plus technique. Le nom d'usager arrive en
#: dernier des noms lisibles, et le « sub » n'y figure pas du tout : un
#: identifiant opaque affiché en haut de l'écran n'aide personne — mieux vaut
#: alors ne rien afficher et laisser le nom d'usager tenir ce rôle.
_NAME_FALLBACKS = ("name", "given_name", "nickname", "preferred_username", "email")


def display_name_from(claims: Dict[str, Any], claim: str = "") -> str:
    """
    Nom à afficher, d'après la revendication choisie puis des replis.

    ``claim`` l'emporte : c'est le réglage du panneau. S'il est vide ou si le
    fournisseur ne l'a pas envoyé, on descend la liste des replis plutôt que de
    laisser l'écran vide — un compte sans nom affiché n'est pas une erreur de
    configuration qu'il faut faire payer à l'usager.
    """
    candidats = [claim] if claim else []
    candidats += [c for c in _NAME_FALLBACKS if c != claim]
    for nom in candidats:
        valeur = claims.get(nom)
        if isinstance(valeur, str) and valeur.strip():
            return valeur.strip()
    return ""


def picture_from(claims: Dict[str, Any], claim: str = "") -> str:
    """
    Adresse de l'avatar, ou chaîne vide.

    Seules les adresses ``https:`` sont retenues. Deux raisons : une adresse
    ``http:`` serait bloquée comme contenu mixte sur une page servie en HTTPS,
    et un ``data:`` ou un ``javascript:`` venus du fournisseur n'ont rien à
    faire dans un attribut ``src`` — la revendication est du texte contrôlé par
    un tiers, même si ce tiers est de confiance.

    Une chaîne vide fait retomber l'affichage sur les initiales.
    """
    nom = claim or settings.oidc_picture_claim or "picture"
    valeur = claims.get(nom)
    if not isinstance(valeur, str):
        return ""
    valeur = valeur.strip()
    if not valeur:
        return ""
    if not valeur.lower().startswith("https://"):
        logger.info(
            "Avatar ignoré : « %s » n'est pas une adresse https. La pastille "
            "affichera les initiales.", valeur[:60],
        )
        return ""

    # Défense en profondeur. Le gabarit échappe déjà cette valeur (Jinja est en
    # autoescape, vérifié), mais elle finit dans un attribut « src » et vient
    # d'un tiers : une adresse contenant un guillemet ou un espace n'est de
    # toute façon pas une adresse valide, et la refuser évite de faire reposer
    # la sûreté sur une seule couche.
    if any(c in valeur for c in '"\'<>` ') or any(ord(c) < 32 for c in valeur):
        logger.warning(
            "Avatar ignoré : l'adresse contient des caractères interdits."
        )
        return ""
    return valeur[:1000]


def groups_from(claims: Dict[str, Any]) -> List[str]:
    """
    Groupes annoncés par le fournisseur, normalisés en minuscules.

    Tolérant sur la forme : selon les fournisseurs, la revendication est une
    liste, une chaîne séparée par des espaces ou par des virgules. Une valeur
    inattendue donne une liste vide plutôt qu'une erreur — les groupes sont un
    complément, leur absence ne doit pas empêcher la connexion.
    """
    brut = claims.get(settings.oidc_groups_claim)
    if brut is None:
        return []
    if isinstance(brut, str):
        # Un nom distingué LDAP contient des virgules qui SÉPARENT SES PROPRES
        # composants (« cn=admins,ou=groups,dc=x ») : le découper comme une
        # liste produirait « ou=groups » et « dc=x » comme noms de groupes.
        # On ne découpe donc que ce qui ne ressemble pas à un DN.
        brut = [brut] if "=" in brut else [m for m in brut.replace(",", " ").split() if m]
    if not isinstance(brut, (list, tuple, set)):
        logger.info("Revendication de groupes ignorée (type %s)", type(brut).__name__)
        return []

    vus, sortie = set(), []
    for item in brut:
        nom = str(item or "").strip().lower()
        # Certains fournisseurs renvoient des chemins (« /admins ») ou des
        # noms distingués LDAP : on ne garde que le dernier segment.
        if nom.startswith("cn="):
            nom = nom.split(",", 1)[0][3:]
        nom = nom.strip("/").split("/")[-1]
        if nom and nom not in vus:
            vus.add(nom)
            sortie.append(nom)
    return sortie


# ---------------------------------------------------------------------------
# Déconnexion
# ---------------------------------------------------------------------------
async def end_session_url(id_token: str = "", retour: str = "", host: str = "") -> str:
    """
    Adresse de déconnexion du fournisseur (« RP-initiated logout »).

    Retourne une chaîne vide si le fournisseur n'annonce pas de point de
    terminaison : la session locale est alors la seule qu'on puisse clore, et
    l'appelant se contente de rediriger vers l'accueil.
    """
    provider, cid, secret, _, base = _endpoints_for(host)
    retour = retour or base or ""
    try:
        client = _client(provider, cid, secret)
        metadata = await client.load_server_metadata()
    except Exception as exc:
        logger.info("Métadonnées du fournisseur illisibles à la déconnexion : %s", exc)
        return ""

    point = metadata.get("end_session_endpoint")
    if not point:
        return ""

    params = {}
    if id_token:
        params["id_token_hint"] = id_token
    elif retour:
        # Sans « id_token_hint », la plupart des fournisseurs — Pocket ID
        # compris — IGNORENT « post_logout_redirect_uri » : l'usager reste sur
        # leur page de déconnexion. On le dit dans le journal plutôt que de
        # laisser chercher.
        logger.warning(
            "Déconnexion sans jeton d'identité : le fournisseur ignorera "
            "probablement le retour vers %s.", retour,
        )
    if retour:
        params["post_logout_redirect_uri"] = retour
        # Alternative à id_token_hint prévue par la spécification, et
        # inoffensive lorsque les deux sont présents.
        params["client_id"] = cid

    if not params:
        return point
    separateur = "&" if "?" in point else "?"
    return f"{point}{separateur}{urlencode(params)}"


# ---------------------------------------------------------------------------
# Redirection interne
# ---------------------------------------------------------------------------
def safe_next_path(candidate: str) -> str:
    """
    Chemin de retour après connexion, ramené à l'intérieur de l'application.

    Un « next » venu de l'extérieur ne doit jamais pouvoir emmener l'usager
    ailleurs : une adresse absolue, un chemin protocole-relatif (« //ailleurs »)
    ou un espace de noms inattendu retombent sur l'accueil. C'est la faille
    d'« open redirect », dont l'usage classique est d'habiller une page de
    phishing d'un domaine de confiance.
    """
    chemin = (candidate or "").strip()
    if not chemin.startswith("/") or chemin.startswith("//"):
        return "/"
    # Un chemin ne doit pas contenir de schéma ni d'hôte.
    analyse = urlparse(chemin)
    if analyse.scheme or analyse.netloc:
        return "/"
    if chemin.startswith("/auth/"):
        return "/"
    return chemin


def new_secret(length: int = 48) -> str:
    """Clé de session utilisable, pour la documentation et le démarrage."""
    return secrets.token_urlsafe(length)
