#!/usr/bin/env python3
"""
veille.py — Veille IT & Sécurité automatique AFPOLS — 7h00 chaque jour ouvré.

Sources consultées :
  - CERT-FR RSS (alertes sécurité nationales)
  - ANSSI actualités RSS
  - NVD CVE API (Salesforce, ESET, Cisco Meraki, Microsoft 365, Conga)
  - Have I Been Pwned (domaine afpols.fr)
  - Salesforce Trust API (incidents actifs)
  - Microsoft 365 Status (public)

Analyse par Gemini + envoi d'un rapport HTML pro par mail.

Usage :
    python scripts/veille.py
    python scripts/veille.py --dry-run
"""

import sys
import os
import json
import logging
import argparse
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ─── Path bootstrap (même pattern que les autres scripts) ────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import gmail_client as gmail
import claude_client as claude

# ─── Logging ─────────────────────────────────────────────────────────────────
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(config.LOG_DIR, "veille.log"), encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# ─── Constantes ───────────────────────────────────────────────────────────────
STATE_FILE  = "state/veille_last.json"
USER_AGENT  = "AFPOLS-RSSI-Veille/1.0"
HTTP_TIMEOUT = 15

CERT_FR_RSS       = "https://www.cert.ssi.gouv.fr/feed/"
CERT_FR_AVIS_RSS  = "https://www.cert.ssi.gouv.fr/avis/feed/"
NVD_CVE_URL       = "https://services.nvd.nist.gov/rest/json/cves/2.0"
SF_TRUST_URL      = "https://api.status.salesforce.com/v1/incidents"
M365_STATUS       = "https://status.office.com/api/MSCommerce/2020/Global/current"
HIBP_BREACHES     = "https://haveibeenpwned.com/api/v3/breaches"

