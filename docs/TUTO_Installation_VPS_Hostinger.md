# Installer PAF-001 sur un VPS Hostinger
### Guide complet pas a pas -- Ubuntu 24 LTS

---

> **Ce guide est fait pour toi si :**
> - Tu as un VPS Hostinger (Ubuntu 22 ou 24 LTS)
> - Tu veux faire tourner PAF-001 en paper trading
> - Tu n'as jamais deploye de bot sur un serveur
>
> **Duree estimee :** 30 a 45 minutes
> **Niveau requis :** Savoir copier-coller des commandes dans un terminal

---

## SOMMAIRE

1. [Connexion au VPS](#1-connexion-au-vps)
2. [Preparation du serveur](#2-preparation-du-serveur)
3. [Installation de Python](#3-installation-de-python)
4. [Telechargement du bot](#4-telechargement-du-bot)
5. [Installation des dependances](#5-installation-des-dependances)
6. [Configuration des secrets](#6-configuration-des-secrets)
7. [Premier demarrage](#7-premier-demarrage)
8. [Acces au dashboard](#8-acces-au-dashboard)
9. [Faire tourner le bot en permanence](#9-faire-tourner-le-bot-en-permanence)
10. [Commandes utiles au quotidien](#10-commandes-utiles-au-quotidien)
11. [Resolution des problemes courants](#11-resolution-des-problemes-courants)

---

## 1. Connexion au VPS

### 1.1 Trouver les identifiants sur Hostinger

1. Connecte-toi sur [hpanel.hostinger.com](https://hpanel.hostinger.com)
2. Va dans **VPS** puis clique sur ton serveur
3. Dans l'onglet **Apercu**, note :
   - L'**adresse IP** de ton VPS (ex: `185.234.XXX.XXX`)
   - Le **nom d'utilisateur** (generalement `root`)
4. Le mot de passe t'a ete envoye par email lors de la creation du VPS

### 1.2 Se connecter via SSH

**Sur Mac ou Linux**, ouvre le Terminal et tape :

```bash
ssh root@TON_IP_VPS
```

**Sur Windows**, utilise PuTTY ou Windows Terminal :
```
Hote : TON_IP_VPS
Port : 22
Utilisateur : root
```

> Premiere connexion : Tape `yes` quand on te demande de confirmer
> la cle d'hote, puis entre ton mot de passe.

**Resultat attendu :**
```
Welcome to Ubuntu 24.04 LTS
root@srv1420721:~#
```

Si tu vois ce prompt, tu es connecte.

---

## 2. Preparation du serveur

### 2.1 Mettre a jour le systeme

```bash
apt update && apt upgrade -y
```

> Cette commande peut prendre 2 a 5 minutes.
> **Resultat attendu :** Beaucoup de texte defilant, puis retour au prompt.

### 2.2 Installer les outils necessaires

```bash
apt install -y git curl wget nano screen htop unzip build-essential \
    libssl-dev libffi-dev python3-dev
```

**Resultat attendu :** Installation de plusieurs paquets, puis retour au prompt.

### 2.3 Creer un utilisateur dedie (recommande)

> Bonne pratique : ne pas faire tourner le bot en `root`.

```bash
# Creer l'utilisateur
adduser paf

# Lui donner les droits sudo
usermod -aG sudo paf

# Basculer vers cet utilisateur
su - paf
```

> Pour la suite du tutoriel, toutes les commandes sont executees en tant
> qu'utilisateur `paf` (ou `root` si tu preferes garder root).

---

## 3. Installation de Python

### 3.1 Verifier la version Python disponible

```bash
python3 --version
```

**Resultat attendu :** `Python 3.10.x` ou superieur.

> PAF-001 necessite **Python 3.10 minimum** (utilise les type hints modernes).

Si la version est inferieure a 3.10, installe Python 3.10 :

```bash
# Ajouter le repository deadsnakes
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3.10-dev
```

### 3.2 Verifier pip

```bash
python3 -m pip --version
```

Si pip n'est pas installe :
```bash
curl -sS https://bootstrap.pypa.io/get-pip.py | python3
```

---

## 4. Telechargement du bot

### 4.1 Creer le dossier de travail

```bash
mkdir -p ~/paf001
cd ~/paf001
```

### 4.2 Copier les fichiers du bot

**Option A -- Depuis GitHub (si le repo est heberge) :**
```bash
git clone URL_DE_TON_REPO .
```

**Option B -- Upload manuel depuis ton ordinateur :**

Sur **ton ordinateur** (pas le VPS), ouvre un nouveau terminal et tape :
```bash
# Copier tout le dossier du bot sur le VPS
scp -r /chemin/vers/BotPolymarket/* paf@TON_IP_VPS:~/paf001/
```

> Remplace `/chemin/vers/BotPolymarket/` par le chemin reel du dossier
> sur ton ordinateur (ex: `D:\BotPolymarket\` sur Windows, `~/BotPolymarket/` sur Mac).

**Option C -- Depuis Hostinger File Manager :**
1. Dans hpanel, va dans **Fichiers** puis **Gestionnaire de fichiers**
2. Navigue vers `/home/paf/paf001/`
3. Upload les fichiers via l'interface web

### 4.3 Verifier le contenu

```bash
ls -la ~/paf001/
```

**Resultat attendu :** Tu dois voir ces fichiers et dossiers :
```
drwxr-xr-x  core/
drwxr-xr-x  signals/
drwxr-xr-x  trading/
drwxr-xr-x  tests/
drwxr-xr-x  backtesting/
-rw-r--r--  main.py
-rw-r--r--  dashboard_server.py
-rw-r--r--  requirements.txt
-rw-r--r--  .env.example
-rw-r--r--  pyproject.toml
```

---

## 5. Installation des dependances

### 5.1 Creer un environnement virtuel Python

```bash
cd ~/paf001
python3 -m venv .venv
```

**Resultat attendu :** Dossier `.venv/` cree dans `~/paf001/`.

### 5.2 Activer l'environnement

```bash
source .venv/bin/activate
```

**Resultat attendu :** Le prompt change et affiche `(.venv)` devant :
```
(.venv) paf@srv1420721:~/paf001$
```

> **Important :** Cette commande est a refaire a chaque fois que tu te
> reconnectes au VPS et que tu veux travailler sur le bot.

### 5.3 Installer les dependances

```bash
pip install -r requirements.txt
```

> Cette etape peut prendre **5 a 15 minutes** selon ta connexion.
> Il y a 13 packages principaux a installer (aiohttp, numpy, scipy,
> pandas, scikit-learn, torch, etc.) plus leurs sous-dependances.

**Resultat attendu :** Beaucoup de `Downloading...` et `Installing...`,
puis a la fin :
```
Successfully installed aiohttp-X.X.X numpy-X.X.X scipy-X.X.X ...
```

> **Note sur torch :** PyTorch est volumineux (~2 Go). Si ton VPS a peu
> de RAM (<2 Go), installe la version CPU uniquement :
> ```bash
> pip install torch --index-url https://download.pytorch.org/whl/cpu
> pip install -r requirements.txt
> ```

### 5.4 Installer les dependances de test (optionnel mais recommande)

```bash
pip install -r requirements-dev.txt
```

### 5.5 Verifier l'installation

```bash
python -c "import aiohttp, numpy, scipy; print('Dependances OK')"
```

**Resultat attendu :** `Dependances OK`

Si tu vois une erreur `ModuleNotFoundError`, relance l'etape 5.3.

---

## 6. Configuration des secrets

> **Cette etape est critique.** Tes cles API et secrets ne doivent
> JAMAIS se retrouver dans un fichier partage ou committe sur Git.

### 6.1 Creer le fichier de configuration

```bash
cp .env.example .env
```

### 6.2 Editer le fichier .env

```bash
nano .env
```

Tu vas voir s'afficher le fichier de configuration. Remplis chaque valeur :

```bash
# --- MODE ---
# LAISSER a true -- Ne jamais mettre false sans paper trading valide
DRY_RUN=true

# --- POLYMARKET CREDENTIALS ---
# Obtenir via https://docs.polymarket.com/#authentication
POLY_API_KEY=colle_ta_cle_ici
POLY_API_SECRET=colle_ton_secret_ici
POLY_API_PASSPHRASE=colle_ta_passphrase_ici

# Cle privee du wallet Polygon (hex, sans 0x)
POLY_PRIVATE_KEY=
POLY_WALLET_ADDRESS=

# --- CAPITAL ---
INITIAL_BANKROLL=100.0

# --- APIS OPTIONNELLES ---
# BLS.gov -- donnees CPI/emploi (inscription gratuite sur bls.gov)
BLS_API_KEY=

# Wallet de reference a surveiller (whale tracking)
REFERENCE_WALLET=

# --- TELEGRAM ALERTES ---
# Creer un bot via @BotFather, puis obtenir le chat_id via @userinfobot
TELEGRAM_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=123456789

# --- PERSISTANCE ---
DB_PATH=paf_trading.db
SIGNAL_DB_PATH=paf_signals.db
```

> **Navigation dans nano :**
> - Pour sauvegarder : `Ctrl+O` puis `Entree`
> - Pour quitter : `Ctrl+X`

### 6.3 Configurer la cle privee du wallet Polygon (optionnel en paper)

En mode `DRY_RUN=true`, tu n'as pas besoin de cle privee. Mais si tu
veux la configurer pour plus tard :

```bash
# Creer le dossier securise
mkdir -p ~/.paf

# Creer le fichier avec ta cle privee
# ATTENTION : remplace TA_CLE_PRIVEE par ta vraie cle (sans les guillemets, sans 0x)
echo "TA_CLE_PRIVEE_POLYGON" > ~/.paf/poly_key

# Securiser les permissions (lecture uniquement par toi)
chmod 600 ~/.paf/poly_key

# Verifier
ls -la ~/.paf/poly_key
```

**Resultat attendu :**
```
-rw------- 1 paf paf 66 Jan 15 08:32 /home/paf/.paf/poly_key
```

> Les permissions `-rw-------` (600) signifient que seul toi peux
> lire ce fichier. C'est indispensable pour la securite.

### 6.4 Verifier que .env est protege

```bash
# .env ne doit JAMAIS apparaitre dans git status
git check-ignore -v .env
```

**Resultat attendu :**
```
.gitignore:1:.env    .env
```

Si .env n'est pas ignore, ajoute-le manuellement :
```bash
echo ".env" >> .gitignore
```

### 6.5 Creer les dossiers de travail

```bash
mkdir -p ~/paf001/logs
mkdir -p ~/paf001/backups
```

> Le bot cree ces dossiers :
> - `logs/` -- fichiers de log (rotation auto : 50 Mo x 10 fichiers = 500 Mo max)
> - `backups/` -- sauvegardes auto de la DB toutes les 6h (max 30 backups)

---

## 7. Premier demarrage

### 7.1 Verifier que tout est en ordre

```bash
cd ~/paf001
source .venv/bin/activate

# Test d'import de tous les modules
python -c "
import main, core.config, core.database
import signals.crucix_router, trading.risk_manager
import trading.execution, dashboard_server
print('Tous les imports OK')
"
```

**Resultat attendu :** `Tous les imports OK`

Si tu vois `ModuleNotFoundError: No module named 'X'`, retourne a l'etape 5.3.

### 7.2 Lancer la suite de tests

```bash
pytest tests/ -q --tb=short
```

**Resultat attendu :**
```
164 passed in X.XXs
```

> Si certains tests echouent, note les erreurs et compare avec
> la section [Resolution des problemes](#11-resolution-des-problemes-courants).

### 7.3 Premier demarrage en foreground

Pour la toute premiere fois, lance le bot en foreground pour voir ce qui se passe :

```bash
DRY_RUN=true python main.py
```

**Resultat attendu dans les premieres secondes :**
```
2026-03-20T08:32:11 INFO  main              Loading config...
2026-03-20T08:32:11 INFO  database          Connecting to paf_trading.db (WAL mode)
2026-03-20T08:32:11 INFO  reconciliation    No pending orders to reconcile
2026-03-20T08:32:11 INFO  health_monitor    All components healthy
2026-03-20T08:32:12 INFO  main              Starting trading loop -- DRY_RUN=True PAPER=True
2026-03-20T08:32:15 INFO  scanner           Scanning Polymarket markets...
```

> Si tu vois ces lignes, le bot fonctionne correctement.
>
> Pour arreter : `Ctrl+C`
>
> **Ce que tu NE veux PAS voir :**
> ```
> ERROR  Cannot connect to CLOB API
> ERROR  Telegram token invalid
> CRITICAL  Database corruption detected
> ```
> Si tu vois ces erreurs, va a la section [Resolution des problemes](#11-resolution-des-problemes-courants).

---

## 8. Acces au dashboard

Le dashboard est un panneau de controle web qui te permet de surveiller
le bot en temps reel -- bankroll, positions, metriques, kill switches.

**Port du dashboard : 8765** (configure dans `dashboard_server.py`)

### 8.1 Depuis le VPS lui-meme (test rapide)

```bash
# Dans un deuxieme terminal SSH
curl -s http://localhost:8765/ping
```

**Resultat attendu :** `pong`

### 8.2 Depuis ton ordinateur (via tunnel SSH)

Le dashboard n'est pas expose directement sur Internet pour des raisons
de securite. On utilise un tunnel SSH.

**Sur ton ordinateur** (pas le VPS), ouvre un terminal :

```bash
# Creer le tunnel SSH
ssh -L 8765:localhost:8765 paf@TON_IP_VPS -N
```

> Cette commande ne retourne rien -- c'est normal. Le tunnel est actif
> tant que cette fenetre est ouverte.

Puis dans ton navigateur, va sur :
```
http://localhost:8765/
```

### 8.3 S'authentifier

Le dashboard requiert un token via header `Authorization: Bearer`.

Pour tester depuis le terminal :

```bash
# Genere un token si tu ne l'as pas fait
openssl rand -hex 32

# Teste le dashboard (remplace TON_TOKEN par la valeur dans .env DASHBOARD_AUTH_TOKEN)
# Si tu n'as pas configure DASHBOARD_AUTH_TOKEN dans .env, le bot en genere
# un automatiquement au demarrage -- regarde les logs pour le trouver.
curl -H "Authorization: Bearer TON_TOKEN" \
  http://localhost:8765/api/data | python3 -m json.tool
```

Pour utiliser le dashboard dans le navigateur, utilise une extension comme
**ModHeader** (Chrome/Firefox) pour ajouter le header :

```
Nom    : Authorization
Valeur : Bearer TON_TOKEN
```

### 8.4 Endpoints disponibles

| URL | Description | Auth requise |
|-----|-------------|--------------|
| `/ping` | Le bot repond ? (retourne `pong`) | Non |
| `/api/data` | Donnees JSON completes (bankroll, positions, metriques) | Oui |
| `/` ou `/dashboard` | Interface HTML complete avec graphiques | Oui |

---

## 9. Faire tourner le bot en permanence

Si tu fermes ton terminal SSH, le bot s'arrete. Pour le faire tourner
en permanence, utilise `screen` ou `systemd`.

### Option A -- screen (simple, recommande pour debuter)

```bash
# Creer une session screen nommee "paf001"
screen -S paf001

# Dans la session screen, activer l'env et demarrer
cd ~/paf001
source .venv/bin/activate
DRY_RUN=true python main.py
```

**Pour detacher la session (le bot continue de tourner) :**
```
Ctrl+A puis D
```

**Pour revenir voir le bot plus tard :**
```bash
screen -r paf001
```

**Pour voir toutes les sessions screen actives :**
```bash
screen -ls
```

### Option B -- systemd (robuste, redemarrage automatique)

```bash
# Creer le fichier de service
sudo nano /etc/systemd/system/paf001.service
```

Colle ce contenu **(adapte les chemins a ton utilisateur)** :

```ini
[Unit]
Description=PAF-001 Polymarket Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=paf
WorkingDirectory=/home/paf/paf001
EnvironmentFile=/home/paf/paf001/.env
ExecStart=/home/paf/paf001/.venv/bin/python main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/paf/paf001/logs/bot.log
StandardError=append:/home/paf/paf001/logs/bot.log
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

> **Pourquoi `KillSignal=SIGTERM` et `TimeoutStopSec=60` ?**
> Le bot intercepte SIGTERM pour effectuer un arret propre : il flush la DB,
> sauvegarde le bankroll, et envoie une notification Telegram. Il a besoin
> de quelques secondes pour finir proprement.

```bash
# Activer et demarrer le service
sudo systemctl daemon-reload
sudo systemctl enable paf001
sudo systemctl start paf001

# Verifier que ca tourne
sudo systemctl status paf001
```

**Resultat attendu :**
```
* paf001.service - PAF-001 Polymarket Trading Bot
     Loaded: loaded (/etc/systemd/system/paf001.service; enabled)
     Active: active (running) since ...
```

**Commandes systemd utiles :**
```bash
sudo systemctl start paf001    # Demarrer
sudo systemctl stop paf001     # Arreter proprement (SIGTERM)
sudo systemctl restart paf001  # Redemarrer
sudo systemctl status paf001   # Voir l'etat
journalctl -u paf001 -f        # Logs en temps reel
```

---

## 10. Commandes utiles au quotidien

### Voir les logs en temps reel

```bash
# Tous les logs
tail -f ~/paf001/logs/bot.log

# Seulement les erreurs
tail -f ~/paf001/logs/bot.log | grep -E "ERROR|CRITICAL|WARNING"

# Les 50 dernieres lignes
tail -50 ~/paf001/logs/bot.log
```

> Les logs sont en rotation automatique : 50 Mo par fichier, 10 fichiers max
> (soit 500 Mo maximum de logs).

### Verifier que le bot tourne

```bash
ps aux | grep "python main.py" | grep -v grep
```

**Resultat attendu :** Une ligne avec le processus du bot.

### Verifier l'usage des ressources

```bash
# CPU et memoire du bot
ps aux | grep "python main.py" | grep -v grep | awk '{print "CPU:", $3"% | RAM:", $4"%"}'

# Espace disque (logs + DB)
du -sh ~/paf001/logs/ ~/paf001/backups/ ~/paf001/*.db 2>/dev/null
```

### Verifier le bankroll et les positions

```bash
# Bankroll actuel
sqlite3 ~/paf001/paf_trading.db \
  "SELECT 'Bankroll: ' || nav || ' EUR' FROM nav_history ORDER BY ts DESC LIMIT 1"

# Positions ouvertes
sqlite3 ~/paf001/paf_trading.db \
  "SELECT market_id, cost_basis, current_price FROM open_positions"

# Nombre de trades resolus
sqlite3 ~/paf001/paf_trading.db \
  "SELECT COUNT(*) || ' trades resolus' FROM trades WHERE status='closed'"
```

### Arreter le bot proprement

```bash
# Via systemd (recommande)
sudo systemctl stop paf001

# Ou via SIGTERM directement
kill -SIGTERM $(pgrep -f "python main.py")

# En urgence (arret immediat de TOUT trading)
touch ~/paf001/.emergency_stop
```

> Le fichier `.emergency_stop` est verifie a chaque cycle (toutes les 10 min).
> Quand le bot le detecte, il arrete immediatement tout trading.
> Pour reprendre : `rm ~/paf001/.emergency_stop` puis redemarrer.

### Relancer apres modification

```bash
sudo systemctl restart paf001
sleep 3
sudo systemctl status paf001
```

### Mettre a jour le bot (nouvelle version)

```bash
# 1. Arreter
sudo systemctl stop paf001

# 2. Sauvegarder les DB
cp ~/paf001/paf_trading.db ~/paf001/paf_trading.db.backup
cp ~/paf001/paf_signals.db ~/paf001/paf_signals.db.backup

# 3. Mettre a jour les fichiers
cd ~/paf001
git pull  # Ou copier les nouveaux fichiers manuellement

# 4. Mettre a jour les dependances si requirements.txt a change
source .venv/bin/activate
pip install -r requirements.txt

# 5. Lancer les tests
pytest tests/ -q --tb=short

# 6. Redemarrer
sudo systemctl start paf001
```

---

## 11. Resolution des problemes courants

### Erreur : `ModuleNotFoundError`

```
ModuleNotFoundError: No module named 'aiohttp'
```

**Solution :**
```bash
source .venv/bin/activate  # Activer l'environnement
pip install -r requirements.txt
```

---

### Erreur : `Connection refused` sur le dashboard

**Solution :** Le bot n'est pas demarre, ou le tunnel SSH n'est pas actif.

```bash
# Verifier si le bot tourne
ps aux | grep "python main.py" | grep -v grep

# Verifier si le port 8765 est ouvert
ss -tlnp | grep 8765

# Recreer le tunnel SSH depuis ton ordinateur
ssh -L 8765:localhost:8765 paf@TON_IP_VPS -N
```

---

### Erreur : `401 Unauthorized` sur le dashboard

**Solution :** Token manquant ou incorrect.

Le dashboard accepte uniquement l'authentification via header
`Authorization: Bearer <token>`.

```bash
# Si tu n'as pas configure DASHBOARD_AUTH_TOKEN dans .env,
# le bot en genere un au demarrage. Cherche-le dans les logs :
grep -i "token\|dashboard" ~/paf001/logs/bot.log | tail -5

# Tester avec curl
curl -H "Authorization: Bearer TON_TOKEN" http://localhost:8765/ping
```

---

### Erreur : `Telegram token invalid`

**Solution :**
1. Va sur Telegram, cherche `@BotFather`
2. Envoie `/mybots` puis selectionne ton bot puis `API Token`
3. Copie le token dans `.env` a la ligne `TELEGRAM_TOKEN=`
4. Pour le Chat ID, envoie un message a `@userinfobot` puis `/start`
5. Redemarre le bot

---

### Erreur : `CLOB API 403 Forbidden`

**Solution :** Cles API Polymarket expirees ou invalides.

1. Va sur [app.polymarket.com](https://app.polymarket.com)
2. Settings puis API Keys puis regenere les cles
3. Mets a jour `.env` avec les nouvelles cles
4. Redemarre le bot

> En mode `DRY_RUN=true`, les cles API ne sont pas strictement necessaires
> pour le scan de marche (Gamma API est publique). Mais elles sont requises
> pour soumettre des ordres.

---

### Le bot s'arrete tout seul

**Solution :** Verifier les logs pour trouver la cause.

```bash
# Voir les dernieres lignes avant l'arret
tail -100 ~/paf001/logs/bot.log | grep -E "ERROR|CRITICAL|KILL|SHUTDOWN"
```

Causes courantes :
- **Kill switch declenche** (normal si bankroll < 60 EUR ou MDD > 8%)
- **Erreur API** (reseau temporairement indisponible -- systemd redemarre auto)
- **Fichier `.emergency_stop` present** -- `rm ~/paf001/.emergency_stop`
- **Profit Factor < 1.2 au demarrage** (normal pour un bot neuf sans historique,
  le KS4 se desactive apres quelques trades resolus)

---

### Espace disque plein

```bash
# Verifier l'espace
df -h /

# Taille des logs et backups
du -sh ~/paf001/logs/ ~/paf001/backups/

# Les logs sont auto-rotes (50 Mo x 10 fichiers max)
# Si le dossier backups est trop gros, supprimer les anciens (> 30 fichiers) :
ls -t ~/paf001/backups/*.db 2>/dev/null | tail -n +31 | xargs rm -f
```

---

### Le bot tourne mais ne trouve pas de marches

**C'est normal.** Le bot cherche des marches avec des criteres
precis :
- Volume 24h entre 5 000$ et 80 000$
- Resolution dans 5 a 21 jours (S1) ou 14 a 90 jours (S2)
- Prix YES entre 0.70 et 0.92 (favoris) ou 0.01 et 0.10 (longshots)
- Edge detectable >= 4 cents

Si aucun marche ne passe les filtres, le bot attend le prochain cycle
(toutes les 10 minutes).

Pour verifier que le scan fonctionne :
```bash
grep -i "scan\|candidate\|eligible" ~/paf001/logs/bot.log | tail -5
```

---

### Erreur : `aiodns needs a SelectorEventLoop on Windows`

Cette erreur n'apparait PAS sur le VPS (Linux). Elle ne concerne que
le developpement local sur Windows.

---

## Recapitulatif des fichiers et ports

| Element | Valeur |
|---------|--------|
| Port dashboard | **8765** |
| DB trading | `paf_trading.db` |
| DB signaux | `paf_signals.db` |
| Logs | `logs/bot.log` (rotation 50 Mo x 10) |
| Backups | `backups/` (toutes les 6h, max 30) |
| Emergency stop | `.emergency_stop` (creer pour arreter) |
| Cycle signal | Toutes les 10 minutes |
| Cycle scan | Toutes les 60 minutes |
| Monitor positions | Toutes les 2 minutes |
| Heartbeat Telegram | Toutes les 30 minutes |
| Python minimum | 3.10+ |
| Packages | 13 dependances principales |

---

## Etapes suivantes

Ton bot PAF-001 tourne sur le VPS Hostinger.

```
Phase actuelle : Paper Trading (DRY_RUN=true)

14 jours minimum

Brier < 0.20 + Sharpe > 1.0 + GoLiveChecker vert

DRY_RUN=false (capital reel avec 2 EUR max/trade)
```

**Check quotidien (2 minutes) :**
```bash
# Etat du bot
curl -s -H "Authorization: Bearer TON_TOKEN" \
  http://localhost:8765/api/data | python3 -m json.tool | head -20

# Erreurs du jour
grep $(date +%Y-%m-%d) ~/paf001/logs/bot.log | grep -c "ERROR"
```

---

*PAF-001 -- Guide d'installation VPS Hostinger*
*Genere automatiquement depuis l'analyse du codebase*
