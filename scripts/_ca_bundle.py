"""Contourne l'inspection TLS d'un antivirus (Avast, etc.) qui intercepte le trafic
HTTPS avec sa propre autorite racine installee dans le magasin Windows.

Deux mecanismes distincts sont necessaires car deux piles TLS differentes sont
en jeu dans ce projet :

1. curl_cffi (utilise par yfinance) ne passe pas par le module ssl de Python :
   on lui fournit un fichier CA explicite (certifi + magasin racine Windows
   exporte) via CURL_CA_BUNDLE / SSL_CERT_FILE.
2. requests/urllib3 passent par le module ssl de Python (OpenSSL), qui rejette
   le certificat racine genere par Avast car son extension Basic Constraints
   n'est pas marquee "critical" (OpenSSL 3.x est strict sur ce point, alors que
   curl_cffi/BoringSSL ne l'est pas) -- un fichier CA ne resout donc rien ici.
   On utilise `truststore` pour deleguer entierement la verification TLS a
   l'API native de Windows (SChannel), qui accepte deja ce certificat.

ponytail: regenere le fichier CA a chaque run (34 requetes, cout negligeable)
plutot que de gerer une logique de cache/invalidation.
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

_truststore_injected = False


def ensure_ca_bundle() -> str:
    """Construit (si besoin) un bundle CA = certifi + magasin racine Windows pour
    curl_cffi, et active la verification TLS native Windows (truststore) pour
    requests/urllib3. Ne fait rien sur un OS non-Windows (le probleme est
    specifique a l'inspection TLS Windows/antivirus)."""
    if sys.platform != "win32":
        return ""

    global _truststore_injected
    if not _truststore_injected:
        import truststore

        truststore.inject_into_ssl()
        _truststore_injected = True

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
    return str(BUNDLE_PATH)
