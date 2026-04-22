"""Apprentissage des habitudes mail — détecte les expéditeurs auxquels Chérif répond vite."""

import json
import logging
import os
import re
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save(path: str, data: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _extract_email(from_field: str) -> str:
    """Extrait l'adresse email brute du champ From."""
    match = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_field)
    return match.group(0).lower() if match else from_field.lower().strip()


def record_fast_reply(sent_mails: list, received_mails: list):
    """
    Compare les mails envoyés aux mails reçus.
    Si Chérif a répondu à quelqu'un en moins de FAST_REPLY_THRESHOLD_HOURS,
    incrémente son compteur dans learned_vips.json.
    """
    data = _load(config.LEARN_FILE)

    received_by_thread = {m.get("thread_id"): m for m in received_mails if m.get("thread_id")}

    for sent in sent_mails:
        thread_id = sent.get("thread_id")
        if not thread_id or thread_id not in received_by_thread:
            continue

        original = received_by_thread[thread_id]
        try:
            ts_received = int(original.get("timestamp", 0)) / 1000
            ts_sent     = int(sent.get("timestamp", 0)) / 1000
            delay_hours = (ts_sent - ts_received) / 3600

            if 0 < delay_hours <= config.FAST_REPLY_THRESHOLD_HOURS:
                email = _extract_email(original.get("from", ""))
                if email not in data:
                    data[email] = {"count": 0, "last_seen": None, "is_vip": False}
                data[email]["count"] += 1
                data[email]["last_seen"] = datetime.utcnow().isoformat()

                if data[email]["count"] >= config.VIP_LEARN_THRESHOLD:
                    if not data[email]["is_vip"]:
                        logger.info(f"Nouvel expéditeur appris comme quasi-VIP : {email}")
                    data[email]["is_vip"] = True

        except (ValueError, TypeError):
            continue

    _save(config.LEARN_FILE, data)


def get_learned_vips() -> list[str]:
    """Retourne la liste des emails appris comme quasi-VIP."""
    data = _load(config.LEARN_FILE)
    return [email for email, info in data.items() if info.get("is_vip")]


def is_learned_vip(from_field: str) -> bool:
    """Vérifie si un expéditeur est un VIP appris."""
    email = _extract_email(from_field)
    vips  = get_learned_vips()
    return any(vip in email or email in vip for vip in vips)


def record_archived_sender(sender_email: str):
    """
    Incrémente le compteur d'archivages pour un expéditeur dans spam_senders.json.
    Quand le compteur atteint SPAM_LEARN_THRESHOLD, l'expéditeur est marqué spam.
    """
    path = config.SPAM_FILE
    data = _load(path)
    email = _extract_email(sender_email)
    if email not in data:
        data[email] = {"count": 0, "blocked": False, "last_archived": None}
    data[email]["count"] += 1
    data[email]["last_archived"] = datetime.utcnow().isoformat()
    threshold = getattr(config, "SPAM_LEARN_THRESHOLD", 3)
    if data[email]["count"] >= threshold and not data[email]["blocked"]:
        data[email]["blocked"] = True
        logger.info(f"Expéditeur marqué spam auto (archivé {data[email]['count']}x) : {email}")
    _save(path, data)


def get_spam_senders() -> list:
    """
    Retourne la liste des adresses email archivées SPAM_LEARN_THRESHOLD fois ou plus.
    """
    path = config.SPAM_FILE
    data = _load(path)
    threshold = getattr(config, "SPAM_LEARN_THRESHOLD", 3)
    return [email for email, info in data.items() if info.get("count", 0) >= threshold]


def is_spam_sender(from_field: str) -> bool:
    """Vérifie si un expéditeur est dans la liste spam apprise."""
    email = _extract_email(from_field)
    spammers = get_spam_senders()
    return any(sp in email or email in sp for sp in spammers)


def get_stats() -> dict:
    """Retourne les statistiques d'apprentissage."""
    data = _load(config.LEARN_FILE)
    vips = [e for e, i in data.items() if i.get("is_vip")]
    return {
        "total_tracked": len(data),
        "learned_vips": len(vips),
        "vip_list": vips,
        "top_responders": sorted(
            [(e, i["count"]) for e, i in data.items()],
            key=lambda x: x[1],
            reverse=True
        )[:10],
    }
