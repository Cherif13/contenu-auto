"""Wrapper Google Calendar API — récupération des événements du jour."""

import os
import logging
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES_CAL       = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_CAL_FILE   = "token_calendar.json"
CREDENTIALS_FILE = "credentials.json"


def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_CAL_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_CAL_FILE, SCOPES_CAL)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES_CAL)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_CAL_FILE, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def get_events_today(service, days_ahead: int = 0) -> list:
    """Retourne les événements d'aujourd'hui (ou J+days_ahead)."""
    target = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    start  = target.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    end    = target.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=start,
            timeMax=end,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except HttpError as e:
        logger.warning(f"Erreur récupération agenda: {e}")
        return []


def format_events_for_briefing(events: list) -> str:
    """Formate les événements en texte lisible pour le briefing."""
    if not events:
        return "Aucun événement aujourd'hui."

    lines = []
    for ev in events:
        start = ev.get("start", {})
        time_str = start.get("dateTime", start.get("date", ""))
        if "T" in time_str:
            dt = datetime.fromisoformat(time_str)
            time_label = dt.strftime("%H:%M")
        else:
            time_label = "Journée"

        title    = ev.get("summary", "(sans titre)")
        location = ev.get("location", "")
        location_str = f" — {location}" if location else ""
        lines.append(f"• {time_label} : {title}{location_str}")

    return "\n".join(lines)


def get_events_next_24h(service) -> list:
    """Événements dans les prochaines 24h — pour détecter les réunions à venir."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=24)
    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except HttpError as e:
        logger.warning(f"Erreur agenda 24h: {e}")
        return []


def event_keywords(events: list) -> list:
    """Extrait les mots-clés des titres d'événements (pour croiser avec les mails)."""
    words = []
    for ev in events:
        title = ev.get("summary", "")
        words.extend([w.lower() for w in title.split() if len(w) > 3])
    return list(set(words))