# ── Veille IA & Évolutions ────────────────────────────────────────────────────
IA_RSS_SOURCES = [
    ("OpenAI",        "https://openai.com/blog/rss.xml"),
    ("The Verge AI",  "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
    ("MIT Tech AI",   "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("AI News",       "https://www.artificialintelligence-news.com/feed/"),
    ("ZDNet FR",      "https://www.zdnet.fr/feeds/rss/actualites/"),
    ("Silicon.fr",    "https://www.silicon.fr/feed"),
]

# Mots-clés sécurité IA (menaces, attaques IA)
IA_SECU_KEYWORDS = [
    "deepfake", "phishing ia", "ai security", "prompt injection",
    "jailbreak", "ai attack", "llm security", "model poisoning",
    "adversarial", "ai threat", "ai fraud", "ai malware",
    "cybersecurity ai", "ai vulnerability", "securite ia",
]

# Mots-clés évolutions IA (nouveautés, outils, mises à jour)
IA_EVOL_KEYWORDS = [
    "salesforce einstein", "copilot", "chatgpt", "gpt-5", "gpt-4",
    "llm", "intelligence artificielle", "ia generative", "generative ai",
    "new model", "nouveau modele", "openai", "gemini", "claude",
    "anthropic", "claude 3", "claude 4", "sonnet", "opus", "haiku",
    "hugging face", "machine learning", "ai tool", "outil ia",
    "automatisation", "workflow ai", "agent ia", "ai agent",
    "microsoft ai", "google ai", "ai update", "mise a jour ia",
    "formation ia", "ai formation", "mistral", "llama", "deepseek",
    "perplexity", "grok", "xai",
]

# Union pour le filtre global
IA_KEYWORDS = IA_SECU_KEYWORDS + IA_EVOL_KEYWORDS

NVD_KEYWORDS   = ["Salesforce", "ESET", "Cisco Meraki", "Microsoft 365", "Conga"]
CVSS_THRESHOLD = 7.0


# ─── Traduction FR ────────────────────────────────────────────────────────────

# Sources en anglais → toujours traduire
EN_SOURCES = {"OpenAI", "The Verge AI", "MIT Tech AI", "AI News"}

def _translate_to_fr(text: str, max_chars: int = 500) -> str:
    """Traduit un texte en français via Google Translate (gratuit, sans clé)."""
    if not text or not text.strip():
        return text
    try:
        from deep_translator import GoogleTranslator
        chunk = text[:max_chars].strip()
        return GoogleTranslator(source="auto", target="fr").translate(chunk) or text
    except Exception:
        return text  # fallback silencieux : texte original


def _translate_item(item: dict) -> dict:
    """Traduit title et summary d'un item si la source est anglophone."""
    if item.get("source") not in EN_SOURCES:
        return item
    item = dict(item)  # copie pour ne pas muter l'original
    item["title"]   = _translate_to_fr(item.get("title", ""), 200)
    item["summary"] = _translate_to_fr(item.get("summary", ""), 400)
    return item


# ─── Helpers HTTP ─────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict = None, timeout: int = HTTP_TIMEOUT) -> bytes | None:
    """GET simple avec gestion d'erreurs. Retourne les bytes bruts ou None."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} sur {url}: {e.reason}")
    except urllib.error.URLError as e:
        logger.warning(f"URLError sur {url}: {e.reason}")
    except Exception as e:
        logger.warning(f"Erreur inattendue sur {url}: {e}")
    return None


def _parse_rss(raw: bytes) -> list[dict]:
    """Parse un flux RSS XML et retourne la liste des items."""
    items = []
    try:
        root = ET.fromstring(raw)
        # Supprime les namespaces pour simplifier les sélecteurs
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]
        for item in root.iter("item"):
            entry = {
                "title":   (item.findtext("title") or "").strip(),
                "link":    (item.findtext("link")  or "").strip(),
                "summary": (item.findtext("description") or item.findtext("summary") or "").strip(),
                "pub_date": item.findtext("pubDate") or item.findtext("date") or "",
            }
            items.append(entry)
    except ET.ParseError as e:
        logger.warning(f"Erreur parsing RSS: {e}")
    return items


def _filter_last_24h(items: list[dict]) -> list[dict]:
    """Filtre les items RSS publiés dans les 24 dernières heures."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    recent = []
    for item in items:
        pub = item.get("pub_date", "")
        if not pub:
            # Si pas de date, on garde par défaut
            recent.append(item)
            continue
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                recent.append(item)
        except Exception:
            # Format de date non parseable → on garde l'item
            recent.append(item)
    return recent


# ─── Source 1 : CERT-FR ───────────────────────────────────────────────────────

def fetch_certfr() -> list[dict]:
    logger.info("Fetching CERT-FR RSS...")
    raw = _http_get(CERT_FR_RSS)
    if not raw:
        logger.warning("CERT-FR : aucune donnée récupérée")
        return []
    items = _parse_rss(raw)
    recent = _filter_last_24h(items)
    logger.info(f"CERT-FR : {len(recent)} alerte(s) sur 24h (total flux: {len(items)})")
    return [
        {
            "source": "CERT-FR",
            "title":   i["title"],
            "link":    i["link"],
            "summary": i["summary"][:500],
            "pub_date": i["pub_date"],
        }
        for i in recent
    ]


# ─── Source 2 : CERT-FR Avis ──────────────────────────────────────────────────

def fetch_certfr_avis() -> list[dict]:
    """Flux CERT-FR Avis (correctifs, mises à jour critiques)."""
    logger.info("Fetching CERT-FR Avis RSS...")
    raw = _http_get(CERT_FR_AVIS_RSS)
    if not raw:
        logger.warning("CERT-FR Avis : aucune donnée récupérée")
        return []
    items = _parse_rss(raw)
    recent = _filter_last_24h(items)
    logger.info(f"CERT-FR Avis : {len(recent)} avis sur 24h (total flux: {len(items)})")
    return [
        {
            "source": "CERT-FR Avis",
            "title":   i["title"],
            "link":    i["link"],
            "summary": i["summary"][:500],
            "pub_date": i["pub_date"],
        }
        for i in recent
    ]


# ─── Source 3 : NVD CVE API ───────────────────────────────────────────────────

def _extract_cvss_score(metrics: dict) -> float:
    """Extrait le score CVSS le plus élevé disponible (v4, v31, v3, v2)."""
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key, [])
        if entries:
            try:
                return float(
                    entries[0].get("cvssData", {}).get("baseScore", 0)
                    or entries[0].get("baseScore", 0)
                )
            except (TypeError, ValueError):
                continue
    return 0.0


def fetch_nvd_cves() -> list[dict]:
    """Interroge l'API NVD pour chaque keyword, filtre CVSS >= 7.0."""
    all_cves = []
    now_utc   = datetime.now(tz=timezone.utc)
    pub_start = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000")
    pub_end   = now_utc.strftime("%Y-%m-%dT%H:%M:%S.000")

    for keyword in NVD_KEYWORDS:
        logger.info(f"NVD CVE — recherche : {keyword}")
        params = urllib.parse.urlencode({
            "keywordSearch": keyword,
            "pubStartDate":  pub_start,
            "pubEndDate":    pub_end,
            "resultsPerPage": 20,
        })
        url = f"{NVD_CVE_URL}?{params}"
        raw = _http_get(url)
        if not raw:
            logger.warning(f"NVD : pas de réponse pour '{keyword}'")
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"NVD JSON error pour '{keyword}': {e}")
            continue

        for vuln in data.get("vulnerabilities", []):
            cve = vuln.get("cve", {})
            cve_id = cve.get("id", "")
            descriptions = cve.get("descriptions", [])
            description  = next(
                (d["value"] for d in descriptions if d.get("lang") == "en"),
                ""
            )[:400]
            metrics   = cve.get("metrics", {})
            score     = _extract_cvss_score(metrics)

            if score < CVSS_THRESHOLD:
                continue

            # Produit affecté
            configs = cve.get("configurations", [])
            affected = keyword  # fallback sur le mot-clé de recherche
            if configs:
                try:
                    nodes = configs[0].get("nodes", [])
                    if nodes:
                        cpe_matches = nodes[0].get("cpeMatch", [])
                        if cpe_matches:
                            cpe = cpe_matches[0].get("criteria", "")
                            # cpe:2.3:a:vendor:product:... → vendor:product
                            parts = cpe.split(":")
                            if len(parts) >= 5:
                                affected = f"{parts[3]}:{parts[4]}"
                except Exception:
                    pass

            all_cves.append({
                "source":      "NVD",
                "keyword":     keyword,
                "cve_id":      cve_id,
                "description": _translate_to_fr(description, 400),
                "cvss_score":  score,
                "affected":    affected,
                "link":        f"https://nvd.nist.gov/vuln/detail/{cve_id}",
            })

        logger.info(f"NVD '{keyword}' : {len(all_cves)} CVE(s) haute sévérité trouvés jusqu'ici")

    # Dédoublonnage par CVE ID
    seen = set()
    deduped = []
    for c in all_cves:
        if c["cve_id"] not in seen:
            seen.add(c["cve_id"])
            deduped.append(c)

    logger.info(f"NVD total : {len(deduped)} CVE(s) uniques avec CVSS >= {CVSS_THRESHOLD}")
    return deduped


# ─── Source 4 : Have I Been Pwned (domaine afpols.fr) ────────────────────────

