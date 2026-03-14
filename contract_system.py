"""
SMART CONTRACT SYSTEM - COMPLETE & FIXED
Fase 1: Organiseert PDFs automatisch met OCR support
Fase 2: Analyseert huurcontracten en genereert JSON


BELANGRIJK: Gebruik .env.local in de projectroot met alle credentials (zelfde als Next.js)
"""

import dropbox
import pdfplumber
import fitz  # PyMuPDF
import io
import os
import time
import smtplib
import json
import base64
import re
import logging
import functools
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any, Dict, List, Optional, Set, Tuple, Callable
from pdf2image import convert_from_bytes
from PIL import Image
from dotenv import load_dotenv

# Key rotator: geïntegreerd in dit bestand (geen aparte gemini_key_rotator meer)

# Load .env.local: zoek omhoog tot we het bestand vinden (projectroot), dan script dir, dan cwd
_script_dir = os.path.dirname(os.path.abspath(__file__))
_env_paths_to_try = []
# 1) Zoek omhoog naar .env.local (projectroot heeft het meestal)
_dir = _script_dir
for _ in range(5):
    _candidate = os.path.join(_dir, '.env.local')
    if os.path.isfile(_candidate):
        _env_paths_to_try.append(_candidate)
        break
    _parent = os.path.dirname(_dir)
    if _parent == _dir:
        break
    _dir = _parent
# 2) script dir en cwd
_env_paths_to_try.append(os.path.join(_script_dir, '.env.local'))
_env_paths_to_try.append(os.path.join(os.path.abspath('.'), '.env.local'))
_env_loaded_path = None
for _p in _env_paths_to_try:
    if os.path.isfile(_p) and load_dotenv(_p, override=False):
        _env_loaded_path = _p
        break
if _env_loaded_path:
    print(f"✓ Loaded .env.local from: {_env_loaded_path}")
else:
    _existing = [p for p in _env_paths_to_try if os.path.isfile(p)]
    print("⚠ No .env.local loaded. Tried:", _env_paths_to_try[:3], "(existing:" + str(_existing) + ")")

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('contract_system.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# RETRY DECORATOR (will be defined after constants)
# ============================================================================
# Note: Retry decorator implementation moved after constants definition

# ============================================================================
# CONFIGURATIE - CREDENTIALS
# ============================================================================
# All credentials are loaded from .env.local (same file as Next.js)
# Make sure .env.local in the project root contains all required variables

# FASE 1: ORGANISEER (Full access SOURCE)
APP_KEY_SOURCE_FULL = os.getenv('APP_KEY_SOURCE_FULL')
APP_SECRET_SOURCE_FULL = os.getenv('APP_SECRET_SOURCE_FULL')
REFRESH_TOKEN_SOURCE_FULL = os.getenv('REFRESH_TOKEN_SOURCE_FULL')
# Gemini key rotator: 20 keys totaal — 8 voor ordenen, 12 voor extractie. Max 18 calls per key per 24u.
NUM_KEYS_ORGANIZE = 8   # GEMINI_API_KEY_1 .. GEMINI_API_KEY_8
NUM_KEYS_EXTRACT = 12   # GEMINI_API_KEY_9 .. GEMINI_API_KEY_20
MAX_CALLS_PER_KEY_PER_24H = 18
GEMINI_ROTATOR_STATE_FILE = os.path.join(_script_dir, "gemini_key_rotator_state.json")
GEMINI_ONE_DAY_SECONDS = 24 * 3600

def _load_organize_keys():
    """Lijst van KEY_1..KEY_8 (alleen niet-lege)."""
    keys = []
    for i in range(1, NUM_KEYS_ORGANIZE + 1):
        val = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if val:
            keys.append(val)
    return keys

def _load_extract_keys():
    """Lijst van KEY_9..KEY_20 (alleen niet-lege)."""
    keys = []
    for i in range(NUM_KEYS_ORGANIZE + 1, NUM_KEYS_ORGANIZE + NUM_KEYS_EXTRACT + 1):
        val = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if val:
            keys.append(val)
    return keys

def _load_rotator_state(organize_keys, extract_keys):
    """Laad state: next_reset_at en counts voor beide pools. Bij verlopen reset: tellers als 0."""
    data = {"next_reset_at": None, "counts_organize": [], "counts_extract": []}
    if os.path.exists(GEMINI_ROTATOR_STATE_FILE):
        try:
            with open(GEMINI_ROTATOR_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, TypeError):
            pass
    co = data.get("counts_organize", [])
    ce = data.get("counts_extract", [])
    co = (co + [0] * len(organize_keys))[: len(organize_keys)]
    ce = (ce + [0] * len(extract_keys))[: len(extract_keys)]
    return co, ce, data.get("next_reset_at")

def _save_rotator_state(counts_organize, counts_extract, next_reset_at):
    with open(GEMINI_ROTATOR_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "next_reset_at": next_reset_at,
            "counts_organize": counts_organize,
            "counts_extract": counts_extract,
        }, f, indent=2)

def get_next_organize_key():
    """Volgende key voor ordenen (KEY_1..8). Max 18 calls per key per 24u; na 24u tellers op 0."""
    ok = _load_organize_keys()
    ek = _load_extract_keys()
    if not ok:
        return None, -1
    co, ce, next_reset_at = _load_rotator_state(ok, ek)
    now = time.time()
    if next_reset_at is None or now >= next_reset_at:
        co = [0] * len(ok)
        ce = [0] * len(ek)
        next_reset_at = now + GEMINI_ONE_DAY_SECONDS
        _save_rotator_state(co, ce, next_reset_at)
    best_i = -1
    best_c = MAX_CALLS_PER_KEY_PER_24H
    for i, c in enumerate(co):
        if c < best_c:
            best_c = c
            best_i = i
    if best_i < 0 or co[best_i] >= MAX_CALLS_PER_KEY_PER_24H:
        return None, -1
    co[best_i] += 1
    _save_rotator_state(co, ce, next_reset_at)
    return ok[best_i], best_i

def get_next_extract_key():
    """Volgende key voor extractie (KEY_9..20). Max 18 calls per key per 24u; na 24u tellers op 0."""
    ok = _load_organize_keys()
    ek = _load_extract_keys()
    if not ek:
        return None, -1
    co, ce, next_reset_at = _load_rotator_state(ok, ek)
    now = time.time()
    if next_reset_at is None or now >= next_reset_at:
        co = [0] * len(ok)
        ce = [0] * len(ek)
        next_reset_at = now + GEMINI_ONE_DAY_SECONDS
        _save_rotator_state(co, ce, next_reset_at)
    best_i = -1
    best_c = MAX_CALLS_PER_KEY_PER_24H
    for i, c in enumerate(ce):
        if c < best_c:
            best_c = c
            best_i = i
    if best_i < 0 or ce[best_i] >= MAX_CALLS_PER_KEY_PER_24H:
        return None, -1
    ce[best_i] += 1
    _save_rotator_state(co, ce, next_reset_at)
    return ek[best_i], best_i

def get_gemini_key_rotator_state_summary():
    """Voor weergave: (pool, key_index, count, next_reset_at). Tellers zijn 0 als reset verstreken."""
    ok = _load_organize_keys()
    ek = _load_extract_keys()
    co, ce, next_reset_at = _load_rotator_state(ok, ek)
    now = time.time()
    if next_reset_at is None or now >= next_reset_at:
        co = [0] * len(ok)
        ce = [0] * len(ek)
        next_reset_at = now + GEMINI_ONE_DAY_SECONDS
    out = []
    for i in range(len(ok)):
        out.append(("organize", i + 1, co[i] if i < len(co) else 0, next_reset_at))
    for i in range(len(ek)):
        out.append(("extract", NUM_KEYS_ORGANIZE + i + 1, ce[i] if i < len(ce) else 0, next_reset_at))
    return out

def _has_organize_keys():
    """True als minstens KEY_1..KEY_8 gezet zijn."""
    return len(_load_organize_keys()) >= 1

def _has_extract_keys():
    """True als minstens KEY_9..KEY_20 gezet zijn."""
    return len(_load_extract_keys()) >= 1

def _first_organize_key():
    """Eerste key van pool organise (voor init/model-listing; verbruikt geen rotator-slot)."""
    keys = _load_organize_keys()
    return keys[0] if keys else None

# FASE 2: ANALYSEER (Read-only SOURCE)
APP_KEY_SOURCE_RO = os.getenv('APP_KEY_SOURCE_RO')
APP_SECRET_SOURCE_RO = os.getenv('APP_SECRET_SOURCE_RO')
REFRESH_TOKEN_SOURCE_RO = os.getenv('REFRESH_TOKEN_SOURCE_RO')

# TARGET Dropbox (alleen nog voor CSV-log; JSON gaat naar Supabase)
APP_KEY_TARGET = os.getenv('APP_KEY_TARGET')
APP_SECRET_TARGET = os.getenv('APP_SECRET_TARGET')
REFRESH_TOKEN_TARGET = os.getenv('REFRESH_TOKEN_TARGET')

# Supabase (JSON contract storage, vervangt Dropbox TARGET voor bestanden)
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')

# EMAIL
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
SENDER_PASSWORD = os.getenv('SENDER_PASSWORD')
RECIPIENT_EMAIL = [os.getenv('RECIPIENT_EMAIL', '')]

# ============================================================================
# CONFIGURATIE - SETTINGS
# ============================================================================

ORGANIZED_FOLDER_PREFIX = '/Georganiseerd'
SCAN_ROOT = ''
CHECK_INTERVAL = 20
BATCH_SIZE = 5

# History files: vast pad in alexander/ zodat cwd geen verschil maakt (geen dubbele verwerking)
ORGANIZED_HISTORY = os.path.join(_script_dir, "organized_history.txt")
ANALYZED_HISTORY = os.path.join(_script_dir, "analyzed_docs.txt")
FOLDER_CACHE = os.path.join(_script_dir, "folder_structure.json")

# CSV log in TARGET
CSV_LOG_PATH = "/verwerking_log.csv"

# Retry settings
MAX_RETRIES = 3
RETRY_WAIT = 15
RATE_LIMIT_WAIT = 90
QUOTA_EXCEEDED_WAIT = 3600

# Confidence target
TARGET_CONFIDENCE = 95

# Folders to exclude from organizing
EXCLUDE_FOLDERS = {
    '/Camera Uploads',
    '/.dropbox',
    '/Apps',
    ORGANIZED_FOLDER_PREFIX
}

# Keywords for rental contract folders
RENTAL_KEYWORDS = [
    'huur', 'verhuur', 'rental', 'lease',
    'huurcontract', 'huurovereenkomst'
]

# ============================================================================
# TEXT EXTRACTION CONSTANTS
# ============================================================================

TEXT_SAMPLE_SIZE = 3500  # Characters for classification (legacy)
ORGANIZE_AND_SUMMARY_TEXT_SIZE = 45000  # Full doc text for 1 call: classify + summary (Plan: stap 1+6)
TEXT_CHUNK_1_SIZE = 20000  # First chunk for extraction
TEXT_CHUNK_2_SIZE = 35000  # Second chunk for extraction
TEXT_CHUNK_OVERLAP = 15000  # Overlap between chunks
MIN_TEXT_LENGTH = 200  # Minimum text to avoid OCR
MIN_TEXT_FOR_PROCESSING = 30  # Minimum text to process document
SUMMARY_TEXT_SIZE = 3000  # Text size for summary generation
INITIAL_PAGES_TO_SCAN = 5  # Pages to scan initially
MAX_PAGES_TO_SCAN = 15  # Maximum pages to scan
OCR_PAGES_LIMIT = 3  # Maximum pages for OCR
OCR_DPI = 200  # DPI for OCR image conversion

# ============================================================================
# RETRY DECORATOR
# ============================================================================

