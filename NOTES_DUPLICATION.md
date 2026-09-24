# Notes -- duplication de Rona-Tracker vers Suivi HD (2026-09-24)

## Ce qui a ete fait automatiquement

- Dossier `Suivi-HD` cree a partir d'une copie complete de `Rona-Tracker`
  (code seulement -- `venv`, `stock.db`, `exports_excel`, `.env_cloud`,
  l'historique `.git` et les scripts de mise a jour RONA passes n'ont PAS
  ete copies).
- `dashboard.py`: titre, placeholders et noms de fichiers CSV renommes
  generiquement / vers Home Depot; la liste d'images RONA (`SEED_IMAGES_BY_SKU`)
  a ete videe.
- Base de donnees: aucune -- `stock.db` sera cree automatiquement, vide,
  au premier lancement (le schema est generique, sans donnees RONA).
- `README.md`: titre mis a jour.

## Ce qu'il reste a faire (et pourquoi je ne peux pas le faire seul)

1. **Choisir les produits et magasins Home Depot a suivre.** Rien n'est
   configure encore -- il faut d'abord explorer homedepot.ca pour verifier
   si le site affiche la quantite en stock par succursale de la meme facon
   que rona.ca (ce n'est pas garanti). Donnez-moi les URLs de produits et
   la liste des succursales voulues et je peux commencer cette exploration.

2. **Base de donnees Postgres (Neon) separee.** Suivi HD doit avoir sa
   propre base -- il faut un nouveau projet Neon (compte deja existant,
   juste creer un 2e projet, ~2 min). Je ne peux pas creer de compte ni
   remplir de mot de passe a votre place.

3. **Depot GitHub separe.** `git init` + nouveau depot prive (le meme
   processus que pour rona-tracker, voir DEPLOIEMENT_CLOUD.md).

4. **Service Render separe.** Nouveau "Blueprint" Render pointant sur ce
   nouveau depot, avec la nouvelle URL Postgres -- meme etapes que
   DEPLOIEMENT_CLOUD.md, section "Etape 3".

Quand vous etes pret, dites-moi et je vous guide etape par etape (ou je
fais directement les parties que je peux faire -- git local, adaptation
du code -- pendant que vous faites les clics de creation de compte/projet).
