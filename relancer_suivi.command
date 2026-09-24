#!/bin/bash
# Double-clique sur ce fichier dans le Finder pour (re)lancer le suivi de
# stock RONA : il ouvre le Terminal, arrete une instance en cours s'il y en
# a une, puis relance dashboard.py.
cd "$(dirname "$0")"

# Arrete une instance deja en cours (si dashboard.py tournait deja).
pkill -f "python.*dashboard.py" 2>/dev/null
# Ceinture et bretelles: si un vieux processus tient encore le port 8765
# (lance autrement que par ce script), on le libere aussi -- sinon le
# nouveau dashboard.py ne demarre pas et le navigateur continue de parler
# a l'ancien, avec l'ancien code, sans erreur visible.
lsof -ti tcp:8765 | xargs kill 2>/dev/null
sleep 1

source venv/bin/activate
echo "Lancement du suivi de stock RONA..."
echo "Laisse cette fenetre ouverte. Ferme-la (ou Ctrl+C) pour arreter le suivi."
echo ""
python dashboard.py