def retry_on_failure(max_retries: int = MAX_RETRIES, wait_time: int = RETRY_WAIT, 
                     exceptions: tuple = (Exception,)):
    """Decorator to retry function on failure"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {e}")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"{func.__name__} failed after {max_retries} attempts: {e}")
                        raise
            return None
        return wrapper
    return decorator

# ============================================================================
# CREDENTIALS VALIDATION
# ============================================================================

def validate_credentials() -> bool:
    """Validate that all required credentials are loaded and exit if missing"""
    required_creds = {
        'APP_KEY_SOURCE_FULL': APP_KEY_SOURCE_FULL,
        'APP_SECRET_SOURCE_FULL': APP_SECRET_SOURCE_FULL,
        'REFRESH_TOKEN_SOURCE_FULL': REFRESH_TOKEN_SOURCE_FULL,
        'GEMINI_API_KEY_1..8 (organize)': _has_organize_keys(),
        'APP_KEY_SOURCE_RO': APP_KEY_SOURCE_RO,
        'APP_SECRET_SOURCE_RO': APP_SECRET_SOURCE_RO,
        'REFRESH_TOKEN_SOURCE_RO': REFRESH_TOKEN_SOURCE_RO,
        'GEMINI_API_KEY_9..20 (extract)': _has_extract_keys(),
        'APP_KEY_TARGET': APP_KEY_TARGET,
        'APP_SECRET_TARGET': APP_SECRET_TARGET,
        'REFRESH_TOKEN_TARGET': REFRESH_TOKEN_TARGET,
        'SENDER_EMAIL': SENDER_EMAIL,
        'SENDER_PASSWORD': SENDER_PASSWORD,
        'RECIPIENT_EMAIL': RECIPIENT_EMAIL[0] if RECIPIENT_EMAIL else None
    }
    
    missing = [key for key, value in required_creds.items() if value is False or value is None or (isinstance(value, str) and not value.strip())]
    
    if missing:
        logger.error("=" * 60)
        logger.error("❌ MISSING REQUIRED CREDENTIALS")
        logger.error("=" * 60)
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        logger.error("")
        logger.error("Please check your .env.local file and ensure all credentials are set.")
        logger.error("=" * 60)
        return False
    
    # Validate email format if provided
    if SENDER_EMAIL and '@' not in SENDER_EMAIL:
        logger.warning(f"⚠️  SENDER_EMAIL format may be invalid: {SENDER_EMAIL}")
    
    if RECIPIENT_EMAIL[0] and '@' not in RECIPIENT_EMAIL[0]:
        logger.warning(f"⚠️  RECIPIENT_EMAIL format may be invalid: {RECIPIENT_EMAIL[0]}")
    
    logger.info("✓ All credentials validated successfully")
    return True


# ============================================================================
# ENHANCED CONTRACT NORMALIZER
# ============================================================================

class ContractNormalizer:
    """Normalizer that outputs format matching website requirements"""

    def normalize(self, raw_data: dict) -> dict:
        """Returns contract_data structure"""
        try:
            contract_data = self._unwrap_data(raw_data)
            if "error" in contract_data:
                return {"error": contract_data["error"]}

            return {
                "contract_type": self._normalize_contract_type(contract_data),
                "datum_contract": self._normalize_datum(contract_data),
                "partijen": self._normalize_partijen_flat(contract_data),
                "pand": self._normalize_pand_flat(contract_data),
                "financieel": self._normalize_financieel_flat(contract_data),
                "periodes": self._normalize_periodes_flat(contract_data),
                "voorwaarden": self._normalize_voorwaarden_flat(contract_data),
                "juridisch": self._normalize_juridisch_flat(contract_data)
            }
        except Exception as e:
            return {"error": f"Normalization failed: {str(e)}"}

    def _unwrap_data(self, raw_data: dict) -> dict:
        return raw_data.get('contract_data') or raw_data.get('data') or raw_data.get('extracted_data') or raw_data

    def _safe_get(self, obj: Any, *keys: str, default: Any = None) -> Any:
        for key in keys:
            if isinstance(obj, dict):
                obj = obj.get(key)
            else:
                return default
            if obj is None:
                return default
        return obj if obj != "" else default

    def _extract_number(self, value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            clean = re.sub(r'[€$£\s]', '', value).replace(',', '.')
            match = re.search(r'[\d.]+', clean)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    pass
        return None

    def _normalize_boolean(self, value: Any) -> Optional[bool]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lower = value.lower().strip()
            if lower in ['true', 'ja', 'yes', 'toegestaan', '1']:
                return True
            if lower in ['false', 'nee', 'no', 'verboden', '0']:
                return False
        return None

    def _normalize_date(self, value: Any) -> Optional[str]:
        """Returns date in YYYY-MM-DD format"""
        if not value or value == "N/A":
            return None
        if isinstance(value, str):
            for fmt in ['%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d']:
                try:
                    dt = datetime.strptime(value.strip(), fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
        return str(value) if value else None

    def _normalize_contract_type(self, data: dict) -> str:
        ct = self._safe_get(data, 'contract_type') or self._safe_get(data, 'document_type') or self._safe_get(data, 'type')
        return ct.lower() if ct else 'huurovereenkomst'

    def _normalize_datum(self, data: dict) -> Optional[str]:
        date_value = self._safe_get(data, 'datum_contract') or self._safe_get(data, 'datum') or self._safe_get(data, 'contract_datum')
        return self._normalize_date(date_value)

    def _normalize_partijen_flat(self, data: dict) -> dict:
        """Returns flat partijen matching document 2: verhuurder + single huurder object"""
        partijen_data = self._safe_get(data, 'partijen', default={})

        # Verhuurder
        verhuurder = self._safe_get(partijen_data, 'verhuurder', default={})
        if isinstance(verhuurder, str):
            verhuurder = {"naam": verhuurder}

        # Collect all huurders
        huurders_list = []
        if 'huurders' in partijen_data and isinstance(partijen_data['huurders'], list):
            huurders_list = partijen_data['huurders']
        elif 'huurder' in partijen_data:
            h = partijen_data['huurder']
            if isinstance(h, dict):
                huurders_list = [h]
            elif isinstance(h, str):
                huurders_list = [{"naam": h}]

        # Combine names and use first huurder's contact details
        huurder_namen = []
        first_huurder = {}
        for h in huurders_list:
            if isinstance(h, dict):
                naam = self._safe_get(h, 'naam')
                if naam:
                    huurder_namen.append(naam)
                if not first_huurder:
                    first_huurder = h

        combined_naam = " & ".join(huurder_namen) if huurder_namen else ""

        return {
            "verhuurder": {
                "naam": self._safe_get(verhuurder, 'naam') or "",
                "adres": self._safe_get(verhuurder, 'adres') or self._safe_get(verhuurder, 'zetel') or "",
                "telefoon": self._safe_get(verhuurder, 'telefoon') or "",
                "email": self._safe_get(verhuurder, 'email') or self._safe_get(verhuurder, 'e-mail') or ""
            },
            "huurder": {
                "naam": combined_naam or "",
                "adres": self._safe_get(first_huurder, 'adres') or self._safe_get(first_huurder, 'woonplaats') or "",
                "telefoon": self._safe_get(first_huurder, 'telefoon') or self._safe_get(first_huurder, 'gsm') or "",
                "email": self._safe_get(first_huurder, 'email') or self._safe_get(first_huurder, 'e-mail') or ""
            }
        }

    def _normalize_pand_flat(self, data: dict) -> dict:
        """Returns flat pand"""
        pand = self._safe_get(data, 'pand') or self._safe_get(data, 'onderwerp') or {}

        # Extract address as simple string
        adres_raw = self._safe_get(pand, 'adres', default={})
        if isinstance(adres_raw, dict):
            volledig = self._safe_get(adres_raw, 'volledig_adres') or self._safe_get(adres_raw, 'volledig')
            if not volledig:
                parts = []
                straat = self._safe_get(adres_raw, 'straat')
                nummer = self._safe_get(adres_raw, 'nummer')
                if straat or nummer:
                    parts.append(f"{straat or ''} {nummer or ''}".strip())
                postcode = self._safe_get(adres_raw, 'postcode')
                stad = self._safe_get(adres_raw, 'stad')
                if postcode or stad:
                    parts.append(f"{postcode or ''} {stad or ''}".strip())
                volledig = ", ".join(parts) if parts else ""
            adres_str = volledig
        else:
            adres_str = str(adres_raw) if adres_raw else ""

        # EPC (uitgebreid voor zoeken/overzicht)
        epc_data = self._safe_get(pand, 'epc') or {}
        if isinstance(epc_data, str):
            epc_data = {"label": epc_data}
        epc_geldig = self._normalize_date(self._safe_get(epc_data, 'geldig_tot'))

        # Kadaster
        kadaster = self._safe_get(pand, 'kadaster') or {}
        ki = self._extract_number(self._safe_get(kadaster, 'kadastraal_inkomen') or self._safe_get(kadaster, 'ki'))

        return {
            "adres": adres_str,
            "type": self._safe_get(pand, 'type') or self._safe_get(pand, 'type_woning') or "appartement",
            "oppervlakte": self._extract_number(self._safe_get(pand, 'oppervlakte') or self._safe_get(pand, 'totale_bewoonbare_oppervlakte')),
            "aantal_kamers": self._extract_number(self._safe_get(pand, 'aantal_kamers') or self._safe_get(pand, 'kamers')),
            "verdieping": self._extract_number(self._safe_get(pand, 'verdieping')),
            "epc": {
                "energielabel": self._safe_get(epc_data, 'energielabel') or self._safe_get(epc_data, 'label') or "",
                "certificaatnummer": self._safe_get(epc_data, 'certificaatnummer') or self._safe_get(epc_data, 'nummer') or "",
                "geldig_tot": epc_geldig,
                "bewoonbare_oppervlakte_epc": self._extract_number(self._safe_get(epc_data, 'bewoonbare_oppervlakte_epc')),
                "primair_energieverbruik": self._safe_get(epc_data, 'primair_energieverbruik'),
                "referentiejaar": self._safe_get(epc_data, 'referentiejaar'),
            },
            "kadaster": {
                "afdeling": self._safe_get(kadaster, 'afdeling') or "",
                "sectie": self._safe_get(kadaster, 'sectie') or "",
                "nummer": self._safe_get(kadaster, 'nummer') or self._safe_get(kadaster, 'perceelnummer') or "",
                "kadastraal_inkomen": ki,
                "gemeente_kadaster": self._safe_get(kadaster, 'gemeente_kadaster') or "",
                "grondnummer": self._safe_get(kadaster, 'grondnummer') or "",
            },
            "asbest": self._normalize_asbest_flat(self._safe_get(pand, 'asbest')),
        }

    def _normalize_asbest_flat(self, asbest_data: Any) -> dict:
        """Returns flat asbest (asbestattest) voor zoeken/overzicht."""
        if not asbest_data or not isinstance(asbest_data, dict):
            return {
                "status": "",
                "datum_attest": None,
                "referentienummer": "",
                "opmerking": "",
                "geldig_tot": None,
            }
        return {
            "status": self._safe_get(asbest_data, 'status') or "",
            "datum_attest": self._normalize_date(self._safe_get(asbest_data, 'datum_attest')),
            "referentienummer": self._safe_get(asbest_data, 'referentienummer') or "",
            "opmerking": self._safe_get(asbest_data, 'opmerking') or "",
            "geldig_tot": self._normalize_date(self._safe_get(asbest_data, 'geldig_tot')),
        }

    def _normalize_financieel_flat(self, data: dict) -> dict:
        """Returns flat financieel"""
        financieel = self._safe_get(data, 'financieel', default={})

        # Huurprijs
        huurprijs_raw = self._safe_get(financieel, 'huurprijs') or self._safe_get(financieel, 'maandelijkse_huurprijs')
        if isinstance(huurprijs_raw, dict):
            huurprijs_raw = self._safe_get(huurprijs_raw, 'bedrag')
        huurprijs = self._extract_number(huurprijs_raw)

        # Waarborg
        waarborg_raw = self._safe_get(financieel, 'waarborg') or self._safe_get(financieel, 'huurwaarborg') or {}
        if isinstance(waarborg_raw, (int, float)):
            waarborg_raw = {"bedrag": waarborg_raw}

        waarborg_bedrag = self._extract_number(self._safe_get(waarborg_raw, 'bedrag'))

        # Build waar_gedeponeerd string
        waar_parts = []
        bank = self._safe_get(waarborg_raw, 'bank_naam') or self._safe_get(waarborg_raw, 'bank')
        iban = self._safe_get(waarborg_raw, 'iban')
        waar_gedeponeerd_raw = self._safe_get(waarborg_raw, 'waar_gedeponeerd')

        if waar_gedeponeerd_raw:
            waar_gedeponeerd = waar_gedeponeerd_raw
        elif bank or iban:
            if bank:
                waar_parts.append(bank)
            if iban:
                waar_parts.append(f"(rekening {iban})")
            waar_gedeponeerd = " ".join(waar_parts)
        else:
            waar_gedeponeerd = ""

        # Kosten
        kosten_raw = self._safe_get(financieel, 'kosten')
        if kosten_raw:
            kosten = str(kosten_raw)
        else:
            gem_kosten = self._safe_get(financieel, 'gemeenschappelijke_kosten', default={})
            if isinstance(gem_kosten, dict):
                inbegrepen_items = self._safe_get(gem_kosten, 'inbegrepen', default=[])
                if inbegrepen_items:
                    items_text = []
                    for item in inbegrepen_items:
                        if isinstance(item, dict):
                            post = self._safe_get(item, 'post')
                            if post:
                                items_text.append(post)
                    if items_text:
                        kosten = f"Gemeenschappelijke kosten ({', '.join(items_text)}) zijn inbegrepen in de huurprijs."
                    else:
                        kosten = "Gemeenschappelijke kosten inbegrepen."
                else:
                    kosten = ""
            else:
                kosten = ""

        # Indexatie
        indexatie = self._normalize_boolean(self._safe_get(financieel, 'indexatie') or self._safe_get(financieel, 'indexering'))

        return {
            "huurprijs": huurprijs,
            "waarborg": {
                "bedrag": waarborg_bedrag,
                "waar_gedeponeerd": waar_gedeponeerd
            },
            "kosten": kosten,
            "indexatie": indexatie
        }

    def _normalize_periodes_flat(self, data: dict) -> dict:
        """Returns flat periodes"""
        periodes = self._safe_get(data, 'periodes', default={})

        ingangsdatum = self._normalize_date(
            self._safe_get(periodes, 'ingangsdatum') or self._safe_get(periodes, 'aanvang') or self._safe_get(periodes, 'start')
        )
        einddatum = self._normalize_date(
            self._safe_get(periodes, 'einddatum') or self._safe_get(periodes, 'einde')
        )

        duur = self._safe_get(periodes, 'duur') or self._safe_get(periodes, 'contract_type_duur') or self._safe_get(periodes, 'looptijd') or ""

        opzegtermijn_raw = self._safe_get(periodes, 'opzegtermijn')
        opzegtermijn_huurder = self._safe_get(periodes, 'opzegtermijn_huurder')
        opzegtermijn_verhuurder = self._safe_get(periodes, 'opzegtermijn_verhuurder')

        if opzegtermijn_raw:
            opzegtermijn = str(opzegtermijn_raw)
        else:
            parts = []
            if opzegtermijn_huurder:
                parts.append(f"{opzegtermijn_huurder} (huurder)")
            if opzegtermijn_verhuurder:
                parts.append(f"{opzegtermijn_verhuurder} (verhuurder)")
            opzegtermijn = "; ".join(parts) if parts else ""

        return {
            "ingangsdatum": ingangsdatum,
            "einddatum": einddatum,
            "duur": duur,
            "opzegtermijn": opzegtermijn
        }

    def _normalize_voorwaarden_flat(self, data: dict) -> dict:
        """Returns flat voorwaarden"""
        voorwaarden = self._safe_get(data, 'voorwaarden', default={})

        huisdieren_raw = self._safe_get(voorwaarden, 'huisdieren')
        if isinstance(huisdieren_raw, dict):
            huisdieren = self._normalize_boolean(self._safe_get(huisdieren_raw, 'toegestaan'))
        else:
            huisdieren = self._normalize_boolean(huisdieren_raw)

        onderverhuur = self._normalize_boolean(self._safe_get(voorwaarden, 'onderverhuur'))
        werken = self._safe_get(voorwaarden, 'werken') or ""

        return {
            "huisdieren": huisdieren,
            "onderverhuur": onderverhuur,
            "werken": werken
        }

    def _normalize_juridisch_flat(self, data: dict) -> dict:
        """Returns flat juridisch"""
        juridisch = self._safe_get(data, 'juridisch', default={})

        return {
            "toepasselijk_recht": self._safe_get(juridisch, 'toepasselijk_recht') or "",
            "bevoegde_rechtbank": self._safe_get(juridisch, 'bevoegde_rechtbank') or ""
        }


normalizer = ContractNormalizer()


# ============================================================================
# FOLDER MANAGER
# ============================================================================

class FolderManager:
    """Manages dynamic folder structure"""

    def __init__(self, dbx):
        self.dbx = dbx
        self.folders = self.load_cache()

    def load_cache(self) -> Dict[str, dict]:
        """Load existing folder structure"""
        try:
            with open(FOLDER_CACHE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_cache(self):
        """Save folder structure"""
        try:
            with open(FOLDER_CACHE, 'w', encoding='utf-8') as f:
                json.dump(self.folders, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️  Cache save failed: {e}")

    def scan_organized_folders(self) -> List[Dict[str, str]]:
        """Scan alleen niveau-1 mappen (één map per adres) voor AI-context."""
        existing = []

        try:
            # Check if main folder exists
            try:
                self.dbx.files_get_metadata(ORGANIZED_FOLDER_PREFIX)
            except dropbox.exceptions.ApiError:
                return []

            # Alleen directe kinderen = niveau-1 mappen (adres-mappen)
            result = self.dbx.files_list_folder(ORGANIZED_FOLDER_PREFIX, recursive=False)

            while True:
                for entry in result.entries:
                    if isinstance(entry, dropbox.files.FolderMetadata):
                        path = entry.path_display
                        name = entry.name

                        try:
                            folder_contents = self.dbx.files_list_folder(path)
                            file_count = sum(1 for e in folder_contents.entries
                                           if isinstance(e, dropbox.files.FileMetadata))
                        except Exception:
                            file_count = 0

                        existing.append({
                            'path': path,
                            'name': name,
                            'file_count': file_count
                        })

                        if path not in self.folders:
                            self.folders[path] = {
                                'name': name,
                                'created': datetime.now().isoformat(),
                                'description': 'Auto-detected',
                                'file_count': file_count
                            }

                if not result.has_more:
                    break
                result = self.dbx.files_list_folder_continue(result.cursor)

        except Exception as e:
            print(f"⚠️  Scan error: {e}")

        self.save_cache()
        return existing

    @staticmethod
    def sanitize_suggested_filename(suggested: str, fallback: str) -> str:
        """Make AI-suggested filename safe: basename only, safe chars, .pdf extension."""
        if not suggested or not isinstance(suggested, str):
            return fallback
        # Alleen bestandsnaam (geen pad)
        name = suggested.strip().replace("\\", "/").split("/")[-1]
        # Verwijder onveilige tekens, spaties → underscore
        name = re.sub(r'[^\w\s\-.]', '', name)
        name = re.sub(r'\s+', '_', name)
        name = re.sub(r'_+', '_', name).strip('_.')
        if not name:
            return fallback
        if not name.lower().endswith('.pdf'):
            name = name + '.pdf'
        return name[:200]  # redelijke max lengte

    @staticmethod
    def fallback_filename_from_folder(folder_path: str, original_name: str) -> str:
        """Als AI geen geldige suggested_filename gaf: Adres_document.pdf (niveau 1 = adres-map)."""
        # Laatste map uit path = adres (bv. Kerkstraat_10, Onbekend_adres)
        parts = [p for p in folder_path.strip().replace("\\", "/").split("/") if p]
        adres_part = parts[-1] if parts else "Onbekend_adres"
        adres_part = re.sub(r'[^\w\-]', '_', adres_part).strip('_') or "Onbekend_adres"
        return f"{adres_part}_document.pdf"

    def sanitize_folder_path(self, path: str) -> str:
        """Make folder path safe"""
        path = path.strip()

        if path.startswith(ORGANIZED_FOLDER_PREFIX):
            path = path[len(ORGANIZED_FOLDER_PREFIX):]

        if not path.startswith('/'):
            path = '/' + path

        parts = path.split('/')
        cleaned_parts = []

        for part in parts:
            if not part:
                continue
            part = re.sub(r'[^\w\s-]', '', part)
            part = re.sub(r'\s+', '_', part)
            part = re.sub(r'_+', '_', part)
            part = part.strip('_')

            if part:
                cleaned_parts.append(part)

        if cleaned_parts:
            return ORGANIZED_FOLDER_PREFIX + '/' + '/'.join(cleaned_parts)
        else:
            return ORGANIZED_FOLDER_PREFIX + '/Overig'

    def create_folder(self, folder_path: str, description: str = "") -> bool:
        """Create new folder (with parent folders)"""
        try:
            folder_path = self.sanitize_folder_path(folder_path)

            parts = folder_path.split('/')[1:]
            current_path = ''

            for part in parts:
                current_path += '/' + part

                try:
                    self.dbx.files_get_metadata(current_path)
                except dropbox.exceptions.ApiError as e:
                    if e.error.is_path() and e.error.get_path().is_not_found():
                        try:
                            self.dbx.files_create_folder_v2(current_path)
                            print(f"📁 Folder created: {current_path}")
                        except dropbox.exceptions.ApiError as create_error:
                            if not (create_error.error.is_path() and
                                  create_error.error.get_path().is_conflict()):
                                raise

            if folder_path not in self.folders:
                self.folders[folder_path] = {
                    'name': folder_path.split('/')[-1],
                    'created': datetime.now().isoformat(),
                    'description': description,
                    'file_count': 0
                }
                self.save_cache()
                print(f"   Description: {description}")

            return True

        except Exception as e:
            print(f"❌ Folder creation error: {e}")
            return False

    def get_folder_summary(self) -> str:
        """Create summary for AI"""
        if not self.folders:
            return "No existing organized folders."

        summary = []
        for path, info in sorted(self.folders.items()):
            relative_path = path.replace(ORGANIZED_FOLDER_PREFIX, '')
            desc = info.get('description', 'no description')
            count = info.get('file_count', 0)
            summary.append(f"- {relative_path}: {desc} ({count} file(s))")

        return "\n".join(summary[:15])


# ============================================================================
# UTILITIES
# ============================================================================

def format_file_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def _normalize_dropbox_path(path):
    """Eén vorm voor paden zodat history-check geen dubbele verwerking toelaat."""
    if not path:
        return path
    # Eén pad per regel: geen newlines (voorkom path1+path2 in één entry)
    p = path.strip().split("\n")[0].strip()
    return f"/{p.lstrip('/')}" if p else p


def _is_valid_history_line(line):
    """Filter corrupte regels (twee paden aan elkaar, bv. .../file.pdf/Georganiseerd/...)."""
    s = line.strip()
    if not s:
        return False
    # Eén geldig pad eindigt op .pdf; bevat geen ".pdf/" (twee paden geplakt)
    if ".pdf/" in s:
        return False
    return True


def load_history(filename):
    """Load processed files from history (paden genormaliseerd). Corrupte regels worden overgeslagen."""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return set(
                _normalize_dropbox_path(line)
                for line in f
                if line.strip() and _is_valid_history_line(line)
            )
    except FileNotFoundError:
        return set()


def add_to_history(filename, path):
    """Add file to history (pad genormaliseerd, één pad per regel)."""
    norm = _normalize_dropbox_path(path)
    if not norm or not _is_valid_history_line(norm):
        return
    try:
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{norm}\n")
    except Exception as e:
        print(f"⚠️  History update failed: {e}")


def remove_from_history(filename, path):
    """Remove file from history (to requeue for retry). Pad genormaliseerd."""
    norm = _normalize_dropbox_path(path)
    if not norm:
        return
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # Verwijder deze entry en schrijf alleen geldige regels terug (opruimen corrupte regels)
        filtered = [
            line for line in lines
            if line.strip() and _is_valid_history_line(line) and _normalize_dropbox_path(line) != norm
        ]
        with open(filename, 'w', encoding='utf-8') as f:
            for line in filtered:
                f.write(line if line.endswith("\n") else line + "\n")
        print(f"   🔄 Removed from history (requeued): {path}")
    except FileNotFoundError:
        # File doesn't exist, nothing to remove
        pass
    except Exception as e:
        print(f"⚠️  Failed to remove from history: {e}")


def is_quota_error(error_msg):
    error_str = str(error_msg).lower()
    return "429" in error_str or "quota" in error_str or "resource_exhausted" in error_str


def is_api_key_error(error_msg):
    """Check if error is related to invalid API key"""
    error_str = str(error_msg).lower()
    return (
        "api key" in error_str or
        "invalid api key" in error_str or
        "authentication" in error_str or
        "401" in error_str or
        "403" in error_str or
        "permission denied" in error_str or
        "api_key_not_valid" in error_str
    )


def send_email(subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = ', '.join(RECIPIENT_EMAIL)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        print(f"✉️  Email sent")
        return True
    except Exception as e:
        print(f"❌ Email error: {e}")
        return False


# ============================================================================
# SUPABASE REST API (geen pip-pakket i.v.m. lokale map supabase/)
# ============================================================================

def _supabase_headers(supabase_config):
    url, key = supabase_config.get('url'), supabase_config.get('key')
    if not url or not key:
        raise RuntimeError("Supabase config ontbreekt (url/key).")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def supabase_upsert_contract(supabase_config, json_name, json_data):
    """Upsert één contract in Supabase via REST API."""
    import requests
    url = supabase_config["url"]
    r = requests.post(
        f"{url}/rest/v1/contracts",
        headers=_supabase_headers(supabase_config),
        json={"name": json_name, "data": json_data},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase upsert failed: {r.status_code} {r.text[:200]}")
    return r


def supabase_update_contract_data(supabase_config, json_name, json_data):
    """Update alleen het veld data van een contract."""
    import requests
    from urllib.parse import quote
    url = supabase_config["url"]
    # PostgREST: string met . moet tussen dubbele aanhalingstekens, dan URL-encoden
    value = quote(f'"{json_name}"', safe="")
    r = requests.patch(
        f"{url}/rest/v1/contracts?name=eq.{value}",
        headers={k: v for k, v in _supabase_headers(supabase_config).items() if k != "Prefer"},
        json={"data": json_data},
        timeout=30,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Supabase update failed: {r.status_code} {r.text[:200]}")
    return r


def supabase_upsert_document_text(supabase_config, dropbox_path: str, name: str, full_text: str):
    """Stap 7: geëxtraheerde tekst opslaan voor full-text zoeken (document_texts). Upsert: POST, bij 409 PATCH."""
    import requests
    from urllib.parse import quote
    url = supabase_config["url"]
    text_value = (full_text[:500000] if full_text else "") or ""
    body = {"dropbox_path": dropbox_path, "name": name, "full_text": text_value}
    headers = _supabase_headers(supabase_config)
    r = requests.post(
        f"{url}/rest/v1/document_texts",
        headers=headers,
        json=body,
        timeout=30,
    )
    if r.status_code in (200, 201):
        return r
    if r.status_code == 409 or "duplicate" in (r.text or "").lower():
        # Row bestaat al: update full_text via PATCH (PostgREST: string tussen aanhalingstekens)
        path_enc = quote(f'"{dropbox_path}"', safe="")
        name_enc = quote(f'"{name}"', safe="")
        r2 = requests.patch(
            f"{url}/rest/v1/document_texts?dropbox_path=eq.{path_enc}&name=eq.{name_enc}",
            headers={k: v for k, v in headers.items() if k != "Prefer"},
            json={"full_text": text_value},
            timeout=30,
        )
        if r2.status_code in (200, 204):
            return r2
        err = r2.text[:500] if r2.text else "(geen body)"
        logger.warning(f"document_texts PATCH failed: {r2.status_code} body={err}")
        raise RuntimeError(f"Supabase document_texts update failed: {r2.status_code} {err}")
    err_detail = r.text[:500] if r.text else "(geen body)"
    logger.warning(f"document_texts POST failed: {r.status_code} body={err_detail}")
    raise RuntimeError(f"Supabase document_texts upsert failed: {r.status_code} {err_detail}")


# ============================================================================
# CSV LOGGING
# ============================================================================

def ensure_csv_exists(dbx_target):
    try:
        dbx_target.files_get_metadata(CSV_LOG_PATH)
        return True
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            header = 'timestamp,filename,document_type,confidence_score,needs_review,text_length,fields_complete,issues,warnings,json_path,processing_status\n'
            try:
                dbx_target.files_upload(header.encode('utf-8'), CSV_LOG_PATH, mode=dropbox.files.WriteMode.overwrite)
                print("📊 CSV log created")
                return True
            except Exception as ex:
                print(f"❌ CSV creation error: {ex}")
                return False
        else:
            print(f"❌ CSV check error: {e}")
            return False


def log_to_csv(dbx_target, filename, result, json_path, status="success"):
    try:
        if not ensure_csv_exists(dbx_target):
            print("⚠️  Cannot find/create CSV - skipping logging")
            return False

        try:
            _, response = dbx_target.files_download(CSV_LOG_PATH)
            current_csv = response.content.decode('utf-8')
        except Exception as e:
            print(f"❌ CSV download error: {e}")
            return False

        conf = result.get('confidence', {})
        new_row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            filename,
            result.get('title', 'Unknown'),
            str(conf.get('score', 0)),
            'Yes' if conf.get('needs_review', True) else 'No',
            str(conf.get('metrics', {}).get('text_length', 0)),
            f"{conf.get('metrics', {}).get('completeness', 0):.0%}",
            '; '.join(conf.get('issues', [])) or 'None',
            '; '.join(conf.get('warnings', [])) or 'None',
            json_path or '',
            status
        ]
        new_row = [f'"{str(field).replace('"', '""')}"' for field in new_row]
        new_line = ','.join(new_row) + '\n'
        updated_csv = current_csv + new_line
        dbx_target.files_upload(updated_csv.encode('utf-8'), CSV_LOG_PATH, mode=dropbox.files.WriteMode.overwrite)
        return True
    except Exception as e:
        print(f"❌ CSV logging error: {e}")
        return False


# ============================================================================
# INITIALIZATION
# ============================================================================

def init_clients():
    """Initialize all Dropbox and Gemini clients"""
    # Validate credentials first
    if not validate_credentials():
        return None
    
    try:
        # ORGANIZE: KEY_1..8, rotator in organize_batch. Init = eerste key voor model-listing (geen rotator-verbruik).
        first_organize_key = _first_organize_key()
        client_organize = genai.Client(api_key=first_organize_key) if first_organize_key else None
        # EXTRACT: KEY_9..20, rotator in process_rental_contract (geen vaste client meer).
        client_analyze = None  # wordt per contract opgebouwd via get_next_extract_key()

        # Try to dynamically select best available model, fallback to default if API key invalid
        model_id = None
        try:
            all_models = client_organize.models.list() if client_organize else []
            generative_models = []
            for m in all_models:
                model_name = m.name.replace('models/', '')
                if 'gemini' in model_name.lower() and 'embedding' not in model_name.lower():
                    generative_models.append(model_name)

            logger.info(f"Available models: {generative_models}")

            # Preferred models (stable, geen experimental!)
            preferred_models = ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-1.5-flash-latest', 'gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']
            for pref in preferred_models:
                if pref in generative_models:
                    model_id = pref
                    break

            if not model_id and generative_models:
                model_id = generative_models[0]
        except Exception as model_error:
            logger.warning(f"Could not list models (API key may be invalid): {model_error}")
            logger.info("Using default model: gemini-1.5-flash-latest")

        if not model_id:
            model_id = 'gemini-2.5-flash'  # Default to latest stable model

        # Use same model for both
        model_organize = model_id
        model_analyze = model_id

        # Dropbox clients
        dbx_organize = dropbox.Dropbox(
            app_key=APP_KEY_SOURCE_FULL,
            app_secret=APP_SECRET_SOURCE_FULL,
            oauth2_refresh_token=REFRESH_TOKEN_SOURCE_FULL
        )

        dbx_analyze = dropbox.Dropbox(
            app_key=APP_KEY_SOURCE_RO,
            app_secret=APP_SECRET_SOURCE_RO,
            oauth2_refresh_token=REFRESH_TOKEN_SOURCE_RO
        )

        dbx_target = dropbox.Dropbox(
            app_key=APP_KEY_TARGET,
            app_secret=APP_SECRET_TARGET,
            oauth2_refresh_token=REFRESH_TOKEN_TARGET
        )

        # Verify connections
        account_org = dbx_organize.users_get_current_account()
        logger.info(f"✅ Dropbox Organize: {account_org.name.display_name}")

        account_ana = dbx_analyze.users_get_current_account()
        logger.info(f"✅ Dropbox Analyze: {account_ana.name.display_name}")

        account_tgt = dbx_target.users_get_current_account()
        logger.info(f"✅ Dropbox Target: {account_tgt.name.display_name}")

        logger.info(f"✅ Gemini Organize: {model_organize} (8 keys, max 18/24u)")
        logger.info(f"✅ Gemini Extract: {model_organize} (12 keys, max 18/24u)")

        # Ensure organized folder exists
        try:
            dbx_organize.files_get_metadata(ORGANIZED_FOLDER_PREFIX)
        except dropbox.exceptions.ApiError:
            dbx_organize.files_create_folder_v2(ORGANIZED_FOLDER_PREFIX)
            logger.info(f"📁 Created {ORGANIZED_FOLDER_PREFIX}")

        # Ensure CSV exists (Dropbox TARGET blijft voor CSV)
        ensure_csv_exists(dbx_target)

        # Supabase via REST API (geen pip-pakket: lokale map supabase/ zou die overschaduwen)
        _url = (os.getenv('SUPABASE_URL') or SUPABASE_URL or '').strip().rstrip('/')
        _key = (os.getenv('SUPABASE_SERVICE_KEY') or SUPABASE_SERVICE_KEY or '').strip()
        if not _url or not _key:
            raise RuntimeError("SUPABASE_URL of SUPABASE_SERVICE_KEY ontbreekt in .env.local. Zet ze (Supabase Dashboard → Settings → API) en draai opnieuw.")
        try:
            import requests
        except ImportError:
            raise RuntimeError("'requests' is nodig voor Supabase. Installeer met: pip install requests")
        # Snelle check: GET op REST endpoint
        r = requests.get(
            f"{_url}/rest/v1/contracts?limit=1",
            headers={"apikey": _key, "Authorization": f"Bearer {_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code not in (200, 206):
            raise RuntimeError(f"Supabase bereikbaar maar fout: {r.status_code}. Controleer SUPABASE_URL en SUPABASE_SERVICE_KEY.")
        logger.info("✅ Supabase: JSON contract storage actief (REST API)")

        return {
            'dbx_organize': dbx_organize,
            'dbx_analyze': dbx_analyze,
            'dbx_target': dbx_target,
            'supabase': {'url': _url, 'key': _key},
            'gemini_organize': client_organize,
            'gemini_analyze': client_analyze,
            'model_organize': model_organize,
            'model_analyze': model_analyze
        }

    except Exception as e:
        logger.error(f"❌ Initialization error: {e}", exc_info=True)
        return None


# ============================================================================
# STAP 8: MONITORING (pipeline stats)
# ============================================================================
_pipeline_stats = {
    "text_layer": 0,
    "ocr_google_vision": 0,
    "ocr_vision": 0,  # Gemini Vision fallback
    "contract_stages_rules": 0,
    "contract_stages_ai": 0,
}


def record_extraction_method(method: str):
    """Stap 8: registreer tekstbron (text_layer, ocr_tesseract, ocr_vision)."""
    if method in _pipeline_stats:
        _pipeline_stats[method] += 1


def record_contract_stages(rules_count: int, ai_count: int):
    """Stap 8: registreer aantal stages via regels vs AI."""
    _pipeline_stats["contract_stages_rules"] += rules_count
    _pipeline_stats["contract_stages_ai"] += ai_count


def print_pipeline_stats():
    """Stap 8: toon % documenten via regels / AI / OCR-fallback."""
    total_docs = _pipeline_stats["text_layer"] + _pipeline_stats["ocr_google_vision"] + _pipeline_stats["ocr_vision"]
    if total_docs == 0:
        return
    print("\n📊 Pipeline stats (deze run)")
    print(f"   Tekst: {_pipeline_stats['text_layer']} tekstlaag, {_pipeline_stats['ocr_google_vision']} Google Vision, {_pipeline_stats['ocr_vision']} Gemini Vision (fallback)")
    total_stages = _pipeline_stats["contract_stages_rules"] + _pipeline_stats["contract_stages_ai"]
    if total_stages:
        pct_rules = 100 * _pipeline_stats["contract_stages_rules"] / total_stages
        print(f"   Contractvelden: {_pipeline_stats['contract_stages_rules']} via regels, {_pipeline_stats['contract_stages_ai']} via AI ({pct_rules:.0f}% regels)")


# ============================================================================
# PDF TEXT EXTRACTION WITH OCR (Stap 2: pdfplumber → Google Vision → Gemini Vision fallback)
# ============================================================================

def extract_text_google_vision(image_data: List[bytes], api_key: str) -> str:
    """OCR met Google Cloud Vision API (DOCUMENT_TEXT_DETECTION). Key in .env.local: GOOGLE_VISION_API_KEY."""
    if not api_key or not api_key.strip() or not image_data:
        return ""
    import urllib.request
    requests_list = []
    for img_bytes in image_data:
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        requests_list.append({
            "image": {"content": b64},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
        })
    try:
        url = "https://vision.googleapis.com/v1/images:annotate?key=" + api_key.strip()
        body = json.dumps({"requests": requests_list}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        out = []
        for r in data.get("responses", []):
            if "fullTextAnnotation" in r and r["fullTextAnnotation"].get("text"):
                out.append(r["fullTextAnnotation"]["text"])
        return "\n\n".join(out) if out else ""
    except Exception as e:
        logger.warning(f"   Google Vision error: {e}")
        return ""


def extract_text_with_ocr(pdf_bytes: bytes, gemini_client, model: str, 
                          initial_pages: int = INITIAL_PAGES_TO_SCAN) -> Tuple[str, dict]:
    """Extract text from PDF with OCR fallback for scanned documents"""
    try:
        full_text = ""
        total_pages = 0
        pages_scanned = 0
        extraction_method = "text"

        # Phase 1: Try normal text extraction
        with io.BytesIO(pdf_bytes) as pdf_file:
            with pdfplumber.open(pdf_file) as pdf:
                total_pages = len(pdf.pages)
                pages_to_scan = min(initial_pages, total_pages)

                for i in range(pages_to_scan):
                    try:
                        page_text = pdf.pages[i].extract_text() or ""
                        full_text += page_text + "\n"
                        pages_scanned += 1
                    except Exception:
                        continue

        cleaned_text = ' '.join(full_text.split())

        # Check if we have enough text - if not, use OCR
        if len(cleaned_text) < MIN_TEXT_LENGTH and total_pages > 0:
            logger.warning(f"   ⚠️  Little text found ({len(cleaned_text)} chars)")
            logger.info(f"   📸 Scanned document detected - using OCR...")

            extraction_method = "ocr"

            try:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                image_data = []

                pages_to_ocr = min(OCR_PAGES_LIMIT, total_pages)
                for page_num in range(pages_to_ocr):
                    page = doc[page_num]
                    # Convert page to image at specified DPI
                    pix = page.get_pixmap(dpi=OCR_DPI)
                    # Get PNG bytes
                    img_bytes = pix.tobytes("png")
                    image_data.append(img_bytes)

                doc.close()

                logger.info(f"   🔍 Running OCR on {len(image_data)} page(s)...")

                # Stap 2: eerst Google Vision (aparte key); zo niet gezet of fout → Gemini Vision fallback
                ocr_text = ""
                google_vision_key = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
                if google_vision_key:
                    ocr_text = extract_text_google_vision(image_data, google_vision_key)
                    if ocr_text and len(ocr_text) > 100:
                        cleaned_text = ocr_text
                        extraction_method = "ocr_google_vision"
                        logger.info(f"   ✓ Google Vision OCR: {len(cleaned_text)} chars")
                    else:
                        ocr_text = ""
                if not ocr_text and gemini_client and model:
                    ocr_text = extract_text_vision(image_data, gemini_client, model)
                    if ocr_text and len(ocr_text) > 100:
                        cleaned_text = ocr_text
                        extraction_method = "ocr_vision"
                        logger.info(f"   ✓ Gemini Vision OCR (fallback): {len(cleaned_text)} chars")
                    else:
                        logger.warning(f"   ⚠️  OCR yielded little text")

            except Exception as ocr_error:
                logger.error(f"   ❌ OCR error: {ocr_error}", exc_info=True)

        # Smart extra scanning
        need_more = False
        if len(cleaned_text) < 500:
            need_more = True
        elif extraction_method == "text":
            generic_words = ['voorblad', 'inhoudsopgave', 'inhoud', 'index']
            if any(word in cleaned_text.lower() for word in generic_words) and len(cleaned_text) < 1000:
                need_more = True

        if need_more and total_pages > initial_pages and extraction_method == "text":
            logger.info(f"   📖 Scanning extra pages...")

            with io.BytesIO(pdf_bytes) as pdf_file:
                with pdfplumber.open(pdf_file) as pdf:
                    max_scan = min(MAX_PAGES_TO_SCAN, total_pages)
                    for i in range(initial_pages, max_scan):
                        try:
                            page_text = pdf.pages[i].extract_text() or ""
                            full_text += page_text + "\n"
                            pages_scanned += 1

                            if i % 3 == 0:
                                temp = ' '.join(full_text.split())
                                if len(temp) > 1000:
                                    break
                        except Exception:
                            continue

            cleaned_text = ' '.join(full_text.split())

        metadata = {
            'total_pages': total_pages,
            'pages_scanned': pages_scanned,
            'text_length': len(cleaned_text),
            'extraction_method': extraction_method,
            'ocr_engine': extraction_method if extraction_method in ("ocr_google_vision", "ocr_vision") else "text_layer",
        }
        return cleaned_text, metadata

    except Exception as e:
        logger.error(f"⚠️  Extraction error: {e}", exc_info=True)
        return "", {'error': str(e)}
def extract_text_vision(images: List[bytes], gemini_client, model) -> str:
    """Extract text from images with Gemini Vision API"""
    try:
        prompt = "Extract ALL text from these images. Include handwritten text if present. Return ONLY the extracted text, no explanations."

        # ✅ FIXED: Just use string directly, not wrapped in Part
        parts = [prompt]

        for img_bytes in images:
            parts.append(types.Part.from_bytes(
                data=img_bytes,
                mime_type="image/png"
            ))

        for attempt in range(MAX_RETRIES):
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=parts
                )
                return response.text

            except Exception as e:
                if "429" in str(e) and attempt < MAX_RETRIES - 1:
                    print(f"   Rate limit - waiting {RATE_LIMIT_WAIT}s...")
                    time.sleep(RATE_LIMIT_WAIT)
                else:
                    raise

        return ""

    except Exception as e:
        print(f"   Vision OCR error: {e}")
        return ""

# ============================================================================
# PHASE 1: SMART CLASSIFICATION
# ============================================================================

def _force_non_contract_out_of_contract_folders(result: dict, filename: str) -> dict:
    """Zorg dat verhaal/essay/onderwijs/factuur NOOIT in /Onbekend_adres of /Contracten komen (code-fix na AI)."""
    folder_path = (result.get('folder_path') or '').strip()
    reasoning = (result.get('reasoning') or '').lower()
    suggested = (result.get('suggested_filename') or '').lower()
    name_lower = filename.lower()

    # Alleen ingrijpen als AI een contract-map heeft gegeven
    if 'onbekend_adres' not in folder_path.lower() and '/contracten/' not in folder_path.lower():
        return result

    # Signalen dat het GEEN contract is (certificaat, verklaring, verhaal, onderwijs, factuur, …)
    non_contract_signals = [
        'verhaal', 'essay', 'narratief', 'persoonlijke tekst', 'geen contract',
        'onderwijs', 'college', 'cursus', 'dictaat', 'studie',
        'certificaat', 'verklaring', 'deelname', 'studentenverklaring', 'bewijs',
        'factuur', 'offerte', 'betalingsdocument',
        'teksten', 'overig'
    ]
    is_likely_non_contract = any(s in reasoning or s in suggested or s in name_lower for s in non_contract_signals)

    if not is_likely_non_contract:
        return result

    # Override naar inhoud-map (geen contract-map); hernoem weg van Onbekend_adres_*
    if any(s in reasoning or s in suggested or s in name_lower for s in ['verhaal', 'essay', 'narratief', 'tekst']):
        result['folder_path'] = '/Verhaal'
        result['suggested_filename'] = 'verhaal_document.pdf'
        result['reasoning'] = (result.get('reasoning') or '') + ' [Correctie: verhaal/essay → /Verhaal.]'
    elif any(s in reasoning or s in suggested or s in name_lower for s in ['certificaat', 'verklaring', 'deelname', 'studentenverklaring', 'bewijs']):
        result['folder_path'] = '/Onderwijs'
        result['suggested_filename'] = 'certificaat_verklaring.pdf'
        result['reasoning'] = (result.get('reasoning') or '') + ' [Correctie: certificaat/verklaring → /Onderwijs.]'
    elif any(s in reasoning or s in suggested or s in name_lower for s in ['onderwijs', 'college', 'cursus', 'dictaat']):
        result['folder_path'] = '/Onderwijs'
        result['suggested_filename'] = 'onderwijs_document.pdf'
        result['reasoning'] = (result.get('reasoning') or '') + ' [Correctie: onderwijs → /Onderwijs.]'
    elif any(s in reasoning or s in suggested or s in name_lower for s in ['factuur', 'offerte']):
        result['folder_path'] = '/Facturen'
        result['suggested_filename'] = 'factuur_document.pdf'
    else:
        result['folder_path'] = '/Verhaal'
        result['suggested_filename'] = 'document.pdf'
        result['reasoning'] = (result.get('reasoning') or '') + ' [Correctie: geen contract → /Verhaal.]'

    return result


def _parse_json_from_response(raw: str) -> dict:
    """Robuust JSON uit modelresponse halen: strip markdown, vind eerste { ... } (inclusief genest)."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        for p in parts[1:]:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                raw = p
                break
        else:
            raw = raw.replace("```", "").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
    start = raw.find("{")
    if start < 0:
        raise json.JSONDecodeError("No JSON object found", raw, 0)
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start : i + 1])
    raise json.JSONDecodeError("Unbalanced braces", raw, start)


