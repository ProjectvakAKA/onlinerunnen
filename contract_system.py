"""
SMART CONTRACT SYSTEM - COMPLETE & FIXED
Fase 1: Organiseert PDFs automatisch met OCR support
Fase 2: Analyseert huurcontracten en genereert JSON


BELANGRIJK: Gebruik .env.local in de projectroot met alle credentials (zelfde als Next.js)
"""

from contextlib import contextmanager

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
import unicodedata
import signal
import logging
import functools
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from typing import Any, Dict, List, Optional, Set, Tuple, Callable
from pdf2image import convert_from_bytes
from PIL import Image
from dotenv import load_dotenv

try:
    import fcntl  # exclusieve lock: voorkomt dubbele analyse bij 2× `python contract_system.py`
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

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

# Shutdown-flag voor Ctrl+C: tijdens lange API-calls reageert Python pas als de call klaar is.
# Door een flag te zetten en in de loops te checken, stoppen we netjes na de huidige bewerking.
_shutdown_requested = [False]  # list zodat de signal handler kan muteren

def _sigint_handler(signum, frame):
    _shutdown_requested[0] = True
    print("\n⏹️  Stop aangevraagd (wacht tot huidige bewerking klaar is)...")

# Zorg dat prompts uit dezelfde map als dit script geïmporteerd kunnen worden
import sys
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
from prompts import (
    PROMPT_OCR_VISION,
    PROMPT_ORGANIZE,
    SOURCE_QUOTE_INSTRUCTION,
    WORD_IDS_INSTRUCTION,
    PROMPT_PARTIJEN,
    PROMPT_PAND,
    PROMPT_FINANCIEEL,
    PROMPT_PERIODES,
    PROMPT_VOORWAARDEN,
    PROMPT_JURIDISCH,
    PROMPT_METADATA,
    PROMPT_EPC_METADATA,
    PROMPT_EPC_GEBOUW,
    PROMPT_EPC_PRESTATIES,
    PROMPT_EPC_INSTALLATIES,
    PROMPT_EPC_AANBEVELINGEN,
    PROMPT_SUMMARY,
    EMAIL_SUBJECT,
    EMAIL_BODY,
    EMAIL_IMAGE_PATH,
)

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
    """Volgende key voor extractie (KEY_9..20). Max 18 calls per key per 24u. Teller wordt per API-call verhoogd via record_extract_use()."""
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
    return ek[best_i], best_i


def record_extract_use(key_idx: int) -> None:
    """+1 op de extract-teller voor de gegeven key (na elke Gemini API-call tijdens extractie)."""
    ok = _load_organize_keys()
    ek = _load_extract_keys()
    if key_idx < 0 or key_idx >= len(ek):
        return
    co, ce, next_reset_at = _load_rotator_state(ok, ek)
    now = time.time()
    if next_reset_at is None or now >= next_reset_at:
        co = [0] * len(ok)
        ce = [0] * len(ek)
        next_reset_at = now + GEMINI_ONE_DAY_SECONDS
    ce[key_idx] = ce[key_idx] + 1
    _save_rotator_state(co, ce, next_reset_at)

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
SUPABASE_URL = os.getenv('SUPABASE_URL') or os.getenv('NEXT_PUBLIC_SUPABASE_URL')
# Voorkeur voor SERVICE_ROLE_KEY (service_role), anders SERVICE_KEY — zo wint de juiste key als beide gezet zijn
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_SERVICE_KEY')

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
PHASE2_LOCK_PATH = os.path.join(_script_dir, ".phase2_analysis.lock")
FOLDER_CACHE = os.path.join(_script_dir, "folder_structure.json")

# CSV log in TARGET (v2 = nieuw format met extracted_* kolommen; wordt automatisch gebruikt als oude CSV nog bestaat)
CSV_LOG_PATH = "/verwerking_log.csv"
CSV_LOG_PATH_NEW = "/verwerking_log_v2.csv"
NEW_CSV_HEADER = (
    'timestamp,filename,document_type,confidence_score,text_length,fields_complete,'
    'source_quote_pct,extracted_huurprijs,extracted_adres,extracted_ingangsdatum,extracted_verhuurder,extracted_huurder,'
    'issues,warnings,json_path,processing_status\n'
)
# Let op: verwerking_log gebruikt geen needs_review meer; confidence_score blijft voor ordening.

