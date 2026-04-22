#!/usr/bin/env python3
"""
rapport_hebdo.py — Rapport hebdomadaire du vendredi à 17h30.
Agrège les données de la semaine et génère un email HTML de synthèse.

Usage :
    python scripts/rapport_hebdo.py
    python scripts/rapport_hebdo.py --dry-run
"""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import gmail_client as gmail
import claude_client as claude

os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{config.LOG_DIR}/rapport_hebdo.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


# ─── Lecture de l'historique de la semaine ────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        try:
            with open(config.STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def get_week_range():
    """Retourne les dates de début (lundi) et fin (vendredi) de la semaine courante."""
    today  = datetime.now()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def collect_week_stats(gmail_svc, monday: datetime, friday: datetime) -> dict:
    """
    Collecte les statistiques de la semaine en interrogeant Gmail
    et en lisant l'état sauvegardé.
    """
    stats = {
        "total_received": 0,
        "by_priority": defaultdict(int),
        "top_senders": defaultdict(int),
        "urgents_with_timestamp": [],   # [(received_ts, replied_ts)] pour calcul temps réponse
        "nb_sent": 0,
    }

    # ── Mails reçus cette semaine ─────────────────────────────────────────────
    date_str  = monday.strftime("%Y/%m/%d")
    query     = f"in:inbox after:{date_str}"
    try:
        raw_msgs = gmail.list_messages(gmail_svc, query, max_results=500)
        stats["total_received"] = len(raw_msgs)
        logger.info(f"{len(raw_msgs)} mails reçus cette semaine")

        # Analyse des 100 premiers pour les top senders et catégories
        for m in raw_msgs[:100]:
            details = gmail.get_message_details(gmail_svc, m["id"])
            if not details:
                continue
            from_field = details.get("from", "")
            sender = from_field.split("<")[0].strip() or from_field
            stats["top_senders"][sender] += 1

            # Estimation priorité basée sur labels
            labels = details.get("label_ids", [])
            if any("Urgent" in lbl or "urgent" in lbl.lower() for lbl in labels):
                stats["by_priority"]["urgent"] += 1
                ts = int(details.get("timestamp") or 0)
                if ts:
                    stats["urgents_with_timestamp"].append({"received_ts": ts, "subject": details.get("subject", "")})
            elif any("traiter" in lbl.lower() for lbl in labels):
                stats["by_priority"]["a_traiter"] += 1
            elif any("attendre" in lbl.lower() for lbl in labels):
                stats["by_priority"]["peut_attendre"] += 1
            elif any("Newsletter" in lbl or "Notification" in lbl for lbl in labels):
                stats["by_priority"]["newsletters_notifs"] += 1
            else:
                stats["by_priority"]["autre"] += 1
    except Exception as e:
        logger.warning(f"Erreur collecte mails semaine: {e}")

    # ── Mails envoyés cette semaine ───────────────────────────────────────────
    try:
        sent_query = f"in:sent after:{date_str}"
        sent_msgs  = gmail.list_messages(gmail_svc, sent_query, max_results=200)
        stats["nb_sent"] = len(sent_msgs)
        logger.info(f"{len(sent_msgs)} mails envoyés cette semaine")
    except Exception as e:
        logger.warning(f"Erreur collecte envoyés: {e}")

    return stats


def compute_avg_response_time(urgents_ts: list, gmail_svc) -> str:
    """Calcule le temps de réponse moyen aux urgents (en heures)."""
    if not urgents_ts:
        return "N/A"
    try:
        # Récupère les mails envoyés de la semaine pour croiser les threads
        date_str  = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
        sent_msgs = gmail.list_messages(gmail_svc, f"in:sent after:{date_str}", max_results=100)
        sent_details = []
        for s in sent_msgs:
            d = gmail.get_message_details(gmail_svc, s["id"])
            if d:
                sent_details.append(d)

        sent_by_thread = {d["thread_id"]: int(d.get("timestamp") or 0) for d in sent_details if d.get("thread_id")}

        delays = []
        for urg in urgents_ts:
            rec_ts  = urg.get("received_ts", 0)
            # On cherche une réponse dans le même thread — approximation par sujet similaire
            # Pour simplifier : on prend le premier envoi après réception dans la fenêtre
            for sent_ts in sorted(sent_by_thread.values()):
                if sent_ts > rec_ts:
                    delay_h = (sent_ts - rec_ts) / (1000 * 3600)
                    if delay_h < 48:
                        delays.append(delay_h)
                    break

        if delays:
            avg = sum(delays) / len(delays)
            return f"{avg:.1f}h"
        return "N/A"
    except Exception as e:
        logger.debug(f"Erreur calcul temps réponse: {e}")
        return "N/A"


# ─── Génération HTML du rapport ───────────────────────────────────────────────

def build_rapport_html(stats: dict, prev_stats: dict | None,
                       monday: datetime, friday: datetime,
                       avg_response: str) -> str:
    now        = datetime.now()
    date_range = f"{monday.strftime('%d/%m')} – {friday.strftime('%d/%m/%Y')}"

    # Top 5 senders
    top5 = sorted(stats["top_senders"].items(), key=lambda x: x[1], reverse=True)[:5]
    top5_rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'>{i+1}.</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><b>{sender}</b></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{count}</td></tr>"
        for i, (sender, count) in enumerate(top5)
    )

    # Comparaison semaine précédente
    comp_block = ""
    if prev_stats:
        prev_total = prev_stats.get("total_received", 0)
        curr_total = stats["total_received"]
        delta      = curr_total - prev_total
        arrow      = "⬆️" if delta > 0 else ("⬇️" if delta < 0 else "➡️")
        comp_block = f"""
<div style="background:#f0f8ff;border:1px solid #c0dff0;border-radius:6px;padding:16px;margin-top:16px">
  <h3 style="color:#2980b9;margin-top:0">📊 Comparaison semaine précédente</h3>
  <p>Mails reçus : <b>{curr_total}</b> vs <b>{prev_total}</b> la semaine dernière {arrow}
  ({'+' if delta >= 0 else ''}{delta} mails)</p>
</div>"""

    # Répartition catégories
    by_prio = stats["by_priority"]
    cats_rows = ""
    cat_labels = {
        "urgent":             ("🔴", "Urgents"),
        "a_traiter":          ("🟡", "À traiter"),
        "peut_attendre":      ("🟢", "Peut attendre"),
        "newsletters_notifs": ("📰", "Newsletters / Notifications"),
        "autre":              ("⚪", "Autre"),
    }
    total_cat = sum(by_prio.values()) or 1
    for key, (icon, label) in cat_labels.items():
        count = by_prio.get(key, 0)
        pct   = round(count / total_cat * 100)
        cats_rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{icon} {label}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{count}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{pct}%</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px">

  <div style="background:#1a1a2e;color:#fff;padding:24px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;font-size:24px">📊 Rapport hebdomadaire AFPOLS</h1>
    <p style="margin:6px 0 0;opacity:0.85">Semaine du {date_range} | Généré le {now.strftime('%d/%m/%Y à %H:%M')}</p>
  </div>

  <!-- KPIs -->
  <div style="display:flex;gap:12px;padding:16px;background:#f9f9f9;border:1px solid #ddd;border-top:none;flex-wrap:wrap">
    <div style="flex:1;min-width:140px;background:#fff;border:1px solid #eee;border-radius:6px;padding:14px;text-align:center">
      <div style="font-size:32px;font-weight:bold;color:#1a1a2e">{stats['total_received']}</div>
      <div style="color:#666;font-size:13px">Mails reçus</div>
    </div>
    <div style="flex:1;min-width:140px;background:#fff;border:1px solid #eee;border-radius:6px;padding:14px;text-align:center">
      <div style="font-size:32px;font-weight:bold;color:#cc3a21">{by_prio.get('urgent', 0)}</div>
      <div style="color:#666;font-size:13px">Urgents</div>
    </div>
    <div style="flex:1;min-width:140px;background:#fff;border:1px solid #eee;border-radius:6px;padding:14px;text-align:center">
      <div style="font-size:32px;font-weight:bold;color:#27ae60">{stats['nb_sent']}</div>
      <div style="color:#666;font-size:13px">Mails envoyés</div>
    </div>
    <div style="flex:1;min-width:140px;background:#fff;border:1px solid #eee;border-radius:6px;padding:14px;text-align:center">
      <div style="font-size:32px;font-weight:bold;color:#2980b9">{avg_response}</div>
      <div style="color:#666;font-size:13px">Temps réponse moy.</div>
    </div>
  </div>

  <!-- Répartition -->
  <div style="padding:16px;border:1px solid #ddd;border-top:none">
    <h2 style="color:#1a1a2e;margin-top:0">📂 Répartition par catégorie</h2>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f5f5f5">
          <th style="padding:8px 12px;text-align:left">Catégorie</th>
          <th style="padding:8px 12px;text-align:right">Mails</th>
          <th style="padding:8px 12px;text-align:right">%</th>
        </tr>
      </thead>
      <tbody>{cats_rows}</tbody>
    </table>
  </div>

  <!-- Top senders -->
  <div style="padding:16px;border:1px solid #ddd;border-top:none">
    <h2 style="color:#1a1a2e;margin-top:0">👥 Top 5 expéditeurs</h2>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f5f5f5">
          <th style="padding:8px 12px;text-align:left">#</th>
          <th style="padding:8px 12px;text-align:left">Expéditeur</th>
          <th style="padding:8px 12px;text-align:right">Mails</th>
        </tr>
      </thead>
      <tbody>{top5_rows or "<tr><td colspan='3' style='padding:8px 12px;color:#888'>Données insuffisantes</td></tr>"}</tbody>
    </table>
  </div>

  {comp_block}

  <div style="background:#f5f5f5;padding:14px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;font-size:13px;color:#666;margin-top:0">
    Rapport généré automatiquement à {now.strftime('%H:%M')} — Routine Mail AFPOLS
  </div>