def smart_classify(text: str, filename: str, current_location: str,
                   existing_folders: str, gemini_client, model, pdf_metadata: dict) -> Optional[Dict]:
    """Eén call: document volledig inlezen, korte samenvatting maken (Plan stap 6) + classificatie/ordening (Plan stap 1). Output gebruikt voor Dropbox-ordenen."""

    # Meer tekst meegeven zodat de model het document kan samenvatten (stap 1+6 in één call)
    text_for_call = text[:ORGANIZE_AND_SUMMARY_TEXT_SIZE] if len(text) > ORGANIZE_AND_SUMMARY_TEXT_SIZE else text
    if len(text) > ORGANIZE_AND_SUMMARY_TEXT_SIZE:
        text_for_call += "\n\n[... document afgekapt voor lengte ...]"

    extraction_method = pdf_metadata.get('extraction_method', 'text')
    method_note = " (OCR used)" if extraction_method == "ocr" else ""

    prompt = f"""SYSTEM: Je bent een EXPERT documentclassificeerder. Je doet TWEE dingen in één antwoord:
1) LEES het document en maak een KORTE samenvatting (max 3-4 zinnen; voor metadata).
2) BEPAAL het type document en de mapstructuur (waar het in Dropbox moet).

CRITICAL: /Onbekend_adres is ALLEEN voor CONTRACTEN (huur, EPC, eigendomstitel, …) waar het adres niet uit de tekst komt. Verhalen, essays, onderwijs, facturen → NOOIT /Onbekend_adres. Verhaal/essay/narratief → ALTIJD /Verhaal.

FILENAME: {filename}
LOCATION: {current_location}
EXTRACTION: {pdf_metadata.get('pages_scanned', '?')}/{pdf_metadata.get('total_pages', '?')} pages{method_note}

EXISTING FOLDERS (bestaande mappen; kijk of jouw folder_path hier al in voorkomt → action "existing", anders "new"):
{existing_folders if existing_folders else "Geen mappen nog - eerste document."}

DOCUMENT TEKST (volledig inlezen voor samenvatting; gebruik ook voor classificatie):
{text_for_call}

═══════════════════════════════════════════════════════════════════
REGELS
═══════════════════════════════════════════════════════════════════

1. BEPAAL HET TYPE DOCUMENT (uit inhoud, niet uit bestandsnaam):
   - Huurcontract / huurovereenkomst → CONTRACT, type Huurcontracten
   - EPC / energieprestatiecertificaat / energiedocument → CONTRACT, type EPC
   - Asbestattest / asbestinventaris / asbestvrijverklaring → CONTRACT, type Asbest
   - Eigendomstitel / akte → CONTRACT, type Eigendomstitel
   - Koopcontract / verkoopovereenkomst → CONTRACT, type Koopcontracten
   - Verhaal, essay, persoonlijke tekst, narratief, kort verhaal → NIET CONTRACT → folder_path = /Verhaal (NOOIT /Onbekend_adres)
   - Certificaat, verklaring, studentenverklaring, bewijs van deelname → NIET CONTRACT → folder_path = /Onderwijs (NOOIT /Onbekend_adres)
   - Onderwijs, college, cursus, studie, dictaat → NIET CONTRACT → map /Onderwijs (eventueel /Onderwijs/Subcategorie)
   - Factuur, offerte, betalingsdocument → NIET CONTRACT → map /Facturen
   - Overige zakelijke documenten → map die past bij inhoud (bv. /Correspondentie, /Rapporten)
   - Twijfel of onduidelijk → /Overig

2. CONTRACTEN (huur, EPC, asbest, eigendomstitel, koop, …):
   - folder_path = /Contracten/[TypeMap]/[Adres]
   - TypeMap = Huurcontracten, EPC, Asbest, Eigendomstitel, Koopcontracten.
   - Adres = straat + nummer uit de tekst (bv. Kerkstraat_10, Meir_78_bus_3). Alleen bij CONTRACTEN: als geen adres in tekst → Onbekend_adres.
   - Zelfde adres + zelfde type: als die map al in EXISTING FOLDERS staat → action "existing", anders "new".
   - suggested_filename = Adres_Type.pdf (bv. Kerkstraat_10_huurcontract.pdf, Meir_78_bus_3_EPC.pdf, Kerkstraat_10_asbest.pdf).
   Voorbeelden: /Contracten/Huurcontracten/Kerkstraat_10, /Contracten/EPC/Meir_78_bus_3, /Contracten/Asbest/Kerkstraat_10, /Contracten/Eigendomstitel/Onbekend_adres.

3. NIET-CONTRACTEN (verhaal, certificaat, verklaring, onderwijs, factuur, …):
   - Verhaal/essay/narratief → folder_path = /Verhaal. Certificaat/verklaring/studentenverklaring → folder_path = /Onderwijs. NOOIT /Onbekend_adres.
   - Onderwijs → /Onderwijs of /Onderwijs/Sub. Factuur → /Facturen. Overig → /Teksten, /Overig, etc.
   - Geen adres in het pad. suggested_filename = korte beschrijvende naam (letters, cijfers, underscores), eindig op .pdf.

4. ACTION:
   - "existing" = folder_path staat al in EXISTING FOLDERS (zelfde pad gebruiken).
   - "new" = folder_path bestaat nog niet, wordt aangemaakt.

VERPLICHT: Geef ALTIJD action, folder_path, confidence (0-100), reasoning, suggested_filename, EN summary.
- folder_path altijd met leading slash, delen gescheiden door /, geen spaties in mapnamen (gebruik underscore).
- suggested_filename: alleen letters, cijfers, underscores; eindig op .pdf.
- summary: korte samenvatting van het document (max 3-4 zinnen), gebaseerd op de volledige tekst hierboven.

ANTWOORD FORMAT (ALLEEN JSON, geen tekst ervoor/erna):

Voorbeeld CONTRACT (huurcontract Kerkstraat 10, map bestaat al):
{{
  "action": "existing",
  "folder_path": "/Contracten/Huurcontracten/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Huurcontract voor Kerkstraat 10; map /Contracten/Huurcontracten/Kerkstraat_10 bestaat al.",
  "suggested_filename": "Kerkstraat_10_huurcontract.pdf",
  "summary": "Huurcontract tussen verhuurder X en huurder Y voor Kerkstraat 10. Looptijd 3 jaar, huurprijs 850 euro. Waarborg twee maanden."
}}

Voorbeeld CONTRACT (EPC Meir 78 bus 3, nieuwe map):
{{
  "action": "new",
  "folder_path": "/Contracten/EPC/Meir_78_bus_3",
  "confidence": 98,
  "reasoning": "Energieprestatiecertificaat voor Meir 78 bus 3; map bestaat nog niet.",
  "suggested_filename": "Meir_78_bus_3_EPC.pdf",
  "summary": "EPC voor Meir 78 bus 3. Energielabel C. Bewoonbare oppervlakte 95 m². Geldig tot 2030."
}}

Voorbeeld CONTRACT (Asbestattest Kerkstraat 10):
{{
  "action": "new",
  "folder_path": "/Contracten/Asbest/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Asbestattest / asbestvrijverklaring voor Kerkstraat 10.",
  "suggested_filename": "Kerkstraat_10_asbest.pdf",
  "summary": "Asbestattest voor Kerkstraat 10. Pand asbestvrij verklaard. Datum attest vermeld."
}}

Voorbeeld NIET-CONTRACT (verhaal) — ALTIJD /Verhaal, NOOIT /Onbekend_adres:
{{
  "action": "new",
  "folder_path": "/Verhaal",
  "confidence": 90,
  "reasoning": "Verhaal/essay/narratief, geen contract; hoort in map Verhaal. Onbekend_adres is alleen voor contracten zonder adres.",
  "suggested_filename": "verhaal_document.pdf",
  "summary": "Persoonlijk verhaal over een reis. Geen contract of officieel document."
}}
FOUT: verhaal of certificaat in /Onbekend_adres plaatsen. Onbekend_adres = ALLEEN voor contracten (huur, EPC, …) waar het adres ontbreekt.

Voorbeeld NIET-CONTRACT (certificaat/verklaring) — ALTIJD /Onderwijs, NOOIT /Onbekend_adres:
{{
  "action": "existing",
  "folder_path": "/Onderwijs",
  "confidence": 95,
  "reasoning": "Certificaat van deelname / studentenverklaring; geen contract. Hoort in /Onderwijs.",
  "suggested_filename": "certificaat_verklaring.pdf",
  "summary": "Certificaat van deelname aan een cursus. Geen contract."
}}

Voorbeeld NIET-CONTRACT (onderwijs):
{{
  "action": "existing",
  "folder_path": "/Onderwijs/Bedrijfskunde",
  "confidence": 92,
  "reasoning": "Onderwijsmateriaal bedrijfskunde; map bestaat al.",
  "suggested_filename": "college_week3.pdf",
  "summary": "College-notities bedrijfskunde week 3. Geen contract."
}}

JSON:"""

    for attempt in range(MAX_RETRIES):
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )

            raw = response.text.strip()
            result = _parse_json_from_response(raw)

            # Validate result
            required_fields = ['action', 'folder_path', 'confidence', 'reasoning']
            if not all(field in result for field in required_fields):
                raise ValueError(f"Missing fields: {[f for f in required_fields if f not in result]}")

            if result['action'] not in ['existing', 'new']:
                raise ValueError(f"Invalid action: {result['action']}")

            # Samenvatting (Plan stap 6 in dezelfde call)
            result['summary'] = (result.get('summary') or '').strip() if isinstance(result.get('summary'), str) else ''

            # Clean folder path
            folder_path = result['folder_path'].strip()
            if not folder_path.startswith('/'):
                folder_path = '/' + folder_path

            result['folder_path'] = folder_path

            # FIX: niet-contracten NOOIT in /Onbekend_adres of /Contracten (AI negeert prompt soms)
            result = _force_non_contract_out_of_contract_folders(result, filename)

            return result

        except json.JSONDecodeError as e:
            print(f"   ⚠️  JSON parse error: {str(e)[:100]}")
            if attempt < MAX_RETRIES - 1:
                print(f"   ⏳ Retry in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
                continue
            else:
                return None

        except Exception as e:
            error_str = str(e)

            if "429" in error_str or "quota" in error_str.lower() or "resource_exhausted" in error_str.lower():
                print(f"   ⚠️  Rate limit reached")
                if attempt < MAX_RETRIES - 1:
                    wait_time = RATE_LIMIT_WAIT * (attempt + 1)
                    print(f"   ⏳ Waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

            print(f"   ❌ Classification error: {str(e)[:100]}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_WAIT)
                continue

            return None

    return None


# ============================================================================
# PHASE 1: ORGANIZE DOCUMENTS
# ============================================================================

def organize_batch(clients, folder_mgr, organized_history, max_docs=BATCH_SIZE):
    """Organize a batch of unorganized PDFs. Gebruikt 8 organize-keys (KEY_1..8), max 18/24u per key via get_next_organize_key()."""

    dbx = clients['dbx_organize']
    model = clients['model_organize']

    try:
        # Find unorganized PDFs
        unorganized = []

        result = dbx.files_list_folder(SCAN_ROOT if SCAN_ROOT else '', recursive=False)

        seen_paths = set()  # genormaliseerde paden in deze ronde (voorkom dubbele in één batch)
        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    if entry.name.lower().endswith('.pdf'):
                        path = entry.path_display
                        norm = _normalize_dropbox_path(path)

                        # Skip if already organized (genormaliseerd)
                        if norm in organized_history:
                            continue
                        # Skip als we dit bestand al in deze ronde hebben (geen dubbele verwerking)
                        if norm in seen_paths:
                            continue
                        seen_paths.add(norm)

                        # Skip if in excluded folders
                        skip = False
                        for excluded in EXCLUDE_FOLDERS:
                            if path.startswith(excluded):
                                skip = True
                                break

                        if not skip:
                            unorganized.append({
                                'path': path,
                                'name': entry.name,
                                'size': entry.size
                            })

            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)

        if not unorganized:
            return 0

        # Process batch (max_docs, geen duplicaten door seen_paths)
        batch = unorganized[:max_docs]
        print(f"\n📦 Processing batch of {len(batch)} document(s)")

        for i, pdf_info in enumerate(batch, 1):
            try:
                print(f"\n{'='*70}")
                print(f"📄 [{i}/{len(batch)}] {pdf_info['name']}")
                print(f"{'='*70}")

                # Rotator: 8 keys voor ordenen, max 18/24u per key
                api_key, key_idx = get_next_organize_key()
                if api_key is None:
                    logger.error("Geen organize-key beschikbaar (alle 8 keys op 18/24u). Stop batch.")
                    break
                summary_list = get_gemini_key_rotator_state_summary()
                organize_summary = [s for s in summary_list if s[0] == "organize"]
                if key_idx < len(organize_summary):
                    _, _, count, _ = organize_summary[key_idx]
                    print(f"🔑 Organize key {key_idx + 1}/8 ({count}/{MAX_CALLS_PER_KEY_PER_24H} in 24u)")
                gemini = genai.Client(api_key=api_key)

                # Download
                print(f"⬇️  Downloading...")
                _, response = dbx.files_download(pdf_info['path'])

                # Extract text
                text, pdf_metadata = extract_text_with_ocr(response.content, gemini, model)

                if not text or len(text) < MIN_TEXT_FOR_PROCESSING:
                    logger.warning(f"⚠️  Insufficient text ({len(text)} chars) - skipping")
                    add_to_history(ORGANIZED_HISTORY, pdf_info['path'])
                    continue

                extraction_info = f"{pdf_metadata.get('extraction_method', 'text').upper()}"
                print(f"✓ Text: {len(text)} chars via {extraction_info}")
                record_extraction_method(pdf_metadata.get("ocr_engine", "text_layer"))

                # Scan folders
                folder_mgr.scan_organized_folders()
                folder_summary = folder_mgr.get_folder_summary()

                # AI classification
                print("🤖 AI analyzing document...")
                result = smart_classify(text, pdf_info['name'], pdf_info['path'],
                                      folder_summary, gemini, model, pdf_metadata)

                if not result:
                    print(f"❌ Classification failed - document stays where it is")
                    continue

                action = result['action']
                folder_path = result['folder_path']
                confidence = result['confidence']
                reasoning = result['reasoning']

                # Bestemming bestandsnaam: AI geeft folder_path + suggested_filename (geen override meer)
                suggested = result.get('suggested_filename')
                dest_name = FolderManager.sanitize_suggested_filename(suggested, "") if suggested else ""
                if not dest_name:
                    dest_name = FolderManager.fallback_filename_from_folder(folder_path, pdf_info['name'])

                print(f"\n📊 AI DECISION:")
                print(f"   Action: {action.upper()}")
                print(f"   Folder: {folder_path}")
                print(f"   Confidence: {confidence}%")
                print(f"   Reason: {reasoning}")
                if result.get('summary'):
                    print(f"   Summary: {result['summary'][:120]}{'…' if len(result.get('summary', '')) > 120 else ''}")

                # Create new folder if needed
                if action == "new":
                    description = result.get('description', reasoning)
                    print(f"\n📁 Creating new folder...")
                    if not folder_mgr.create_folder(folder_path, description):
                        continue

                # Bestandsnaam: altijd hernoemen naar Adres_Type.pdf (AI-suggestie of fallback)
                if dest_name != pdf_info['name']:
                    print(f"   📝 Hernoemen: {pdf_info['name']} → {dest_name}")

                # Move document (eventueel met nieuwe naam)
                full_folder_path = folder_mgr.sanitize_folder_path(folder_path)
                new_path = f"{full_folder_path}/{dest_name}"

                print(f"\n📤 Moving...")
                try:
                    dbx.files_move_v2(pdf_info['path'], new_path, autorename=True)
                    print(f"✅ SUCCESS → {full_folder_path}")

                    # Update stats
                    if full_folder_path in folder_mgr.folders:
                        folder_mgr.folders[full_folder_path]['file_count'] = \
                            folder_mgr.folders[full_folder_path].get('file_count', 0) + 1
                        folder_mgr.save_cache()

                    # Add to history
                    add_to_history(ORGANIZED_HISTORY, pdf_info['path'])

                    # Plan stap 1+6: samenvatting uit dezelfde call opslaan in Dropbox (zelfde map als het document)
                    summary_text = result.get('summary') or ''
                    if summary_text:
                        dest_stem = dest_name.rsplit('.', 1)[0] if '.' in dest_name else dest_name
                        summary_path = f"{full_folder_path}/{dest_stem}_summary.json"
                        metadata = {
                            'summary': summary_text,
                            'classification': {
                                'action': action,
                                'folder_path': folder_path,
                                'confidence': confidence,
                                'reasoning': reasoning,
                                'suggested_filename': result.get('suggested_filename'),
                            },
                            'timestamp': datetime.utcnow().isoformat() + 'Z',
                            'source_file': pdf_info['name'],
                        }
                        try:
                            dbx.files_upload(
                                json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'),
                                summary_path,
                                mode=dropbox.files.WriteMode.overwrite,
                            )
                            print(f"   📄 Samenvatting opgeslagen: {dest_stem}_summary.json")
                        except Exception as up_err:
                            logger.warning(f"Summary upload failed: {up_err}")

                    # Stap 7: geëxtraheerde tekst in Supabase voor zoekfeature (met retry)
                    supabase_config = clients.get('supabase')
                    if supabase_config and text:
                        for attempt in (1, 2):
                            try:
                                print(f"   → Saving to document_texts: {dest_name}" + (" (retry)" if attempt == 2 else ""))
                                supabase_upsert_document_text(supabase_config, new_path, dest_name, text)
                                print(f"   📄 Tekst opgeslagen in Supabase (document_texts) → zoekbaar op /zoeken")
                                break
                            except Exception as doc_err:
                                logger.warning(f"document_texts save failed (attempt {attempt}): {doc_err}")
                                if attempt == 2:
                                    print(f"   ❌ document_texts NIET opgeslagen na 2 pogingen: {doc_err}")
                                else:
                                    print(f"   ⚠️ document_texts save failed, retry...")
                    elif not supabase_config:
                        print(f"   ⚠️ Supabase config ontbreekt — document_texts niet opgeslagen")
                    elif not text:
                        print(f"   ⚠️ Geen tekst om op te slaan — document_texts overgeslagen")

                except dropbox.exceptions.ApiError as e:
                    print(f"❌ Move error: {e}")
                    continue

                # Rate limiting between documents
                if i < len(batch):
                    print(f"\n⏳ Waiting 5s before next document...")
                    time.sleep(5)

            except Exception as e:
                print(f"❌ Processing error: {e}")
                continue

        return len(batch)

    except Exception as e:
        print(f"❌ Batch organize error: {e}")
        return 0


# ============================================================================
# PHASE 2: RENTAL CONTRACT DETECTION
# ============================================================================

def find_rental_contract_folders(dbx):
    """Find folders likely containing rental contracts"""

    rental_folders = []

    try:
        # Check if organized folder exists
        try:
            dbx.files_get_metadata(ORGANIZED_FOLDER_PREFIX)
        except dropbox.exceptions.ApiError:
            print("⚠️  Organized folder niet gevonden")
            return []

        # Scan recursively
        result = dbx.files_list_folder(ORGANIZED_FOLDER_PREFIX, recursive=True)

        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FolderMetadata):
                    path = entry.path_display
                    path_lower = path.lower()

                    # Check if folder name contains rental keywords
                    if any(keyword in path_lower for keyword in RENTAL_KEYWORDS):
                        rental_folders.append(path)
                        print(f"   ✓ Huurcontract folder gevonden: {path}")

            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)

    except Exception as e:
        print(f"⚠️  Rental folder scan error: {e}")

    return rental_folders