def fetch_hibp() -> list[dict]:
    """
    Vérifie si afpols.fr est référencé dans les breaches HIBP.
    Utilise l'endpoint public /api/v3/breaches et filtre par domain.
    """
    logger.info("Fetching HIBP breaches (domaine afpols.fr)...")
    raw = _http_get(
        HIBP_BREACHES,
        headers={"User-Agent": USER_AGENT}
    )
    if not raw:
        logger.warning("HIBP : aucune donnée récupérée — skip")
        return []
    try:
        breaches = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"HIBP JSON error: {e}")
        return []

    domain_hits = [
        b for b in breaches
        if b.get("Domain", "").lower() == "afpols.fr"
    ]
    if domain_hits:
        logger.warning(f"HIBP : {len(domain_hits)} breach(es) pour afpols.fr !")
        return [
            {
                "source":      "HIBP",
                "name":        b.get("Name", ""),
                "breach_date": b.get("BreachDate", ""),
                "description": b.get("Description", "")[:300],
                "data_classes": b.get("DataClasses", []),
            }
            for b in domain_hits
        ]
    else:
        logger.info("HIBP : aucune breach pour afpols.fr")
        return []


# ─── Source 5 : Salesforce Trust ─────────────────────────────────────────────

def fetch_salesforce_status() -> list[dict]:
    logger.info("Fetching Salesforce Trust incidents...")
    raw = _http_get(SF_TRUST_URL)
    if not raw:
        logger.warning("Salesforce Trust : aucune donnée — skip")
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Salesforce Trust JSON error: {e}")
        return []

    incidents = data if isinstance(data, list) else data.get("incidents", [])
    active = [
        i for i in incidents
        if str(i.get("status", "")).lower() not in ("resolved", "closed", "")
    ]
    logger.info(f"Salesforce Trust : {len(active)} incident(s) actif(s)")
    return [
        {
            "source":      "Salesforce Trust",
            "id":          inc.get("id", ""),
            "title":       inc.get("message", inc.get("title", "Incident Salesforce")),
            "status":      inc.get("status", ""),
            "affected":    inc.get("affectedComponents", inc.get("instanceKeys", [])),
            "created_at":  inc.get("createdAt", inc.get("startTime", "")),
        }
        for inc in active
    ]


# ─── Source 6 : Microsoft 365 Status ─────────────────────────────────────────

def fetch_m365_status() -> list[dict]:
    logger.info("Fetching Microsoft 365 Status...")
    raw = _http_get(M365_STATUS)
    if not raw:
        logger.info("M365 Status : endpoint indisponible — skip")
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"M365 Status JSON error: {e} — skip")
        return []

    # L'API peut retourner différentes structures selon la version
    incidents = []
    if isinstance(data, list):
        incidents = data
    elif isinstance(data, dict):
        incidents = (
            data.get("Workloads", [])
            or data.get("workloads", [])
            or data.get("incidents", [])
            or []
        )

    # Filtre les services en incident (pas "ServiceOperational")
    issues = [
        svc for svc in incidents
        if str(svc.get("WorkloadDisplayStatus", svc.get("status", "ServiceOperational")))
        not in ("ServiceOperational", "")
    ]
    logger.info(f"M365 Status : {len(issues)} service(s) non-opérationnel(s)")
    return [
        {
            "source":  "Microsoft 365",
            "service": svc.get("WorkloadDisplayName", svc.get("service", "")),
            "status":  svc.get("WorkloadDisplayStatus", svc.get("status", "")),
        }
        for svc in issues
    ]


# ─── Source 7 : Veille IA & Évolutions ───────────────────────────────────────

def fetch_ia_news() -> dict:
    """
    Agrège les flux RSS IA et les classe en deux catégories :
    - "secu" : menaces IA (deepfake, prompt injection, AI attacks)
    - "evol" : évolutions IA (nouveaux modèles, outils, mises à jour)
    Retourne {"secu": [...], "evol": [...]}
    """
    secu_items = []
    evol_items = []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    seen_titles = set()

    for source_name, url in IA_RSS_SOURCES:
        logger.info(f"Fetching IA RSS — {source_name}...")
        raw = _http_get(url)
        if not raw:
            logger.warning(f"IA RSS {source_name} : pas de réponse")
            continue
        items = _parse_rss(raw)

        for item in items:
            # Filtre date 48h
            pub = item.get("pub_date", "")
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except Exception:
                    pass

            text_check = (item.get("title", "") + " " + item.get("summary", "")).lower()

            # Dédoublonnage
            title_key = item.get("title", "").lower()[:80]
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            entry = {
                "source":   source_name,
                "title":    item["title"],
                "link":     item["link"],
                "summary":  item["summary"][:300],
                "pub_date": item["pub_date"][:16] if item.get("pub_date") else "",
            }
            # Traduction FR pour les sources anglophones
            entry = _translate_item(entry)

            is_secu = any(kw in text_check for kw in IA_SECU_KEYWORDS)
            is_evol = any(kw in text_check for kw in IA_EVOL_KEYWORDS)

            if is_secu:
                secu_items.append(entry)
            elif is_evol:
                evol_items.append(entry)

    logger.info(f"Veille IA : {len(secu_items)} secu / {len(evol_items)} evol sur 48h")
    return {"secu": secu_items[:8], "evol": evol_items[:8]}


# ─── Collecte globale ─────────────────────────────────────────────────────────

def collect_all_data() -> dict:
    """Appelle toutes les sources et consolide les résultats."""
    return {
        "certfr":         fetch_certfr(),
        "anssi":          fetch_certfr_avis(),
        "nvd_cves":       fetch_nvd_cves(),
        "hibp":           fetch_hibp(),
        "salesforce":     fetch_salesforce_status(),
        "m365":           fetch_m365_status(),
        "ia_news":        fetch_ia_news(),   # dict {"secu": [...], "evol": [...]}
        "collected_at":   datetime.now(tz=timezone.utc).isoformat(),
    }


# ─── Analyse IA (Gemini) ──────────────────────────────────────────────────────