</body>
</html>"""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True

    now            = datetime.now()
    monday, friday = get_week_range()
    date_range     = f"{monday.strftime('%d/%m')} – {friday.strftime('%d/%m/%Y')}"

    logger.info(f"=== Rapport hebdomadaire — semaine {date_range} ===")

    gmail_svc = gmail.get_gmail_service()

    # Collecte des stats de la semaine courante
    stats = collect_week_stats(gmail_svc, monday, friday)

    # Temps de réponse moyen aux urgents
    avg_response = compute_avg_response_time(stats["urgents_with_timestamp"], gmail_svc)

    # Données semaine précédente (si disponibles dans l'état)
    state      = load_state()
    prev_stats = state.get("rapport_hebdo_prev")

    # Génération HTML
    html = build_rapport_html(stats, prev_stats, monday, friday, avg_response)

    # Envoi
    subject = f"📊 Rapport hebdo AFPOLS — semaine {date_range} | {stats['total_received']} mails"
    gmail.send_briefing(gmail_svc, subject=subject, body_html=html)
    logger.info(f"Rapport hebdomadaire envoyé : {subject}")

    # Sauvegarde des stats pour comparaison la semaine prochaine
    state["rapport_hebdo_prev"] = {
        "total_received": stats["total_received"],
        "nb_sent":        stats["nb_sent"],
        "by_priority":    dict(stats["by_priority"]),
        "week_of":        monday.strftime("%Y-%m-%d"),
    }
    state["rapport_hebdo"] = {
        "run_at":          now.isoformat(),
        "total_received":  stats["total_received"],
        "nb_sent":         stats["nb_sent"],
        "nb_urgent":       stats["by_priority"].get("urgent", 0),
        "avg_response":    avg_response,
    }
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] Rapport hebdomadaire envoye sur {config.NOTIF_EMAIL}")
    print(f"   Semaine : {date_range}")
    print(f"   {stats['total_received']} mails recus | {stats['nb_sent']} envoyes | temps reponse moy: {avg_response}\n")


if __name__ == "__main__":
    main()