def find_pdfs_in_folders(dbx, folders):
    """Find all PDFs in specified folders"""

    pdfs = []

    for folder in folders:
        try:
            result = dbx.files_list_folder(folder, recursive=False)

            while True:
                for entry in result.entries:
                    if isinstance(entry, dropbox.files.FileMetadata):
                        if entry.name.lower().endswith('.pdf'):
                            pdfs.append({
                                'path': entry.path_display,
                                'name': entry.name,
                                'folder': folder,
                                'size': entry.size
                            })

                if not result.has_more:
                    break
                result = dbx.files_list_folder_continue(result.cursor)

        except Exception as e:
            print(f"⚠️  Error scanning {folder}: {e}")
            continue

    return pdfs


# ============================================================================
# PHASE 2: CONTRACT DATA EXTRACTION (Stap 4+5: regels eerst, dan AI voor ontbrekende velden)
# ============================================================================

def try_regex_contract_fields(full_text: str) -> Dict[str, dict]:
    """
    Stap 4: vaste velden via regels/regex. Alles wat hier uit komt hoeft geen Gemini-call.
    Returns dict stage_name -> { field: value } voor zover gevonden.
    """
    out = {}
    if not full_text or len(full_text) < 50:
        return out
    text = full_text.replace("\n", " ")

    # Huurprijs: € 1150 / 1150 euro / 1150,00 EUR
    m = re.search(r"(?:€|euro|eur)\s*:?\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:€|euro|eur)", text, re.I)
    if m:
        raw = (m.group(1) or m.group(2) or "").replace(",", ".")
        try:
            out.setdefault("financieel", {})["huurprijs"] = float(raw)
        except ValueError:
            pass
    if not out.get("financieel") and re.search(r"maandhuur\s*[:\s]*(\d+(?:[.,]\d+)?)", text, re.I):
        m = re.search(r"maandhuur\s*[:\s]*(\d+(?:[.,]\d+)?)", text, re.I)
        if m:
            try:
                out.setdefault("financieel", {})["huurprijs"] = float(m.group(1).replace(",", "."))
            except ValueError:
                pass

    # Ingangsdatum: 01/05/2025, 2025-05-01, 1 mei 2025
    for pat in [
        r"(?:ingangsdatum|aanvang|start)\s*[:\s]*(\d{4}-\d{2}-\d{2})",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2})/(\d{1,2})/(\d{4})",
        r"(\d{1,2})\s+(?:januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)\s+(\d{4})",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            g = m.groups()
            if len(g) == 1 and len(g[0]) == 10:
                out.setdefault("periodes", {})["ingangsdatum"] = g[0]
                break
            if len(g) == 3 and len(g[2]) == 4:
                try:
                    y, mth, d = int(g[2]), int(g[1]) if g[1].isdigit() else 0, int(g[0])
                    if 1 <= mth <= 12 and 1 <= d <= 31:
                        out.setdefault("periodes", {})["ingangsdatum"] = f"{y}-{mth:02d}-{d:02d}"
                except (ValueError, TypeError):
                    pass
                break

    # Adres pand: Straatnaam 123 (bus X), postcode Stad
    addr = re.search(
        r"(?:gelegen te|adres|adres van het goed)\s*[:\s]*([A-Za-zÀ-ÿ\s\-]+?\d+[A-Za-z]?(?:\s*bus\s*\d+)?(?:\s*,?\s*\d{4}\s+[A-Za-zÀ-ÿ\s\-]+)?)",
        text,
        re.I,
    )
    if addr:
        adr = addr.group(1).strip()
        if len(adr) > 5 and len(adr) < 200:
            out.setdefault("pand", {})["adres"] = adr[:150]

    return out


