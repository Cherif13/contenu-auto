"""Wrapper Gmail API — authentification OAuth2 + opérations courantes."""

import os
import io
import json
import base64
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]

TOKEN_FILE       = "token.json"
CREDENTIALS_FILE = "credentials.json"


# ─── Authentification ─────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    "credentials.json introuvable. Suis le guide setup/guide_google_cloud.md"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ─── Labels ───────────────────────────────────────────────────────────────────

def get_or_create_labels(service) -> dict:
    """Retourne un dict {clé_interne: label_id} en créant les labels manquants."""
    existing = {l["name"]: l["id"] for l in service.users().labels().list(userId="me").execute().get("labels", [])}
    label_ids = {}

    for key, cfg in config.LABELS.items():
        name = cfg["name"]
        if name in existing:
            label_ids[key] = existing[name]
        else:
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": cfg["color"],
            }
            if not config.DRY_RUN:
                created = service.users().labels().create(userId="me", body=body).execute()
                label_ids[key] = created["id"]
                logger.info(f"Label créé : {name}")
            else:
                label_ids[key] = f"DRY_RUN_{key}"
                logger.info(f"[DRY-RUN] Label simulé : {name}")

    return label_ids


# ─── Lecture des mails ────────────────────────────────────────────────────────

def list_messages(service, query: str, max_results: int = 500) -> list:
    """Liste les messages correspondant à la query Gmail."""
    messages = []
    page_token = None

    while True:
        params = {"userId": "me", "q": query, "maxResults": min(100, max_results - len(messages))}
        if page_token:
            params["pageToken"] = page_token

        result = service.users().messages().list(**params).execute()
        messages.extend(result.get("messages", []))

        page_token = result.get("nextPageToken")
        if not page_token or len(messages) >= max_results:
            break

    return messages


def _extract_body_from_payload(payload: dict, max_chars: int = 2000) -> str:
    """Extrait le texte brut (text/plain) d'un payload Gmail (simple ou multipart)."""
    text = ""
    try:
        mime_type = payload.get("mimeType", "")
        parts     = payload.get("parts", [])

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        elif mime_type == "text/html" and not text:
            # fallback HTML si pas de text/plain trouvé
            data = payload.get("body", {}).get("data", "")
            if data:
                raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                # strip tags basique
                import re
                text = re.sub(r"<[^>]+>", " ", raw)

        elif parts:
            for part in parts:
                part_type = part.get("mimeType", "")
                if part_type == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                        break
                elif part_type.startswith("multipart/"):
                    # récursion sur parties imbriquées
                    sub_text = _extract_body_from_payload(part, max_chars)
                    if sub_text:
                        text = sub_text
                        break
            # si toujours pas de text/plain, essayer text/html
            if not text:
                for part in parts:
                    if part.get("mimeType", "") == "text/html":
                        data = part.get("body", {}).get("data", "")
                        if data:
                            import re
                            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                            text = re.sub(r"<[^>]+>", " ", raw)
                            break
    except Exception as e:
        logger.debug(f"Erreur extraction corps: {e}")

    # Nettoyage et troncature
    text = " ".join(text.split())  # normalise les espaces/retours
    return text[:max_chars]


def get_message_details(service, msg_id: str) -> dict:
    """Récupère les détails d'un message (objet, expéditeur, snippet, corps, date)."""
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        body = _extract_body_from_payload(msg["payload"], max_chars=2000)
        return {
            "id": msg_id,
            "thread_id": msg.get("threadId"),
            "subject": headers.get("Subject", "(sans objet)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
            "body": body,
            "label_ids": msg.get("labelIds", []),
            "timestamp": msg.get("internalDate"),
        }
    except HttpError as e:
        logger.warning(f"Erreur lecture message {msg_id}: {e}")
        return {}


def get_messages_since(service, since_hours: int = 18, query_extra: str = "") -> list:
    """Messages reçus dans les X dernières heures."""
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    date_str = cutoff.strftime("%Y/%m/%d")
    query = f"after:{date_str} in:inbox {query_extra}".strip()
    return list_messages(service, query)


def get_unread_messages(service, since_days: int = None) -> list:
    query = "is:unread in:inbox"
    if since_days:
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        query += f" after:{cutoff.strftime('%Y/%m/%d')}"
    return list_messages(service, query)


def get_sent_today(service) -> list:
    today = datetime.utcnow().strftime("%Y/%m/%d")
    return list_messages(service, f"in:sent after:{today}")


# ─── Modification des mails ───────────────────────────────────────────────────

def apply_labels(service, msg_id: str, add_labels: list, remove_labels: list = None):
    if config.DRY_RUN:
        logger.info(f"[DRY-RUN] Appliquer labels {add_labels} → message {msg_id}")
        return
    try:
        body = {"addLabelIds": add_labels}
        if remove_labels:
            body["removeLabelIds"] = remove_labels
        service.users().messages().modify(userId="me", id=msg_id, body=body).execute()
    except HttpError as e:
        logger.warning(f"Erreur application label sur {msg_id}: {e}")


def archive_message(service, msg_id: str):
    """Retire INBOX — archive le mail."""
    if config.DRY_RUN:
        logger.info(f"[DRY-RUN] Archiver message {msg_id}")
        return
    try:
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["INBOX"]}
        ).execute()
    except HttpError as e:
        logger.warning(f"Erreur archivage {msg_id}: {e}")


