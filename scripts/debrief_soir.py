#!/usr/bin/env python3
"""
debrief_soir.py — Débrief de fin de journée à 18h.
Fait le bilan, repriorise pour demain, passe en mode repos.

Usage :
    python scripts/debrief_soir.py
    python scripts/debrief_soir.py --dry-run
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
        logging.FileHandler(f"{config.LOG_DIR}/soir.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_still_urgent(gmail_svc, label_ids: dict) -> list:
    """Récupère les mails encore labellisés 🔴 Urgent en fin de journée."""
    urgent_label_id = label_ids.get("urgent", "")
    if not urgent_label_id or urgent_label_id.startswith("DRY_RUN"):
        return []
    msgs = gmail.list_messages(gmail_svc, f"label:{urgent_label_id.replace(' ', '-')}", max_results=50)
    details = []
    for m in msgs[:20]:
        d = gmail.get_message_details(gmail_svc, m["id"])
        if d:
            details.append(d)
    return details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--no-repos",  action="store_true", help="Désactive le mode repos (archive notifs soirée)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True

    now        = datetime.now()
    date_label = now.strftime("%A %d %B %Y").capitalize()

    logger.info(f"=== Débrief soir — {date_label} ===")

    gmail_svc = gmail.get_gmail_service()
    cal_svc   = cal.get_calendar_service()
    label_ids = gmail.get_or_create_labels(gmail_svc)

    # Mails envoyés aujourd'hui
    sent_today   = gmail.get_sent_today(gmail_svc)
    sent_details = []
    for m in sent_today[:30]:
        d = gmail.get_message_details(gmail_svc, m["id"])
        if d:
            sent_details.append(d)

    # Apprentissage des réponses rapides
    all_today = gmail.get_messages_since(gmail_svc, since_hours=24)
    today_details = []
    for m in all_today[:50]:
        d = gmail.get_message_details(gmail_svc, m["id"])
        if d:
            today_details.append(d)
    learner.record_fast_reply(sent_details, today_details)

    # Urgents encore ouverts
    urgents_restants   = get_still_urgent(gmail_svc, label_ids)
    a_traiter_restants = gmail.list_messages(
        gmail_svc,
        f"label:{label_ids.get('a_traiter','').replace(' ','-')} is:unread",
        max_results=20
    )

    # Agenda demain
    events_demain = cal.get_events_today(cal_svc, days_ahead=1)
    agenda_demain = cal.format_events_for_briefing(events_demain)

    # Génération débrief via Claude
    html_debrief = claude.generate_debrief(
        date=date_label,
        nb_sent=len(sent_details),
        urgents_restants=urgents_restants,
        a_traiter_restants=a_traiter_restants,
    )

    # Ajout agenda demain
    agenda_block = f"""
    <div style="background:#f0f8ff;padding:15px;border:1px solid #c0dff0;margin-top:15px;border-radius:6px">
      <h2 style="color:#2980b9;margin-top:0">📅 Agenda de demain</h2>
      <pre style="font-family:Arial;white-space:pre-wrap;margin:0">{agenda_demain}</pre>
    </div>
    <div style="background:#f5f5f5;padding:15px;border:1px solid #ddd;margin-top:15px;border-radius:6px;font-size:13px;color:#666">
      🌙 Mode repos activé — les notifications reçues après 18h seront archivées demain matin.<br>
      Généré à {now.strftime('%H:%M')} — Routine Mail AFPOLS
    </div>"""

    if "</body>" in html_debrief:
        html_debrief = html_debrief.replace("</body>", agenda_block + "</body>")
    else:
        html_debrief += agenda_block

    subject = f"🌙 Débrief soir — {now.strftime('%d/%m/%Y')} | {len(urgents_restants)} urgent(s) restant(s)"
    gmail.send_briefing(gmail_svc, subject=subject, body_html=html_debrief)
    logger.info(f"Débrief soir envoyé : {subject}")

    # Mode repos : archiver les notifications automatiques reçues après 18h
    if not args.no_repos:
        notif_query = "in:inbox is:unread category:updates OR category:promotions"
        notif_msgs  = gmail.list_messages(gmail_svc, notif_query, max_results=50)
        archived_count = 0
        for m in notif_msgs:
            gmail.archive_message(gmail_svc, m["id"])
            archived_count += 1
        if archived_count:
            logger.info(f"Mode repos : {archived_count} notifications archivées")

    # Sauvegarde état
    state = load_state()
    state["debrief_soir"] = {
        "run_at": now.isoformat(),
        "nb_sent": len(sent_details),
        "nb_urgent_restants": len(urgents_restants),
        "nb_a_traiter_restants": len(a_traiter_restants),
    }
    save_state(state)

    learn_stats = learner.get_stats()
    print(f"\n✅ Débrief soir envoyé")
    print(f"   {len(sent_details)} mails envoyés aujourd'hui")
    print(f"   {len(urgents_restants)} urgent(s) encore ouverts pour demain")
    print(f"   {learn_stats['learned_vips']} expéditeur(s) appris comme quasi-VIP\n")


if __name__ == "__main__":
    main()