def extract_contract_data(full_text, gemini_client, model, initial_data: Optional[Dict[str, dict]] = None):
    """
    Multi-stage huurcontract extractor. Stap 4+5: eerst regels (initial_data), dan AI voor ontbrekende secties.
    """
    if initial_data is None:
        initial_data = try_regex_contract_fields(full_text)
        if initial_data:
            print(f"   📐 Regels/regex: {list(initial_data.keys())} (minder API-calls)")

    # Split text in chunks voor betere extractie
    text_chunk_1 = full_text[:TEXT_CHUNK_1_SIZE]
    text_chunk_2 = full_text[TEXT_CHUNK_OVERLAP:TEXT_CHUNK_2_SIZE] if len(full_text) > TEXT_CHUNK_OVERLAP else ""

    print("   🎯 Starting extraction (regels + AI voor ontbrekende velden)...")

    extracted_sections = {}

    # ========================================================================
    # STAGE 1: PARTIJEN (Verhuurder + Huurder)
    # ========================================================================

    partijen_prompt = f"""Je bent een expert in Belgische huurcontracten. 

TAAK: Extraheer ALLE informatie over verhuurder(s) en huurder(s).

ZOEK SPECIFIEK NAAR:
- Volledige namen (voor + achternaam, of bedrijfsnaam)
- Adressen (straat + nummer + bus + postcode + stad)
- Telefoonnummers (vast + GSM)
- Email adressen
- BTW nummers (voor bedrijven)
- Rijksregisternummers

BELANGRIJK:
- Als er MEERDERE huurders zijn → combineer namen met " & "
- Als adres NIET vermeld → gebruik "ONTBREKEND"
- Als telefoon NIET vermeld → gebruik "ONTBREKEND"
- Kopieer exacte spelling uit contract

CONTRACT TEKST:
{text_chunk_1}

VOORBEELD OUTPUT (volg deze structuur EXACT):
{{
  "verhuurder": {{
    "naam": "Vastgoed Beheer NV",
    "adres": "Industrielaan 5, 9000 Gent",
    "telefoon": "+32 9 123 45 67",
    "email": "info@vastgoedbeheer.be"
  }},
  "huurder": {{
    "naam": "Marie Dupont & Peter Vermeulen",
    "adres": "Voorlopig adres: Kerkstraat 5, 1000 Brussel",
    "telefoon": "+32 2 987 65 43",
    "email": "marie.dupont@email.be"
  }}
}}

ALLEEN JSON (geen tekst ervoor/erna):"""

    # ========================================================================
    # STAGE 2: PAND (Adres + Kenmerken + EPC + Kadaster + Asbest + Overzicht)
    # ========================================================================
    # Gedetailleerde extractie zodat Huizen-tab optimaal kan zoeken op EPC, kadastraal, asbest en overzicht.

    pand_prompt = f"""Je bent een expert in Belgische huurcontracten en vastgoeddocumenten.

TAAK: Extraheer ALLE informatie over het gehuurde pand. Deze data wordt gebruikt voor zoeken en overzicht (EPC, kadastraal, asbest, pandoverzicht). Wees zo volledig en precies mogelijk.

══════════════════════════════════════════════════════════════════════════════
1. ADRES EN WOONKENMERKEN
══════════════════════════════════════════════════════════════════════════════
- Volledig adres: straat, nummer, bus, postcode, stad (exact zoals in document)
- Type woning: appartement, huis, studio, duplex, enz.
- Bewoonbare oppervlakte in m² (alleen getal, geen "m²")
- Aantal kamers / slaapkamers
- Verdieping (getal of "gelijkvloers", "kelder")
- Eventueel: bouwjaar, staat van het pand

══════════════════════════════════════════════════════════════════════════════
2. EPC (Energieprestatiecertificaat) — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- energielabel: exacte letter/klasse (A++, A+, A, B, C, D, E, F, G)
- certificaatnummer: volledig nummer (bv. 20231205-0001234-00000001 of formaat op het attest)
- geldig_tot: einddatum geldigheid EPC (formaat YYYY-MM-DD); als alleen jaar → YYYY-12-31
- bewoonbare_oppervlakte_epc: oppervlakte in m² zoals op EPC vermeld (getal)
- primair_energieverbruik: indien vermeld (kWh/m² jaar of totaal)
- referentiejaar: indien vermeld op EPC
Als EPC niet in de tekst staat → gebruik "ONTBREKEND" per veld.

══════════════════════════════════════════════════════════════════════════════
3. KADASTER — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- afdeling: kadastrale afdeling (bv. "Brussel 1e afdeling", "Antwerpen 2e afdeling")
- sectie: kadastrale sectie (letter of code)
- nummer: perceelnummer / lotnummer (bv. "123/4B", "456", "789/C")
- kadastraal_inkomen: bedrag in euro (alleen getal)
- gemeente_kadaster: gemeente volgens kadaster indien anders dan adres
- grondnummer: indien vermeld
Kadastraal inkomen = KI; zoek ook naar "kadastraal inkomen", "KI", "indexcijfer". Als niet vermeld → "ONTBREKEND" of null.

══════════════════════════════════════════════════════════════════════════════
4. ASBEST (Asbestattest / asbestinventaris) — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- status: "asbestvrij" / "bevat_geen_asbest" / "bevat_asbest" / "niet_onderzocht" / "ONTBREKEND"
- datum_attest: datum van het asbestattest (YYYY-MM-DD)
- referentienummer: referentie- of attestnummer indien vermeld
- opmerking: korte opmerking indien van toepassing (bv. "asbestvrij verklaard na verwijdering")
- geldig_tot: indien geldigheidsduur vermeld
Zoek naar: asbestattest, asbestinventaris, asbestvrij, asbestvrijverklaring, bevat geen asbest, asbest bevat. Als geen asbestinfo in document → status "ONTBREKEND", rest leeg of null.

══════════════════════════════════════════════════════════════════════════════
5. OVERZICHT
══════════════════════════════════════════════════════════════════════════════
Deze velden samen vormen het "overzicht" per pand. Vul elk veld in dat je in de tekst vindt; gebruik "ONTBREKEND" of null alleen als het echt ontbreekt. Geen veld weglaten in de JSON-structuur.

BELANGRIJK (altijd toepassen):
- Oppervlakte = alleen het getal (geen "m²", geen eenheid). Ook bewoonbare_oppervlakte_epc = getal.
- Kadastraal inkomen = bedrag in euro, alleen het getal (geen €, geen eenheid).
- Als iets NIET in de tekst vermeld staat → gebruik "ONTBREKEND" (string) of null voor optionele velden.
- Kopieer exacte adressen zoals in het contract (geen afkortingen tenzij zo in document).

CONTRACT TEKST:
{text_chunk_1}

Extra context (voor EPC/kadaster/asbest die later in document staan):
{text_chunk_2[:6000] if text_chunk_2 else ""}

VOORBEELD OUTPUT (volg deze structuur; alle sleutels aanwezig, ontbrekende waarden "ONTBREKEND" of null):
{{
  "adres": "Kerkstraat 10 bus 3, 1000 Brussel",
  "type": "appartement",
  "oppervlakte": 85.5,
  "aantal_kamers": 3,
  "verdieping": 2,
  "epc": {{
    "energielabel": "B",
    "certificaatnummer": "20231205-0001234-00000001",
    "geldig_tot": "2030-12-31",
    "bewoonbare_oppervlakte_epc": 85,
    "primair_energieverbruik": null,
    "referentiejaar": null
  }},
  "kadaster": {{
    "afdeling": "Brussel 1e afdeling",
    "sectie": "A",
    "nummer": "123/4B",
    "kadastraal_inkomen": 1234.56,
    "gemeente_kadaster": null,
    "grondnummer": null
  }},
  "asbest": {{
    "status": "asbestvrij",
    "datum_attest": "2024-06-15",
    "referentienummer": "ATT-2024-12345",
    "opmerking": null,
    "geldig_tot": null
  }}
}}

ALLEEN JSON (geen tekst ervoor of erna):"""

    # ========================================================================
    # STAGE 3: FINANCIEEL (Huur + Waarborg + Kosten + Indexatie)
    # ========================================================================

    financieel_prompt = f"""Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE financiële informatie.

ZOEK SPECIFIEK NAAR:
- Maandelijkse huurprijs (bedrag in euro)
- Waarborg/huurwaarborg bedrag
- Bank waar waarborg gedeponeerd is (naam + IBAN rekening)
- Gemeenschappelijke kosten (wat is inbegrepen)
- Privélasten (energie, water, gas, internet)
- Indexatie (ja/nee)

BELANGRIJK:
- Huurprijs = alleen het getal (geen € teken)
- Waarborg bedrag = alleen het getal
- waar_gedeponeerd = "Banknaam (rekening BE12 3456 7890 1234)"
- kosten = beschrijving in tekst (wat inbegrepen, wat apart)
- indexatie = true/false

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2[:5000] if text_chunk_2 else ""}

VOORBEELD OUTPUT:
{{
  "huurprijs": 1150.0,
  "waarborg": {{
    "bedrag": 3450.0,
    "waar_gedeponeerd": "Belfius Bank (rekening BE71 0961 2345 6769)"
  }},
  "kosten": "Gemeenschappelijke kosten (verwarming, water, lift) zijn inbegrepen in de huurprijs. Privélasten (elektriciteit, gas, internet) zijn voor rekening van huurder.",
  "indexatie": true,
  "gemeenschappelijke_kosten": {{
    "inbegrepen": [
      {{"post": "Verwarming"}},
      {{"post": "Water"}},
      {{"post": "Lift"}},
      {{"post": "Gemeenschappelijke delen"}}
    ]
  }}
}}

ALLEEN JSON:"""

    # ========================================================================
    # STAGE 4: PERIODES (Data + Duur + Opzegtermijnen)
    # ========================================================================

    periodes_prompt = f"""Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE informatie over periodes en termijnen.

ZOEK SPECIFIEK NAAR:
- Ingangsdatum / aanvangsdatum (datum wanneer huur start)
- Einddatum (als contract bepaalde duur heeft)
- Duur van het contract (bijv. "9 jaar", "3 jaar", "onbepaalde duur")
- Opzegtermijn voor huurder (hoeveel maanden)
- Opzegtermijn voor verhuurder (hoeveel maanden)
- Eventuele verlengingsvoorwaarden

BELANGRIJK:
- Datums in formaat YYYY-MM-DD (bijv. "2025-05-01")
- Als geen einddatum → "ONTBREKEND"
- Opzegtermijnen apart voor huurder en verhuurder
- Duur = letterlijk zoals in contract staat

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2[:5000] if text_chunk_2 else ""}

VOORBEELD OUTPUT:
{{
  "ingangsdatum": "2025-05-01",
  "einddatum": "ONTBREKEND",
  "duur": "9 jaar",
  "opzegtermijn_huurder": "3 maanden",
  "opzegtermijn_verhuurder": "6 maanden"
}}

ALLEEN JSON:"""

    # ========================================================================
    # STAGE 5: VOORWAARDEN (Huisdieren + Onderverhuur + Werken)
    # ========================================================================

    voorwaarden_prompt = f"""Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE bijzondere voorwaarden en bepalingen.

ZOEK SPECIFIEK NAAR:
- Huisdieren toegestaan? (ja/nee/met toestemming)
- Onderverhuur toegestaan? (ja/nee)
- Werken/verbouwingen (wat mag/niet mag)
- Andere bijzondere bepalingen

BELANGRIJK:
- huisdieren = true/false/"ONTBREKEND"
- onderverhuur = true/false
- werken = beschrijving in tekst

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2[:5000] if text_chunk_2 else ""}

VOORBEELD OUTPUT:
{{
  "huisdieren": true,
  "onderverhuur": false,
  "werken": "Kleine herstellingswerken toegestaan. Grotere verbouwingen enkel met schriftelijke toestemming van verhuurder."
}}

ALLEEN JSON:"""

    # ========================================================================
    # STAGE 6: JURIDISCH (Recht + Rechtbank)
    # ========================================================================

    juridisch_prompt = f"""Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer juridische bepalingen.

ZOEK SPECIFIEK NAAR:
- Toepasselijk recht (bijv. "Vlaams Woninghuurdecreet")
- Bevoegde rechtbank / vrederechter (bijv. "Vrederechter van het kanton Gent")

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2[:5000] if text_chunk_2 else ""}

VOORBEELD OUTPUT:
{{
  "toepasselijk_recht": "Vlaams Woninghuurdecreet van 9 november 2018",
  "bevoegde_rechtbank": "Vrederechter van het kanton Brussel"
}}

ALLEEN JSON:"""

    # ========================================================================
    # STAGE 7: CONTRACT METADATA (Type + Datum)
    # ========================================================================

    metadata_prompt = f"""Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer algemene contract informatie.

ZOEK SPECIFIEK NAAR:
- Type contract (huurovereenkomst/huurcontract)
- Datum van ondertekening contract

CONTRACT TEKST:
{text_chunk_1[:5000]}

VOORBEELD OUTPUT:
{{
  "contract_type": "huurovereenkomst",
  "datum_contract": "2025-03-18"
}}

ALLEEN JSON:"""

    # ========================================================================
    # EXECUTE ALL STAGES
    # ========================================================================

    stages = {
        "metadata": metadata_prompt,
        "partijen": partijen_prompt,
        "pand": pand_prompt,
        "financieel": financieel_prompt,
        "periodes": periodes_prompt,
        "voorwaarden": voorwaarden_prompt,
        "juridisch": juridisch_prompt
    }

    for stage_name, prompt in stages.items():
        print(f"      📊 Extracting {stage_name}...")
        # Altijd AI-extractie per stage (geen skip op basis van regex), zodat we alle velden krijgen.
        # initial_data van regex wordt niet meer gebruikt om stages over te slaan.
        for attempt in range(3):
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json"
                    )
                )

                # Parse JSON
                raw = response.text.strip()

                # Clean up mogelijk markdown
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()

                stage_data = json.loads(raw)
                extracted_sections[stage_name] = stage_data

                # Count extracted fields
                non_null_count = sum(1 for v in json.dumps(stage_data).split()
                                     if v not in ['null', 'None', '""', '{}', '[]',"ONTBREKEND"])
                print(f"         ✓ {non_null_count} data points extracted")
                break

            except json.JSONDecodeError as e:
                print(f"         ⚠️  JSON parse error (attempt {attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"         ❌ Failed to extract {stage_name}")
                    extracted_sections[stage_name] = {}

            except Exception as e:
                error_msg = str(e)
                if is_quota_error(error_msg):
                    return {"error": "QUOTA_EXCEEDED", "details": error_msg}
                
                if is_api_key_error(error_msg):
                    return {"error": "API_KEY_ERROR", "details": error_msg}

                if "429" in error_msg and attempt < 2:
                    print(f"         ⚠️  Rate limit - waiting...")
                    time.sleep(RETRY_WAIT)
                else:
                    print(f"         ❌ Error: {str(e)[:100]}")
                    extracted_sections[stage_name] = {}
                    break

        # Rate limiting tussen stages - 5 RPM = min 12 seconden tussen requests
        # 7 stages per contract, dus min 12s tussen elke stage
        time.sleep(12)  # Na elke stage - respecteert 5 RPM limiet

    # Alle stages zijn via AI uitgevoerd (geen skip meer op basis van regex)
    record_contract_stages(0, len(stages))

    # ========================================================================
    # MERGE ALL STAGES
    # ========================================================================

    metadata = extracted_sections.get("metadata", {})

    final_data = {
        "contract_type": metadata.get("contract_type", "huurovereenkomst"),
        "datum_contract": metadata.get("datum_contract"),
        "partijen": extracted_sections.get("partijen", {}),
        "pand": extracted_sections.get("pand", {}),
        "financieel": extracted_sections.get("financieel", {}),
        "periodes": extracted_sections.get("periodes", {}),
        "voorwaarden": extracted_sections.get("voorwaarden", {}),
        "juridisch": extracted_sections.get("juridisch", {})
    }

    # ========================================================================
    # VALIDATION & REPORTING
    # ========================================================================

    print("\n      📋 EXTRACTION SUMMARY:")

    critical_fields = {
        "Huurprijs": final_data.get("financieel", {}).get("huurprijs"),
        "Ingangsdatum": final_data.get("periodes", {}).get("ingangsdatum"),
        "Verhuurder naam": final_data.get("partijen", {}).get("verhuurder", {}).get("naam"),
        "Huurder naam": final_data.get("partijen", {}).get("huurder", {}).get("naam"),
        "Pand adres": final_data.get("pand", {}).get("adres")
    }

    found_critical = sum(1 for v in critical_fields.values() if v)
    print(f"         Critical fields: {found_critical}/5")

    for field, value in critical_fields.items():
        status = "✓" if value else "✗"
        print(f"         {status} {field}: {value or 'MISSING'}")

    # Count all non-null fields
    total_fields = 0
    filled_fields = 0

    def count_fields(obj):
        nonlocal total_fields, filled_fields
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    count_fields(v)
                else:
                    total_fields += 1
                    if v is not None and v != "" and v != []:
                        filled_fields += 1
        elif isinstance(obj, list):
            for item in obj:
                count_fields(item)

    count_fields(final_data)

    completeness = (filled_fields / total_fields * 100) if total_fields > 0 else 0
    print(f"         Overall completeness: {completeness:.0f}% ({filled_fields}/{total_fields} fields)")

    return final_data


# ============================================================================
# CONFIDENCE & SUMMARY GENERATION
# ============================================================================

def calculate_confidence_normalized(normalized_data: dict, full_text: str,
                                   doc_type: str, type_verified: bool) -> dict:
    """Calculate confidence score based on normalized data completeness"""

    score = 0
    issues = []
    warnings = []

    # Base score for verified document type
    if type_verified:
        score += 20

    # Critical fields check (40 points max)
    critical_fields = {
        'huurprijs': normalized_data.get('financieel', {}).get('huurprijs'),
        'ingangsdatum': normalized_data.get('periodes', {}).get('ingangsdatum'),
        'verhuurder_naam': normalized_data.get('partijen', {}).get('verhuurder', {}).get('naam'),
        'huurder_naam': normalized_data.get('partijen', {}).get('huurder', {}).get('naam'),
        'pand_adres': normalized_data.get('pand', {}).get('adres')
    }

    found_critical = sum(1 for v in critical_fields.values() if v and v != "")
    critical_score = (found_critical / len(critical_fields)) * 40
    score += critical_score

    if found_critical < len(critical_fields):
        missing = [k for k, v in critical_fields.items() if not v or v == ""]
        issues.append(f"Missing critical: {', '.join(missing)}")

    # Overall completeness (30 points max)
    def count_filled_fields(obj):
        total = 0
        filled = 0
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    t, f = count_filled_fields(v)
                    total += t
                    filled += f
                else:
                    total += 1
                    if v is not None and v != "" and v != [] and v != {}:
                        filled += 1
        elif isinstance(obj, list):
            for item in obj:
                t, f = count_filled_fields(item)
                total += t
                filled += f
        return total, filled

    total_fields, filled_fields = count_filled_fields(normalized_data)
    completeness = (filled_fields / total_fields) if total_fields > 0 else 0
    score += completeness * 30

    # Text quality (10 points max)
    text_length = len(full_text.strip())
    if text_length < 500:
        warnings.append(f"Short document: {text_length} characters")
        score -= 5
    elif text_length > 2000:
        score += 10
    else:
        score += 5

    # Normalize score
    score = max(0, min(100, round(score, 1)))

    # Determine if review needed
    needs_review = score < TARGET_CONFIDENCE or len(issues) > 0

    # Build details string
    details_parts = []
    if issues:
        details_parts.append("ISSUES:\n- " + "\n- ".join(issues))
    if warnings:
        details_parts.append("WARNINGS:\n- " + "\n- ".join(warnings))

    if not details_parts:
        details_parts.append("All critical fields present")
        details_parts.append(f"Data completeness: {completeness:.0%}")

    return {
        'score': score,
        'needs_review': needs_review,
        'issues': issues,
        'warnings': warnings,
        'details': "\n\n".join(details_parts),
        'metrics': {
            'text_length': text_length,
            'completeness': completeness,
            'critical_fields_found': found_critical,
            'critical_fields_total': len(critical_fields)
        }
    }


def generate_summary(full_text: str, doc_type: str, gemini_client, model) -> str:
    """Generate natural language summary using Gemini"""

    # Use first N chars for summary
    text_sample = full_text[:SUMMARY_TEXT_SIZE] if len(full_text) > SUMMARY_TEXT_SIZE else full_text

    prompt = f"""Maak een bondige samenvatting van dit {doc_type} in maximaal 3 korte alinea's.

Focus op:
- Partijen (verhuurder en huurder)
- Pand (adres en kenmerken)
- Financiële voorwaarden (huur, waarborg)
- Belangrijkste termijnen en voorwaarden

CONTRACT TEKST:
{text_sample}

Geef ALLEEN de samenvatting (geen introductie):"""

    try:
        for attempt in range(MAX_RETRIES):
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=3000
                    )
                )

                summary = response.text.strip()
                return summary

            except Exception as e:
                error_str = str(e)

                # Check for API key errors
                if is_api_key_error(error_str):
                    return "API_KEY_ERROR"
                
                # Check for quota errors
                if is_quota_error(error_str):
                    return "QUOTA_EXCEEDED"

                # Rate limiting
                if "429" in error_str and attempt < MAX_RETRIES - 1:
                    print(f"      Rate limit - waiting {RATE_LIMIT_WAIT}s...")
                    time.sleep(RATE_LIMIT_WAIT)
                    continue

                # Other errors
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_WAIT)
                    continue
                else:
                    raise

        return "Summary generation failed after multiple attempts"

    except Exception as e:
        return f"Error generating summary: {str(e)[:200]}"


