"""Contourne l'inspection TLS d'un antivirus (Avast, etc.) qui intercepte le trafic
HTTPS avec sa propre autorite racine installee dans le magasin Windows mais absente
du bundle public certifi. On combine certifi + magasin racine Windows dans un fichier
local, et on pointe dessus AVANT d'importer une lib HTTP (yfinance/curl_cffi).

ponytail: regenere le fichier a chaque run (34 requetes, cout negligeable) plutot que
de gerer une logique de cache/invalidation.
"""
import os
import subprocess
import sys
from pathlib import Path

BUNDLE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "ca_bundle.pem"

_POWERSHELL_EXPORT = (
    "Get-ChildItem Cert:\\LocalMachine\\Root, Cert:\\CurrentUser\\Root "
    "| ForEach-Object { "
    "[System.Convert]::ToBase64String($_.Export('Cert'), 'InsertLineBreaks') "
    "| ForEach-Object { \"-----BEGIN CERTIFICATE-----`n$_`n-----END CERTIFICATE-----\" } "
    "}"
)


def ensure_ca_bundle() -> str:
    """Construit (si besoin) un bundle CA = certifi + magasin racine Windows, et
    configure les variables d'environnement lues par requests/curl_cffi. Ne fait
    rien sur un OS non-Windows (le probleme est specifique a l'inspection TLS
    Windows/antivirus)."""
    if sys.platform != "win32":
        return ""

    import certifi

    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)

    windows_certs = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _POWERSHELL_EXPORT],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout

    with open(certifi.where(), "r", encoding="utf-8") as f:
        combined = f.read() + "\n" + windows_certs

    BUNDLE_PATH.write_text(combined, encoding="utf-8")

    os.environ["CURL_CA_BUNDLE"] = str(BUNDLE_PATH)
    os.environ["SSL_CERT_FILE"] = str(BUNDLE_PATH)
    os.environ["REQUESTS_CA_BUNDLE"] = str(BUNDLE_PATH)
    return str(BUNDLE_PATH)
