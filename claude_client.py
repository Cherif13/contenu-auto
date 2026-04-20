"""Wrapper Google Gemini API — classification et génération de briefings (gratuit)."""

import json
import logging
import os
import urllib.request
import urllib.error

import config

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_URL   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_CONTEXT = """Tu es l'assistant mail de Chérif Nouredine, RSSI et Responsable Informatique à l'AFPOLS (organisme de formation logement social, ~50 collaborateurs, infrastructure 100% cloud, rattaché à l'USH).

Contexte métier :
- Système central : SOGI (Salesforce Lightning) — CRM + inscriptions + facturation
- Sécurité : EDR ESET, LockSelf, firewall Cisco Meraki
- Prestataires clés : BlueBears IT (support N1), ArkeUp 360 (Salesforce), Jonathan Vétu
- DPO : Jonathan Guerrand | N+1 : Hugues Campan (Directeur Marketing)
- Migration en cours : Salesforce Classic → Lightning

Tu réponds UNIQUEMENT en JSON valide sans texte autour, sauf quand on te demande du HTML."""


def _call_gemini(prompt: str, max_tokens: int = 4096) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY manquant dans le fichier .env")

    url  = f"{GEMINI_URL}?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{SYSTEM_CONTEXT}\n\n{prompt}"}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        logger.error(f"Erreur Gemini {e.code}: {e.read().decode()}")
        return ""
    except Exception as e:
        logger.error(f"Erreur Gemini: {e}")
        return ""


def _clean_json(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


CLASSIFICATION_PROMPT = """Pour chaque mail ci-dessous, retourne un tableau JSON.
Chaque élément contient :
- "id": identifiant du mail
- "priority": "urgent" | "a_traiter" | "peut_attendre" | "archive"
- "category": "secu" | "equipe" | "clients" | "newsletters" | "notifications" | "reference" | "autre"
- "draft_needed": true si brouillon de réponse nécessaire
- "reason": 1 phrase max

Règles AFPOLS :
URGENT : expéditeur VIP (Hugues Campan, Jonathan Vétu, BlueBears, ArkeUp, ANSSI, CNIL, DPO),
         mots-clés sécurité (incident, phishing, cyberattaque, SOGI down, panne, breach, RGPD, violation),
         facturation bloquée, domaine/certificat expiré, délai < 24h.
À TRAITER : demande d'action sans urgence, projet en cours.
PEUT ATTENDRE : FYI, CC, accusé réception.
ARCHIVE : notification automatique, newsletter, promo.

Mails :
{mails_json}

Retourne UNIQUEMENT le tableau JSON."""


def classify_mails(mails: list) -> list:
    if not mails:
        return []
    mails_json = json.dumps(
        [{"id": m["id"], "from": m["from"], "subject": m["subject"], "snippet": m["snippet"]} for m in mails],
        ensure_ascii=False, indent=2
    )
    raw = _call_gemini(CLASSIFICATION_PROMPT.format(mails_json=mails_json))
    try:
        return json.loads(_clean_json(raw))
    except json.JSONDecodeError:
        logger.error("Erreur parsing JSON Gemini")
        return [{"id": m["id"], "priority": "a_traiter", "category": "autre",
                 "draft_needed": False, "reason": "Erreur classification"} for m in mails]


BRIEFING_PROMPT = """Génère un briefing mail HTML complet pour Chérif Nouredine — RSSI AFPOLS.

Date : {date} | Période : {periode}
Mails : {nb_total} total ({nb_urgent} urgents, {nb_a_traiter} à traiter, {nb_info} info)

Agenda du jour :
{agenda}

Urgents :
{urgents_json}

À traiter :
{a_traiter_json}

Informationnels (résumé) :
{info_json}

Génère un email HTML avec :
1. En-tête coloré professionnel
2. Section URGENTS (expéditeur, objet, résumé 1 phrase, "✍️ Brouillon préparé" si applicable)
3. Agenda du jour
4. To-do list priorisée avec temps estimés
5. Liste compacte des "à traiter"
6. Suggestion de gestion du temps

Retourne UNIQUEMENT le HTML."""


def generate_briefing(date, periode, agenda, urgents, a_traiter, informationnels) -> str:
    prompt = BRIEFING_PROMPT.format(
        date=date, periode=periode,
        nb_total=len(urgents)+len(a_traiter)+len(informationnels),
        nb_urgent=len(urgents), nb_a_traiter=len(a_traiter), nb_info=len(informationnels),
        agenda=agenda,
        urgents_json=json.dumps(urgents, ensure_ascii=False, indent=2),
        a_traiter_json=json.dumps(a_traiter[:10], ensure_ascii=False, indent=2),
        info_json=json.dumps([{"from": m.get("from"), "subject": m.get("subject")} for m in informationnels[:10]], ensure_ascii=False),
    )
    result = _call_gemini(prompt, max_tokens=4096)
    return result or "<p>Erreur génération briefing.</p>"


DRAFT_PROMPT = """Rédige un brouillon de réponse professionnel pour ce mail.
Contexte : Chérif Nouredine, RSSI AFPOLS, répond en tant que responsable IT.
Ton : professionnel, direct, efficace. Maximum 5 lignes.
Laisse [À COMPLÉTER] pour les parties à personnaliser.

Mail :
- De : {sender}
- Objet : {subject}
- Contenu : {snippet}

Retourne UNIQUEMENT le texte de la réponse."""


def generate_draft(mail: dict) -> str:
    prompt = DRAFT_PROMPT.format(
        sender=mail.get("from", ""),
        subject=mail.get("subject", ""),
        snippet=mail.get("snippet", ""),
    )
    return _call_gemini(prompt, max_tokens=512)


DEBRIEF_PROMPT = """Génère un débrief de fin de journée HTML pour Chérif Nouredine — RSSI AFPOLS.

Date : {date}
Mails envoyés aujourd'hui : {nb_sent}
Urgents encore ouverts : {nb_urgent_restants}
À traiter restants : {nb_a_traiter_restants}

Urgents restants :
{urgents_restants_json}

Génère un email HTML compact avec :
1. ✅ Ce qui a été traité
2. ⏸️ Ce qui reste en suspens
3. 📅 Top 3 priorités pour demain
4. Mot de clôture court

Retourne UNIQUEMENT le HTML."""


def generate_debrief(date, nb_sent, urgents_restants, a_traiter_restants) -> str:
    prompt = DEBRIEF_PROMPT.format(
        date=date, nb_sent=nb_sent,
        nb_urgent_restants=len(urgents_restants),
        nb_a_traiter_restants=len(a_traiter_restants),
        urgents_restants_json=json.dumps(urgents_restants[:5], ensure_ascii=False, indent=2),
    )
    return _call_gemini(prompt, max_tokens=2048) or "<p>Erreur débrief.</p>"