# ============================================================================
# PHASE 2: PROCESS RENTAL CONTRACT
# ============================================================================

def process_rental_contract(clients, pdf_info):
    """Process and analyze a rental contract. Gebruikt rotator: 12 extract-keys (KEY_9..20), max 18/24u per key."""

    dbx_analyze = clients['dbx_analyze']
    dbx_target = clients['dbx_target']
    model = clients['model_analyze']

    # Volgende extract-key (KEY_9..20); 1 key per contract
    api_key, key_idx = get_next_extract_key()
    if api_key is None:
        logger.error("Geen extract-key beschikbaar (alle 12 keys op 18/24u). Sla contract over.")
        return {'quota_error': True, 'requeue': True}
    gemini = genai.Client(api_key=api_key)
    summary_list = get_gemini_key_rotator_state_summary()
    extract_summary = [s for s in summary_list if s[0] == "extract"]
    if key_idx < len(extract_summary):
        _, _, count, _ = extract_summary[key_idx]
        print(f"🔑 Extract key {key_idx + 1}/12 ({count}/{MAX_CALLS_PER_KEY_PER_24H} in 24u)")

    # BELANGRIJK: Voeg direct toe aan history zodra processing start
    # Dit voorkomt dat hetzelfde document meerdere keren wordt verwerkt
    add_to_history(ANALYZED_HISTORY, pdf_info['path'])
    print(f"   📝 Added to history: {pdf_info['path']}")

    try:
        print(f"\n{'='*70}")
        print(f"📄 {pdf_info['name']}")
        print(f"📂 Location: {pdf_info['folder']}")
        print(f"{'='*70}")

        start_time = time.time()

        # Download and extract text WITH OCR fallback
        print(f"⬇️  Downloading...")
        _, response = dbx_analyze.files_download(pdf_info['path'])

        print(f"📖 Extracting text...")
        full_text, pdf_metadata = extract_text_with_ocr(response.content, gemini, model)
        print(f"✓ Text: {len(full_text)} chars")
        record_extraction_method(pdf_metadata.get("ocr_engine", "text_layer"))

        if len(full_text.strip()) < 50:
            print(f"⚠️  Too little text - skipping")
            return None

        # Extract raw data
        print("🤖 Gemini analyzing contract...")
        contract_data = extract_contract_data(full_text, gemini, model)

        if contract_data.get('error') == 'QUOTA_EXCEEDED':
            print(f"❌ QUOTA EXCEEDED - requeuing document for retry")
            # Remove from history so it will be retried later
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'quota_error': True, 'requeue': True}
        
        if contract_data.get('error') == 'API_KEY_ERROR':
            print(f"❌ API KEY ERROR - requeuing document for retry")
            print(f"   Details: {contract_data.get('details', 'Unknown API key error')}")
            # Remove from history so it will be retried when API key is fixed
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'api_key_error': True, 'requeue': True}

        # Normalize data
        normalized_data = normalizer.normalize(contract_data)

        # Calculate confidence
        confidence = calculate_confidence_normalized(normalized_data, full_text,
                                                     "huurovereenkomst", True)

        # Generate summary
        summary = generate_summary(full_text, "huurovereenkomst", gemini, model)

        if "QUOTA_EXCEEDED" in summary:
            print(f"❌ QUOTA EXCEEDED - requeuing document for retry")
            # Remove from history so it will be retried later
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'quota_error': True, 'requeue': True}
        
        if "API_KEY_ERROR" in summary or is_api_key_error(summary):
            print(f"❌ API KEY ERROR in summary - requeuing document for retry")
            # Remove from history so it will be retried when API key is fixed
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'api_key_error': True, 'requeue': True}

        processing_time = time.time() - start_time

        print(f"✓ Processed in {processing_time:.1f}s - Score: {confidence['score']}%")

        result = {
            "success": True,
            "filename": pdf_info['name'],
            "title": "Huurovereenkomst",
            "type_verified": True,
            "full_text": full_text,
            "raw_data": contract_data,
            "normalized_data": normalized_data,
            "summary": summary,
            "confidence": confidence,
            "processing_time": processing_time
        }

        # Save JSON to TARGET
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = pdf_info['name'].replace('.pdf', '')

        json_data = {
            "filename": result['filename'],
            "document_type": result['title'],
            "type_verified": result['type_verified'],
            "processed": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "confidence": result['confidence'],
            "contract_data": normalized_data,
            "summary": result['summary']
        }

        if "raw_data" in result and "error" not in result['raw_data']:
            json_data["raw_data"] = result['raw_data']

        json_name = f"data_{base}_{ts}.json"
        json_file = f"/{json_name}"
        supabase_config = clients.get('supabase')

        # Stap 7: ALTIJD document_texts schrijven (vóór contract) — anders kan pdf-path in de app geen PDF koppelen
        if supabase_config:
            text_to_store = (full_text or "").strip()
            if not text_to_store:
                print(f"   ⚠️ Geen full_text — schrijven toch rij in document_texts (path/naam voor pdf-koppeling)")
            for attempt in (1, 2):
                try:
                    print(f"   → Saving to document_texts: {pdf_info['name']}" + (" (retry)" if attempt == 2 else ""))
                    supabase_upsert_document_text(supabase_config, pdf_info['path'], pdf_info['name'], text_to_store)
                    print(f"   📄 document_texts opgeslagen → zoekbaar / pdf-koppeling luik 4")
                    break
                except Exception as doc_err:
                    logger.warning(f"document_texts save failed (attempt {attempt}): {doc_err}")
                    if attempt == 2:
                        print(f"   ❌ CRITICAL: document_texts NIET opgeslagen na 2 pogingen: {doc_err}")
                        print(f"   ❌ PDF zal niet gekoppeld worden in de app. Controleer Supabase RLS/table.")
                    else:
                        print(f"   ⚠️ document_texts save failed, retry...")
        else:
            print(f"   ⚠️ Geen Supabase config — document_texts overgeslagen")

        # JSON opslaan: alleen Supabase (via REST API)
        print(f"💾 Saving JSON to Supabase...")
        try:
            supabase_upsert_contract(supabase_config, json_name, json_data)
            print(f"✅ JSON saved to Supabase: {json_name}")
        except Exception as e:
            logger.error(f"Supabase upsert failed: {e}")
            print(f"❌ Supabase save failed — JSON niet opgeslagen")
            raise

        # Auto-push to Whise if confidence >= 95%
        conf = confidence  # Define conf variable for consistency
        if conf['score'] >= 95:
            try:
                print(f"🚀 Auto-pushing to Whise (confidence: {conf['score']}%)...")
                
                # Get Whise API credentials from environment
                whise_api_endpoint = os.getenv('WHISE_API_ENDPOINT')
                whise_api_token = os.getenv('WHISE_API_TOKEN')
                
                if whise_api_endpoint and whise_api_token:
                    # Prepare Whise payload
                    whise_payload = {
                        'property_id': normalized_data.get('pand', {}).get('adres') or pdf_info['name'],
                        'contract_data': {
                            'huurprijs': normalized_data.get('financieel', {}).get('huurprijs'),
                            'adres': normalized_data.get('pand', {}).get('adres'),
                            'type': normalized_data.get('pand', {}).get('type'),
                            'oppervlakte': normalized_data.get('pand', {}).get('oppervlakte'),
                            'verhuurder': normalized_data.get('partijen', {}).get('verhuurder', {}).get('naam'),
                            'huurder': normalized_data.get('partijen', {}).get('huurder', {}).get('naam'),
                            'ingangsdatum': normalized_data.get('periodes', {}).get('ingangsdatum'),
                            'einddatum': normalized_data.get('periodes', {}).get('einddatum'),
                        },
                        'metadata': {
                            'filename': pdf_info['name'],
                            'confidence': conf['score'],
                            'processed': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'source': 'contract-system-auto'
                        }
                    }
                    
                    # Make HTTP POST request to Whise API
                    try:
                        import requests
                    except ImportError:
                        raise ImportError("'requests' library is required for Whise API. Install with: pip install requests")
                    
                    response = requests.post(
                        whise_api_endpoint,
                        headers={
                            'Authorization': f'Bearer {whise_api_token}',
                            'Content-Type': 'application/json'
                        },
                        json=whise_payload,
                        timeout=30
                    )
                    
                    if response.status_code == 200 or response.status_code == 201:
                        whise_result = response.json()
                        logger.info(f"✅ Successfully auto-pushed to Whise: {json_file} (Whise ID: {whise_result.get('id', 'N/A')})")
                        print(f"✅ Automatisch gepusht naar Whise (ID: {whise_result.get('id', 'N/A')})")
                        
                        # Add whise_pushed flag to JSON data
                        json_data['whise_pushed'] = True
                        json_data['whise_id'] = whise_result.get('id')
                        json_data['whise_pushed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                        # Update JSON with Whise metadata (alleen Supabase)
                        try:
                            supabase_update_contract_data(supabase_config, json_name, json_data)
                        except Exception as e:
                            logger.warning(f"Supabase update after Whise push failed: {e}")
                    else:
                        error_text = response.text
                        logger.warning(f"⚠️  Whise API returned error: {response.status_code} - {error_text}")
                        print(f"⚠️  Whise push failed: {response.status_code} - {error_text}")
                else:
                    logger.info(f"ℹ️  Whise API not configured (confidence: {conf['score']}%)")
                    print(f"ℹ️  Whise API niet geconfigureerd - contract is klaar voor push (confidence: {conf['score']}%)")
                    
            except ImportError:
                logger.warning("⚠️  'requests' library not installed. Install with: pip install requests")
                print("⚠️  'requests' library niet geïnstalleerd. Installeer met: pip install requests")
            except Exception as e:
                logger.warning(f"Failed to auto-push to Whise: {e}")
                print(f"⚠️  Auto-push naar Whise mislukt: {e}")

        # Log to CSV (Dropbox TARGET blijft voor CSV)
        log_to_csv(dbx_target, pdf_info['name'], result, json_name, "success")

        # Send email - alleen als verwerking succesvol was
        conf = result['confidence']

        if conf['score'] >= TARGET_CONFIDENCE and not conf['needs_review']:
            status_section = f"""STATUS
Confidence Score: {conf['score']}%
Assessment: ✅ Approved
Data Completeness: {conf['metrics'].get('completeness', 0):.0%}
"""
        else:
            status_section = f"""STATUS
Confidence Score: {conf['score']}%
Assessment: ⚠️ Review Required
Data Completeness: {conf['metrics'].get('completeness', 0):.0%}

ATTENTION POINTS
{conf['details']}
"""

        # Add key contract details
        normalized = result['normalized_data']
        details_section = ""

        if normalized.get('partijen'):
            verhuurder = normalized['partijen'].get('verhuurder', {}).get('naam', 'N/A')
            huurder = normalized['partijen'].get('huurder', {}).get('naam', 'N/A')
            details_section += f"""
PARTIES
Landlord: {verhuurder}
Tenant: {huurder}
"""

        if normalized.get('pand'):
            adres = normalized['pand'].get('adres', 'N/A')
            details_section += f"""
PROPERTY
Address: {adres}
"""

        if normalized.get('financieel'):
            huurprijs = normalized['financieel'].get('huurprijs')
            waarborg = normalized['financieel'].get('waarborg', {}).get('bedrag')
            if huurprijs or waarborg:
                details_section += f"""
FINANCIAL"""
                if huurprijs:
                    details_section += f"\nRent: €{huurprijs:.2f}/month"
                if waarborg:
                    details_section += f"\nDeposit: €{waarborg:.2f}"
                details_section += "\n"

        if normalized.get('periodes'):
            start = normalized['periodes'].get('ingangsdatum', 'N/A')
            duur = normalized['periodes'].get('duur', 'N/A')
            details_section += f"""
PERIOD
Start Date: {start}
Duration: {duur}
"""

        email_body = f"""Dear,

Please find attached the automated analysis of the following document:

DOCUMENT INFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Filename: {pdf_info['name']}
Type: {result['title']}
Size: {format_file_size(len(response.content))}
Processing Date: {datetime.now().strftime('%d-%m-%Y %H:%M')}

{status_section}
KEY DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{details_section}
SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{result['summary']}

STRUCTURED DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Full JSON available in Dropbox:
{json_file}

✅ JSON ready for import into your v0 website

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This analysis was automatically generated.
Please review the data for documents requiring review.

Best regards,
Automated Document Processing System
"""

        subject = f"Contract Processed: {result['title']} - {pdf_info['name']}"
        send_email(subject, email_body)

        if result['confidence']['needs_review']:
            print("⚠️  Review required")
        else:
            print("✅ Approved")

        return result

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Processing error: {error_msg}")
        
        # Check if it's an API key error
        if is_api_key_error(error_msg):
            print(f"   🔄 API Key error detected - requeuing document for retry")
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'api_key_error': True, 'requeue': True, 'error': error_msg}
        
        # Check if it's a quota error
        if is_quota_error(error_msg):
            print(f"   🔄 Quota error detected - requeuing document for retry")
            remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
            return {'quota_error': True, 'requeue': True, 'error': error_msg}
        
        # For other errors, also requeue (but log the error)
        print(f"   🔄 Unknown error - requeuing document for retry")
        remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
        return {'error': True, 'requeue': True, 'error_msg': error_msg}


