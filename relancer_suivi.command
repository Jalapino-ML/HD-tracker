#!/bin/bash
# Double-clique sur ce fichier dans le Finder pour (re)lancer Suivi HD
# (suivi de stock Home Depot) : il installe l'environnement au besoin,
# arrete une instance en cours s'il y en a une, puis relance dashboard.py.
cd "$(dirname "$0")"

# Arrete une instance deja en cours (si dashboard.py tournait deja).
pkill -f "python.*dashboard.py" 2>/dev/null
# Ceinture et bretelles: si un vieux processus tient encore le port 8766
# (lance autrement que par ce script), on le libere aussi -- sinon le
# nouveau dashboard.py ne demarre pas et le navigateur continue de parler
# a l'ancien, avec l'ancien code, sans erreur visible.
lsof -ti tcp:8766 | xargs kill 2>/dev/null
sleep 1

# Premiere fois: cree l'environnement Python (venv) et installe les
# dependances si le dossier venv n'existe pas encore.
if [ ! -d venv ]; then
  echo "Premiere installation -- creation de l'environnement Python..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
else
  source venv/bin/activate
fi

echo "Lancement de Suivi HD..."
echo "Laisse cette fenetre ouverte. Ferme-la (ou Ctrl+C) pour arreter le suivi."
echo ""
python dashboard.py
