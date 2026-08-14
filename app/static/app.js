/* =============================================================================
 * ConsultAI — logique de l'interface
 * =============================================================================
 * Aucune dépendance de compilation : JavaScript natif, chargé directement par
 * le navigateur. Trois bibliothèques externes seulement (Tailwind, marked,
 * DOMPurify), toutes chargées dans index.html.
 *
 * Organisation du fichier :
 *   1. Utilitaires (DOM, réseau, notifications)
 *   2. État applicatif
 *   3. Gabarits (chargement + menu déroulant)
 *   4. Enregistrement audio (MediaRecorder, téléversement par fragments)
 *   5. Transcription et mise en forme
 *   6. Rendu Markdown, sauvegarde automatique
 *   7. Export (copier, PDF)
 *   8. Modales (gabarits, brouillons)
 *   9. Initialisation
 * ========================================================================== */

(function () {
  'use strict';

  /* =========================================================================
   * 0. TRADUCTION
   * ======================================================================
   * La langue et le catalogue complet sont inclus dans la page par le serveur
   * (voir window.CONSULTAI dans index.html). Rien n'est chargé par le réseau :
   * l'interface doit pouvoir écrire du texte à sa première ligne de code, et
   * un aller-retour de plus l'afficherait un instant en clés brutes.
   *
   * Le code et ses commentaires restent en français — ils s'adressent à qui
   * maintient l'application. Seul ce que l'usager lit est traduit.
   * ====================================================================== */

  const LANG = (window.CONSULTAI && window.CONSULTAI.lang) || 'fr';
  const CATALOG = (window.CONSULTAI && window.CONSULTAI.i18n) || {};

  /** Étiquette de région pour toLocaleString : dates et heures suivent la langue. */
  const LOCALE = LANG === 'en' ? 'en-CA' : 'fr-CA';

  /**
   * Texte traduit, champs entre accolades remplis.
   *
   * Une clé absente est renvoyée telle quelle plutôt que de lever : un libellé
   * qui s'affiche en clair est un défaut visible et réparable ; une exception
   * au milieu d'un rendu laisse un écran à moitié construit.
   */
  function T(key, fields) {
    const texte = CATALOG[key];
    if (texte === undefined) return key;
    if (!fields) return texte;
    return texte.replace(/\{(\w+)\}/g, (motif, nom) =>
      Object.prototype.hasOwnProperty.call(fields, nom) ? String(fields[nom]) : motif);
  }

  /* =========================================================================
   * 1. UTILITAIRES
   * ====================================================================== */

  const $ = (id) => document.getElementById(id);

  /**
   * Renvoie la valeur résolue d'une variable CSS personnalisée, utile pour
   * les couleurs qui dépendent du thème (toile, …). La fonction lit le
   * ``<html>`` (où les variables sont définies via ``data-theme``) et non un
   * élément arbitraire, pour que le résultat soit le même partout.
   */
  const accentColor = (prop) =>
    getComputedStyle(document.documentElement).getPropertyValue(prop).trim();

  /** Échappe le HTML avant insertion dans le DOM (contenu saisi par l'usager). */
  function esc(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  /**
   * Titres et abréviations françaises courantes en consultation, à ne pas
   * confondre avec une fin de phrase — « Dr. » suivi d'un nom propre est le
   * cas le plus fréquent, de loin, dans une dictée médicale. Liste courte et
   * volontairement incomplète : le but est de couvrir le cas courant, pas de
   * remplacer un véritable découpeur de phrases.
   */
  const SENTENCE_ABBREVIATIONS = '(?:Dr|Dre|Drs|M|Mr|Mme|Mlle|Me|Pr|St|Ste)';
  const SENTENCE_SPLIT_RE = new RegExp(
    `(?<!\\b${SENTENCE_ABBREVIATIONS}\\.)(?<=[.!?])\\s+`,
  );

  /**
   * Une phrase par ligne : plus facile à relire pendant une dictée longue
   * qu'un seul bloc continu. Découpe après un signe de fin de phrase suivi
   * d'une espace, sauf immédiatement après un titre courant (voir
   * SENTENCE_ABBREVIATIONS) — imparfait sur les abréviations qui n'y figurent
   * pas, mais une dictée médicale ponctue rarement au milieu d'une autre
   * abréviation de façon à tromper cette heuristique.
   *
   * Reformate le texte ENTIER à chaque appel plutôt que la seule tranche
   * fraîche : les fragments transcrits n'ont aucune raison de tomber sur une
   * frontière de phrase (une phrase peut s'étaler sur deux tranches), seul
   * le texte au complet permet de retrouver les vraies frontières.
   * Idempotent : reformater un texte déjà mis en forme ne change rien
   * d'autre que la normalisation des espaces entre phrases.
   */
  function formatSentences(text) {
    return (text || '')
      .split(SENTENCE_SPLIT_RE)
      .map((phrase) => phrase.trim())
      .filter(Boolean)
      .join('\n');
  }

  /** Notification éphémère en bas à droite. */
  /**
   * Retourne un ``{ dismiss }`` : nécessaire pour un toast « en cours »
   * (durée volontairement longue, en attendant qu'une opération se termine),
   * qu'il faut pouvoir retirer DÈS que le résultat arrive plutôt que le
   * laisser courir jusqu'à sa propre échéance — voir runRetranscription.
   *
   * Le toast se ferme aussi à la main : croix en regard, ou glissé latéral
   * (le glissé suit le doigt, relâché au-delà de 64 px il emporte le toast).
   * Le compte à rebours est suspendu pendant le glissé — disparaître sous le
   * doigt serait pire qu'une notification qui s'attarde.
   */
  function toast(message, type = 'info', durationMs = 4500) {
    const palette = {
      info: 'bg-slate-800',
      success: 'toast-success',
      error: 'bg-red-700',
      warning: 'bg-amber-600',
    };
    const el = document.createElement('div');
    el.className = `${palette[type] || palette.info} text-white text-sm pl-4 pr-2 py-2 rounded-lg
                    shadow-lg max-w-md transition-opacity duration-300 flex items-center gap-2`;
    const text = document.createElement('span');
    text.className = 'flex-1';
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'shrink-0 -mr-1 p-1 rounded text-white/70 hover:text-white';
    close.setAttribute('aria-label', T('toast.dismiss'));
    close.innerHTML = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
      + ' stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    el.append(text, close);
    $('toastZone').appendChild(el);

    let dismissed = false;
    let timer;
    const dismiss = (slideX) => {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      if (slideX) {
        el.style.transition = 'transform 200ms ease-in, opacity 200ms ease-in';
        el.style.transform = `translateX(${slideX}px)`;
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 220);
      } else {
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 320);
      }
    };
    timer = setTimeout(dismiss, durationMs);
    close.addEventListener('click', () => dismiss());

    // Glissé pour fermer. La croix gère son propre clic : le glissé ne la
    // concerne pas. pan-y laisse le défilement vertical au navigateur, seul
    // l'axe horizontal nous revient.
    let swipeX = null;
    el.style.touchAction = 'pan-y';
    el.addEventListener('pointerdown', (ev) => {
      if (ev.target.closest('button')) return;
      swipeX = ev.clientX;
      clearTimeout(timer);
      el.setPointerCapture(ev.pointerId);
    });
    el.addEventListener('pointermove', (ev) => {
      if (swipeX === null) return;
      const dx = ev.clientX - swipeX;
      el.style.transition = 'none';
      el.style.transform = `translateX(${dx}px)`;
      el.style.opacity = String(Math.max(0.2, 1 - Math.abs(dx) / 160));
    });
    el.addEventListener('pointerup', (ev) => {
      if (swipeX === null) return;
      const dx = ev.clientX - swipeX;
      swipeX = null;
      if (Math.abs(dx) > 64) {
        dismiss(Math.sign(dx) * el.offsetWidth * 1.2);
      } else {
        // Retour élastique, puis un sursis avant l'auto-fermeture.
        el.style.transition = 'transform 150ms ease-out, opacity 150ms ease-out';
        el.style.transform = '';
        el.style.opacity = '';
        timer = setTimeout(dismiss, 3000);
      }
    });
    el.addEventListener('pointercancel', () => {
      if (swipeX === null) return;
      swipeX = null;
      el.style.transition = '';
      el.style.transform = '';
      el.style.opacity = '';
    });
    return { dismiss: () => dismiss() };
  }

  /**
   * Variante de toast() porteuse d'un bouton d'action, pour une notification
   * qui propose de FAIRE quelque chose plutôt que de seulement informer (par
   * exemple : suivre une dictée commencée sur un autre appareil). Volontairement
   * plus simple que toast() — pas de glissé pour fermer, une croix suffit —
   * pour ne pas risquer de régression sur le toast partout ailleurs utilisé.
   *
   * ``durationMs = 0`` rend le toast persistant : il ne part que par la croix
   * ou par le bouton d'action. Une invitation manquée parce qu'on regardait
   * ailleurs pendant 12 s ne doit pas exiger un rechargement de page pour
   * réapparaître.
   */
  function toastWithAction(message, actionLabel, onAction, durationMs = 12000) {
    const el = document.createElement('div');
    el.className = 'bg-slate-800 text-white text-sm pl-4 pr-2 py-2 rounded-lg shadow-lg max-w-md '
      + 'transition-opacity duration-300 flex items-center gap-2';
    const text = document.createElement('span');
    text.className = 'flex-1';
    text.textContent = message;
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'shrink-0 px-2.5 py-1 rounded bg-white/15 hover:bg-white/25 text-xs font-medium';
    action.textContent = actionLabel;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'shrink-0 -mr-1 p-1 rounded text-white/70 hover:text-white';
    close.setAttribute('aria-label', T('toast.dismiss'));
    close.innerHTML = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
      + ' stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    el.append(text, action, close);
    $('toastZone').appendChild(el);

    let dismissed = false;
    let timer;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 320);
    };
    timer = durationMs > 0 ? setTimeout(dismiss, durationMs) : null;
    close.addEventListener('click', dismiss);
    action.addEventListener('click', () => { dismiss(); onAction(); });
  }

  /** Voile bloquant pendant les traitements longs (STT, Gemini). */
  function setBusy(active, message) {
    $('busyMessage').textContent = message || T('app.busy_default');
    $('busyOverlay').classList.toggle('hidden', !active);
  }

  /**
   * État « génération en cours » — remplace le voile plein écran, qui
   * cacherait le texte en cours de génération, par un témoin discret :
   *  - grand écran : pastille posée sur le panneau de transcription (le
   *    panneau de la note reste libre, le texte y défile) ;
   *  - mobile : barre horizontale animée, centrée sur la portion encore vide
   *    de l'aperçu (voir positionGenBar).
   * Le fond du texte généré passe temporairement en gris pâle, le temps que
   * le résultat soit confirmé par la réponse finale de /api/generate.
   */
  function setGenerating(active) {
    $('previewPane').classList.toggle('gen-pane', active);
    $('markdownEditor').classList.toggle('gen-pane', active);

    const isMobile = window.matchMedia('(max-width: 1023px)').matches;
    $('genIndicator').classList.toggle('hidden', !active || isMobile);
    $('genBar').classList.toggle('hidden', !active || !isMobile);
    if (active && isMobile) positionGenBar();
  }

  /**
   * Place la barre horizontale mobile au centre vertical de la portion vide
   * de l'aperçu — entre la fin du texte déjà généré et le bas de la zone
   * visible. Le texte défilant en bas (on suit toujours la fin), la portion
   * vide est ce qui sépare la dernière ligne du bas du cadre.
   */
  function positionGenBar() {
    const pane = $('previewPane');
    const bar = $('genBar');
    const viewportBottom = pane.scrollTop + pane.clientHeight;
    if (viewportBottom <= 0 || pane.clientHeight <= 0) return;
    // Le vide visible commence après le texte déjà rendu, ou au haut du cadre
    // si tout est déjà rempli (le témoin se rabat alors sur le bord inférieur).
    const emptyTop = Math.min(pane.scrollHeight, viewportBottom);
    const center = (emptyTop + viewportBottom) / 2;
    bar.style.top = `${Math.max(0, center - bar.offsetHeight / 2)}px`;
  }

  /**
   * Appel API centralisé.
   * Traduit les erreurs HTTP en exceptions porteuses du message renvoyé par
   * FastAPI (champ « detail »), déjà rédigé en français.
   */
  async function api(path, options = {}) {
    const config = Object.assign({ headers: {} }, options);
    if (config.body && !(config.body instanceof FormData)) {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }
    // Permet au flux SSE (voir connectLiveEvents) de reconnaître les
    // écritures faites par CET onglet et de ne pas se les appliquer à
    // lui-même une seconde fois.
    config.headers['X-ConsultAI-Tab'] = state.tabId;

    let response;
    try {
      response = await fetch(path, config);
    } catch (err) {
      // Une annulation volontaire (AbortController.abort()) n'est pas une
      // panne réseau : on laisse l'appelant la distinguer par son nom plutôt
      // que de la maquiller en « injoignable », ce qui afficherait un toast
      // d'erreur pour une requête supplantée intentionnellement.
      if (err.name === 'AbortError') throw err;
      throw new Error(T('net.unreachable'));
    }

    if (response.status === 204) return null;

    const isJson = (response.headers.get('content-type') || '').includes('application/json');
    const payload = isJson ? await response.json().catch(() => null) : null;

    if (!response.ok) {
      const detail = (payload && payload.detail) || T('net.http_error', { status: response.status });
      throw new Error(detail);
    }
    return payload;
  }

  /** Retarde l'exécution : évite d'envoyer une requête à chaque frappe. */
  function debounce(fn, delay) {
    let handle = null;
    return function (...args) {
      clearTimeout(handle);
      handle = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const m = String(Math.floor(seconds / 60)).padStart(2, '0');
    const s = String(Math.floor(seconds % 60)).padStart(2, '0');
    return `${m}:${s}`;
  }

  /**
   * Nom du mois en toutes lettres (« août » → « Août »), dans la langue de
   * l'interface : le navigateur connaît les noms, inutile de les traduire
   * dans i18n.py. Sert à « Mon usage » et au récapitulatif des statistiques.
   */
  function monthName(year, month) {
    const nom = new Date(year, month - 1).toLocaleString(LOCALE, { month: 'long' });
    return nom.charAt(0).toUpperCase() + nom.slice(1);
  }


  function formatDateTime(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString(LOCALE, {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) {
      return iso;
    }
  }

  function formatTime(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
    } catch (_) {
      return '';
    }
  }

  /** Abréviations de mois courtes, alignées sur l'usage français (« 8 aoû. »,
   * « 11 déc. 2025 ») et anglais (« 8 Aug. », « 11 Dec. 2025 »). */
  const MONTH_SHORT = LANG === 'en'
    ? ['Jan.', 'Feb.', 'Mar.', 'Apr.', 'May', 'June', 'July', 'Aug.', 'Sept.', 'Oct.', 'Nov.', 'Dec.']
    : ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'aoû.', 'sept.', 'oct.', 'nov.', 'déc.'];

  /** Date courte de la liste des sauvegardes : heure seule si aujourd'hui,
   * « 8 aoû. » sinon, et « 11 déc. 2025 » si l'année n'est pas celle en cours. */
  function formatBackupDate(iso) {
    if (!iso) return '';
    try {
      const date = new Date(iso);
      if (Number.isNaN(date.getTime())) return iso;
      if (localDayKey(iso) === localDayKey(new Date().toISOString())) {
        return formatTime(iso);
      }
      let out = `${date.getDate()} ${MONTH_SHORT[date.getMonth()]}`;
      if (date.getFullYear() !== new Date().getFullYear()) {
        out += ` ${date.getFullYear()}`;
      }
      return out;
    } catch (_) {
      return iso;
    }
  }

  /** Clé de regroupement AAAA-MM-JJ dans le fuseau local, et non en UTC. */
  function localDayKey(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${date.getFullYear()}-${month}-${day}`;
  }

  /** « Aujourd'hui », « Hier », sinon « mercredi 29 juillet 2026 ». */
  function formatDayHeading(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return T('drafts.unknown_date');

    const today = localDayKey(new Date().toISOString());
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);

    const key = localDayKey(iso);
    if (key === today) return T('drafts.today');
    if (key === localDayKey(yesterday.toISOString())) return T('drafts.yesterday');

    const label = date.toLocaleDateString(LOCALE, {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
    });
    return label.charAt(0).toUpperCase() + label.slice(1);
  }

  /* =========================================================================
   * 2. ÉTAT APPLICATIF
   * ====================================================================== */

  // Requêtes en cours, une par type d'appel : permettent à un nouveau clic
  // d'annuler le précédent plutôt que de laisser les deux courir en
  // parallèle contre un point de terminaison lent, où celui qui finit en
  // dernier écraserait silencieusement l'autre en base (voir les gardes
  // ``_generation_guard``/``_retranscribe_guard``/``_transcribe_guard`` côté
  // serveur, qui appliquent la même règle si l'annulation côté navigateur
  // n'empêche pas le thread abandonné de répondre quand même). Hors de
  // ``state`` volontairement : ce n'est pas une donnée du brouillon,
  // seulement la mécanique d'annulation d'une requête réseau.
  let pendingGenerate = null;
  let pendingRetranscribe = null;
  let pendingTranscribe = null;

  const state = {
    templates: [],
    isTemplateAdmin: true,
    //: Nom d'utilisateur courant, lu dans /api/config — sert à reconnaître
    //: ses gabarits personnels (``owner``) parmi la liste des gabarits.
    username: '',
    // Identifiant propre à CET onglet, régénéré à chaque chargement — sert
    // uniquement à reconnaître ses propres écritures dans le flux SSE
    // (voir connectLiveEvents) pour ne pas se les appliquer à soi-même.
    tabId: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()),
    consultationId: null,   // brouillon courant en base
    recording: false,
    paused: false,
    recordedSeconds: 0,
    lastSavedSnapshot: '',
    // Nombre d'enregistrements conservés pour ce brouillon — décide si
    // « Retranscrire » a quelque chose à renvoyer au service vocal (voir
    // updateActionButtons()). Mis à jour uniquement par renderRecordings().
    recordingsCount: 0,
    // Dernière note réellement générée (pas éditée) pour ce brouillon : sert
    // à savoir, avant une régénération, s'il y a une modification à perdre.
    lastGeneratedMarkdown: '',
    // Texte de l'éditeur au moment où la génération a démarré : si elle échoue
    // en cours de route (le flux a déjà rempli l'éditeur de texte partiel), on
    // restaure ce contenu — on ne laisse jamais un brouillon écrasé par un
    // texte inachevé.
    preGenerateMarkdown: '',
    // Jeton de corrélation de LA génération en cours dans CET onglet : les
    // évènements ``generation_chunk`` qui ne le portent pas (flux supplanté,
    // autre onglet) sont ignorés. Voir _generate_and_publish côté serveur.
    generationToken: null,
    // Langue dans laquelle l'audio a réellement été reconnu — pas celle du
    // gabarit courant. C'est l'écart entre les deux qui déclenche la
    // proposition de retranscription (voir maybeOfferRetranscription).
    transcriptLanguage: '',
    editingMarkdown: false,
    mobilePane: 'dictee',   // panneau visible sur petit écran
    // Le fournisseur actif contourne-t-il le STT (audio envoyé seul) ? Lu
    // depuis /api/config (voir refreshClientConfig) — decide si « Générer »
    // peut s'activer sans transcription, voir updateActionButtons().
    llmBypassStt: false,
    llmBypassSttKeepTranscript: false,
  };

  // Objets liés à l'enregistrement, regroupés pour un nettoyage simple.
  const recorder = {
    mediaRecorder: null,
    stream: null,
    timerHandle: null,
    audioContext: null,
    analyser: null,
    animationHandle: null,
    mimeType: '',
    wakeLock: null,
  };

  /**
   * Dictée en cours, côté transport.
   *
   * L'audio ne s'accumule plus dans l'onglet jusqu'au dernier moment : chaque
   * fragment produit par MediaRecorder est écrit dans IndexedDB puis poussé
   * vers le serveur. `queue` ne contient donc que ce qui n'est pas encore
   * accusé de réception — sur un réseau qui tombe, elle grossit, et se vide
   * dès le retour sans que le médecin ait à intervenir.
   */
  const dictation = {
    localId: null,        // clé de la copie locale (IndexedDB)
    sessionId: null,      // session côté serveur, null si sa création a échoué
    consultationId: null,
    seq: 0,               // numéro du prochain fragment à produire
    queue: [],            // fragments en attente d'accusé de réception
    sending: false,
    failures: 0,
    retryHandle: null,
    appliedParts: 0,      // tranches déjà recopiées dans la transcription
    pollHandle: null,
    active: false,
  };

  /** Cadences réglées par le serveur (/api/config), avec des valeurs de repli. */
  const dictationConfig = { chunkSeconds: 5, segmentSeconds: 10 };

  /** Le point de rupture « lg » de Tailwind, seuil du double panneau. */
  const isMobileLayout = () => window.matchMedia('(max-width: 1023px)').matches;

  /** L'application tourne-t-elle installée sur l'écran d'accueil ? */
  const isStandalone = () =>
    window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

  /* =========================================================================
   * 3. GABARITS — chargement et menu déroulant
   * ====================================================================== */

  async function loadTemplates(preselectId) {
    const data = await api('/api/templates');
    state.templates = data.templates || [];

    const select = $('templateSelect');
    const previous = preselectId || select.value || localStorage.getItem('consultai.lastTemplate');

    select.innerHTML = '';
    if (!state.templates.length) {
      select.innerHTML = `<option value="">${esc(T('tpl.none_option'))}</option>`;
      updateTemplateDescription();
      return;
    }

    state.templates.forEach((tpl) => {
      const option = document.createElement('option');
      option.value = tpl.id;
      option.textContent = tpl.name;
      select.appendChild(option);
    });

    // Restaure le dernier gabarit utilisé s'il existe toujours.
    if (previous && state.templates.some((t) => String(t.id) === String(previous))) {
      select.value = String(previous);
    }
    updateTemplateDescription();
  }

  function currentTemplate() {
    const id = $('templateSelect').value;
    return state.templates.find((t) => String(t.id) === String(id)) || null;
  }

  function updateTemplateDescription() {
    const tpl = currentTemplate();
    const desc = $('templateDescription');
    desc.textContent = tpl ? (tpl.description || '') : '';
    desc.classList.toggle('hidden', !desc.textContent);
    if (tpl) localStorage.setItem('consultai.lastTemplate', String(tpl.id));
  }

  /**
   * Nom lisible d'une langue, lu dans le sélecteur des gabarits — lui-même
   * rempli par le serveur depuis i18n.LANGUAGES. Évite de tenir une seconde
   * liste ici, qui divergerait le jour où une langue s'ajoute.
   */
  function languageName(code) {
    const option = document.querySelector(`#tplLanguage option[value="${code}"]`);
    return option ? option.textContent.trim() : (code || '?');
  }

  /**
   * Propose de retranscrire quand le gabarit choisi n'est pas dans la langue
   * où l'audio a été reconnu.
   *
   * La dictée démarre presque toujours avant que le gabarit soit choisi : le
   * texte part alors dans la langue par défaut, et une consultation anglaise
   * revient transcrite en français — illisible, et que la mise en forme ne
   * peut pas rattraper puisque le modèle reçoit déjà des mots faux.
   *
   * Jamais automatique : retranscrire coûte un appel facturé et écrase le
   * texte en place. C'est donc une question, pas une correction.
   */
  async function maybeOfferRetranscription() {
    const tpl = currentTemplate();
    const ancienne = state.transcriptLanguage;
    // Rien de transcrit, gabarit sans langue, ou déjà la bonne : rien à dire.
    if (!tpl || !tpl.language || !ancienne || ancienne === tpl.language) return;

    const nouvelle = languageName(tpl.language);
    const avant = languageName(ancienne);

    // Dictée en cours : l'audio vit encore dans la session, pas dans le
    // brouillon — il n'y a rien à retranscrire depuis les enregistrements. On
    // rattache la session au nouveau gabarit pour que la SUITE parte dans la
    // bonne langue, et on annonce l'écart qui subsiste.
    if (dictation.active) {
      if (dictation.sessionId) {
        try {
          await api(`/api/dictation/${dictation.sessionId}`, {
            method: 'PATCH', body: { template_id: tpl.id },
          });
        } catch (err) {
          console.warn('Gabarit de la dictée non mis à jour', err);
        }
      }
      state.transcriptLanguage = tpl.language;
      toast(T('retranscribe.during_dictation', { nouvelle, ancienne: avant }), 'info', 9000);
      return;
    }

    if (!state.consultationId || !$('transcript').value.trim()) return;
    await runRetranscription(tpl, T('retranscribe.confirm', { nouvelle, ancienne: avant }));
  }

  /**
   * Renvoie l'audio conservé au service vocal et remplace la transcription.
   *
   * Partagée par les deux déclencheurs : la proposition automatique sur
   * changement de langue, et le bouton « Retranscrire », toujours offert — on
   * change aussi de service vocal en cours de route, et c'est alors le seul
   * moyen de rejuger un enregistrement déjà transcrit.
   *
   * Le message de confirmation vient de l'appelant : ce qu'on perd est le même
   * dans les deux cas, mais ce qui le motive ne l'est pas.
   */
  async function runRetranscription(tpl, message) {
    if (!state.consultationId) return;
    if (!window.confirm(message)) return;

    const langue = languageName(
      (tpl && tpl.language) ? tpl.language : state.transcriptLanguage,
    );
    // Un nouveau clic annule le précédent au lieu de les laisser courir en
    // parallèle — même logique que generateNote(). Placé APRÈS le confirm()
    // ci-dessus : refuser la boîte de dialogue ne doit pas annuler une
    // retranscription légitimement en cours.
    if (pendingRetranscribe) pendingRetranscribe.abort();
    const controller = new AbortController();
    pendingRetranscribe = controller;

    // Durée volontairement longue : l'opération peut prendre du temps sur un
    // long enregistrement. Sans le retirer explicitement dès la réponse, ce
    // toast restait affiché jusqu'à ses 60 s même après celui de résultat.
    const enCours = toast(T('retranscribe.running', { langue }), 'info', 60000);
    try {
      const data = await api(`/api/consultations/${state.consultationId}/retranscribe`, {
        method: 'POST', signal: controller.signal,
        body: { template_id: tpl ? tpl.id : null },
      });
      enCours.dismiss();
      // Supplantée par une tentative plus récente (voir le garde côté
      // serveur) : ce texte n'a jamais été écrit en base, on ne l'affiche
      // donc pas non plus — silencieusement, ce n'est pas un échec.
      if (data.superseded) return;
      $('transcript').value = formatSentences(data.transcript);
      state.transcriptLanguage = data.stt_language || (tpl ? tpl.language : '');
      // Le serveur a déjà écrit ce texte en base. On force malgré tout une
      // sauvegarde : elle emporte aussi le gabarit qui vient de changer, et
      // écrase une éventuelle sauvegarde différée partie avec l'ancien texte.
      state.lastSavedSnapshot = '';
      scheduleSave();
      updateActionButtons();
      showTranscriptEngine(data.stt_used);
      flashElement('transcriptFooter');
      const total = data.recordings_total || 0;
      const utilises = data.recordings || 0;
      const partiel = total > utilises;
      toast(
        partiel
          ? T('retranscribe.done_partial', {
              langue, count: (data.transcript || '').length, used: utilises, total,
            })
          : T('retranscribe.done', { langue, count: (data.transcript || '').length }),
        partiel ? 'warning' : 'success',
        partiel ? 10000 : undefined,
      );
    } catch (err) {
      enCours.dismiss();
      if (err.name !== 'AbortError') {
        toast(T('retranscribe.failed', { error: err.message || err }), 'error', 8000);
      }
    } finally {
      if (pendingRetranscribe === controller) pendingRetranscribe = null;
    }
  }

  /* =========================================================================
   * 4. ENREGISTREMENT AUDIO
   * ====================================================================== */

  /**
   * Choisit le meilleur conteneur supporté par le navigateur.
   * Chrome/Edge/Firefox → webm/opus ; Safari iOS et macOS → mp4/aac.
   * Le serveur transcode ensuite systématiquement via ffmpeg, donc tous ces
   * formats sont acceptables.
   */
  function pickMimeType() {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/mp4',
      'audio/aac',
    ];
    if (typeof MediaRecorder === 'undefined') return '';
    for (const type of candidates) {
      if (MediaRecorder.isTypeSupported(type)) return type;
    }
    return ''; // laisse le navigateur décider
  }

  /* -------------------------------------------------------------------------
   * Copie locale de l'audio (IndexedDB)
   * ----------------------------------------------------------------------
   * Le serveur reçoit la dictée au fil de l'eau, mais il ne la reçoit pas
   * instantanément : entre le micro et le disque du NAS il y a un réseau, et
   * ce réseau tombe. Chaque fragment est donc d'abord écrit ici, et n'en est
   * effacé qu'une fois la dictée conclue avec succès.
   *
   * Toute opération est « au mieux » : un navigateur en navigation privée
   * peut refuser IndexedDB, et une dictée doit pouvoir se faire quand même.
   * Une erreur de stockage ne fait donc jamais échouer l'enregistrement, elle
   * ne fait que retirer le filet.
   * ---------------------------------------------------------------------- */

  const AUDIO_DB_NAME = 'consultai-audio';
  let audioDbPromise = null;

  function openAudioDb() {
    if (audioDbPromise) return audioDbPromise;
    audioDbPromise = new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) {
        reject(new Error('IndexedDB indisponible'));
        return;
      }
      const request = indexedDB.open(AUDIO_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('sessions')) {
          db.createObjectStore('sessions', { keyPath: 'localId' });
        }
        if (!db.objectStoreNames.contains('chunks')) {
          const store = db.createObjectStore('chunks', { keyPath: ['localId', 'seq'] });
          store.createIndex('localId', 'localId');
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return audioDbPromise;
  }

  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  const audioStore = {
    async createSession(meta) {
      const db = await openAudioDb();
      const record = Object.assign({ createdAt: Date.now(), chunkCount: 0 }, meta);
      await requestResult(db.transaction('sessions', 'readwrite').objectStore('sessions').put(record));
      return record;
    },

    async patchSession(localId, patch) {
      const db = await openAudioDb();
      const store = db.transaction('sessions', 'readwrite').objectStore('sessions');
      const existing = await requestResult(store.get(localId));
      if (!existing) return null;
      const merged = Object.assign(existing, patch);
      await requestResult(store.put(merged));
      return merged;
    },

    async putChunk(localId, seq, blob) {
      const db = await openAudioDb();
      await requestResult(
        db.transaction('chunks', 'readwrite').objectStore('chunks').put({ localId, seq, blob }),
      );
    },

    async listSessions() {
      const db = await openAudioDb();
      const rows = await requestResult(db.transaction('sessions').objectStore('sessions').getAll());
      return (rows || []).sort((a, b) => b.createdAt - a.createdAt);
    },

    async chunks(localId) {
      const db = await openAudioDb();
      const index = db.transaction('chunks').objectStore('chunks').index('localId');
      const rows = await requestResult(index.getAll(IDBKeyRange.only(localId)));
      return (rows || []).sort((a, b) => a.seq - b.seq);
    },

    async remove(localId) {
      const db = await openAudioDb();
      const transaction = db.transaction(['sessions', 'chunks'], 'readwrite');
      transaction.objectStore('sessions').delete(localId);
      const index = transaction.objectStore('chunks').index('localId');
      const cursorRequest = index.openKeyCursor(IDBKeyRange.only(localId));
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor) return;
        transaction.objectStore('chunks').delete(cursor.primaryKey);
        cursor.continue();
      };
      await new Promise((resolve) => {
        transaction.oncomplete = resolve;
        transaction.onerror = resolve;
        transaction.onabort = resolve;
      });
    },
  };

  /** Exécute une opération de stockage sans jamais propager son échec. */
  async function bestEffort(operation, context) {
    try {
      return await operation();
    } catch (err) {
      console.warn(`Copie locale de l'audio — ${context} :`, err);
      return null;
    }
  }

  /* -------------------------------------------------------------------------
   * Téléversement des fragments
   * ---------------------------------------------------------------------- */

  function updateDictationStatus() {
    const line = $('dictationStatus');
    const pending = dictation.queue.length;

    if (!dictation.active && !pending) {
      line.classList.add('hidden');
      return;
    }
    line.classList.remove('hidden');

    if (!pending) {
      line.className = 'text-xs px-1 text-slate-500';
      line.textContent = dictation.sessionId
        ? T('dictation.streaming')
        : T('dictation.local_only');
      return;
    }

    const seconds = Math.round(pending * dictationConfig.chunkSeconds);
    if (dictation.failures > 0) {
      line.className = 'text-xs px-1 text-amber-700 font-medium';
      line.textContent = T('dictation.retrying', { count: pending, seconds });
    } else {
      line.className = 'text-xs px-1 text-slate-500';
      line.textContent = T('dictation.sending', { count: pending });
    }
  }

  /**
   * Amène la transcription à son tout dernier caractère. Recalé dans la trame
   * de rendu suivante : la valeur vient d'être réécrite, et attendre le replan
   * de mise en page garantit que scrollHeight reflète le NOUVEAU contenu —
   * sinon certains navigateurs déroulent trop tôt, à l'ancien fond, et la
   * nouvelle ligne reste masquée sous le pied. La première tentative synchrone
   * couvre le cas général ; la seconde, différée, rattrape le replan différé.
   */
  function scrollTranscriptToBottom() {
    const box = $('transcript');
    box.scrollTop = box.scrollHeight;
    requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
  }

  /** Recopie dans la transcription les tranches que le serveur vient de rendre. */
  function applyDictationParts(session) {
    if (!session || !Array.isArray(session.parts)) return;
    const fresh = session.parts.slice(dictation.appliedParts);
    if (!fresh.length) return;
    dictation.appliedParts = session.parts.length;

    const box = $('transcript');
    const existing = box.value.replace(/\s+$/, '');
    box.value = formatSentences(existing ? `${existing} ${fresh.join(' ')}` : fresh.join(' '));
    scrollTranscriptToBottom();
    updateTranscriptMeta({ duration_seconds: session.transcribed_seconds });
    updateActionButtons();
  }

  async function postChunk(sessionId, seq, blob, durationMs) {
    const form = new FormData();
    form.append('seq', String(seq));
    form.append('duration_ms', String(Math.round(durationMs || 0)));
    form.append('file', blob, 'fragment');
    return api(`/api/dictation/${sessionId}/chunk`, { method: 'POST', body: form });
  }

  /**
   * Vide la file, un fragment à la fois et dans l'ordre.
   *
   * L'ordre est impératif : le serveur concatène les fragments tels quels,
   * un fragment sauté laisserait un blanc au milieu de la consultation. En
   * cas d'échec on garde donc le fragment en tête de file et on réessaie,
   * avec un délai qui double à chaque tentative.
   */
  function pumpQueue() {
    if (dictation.sending || !dictation.sessionId || !dictation.queue.length) {
      updateDictationStatus();
      return;
    }
    dictation.sending = true;
    const item = dictation.queue[0];

    postChunk(dictation.sessionId, item.seq, item.blob, item.durationMs)
      .then((session) => {
        dictation.queue.shift();
        dictation.failures = 0;
        applyDictationParts(session);
      })
      .catch((err) => {
        dictation.failures += 1;
        console.warn(`Fragment ${item.seq} non transmis :`, err);
        clearTimeout(dictation.retryHandle);
        dictation.retryHandle = setTimeout(
          pumpQueue, Math.min(30000, 1000 * Math.pow(2, dictation.failures)),
        );
      })
      .finally(() => {
        dictation.sending = false;
        updateDictationStatus();
        if (dictation.queue.length && dictation.failures === 0) pumpQueue();
      });
  }

  /** Attend que tout soit accusé de réception ; lève si l'envoi ne passe pas. */
  async function drainQueue(maxWaitMs = 60000) {
    const deadline = Date.now() + maxWaitMs;
    clearTimeout(dictation.retryHandle);
    dictation.failures = 0;
    pumpQueue();

    while (dictation.queue.length) {
      if (Date.now() > deadline) {
        throw new Error(T('dictation.drain_failed', { count: dictation.queue.length }));
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
      if (!dictation.sending) pumpQueue();
    }
  }

  function startDictationPolling() {
    stopDictationPolling();
    // Filet pour les moments sans téléversement — pause, ou fin de dictée
    // pendant que le serveur découpe encore : la réponse d'un fragment suffit
    // le reste du temps à rapatrier les nouvelles tranches.
    dictation.pollHandle = setInterval(async () => {
      if (!dictation.sessionId || dictation.sending) return;
      try {
        applyDictationParts(await api(`/api/dictation/${dictation.sessionId}`));
      } catch (_) {
        /* le prochain tour réessaiera */
      }
    }, 7000);
  }

  function stopDictationPolling() {
    if (dictation.pollHandle) clearInterval(dictation.pollHandle);
    dictation.pollHandle = null;
  }

  function resetDictationState() {
    clearTimeout(dictation.retryHandle);
    stopDictationPolling();
    dictation.localId = null;
    dictation.sessionId = null;
    dictation.consultationId = null;
    dictation.seq = 0;
    dictation.queue = [];
    dictation.sending = false;
    dictation.failures = 0;
    dictation.retryHandle = null;
    dictation.appliedParts = 0;
    dictation.active = false;
    updateDictationStatus();
  }

  /* -------------------------------------------------------------------------
   * Waveform du micro
   * ----------------------------------------------------------------------
   * Un simple témoin d'activité ne dit pas grand-chose : il s'allume aussi
   * bien pour une voix que pour du souffle. On garde donc l'historique des
   * derniers niveaux et on le fait défiler, ce qui rend lisible le rythme de
   * la parole — et rend une panne de micro évidente, la trace restant plate
   * pendant qu'on parle.
   * ---------------------------------------------------------------------- */

  /** Largeur d'une barre et de son espacement, en pixels CSS. */
  const WAVE_BAR = 3;
  const WAVE_GAP = 1;
  /** Cadence d'échantillonnage : le défilement doit être indépendant du FPS. */
  const WAVE_SAMPLE_MS = 55;

  const wave = {
    canvases: [],     // { canvas, ctx, capacity, resizeObserver } — barre d'outils ET dictaphone
    levels: [],       // historique, du plus ancien au plus récent
    peak: 0,          // crête accumulée depuis le dernier échantillon retenu
    lastSample: 0,
  };

  /** Nombre de barres à conserver : la plus large des zones d'affichage. */
  function waveCapacity() {
    return Math.max(1, ...wave.canvases.map((c) => c.capacity || 0));
  }

  /** Adapte la résolution du canvas à sa taille CSS et à la densité d'écran. */
  function resizeWaveCanvas(entry) {
    const rect = entry.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const dpr = window.devicePixelRatio || 1;
    entry.canvas.width = Math.round(rect.width * dpr);
    entry.canvas.height = Math.round(rect.height * dpr);
    // Tout le tracé se fait ensuite en pixels CSS.
    entry.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    entry.capacity = Math.max(1, Math.floor(rect.width / (WAVE_BAR + WAVE_GAP)));
    if (wave.levels.length > waveCapacity()) {
      wave.levels = wave.levels.slice(-waveCapacity());
    }
    drawWave();
  }

  function drawWave() {
    wave.canvases.forEach((entry) => {
      const rect = entry.canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;   // calque dictaphone masqué
      const { ctx } = entry;
      const width = rect.width;
      const height = rect.height;

      const middle = height / 2;
      ctx.clearRect(0, 0, width, height);

      // Ligne de repos : sans elle, un canvas vide ressemble à un bogue
      // d'affichage plutôt qu'à un micro au silence.
      ctx.fillStyle = '#cbd5e1';                       // slate-300
      ctx.fillRect(0, middle - 0.5, width, 1);

      // Teal à l'enregistrement, ambre en pause : la même convention que la
      // pastille d'état et le bouton, pour qu'un coup d'œil suffise.
      ctx.fillStyle = state.paused ? '#f59e0b' : (state.accentWaveColor || '#14b8a6');

      const step = WAVE_BAR + WAVE_GAP;
      // Les barres sont ancrées à droite : la plus récente reste au bord, et
      // l'historique s'échappe vers la gauche.
      const visible = wave.levels.slice(-entry.capacity);
      const offset = width - visible.length * step;

      for (let i = 0; i < visible.length; i += 1) {
        // 2 px minimum : un passage silencieux doit rester visible comme une
        // portion de trace, pas comme un trou dans le tracé.
        const barHeight = Math.max(2, visible[i] * (height - 4));
        ctx.fillRect(offset + i * step, middle - barHeight / 2, WAVE_BAR, barHeight);
      }
    });
  }

  /** Waveform du micro — confirme visuellement que le micro capte bien. */
  function startWaveform(stream) {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      recorder.audioContext = new AudioCtx();
      const source = recorder.audioContext.createMediaStreamSource(stream);
      recorder.analyser = recorder.audioContext.createAnalyser();
      recorder.analyser.fftSize = 512;
      source.connect(recorder.analyser);

      const buffer = new Uint8Array(recorder.analyser.frequencyBinCount);
      wave.levels = [];
      wave.peak = 0;
      wave.lastSample = 0;
      wave.canvases.forEach(resizeWaveCanvas);

      const tick = (now) => {
        recorder.analyser.getByteTimeDomainData(buffer);
        // Écart quadratique moyen par rapport au silence (128).
        let sum = 0;
        for (let i = 0; i < buffer.length; i += 1) {
          const deviation = (buffer[i] - 128) / 128;
          sum += deviation * deviation;
        }
        const level = Math.min(1, Math.sqrt(sum / buffer.length) * 2.8);

        // En pause, le tracé se fige : continuer à empiler des zéros
        // effacerait en quelques secondes ce qui vient d'être dicté.
        if (!state.paused) {
          // On retient la crête entre deux échantillons plutôt que la dernière
          // valeur lue : une consonne brève ne doit pas passer entre les
          // mailles du filet et laisser croire à un micro sourd.
          wave.peak = Math.max(wave.peak, level);

          if (!wave.lastSample) wave.lastSample = now;
          if (now - wave.lastSample >= WAVE_SAMPLE_MS) {
            wave.lastSample = now;
            wave.levels.push(wave.peak);
            wave.peak = 0;
            if (wave.levels.length > waveCapacity()) wave.levels.shift();
          }
        } else {
          // À la reprise, l'horloge repart de zéro plutôt que de rattraper
          // d'un coup toute la durée de la pause.
          wave.lastSample = now;
        }

        drawWave();
        recorder.animationHandle = requestAnimationFrame(tick);
      };
      recorder.animationHandle = requestAnimationFrame(tick);
    } catch (err) {
      // Le waveform est un simple confort : on n'interrompt jamais
      // l'enregistrement s'il échoue.
      console.warn('Analyseur audio indisponible :', err);
    }
  }

  function stopWaveform() {
    if (recorder.animationHandle) cancelAnimationFrame(recorder.animationHandle);
    recorder.animationHandle = null;
    if (recorder.audioContext) {
      recorder.audioContext.close().catch(() => {});
      recorder.audioContext = null;
    }
    wave.levels = [];
    wave.peak = 0;
    wave.lastSample = 0;
    drawWave();
  }

  /** Prépare un canvas waveform : il doit exister même à l'arrêt. */
  function registerWaveCanvas(id) {
    const canvas = $(id);
    if (!canvas || !canvas.getContext) return;
    const entry = { canvas, ctx: canvas.getContext('2d'), capacity: 0, resizeObserver: null };

    if (typeof ResizeObserver !== 'undefined') {
      entry.resizeObserver = new ResizeObserver(() => resizeWaveCanvas(entry));
      entry.resizeObserver.observe(canvas);
    } else {
      // Safari ancien : la rotation de l'iPad reste le cas à couvrir.
      window.addEventListener('resize', () => resizeWaveCanvas(entry));
    }
    wave.canvases.push(entry);
    resizeWaveCanvas(entry);
  }

  function setupWaveform() {
    registerWaveCanvas('levelWave');
    // Miroir dans le mode dictaphone : l'historique est partagé, seul le
    // nombre de barres affichées dépend de la largeur de chaque canvas.
    registerWaveCanvas('dictaphoneWave');
  }

  function startTimer() {
    stopTimer();
    recorder.timerHandle = setInterval(() => {
      if (!state.paused) {
        state.recordedSeconds += 1;
        $('timer').textContent = formatDuration(state.recordedSeconds);
        if (dphone.active) $('dictaphoneTimer').textContent = formatDuration(state.recordedSeconds);
      }
    }, 1000);
  }

  function stopTimer() {
    if (recorder.timerHandle) clearInterval(recorder.timerHandle);
    recorder.timerHandle = null;
  }

  // Icônes du bouton pause/reprise. Le bouton contient un SVG et non du
  // texte : on remplace donc son contenu, sans jamais utiliser textContent
  // qui effacerait l'icône.
  const ICON_PAUSE = '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">'
    + '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  const ICON_RESUME = '<svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">'
    + '<path d="M8 5.14v13.72a1 1 0 0 0 1.53.85l10.7-6.86a1 1 0 0 0 0-1.7L9.53 4.29A1 1 0 0 0 8 5.14z"/></svg>';

  function updateRecordingUI() {
    const dot = $('recDot');
    const label = $('btnRecordLabel');

    $('btnPause').disabled = !state.recording;
    $('btnFinish').disabled = !state.recording;
    $('btnRecord').disabled = state.recording;

    // « Arrêter sans envoyer » et l'import se relaient : le premier n'a de
    // sens qu'en cours de dictée, le second n'en a qu'à l'arrêt.
    $('btnAbort').classList.toggle('hidden', !state.recording);
    $('btnAbort').classList.toggle('grid', state.recording);
    $('importAudioLabel').classList.toggle('hidden', state.recording);

    if (state.recording && !state.paused) {
      dot.className = 'w-3 h-3 rounded-full bg-red-600 rec-dot shrink-0';
      label.textContent = T('rec.recording');
      $('btnPause').innerHTML = ICON_PAUSE;
      $('btnPause').title = T('rec.pause');
    } else if (state.recording && state.paused) {
      dot.className = 'w-3 h-3 rounded-full bg-amber-500 shrink-0';
      label.textContent = T('rec.paused');
      $('btnPause').innerHTML = ICON_RESUME;
      $('btnPause').title = T('rec.resume');
    } else {
      dot.className = 'w-3 h-3 rounded-full bg-slate-300 shrink-0';
      label.textContent = T('rec.record');
      $('btnPause').innerHTML = ICON_PAUSE;
    }

    // Le texte d'aide cède la place à l'état de la dictée : tant que la
    // première tranche n'est pas revenue, la zone vide doit dire ce qui se
    // passe plutôt que répéter comment démarrer — on l'a déjà fait.
    $('transcript').placeholder = state.recording
      ? T('transcript.placeholder_recording')
      : T('transcript.placeholder');

    syncDictaphoneUI();
  }

  /* -------------------------------------------------------------------------
   * Mode dictaphone (téléphone retourné)
   * ----------------------------------------------------------------------
   * Le micro du téléphone est en bas : retourné, il fait face à qui parle.
   * L'écran devient alors un unique gros bouton Enregistrer / Pause.
   *
   * Deux détections, car les plateformes ne se comportent pas pareil :
   *
   * * screen.orientation à 180° — Android laisse (parfois) l'écran pivoter
   *   tête en bas ; l'affichage est alors DÉJÀ à l'endroit, le calque est
   *   posé tel quel ;
   * * deviceorientation — iOS ne pivote jamais l'écran à 180°, mais les
   *   capteurs voient le téléphone physiquement retourné : le calque est
   *   alors tourné de 180° en CSS. Sur iOS 13+, ces événements exigent une
   *   permission demandée sur un geste (voir maybeRequestOrientationPermission).
   *
   * Anti-rebond : un état ne s'applique qu'après 400 ms stables, pour ne
   * pas faire clignoter le calque pendant le mouvement de bascule.
   * ---------------------------------------------------------------------- */
  /** iOS : aucune demande de permission capteur ; le mode retourné
   * s'y active uniquement par le bouton « Mode retourné ». */
  const isIOSDevice = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (/Macintosh/.test(navigator.userAgent) && 'ontouchstart' in window);

  const dphone = { active: false, rotate: false, candidate: null, since: 0 };

  const ICON_MIC_BIG = '<svg class="w-20 h-20" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/>'
    + '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
  const ICON_PAUSE_BIG = '<svg class="w-16 h-16" viewBox="0 0 24 24" fill="currentColor">'
    + '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  const ICON_RESUME_BIG = '<svg class="w-16 h-16" viewBox="0 0 24 24" fill="currentColor">'
    + '<path d="M8 5.14v13.72a1 1 0 0 0 1.53.85l10.7-6.86a1 1 0 0 0 0-1.7L9.53 4.29A1 1 0 0 0 8 5.14z"/></svg>';

  function screenIsUpsideDown() {
    const angle = (screen.orientation && typeof screen.orientation.angle === 'number')
      ? screen.orientation.angle
      : (typeof window.orientation === 'number' ? window.orientation : 0);
    return Math.abs(angle) === 180;
  }

  /** Téléphone vertical, tête en bas : beta ≈ -90°, quelle que soit la bascule. */
  function sensorSaysUpsideDown(ev) {
    if (!ev || ev.beta === null || ev.beta === undefined) return false;
    return ev.beta < -45 && ev.beta > -135 && Math.abs(ev.gamma || 0) < 45;
  }

  function setDictaphone(active, rotate) {
    if (dphone.active === active && dphone.rotate === rotate) return;
    dphone.active = active;
    dphone.rotate = active ? rotate : false;
    const overlay = $('dictaphoneOverlay');
    if (!overlay) return;
    overlay.classList.toggle('hidden', !active);
    overlay.style.transform = dphone.rotate ? 'rotate(180deg)' : '';
    if (active) syncDictaphoneUI();
  }

  function applyDictaphoneCandidate(ev) {
    // L'écran pivoté par l'OS prime : l'affichage est déjà à l'endroit.
    const c = screenIsUpsideDown()
      ? { active: true, rotate: false }
      : sensorSaysUpsideDown(ev)
        ? { active: true, rotate: true }
        : { active: false, rotate: false };
    const now = Date.now();
    if (!dphone.candidate || dphone.candidate.active !== c.active || dphone.candidate.rotate !== c.rotate) {
      dphone.candidate = c;
      dphone.since = now;
      return;
    }
    if (now - dphone.since >= 400) setDictaphone(c.active, c.rotate);
  }

  window.addEventListener('deviceorientation', applyDictaphoneCandidate);
  if (screen.orientation && screen.orientation.addEventListener) {
    screen.orientation.addEventListener('change', () => applyDictaphoneCandidate(null));
  } else {
    // Safari ancien : l'événement fenêtre tient lieu de change.
    window.addEventListener('orientationchange',
      () => setTimeout(() => applyDictaphoneCandidate(null), 100));
  }

  /** iOS 13+ n'émet deviceorientation qu'après une permission, sur un geste. */
  let orientationPermissionAsked = false;
  function maybeRequestOrientationPermission() {
    if (isIOSDevice) return; // iOS : aucune demande de permission
    if (orientationPermissionAsked) return;
    if (typeof DeviceOrientationEvent === 'undefined'
        || typeof DeviceOrientationEvent.requestPermission !== 'function') return;
    orientationPermissionAsked = true;
    DeviceOrientationEvent.requestPermission().catch(() => {});
  }
  document.addEventListener('pointerdown', maybeRequestOrientationPermission);

  /** Calque du mode dictaphone : miroir de updateRecordingUI(). */
  function syncDictaphoneUI() {
    const main = $('btnDictaphoneMain');
    if (!main) return;
    const actions = $('dictaphoneActions');
    $('dictaphoneTimer').textContent = formatDuration(state.recordedSeconds);
    // Visibilité seule (jamais display:none) : le slot conserve sa hauteur et
    // le gros bouton reste centré quand les actions apparaissent.
    actions.classList.toggle('invisible', !state.recording);
    const base = 'w-56 h-56 rounded-full grid place-items-center shadow-2xl active:scale-95 transition ';
    if (state.recording && !state.paused) {
      main.className = base + 'bg-red-600 rec-dot';
      main.innerHTML = ICON_PAUSE_BIG;
      main.title = T('rec.pause');
    } else if (state.recording && state.paused) {
      main.className = base + 'bg-amber-500';
      main.innerHTML = ICON_RESUME_BIG;
      main.title = T('rec.resume');
    } else {
      main.className = base + 'bg-red-600';
      main.innerHTML = ICON_MIC_BIG;
      main.title = T('rec.record');
    }
  }

  /* -------------------------------------------------------------------------
   * Verrou d'écran
   * ----------------------------------------------------------------------
   * Sans lui, le téléphone met l'écran en veille pendant une longue dictée,
   * ce qui suspend l'onglet et interrompt l'enregistrement. L'API n'est pas
   * disponible partout (iOS l'a ajoutée tardivement) : toute erreur est donc
   * silencieuse, l'enregistrement doit continuer quoi qu'il arrive.
   * ---------------------------------------------------------------------- */
  async function acquireWakeLock() {
    if (!('wakeLock' in navigator)) return;
    try {
      recorder.wakeLock = await navigator.wakeLock.request('screen');
      recorder.wakeLock.addEventListener('release', () => { recorder.wakeLock = null; });
    } catch (err) {
      console.warn('Verrou d\'écran indisponible :', err);
    }
  }

  function releaseWakeLock() {
    if (recorder.wakeLock) {
      recorder.wakeLock.release().catch(() => {});
      recorder.wakeLock = null;
    }
  }

  // Le verrou saute quand l'onglet passe en arrière-plan : on le reprend au
  // retour si l'enregistrement est toujours en cours.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.recording && !recorder.wakeLock) {
      acquireWakeLock();
    }
    // Filet de sécurité : EventSource se reconnecte tout seul, mais une mise
    // en veille prolongée (Safari iOS notamment) peut la tuer sans jamais
    // relancer ce mécanisme intégré.
    if (document.visibilityState === 'visible' && liveSource
        && liveSource.readyState === EventSource.CLOSED) {
      connectLiveEvents();
    }
  });

  async function startRecording() {
    // getUserMedia exige un contexte sécurisé : HTTPS ou localhost.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast(T('mic.insecure'), 'error', 9000);
      return;
    }

    try {
      recorder.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (err) {
      let message;
      if (err && err.name === 'NotAllowedError') {
        message = T('mic.denied');
      } else {
        message = T('mic.unavailable', { error: err && err.message ? err.message : err });
      }
      // Certaines versions d'iOS refusent le micro à une application lancée
      // depuis l'écran d'accueil : l'indiquer évite un long dépannage.
      if (isStandalone() && /iPhone|iPad|iPod/.test(navigator.userAgent)) {
        message += T('mic.ios_standalone');
      }
      toast(message, 'error', 11000);
      return;
    }

    recorder.mimeType = pickMimeType();

    try {
      recorder.mediaRecorder = recorder.mimeType
        ? new MediaRecorder(recorder.stream, { mimeType: recorder.mimeType, audioBitsPerSecond: 64000 })
        : new MediaRecorder(recorder.stream);
    } catch (err) {
      toast(T('mic.recorder_failed', { error: err.message }), 'error');
      recorder.stream.getTracks().forEach((track) => track.stop());
      return;
    }

    // --- Ouverture de la session côté serveur ------------------------------
    // Le brouillon existe avant la première seconde d'audio : c'est lui qui
    // recevra le texte, et il survit à la fermeture de l'onglet.
    resetDictationState();
    dictation.active = true;
    dictation.localId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

    try {
      dictation.consultationId = await ensureConsultation();
      const tpl = currentTemplate();
      const session = await api('/api/dictation', {
        method: 'POST',
        body: {
          consultation_id: dictation.consultationId,
          template_id: tpl ? tpl.id : null,
          mime_type: recorder.mimeType || 'audio/webm',
        },
      });
      dictation.sessionId = session.session_id;
    } catch (err) {
      // Serveur injoignable : on enregistre quand même. Les fragments
      // s'accumulent localement et partiront à « Terminer », ou plus tard
      // depuis la bannière de récupération. Refuser de dicter parce que le
      // Wi-Fi est tombé serait le pire des deux mondes.
      toast(T('dictation.server_unreachable', { error: err.message }), 'warning', 9000);
    }

    await bestEffort(() => audioStore.createSession({
      localId: dictation.localId,
      serverSessionId: dictation.sessionId,
      consultationId: dictation.consultationId,
      mimeType: recorder.mimeType || 'audio/webm',
      label: buildTitle(),
    }), 'création de la session');

    recorder.mediaRecorder.ondataavailable = (event) => {
      if (!event.data || event.data.size === 0) return;
      const seq = dictation.seq;
      dictation.seq += 1;
      const durationMs = dictationConfig.chunkSeconds * 1000;

      dictation.queue.push({ seq, blob: event.data, durationMs });
      bestEffort(() => audioStore.putChunk(dictation.localId, seq, event.data),
                 `écriture du fragment ${seq}`);
      pumpQueue();
    };
    recorder.mediaRecorder.onerror = (event) => {
      toast(T('mic.recorder_error', { error: event.error && event.error.name }), 'error');
    };

    // Un fragment toutes les quelques secondes : c'est l'unité de
    // téléversement, donc aussi le pire cas de perte sur une panne franche.
    recorder.mediaRecorder.start(dictationConfig.chunkSeconds * 1000);

    state.recording = true;
    state.paused = false;
    state.recordedSeconds = 0;
    $('timer').textContent = '00:00';

    startTimer();
    startWaveform(recorder.stream);
    acquireWakeLock();          // empêche la mise en veille pendant la dictée
    startDictationPolling();
    updateRecordingUI();
    updateDictationStatus();
    window.addEventListener('beforeunload', warnBeforeUnload);
    hideRecoveryBanner();
  }

  function togglePause() {
    if (!recorder.mediaRecorder || !state.recording) return;
    if (state.paused) {
      recorder.mediaRecorder.resume();
      state.paused = false;
    } else {
      recorder.mediaRecorder.pause();
      state.paused = true;
    }
    updateRecordingUI();
  }

  /**
   * Arrête le micro et attend le tout dernier fragment.
   *
   * MediaRecorder émet un « ondataavailable » final après stop() : sans cette
   * attente, les dernières secondes de la dictée seraient perdues — exactement
   * le passage où le médecin conclut.
   */
  function stopMicrophone() {
    return new Promise((resolve) => {
      const cleanup = () => {
        state.recording = false;
        state.paused = false;
        stopTimer();
        stopWaveform();
        releaseWakeLock();
        updateRecordingUI();
        window.removeEventListener('beforeunload', warnBeforeUnload);
        if (recorder.stream) {
          recorder.stream.getTracks().forEach((track) => track.stop());
          recorder.stream = null;
        }
        resolve();
      };

      if (!recorder.mediaRecorder || recorder.mediaRecorder.state === 'inactive') {
        cleanup();
        return;
      }
      recorder.mediaRecorder.onstop = cleanup;
      recorder.mediaRecorder.stop();
    });
  }

  /**
   * Conclut proprement la dictée encore active et rapatrie la transcription
   * COMPLÈTE.
   *
   * Arrêter le micro est ce qui vide le tampon du MediaRecorder : les
   * dernières secondes — celles dites juste avant une pause — y attendent
   * encore, hors de la transcription. « Terminer » et « Mettre en forme »
   * doivent tous deux vider ce tampon avant de continuer.
   *
   * Renvoie false si la dictée était trop courte pour mériter une session,
   * ou si la finalisation a échoué (la bannière de récupération reprend
   * alors le relais).
   */
  async function completeDictation() {
    await stopMicrophone();

    if (!dictation.seq) {
      toast(T('dictation.too_short'), 'warning');
      await bestEffort(() => audioStore.remove(dictation.localId), 'nettoyage');
      resetDictationState();
      return false;
    }

    setBusy(true, T('dictation.finishing'));
    try {
      if (!dictation.sessionId) {
        // La session n'a jamais pu être ouverte : tout est encore local, on
        // rejoue l'enregistrement complet.
        await uploadStoredSession(dictation.localId, { silent: true });
      } else {
        await drainQueue();
        const result = await api(`/api/dictation/${dictation.sessionId}/finish`, { method: 'POST' });
        applyDictationParts(result);
        if (result.stt_language) state.transcriptLanguage = result.stt_language;
        showTranscriptEngine(result.stt_used);
        await bestEffort(() => audioStore.remove(dictation.localId), 'nettoyage');
      }
      // L'audio vient d'être rattaché au brouillon par le serveur : à
      // rafraîchir AVANT de décider si on peut générer (audio-only).
      await loadRecordings();
      return true;
    } catch (err) {
      toast(err.message, 'error', 12000);
      // L'audio reste sur le serveur et dans le navigateur : la bannière
      // permet de reprendre là où l'on s'est arrêté.
      resetDictationState();
      refreshRecoveryBanner();
      return false;
    } finally {
      setBusy(false);
      resetDictationState();
    }
  }

  /** « Terminer » : conclut la dictée et rapatrie le texte restant. */
  async function finishRecording() {
    if (!state.recording && !dictation.active) return;
    if (!(await completeDictation())) return;

    const transcript = $('transcript').value.trim();
    if (transcript) {
      // La transcription vient du serveur : la marquer comme sauvegardée
      // évite une réécriture inutile, mais on force un enregistrement pour
      // que le titre et les métadonnées suivent.
      state.lastSavedSnapshot = '';
      scheduleSave();
      flashElement('transcriptFooter');
      toast(T('dictation.finished', { count: transcript.length }), 'success');
    } else if (audioOnlyReady()) {
      // Contournement du STT actif (Gemini / Qwen Omni en audio direct) :
      // c'est le fonctionnement NORMAL de ce réglage, pas un échec — il n'y
      // a jamais de transcription à attendre, l'audio suffit. On enchaîne
      // directement sur la mise en forme, comme si le médecin avait cliqué
      // lui-même.
      await generateNote();
    } else {
      toast(T('dictation.no_speech'), 'warning', 8000);
    }
  }

  /** « Arrêter sans envoyer » : la dictée est abandonnée et son audio effacé. */
  async function abortRecording() {
    if (!state.recording && !dictation.active) return;

    const alreadyTranscribed = dictation.appliedParts > 0;
    const message = alreadyTranscribed
      ? T('dictation.confirm_abort_transcribed')
      : T('dictation.confirm_abort');
    if (!window.confirm(message)) return;

    await stopMicrophone();
    const { sessionId, localId } = dictation;
    dictation.queue = [];
    resetDictationState();

    if (sessionId) {
      try {
        await api(`/api/dictation/${sessionId}/cancel`, { method: 'POST' });
      } catch (err) {
        console.warn(T('dictation.cancel_failed'), err);
      }
    }
    await bestEffort(() => audioStore.remove(localId), 'suppression');
    toast(T('dictation.aborted'), 'info');
  }

  function warnBeforeUnload(event) {
    event.preventDefault();
    event.returnValue = '';
    return '';
  }

  /* -------------------------------------------------------------------------
   * Récupération d'une dictée interrompue
   * ----------------------------------------------------------------------
   * Deux sources, réunies par l'identifiant de session : ce que le serveur a
   * reçu (il le garde plusieurs jours) et ce que le navigateur a gardé. Le
   * cas le plus fréquent — onglet fermé, application iOS tuée en arrière-plan
   * — laisse les deux, et la reprise n'envoie alors que les fragments qui
   * manquent au serveur : le texte déjà transcrit n'est pas redemandé.
   * ---------------------------------------------------------------------- */

  function hideRecoveryBanner() {
    $('recoveryBanner').classList.add('hidden');
  }

  async function refreshRecoveryBanner() {
    if (state.recording) return;

    let serverSessions = [];
    try {
      serverSessions = (await api('/api/dictation')).sessions || [];
    } catch (_) {
      /* hors ligne : on se contente des copies locales */
    }
    const localSessions = (await bestEffort(() => audioStore.listSessions(), 'inventaire')) || [];

    const byServerId = new Map(serverSessions.map((s) => [s.session_id, s]));
    const entries = [];

    localSessions.forEach((local) => {
      entries.push({
        localId: local.localId,
        server: byServerId.get(local.serverSessionId) || null,
        consultationId: local.consultationId,
        mimeType: local.mimeType,
        label: local.label,
        createdAt: local.createdAt,
      });
      byServerId.delete(local.serverSessionId);
    });
    // Dictées connues du seul serveur : autre navigateur, ou copie locale
    // effacée. L'audio est là, elles restent récupérables.
    byServerId.forEach((server) => {
      entries.push({
        localId: null,
        server,
        consultationId: server.consultation_id,
        label: '',
        createdAt: server.created_at * 1000,
      });
    });

    const banner = $('recoveryBanner');
    const list = $('recoveryList');
    if (!entries.length) {
      banner.classList.add('hidden');
      list.innerHTML = '';
      return;
    }

    entries.sort((a, b) => b.createdAt - a.createdAt);
    list.innerHTML = entries.map((entry, index) => {
      const when = formatDateTime(new Date(entry.createdAt).toISOString());
      const seconds = entry.server ? entry.server.received_seconds : 0;

      // Une session inconnue de CET onglet (pas de copie locale), encore au
      // statut « recording » et dont le dernier fragment date de moins d'une
      // minute : très probablement une dictée qui tourne VRAIMENT ailleurs en
      // ce moment (un autre appareil, un autre onglet), pas une dictée
      // abandonnée. « Reprendre et transcrire » y appellerait /finish et
      // tronquerait cette dictée en cours — on propose de la suivre à la
      // place (voir onSyncTranscript, qui prend le relais une fois
      // l'onglet ouvert sur la même consultation).
      const liveElsewhere = !entry.localId && entry.server && entry.server.status === 'recording'
        && (Date.now() / 1000 - entry.server.updated_at) < 60;

      if (liveElsewhere) {
        return `<li class="text-sm" data-index="${index}">
          <p class="text-slate-600">${esc([entry.label || T('recovery.unnamed'), T('recovery.live_elsewhere')]
            .filter(Boolean).join(' · '))}</p>
          <div class="flex flex-wrap gap-2 mt-1">
            <button type="button" data-act="follow" data-index="${index}"
                    class="accent-btn px-2.5 py-1 rounded-md text-xs font-medium">
              ${esc(T('sync.follow'))}</button>
          </div>
        </li>`;
      }

      const detail = [
        entry.label || T('recovery.unnamed'),
        when,
        seconds
          ? T('recovery.received', { duration: formatDuration(seconds) })
          : T('recovery.not_received'),
      ].filter(Boolean).join(' · ');

      return `<li class="text-sm" data-index="${index}">
        <p class="text-amber-900">${esc(detail)}</p>
        <div class="flex flex-wrap gap-2 mt-1">
          <!-- « Reprendre » ouvre sans conclure (peekStoredSession) — n'a de
               sens que si le serveur a déjà une session à rouvrir. Conclure
               (audio archivé, reliquat transcrit) reste un geste à part, le
               même mot que pour la dictée en cours (rec.finish_title) ;
               seul bouton PLEIN quand « Reprendre » ne peut pas s'afficher
               (dictée jamais parvenue au serveur). -->
          ${entry.server ? `<button type="button" data-act="resume" data-index="${index}"
                  class="px-2.5 py-1 rounded-md bg-amber-600 text-white text-xs font-medium
                         hover:bg-amber-700">${esc(T('recovery.resume'))}</button>` : ''}
          <button type="button" data-act="finish" data-index="${index}"
                  class="px-2.5 py-1 rounded-md text-xs font-medium ${entry.server
                    ? 'border border-amber-400 text-amber-800 hover:bg-amber-100'
                    : 'bg-amber-600 text-white hover:bg-amber-700'}">
            ${esc(T('rec.finish_title'))}</button>
          ${entry.localId ? `<button type="button" data-act="download" data-index="${index}"
                  class="px-2.5 py-1 rounded-md border border-amber-400 text-amber-800 text-xs
                         hover:bg-amber-100">${esc(T('recovery.download'))}</button>` : ''}
          <button type="button" data-act="discard" data-index="${index}"
                  class="px-2.5 py-1 rounded-md border border-amber-400 text-amber-800 text-xs
                         hover:bg-amber-100">${esc(T('recovery.discard'))}</button>
        </div>
      </li>`;
    }).join('');

    list.querySelectorAll('button[data-act]').forEach((button) => {
      button.addEventListener('click', () => {
        const entry = entries[Number(button.dataset.index)];
        if (button.dataset.act === 'follow') {
          // Rien à recharger ni à conclure ici (voir onSyncTranscript) : le
          // seul geste manquant est de sortir le bandeau de l'écran, sinon
          // il reste affiché au-dessus de la consultation qu'on vient
          // d'ouvrir pour la suivre.
          loadDraft(entry.consultationId);
          hideRecoveryBanner();
        } else if (button.dataset.act === 'resume') peekStoredSession(entry);
        else if (button.dataset.act === 'finish') finishStoredSession(entry);
        else if (button.dataset.act === 'download') downloadStoredSession(entry);
        else discardStoredSession(entry);
      });
    });
    banner.classList.remove('hidden');
  }

  /** Reconstitue le fichier audio complet à partir des fragments conservés. */
  async function assembleStoredAudio(localId, mimeType) {
    const chunks = await audioStore.chunks(localId);
    if (!chunks.length) return null;
    return new Blob(chunks.map((row) => row.blob), { type: mimeType || 'audio/webm' });
  }

  async function downloadStoredSession(entry) {
    const blob = await bestEffort(
      () => assembleStoredAudio(entry.localId, entry.mimeType), 'assemblage',
    );
    if (!blob) {
      toast(T('recovery.nothing_local'), 'warning');
      return;
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `dictee-${new Date(entry.createdAt).toISOString().slice(0, 19).replace(/[:T]/g, '-')}`
      + (entry.mimeType && entry.mimeType.includes('mp4') ? '.mp4' : '.webm');
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  async function discardStoredSession(entry) {
    if (!window.confirm(T('recovery.confirm_discard'))) return;
    if (entry.server) {
      try {
        await api(`/api/dictation/${entry.server.session_id}/cancel`, { method: 'POST' });
      } catch (err) {
        console.warn(T('recovery.delete_failed'), err);
      }
    }
    if (entry.localId) await bestEffort(() => audioStore.remove(entry.localId), 'suppression');
    toast(T('recovery.deleted'), 'info');
    refreshRecoveryBanner();
  }

  /**
   * Envoie ce qui manque au serveur pour cette session, sans rien conclure :
   * la consultation s'ouvre sur ce qui est déjà transcrit (traité au fil de
   * la dictée d'origine), la session reste ouverte côté serveur pour un
   * « Terminer et transcrire » explicite, plus tard — voir finishStoredSession.
   * N'a de sens que si le serveur connaît déjà cette session (voir le rendu
   * du bouton, absent sinon) : une dictée qui n'a jamais atteint le serveur
   * n'a rien à montrer avant d'être intégralement envoyée.
   */
  async function peekStoredSession(entry) {
    if (!entry.server) return;
    setBusy(true, T('recovery.peeking'));
    try {
      if (entry.localId) {
        const chunks = await audioStore.chunks(entry.localId);
        for (const row of chunks) {
          if (row.seq < entry.server.next_seq) continue;
          await postChunk(entry.server.session_id, row.seq, row.blob, dictationConfig.chunkSeconds * 1000);
        }
      }
      const current = await api(`/api/dictation/${entry.server.session_id}`);
      await loadDraft(current.consultation_id);
      toast(T('recovery.peeked'), 'info', 8000);
    } catch (err) {
      toast(T('recovery.resume_failed', { error: err.message }), 'error', 12000);
    } finally {
      setBusy(false);
      refreshRecoveryBanner();
    }
  }

  /**
   * Reprend une dictée interrompue et la CONCLUT : complète ce qui manque au
   * serveur, transcrit le reliquat, archive l'audio. Le texte retourne dans
   * la consultation d'origine.
   */
  async function finishStoredSession(entry) {
    setBusy(true, T('recovery.resuming'));
    try {
      if (entry.server) {
        // Le serveur a déjà une partie de l'audio : on ne lui renvoie que la
        // suite, sans quoi le début serait transcrit deux fois.
        if (entry.localId) {
          const chunks = await audioStore.chunks(entry.localId);
          for (const row of chunks) {
            if (row.seq < entry.server.next_seq) continue;
            await postChunk(entry.server.session_id, row.seq, row.blob, dictationConfig.chunkSeconds * 1000);
          }
        }
        const result = await api(`/api/dictation/${entry.server.session_id}/finish`, { method: 'POST' });
        if (entry.localId) await bestEffort(() => audioStore.remove(entry.localId), 'nettoyage');
        await loadDraft(result.consultation_id);
        toast(T('recovery.resumed', { count: result.part_count }), 'success');
      } else {
        await uploadStoredSession(entry.localId);
      }
    } catch (err) {
      toast(T('recovery.resume_failed', { error: err.message }), 'error', 12000);
    } finally {
      setBusy(false);
      refreshRecoveryBanner();
    }
  }

  /**
   * Rejoue une dictée qui n'a jamais atteint le serveur : nouvelle session,
   * tous les fragments dans l'ordre, puis clôture.
   */
  async function uploadStoredSession(localId, options = {}) {
    const chunks = await audioStore.chunks(localId);
    if (!chunks.length) throw new Error(T('recovery.no_chunks'));

    const local = (await audioStore.listSessions()).find((row) => row.localId === localId);
    const consultationId = (local && local.consultationId) || await ensureConsultation();
    const tpl = currentTemplate();

    const session = await api('/api/dictation', {
      method: 'POST',
      body: {
        consultation_id: consultationId,
        template_id: tpl ? tpl.id : null,
        mime_type: (local && local.mimeType) || 'audio/webm',
      },
    });

    for (const row of chunks) {
      setBusy(true, T('recovery.uploading', { current: row.seq + 1, total: chunks.length }));
      await postChunk(session.session_id, row.seq, row.blob, dictationConfig.chunkSeconds * 1000);
    }

    setBusy(true, T('transcribe.busy_short'));
    const result = await api(`/api/dictation/${session.session_id}/finish`, { method: 'POST' });
    await bestEffort(() => audioStore.remove(localId), 'nettoyage');

    if (options.silent && result.consultation_id === state.consultationId) {
      // On est déjà sur la bonne consultation : on ajoute simplement le texte.
      dictation.appliedParts = 0;
      applyDictationParts(result);
    } else {
      await loadDraft(result.consultation_id);
    }
    return result;
  }

  /* =========================================================================
   * 5. MÉTADONNÉES, TRANSCRIPTION ET MISE EN FORME
   * ====================================================================== */

  /* -------------------------------------------------------------------------
   * Métadonnées de la consultation
   * ----------------------------------------------------------------------
   * Le médecin dicte déjà la raison de consultation au début de sa dictée :
   * ces champs sont donc relus dans la transcription par le serveur après la
   * mise en forme, et non saisis au clavier. Ils ne servent qu'à reconnaître
   * la consultation dans la liste des brouillons. L'identité du patient (nom,
   * numéro de dossier) n'est volontairement ni saisie ni collectée.
   *
   * Les clés sont celles de l'API.
   * ---------------------------------------------------------------------- */
  const META_ELEMENTS = {
    consultation_date: 'metaDate',
    reason: 'metaReason',
    requester: 'metaRequester',
    accompanied_by: 'metaAccompanied',
  };

  const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

  /** Métadonnées saisies, dans la forme attendue par /api/generate. */
  function readMetadata() {
    return {
      consultation_date: $('metaDate').value.trim(),
      reason: $('metaReason').value.trim(),
      requester: $('metaRequester').value.trim(),
      accompanied_by: $('metaAccompanied').value.trim(),
    };
  }

  /**
   * Reporte dans les champs les métadonnées renvoyées par le serveur.
   * Le serveur a déjà arbitré : ce qu'il renvoie tient compte de ce qui avait
   * été saisi manuellement, on peut donc écrire sans condition.
   */
  function applyMetadata(metadata) {
    if (!metadata) return false;
    let filled = false;

    Object.keys(META_ELEMENTS).forEach((key) => {
      const field = $(META_ELEMENTS[key]);
      const value = (metadata[key] || '').trim();
      if (!field || !value) return;
      // Un <input type="date"> rejette silencieusement toute valeur qui n'est
      // pas au format ISO : mieux vaut garder l'ancienne que la vider.
      if (field.type === 'date' && !ISO_DATE.test(value)) return;
      if (field.value !== value) filled = true;
      field.value = value;
    });

    return filled;
  }

  /** Vide tous les champs de métadonnées. */
  function clearMetadata() {
    Object.values(META_ELEMENTS).forEach((id) => { $(id).value = ''; });
    $('metaDate').value = new Date().toISOString().slice(0, 10);
  }

  /** Crée le brouillon en base au premier besoin, puis retourne son identifiant. */
  async function ensureConsultation() {
    if (state.consultationId) return state.consultationId;
    const tpl = currentTemplate();
    const created = await api('/api/consultations', {
      method: 'POST',
      body: {
        title: buildTitle(),
        reason: $('metaReason').value.trim(),
        template_id: tpl ? tpl.id : null,
        raw_transcript: $('transcript').value,
      },
    });
    state.consultationId = created.id;
    return state.consultationId;
  }

  function buildTitle() {
    const tpl = currentTemplate();
    const label = $('metaReason').value.trim() || (tpl ? tpl.name : '');
    return [label || T('drafts.default_title')]
      .join(' — ').slice(0, 300);
  }

  async function sendForTranscription(blob, filename) {
    const megabytes = (blob.size / 1048576).toFixed(1);

    // Même logique que generateNote()/runRetranscription() : un nouvel
    // import annule le précédent plutôt que de laisser les deux écrire dans
    // le même brouillon dans le désordre.
    if (pendingTranscribe) pendingTranscribe.abort();
    const controller = new AbortController();
    pendingTranscribe = controller;

    setBusy(true, T('transcribe.busy', { size: megabytes }));

    try {
      // Le brouillon existe avant l'envoi : si le navigateur se ferme pendant
      // la transcription, le serveur y écrit quand même le texte.
      const consultationId = await ensureConsultation();

      const form = new FormData();
      form.append('file', blob, filename);
      const tpl = currentTemplate();
      if (tpl) form.append('template_id', String(tpl.id));
      form.append('consultation_id', String(consultationId));

      const result = await api('/api/transcribe', {
        method: 'POST', signal: controller.signal, body: form,
      });

      // Supplantée par un import plus récent (voir le garde côté serveur) :
      // ce texte n'a jamais été écrit en base, on ne l'affiche donc pas non
      // plus — silencieusement, ce n'est pas un échec. L'audio, lui, a bien
      // été conservé (voir main.py) : loadRecordings() le montrera quand même.
      if (result.superseded) {
        loadRecordings();
        return;
      }

      const existing = $('transcript').value.trim();
      $('transcript').value = formatSentences(
        existing ? `${existing}\n\n${result.transcript}` : result.transcript,
      );

      if (result.stt_language) state.transcriptLanguage = result.stt_language;
      updateTranscriptMeta(result);
      loadRecordings();
      showTranscriptEngine(result.stt_used);
      flashElement('transcriptFooter');
      updateActionButtons();
      toast(
        T('transcribe.done', {
          count: result.transcript.length,
          confidence: (result.confidence * 100).toFixed(0),
        }),
        'success',
      );
    } catch (err) {
      if (err.name !== 'AbortError') toast(err.message, 'error', 10000);
    } finally {
      if (pendingTranscribe === controller) {
        pendingTranscribe = null;
        setBusy(false);
      }
    }
  }

  function updateTranscriptMeta(result) {
    const characters = $('transcript').value.length;
    const parts = [T('transcript.characters', { count: characters })];
    if (result && result.duration_seconds) {
      parts.push(T('transcript.audio', { duration: formatDuration(result.duration_seconds) }));
    }
    $('transcriptMeta').textContent = parts.join(' · ');
  }

  async function generateNote() {
    // Une dictée encore active (enregistrement ou pause) doit d'abord être
    // conclue : les dernières secondes — celles dites juste avant une pause —
    // attendent encore dans le tampon du MediaRecorder et manquent à la
    // transcription. Conclure d'abord, générer ensuite, comme si le médecin
    // avait appuyé sur « Terminer » avant « Mettre en forme ».
    if (state.recording || dictation.active) {
      if (!(await completeDictation())) return;
    }

    const transcript = $('transcript').value.trim();
    const tpl = currentTemplate();

    if (!transcript && !audioOnlyReady()) {
      toast(T('generate.empty'), 'warning');
      return;
    }
    if (!tpl) {
      toast(T('generate.no_template'), 'warning');
      return;
    }

    // Régénérer remplace toujours la note affichée par la nouvelle — on ne
    // prévient que s'il y a réellement une modification à perdre, pour ne
    // pas interrompre le cas courant (première génération, ou régénération
    // sans qu'on ait touché au texte depuis).
    if (
      state.lastGeneratedMarkdown
      && $('markdownEditor').value.trim() !== state.lastGeneratedMarkdown.trim()
      && !window.confirm(T('generate.confirm_overwrite'))
    ) {
      return;
    }

    // Un clic pendant qu'une génération tourne déjà annule la précédente :
    // sur un point de terminaison lent (auto-hébergé), les deux finiraient
    // sinon par se terminer l'une après l'autre, la seconde écrasant
    // silencieusement le résultat de la première sans qu'on sache laquelle
    // est réellement affichée (voir le garde-fou serveur, qui rejette de
    // toute façon toute réponse devenue périmée entre-temps).
    if (pendingGenerate) pendingGenerate.abort();
    const controller = new AbortController();
    pendingGenerate = controller;

    // Une régénération peut écraser une note déjà en place : on conserve
    // l'ancien texte pour pouvoir le restituer si la nouvelle échoue en
    // plein flux, et on ouvre un jeton que le serveur répétera dans chaque
    // morceau diffusé en direct — seuls les morceaux portant CE jeton seront
    // appliqués (voir onGenerationChunk).
    state.preGenerateMarkdown = $('markdownEditor').value;
    state.generationToken = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random());

    // Le voile plein écran laisserait le texte généré invisible : à la place
    // on force la vue « Aperçu » (où le texte défilera en direct) et on pose
    // un témoin discret — pastille sur le panneau de transcription sur grand
    // écran, barre horizontale dans l'aperçu sur mobile.
    setGenerating(true);
    showPreview();
    setMobilePane('note');

    try {
      const consultationId = await ensureConsultation();
      const result = await api('/api/generate', {
        method: 'POST',
        signal: controller.signal,
        body: Object.assign({
          template_id: tpl.id,
          transcript,
          consultation_id: consultationId,
          extra_instructions: $('ctxExtra').value.trim(),
          use_pro: false,
          generation_token: state.generationToken,
        }, readMetadata()),
      });

      // Supplantée par un AUTRE onglet (un onglet ne se supplante jamais
      // lui-même : son clic annule sa propre requête, AbortError). Le serveur
      // n'a rien persisté : on ne touche à rien — l'écran garde le texte déjà
      // affiché, et l'évènement ``generated`` de l'onglet gagnant
      // resynchronisera ce brouillon dans un instant.
      if (result.superseded) return;
      state.consultationId = result.consultation_id;
      state.lastGeneratedMarkdown = result.markdown;
      $('markdownEditor').value = result.markdown;
      renderMarkdown();
      showPreview();
      // Sur mobile, on amène l'usager directement au résultat.
      setMobilePane('note');

      // Les métadonnées viennent d'être relues dans la dictée : on les
      // affiche, et on déplie la section pour que le médecin puisse les
      // vérifier d'un coup d'œil plutôt que de les découvrir plus tard dans
      // la liste des brouillons.
      showNoteEngines(result.stt_used, result.llm_used, result.audio_used);
      showDebugInfo({
        llm: result.llm_used,
        stt: result.stt_used,
        audioUsed: result.audio_used,
        promptTokens: result.usage && result.usage.prompt_tokens,
        outputTokens: result.usage && result.usage.output_tokens,
        elapsedSeconds: result.elapsed_seconds,
        truncated: result.truncated,
      });
      flashElement('noteFooter');
      updateActionButtons();
      if (applyMetadata(result.metadata)) {
        $('metaDetails').open = true;
      }

      if (result.truncated) {
        toast(T('generate.truncated'), 'warning', 10000);
      } else {
        toast(T('generate.done', { model: result.model }), 'success');
      }
      scheduleSave();
    } catch (err) {
      // Une annulation volontaire (supplantée par un clic plus récent) ne
      // doit produire ni toast d'erreur ni changement d'état : c'est
      // exactement le comportement voulu, pas une panne. Sur toute AUTRE
      // erreur, le flux a peut-être déjà rempli l'éditeur de texte partiel :
      // on restitue la note qui s'y trouvait avant cette tentative.
      if (err.name !== 'AbortError') {
        $('markdownEditor').value = state.preGenerateMarkdown;
        renderMarkdown();
        showPreview();
        toast(err.message, 'error', 10000);
      }
    } finally {
      // Les deux lignes ci-dessous ne s'exécutent que si RIEN de plus récent
      // n'a pris la relève — sinon le ``finally`` de CET appel annulé,
      // exécuté après coup, effacerait la référence du nouvel appel en cours
      // ET masquerait son témoin alors qu'il tourne toujours.
      if (pendingGenerate === controller) {
        pendingGenerate = null;
        state.generationToken = null;
        setGenerating(false);
      }
    }
  }

  /* =========================================================================
   * 6. RENDU MARKDOWN ET SAUVEGARDE AUTOMATIQUE
   * ====================================================================== */

  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true });
  }

  /** Convertit le Markdown en HTML assaini (protection contre le XSS). */
  function markdownToHtml(markdown) {
    const rawHtml = window.marked ? marked.parse(markdown || '') : esc(markdown);
    return window.DOMPurify ? DOMPurify.sanitize(rawHtml) : rawHtml;
  }

  /* -------------------------------------------------------------------------
   * Champs Markdown (consigne générale, gabarits)
   * ----------------------------------------------------------------------
   * Ces champs contiennent du Markdown que le modèle reproduira à la lettre :
   * une altération silencieuse du texte s'y paie en note mal structurée.
   *
   * Le vrai danger n'est pas l'absence d'aperçu, c'est la correction
   * automatique. Sur l'iPad, iOS réécrit la ponctuation d'un champ de texte :
   * les guillemets droits deviennent typographiques, « -- » devient un tiret
   * cadratin, et la lettre qui suit un « # » est mise en majuscule. Chacune de
   * ces « corrections » casse la syntaxe — et rien à l'écran ne le signale.
   * ---------------------------------------------------------------------- */

  /** Neutralise tout ce qui réécrit le texte à l'insu de l'usager. */
  function disableTextRewriting(field) {
    if (!field) return;
    field.setAttribute('spellcheck', 'false');
    field.setAttribute('autocorrect', 'off');
    field.setAttribute('autocapitalize', 'off');
    field.setAttribute('autocomplete', 'off');
    field.setAttribute('data-gramm', 'false');        // Grammarly
    field.setAttribute('data-enable-grammarly', 'false');
  }

  const MARKDOWN_PREVIEW_CLASS =
    'hidden mt-1 rounded-lg border border-slate-200 bg-slate-50 p-3 max-h-96 overflow-auto '
    + 'thin-scroll prose prose-slate prose-sm max-w-none prose-headings:font-semibold '
    + 'prose-h1:text-base prose-h2:text-sm prose-h2:uppercase prose-h2:tracking-wide '
    + 'prose-h2:border-b prose-h2:border-slate-200 prose-h2:pb-1 prose-h3:text-xs '
    + 'prose-table:text-xs';

  /** Ajoute au champ la protection ci-dessus et un aperçu commutable. */
  function enableMarkdownEditing(textarea) {
    if (!textarea || textarea.dataset.markdownReady) return;
    textarea.dataset.markdownReady = '1';
    disableTextRewriting(textarea);
    textarea.style.tabSize = '2';

    const bar = document.createElement('div');
    bar.className = 'flex items-center gap-2 mt-1';

    const toggle = document.createElement('button');
    toggle.type = 'button';   // sans quoi il soumettrait le formulaire du gabarit
    toggle.className = 'px-2 py-0.5 rounded border border-slate-300 text-[11px] '
                     + 'text-slate-600 hover:bg-slate-50 transition';
    toggle.textContent = T('note.preview');

    const hint = document.createElement('span');
    hint.className = 'text-[11px] text-slate-400';
    hint.textContent = T('markdown.hint');

    bar.append(toggle, hint);
    const preview = document.createElement('div');
    preview.className = MARKDOWN_PREVIEW_CLASS;
    textarea.insertAdjacentElement('afterend', preview);
    textarea.insertAdjacentElement('afterend', bar);

    toggle.addEventListener('click', () => {
      const versApercu = preview.classList.contains('hidden');
      // Un champ obligatoire masqué et vide fait échouer la validation du
      // navigateur sur un élément qu'il ne peut pas mettre en évidence :
      // on refuse simplement de basculer.
      if (versApercu && !textarea.value.trim()) {
        hint.textContent = T('markdown.nothing_to_preview');
        setTimeout(() => {
          hint.textContent = T('markdown.hint');
        }, 2500);
        return;
      }
      if (versApercu) {
        preview.innerHTML = markdownToHtml(textarea.value);
        // L'aperçu reprend la hauteur du champ : sans cela la page sautait
        // sous le curseur à chaque bascule.
        preview.style.minHeight = `${textarea.offsetHeight}px`;
      }
      preview.classList.toggle('hidden', !versApercu);
      textarea.classList.toggle('hidden', versApercu);
      toggle.textContent = versApercu ? T('note.write') : T('note.preview');
    });
  }

  /**
   * Ramène un champ en mode écriture.
   *
   * Nécessaire avant de recharger un gabarit : sans cela, l'aperçu resterait
   * affiché avec le contenu de l'ancien gabarit tandis que l'éditeur, masqué,
   * porterait déjà le nouveau.
   */
  function exitMarkdownPreview(textarea) {
    if (!textarea || !textarea.dataset.markdownReady) return;
    const bar = textarea.nextElementSibling;
    const preview = bar && bar.nextElementSibling;
    if (!preview || preview.classList.contains('hidden')) return;
    preview.classList.add('hidden');
    textarea.classList.remove('hidden');
    const bouton = bar.querySelector('button');
    if (bouton) bouton.textContent = T('note.preview');
  }

  /**
   * Affiche les moteurs qui ont produit le document affiché.
   *
   * Volontairement distinct de l'indicateur de l'en-tête d'application, qui
   * montre la configuration courante : les deux diffèrent dès qu'un réglage
   * a changé depuis la génération, et c'est précisément l'écart qu'on veut
   * pouvoir constater.
   */
  /**
   * Nom court à afficher pour un « fournisseur / modèle » (ex. « custom /
   * gemma4-26b-a4b-grounded »). Le nom du FOURNISSEUR d'ordinaire — il
   * suffit à identifier le service (« gemini », « assemblyai »). Exception
   * pour « custom » : ce nom-là ne distingue rien, tous les points de
   * terminaison personnalisés le partagent, donc c'est le modèle qui
   * identifie réellement ce qui a tourné.
   */
  function shortEngineName(providerModel) {
    const parts = (providerModel || '').split(' / ');
    const provider = parts[0] || '';
    if (provider.toLowerCase() !== 'custom') return provider;
    return parts.slice(1).join(' / ') || provider;
  }

  function showNoteEngines(stt, llm, audioUsed) {
    const el = $('noteEngines');
    if (!el) return;
    const parts = [];
    if (stt) parts.push(T('note.engine_dictation', { engine: shortEngineName(stt) }));
    if (llm) {
      let note = T('note.engine_note', { engine: shortEngineName(llm) });
      if (audioUsed) note += ` ${T('note.engine_audio')}`;
      parts.push(note);
    }
    el.textContent = parts.join(' · ');
    el.title = [
      stt ? T('note.engine_stt_title', { engine: stt }) : '',
      llm ? T('note.engine_llm_title', { engine: llm }) : '',
      audioUsed ? T('note.engine_audio_title') : '',
    ].filter(Boolean).join('\n');
    el.classList.toggle('hidden', !parts.length);
    refreshNoteFooter();
  }

  /**
   * STT contourné (audio envoyé seul au modèle, Gemini ou Qwen Omni) : dans
   * ce mode l'audio conservé suffit à lui seul à activer « Générer », qu'une
   * transcription soit affichée ou non — la conserver (voir
   * llmBypassSttKeepTranscript) ne sert qu'à l'affichage pendant la dictée,
   * jamais de source pour la génération (voir llm.generate_note, audio_only)
   * — voir refreshClientConfig(), qui alimente les deux réglages, et les
   * trois appelants ci-dessous (bouton, garde-fou de generateNote(), fin de
   * dictée automatique).
   */
  function audioOnlyReady() {
    const hasAudio = Boolean(state.consultationId) && state.recordingsCount > 0;
    return state.llmBypassStt && hasAudio;
  }

  /**
   * Grise (plutôt que masque) « Retranscrire » et « Mettre en forme » quand
   * l'action n'a rien à faire — pas de transcription, pas de gabarit choisi,
   * pas d'audio conservé. Appelée à chaque événement qui peut changer l'une
   * de ces trois conditions : chargement d'un brouillon, nouvelle
   * consultation, réception de texte (dictée, import, retranscription),
   * chargement des enregistrements, changement de gabarit.
   */
  function updateActionButtons() {
    const hasTranscript = Boolean($('transcript').value.trim());
    const hasTemplate = Boolean(currentTemplate());
    const hasAudio = Boolean(state.consultationId) && state.recordingsCount > 0;

    [$('btnGenerate'), $('btnGenerateMobile')].forEach((el) => {
      if (el) el.disabled = !((hasTranscript || audioOnlyReady()) && hasTemplate);
    });
    [$('btnRetranscribe'), $('btnRetranscribeMobile')].forEach((el) => {
      if (el) el.disabled = !hasAudio;
    });
  }

  /**
   * Informations RÉELLES sur la dernière génération — jamais celles que le
   * modèle prétendrait fournir dans le corps de la note (voir la consigne
   * générale : un modèle n'a aucun accès à son propre décompte de jetons, et
   * ce qu'il en dirait serait inventé). Jetons et durée sont conservés en
   * base (``usage_prompt_tokens``/``usage_output_tokens``/
   * ``generation_seconds``) depuis la version 1.3.1 : un brouillon généré
   * avant ce champ les montre comme indisponibles plutôt que d'afficher un 0
   * qui laisserait croire à une vraie mesure.
   */
  function showDebugInfo(info) {
    const wrap = $('debugInfo');
    const list = $('debugInfoList');
    if (!wrap || !list) return;
    info = info || {};
    const lines = [];
    if (info.llm) lines.push(T('debug.llm', { value: info.llm }));
    if (info.stt) lines.push(T('debug.stt', { value: info.stt }));
    if (info.audioUsed) lines.push(T('debug.audio'));
    if (info.promptTokens != null || info.outputTokens != null) {
      lines.push(T('debug.tokens', {
        in_tokens: info.promptTokens != null ? info.promptTokens : '?',
        out_tokens: info.outputTokens != null ? info.outputTokens : '?',
      }));
    } else if (info.llm) {
      lines.push(T('debug.tokens_unavailable'));
    }
    if (info.elapsedSeconds != null) {
      lines.push(T('debug.duration', { seconds: info.elapsedSeconds.toFixed(1) }));
    }
    if (info.truncated) lines.push(T('debug.truncated'));
    list.innerHTML = lines.map((line) => `<li>${esc(line)}</li>`).join('');
    wrap.classList.toggle('hidden', !lines.length);
  }

  /**
   * Le pied de la note n'existe que s'il a quelque chose à dire : sans cela son
   * filet supérieur laisserait une bande vide sous le texte. Ses deux occupants
   * apparaissent et disparaissent indépendamment — les moteurs à la génération
   * ou à l'ouverture d'un brouillon, l'état de sauvegarde au fil de la frappe —
   * d'où ce point unique consulté par les deux.
   */
  function refreshNoteFooter() {
    const pied = $('noteFooter');
    if (!pied) return;
    const moteurs = $('noteEngines');
    const etat = $('saveStatus');
    const visible = (moteurs && !moteurs.classList.contains('hidden') && moteurs.textContent.trim())
                 || (etat && etat.textContent.trim());
    pied.classList.toggle('hidden', !visible);
  }

  /**
   * Même rôle que showNoteEngines(), côté dictée : quel service a RÉELLEMENT
   * produit la transcription affichée. Un seul moteur ici (pas de LLM), d'où
   * une fonction plus courte plutôt qu'un paramètre optionnel sur l'originale.
   */
  function showTranscriptEngine(sttUsed) {
    const el = $('transcriptEngine');
    if (!el) return;
    if (sttUsed) {
      el.textContent = T('note.engine_dictation', { engine: shortEngineName(sttUsed) });
      el.title = T('note.engine_stt_title', { engine: sttUsed });
    } else {
      el.textContent = '';
      el.title = '';
    }
    el.classList.toggle('hidden', !sttUsed);
    refreshTranscriptFooter();
  }

  /** Pendant de refreshNoteFooter() pour le pied du panneau de dictée. */
  function refreshTranscriptFooter() {
    const pied = $('transcriptFooter');
    if (!pied) return;
    const moteur = $('transcriptEngine');
    const etat = $('saveStatusDictee');
    const avis = $('transcriptBypassNotice');
    const visible = (moteur && !moteur.classList.contains('hidden') && moteur.textContent.trim())
                 || (etat && etat.textContent.trim())
                 || (avis && !avis.classList.contains('hidden'));
    pied.classList.toggle('hidden', !visible);
  }

  /**
   * Rappelle, dans le pied de la dictée, que le texte affiché ne sert PAS à
   * la génération quand le STT tourne pour l'affichage seul (contournement
   * actif ET transcription conservée — voir refreshClientConfig()). Sans ce
   * rappel, rien ne distingue ce texte-ci d'une transcription qui, elle,
   * alimente réellement la note — visible dès le début de la dictée, pas
   * seulement une fois celle-ci terminée.
   */
  function updateBypassSttNotice() {
    const el = $('transcriptBypassNotice');
    if (!el) return;
    const show = state.llmBypassStt && state.llmBypassSttKeepTranscript;
    el.textContent = show ? T('transcript.bypass_notice') : '';
    el.title = show ? T('transcript.bypass_notice_title') : '';
    el.classList.toggle('hidden', !show);
    refreshTranscriptFooter();
  }

  /**
   * Confirmation visuelle qu'un résultat VIENT d'arriver (note générée,
   * transcription reçue), sans dépendre d'une relecture du texte : un vert
   * désaturé plein, qui s'efface tout seul en 10 s. Même mécanique que
   * ``toast()`` (style en ligne + minuterie) plutôt qu'une classe Tailwind —
   * la durée voulue (10 000 ms) n'a pas d'utilitaire tout fait.
   */
  function flashElement(id) {
    const el = $(id);
    if (!el) return;
    el.style.transition = 'none';
    el.style.backgroundColor = accentColor('--color-accent-bg-subtle') || '#f0fdfa';
    void el.offsetWidth; // force le navigateur à appliquer la couleur ci-dessus avant la transition
    el.style.transition = 'background-color 10000ms ease-out';
    el.style.backgroundColor = '';
    setTimeout(() => { el.style.transition = ''; }, 10000);
  }

  /**
   * Seule écriture de l'état de sauvegarde : passer par ici garantit que le
   * pied s'ouvre et se referme avec lui.
   */
  function setSaveStatus(texte) {
    const el = $('saveStatus');
    if (el) el.textContent = texte || '';
    refreshNoteFooter();
    const elDictee = $('saveStatusDictee');
    if (elDictee) elDictee.textContent = texte || '';
    refreshTranscriptFooter();
  }

  function renderMarkdown() {
    const markdown = $('markdownEditor').value;
    const pane = $('previewPane');
    if (!markdown.trim()) {
      pane.innerHTML = `<p class="text-slate-400 italic">${esc(T('note.empty'))}</p>`;
    } else {
      pane.innerHTML = markdownToHtml(markdown);
    }
    // #genBar vit DANS l'aperçu (positionnement absolu dans son défilement)
    // mais ``innerHTML`` l'efface à chaque rendu : on le remet en place.
    const bar = $('genBar');
    if (!pane.contains(bar)) pane.appendChild(bar);
  }

  /**
   * Bascule entre « Dictée » et « Note structurée » sur petit écran.
   * Sur grand écran, la classe max-lg:hidden n'a aucun effet : les deux
   * panneaux restent côte à côte quel que soit l'onglet sélectionné.
   */
  function setMobilePane(name) {
    state.mobilePane = name;

    $('paneDictee').classList.toggle('max-lg:hidden', name !== 'dictee');
    $('paneNote').classList.toggle('max-lg:hidden', name !== 'note');
    // Retranscrire n'a de sens que devant la transcription — masqué sur
    // l'onglet Note plutôt que gardé grisé, ce serait un bouton de plus sans
    // rien à y faire. Mettre en forme, lui, reste joignable des deux côtés.
    $('btnRetranscribeMobile').classList.toggle('hidden', name !== 'dictee');

    const activeClasses = 'py-2 rounded-md text-sm font-medium bg-white shadow-sm text-slate-800';
    const idleClasses = 'py-2 rounded-md text-sm font-medium text-slate-500';
    $('paneTabDictee').className = name === 'dictee' ? activeClasses : idleClasses;
    $('paneTabNote').className = name === 'note' ? activeClasses : idleClasses;
  }

  function showPreview() {
    state.editingMarkdown = false;
    $('previewPane').classList.remove('hidden');
    $('markdownEditor').classList.add('hidden');
    $('tabPreview').className = 'px-3 py-1.5 accent-tab font-medium';
    $('tabEdit').className = 'px-3 py-1.5 hover:bg-slate-50 text-slate-600';
    renderMarkdown();
  }

  function showEditor() {
    state.editingMarkdown = true;
    $('previewPane').classList.add('hidden');
    $('markdownEditor').classList.remove('hidden');
    $('tabEdit').className = 'px-3 py-1.5 accent-tab font-medium';
    $('tabPreview').className = 'px-3 py-1.5 hover:bg-slate-50 text-slate-600';
    $('markdownEditor').focus();
  }

  /**
   * Sauvegarde automatique.
   * Déclenchée 1,5 s après la dernière frappe. Aucun envoi si rien n'a changé
   * depuis la dernière sauvegarde réussie.
   */
  /** Empreinte de ce qui doit déclencher une sauvegarde. */
  function workspaceSnapshot() {
    return JSON.stringify({
      transcript: $('transcript').value,
      markdown: $('markdownEditor').value,
      metadata: readMetadata(),
    });
  }

  const scheduleSave = debounce(async function saveDraft() {
    const snapshot = workspaceSnapshot();
    if (snapshot === state.lastSavedSnapshot) return;

    // Rien à sauvegarder tant qu'aucun contenu n'existe.
    if (!$('transcript').value.trim() && !$('markdownEditor').value.trim()) return;

    setSaveStatus(T('save.saving'));
    try {
      const consultationId = await ensureConsultation();
      const tpl = currentTemplate();
      const body = Object.assign({
        title: buildTitle(),
        template_id: tpl ? tpl.id : null,
        raw_transcript: $('transcript').value,
        edited_markdown: $('markdownEditor').value,
        audio_seconds: state.recordedSeconds,
      }, readMetadata());

      // Pendant une dictée, c'est le serveur qui écrit la transcription,
      // tranche par tranche. La renvoyer depuis l'écran ferait perdre celles
      // arrivées entre le dernier rafraîchissement et cette sauvegarde.
      if (dictation.active) delete body.raw_transcript;

      await api(`/api/consultations/${consultationId}`, { method: 'PATCH', body });
      state.lastSavedSnapshot = snapshot;
      setSaveStatus(T('save.saved_at', {
        time: new Date().toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' }),
      }));
    } catch (err) {
      setSaveStatus(T('save.failed'));
      console.error(err);
    }
  }, 1500);

  /* =========================================================================
   * 7. EXPORT
   * ====================================================================== */

  /**
   * Copie avec mise en forme : dépose simultanément une version HTML et une
   * version texte dans le presse-papiers. Coller dans Word ou dans un DME
   * conserve les titres et les listes ; coller dans un champ texte brut
   * donne le Markdown.
   */
  async function copyRichText() {
    const markdown = $('markdownEditor').value;
    if (!markdown.trim()) {
      toast(T('copy.nothing'), 'warning');
      return;
    }

    const html = `<div style="font-family:Georgia,'Times New Roman',serif;font-size:11pt;line-height:1.5">
                    ${markdownToHtml(markdown)}
                  </div>`;

    try {
      if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
        await navigator.clipboard.write([
          new ClipboardItem({
            'text/html': new Blob([html], { type: 'text/html' }),
            // Version texte : la structure par la seule disposition, jamais
            // du Markdown brut — c'est ce que reçoit un champ qui refuse
            // le HTML, et « ## » en clair n'y aide personne.
            'text/plain': new Blob([markdownToPlainText(markdown)], { type: 'text/plain' }),
          }),
        ]);
      } else {
        // Repli (Firefox ancien, Safari ancien) : sélection d'un élément
        // temporaire puis execCommand.
        const holder = document.createElement('div');
        holder.innerHTML = html;
        holder.setAttribute('contenteditable', 'true');
        holder.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
        document.body.appendChild(holder);
        const range = document.createRange();
        range.selectNodeContents(holder);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('copy');
        selection.removeAllRanges();
        holder.remove();
      }
      toast(T('copy.rich_done'), 'success');
    } catch (err) {
      toast(T('copy.failed', { error: err.message }), 'error');
    }
  }

  /* -------------------------------------------------------------------------
   * Markdown → texte simple
   * ----------------------------------------------------------------------
   * Le DME de l'hôpital n'interprète pas le Markdown : y coller la note telle
   * quelle y laisse des « ## » et des « ** » en clair. On produit donc une
   * version dont la structure tient à la seule disposition du texte.
   *
   * La conversion est faite ici, dans le navigateur, à partir du Markdown déjà
   * généré : elle ne coûte aucun appel au modèle et donc aucun jeton.
   *
   * ASCII pur, volontairement : un champ de DME hérité peut ne pas accepter
   * les caractères de filet Unicode, et un « ═ » remplacé par « ? » serait
   * pire que le tiret qu'il remplace.
   * ---------------------------------------------------------------------- */

  /** Retire les marqueurs de style d'une ligne, en gardant le texte. */
  function stripInlineMarkdown(text) {
    return text
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')      // images
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1 ($2)') // liens : garder l'URL
      .replace(/(\*\*|__)(.*?)\1/g, '$2')             // gras
      .replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s.,;:!?)]|$)/g, '$1$2')  // italique
      .replace(/~~(.*?)~~/g, '$1')
      .replace(/`([^`]*)`/g, '$1')
      .trimEnd();
  }

  /** Une ligne de tableau Markdown découpée en cellules. */
  function splitTableRow(line) {
    return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '')
      .split('|').map((c) => stripInlineMarkdown(c.trim()));
  }

  const TABLE_SEPARATOR = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

  /** Rend un tableau en colonnes alignées par des espaces. */
  function renderPlainTable(rows) {
    if (!rows.length) return [];
    const columns = Math.max(...rows.map((r) => r.length));
    const widths = [];
    for (let c = 0; c < columns; c += 1) {
      widths.push(Math.max(...rows.map((r) => (r[c] || '').length)));
    }
    const ligne = (cells) => cells
      .map((cell, c) => (cell || '').padEnd(c === columns - 1 ? 0 : widths[c]))
      .join('  ').trimEnd();

    const out = [ligne(rows[0]), widths.map((w) => '-'.repeat(w)).join('  ').trimEnd()];
    rows.slice(1).forEach((r) => out.push(ligne(r)));
    return out;
  }

  function markdownToPlainText(markdown) {
    const lignes = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let tableau = [];

    const viderTableau = () => {
      if (tableau.length) {
        out.push(...renderPlainTable(tableau));
        tableau = [];
      }
    };

    lignes.forEach((brute) => {
      const ligne = brute.replace(/\s+$/, '');

      // --- Tableaux : accumulés puis alignés d'un bloc ---
      if (/^\s*\|/.test(ligne)) {
        if (!TABLE_SEPARATOR.test(ligne)) tableau.push(splitTableRow(ligne));
        return;
      }
      viderTableau();

      const titre = ligne.match(/^(#{1,6})\s+(.*)$/);
      if (titre) {
        const niveau = titre[1].length;
        const texte = stripInlineMarkdown(titre[2]).replace(/[:\s]+$/, '');
        if (out.length) out.push('');
        if (niveau === 1) {
          out.push(texte.toUpperCase(), '='.repeat(Math.max(texte.length, 3)));
        } else if (niveau === 2) {
          out.push(texte.toUpperCase(), '-'.repeat(Math.max(texte.length, 3)));
        } else {
          // Au-delà du deuxième niveau, un filet de plus nuirait à la
          // lisibilité : la position et le deux-points suffisent.
          out.push(`${texte} :`);
        }
        return;
      }

      // --- Filet horizontal ---
      if (/^\s*([-*_])\1{2,}\s*$/.test(ligne)) {
        out.push('', '-'.repeat(60), '');
        return;
      }

      // --- Listes ---
      const puce = ligne.match(/^(\s*)[-*+]\s+(.*)$/);
      if (puce) {
        const creux = '  '.repeat(Math.floor(puce[1].length / 2));
        out.push(`${creux}- ${stripInlineMarkdown(puce[2])}`);
        return;
      }
      const numero = ligne.match(/^(\s*)(\d+)[.)]\s+(.*)$/);
      if (numero) {
        const creux = '  '.repeat(Math.floor(numero[1].length / 2));
        out.push(`${creux}${numero[2]}. ${stripInlineMarkdown(numero[3])}`);
        return;
      }

      // --- Citation ---
      const citation = ligne.match(/^\s*>\s?(.*)$/);
      if (citation) {
        out.push(`  | ${stripInlineMarkdown(citation[1])}`);
        return;
      }

      out.push(stripInlineMarkdown(ligne));
    });

    viderTableau();

    // Deux sauts de ligne consécutifs au maximum : au-delà, le DME étire la
    // note sur des écrans inutiles.
    return out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/^\n+|\s+$/g, '') + '\n';
  }

  async function copyPlainText() {
    const markdown = $('markdownEditor').value;
    if (!markdown.trim()) {
      toast(T('copy.nothing'), 'warning');
      return;
    }
    try {
      await navigator.clipboard.writeText(markdownToPlainText(markdown));
      toast(T('copy.plain_done'), 'success');
    } catch (err) {
      toast(T('copy.failed', { error: err.message }), 'error');
    }
  }

  async function copyMarkdown() {
    const markdown = $('markdownEditor').value;
    if (!markdown.trim()) {
      toast(T('copy.nothing'), 'warning');
      return;
    }
    try {
      await navigator.clipboard.writeText(markdown);
      toast(T('copy.markdown_done'), 'success');
    } catch (err) {
      toast(T('copy.failed', { error: err.message }), 'error');
    }
  }

  /**
   * Export PDF via l'impression du navigateur.
   * Aucune bibliothèque n'est nécessaire : la feuille de style @media print
   * d'index.html produit un document propre, et « Enregistrer au format PDF »
   * est disponible sur toutes les plateformes (macOS, iOS, Windows, Android).
   */
  function exportPdf() {
    const markdown = $('markdownEditor').value;
    if (!markdown.trim()) {
      toast(T('pdf.nothing'), 'warning');
      return;
    }

    const printedOn = new Date().toLocaleString(LOCALE);
    const footer = `<div class="print-footer">
        ${esc(T('pdf.footer'))}
        ${esc(T('pdf.footer_printed', { date: printedOn }))}
      </div>`;

    $('printArea').innerHTML = markdownToHtml(markdown) + footer;
    window.print();
  }

  /* =========================================================================
   * 8. MODALES
   * ====================================================================== */

  /* ---- Gestion des gabarits --------------------------------------------- */

  /**
   * Sur petit écran, la liste et le formulaire ne tiennent pas côte à côte :
   * on navigue de l'un à l'autre. Sur grand écran, les classes max-lg:*
   * n'ont aucun effet et les deux colonnes restent affichées.
   */
  function setTemplateMobileView(view) {
    const showingForm = view === 'form';
    $('templateListPane').classList.toggle('max-lg:hidden', showingForm);
    $('templateForm').classList.toggle('max-lg:hidden', !showingForm);
    $('btnBackToTemplateList').classList.toggle('hidden', !showingForm);
    $('templateListHint').classList.toggle('hidden', showingForm);
  }

  function openTemplatesModal() {
    $('templatesModal').classList.remove('hidden');
    renderTemplateList();
    const selected = currentTemplate();
    if (selected) fillTemplateForm(selected);
    else resetTemplateForm();
    // On ouvre toujours sur la liste : c'est le point d'entrée naturel.
    setTemplateMobileView('list');
  }

  function closeTemplatesModal() {
    $('templatesModal').classList.add('hidden');
  }

  function renderTemplateList() {
    const list = $('templateList');
    list.innerHTML = '';

    state.templates.forEach((tpl) => {
      const item = document.createElement('li');
      const active = String(tpl.id) === $('tplId').value;
      item.className = `px-4 py-3 cursor-pointer hover:bg-white transition ${active ? 'bg-white border-l-4 accent-border' : ''}`;
      // La langue est affichée pour CHAQUE gabarit : c'est elle qui décide de
      // la langue de la note produite, et s'en apercevoir après coup coûte une
      // régénération.
      const langue = (tpl.language || 'fr').toUpperCase();
      item.innerHTML = `
        <div class="font-medium text-sm text-slate-800 flex items-center gap-1.5">
          ${tpl.is_locked ? `<svg class="w-3 h-3 text-amber-600 shrink-0" viewBox="0 0 24 24"
               fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
               <rect x="4" y="11" width="16" height="10" rx="2"/>
               <path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>` : ''}
          <span class="truncate">${esc(tpl.name)}</span>
        </div>
        <div class="text-xs text-slate-500 mt-0.5 line-clamp-2">${esc(tpl.description || T('tpl.no_description'))}</div>
        <div class="mt-1.5 flex items-center gap-1 flex-wrap">
          <span class="text-[10px] px-1.5 py-0.5 rounded accent-badge font-medium">${esc(langue)}</span>
          ${tpl.owner === state.username
            ? `<span class="text-[10px] px-1.5 py-0.5 rounded accent-badge">${esc(T('tpl.personal_badge'))}</span>`
            : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">${esc(T('tpl.shared_badge'))}</span>`}
          ${tpl.is_locked ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">${esc(T('tpl.locked_badge'))}</span>` : ''}
          ${tpl.is_default && !tpl.is_locked ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">${esc(T('tpl.preloaded'))}</span>` : ''}
        </div>
      `;
      item.addEventListener('click', () => {
        fillTemplateForm(tpl);
        renderTemplateList();
        setTemplateMobileView('form');
      });
      list.appendChild(item);
    });

    if (!state.templates.length) {
      list.innerHTML = `<li class="px-4 py-6 text-sm text-slate-400 text-center">${esc(T('tpl.none'))}</li>`;
    }
  }

  /**
   * Rend le formulaire inerte pour le gabarit ouvert.
   *
   * Un gabarit n'est éditable que s'il n'est pas protégé ET que l'utilisateur
   * a le droit de le réécrire : administrateur pour un gabarit partagé,
   * propriétaire pour un gabarit personnel. Dans le cas contraire les champs
   * restent LISIBLES — on veut pouvoir consulter le gabarit avant de décider
   * de le dupliquer — mais ne s'enregistrent pas. Le serveur refuse de toute
   * façon : ceci n'est que la politesse qui évite de saisir pour rien.
   */
  function applyTemplateLock(locked, editable) {
    const readOnly = Boolean(locked) || !editable;
    const champs = ['tplName', 'tplDescription', 'tplInstructions', 'tplLayout',
                    'tplHints', 'tplOrder', 'tplLanguage'];
    champs.forEach((id) => {
      const el = $(id);
      if (el) el.disabled = readOnly;
    });
    $('tplLockedBanner').classList.toggle('hidden', !locked);
    $('tplReadonlyBanner').classList.toggle('hidden', locked || editable);
    // « Enregistrer » et « Supprimer » n'ont pas de sens ici ; « Dupliquer »
    // est au contraire l'action à mettre en avant.
    const submit = $('templateForm').querySelector('button[type="submit"]');
    if (submit) submit.classList.toggle('hidden', readOnly);
    $('btnDeleteTemplate').classList.toggle('hidden', readOnly);
  }

  function fillTemplateForm(tpl) {
    $('tplId').value = tpl.id;
    $('tplName').value = tpl.name;
    [$('tplInstructions'), $('tplLayout')].forEach(exitMarkdownPreview);
    $('tplDescription').value = tpl.description || '';
    $('tplInstructions').value = tpl.system_instructions || '';
    $('tplLayout').value = tpl.layout_format || '';
    $('tplHints').value = tpl.phrase_hints || '';
    $('tplOrder').value = tpl.sort_order != null ? tpl.sort_order : 100;
    $('tplLanguage').value = tpl.language || 'fr';
    // Supprimer et dupliquer n'ont de sens que sur un gabarit déjà en base.
    $('btnDeleteTemplate').classList.remove('hidden');
    $('btnDuplicateTemplate').classList.remove('hidden');
    // Un gabarit personnel ne se réécrit que par son propriétaire ; un gabarit
    // partagé n'est réécrit que par un administrateur de gabarits.
    const isOwn = Boolean(tpl.owner) && tpl.owner === state.username;
    applyTemplateLock(tpl.is_locked, state.isTemplateAdmin || isOwn);
    $('templateFormStatus').textContent = '';
  }

  function resetTemplateForm() {
    $('tplId').value = '';
    $('tplName').value = '';
    [$('tplInstructions'), $('tplLayout')].forEach(exitMarkdownPreview);
    $('tplDescription').value = '';
    $('tplInstructions').value = '';
    $('tplLayout').value = T('tpl.default_layout');
    $('tplHints').value = '';
    $('tplOrder').value = '100';
    // Nouveau gabarit : la langue de l'interface est le point de départ le plus
    // probable, sans être imposée.
    $('tplLanguage').value = LANG;
    // Tout gabarit créé ici est personnel : son auteur en garde la main.
    applyTemplateLock(false, true);
    $('btnDeleteTemplate').classList.add('hidden');
    $('btnDuplicateTemplate').classList.add('hidden');
    $('templateFormStatus').textContent = '';
    $('tplName').focus();
  }

  async function submitTemplateForm(event) {
    event.preventDefault();

    const id = $('tplId').value;
    const body = {
      name: $('tplName').value.trim(),
      description: $('tplDescription').value.trim(),
      system_instructions: $('tplInstructions').value.trim(),
      layout_format: $('tplLayout').value.trim(),
      phrase_hints: $('tplHints').value.trim(),
      sort_order: parseInt($('tplOrder').value, 10) || 100,
      language: $('tplLanguage').value || 'fr',
    };

    if (!body.name || !body.system_instructions || !body.layout_format) {
      toast(T('tpl.required_fields'), 'warning');
      return;
    }

    $('templateFormStatus').textContent = T('tpl.saving');
    try {
      const saved = id
        ? await api(`/api/templates/${id}`, { method: 'PUT', body })
        : await api('/api/templates', { method: 'POST', body });

      await loadTemplates(saved.id);
      fillTemplateForm(saved);
      renderTemplateList();
      updateTemplateDescription();
      $('templateFormStatus').textContent = '';
      // Retour à la liste sur mobile : confirme visuellement l'enregistrement.
      if (isMobileLayout()) setTemplateMobileView('list');
      toast(id ? T('tpl.updated') : T('tpl.created'), 'success');
    } catch (err) {
      $('templateFormStatus').textContent = '';
      toast(err.message, 'error', 8000);
    }
  }

  /**
   * Duplique le gabarit ouvert dans le formulaire.
   *
   * La copie est faite côté serveur : lui seul peut garantir l'unicité du nom
   * sans laisser l'utilisateur buter sur une erreur 409 à l'enregistrement.
   */
  async function duplicateTemplate() {
    const id = $('tplId').value;
    if (!id) {
      toast(T('tpl.open_first'), 'warning');
      return;
    }

    $('templateFormStatus').textContent = T('tpl.duplicating');
    try {
      const copy = await api(`/api/templates/${id}/duplicate`, { method: 'POST' });
      await loadTemplates(copy.id);
      fillTemplateForm(copy);
      renderTemplateList();
      updateTemplateDescription();
      $('templateFormStatus').textContent = '';
      // On reste sur le formulaire : dupliquer sert toujours à modifier
      // la copie dans la foulée.
      setTemplateMobileView('form');
      $('tplName').focus();
      $('tplName').select();
      toast(T('tpl.duplicated', { name: copy.name }), 'success');
    } catch (err) {
      $('templateFormStatus').textContent = '';
      toast(err.message, 'error', 8000);
    }
  }

  async function deleteTemplate() {
    const id = $('tplId').value;
    if (!id) return;

    const tpl = state.templates.find((t) => String(t.id) === String(id));
    const name = tpl ? tpl.name : T('tpl.this_one');
    if (!window.confirm(T('tpl.confirm_delete', { name }))) return;

    try {
      await api(`/api/templates/${id}`, { method: 'DELETE' });
      await loadTemplates();
      resetTemplateForm();
      renderTemplateList();
      updateTemplateDescription();
      setTemplateMobileView('list');
      toast(T('tpl.deleted'), 'success');
    } catch (err) {
      toast(err.message, 'error', 8000);
    }
  }

  /* ---- Brouillons -------------------------------------------------------- */

  /**
   * Un brouillon dans la liste.
   *
   * On y met le strict nécessaire pour reconnaître la consultation : la
   * raison, à quelle heure. Volontairement pas d'extrait de la note — l'écran
   * est souvent consulté en présence d'un patient, et un paragraphe de contenu
   * clinique n'aide en rien à retrouver la bonne ligne.
   */
  function renderDraftItem(draft) {
    const item = document.createElement('li');
    item.className = 'px-5 py-3 border-b border-slate-100 hover:bg-slate-50 transition '
      + 'flex items-start gap-3';

    const name = draft.reason || draft.title || T('drafts.unnamed_patient');
    const reason = draft.reason
      ? `<div class="text-xs text-slate-600 mt-0.5 truncate">${esc(draft.reason)}</div>`
      : `<div class="text-xs text-slate-400 italic mt-0.5">${esc(T('drafts.no_reason'))}</div>`;

    item.innerHTML = `
      <span class="text-xs font-mono tabular-nums text-slate-400 pt-0.5 shrink-0 w-11">
        ${esc(formatTime(draft.created_at))}
      </span>
      <div class="flex-1 min-w-0 cursor-pointer" data-open="${draft.id}">
        <div class="font-medium text-sm text-slate-800 truncate">
          ${esc(name)}
        </div>
        ${reason}
        <div class="text-[11px] text-slate-400 mt-1">
          ${draft.template_name ? esc(draft.template_name) + ' · ' : ''}
          <span class="uppercase tracking-wide">${esc(draft.status)}</span>
        </div>
      </div>
      <button type="button" data-delete="${draft.id}"
              class="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50 shrink-0">
        ${esc(T('drafts.delete'))}
      </button>
    `;
    return item;
  }

  /**
   * Liste groupée par jour de dictée, du plus récent au plus ancien, et à
   * l'intérieur d'un jour de l'heure la plus récente à la plus ancienne.
   *
   * Le regroupement se fait ici et non côté serveur : la date d'un même
   * horodatage UTC dépend du fuseau du navigateur, seul le client sait donc
   * dans quel jour tombe réellement une dictée de fin de soirée.
   */
  function renderDraftList(drafts) {
    const list = $('draftList');
    list.innerHTML = '';

    const sorted = drafts.slice().sort(
      (a, b) => new Date(b.created_at) - new Date(a.created_at)
    );

    let currentDay = null;
    sorted.forEach((draft) => {
      const day = localDayKey(draft.created_at);
      if (day !== currentDay) {
        currentDay = day;
        const heading = document.createElement('li');
        heading.className = 'sticky top-0 z-10 px-5 py-1.5 bg-slate-100 border-y border-slate-200 '
          + 'text-[11px] font-semibold uppercase tracking-wide text-slate-500';
        heading.textContent = formatDayHeading(draft.created_at);
        list.appendChild(heading);
      }
      list.appendChild(renderDraftItem(draft));
    });
  }

  async function openDraftsModal() {
    $('draftsModal').classList.remove('hidden');
    const list = $('draftList');
    list.innerHTML = `<li class="px-5 py-6 text-sm text-slate-400 text-center">${esc(T('drafts.loading'))}</li>`;

    try {
      const data = await api('/api/consultations');
      const drafts = data.consultations || [];

      const notice = $('draftsRetentionNotice');
      if (data.retention_hours > 0) {
        notice.textContent = T('drafts.retention_notice', { hours: data.retention_hours });
        notice.classList.remove('hidden');
      } else {
        notice.classList.add('hidden');
      }

      if (!drafts.length) {
        list.innerHTML = `<li class="px-5 py-8 text-sm text-slate-400 text-center">${esc(T('drafts.none'))}</li>`;
        return;
      }

      renderDraftList(drafts);

      list.querySelectorAll('[data-open]').forEach((node) => {
        node.addEventListener('click', () => loadDraft(node.dataset.open));
      });
      list.querySelectorAll('[data-delete]').forEach((node) => {
        node.addEventListener('click', async (event) => {
          event.stopPropagation();
          if (!window.confirm(T('drafts.confirm_delete'))) return;
          try {
            await api(`/api/consultations/${node.dataset.delete}`, { method: 'DELETE' });
            if (String(state.consultationId) === String(node.dataset.delete)) {
              state.consultationId = null;
            }
            toast(T('drafts.deleted'), 'success');
            openDraftsModal();
          } catch (err) {
            toast(err.message, 'error');
          }
        });
      });
    } catch (err) {
      list.innerHTML = `<li class="px-5 py-6 text-sm text-red-600 text-center">${esc(err.message)}</li>`;
    }
  }

  async function loadDraft(id) {
    if (state.recording) {
      toast(T('drafts.busy_open'), 'warning');
      return;
    }
    try {
      const draft = await api(`/api/consultations/${id}`);
      state.consultationId = draft.id;
      state.recordedSeconds = draft.audio_seconds || 0;

      $('transcript').value = formatSentences(draft.raw_transcript);
      // Toujours la dernière note GÉNÉRÉE, jamais une version éditée : une
      // régénération remplace désormais ``edited_markdown`` sans condition
      // (voir api_generate), donc les deux ne divergent plus qu'en cours de
      // relecture — et c'est cet état de relecture qu'on ne veut pas montrer
      // à la réouverture, pour ne jamais rouvrir sur un texte périmé.
      state.lastGeneratedMarkdown = draft.generated_markdown || '';
      $('markdownEditor').value = draft.generated_markdown || '';
      $('timer').textContent = formatDuration(state.recordedSeconds);

      clearMetadata();
      applyMetadata({
        consultation_date: draft.consultation_date,
        reason: draft.reason,
        requester: draft.requester,
        accompanied_by: draft.accompanied_by,
      });

      if (draft.template_id) {
        $('templateSelect').value = String(draft.template_id);
        updateTemplateDescription();
      }
      updateActionButtons();

      updateTranscriptMeta(null);
      showPreview();
      // On ouvre sur la note si elle existe déjà, sinon sur la dictée.
      setMobilePane($('markdownEditor').value.trim() ? 'note' : 'dictee');
      state.lastSavedSnapshot = workspaceSnapshot();
      loadRecordings();
      showNoteEngines(draft.stt_used, draft.llm_used, draft.audio_used);
      showTranscriptEngine(draft.stt_used);
      showDebugInfo({
        llm: draft.llm_used,
        stt: draft.stt_used,
        audioUsed: draft.audio_used,
        promptTokens: draft.usage_prompt_tokens,
        outputTokens: draft.usage_output_tokens,
        elapsedSeconds: draft.generation_seconds,
      });
      state.transcriptLanguage = draft.stt_language || '';
      setSaveStatus(T('save.loaded_at', { date: formatDateTime(draft.updated_at) }));
      $('draftsModal').classList.add('hidden');
      toast(T('drafts.loaded', { title: draft.title }), 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  /** Réinitialise l'espace de travail pour une nouvelle consultation. */
  /** Remise à zéro complète de l'écran, sans confirmation ni garde. */
  function resetWorkspace() {
    state.consultationId = null;
    state.recordedSeconds = 0;
    state.lastSavedSnapshot = '';
    state.lastGeneratedMarkdown = '';
    $('transcript').value = '';
    $('markdownEditor').value = '';
    $('ctxExtra').value = '';
    clearMetadata();
    $('timer').textContent = '00:00';
    $('transcriptMeta').textContent = '';
    setSaveStatus('');
    state.transcriptLanguage = '';
    showNoteEngines('', '');
    showTranscriptEngine('');
    showDebugInfo(null);
    loadRecordings();
    showPreview();
    setMobilePane('dictee');
  }

  function newConsultation() {
    if (state.recording) {
      toast(T('drafts.busy_new'), 'warning');
      return;
    }
    if ($('transcript').value.trim() || $('markdownEditor').value.trim()) {
      if (!window.confirm(T('drafts.confirm_new'))) {
        return;
      }
    }
    resetWorkspace();
  }

  /**
   * Corbeille : supprime la consultation ENTIÈRE — transcription, note,
   * métadonnées et le brouillon côté serveur — et non seulement le texte à
   * l'écran. Même avertissement que la suppression depuis la liste des
   * brouillons : c'est la même opération, vue depuis la consultation ouverte.
   */
  async function deleteCurrentConsultation() {
    if (state.recording) {
      toast(T('drafts.busy_new'), 'warning');
      return;
    }
    // Rien à supprimer : ni brouillon serveur, ni contenu local.
    const vide = !$('transcript').value.trim()
      && !$('markdownEditor').value.trim()
      && !state.consultationId;
    if (vide) return;

    if (!window.confirm(T('drafts.confirm_delete'))) return;

    if (state.consultationId) {
      try {
        await api(`/api/consultations/${state.consultationId}`, { method: 'DELETE' });
        toast(T('drafts.deleted'), 'success');
      } catch (err) {
        toast(err.message, 'error');
        return;
      }
    }
    // L'écran repart sur une consultation vierge, comme après « Nouvelle ».
    resetWorkspace();
  }

  /* -------------------------------------------------------------------------
   * Service worker — installation sur l'écran d'accueil
   * ----------------------------------------------------------------------
   * Servi depuis la racine (/sw.js) pour couvrir toute l'application. Il ne
   * met en cache que les ressources statiques : ni la page, ni les appels
   * d'API, qui contiennent des renseignements de santé (voir sw.js).
   * ---------------------------------------------------------------------- */
  /**
   * Vérifie que l'installation en tant qu'application est possible, et
   * explique précisément ce qui bloque le cas échéant.
   *
   * Sans ce contrôle, un échec d'installation est totalement silencieux :
   * l'option « Installer » n'apparaît simplement jamais, sans le moindre
   * message. Les causes réelles sont presque toujours l'une des trois
   * ci-dessous, et aucune n'est devinable depuis l'interface.
   *
   * Le résultat est aussi exposé dans la console via window.consultaiDiag().
   */
  async function diagnosePwa(verbose) {
    const report = {
      contexteSecurise: window.isSecureContext,
      protocole: window.location.protocol,
      serviceWorkerSupporte: 'serviceWorker' in navigator,
      manifeste: T('pwa.state_unchecked'),
      serviceWorkerActif: false,
      installee: isStandalone(),
    };

    // Le manifeste est-il réellement servi, ou intercepté par le SSO ?
    try {
      const response = await fetch('/static/manifest.webmanifest', { credentials: 'same-origin' });
      const type = response.headers.get('content-type') || '';
      if (!response.ok) {
        report.manifeste = `HTTP ${response.status}`;
      } else if (type.includes('html')) {
        // Cas typique : le proxy authentifie encore cette ressource (sa
        // propre authentification devrait être désactivée ici — README §4)
        // et renvoie sa page de connexion au lieu du manifeste.
        report.manifeste = T('pwa.state_sso');
      } else {
        await response.clone().json();
        report.manifeste = 'OK';
      }
    } catch (err) {
      report.manifeste = T('pwa.state_unreadable', { error: err.message });
    }

    if ('serviceWorker' in navigator) {
      const registration = await navigator.serviceWorker.getRegistration('/');
      report.serviceWorkerActif = Boolean(registration && registration.active);
    }

    console.table(report);

    if (verbose) {
      if (!report.contexteSecurise) {
        toast(T('pwa.insecure', { protocol: report.protocole }), 'warning', 12000);
      } else if (report.manifeste !== 'OK') {
        toast(T('pwa.manifest_blocked', { state: report.manifeste }), 'warning', 14000);
      }
    }
    return report;
  }

  // Accessible depuis la console du navigateur : consultaiDiag()
  window.consultaiDiag = () => diagnosePwa(false);

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    // Un service worker exige un contexte sécurisé ; en HTTP direct
    // (dépannage local), on n'essaie même pas.
    if (!window.isSecureContext) {
      diagnosePwa(true);
      return;
    }
    // Contrôle différé : ne gêne pas le démarrage, mais signale un blocage.
    setTimeout(() => diagnosePwa(true), 3000);

    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .then((registration) => {
          // Nouvelle version déployée : on l'active sans attendre la
          // fermeture de tous les onglets.
          registration.addEventListener('updatefound', () => {
            const installing = registration.installing;
            if (!installing) return;
            installing.addEventListener('statechange', () => {
              if (installing.state === 'installed' && navigator.serviceWorker.controller) {
                installing.postMessage('SKIP_WAITING');
                toast(T('pwa.updated'), 'info', 8000);
              }
            });
          });
        })
        .catch((err) => console.warn('Service worker non enregistré :', err));
    });

    // Le nouveau service worker vient de prendre le contrôle, mais la page
    // tourne encore avec l'ANCIEN app.js — un décalage qui ressemble à un
    // bogue d'interface (HTML neuf, JS périmé). On recharge donc soi-même,
    // sauf pendant une dictée : le toast « pwa.updated » invite alors à le
    // faire à la main plutôt que de couper l'enregistrement. Sans garde
    // particulière contre les boucles : controllerchange ne se déclenche
    // qu'un changement de contrôleur, jamais au rechargement sous le même.
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (state.recording) return;
      window.location.reload();
    });
  }

  /* =========================================================================
   * 8 bis. ENREGISTREMENTS CONSERVÉS
   * ======================================================================
   * L'audio d'une consultation reste attaché à son brouillon. Il ne sert
   * qu'à une chose, mais elle compte : réécouter le passage dont on doute.
   * ====================================================================== */

  function formatBytes(bytes) {
    if (!bytes) return '';
    const mega = bytes / 1048576;
    return mega >= 1 ? `${mega.toFixed(1)} Mo` : `${Math.round(bytes / 1024)} ko`;
  }

  async function loadRecordings() {
    const block = $('recordingsBlock');
    if (!state.consultationId) {
      block.classList.add('hidden');
      $('recordingsList').innerHTML = '';
      state.recordingsCount = 0;
      updateActionButtons();
      return;
    }
    try {
      const data = await api(`/api/consultations/${state.consultationId}/recordings`);
      renderRecordings(data.recordings || []);
    } catch (err) {
      block.classList.add('hidden');
      console.warn(T('recordings.load_failed'), err);
    }
  }

  function renderRecordings(rows) {
    const block = $('recordingsBlock');
    const list = $('recordingsList');
    state.recordingsCount = rows.length;

    if (!rows.length) {
      block.classList.add('hidden');
      list.innerHTML = '';
      updateActionButtons();
      return;
    }
    block.classList.remove('hidden');
    $('recordingsCount').textContent = `— ${rows.length}`;

    list.innerHTML = rows.map((row) => {
      const detail = [
        row.source === 'import' ? T('recordings.source_import') : T('recordings.source_dictation'),
        row.duration_seconds ? formatDuration(row.duration_seconds) : '',
        formatBytes(row.size_bytes),
        formatDateTime(row.created_at),
      ].filter(Boolean).join(' · ');

      return `<li class="rounded-lg border border-slate-200 p-2">
        <div class="flex items-center gap-2 text-xs text-slate-600">
          <span class="truncate">${esc(detail)}</span>
          <a class="ml-auto shrink-0 px-2 py-0.5 rounded border border-slate-300 hover:bg-slate-50"
             href="/api/recordings/${row.id}/audio" download>${esc(T('recordings.download'))}</a>
          <button type="button" data-delete="${row.id}"
                  class="shrink-0 px-2 py-0.5 rounded border border-red-200 text-red-600 hover:bg-red-50">
            ${esc(T('recordings.discard'))}</button>
        </div>
        <!-- preload="none" : la liste peut contenir plusieurs dizaines de Mo,
             qu'il serait absurde de télécharger pour l'afficher. -->
        <audio class="w-full mt-1.5 h-9" controls preload="none"
               src="/api/recordings/${row.id}/audio"></audio>
      </li>`;
    }).join('');

    list.querySelectorAll('button[data-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (!window.confirm(T('recordings.confirm_delete'))) return;
        try {
          await api(`/api/recordings/${button.dataset.delete}`, { method: 'DELETE' });
          toast(T('recordings.deleted'), 'info');
          loadRecordings();
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    });
    updateActionButtons();
  }

  /* =========================================================================
   * 8 ter. PANNEAU D'ADMINISTRATION
   * ======================================================================
   * Les champs sont construits à partir du schéma renvoyé par l'API : le
   * serveur (app/runtime_config.py) reste la seule description des réglages,
   * et en ajouter un ne demande de toucher ni à cette fonction ni au HTML.
   *
   * Seuls les champs modifiés sont renvoyés. C'est ce qui permet aux clés
   * d'API de n'être jamais transmises au navigateur : le champ arrive vide,
   * on n'y touche pas, il n'est pas renvoyé, la clé reste en place.
   * ====================================================================== */

  const adminState = {
    fields: [],
    dirty: new Set(),
    groups: [],
    tabs: [],
    tab: null,          // onglet visible ; null = le premier
    //: Valeurs saisies mais pas encore enregistrées, par clé de réglage.
    //
    // INDISPENSABLE depuis l'arrivée des sous-menus : naviguer d'un service à
    // l'autre reconstruit les champs, donc retire du DOM ceux qu'on vient de
    // remplir. Lire les valeurs dans le DOM au moment d'enregistrer, comme
    // avant, perdait silencieusement une clé saisie sous un autre sous-onglet.
    values: {},
    //: Sous-onglet visible, par groupe : { 'group.stt': 'soniox', … }
    subTab: {},
    people: null,       // réponse de /api/admin/users, chargée à la demande
    backups: null,      // réponse de /api/admin/backup, chargée à la demande
    stats: null,        // réponse de /api/admin/usage, chargée à la demande
    pricing: null,       // réponse de /api/admin/pricing, chargée à la demande
    statsRange: null,   // { from, to } — filtre courant de l'onglet Statistiques
    statsOwner: '',     // filtre d'usager de l'onglet Statistiques ('' = tous)
    logPage: 0,         // page courante du journal des générations
  };

  /**
    * Clé du groupe de réglages qui porte AUSSI la gestion des comptes.
    *
    * Ce n'est plus un onglet artificiel : « group.users » est un vrai groupe,
    * avec ses propres réglages (quelle revendication porte le nom, laquelle
    * porte l'avatar). L'onglet affiche donc ces champs, puis les comptes et les
    * groupes — c'est au même endroit qu'on regarde quand un nom s'affiche mal.
    *
    * On compare des CLÉS et jamais des libellés : ceux-ci sont traduits.
    */
  const PEOPLE_GROUP = 'group.users';
  //: Mêmes principes que PEOPLE_GROUP ci-dessus : un onglet à écran entièrement
  //: personnalisé plutôt que des champs génériques, pour les mêmes raisons
  //: (listes, actions, pas de simple clé/valeur).
  const BACKUP_GROUP = 'group.backup';
  const STATS_GROUP = 'group.stats';

  /**
   * Champs « modèle » (principal et rapide) de chaque fournisseur de modèle
   * de langage — ce sont eux, et EUX SEULS, qui doivent proposer la liste du
   * datalist partagé « Modèles disponibles ». Les champs *_model des services
   * vocaux (Deepgram, Cohere Transcribe…) portent le même suffixe mais n'ont
   * rien à voir : leur donner ce datalist suggérerait des noms de modèles de
   * langage dans un champ de reconnaissance vocale.
   */
  const LLM_MODEL_FIELD_KEYS = new Set([
    'gemini_model', 'gemini_model_fast',
    'anthropic_model', 'anthropic_model_fast',
    'openai_model', 'openai_model_fast',
    'cohere_llm_model', 'cohere_llm_model_fast',
    'mistral_llm_model', 'mistral_llm_model_fast',
    'qwen_omni_model', 'qwen_omni_model_fast',
    'custom_llm_model', 'custom_llm_model_fast',
  ]);

  function adminFieldMarkup(field, groupKey) {
    // Une clé partagée (Cohere, Mistral) est répétée sous deux groupes : sans
    // ce préfixe, les deux copies porteraient le même ``id`` HTML, et
    // l'attribut ``for`` de leur étiquette pointerait toujours vers la
    // PREMIÈRE — l'autre étiquette resterait alors sans effet au clic.
    const id = groupKey ? `adm_${groupKey}_${field.key}` : `adm_${field.key}`;
    const help = field.help
      ? `<p class="text-[11px] text-slate-500 mt-1 leading-relaxed">${esc(field.help)}</p>` : '';
    const origin = field.overridden
      ? `<span class="text-[10px] px-1.5 py-0.5 rounded accent-badge ml-1.5">${esc(T('admin.from_panel'))}</span>`
      : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 ml-1.5">${esc(T('admin.from_env'))}</span>`;

    let control;
    if (field.kind === 'choice') {
      const options = field.choices.map((choice) =>
        `<option value="${esc(choice.value)}"${choice.value === field.value ? ' selected' : ''}>
           ${esc(choice.label)}</option>`).join('');
      control = `<select id="${id}" data-key="${field.key}"
                   class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white">
                   ${options}</select>`;
    } else if (field.kind === 'textarea') {
      control = `<textarea id="${id}" data-key="${field.key}" rows="6"
                   placeholder="${esc(field.placeholder)}"
                   class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono
                          leading-relaxed">${esc(field.value || '')}</textarea>`;
    } else if (field.kind === 'secret') {
      const placeholder = field.configured
        ? T('admin.secret_configured', { hint: field.hint })
        : T('admin.secret_missing');
      control = `<div class="flex gap-2">
          <input id="${id}" data-key="${field.key}" type="password" autocomplete="off"
                 placeholder="${esc(placeholder)}"
                 class="flex-1 min-w-0 border border-slate-300 rounded-lg px-3 py-2 text-sm">
          <button type="button" data-clear="${field.key}"
                  class="shrink-0 px-2.5 rounded-lg border border-slate-300 text-xs hover:bg-slate-50"
                  title="${esc(T('admin.secret_clear_title'))}">${esc(T('admin.secret_clear'))}</button>
        </div>`;
    } else {
      const type = field.kind === 'number' ? 'number' : 'text';
      const step = field.kind === 'number' ? ' step="0.05" min="0" max="2"' : '';
      const list = LLM_MODEL_FIELD_KEYS.has(field.key) ? ' list="modelOptions"' : '';
      control = `<input id="${id}" data-key="${field.key}" type="${type}"${step}${list}
                   value="${esc(field.value || '')}" placeholder="${esc(field.placeholder)}"
                   class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">`;
    }

    return `<div>
      <label for="${id}" class="block text-xs font-medium text-slate-600 mb-1">
        ${esc(field.label)}${origin}
      </label>
      ${control}${help}
    </div>`;
  }

  /* -------------------------------------------------------------------------
   * Champs sans objet selon le fournisseur choisi
   * ----------------------------------------------------------------------
   * Une clé Anthropic n'a rien à faire à l'écran quand OpenAI est sélectionné :
   * elle n'est pas seulement inutile, elle laisse croire qu'il faut la
   * renseigner. On masque donc ce qui ne sert pas au fournisseur courant.
   *
   * MASQUER N'EST PAS EFFACER : la valeur reste en base et reparaît au retour
   * du fournisseur. Le champ masqué n'est simplement pas rendu, donc jamais
   * marqué modifié, donc jamais renvoyé.
   * ---------------------------------------------------------------------- */
  const PROVIDER_ONLY = {
    // Modèle de langage : AUCUN réglage commun — modèle, modèle rapide et
    // température sont propres à chaque fournisseur (voir runtime_config.py).
    gemini_api_key: { key: 'llm_provider', value: 'gemini' },
    gemini_model: { key: 'llm_provider', value: 'gemini' },
    gemini_model_fast: { key: 'llm_provider', value: 'gemini' },
    gemini_temperature: { key: 'llm_provider', value: 'gemini' },
    // Options audio : propres à Gemini, PAS des réglages communs — sans ces
    // entrées elles s'affichaient (à tort) sous les six autres onglets.
    gemini_send_audio: { key: 'llm_provider', value: 'gemini' },
    gemini_send_audio_max_minutes: { key: 'llm_provider', value: 'gemini' },
    gemini_bypass_stt: { key: 'llm_provider', value: 'gemini' },
    gemini_bypass_stt_keep_transcript: { key: 'llm_provider', value: 'gemini' },
    anthropic_api_key: { key: 'llm_provider', value: 'anthropic' },
    anthropic_model: { key: 'llm_provider', value: 'anthropic' },
    anthropic_model_fast: { key: 'llm_provider', value: 'anthropic' },
    anthropic_temperature: { key: 'llm_provider', value: 'anthropic' },
    openai_api_key: { key: 'llm_provider', value: 'openai' },
    openai_model: { key: 'llm_provider', value: 'openai' },
    openai_model_fast: { key: 'llm_provider', value: 'openai' },
    openai_temperature: { key: 'llm_provider', value: 'openai' },
    // Suffixe « _llm » : cohere_model / mistral_model existent déjà côté
    // Reconnaissance vocale et désignent le modèle de TRANSCRIPTION.
    cohere_llm_model: { key: 'llm_provider', value: 'cohere' },
    cohere_llm_model_fast: { key: 'llm_provider', value: 'cohere' },
    cohere_llm_temperature: { key: 'llm_provider', value: 'cohere' },
    mistral_llm_model: { key: 'llm_provider', value: 'mistral' },
    mistral_llm_model_fast: { key: 'llm_provider', value: 'mistral' },
    mistral_llm_temperature: { key: 'llm_provider', value: 'mistral' },
    qwen_omni_api_key: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_base_url: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_model: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_model_fast: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_temperature: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_send_audio: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_send_audio_max_minutes: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_bypass_stt: { key: 'llm_provider', value: 'qwen_omni' },
    qwen_omni_bypass_stt_keep_transcript: { key: 'llm_provider', value: 'qwen_omni' },
    custom_llm_api_key: { key: 'llm_provider', value: 'custom' },
    custom_llm_base_url: { key: 'llm_provider', value: 'custom' },
    custom_llm_model: { key: 'llm_provider', value: 'custom' },
    custom_llm_model_fast: { key: 'llm_provider', value: 'custom' },
    custom_llm_temperature: { key: 'llm_provider', value: 'custom' },
    // Options audio : le point de terminaison personnalisé peut être
    // multimodal (OpenRouter) — mêmes réglages que Gemini/Qwen Omni.
    custom_send_audio: { key: 'llm_provider', value: 'custom' },
    custom_send_audio_max_minutes: { key: 'llm_provider', value: 'custom' },
    custom_bypass_stt: { key: 'llm_provider', value: 'custom' },
    custom_bypass_stt_keep_transcript: { key: 'llm_provider', value: 'custom' },
    // Réglages propres à chaque service vocal
    deepgram_api_key: { key: 'stt_provider', value: 'deepgram' },
    deepgram_model: { key: 'stt_provider', value: 'deepgram' },
    deepgram_language: { key: 'stt_provider', value: 'deepgram' },
    assemblyai_api_key: { key: 'stt_provider', value: 'assemblyai' },
    assemblyai_model: { key: 'stt_provider', value: 'assemblyai' },
    assemblyai_language: { key: 'stt_provider', value: 'assemblyai' },
    assemblyai_medical: { key: 'stt_provider', value: 'assemblyai' },
    soniox_api_key: { key: 'stt_provider', value: 'soniox' },
    soniox_model: { key: 'stt_provider', value: 'soniox' },
    soniox_language: { key: 'stt_provider', value: 'soniox' },
    soniox_send_context: { key: 'stt_provider', value: 'soniox' },
    cohere_api_key: { key: 'stt_provider', value: 'cohere' },
    cohere_model: { key: 'stt_provider', value: 'cohere' },
    cohere_language: { key: 'stt_provider', value: 'cohere' },
    mistral_api_key: { key: 'stt_provider', value: 'mistral' },
    mistral_model: { key: 'stt_provider', value: 'mistral' },
    mistral_language: { key: 'stt_provider', value: 'mistral' },
    openai_stt_model: { key: 'stt_provider', value: 'openai' },
    openai_stt_language: { key: 'stt_provider', value: 'openai' },
    custom_stt_api_key: { key: 'stt_provider', value: 'custom' },
    custom_stt_base_url: { key: 'stt_provider', value: 'custom' },
    custom_stt_model: { key: 'stt_provider', value: 'custom' },
    custom_stt_language: { key: 'stt_provider', value: 'custom' },
  };

  /**
   * Groupes présentés par SERVICE, avec un sous-menu.
   *
   * Le sous-menu remplace la liste déroulante de sélection : on choisit un
   * service en cliquant son onglet, et l'on voit sous celui-ci tout ce qui le
   * concerne. Les réglages qui s'appliquent à TOUS les services — le retrait
   * des pauses, le modèle, la température — sont répétés sous chaque service :
   * c'est délibéré, on règle un service d'un seul endroit.
   */
  const PROVIDER_GROUPS = {
    'group.stt': 'stt_provider',
    'group.llm': 'llm_provider',
  };

  /**
   * Clé d'API dont dépend un service, par groupe et par service.
   *
   * Nécessaire parce que la correspondance n'est pas toujours « un service, un
   * champ » : Cohere et Mistral emploient LA MÊME clé pour la reconnaissance
   * vocale et pour le modèle de langage. Le réglage n'existe qu'une fois côté
   * serveur, sous Reconnaissance vocale — mais ``partitionFields`` RÉPÈTE ce
   * même champ sous Modèle de langage plutôt que de renvoyer vers l'autre
   * onglet : les deux copies partagent le même ``data-key`` et sont
   * maintenues synchronisées (voir le gestionnaire d'``input`` plus bas).
   */
  const PROVIDER_KEY_FIELD = {
    'group.stt|deepgram': 'deepgram_api_key',
    'group.stt|assemblyai': 'assemblyai_api_key',
    'group.stt|soniox': 'soniox_api_key',
    'group.stt|cohere': 'cohere_api_key',
    'group.stt|mistral': 'mistral_api_key',
    // Pas de clé propre : même compte que le modèle de langage OpenAI,
    // réglage porté par « group.llm » (voir runtime_config.py).
    'group.stt|openai': 'openai_api_key',
    'group.stt|custom': 'custom_stt_api_key',
    'group.llm|gemini': 'gemini_api_key',
    'group.llm|anthropic': 'anthropic_api_key',
    'group.llm|openai': 'openai_api_key',
    'group.llm|cohere': 'cohere_api_key',
    'group.llm|mistral': 'mistral_api_key',
    'group.llm|qwen_omni': 'qwen_omni_api_key',
    'group.llm|custom': 'custom_llm_api_key',
  };

  /**
   * Avertissements affichés en tête d'un groupe, selon la valeur d'un réglage.
   *
   * Cohere plafonne à 5 requêtes/minute : c'est une contrainte qui décide de
   * l'usage qu'on peut en faire, pas un détail à enterrer dans un texte
   * d'aide. Elle s'affiche donc en évidence dès qu'on le sélectionne.
   */
  const PROVIDER_WARNINGS = [
    {
      group: 'group.stt',
      key: 'stt_provider',
      value: 'cohere',
      messages: ['admin.cohere_warning', 'admin.cohere_no_vocab'],
    },
  ];

  /**
   * Valeur courante d'un réglage.
   *
   * Ordre : ce qui a été saisi (même sous un autre sous-onglet), puis ce qui est
   * à l'écran, puis ce que le serveur a renvoyé.
   */
  function adminValueOf(key) {
    if (Object.prototype.hasOwnProperty.call(adminState.values, key)) {
      return adminState.values[key];
    }
    const element = $('adminFields').querySelector(`[data-key="${key}"]`);
    if (element) return element.value;
    const field = adminState.fields.find((f) => f.key === key);
    return field ? (field.value || '') : '';
  }


  /** Le service affiché pour ce groupe : celui qu'on visite, sinon l'actif. */
  function currentProvider(groupKey) {
    const cle = PROVIDER_GROUPS[groupKey];
    return adminState.subTab[groupKey] || adminValueOf(cle);
  }

  /** Les services offerts, tels que le serveur les déclare. */
  function providerChoices(groupKey) {
    const champ = adminState.fields.find((f) => f.key === PROVIDER_GROUPS[groupKey]);
    return (champ && champ.choices) || [];
  }

  /**
   * Répartit les champs d'un groupe : propres au service affiché, puis communs.
   *
   * La répartition est DÉDUITE de PROVIDER_ONLY, déjà nécessaire par ailleurs :
   * un champ sans règle s'applique à tous les services, un champ avec règle
   * n'appartient qu'au sien. Le sélecteur de service lui-même n'est pas rendu —
   * le sous-menu le remplace.
   */
  function partitionFields(groupKey, provider) {
    const selecteur = PROVIDER_GROUPS[groupKey];
    const propres = [];
    const communs = [];
    adminState.fields.forEach((field) => {
      if (field.group !== groupKey || field.key === selecteur) return;
      const regle = PROVIDER_ONLY[field.key];
      if (!regle) communs.push(field);
      else if (regle.key === selecteur && regle.value === provider) propres.push(field);
    });

    // Clé partagée entre deux services (Cohere, Mistral) : le champ n'existe
    // qu'une fois côté serveur, sous Reconnaissance vocale, mais on le RÉPÈTE
    // ici plutôt que de renvoyer vers l'autre onglet — dans les deux cas la
    // saisie écrit le même réglage, inutile de changer d'onglet pour la faire.
    const partagee = PROVIDER_KEY_FIELD[`${groupKey}|${provider}`];
    const champPartage = partagee && adminState.fields.find((f) => f.key === partagee);
    if (champPartage && champPartage.group !== groupKey) {
      propres.unshift(champPartage);
    }

    return { propres, communs };
  }

  function providerSubMenu(groupKey) {
    const actif = adminValueOf(PROVIDER_GROUPS[groupKey]);
    const enregistre = (adminState.fields.find(
      (f) => f.key === PROVIDER_GROUPS[groupKey],
    ) || {}).value;
    const vu = currentProvider(groupKey);

    const boutons = providerChoices(groupKey).map((choix, index) => {
      const estVu = choix.value === vu;
      return `<button type="button" data-subtab="${index}"
                class="shrink-0 px-3 py-1.5 rounded-lg text-xs border transition ${
                  estVu
                    ? 'bg-white border-slate-300 text-slate-800 font-medium shadow-sm'
                    : 'bg-transparent border-transparent text-slate-500 hover:text-slate-700'}">
                ${esc(choix.label)}${choix.value === enregistre
                   ? '<span class="ml-1.5 inline-block w-1.5 h-1.5 rounded-full accent-dot"'
                    + ` title="${esc(T('admin.provider_active'))}"></span>`
                  : ''}</button>`;
    }).join('');

    return `<div class="flex gap-1 overflow-x-auto thin-scroll rounded-lg bg-slate-100 p-1">
              ${boutons}</div>`;
  }

  /** Bandeau d'état du service affiché : actif, en attente, ou à activer. */
  function providerStatus(groupKey) {
    const cle = PROVIDER_GROUPS[groupKey];
    const vu = currentProvider(groupKey);
    const enregistre = (adminState.fields.find((f) => f.key === cle) || {}).value;
    const stage = adminValueOf(cle);
    const libelle = (providerChoices(groupKey).find((c) => c.value === vu) || {}).label || vu;

    // Une clé absente est la première cause de « ça ne marche pas » après un
    // changement de service : on le dit ici plutôt que de laisser découvrir
    // l'échec à la première dictée.
    const nomClef = PROVIDER_KEY_FIELD[`${groupKey}|${vu}`];
    const champClef = nomClef && adminState.fields.find((f) => f.key === nomClef);
    const sansClef = champClef && !champClef.configured
      && !adminState.values[nomClef]
      ? `<p class="text-[11px] text-amber-800 mt-1">${esc(T('admin.provider_no_key'))}</p>`
      : '';

    if (vu === enregistre && vu === stage) {
      return `<div class="flex items-center gap-2 text-xs accent-text">
          <span class="w-1.5 h-1.5 rounded-full accent-dot"></span>
          <span class="font-medium">${esc(libelle)}</span>
          <span class="text-slate-500">— ${esc(T('admin.provider_active'))}</span>
        </div>${sansClef}`;
    }
    if (vu === stage) {
      return `<div class="flex items-center gap-2 text-xs text-amber-800">
          <span class="font-medium">${esc(libelle)}</span>
          <span>— ${esc(T('admin.provider_staged'))}</span>
        </div>${sansClef}`;
    }
    return `<div class="flex items-center gap-2 flex-wrap">
        <span class="text-xs font-medium text-slate-700">${esc(libelle)}</span>
        <button type="button" data-activate="${esc(vu)}" data-provider-key="${esc(cle)}"
                class="accent-btn px-2.5 py-1 rounded-lg text-xs font-medium transition">
          ${esc(T('admin.provider_use'))}</button>
      </div>${sansClef}`;
  }

  function renderAdminFields(groups) {
    const container = $('adminFields');
    const order = (groups && groups.length)
      ? groups.slice()
      : Array.from(new Set(adminState.fields.map((f) => f.group)))
        .map((key) => ({
          key,
          label: (adminState.fields.find((f) => f.group === key) || {}).group_label || key,
        }));

    adminState.groups = order;

    container.innerHTML = '<datalist id="modelOptions"></datalist>' + order.map((group, index) => {
      const parService = PROVIDER_GROUPS[group.key];
      let corps;

      if (parService) {
        const vu = currentProvider(group.key);
        const { propres, communs } = partitionFields(group.key, vu);
        // Les avertissements suivent le service CONSULTÉ et non le service
        // actif : on veut lire les réserves sur Cohere avant de l'activer.
        const alertes = PROVIDER_WARNINGS
          .filter((a) => a.group === group.key && a.value === vu)
          .flatMap((a) => a.messages)
          .map((c) => `<p class="rounded-lg border border-amber-300 bg-amber-50 p-2.5
                                 text-[11px] leading-relaxed text-amber-900">${esc(T(c))}</p>`)
          .join('');

        corps = `
          ${providerSubMenu(group.key)}
          <div class="rounded-lg border border-slate-200 p-3 space-y-3">
            ${providerStatus(group.key)}
            ${alertes}
            ${propres.length
              ? propres.map((f) => adminFieldMarkup(f, group.key)).join('')
              : `<p class="text-[11px] text-slate-500 leading-relaxed">${
                  T('admin.provider_env_only')}</p>`}
          </div>
          ${communs.length ? `<div class="pt-1 space-y-3">
            <p class="text-[11px] font-medium text-slate-500 uppercase tracking-wide">
              ${esc(T('admin.provider_shared'))}</p>
            ${communs.map(adminFieldMarkup).join('')}
          </div>` : ''}`;
      } else {
        const fields = adminState.fields.filter((f) => f.group === group.key);
        if (!fields.length) return '';
        corps = fields.map(adminFieldMarkup).join('');
      }

      return `<section data-group-index="${index}" class="space-y-3">
        <h3 class="text-sm font-semibold text-slate-800 border-b border-slate-200 pb-1">
          ${esc(group.label)}</h3>
        ${corps}
      </section>`;
    }).join('');

    // La consigne générale est du Markdown au même titre qu'un gabarit.
    container.querySelectorAll('textarea[data-key]').forEach(enableMarkdownEditing);

    container.querySelectorAll('[data-key]').forEach((element) => {
      const mark = () => {
        const key = element.dataset.key;
        adminState.dirty.add(key);
        // Mémorisé dans l'état : la valeur doit survivre à un changement de
        // sous-onglet, qui reconstruit le DOM.
        adminState.values[key] = element.value;
        // Une clé partagée (Cohere, Mistral) existe en DOUBLE — sous
        // Reconnaissance vocale et sous Modèle de langage. Les deux onglets
        // restent montés en parallèle (seul un ``hidden`` bascule lequel se
        // voit) : sans ce report immédiat, la copie de l'autre onglet
        // resterait affichée avec l'ancienne valeur jusqu'au prochain rendu
        // complet.
        container.querySelectorAll(`[data-key="${key}"]`).forEach((autre) => {
          if (autre !== element) autre.value = element.value;
        });
        $('adminStatus').textContent = T('admin.unsaved');
      };
      element.addEventListener('input', mark);
      element.addEventListener('change', mark);
    });

    // Navigation entre services : aucune écriture, on change ce qu'on regarde.
    container.querySelectorAll('button[data-subtab]').forEach((bouton) => {
      const section = bouton.closest('section[data-group-index]');
      const groupKey = order[Number(section.dataset.groupIndex)].key;
      bouton.addEventListener('click', () => {
        const choix = providerChoices(groupKey)[Number(bouton.dataset.subtab)];
        if (!choix) return;
        adminState.subTab[groupKey] = choix.value;
        renderAdminFields(adminState.groups);
        showAdminTab(adminState.tab);
      });
    });

    // Activation : c'est un acte EXPLICITE, distinct de la navigation. Sans
    // cela, simplement consulter un service pour y coller une clé l'aurait mis
    // en service — le piège exact qu'on cherche à éviter.
    container.querySelectorAll('button[data-activate]').forEach((bouton) => {
      bouton.addEventListener('click', () => {
        adminState.values[bouton.dataset.providerKey] = bouton.dataset.activate;
        adminState.dirty.add(bouton.dataset.providerKey);
        $('adminStatus').textContent = T('admin.unsaved');
        renderAdminFields(adminState.groups);
        showAdminTab(adminState.tab);
      });
    });

    // « Effacer » vide le champ ET le marque modifié : à l'enregistrement, une
    // valeur vide supprime la surcharge, et le réglage revient au .env.
    container.querySelectorAll('button[data-clear]').forEach((button) => {
      button.addEventListener('click', () => {
        // querySelectorAll et non querySelector : une clé partagée (Cohere,
        // Mistral) a une copie sous chaque onglet, toutes deux à vider.
        container.querySelectorAll(`[data-key="${button.dataset.clear}"]`).forEach((input) => {
          input.value = '';
          input.placeholder = T('admin.secret_will_clear');
        });
        adminState.dirty.add(button.dataset.clear);
        adminState.values[button.dataset.clear] = '';
        $('adminStatus').textContent = T('admin.unsaved');
      });
    });

    renderAdminTabs();
    showAdminTab(adminState.tab);
  }

  /* -------------------------------------------------------------------------
   * Onglets du panneau
   * ---------------------------------------------------------------------- */
  function renderAdminTabs() {
    const barre = $('adminTabs');
    if (!barre) return;

    const onglets = adminState.groups.slice();
    if (!adminState.tab || !onglets.some((o) => o.key === adminState.tab)) {
      adminState.tab = onglets.length ? onglets[0].key : null;
    }
    adminState.tabs = onglets;

    // L'identifiant ne traverse PAS le HTML : on ne met qu'un index dans
    // l'attribut et la correspondance se fait en JavaScript. Les libellés de
    // groupe sont des chaînes traduites, et esc() n'échappe pas les guillemets :
    // faire voyager la valeur dans un attribut, c'est en dépendre.
    barre.innerHTML = onglets.map((onglet, index) => {
      const actif = onglet.key === adminState.tab;
      return `<button type="button" data-tab-index="${index}"
                class="shrink-0 px-3 py-2 text-xs font-medium border-b-2 transition ${
                  actif
                    ? 'accent-border accent-text'
                    : 'border-transparent text-slate-500 hover:text-slate-700'}">
                ${esc(onglet.label)}</button>`;
    }).join('');

    barre.querySelectorAll('button[data-tab-index]').forEach((bouton) => {
      bouton.addEventListener('click', () => {
        const onglet = adminState.tabs[Number(bouton.dataset.tabIndex)];
        if (!onglet) return;
        adminState.tab = onglet.key;
        renderAdminTabs();
        showAdminTab(adminState.tab);
      });
    });
  }

  /**
   * Bandeau d'explication de l'onglet courant, et bascule d'affichage.
   *
   * Rendu ICI et non dans chaque section : la bascule pilote un état global, et
   * en afficher une par groupe donnait deux cases à cocher pour un seul
   * réglage. Le rappel sur la surcharge du .env n'apparaît que sur les onglets
   * qui portent effectivement des réglages.
   */
  function renderAdminIntro(tab) {
    const boite = $('adminIntro');
    if (!boite) return;

    const phrase = T(`admin.intro.${tab}`);
    const reglages = adminState.fields.some((f) => f.group === tab);

    // La bascule « afficher tous les fournisseurs » a disparu : le sous-menu
    // donne accès aux réglages de CHAQUE service, actif ou non. On peut donc y
    // coller une clé sans mettre le service en production, ce qui était le
    // problème que la bascule rustinait.
    boite.innerHTML = `
      <div class="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
        <p class="text-xs text-slate-600 leading-relaxed">${esc(phrase)}</p>
        ${reglages ? `<p class="text-[11px] text-slate-500 mt-1 leading-relaxed">${T('admin.env_note')}</p>` : ''}
      </div>`;
  }

  function showAdminTab(tab) {
    const comptes = tab === PEOPLE_GROUP;
    const sauvegarde = tab === BACKUP_GROUP;
    const stats = tab === STATS_GROUP;

    // L'onglet des comptes affiche ses propres réglages EN PLUS des comptes :
    // #adminFields reste donc visible, seule la section correspondante étant
    // dévoilée. « Modèles disponibles » n'a en revanche rien à y faire.
    $('adminFields').classList.remove('hidden');
    $('adminPeople').classList.toggle('hidden', !comptes);
    $('adminBackup').classList.toggle('hidden', !sauvegarde);
    $('adminStats').classList.toggle('hidden', !stats);

    renderAdminIntro(tab);

    // « Modèles disponibles » n'a de sens que sur l'onglet du modèle de langage ;
    // « Enregistrer » que sur un onglet portant des réglages.
    $('btnListModels').classList.toggle('hidden', tab !== 'group.llm');
    const aDesReglages = adminState.fields.some((f) => f.group === tab);
    $('btnSaveAdmin').classList.toggle('hidden', !aDesReglages);

    // Comparaison par index, pour la même raison que les onglets : les
    // libellés sont traduits, les clés ne le sont pas.
    const indexActif = adminState.groups.findIndex((g) => g.key === tab);
    $('adminFields').querySelectorAll('section[data-group-index]').forEach((section) => {
      section.classList.toggle(
        'hidden', Number(section.dataset.groupIndex) !== indexActif,
      );
    });

    if (comptes && !adminState.people) loadPeople();
    if (sauvegarde && !adminState.backups) loadBackups();
    if (stats && !adminState.stats) loadStats();
  }

  /* -------------------------------------------------------------------------
   * Comptes et groupes
   * ----------------------------------------------------------------------
   * Chaque ligne s'applique immédiatement, sans bouton « Enregistrer » global :
   * retirer un droit à quelqu'un ne doit pas pouvoir rester en attente dans un
   * formulaire qu'on oublie de valider.
   * ---------------------------------------------------------------------- */
  async function loadPeople() {
    const boite = $('adminPeople');
    boite.innerHTML = `<p class="text-sm text-slate-500">${esc(T('admin.loading'))}</p>`;
    try {
      adminState.people = await api('/api/admin/users');
      renderPeople();
    } catch (err) {
      boite.innerHTML = `<p class="text-sm text-red-600">${esc(err.message)}</p>`;
    }
  }

  /**
   * Initiales d'un libellé, pour le repli d'avatar dans la liste des comptes.
   *
   * Volontairement plus simple que la version du serveur (app/auth.py), qui
   * écarte les titres de civilité : ici il ne s'agit que d'un carré de 28 px
   * dans une liste d'administration, pas de l'identité affichée en permanence.
   */
  function initialsOf(libelle) {
    const mots = String(libelle || '').split(/[^\p{L}]+/u).filter(Boolean);
    if (!mots.length) return '?';
    if (mots.length === 1) return mots[0].slice(0, 2).toUpperCase();
    return (mots[0][0] + mots[mots.length - 1][0]).toUpperCase();
  }

  function permissionBadges(groupe) {
    const puces = [];
    if (groupe.is_admin) {
      puces.push(`<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">${
        esc(T('people.perm_admin'))}</span>`);
    }
    if (groupe.can_manage_templates && !groupe.is_admin) {
      puces.push(`<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">${
        esc(T('people.perm_templates'))}</span>`);
    }
    return puces.join(' ');
  }

  function renderPeople() {
    const data = adminState.people || { users: [], groups: [] };
    const boite = $('adminPeople');

    const lignesUsagers = (data.users || []).map((user) => {
      const moi = user.id === data.current_user_id;
      // L'appartenance en pastilles a bascule plutot qu'en mur de cases a
      // cocher : avec plusieurs groupes, la rangee de cases devenait illisible
      // et ne montrait pas d'un coup d'oeil a quoi l'usager appartient.
      const cases = (data.groups || []).map((groupe) => {
        const coche = (user.groups || []).some((g) => g.id === groupe.id);
        return `<button type="button" data-user="${user.id}" data-group="${groupe.id}"
                  aria-pressed="${coche}"
                  class="px-2 py-0.5 rounded-full text-[11px] border transition ${
                    coche
                      ? 'accent-btn accent-btn-bordered'
                      : 'bg-white text-slate-500 border-slate-300 hover:border-slate-400'}">
                  ${esc(groupe.name)}</button>`;
      }).join(' ');

      const etat = user.is_active
        ? `<span class="text-[10px] px-1.5 py-0.5 rounded accent-badge">${
            esc(T('people.active'))}</span>`
        : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700">${
            esc(T('people.disabled'))}</span>`;

      const details = [
        user.email && user.email !== user.username ? esc(user.email) : '',
        T('people.consultations', { count: user.consultation_count }),
        user.has_signed_in
          ? (user.last_login_at
              ? T('people.last_login', { date: formatDateTime(user.last_login_at) })
              : '')
          : T('people.never_signed_in'),
      ].filter(Boolean).join(' · ');

      // Avatar du fournisseur, repli sur les initiales. « onerror » retire
      // l'image et dévoile le repli : une adresse morte laisserait sinon un
      // carré vide, alors que les initiales sont toujours calculables.
      const initiales = initialsOf(user.display_name || user.username);
      const pastille = user.avatar_url
        ? `<span class="w-7 h-7 rounded-full bg-slate-200 overflow-hidden shrink-0
                       grid place-items-center text-[10px] font-semibold text-slate-600">
             <img src="${esc(user.avatar_url)}" alt="" referrerpolicy="no-referrer"
                  class="w-full h-full object-cover"
                  onerror="this.replaceWith(document.createTextNode('${esc(initiales)}'))">
           </span>`
        : `<span class="w-7 h-7 rounded-full bg-slate-200 shrink-0 grid place-items-center
                       text-[10px] font-semibold text-slate-600">${esc(initiales)}</span>`;

      return `<li class="rounded-lg border border-slate-200 p-3 space-y-2">
        <div class="flex items-center gap-2 flex-wrap">
          ${pastille}
          <span class="font-medium text-sm text-slate-800">${esc(user.display_name || user.username)}</span>
          <span class="text-xs text-slate-500">${esc(user.username)}</span>
          ${moi ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">${
            esc(T('people.you'))}</span>` : ''}
          ${etat}
          <button type="button" data-toggle-active="${user.id}"
                  class="ml-auto text-xs px-2 py-1 rounded border transition ${
                    user.is_active
                      ? 'border-red-200 text-red-600 hover:bg-red-50'
                      : 'accent-btn-ghost hover:accent-bg-subtle'}">
            ${esc(user.is_active ? T('people.deactivate') : T('people.reactivate'))}</button>
          ${moi ? '' : `<button type="button" data-delete-user="${user.id}"
                  data-name="${esc(user.display_name || user.username)}"
                  class="ml-2 text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50">
            ${esc(T('people.delete_user'))}</button>`}
        </div>
        <p class="text-[11px] text-slate-500">${esc(details)}</p>
        <div>${cases}</div>
      </li>`;
    }).join('');

    // Deux niveaux au lieu d'un seul : identite en haut, permissions et actions
    // en bas. En flex-wrap sur une ligne, le nom, les pastilles, le compte de
    // membres, la description et deux cases a cocher se replisaient dans un
    // ordre imprevisible des que la fenetre rétrécissait.
    const lignesGroupes = (data.groups || []).map((groupe) => `
      <li class="rounded-lg border border-slate-200 p-3 space-y-2">
        <div class="flex items-baseline gap-2 flex-wrap">
          <span class="font-medium text-sm text-slate-800">${esc(groupe.name)}</span>
          ${permissionBadges(groupe)}
          <span class="text-[11px] text-slate-500 ml-auto shrink-0">${
            esc(T('people.members', { count: groupe.member_count }))}</span>
        </div>
        ${groupe.description
          ? `<p class="text-[11px] text-slate-500 leading-relaxed">${esc(groupe.description)}</p>`
          : ''}
        <div class="flex items-center gap-4 flex-wrap pt-1 border-t border-slate-100">
          <label class="inline-flex items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" data-perm-group="${groupe.id}" data-perm="is_admin"
                   ${groupe.is_admin ? 'checked' : ''}
                   class="rounded border-slate-300 accent-ring" style="accent-color: var(--color-accent-border)">
            ${esc(T('people.perm_admin'))}</label>
          <label class="inline-flex items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" data-perm-group="${groupe.id}" data-perm="can_manage_templates"
                   ${groupe.can_manage_templates ? 'checked' : ''}
                   class="rounded border-slate-300 accent-ring" style="accent-color: var(--color-accent-border)">
            ${esc(T('people.perm_templates'))}</label>
          ${groupe.is_system ? '' : `<button type="button" data-delete-group="${groupe.id}"
                   data-name="${esc(groupe.name)}"
                   class="ml-auto text-xs px-2 py-1 rounded border border-red-200 text-red-600
                          hover:bg-red-50">${esc(T('people.delete_group'))}</button>`}
        </div>
      </li>`).join('');

    boite.innerHTML = `
      <section class="space-y-3">
        <h3 class="text-sm font-semibold text-slate-800 border-b border-slate-200 pb-1">
          ${esc(T('people.users_title'))}</h3>
        <p class="text-[11px] text-slate-500">${esc(T('people.disabled_warning'))}</p>
        <ul class="space-y-2">${lignesUsagers
          || `<li class="text-sm text-slate-500">${esc(T('people.no_users'))}</li>`}</ul>
      </section>
      <section class="space-y-3">
        <h3 class="text-sm font-semibold text-slate-800 border-b border-slate-200 pb-1">
          ${esc(T('people.groups_title'))}</h3>
        <p class="text-[11px] text-slate-500">${esc(T('people.provider_groups_note'))}</p>
        <ul class="space-y-2">${lignesGroupes}</ul>
        <div class="rounded-lg border border-dashed border-slate-300 p-3 space-y-2">
          <p class="text-xs font-medium text-slate-600">${esc(T('people.new_group'))}</p>
          <div class="flex flex-wrap gap-2">
            <input id="newGroupName" type="text" maxlength="80"
                   placeholder="${esc(T('people.group_name_ph'))}"
                   class="flex-1 min-w-[8rem] rounded-lg border-slate-300 text-sm">
            <input id="newGroupDesc" type="text" maxlength="300"
                   placeholder="${esc(T('people.group_desc_ph'))}"
                   class="flex-[2] min-w-[10rem] rounded-lg border-slate-300 text-sm">
            <button type="button" id="btnCreateGroup"
                     class="accent-btn px-3 py-2 rounded-lg text-sm">
              ${esc(T('people.create'))}</button>
          </div>
        </div>
      </section>`;

    bindPeopleActions();
  }

  function bindPeopleActions() {
    const boite = $('adminPeople');

    // Appartenance : on envoie la liste complète des groupes cochés, le serveur
    // remplace. Envoyer un delta obligerait à tenir un état partagé.
    boite.querySelectorAll('button[data-user][data-group]').forEach((pastille) => {
      pastille.addEventListener('click', async () => {
        const userId = Number(pastille.dataset.user);
        const groupId = Number(pastille.dataset.group);
        const actifs = new Set(
          Array.from(boite.querySelectorAll(`button[data-user="${userId}"]`))
            .filter((b) => b.getAttribute('aria-pressed') === 'true')
            .map((b) => Number(b.dataset.group)),
        );
        // Bascule locale, puis on envoie la liste complète : le serveur
        // remplace l'appartenance, il n'applique pas de delta.
        if (actifs.has(groupId)) actifs.delete(groupId);
        else actifs.add(groupId);
        await savePerson(userId, { group_ids: Array.from(actifs) });
      });
    });

    boite.querySelectorAll('button[data-toggle-active]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        const userId = Number(bouton.dataset.toggleActive);
        const user = (adminState.people.users || []).find((u) => u.id === userId);
        await savePerson(userId, { is_active: !(user && user.is_active) });
      });
    });

    boite.querySelectorAll('button[data-delete-user]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        const nom = bouton.dataset.name;
        if (!window.confirm(T('people.confirm_delete_user', { name: nom }))) return;
        try {
          await api(`/api/admin/users/${bouton.dataset.deleteUser}`, { method: 'DELETE' });
          toast(T('people.user_deleted'), 'success');
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
        await loadPeople();
      });
    });

    boite.querySelectorAll('input[data-perm-group]').forEach((caseACocher) => {
      caseACocher.addEventListener('change', async () => {
        const corps = {};
        corps[caseACocher.dataset.perm] = caseACocher.checked;
        try {
          await api(`/api/admin/groups/${caseACocher.dataset.permGroup}`, {
            method: 'PATCH', body: corps,
          });
          toast(T('people.group_saved'), 'success');
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
        await loadPeople();
      });
    });

    boite.querySelectorAll('button[data-delete-group]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        const nom = bouton.dataset.name;
        if (!window.confirm(T('people.confirm_delete_group', { name: nom }))) return;
        try {
          await api(`/api/admin/groups/${bouton.dataset.deleteGroup}`, { method: 'DELETE' });
          toast(T('people.group_deleted'), 'success');
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
        await loadPeople();
      });
    });

    const creer = $('btnCreateGroup');
    if (creer) {
      creer.addEventListener('click', async () => {
        const nom = $('newGroupName').value.trim();
        if (!nom) return;
        try {
          await api('/api/admin/groups', {
            method: 'POST',
            body: { name: nom, description: $('newGroupDesc').value.trim() },
          });
          toast(T('people.group_created'), 'success');
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
        await loadPeople();
      });
    }
  }

  async function savePerson(userId, corps) {
    try {
      await api(`/api/admin/users/${userId}`, { method: 'PATCH', body: corps });
      toast(T('people.saved'), 'success');
    } catch (err) {
      toast(err.message, 'error', 9000);
    }
    // Rechargement systématique, y compris après un échec : le serveur peut
    // avoir refusé (dernier administrateur), et l'écran doit alors revenir à
    // l'état réel plutôt que garder une case cochée à tort.
    await loadPeople();
  }

  /* -------------------------------------------------------------------------
   * Sauvegarde / restauration
   * -------------------------------------------------------------------------
   * Une restauration réussie bloque l'écriture côté serveur (voir le
   * middleware dans app/main.py) jusqu'au redémarrage manuel du conteneur :
   * dès que la réponse le signale, l'écran bascule sur un avis permanent,
   * non refermable — un toast serait trop facile à manquer pour une action
   * de cette gravité.
   * ---------------------------------------------------------------------- */
  function formatBytes(n) {
    if (!n) return '0 Ko';
    const units = ['o', 'Ko', 'Mo', 'Go'];
    let value = n;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function showRestartRequiredNotice(pending) {
    const boite = $('adminBackup');
    boite.innerHTML = `
      <div class="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
        <p class="text-sm font-medium text-amber-900">${esc(T('admin.backup.restart_required'))}</p>
      </div>`;
    $('btnSaveAdmin').classList.add('hidden');
    $('btnCloseAdmin').setAttribute('disabled', 'disabled');
  }

  async function loadBackups() {
    const boite = $('adminBackup');
    boite.innerHTML = `<p class="text-sm text-slate-500">${esc(T('admin.backup.loading'))}</p>`;
    try {
      adminState.backups = await api('/api/admin/backup');
      if (adminState.backups.restore_pending) {
        showRestartRequiredNotice(adminState.backups.restore_pending);
        return;
      }
      renderBackups();
    } catch (err) {
      boite.innerHTML = `<p class="text-sm text-red-600">${esc(err.message)}</p>`;
    }
  }

  function renderBackups() {
    const data = adminState.backups || { backups: [], retention_count: 0, last_run: {} };
    const boite = $('adminBackup');

    const dernier = data.last_run || {};
    let statutHtml;
    if (!dernier.at) {
      statutHtml = esc(T('admin.backup.last_run_never'));
    } else if (dernier.status === 'error') {
      statutHtml = esc(T('admin.backup.last_run_error', { error: dernier.error || '' }));
    } else {
      statutHtml = esc(T('admin.backup.last_run', { at: formatTime(dernier.at) }));
    }

    const lignes = (data.backups || []).map((b) => `
      <li class="flex items-center gap-3 px-3 py-2 border-b border-slate-100 text-sm">
        <span class="flex-1 min-w-0 truncate">${esc(formatBackupDate(b.created_at))}
          <span class="text-slate-400">· ${esc(T(`admin.backup.kind.${b.kind}`))} · ${esc(formatBytes(b.size_bytes))}</span>
        </span>
        <a href="/api/admin/backup/${encodeURIComponent(b.filename)}"
           class="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">${esc(T('admin.backup.download'))}</a>
        <button type="button" data-restore="${esc(b.filename)}"
                class="text-xs px-2 py-1 rounded border border-amber-300 text-amber-700 hover:bg-amber-50">${esc(T('admin.backup.restore'))}</button>
        <button type="button" data-delete-backup="${esc(b.filename)}"
                class="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50">${esc(T('admin.backup.delete'))}</button>
      </li>`).join('');

    boite.innerHTML = `
      <div class="flex flex-wrap items-center gap-2">
        <button type="button" id="btnBackupNow"
                class="accent-btn px-3 py-2 rounded-lg text-sm font-medium">${esc(T('admin.backup.now'))}</button>
        <label class="text-xs px-3 py-2 rounded-lg border border-slate-300 hover:bg-slate-50 cursor-pointer">
          ${esc(T('admin.backup.upload_restore'))}
          <input type="file" id="inputRestoreUpload" accept=".zip" class="hidden">
        </label>
        <span class="text-xs text-slate-500">${esc(T('admin.backup.retention_help_count', { count: data.retention_count }))}</span>
      </div>
      <p class="text-xs text-slate-500">${statutHtml}</p>
      <ul class="rounded-lg border border-slate-200 overflow-hidden">
        ${lignes || `<li class="px-3 py-6 text-sm text-slate-400 text-center">${esc(T('admin.backup.empty'))}</li>`}
      </ul>`;

    $('btnBackupNow').addEventListener('click', async (event) => {
      event.target.disabled = true;
      event.target.textContent = T('admin.backup.creating');
      try {
        await api('/api/admin/backup', { method: 'POST' });
        toast(T('admin.backup.now'), 'success');
        await loadBackups();
      } catch (err) {
        toast(err.message, 'error', 9000);
        event.target.disabled = false;
        event.target.textContent = T('admin.backup.now');
      }
    });

    boite.querySelectorAll('[data-delete-backup]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        if (!window.confirm(T('admin.backup.delete_confirm'))) return;
        try {
          await api(`/api/admin/backup/${encodeURIComponent(bouton.dataset.deleteBackup)}`, { method: 'DELETE' });
          await loadBackups();
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
      });
    });

    boite.querySelectorAll('[data-restore]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        if (!window.confirm(T('admin.backup.restore_confirm'))) return;
        try {
          const result = await api(
            `/api/admin/backup/restore/${encodeURIComponent(bouton.dataset.restore)}`,
            { method: 'POST' },
          );
          showRestartRequiredNotice(result.restore);
        } catch (err) {
          toast(err.message, 'error', 12000);
        }
      });
    });

    const inputUpload = $('inputRestoreUpload');
    if (inputUpload) {
      inputUpload.addEventListener('change', async () => {
        const fichier = inputUpload.files && inputUpload.files[0];
        if (!fichier) return;
        if (!window.confirm(T('admin.backup.restore_confirm'))) { inputUpload.value = ''; return; }
        const form = new FormData();
        form.append('file', fichier);
        try {
          const result = await api('/api/admin/backup/restore', { method: 'POST', body: form });
          showRestartRequiredNotice(result.restore);
        } catch (err) {
          toast(err.message, 'error', 12000);
        }
      });
    }
  }

  /* -------------------------------------------------------------------------
   * Statistiques d'usage
   * ---------------------------------------------------------------------- */
  //: Taille d'une page du journal des générations.
  const LOG_PAGE_SIZE = 50;

  function defaultStatsRange() {
    const to = new Date();
    const from = new Date(to.getTime() - 29 * 86400000);
    const iso = (d) => d.toISOString().slice(0, 10);
    return { from: iso(from), to: iso(to) };
  }

  async function loadStats() {
    const boite = $('adminStats');
    boite.innerHTML = `<p class="text-sm text-slate-500">${esc(T('admin.stats.loading'))}</p>`;
    if (!adminState.statsRange) adminState.statsRange = defaultStatsRange();
    try {
      const range = adminState.statsRange;
      const qs = new URLSearchParams({ date_from: range.from, date_to: range.to });
      if (adminState.statsOwner) qs.set('owner', adminState.statsOwner);
      // Le journal est paginé : on n'en charge que la première page ici,
      // les suivantes arrivent par /api/admin/usage/log (voir loadLog).
      const logQs = new URLSearchParams(qs);
      logQs.set('offset', '0');
      logQs.set('limit', String(LOG_PAGE_SIZE));
      const [usageData, pricingData, logData] = await Promise.all([
        api(`/api/admin/usage?${qs}`),
        api('/api/admin/pricing'),
        api(`/api/admin/usage/log?${logQs}`),
      ]);
      adminState.stats = { ...usageData, log: logData };
      adminState.pricing = pricingData.rates || [];
      adminState.logPage = 0;
      renderStats();
    } catch (err) {
      boite.innerHTML = `<p class="text-sm text-red-600">${esc(err.message)}</p>`;
    }
  }

  /** Recharge UNE page du journal sans reconstruire tout l'onglet. */
  async function loadLog() {
    const range = adminState.statsRange;
    const qs = new URLSearchParams({ date_from: range.from, date_to: range.to });
    if (adminState.statsOwner) qs.set('owner', adminState.statsOwner);
    qs.set('offset', String(adminState.logPage * LOG_PAGE_SIZE));
    qs.set('limit', String(LOG_PAGE_SIZE));
    try {
      const data = await api(`/api/admin/usage/log?${qs}`);
      if (!adminState.stats) adminState.stats = {};
      adminState.stats.log = data;
      const zone = $('logSection');
      if (zone) {
        zone.innerHTML = renderLog();
        bindLogPager();
      }
    } catch (err) {
      const zone = $('logSection');
      if (zone) zone.innerHTML = `<p class="text-sm text-red-600">${esc(err.message)}</p>`;
    }
  }

  function changeLogPage(delta) {
    const log = (adminState.stats && adminState.stats.log) || null;
    const totalPages = log ? Math.max(1, Math.ceil(log.total / (log.limit || LOG_PAGE_SIZE))) : 1;
    adminState.logPage = Math.min(Math.max(0, adminState.logPage + delta), totalPages - 1);
    loadLog();
  }

  function bindLogPager() {
    const prev = $('logPrev');
    const next = $('logNext');
    if (prev) prev.addEventListener('click', () => changeLogPage(-1));
    if (next) next.addEventListener('click', () => changeLogPage(1));
  }

  /** Filtre de période + usager, commun au journal et au détail agrégé. */
  function renderStatsFilter() {
    const data = adminState.stats || { total_cost: 0, currency: 'USD' };
    const owners = [...new Set((adminState.stats && adminState.stats.overview
      ? adminState.stats.overview.rows : []).map((r) => r.owner))].sort();
    const options = owners.map((o) =>
      `<option value="${esc(o)}" ${o === adminState.statsOwner ? 'selected' : ''}>${esc(o)}</option>`).join('');
    return `
      <div class="flex flex-wrap items-end gap-3">
        <label class="text-xs text-slate-600">${esc(T('admin.stats.date_from'))}
          <input type="date" id="statsFrom" value="${esc(adminState.statsRange.from)}"
                 class="block border border-slate-300 rounded px-2 py-1 text-sm">
        </label>
        <label class="text-xs text-slate-600">${esc(T('admin.stats.date_to'))}
          <input type="date" id="statsTo" value="${esc(adminState.statsRange.to)}"
                 class="block border border-slate-300 rounded px-2 py-1 text-sm">
        </label>
        ${options ? `<label class="text-xs text-slate-600">${esc(T('admin.stats.col_owner'))}
          <select id="statsOwner" class="block border border-slate-300 rounded px-2 py-1 text-sm">
            <option value="">${esc(T('admin.stats.owner_all'))}</option>
            ${options}
          </select></label>` : ''}
        <span class="ml-auto text-sm font-medium">${esc(T('admin.stats.total_cost'))} :
          ${data.total_cost.toFixed(2)} $</span>
      </div>`;
  }

  /**
   * Récapitulatif par usager : notes générées ET coût, sur les trois derniers
   * mois calendaires, l'année en cours et l'année précédente. Indépendant de
   * la plage de dates choisie pour le journal. Tableau sur grand écran,
   * cartes empilées sur mobile.
   */
  function renderNotesCostOverview() {
    const overview = (adminState.stats && adminState.stats.overview) || null;
    if (!overview || !overview.rows.length) return '';

    const cellule = (notes, cout) => `
      <td class="px-2 py-1.5 text-right tabular-nums">${notes}
        <span class="text-[11px] text-slate-400">${esc(T('admin.stats.notes_short'))}</span><br>
        <span class="text-[11px] text-slate-500">${cout ? cout.toFixed(2) : '—'} $</span></td>`;
    const periode = (label, notes, cout) => `
      <div class="flex items-baseline justify-between gap-2 text-sm">
        <span class="text-slate-500">${label}</span>
        <span class="tabular-nums">${notes}
          <span class="text-[11px] text-slate-400">${esc(T('admin.stats.notes_short'))}</span>
          · ${cout ? cout.toFixed(2) : '—'}
          <span class="text-[11px] text-slate-400">$</span></span>
      </div>`;

    const lignesTable = overview.rows.map((r) => `
      <tr class="border-b border-slate-100">
        <td class="px-2 py-1.5">${esc(r.owner)}</td>
        ${r.month_notes.map((n, i) => cellule(n, r.month_costs[i])).join('')}
        ${r.year_notes.map((n, i) => cellule(n, r.year_costs[i])).join('')}
      </tr>`).join('');
    const cartesMobile = overview.rows.map((r) => `
      <div class="rounded-lg border border-slate-200 p-3 space-y-1.5">
        <p class="text-sm font-medium text-slate-700">${esc(r.owner)}</p>
        ${overview.months.map((m, i) => periode(monthName(m.year, m.month), r.month_notes[i], r.month_costs[i])).join('')}
        ${overview.years.map((a, i) => periode(`${a}`, r.year_notes[i], r.year_costs[i])).join('')}
      </div>`).join('');

    return `
      <section class="space-y-2">
        <h3 class="text-sm font-semibold text-slate-700">${esc(T('admin.stats.overview_title'))}</h3>
        <div class="hidden sm:block overflow-auto rounded-lg border border-slate-200">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-xs text-slate-500">
              <tr>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_owner'))}</th>
                ${overview.months.map((m) => `<th class="px-2 py-1.5 text-right">${esc(monthName(m.year, m.month))}</th>`).join('')}
                ${overview.years.map((a) => `<th class="px-2 py-1.5 text-right">${a}</th>`).join('')}
              </tr>
            </thead>
            <tbody>${lignesTable}</tbody>
          </table>
        </div>
        <div class="sm:hidden space-y-3">${cartesMobile}</div>
      </section>`;
  }

  /**
   * Journal des générations : une ligne par appel LLM, une ligne résumée par
   * dictée pour le STT. Respecte la plage de dates et le filtre d'usager, et
   * n'affiche qu'une page à la fois (pagination serveur via loadLog).
   */
  function renderLog() {
    const log = (adminState.stats && adminState.stats.log) || null;
    const entries = (log && log.entries) || [];

    const typeBadge = (kind) => kind === 'stt'
      ? `<span class="text-[11px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700">${esc(T('admin.stats.kind.stt'))}</span>`
      : `<span class="text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">${esc(T('admin.stats.kind.llm'))}</span>`;

    const usageText = (e) => {
      if (e.kind === 'stt') {
        const min = e.audio_seconds ? (e.audio_seconds / 60).toFixed(1) : '—';
        return e.segments ? `${min} min · ${e.segments} ${esc(T('admin.stats.segments'))}` : `${min} min`;
      }
      const total = (e.prompt_tokens || 0) + (e.output_tokens || 0) + (e.audio_prompt_tokens || 0);
      return total ? `${e.prompt_tokens}${e.audio_prompt_tokens ? `+${e.audio_prompt_tokens}♪` : ''}/${e.output_tokens}` : '—';
    };
    const consultation = (e) => {
      if (!e.consultation_id) return '—';
      const nom = e.consultation_title ? ` — ${esc(e.consultation_title)}` : '';
      return `#${e.consultation_id}${nom}`;
    };
    const cout = (e) => (e.cost != null ? `${e.cost.toFixed(4)} $` : '—');

    const lignesTable = entries.map((e) => `
      <tr class="border-b border-slate-100">
        <td class="px-2 py-1.5 whitespace-nowrap tabular-nums">${esc(formatDateTime(e.created_at))}</td>
        <td class="px-2 py-1.5">${esc(e.owner)}</td>
        <td class="px-2 py-1.5">${typeBadge(e.kind)}</td>
        <td class="px-2 py-1.5 text-slate-500">${consultation(e)}</td>
        <td class="px-2 py-1.5">${esc(e.provider)}${e.model ? ` <span class="text-slate-500">/ ${esc(e.model)}</span>` : ''}</td>
        <td class="px-2 py-1.5 tabular-nums">${usageText(e)}</td>
        <td class="px-2 py-1.5 tabular-nums text-right">${cout(e)}</td>
      </tr>`).join('');
    const cartesMobile = entries.map((e) => `
      <div class="rounded-lg border border-slate-200 p-3 space-y-1.5">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500 tabular-nums">${esc(formatDateTime(e.created_at))}</span>
          <span class="flex items-center gap-2">${typeBadge(e.kind)}<span class="text-sm font-medium tabular-nums">${cout(e)}</span></span>
        </div>
        <p class="text-sm font-medium text-slate-700">${esc(e.owner)}</p>
        <p class="text-xs text-slate-500">${consultation(e)}</p>
        <p class="text-xs text-slate-600">${esc(e.provider)}${e.model ? ` / ${esc(e.model)}` : ''}</p>
        <p class="text-xs text-slate-600 tabular-nums">${usageText(e)}</p>
      </div>`).join('');

    const vide = `<tr><td colspan="7" class="px-2 py-6 text-center text-slate-400">${esc(T('admin.stats.empty'))}</td></tr>`;

    return `
      <section class="space-y-2">
        <h3 class="text-sm font-semibold text-slate-700">${esc(T('admin.stats.log_title'))}</h3>
        <div id="logSection" class="space-y-2">
          <div class="hidden sm:block overflow-auto rounded-lg border border-slate-200">
            <table class="w-full text-sm">
              <thead class="bg-slate-50 text-xs text-slate-500">
                <tr>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_date'))}</th>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_owner'))}</th>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_kind'))}</th>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_consultation'))}</th>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_model'))}</th>
                  <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_usage'))}</th>
                  <th class="px-2 py-1.5 text-right">${esc(T('admin.stats.col_cost'))}</th>
                </tr>
              </thead>
              <tbody>${entries.length ? lignesTable : vide}</tbody>
            </table>
          </div>
          <div class="sm:hidden space-y-3">${entries.length ? cartesMobile : `<p class="text-sm text-center text-slate-400 py-6">${esc(T('admin.stats.empty'))}</p>`}</div>
          ${renderLogPager()}
          <p class="text-[11px] text-slate-400">${esc(T('admin.stats.log_retention_note'))}</p>
        </div>
      </section>`;
  }

  /** Pagination du journal : Précédent / page / Suivant, désactivés aux bornes. */
  function renderLogPager() {
    const log = (adminState.stats && adminState.stats.log) || null;
    if (!log || !log.total) return '';
    const totalPages = Math.max(1, Math.ceil(log.total / (log.limit || LOG_PAGE_SIZE)));
    const page = Math.min(adminState.logPage, totalPages - 1);
    const desactive = 'disabled opacity-40 cursor-default';
    return `
      <nav class="flex items-center justify-between gap-3 pt-1 text-sm">
        <button type="button" id="logPrev" class="px-3 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50 ${page === 0 ? desactive : ''}">${esc(T('admin.stats.log_prev'))}</button>
        <span class="text-xs text-slate-500 tabular-nums">${esc(T('admin.stats.log_page', { page: page + 1, pages: totalPages, total: log.total }))}</span>
        <button type="button" id="logNext" class="px-3 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50 ${page >= totalPages - 1 ? desactive : ''}">${esc(T('admin.stats.log_next'))}</button>
      </nav>`;
  }

  /** Détail agrégé coût/jetons par usager × fournisseur × modèle, replié. */
  function renderBreakdownDetails() {
    const data = adminState.stats || { rows: [], total_cost: 0, currency: 'USD' };
    const lignes = data.rows.map((r) => `
      <tr class="border-b border-slate-100">
        <td class="px-2 py-1.5">${esc(r.owner)}</td>
        <td class="px-2 py-1.5">${esc(r.provider)}</td>
        <td class="px-2 py-1.5 text-slate-500">${esc(r.model || '—')}</td>
        <td class="px-2 py-1.5">${esc(T(`admin.stats.kind.${r.kind}`))}</td>
        <td class="px-2 py-1.5 tabular-nums">${r.event_count || 0}</td>
        <td class="px-2 py-1.5 tabular-nums">${(r.prompt_tokens + r.output_tokens + (r.audio_prompt_tokens || 0)) ? `${r.prompt_tokens}${r.audio_prompt_tokens ? `+${r.audio_prompt_tokens}♪` : ''}/${r.output_tokens}` : '—'}</td>
        <td class="px-2 py-1.5 tabular-nums">${r.audio_seconds ? (r.audio_seconds / 60).toFixed(1) : '—'}</td>
        <td class="px-2 py-1.5 tabular-nums">${r.cost.toFixed(4)} $</td>
      </tr>`).join('');
    return `
      <details class="rounded-lg border border-slate-200">
        <summary class="px-3 py-2 text-sm font-semibold text-slate-700 cursor-pointer hover:bg-slate-50">${esc(T('admin.stats.breakdown_title'))}</summary>
        <div class="overflow-auto border-t border-slate-100">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-xs text-slate-500">
              <tr>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_owner'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_provider'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_model'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_kind'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_events'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_tokens'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_audio'))}</th>
                <th class="px-2 py-1.5 text-left">${esc(T('admin.stats.col_cost'))}</th>
              </tr>
            </thead>
            <tbody>${lignes || `<tr><td colspan="8" class="px-2 py-6 text-center text-slate-400">${esc(T('admin.stats.empty'))}</td></tr>`}</tbody>
          </table>
        </div>
      </details>`;
  }

  const PRICING_UNITS = ['token_input_1m', 'token_output_1m', 'token_audio_input_1m', 'audio_minute'];

  /** Tableau des tarifs, sans son titre (porté par le <details> de l'onglet). */
  function renderPricingTable() {
    const rates = adminState.pricing || [];
    const lignes = rates.map((r) => `
      <tr class="border-b border-slate-100" data-pricing-row="${r.id}">
        <td class="px-2 py-1.5">${esc(r.provider)}</td>
        <td class="px-2 py-1.5 text-slate-500">${esc(r.model || '—')}</td>
        <td class="px-2 py-1.5">${esc(T(`admin.stats.kind.${r.kind}`))}</td>
        <td class="px-2 py-1.5">${esc(T(`admin.stats.unit.${r.unit}`))}</td>
        <td class="px-2 py-1.5">
          <input type="number" step="0.0001" value="${r.rate}" data-pricing-rate
                 class="w-24 border border-slate-300 rounded px-1.5 py-0.5 text-sm">
        </td>
        <td class="px-2 py-1.5">
          <button type="button" data-save-pricing="${r.id}"
                  class="text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-50">${esc(T('admin.save'))}</button>
          <button type="button" data-delete-pricing="${r.id}"
                  class="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50">${esc(T('admin.backup.delete'))}</button>
        </td>
      </tr>`).join('');

    return `
      <div class="overflow-auto rounded-lg border border-slate-200">
        <table class="w-full text-sm">
          <tbody>${lignes}</tbody>
        </table>
      </div>
      <!-- Un <div>, jamais un <form> : cette section vit à l'intérieur de
           #adminForm (le formulaire du panneau admin, voir index.html) — un
           <form> imbriqué dans un autre est invalide en HTML, le navigateur
           l'ignore silencieusement, et le bouton ne déclenche alors plus
           rien. -->
      <div id="formAddPricing" class="flex flex-wrap items-end gap-2">
        <input name="provider" required placeholder="${esc(T('admin.stats.pricing_provider'))}"
               class="border border-slate-300 rounded px-2 py-1 text-sm w-32">
        <input name="model" placeholder="${esc(T('admin.stats.pricing_model'))}"
               class="border border-slate-300 rounded px-2 py-1 text-sm w-40">
        <select name="kind" class="border border-slate-300 rounded px-2 py-1 text-sm">
          <option value="llm">${esc(T('admin.stats.kind.llm'))}</option>
          <option value="stt">${esc(T('admin.stats.kind.stt'))}</option>
        </select>
        <select name="unit" class="border border-slate-300 rounded px-2 py-1 text-sm">
          ${PRICING_UNITS.map((u) => `<option value="${u}">${esc(T(`admin.stats.unit.${u}`))}</option>`).join('')}
        </select>
        <input name="rate" type="number" step="0.0001" required placeholder="${esc(T('admin.stats.pricing_rate'))}"
               class="border border-slate-300 rounded px-2 py-1 text-sm w-24">
        <button type="button" id="btnAddPricing" class="accent-btn px-3 py-1.5 rounded-lg text-sm font-medium">${esc(T('admin.stats.pricing_add'))}</button>
      </div>`;
  }

  /** Les tarifs dans un <details> replié : la masse CRUD n'écrase plus l'onglet. */
  function renderPricingDetails() {
    return `
      <details class="rounded-lg border border-slate-200">
        <summary class="px-3 py-2 text-sm font-semibold text-slate-700 cursor-pointer hover:bg-slate-50">${esc(T('admin.stats.pricing_title'))}</summary>
        <div class="p-3 space-y-3 border-t border-slate-100">${renderPricingTable()}</div>
      </details>`;
  }

  function renderStats() {
    const boite = $('adminStats');
    boite.innerHTML =
      renderStatsFilter() +
      renderNotesCostOverview() +
      renderLog() +
      renderBreakdownDetails() +
      renderPricingDetails();

    ['statsFrom', 'statsTo'].forEach((id) => {
      $(id).addEventListener('change', () => {
        adminState.statsRange = { from: $('statsFrom').value, to: $('statsTo').value };
        loadStats();
      });
    });
    const selectOwner = $('statsOwner');
    if (selectOwner) {
      selectOwner.addEventListener('change', () => {
        adminState.statsOwner = selectOwner.value;
        loadStats();
      });
    }
    bindLogPager();

    // Tarifs : sauvegarde/suppression/ajout — la section vit dans un
    // <details>, les écouteurs parcourent tout le conteneur comme avant.
    boite.querySelectorAll('[data-save-pricing]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        const id = bouton.dataset.savePricing;
        const row = adminState.pricing.find((r) => String(r.id) === id);
        if (!row) return;
        const rateInput = boite.querySelector(`tr[data-pricing-row="${id}"] [data-pricing-rate]`);
        try {
          await api(`/api/admin/pricing/${id}`, {
            method: 'PUT',
            body: { ...row, rate: Number(rateInput.value) },
          });
          toast(T('admin.save'), 'success');
          await loadStats();
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
      });
    });

    boite.querySelectorAll('[data-delete-pricing]').forEach((bouton) => {
      bouton.addEventListener('click', async () => {
        if (!window.confirm(T('admin.stats.pricing_delete_confirm'))) return;
        try {
          await api(`/api/admin/pricing/${bouton.dataset.deletePricing}`, { method: 'DELETE' });
          await loadStats();
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
      });
    });

    const zoneAjout = $('formAddPricing');
    const btnAjout = $('btnAddPricing');
    if (zoneAjout && btnAjout) {
      btnAjout.addEventListener('click', async () => {
        const champ = (nom) => zoneAjout.querySelector(`[name="${nom}"]`).value;
        const provider = champ('provider').trim();
        const rateBrut = champ('rate');
        if (!provider || rateBrut === '') {
          zoneAjout.querySelector('[name="provider"]').reportValidity();
          return;
        }
        try {
          await api('/api/admin/pricing', {
            method: 'POST',
            body: {
              provider,
              model: champ('model').trim(),
              kind: champ('kind'),
              unit: champ('unit'),
              rate: Number(rateBrut),
              currency: 'USD',
            },
          });
          await loadStats();
        } catch (err) {
          toast(err.message, 'error', 9000);
        }
      });
    }
  }

  async function openAdminModal() {
    $('adminModal').classList.remove('hidden');
    $('adminStatus').textContent = '';
    // Rechargé à chaque ouverture : les comptes peuvent avoir changé ailleurs.
    adminState.people = null;
    adminState.backups = null;
    adminState.stats = null;
    adminState.pricing = null;
    adminState.statsOwner = '';
    adminState.dirty = new Set();
    adminState.values = {};
    adminState.subTab = {};
    $('adminFields').innerHTML = `<p class="text-sm text-slate-500">${esc(T('admin.loading'))}</p>`;
    try {
      const data = await api('/api/admin/settings');
      adminState.fields = data.settings || [];
      adminState.dirty = new Set();
      renderAdminFields(data.groups);
    } catch (err) {
      $('adminFields').innerHTML =
        `<p class="text-sm text-red-600">${esc(err.message)}</p>`;
    }
  }

  async function saveAdminSettings() {
    if (!adminState.dirty.size) {
      $('adminStatus').textContent = T('admin.nothing_to_save');
      return;
    }
    // Depuis l'état et non depuis le DOM : un champ rempli sous un autre
    // sous-onglet n'est plus à l'écran, et sa valeur serait perdue.
    const values = {};
    adminState.dirty.forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(adminState.values, key)) {
        values[key] = adminState.values[key];
        return;
      }
      const element = $('adminFields').querySelector(`[data-key="${key}"]`);
      if (element) values[key] = element.value;
    });

    $('adminStatus').textContent = T('admin.saving');
    try {
      const result = await api('/api/admin/settings', { method: 'PUT', body: { values } });

      adminState.fields = result.settings || [];
      adminState.dirty = new Set();
      adminState.values = {};
      renderAdminFields(result.groups || null);
      $('adminStatus').textContent = T('admin.saved_count', { count: result.changed.length });
      toast(T('admin.applied'), 'success');
      // Le bandeau et les cadences du client dépendent de ces valeurs.
      await refreshClientConfig();
    } catch (err) {
      $('adminStatus').textContent = '';
      toast(err.message, 'error', 10000);
    }
  }

  /** Interroge le fournisseur sélectionné et propose ses modèles dans le champ. */
  async function listAvailableModels() {
    // Le sélecteur de service n'est pas rendu (le sous-menu le remplace) :
    // c'est le service CONSULTÉ qu'il faut interroger, pas nécessairement
    // l'actif — sans quoi ce bouton renseignait toujours les modèles du
    // fournisseur en service, même en visitant l'onglet d'un autre.
    const provider = currentProvider('group.llm');
    $('adminStatus').textContent = T('admin.querying');
    try {
      const data = await api(`/api/models?provider=${encodeURIComponent(provider)}`);
      // Une seule liste, mais rattachée aux DEUX champs de modèle du
      // fournisseur interrogé — le principal et le rapide portent tous deux
      // ``list="modelOptions"`` depuis leur rendu (voir LLM_MODEL_FIELD_KEYS),
      // il suffit donc de remplir le datalist partagé.
      const datalist = $('modelOptions');
      datalist.innerHTML = (data.models || [])
        .map((name) => `<option value="${esc(name)}"></option>`).join('');

      $('adminStatus').textContent = T('admin.models_listed', { count: data.models.length });
      if (!data.configured_available) {
        toast(T('admin.model_missing', { model: data.configured }), 'warning', 10000);
      }
      if (data.fast_model && !data.fast_model_available) {
        toast(T('admin.fast_model_missing', { model: data.fast_model }), 'warning', 10000);
      }
    } catch (err) {
      $('adminStatus').textContent = '';
      toast(err.message, 'error', 10000);
    }
  }

  /* =========================================================================
   * 8 quater. IDENTITÉ ET DÉCONNEXION
   * ======================================================================
   * Une seule route serveur, /auth/logout (main.auth_logout) : elle ferme la
   * session locale PUIS redirige vers le point de déconnexion OIDC du
   * fournisseur (RP-initiated logout). Le client n'a rien d'autre à faire
   * qu'y naviguer.
   * ====================================================================== */

  function toggleIdentityMenu(force) {
    const menu = $('identityMenu');
    const ouvrir = force === undefined ? menu.classList.contains('hidden') : force;
    menu.classList.toggle('hidden', !ouvrir);
    $('btnIdentity').setAttribute('aria-expanded', ouvrir ? 'true' : 'false');
    if (!ouvrir) $('logoutHint').classList.add('hidden');
    if (ouvrir) loadMyUsage();
  }

  /**
   * Récapitulatif d'usage personnel, 30 derniers jours — rechargé à chaque
   * ouverture du menu plutôt que mis en cache : c'est un coup d'œil
   * occasionnel, pas un écran qu'on garde ouvert, autant refléter l'instant.
   */
  async function loadMyUsage() {
    const boite = $('myUsage');
    if (!boite) return;
    boite.textContent = T('identity.usage.loading');
    try {
      const data = await api('/api/me/usage');

      const rendreMois = (mois) => {
        const morceaux = [];
        const tokens = (mois.prompt_tokens || 0) + (mois.output_tokens || 0) + (mois.audio_prompt_tokens || 0);
        if (tokens) morceaux.push(T('identity.usage.tokens', { count: tokens.toLocaleString() }));
        if (mois.audio_seconds) {
          morceaux.push(T('identity.usage.audio_minutes', { count: (mois.audio_seconds / 60).toFixed(1) }));
        }
        const lignes = morceaux.length
          ? `<p>${esc(morceaux.join(' · '))}</p>`
          + (mois.cost ? `<p class="mt-0.5 text-slate-500">${esc(T('identity.usage.cost', { amount: mois.cost.toFixed(2) }))}</p>` : '')
          : `<p class="text-slate-400">${esc(T('identity.usage.empty'))}</p>`;
        return `<div>
          <p class="text-[11px] text-slate-500 mb-1">${esc(T('identity.usage.month', { month: monthName(mois.year, mois.month) }))}</p>
          <div class="text-xs text-slate-600">${lignes}</div>
        </div>`;
      };

      boite.className = 'space-y-3';
      boite.innerHTML = rendreMois(data.current) + rendreMois(data.previous);
    } catch (err) {
      boite.textContent = '';
    }
  }

  function showLogoutHint(message, ton) {
    const hint = $('logoutHint');
    hint.textContent = message;
    hint.className = `px-3 pb-2 pt-1 text-[11px] leading-relaxed ${
      ton === 'error' ? 'text-red-700' : 'text-slate-500'}`;
  }

  /**
   * Déconnexion.
   *
   * Une navigation de premier niveau (pas un fetch) : /auth/logout se termine
   * par une redirection 302 vers le fournisseur OIDC, que seule une vraie
   * navigation du navigateur peut suivre jusqu'au bout.
   */
  async function logout() {
    if (state.recording) {
      showLogoutHint(T('identity.logout_busy'), 'error');
      return;
    }
    showLogoutHint(T('identity.logout_progress'));
    // Navigation de premier niveau vers notre propre route : elle ferme la
    // session locale PUIS renvoie au fournisseur. L'ordre compte — voir
    // main.auth_logout.
    window.location.href = state.logoutUrl || '/auth/logout';
  }

  /* -------------------------------------------------------------------------
   * Choix de la langue
   * ----------------------------------------------------------------------
   * Préférence personnelle, enregistrée côté serveur sous l'identité
   * authentifiée plutôt que dans le témoin de session : elle doit survivre à
   * l'expiration du témoin et suivre l'usager d'un appareil à l'autre (voir
   * database.UserPreference).
   *
   * Le rechargement est nécessaire et non un raccourci : toute l'interface est
   * rendue par le serveur avec le catalogue de la langue courante, et les
   * consignes envoyées au modèle en dépendent aussi. Il est sans danger — le
   * brouillon est déjà en base — sauf pendant une dictée, d'où le garde-fou.
   * ---------------------------------------------------------------------- */

  function renderLanguageChoices(langues, courante) {
    const boite = $('languageChoices');
    if (!boite) return;

    boite.innerHTML = (langues || []).map((langue, index) => {
      const actif = langue.value === courante;
      const bordure = index === 0 ? '' : 'border-l border-slate-200';
      const fond = actif
        ? 'accent-tab font-medium'
        : 'hover:bg-slate-50 text-slate-600';
      return `<button type="button" role="menuitem" data-lang="${esc(langue.value)}"
                      aria-current="${actif ? 'true' : 'false'}"
                      class="px-3 py-1.5 transition ${bordure} ${fond}">
                ${esc(langue.label)}</button>`;
    }).join('');

    boite.querySelectorAll('button[data-lang]').forEach((bouton) => {
      bouton.addEventListener('click', () => setLanguage(bouton.dataset.lang));
    });
  }

  async function setLanguage(langue) {
    if (langue === LANG) {
      toggleIdentityMenu(false);
      return;
    }
    if (state.recording) {
      showLogoutHint(T('identity.logout_busy'), 'error');
      return;
    }
    try {
      await api('/api/me/language', { method: 'PUT', body: { language: langue } });
      showLogoutHint(T('identity.language_saved'));
      window.location.reload();
    } catch (err) {
      showLogoutHint(T('identity.language_failed', { error: err.message }), 'error');
    }
  }

  /* ------ Thème de couleur ------ */

  /**
   * Applique un thème en posant l'attribut ``data-theme`` sur ``<html>``,
   * ce qui active le bloc de variables CSS correspondant dans la feuille
   * de style. Sans rechargement de page.
   */
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme || 'teal');
    state.accentWaveColor = accentColor('--color-accent-wave') || '#14b8a6';
  }

  function renderThemeChoices(themes, courant) {
    const boite = $('themeChoices');
    if (!boite) return;

    boite.innerHTML = (themes || []).map((couleur) => {
      const actif = couleur.value === courant;
      const ring = actif
        ? 'border-slate-700 shadow-[inset_0_0_0_1px_rgba(255,255,255,.45)]'
        : 'border-transparent hover:border-slate-400';
      const etiquette = LANG === 'en' ? couleur.label_en : couleur.label_fr;
      return `<button type="button" data-theme="${esc(couleur.value)}"
                      title="${esc(etiquette)}" aria-label="${esc(etiquette)}"
                      aria-current="${actif ? 'true' : 'false'}"
                      class="w-5 h-5 rounded-full border-2 transition ${ring}"
                      style="background-color:${esc(couleur.hex || '#0f766e')}"></button>`;
    }).join('');

    boite.querySelectorAll('button[data-theme]').forEach((bouton) => {
      bouton.addEventListener('click', () => setTheme(bouton.dataset.theme));
    });
  }

  async function setTheme(theme) {
    if (theme === (document.documentElement.getAttribute('data-theme') || 'teal')) {
      toggleIdentityMenu(false);
      return;
    }
    try {
      const resp = await api('/api/me/theme', { method: 'PUT', body: { theme } });
      applyTheme(resp.theme);
      renderThemeChoices(resp.themes, resp.theme);
      toggleIdentityMenu(false);
    } catch (err) {
      toast(T('identity.language_failed', { error: err.message }), 'error');
    }
  }

  /* =========================================================================
   * 9. INITIALISATION
   * ====================================================================== */

  /**
   * Lit /api/config et en applique les conséquences visibles.
   *
   * Rappelée après un enregistrement dans le panneau : le fournisseur affiché
   * dans le pied de page et les cadences de dictée viennent de là.
   */
  async function refreshClientConfig() {
    const config = await api('/api/config');
    state.isTemplateAdmin = config.is_template_admin;
    state.username = config.username || '';

    if (config.dictation_chunk_seconds) {
      dictationConfig.chunkSeconds = config.dictation_chunk_seconds;
    }
    if (config.dictation_segment_seconds) {
      dictationConfig.segmentSeconds = config.dictation_segment_seconds;
    }

    $('btnNewTemplate').classList.toggle('hidden', false);
    state.logoutUrl = config.logout_url || '/auth/logout';
    state.isAdmin = Boolean(config.is_admin);
    state.llmBypassStt = Boolean(config.llm_bypass_stt);
    state.llmBypassSttKeepTranscript = Boolean(config.llm_bypass_stt_keep_transcript);
    updateActionButtons();
    updateBypassSttNotice();
    renderLanguageChoices(config.languages, config.language || LANG);

    if (config.theme) applyTheme(config.theme);
    if (config.themes) renderThemeChoices(config.themes, config.theme || 'teal');

    // Le panneau est réservé aux administrateurs, pas à quiconque peut écrire
    // un gabarit : ce sont deux droits distincts depuis l'arrivée des groupes.
    $('btnAdmin').classList.toggle('hidden', !state.isAdmin);
    $('btnAdmin').classList.toggle('flex', state.isAdmin);

    // Quel moteur travaille réellement : la question se pose dès qu'une note
    // sort différente de d'habitude, et la réponse n'était nulle part.
    const label = $('engineLabel');
    if (label) {
      const stt = config.stt_provider === 'custom' && config.stt_model
        ? config.stt_model
        : config.stt_provider;
      // Contournement du STT actif : même s'il tourne encore pour
      // l'affichage pendant la dictée (voir llmBypassSttKeepTranscript), son
      // résultat n'entre JAMAIS dans la génération — la flèche « → »
      // suggérerait à tort qu'il fait partie de la chaîne qui produit la
      // note, entre parenthèses ça ne dit plus que « pour information ».
      const sttOutOfPipeline = state.llmBypassStt;
      label.textContent = sttOutOfPipeline
        ? `(${stt}) - ${config.llm_provider} · ${config.llm_model}`
        : `${stt} → ${config.llm_provider} · ${config.llm_model}`;
    }
    return config;
  }

  /* =========================================================================
   * 9 bis. DIFFUSION EN DIRECT (SSE)
   * ========================================================================
   * Un même médecin ouvre parfois la même consultation sur deux appareils à
   * la fois (téléphone en dictée, bureau en lecture). Un seul flux SSE par
   * onglet, ouvert une fois pour toute la session, reçoit tout ce que
   * live.publish() (voir app/live.py) diffuse pour cet usager : nouvelle
   * tranche dictée, note générée, enregistrement ajouté/retiré, brouillon
   * modifié ailleurs. Chaque évènement porte de quoi reconnaître ses propres
   * écritures (voir X-ConsultAI-Tab dans api()) pour ne jamais se les
   * appliquer à soi-même une seconde fois.
   * ====================================================================== */

  let liveSource = null;

  /** Y a-t-il, dans CET onglet, quelque chose qu'un rechargement ferait perdre ? */
  function hasUnsavedChanges() {
    return dictation.active || workspaceSnapshot() !== state.lastSavedSnapshot;
  }

  /**
   * Bloque l'édition et impose un rechargement.
   *
   * Volontairement sans échappatoire (pas de croix, pas de clic en dehors —
   * voir la modale dans index.html) : la seule sortie est « Recharger », qui
   * part sur la version la plus récente et abandonne ce que cet onglet avait
   * en cours. Fermer la modale sans agir laisserait croire que tout va bien
   * jusqu'à la prochaine sauvegarde automatique, qui écraserait alors en
   * silence ce que l'autre appareil vient d'écrire.
   */
  function showBlockingSyncModal(messageKey) {
    $('syncModalMessage').textContent = T(messageKey);
    $('syncModal').classList.remove('hidden');
  }

  function hideBlockingSyncModal() {
    $('syncModal').classList.add('hidden');
  }

  function onSyncTranscript(evt) {
    const payload = JSON.parse(evt.data);
    if (dictation.sessionId && payload.session_id === dictation.sessionId) return;
    if (String(payload.consultation_id) !== String(state.consultationId)) return;
    if (!payload.text) return;

    if (hasUnsavedChanges()) {
      showBlockingSyncModal('sync.conflict_transcript');
      return;
    }
    const box = $('transcript');
    const existing = box.value.replace(/\s+$/, '');
    box.value = formatSentences(existing ? `${existing} ${payload.text}` : payload.text);
    scrollTranscriptToBottom();
    updateTranscriptMeta({ duration_seconds: payload.audio_seconds });
    updateActionButtons();
    // Le texte poussé est déjà durable côté serveur : ne pas le marquer
    // « à sauvegarder », sinon la prochaine sauvegarde automatique renverrait
    // inutilement ce qui vient d'arriver.
    state.lastSavedSnapshot = workspaceSnapshot();
  }

  function onSyncRecording(evt) {
    const payload = JSON.parse(evt.data);
    if (payload.origin_tab === state.tabId) return;
    if (String(payload.consultation_id) === String(state.consultationId)) loadRecordings();
  }

  function onSyncGeneratedOrPatched(evt) {
    const payload = JSON.parse(evt.data);
    if (payload.origin_tab === state.tabId) return;
    if (String(payload.consultation_id) !== String(state.consultationId)) {
      if (evt.type === 'consultation_patched' && !$('draftsModal').classList.contains('hidden')) {
        openDraftsModal();
      }
      return;
    }
    if (hasUnsavedChanges()) {
      showBlockingSyncModal('sync.conflict_generic');
      return;
    }
    loadDraft(state.consultationId);
    if (!$('draftsModal').classList.contains('hidden')) openDraftsModal();
  }

  /**
   * Un morceau du texte de la note, diffusé par le serveur pendant que
   * /api/generate attend encore. Seuls les morceaux de CETTE tentative
   * (même jeton de corrélation, même brouillon) sont appliqués — un flux
   * supplanté par un clic de régénération, ou un flux d'un autre onglet,
   * est ignoré sans effet. La réponse JSON finale de /api/generate reste la
   * source de vérité : elle remplace ce texte brut par le texte définitif.
   */
  function onGenerationChunk(evt) {
    if (!pendingGenerate) return;
    const payload = JSON.parse(evt.data || '{}');
    if (!payload.generation_token || payload.generation_token !== state.generationToken) return;
    if (String(payload.consultation_id) !== String(state.consultationId)) return;

    $('markdownEditor').value = payload.markdown || '';
    renderMarkdown();
    // On suit toujours la fin du texte en cours de génération, quelle que
    // soit la vue active (Aperçu ou Éditer).
    if (state.editingMarkdown) {
      $('markdownEditor').scrollTop = $('markdownEditor').scrollHeight;
    } else {
      const pane = $('previewPane');
      pane.scrollTop = pane.scrollHeight;
      positionGenBar();
    }
  }

  function onSyncConsultationCreated() {
    if (!$('draftsModal').classList.contains('hidden')) openDraftsModal();
  }

  /**
   * Une dictée démarre ailleurs (autre onglet, autre appareil) sur une
   * consultation qu'on ne regarde pas déjà : proposer d'y basculer pour la
   * suivre en direct, plutôt que de laisser le médecin la découvrir plus
   * tard dans « Mes brouillons ». Celle qu'on regarde déjà se met à jour
   * toute seule (voir onSyncTranscript) — rien à proposer dans ce cas.
   *
   * Le toast est persistant (durationMs = 0) : une invitation qui disparaît
   * au bout de 12 s obligeait à recharger la page quand on n'avait pas les
   * yeux dessus. Le bandeau de récupération, lui aussi rafraîchi ici, garde
   * une trace durable de la dictée à suivre.
   */
  function onSyncDictationStarted(evt) {
    const payload = JSON.parse(evt.data);
    if (payload.origin_tab === state.tabId) return;
    refreshRecoveryBanner();
    if (String(payload.consultation_id) === String(state.consultationId)) return;
    toastWithAction(
      T('sync.dictation_started', { title: payload.title }),
      T('sync.follow'),
      () => loadDraft(payload.consultation_id),
      0,
    );
  }

  /**
   * La dictée suivie (ou démarrée) ailleurs vient de se conclure ou d'être
   * abandonnée : la session a disparu du serveur, le bandeau « Suivre »
   * n'a plus de raison d'être — on le recalcule plutôt que d'attendre le
   * prochain chargement de page.
   */
  function onSyncDictationStopped(evt) {
    const payload = JSON.parse(evt.data);
    if (payload.origin_tab === state.tabId) return;
    refreshRecoveryBanner();
  }

  function onSyncConsultationDeleted(evt) {
    const payload = JSON.parse(evt.data);
    if (payload.origin_tab === state.tabId) return;
    if (!$('draftsModal').classList.contains('hidden')) openDraftsModal();
    if (String(payload.consultation_id) === String(state.consultationId)) {
      toast(T('sync.consultation_deleted'), 'warning', 8000);
      resetWorkspace();
    }
  }

  /** Ouvre (ou rouvre) le flux SSE de cet usager. Un seul à la fois. */
  function connectLiveEvents() {
    if (liveSource) liveSource.close();
    liveSource = new EventSource('/api/events');
    liveSource.addEventListener('transcript', onSyncTranscript);
    liveSource.addEventListener('dictation_started', onSyncDictationStarted);
    liveSource.addEventListener('dictation_stopped', onSyncDictationStopped);
    liveSource.addEventListener('recording_added', onSyncRecording);
    liveSource.addEventListener('recording_deleted', onSyncRecording);
    liveSource.addEventListener('generated', onSyncGeneratedOrPatched);
    liveSource.addEventListener('generation_chunk', onGenerationChunk);
    liveSource.addEventListener('consultation_patched', onSyncGeneratedOrPatched);
    liveSource.addEventListener('consultation_created', onSyncConsultationCreated);
    liveSource.addEventListener('consultation_deleted', onSyncConsultationDeleted);
    // EventSource se reconnecte déjà tout seul (avec le délai « retry: » du
    // serveur) : on se contente de journaliser plutôt que de dupliquer cette
    // logique.
    liveSource.onerror = () => console.warn('Flux en direct interrompu, reconnexion automatique en cours.');
  }

  async function init() {
    // Date du jour pré-remplie : c'est le cas de très loin le plus fréquent,
    // et une valeur déjà présente n'est jamais écrasée par l'extraction.
    $('metaDate').value = new Date().toISOString().slice(0, 10);

    // Rotation / changement de fenêtre pendant une génération : le témoin
    // adapté (pastille grand écran vs barre mobile) est re-évalué et la barre
    // mobile recalculée pour rester centrée sur la portion vide.
    window.addEventListener('resize', () => {
      if (state.generationToken !== null) setGenerating(true);
    });

    // --- Enregistrement ---
    $('btnRecord').addEventListener('click', startRecording);
    $('btnPause').addEventListener('click', togglePause);
    $('btnFinish').addEventListener('click', finishRecording);
    $('btnAbort').addEventListener('click', abortRecording);

    // --- Mode dictaphone (téléphone retourné) ---
    $('btnDictaphoneMain').addEventListener('click', () => {
      if (!state.recording) startRecording();
      else togglePause();
    });
    $('btnDictaphoneFinish').addEventListener('click', finishRecording);
    // iOS : pas de détection automatique (aucune permission demandée). Un
    // bouton « Mode retourné » affiche le calque RENVERSÉ de 180° — à se
    // lire à l'endroit une fois le téléphone retourné — et un ✕ permet d'en
    // sortir. Android garde l'auto-détection seule : boutons masqués.
    if (isIOSDevice) {
      const flipBtn = $('btnFlipMode');
      const exitBtn = $('btnDictaphoneExit');
      const hint = $('dictaphoneHint');
      if (flipBtn) {
        flipBtn.classList.remove('hidden');
        flipBtn.addEventListener('click', () => setDictaphone(!dphone.active, true));
      }
      if (exitBtn) {
        exitBtn.classList.remove('hidden');
        exitBtn.addEventListener('click', () => setDictaphone(false, false));
      }
      if (hint) hint.textContent = T('dictaphone.hint_manual');
    }

    // --- Import d'un fichier audio existant ---
    $('audioFileInput').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      await sendForTranscription(file, file.name);
      event.target.value = ''; // permet de réimporter le même fichier
    });

    // --- Gabarits ---
    $('templateSelect').addEventListener('change', () => {
      updateTemplateDescription();
      scheduleSave();
      updateActionButtons();
      // Le seul moment où l'on peut s'apercevoir que la dictée a été
      // transcrite dans la mauvaise langue.
      maybeOfferRetranscription();
    });
    const handleRetranscribeClick = () => {
      const tpl = currentTemplate();
      const langue = languageName(
        (tpl && tpl.language) ? tpl.language : state.transcriptLanguage,
      );
      runRetranscription(tpl, T('retranscribe.confirm_manual', { langue }));
    };
    $('btnRetranscribe').addEventListener('click', handleRetranscribeClick);
    $('btnRetranscribeMobile').addEventListener('click', handleRetranscribeClick);
    $('btnManageTemplates').addEventListener('click', openTemplatesModal);
    $('btnCloseTemplates').addEventListener('click', closeTemplatesModal);
    $('btnNewTemplate').addEventListener('click', () => {
      resetTemplateForm();
      renderTemplateList();
      setTemplateMobileView('form');
    });
    // Instructions et mise en page contiennent du Markdown que le modèle
    // reproduit tel quel ; le vocabulaire ne l'est pas, mais la correction
    // automatique y détruirait tout autant « périndopril » ou « CIUSSS ».
    enableMarkdownEditing($('tplInstructions'));
    enableMarkdownEditing($('tplLayout'));
    disableTextRewriting($('tplHints'));

    $('templateForm').addEventListener('submit', submitTemplateForm);
    $('btnDuplicateTemplate').addEventListener('click', duplicateTemplate);
    // Les bandeaux des gabarits protégés et des gabarits partagés en lecture
    // seule proposent la même action, mise en avant.
    $('btnDuplicateLocked').addEventListener('click', duplicateTemplate);
    $('btnDuplicateReadonly').addEventListener('click', duplicateTemplate);
    $('btnDeleteTemplate').addEventListener('click', deleteTemplate);

    // --- Brouillons ---
    $('btnDrafts').addEventListener('click', openDraftsModal);
    $('btnCloseDrafts').addEventListener('click', () => $('draftsModal').classList.add('hidden'));

    // --- Politique de confidentialité ---
    $('btnPrivacyPolicy').addEventListener('click', () => $('privacyModal').classList.remove('hidden'));
    $('btnClosePrivacy').addEventListener('click', () => $('privacyModal').classList.add('hidden'));

    // --- Synchronisation en direct ---
    $('btnSyncReload').addEventListener('click', () => {
      hideBlockingSyncModal();
      loadDraft(state.consultationId);
    });

    // --- Identité et déconnexion ---
    $('btnIdentity').addEventListener('click', (event) => {
      event.stopPropagation();
      toggleIdentityMenu();
    });
    $('btnLogout').addEventListener('click', logout);
    // Un clic ailleurs referme le menu ; le clic sur le menu lui-même ne doit
    // pas remonter jusque-là, sinon toute interaction le fermerait.
    $('identityMenu').addEventListener('click', (event) => event.stopPropagation());
    document.addEventListener('click', () => toggleIdentityMenu(false));

    // --- Panneau d'administration ---
    $('btnAdmin').addEventListener('click', openAdminModal);
    $('btnCloseAdmin').addEventListener('click', () => $('adminModal').classList.add('hidden'));
    $('btnSaveAdmin').addEventListener('click', saveAdminSettings);
    $('btnListModels').addEventListener('click', listAvailableModels);

    // --- Sélecteur de panneau (mobile) ---
    $('paneTabDictee').addEventListener('click', () => setMobilePane('dictee'));
    $('paneTabNote').addEventListener('click', () => setMobilePane('note'));
    $('btnBackToTemplateList').addEventListener('click', () => setTemplateMobileView('list'));

    // --- Génération et édition ---
    // Deux boutons « Mettre en forme » : celui de l'en-tête du panneau
    // (grand écran) et celui de la barre d'action basse (mobile), qui reste
    // atteignable même depuis l'onglet « Note structurée ».
    $('btnGenerate').addEventListener('click', generateNote);
    $('btnGenerateMobile').addEventListener('click', generateNote);
    // La corbeille supprime la consultation entière, des deux côtés.
    $('btnClearTranscript').addEventListener('click', deleteCurrentConsultation);
    $('btnClearTranscriptMobile').addEventListener('click', deleteCurrentConsultation);
    $('btnNew').addEventListener('click', newConsultation);
    $('tabPreview').addEventListener('click', showPreview);
    $('tabEdit').addEventListener('click', showEditor);

    $('markdownEditor').addEventListener('input', scheduleSave);
    Object.values(META_ELEMENTS).forEach((id) => {
      $(id).addEventListener('input', scheduleSave);
    });

    // --- Export ---
    $('btnCopyRich').addEventListener('click', copyRichText);
    $('btnCopyPlain').addEventListener('click', copyPlainText);
    $('btnCopyMd').addEventListener('click', copyMarkdown);
    $('btnPdf').addEventListener('click', exportPdf);

    // --- Fermeture des modales ---
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeTemplatesModal();
        $('draftsModal').classList.add('hidden');
        $('adminModal').classList.add('hidden');
        $('privacyModal').classList.add('hidden');
        toggleIdentityMenu(false);
      }
      // Ctrl/Cmd + S : sauvegarde immédiate
      if ((event.ctrlKey || event.metaKey) && event.key === 's') {
        event.preventDefault();
        scheduleSave();
      }
      // Ctrl/Cmd + Entrée : mise en forme
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        generateNote();
      }
    });
    [$('templatesModal'), $('draftsModal'), $('adminModal'), $('privacyModal')].forEach((modal) => {
      modal.addEventListener('click', (event) => {
        if (event.target === modal) modal.classList.add('hidden');
      });
    });

    // --- Chargement initial ---
    try {
      await refreshClientConfig();
      await loadTemplates();
    } catch (err) {
      toast(T('app.load_failed', { error: err.message }), 'error', 12000);
    }

    // Une dictée laissée en plan par un onglet fermé se signale d'elle-même.
    refreshRecoveryBanner().catch((err) => console.warn('Récupération :', err));

    connectLiveEvents();

    // Le réseau revient : la file d'attente repart sans attendre le prochain
    // fragment, qui pourrait ne jamais venir si le médecin est en pause.
    window.addEventListener('online', () => {
      dictation.failures = 0;
      pumpQueue();
      if (liveSource && liveSource.readyState === EventSource.CLOSED) connectLiveEvents();
    });

    updateRecordingUI();
    setupWaveform();
    showPreview();
    setMobilePane('dictee');
    updateActionButtons();

    // Raccourci « Nouvelle consultation » du manifeste (appui long sur
    // l'icône de l'écran d'accueil) : /?nouvelle=1
    if (new URLSearchParams(window.location.search).has('nouvelle')) {
      history.replaceState(null, '', '/');
    }

    registerServiceWorker();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
