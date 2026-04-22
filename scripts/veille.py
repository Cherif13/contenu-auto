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

CERT_FR_RSS       = "https://www.cert.ssi.gouv.fr/feed/"       # Alertes CERT-FR
CERT_FR_AVIS_RSS  = "https://www.cert.ssi.gouv.fr/avis/feed/"  # Avis CERT-FR (remplace ANSSI indisponible)
NVD_CVE_URL       = "https://services.nvd.nist.gov/rest/json/cves/2.0"
SF_TRUST_URL      = "https://api.status.salesforce.com/v1/incidents"
M365_STATUS  = "https://status.office.com/api/MSCommerce/2020/Global/current"
HIBP_BREACHES = "https://haveibeenpwned.com/api/v3/breaches"

NVD_KEYWORDS = ["Salesforce", "ESET", "Cisco Meraki", "Microsoft 365", "Conga"]
CVSS_THRESHOLD = 7.0


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
                "description": description,
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


# ─── Collecte globale ─────────────────────────────────────────────────────────

def collect_all_data() -> dict:
    """Appelle toutes les sources et consolide les résultats."""
    return {
        "certfr":         fetch_certfr(),
        "anssi":          fetch_certfr_avis(),   # CERT-FR Avis remplace ANSSI (RSS indisponible)
        "nvd_cves":       fetch_nvd_cves(),
        "hibp":           fetch_hibp(),
        "salesforce":     fetch_salesforce_status(),
        "m365":           fetch_m365_status(),
        "collected_at":   datetime.now(tz=timezone.utc).isoformat(),
    }


# ─── Analyse IA (Gemini) ──────────────────────────────────────────────────────

VEILLE_PROMPT = """Tu es RSSI à l'AFPOLS (organisme de formation logement social, ~50 collaborateurs, 100% cloud, rattaché à l'USH).
Stack IT : Salesforce/SOGI (cœur opérationnel), ESET EDR, Cisco Meraki (firewall), LockSelf (coffre-fort MDP), Microsoft 365, Conga (signature/contrats), OwnBackup (backup SF).

Voici les alertes sécu et mises à jour détectées aujourd'hui pour notre stack :

{data_json}

Réponds en JSON avec exactement cette structure :
{{
  "points_critiques": ["bullet 1", "bullet 2", "bullet 3"],
  "analyse_impacts": [
    {{"alerte": "titre alerte", "impact": "1-3 phrases d'impact pour l'AFPOLS"}}
  ],
  "actions_semaine": ["action 1", "action 2", "action 3"],
  "amelioration_infra": "suggestion basée sur les nouveautés détectées (2-4 phrases)",
  "niveau_risque_global": "CRITIQUE | ELEVÉ | MODÉRÉ | FAIBLE"
}}

Retourne UNIQUEMENT le JSON valide."""


