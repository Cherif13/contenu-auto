# Routine Mail AFPOLS — Chérif Nouredine (RSSI)

Système automatisé de gestion mail basé sur Gmail API + Google Calendar + Claude (Anthropic).

## Démarrage rapide

### 1. Prérequis
```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client anthropic python-dotenv
```

### 2. Configuration
```bash
copy .env.example .env
# Édite .env : ajoute ton email Gmail pro et ta clé Anthropic
```

### 3. Authentification Google (1 seule fois)
Suis le guide : [setup/guide_google_cloud.md](setup/guide_google_cloud.md)

### 4. Test de connexion (dry-run)
```bash
python scripts/briefing_matin.py --dry-run
```

### 5. Tri de la boîte existante (1 seule fois)
```bash
# Simulation d'abord
python scripts/tri_historique.py

# Puis pour de vrai (après validation)
python scripts/tri_historique.py --apply
```

---

## Scripts disponibles

| Script | Usage | Heure auto |
|--------|-------|------------|
| `scripts/tri_historique.py` | Nettoie toute la boîte (1 fois) | — |
| `scripts/briefing_matin.py` | Briefing + brouillons urgents | 8h30 |
| `scripts/point_midi.py` | Point rapide mi-journée | 13h30 |
| `scripts/debrief_soir.py` | Bilan + repriorisation demain | 18h00 |

### Options communes
```bash
--dry-run       # Simulation — aucune modification
--since-hours N # Mails des N dernières heures
```

---

## Automatisation Windows (Planificateur de tâches)

Ouvre le **Planificateur de tâches Windows** et crée 3 tâches :

| Tâche | Heure | Commande |
|-------|-------|---------|
| Briefing matin | Lun-Ven 8h30 | `python "C:\Users\cheri\Desktop\projet mail\routine-mail\scripts\briefing_matin.py"` |
| Point midi | Lun-Ven 13h30 | `python "C:\Users\cheri\Desktop\projet mail\routine-mail\scripts\point_midi.py"` |
| Débrief soir | Lun-Ven 18h00 | `python "C:\Users\cheri\Desktop\projet mail\routine-mail\scripts\debrief_soir.py"` |

Répertoire de démarrage : `C:\Users\cheri\Desktop\projet mail\routine-mail`

---

## Ajuster les règles

Tout se configure dans [config.py](config.py) :

```python
# Ajouter un VIP
VIP_SENDERS.append("nouveau.contact@exemple.fr")

# Ajouter un mot-clé urgent
URGENT_KEYWORDS.append("mon-mot-clé")

# Changer l'heure de briefing
HEURE_BRIEFING = "08:45"

# Activer le mode dry-run temporairement
DRY_RUN = True
```

---

## Pause (vacances / congés)

Pour désactiver temporairement la routine :
1. Dans le Planificateur de tâches → désactiver les 3 tâches
2. Ou mettre `DRY_RUN=true` dans `.env` (les scripts tournent mais ne modifient rien)

---

## Logs

```
logs/matin.log    # Briefing matin
logs/midi.log     # Point midi
logs/soir.log     # Débrief soir
logs/tri_historique.log  # Tri initial
```

---

## Fichiers sensibles (ne jamais partager)

- `credentials.json` — identifiants Google Cloud
- `token.json` — token d'accès OAuth
- `.env` — clé Anthropic + email

---

## Structure du projet

```
routine-mail/
├── .env                    ← tes secrets (jamais dans git)
├── .env.example            ← modèle
├── credentials.json        ← OAuth Google (jamais dans git)
├── token.json              ← généré au 1er run (jamais dans git)
├── config.py               ← toute la configuration
├── gmail_client.py         ← wrapper Gmail API
├── calendar_client.py      ← wrapper Google Calendar
├── claude_client.py        ← wrapper Claude (Anthropic)
├── learner.py              ← apprentissage des habitudes
├── scripts/
│   ├── tri_historique.py
│   ├── briefing_matin.py
│   ├── point_midi.py
│   └── debrief_soir.py
├── state/
│   ├── last_run.json       ← historique des exécutions
│   └── learned_vips.json   ← VIPs appris automatiquement
├── logs/
│   └── *.log
└── setup/
    └── guide_google_cloud.md
```