# Retry settings
MAX_RETRIES = 3
RETRY_WAIT = 15
RATE_LIMIT_WAIT = 90
QUOTA_EXCEEDED_WAIT = 3600
MAX_KEY_SWITCH_RETRIES = 3
SERVICE_UNAVAILABLE_WAIT = 60
MODEL_FALLBACK_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-pro",
]

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
OCR_VISION_MIN_CHARS = 100  # Onder deze lengte: opnieuw Vision proberen
OCR_VISION_RETRIES = 3  # Aantal Vision-pogingen bij te weinig tekst
OCR_VISION_RETRY_WAIT = 10  # Seconden wachten tussen Vision-retries

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

    def _safe_get_value(self, obj: Any, *keys: str, default: Any = None) -> Any:
        """Like _safe_get but unwraps {value, source_quote, source_page} to the value string."""
        v = self._safe_get(obj, *keys, default=default)
        return _unwrap_field_value(v) if v is not None else default

    def _get_field_for_output(self, obj: Any, *keys: str, default: Any = None) -> Any:
        """Get field for output: if it's a dict with 'value' (and optionally source_quote/source_page), return that dict; else return the value."""
        v = self._safe_get(obj, *keys, default=default)
        if v is None or v == "":
            return default
        if isinstance(v, dict) and "value" in v:
            return v
        return v

    def _extract_number(self, value: Any) -> Optional[float]:
        value = _unwrap_field_value(value)
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

    def _number_field_for_output(self, obj: Any, *keys: str) -> Any:
        """Like _extract_number but preserves source_quote/source_page/word_ids when present (for PDF highlight + UI)."""
        v = self._safe_get(obj, *keys, default=None)
        if v is None or v == "":
            return None
        num = self._extract_number(v)
        has_source = (
            isinstance(v, dict)
            and "value" in v
            and (v.get("source_quote") is not None or v.get("source_page") is not None or v.get("word_ids") is not None)
        )
        if has_source:
            out = {"value": num, "source_quote": v.get("source_quote"), "source_page": v.get("source_page")}
            if v.get("word_ids") is not None:
                out["word_ids"] = v["word_ids"]
            return out
        return num

    def _text_passage_field_for_output(self, obj: Any, key: str) -> Optional[dict]:
        """Lange tekst + source_quote + word_ids (zelfde patroon als huisdieren_toelating)."""
        v = self._safe_get(obj, key, default=None)
        if not isinstance(v, dict) or "value" not in v:
            return None
        out: Dict[str, Any] = {}
        val = v.get("value")
        if isinstance(val, bool):
            if key == "indexatie":
                out["value"] = "Ja — indexatie van toepassing." if val else "Nee — geen indexatie."
            elif key == "onderverhuur":
                out["value"] = "Ja — onderverhuur is toegestaan." if val else "Nee — onderverhuur is niet toegestaan."
            else:
                out["value"] = "Ja." if val else "Nee."
        else:
            out["value"] = "" if val is None else str(val)
        if v.get("source_quote") is not None:
            out["source_quote"] = v["source_quote"]
        if v.get("source_page") is not None:
            out["source_page"] = v["source_page"]
        if v.get("word_ids") is not None:
            out["word_ids"] = v["word_ids"]
        return out

    def _legacy_gemeenschappelijke_kosten_string(self, financieel: dict) -> str:
        """Oude extracties: kosten-string of inbegrepen-lijst."""
        kosten_raw = self._safe_get_value(financieel, 'kosten')
        if kosten_raw is not None and kosten_raw != "":
            return str(kosten_raw)
        gem_kosten = self._safe_get(financieel, 'gemeenschappelijke_kosten', default={})
        if isinstance(gem_kosten, dict):
            inbegrepen_items = self._safe_get(gem_kosten, 'inbegrepen', default=[])
            if inbegrepen_items:
                items_text = []
                for item in inbegrepen_items:
                    if isinstance(item, dict):
                        post = self._safe_get(item, 'post')
                        if post:
                            items_text.append(str(post))
                if items_text:
                    return f"Gemeenschappelijke kosten ({', '.join(items_text)}) zijn inbegrepen in de huurprijs."
                return "Gemeenschappelijke kosten inbegrepen."
        return ""

    def _normalize_boolean(self, value: Any) -> Optional[bool]:
        value = _unwrap_field_value(value)
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
        value = _unwrap_field_value(value)
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
        ct = _unwrap_field_value(ct) if ct is not None else None
        return ct.lower() if (ct and isinstance(ct, str)) else 'huurovereenkomst'

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
                naam = self._safe_get_value(h, 'naam')
                if naam:
                    huurder_namen.append(naam)
                if not first_huurder:
                    first_huurder = h

        combined_naam = " & ".join(huurder_namen) if huurder_namen else ""

        return {
            "verhuurder": {
                "naam": self._get_field_for_output(verhuurder, 'naam') or "",
                "adres": self._get_field_for_output(verhuurder, 'adres') or self._get_field_for_output(verhuurder, 'zetel') or "",
                "telefoon": self._get_field_for_output(verhuurder, 'telefoon') or "",
                "email": self._get_field_for_output(verhuurder, 'email') or self._get_field_for_output(verhuurder, 'e-mail') or ""
            },
            "huurder": {
                "naam": combined_naam or "",
                "adres": self._get_field_for_output(first_huurder, 'adres') or self._get_field_for_output(first_huurder, 'woonplaats') or "",
                "telefoon": self._get_field_for_output(first_huurder, 'telefoon') or self._get_field_for_output(first_huurder, 'gsm') or "",
                "email": self._get_field_for_output(first_huurder, 'email') or self._get_field_for_output(first_huurder, 'e-mail') or ""
            }
        }

    def _normalize_pand_flat(self, data: dict) -> dict:
        """Returns flat pand"""
        pand = self._safe_get(data, 'pand') or self._safe_get(data, 'onderwerp') or {}

        # Extract address as simple string
        adres_raw = self._safe_get(pand, 'adres', default={})
        if isinstance(adres_raw, dict) and "value" in adres_raw and not any(k not in ("value", "source_quote", "source_page", "word_ids") for k in adres_raw):
            adres_str = str(adres_raw.get("value", ""))
        elif isinstance(adres_raw, dict):
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
        epc_geldig = self._normalize_date(self._safe_get_value(epc_data, 'geldig_tot'))

        # Kadaster
        kadaster = self._safe_get(pand, 'kadaster') or {}
        ki = self._extract_number(self._safe_get(kadaster, 'kadastraal_inkomen') or self._safe_get(kadaster, 'ki'))

        return {
            "adres": self._get_field_for_output(pand, 'adres') or adres_str,
            "type": self._get_field_for_output(pand, 'type') or self._get_field_for_output(pand, 'type_woning') or "appartement",
            "oppervlakte": self._extract_number(self._safe_get_value(pand, 'oppervlakte') or self._safe_get_value(pand, 'totale_bewoonbare_oppervlakte')),
            "aantal_kamers": self._number_field_for_output(pand, 'aantal_kamers') or self._number_field_for_output(pand, 'kamers'),
            "verdieping": self._number_field_for_output(pand, 'verdieping'),
            "epc": {
                "energielabel": self._get_field_for_output(epc_data, 'energielabel') or self._get_field_for_output(epc_data, 'label') or "",
                "certificaatnummer": self._get_field_for_output(epc_data, 'certificaatnummer') or self._get_field_for_output(epc_data, 'nummer') or "",
                "geldig_tot": epc_geldig,
                "bewoonbare_oppervlakte_epc": self._extract_number(self._safe_get_value(epc_data, 'bewoonbare_oppervlakte_epc')),
                "primair_energieverbruik": self._get_field_for_output(epc_data, 'primair_energieverbruik'),
                "referentiejaar": self._get_field_for_output(epc_data, 'referentiejaar'),
            },
            "kadaster": {
                "afdeling": self._get_field_for_output(kadaster, 'afdeling') or "",
                "sectie": self._get_field_for_output(kadaster, 'sectie') or "",
                "nummer": self._get_field_for_output(kadaster, 'nummer') or self._get_field_for_output(kadaster, 'perceelnummer') or "",
                "kadastraal_inkomen": ki,
                "gemeente_kadaster": self._get_field_for_output(kadaster, 'gemeente_kadaster') or "",
                "grondnummer": self._get_field_for_output(kadaster, 'grondnummer') or "",
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
            "status": self._get_field_for_output(asbest_data, 'status') or "",
            "datum_attest": self._normalize_date(self._safe_get_value(asbest_data, 'datum_attest')),
            "referentienummer": self._get_field_for_output(asbest_data, 'referentienummer') or "",
            "opmerking": self._get_field_for_output(asbest_data, 'opmerking') or "",
            "geldig_tot": self._normalize_date(self._safe_get_value(asbest_data, 'geldig_tot')),
        }

    def _normalize_financieel_flat(self, data: dict) -> dict:
        """Returns flat financieel"""
        financieel = self._safe_get(data, 'financieel', default={})

        # Huurprijs (bewaar source_quote/source_page voor PDF-markering en UI)
        huurprijs_raw = self._safe_get(financieel, 'huurprijs')
        if isinstance(huurprijs_raw, dict) and 'bedrag' in huurprijs_raw:
            huurprijs = self._number_field_for_output(huurprijs_raw, 'bedrag')
        else:
            huurprijs = self._number_field_for_output(financieel, 'huurprijs')
        if huurprijs is None:
            huurprijs = self._number_field_for_output(financieel, 'maandelijkse_huurprijs')

        # Waarborg
        waarborg_raw = self._safe_get(financieel, 'waarborg') or self._safe_get(financieel, 'huurwaarborg') or {}
        if isinstance(waarborg_raw, (int, float)):
            waarborg_raw = {"bedrag": waarborg_raw}

        waarborg_bedrag = self._number_field_for_output(waarborg_raw, 'bedrag')
        if waarborg_bedrag is None:
            waarborg_bedrag = self._extract_number(self._safe_get_value(waarborg_raw, 'bedrag'))

        # Build waar_gedeponeerd string
        waar_parts = []
        bank = self._safe_get_value(waarborg_raw, 'bank_naam') or self._safe_get_value(waarborg_raw, 'bank')
        iban = self._safe_get_value(waarborg_raw, 'iban')
        waar_gedeponeerd_raw = self._safe_get_value(waarborg_raw, 'waar_gedeponeerd')

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

        # Indexatie: rich passage + bron (fluo), fallback op boolean/string
        idx_raw = self._safe_get(financieel, 'indexatie')
        if isinstance(idx_raw, dict) and "value" in idx_raw:
            indexatie_out = self._text_passage_field_for_output(financieel, 'indexatie') or {
                "value": str(idx_raw.get("value", "")),
            }
        elif isinstance(idx_raw, bool):
            indexatie_out = {
                "value": ("Ja — indexatie van toepassing." if idx_raw else "Nee — geen indexatie."),
            }
        else:
            b = self._normalize_boolean(
                self._safe_get_value(financieel, 'indexatie')
                or self._safe_get_value(financieel, 'indexering')
            )
            if b is True:
                indexatie_out = {"value": "Ja — indexatie van toepassing (model)."}
            elif b is False:
                indexatie_out = {"value": "Nee — geen indexatie (model)."}
            else:
                indexatie_out = {"value": "Niet vermeld — geen indexatie-informatie."}

        # Gemeenschappelijke kosten: rich passage + bron (fluo)
        g_raw = self._safe_get(financieel, 'gemeenschappelijke_kosten')
        if isinstance(g_raw, dict) and g_raw.get("value") is not None:
            gemeenschappelijke_kosten_out = self._text_passage_field_for_output(
                financieel, 'gemeenschappelijke_kosten'
            ) or {"value": str(g_raw.get("value", ""))}
        else:
            legacy = self._legacy_gemeenschappelijke_kosten_string(financieel)
            gemeenschappelijke_kosten_out = {"value": legacy or "ONTBREKEND"}

        out: Dict[str, Any] = {
            "huurprijs": huurprijs,
            "waarborg": {
                "bedrag": waarborg_bedrag,
                "waar_gedeponeerd": self._get_field_for_output(waarborg_raw, 'waar_gedeponeerd') or waar_gedeponeerd,
            },
            "indexatie": indexatie_out,
            "gemeenschappelijke_kosten": gemeenschappelijke_kosten_out,
        }
        return out

    def _normalize_periodes_flat(self, data: dict) -> dict:
        """Returns flat periodes"""
        periodes = self._safe_get(data, 'periodes', default={})

        ingangsdatum = self._normalize_date(
            self._safe_get_value(periodes, 'ingangsdatum') or self._safe_get_value(periodes, 'aanvang') or self._safe_get_value(periodes, 'start')
        )
        einddatum = self._normalize_date(
            self._safe_get_value(periodes, 'einddatum') or self._safe_get_value(periodes, 'einde')
        )

        duur = self._get_field_for_output(periodes, 'duur') or self._get_field_for_output(periodes, 'contract_type_duur') or self._get_field_for_output(periodes, 'looptijd') or ""

        opzegtermijn_raw = self._safe_get_value(periodes, 'opzegtermijn')
        opzegtermijn_huurder = self._safe_get_value(periodes, 'opzegtermijn_huurder')
        opzegtermijn_verhuurder = self._safe_get_value(periodes, 'opzegtermijn_verhuurder')

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
            "opzegtermijn": self._get_field_for_output(periodes, 'opzegtermijn') or self._get_field_for_output(periodes, 'opzegtermijn_huurder') or self._get_field_for_output(periodes, 'opzegtermijn_verhuurder') or opzegtermijn
        }

    def _normalize_voorwaarden_flat(self, data: dict) -> dict:
        """Returns flat voorwaarden. huisdieren / onderverhuur / werken met source_quote + word_ids voor PDF-markering."""
        voorwaarden = self._safe_get(data, 'voorwaarden', default={})

        # Uitgebreide huisdierenbepaling (tekst + bron voor fluor)
        huisdieren_toelating = self._get_field_for_output(voorwaarden, 'huisdieren_toelating')
        if huisdieren_toelating is None:
            # Fallback: oude veld "huisdieren" (boolean) als string voor weergave
            huisdieren_raw = self._safe_get_value(voorwaarden, 'huisdieren')
            if isinstance(huisdieren_raw, dict):
                h = self._normalize_boolean(self._safe_get_value(huisdieren_raw, 'toegestaan'))
            else:
                h = self._normalize_boolean(huisdieren_raw)
            huisdieren_toelating = "Ja" if h is True else ("Nee" if h is False else "")

        # Onderverhuur: altijd object voor flatten + fluor (niet kale boolean)
        ov_raw = self._safe_get(voorwaarden, 'onderverhuur')
        if isinstance(ov_raw, dict) and ov_raw.get("value") is not None:
            onderverhuur_out = self._text_passage_field_for_output(voorwaarden, 'onderverhuur') or {
                "value": str(ov_raw.get("value", "")),
            }
        else:
            b = self._normalize_boolean(self._safe_get_value(voorwaarden, 'onderverhuur'))
            if b is True:
                onderverhuur_out = {"value": "Ja — onderverhuur is toegestaan (geen brontekst in extract)."}
            elif b is False:
                onderverhuur_out = {"value": "Nee — onderverhuur is niet toegestaan (geen brontekst in extract)."}
            else:
                onderverhuur_out = {"value": "ONTBREKEND"}

        # Werken: rich object of legacy string
        w_raw = self._safe_get(voorwaarden, 'werken')
        if isinstance(w_raw, dict) and w_raw.get("value") is not None:
            werken_out = self._text_passage_field_for_output(voorwaarden, 'werken') or {
                "value": str(w_raw.get("value", "")),
            }
        else:
            w_str = self._safe_get_value(voorwaarden, 'werken')
            werken_out = {"value": str(w_str)} if w_str not in (None, "") else {"value": "ONTBREKEND"}

        return {
            "huisdieren_toelating": huisdieren_toelating,
            "onderverhuur": onderverhuur_out,
            "werken": werken_out,
        }

    def _normalize_juridisch_flat(self, data: dict) -> dict:
        """Returns flat juridisch"""
        juridisch = self._safe_get(data, 'juridisch', default={})

        return {
            "toepasselijk_recht": self._get_field_for_output(juridisch, 'toepasselijk_recht') or "",
            "bevoegde_rechtbank": self._get_field_for_output(juridisch, 'bevoegde_rechtbank') or ""
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
    """Add file to history (pad genormaliseerd, één pad per regel). Idempotent: geen dubbele regels."""
    norm = _normalize_dropbox_path(path)
    if not norm or not _is_valid_history_line(norm):
        return
    try:
        existing = load_history(filename)
        if norm in existing:
            return
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{norm}\n")
    except Exception as e:
        print(f"⚠️  History update failed: {e}")


@contextmanager
def _phase2_analysis_lock():
    """Exclusieve lock rond claim op analyzed_docs (twee terminal-processen / dev:all)."""
    if not _HAS_FCNTL:
        yield
        return
    fp = open(PHASE2_LOCK_PATH, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def dedupe_history_file_on_disk(filename: str) -> None:
    """Verwijdert dubbele regels in een history-bestand (behoudt volgorde, één keer bij start)."""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    seen: Set[str] = set()
    out: List[str] = []
    changed = False
    for line in lines:
        if not line.strip() or not _is_valid_history_line(line):
            changed = True
            continue
        n = _normalize_dropbox_path(line)
        if n in seen:
            changed = True
            continue
        seen.add(n)
        out.append(f"{n}\n")
    if not changed:
        return
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.writelines(out)
        print(f"✓ History opgeschoond (dubbele regels verwijderd): {os.path.basename(filename)}")
    except Exception as e:
        print(f"⚠️  History-dedup mislukt ({filename}): {e}")


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


def send_email(subject, body, image_path=None):
    """
    Verstuur e-mail met optionele PNG-bijlage.
    image_path: pad naar een .png-bestand (bijv. logo); wordt als bijlage meegestuurd.
    """
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = ', '.join(RECIPIENT_EMAIL)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        if image_path and os.path.isfile(image_path):
            with open(image_path, 'rb') as f:
                img = MIMEImage(f.read(), _subtype='png')
            img.add_header('Content-Disposition', 'attachment', filename=os.path.basename(image_path))
            msg.attach(img)
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
            try:
                dbx_target.files_upload(NEW_CSV_HEADER.encode('utf-8'), CSV_LOG_PATH, mode=dropbox.files.WriteMode.overwrite)
                print("📊 CSV log created")
                return True
            except Exception as ex:
                print(f"❌ CSV creation error: {ex}")
                return False
        else:
            print(f"❌ CSV check error: {e}")
            return False


def _csv_cell(v):
    """One value for CSV; unwrap source objects to display value."""
    if v is None or v == "":
        return ""
    if isinstance(v, dict) and "value" in v and not any(k not in ("value", "source_quote", "source_page", "word_ids") for k in v):
        v = v.get("value")
    if v is None or v == "":
        return ""
    return str(v).strip()


# API performance log (lokaal, voor tracking Gemini-calls)
API_PERFORMANCE_LOG = os.path.join(_script_dir, "api_performance.csv")
API_PERFORMANCE_HEADER = (
    "timestamp_iso,operation,document_type,filename,duration_sec,success,error,model,key_index,extraction_method\n"
)


def log_api_performance(
    operation: str,
    filename: str,
    duration_sec: float,
    success: bool,
    document_type: Optional[str] = None,
    error: Optional[str] = None,
    model: Optional[str] = None,
    key_index: Optional[int] = None,
    extraction_method: Optional[str] = None,
) -> None:
    """Schrijf één regel naar api_performance.csv voor consistente API-performance tracking."""
    try:
        ts = datetime.now().isoformat()
        err = (error or "").replace('"', '""').strip()[:500]
        row = [
            ts,
            operation,
            document_type or "",
            filename,
            f"{duration_sec:.2f}",
            "1" if success else "0",
            err,
            model or "",
            str(key_index) if key_index is not None else "",
            extraction_method or "",
        ]
        line = ",".join('"' + str(f).replace('"', '""') + '"' for f in row) + "\n"
        file_exists = os.path.isfile(API_PERFORMANCE_LOG)
        with open(API_PERFORMANCE_LOG, "a", encoding="utf-8") as f:
            if not file_exists or os.path.getsize(API_PERFORMANCE_LOG) == 0:
                f.write(API_PERFORMANCE_HEADER)
            f.write(line)
    except Exception as e:
        logger.warning(f"API performance log write failed: {e}")


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

        # Gebruik nieuw bestand (v2) als de bestaande CSV het oude format heeft (geen source_quote_pct kolom)
        first_line = (current_csv.split('\n')[0] or '').strip()
        if 'source_quote_pct' not in first_line:
            csv_path = CSV_LOG_PATH_NEW
            try:
                _, response_v2 = dbx_target.files_download(csv_path)
                current_csv = response_v2.content.decode('utf-8')
            except Exception:
                print("📊 Bestaande CSV heeft oud format → log naar " + csv_path)
                current_csv = NEW_CSV_HEADER
        else:
            csv_path = CSV_LOG_PATH

        conf = result.get('confidence', {})
        metrics = conf.get('metrics', {})
        nd = result.get('normalized_data', {})
        financieel = nd.get('financieel') or {}
        partijen = nd.get('partijen') or {}
        pand = nd.get('pand') or {}
        periodes = nd.get('periodes') or {}
        verhuurder = partijen.get('verhuurder') or {}
        huurder = partijen.get('huurder') or {}
        if isinstance(huurder, list) and huurder:
            huurder = huurder[0] if isinstance(huurder[0], dict) else {}
        elif not isinstance(huurder, dict):
            huurder = {}

        new_row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            filename,
            result.get('title', 'Unknown'),
            str(conf.get('score', 0)),
            str(metrics.get('text_length', 0)),
            f"{metrics.get('completeness', 0):.0%}",
            f"{metrics.get('source_quote_pct', 0):.0%}",
            _csv_cell(financieel.get('huurprijs')),
            _csv_cell(pand.get('adres')),
            _csv_cell(periodes.get('ingangsdatum')),
            _csv_cell(verhuurder.get('naam')),
            _csv_cell(huurder.get('naam')),
            '; '.join(conf.get('issues', [])) or 'None',
            '; '.join(conf.get('warnings', [])) or 'None',
            json_path or '',
            status
        ]
        new_row = [f'"{str(field).replace('"', '""')}"' for field in new_row]
        new_line = ','.join(new_row) + '\n'
        updated_csv = current_csv.rstrip('\n') + '\n' + new_line
        dbx_target.files_upload(updated_csv.encode('utf-8'), csv_path, mode=dropbox.files.WriteMode.overwrite)
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
        _url = (os.getenv('SUPABASE_URL') or os.getenv('NEXT_PUBLIC_SUPABASE_URL') or SUPABASE_URL or '').strip().rstrip('/')
        _key = (os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_SERVICE_KEY') or SUPABASE_SERVICE_KEY or '').strip()
        if not _url or not _key:
            raise RuntimeError("SUPABASE_URL en SUPABASE_SERVICE_KEY (of NEXT_PUBLIC_SUPABASE_URL en SUPABASE_SERVICE_ROLE_KEY) ontbreken in .env.local. Zet ze in Supabase Dashboard → Settings → API.")
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
            if r.status_code == 401:
                raise RuntimeError(
                    "Supabase 401: verkeerde API-key. Gebruik de service_role key (Supabase → Settings → API → Project API keys → service_role), niet de anon key. Zet die in .env.local als SUPABASE_SERVICE_ROLE_KEY."
                )
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
    "ocr_vision": 0,  # Gemini Vision voor gescande PDFs
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
    total_docs = _pipeline_stats["text_layer"] + _pipeline_stats["ocr_vision"]
    if total_docs == 0:
        return
    print("\n📊 Pipeline stats (deze run)")
    print(f"   Tekst: {_pipeline_stats['text_layer']} tekstlaag, {_pipeline_stats['ocr_vision']} Gemini Vision (OCR)")
    total_stages = _pipeline_stats["contract_stages_rules"] + _pipeline_stats["contract_stages_ai"]
    if total_stages:
        pct_rules = 100 * _pipeline_stats["contract_stages_rules"] / total_stages
        print(f"   Contractvelden: {_pipeline_stats['contract_stages_rules']} via regels, {_pipeline_stats['contract_stages_ai']} via AI ({pct_rules:.0f}% regels)")


# ============================================================================
# PDF TEXT EXTRACTION WITH OCR (pdfplumber → Gemini Vision voor gescande PDFs)
# ============================================================================

def extract_text_with_ocr(pdf_bytes: bytes, gemini_client, model: str,
                          initial_pages: int = INITIAL_PAGES_TO_SCAN,
                          extract_key_idx: Optional[int] = None) -> Tuple[str, dict, List[Tuple[int, str]]]:
    """Extract text from PDF with OCR fallback for scanned documents. Returns (cleaned_text, metadata, pages_text)."""
    try:
        full_text = ""
        total_pages = 0
        pages_scanned = 0
        extraction_method = "text"

        # Phase 1: Try normal text extraction (per-page for source_quote highlighting)
        pages_text = []  # list of (1-based page number, page text)
        with io.BytesIO(pdf_bytes) as pdf_file:
            with pdfplumber.open(pdf_file) as pdf:
                total_pages = len(pdf.pages)
                pages_to_scan = min(initial_pages, total_pages)

                for i in range(pages_to_scan):
                    try:
                        page_text = pdf.pages[i].extract_text() or ""
                        full_text += page_text + "\n"
                        pages_text.append((i + 1, page_text))
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

                logger.info(f"   🔍 Running OCR on {len(image_data)} page(s) (Gemini Vision)...")

                ocr_text = ""
                if gemini_client and model:
                    for vision_attempt in range(OCR_VISION_RETRIES):
                        ocr_text = extract_text_vision(image_data, gemini_client, model, key_idx=extract_key_idx)
                        if ocr_text and len(ocr_text.strip()) >= OCR_VISION_MIN_CHARS:
                            cleaned_text = ocr_text
                            extraction_method = "ocr_vision"
                            if vision_attempt > 0:
                                logger.info(f"   ✓ Gemini Vision OCR (poging {vision_attempt + 1}): {len(cleaned_text)} chars")
                            else:
                                logger.info(f"   ✓ Gemini Vision OCR: {len(cleaned_text)} chars")
                            break
                        if vision_attempt < OCR_VISION_RETRIES - 1:
                            logger.warning(f"   ⚠️  Vision gaf te weinig tekst ({len(ocr_text or '')} chars) — opnieuw proberen over {OCR_VISION_RETRY_WAIT}s...")
                            time.sleep(OCR_VISION_RETRY_WAIT)
                    if not ocr_text or len(ocr_text.strip()) < OCR_VISION_MIN_CHARS:
                        logger.warning(f"   ⚠️  OCR na {OCR_VISION_RETRIES} pogingen nog te weinig tekst")
                else:
                    logger.warning(f"   ⚠️  Geen Gemini client/model — OCR overgeslagen")

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
                            pages_text.append((i + 1, page_text))
                            pages_scanned += 1

                            if i % 3 == 0:
                                temp = ' '.join(full_text.split())
                                if len(temp) > 1000:
                                    break
                        except Exception:
                            continue

            cleaned_text = ' '.join(full_text.split())

        if not pages_text and cleaned_text:
            pages_text = [(1, cleaned_text)]

        metadata = {
            'total_pages': total_pages,
            'pages_scanned': pages_scanned,
            'text_length': len(cleaned_text),
            'extraction_method': extraction_method,
            'ocr_engine': extraction_method if extraction_method == "ocr_vision" else "text_layer",
        }
        if extraction_method != "text" and cleaned_text:
            pages_text = [(1, cleaned_text)]
        return cleaned_text, metadata, pages_text

    except Exception as e:
        logger.error(f"⚠️  Extraction error: {e}", exc_info=True)
        return "", {'error': str(e)}, []
def extract_text_vision(images: List[bytes], gemini_client, model, key_idx: Optional[int] = None) -> str:
    """Extract text from images with Gemini Vision API"""
    try:
        # ✅ FIXED: Just use string directly, not wrapped in Part
        parts = [PROMPT_OCR_VISION]

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
                if key_idx is not None:
                    record_extract_use(key_idx)
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

    # EPC/EPB (energieprestatiecertificaat) hoort bij vastgoed/Contracten — niet naar Onderwijs
    epc_signals = ['epc', 'epb', 'energieprestatie', 'energiecertificaat', 'energieprestatiecertificaat']
    is_epc = any(ep in reasoning or ep in suggested or ep in name_lower for ep in epc_signals)
    if is_epc:
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

    prompt = PROMPT_ORGANIZE.format(
        filename=filename,
        current_location=current_location,
        pages_scanned=pdf_metadata.get('pages_scanned', '?'),
        total_pages=pdf_metadata.get('total_pages', '?'),
        method_note=method_note,
        existing_folders=existing_folders if existing_folders else "Geen mappen nog - eerste document.",
        text_for_call=text_for_call,
    )

    fallback_models = [m for m in MODEL_FALLBACK_CHAIN if isinstance(m, str) and m.strip() and m != model]
    model_candidates = [model] + fallback_models

    for model_idx, current_model in enumerate(model_candidates):
        if model_idx > 0:
            print(f"   🔄 Switching model after unavailable error: {current_model}")

        for attempt in range(MAX_RETRIES):
            try:
                response = gemini_client.models.generate_content(
                    model=current_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                        max_output_tokens=4096,
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
                print(f"   ⚠️  JSON parse error ({current_model}): {str(e)[:100]}")
                if attempt < MAX_RETRIES - 1:
                    print(f"   ⏳ Retry in {RETRY_WAIT}s...")
                    time.sleep(RETRY_WAIT)
                    continue
                break

            except Exception as e:
                error_str = str(e)
                error_lower = error_str.lower()

                if "429" in error_str or "quota" in error_lower or "resource_exhausted" in error_lower:
                    print(f"   ⚠️  Rate limit reached ({current_model})")
                    if attempt < MAX_RETRIES - 1:
                        wait_time = RATE_LIMIT_WAIT * (attempt + 1)
                        print(f"   ⏳ Waiting {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    break

                is_unavailable = (
                    "503" in error_str
                    or "service unavailable" in error_lower
                    or "currently experiencing high demand" in error_lower
                    or "unavailable" in error_lower
                )
                if is_unavailable and model_idx < len(model_candidates) - 1:
                    print(f"   ⚠️  {current_model} unavailable (503/high demand), trying fallback model...")
                    break

                print(f"   ❌ Classification error ({current_model}): {str(e)[:100]}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_WAIT)
                    continue
                break

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
            if _shutdown_requested[0]:
                print("\n⏹️  Stop aangevraagd - batch afgebroken.")
                break
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
                text, pdf_metadata, _ = extract_text_with_ocr(response.content, gemini, model)

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
                organize_start = time.time()
                result = smart_classify(text, pdf_info['name'], pdf_info['path'],
                                      folder_summary, gemini, model, pdf_metadata)
                organize_duration = time.time() - organize_start
                model_name = getattr(model, "name", str(model)) if model else ""

                if not result:
                    print(f"❌ Classification failed - document stays where it is")
                    log_api_performance("organize", pdf_info['name'], organize_duration, False, model=model_name, key_index=key_idx)
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
                    log_api_performance("organize", pdf_info['name'], organize_duration, True, model=model_name, key_index=key_idx)

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
# PHASE 2: CONTRACT FOLDER DETECTION (huur, EPC, asbest, kadaster, …)
# ============================================================================

def find_rental_contract_folders(dbx):
    """Find folders likely containing rental contracts (legacy: alleen huur-keywords)."""
    return _find_contract_folders_with_pdfs(dbx, keywords_only=True)


def find_contract_folders_with_pdfs(dbx):
    """Find ALL folders under /Contracten/ that contain at least one PDF (huur, EPC, asbest, kadaster, …)."""
    return _find_contract_folders_with_pdfs(dbx, keywords_only=False)


def _find_contract_folders_with_pdfs(dbx, keywords_only=False):
    """Internal: folders onder Organised die PDFs bevatten. keywords_only=True = alleen RENTAL_KEYWORDS (oude gedrag)."""
    contract_folders = set()

    try:
        try:
            dbx.files_get_metadata(ORGANIZED_FOLDER_PREFIX)
        except dropbox.exceptions.ApiError:
            print("⚠️  Organized folder niet gevonden")
            return []

        # Recursief onder Organized zoeken naar alle PDFs; parent-map van elke PDF onder /Contracten/ bewaren
        result = dbx.files_list_folder(ORGANIZED_FOLDER_PREFIX, recursive=True)
        contract_prefix = (ORGANIZED_FOLDER_PREFIX + "/Contracten").replace("//", "/")

        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata) and entry.name.lower().endswith(".pdf"):
                    path = entry.path_display
                    if "/Contracten/" not in path:
                        continue
                    parent = path.rsplit("/", 1)[0]
                    if not parent.startswith(contract_prefix):
                        continue
                    if keywords_only:
                        if any(kw in parent.lower() for kw in RENTAL_KEYWORDS):
                            contract_folders.add(parent)
                    else:
                        contract_folders.add(parent)

            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)

        for folder in sorted(contract_folders):
            print(f"   ✓ Contract folder: {folder}")

    except Exception as e:
        print(f"⚠️  Contract folder scan error: {e}")

    return sorted(contract_folders)


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


def _unwrap_field_value(v: Any) -> Any:
    """Get display value from a field that may be {value, source_quote, source_page} or a plain string."""
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _add_source_pages(data: Any, pages_text: List[Tuple[int, str]]) -> None:
    """Recursively add source_page to every dict that has source_quote by searching per-page text."""
    if not pages_text:
        return

    def normalize_ws(s: str) -> str:
        return " ".join((s or "").split()).strip()

    def find_page(quote: str) -> Optional[int]:
        if not quote or not quote.strip():
            return None
        q = normalize_ws(quote)
        if not q:
            return None
        for page_num, page_content in pages_text:
            if normalize_ws(page_content).find(q) >= 0:
                return page_num
        if len(q) > 20:
            for page_num, page_content in pages_text:
                if q[:20] in normalize_ws(page_content) or q[-20:] in normalize_ws(page_content):
                    return page_num
        return None

    if isinstance(data, dict):
        if "source_quote" in data and "source_page" not in data:
            data["source_page"] = find_page(str(data.get("source_quote", "")))
        for child in data.values():
            _add_source_pages(child, pages_text)
    elif isinstance(data, list):
        for item in data:
            _add_source_pages(item, pages_text)


def extract_words_with_ids(pdf_bytes: bytes) -> Tuple[Dict[int, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """
    Extract words with bbox from PDF using PyMuPDF. Returns (words_by_page, all_words).
    words_by_page: 1-based page num -> list of {id, text, page, x, y, width, height}.
    y is PDF coords (origin bottom-left) for frontend overlay.
    """
    words_by_page: Dict[int, List[Dict[str, Any]]] = {}
    all_words: List[Dict[str, Any]] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        global_id = 0
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_num = page_idx + 1
            rect = page.rect
            page_height = rect.height
            # get_text("words") returns list of (x0, y0, x1, y1, "word", block_no, line_no, word_no)
            word_list = page.get_text("words")
            page_words = []
            for w in word_list:
                x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
                word_str = (w[4] if len(w) > 4 else "").strip()
                if not word_str:
                    continue
                # PDF coords: y from bottom -> store y = page_height - y1 (bottom of word)
                width = x1 - x0
                height = y1 - y0
                entry = {
                    "id": global_id,
                    "text": word_str,
                    "page": page_num,
                    "x": round(x0, 2),
                    "y": round(page_height - y1, 2),
                    "width": round(width, 2),
                    "height": round(height, 2),
                }
                page_words.append(entry)
                all_words.append(entry)
                global_id += 1
            if page_words:
                words_by_page[page_num] = page_words
        doc.close()
    except Exception as e:
        logger.warning(f"Word extraction failed: {e}")
    return words_by_page, all_words


def build_numbered_prompt(all_words: List[Dict[str, Any]], max_chars: int) -> str:
    """Build '[0] word1 [1] word2 ...' until total length ~ max_chars."""
    parts = []
    total = 0
    for w in all_words:
        seg = f"[{w['id']}] {w['text']} "
        if total + len(seg) > max_chars:
            break
        parts.append(seg)
        total += len(seg)
    return "".join(parts).strip() if parts else ""


def _normalize_match_token(t: str) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = t.strip().lower()
    t = t.strip(".,;:!?\"'«»()[]")
    if len(t) > 1 and t.endswith(".") and t[:-1].isdigit():
        t = t[:-1]
    return t


def _tokenize_for_match(s: str) -> List[str]:
    if not s or not str(s).strip():
        return []
    parts = re.findall(r"[A-Za-zÀ-ÿ0-9]+(?:[.'-][A-Za-zÀ-ÿ0-9]+)?", str(s))
    return [x for x in (_normalize_match_token(p) for p in parts) if x]


_ONDERVERHUUR_KEYWORDS = frozenset(
    {
        "onderverhuur",
        "onderhuur",
        "tussenverhuur",
        "meeverhuur",
        "airbnb",
        "booking",
        "shortstay",
    }
)


def find_word_ids_for_source_quote(source_quote: str, all_words: List[Dict[str, Any]]) -> Optional[List[int]]:
    """
    Map source_quote to contiguous word IDs in all_words (volledige PDF, niet alleen het genummerde prompt-deel).
    Oplossing voor lange contracten waar build_numbered_prompt stopt vóór de clausule over onderverhuur.
    """
    if not source_quote or not all_words:
        return None
    q_tokens = _tokenize_for_match(source_quote)
    if not q_tokens:
        return None
    hay_tokens: List[str] = []
    hay_ids: List[int] = []
    for w in all_words:
        tid = w.get("id")
        if tid is None:
            continue
        nt = _normalize_match_token(str(w.get("text", "")))
        if not nt:
            continue
        hay_tokens.append(nt)
        hay_ids.append(int(tid))
    n, m = len(hay_tokens), len(q_tokens)
    if m > n or m == 0:
        return None
    for start in range(n - m + 1):
        if hay_tokens[start : start + m] == q_tokens:
            return hay_ids[start : start + m]
    for take in range(min(m, 18), 2, -1):
        tail = q_tokens[-take:]
        tl = len(tail)
        for start in range(n - tl + 1):
            if hay_tokens[start : start + tl] == tail:
                return hay_ids[start : start + tl]
    for kw in _ONDERVERHUUR_KEYWORDS:
        if kw not in q_tokens:
            continue
        idx = q_tokens.index(kw)
        mid = q_tokens[max(0, idx - 1) : idx + 10]
        ml = len(mid)
        if ml < 2:
            continue
        for start in range(n - ml + 1):
            if hay_tokens[start : start + ml] == mid:
                return hay_ids[start : start + ml]
    for i, ht in enumerate(hay_tokens):
        if ht in _ONDERVERHUUR_KEYWORDS or "airbnb" in ht:
            lo = max(0, i - 4)
            hi = min(n, i + 14)
            return hay_ids[lo:hi]
    return None


def _regex_find_onderverhuur_snippet(full_text: str) -> Optional[str]:
    """Haal een leesbare bronregel uit de volledige tekst (als het model geen source_quote gaf)."""
    if not full_text or len(full_text) < 20:
        return None
    for line in full_text.splitlines():
        t = line.strip()
        if len(t) < 12 or len(t) > 600:
            continue
        if re.search(
            r"onderverhuur|tussenverhuur|onder\s*[-]?\s*verhuur|airbnb|mee\s*verhuur|booking\.com",
            t,
            re.I,
        ):
            return t
    compact = re.sub(r"\s+", " ", full_text)
    m = re.search(
        r".{0,55}(?:\d+\.\s*)?(?:geen\s+)?(?:onderverhuur|tussenverhuur|onder\s*verhuur)[^.]{8,200}\.",
        compact,
        re.I,
    )
    if m:
        s = m.group(0).strip()
        if 12 <= len(s) <= 550:
            return s
    return None


def _enrich_word_ids_from_source_quotes(data: Any, all_words: Optional[List[Dict[str, Any]]]) -> None:
    """Vul ontbrekende word_ids af vanuit source_quote (alle bron-velden)."""
    if not all_words:
        return
    if isinstance(data, dict):
        keys = set(data.keys())
        if "value" in keys and keys <= {"value", "source_quote", "source_page", "word_ids"}:
            sq = data.get("source_quote")
            wids = data.get("word_ids")
            if sq and isinstance(sq, str) and sq.strip():
                if not wids or (isinstance(wids, list) and len(wids) == 0):
                    found = find_word_ids_for_source_quote(sq.strip(), all_words)
                    if found:
                        data["word_ids"] = found
            return
        for v in data.values():
            _enrich_word_ids_from_source_quotes(v, all_words)
    elif isinstance(data, list):
        for item in data:
            _enrich_word_ids_from_source_quotes(item, all_words)


def _ensure_onderverhuur_rich_field(
    voorwaarden: dict,
    full_text: str,
) -> None:
    """
    Boolean-only / ontbrekende source_quote → object met bronregel + (via all_words) word_ids.
    """
    if not isinstance(voorwaarden, dict):
        return
    ov = voorwaarden.get("onderverhuur")

    if ov is True or ov is False:
        snippet = _regex_find_onderverhuur_snippet(full_text)
        base = "Ja — onderverhuur is toegestaan." if ov else "Nee — onderverhuur is niet toegestaan."
        val = base
        if snippet:
            low = snippet.lower()
            if any(x in low for x in ("geen onderverhuur", "geen onder", "niet toegestaan", "verboden")):
                val = f"Nee — {snippet.strip()[:320]}"
            elif any(x in low for x in ("toegestaan", " mag ", "mogen")):
                val = f"Ja — {snippet.strip()[:320]}"
            else:
                val = f"{'Ja' if ov else 'Nee'} — {snippet.strip()[:320]}"
        voorwaarden["onderverhuur"] = {
            "value": val,
            "source_quote": (snippet or "").strip(),
        }
        return

    if not isinstance(ov, dict):
        return

    val = ov.get("value")
    if isinstance(val, bool):
        snippet = ov.get("source_quote") if isinstance(ov.get("source_quote"), str) else None
        if not (snippet and snippet.strip()):
            snippet = _regex_find_onderverhuur_snippet(full_text) or ""
        text_val = (
            f"{'Ja' if val else 'Nee'} — {snippet.strip()[:320]}"
            if snippet and snippet.strip()
            else ("Ja — onderverhuur is toegestaan." if val else "Nee — onderverhuur is niet toegestaan.")
        )
        ov["value"] = text_val
        if snippet and snippet.strip():
            ov["source_quote"] = snippet.strip()
        elif "source_quote" in ov and not str(ov.get("source_quote") or "").strip():
            ov.pop("source_quote", None)
        return

    sq = ov.get("source_quote")
    if not (isinstance(sq, str) and sq.strip()):
        snippet = _regex_find_onderverhuur_snippet(full_text)
        if snippet:
            ov["source_quote"] = snippet


def get_document_type_from_path_and_name(path: str, name: str) -> str:
    """Bepaal documenttype uit pad en bestandsnaam (oude structuur /Contracten/Type/Adres of nieuwe /Contracten/Provincie/Adres met Adres_Type.pdf)."""
    path_upper = (path or "").upper().replace("\\", "/")
    name_upper = (name or "").upper()
    if "/EPC/" in path_upper or path_upper.rstrip("/").endswith("/EPC"):
        return "epc"
    if "_EPC." in name_upper or name_upper.endswith("_EPC.PDF") or name_upper.endswith("EPC.PDF"):
        return "epc"
    if "/ASBEST/" in path_upper or path_upper.rstrip("/").endswith("/ASBEST"):
        return "asbest"
    if "_ASBEST." in name_upper or "ASBEST." in name_upper:
        return "asbest"
    if "/KADASTER/" in path_upper or "/EIGENDOMSTITEL/" in path_upper:
        return "kadaster"
    if "_KADASTER." in name_upper or "_EIGENDOMSTITEL." in name_upper:
        return "kadaster"
    return "huurcontract"


# EPC-schema v2: metadata, gebouw, prestaties, installaties, aanbevelingen (zie prompts.py)


def _is_epc_source_leaf(v: Any) -> bool:
    if not isinstance(v, dict) or "value" not in v:
        return False
    return all(k in ("value", "source_quote", "source_page", "word_ids") for k in v.keys())


def _epc_coerce_int(value: Any) -> Optional[int]:
    if _is_epc_source_leaf(value):
        value = value.get("value")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        s = value.strip().replace(",", ".").replace(" ", "")
        if not s or s.lower() in ("n/a", "ontbreekt", "-"):
            return None
        try:
            f = float(s)
            return int(round(f))
        except (ValueError, TypeError):
            return None
    return None


def _epc_coerce_int_leaf(v: Any) -> Any:
    """Integer of bronobject met genormaliseerde value."""
    if _is_epc_source_leaf(v):
        inner = _epc_coerce_int(v.get("value"))
        return {**v, "value": inner}
    return _epc_coerce_int(v)


def _epc_str_leaf(v: Any) -> Any:
    """String of bronobject met getrimde value."""
    if _is_epc_source_leaf(v):
        inner = v.get("value")
        if inner is None:
            return {**v, "value": None}
        s = str(inner).strip() or None
        return {**v, "value": s}
    if isinstance(v, str):
        return v.strip() or None
    return v


def _epc_normalize_dd_mm_yyyy(value: Any) -> Optional[str]:
    """Datums naar DD/MM/YYYY (string) of None."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("n/a", "ontbreekt"):
            return None
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
    return None


def try_regex_epc_fields(full_text: str) -> Dict[str, Any]:
    """Optionele regex-hints (certificaatnummer, datums) — merge na AI indien AI null gaf."""
    hints: Dict[str, Any] = {"metadata": {}}
    if not full_text or len(full_text) < 30:
        return {}
    text = full_text.replace("\n", " ")
    m = re.search(
        r"(?:certificaat|attest|identificatie)(?:nummer)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/_]{4,})",
        text,
        re.I,
    )
    if m:
        hints["metadata"]["certificaat_nummer"] = m.group(1).strip()
    for label, key in (
        (r"geldig\s*(?:tot|tem|tot en met)\s*[:#]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", "geldig_tot"),
        (r"(?:datum\s*)?(?:opmaak|uitgifte)\s*[:#]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", "datum_opmaak"),
    ):
        mm = re.search(label, text, re.I)
        if mm:
            norm = _epc_normalize_dd_mm_yyyy(mm.group(1))
            if norm:
                hints["metadata"][key] = norm
    return {k: v for k, v in hints.items() if v}


def _merge_epc_regex_metadata(meta: Dict[str, Any], hints: Dict[str, Any]) -> None:
    """Vul ontbrekende metadata-velden met regex-hints."""
    if not hints:
        return
    for k, v in hints.items():
        if meta.get(k) in (None, "", []):
            meta[k] = v


def normalize_epc_data(epc_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliseer genest EPC-schema: datums DD/MM/YYYY, integers, strings; behoud bronobjecten {value, source_quote, ...}."""
    if not epc_data or epc_data.get("error"):
        return epc_data

    def norm_meta(m: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in ("certificaat_nummer", "datum_opmaak", "geldig_tot", "energiedeskundige"):
            v = m.get(k)
            if k in ("datum_opmaak", "geldig_tot"):
                if _is_epc_source_leaf(v):
                    inner = _epc_normalize_dd_mm_yyyy(v.get("value"))
                    out[k] = {**v, "value": inner}
                else:
                    out[k] = _epc_normalize_dd_mm_yyyy(v)
            else:
                out[k] = _epc_str_leaf(v)
        return out

    def norm_gebouw(g: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "adres": _epc_str_leaf(g.get("adres")),
            "referentie": _epc_str_leaf(g.get("referentie")),
            "bouwjaar": _epc_coerce_int_leaf(g.get("bouwjaar")),
            "oppervlakte_m2": _epc_coerce_int_leaf(g.get("oppervlakte_m2")),
            "volume_m3": _epc_coerce_int_leaf(g.get("volume_m3")),
        }

    def norm_pres(p: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "energiescore_kwh_m2",
            "doelstelling_kwh_m2",
            "primair_verbruik_kwh",
            "co2_emissie_kg",
            "s_peil",
        )
        return {k: _epc_coerce_int_leaf(p.get(k)) for k in keys}

    def norm_inst(i: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in ("verwarming", "sanitair_warm_water", "zonne_energie", "ventilatie"):
            out[k] = _epc_str_leaf(i.get(k))
        return out

    def norm_aanbevelingen(arr: Any) -> list:
        if not isinstance(arr, list):
            return []
        out_list = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            out_list.append({
                "onderdeel": _epc_str_leaf(item.get("onderdeel")),
                "actie": _epc_str_leaf(item.get("actie")),
                "prijs_min_eur": _epc_coerce_int_leaf(item.get("prijs_min_eur")),
                "prijs_max_eur": _epc_coerce_int_leaf(item.get("prijs_max_eur")),
            })
        return out_list

    meta = norm_meta(epc_data.get("metadata") or {})
    gebouw = norm_gebouw(epc_data.get("gebouw") or {})
    prestaties = norm_pres(epc_data.get("prestaties") or {})
    installaties = norm_inst(epc_data.get("installaties") or {})
    aanbevelingen = norm_aanbevelingen(epc_data.get("aanbevelingen"))

    return {
        "metadata": meta,
        "gebouw": gebouw,
        "prestaties": prestaties,
        "installaties": installaties,
        "aanbevelingen": aanbevelingen,
    }


def _epc_count_filled(obj: Any) -> Tuple[int, int]:
    """Tel (gevuld, totaal) recursief voor confidence."""
    filled, total = 0, 0
    if obj is None:
        return 0, 0
    if isinstance(obj, dict):
        if "value" in obj and not any(
            k not in ("value", "source_quote", "source_page", "word_ids") for k in obj.keys()
        ):
            total += 1
            w = obj.get("value")
            if w is not None and w != "" and w != []:
                filled += 1
            return filled, total
        for v in obj.values():
            f, t = _epc_count_filled(v)
            filled += f
            total += t
        return filled, total
    if isinstance(obj, list):
        for it in obj:
            f, t = _epc_count_filled(it)
            filled += f
            total += t
        return filled, total
    total = 1
    if obj != "" and obj != []:
        filled = 1
    return filled, total


def calculate_confidence_epc(epc_data: Dict[str, Any]) -> Dict[str, Any]:
    """Confidence voor EPC v2: kritieke velden + volledigheid."""
    meta = epc_data.get("metadata") or {}
    gebouw = epc_data.get("gebouw") or {}
    critical = {
        "certificaat_nummer": _unwrap_field_value(meta.get("certificaat_nummer")),
        "adres": _unwrap_field_value(gebouw.get("adres")),
        "geldig_tot": _unwrap_field_value(meta.get("geldig_tot")),
        "oppervlakte_m2": _unwrap_field_value(gebouw.get("oppervlakte_m2")),
    }
    filled_critical = sum(1 for v in critical.values() if v is not None and v != "")
    critical_score = (filled_critical / len(critical)) * 50
    filled, total = _epc_count_filled(epc_data)
    completeness = (filled / total * 50) if total else 0
    score = min(100, round(critical_score + completeness))
    details: Dict[str, Any] = {}
    if filled_critical < len(critical):
        details["missing_critical"] = [k for k, v in critical.items() if not v and v != 0]
    return {"score": score, "details": details}


def extract_epc_data(
    full_text: str,
    gemini_client,
    model,
    extract_key_idx: Optional[int] = None,
    all_words: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Multi-stage EPC-extractie (Vlaams schema): metadata, gebouw, prestaties, installaties, aanbevelingen.
    Zelfde patroon als huurcontract: meerdere Gemini-calls + optionele word_ids.
    """
    regex_hints = try_regex_epc_fields(full_text)
    if regex_hints:
        print(f"   📐 EPC regex-hints: {list(regex_hints.keys())}")

    text_chunk_1 = full_text[:TEXT_CHUNK_1_SIZE]
    text_chunk_2 = full_text[TEXT_CHUNK_OVERLAP:TEXT_CHUNK_2_SIZE] if len(full_text) > TEXT_CHUNK_OVERLAP else ""
    _ctx2_6k = text_chunk_2[:6000] if text_chunk_2 else ""

    numbered_suffix = ""
    if all_words and len(all_words) > 0:
        numbered_chunk = build_numbered_prompt(all_words, TEXT_CHUNK_1_SIZE)
        if numbered_chunk:
            numbered_suffix = "\n\n" + WORD_IDS_INSTRUCTION + "\n\nGENUMBERDE TEKST (gebruik de [getal]-IDs voor word_ids):\n" + numbered_chunk
            print(f"   📌 EPC Word IDs: {len(all_words)} woorden voor markering")

    _sq = SOURCE_QUOTE_INSTRUCTION + numbered_suffix

    stages = {
        "metadata": PROMPT_EPC_METADATA.format(
            text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq
        ),
        "gebouw": PROMPT_EPC_GEBOUW.format(
            text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq
        ),
        "prestaties": PROMPT_EPC_PRESTATIES.format(
            text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq
        ),
        "installaties": PROMPT_EPC_INSTALLATIES.format(
            text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq
        ),
        "aanbevelingen": PROMPT_EPC_AANBEVELINGEN.format(
            text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq
        ),
    }

    extracted: Dict[str, Any] = {}

    fallback_models = [m for m in MODEL_FALLBACK_CHAIN if isinstance(m, str) and m.strip() and m != model]
    model_candidates = [model] + fallback_models

    for stage_name, prompt in stages.items():
        print(f"      📊 EPC extracting {stage_name}...")
        stage_data: Dict[str, Any] = {}
        stage_done = False
        for model_idx, current_model in enumerate(model_candidates):
            if model_idx > 0:
                print(f"         🔄 EPC fallback model: {current_model}")
            attempt = 0
            while attempt < 3:
                try:
                    response = gemini_client.models.generate_content(
                        model=current_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            response_mime_type="application/json",
                        ),
                    )
                    raw = response.text.strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        raw = raw.strip()
                    stage_data = json.loads(raw)
                    if extract_key_idx is not None:
                        record_extract_use(extract_key_idx)
                    stage_done = True
                    break
                except json.JSONDecodeError:
                    attempt += 1
                    print(f"         ⚠️  EPC JSON parse error ({stage_name}) attempt {attempt}/3")
                    if attempt < 3:
                        time.sleep(3)
                    else:
                        stage_data = {}
                except Exception as e:
                    err = str(e)
                    err_l = err.lower()
                    if is_quota_error(err):
                        return {"error": "QUOTA_EXCEEDED", "details": err}
                    if is_api_key_error(err):
                        return {"error": "API_KEY_ERROR", "details": err}
                    if "429" in err and attempt < 2:
                        attempt += 1
                        print("         ⚠️  Rate limit — waiting...")
                        time.sleep(RETRY_WAIT)
                        continue
                    is_unavailable = (
                        "503" in err
                        or "service unavailable" in err_l
                        or "currently experiencing high demand" in err_l
                        or "unavailable" in err_l
                    )
                    if is_unavailable:
                        print(f"         ⚠️  Model unavailable (503/high demand), waiting {SERVICE_UNAVAILABLE_WAIT}s...")
                        time.sleep(SERVICE_UNAVAILABLE_WAIT)
                        # Tijdelijke overbelasting: blijf wachten/retryen in deze stage.
                        continue
                    print(f"         ❌ EPC {stage_name}: {str(e)[:120]}")
                    stage_data = {}
                    break
            if stage_done:
                break
        if not stage_done:
            return {
                "error": "STAGE_FAILED",
                "stage": stage_name,
                "details": f"EPC stage '{stage_name}' kon niet succesvol worden afgerond"
            }

        if stage_name == "aanbevelingen":
            arr = stage_data.get("aanbevelingen")
            extracted["aanbevelingen"] = arr if isinstance(arr, list) else []
        else:
            extracted[stage_name] = stage_data

        time.sleep(12)

    merged = {
        "metadata": extracted.get("metadata") or {},
        "gebouw": extracted.get("gebouw") or {},
        "prestaties": extracted.get("prestaties") or {},
        "installaties": extracted.get("installaties") or {},
        "aanbevelingen": extracted.get("aanbevelingen") or [],
    }
    _merge_epc_regex_metadata(merged["metadata"], regex_hints.get("metadata") or {})

    return merged


def extract_contract_data(full_text, gemini_client, model, initial_data: Optional[Dict[str, dict]] = None, pages_text: Optional[List[Tuple[int, str]]] = None, extract_key_idx: Optional[int] = None, all_words: Optional[List[Dict[str, Any]]] = None, words_by_page: Optional[Dict[int, List[Dict[str, Any]]]] = None):
    """
    Multi-stage huurcontract extractor. Stap 4+5: eerst regels (initial_data), dan AI voor ontbrekende secties.
    pages_text: optional list of (1-based page number, page text) for adding source_page to source_quote fields.
    all_words/words_by_page: optional word-level data for numbered prompt and word_ids in response.
    """
    if initial_data is None:
        initial_data = try_regex_contract_fields(full_text)
        if initial_data:
            print(f"   📐 Regels/regex: {list(initial_data.keys())} (minder API-calls)")

    # Split text in chunks voor betere extractie
    text_chunk_1 = full_text[:TEXT_CHUNK_1_SIZE]
    text_chunk_2 = full_text[TEXT_CHUNK_OVERLAP:TEXT_CHUNK_2_SIZE] if len(full_text) > TEXT_CHUNK_OVERLAP else ""

    # Genummerde tekst voor word_ids (exacte PDF-markering)
    numbered_suffix = ""
    if all_words and len(all_words) > 0:
        numbered_chunk = build_numbered_prompt(all_words, TEXT_CHUNK_1_SIZE)
        if numbered_chunk:
            numbered_suffix = "\n\n" + WORD_IDS_INSTRUCTION + "\n\nGENUMBERDE TEKST (gebruik de [getal]-IDs voor word_ids):\n" + numbered_chunk
            print(f"   📌 Word IDs: {len(all_words)} woorden in prompt voor exacte markering")

    print("   🎯 Starting extraction (regels + AI voor ontbrekende velden)...")

    extracted_sections = {}

    _sq = SOURCE_QUOTE_INSTRUCTION + numbered_suffix
    _ctx2_6k = text_chunk_2[:6000] if text_chunk_2 else ""
    _ctx2_5k = text_chunk_2[:5000] if text_chunk_2 else ""

    partijen_prompt = PROMPT_PARTIJEN.format(text_chunk_1=text_chunk_1, source_quote_instruction=_sq)
    pand_prompt = PROMPT_PAND.format(text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_6k, source_quote_instruction=_sq)
    financieel_prompt = PROMPT_FINANCIEEL.format(text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_5k, source_quote_instruction=_sq)
    periodes_prompt = PROMPT_PERIODES.format(text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_5k, source_quote_instruction=_sq)
    voorwaarden_prompt = PROMPT_VOORWAARDEN.format(text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_5k, source_quote_instruction=_sq)
    juridisch_prompt = PROMPT_JURIDISCH.format(text_chunk_1=text_chunk_1, text_chunk_2=_ctx2_5k, source_quote_instruction=_sq)
    metadata_prompt = PROMPT_METADATA.format(text_chunk_1=text_chunk_1[:5000], source_quote_instruction=_sq)

    stages = {
        "metadata": metadata_prompt,
        "partijen": partijen_prompt,
        "pand": pand_prompt,
        "financieel": financieel_prompt,
        "periodes": periodes_prompt,
        "voorwaarden": voorwaarden_prompt,
        "juridisch": juridisch_prompt
    }

    # Prompts staan in prompts.py
    fallback_models = [m for m in MODEL_FALLBACK_CHAIN if isinstance(m, str) and m.strip() and m != model]
    model_candidates = [model] + fallback_models

    for stage_name, prompt in stages.items():
        print(f"      📊 Extracting {stage_name}...")
        # Altijd AI-extractie per stage (geen skip op basis van regex), zodat we alle velden krijgen.
        # initial_data van regex wordt niet meer gebruikt om stages over te slaan.
        stage_done = False
        for model_idx, current_model in enumerate(model_candidates):
            if model_idx > 0:
                print(f"         🔄 Fallback model: {current_model}")
            attempt = 0
            while attempt < 3:
                try:
                    response = gemini_client.models.generate_content(
                        model=current_model,
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
                    if extract_key_idx is not None:
                        record_extract_use(extract_key_idx)
                    stage_done = True
                    break

                except json.JSONDecodeError as e:
                    attempt += 1
                    print(f"         ⚠️  JSON parse error (attempt {attempt}/3)")
                    if attempt < 3:
                        time.sleep(3)
                    else:
                        print(f"         ❌ Failed to extract {stage_name}")
                        extracted_sections[stage_name] = {}

                except Exception as e:
                    error_msg = str(e)
                    error_lower = error_msg.lower()
                    if is_quota_error(error_msg):
                        return {"error": "QUOTA_EXCEEDED", "details": error_msg}

                    if is_api_key_error(error_msg):
                        return {"error": "API_KEY_ERROR", "details": error_msg}

                    if "429" in error_msg and attempt < 2:
                        attempt += 1
                        print(f"         ⚠️  Rate limit - waiting...")
                        time.sleep(RETRY_WAIT)
                        continue

                    is_unavailable = (
                        "503" in error_msg
                        or "service unavailable" in error_lower
                        or "currently experiencing high demand" in error_lower
                        or "unavailable" in error_lower
                    )
                    if is_unavailable:
                        print(f"         ⚠️  Model unavailable (503/high demand), waiting {SERVICE_UNAVAILABLE_WAIT}s...")
                        time.sleep(SERVICE_UNAVAILABLE_WAIT)
                        # Tijdelijke overbelasting: blijf wachten/retryen in deze stage.
                        continue

                    print(f"         ❌ Error: {str(e)[:100]}")
                    extracted_sections[stage_name] = {}
                    break
            if stage_done:
                break
        if not stage_done:
            return {
                "error": "STAGE_FAILED",
                "stage": stage_name,
                "details": f"Contract stage '{stage_name}' kon niet succesvol worden afgerond"
            }

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

    if pages_text:
        _add_source_pages(final_data, pages_text)

    # PDF-fluor: genummerde prompt dekt niet altijd het hele document; vul word_ids uit source_quote + all_words.
    # Onderverhuur: regex-bron als het model boolean-only gaf.
    if all_words:
        _ensure_onderverhuur_rich_field(final_data.get("voorwaarden") or {}, full_text)
        _enrich_word_ids_from_source_quotes(final_data, all_words)

    # ========================================================================
    # VALIDATION & REPORTING
    # ========================================================================

    print("\n      📋 EXTRACTION SUMMARY:")

    def _get_leaf(obj: dict, *keys: str) -> Any:
        for k in keys:
            obj = obj.get(k) if isinstance(obj, dict) else None
            if obj is None:
                return None
        return obj

    critical_fields = {
        "Huurprijs": _unwrap_field_value(_get_leaf(final_data, "financieel", "huurprijs")),
        "Ingangsdatum": _unwrap_field_value(_get_leaf(final_data, "periodes", "ingangsdatum")),
        "Verhuurder naam": _unwrap_field_value(_get_leaf(final_data, "partijen", "verhuurder", "naam")),
        "Huurder naam": _unwrap_field_value(_get_leaf(final_data, "partijen", "huurder", "naam")),
        "Pand adres": _unwrap_field_value(_get_leaf(final_data, "pand", "adres"))
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
            if "value" in obj and not any(k not in ("value", "source_quote", "source_page", "word_ids") for k in obj):
                total_fields += 1
                w = obj.get("value")
                if w is not None and w != "" and w != []:
                    filled_fields += 1
            else:
                for v in obj.values():
                    count_fields(v)
        elif isinstance(obj, list):
            for item in obj:
                count_fields(item)
        else:
            total_fields += 1
            if obj is not None and obj != "" and obj != []:
                filled_fields += 1

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
        'huurprijs': _unwrap_field_value(normalized_data.get('financieel', {}).get('huurprijs')),
        'ingangsdatum': _unwrap_field_value(normalized_data.get('periodes', {}).get('ingangsdatum')),
        'verhuurder_naam': _unwrap_field_value(normalized_data.get('partijen', {}).get('verhuurder', {}).get('naam')),
        'huurder_naam': _unwrap_field_value(normalized_data.get('partijen', {}).get('huurder', {}).get('naam')),
        'pand_adres': _unwrap_field_value(normalized_data.get('pand', {}).get('adres'))
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
            if "value" in obj and not any(k not in ("value", "source_quote", "source_page", "word_ids") for k in obj):
                total += 1
                v = obj.get("value")
                if v is not None and v != "" and v != [] and v != {}:
                    filled += 1
                return total, filled
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

    # Source-quote coverage: velden met bronvermelding (proxy voor controleerbaarheid)
    def count_source_quotes(obj):
        total_leaf = 0
        with_quote = 0
        if isinstance(obj, dict):
            if "value" in obj and not any(k not in ("value", "source_quote", "source_page", "word_ids") for k in obj):
                total_leaf += 1
                if obj.get("source_quote"):
                    with_quote += 1
                return total_leaf, with_quote
            for k, v in obj.items():
                t, w = count_source_quotes(v)
                total_leaf += t
                with_quote += w
        elif isinstance(obj, list):
            for item in obj:
                t, w = count_source_quotes(item)
                total_leaf += t
                with_quote += w
        return total_leaf, with_quote

    fields_with_value, fields_with_source_quote = count_source_quotes(normalized_data)
    source_quote_pct = (fields_with_source_quote / fields_with_value) if fields_with_value > 0 else 0.0

    # Text quality (10 points max)
    text_length = len(full_text.strip())
    if text_length < 500:
        warnings.append(f"Short document: {text_length} characters")
        score -= 5
    elif text_length > 2000:
        score += 10
    else:
        score += 5

    # Normalize score (voor ordening; geen drempel meer)
    score = max(0, min(100, round(score, 1)))

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
        'issues': issues,
        'warnings': warnings,
        'details': "\n\n".join(details_parts),
        'metrics': {
            'text_length': text_length,
            'completeness': completeness,
            'critical_fields_found': found_critical,
            'critical_fields_total': len(critical_fields),
            'source_quote_pct': source_quote_pct,
            'fields_with_source_quote': fields_with_source_quote,
            'fields_with_value': fields_with_value,
        }
    }


def generate_summary(full_text: str, doc_type: str, gemini_client, model, extract_key_idx: Optional[int] = None) -> str:
    """Generate natural language summary using Gemini"""

    # Use first N chars for summary
    text_sample = full_text[:SUMMARY_TEXT_SIZE] if len(full_text) > SUMMARY_TEXT_SIZE else full_text

    prompt = PROMPT_SUMMARY.format(doc_type=doc_type, text_sample=text_sample)

    try:
        for attempt in range(MAX_RETRIES):
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=3000,
                    )
                )

                summary = response.text.strip()
                if extract_key_idx is not None:
                    record_extract_use(extract_key_idx)
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

def process_rental_contract(clients, pdf_info, key_switch_attempt: int = 0):
    """Process and analyze a rental contract. Gebruikt rotator: 12 extract-keys (KEY_9..20), max 18/24u per key."""
    start_time = time.time()
    dbx_analyze = clients['dbx_analyze']
    dbx_target = clients['dbx_target']
    model = clients['model_analyze']
    model_name = getattr(model, "name", str(model)) if model else ""

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
    if key_switch_attempt > 0:
        print(f"   ↪️  Key switch retry {key_switch_attempt}/{MAX_KEY_SWITCH_RETRIES}")

    def _retry_with_next_key(reason: str, details: str = ""):
        # Tel deze key mee zodat de volgende selectie waarschijnlijk een andere key kiest.
        if key_idx is not None and key_idx >= 0:
            record_extract_use(key_idx)
        remove_from_history(ANALYZED_HISTORY, pdf_info['path'])

        if key_switch_attempt < MAX_KEY_SWITCH_RETRIES:
            print(f"   🔁 {reason} - switching to next key (attempt {key_switch_attempt + 1}/{MAX_KEY_SWITCH_RETRIES})")
            if details:
                print(f"      details: {details[:140]}")
            time.sleep(2)
            return process_rental_contract(clients, pdf_info, key_switch_attempt=key_switch_attempt + 1)

        payload = {'requeue': True}
        if reason == "QUOTA_EXCEEDED":
            payload['quota_error'] = True
        elif reason == "API_KEY_ERROR":
            payload['api_key_error'] = True
        else:
            payload['error'] = True
        if details:
            payload['error_msg'] = details
        return payload

    # Claim onder lock + verse history: voorkomt dubbele Gemini-run bij 2× script of race tussen workers
    with _phase2_analysis_lock():
        hist = load_history(ANALYZED_HISTORY)
        norm_path = _normalize_dropbox_path(pdf_info['path'])
        if norm_path in hist:
            print(f"   ⏭️  Skip — staat al in analyzed_docs (andere worker of eerdere run): {norm_path}")
            return {'success': True, 'skipped': True, 'already_done': True}
        add_to_history(ANALYZED_HISTORY, pdf_info['path'])
    print(f"   📝 Claimed for analysis: {pdf_info['path']}")

    try:
        print(f"\n{'='*70}")
        print(f"📄 {pdf_info['name']}")
        print(f"📂 Location: {pdf_info['folder']}")
        print(f"{'='*70}")

        # Download and extract text WITH OCR fallback
        print(f"⬇️  Downloading...")
        _, response = dbx_analyze.files_download(pdf_info['path'])

        print(f"📖 Extracting text...")
        full_text, pdf_metadata, pages_text = extract_text_with_ocr(response.content, gemini, model, extract_key_idx=key_idx)
        print(f"✓ Text: {len(full_text)} chars")
        record_extraction_method(pdf_metadata.get("ocr_engine", "text_layer"))

        if len(full_text.strip()) < 50:
            print(f"⚠️  Too little text - skipping")
            return None

        # Woord-niveau (zelfde als huurcontract) voor PDF-markering bij EPC + huur
        words_by_page: Dict[int, List[Dict[str, Any]]] = {}
        all_words: List[Dict[str, Any]] = []
        if pdf_metadata.get("extraction_method") == "text" and response.content:
            try:
                words_by_page, all_words = extract_words_with_ids(response.content)
            except Exception as we:
                logger.warning(f"Word extraction skipped: {we}")

        doc_type = get_document_type_from_path_and_name(pdf_info.get('path') or '', pdf_info.get('name') or '')
        if doc_type == 'epc':
            print("🤖 Gemini analyzing EPC document (multi-stage)...")
            epc_data = extract_epc_data(
                full_text, gemini, model,
                extract_key_idx=key_idx,
                all_words=all_words if all_words else None,
            )
            if epc_data.get('error') == 'QUOTA_EXCEEDED':
                return _retry_with_next_key('QUOTA_EXCEEDED', epc_data.get('details', 'EPC extract quota'))
            if epc_data.get('error') == 'API_KEY_ERROR':
                return _retry_with_next_key('API_KEY_ERROR', epc_data.get('details', 'EPC extract key error'))
            if epc_data.get('error'):
                print(f"❌ EPC extractie mislukt: {epc_data.get('error')}")
                if epc_data.get('error') == 'STAGE_FAILED':
                    print(f"   ❌ EPC stage failed: {epc_data.get('stage')}")
                return None
            epc_data = normalize_epc_data(epc_data)
            gebouw = epc_data.get('gebouw') or {}
            meta = epc_data.get('metadata') or {}
            adres = _unwrap_field_value(gebouw.get('adres')) or ''
            if isinstance(adres, str):
                adres = adres.strip()
            normalized_data = {
                "pand": {"adres": adres},
                "epc_document": epc_data,
            }
            confidence = calculate_confidence_epc(epc_data)
            cert = _unwrap_field_value(meta.get('certificaat_nummer')) or '—'
            opp = _unwrap_field_value(gebouw.get('oppervlakte_m2'))
            geldig = _unwrap_field_value(meta.get('geldig_tot')) or '—'
            score = _unwrap_field_value((epc_data.get('prestaties') or {}).get('energiescore_kwh_m2'))
            summary = (
                f"EPC: cert. {cert}, adres {adres or '—'}, score {score if score is not None else '—'} kWh/m², "
                f"opp. {opp if opp is not None else '—'} m², geldig tot {geldig}."
            ).strip()
            processing_time = time.time() - start_time
            print(f"✓ EPC verwerkt in {processing_time:.1f}s - Score: {confidence['score']}%")
            result = {
                "success": True,
                "filename": pdf_info['name'],
                "title": "EPC",
                "type_verified": True,
                "full_text": full_text,
                "raw_data": epc_data,
                "normalized_data": normalized_data,
                "summary": summary,
                "confidence": confidence,
                "processing_time": processing_time,
                "words_by_page": words_by_page if words_by_page else None,
            }
        else:
            # Word-level extraction for position-based PDF highlight (only when text came from text layer)

            # Extract raw data (huurcontract)
            print("🤖 Gemini analyzing contract...")
            contract_data = extract_contract_data(
                full_text, gemini, model,
                pages_text=pages_text,
                extract_key_idx=key_idx,
                all_words=all_words if all_words else None,
                words_by_page=words_by_page if words_by_page else None,
            )

            if contract_data.get('error') == 'QUOTA_EXCEEDED':
                print(f"❌ QUOTA EXCEEDED - requeuing document for retry")
                return _retry_with_next_key('QUOTA_EXCEEDED', contract_data.get('details', 'Contract extract quota'))
            if contract_data.get('error') == 'API_KEY_ERROR':
                print(f"❌ API KEY ERROR - requeuing document for retry")
                return _retry_with_next_key('API_KEY_ERROR', contract_data.get('details', 'Contract extract key error'))
            if contract_data.get('error') == 'STAGE_FAILED':
                print(f"❌ STAGE FAILED - stopping contract extraction at stage: {contract_data.get('stage')}")
                remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
                return {'error': True, 'requeue': True, 'error_msg': contract_data.get('details', 'Stage failed')}

            normalized_data = normalizer.normalize(contract_data)
            confidence = calculate_confidence_normalized(normalized_data, full_text, "huurovereenkomst", True)
            summary = generate_summary(full_text, "huurovereenkomst", gemini, model, extract_key_idx=key_idx)

            if "QUOTA_EXCEEDED" in summary:
                return _retry_with_next_key('QUOTA_EXCEEDED', 'Summary quota exceeded')
            if "API_KEY_ERROR" in summary or is_api_key_error(summary):
                return _retry_with_next_key('API_KEY_ERROR', str(summary))

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
                "processing_time": processing_time,
                "words_by_page": words_by_page if words_by_page else None,
            }

        # Save JSON to TARGET
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = pdf_info['name'].replace('.pdf', '')
        path_upper = (pdf_info.get('path') or '').upper().replace('\\', '/')

        # Voor EPC/kadaster/asbest: als pand.adres ontbreekt, afleiden uit mappad
        # Oude structuur: .../Type/Adres/file.pdf (Adres = segment vóór EPC/ASBEST). Nieuwe: .../Provincie/Adres/file.pdf (Adres = laatste map).
        if doc_type in ('epc', 'kadaster', 'asbest'):
            pand = normalized_data.get('pand') or {}
            if not isinstance(pand, dict):
                pand = {}
            current_adres = pand.get('adres')
            adres_val = _unwrap_field_value(current_adres) if current_adres is not None else None
            if not adres_val or (isinstance(adres_val, str) and not adres_val.strip()):
                path_str = (pdf_info.get('path') or '').replace('\\', '/').strip('/')
                parts = [p for p in path_str.split('/') if p]
                adres_folder = None
                sub_names = ('EPC', 'ASBEST', 'KADASTER', 'EIGENDOMSTITEL')
                for i, seg in enumerate(parts):
                    if seg.upper() in sub_names and i > 0:
                        adres_folder = parts[i - 1]
                        break
                if not adres_folder and len(parts) >= 2:
                    # Nieuwe structuur: /Contracten/Provincie/Adres/file.pdf → laatste map = Adres
                    adres_folder = parts[-2] if not parts[-1].lower().endswith('.pdf') else parts[-2]
                if adres_folder:
                    derived_adres = adres_folder.replace('_', ' ').strip()
                    if derived_adres:
                        normalized_data.setdefault('pand', {})['adres'] = derived_adres

        json_data = {
            "filename": result['filename'],
            "document_type": doc_type,
            "type_verified": result['type_verified'],
            "processed": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "confidence": result['confidence'],
            "contract_data": normalized_data,
            "summary": result['summary']
        }

        if "raw_data" in result and "error" not in result['raw_data']:
            json_data["raw_data"] = result['raw_data']

        # Words per page for exact PDF highlight in frontend (word_ids in contract_data)
        if result.get("words_by_page"):
            json_data["words_by_page"] = result["words_by_page"]

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

        # Log to CSV (Dropbox TARGET blijft voor CSV)
        log_to_csv(dbx_target, pdf_info['name'], result, json_name, "success")
        log_api_performance(
            "extract",
            pdf_info['name'],
            result.get('processing_time', 0),
            True,
            document_type=result.get('title'),
            model=model_name,
            key_index=key_idx,
            extraction_method=pdf_metadata.get('extraction_method'),
        )

        # Send email - alleen als verwerking succesvol was
        # Add key contract details (unwrap value/source_quote dicts voor weergave)
        normalized = result['normalized_data']
        details_section = ""

        if normalized.get('partijen'):
            verhuurder = _unwrap_field_value(normalized['partijen'].get('verhuurder', {}).get('naam')) or 'N/A'
            huurder = _unwrap_field_value(normalized['partijen'].get('huurder', {}).get('naam')) or 'N/A'
            details_section += f"""
PARTIES
Landlord: {verhuurder}
Tenant: {huurder}
"""

        if normalized.get('pand'):
            adres = _unwrap_field_value(normalized['pand'].get('adres')) or 'N/A'
            details_section += f"""
PROPERTY
Address: {adres}
"""

        if normalized.get('financieel'):
            huurprijs_raw = normalized['financieel'].get('huurprijs')
            waarborg_raw = normalized['financieel'].get('waarborg', {}).get('bedrag')
            huurprijs = _unwrap_field_value(huurprijs_raw)
            waarborg = _unwrap_field_value(waarborg_raw)
            if huurprijs is not None or waarborg is not None:
                details_section += f"""
FINANCIAL"""
                if huurprijs is not None and huurprijs != '':
                    try:
                        details_section += f"\nRent: €{float(huurprijs):.2f}/month"
                    except (TypeError, ValueError):
                        details_section += f"\nRent: {huurprijs}/month"
                if waarborg is not None and waarborg != '':
                    try:
                        details_section += f"\nDeposit: €{float(waarborg):.2f}"
                    except (TypeError, ValueError):
                        details_section += f"\nDeposit: {waarborg}"
                details_section += "\n"

        if normalized.get('periodes'):
            start = _unwrap_field_value(normalized['periodes'].get('ingangsdatum')) or 'N/A'
            duur = _unwrap_field_value(normalized['periodes'].get('duur')) or 'N/A'
            details_section += f"""
PERIOD
Start Date: {start}
Duration: {duur}
"""

        subject = EMAIL_SUBJECT.format(
            title=result['title'],
            filename=pdf_info['name'],
        )
        email_body = EMAIL_BODY.format(
            filename=pdf_info['name'],
            title=result['title'],
            details_section=details_section,
            summary=result['summary'],
        )
        image_path = None
        if EMAIL_IMAGE_PATH:
            image_path = EMAIL_IMAGE_PATH if os.path.isabs(EMAIL_IMAGE_PATH) else os.path.join(_script_dir, EMAIL_IMAGE_PATH)
        send_email(subject, email_body, image_path=image_path)

        return result

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Processing error: {error_msg}")
        log_api_performance(
            "extract",
            pdf_info['name'],
            time.time() - start_time,
            False,
            document_type=get_document_type_from_path_and_name(pdf_info.get('path') or '', pdf_info.get('name') or ''),
            error=error_msg,
            model=model_name,
            key_index=key_idx,
        )
        # Check if it's an API key error
        if is_api_key_error(error_msg):
            print(f"   🔄 API Key error detected - requeuing document for retry")
            return _retry_with_next_key('API_KEY_ERROR', error_msg)
        
        # Check if it's a quota error
        if is_quota_error(error_msg):
            print(f"   🔄 Quota error detected - requeuing document for retry")
            return _retry_with_next_key('QUOTA_EXCEEDED', error_msg)
        
        # For other errors, also requeue (but log the error)
        print(f"   🔄 Unknown error - requeuing document for retry")
        remove_from_history(ANALYZED_HISTORY, pdf_info['path'])
        return {'error': True, 'requeue': True, 'error_msg': error_msg}


# ============================================================================
# PHASE 2: ANALYZE CONTRACTS (huur, EPC, asbest, kadaster, …)
# ============================================================================

def analyze_rental_contracts(clients, analyzed_history):
    """Find and analyze all contract documents (huur, EPC, asbest, kadaster) under /Contracten/."""

    dbx_analyze = clients['dbx_analyze']

    print(f"\n{'='*70}")
    print(f"🔍 PHASE 2: ANALYZING CONTRACTS (huur, EPC, asbest, …)")
    print(f"{'='*70}")

    # DEBUG: Show what's in history
    print(f"📋 Analyzed history contains {len(analyzed_history)} entries")
    if analyzed_history:
        print(f"   Sample entries:")
        for path in list(analyzed_history)[:3]:
            print(f"   - {path}")

    # Find all contract folders (onder /Contracten/) die PDFs bevatten
    print("📁 Scanning for contract folders (huur, EPC, asbest, …)...")
    contract_folders = find_contract_folders_with_pdfs(dbx_analyze)

    if not contract_folders:
        print("ℹ️  No contract folders with PDFs found under /Contracten/")
        return 0

    print(f"✓ Found {len(contract_folders)} folder(s) with PDFs")

    # Find PDFs in these folders
    print("\n📄 Finding PDFs in contract folders...")
    all_pdfs = find_pdfs_in_folders(dbx_analyze, contract_folders)

    if not all_pdfs:
        print("ℹ️  No PDFs found in contract folders")
        return 0

    print(f"✓ Found {len(all_pdfs)} PDF(s) (huur, EPC, asbest, …)")

    # DEBUG: Show all PDF paths
    print(f"\n📝 All PDF paths found:")
    for pdf in all_pdfs[:5]:
        in_history = "✓ IN HISTORY" if pdf['path'] in analyzed_history else "✗ NEW"
        print(f"   {in_history}: {pdf['path']}")
    if len(all_pdfs) > 5:
        print(f"   ... and {len(all_pdfs) - 5} more")

    # Filter out already analyzed + unieke paden (zelfde PDF niet 2× in één batch)
    seen_paths: Set[str] = set()
    new_pdfs: List[Dict[str, Any]] = []
    for pdf in all_pdfs:
        pth = pdf.get("path") or ""
        if pth in analyzed_history or pth in seen_paths:
            continue
        seen_paths.add(pth)
        new_pdfs.append(pdf)

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
        if _shutdown_requested[0]:
            print("\n⏹️  Stop aangevraagd - analyse afgebroken.")
            break
        print(f"\n📋 Contract {i}/{len(new_pdfs)}")
        print(f"   Path: {pdf_info['path']}")

        result = process_rental_contract(clients, pdf_info)

        if result and result.get("skipped"):
            print(f"   ⏭️  Geen actie (dubbele claim vermeden): {pdf_info['path']}")
            continue

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

    # Ctrl+C: flag zetten zodat we na de huidige bewerking stoppen (niet midden in een API-call)
    signal.signal(signal.SIGINT, _sigint_handler)
    _shutdown_requested[0] = False

    # Initialize
    clients = init_clients()

    if not clients:
        print("❌ Cannot start - check credentials!")
        return

    folder_mgr = FolderManager(clients['dbx_organize'])

    # Dubbele regels in analyzed_docs.txt opruimen; daarna verse load
    dedupe_history_file_on_disk(ANALYZED_HISTORY)
    organized_history = load_history(ORGANIZED_HISTORY)
    analyzed_history = load_history(ANALYZED_HISTORY)

    print(f"\n✓ Organized history: {len(organized_history)} file(s)")
    print(f"✓ Analyzed history: {len(analyzed_history)} contract(s)")
    print(f"\n{'='*70}")
    print(f"🚀 SYSTEM STARTED")
    print(f"{'='*70}\n")

    while True:
        if _shutdown_requested[0]:
            print("\n\n⏹️  Stopped by user")
            break
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
            # Wait before next cycle (stop als gebruiker Ctrl+C heeft gedrukt)
            if _shutdown_requested[0]:
                print("\n\n⏹️  Stopped by user")
                break
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
    api_key, key_idx = get_next_extract_key()
    if not api_key:
        print("❌ Geen extract-key beschikbaar (12 keys op 18/24u)")
        return False
    gemini = genai.Client(api_key=api_key)
    full_text, _, _ = extract_text_with_ocr(pdf_bytes, gemini, clients["model_analyze"], extract_key_idx=key_idx)
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