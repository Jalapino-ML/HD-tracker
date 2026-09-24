# Héberger le tableau de bord RONA dans le cloud (gratuit)

Ce guide déploie une **copie en lecture** du tableau de bord sur Render,
accessible depuis n'importe où (même sans Tailscale), avec ses données dans
une base Postgres gratuite (Neon). Le Mac reste la source de vérité: c'est
toujours là que tu fais les mises à jour manuelles; un petit script
(`sync_to_cloud.py`) pousse ensuite les nouveaux chiffres vers le cloud.

Aucune de ces étapes ne peut être faite à ta place (création de comptes,
mots de passe) — mais chacune est courte. Compte ~20-30 minutes la première
fois.

## Étape 1 — Créer la base Postgres gratuite (Neon)

1. Va sur **https://neon.tech** et crée un compte gratuit (bouton "Sign up" —
   GitHub, Google ou courriel).
2. Une fois connecté, crée un nouveau projet (nom suggéré: `rona-tracker`).
   Neon crée automatiquement une base de données.
3. Sur la page du projet, trouve le bouton **"Connection string"** (ou
   "Connect") — copie l'URL qui commence par `postgresql://...`. Elle
   contient déjà le mot de passe, garde-la privée.
4. C'est tout pour Neon: le plan gratuit est permanent, pas de carte de
   crédit requise, et la base ne se met jamais en pause définitivement
   (elle "s'endort" après inactivité mais se réveille automatiquement à la
   prochaine connexion, en 1-2 secondes).

## Étape 2 — Mettre le projet sur GitHub

Dans le Terminal, sur le Mac:

```bash
cd ~/Documents/AI-Folder/Rona-Tracker
git init
git add .
git commit -m "Rona Tracker"
```

Puis sur **https://github.com**, crée un compte si tu n'en as pas, puis un
nouveau dépôt (bouton vert "New") — nom suggéré `rona-tracker`, visibilité
**Private** recommandée (le code n'a rien de secret, mais autant rester
prudent). Ne coche AUCUNE case (pas de README/gitignore/licence — le dépôt
doit être vide).

GitHub affiche ensuite des commandes à coller — ce sera quelque chose comme:

```bash
git remote add origin https://github.com/TON-NOM/rona-tracker.git
git branch -M main
git push -u origin main
```

(La première fois, GitHub va te demander de t'authentifier dans le
navigateur — suis simplement les instructions à l'écran.)

`.gitignore` est déjà configuré pour NE PAS pousser `stock.db` ni
`config.yaml` (données privées) — seul le code part sur GitHub.

## Étape 3 — Déployer sur Render

1. Va sur **https://render.com**, crée un compte gratuit (le plus simple:
   "Sign up with GitHub" — ça connecte les deux d'un coup).
2. Dans le tableau de bord Render, clique **"New +"** → **"Blueprint"**.
3. Choisis ton dépôt `rona-tracker` (autorise Render à y accéder si demandé).
   Render détecte automatiquement `render.yaml` et propose de créer un
   service web nommé `rona-tracker`.
4. Avant de confirmer, Render demande la valeur de `DATABASE_URL` (marquée
   `sync: false` dans `render.yaml`, donc à coller manuellement) — colle
   l'URL Postgres copiée à l'étape 1.
5. Clique **"Apply"** / **"Create"**. Le premier build prend 2-3 minutes.
6. Une fois déployé, Render affiche une URL du genre
   `https://rona-tracker.onrender.com` — c'est ton tableau de bord,
   accessible de partout, sur PC, téléphone ou tablette, sans Tailscale.

Note: sur le plan gratuit, le service s'endort après ~15 minutes sans
visite, et met environ 1 minute à se "réveiller" au chargement suivant.
C'est normal et sans danger — les données ne bougent pas pendant ce temps.

## Étape 4 — Copier les données actuelles vers le cloud

Une fois Neon et Render en place, envoie les données actuelles de
`stock.db` vers Postgres. Sur le Mac:

```bash
cd ~/Documents/AI-Folder/Rona-Tracker
source venv/bin/activate
pip install psycopg2-binary
export DATABASE_URL="postgresql://...."   # la meme URL collee dans Render
python sync_to_cloud.py
```

Tu peux aussi coller l'URL une fois dans un fichier `.env_cloud` (une seule
ligne `DATABASE_URL=postgresql://...`) à côté des scripts pour ne plus
avoir à la retaper — `sync_to_cloud.py` la lit automatiquement s'il existe
(ce fichier est aussi exclu de GitHub par `.gitignore`).

Recharge `https://rona-tracker.onrender.com` — les données doivent
apparaître.

## Après ça: le flux de mise à jour normal

Rien ne change pour les mises à jour elles-mêmes (toujours faites
manuellement via navigateur Chrome connecté, comme aujourd'hui, écrites
dans `stock.db` local). La seule étape ajoutée: après une mise à jour,
lancer `python sync_to_cloud.py` pour que la copie cloud reflète les
nouveaux chiffres. Je peux le faire automatiquement à la fin de chaque
mise à jour que je fais pour toi, si tu me donnes (ou mets dans
`.env_cloud`) l'URL Postgres.

## Résumé des deux accès

| | Tailscale | Cloud (Render) |
|---|---|---|
| Installation requise sur les autres appareils | App Tailscale | Aucune, juste un navigateur |
| Fonctionne hors réseau maison / Tailscale | Non | Oui, partout |
| Délai au premier chargement | Aucun | ~1 min si inactif 15+ min |
| Données | Toujours à jour (temps réel) | À jour après le prochain `sync_to_cloud.py` |
| Coût | Gratuit | Gratuit |
