"""Wrapper Google Gemini API — classification et génération de briefings (gratuit)."""

import json
import logging
import os
import time
import urllib.request
import urllib.error

import config

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_CONTEXT = """Tu es l'assistant mail de Chérif Nouredine, RSSI et Responsable Informatique à l'AFPOLS (organisme de formation logement social, ~50 collaborateurs, infrastructure 100% cloud, rattaché à l'USH).

Contexte métier :
- Système central : SOGI (Salesforce Lightning) — CRM + inscriptions + facturation
- Sécurité : EDR ESET, LockSelf, firewall Cisco Meraki
- Prestataires clés : BlueBears IT (support N1), ArkeUp 360 (Salesforce), Jonathan Vétu
- DPO : Jonathan Guerrand | N+1 : Hugues Campan (Directeur Marketing)
- Migration en cours : Salesforce Classic → Lightning

Tu réponds UNIQUEMENT en JSON valide sans texte autour, sauf quand on te demande du HTML."""


def _call_gemini(prompt: str, max_tokens: int = 4096, retries: int = 3) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY manquant dans le fichier .env")

    url  = f"{GEMINI_URL}?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{SYSTEM_CONTEXT}\n\n{prompt}"}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }).encode("utf-8")

    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            if e.code == 429:
                if attempt + 1 >= retries:
                    logger.error("Gemini indisponible après plusieurs tentatives.")
                    return ""
                wait = 60 * (attempt + 1)
                logger.warning(f"Quota Gemini atteint, attente {wait}s (tentative {attempt+1}/{retries})...")
                time.sleep(wait)
            else:
                logger.error(f"Erreur Gemini {e.code}: {error_body}")
                return ""
        except Exception as e:
            logger.error(f"Erreur Gemini: {e}")
            return ""
    logger.error("Gemini indisponible après plusieurs tentatives.")
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
    raw = _call_gemini(CLASSIFICATION_PROMPT.format(mails_json=mails_json), retries=1)
    if not raw:
        logger.warning("Gemini indisponible — classification locale par défaut.")
        return [{"id": m["id"], "priority": "a_traiter", "category": "autre",
                 "draft_needed": False, "reason": "Quota Gemini atteint"} for m in mails]
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
2. Section URGENTS — pour CHAQUE mail urgent, affiche :
   - Expéditeur et objet en gras
   - Exactement 3 bullet points résumant l'essentiel du mail (utilise le champ "body" s'il est disponible, sinon le "snippet")
   - "✍️ Brouillon préparé" si draft_needed est true
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
    result = _call_gemini(prompt, max_tokens=4096, retries=1)
    return result or _fallback_briefing(date, periode, urgents, a_traiter, informationnels)


def _fallback_briefing(date, periode, urgents, a_traiter, informationnels) -> str:
    def urgent_rows(mails):
        html = ""
        for m in mails:
            snippet_text = m.get("snippet", "")
            # Préfère le body s'il est disponible et non vide
            preview = m.get("body") or snippet_text
            preview = preview[:300] if preview else ""
            draft_badge = " &nbsp;<span style='color:#1a73e8;font-size:12px'>✍️ Brouillon préparé</span>" if m.get("draft_needed") else ""
            html += (
                f"<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #fdd;vertical-align:top'>"
                f"<b>{m.get('from','')}</b><br>"
                f"<span style='color:#333'>{m.get('subject','')}</span>{draft_badge}"
                f"</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #fdd;color:#555;font-size:13px;vertical-align:top'>"
                f"{preview}"
                f"</td>"
                f"</tr>"
            )
        return html or "<tr><td colspan='2' style='padding:6px 10px;color:#888'>Aucun urgent</td></tr>"

    def simple_rows(mails):
        html = ""
        for m in mails:
            html += f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'><b>{m.get('from','')}</b></td><td style='padding:6px 10px;border-bottom:1px solid #eee'>{m.get('subject','')}</td></tr>"
        return html or "<tr><td colspan='2' style='padding:6px 10px;color:#888'>Aucun</td></tr>"

    return f"""<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto'>
<div style='background:#1a1a2e;color:#fff;padding:20px;border-radius:8px 8px 0 0'>
  <h1 style='margin:0'>🌅 Briefing matin — {date}</h1>
  <p style='margin:5px 0 0;opacity:.8'>{periode} | {len(urgents)} urgent(s) · {len(a_traiter)} à traiter · {len(informationnels)} info</p>
</div>
<div style='padding:20px'>
  <h2 style='color:#cc3a21'>🔴 Urgents ({len(urgents)})</h2>
  <table style='width:100%;border-collapse:collapse;background:#fff8f8'>
    <thead><tr>
      <th style='padding:8px 10px;text-align:left;width:40%'>Expéditeur / Objet</th>
      <th style='padding:8px 10px;text-align:left'>Aperçu</th>
    </tr></thead>
    <tbody>{urgent_rows(urgents)}</tbody>
  </table>
  <h2 style='color:#e6a817;margin-top:24px'>🟡 À traiter ({len(a_traiter)})</h2>
  <table style='width:100%;border-collapse:collapse'>{simple_rows(a_traiter[:15])}</table>
  <p style='color:#888;font-size:12px;margin-top:20px'>Briefing généré sans IA (quota Gemini atteint)</p>
</div></body></html>"""


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
