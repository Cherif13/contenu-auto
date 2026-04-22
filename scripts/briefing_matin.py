#!/usr/bin/env python3
"""
briefing_matin.py — Briefing automatique à 8h30.
Analyse les mails depuis 18h la veille, croise avec l'agenda, prépare les brouillons urgents.

Nouveautés :
- Détection des urgents sans réponse depuis +24h (⚠️ RELANCES)
- Groupement par thread (dédoublonnage)
- Résumé audio MP3 (gTTS)
- Pré-filtre anti-spam appris

Usage :
    python scripts/briefing_matin.py
    python scripts/briefing_matin.py --dry-run
    python scripts/briefing_matin.py --since-hours 14
"""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import gmail_client as gmail
import calendar_client as cal
import claude_client as claude
import learner

os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{config.LOG_DIR}/matin.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

STATE_FILE = config.STATE_FILE


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─── Groupement par thread ─────────────────────────────────────────────────────

def deduplicate_by_thread(mails: list) -> list:
    """
    Groupe les mails par thread_id et ne conserve que le plus récent de chaque fil.
    Ajoute 'thread_count' pour indiquer combien de messages sont dans le fil.
    """
    threads: dict = {}
    for mail in mails:
        tid = mail.get("thread_id") or mail["id"]
        try:
            ts = int(mail.get("timestamp") or 0)
        except (ValueError, TypeError):
            ts = 0
        if tid not in threads or ts > threads[tid]["_ts"]:
            mail["_ts"] = ts
            mail["thread_count"] = 1
            threads[tid] = mail
        else:
            threads[tid]["thread_count"] = threads[tid].get("thread_count", 1) + 1

    result = list(threads.values())
    for m in result:
        m.pop("_ts", None)
    return result


# ─── Détection urgents sans réponse ───────────────────────────────────────────

def find_unreplied_urgents(gmail_svc, previous_urgent_ids: list) -> list:
    """
    Croise les urgents du briefing d'hier avec les mails envoyés pour détecter
    ceux qui n'ont pas reçu de réponse depuis RELANCE_HOURS heures.
    Retourne la liste des mails urgents sans réponse.
    """
    if not previous_urgent_ids:
        return []

    relance_hours = getattr(config, "RELANCE_HOURS", 24)
    cutoff_ts     = (datetime.utcnow() - timedelta(hours=relance_hours)).timestamp() * 1000

    # Récupère les threads des urgents d'hier
    unreplied = []
    try:
        # Mails envoyés depuis les dernières 48h pour couvrir les réponses possibles
        sent_msgs = gmail.list_messages(gmail_svc, "in:sent", max_results=100)
        sent_thread_ids = set()
        for s in sent_msgs:
            d = gmail.get_message_details(gmail_svc, s["id"])
            if d and d.get("thread_id"):
                sent_thread_ids.add(d["thread_id"])

        for uid in previous_urgent_ids:
            try:
                d = gmail.get_message_details(gmail_svc, uid)
                if not d:
                    continue
                thread_id = d.get("thread_id")
                ts        = int(d.get("timestamp") or 0)
                # Mail vieux de + de RELANCE_HOURS et aucune réponse dans le thread
                if ts < cutoff_ts and thread_id not in sent_thread_ids:
                    unreplied.append(d)
            except Exception as e:
                logger.debug(f"Erreur vérification relance {uid}: {e}")
    except Exception as e:
        logger.warning(f"Erreur détection relances: {e}")

    logger.info(f"{len(unreplied)} urgent(s) sans réponse depuis +{relance_hours}h")
    return unreplied


