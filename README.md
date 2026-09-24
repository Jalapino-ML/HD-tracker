# Suivi HD -- Suivi de stock Home Depot par magasin

> Duplique depuis Rona-Tracker le 2026-09-24. Meme moteur (dashboard.py / db.py),
> base de donnees et deploiement separes. Voir NOTES_DUPLICATION.md.

Relève la quantité de plusieurs articles dans plusieurs succursales, journalise chaque
changement, mesure la fraîcheur réelle de la source, alerte sur votre téléphone et expose
un tableau de bord local qui se rafraîchit tout seul.

## Workflow actuel (lisez-moi d'abord)

**La verification automatique par navigateur est desactivee.** RONA a mis en place une
protection anti-bot qui detecte les navigateurs pilotes automatiquement (Playwright), meme
en mode visible et avec des delais humains simules -- elle affiche alors une page
d'interstitiel ("Verification de securite en cours") au lieu du contenu. `scrape.py` et
`collect.py` restent dans le projet et fonctionnels si vous voulez experimenter, mais ne
sont plus lances automatiquement par `dashboard.py` (ni au demarrage, ni via le bouton
"Verifier maintenant", ni a l'ajout d'un produit).

Le suivi fonctionne donc en 100% manuel :

1. `python dashboard.py` lance uniquement le tableau de bord (plus de superviseur
   d'arriere-plan).
2. Ouvrez `http://127.0.0.1:8765`. Collez une URL de fiche produit rona.ca dans la barre du
   haut pour suivre un nouvel article -- seul le nom est devine depuis l'URL (aucun
   navigateur n'est ouvert); SKU, prix et image restent vides.
3. Dans le panneau **Magasins surveillés**, ajoutez chaque magasin qui vous intéresse avec
   son nom et un code postal ou une ville proche (l'"ancre" de recherche) — utilisez de
   préférence le code postal du magasin lui-même pour être sûr qu'il apparaisse.
4. Cliquez l'étoile sur un produit pour l'épingler en haut de la page.
5. Pour mettre a jour les quantités : consultez vous-même la disponibilité sur rona.ca
   (widget "Vérifiez d'autres magasins" sur chaque fiche produit), puis enregistrez les
   valeurs avec un petit script Python (voir `insert_manual_readings.py` comme modele --
   adaptez la liste `ROWS` avec vos propres sku/store_id/quantite et executez-le).
6. Toutes ces données (produits, magasins, historique, changements) vivent dans `stock.db`
   (SQLite, dans ce dossier) : elles survivent à un redémarrage de l'app, pas besoin de
   tout reconfigurer.

Le reste de ce document (README d'origine) décrit `discover.py`, `calibrate.py` et le mode
`http` de `config.yaml`, qui restent disponibles mais ne sont plus le chemin recommandé.

## Le point important sur le « temps réel »

Trois délais séparent la tablette de votre écran. Vous ne contrôlez que le dernier.

1. **Tablette vers caisse.** Le stock ne bouge qu'au passage en caisse. Rien à faire ici.
2. **Système interne vers site web.** Synchronisation par lots, souvent horaire, parfois deux
   fois par jour, plus une mise en cache CDN de quelques minutes. C'est le vrai plafond.
3. **Site web vers votre tracker.** Sonder plus vite que l'étape 2 ne donne pas d'information
   plus fraîche, seulement un risque de blocage.

L'outil mesure l'étape 2 au lieu de la deviner, puis cale son rythme dessus :

- `updated_path` lit la date de mise à jour déclarée par la source, si elle existe. C'est la
  mesure exacte de l'âge de la donnée.
- Les en-têtes `Cache-Control` et `Age` donnent le plancher imposé par le CDN.
- `calibrate.py` mesure empiriquement quand les valeurs changent vraiment et recommande un rythme.
- Le tableau de bord affiche en permanence l'âge du dernier relevé et l'âge déclaré par la
  source, pour que « reçu à l'instant » ne se confonde jamais avec « à jour ».

Pour la valeur de caisse à la seconde près, seul le portail PRO ou un appel au comptoir
la donne. Aucun montage sur le site public n'y arrive.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Playwright n'est nécessaire que pour la découverte et le mode de repli :

```bash
pip install playwright && playwright install chromium
```

## Étape 1 : trouver l'appel qui donne la quantité

Seule étape manuelle, et obligatoire : l'URL interne n'est pas publique et peut changer.

1. Ouvrez une fiche produit sur rona.ca.
2. Outils de développement, onglet Réseau, filtre Fetch/XHR.
3. Changez de magasin dans la page.
4. Repérez l'appel dont la réponse contient la disponibilité, puis « Copier comme cURL ».
5. Reportez dans `config.yaml` : `url`, `method`, `headers`, `cookies`, `quantity_path`, et
   surtout `updated_path` si la réponse contient une date de mise à jour.

Version assistée :

```bash
python discover.py "https://www.rona.ca/fr/produit/...-93515000"
```

Le script journalise tous les appels dont l'URL contient inventory, availability, stock,
store ou disponib, et écrit le tout dans `capture.json` avec les cookies. Les identifiants
de succursale apparaissent dans cette même requête.

## Étape 2 : vérifier un premier relevé

```bash
python collect.py
```

Chaque ligne montre la quantité, le code HTTP, la latence et l'âge de la copie servie par le
cache. Une étoile en début de ligne signale un changement. Si `qte=None`, le script imprime
le début de la réponse brute : comparez avec votre `quantity_path`.

## Étape 3 : mesurer la cadence réelle

```bash
python calibrate.py --every 5 --hours 24     # sonde puis produit le rapport
python calibrate.py --report                 # rapport sur l'historique déjà en base
```

Le rapport donne l'intervalle médian entre deux mises à jour de la source, la répartition
des changements par heure locale, le `max-age` du CDN, et une recommandation de rythme avec
la ligne de cron correspondante. Si les changements se concentrent sur peu d'heures, la
source est synchronisée par lots et le rapport le dit.

## Étape 4 : surveiller

Deux modes, au choix.

**Ordonnancement adaptatif**, recommandé :

```bash
python watch.py
```

Rythme resserré juste après un changement, ralentissement progressif quand rien ne bouge,
veille la nuit, et jamais plus vite que le `max-age` observé. Réglages dans la section
`schedule` de `config.yaml`.

**Cron**, si vous préférez :

```cron
*/30 7-21 * * *  cd ~/rona-tracker && ./venv/bin/python collect.py --quiet >> logs/collect.log 2>&1
```

Le collecteur envoie `If-None-Match` avec l'ETag du dernier relevé. Une réponse 304 confirme
que rien n'a changé sans retélécharger le corps : c'est plus léger et plus discret.

## Alertes

Deux familles, dans la section `alerts` :

- **Tout changement** (`on_any_change`), envoyé sans délai d'attente. C'est ça, « je veux le
  savoir dès que ça change ».
- **État** : rupture et stock bas, avec un anti-répétition pour ne pas recevoir le même
  message à chaque passage.

Canaux de livraison :

- **Notification poussée**, le plus rapide. ntfy.sh est gratuit et sans compte : installez
  l'application, choisissez un nom de sujet difficile à deviner, mettez
  `https://ntfy.sh/votre-sujet` dans `webhook.url`. Slack et Discord fonctionnent aussi en
  changeant `format`.
- **Courriel**, et **SMS** via la passerelle de votre fournisseur, du genre
  `5145551234@txt.bell.ca`. Avec Gmail, utilisez un mot de passe d'application.

Sans canal activé, les alertes s'affichent dans la console et sont journalisées.

## Tableau de bord

```bash
python dashboard.py     # http://127.0.0.1:8765
```

Une étiquette de casier par magasin : quantité, variation depuis le dernier relevé, courbe
en escalier de l'historique. En haut, le bandeau de fraîcheur avec un compteur d'âge qui
avance à la seconde. En bas, le journal horodaté de tous les changements. La page se
recharge d'elle-même dès qu'un changement entre en base.

`preview.html` montre le résultat avec des données de démonstration, sans rien installer.

## Export

```bash
python export.py --xlsx
```

Deux CSV et un classeur Excel à deux feuilles : les relevés avec leurs métadonnées de
fraîcheur, et le journal des changements. Les mêmes exports sont disponibles depuis le
tableau de bord.

## Ce qu'il faut savoir

**La quantité exacte n'est pas toujours exposée.** Le site affiche souvent « En stock » ou
« Stock limité ». Si l'appel ne retourne qu'un statut, il reste la méthode du panier : demander
une quantité volontairement trop grande, le site répond « seulement X disponible ». C'est plus
lourd, ça demande une session panier, et ça ne se justifie que si le nombre exact est essentiel.

**Les conditions d'utilisation de rona.ca interdisent la collecte automatisée.** Gardez cet
outil pour un usage personnel et à faible cadence. Si le besoin devient récurrent côté
affaires, demandez un accès au portail PRO : la disponibilité multi-magasins y est offerte
officiellement, et rien ne casse quand le site est refondu.

## Fichiers

| Fichier | Rôle |
|---|---|
| `config.example.yaml` | Modèle de configuration à copier en `config.yaml` |
| `discover.py` | Trouve l'appel d'inventaire, les cookies et la forme de la réponse |
| `collect.py` | Un relevé : quantités, fraîcheur, journal des changements, alertes |
| `calibrate.py` | Mesure la cadence réelle et recommande un rythme de sondage |
| `watch.py` | Surveillance continue à intervalle adaptatif |
| `alerts.py` | Règles d'alerte, notification poussée, courriel, SMS |
| `db.py` | Schéma SQLite, journal des changements, calcul de fraîcheur |
| `dashboard.py` | Tableau de bord auto-rafraîchi et exports CSV |
| `export.py` | Export CSV et Excel |
| `preview.html` | Aperçu statique avec données de démonstration |
