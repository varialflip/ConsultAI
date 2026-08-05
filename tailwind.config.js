// Construit la feuille de style à l'image (voir Dockerfile, étape
// « tailwind-builder ») : cdn.tailwindcss.com compile en JIT dans CHAQUE
// navigateur à chaque chargement — pratique en développement, mais Tailwind
// lui-même déconseille cette voie en production (dépendance réseau externe
// à chaque visite, aucun cache, tout le poids du moteur JIT téléchargé pour
// rien). Un fichier déjà construit évite les trois.
//
// Le contenu scanné n'est PAS sémantique : Tailwind cherche des motifs de
// classe dans le texte brut de ces fichiers, y compris à l'intérieur des
// gabarits littéraux JavaScript — inutile de lister app.js séparément par
// fonction, un seul chemin suffit.
module.exports = {
  content: [
    './app/templates/**/*.html',
    './app/static/app.js',
  ],
  theme: {
    extend: {},
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
};
