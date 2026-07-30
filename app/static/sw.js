/* =============================================================================
 * ConsultAI — Service Worker
 * =============================================================================
 * RÈGLE DE CONFIDENTIALITÉ, À NE PAS ASSOUPLIR
 * -------------------------------------------
 * Cette application manipule des renseignements de santé. Le service worker ne
 * met en cache QUE des ressources statiques et anonymes (JavaScript, icônes,
 * manifeste). Il ne met JAMAIS en cache :
 *
 *   • la page « / »        — elle contient l'identité de l'usager connecté ;
 *   • les appels « /api/ » — transcriptions, notes, gabarits, brouillons.
 *
 * Deux raisons : (1) ces données resteraient lisibles sur le disque de
 * l'appareil après la déconnexion ; (2) une réponse mise en cache court-
 * circuiterait la vérification d'autorisation faite par Pangolin et par
 * app/auth.py à chaque requête.
 *
 * Tout ce qui n'est pas explicitement listé ci-dessous part directement sur le
 * réseau, sans interception.
 * ========================================================================== */

// Incrémentez cette version à chaque modification d'un fichier statique :
// cela purge l'ancien cache et force le rechargement chez tous les usagers.
const VERSION = 'consultai-v10';
const SHELL_CACHE = `${VERSION}-shell`;
const VENDOR_CACHE = `${VERSION}-vendor`;

// Le manifeste est servi dynamiquement et dépend de la langue configurée : il
// est délibérément ABSENT de cette liste et exclu de l'interception plus bas.
// Mis en cache, il continuerait d'annoncer l'ancienne langue sur l'écran
// d'accueil après un changement de réglage.
const MANIFEST_PATH = '/static/manifest.webmanifest';

// Ressources propres à l'application, préchargées à l'installation.
const SHELL_ASSETS = [
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32.png',
];

// Bibliothèques tierces (CDN). Mises en cache à l'usage plutôt qu'à
// l'installation : si le CDN est injoignable au moment de l'installation, on
// ne veut pas que le service worker échoue entièrement.
const VENDOR_HOSTS = ['cdn.tailwindcss.com', 'cdn.jsdelivr.net'];

/* -------------------------------------------------------------------------
 * Installation — préchargement du shell
 * ---------------------------------------------------------------------- */
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      // addAll() échoue en bloc si une seule ressource manque : on ajoute donc
      // les fichiers un à un pour rester tolérant.
      .then((cache) => Promise.all(
        SHELL_ASSETS.map((url) => cache.add(url).catch((err) => {
          console.warn('[SW] préchargement ignoré :', url, err);
        })),
      ))
      .then(() => self.skipWaiting()),
  );
});

/* -------------------------------------------------------------------------
 * Activation — purge des versions précédentes
 * ---------------------------------------------------------------------- */
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => !name.startsWith(VERSION))
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

/* -------------------------------------------------------------------------
 * Interception des requêtes
 * ---------------------------------------------------------------------- */
self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Jamais autre chose qu'une lecture simple.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // --- Ressources statiques de l'application : cache d'abord, puis
  //     rafraîchissement en arrière-plan (« stale-while-revalidate »).
  if (url.origin === self.location.origin
      && url.pathname.startsWith('/static/')
      && url.pathname !== MANIFEST_PATH) {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
    return;
  }

  // --- Bibliothèques CDN : cache d'abord. Évite que l'interface soit
  //     inutilisable si le CDN est lent ou bloqué alors que le NAS répond.
  if (VENDOR_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(request, VENDOR_CACHE));
    return;
  }

  // --- TOUT LE RESTE (« / », « /api/… », « /healthz ») : réseau direct,
  //     aucune interception, aucune mise en cache. Voir l'en-tête du fichier.
});

/* -------------------------------------------------------------------------
 * Stratégies de cache
 * ---------------------------------------------------------------------- */
async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);

  const network = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);

  // Réponse immédiate depuis le cache si disponible ; sinon on attend le réseau.
  const response = cached || await network;
  if (response) return response;

  return new Response('Ressource indisponible hors ligne.', {
    status: 504,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    // Les réponses « opaque » (CDN sans CORS) ont un statut 0 : on les met
    // quand même en cache, c'est le comportement normal pour un script tiers.
    if (response && (response.ok || response.type === 'opaque')) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return new Response('// Bibliothèque indisponible hors ligne', {
      status: 504,
      headers: { 'Content-Type': 'application/javascript; charset=utf-8' },
    });
  }
}

/* -------------------------------------------------------------------------
 * Mise à jour pilotée par la page
 * ---------------------------------------------------------------------- */
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
