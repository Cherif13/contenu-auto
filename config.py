# Configuration centrale — Routine Mail AFPOLS
# Modifie ce fichier pour ajuster les règles sans toucher au code.

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Identité ────────────────────────────────────────────────────────────────
EMAIL_PRO = os.getenv("EMAIL_PRO", "")          # ton adresse Gmail pro
NOTIF_EMAIL = EMAIL_PRO                          # briefings envoyés sur la même boîte

# ─── Horaires ────────────────────────────────────────────────────────────────
HEURE_DEBUT_MATIN   = "09:00"
HEURE_FIN_MATIN     = "12:30"
HEURE_DEBUT_APREM   = "14:00"
HEURE_FIN_APREM     = "18:00"
HEURE_BRIEFING      = "08:30"   # heure du briefing matin (cron)
HEURE_MIDI          = "13:30"   # point midi
HEURE_DEBRIEF       = "18:00"   # débrief soir

# ─── Expéditeurs VIP (toujours 🔴 URGENT) ────────────────────────────────────
# Ajoute les emails exacts ou des fragments (@domaine.fr, nom complet)
VIP_SENDERS = [
    # Hiérarchie
    "hugues.campan",        # N+1 — Directeur Marketing AFPOLS
    "direction@afpols",
    "codir",

    # Prestataires critiques SOGI/Salesforce
    "jonathan.vetu",        # Chef de projet Lightning
    "bluebears",            # Support N1
    "arkeup",               # Maintenance Salesforce
    "jonathan.guerrand",    # DPO / RGPD

    # Autorités sécurité
    "cert@ssi.gouv.fr",
    "contact@anssi.fr",
    "@cnil.fr",
    "anssi",
    "cnil",

    # Éditeurs critiques (incidents)
    "salesforce.com",
    "conga.com",
    "ownbackup.com",
    "meraki.com",
    "cisco.com",
    "eset.com",
    "lockself",
]

# ─── Mots-clés urgents dans l'objet ou le corps ──────────────────────────────
URGENT_KEYWORDS = [
    # Générique
    "urgent", "asap", "deadline", "bloquant", "critique", "alerte",
    "aujourd'hui", "ce soir", "d'ici", "relance", "rappel urgent",
    "important", "action requise", "réponse attendue",

    # RSSI / Cybersécurité
    "incident", "cyberattaque", "phishing", "ransomware", "violation",
    "compromis", "intrusion", "fuite de données", "breach", "vulnérabilité",
    "CVE", "patch critique", "mise à jour de sécurité", "mot de passe",
    "PRA", "PCA", "plan de reprise", "continuité",

    # SOGI / Salesforce (cœur opérationnel)
    "SOGI", "Salesforce", "Lightning", "indisponible", "erreur critique",
    "panne", "down", "hors service",

    # Facturation / conformité
    "facturation", "blocage facturation", "bon de commande", "SIRET",
    "Conga", "Anaël", "non conforme", "non-conformité",

    # Domaines / certificats
    "expiration", "certificat", "domaine", "SSL", "renouvellement",

    # RGPD
    "RGPD", "CNIL", "violation de données", "DPO", "notification CNIL",
    "plainte", "audit",

    # USH
    "USH", "VPN Harmony", "Anaël Finance",
]

# ─── Mots-clés "peut attendre" (baisse la priorité) ──────────────────────────
LOW_PRIORITY_KEYWORDS = [
    "newsletter", "webinar", "invitation", "publication", "communiqué",
    "félicitations", "bonne nouvelle", "enquête satisfaction",
    "rapport mensuel", "compte rendu", "CR réunion",
    "FYI", "pour information", "pour info",
]

# ─── Domaines à classifier automatiquement en "Notifications" ────────────────
NOTIFICATION_DOMAINS = [
    "linkedin.com", "facebook.com", "twitter.com", "github.com",
    "noreply@", "no-reply@", "donotreply@", "notifications@",
    "mailer@", "bounce@", "postmaster@",
    "trello.com", "asana.com", "slack.com",
]

# ─── Domaines newsletters ─────────────────────────────────────────────────────
NEWSLETTER_DOMAINS = [
    "mailchimp.com", "sendinblue.com", "brevo.com", "mailjet.com",
    "constantcontact.com", "hubspot.com", "pardot.com",
    "list-manage.com", "campaign-archive.com",
]

# ─── Labels Gmail à créer ────────────────────────────────────────────────────
LABELS = {
    "urgent":        {"name": "🔴 Urgent",         "color": {"textColor": "#ffffff", "backgroundColor": "#cc3a21"}},
    "a_traiter":     {"name": "🟡 À traiter",       "color": {"textColor": "#594c05", "backgroundColor": "#ffd6a2"}},
    "peut_attendre": {"name": "🟢 Peut attendre",   "color": {"textColor": "#0b4f30", "backgroundColor": "#b9e4d0"}},
    "en_attente":    {"name": "⏳ En attente",       "color": {"textColor": "#1a73e8", "backgroundColor": "#c2e7ff"}},
    "reference":     {"name": "📚 Référence",        "color": {"textColor": "#444444", "backgroundColor": "#e8eaed"}},
    "newsletters":   {"name": "📰 Newsletters",      "color": {"textColor": "#444444", "backgroundColor": "#f1f3f4"}},
    "notifications": {"name": "🔔 Notifications",    "color": {"textColor": "#444444", "backgroundColor": "#f1f3f4"}},
    "equipe":        {"name": "👥 Équipe",           "color": {"textColor": "#41236d", "backgroundColor": "#d0bcf1"}},
    "clients":       {"name": "🤝 Partenaires",      "color": {"textColor": "#7a2e0b", "backgroundColor": "#ffd2a8"}},
    "secu":          {"name": "🔒 Sécurité",         "color": {"textColor": "#ffffff", "backgroundColor": "#8b0000"}},
}

# ─── Archivage automatique ────────────────────────────────────────────────────
# Mails non lus de plus de X jours sans importance → archivés
ARCHIVE_AFTER_DAYS = 90

# ─── Apprentissage des habitudes ─────────────────────────────────────────────
# Seuil : si tu réponds à quelqu'un dans les 4h → il monte en priorité
FAST_REPLY_THRESHOLD_HOURS = 4
# Après X réponses rapides → l'expéditeur devient quasi-VIP
VIP_LEARN_THRESHOLD = 3

# ─── Chemin état / logs ───────────────────────────────────────────────────────
STATE_FILE  = "state/last_run.json"
LEARN_FILE  = "state/learned_vips.json"
LOG_DIR     = "logs"

# ─── Claude API ───────────────────────────────────────────────────────────────
CLAUDE_MODEL        = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS   = 4096
BATCH_SIZE          = 20    # mails envoyés par lot à Claude

# ─── Mode dry-run ─────────────────────────────────────────────────────────────
# True = simulation sans modifier la boîte mail (idéal pour tester)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