VEILLE_PROMPT = """Tu es RSSI à l'AFPOLS (organisme de formation logement social, ~50 collaborateurs, 100% cloud, rattaché à l'USH).
Stack IT : Salesforce/SOGI (cœur opérationnel), ESET EDR, Cisco Meraki (firewall), LockSelf (coffre-fort MDP), Microsoft 365, Conga (signature/contrats), OwnBackup (backup SF).

Voici les alertes sécu, mises à jour et actualités IA détectées aujourd'hui :

{data_json}

Réponds en JSON avec exactement cette structure :
{{
  "resume_top": "2-3 phrases MAX résumant l'essentiel à retenir aujourd'hui (ce que Cherif doit savoir sans scroller)",
  "points_critiques": ["bullet 1", "bullet 2", "bullet 3"],
  "analyse_impacts": [
    {{"alerte": "titre alerte", "impact": "1-3 phrases d'impact pour l'AFPOLS"}}
  ],
  "actions_semaine": ["action 1", "action 2", "action 3"],
  "ia_opportunites": ["opportunité IA 1 pour AFPOLS", "opportunité IA 2"],
  "amelioration_infra": "suggestion basée sur les nouveautés détectées (2-4 phrases)",
  "niveau_risque_global": "CRITIQUE | ELEVÉ | MODÉRÉ | FAIBLE"
}}

Retourne UNIQUEMENT le JSON valide."""


def analyze_with_gemini(data: dict) -> dict | None:
    """Appelle Gemini pour analyser les données de veille. Retourne None si échec."""
    summary = {
        "certfr_count":     len(data["certfr"]),
        "certfr_items":     data["certfr"][:5],
        "avis_count":       len(data["anssi"]),
        "avis_items":       data["anssi"][:3],
        "cves_high":        [c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0],
        "cves_medium":      [c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0],
        "hibp_breaches":    data["hibp"],
        "sf_incidents":     data["salesforce"],
        "m365_issues":      data["m365"],
        "ia_secu":          data.get("ia_news", {}).get("secu", [])[:5],
        "ia_evol":          data.get("ia_news", {}).get("evol", [])[:5],
    }
    prompt = VEILLE_PROMPT.format(
        data_json=json.dumps(summary, ensure_ascii=False, indent=2)
    )
    logger.info("Analyse Gemini en cours...")
    raw = claude._call_gemini(prompt, max_tokens=2048, retries=1)
    if not raw:
        logger.warning("Gemini indisponible — analyse dégradée")
        return None
    try:
        cleaned = claude._clean_json(raw)
        return json.loads(cleaned)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"Erreur parsing réponse Gemini: {e}")
        return None


def _fallback_analysis(data: dict) -> dict:
    """Analyse simplifiée sans IA quand Gemini est indisponible."""
    nb_crit = len([c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0])
    nb_high = len([c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0])
    nb_certfr = len(data["certfr"])
    nb_sf    = len(data["salesforce"])

    bullets = []
    if nb_crit:
        bullets.append(f"{nb_crit} CVE(s) critique(s) (CVSS ≥ 9.0) détectée(s) sur la stack AFPOLS")
    if nb_high:
        bullets.append(f"{nb_high} CVE(s) haute sévérité (CVSS 7-9) à évaluer")
    if nb_certfr:
        bullets.append(f"{nb_certfr} alerte(s) CERT-FR dans les dernières 24h")
    if nb_sf:
        bullets.append(f"{nb_sf} incident(s) actif(s) sur Salesforce Trust")
    if data["hibp"]:
        bullets.append("Breach HIBP détectée sur afpols.fr — vérification urgente requise")
    if not bullets:
        bullets.append("Aucune alerte critique détectée — situation normale")

    niveau = "FAIBLE"
    if nb_crit or data["hibp"]:
        niveau = "CRITIQUE"
    elif nb_high >= 2 or nb_sf:
        niveau = "ELEVÉ"
    elif nb_certfr or nb_high:
        niveau = "MODÉRÉ"

    ia = data.get("ia_news", {})
    nb_ia = len(ia.get("secu", [])) + len(ia.get("evol", []))
    return {
        "resume_top": f"{nb_certfr} alerte(s) CERT-FR · {nb_crit} CVE critique(s) · {nb_sf} incident(s) Salesforce. Niveau de risque : {niveau}. Analyse IA indisponible (quota Gemini).",
        "points_critiques": bullets,
        "analyse_impacts": [],
        "actions_semaine": [
            "Vérifier le bulletin CERT-FR du jour sur cert.ssi.gouv.fr",
            "Consulter NVD pour les CVE identifiées sur la stack",
            "Valider l'état des sauvegardes OwnBackup",
        ],
        "ia_opportunites": [f"{nb_ia} article(s) IA détecté(s) — {len(ia.get('secu',[]))} sécurité / {len(ia.get('evol',[]))} évolutions"] if nb_ia else [],
        "amelioration_infra": "Analyse IA indisponible (quota Gemini atteint). Veuillez consulter manuellement les sources référencées.",
        "niveau_risque_global": niveau,
    }


# ─── Génération HTML ───────────────────────────────────────────────────────────

SEVERITY_COLORS = {
    "CRITIQUE": ("#cc3a21", "#fff8f8", "#fdd"),
    "ELEVÉ":    ("#e6a817", "#fffbf0", "#ffe8a1"),
    "MODÉRÉ":   ("#1a73e8", "#f0f6ff", "#c9d9f5"),
    "FAIBLE":   ("#137333", "#f0faf4", "#b9e4d0"),
}


def _risk_badge(niveau: str) -> str:
    color_map = {
        "CRITIQUE": "#cc3a21",
        "ELEVÉ":    "#e6a817",
        "MODÉRÉ":   "#1a73e8",
        "FAIBLE":   "#137333",
    }
    color = color_map.get(niveau, "#888")
    return (
        f"<span style='background:{color};color:#fff;padding:3px 10px;"
        f"border-radius:12px;font-size:13px;font-weight:bold'>{niveau}</span>"
    )