def analyze_with_gemini(data: dict) -> dict | None:
    """Appelle Gemini pour analyser les données de veille. Retourne None si échec."""
    # Prépare un résumé compact pour ne pas dépasser les tokens
    summary = {
        "certfr_count":     len(data["certfr"]),
        "certfr_items":     data["certfr"][:5],
        "anssi_count":      len(data["anssi"]),
        "anssi_items":      data["anssi"][:3],
        "cves_high":        [c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0],
        "cves_medium":      [c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0],
        "hibp_breaches":    data["hibp"],
        "sf_incidents":     data["salesforce"],
        "m365_issues":      data["m365"],
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

    return {
        "points_critiques": bullets,
        "analyse_impacts": [],
        "actions_semaine": [
            "Vérifier le bulletin CERT-FR du jour sur cert.ssi.gouv.fr",
            "Consulter NVD pour les CVE identifiées sur la stack",
            "Valider l'état des sauvegardes OwnBackup",
        ],
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


def generate_html(data: dict, analysis: dict, date_label: str) -> str:
    cves_critical = [c for c in data["nvd_cves"] if c["cvss_score"] >= 9.0]
    cves_high     = [c for c in data["nvd_cves"] if 7.0 <= c["cvss_score"] < 9.0]
    certfr_items  = data["certfr"]
    anssi_items   = data["anssi"]
    sf_incidents  = data["salesforce"]
    m365_issues   = data["m365"]
    hibp_breaches = data["hibp"]

    nb_critiques  = len(cves_critical) + len(hibp_breaches)
    nb_importantes = len(cves_high) + len(certfr_items) + len(sf_incidents) + len(m365_issues)

    niveau       = analysis.get("niveau_risque_global", "FAIBLE")
    header_color = SEVERITY_COLORS.get(niveau, ("#1a1a2e", "#fff", "#333"))[0]

    ras_mode = (nb_critiques == 0 and nb_importantes == 0 and not anssi_items)
    if ras_mode:
        header_color = "#137333"

    # ── Points critiques IA ──────────────────────────────────────────────────
    bullets_html = "".join(
        f"<li style='margin-bottom:6px'>{b}</li>"
        for b in analysis.get("points_critiques", [])
    )

    # ── Impacts ─────────────────────────────────────────────────────────────
    impacts_html = ""
    for imp in analysis.get("analyse_impacts", []):
        impacts_html += (
            f"<div style='margin-bottom:10px;padding:10px;background:#f8f9fa;"
            f"border-radius:6px;border-left:3px solid #1a73e8'>"
            f"<b style='color:#1a73e8'>{imp.get('alerte','')}</b><br>"
            f"<span style='font-size:13px;color:#444'>{imp.get('impact','')}</span>"
            f"</div>"
        )
    if not impacts_html:
        impacts_html = "<p style='color:#888;font-size:13px'>Aucun impact spécifique identifié.</p>"

    # ── Actions ─────────────────────────────────────────────────────────────
    actions_html = "".join(
        f"<li style='margin-bottom:6px'>{a}</li>"
        for a in analysis.get("actions_semaine", [])
    )

    # ── CVE critiques ────────────────────────────────────────────────────────
    cve_crit_rows = "".join(_cve_row(c) for c in cves_critical) or (
        "<tr><td colspan='4' style='padding:10px;color:#137333;text-align:center'>"
        "✅ Aucun CVE critique détecté</td></tr>"
    )

    # ── CVE hauts ────────────────────────────────────────────────────────────
    cve_high_rows = "".join(_cve_row(c) for c in cves_high) or (
        "<tr><td colspan='4' style='padding:10px;color:#137333;text-align:center'>"
        "✅ Aucun CVE haute sévérité détecté</td></tr>"
    )

    # ── Alertes RSS CERT-FR / ANSSI ──────────────────────────────────────────
    rss_certfr_html = "".join(_rss_card(i) for i in certfr_items) or (
        "<p style='color:#137333'>✅ Aucune alerte CERT-FR dans les 24 dernières heures.</p>"
    )
    rss_anssi_html = "".join(_rss_card(i) for i in anssi_items) or (
        "<p style='color:#888'>Aucun avis CERT-FR dans les 24 dernières heures.</p>"
    )

    # ── HIBP ─────────────────────────────────────────────────────────────────
    hibp_html = ""
    if hibp_breaches:
        for b in hibp_breaches:
            classes = ", ".join(b.get("data_classes", [])) or "N/A"
            hibp_html += (
                f"<div style='background:#fff0f0;border:2px solid #cc3a21;border-radius:8px;"
                f"padding:14px;margin-bottom:10px'>"
                f"<b style='color:#cc3a21'>⚠️ BREACH : {b['name']}</b> — {b['breach_date']}<br>"
                f"<span style='font-size:13px'>{b['description']}</span><br>"
                f"<span style='font-size:12px;color:#555'>Données exposées : {classes}</span>"
                f"</div>"
            )
    else:
        hibp_html = "<p style='color:#137333'>✅ Aucune breach afpols.fr dans HIBP.</p>"

    # ── Services ─────────────────────────────────────────────────────────────
    all_incidents = sf_incidents + [
        {"source": "Microsoft 365", "title": s["service"], "status": s["status"]}
        for s in m365_issues
    ]
    service_rows = "".join(_service_status_row(inc) for inc in all_incidents) or (
        "<tr><td colspan='3' style='padding:10px;color:#137333;text-align:center'>"
        "✅ Tous les services opérationnels (Salesforce & M365)</td></tr>"
    )

    # ── Bilan RAS ────────────────────────────────────────────────────────────
    ras_banner = ""
    if ras_mode:
        ras_banner = (
            "<div style='background:#e6f4ea;border:2px solid #137333;border-radius:8px;"
            "padding:16px;margin-bottom:20px;text-align:center'>"
            "<span style='font-size:20px'>✅</span> "
            "<b style='color:#137333;font-size:16px'>RAS — Aucune alerte significative détectée</b><br>"
            "<span style='font-size:13px;color:#555'>Tous les services sont opérationnels. "
            "Pas de CVE critique sur la stack AFPOLS.</span>"
            "</div>"
        )

    amelioration = analysis.get("amelioration_infra", "")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Veille IT — AFPOLS — {date_label}</title>
</head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:800px;margin:0 auto;background:#f5f5f5;padding:0">

<!-- HEADER -->
<div style="background:{header_color};color:#fff;padding:24px 28px;border-radius:8px 8px 0 0">
  <h1 style="margin:0;font-size:22px">🔍 Veille IT &amp; Sécurité — AFPOLS — {date_label}</h1>
  <p style="margin:6px 0 0;opacity:.85;font-size:14px">
    {nb_critiques} critique(s) &nbsp;|&nbsp; {nb_importantes} alerte(s) importante(s)
    &nbsp;|&nbsp; Risque global : {_risk_badge(niveau)}
  </p>
</div>

<div style="background:#fff;padding:24px 28px">

  {ras_banner}

  <!-- SECTION : ALERTES CRITIQUES -->
  <h2 style="color:#cc3a21;border-bottom:2px solid #cc3a21;padding-bottom:6px">
    🚨 ALERTES CRITIQUES (CVE ≥ 9.0 &amp; Breaches)
  </h2>

  <h3 style="color:#cc3a21;font-size:15px">CVE Critiques (CVSS ≥ 9.0)</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px">
    <thead style="background:#f5f5f5">
      <tr>
        <th style="padding:8px 12px;text-align:left;width:15%">CVE ID</th>
        <th style="padding:8px 12px;text-align:left">Description</th>
        <th style="padding:8px 12px;text-align:center;width:8%">Score</th>
        <th style="padding:8px 12px;text-align:left;width:18%">Produit</th>
      </tr>
    </thead>
    <tbody>{cve_crit_rows}</tbody>
  </table>

  <h3 style="color:#cc3a21;font-size:15px">Have I Been Pwned — afpols.fr</h3>
  {hibp_html}

  <!-- SECTION : ALERTES IMPORTANTES -->
  <h2 style="color:#e6a817;border-bottom:2px solid #e6a817;padding-bottom:6px;margin-top:28px">
    ⚠️ ALERTES IMPORTANTES (CVE 7.0–8.9)
  </h2>

  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px">
    <thead style="background:#f5f5f5">
      <tr>
        <th style="padding:8px 12px;text-align:left;width:15%">CVE ID</th>
        <th style="padding:8px 12px;text-align:left">Description</th>
        <th style="padding:8px 12px;text-align:center;width:8%">Score</th>
        <th style="padding:8px 12px;text-align:left;width:18%">Produit</th>
      </tr>
    </thead>
    <tbody>{cve_high_rows}</tbody>
  </table>

  <h3 style="color:#cc3a21;font-size:15px;margin-top:20px">CERT-FR — Alertes 24h</h3>
  {rss_certfr_html}

  <h3 style="color:#cc3a21;font-size:15px;margin-top:20px">CERT-FR Avis — Correctifs 24h</h3>
  {rss_anssi_html}

  <!-- SECTION : ÉTAT DES SERVICES -->
  <h2 style="color:#1a73e8;border-bottom:2px solid #1a73e8;padding-bottom:6px;margin-top:28px">
    📊 ÉTAT DES SERVICES
  </h2>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead style="background:#f5f5f5">
      <tr>
        <th style="padding:8px 12px;text-align:left;width:20%">Plateforme</th>
        <th style="padding:8px 12px;text-align:left">Incident / Service</th>
        <th style="padding:8px 12px;text-align:left;width:20%">Statut</th>
      </tr>
    </thead>
    <tbody>{service_rows}</tbody>
  </table>

  <!-- SECTION : ANALYSE IA -->
  <h2 style="color:#41236d;border-bottom:2px solid #41236d;padding-bottom:6px;margin-top:28px">
    💡 ANALYSE IA &amp; RECOMMANDATIONS
  </h2>

  <h3 style="font-size:15px;color:#333">Points critiques</h3>
  <ul style="margin:0 0 16px;padding-left:20px;line-height:1.7">
    {bullets_html}
  </ul>

  <h3 style="font-size:15px;color:#333">Analyse d'impact AFPOLS</h3>
  {impacts_html}

  <h3 style="font-size:15px;color:#333">Actions recommandées cette semaine</h3>
  <ul style="margin:0 0 16px;padding-left:20px;line-height:1.7">
    {actions_html}
  </ul>

  <h3 style="font-size:15px;color:#333">Suggestion d'amélioration infrastructure</h3>
  <div style="background:#f0f6ff;border-left:4px solid #1a73e8;padding:12px 16px;border-radius:0 6px 6px 0;font-size:14px;color:#333">
    {amelioration or "Aucune suggestion disponible."}
  </div>

  <!-- SECTION : LIENS UTILES -->
  <h2 style="color:#555;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:28px">
    🔗 LIENS UTILES
  </h2>
  <table style="width:100%;font-size:13px">
    <tr>
      <td style="padding:6px 10px">
        <a href="https://www.cert.ssi.gouv.fr/" style="color:#1a73e8">🛡️ CERT-FR</a>
      </td>
      <td style="padding:6px 10px">
        <a href="https://nvd.nist.gov/vuln/search" style="color:#1a73e8">🔎 NVD CVE Search</a>
      </td>
      <td style="padding:6px 10px">
        <a href="https://www.cert.ssi.gouv.fr/avis/" style="color:#1a73e8">📋 CERT-FR Avis</a>
      </td>
    </tr>
    <tr>
      <td style="padding:6px 10px">
        <a href="https://trust.salesforce.com/" style="color:#1a73e8">☁️ Salesforce Trust</a>
      </td>
      <td style="padding:6px 10px">
        <a href="https://status.office.com/" style="color:#1a73e8">📧 Microsoft 365 Status</a>
      </td>
      <td style="padding:6px 10px">
        <a href="https://haveibeenpwned.com/DomainSearch" style="color:#1a73e8">🔓 HIBP Domain</a>
      </td>
    </tr>
  </table>

</div>

<!-- FOOTER -->
<div style="background:#f0f0f0;padding:12px 28px;border-radius:0 0 8px 8px;font-size:12px;color:#888;text-align:center">
  Veille générée automatiquement — RSSI AFPOLS &nbsp;|&nbsp;
  Sources : CERT-FR Alertes · CERT-FR Avis · NVD · HIBP · Salesforce Trust · Microsoft 365 &nbsp;|&nbsp;
  {datetime.now(tz=timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}
</div>

</body>
</html>"""


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
