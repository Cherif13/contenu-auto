"""Wrapper Claude API (Anthropic) — classification et génération de briefings."""

import json
import logging
import os
from typing import Any

import anthropic
import config

logger = logging.getLogger(__name__)

_client = None

def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY manquant dans le fichier .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ─── Prompt système partagé ───────────────────────────────────────────────────

SYSTEM_CONTEXT = """Tu es l'assistant mail de Chérif Nouredine, RSSI et Responsable Informatique à l'AFPOLS (Association de Formation des Professionnels du Logement Social, ~50 collaborateurs, infrastructure 100% cloud, rattachée à l'USH).

Contexte métier critique :
- Système central : SOGI (Salesforce Lightning) — CRM + inscriptions + facturation
- Sécurité : EDR ESET, LockSelf (mots de passe), firewall Cisco Meraki
- Prestataires clés : BlueBears IT (support N1), ArkeUp 360 (Salesforce), Jonathan Vétu
- Migration en cours : Salesforce Classic → Lightning
- Migration prévue 2027 : Google Workspace → Microsoft 365
- DPO : Jonathan Guerrand | Qualiopi : Cécile Croquin | N+1 : Hugues Campan

Tu appliques les règles de priorité AFPOLS et tu réponds UNIQUEMENT en JSON valide, sans texte autour."""


# ─── Classification des mails ─────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """Pour chaque mail ci-dessous, retourne un tableau JSON.
Chaque élément doit contenir :
- "id": l'identifiant du mail
- "priority": "urgent" | "a_traiter" | "peut_attendre" | "archive"
- "category": "secu" | "equipe" | "clients" | "newsletters" | "notifications" | "reference" | "autre"
- "draft_needed": true si un brouillon de réponse doit être préparé (mails urgents avec question directe)
- "reason": 1 phrase max expliquant la décision

Règles de priorité :
URGENT si : expéditeur VIP (Hugues Campan, Jonathan Vétu, BlueBears, ArkeUp, ANSSI, CNIL, DPO),
            mots-clés sécurité (incident, phishing, cyberattaque, SOGI down, panne, breach, RGPD, violation),
            facturation bloquée, domaine/certificat expiré, demande avec délai < 24h.
À TRAITER si : demande d'action sans urgence, projet en cours, réunion à préparer.
PEUT ATTENDRE si : FYI, CC, accusé réception, compte rendu sans action requise.
ARCHIVE si : notification automatique, newsletter, promo, mail > 90 jours non lu sans importance.

Mails à classifier :
{mails_json}

Retourne UNIQUEMENT le tableau JSON."""


def classify_mails(mails: list[dict]) -> list[dict]:
    """Envoie un lot de mails à Claude pour classification."""
    if not mails:
        return []

    client = get_client()
    mails_json = json.dumps(
        [{"id": m["id"], "from": m["from"], "subject": m["subject"], "snippet": m["snippet"]} for m in mails],
        ensure_ascii=False, indent=2
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=SYSTEM_CONTEXT,
            messages=[{"role": "user", "content": CLASSIFICATION_PROMPT.format(mails_json=mails_json)}],
        )
        raw = response.content[0].text.strip()
        # Nettoyage au cas où Claude ajoute des balises markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError) as e:
        logger.error(f"Erreur parsing réponse Claude: {e}")
        return [{"id": m["id"], "priority": "a_traiter", "category": "autre",
                 "draft_needed": False, "reason": "Erreur classification"} for m in mails]
    except anthropic.APIError as e:
        logger.error(f"Erreur API Anthropic: {e}")
        return []


# ─── Génération du briefing ───────────────────────────────────────────────────

BRIEFING_PROMPT = """Génère un briefing mail HTML pour Chérif Nouredine — RSSI AFPOLS.

Date : {date}
Période : {periode}
Nouveaux mails : {nb_total} ({nb_urgent} urgents, {nb_a_traiter} à traiter, {nb_info} informationnels)

Agenda du jour :
{agenda}

Mails urgents :
{urgents_json}

Mails à traiter :
{a_traiter_json}