def build_relances_html(unreplied: list) -> str:
    """Génère le bloc HTML ⚠️ RELANCES NÉCESSAIRES en orange."""
    if not unreplied:
        return ""

    rows = ""
    for m in unreplied:
        rows += (
            f"<tr style='background:#fff3cd'>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #ffc107'>"
            f"<b>{m.get('from','').split('<')[0].strip()}</b>"
            f"</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #ffc107'>"
            f"{m.get('subject','')}"
            f"</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #ffc107;color:#666;font-size:13px'>"
            f"{m.get('snippet','')[:120]}"
            f"</td>"
            f"</tr>"
        )

    return f"""
<div style="background:#fff3cd;border:2px solid #ffc107;border-radius:8px;padding:16px;margin-bottom:20px">
  <h2 style="color:#856404;margin-top:0">⚠️ RELANCES NÉCESSAIRES ({len(unreplied)})</h2>
  <p style="color:#856404;margin-top:0;font-size:14px">
    Ces mails urgents d'hier n'ont pas encore reçu de réponse depuis plus de
    {getattr(config, 'RELANCE_HOURS', 24)}h.
  </p>
  <table style="width:100%;border-collapse:collapse">
    <thead>
      <tr style="background:#ffc107">
        <th style="padding:8px 12px;text-align:left;width:25%">Expéditeur</th>
        <th style="padding:8px 12px;text-align:left;width:35%">Objet</th>
        <th style="padding:8px 12px;text-align:left">Aperçu</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


# ─── Pré-classification locale (avec anti-spam) ───────────────────────────────

def pre_classify_local(mail: dict) -> str | None:
    """Pré-classification rapide : spam → archive, VIP → urgent, etc."""
    from_field = mail.get("from", "").lower()
    subject    = mail.get("subject", "").lower()
    snippet    = mail.get("snippet", "").lower()
    text       = f"{from_field} {subject} {snippet}"

    # Anti-spam appris
    try:
        if learner.is_spam_sender(from_field):
            logger.debug(f"Spam appris — archivé auto : {from_field}")
            return "archive"
    except Exception as e:
        logger.debug(f"Erreur filtre spam: {e}")

    # VIP → urgent
    for vip in config.VIP_SENDERS:
        if vip.lower() in from_field:
            return "urgent"

    # Mots-clés urgents
    for kw in config.URGENT_KEYWORDS:
        if kw.lower() in text:
            return "urgent"

    # Notifications → archive
    for domain in config.NOTIFICATION_DOMAINS:
        if domain.lower() in from_field:
            return "archive"

    # Newsletters
    for domain in config.NEWSLETTER_DOMAINS:
        if domain.lower() in from_field:
            return "archive"

    # Faible priorité
    for kw in config.LOW_PRIORITY_KEYWORDS:
        if kw.lower() in text:
            return "peut_attendre"

    return None


# ─── Résumé audio ─────────────────────────────────────────────────────────────

def build_audio_text(date_label: str, urgents: list) -> str:
    """Génère le texte très court (~15s) pour la synthèse vocale."""
    nb_urgents = len(urgents)
    if nb_urgents == 0:
        return "Bonjour Chérif. Aucun urgent ce matin. Bonne journée."
    premier_sujet = urgents[0].get("subject", "")
    return (
        f"Bonjour Chérif. {nb_urgents} urgent{'s' if nb_urgents > 1 else ''} ce matin. "
        f"Point clé : {premier_sujet}. Bonne journée."
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--since-hours", type=int, default=18,
                        help="Mails des X dernières heures (défaut: 18)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True

    now        = datetime.now()
    date_label = now.strftime("%A %d %B %Y").capitalize()
    periode    = f"depuis hier 18h ({args.since_hours}h)"

    logger.info(f"=== Briefing matin — {date_label} ===")

    # ── 1. Connexion services ────────────────────────────────────────────────
    gmail_svc = gmail.get_gmail_service()
    cal_svc   = cal.get_calendar_service()
    label_ids = gmail.get_or_create_labels(gmail_svc)

    # ── 2. Récupérer les urgents sans réponse (d'hier) ───────────────────────
    previous_state     = load_state()
    previous_urgents   = previous_state.get("briefing_matin", {}).get("urgent_ids", [])
    unreplied_urgents  = find_unreplied_urgents(gmail_svc, previous_urgents)

    # ── 3. Récupérer les mails depuis hier 18h ───────────────────────────────
    logger.info(f"Récupération des mails ({args.since_hours}h)...")
    raw_msgs = gmail.get_messages_since(gmail_svc, since_hours=args.since_hours)
    logger.info(f"{len(raw_msgs)} mails trouvés")

    mails_details = []
    for m in raw_msgs:
        details = gmail.get_message_details(gmail_svc, m["id"])
        if details:
            mails_details.append(details)

    # ── 4. Groupement par thread (dédoublonnage) ─────────────────────────────
    mails_details = deduplicate_by_thread(mails_details)
    logger.info(f"{len(mails_details)} mails après dédoublonnage par thread")

    # ── 5. Pré-classification locale + filtre spam ───────────────────────────
    to_classify = []
    pre_results = []
    spam_archived = 0

    for mail in mails_details:
        result = pre_classify_local(mail)
        if result == "archive":
            # Archiver et enregistrer le spam sender
            gmail.archive_message(gmail_svc, mail["id"])
            try:
                learner.record_archived_sender(mail.get("from", ""))
            except Exception as e:
                logger.debug(f"Erreur record spam: {e}")
            spam_archived += 1
        elif result:
            pre_results.append({
                "id": mail["id"], "priority": result,
                "category": "autre", "draft_needed": False,
                "reason": "Pré-classification locale"
            })
        else:
            to_classify.append(mail)

    if spam_archived:
        logger.info(f"{spam_archived} mail(s) archivés (spam/notif pré-filtre)")

    # ── 6. Classification Gemini par batchs ──────────────────────────────────
    all_classifications = list(pre_results)
    for i in range(0, len(to_classify), config.BATCH_SIZE):
        batch  = to_classify[i:i + config.BATCH_SIZE]
        result = claude.classify_mails(batch)
        all_classifications.extend(result)

    # Index des classifications par id
    classif_by_id = {r["id"]: r for r in all_classifications}

    urgents     = []
    a_traiter   = []
    informatifs = []

    for mail in mails_details:
        if mail.get("_archived"):
            continue
        classif  = classif_by_id.get(mail["id"], {})
        priority = classif.get("priority", "peut_attendre")
        mail["priority"] = priority
        mail["category"] = classif.get("category", "autre")
        mail["reason"]   = classif.get("reason", "")
        mail["draft_needed"] = classif.get("draft_needed", False)

        # Label thread
        subject_display = mail.get("subject", "(sans objet)")
        tc = mail.get("thread_count", 1)
        if tc > 1:
            mail["subject"] = f"{subject_display} [fil: {tc} messages]"

        # Appliquer le label
        label_key = priority if priority in label_ids else "peut_attendre"
        if mail.get("category") in label_ids:
            label_key = mail["category"]
        gmail.apply_labels(gmail_svc, mail["id"], [label_ids.get(label_key, "")])

        if priority == "urgent":
            urgents.append(mail)
        elif priority == "a_traiter":
            a_traiter.append(mail)
        elif priority != "archive":
            informatifs.append(mail)

    logger.info(f"Résultat : {len(urgents)} urgents / {len(a_traiter)} à traiter / {len(informatifs)} info")

    # ── 7. Brouillons pour les urgents ───────────────────────────────────────
    for mail in urgents:
        if mail.get("draft_needed"):
            logger.info(f"Génération brouillon pour : {mail['subject']}")
            draft_text = claude.generate_draft(mail)
            if draft_text:
                subject = f"Re: {mail['subject']}"
                body_html = f"<p>{draft_text.replace(chr(10), '<br>')}</p>"
                gmail.create_draft(gmail_svc, to=mail["from"],
                                   subject=subject, body_html=body_html,
                                   reply_to_msg_id=mail.get("thread_id"))

    # ── 8. Agenda du jour ────────────────────────────────────────────────────
    events       = cal.get_events_today(cal_svc)
    agenda_text  = cal.format_events_for_briefing(events)
    logger.info(f"Agenda : {len(events)} événements")

    # ── 9. Mise à jour apprentissage ─────────────────────────────────────────
    sent_today   = gmail.get_sent_today(gmail_svc)
    sent_details = [gmail.get_message_details(gmail_svc, m["id"]) for m in sent_today]
    learner.record_fast_reply(sent_details, mails_details)

    # ── 10. Génération briefing HTML ─────────────────────────────────────────
    logger.info("Génération du briefing avec Gemini...")
    html_briefing = claude.generate_briefing(
        date=date_label,
        periode=periode,
        agenda=agenda_text,
        urgents=urgents,
        a_traiter=a_traiter,
        informationnels=informatifs,
    )

    # ── 11. Injecter le bloc RELANCES en tête du briefing ────────────────────
    relances_html = build_relances_html(unreplied_urgents)
    if relances_html:
        if "<body" in html_briefing:
            # Insérer après la balise ouvrante <body...>
            import re
            html_briefing = re.sub(
                r"(<body[^>]*>)",
                r"\1" + relances_html,
                html_briefing,
                count=1
            )
        else:
            html_briefing = relances_html + html_briefing

    # ── 12. Envoi du briefing (avec audio si activé) ─────────────────────────
    subject_email = f"☀️ Briefing matin — {now.strftime('%d/%m/%Y')} | {len(urgents)} urgent(s)"

    if unreplied_urgents:
        subject_email += f" | ⚠️ {len(unreplied_urgents)} relance(s)"

    audio_text = build_audio_text(date_label, urgents)

    try:
        gmail.send_briefing_with_audio(
            gmail_svc,
            subject=subject_email,
            body_html=html_briefing,
            audio_text=audio_text,
        )
    except Exception as e:
        logger.error(f"Erreur envoi audio, tentative sans audio: {e}")
        gmail.send_briefing(gmail_svc, subject=subject_email, body_html=html_briefing)

    logger.info(f"Briefing envoyé : {subject_email}")

    # ── 13. Sauvegarde état ──────────────────────────────────────────────────
    state = load_state()
    state["briefing_matin"] = {
        "run_at": now.isoformat(),
        "nb_total": len(mails_details),
        "nb_urgent": len(urgents),
        "nb_a_traiter": len(a_traiter),
        "urgent_ids": [m["id"] for m in urgents],
        "a_traiter_ids": [m["id"] for m in a_traiter],
        "unreplied_ids": [m["id"] for m in unreplied_urgents],
    }
    save_state(state)

    print(f"\n[OK] Briefing matin envoye sur {config.NOTIF_EMAIL}")
    print(f"   {len(urgents)} urgent(s) | {len(a_traiter)} a traiter | {len(informatifs)} informationnels")
    if unreplied_urgents:
        print(f"   /!\\ {len(unreplied_urgents)} relance(s) necessaire(s)")


if __name__ == "__main__":
    main()
