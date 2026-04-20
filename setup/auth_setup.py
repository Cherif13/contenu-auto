"""
Script d'authentification unique — génère token.json et token_calendar.json
puis encode tout en base64 pour GitHub Secrets.
"""
import base64, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CREDS = "credentials.json"
TOKEN_GMAIL = "token.json"
TOKEN_CAL   = "token_calendar.json"

SCOPES_GMAIL = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]
SCOPES_CAL = ["https://www.googleapis.com/auth/calendar.readonly"]

def auth(scopes, token_file, label):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    print(f"✅ {label} OK → {token_file}")
    return creds

def to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

if not os.path.exists(CREDS):
    print("❌ credentials.json introuvable dans ce dossier.")
    print("   Place le fichier téléchargé depuis Google Cloud ici :")
    print(f"   {os.path.abspath(CREDS)}")
    sys.exit(1)

print("\n=== Authentification Gmail ===")
print("Une fenêtre de navigateur va s'ouvrir → connecte-toi avec ton compte Gmail pro AFPOLS\n")
auth(SCOPES_GMAIL, TOKEN_GMAIL, "Gmail")

print("\n=== Authentification Google Calendar ===")
print("Une 2ème fenêtre va s'ouvrir → même compte Gmail pro\n")
auth(SCOPES_CAL, TOKEN_CAL, "Calendar")

print("\n" + "="*60)
print("COPIE CES VALEURS DANS GITHUB SECRETS")
print("="*60)
print("\n📌 Secret : GOOGLE_CREDENTIALS")
print(to_b64(CREDS))
print("\n📌 Secret : GOOGLE_TOKEN")
print(to_b64(TOKEN_GMAIL))
print("\n📌 Secret : GOOGLE_TOKEN_CAL")
print(to_b64(TOKEN_CAL))
print("\n" + "="*60)
print("✅ Tout est prêt. Garde cette fenêtre ouverte pour copier les valeurs.")