def _cve_row(cve: dict) -> str:
    score = cve["cvss_score"]
    bg = "#fff0f0" if score >= 9.0 else "#fffbf0"
    badge_color = "#cc3a21" if score >= 9.0 else "#e6a817"
    return (
        f"<tr style='background:{bg}'>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;font-family:monospace;white-space:nowrap'>"
        f"<a href='{cve['link']}' style='color:#1a73e8'>{cve['cve_id']}</a></td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;font-size:13px'>{cve['description'][:250]}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center'>"
        f"<span style='background:{badge_color};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:bold'>"
        f"{score:.1f}</span></td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#555'>{cve['affected']}</td>"
        f"</tr>"
    )


def _rss_card(item: dict) -> str:
    source_color = "#cc3a21" if item["source"].startswith("CERT-FR") else "#1a0dab"
    link_part = (
        f"<a href='{item['link']}' style='color:#1a73e8;font-size:12px'>→ Lire l'alerte</a>"
        if item.get("link") else ""
    )
    return (
        f"<div style='border-left:4px solid {source_color};padding:10px 14px;"
        f"margin-bottom:10px;background:#fafafa;border-radius:0 6px 6px 0'>"
        f"<div style='font-size:11px;color:{source_color};font-weight:bold;margin-bottom:4px'>"
        f"{item['source']} — {item.get('pub_date','')[:16]}</div>"
        f"<div style='font-weight:600;margin-bottom:4px'>{item['title']}</div>"
        f"<div style='font-size:13px;color:#555;margin-bottom:6px'>{item['summary'][:200]}</div>"
        f"{link_part}"
        f"</div>"
    )


def _service_status_row(inc: dict) -> str:
    status = inc.get("status", inc.get("service", ""))
    title  = inc.get("title", inc.get("service", ""))
    return (
        f"<tr>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>"
        f"<b>{inc.get('source','')}</b></td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{title}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;"
        f"color:#cc3a21;font-weight:bold'>{status}</td>"
        f"</tr>"
    )


def _compact_cve_rows(cves: list) -> str:
    """Tableau CVE compact : 1 ligne = 1 CVE."""
    if not cves:
        return ""
    rows = ""
    for c in cves:
        score = c["cvss_score"]
        color = "#cc3a21" if score >= 9.0 else "#e6a817"
        rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-family:monospace;font-size:13px;white-space:nowrap'>"
            f"<a href='{c['link']}' style='color:#1a73e8'>{c['cve_id']}</a></td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;color:#444'>{c['description'][:180]}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;white-space:nowrap'>"
            f"<b style='color:{color}'>{score:.1f}</b></td>"
            f"</tr>"
        )
    return rows


def _compact_alert_rows(items: list, max_items: int = 6) -> str:
    """Liste CERT-FR compacte : titre + lien en 1 ligne."""
    rows = ""
    for i in items[:max_items]:
        date_str = i.get("pub_date", "")[:11]
        source_color = "#cc3a21" if i.get("source", "").startswith("CERT-FR") else "#555"
        rows += (
            f"<tr>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;"
            f"color:#888;white-space:nowrap;width:90px'>{date_str}</td>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:13px'>"
            f"<span style='color:{source_color};font-size:10px;font-weight:bold;margin-right:5px'>"
            f"{i.get('source','')}</span>"
            f"<a href='{i.get('link','')}' style='color:#333;text-decoration:none'>{i['title']}</a>"
            f"</td></tr>"
        )
    if len(items) > max_items:
        rows += (
            f"<tr><td colspan='2' style='padding:5px 10px;font-size:12px;color:#888;font-style:italic'>"
            f"+ {len(items) - max_items} autre(s) alerte(s) — <a href='https://www.cert.ssi.gouv.fr/' style='color:#1a73e8'>voir CERT-FR</a>"
            f"</td></tr>"
        )
    return rows


def _ia_col(items: list, title: str, color: str, empty_msg: str) -> str:
    """Colonne IA (sécu ou évol) pour le layout 2 colonnes."""
    html = (
        f"<div style='flex:1;min-width:280px'>"
        f"<div style='background:{color};color:#fff;padding:8px 12px;"
        f"border-radius:6px 6px 0 0;font-weight:bold;font-size:13px'>{title}</div>"
        f"<div style='border:1px solid #e0e0e0;border-top:none;border-radius:0 0 6px 6px;padding:8px'>"
    )
    if not items:
        html += f"<p style='color:#888;font-size:13px;margin:8px 4px'>{empty_msg}</p>"
    else:
        for i in items[:6]:
            src_label = f"<span style='font-size:10px;color:#888'>[{i['source']}]</span> " if i.get('source') else ""
            link = i.get('link', '')
            link_part = (
                f"<a href='{link}' style='font-size:13px;color:#1a73e8;font-weight:600;"
                f"text-decoration:underline;line-height:1.4'>{i['title']}</a>"
                f"&nbsp;<a href='{link}' style='font-size:11px;color:#888;text-decoration:none'>↗</a>"
            ) if link else f"<span style='font-size:13px;font-weight:600'>{i['title']}</span>"
            html += (
                f"<div style='padding:7px 4px;border-bottom:1px solid #f5f5f5'>"
                f"<div style='font-size:11px;color:#999;margin-bottom:2px'>"
                f"{i.get('pub_date','')[:10]} &nbsp; {src_label}</div>"
                f"{link_part}"
                f"<div style='font-size:12px;color:#666;margin-top:3px'>{i['summary'][:130]}...</div>"
                f"</div>"
            )
    html += "</div></div>"
    return html