# ============================================================================
# PHASE 2: ANALYZE RENTAL CONTRACTS
# ============================================================================

def analyze_rental_contracts(clients, analyzed_history):
    """Find and analyze all rental contracts"""

    dbx_analyze = clients['dbx_analyze']

    print(f"\n{'='*70}")
    print(f"🔍 PHASE 2: ANALYZING RENTAL CONTRACTS")
    print(f"{'='*70}")

    # DEBUG: Show what's in history
    print(f"📋 Analyzed history contains {len(analyzed_history)} entries")
    if analyzed_history:
        print(f"   Sample entries:")
        for path in list(analyzed_history)[:3]:
            print(f"   - {path}")

    # Find rental contract folders
    print("📁 Scanning for rental contract folders...")
    rental_folders = find_rental_contract_folders(dbx_analyze)

    if not rental_folders:
        print("ℹ️  No rental contract folders found")
        print(f"   Tip: Folders moeten keywords bevatten: {', '.join(RENTAL_KEYWORDS)}")
        return 0

    print(f"✓ Found {len(rental_folders)} rental folder(s):")
    for folder in rental_folders:
        print(f"   - {folder}")

    # Find PDFs in these folders
    print("\n📄 Finding PDFs in rental folders...")
    all_pdfs = find_pdfs_in_folders(dbx_analyze, rental_folders)

    if not all_pdfs:
        print("ℹ️  No PDFs found in rental folders")
        return 0

    print(f"✓ Found {len(all_pdfs)} PDF(s) in rental folders")

    # DEBUG: Show all PDF paths
    print(f"\n📝 All PDF paths found:")
    for pdf in all_pdfs[:5]:
        in_history = "✓ IN HISTORY" if pdf['path'] in analyzed_history else "✗ NEW"
        print(f"   {in_history}: {pdf['path']}")
    if len(all_pdfs) > 5:
        print(f"   ... and {len(all_pdfs) - 5} more")

    # Filter out already analyzed
    new_pdfs = [pdf for pdf in all_pdfs if pdf['path'] not in analyzed_history]

    if not new_pdfs:
        print("✅ All rental contracts already analyzed")
        print(f"   Total analyzed: {len(all_pdfs)}")
        return 0

    print(f"\n✓ Found {len(new_pdfs)} new contract(s) to analyze")
    print(f"   Already analyzed: {len(all_pdfs) - len(new_pdfs)}")

    # Process each contract
    analyzed_count = 0
    quota_hit = False

    for i, pdf_info in enumerate(new_pdfs, 1):
        print(f"\n📋 Contract {i}/{len(new_pdfs)}")
        print(f"   Path: {pdf_info['path']}")

        result = process_rental_contract(clients, pdf_info)

        # Check if document was requeued (removed from history)
        if result and result.get('requeue'):
            # Document was requeued, don't add to history set
            if result.get('api_key_error'):
                print(f"\n⚠️  API KEY ERROR - Document requeued: {pdf_info['name']}")
                print(f"   Will retry automatically when API key is fixed")
            elif result.get('quota_error'):
                print(f"\n⚠️  QUOTA EXCEEDED - Document requeued: {pdf_info['name']}")
                print(f"   Will retry automatically when quota resets")
            else:
                print(f"\n⚠️  ERROR - Document requeued: {pdf_info['name']}")
                print(f"   Will retry automatically in next cycle")
            # Continue with next document (don't break)
            continue
        
        # If document was successfully processed, update history set
        if result and result.get('success'):
            analyzed_history.add(pdf_info['path'])
            analyzed_count += 1
        elif result and result.get('quota_error') and not result.get('requeue'):
            # Old quota error handling (without requeue)
            quota_hit = True
            print(f"\n{'='*70}")
            print(f"⚠️  GEMINI API QUOTA REACHED")
            print(f"{'='*70}")
            print(f"Daily limit reached during analysis phase.")
            print(f"Analyzed {analyzed_count} contract(s) before quota limit.")
            print(f"Remaining contracts will be processed in next cycle.")
            print(f"{'='*70}")
            break
        else:
            # Document failed but wasn't requeued (shouldn't happen, but handle it)
            analyzed_history.add(pdf_info['path'])

        # Rate limiting - 5 RPM = min 12 seconden tussen contracten
        if i < len(new_pdfs) and not quota_hit:
            print(f"\n⏳ Waiting 15s before next contract (5 RPM limiet)...")
            time.sleep(15)  # Extra buffer voor 5 RPM limiet

    if analyzed_count > 0:
        print(f"\n✅ Successfully analyzed {analyzed_count} contract(s)")

    return analyzed_count


# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    """Main monitoring loop"""

    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║           SMART CONTRACT SYSTEM - ORGANIZE + ANALYZE                 ║
║                                                                      ║
║  PHASE 1: Auto-organize PDFs with OCR support                       ║
║  PHASE 2: Analyze rental contracts → JSON                           ║
║                                                                      ║
║  ✓ 3 Dropbox clients (organize/analyze/target)                       ║
║  ✓ 2 Gemini clients (organize/analyze)                               ║
║  ✓ Batch processing (max {BATCH_SIZE} per cycle)                     ║
║  ✓ Robust error handling                                             ║
╚══════════════════════════════════════════════════════════════════════╝
    """)

    # Initialize
    clients = init_clients()

    if not clients:
        print("❌ Cannot start - check credentials!")
        return

    folder_mgr = FolderManager(clients['dbx_organize'])

    # Load history
    organized_history = load_history(ORGANIZED_HISTORY)
    analyzed_history = load_history(ANALYZED_HISTORY)

    print(f"\n✓ Organized history: {len(organized_history)} file(s)")
    print(f"✓ Analyzed history: {len(analyzed_history)} contract(s)")
    print(f"\n{'='*70}")
    print(f"🚀 SYSTEM STARTED")
    print(f"{'='*70}\n")

    while True:
        try:
            # PHASE 1: Organize batch
            organized_count = organize_batch(clients, folder_mgr, organized_history, BATCH_SIZE)

            if organized_count > 0:
                print(f"\n✅ Organized {organized_count} document(s)")

                # Reload history
                organized_history = load_history(ORGANIZED_HISTORY)

            else:
                # PHASE 2: Analyze rental contracts (only if nothing to organize)
                analyzed_count = analyze_rental_contracts(clients, analyzed_history)

                if analyzed_count > 0:
                    print(f"\n✅ Analyzed {analyzed_count} rental contract(s)")

                    # Reload history
                    analyzed_history = load_history(ANALYZED_HISTORY)

                else:
                    ts = datetime.now().strftime('%H:%M:%S')
                    print(f"\n[{ts}] ✅ All organized & analyzed - waiting for new documents...",
                          end='\r', flush=True)

            print_pipeline_stats()
            # Wait before next cycle
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n⏹️  Stopped by user")
            break

        except Exception as e:
            print(f"\n❌ Unexpected error: {e}")
            print(f"⏳ Retrying in 60s...")
            time.sleep(60)


def backfill_document_text(dropbox_path: str) -> bool:
    """Eén PDF in document_texts zetten (voor docs die wel geparsed zijn maar niet in document_texts).
    Gebruik: python allesfocusophuur.py backfill-document-text \"/pad/naar/file.pdf\"
    """
    clients = init_clients()
    if not clients:
        print("❌ Geen clients (credentials?)")
        return False
    supabase_config = clients.get("supabase")
    if not supabase_config:
        print("❌ Geen Supabase config")
        return False
    path = dropbox_path.strip()
    if not path.startswith("/"):
        path = "/" + path
    name = os.path.basename(path)
    if not name.lower().endswith(".pdf"):
        print("❌ Geen PDF-pad")
        return False
    print(f"⬇️  Downloaden: {path}")
    try:
        _, response = clients["dbx_analyze"].files_download(path)
        pdf_bytes = response.content
    except Exception as e:
        print(f"❌ Dropbox download failed: {e}")
        return False
    print("📖 Tekst extraheren...")
    api_key, _ = get_next_extract_key()
    if not api_key:
        print("❌ Geen extract-key beschikbaar (12 keys op 18/24u)")
        return False
    gemini = genai.Client(api_key=api_key)
    full_text, _ = extract_text_with_ocr(pdf_bytes, gemini, clients["model_analyze"])
    if len(full_text.strip()) < 20:
        print("⚠️  Te weinig tekst geëxtraheerd")
        return False
    for attempt in (1, 2):
        try:
            supabase_upsert_document_text(supabase_config, path, name, full_text)
            print(f"✅ document_texts bijgewerkt: {name}")
            return True
        except Exception as e:
            if attempt == 2:
                print(f"❌ document_texts na 2 pogingen mislukt: {e}")
                return False
            print(f"⚠️  Poging {attempt} mislukt, retry...")
    return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "backfill-document-text":
        path_arg = sys.argv[2]
        ok = backfill_document_text(path_arg)
        sys.exit(0 if ok else 1)
    # Validate credentials before starting
    if not validate_credentials():
        logger.error("Cannot start: Missing required credentials. Exiting.")
        exit(1)
    
    logger.info("=" * 60)
    logger.info("🚀 Starting Smart Contract System (alexander)")
    logger.info("   document_texts: ACTIEF → zoeken + pdf-koppeling luik 4")
    logger.info("=" * 60)
    main()