Mails informationnels (résumé) :
{info_json}

Génère un email HTML complet avec :
1. Un en-tête coloré (rouge pour urgents, orange pour à traiter, vert pour ok)
2. La section URGENTS avec pour chacun : expéditeur, objet, résumé 1 phrase, mention "✍️ Brouillon préparé" si applicable
3. L'agenda du jour (croisé avec les mails si lien détecté)
4. La to-do list proposée pour la journée (priorisée, avec temps estimé)
5. Les mails "à traiter" en liste compacte
6. Une suggestion courte de gestion du temps

Style : professionnel, direct, adapté à un RSSI en environnement formation.
Retourne UNIQUEMENT le HTML de l'email (pas de JSON, pas de markdown autour)."""


def generate_briefing(
    date: str,
    periode: str,
    agenda: str,
    urgents: list,
    a_traiter: list,
    informationnels: list,
) -> str:
    client = get_client()

    prompt = BRIEFING_PROMPT.format(
        date=date,
        periode=periode,
        nb_total=len(urgents) + len(a_traiter) + len(informationnels),
        nb_urgent=len(urgents),
        nb_a_traiter=len(a_traiter),
        nb_info=len(informationnels),
        agenda=agenda,
        urgents_json=json.dumps(urgents, ensure_ascii=False, indent=2),
        a_traiter_json=json.dumps(a_traiter[:10], ensure_ascii=False, indent=2),
        info_json=json.dumps([{"from": m.get("from"), "subject": m.get("subject")} for m in informationnels[:10]],
                             ensure_ascii=False, indent=2),
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Erreur génération briefing: {e}")
        return f"<p>Erreur génération briefing : {e}</p>"


# ─── Génération de brouillon de réponse ──────────────────────────────────────

DRAFT_PROMPT = """Rédige un brouillon de réponse professionnel pour ce mail.
Contexte : Chérif Nouredine, RSSI AFPOLS, répond en tant que responsable IT.
Ton : professionnel, direct, efficace. Maximum 5 lignes.
Laisse [À COMPLÉTER] pour les parties que Chérif doit personnaliser.

Mail original :
- De : {sender}
- Objet : {subject}
- Contenu : {snippet}

Retourne UNIQUEMENT le texte de la réponse, sans objet ni formule d'introduction de ta part."""


def generate_draft(mail: dict) -> str:
    client = get_client()
    prompt = DRAFT_PROMPT.format(
        sender=mail.get("from", ""),
        subject=mail.get("subject", ""),
        snippet=mail.get("snippet", ""),
    )
    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=512,
            system=SYSTEM_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Erreur génération brouillon: {e}")
        return ""


# ─── Génération débrief soir ──────────────────────────────────────────────────

DEBRIEF_PROMPT = """Génère un débrief de fin de journée HTML pour Chérif Nouredine — RSSI AFPOLS.

Date : {date}
Mails envoyés aujourd'hui : {nb_sent}
Restants urgents non traités : {nb_urgent_restants}
Restants à traiter : {nb_a_traiter_restants}

Détail urgents restants :
{urgents_restants_json}

Génère un email HTML compact avec :
1. ✅ Ce qui a été traité (estimation basée sur les envois)
2. ⏸️ Ce qui reste en suspens avec pourquoi c'est normal ou pas
3. 📅 Top 3 priorités pour demain matin
4. Un mot de clôture court (<2 lignes)

Style : synthétique, sans surplus. Chérif lit ça en 2 minutes max."""


def generate_debrief(date: str, nb_sent: int, urgents_restants: list, a_traiter_restants: list) -> str:
    client = get_client()
    prompt = DEBRIEF_PROMPT.format(
        date=date,
        nb_sent=nb_sent,
        nb_urgent_restants=len(urgents_restants),
        nb_a_traiter_restants=len(a_traiter_restants),
        urgents_restants_json=json.dumps(urgents_restants[:5], ensure_ascii=False, indent=2),
    )
    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Erreur génération débrief: {e}")
        return f"<p>Erreur débrief : {e}</p>"