def generate_html(data: dict, analysis: dict, date_label: str) -> str:
    cves_critical = [c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0]
    cves_high     = [c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0]
    certfr_items  = data["certfr"]
    anssi_items   = data["anssi"]
    sf_incidents  = data["salesforce"]
    m365_issues   = data["m365"]
    hibp_breaches = data["hibp"]
    ia            = data.get("ia_news", {})
    ia_secu       = ia.get("secu", []) if isinstance(ia, dict) else []
    ia_evol       = ia.get("evol", []) if isinstance(ia, dict) else []
    nb_ia         = len(ia_secu) + len(ia_evol)

    nb_critiques   = len(cves_critical) + len(hibp_breaches)
    nb_importantes = len(cves_high) + len(certfr_items) + len(sf_incidents) + len(m365_issues)

    niveau       = analysis.get("niveau_risque_global", "FAIBLE")
    header_color = SEVERITY_COLORS.get(niveau, ("#1a1a2e", "#fff", "#333"))[0]
    ras_mode = (nb_critiques == 0 and nb_importantes == 0 and not anssi_items)
    if ras_mode:
        header_color = "#137333"

    # ── Résumé 10 secondes ───────────────────────────────────────────────────
    resume_text = analysis.get("resume_top", "")
    if not resume_text:
        if ras_mode:
            resume_text = "RAS — Aucune alerte critique. Stack AFPOLS operationnelle."
        else:
            parts = []
            if nb_critiques:   parts.append(f"{nb_critiques} critique(s)")
            if cves_high:      parts.append(f"{len(cves_high)} CVE importante(s)")
            if certfr_items:   parts.append(f"{len(certfr_items)} alerte(s) CERT-FR")
            if sf_incidents:   parts.append(f"{len(sf_incidents)} incident(s) Salesforce")
            resume_text = " — ".join(parts) + f". Risque : {niveau}."

    statuts = [
        ("CVE 9+",     "🔴" if cves_critical else "🟢", str(len(cves_critical))),
        ("CVE 7-9",    "🟠" if cves_high     else "🟢", str(len(cves_high))),
        ("CERT-FR",    "🟠" if certfr_items  else "🟢", str(len(certfr_items))),
        ("HIBP",       "🔴" if hibp_breaches else "🟢", "BREACH!" if hibp_breaches else "OK"),
        ("Salesforce", "🟠" if sf_incidents  else "🟢", f"{len(sf_incidents)} inc."),
        ("IA Secu",    "🟠" if ia_secu       else "🟢", str(len(ia_secu))),
        ("IA Evol",    "🔵",                            str(len(ia_evol))),
    ]
    statuts_html = "".join(
        f"<td style='padding:8px 10px;text-align:center;border-right:1px solid rgba(255,255,255,.12)'>"
        f"<div style='font-size:16px'>{ico}</div>"
        f"<div style='font-size:9px;color:#bbb;margin-top:1px;text-transform:uppercase'>{label}</div>"
        f"<div style='font-size:13px;font-weight:bold;color:#fff'>{val}</div></td>"
        for label, ico, val in statuts
    )

    # ── Actions Gemini ───────────────────────────────────────────────────────
    actions_html = "".join(
        f"<span style='display:inline-block;background:#f0f4ff;border:1px solid #c9d9f5;"
        f"border-radius:4px;padding:4px 10px;margin:3px;font-size:12px;color:#333'>{a}</span>"
        for a in analysis.get("actions_semaine", [])
    ) or "<span style='font-size:13px;color:#888'>Verifier CERT-FR &middot; Valider sauvegardes OwnBackup &middot; Consulter NVD</span>"

    ia_opps_html = "".join(
        f"<li style='margin-bottom:4px;font-size:13px'>{o}</li>"
        for o in analysis.get("ia_opportunites", [])
    ) or "<li style='font-size:13px;color:#888'>Analyse Gemini indisponible — consulter la section IA ci-dessous.</li>"

    # ── CVE compact ──────────────────────────────────────────────────────────
    all_cves = cves_critical + cves_high
    cve_block = ""
    if all_cves:
        cve_rows = _compact_cve_rows(all_cves)
        cve_block = (
            "<table style='width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px'>"
            "<thead style='background:#f8f8f8'><tr>"
            "<th style='padding:6px 10px;text-align:left;width:130px'>CVE</th>"
            "<th style='padding:6px 10px;text-align:left'>Description</th>"
            "<th style='padding:6px 10px;text-align:center;width:55px'>Score</th>"
            f"</tr></thead><tbody>{cve_rows}</tbody></table>"
        )
    else:
        cve_block = "<p style='color:#137333;font-size:13px;margin:4px 0'>OK Aucun CVE critique ou haute severite sur la stack AFPOLS.</p>"

    # ── HIBP compact ─────────────────────────────────────────────────────────
    if hibp_breaches:
        b = hibp_breaches[0]
        hibp_block = (
            f"<div style='background:#fff0f0;border-left:4px solid #cc3a21;padding:8px 12px;"
            f"border-radius:0 4px 4px 0;font-size:13px'>"
            f"<b style='color:#cc3a21'>BREACH : {b['name']}</b> ({b['breach_date']}) — "
            f"{', '.join(b.get('data_classes',[]))[:80]}</div>"
        )
    else:
        hibp_block = "<p style='color:#137333;font-size:13px;margin:4px 0'>OK Aucune breach afpols.fr dans HIBP.</p>"

    # ── Services compact ─────────────────────────────────────────────────────
    all_incidents = sf_incidents + [
        {"source": "M365", "title": s["service"], "status": s["status"]}
        for s in m365_issues
    ]
    if all_incidents:
        svc_rows = "".join(
            f"<tr><td style='padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#888;width:100px'>{inc.get('source','')}</td>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:13px'>{inc.get('title',inc.get('service',''))}</td>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#cc3a21;font-weight:bold'>{inc.get('status','')}</td>"
            f"</tr>"
            for inc in all_incidents
        )
        svc_block = (
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<tbody>{svc_rows}</tbody></table>"
        )
    else:
        svc_block = "<p style='color:#137333;font-size:13px;margin:4px 0'>OK Salesforce et M365 operationnels.</p>"

    # ── CERT-FR compact ──────────────────────────────────────────────────────
    all_alerts = certfr_items + anssi_items
    if all_alerts:
        alert_rows = _compact_alert_rows(all_alerts, max_items=7)
        alert_block = (
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<tbody>{alert_rows}</tbody></table>"
        )
    else:
        alert_block = "<p style='color:#137333;font-size:13px;margin:4px 0'>OK Aucune alerte CERT-FR dans les 24 dernieres heures.</p>"

    # ── Section IA 2 colonnes ─────────────────────────────────────────────────
    ia_secu_col = _ia_col(
        ia_secu, "🔐 Securite IA — Menaces & Failles", "#cc3a21",
        "Aucune menace IA detectee sur les 48h. Surveillance active."
    )
    ia_evol_col = _ia_col(
        ia_evol, "🚀 Evolutions IA — Nouveautes & Outils", "#6c5ce7",
        "Aucune evolution IA detectee sur les 48h."
    )

    # ────────────────────────────────────────────────────────────────────────
    html  = "<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>"
    html += f"<title>Veille IT AFPOLS {date_label}</title></head>"
    html += "<body style='font-family:Arial,sans-serif;max-width:800px;margin:0 auto;background:#f0f2f5;padding:0'>"

    # HEADER
    html += (
        f"<div style='background:{header_color};color:#fff;padding:16px 24px;border-radius:8px 8px 0 0'>"
        f"<div style='font-size:18px;font-weight:bold'>Veille IT &amp; Securite AFPOLS &mdash; {date_label}</div>"
        f"<div style='font-size:12px;opacity:.85;margin-top:4px'>"
        f"{nb_critiques} critique(s) &nbsp;&bull;&nbsp; {nb_importantes} important(e)s"
        f" &nbsp;&bull;&nbsp; {nb_ia} actu IA &nbsp;&bull;&nbsp; Risque : {_risk_badge(niveau)}"
        f"</div></div>"
    )

    # RECAP 10 SECONDES
    html += (
        f"<div style='background:#1a1a2e;padding:14px 24px;border-top:3px solid {header_color}'>"
        f"<div style='font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:6px'>"
        f"Resume — a lire en 10 secondes</div>"
        f"<div style='font-size:14px;color:#fff;font-weight:500;line-height:1.5'>{resume_text}</div>"
        f"<table style='width:100%;margin-top:10px;border-collapse:collapse'><tr>{statuts_html}</tr></table>"
        f"</div>"
    )

    html += "<div style='background:#fff;padding:20px 24px'>"

    # ── SECU en 1 tableau compact ────────────────────────────────────────────
    html += (
        "<h2 style='color:#cc3a21;font-size:15px;border-bottom:2px solid #cc3a21;"
        "padding-bottom:5px;margin-top:0'>SECURITE — CVE &amp; Alertes</h2>"
        "<div style='display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px'>"

        # CVE block
        f"<div style='flex:3;min-width:280px'>"
        f"<div style='font-size:11px;font-weight:bold;color:#cc3a21;margin-bottom:4px;text-transform:uppercase'>CVE Stack AFPOLS</div>"
        f"{cve_block}</div>"

        # HIBP + Services block
        f"<div style='flex:2;min-width:200px'>"
        f"<div style='font-size:11px;font-weight:bold;color:#555;margin-bottom:4px;text-transform:uppercase'>HIBP afpols.fr</div>"
        f"{hibp_block}"
        f"<div style='font-size:11px;font-weight:bold;color:#555;margin-top:10px;margin-bottom:4px;text-transform:uppercase'>Services</div>"
        f"{svc_block}"
        f"</div>"
        f"</div>"

        # Alertes CERT-FR compact
        f"<div style='font-size:11px;font-weight:bold;color:#cc3a21;margin-bottom:4px;text-transform:uppercase'>"
        f"CERT-FR Alertes &amp; Avis (24h)</div>"
        f"{alert_block}"
    )

    # ── VEILLE IA 2 colonnes ─────────────────────────────────────────────────
    html += (
        "<h2 style='color:#6c5ce7;font-size:15px;border-bottom:2px solid #6c5ce7;"
        "padding-bottom:5px;margin-top:20px'>VEILLE IA &amp; EVOLUTIONS</h2>"
        f"<div style='display:flex;gap:14px;flex-wrap:wrap'>"
        f"{ia_secu_col}{ia_evol_col}"
        f"</div>"
    )

    # ── OPPORTUNITES IA (Gemini) ──────────────────────────────────────────────
    html += (
        f"<div style='background:#f5f0ff;border-left:4px solid #6c5ce7;padding:10px 14px;"
        f"border-radius:0 6px 6px 0;margin-top:12px;font-size:13px'>"
        f"<b style='color:#6c5ce7'>Opportunites IA pour AFPOLS (analyse Gemini) :</b>"
        f"<ul style='margin:6px 0 0;padding-left:18px;line-height:1.7'>{ia_opps_html}</ul>"
        f"</div>"
    )

    # ── ACTIONS ──────────────────────────────────────────────────────────────
    html += (
        "<h2 style='color:#41236d;font-size:15px;border-bottom:2px solid #41236d;"
        "padding-bottom:5px;margin-top:20px'>ACTIONS DE LA SEMAINE</h2>"
        f"<div style='margin-bottom:8px'>{actions_html}</div>"
    )

    # ── LIENS ────────────────────────────────────────────────────────────────
    html += (
        "<div style='border-top:1px solid #eee;margin-top:16px;padding-top:10px;"
        "font-size:12px;color:#888'>"
        "<a href='https://www.cert.ssi.gouv.fr/' style='color:#1a73e8;margin-right:14px'>CERT-FR</a>"
        "<a href='https://nvd.nist.gov/vuln/search' style='color:#1a73e8;margin-right:14px'>NVD CVE</a>"
        "<a href='https://trust.salesforce.com/' style='color:#1a73e8;margin-right:14px'>Salesforce Trust</a>"
        "<a href='https://openai.com/blog' style='color:#10a37f;margin-right:14px'>OpenAI</a>"
        "<a href='https://www.theverge.com/ai-artificial-intelligence' style='color:#cc6b00;margin-right:14px'>The Verge AI</a>"
        "<a href='https://haveibeenpwned.com/DomainSearch' style='color:#cc3a21;margin-right:14px'>HIBP</a>"
        "<a href='https://www.silicon.fr/' style='color:#6c5ce7'>Silicon.fr</a>"
        "</div>"
        "</div>"
    )

    # FOOTER
    html += (
        f"<div style='background:#e8eaed;padding:10px 24px;border-radius:0 0 8px 8px;"
        f"font-size:11px;color:#888;text-align:center'>"
        f"Veille auto RSSI AFPOLS &nbsp;&bull;&nbsp; CERT-FR &middot; NVD &middot; HIBP"
        f" &middot; Salesforce &middot; OpenAI &middot; The Verge AI &middot; MIT &middot; ZDNet &middot; Silicon.fr"
        f" &nbsp;&bull;&nbsp; {datetime.now(tz=timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"
        f"</div></body></html>"
    )
    return html


