#!/usr/bin/env python3
"""
point_midi.py — Point rapide à 13h30 avant la reprise à 14h.
Analyse les mails reçus entre 9h et 13h30.

Usage :
    python scripts/point_midi.py
    python scripts/point_midi.py --dry-run
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

os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{config.LOG_DIR}/midi.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

MIDI_SINCE_HOURS = 5  # mails depuis 9h (~5h avant le run de 13h30)


def load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_midi_html(date_label: str, urgents: list, a_traiter: list, agenda_aprem: str,
                    nb_total: int) -> str:
    urgents_html = ""
    for m in urgents:
        urgents_html += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #fdd;color:#c0392b;font-weight:bold">
            {m.get('from','').split('<')[0].strip()}
          </td>
          <td style="padding:8px;border-bottom:1px solid #fdd">{m.get('subject','')}</td>
          <td style="padding:8px;border-bottom:1px solid #fdd;color:#666;font-size:13px">
            {m.get('reason',m.get('snippet','')[:80])}
          </td>
        </tr>"""

    a_traiter_html = "".join(
        f"<li>{m.get('from','').split('<')[0].strip()} — {m.get('subject','')}</li>"
        for m in a_traiter[:8]
    )

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px">

  <div style="background:#e67e22;color:white;padding:20px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;font-size:22px">🌤 Point Midi — {date_label}</h1>
    <p style="margin:5px 0 0;opacity:0.9">{nb_total} mails reçus ce matin</p>
  </div>

  <div style="background:#fff8f0;padding:15px;border:1px solid #f0c080">
    <h2 style="color:#c0392b;margin-top:0">🔴 Nouvelles urgences ({len(urgents)})</h2>
    {"<p style='color:#27ae60'>✅ Aucune nouvelle urgence ce matin.</p>" if not urgents else f'''
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#fdf2f2">
          <th style="padding:8px;text-align:left;width:25%">Expéditeur</th>
          <th style="padding:8px;text-align:left;width:40%">Objet</th>
          <th style="padding:8px;text-align:left">Résumé</th>
        </tr>
      </thead>
      <tbody>{urgents_html}</tbody>
    </table>'''}
  </div>

  <div style="background:#fffdf0;padding:15px;border:1px solid #f0e080;border-top:none">
    <h2 style="color:#e67e22;margin-top:0">🟡 À traiter cet après-midi ({len(a_traiter)})</h2>
    {"<p style='color:#666'>Rien de nouveau.</p>" if not a_traiter else f"<ul style='line-height:1.8'>{a_traiter_html}</ul>"}
  </div>

  <div style="background:#f0f8ff;padding:15px;border:1px solid #c0dff0;border-top:none">
    <h2 style="color:#2980b9;margin-top:0">🗓 Ton après-midi</h2>
    <pre style="font-family:Arial;margin:0;white-space:pre-wrap">{agenda_aprem}</pre>
  </div>

  <div style="background:#f9f9f9;padding:12px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;font-size:13px;color:#666">
    Généré automatiquement à {datetime.now().strftime('%H:%M')} — Routine Mail AFPOLS
  </div>

</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True

    now        = datetime.now()
    date_label = now.strftime("%d/%m/%Y")

    logger.info(f"=== Point midi — {date_label} ===")

    gmail_svc = gmail.get_gmail_service()
    cal_svc   = cal.get_calendar_service()
    label_ids = gmail.get_or_create_labels(gmail_svc)

    # Mails reçus depuis 9h ce matin
    raw_msgs = gmail.get_messages_since(gmail_svc, since_hours=MIDI_SINCE_HOURS)
    logger.info(f"{len(raw_msgs)} mails reçus ce matin")

    mails_details = []
    for m in raw_msgs:
        details = gmail.get_message_details(gmail_svc, m["id"])
        if details:
            mails_details.append(details)

    # Classification
    all_classif = []
    for i in range(0, len(mails_details), config.BATCH_SIZE):
        batch = mails_details[i:i + config.BATCH_SIZE]
        all_classif.extend(claude.classify_mails(batch))

    classif_by_id = {r["id"]: r for r in all_classif}
    urgents   = []
    a_traiter = []

    for mail in mails_details:
        classif  = classif_by_id.get(mail["id"], {})
        priority = classif.get("priority", "peut_attendre")
        mail["priority"] = priority
        mail["reason"]   = classif.get("reason", "")

        label_key = priority if priority in label_ids else "peut_attendre"
        gmail.apply_labels(gmail_svc, mail["id"], [label_ids.get(label_key, "")])

        if priority == "urgent":
            urgents.append(mail)
            if classif.get("draft_needed"):
                draft_text = claude.generate_draft(mail)
                if draft_text:
                    gmail.create_draft(gmail_svc, to=mail["from"],
                                       subject=f"Re: {mail['subject']}",
                                       body_html=f"<p>{draft_text.replace(chr(10),'<br>')}</p>",
                                       reply_to_msg_id=mail.get("thread_id"))
        elif priority == "a_traiter":
            a_traiter.append(mail)

    # Agenda après-midi
    events      = cal.get_events_today(cal_svc)
    agenda_text = cal.format_events_for_briefing(
        [e for e in events if e.get("start", {}).get("dateTime", "14:") >= "13:"]
    )

    html = build_midi_html(date_label, urgents, a_traiter, agenda_text, len(mails_details))
    subject = f"🌤 Point midi — {date_label} | {len(urgents)} urgent(s) nouveau(x)"
    gmail.send_briefing(gmail_svc, subject=subject, body_html=html)
    logger.info(f"Point midi envoyé : {subject}")

    # État
    state = load_state()
    state["point_midi"] = {
        "run_at": now.isoformat(),
        "nb_total": len(mails_details),
        "nb_urgent": len(urgents),
        "urgent_ids": [m["id"] for m in urgents],
    }
    save_state(state)

    print(f"\n✅ Point midi envoyé — {len(urgents)} urgent(s) / {len(a_traiter)} à traiter\n")


if __name__ == "__main__":
    main()