# ─── Brouillons ───────────────────────────────────────────────────────────────

def create_draft(service, to: str, subject: str, body_html: str, reply_to_msg_id: str = None):
    """Crée un brouillon Gmail (réponse ou nouveau mail)."""
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft_body = {"message": {"raw": raw}}

    if reply_to_msg_id:
        draft_body["message"]["threadId"] = reply_to_msg_id

    if config.DRY_RUN:
        logger.info(f"[DRY-RUN] Brouillon simulé → {to} : {subject}")
        return {"id": "DRY_RUN"}

    try:
        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        logger.info(f"Brouillon créé : {draft['id']} → {subject}")
        return draft
    except HttpError as e:
        logger.warning(f"Erreur création brouillon: {e}")
        return None


# ─── Envoi du briefing ────────────────────────────────────────────────────────

def send_briefing(service, subject: str, body_html: str):
    """Envoie le briefing par email sur la boîte pro."""
    msg = MIMEMultipart("alternative")
    msg["To"] = config.NOTIF_EMAIL
    msg["From"] = config.EMAIL_PRO
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    if config.DRY_RUN:
        logger.info(f"[DRY-RUN] Email briefing simulé : {subject}")
        return

    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Briefing envoyé : {subject}")
    except HttpError as e:
        logger.error(f"Erreur envoi briefing: {e}")


# ─── Briefing avec audio MP3 ──────────────────────────────────────────────────

def send_briefing_with_audio(service, subject: str, body_html: str, audio_text: str):
    """
    Génère un MP3 via ElevenLabs (voix naturelle) à partir de audio_text,
    l'attache au mail HTML de briefing et l'envoie.
    Bascule sur send_briefing() si ElevenLabs échoue.
    """
    if not getattr(config, "AUDIO_BRIEFING", True):
        logger.info("AUDIO_BRIEFING désactivé — envoi sans MP3")
        send_briefing(service, subject, body_html)
        return

    audio_bytes = None
    try:
        import asyncio
        import tempfile
        import edge_tts
        # Voix française naturelle Microsoft Neural (gratuite)
        voice = "fr-FR-DeniseNeural"
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        async def _gen():
            communicate = edge_tts.Communicate(audio_text, voice)
            await communicate.save(tmp_path)
        asyncio.run(_gen())
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
        os.unlink(tmp_path)
        logger.info("MP3 généré avec edge-tts (voix Denise, Microsoft Neural)")
    except Exception as e:
        logger.warning(f"Erreur edge-tts: {e} — envoi sans audio")

    if not audio_bytes:
        send_briefing(service, subject, body_html)
        return

    try:
        msg = MIMEMultipart("mixed")
        msg["To"]      = config.NOTIF_EMAIL
        msg["From"]    = config.EMAIL_PRO
        msg["Subject"] = subject

        # Partie HTML
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_html, "html"))
        msg.attach(alt)

        # Partie audio MP3
        audio_part = MIMEBase("audio", "mpeg")
        audio_part.set_payload(audio_bytes)
        encoders.encode_base64(audio_part)
        from datetime import date
        filename = f"briefing_{date.today().strftime('%Y%m%d')}.mp3"
        audio_part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(audio_part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Briefing audio simulé : {subject}")
            return

        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Briefing avec audio envoyé : {subject}")

    except Exception as e:
        logger.error(f"Erreur envoi briefing audio: {e} — tentative sans audio")
        send_briefing(service, subject, body_html)