# ─── State persistence ────────────────────────────────────────────────────────

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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Veille IT & Sécurité AFPOLS — génère et envoie le rapport quotidien"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Simule l'envoi sans écrire dans Gmail")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        config.DRY_RUN = True

    now        = datetime.now(tz=timezone.utc)
    date_label = now.strftime("%d/%m/%Y")

    logger.info(f"=== Veille IT & Sécurité AFPOLS — {date_label} ===")

    # ── 1. Collecte des données ──────────────────────────────────────────────
    logger.info("Collecte des données de veille...")
    data = collect_all_data()

    nb_certfr    = len(data["certfr"])
    nb_anssi     = len(data["anssi"])
    nb_cves      = len(data["nvd_cves"])
    nb_sf_inc    = len(data["salesforce"])
    nb_m365_inc  = len(data["m365"])
    nb_hibp      = len(data["hibp"])

    logger.info(
        f"Collecte terminée — CERT-FR:{nb_certfr} ANSSI:{nb_anssi} "
        f"CVE:{nb_cves} SF:{nb_sf_inc} M365:{nb_m365_inc} HIBP:{nb_hibp}"
    )

    # ── 2. Analyse Gemini ────────────────────────────────────────────────────
    analysis = analyze_with_gemini(data)
    if analysis is None:
        logger.info("Gemini indisponible — fallback analyse locale")
        analysis = _fallback_analysis(data)

    niveau = analysis.get("niveau_risque_global", "FAIBLE")
    logger.info(f"Niveau de risque global : {niveau}")

    # ── 3. Génération HTML ───────────────────────────────────────────────────
    html_body = generate_html(data, analysis, date_label)

    # ── 4. Sujet email ───────────────────────────────────────────────────────
    nb_critiques  = len([c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0]) + nb_hibp
    nb_importantes = (
        len([c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0])
        + nb_certfr + nb_sf_inc + nb_m365_inc
    )
    subject = (
        f"🔍 Veille IT — {date_label} | {nb_critiques} critique(s) | {nb_importantes} alerte(s)"
    )

    # ── 5. Connexion Gmail & envoi ───────────────────────────────────────────
    logger.info("Connexion Gmail...")
    try:
        gmail_svc = gmail.get_gmail_service()
        gmail.send_briefing(gmail_svc, subject=subject, body_html=html_body)
        logger.info(f"Email veille envoyé : {subject}")
    except Exception as e:
        logger.error(f"Erreur envoi Gmail: {e}")
        raise

    # ── 6. Sauvegarde état ───────────────────────────────────────────────────
    state = load_state()
    state["veille"] = {
        "run_at":          now.isoformat(),
        "nb_certfr":       nb_certfr,
        "nb_anssi":        nb_anssi,
        "nb_cves":         nb_cves,
        "nb_critiques":    nb_critiques,
        "nb_importantes":  nb_importantes,
        "sf_incidents":    nb_sf_inc,
        "m365_issues":     nb_m365_inc,
        "hibp_breaches":   nb_hibp,
        "niveau_risque":   niveau,
        "subject_sent":    subject,
    }
    # Sauvegarde également les données brutes pour consultation
    state["veille_data"] = {
        "certfr":     data["certfr"][:10],
        "cves":       data["nvd_cves"],
        "sf":         data["salesforce"],
        "m365":       data["m365"],
        "hibp":       data["hibp"],
        "analysis":   analysis,
    }
    save_state(state)
    logger.info(f"État sauvegardé dans {STATE_FILE}")

    print(f"\n[OK] Veille IT envoyee sur {config.NOTIF_EMAIL}")
    print(f"   Risque : {niveau} | {nb_critiques} critique(s) | {nb_importantes} alerte(s)")
    print(f"   CVE : {nb_cves} | CERT-FR : {nb_certfr} | Avis : {nb_anssi}")
    if nb_hibp:
        print(f"   /!\\ BREACH HIBP detectee pour afpols.fr !!")


if __name__ == "__main__":
    main()
