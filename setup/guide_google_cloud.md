# Guide : Créer le projet Google Cloud et obtenir credentials.json

## Étape 1 — Créer le projet Google Cloud

1. Va sur https://console.cloud.google.com
2. Clique sur le sélecteur de projet en haut → **Nouveau projet**
3. Nom : `routine-mail-afpols`
4. Clique **Créer**

---

## Étape 2 — Activer les APIs

Dans ton projet, va dans **APIs et services > Bibliothèque** :

1. Recherche **Gmail API** → Activer
2. Recherche **Google Calendar API** → Activer

---

## Étape 3 — Configurer l'écran de consentement OAuth

1. **APIs et services > Écran de consentement OAuth**
2. Type d'utilisateur : **Externe** → Créer
3. Remplis :
   - Nom de l'application : `Routine Mail AFPOLS`
   - Email de support : ton email Gmail pro
4. Clique **Enregistrer et continuer** (sur toutes les étapes suivantes)
5. Sur la page **Utilisateurs test** → **+ Ajouter des utilisateurs** → ajoute ton email Gmail pro

---

## Étape 4 — Créer les identifiants OAuth 2.0

1. **APIs et services > Identifiants**
2. **+ Créer des identifiants > ID client OAuth**
3. Type d'application : **Application de bureau**
4. Nom : `routine-mail-desktop`
5. Clique **Créer**
6. **Télécharger le JSON** → renommer le fichier en `credentials.json`
7. Copier `credentials.json` à la racine du projet (dans `routine-mail/`)

---

## Étape 5 — Premier lancement (authentification)

```bash
cd "C:\Users\cheri\Desktop\projet mail\routine-mail"
python scripts/briefing_matin.py --dry-run
```

Une fenêtre de navigateur s'ouvre → **Connexion avec ton compte Gmail pro** → Autoriser.

Un fichier `token.json` est créé automatiquement. Il sera réutilisé à chaque lancement.

---

## Étape 6 — Installer les dépendances Python

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client anthropic python-dotenv
```

---

## Étape 7 — Configurer le .env

```bash
# Dans le dossier routine-mail/
copy .env.example .env
# Puis édite .env avec ton email et ta clé Anthropic
```

---

## Sécurité

- `credentials.json` et `token.json` sont dans `.gitignore` — **ne jamais les partager**
- La clé Anthropic est dans `.env` — **ne jamais la mettre dans le code**
- L'accès est limité à ton compte Gmail pro uniquement
