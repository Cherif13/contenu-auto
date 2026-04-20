#!/usr/bin/env python3
"""
tri_historique.py — Nettoyage COMPLET de la boîte mail AFPOLS.
À lancer UNE seule fois pour ranger tous les anciens mails.

Usage :
    python scripts/tri_historique.py              # dry-run (simulation)
    python scripts/tri_historique.py --apply      # applique réellement
    python scripts/tri_historique.py --apply --limit 200   # limite pour test
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
import claude_client as claude

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{config.LOG_DIR}/tri_historique.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def pre_classify(mail: dict) -> str | None:
    """Pré-classification rapide sans appel Claude (économie de tokens)."""
    from_field = mail.get("from", "").lower()
    subject    = mail.get("subject", "").lower()
    snippet    = mail.get("snippet", "").lower()
    text       = f"{from_field} {subject} {snippet}"

    # VIP → urgent immédiat
    for vip in config.VIP_SENDERS:
        if vip.lower() in from_field:
            return "urgent"

    # Mots-clés urgents
    for kw in config.URGENT_KEYWORDS:
        if kw.lower() in text:
            return "urgent"

    # Notifications automatiques → archive directe
    for domain in config.NOTIFICATION_DOMAINS:
        if domain.lower() in from_field:
            return "archive"

    # Newsletters
    for domain in config.NEWSLETTER_DOMAINS:
        if domain.lower() in from_field:
            return "newsletters"

    # Mots-clés faible priorité
    for kw in config.LOW_PRIORITY_KEYWORDS:
        if kw.lower() in text:
            return "peut_attendre"

    return None  # → envoi à Claude


def process_batch(service, label_ids: dict, batch: list, stats: dict, dry_run: bool):
    """Traite un lot de mails : pré-classfie puis envoie le reste à Claude."""
    pre_classified = []
    to_claude      = []

    for mail in batch:
        result = pre_classify(mail)
        if result:
            pre_classified.append({"id": mail["id"], "priority": result,
                                   "category": "notifications" if result == "archive" else result,
                                   "draft_needed": False, "reason": "Pré-classification locale"})
        else:
            to_claude.append(mail)

    # Classification Claude pour les mails ambigus
    claude_results = claude.classify_mails(to_claude) if to_claude else []
    all_results    = pre_classified + claude_results

    for res in all_results:
        msg_id   = res["id"]
        priority = res.get("priority", "peut_attendre")
        category = res.get("category", "autre")

        # Sélection du label
        label_key = priority if priority in label_ids else "peut_attendre"
        if category in ("newsletters", "notifications"):
            label_key = category
        if category == "secu":
            label_key = "secu"
        if category == "equipe":
            label_key = "equipe"
        if category == "clients":
            label_key = "clients"

        add_labels = [label_ids[label_key]] if label_key in label_ids else []

        if priority == "archive":
            if not dry_run:
                gmail.archive_message(service, msg_id)
            stats["archived"] += 1
        else:
            if add_labels and not dry_run:
                gmail.apply_labels(service, msg_id, add_labels)
            stats[priority] = stats.get(priority, 0) + 1

        logger.debug(f"{msg_id} → {priority} / {category} : {res.get('reason', '')}")

    stats["processed"] += len(all_results)


def main():
    parser = argparse.ArgumentParser(description="Tri historique de la boîte Gmail AFPOLS")
    parser.add_argument("--apply",  action="store_true", help="Applique les modifications (sans = dry-run)")
    parser.add_argument("--limit",  type=int, default=0, help="Limite le nombre de mails traités (0 = illimité)")
    parser.add_argument("--days",   type=int, default=0, help="Ne traite que les mails des X derniers jours (0 = tout)")
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True
        print("\n" + "="*60)
        print("  MODE DRY-RUN — Aucune modification ne sera appliquée")
        print("  Relance avec --apply pour appliquer réellement")
        print("="*60 + "\n")
    else:
        print("\n" + "="*60)
        print("  ⚠️  MODE RÉEL — Les modifications SERONT appliquées")
        confirm = input("  Tape 'oui' pour confirmer : ")
        if confirm.strip().lower() != "oui":
            print("  Annulé.")
            sys.exit(0)
        print("="*60 + "\n")

    os.makedirs(config.LOG_DIR, exist_ok=True)

    logger.info("Connexion Gmail...")
    service   = gmail.get_gmail_service()
    label_ids = gmail.get_or_create_labels(service)
    logger.info(f"Labels prêts : {list(label_ids.keys())}")

    # Construction de la requête
    query = "in:inbox"
    if args.days > 0:
        cutoff  = datetime.utcnow() - timedelta(days=args.days)
        query  += f" after:{cutoff.strftime('%Y/%m/%d')}"

    logger.info(f"Récupération des mails ({query})...")
    max_results = args.limit if args.limit > 0 else 5000
    messages    = gmail.list_messages(service, query, max_results=max_results)
    total       = len(messages)
    logger.info(f"{total} mails trouvés.")

    if total == 0:
        print("Aucun mail à traiter.")
        return

    # Aperçu avant action
    print(f"\n📊 {total} mails à traiter")
    if dry_run:
        print("   (simulation — aucune modification)\n")

    stats = {"processed": 0, "archived": 0, "urgent": 0,
             "a_traiter": 0, "peut_attendre": 0, "newsletters": 0, "notifications": 0}

    # Traitement par batchs
    batch_size = config.BATCH_SIZE
    for i in range(0, total, batch_size):
        batch_ids  = messages[i:i + batch_size]
        batch_msgs = []
        for m in batch_ids:
            details = gmail.get_message_details(service, m["id"])
            if details:
                batch_msgs.append(details)

        process_batch(service, label_ids, batch_msgs, stats, dry_run)

        progress = min(i + batch_size, total)
        print(f"  [{progress}/{total}] traités...", end="\r")

    print()
    print("\n" + "="*60)
    print(f"  ✅ Tri terminé — {'SIMULATION' if dry_run else 'APPLIQUÉ'}")
    print("="*60)
    print(f"  Total traités  : {stats['processed']}")
    print(f"  🔴 Urgents      : {stats.get('urgent', 0)}")
    print(f"  🟡 À traiter   : {stats.get('a_traiter', 0)}")
    print(f"  🟢 Peut attendre: {stats.get('peut_attendre', 0)}")
    print(f"  📰 Newsletters  : {stats.get('newsletters', 0)}")
    print(f"  🔔 Notifications: {stats.get('notifications', 0)}")
    print(f"  🗄️  Archivés    : {stats['archived']}")
    print("="*60)

    if dry_run:
        print("\n👉 Pour appliquer : python scripts/tri_historique.py --apply\n")


if __name__ == "__main__":
    main()
