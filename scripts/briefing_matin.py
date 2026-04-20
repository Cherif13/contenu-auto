#!/usr/bin/env python3
"""
briefing_matin.py — Briefing automatique à 8h30.
Analyse les mails depuis 18h la veille, croise avec l'agenda, prépare les brouillons urgents.

Usage :
    python scripts/briefing_matin.py
    python scripts/briefing_matin.py --dry-run
    python scripts/briefing_matin.py --since-hours 14    # mails des 14 dernières heures
"""

import sys
import os
import json
import argparse
import logging
from datetime import datetime

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
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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

    # ── 2. Récupérer les mails depuis hier 18h ───────────────────────────────
    logger.info(f"Récupération des mails ({args.since_hours}h)...")
    raw_msgs = gmail.get_messages_since(gmail_svc, since_hours=args.since_hours)
    logger.info(f"{len(raw_msgs)} mails trouvés")

    mails_details = []
    for m in raw_msgs:
        details = gmail.get_message_details(gmail_svc, m["id"])
        if details:
            mails_details.append(details)

    # ── 3. Classification par batchs ─────────────────────────────────────────
    all_classifications = []
    for i in range(0, len(mails_details), config.BATCH_SIZE):
        batch  = mails_details[i:i + config.BATCH_SIZE]
        result = claude.classify_mails(batch)
        all_classifications.extend(result)

    # Index des classifications par id
    classif_by_id = {r["id"]: r for r in all_classifications}

    urgents      = []
    a_traiter    = []
    informatifs  = []

    for mail in mails_details:
        classif  = classif_by_id.get(mail["id"], {})
        priority = classif.get("priority", "peut_attendre")
        mail["priority"] = priority
        mail["category"] = classif.get("category", "autre")
        mail["reason"]   = classif.get("reason", "")
        mail["draft_needed"] = classif.get("draft_needed", False)

        # Appliquer le label
        label_key = priority if priority in label_ids else "peut_attendre"
        if mail["category"] in label_ids:
            label_key = mail["category"]
        gmail.apply_labels(gmail_svc, mail["id"], [label_ids.get(label_key, "")])

        if priority == "urgent":
            urgents.append(mail)
        elif priority == "a_traiter":
            a_traiter.append(mail)
        else:
            informatifs.append(mail)

    logger.info(f"Résultat : {len(urgents)} urgents / {len(a_traiter)} à traiter / {len(informatifs)} info")

    # ── 4. Brouillons pour les urgents ───────────────────────────────────────
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

    # ── 5. Agenda du jour ────────────────────────────────────────────────────
    events       = cal.get_events_today(cal_svc)
    agenda_text  = cal.format_events_for_briefing(events)
    event_keys   = cal.event_keywords(events)
    logger.info(f"Agenda : {len(events)} événements")

    # ── 6. Mise à jour apprentissage ─────────────────────────────────────────
    sent_today = gmail.get_sent_today(gmail_svc)
    sent_details = [gmail.get_message_details(gmail_svc, m["id"]) for m in sent_today]
    learner.record_fast_reply(sent_details, mails_details)

    # ── 7. Génération briefing HTML ──────────────────────────────────────────
    logger.info("Génération du briefing avec Claude...")
    html_briefing = claude.generate_briefing(
        date=date_label,
        periode=periode,
        agenda=agenda_text,
        urgents=urgents,
        a_traiter=a_traiter,
        informationnels=informatifs,
    )

    # ── 8. Envoi du briefing ─────────────────────────────────────────────────
    subject = f"☀️ Briefing matin — {now.strftime('%d/%m/%Y')} | {len(urgents)} urgent(s)"
    gmail.send_briefing(gmail_svc, subject=subject, body_html=html_briefing)
    logger.info(f"Briefing envoyé : {subject}")

    # ── 9. Sauvegarde état ───────────────────────────────────────────────────
    state = load_state()
    state["briefing_matin"] = {
        "run_at": now.isoformat(),
        "nb_total": len(mails_details),
        "nb_urgent": len(urgents),
        "nb_a_traiter": len(a_traiter),
        "urgent_ids": [m["id"] for m in urgents],
        "a_traiter_ids": [m["id"] for m in a_traiter],
    }
    save_state(state)

    print(f"\n✅ Briefing matin envoyé sur {config.NOTIF_EMAIL}")
    print(f"   {len(urgents)} urgent(s) | {len(a_traiter)} à traiter | {len(informatifs)} informationnels")
    if urgents:
        print(f"   Brouillons préparés pour les mails avec réponse requise")


if __name__ == "__main__":
    main()
