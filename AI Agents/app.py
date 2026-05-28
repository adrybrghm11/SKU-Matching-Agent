import csv
import concurrent.futures
import hashlib
import io
import html
import json
import os
import re
import threading
import time
import datetime
import random
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict
import streamlit as st
import streamlit.components.v1 as components

TODAY = "2026-05-11"
NEXT_GEN_AVAILABLE_DATE = "2026-05-19"
ENABLE_NEXT_GEN_SUGGESTIONS = True
LOW_STOCK_COMPARABLE_THRESHOLD = 5
SKU_PATTERN = re.compile(r"\b([A-Z0-9]{3})[-\u2010\u2011\u2012\u2013\u2014\u2212]?\s*([0-9]{5})\b")

SURFACE_PRO_10_11_WARRANTY_SKUS = [
    "W49-00185", "A9W-00264", "VP3-00205",
    "NRB-00143", "NRI-00142", "NRQ-00142",
    "W47-00228", "F9W-00287", "HP3-00215",
    "ZWK-00032", "ZWL-00033", "ZWM-00034",
]

SURFACE_LAPTOP_7_WARRANTY_COVERAGE = {
    "W49-00202": "2 Years Total",
    "A9W-00281": "3 Years Total",
    "VP3-00222": "4 Years Total",
    "NRB-00160": "2 Years Total",
    "NRI-00159": "3 Years Total",
    "NRQ-00159": "4 Years Total",
    "W47-00237": "2 Years Total",
    "F9W-00296": "3 Years Total",
    "HP3-00224": "4 Years Total",
    "ZWK-00041": "2 Years Total",
    "ZWL-00042": "3 Years Total",
    "ZWM-00043": "4 Years Total",
}

def _build_pro_10_11_warranty_coverage_map():
    coverage_map = {}
    for idx, sku in enumerate(SURFACE_PRO_10_11_WARRANTY_SKUS):
        year_total = (idx % 3) + 2
        coverage_map[sku] = f"{year_total} Years Total"
    return coverage_map

WARRANTY_COVERAGE_BY_SKU = {
    **_build_pro_10_11_warranty_coverage_map(),
    **SURFACE_LAPTOP_7_WARRANTY_COVERAGE,
}

# Explicitly excluded EOL accessories for Accessory Lookup output.
EOL_ACCESSORY_SKUS = {
    "1GK-00001",  # Surface Dock 2
    "PF3-00005",  # Surface Dock
    "KGZ-00001", "KGZ-00011", "KGZ-00021", "KGZ-00031", "KGZ-00041", "KGZ-00051", "KGZ-00063",  # Surface Mobile Mouse legacy colors
    "LPL-00001",  # Surface Hub 2 Camera
    "FHD-00062", "FHD-00072",  # Arc Mouse legacy colors
    "J61-00001", "J71-00001",  # Adaptive mouse/hub legacy accessories
}

ACCESSORY_DOCK_SKUS = {
    "EP2-19863", "T8I-00001",
}

ACCESSORY_KEYBOARD_SKUS = {
    "Y8U-00001",
    "ZRA-00001",
    "ZRA-00023",
    "8X8-00141",
    "8X8-00164",
    "8XB-00139",
    "8XB-00186",
    "8XB-00162",
    "EP2-00395",
    "EP2-33129",
    "EP2-32892",
    "EP2-32894",
}

ACCESSORY_PEN_SKUS = {
    "8WX-00001",
    "8X3-00001",
    "NJ1-00001",
}

ACCESSORY_MISC_SKUS = {
    "W8Z-00001",
    "1E4-00001",
    "EP2-29830",
}

BASE_DIR = Path(__file__).resolve().parent

PRODUCT_FILES = [
    "Laptop 7 SKU List.csv",
    "Laptop 8 SKU List.csv",
    "Pro 11 SKU List.csv",
]

DEFAULT_INVENTORY_FILES = {
    "DandH5.11.2026.csv": "DandH",
    "Ingram5.11.26.csv": "Ingram",
    "Synnex5.11.26.csv": "Synnex",
    "Surface Accessories.csv": "DandH",
}

INVENTORY_UPLOAD_CACHE_DIR = BASE_DIR / ".inventory_upload_cache"
INVENTORY_UPLOAD_MANIFEST = INVENTORY_UPLOAD_CACHE_DIR / "manifest.json"
INVENTORY_DISTI_NAMES = ("DandH", "Ingram", "Synnex")


def candidate_data_dirs():
    return [
        BASE_DIR,
        BASE_DIR / "data",
        BASE_DIR / "SKU Lookup Agent",
    ]


def resolve_data_file(filename):
    path = Path(filename)
    if path.is_absolute() and path.exists():
        return path

    for data_dir in candidate_data_dirs():
        candidate = data_dir / filename
        if candidate.exists():
            return candidate

    return path


def infer_inventory_distributor_from_name(file_name):
    normalized = re.sub(r"[\s._-]+", " ", clean_text(file_name).lower().replace("&", " and "))
    if "ingram" in normalized:
        return "Ingram"
    if "synnex" in normalized or "td synnex" in normalized:
        return "Synnex"
    if "d and h" in normalized or "dandh" in normalized:
        return "DandH"
    return ""


def detect_inventory_distributor_from_rows(rows):
    for distributor in INVENTORY_DISTI_NAMES:
        required = {normalized_col_key(c) for c in inventory_header_candidates(distributor)}
        for row in rows[:10]:
            normalized = {normalized_col_key(c) for c in row if clean_text(c)}
            if required.issubset(normalized):
                return distributor
    return ""


def load_inventory_upload_manifest():
    if not INVENTORY_UPLOAD_MANIFEST.exists():
        return {}

    try:
        with open(INVENTORY_UPLOAD_MANIFEST, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        return manifest if isinstance(manifest, dict) else {}
    except Exception:
        return {}


def save_inventory_upload_cache(uploaded_file, distributor):
    if not uploaded_file or distributor not in INVENTORY_DISTI_NAMES:
        return None

    INVENTORY_UPLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    source_name = clean_text(getattr(uploaded_file, "name", "")) or f"{distributor}.csv"
    suffix = Path(source_name).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        suffix = ".csv"

    stored_name = f"{distributor.lower()}_{file_hash[:12]}{suffix}"
    stored_path = INVENTORY_UPLOAD_CACHE_DIR / stored_name
    if not stored_path.exists():
        with open(stored_path, "wb") as handle:
            handle.write(file_bytes)

    manifest = load_inventory_upload_manifest()
    manifest[distributor] = {
        "file_name": stored_name,
        "source_name": source_name,
        "file_hash": file_hash,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    with open(INVENTORY_UPLOAD_MANIFEST, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    return stored_path


def load_inventory_rows_from_path(file_path, distributor, source_name):
    records = read_inventory_file(file_path, distributor)
    return parse_inventory_records(records, distributor, source_name)


def load_inventory_rows_from_uploaded_file(uploaded_file, distributor):
    records = read_uploaded_inventory_file(uploaded_file, distributor)
    source_name = clean_text(getattr(uploaded_file, "name", "")) or f"{distributor} upload"
    return parse_inventory_records(records, distributor, source_name)


def get_latest_inventory_upload_label(manifest=None):
    manifest = manifest or load_inventory_upload_manifest()
    latest_timestamp = None

    for distributor in INVENTORY_DISTI_NAMES:
        meta = manifest.get(distributor, {}) if isinstance(manifest, dict) else {}
        saved_at = clean_text(meta.get("saved_at", ""))
        if not saved_at:
            continue
        try:
            timestamp = datetime.datetime.fromisoformat(saved_at)
        except ValueError:
            continue
        if latest_timestamp is None or timestamp > latest_timestamp:
            latest_timestamp = timestamp

    if latest_timestamp is None:
        return "No inventory reports loaded yet"

    return latest_timestamp.strftime("%b %-d, %Y %-I:%M %p") if os.name != "nt" else latest_timestamp.strftime("%b %#d, %Y %#I:%M %p")


def detect_data_dir():
    expected_files = PRODUCT_FILES + list(DEFAULT_INVENTORY_FILES.keys())
    best_dir = BASE_DIR
    best_score = -1

    for data_dir in candidate_data_dirs():
        score = sum((data_dir / name).exists() for name in expected_files)
        if score > best_score:
            best_score = score
            best_dir = data_dir

    return best_dir


DATA_DIR = detect_data_dir()
DEFAULT_PART_LIST_FILE = resolve_data_file("Copy of Copy of Copy of 2024 Parts List .xlsx")


def clean_text(value):
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def normalize_sku_text(text):
    cleaned = clean_text(text).upper()
    return re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", cleaned)


def extract_normalized_skus(text):
    normalized = normalize_sku_text(text)
    matches = SKU_PATTERN.findall(normalized)
    result = []
    seen = set()
    for prefix, suffix in matches:
        sku = f"{prefix}-{suffix}"
        if sku not in seen:
            seen.add(sku)
            result.append(sku)
    return result


def clean_int(value):
    value = clean_text(value).replace(",", "").replace("$", "")
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def is_snapdragon_profile(profile):
    if not profile:
        return False
    platform = clean_text(profile.get("platform", "")).lower()
    hay = " ".join([
        platform,
        clean_text(profile.get("cpu_family", "")).lower(),
        clean_text(profile.get("cpu_tier", "")).lower(),
        clean_text(profile.get("raw_description", "")).lower(),
        clean_text(profile.get("description", "")).lower(),
    ])
    return platform == "arm" or "snapdragon" in hay or "x elite" in hay or "x plus" in hay


def sku_is_snapdragon(sku, *lookups):
    for lookup in lookups:
        profile = (lookup or {}).get(sku)
        if is_snapdragon_profile(profile):
            return True
    return False


def is_eol_profile(profile):
    """Return True when a product profile appears to be EOL/discontinued."""
    if not profile:
        return False

    hay = " ".join([
        clean_text(profile.get("status", "")),
        clean_text(profile.get("sheet_name", "")),
        clean_text(profile.get("raw_description", "")),
        clean_text(profile.get("description", "")),
    ]).lower()

    return bool(re.search(r"\b(eol|end\s*of\s*life|end[-\s]*of[-\s]*sale|discontinued|obsolete|retired)\b", hay))


def is_next_gen_device_profile(profile):
    """Only Laptop 8 (Intel) and Pro 12 (Intel) are considered next-gen."""
    if not profile:
        return False

    hay = " ".join([
        clean_text(profile.get("raw_description", "")),
        clean_text(profile.get("description", "")),
    ]).lower()

    if is_accessory_text(hay):
        return False

    intel_signal = bool(re.search(r"\b(intel|core\s*ultra|ultra\s*[57]|ultra\s*x7|\bu5\b|\bu7\b)\b", hay))
    laptop8_signal = bool(re.search(r"\b(surface\s+laptop\s*8|laptop\s*8|laptop8)\b", hay))
    # Treat "Pro 12" / "Pro 12th" naming as next-gen; keep "Pro 12-inch" separate.
    pro12_named_signal = bool(re.search(r"\b(surface\s+pro\s*12(?:th)?|pro\s*12(?:th)?|12th\s*edition)\b", hay))
    pro12_legacy_inch_signal = bool(re.search(r"\b(pro\s*12(?:[-\s]*inch|[-\s]*in|inch|in)|12[-\s]*inch\s*surface\s*pro)\b", hay))
    pro12_signal = pro12_named_signal and not pro12_legacy_inch_signal

    return intel_signal and (laptop8_signal or pro12_signal)


def is_supported_generation_device_profile(profile):
    """Allow only current-gen families + explicitly allowed next-gen families."""
    if not profile:
        return False

    hay = " ".join([
        clean_text(profile.get("raw_description", "")),
        clean_text(profile.get("description", "")),
    ]).lower()

    if is_accessory_text(hay):
        return False

    # Current generation families
    is_pro11 = bool(re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11)\b", hay))
    is_pro12_inch = bool(re.search(r"\b(surface\s+pro(?:\s|,)*12[-\s]*inch|pro\s*12(?:[-\s]*inch|[-\s]*in|inch|in)|12[-\s]*inch\s*surface\s*pro)\b", hay))
    is_laptop7 = bool(re.search(r"\b(surface\s+laptop\s*7|laptop\s*7|laptop7)\b", hay))
    is_laptop5g = bool(re.search(r"\b(surface\s+laptop\s*5g|laptop\s*5g|laptop5g)\b", hay))

    # Next generation families allowed by business rule
    is_next_gen = is_next_gen_device_profile(profile)

    return is_pro11 or is_pro12_inch or is_laptop7 or is_laptop5g or is_next_gen


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        return [row for row in reader]


def read_uploaded_csv(uploaded_file):
    content = uploaded_file.getvalue().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(content)))


def normalized_col_key(value):
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def normalize_inventory_sku(value):
    candidates = extract_normalized_skus(value)
    if candidates:
        return candidates[0]
    return normalize_sku_text(value).replace(" ", "")


def inventory_header_candidates(distributor):
    if distributor == "DandH":
        return ["Product Code", "Vendor Item No", "Product Name", "Stock on Hand", "Qty On Order"]
    if distributor == "Ingram":
        return ["MFR #", "Product Description", "Ingram On-Hand", "Ingram On-Order"]
    if distributor == "Synnex":
        return ["MFGPart#", "Short Desc", "Avail Qty"]
    return []


def read_inventory_dict_rows_from_rows(rows, distributor):
    if not rows:
        return []

    required = {normalized_col_key(c) for c in inventory_header_candidates(distributor)}
    header_index = 0
    for idx, row in enumerate(rows):
        normalized = {normalized_col_key(c) for c in row if clean_text(c)}
        if required.issubset(normalized):
            header_index = idx
            break

    headers = [clean_text(h) for h in rows[header_index]]
    result = []
    for raw in rows[header_index + 1:]:
        if not any(clean_text(c) for c in raw):
            continue
        if len(raw) < len(headers):
            raw = raw + [""] * (len(headers) - len(raw))
        rec = {headers[i]: raw[i] for i in range(len(headers))}
        result.append(rec)

    return result


def read_inventory_dict_rows_from_text(csv_text, distributor):
    rows = list(csv.reader(io.StringIO(csv_text)))
    return read_inventory_dict_rows_from_rows(rows, distributor)


def read_inventory_dict_rows_from_xlsx_bytes(xlsx_bytes, distributor):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("Install openpyxl to enable inventory XLSX parsing.")

    workbook = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([clean_text(cell) for cell in row])
        records = read_inventory_dict_rows_from_rows(rows, distributor)
        if records:
            return records
    return []


def read_uploaded_inventory_file(uploaded_file, distributor):
    file_name = clean_text(getattr(uploaded_file, "name", "")).lower()
    if file_name.endswith(".xlsx"):
        return read_inventory_dict_rows_from_xlsx_bytes(uploaded_file.getvalue(), distributor)

    content = uploaded_file.getvalue().decode("utf-8-sig")
    return read_inventory_dict_rows_from_text(content, distributor)


def read_inventory_file(file_path, distributor):
    file_suffix = Path(file_path).suffix.lower()
    if file_suffix == ".xlsx":
        with open(file_path, "rb") as f:
            return read_inventory_dict_rows_from_xlsx_bytes(f.read(), distributor)

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        return read_inventory_dict_rows_from_text(f.read(), distributor)


@st.cache_data
def parse_part_list_xlsx_bytes(xlsx_bytes):
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {}, {}, "Install openpyxl to enable part list XLSX parsing."

    try:
        workbook = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    except Exception as exc:
        return {}, {}, f"Unable to read XLSX: {exc}"

    source_hints = ("source", "original", "old", "from")
    replacement_hints = ("replacement", "new", "target", "to")
    mapping = {}
    sku_profiles = {}
    sheet_count = 0

    def remember_sku_profile(sku, description, sheet_name):
        desc = clean_text(description)
        current = sku_profiles.get(sku)
        if not current:
            sku_profiles[sku] = {
                "raw_description": desc or f"SKU {sku}",
                "sheet_name": sheet_name,
            }
            return

        current_desc = clean_text(current.get("raw_description"))
        if current_desc == f"SKU {sku}" and desc:
            sku_profiles[sku] = {"raw_description": desc, "sheet_name": sheet_name}
            return

        if not desc:
            # Keep existing description, but refresh sheet context if needed.
            if not clean_text(current.get("sheet_name")) and sheet_name:
                sku_profiles[sku] = {
                    "raw_description": current_desc or f"SKU {sku}",
                    "sheet_name": sheet_name,
                }
            return

        current_has_surface = "surface" in current_desc.lower()
        new_has_surface = "surface" in desc.lower()

        if (not current_has_surface and new_has_surface) or len(desc) > len(current_desc):
            sku_profiles[sku] = {"raw_description": desc, "sheet_name": sheet_name}

    def find_nearby_description(row_cells, anchor_index):
        # Look near the SKU cell for a human-readable product description.
        for offset in (1, 2, -1, -2):
            idx = anchor_index + offset
            if idx < 0 or idx >= len(row_cells):
                continue
            candidate_desc = clean_text(row_cells[idx])
            if candidate_desc and not extract_normalized_skus(candidate_desc):
                if "surface" in candidate_desc.lower() or "u7" in candidate_desc.lower() or "u5" in candidate_desc.lower():
                    return candidate_desc
        return ""

    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([clean_text(cell) for cell in row])

        if not rows:
            continue

        sheet_name = sheet.title
        sheet_count += 1

        header_row = rows[0]
        normalized_columns = {index: clean_text(col).lower() for index, col in enumerate(header_row)}
        source_col = None
        replacement_cols = []

        for col_index, normalized in normalized_columns.items():
            if source_col is None and any(hint in normalized for hint in source_hints):
                source_col = col_index
            if any(hint in normalized for hint in replacement_hints):
                replacement_cols.append(col_index)

        def row_value(row_cells, index):
            if index < 0 or index >= len(row_cells):
                return ""
            return row_cells[index]

        if source_col and replacement_cols:
            for row_cells in rows[1:]:
                source_skus = extract_normalized_skus(row_value(row_cells, source_col))
                if not source_skus:
                    continue

                source_desc = ""
                if source_col + 1 < len(row_cells):
                    candidate_desc = clean_text(row_value(row_cells, source_col + 1))
                    if candidate_desc and not extract_normalized_skus(candidate_desc):
                        source_desc = candidate_desc
                if not source_desc:
                    source_desc = find_nearby_description(row_cells, source_col)

                source = source_skus[0]
                remember_sku_profile(source, source_desc, sheet_name)
                replacements = []
                for repl_col in replacement_cols:
                    repl_desc = ""
                    if repl_col + 1 < len(row_cells):
                        candidate_desc = clean_text(row_value(row_cells, repl_col + 1))
                        if candidate_desc and not extract_normalized_skus(candidate_desc):
                            repl_desc = candidate_desc
                    if not repl_desc:
                        repl_desc = find_nearby_description(row_cells, repl_col)

                    for repl_sku in extract_normalized_skus(row_value(row_cells, repl_col)):
                        remember_sku_profile(repl_sku, repl_desc, sheet_name)
                    replacements.extend(extract_normalized_skus(row_value(row_cells, repl_col)))

                deduped = []
                seen = {source}
                for sku in replacements:
                    if sku not in seen:
                        seen.add(sku)
                        deduped.append(sku)

                if deduped:
                    existing = mapping.get(source, [])
                    mapping[source] = existing + [s for s in deduped if s not in existing]
            continue

        for row_cells in rows:
            row_text = " | ".join(v for v in row_cells if v)

            # Always harvest SKU profiles from nearby description cells.
            for idx, cell in enumerate(row_cells):
                cell_skus = extract_normalized_skus(cell)
                if not cell_skus:
                    continue
                neighbor_desc = find_nearby_description(row_cells, idx)
                for sku in cell_skus:
                    remember_sku_profile(sku, neighbor_desc, sheet_name)

            skus = extract_normalized_skus(row_text)
            if len(skus) < 2:
                continue

            source = skus[0]
            deduped = []
            seen = {source}
            for sku in skus[1:]:
                if sku not in seen:
                    seen.add(sku)
                    deduped.append(sku)

            if deduped:
                existing = mapping.get(source, [])
                mapping[source] = existing + [s for s in deduped if s not in existing]

    if not mapping:
        if not sku_profiles:
            return {}, {}, f"No SKU mappings were detected in the uploaded part list across {sheet_count} sheet(s)."
        return {}, sku_profiles, f"No direct SKU-to-SKU mappings detected. Parsed {len(sku_profiles)} SKU descriptions across {sheet_count} sheet(s)."

    return mapping, sku_profiles, f"Part list mappings loaded: {len(mapping)} source SKU(s) across {sheet_count} sheet(s)"


def parse_part_list_csv_rows(rows, source_name="part list csv"):
    sku_profiles = {}

    def remember_sku_profile(sku, description, sheet_name):
        desc = clean_text(description)
        current = sku_profiles.get(sku)
        if not current:
            sku_profiles[sku] = {
                "raw_description": desc or f"SKU {sku}",
                "sheet_name": sheet_name,
            }
            return

        current_desc = clean_text(current.get("raw_description"))
        if current_desc == f"SKU {sku}" and desc:
            sku_profiles[sku] = {"raw_description": desc, "sheet_name": sheet_name}
            return

        if not desc:
            if not clean_text(current.get("sheet_name")) and sheet_name:
                sku_profiles[sku] = {
                    "raw_description": current_desc or f"SKU {sku}",
                    "sheet_name": sheet_name,
                }
            return

        current_has_surface = "surface" in current_desc.lower()
        new_has_surface = "surface" in desc.lower()
        if (not current_has_surface and new_has_surface) or len(desc) > len(current_desc):
            sku_profiles[sku] = {"raw_description": desc, "sheet_name": sheet_name}

    def find_nearby_description(row_cells, anchor_index):
        for offset in (1, 2, -1, -2):
            idx = anchor_index + offset
            if idx < 0 or idx >= len(row_cells):
                continue
            candidate_desc = clean_text(row_cells[idx])
            if candidate_desc and not extract_normalized_skus(candidate_desc):
                if re.search(r"\b(surface|snapdragon|x\s*elite|x\s*plus|ultra\s*[57]|u[57]|intel|core\s*ultra)\b", candidate_desc.lower()):
                    return candidate_desc
        return ""

    current_section = source_name
    for row in rows:
        row_cells = [clean_text(v) for v in row]
        row_text = " | ".join(v for v in row_cells if v)
        if not row_text:
            continue

        row_lower = row_text.lower()
        if (
            "surface" in row_lower
            and "sku" in row_lower
            and "part number" not in row_lower
            and not row_lower.startswith("sku")
            and not extract_normalized_skus(row_text)
        ):
            current_section = row_text

        for idx, cell in enumerate(row_cells):
            cell_skus = extract_normalized_skus(cell)
            if not cell_skus:
                continue
            neighbor_desc = find_nearby_description(row_cells, idx)
            for sku in cell_skus:
                remember_sku_profile(sku, neighbor_desc, current_section)

    if not sku_profiles:
        return {}, {}, f"No SKU descriptions were detected in {source_name}."

    return {}, sku_profiles, f"Part list descriptions loaded from CSV: {len(sku_profiles)} SKU(s)"


def parse_part_list_csv_text(csv_text, source_name="uploaded csv"):
    rows = list(csv.reader(io.StringIO(csv_text)))
    return parse_part_list_csv_rows(rows, source_name)


def load_part_list_source(file_path):
    try:
        suffix = clean_text(Path(file_path).suffix).lower()
        if suffix == ".xlsx":
            with open(file_path, "rb") as f:
                return parse_part_list_xlsx_bytes(f.read())
        if suffix == ".csv":
            rows = read_csv_rows(file_path)
            return parse_part_list_csv_rows(rows, Path(file_path).name)
        return {}, {}, f"Unsupported part list format: {suffix}"
    except FileNotFoundError:
        return {}, {}, f"Default part list workbook not found: {file_path}"
    except PermissionError:
        return {}, {}, f"Permission denied reading part list (file may be open or syncing): {file_path}"


def merge_part_list_sources(target_map, target_profiles, source_map, source_profiles):
    for source_sku, replacements in (source_map or {}).items():
        existing = target_map.get(source_sku, [])
        merged = existing + [sku for sku in replacements if sku not in existing]
        if merged:
            target_map[source_sku] = merged

    def merge_profile(sku, profile):
        desc = clean_text(profile.get("raw_description"))
        sheet_name = clean_text(profile.get("sheet_name"))
        current = target_profiles.get(sku)
        if not current:
            target_profiles[sku] = {
                "raw_description": desc or f"SKU {sku}",
                "sheet_name": sheet_name,
            }
            return

        current_desc = clean_text(current.get("raw_description"))
        current_sheet = clean_text(current.get("sheet_name"))
        current_has_surface = "surface" in current_desc.lower()
        new_has_surface = "surface" in desc.lower()

        if desc and ((not current_has_surface and new_has_surface) or len(desc) > len(current_desc)):
            target_profiles[sku] = {"raw_description": desc, "sheet_name": sheet_name or current_sheet}
            return

        if not current_sheet and sheet_name:
            target_profiles[sku] = {
                "raw_description": current_desc or desc or f"SKU {sku}",
                "sheet_name": sheet_name,
            }

    for sku, profile in (source_profiles or {}).items():
        merge_profile(sku, profile)


def load_part_list_sources(file_paths):
    merged_map = {}
    merged_profiles = {}
    statuses = []
    loaded_sources = 0

    for file_path in file_paths:
        if not file_path or not Path(file_path).exists():
            continue
        source_map, source_profiles, status = load_part_list_source(file_path)
        if source_map or source_profiles:
            loaded_sources += 1
        merge_part_list_sources(merged_map, merged_profiles, source_map, source_profiles)
        if status:
            statuses.append(f"{Path(file_path).name}: {status}")

    if loaded_sources == 0:
        return {}, {}, "No part list sources were found."

    if statuses:
        return merged_map, merged_profiles, " | ".join(statuses)

    return merged_map, merged_profiles, f"Loaded {loaded_sources} part list source(s)."


def apply_microsoft_like_theme():
    st.markdown(
        """
        <style>
        :root {
            --neo-green: #39ff14;
            --hot-pink: #ff2ea6;
            --bright-orange: #ff8c1a;
            --paper: #ffffff;
            --ink: #111827;
            --muted-ink: #5b6475;
            --border: #e5e7eb;
            --soft-surface: rgba(255, 255, 255, 0.9);
            --shadow: 0 24px 70px rgba(17, 24, 39, 0.08);
        }

        .stApp {
            background:
                radial-gradient(circle at 10% 8%, rgba(255, 46, 166, 0.13) 0%, transparent 26%),
                radial-gradient(circle at 90% 10%, rgba(57, 255, 20, 0.12) 0%, transparent 22%),
                radial-gradient(circle at 74% 88%, rgba(255, 140, 26, 0.16) 0%, transparent 18%),
                linear-gradient(180deg, #ffffff 0%, #fff9fc 48%, #f7fff7 100%);
            color: var(--ink);
            font-family: "Aptos", "Segoe UI", Tahoma, sans-serif;
        }

        .stApp::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background:
                linear-gradient(120deg, rgba(255, 46, 166, 0.05), transparent 28%),
                linear-gradient(300deg, rgba(57, 255, 20, 0.04), transparent 26%);
            z-index: 0;
        }

        header[data-testid="stHeader"] {
            display: none !important;
        }

        div[data-testid="stToolbar"],
        div[data-testid="stToolbarActions"],
        div[data-testid="stHeaderActionElements"],
        div[data-testid="stStatusWidget"] {
            display: none !important;
        }

        div[data-testid="stStatusWidget"] {
            display: none !important;
        }

        .block-container {
            position: relative;
            z-index: 1;
            padding-top: 0.9rem;
            padding-bottom: 2rem;
        }

        h1, h2, h3 {
            color: var(--ink);
            letter-spacing: 0.01em;
        }

        h1 {
            background: linear-gradient(90deg, var(--hot-pink), var(--bright-orange), var(--neo-green));
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            font-weight: 800;
        }

        p, li, span, label, small,
        div[data-testid="stMarkdownContainer"] {
            color: var(--ink) !important;
        }

        .inventory-upload-rail {
            display: flex;
            align-items: center;
            gap: 0.8rem;
            margin: 0 0 0.25rem 0;
            padding: 0.5rem 0 0.1rem 0;
        }

        .inventory-upload-icon {
            font-size: 1.55rem;
            line-height: 1;
            color: var(--hot-pink);
            filter: drop-shadow(0 8px 18px rgba(255, 46, 166, 0.18));
        }

        .inventory-upload-title {
            font-size: 0.98rem;
            font-weight: 800;
            color: var(--ink);
            letter-spacing: 0.01em;
        }

        .inventory-upload-meta {
            font-size: 0.82rem;
            color: var(--muted-ink);
        }

        div[data-testid="stTabs"] {
            background: rgba(255, 255, 255, 0.88);
            border: 1px solid rgba(255, 46, 166, 0.16);
            border-radius: 20px;
            box-shadow: var(--shadow);
            backdrop-filter: blur(8px);
            padding: 0.4rem 0.45rem 0;
        }

        button[role="tab"] p {
            color: var(--ink) !important;
            font-weight: 700;
        }

        button[role="tab"][aria-selected="true"] p {
            color: var(--hot-pink) !important;
        }

        div[data-baseweb="radio"] label,
        div[data-baseweb="radio"] span {
            color: var(--ink) !important;
            font-weight: 600;
        }

        button[kind="primary"], button[data-testid="baseButton-primary"] {
            background: linear-gradient(90deg, var(--hot-pink), var(--bright-orange)) !important;
            border: none !important;
            border-radius: 999px !important;
            color: #ffffff !important;
            font-weight: 800 !important;
            box-shadow: 0 14px 28px rgba(255, 46, 166, 0.18) !important;
        }

        button[kind="primary"]:hover, button[data-testid="baseButton-primary"]:hover {
            background: linear-gradient(90deg, var(--bright-orange), var(--neo-green)) !important;
        }

        .stTextArea textarea,
        .stTextInput input,
        .stSelectbox div[data-baseweb="select"] > div {
            border-radius: 16px;
            border: 1px solid var(--border);
            background: rgba(255, 255, 255, 0.95);
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
            caret-color: var(--hot-pink) !important;
            box-shadow: 0 8px 26px rgba(17, 24, 39, 0.04);
        }

        .stTextArea textarea::placeholder,
        .stTextInput input::placeholder {
            color: #7c8496 !important;
            -webkit-text-fill-color: #7c8496 !important;
            opacity: 1 !important;
        }

        label, .stMarkdown, .stCaption, .stText {
            color: var(--ink);
        }

        section[data-testid="stSidebar"],
        section[data-testid="stSidebar"] > div,
        section[data-testid="stSidebar"] > div > div {
            background: rgba(255, 255, 255, 0.82) !important;
            border-right: 1px solid rgba(57, 255, 20, 0.16) !important;
        }

        section[data-testid="stSidebar"] *,
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] small,
        section[data-testid="stSidebar"] div {
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }

        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3,
        section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3 {
            color: var(--hot-pink) !important;
            -webkit-text-fill-color: var(--hot-pink) !important;
            font-weight: 800 !important;
        }

        div[data-testid="stFileUploader"] > div {
            background-color: rgba(255, 255, 255, 0.82) !important;
            background: rgba(255, 255, 255, 0.82) !important;
            border: 1.5px dashed rgba(57, 255, 20, 0.88) !important;
            border-radius: 18px !important;
            box-shadow: var(--shadow);
        }

        div[data-testid="stFileUploader"] > div:hover {
            background-color: rgba(255, 242, 249, 0.95) !important;
            background: rgba(255, 242, 249, 0.95) !important;
            border-color: var(--hot-pink) !important;
        }

        div[data-testid="stFileUploaderDropzone"],
        section[data-testid="stFileUploaderDropzone"],
        div[data-testid="stFileUploaderDropzone"] > div,
        section[data-testid="stFileUploaderDropzone"] > div,
        div[data-testid="stFileUploaderDropzoneInstructions"],
        div[data-testid="stFileUploaderDropzoneInstructions"] > div {
            background: rgba(255, 255, 255, 0.82) !important;
            background-color: rgba(255, 255, 255, 0.82) !important;
            border: 1.5px dashed rgba(57, 255, 20, 0.88) !important;
            border-radius: 18px !important;
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }

        div[data-testid="stFileUploader"] label,
        div[data-testid="stFileUploader"] small,
        div[data-testid="stFileUploader"] span {
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }

        div[data-testid="stFileUploader"] button {
            background: var(--neo-green) !important;
            color: #07130a !important;
            -webkit-text-fill-color: #07130a !important;
            border-radius: 999px !important;
            border: none !important;
            font-weight: 800 !important;
        }

        div[data-testid="stFileUploader"] button:hover {
            background: var(--bright-orange) !important;
        }

        div[data-testid="stInfo"] {
            border: 1px solid rgba(255, 46, 166, 0.2);
            background: rgba(255, 255, 255, 0.84);
            color: var(--ink);
            border-radius: 14px;
            box-shadow: var(--shadow);
        }

        div[data-testid="stSuccess"] {
            border-radius: 14px;
            border: 1px solid rgba(57, 255, 20, 0.28);
            background: rgba(247, 255, 247, 0.92);
            color: #113311;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def extract_skus(text):
    if not text:
        return []
    return extract_normalized_skus(text)


def parse_bulk_sku_input(text):
    """Parse bulk SKU input from comma/newline/tab-separated text.

    Returns de-duplicated SKUs in original order. Blank values are skipped.
    """
    if not clean_text(text):
        return []

    chunks = re.split(r"[\n\r,;\t]+", str(text))
    results = []
    seen = set()

    for chunk in chunks:
        raw = clean_text(chunk)
        if not raw:
            continue

        extracted = extract_normalized_skus(raw)
        if not extracted:
            continue

        normalized = extracted[0]

        key = normalized.upper()
        if key in seen:
            continue
        seen.add(key)
        results.append(key)

    return results


def build_current_to_next_gen_rows(requested_skus, sku_lookup, inventory_by_sku, part_list_map=None, part_list_lookup=None):
    """Build table rows for bulk current-to-next-gen mapping.

    Reuses existing replacement logic and returns one mapped next-gen row per input SKU.
    """
    rows = []
    profile_lookup = part_list_lookup or {}
    runtime_lookup = sku_lookup or {}

    for input_sku in requested_skus:
        source = profile_lookup.get(input_sku) or runtime_lookup.get(input_sku)
        source_desc = clean_text((source or {}).get("raw_description", "")) or "Not Found"

        next_gen_sku = "Not Found"
        next_gen_desc = "Not Found"

        if source:
            mapped = find_replacements_for_sku(
                input_sku,
                runtime_lookup,
                inventory_by_sku,
                part_list_map,
                profile_lookup,
                prefer_higher_ram=False,
            )

            if mapped:
                top = mapped[0]
                next_gen_sku = clean_text(top.get("replacement_sku", "")) or "Not Found"
                next_profile = profile_lookup.get(next_gen_sku) or runtime_lookup.get(next_gen_sku) or {}
                next_gen_desc = (
                    clean_text(top.get("replacement_description", ""))
                    or clean_text(next_profile.get("raw_description", ""))
                    or "Not Found"
                )

        rows.append({
            "Input SKU": input_sku,
            "Product Description": source_desc,
            "Next Gen SKU": next_gen_sku,
            "Next Gen Product Description": next_gen_desc,
        })

    return rows


def extract_quantities(text, skus):
    result = {}
    upper_text = text.upper()

    for sku in skus:
        patterns = [
            rf'(\d+)\s+(?:UNITS?\s+OF\s+|OF\s+)?{re.escape(sku)}\b',
            rf'{re.escape(sku)}\s*(?:QTY|QUANTITY|X|=|:)\s*(\d+)\b',
            rf'{re.escape(sku)}.*?\b(?:QTY|QUANTITY|X)\s*(\d+)\b',
            rf'NEED\s+(\d+)\s+(?:UNITS?\s+OF\s+)?{re.escape(sku)}\b',
            rf'FOR\s+(\d+)\s+(?:UNITS?\s+OF\s+)?{re.escape(sku)}\b',
        ]

        qty = None
        for pattern in patterns:
            m = re.search(pattern, upper_text)
            if m:
                try:
                    qty = int(m.group(1))
                    break
                except ValueError:
                    pass

        result[sku] = qty

    return result


def infer_color(desc):
    d = desc.lower()
    if "platinum" in d or "plat" in d:
        return "Platinum"
    if "black" in d or "blk" in d or "graphite" in d:
        return "Black"
    return ""


def infer_connectivity(desc):
    d = desc.lower()
    if "5g" in d or "lte" in d:
        return "5G"
    return "WiFi"


def infer_demo(desc):
    return "demo" in desc.lower()


def infer_taa(desc):
    return "taa" in desc.lower()


def infer_generation_and_line_from_file(filename):
    lower = filename.lower()
    if "laptop 7" in lower:
        return "Surface Laptop", 7
    if "laptop 8" in lower:
        return "Surface Laptop", 8
    if "pro 11" in lower:
        return "Surface Pro", 11
    if "pro 12" in lower:
        return "Surface Pro", 12
    return "", ""


def parse_specs(desc):
    result = {
        "screen_size": "",
        "platform": "",
        "cpu_family": "",
        "cpu_tier": "",
        "ram_gb": "",
        "storage_gb": "",
        "color": infer_color(desc),
        "connectivity": infer_connectivity(desc),
        "os": "Windows 11 Pro",
        "form_factor": "",
    }

    d = desc.lower()

    if "13.8" in d or "13.8in" in d:
        result["screen_size"] = "13.8"
    elif '15in' in d or '15"' in d or '15 ' in d:
        result["screen_size"] = "15"
    elif "13in" in d or '13"' in d:
        result["screen_size"] = "13"
    elif "12in" in d or '12"' in d:
        result["screen_size"] = "12"

    if "surface laptop" in d or "laptop" in d or "lpt" in d or "lptp" in d or "lp7" in d or "lpt7" in d:
        result["form_factor"] = "Clamshell"
    if "surface pro" in d or "pro" in d:
        result["form_factor"] = "Tablet"

    if "snapdragon" in d or "snap" in d or "x plus" in d or "x elite" in d:
        result["platform"] = "ARM"
        result["cpu_family"] = "Snapdragon"
        if "x elite" in d or "elite" in d:
            result["cpu_tier"] = "X Elite"
        elif "x plus" in d or "plus" in d:
            result["cpu_tier"] = "X Plus"
        elif "plus/16/256" in d:
            result["cpu_tier"] = "Plus"
    elif "intel" in d or "ultra" in d or "u5/" in d or "u7/" in d or "ux7/" in d or "i7/" in d:
        result["platform"] = "Intel"
        result["cpu_family"] = "Core Ultra"
        if "ux7" in d or "ultra x7" in d or "u7x" in d or "cux7" in d:
            result["cpu_tier"] = "Ultra X7"
        elif "u7" in d or "ultra 7" in d or "cu7" in d or "i7" in d:
            result["cpu_tier"] = "Ultra 7"
        elif "u5" in d or "ultra 5" in d or "cu5" in d:
            result["cpu_tier"] = "Ultra 5"

    spec_match = re.search(r'[/ ](\d{1,2})/(\d{3,4}|1tb|512|256)', d)
    if spec_match:
        result["ram_gb"] = spec_match.group(1)
        storage_raw = spec_match.group(2).upper()
        if storage_raw == "1TB":
            result["storage_gb"] = "1024"
        else:
            result["storage_gb"] = storage_raw

    if not result["ram_gb"]:
        ram_match = re.search(r'[/ ](16|24|32|64)[g/]?', d)
        if ram_match:
            result["ram_gb"] = ram_match.group(1)

    if not result["storage_gb"]:
        if "1tb" in d:
            result["storage_gb"] = "1024"
        else:
            storage_match = re.search(r'/(256|512)\b', d)
            if storage_match:
                result["storage_gb"] = storage_match.group(1)

    return result


@st.cache_data
def parse_product_files():
    rows = []

    for filename in PRODUCT_FILES:
        file_path = resolve_data_file(filename)
        product_line, generation = infer_generation_and_line_from_file(filename)
        for row in read_csv_rows(file_path):
            joined = " | ".join([clean_text(c) for c in row if clean_text(c)])
            sku_match = re.search(r"\b[A-Z0-9]{3}-[0-9]{5}\b", joined.upper())
            sku = sku_match.group(0) if sku_match else None
            if not sku:
                continue

            desc = ""
            non_empty = [clean_text(c) for c in row if clean_text(c)]
            for i, val in enumerate(non_empty):
                if val == sku and i + 1 < len(non_empty):
                    desc = non_empty[i + 1]
                    break

            if not desc:
                for cell in non_empty:
                    if sku in cell and len(cell) > len(sku):
                        desc = cell
                        break

            if not desc or len(desc) < 8:
                continue

            specs = parse_specs(desc)

            item = {
                "sku": sku,
                "product_line": product_line,
                "device_type": product_line,
                "generation": generation,
                "screen_size": specs["screen_size"],
                "platform": specs["platform"],
                "cpu_family": specs["cpu_family"],
                "cpu_tier": specs["cpu_tier"],
                "ram_gb": specs["ram_gb"],
                "storage_gb": specs["storage_gb"],
                "color": specs["color"],
                "connectivity": specs["connectivity"],
                "os": specs["os"],
                "form_factor": specs["form_factor"],
                "is_demo": infer_demo(desc),
                "is_taa": infer_taa(desc),
                "status": "active",
                "raw_description": desc,
            }
            rows.append(item)

    deduped = {}
    for row in rows:
        if row["sku"] not in deduped or len(row["raw_description"]) > len(deduped[row["sku"]]["raw_description"]):
            deduped[row["sku"]] = row

    return list(deduped.values())


def parse_inventory_records(records, distributor, source_name):
    rows = []

    def row_value(row, names):
        normalized = {normalized_col_key(k): v for k, v in row.items()}
        for name in names:
            value = normalized.get(normalized_col_key(name))
            if clean_text(value):
                return value
        return ""

    if distributor == "DandH":
        for row in records:
            raw_sku = row_value(row, ["Vendor Item No", "Product Code"])
            sku = normalize_inventory_sku(raw_sku)
            if not sku:
                continue
            rows.append({
                "snapshot_date": TODAY,
                "distributor": distributor,
                "sku": sku,
                "description": clean_text(row_value(row, ["Product Name"])),
                "qty_on_hand": clean_int(row_value(row, ["Stock on Hand", "Stock On Hand", "Avail Qty"])),
                "qty_on_order": clean_int(row_value(row, ["Qty On Order", "Qty on Order"])),
                "source_file": source_name,
            })

    elif distributor == "Ingram":
        for row in records:
            sku = normalize_inventory_sku(row_value(row, ["MFR #"]))
            if not sku:
                continue
            rows.append({
                "snapshot_date": TODAY,
                "distributor": distributor,
                "sku": sku,
                "description": clean_text(row_value(row, ["Product Description"])),
                "qty_on_hand": clean_int(row_value(row, ["Ingram On-Hand"])),
                "qty_on_order": clean_int(row_value(row, ["Ingram On-Order"])),
                "source_file": source_name,
            })

    elif distributor == "Synnex":
        for row in records:
            sku = normalize_inventory_sku(row_value(row, ["MFGPart#"]))
            if not sku:
                continue
            rows.append({
                "snapshot_date": TODAY,
                "distributor": distributor,
                "sku": sku,
                "description": clean_text(row_value(row, ["Short Desc"])),
                "qty_on_hand": clean_int(row_value(row, ["Avail Qty"])),
                "qty_on_order": 0,
                "source_file": source_name,
            })

    return rows


@st.cache_data
def load_default_inventory_files():
    rows = []
    for filename, distributor in DEFAULT_INVENTORY_FILES.items():
        file_path = resolve_data_file(filename)
        records = read_inventory_file(file_path, distributor)
        rows.extend(parse_inventory_records(records, distributor, filename))
    return rows


def aggregate_inventory(inventory_rows):
    by_sku = defaultdict(list)
    for row in inventory_rows:
        by_sku[row["sku"]].append(row)
    return by_sku


def build_inventory_sku_lookup(inventory_rows):
    lookup = {}
    for row in inventory_rows:
        sku = clean_text(row.get("sku"))
        if not sku:
            continue

        description = clean_text(row.get("description"))
        existing = lookup.get(sku)
        if existing and len(existing.get("raw_description", "")) >= len(description):
            continue

        specs = parse_specs(description) if description else {
            "screen_size": "",
            "platform": "",
            "cpu_family": "",
            "cpu_tier": "",
            "ram_gb": "",
            "storage_gb": "",
            "color": "",
            "connectivity": "",
            "os": "",
            "form_factor": "",
        }

        lookup[sku] = {
            "sku": sku,
            "raw_description": description or f"SKU {sku}",
            "product_line": "Surface Pro" if "pro" in description.lower() else ("Surface Laptop" if "laptop" in description.lower() else "Surface Device"),
            "generation": "",
            "screen_size": specs["screen_size"],
            "platform": specs["platform"],
            "cpu_family": specs["cpu_family"],
            "cpu_tier": specs["cpu_tier"],
            "ram_gb": specs["ram_gb"],
            "storage_gb": specs["storage_gb"],
            "color": specs["color"],
            "connectivity": specs["connectivity"],
            "form_factor": specs["form_factor"],
            "is_demo": False,
        }

    return lookup


def infer_line_and_generation_from_text(text):
    lower = clean_text(text).lower()
    if "surface pro" in lower or "pro " in lower:
        m = re.search(r"pro\s*(\d{1,2})(?:th)?", lower)
        return "Surface Pro", m.group(1) if m else ""
    if "surface laptop" in lower or "laptop" in lower:
        m = re.search(r"laptop\s*(\d{1,2})(?:th)?", lower)
        return "Surface Laptop", m.group(1) if m else ""
    return "Surface Device", ""


def build_part_list_sku_lookup(part_list_profiles):
    lookup = {}
    for sku, profile in part_list_profiles.items():
        desc = clean_text(profile.get("raw_description"))
        sheet = clean_text(profile.get("sheet_name"))
        combined = f"{desc} {sheet}".strip()
        # Parse specs from description first to avoid sheet-level labels (e.g., "INTEL & ARM")
        # from overriding the SKU's actual platform/CPU details.
        specs = parse_specs(desc) if desc else parse_specs(combined)
        product_line, generation = infer_line_and_generation_from_text(combined)
        os_edition = "Windows 11 Pro"
        combined_lower = combined.lower()
        if "windows 11 home" in combined_lower or "win11 home" in combined_lower or "w11 home" in combined_lower:
            os_edition = "Windows 11 Home"

        lookup[sku] = {
            "sku": sku,
            "raw_description": desc or f"SKU {sku}",
            "sheet_name": sheet,
            "product_line": product_line,
            "generation": generation,
            "os": os_edition,
            "screen_size": specs["screen_size"],
            "platform": specs["platform"],
            "cpu_family": specs["cpu_family"],
            "cpu_tier": specs["cpu_tier"],
            "ram_gb": specs["ram_gb"],
            "storage_gb": specs["storage_gb"],
            "color": specs["color"],
            "connectivity": specs["connectivity"],
            "form_factor": specs["form_factor"],
            "is_demo": False,
        }

    return lookup


def summarize_stock(rows):
    return {
        "total_on_hand": sum(int(r["qty_on_hand"]) for r in rows),
        "total_on_order": sum(int(r["qty_on_order"]) for r in rows),
    }


def stock_distributor_text(stock_rows):
    distributors = sorted({
        clean_text(row.get("distributor", ""))
        for row in stock_rows
        if int(row.get("qty_on_hand", 0)) > 0 and clean_text(row.get("distributor", ""))
    })
    return ", ".join(distributors) if distributors else "N/A"


def build_lookup(master_skus):
    return {row["sku"]: row for row in master_skus}


def generation_rank(product):
    try:
        return int(product.get("generation") or 0)
    except ValueError:
        return 0


def is_current_gen_candidate(source, candidate):
    if source["product_line"] != candidate["product_line"]:
        return False
    if source["platform"] != candidate["platform"]:
        return False
    if str(candidate["is_demo"]).lower() == "true":
        return False
    if generation_rank(candidate) <= generation_rank(source):
        return False
    return True


def score_candidate(source, candidate):
    score = 0
    reasons = []

    if source["screen_size"] and source["screen_size"] == candidate["screen_size"]:
        score += 35
        reasons.append("same screen size")
    if source["cpu_tier"] and source["cpu_tier"] == candidate["cpu_tier"]:
        score += 30
        reasons.append("same CPU tier")
    elif source["cpu_tier"] and candidate["cpu_tier"]:
        src = source["cpu_tier"].replace("Ultra X7", "Ultra 7")
        dst = candidate["cpu_tier"].replace("Ultra X7", "Ultra 7")
        if src == dst:
            score += 20
            reasons.append("near CPU tier match")

    if source["ram_gb"] and source["ram_gb"] == candidate["ram_gb"]:
        score += 20
        reasons.append("same RAM")
    if source["storage_gb"] and source["storage_gb"] == candidate["storage_gb"]:
        score += 20
        reasons.append("same storage")
    if source["color"] and source["color"] == candidate["color"]:
        score += 5
        reasons.append("same color")
    if source["connectivity"] and source["connectivity"] == candidate["connectivity"]:
        score += 10
        reasons.append("same connectivity")

    return score, reasons


def score_comparable_candidate(source, candidate):
    """Score in-stock comparables using priority: CPU, RAM, SSD, screen size, color."""
    score = 0
    reasons = []

    src_specs = parse_specs(source.get("raw_description", ""))
    dst_specs = parse_specs(candidate.get("raw_description", ""))

    src_cpu_tier = clean_text(source.get("cpu_tier", "") or src_specs.get("cpu_tier", ""))
    dst_cpu_tier = clean_text(candidate.get("cpu_tier", "") or dst_specs.get("cpu_tier", ""))
    src_cpu_family = clean_text(source.get("cpu_family", "") or src_specs.get("cpu_family", ""))
    dst_cpu_family = clean_text(candidate.get("cpu_family", "") or dst_specs.get("cpu_family", ""))
    src_ram = to_int_or_zero(source.get("ram_gb") or src_specs.get("ram_gb"))
    dst_ram = to_int_or_zero(candidate.get("ram_gb") or dst_specs.get("ram_gb"))
    src_storage = to_int_or_zero(source.get("storage_gb") or src_specs.get("storage_gb"))
    dst_storage = to_int_or_zero(candidate.get("storage_gb") or dst_specs.get("storage_gb"))
    src_screen = clean_text(source.get("screen_size", "") or src_specs.get("screen_size", ""))
    dst_screen = clean_text(candidate.get("screen_size", "") or dst_specs.get("screen_size", ""))
    src_color = clean_text(source.get("color", "") or src_specs.get("color", ""))
    dst_color = clean_text(candidate.get("color", "") or dst_specs.get("color", ""))

    # 1) CPU (most important)
    src_tier = src_cpu_tier.replace("Ultra X7", "Ultra 7")
    dst_tier = dst_cpu_tier.replace("Ultra X7", "Ultra 7")
    src_family = src_cpu_family
    dst_family = dst_cpu_family
    if src_tier and dst_tier and src_tier == dst_tier:
        score += 35
        reasons.append("same CPU tier")
    elif src_family and dst_family and src_family == dst_family:
        score += 24
        reasons.append("same CPU family")
    elif src_tier and dst_tier:
        src_norm = src_tier.lower().replace("ultra x7", "ultra 7")
        dst_norm = dst_tier.lower().replace("ultra x7", "ultra 7")
        if src_norm.split()[0:1] == dst_norm.split()[0:1]:
            score += 16
            reasons.append("near CPU tier")

    # 2) RAM
    if src_ram and dst_ram:
        if src_ram == dst_ram:
            score += 25
            reasons.append("same RAM")
        elif dst_ram > src_ram:
            score += 20
            reasons.append("higher RAM")
        elif dst_ram >= max(8, int(src_ram * 0.75)):
            score += 10
            reasons.append("close RAM")

    # 3) SSD / storage
    if src_storage and dst_storage:
        if src_storage == dst_storage:
            score += 20
            reasons.append("same storage")
        elif dst_storage > src_storage:
            score += 16
            reasons.append("higher storage")
        elif dst_storage >= max(128, int(src_storage * 0.5)):
            score += 8
            reasons.append("close storage")

    # 4) Screen size
    if src_screen and dst_screen:
        if src_screen == dst_screen:
            score += 12
            reasons.append("same screen size")
        else:
            try:
                diff = abs(float(src_screen) - float(dst_screen))
            except ValueError:
                diff = None
            if diff is not None and diff <= 1.2:
                score += 7
                reasons.append("near screen size")
            elif diff is not None and diff <= 2.2:
                score += 3
                reasons.append("different screen size")

    # 5) Color (least important)
    if src_color and src_color == dst_color:
        score += 8
        reasons.append("same color")

    return min(100, score), reasons


def confidence_from_score(score):
    if score >= 95:
        return "high"
    if score >= 70:
        return "medium"
    return "low"

def is_non_device_replacement(source_sku, candidate_sku, candidate):
    """Return True when candidate appears to be a service plan/accessory, not a device replacement."""
    desc = clean_text((candidate or {}).get("raw_description", "")).lower()
    service_keywords = [
        "service plan",
        "extended service",
        "warranty",
        "microsoft complete",
        "accidental damage",
        "nbd",
        "support plan",
        "care plan",
    ]
    if any(k in desc for k in service_keywords):
        return True

    src_prefix = clean_text(source_sku).split("-")[0]
    cand_prefix = clean_text(candidate_sku).split("-")[0]
    # Keep same-family device SKU prefixes aligned when both are present.
    if src_prefix and cand_prefix and src_prefix != cand_prefix:
        return True

    return False


def is_service_plan_candidate(candidate):
    desc = clean_text((candidate or {}).get("raw_description", "")).lower()
    service_keywords = [
        "service plan",
        "extended service",
        "warranty",
        "microsoft complete",
        "accidental damage",
        "nbd",
        "support plan",
        "care plan",
        "complete for business",
        "complete business",
        "surface plus",
        "plus for business",
        "protection plan",
        "microsoft 365",
    ]
    return any(k in desc for k in service_keywords)


def extract_warranty_coverage_term(sku, description):
    """Resolve warranty coverage term, preferring explicit SKU mapping then description text."""
    normalized_sku = clean_text(sku).upper()
    mapped_term = WARRANTY_COVERAGE_BY_SKU.get(normalized_sku, "")
    if mapped_term:
        return mapped_term

    desc = clean_text(description)
    lower = desc.lower()

    years = re.findall(r"\b([1-9])\s*[- ]?year(?:s)?(?:\s*total)?\b", lower)
    if years:
        year_value = int(years[0])
        return f"{year_value} Year Total" if year_value == 1 else f"{year_value} Years Total"

    return "Not specified"


def find_compatible_warranties_for_sku(device_sku, part_list_map=None, part_list_lookup=None):
    """Return compatible warranty/service-plan SKUs for a device SKU from the part list."""
    if not device_sku or not part_list_lookup:
        return []

    source = part_list_lookup.get(device_sku, {})
    source_sheet = clean_text(source.get("sheet_name", "")).lower()
    source_desc = clean_text(source.get("raw_description", "")).lower()

    # No warranty compatibility should be suggested yet for these newly introduced lines.
    # This also prevents accidental matching of Pro 12th devices to Pro 12-inch warranty rows.
    source_hay = f"{source_desc} {source_sheet}"
    is_laptop8_or_13 = bool(re.search(r"\b(laptop\s*8|laptop8|laptop\s*13|laptop13)\b", source_hay))
    is_pro12_or_newer = bool(re.search(r"\b(pro\s*12th|12th\s*edition|surface\s+pro\s*12|pro\s*13|pro13)\b", source_hay))
    is_pro12_inch = bool(re.search(r"\b(12[-\s]*inch|12in)\b", source_hay))
    if is_laptop8_or_13 or (is_pro12_or_newer and not is_pro12_inch):
        return []

    is_pro10_or_11 = bool(re.search(r"\b(surface\s+pro\s*(10|11)|pro\s*(10|11)|pro(10|11))\b", source_hay))
    if is_pro10_or_11:
        explicit = []
        for sku in SURFACE_PRO_10_11_WARRANTY_SKUS:
            candidate = (part_list_lookup or {}).get(sku)
            if not candidate:
                continue
            desc = clean_text(candidate.get("raw_description", ""))
            explicit.append({
                "sku": sku,
                "description": desc,
                "coverage_term": extract_warranty_coverage_term(sku, desc),
            })
        if explicit:
            return explicit

    results = []
    seen = set()

    # Prefer direct source->target mappings from workbook rows.
    for mapped_sku in (part_list_map or {}).get(device_sku, []):
        candidate = (part_list_lookup or {}).get(mapped_sku)
        if not candidate:
            continue
        if not is_service_plan_candidate(candidate):
            continue
        candidate_sheet = clean_text(candidate.get("sheet_name", "")).lower()
        if source_sheet and candidate_sheet and source_sheet != candidate_sheet:
            continue
        if mapped_sku in seen:
            continue
        seen.add(mapped_sku)
        desc = clean_text(candidate.get("raw_description", ""))
        results.append({
            "sku": mapped_sku,
            "description": desc,
            "coverage_term": extract_warranty_coverage_term(mapped_sku, desc),
        })

    # Pull service-plan SKUs from the same sheet as the device.
    if source_sheet:
        source_sheet_normalized = re.sub(r"\s+", "", source_sheet)
        for cand_sku, candidate in (part_list_lookup or {}).items():
            if cand_sku == device_sku:
                continue
            if cand_sku in seen:
                continue
            candidate_sheet = clean_text(candidate.get("sheet_name", "")).lower()
            candidate_sheet_normalized = re.sub(r"\s+", "", candidate_sheet)
            if candidate_sheet != source_sheet and candidate_sheet_normalized != source_sheet_normalized:
                continue
            if not is_service_plan_candidate(candidate):
                continue
            seen.add(cand_sku)
            desc = clean_text(candidate.get("raw_description", ""))
            results.append({
                "sku": cand_sku,
                "description": desc,
                "coverage_term": extract_warranty_coverage_term(cand_sku, desc),
            })

    # Intentionally no workbook-wide fallback: compatibility is same-sheet only.

    results.sort(key=lambda x: x["sku"])
    return results


def build_warranty_section_text(device_skus, part_list_map=None, part_list_lookup=None):
    lines = []
    any_warranty = False
    for sku in device_skus:
        warranties = find_compatible_warranties_for_sku(sku, part_list_map, part_list_lookup)
        if not warranties:
            continue
        any_warranty = True
        lines.append(f"- {sku}:")
        for w in warranties:
            desc = w.get("description", "")
            coverage_term = clean_text(w.get("coverage_term", "")) or "Not specified"
            if desc:
                lines.append(f"  - {w['sku']} [{coverage_term}] ({desc})")
            else:
                lines.append(f"  - {w['sku']} [{coverage_term}]")

    if not any_warranty:
        lines.append("- No compatible warranty/service-plan SKUs found for the listed device SKUs.")
    return "\n".join(lines)


def build_warranty_section_html(device_skus, part_list_map=None, part_list_lookup=None):
    rows = []
    seen = set()
    for sku in device_skus:
        warranties = find_compatible_warranties_for_sku(sku, part_list_map, part_list_lookup)
        for w in warranties:
            key = (sku, w["sku"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                "<tr>"
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(sku))}</td>"
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(w.get('sku', '')))}</td>"
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(w.get('coverage_term', 'Not specified')))}</td>"
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(w.get('description', '')))}</td>"
                "</tr>"
            )

    if not rows:
        return "<p style=\"margin:8px 0 14px 0;\">No compatible warranty/service-plan SKUs found for the listed device SKUs.</p>"

    return (
        "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
        "<thead><tr>"
        "<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">Device SKU</th>"
        "<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">Warranty / Plan SKU</th>"
        "<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">Coverage Term</th>"
        "<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">Description</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def is_allowed_target_sheet(source_sheet_name, candidate_sheet_name):
    """Enforce strict generation-sheet transitions for replacements."""
    source_sheet = clean_text(source_sheet_name).lower()
    candidate_sheet = clean_text(candidate_sheet_name).lower()

    # Pro 10 5G is current-gen, not a next-gen target for any other devices
    if re.search(r"pro\s*10.*5g", candidate_sheet):
        return False

    # Laptop 7 -> Laptop 8 only
    if re.search(r"laptop\s*7", source_sheet):
        return bool(re.search(r"laptop\s*8", candidate_sheet))

    # Pro 11 -> Pro 12 / Pro 12th only (exclude Pro 12-inch sheets)
    if re.search(r"pro\s*11", source_sheet):
        return bool(re.search(r"pro\s*12(?![-\s]*inch)", candidate_sheet))

    return True


def _keyboard_bucket(profile):
    """Return compatibility bucket for keyboard-like SKUs (e.g., pro11, pro12, laptop) or empty."""
    hay = " ".join([
        clean_text((profile or {}).get("raw_description", "")),
        clean_text((profile or {}).get("sheet_name", "")),
        clean_text((profile or {}).get("product_line", "")),
    ]).lower()

    if not re.search(r"\b(keyboard|type\s*cover|folio|flex)\b", hay):
        return ""

    # Pro 11 keyboard family
    if re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11)\b", hay):
        return "pro11"

    # Pro 12-inch / Pro 12th / Pro 13 keyboard family
    if re.search(r"\b(surface\s+pro\s*12|pro\s*12|pro12|12th|surface\s+pro\s*13|pro\s*13|pro13|12[-\s]*inch|12in)\b", hay):
        return "pro12plus"

    if re.search(r"\b(surface\s+laptop|laptop)\b", hay):
        return "laptop"

    return "other_keyboard"


def is_keyboard_compatible_replacement(source_profile, candidate_profile):
    """Keep keyboard replacements inside the same compatibility bucket."""
    src_bucket = _keyboard_bucket(source_profile)
    dst_bucket = _keyboard_bucket(candidate_profile)
    if not src_bucket and not dst_bucket:
        return True
    if not src_bucket or not dst_bucket:
        return False
    return src_bucket == dst_bucket


def should_keep_candidate_for_source(source_profile, candidate_profile, prefer_higher_ram=False):
    """Apply strict compatibility and spec checks for replacements/comparables."""
    source = source_profile or {}
    candidate = candidate_profile or {}

    # Hard keyboard compatibility rule (e.g., Pro 11 keyboards are not compatible with Pro 12-inch family).
    if not is_keyboard_compatible_replacement(source, candidate):
        return False

    # If source is Snapdragon/ARM, never suggest non-Snapdragon replacements.
    if is_snapdragon_profile(source) and not is_snapdragon_profile(candidate):
        return False

    if source.get("product_line") and candidate.get("product_line") and source["product_line"] != candidate["product_line"]:
        return False
    if source.get("platform") and candidate.get("platform") and source["platform"] != candidate["platform"]:
        return False
    if source.get("cpu_tier") and candidate.get("cpu_tier") and source["cpu_tier"] != candidate["cpu_tier"]:
        return False
    if source.get("storage_gb") and candidate.get("storage_gb") and source["storage_gb"] != candidate["storage_gb"]:
        return False

    source_ram = to_int_or_zero(source.get("ram_gb"))
    candidate_ram = to_int_or_zero(candidate.get("ram_gb"))
    if source_ram and candidate_ram:
        if prefer_higher_ram:
            if candidate_ram < source_ram:
                return False
        elif candidate_ram != source_ram:
            return False

    return True


def find_replacements_for_sku(requested_sku, sku_lookup, inventory_by_sku, part_list_map=None, part_list_lookup=None, prefer_higher_ram=False):
    # Replacements must come from the part list only. Inventory is used for stock only.
    source = (part_list_lookup or {}).get(requested_sku)

    # 1) Prefer explicit workbook mappings first (source -> replacement SKUs from XLSX).
    if part_list_map and requested_sku in part_list_map:
        explicit = []
        seen = set()
        
        # Check if source device is 5G
        source_desc = clean_text(source.get("raw_description", "") if source else "").lower()
        source_sheet = clean_text(source.get("sheet_name", "") if source else "").lower()
        source_is_5g = bool(re.search(r"\b5g\b", source_desc)) or bool(re.search(r"\b5g\b", source_sheet))
        
        for repl_sku in part_list_map.get(requested_sku, []):
            if repl_sku in seen or repl_sku == requested_sku:
                continue
            seen.add(repl_sku)

            candidate = (part_list_lookup or {}).get(repl_sku) or {"sku": repl_sku, "raw_description": f"Mapped replacement SKU {repl_sku}"}
            if is_non_device_replacement(requested_sku, repl_sku, candidate):
                continue
            if source and not should_keep_candidate_for_source(source, candidate, prefer_higher_ram):
                continue
            if source and not is_allowed_target_sheet(source.get("sheet_name", ""), candidate.get("sheet_name", "")):
                continue
            repl_stock = summarize_stock(inventory_by_sku.get(repl_sku, []))

            if source:
                score, reasons = score_candidate(source, candidate)
            else:
                score, reasons = 0, []
            score += 100
            reasons.insert(0, "explicit workbook mapping")
            
            # Prioritize 5G candidates if source is 5G
            if source_is_5g:
                cand_desc = clean_text(candidate.get("raw_description", "")).lower()
                cand_sheet = clean_text(candidate.get("sheet_name", "")).lower()
                if bool(re.search(r"\b5g\b", cand_desc)) or bool(re.search(r"\b5g\b", cand_sheet)):
                    score += 50
                    reasons.insert(0, "5G device (matching 5G source)")

            explicit.append({
                "replacement_sku": repl_sku,
                "replacement_description": candidate.get("raw_description", f"Mapped replacement SKU {repl_sku}"),
                "replacement_generation": candidate.get("generation", ""),
                "match_confidence": "high",
                "match_score": score,
                "replacement_type": "part_list_direct_mapping",
                "reason": ", ".join(reasons),
                "available_to_purchase_date": NEXT_GEN_AVAILABLE_DATE,
                "total_on_hand": repl_stock["total_on_hand"],
                "total_on_order": repl_stock["total_on_order"],
                "mapping_source": "part_list_map",
                "replacement_ram_gb": candidate.get("ram_gb", ""),
            })

        explicit.sort(key=lambda x: (-x["match_score"], -x["total_on_hand"], -x["total_on_order"], x["replacement_sku"]))
        if explicit:
            return explicit[:3]

    # 2) Fallback to description-based inference, with sheet-aware transition rules.
    if not source:
        return []

    if part_list_lookup:
        source_ram = to_int_or_zero(source.get("ram_gb"))
        source_gen = to_int_or_zero(source.get("generation"))
        source_sheet = clean_text(source.get("sheet_name", "")).lower()
        has_strict_sheet_rule = bool(
            re.search(r"laptop\s*7", source_sheet) or re.search(r"pro\s*11", source_sheet)
        )

        # Deterministic pass: if a target sheet is known, first pick candidates that are
        # exact spec matches (platform, CPU tier, RAM, storage, color) in that target sheet.
        if has_strict_sheet_rule:
            exact_candidates = []
            
            # Check if source device is 5G
            source_desc = clean_text(source.get("raw_description", "")).lower()
            source_sheet = clean_text(source.get("sheet_name", "")).lower()
            source_is_5g = bool(re.search(r"\b5g\b", source_desc)) or bool(re.search(r"\b5g\b", source_sheet))
            
            for cand_sku, candidate in part_list_lookup.items():
                if cand_sku == requested_sku:
                    continue
                if is_non_device_replacement(requested_sku, cand_sku, candidate):
                    continue
                if not is_allowed_target_sheet(source.get("sheet_name", ""), candidate.get("sheet_name", "")):
                    continue
                if not should_keep_candidate_for_source(source, candidate, prefer_higher_ram):
                    continue
                if source.get("color") and candidate.get("color") and source["color"] != candidate["color"]:
                    continue

                score = 999
                reasons = ["exact spec match in target generation sheet"]
                
                # Boost score for 5G candidates if source is 5G
                if source_is_5g:
                    cand_desc = clean_text(candidate.get("raw_description", "")).lower()
                    cand_sheet = clean_text(candidate.get("sheet_name", "")).lower()
                    if bool(re.search(r"\b5g\b", cand_desc)) or bool(re.search(r"\b5g\b", cand_sheet)):
                        score += 50
                        reasons.insert(0, "5G device (matching 5G source)")

                repl_stock = summarize_stock(inventory_by_sku.get(cand_sku, []))
                exact_candidates.append({
                    "replacement_sku": cand_sku,
                    "replacement_description": candidate.get("raw_description", f"Mapped replacement SKU {cand_sku}"),
                    "replacement_generation": candidate.get("generation", ""),
                    "match_confidence": "high",
                    "match_score": score,
                    "replacement_type": "sheet_exact_spec_match",
                    "reason": ", ".join(reasons),
                    "available_to_purchase_date": NEXT_GEN_AVAILABLE_DATE,
                    "total_on_hand": repl_stock["total_on_hand"],
                    "total_on_order": repl_stock["total_on_order"],
                    "mapping_source": "sheet_rule_exact",
                    "replacement_ram_gb": candidate.get("ram_gb", ""),
                })

            if exact_candidates:
                exact_candidates.sort(key=lambda x: (-x["match_score"], -x["total_on_hand"], -x["total_on_order"], x["replacement_sku"]))
                return exact_candidates[:3]

        inferred = []

        # Check if source device is 5G
        source_desc = clean_text(source.get("raw_description", "")).lower()
        source_sheet = clean_text(source.get("sheet_name", "")).lower()
        source_is_5g = bool(re.search(r"\b5g\b", source_desc)) or bool(re.search(r"\b5g\b", source_sheet))

        for cand_sku, candidate in part_list_lookup.items():
            if cand_sku == requested_sku:
                continue
            if is_non_device_replacement(requested_sku, cand_sku, candidate):
                continue
            if not is_allowed_target_sheet(source.get("sheet_name", ""), candidate.get("sheet_name", "")):
                continue
            if not should_keep_candidate_for_source(source, candidate, prefer_higher_ram):
                continue

            candidate_ram = to_int_or_zero(candidate.get("ram_gb"))
            if prefer_higher_ram and source_ram and candidate_ram and candidate_ram < source_ram:
                continue

            candidate_gen = to_int_or_zero(candidate.get("generation"))
            if source_gen and candidate_gen and candidate_gen < source_gen:
                continue

            score, reasons = score_candidate(source, candidate)
            if prefer_higher_ram and source_ram and candidate_ram > source_ram:
                score += 30
                reasons.append("higher RAM")
            if source_gen and candidate_gen > source_gen:
                score += 20
                reasons.append("newer generation")
            if source.get("cpu_family") and source.get("cpu_family") == candidate.get("cpu_family"):
                score += 10
                reasons.append("same CPU family")
            
            # Prioritize 5G candidates if source is 5G
            if source_is_5g:
                cand_desc = clean_text(candidate.get("raw_description", "")).lower()
                cand_sheet = clean_text(candidate.get("sheet_name", "")).lower()
                if bool(re.search(r"\b5g\b", cand_desc)) or bool(re.search(r"\b5g\b", cand_sheet)):
                    score += 50
                    reasons.insert(0, "5G device (matching 5G source)")

            if score < 20:
                continue

            repl_stock = summarize_stock(inventory_by_sku.get(cand_sku, []))
            inferred.append({
                "replacement_sku": cand_sku,
                "replacement_description": candidate.get("raw_description", f"Mapped replacement SKU {cand_sku}"),
                "replacement_generation": candidate.get("generation", ""),
                "match_confidence": confidence_from_score(score),
                "match_score": score,
                "replacement_type": "part_list_description_match",
                "reason": ", ".join(reasons) if reasons else "matched on part-list description",
                "available_to_purchase_date": NEXT_GEN_AVAILABLE_DATE,
                "total_on_hand": repl_stock["total_on_hand"],
                "total_on_order": repl_stock["total_on_order"],
                "mapping_source": "part_list_description",
                "replacement_ram_gb": candidate.get("ram_gb", ""),
            })

        inferred.sort(key=lambda x: (-x["match_score"], -x["total_on_hand"], -x["total_on_order"], x["replacement_sku"]))
        if inferred:
            return inferred[:3]

    return []


def find_in_stock_comparables_for_sku(requested_sku, part_list_lookup, inventory_by_sku, sku_lookup=None, limit=3, allow_demo=False, allow_taa=False):
    """Return 1-3 in-stock comparable device options for a requested SKU."""
    source = (part_list_lookup or {}).get(requested_sku) or (sku_lookup or {}).get(requested_sku)
    if not source:
        return []
    # Warranty/service-plan rows are not device SKUs and should not trigger hardware comparables.
    if is_service_plan_candidate(source):
        return []
    if is_eol_profile(source):
        return []
    if not is_supported_generation_device_profile(source):
        return []

    source_query = clean_text(source.get("raw_description", ""))
    if not source_query:
        source_query = " ".join([
            clean_text(source.get("product_line", "")),
            clean_text(source.get("generation", "")),
            clean_text(source.get("screen_size", "")),
            clean_text(source.get("cpu_tier", "")),
            clean_text(source.get("ram_gb", "")),
            clean_text(source.get("storage_gb", "")),
            clean_text(source.get("color", "")),
        ]).strip()

    constraints = extract_query_constraints(source_query)
    constraints["product_line"] = clean_text(source.get("product_line", "")) or constraints.get("product_line", "")
    constraints["platform"] = clean_text(source.get("platform", "")) or constraints.get("platform", "")
    constraints["cpu_tier"] = clean_text(source.get("cpu_tier", "")) or constraints.get("cpu_tier", "")
    constraints["screen_size"] = clean_text(source.get("screen_size", "")) or constraints.get("screen_size", "")
    constraints["ram_gb"] = clean_text(source.get("ram_gb", "")) or constraints.get("ram_gb", "")
    constraints["storage_gb"] = clean_text(source.get("storage_gb", "")) or constraints.get("storage_gb", "")
    constraints["color"] = clean_text(source.get("color", "")) or constraints.get("color", "")
    constraints["os"] = clean_text(source.get("os", "")) or constraints.get("os", "")

    description_lower = clean_text(source.get("raw_description", "")).lower()
    source_line = clean_text(source.get("product_line", "")).lower()
    if "surface pro" in source_line:
        if re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11|13(?:\s*|-)?(?:in|inch|inches)|13\")\b", description_lower):
            constraints["pro_generation"] = "11"
        elif re.search(r"\b(surface\s+pro\s*12|pro\s*12|pro12|12(?:\s*|-)?(?:in|inch|inches)|12\")\b", description_lower):
            constraints["pro_generation"] = "12"
    elif "surface laptop" in source_line:
        source_gen = clean_text(source.get("generation", ""))
        if source_gen in {"7", "8", "13"}:
            constraints["laptop_generation"] = source_gen
        elif re.search(r"\b(laptop\s*7|7th\s*edition|7th\s*gen)\b", description_lower):
            constraints["laptop_generation"] = "7"
        elif re.search(r"\b(laptop\s*8|8th\s*edition|8th\s*gen)\b", description_lower):
            constraints["laptop_generation"] = "8"
        elif re.search(r"\b(laptop\s*13|13\s*inch\s*laptop)\b", description_lower):
            constraints["laptop_generation"] = "13"

    return find_in_stock_alternatives(
        source_query,
        constraints,
        part_list_lookup,
        inventory_by_sku,
        exclude_skus={requested_sku},
        limit=limit,
    )


def tokenize_query(text):
    if not text:
        return []
    text = text.lower()
    tokens = re.findall(r"[a-z0-9\.]+", text)
    stop_words = {
        "surface", "microsoft", "device", "commercial", "windows", "win11",
        "for", "the", "and", "with", "or", "please", "quote", "quoted",
        "customer", "needs", "pro"
    }
    return [t for t in tokens if t not in stop_words]


def _normalize_storage_token(storage_token):
    token = clean_text(storage_token).lower().replace(" ", "")
    if token in {"1tb", "1t"}:
        return "1024"
    return token


def _cpu_alias_to_tier(cpu_token):
    token = clean_text(cpu_token).lower().strip()
    normalized = re.sub(r"[^a-z0-9]+", "", token)

    if normalized in {"snapdragonxelite", "xelite"}:
        return "X Elite", "ARM"
    if normalized in {"snapdragonxplus", "xplus"}:
        return "X Plus", "ARM"
    if normalized in {"ultrax7", "ux7"}:
        return "Ultra X7", "Intel"
    if normalized in {"ultra7", "u7", "cu7", "i7"}:
        return "Ultra 7", "Intel"
    if normalized in {"ultra5", "u5", "cu5"}:
        return "Ultra 5", "Intel"

    return "", ""


def _extract_compact_cpu_ram_storage(text):
    separators = r"[/\\|_\-]"
    cpu_pattern = (
        r"snapdragon\s*x\s*elite|snapdragon\s*x\s*plus|"
        r"x\s*elite|x\s*plus|"
        r"ultra\s*x7|ultra\s*7|ultra\s*5|"
        r"ux7|cu7|cu5|u7|u5|i7"
    )

    patterns = [
        rf"\b(?P<cpu>{cpu_pattern})\s*{separators}\s*(?P<ram>8|16|24|32|64)\s*(?:gb|g)?\s*{separators}\s*(?P<storage>256|512|1024|1tb|1t)\b",
        rf"\b(?P<ram>8|16|24|32|64)\s*(?:gb|g)?\s*{separators}\s*(?P<storage>256|512|1024|1tb|1t)\s*{separators}\s*(?P<cpu>{cpu_pattern})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue

        cpu_tier, platform = _cpu_alias_to_tier(match.group("cpu"))
        if cpu_tier:
            return {
                "cpu_tier": cpu_tier,
                "platform": platform,
                "ram_gb": clean_text(match.group("ram")),
                "storage_gb": _normalize_storage_token(match.group("storage")),
            }

    return {}


def _collapse_bullet_text(query):
    """If query contains newlines (multi-line / bullet-point pasted spec), strip bullet markers
    and collapse all lines into a single space-separated string for downstream parsing.
    Single-line queries pass through unchanged."""
    if "\n" not in query.strip():
        return query
    parts = []
    for line in query.strip().splitlines():
        # Strip leading bullet markers: •, ·, -, *, #, numbered lists (1. 2) etc.
        line = re.sub(r"^[\s\u2022\u00b7\-\*#]+", "", line)   # bullet chars
        line = re.sub(r"^\d+[\.\)]\s*", "", line)              # numbered lists
        line = line.strip()
        if line:
            parts.append(line)
    return " ".join(parts)


def _extract_structured_query_from_ae_block(block):
    """Parse a single natural-language device description block into a clean query string.
    Handles patterns like:
      Surface Pro, Copilot+ PC, 13-inch
      • Snapdragon® X Elite (12 Core), with OLED display
      • Black
      • 16GB RAM
      • 512GB SSD
    Returns a normalized string like: Surface Pro 13" Snapdragon X Elite 16GB 512GB Black
    """
    SURFACE_COLORS = {
        "black", "platinum", "sapphire", "violet", "dune", "forest",
        "ocean", "graphite", "sandstone", "cobalt blue", "poppy red",
        "ice blue", "pink", "silver",
    }

    collapsed_block = _collapse_bullet_text(block)
    compact_specs = _extract_compact_cpu_ram_storage(clean_text(collapsed_block).lower())

    lines = []
    for raw in block.splitlines():
        # Strip bullet markers
        raw = re.sub(r"^[\s\u2022\u00b7\-\*#•]+", "", raw)
        raw = raw.strip()
        if raw:
            lines.append(raw)

    product_line = ""
    product_generation = ""
    laptop_generation = ""
    screen_size = ""
    cpu = ""
    ram = ""
    storage = ""
    color = ""
    connectivity = ""

    for line in lines:
        low = line.lower()

        # Product line
        if not product_line:
            if re.search(r"surface\s+pro", low):
                product_line = "Surface Pro"
            elif re.search(r"surface\s+laptop", low) or re.search(r"\blaptop\b", low) or re.search(r"\blp\s*(7|8|13)\b", low):
                product_line = "Surface Laptop"

        if not product_generation and product_line == "Surface Pro":
            if re.search(r"\b13(?:\s*|-)?(?:in|inch|inches)\b|\b13\"", low):
                product_generation = "11"
            elif re.search(r"\b12(?:\s*|-)?(?:in|inch|inches)\b|\b12\"", low):
                product_generation = "12"

        if not laptop_generation and product_line == "Surface Laptop":
            if re.search(r"\b(laptop\s*7|lp\s*7|7th\s*edition|7th\s*gen)\b", low):
                laptop_generation = "7"
            elif re.search(r"\b(laptop\s*8|lp\s*8|8th\s*edition|8th\s*gen)\b", low):
                laptop_generation = "8"
            elif re.search(r"\b(laptop\s*13|lp\s*13|13\s*inch\s*laptop)\b", low):
                laptop_generation = "13"

        # Screen size  (13-inch, 13.8", 15", etc.)
        if not screen_size:
            m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]?\s*inch', low)
            if not m:
                m = re.search(r'(\d+(?:\.\d+)?)\s*(?:in)\b', low)
            if not m:
                m = re.search(r'(\d+(?:\.\d+)?)["\u201d\u2033]', low)
            if m:
                screen_size = m.group(1)

        # CPU tier
        if not cpu:
            if re.search(r"snapdragon.{0,6}x\s*elite", low):
                cpu = "Snapdragon X Elite"
            elif re.search(r"snapdragon.{0,6}x\s*plus", low):
                cpu = "Snapdragon X Plus"
            elif re.search(r"snapdragon", low):
                cpu = "Snapdragon X"
            elif re.search(r"(?:intel\s+)?(?:core\s+)?ultra\s*7", low):
                cpu = "Ultra 7"
            elif re.search(r"\bi7\b", low):
                cpu = "Ultra 7"
            elif re.search(r"(?:intel\s+)?(?:core\s+)?ultra\s*5", low):
                cpu = "Ultra 5"
            else:
                m_cpu = re.search(
                    r"\b(snapdragon\s*x\s*elite|snapdragon\s*x\s*plus|x\s*elite|x\s*plus|ultra\s*x7|ultra\s*7|ultra\s*5|ux7|cu7|cu5|u7|u5|i7)\b",
                    low,
                )
                if m_cpu:
                    cpu_alias, _ = _cpu_alias_to_tier(m_cpu.group(1))
                    cpu = cpu_alias or cpu

        # Connectivity
        if not connectivity:
            if re.search(r"\b(5g|lte)\b", low):
                connectivity = "5G"

        if not cpu and compact_specs.get("cpu_tier"):
            cpu = compact_specs["cpu_tier"]

        # RAM (skip storage-size numbers 256/512/1024)
        if not ram:
            for m in re.finditer(r'\b(\d+)\s*gb\b', low):
                val = int(m.group(1))
                if val in (8, 16, 24, 32, 64):
                    ram = f"{val}GB"
                    break
        if not ram:
            m_compact_ram = re.search(r"(?:^|[/\\|_\-\s])(8|16|24|32|64)(?:$|[/\\|_\-\s])", low)
            if m_compact_ram:
                ram = f"{m_compact_ram.group(1)}GB"
        if not ram and compact_specs.get("ram_gb"):
            ram = f"{compact_specs['ram_gb']}GB"

        # Storage
        if not storage:
            m_tb = re.search(r'(\d+(?:\.\d+)?)\s*tb\b', low)
            m_gb = re.search(r'\b(256|512|1024)\s*gb\b', low)
            m_gb_ssd = re.search(r'(\d+)\s*gb\s+(?:ssd|storage)', low)
            m_1t = re.search(r'\b1t\b', low)
            if m_tb:
                tb_val = float(m_tb.group(1))
                storage = f"{int(tb_val * 1024)}GB"
            elif m_1t:
                storage = "1024GB"
            elif m_gb_ssd:
                storage = f"{m_gb_ssd.group(1)}GB"
            elif m_gb:
                storage = f"{m_gb.group(1)}GB"
        if not storage and compact_specs.get("storage_gb"):
            storage = f"{compact_specs['storage_gb']}GB"

        # Color
        if not color:
            for c in sorted(SURFACE_COLORS, key=len, reverse=True):
                if re.search(r'\b' + re.escape(c) + r'\b', low):
                    color = c.title()
                    break

    parts = []
    if product_line:
        if product_line == "Surface Pro" and product_generation:
            parts.append(f"{product_line} {product_generation}")
        elif product_line == "Surface Laptop" and laptop_generation:
            parts.append(f"{product_line} {laptop_generation}")
        else:
            parts.append(product_line)
    if screen_size:
        parts.append(f'{screen_size}"')
    if cpu:
        parts.append(cpu)
    if ram:
        parts.append(ram)
    if storage:
        parts.append(storage)
    if color:
        parts.append(color)
    if connectivity:
        parts.append(connectivity)

    # Fall back to collapsed raw text if extraction yielded nothing useful
    return " ".join(parts) if parts else collapsed_block


def normalize_ae_query(text):
    """Split a pasted AE device description (possibly a numbered multi-item list) into
    individual normalized query strings.
    Returns a list of (label, query) tuples where label is 'Item 1' / 'Item 2' etc.
    or None for single-item input.
    """
    text = text.strip()

    # Detect numbered items: split on lines that start with a digit + . or )
    # We prepend a newline so the leading item is caught by the split
    item_blocks = re.split(r'\n(?=\s*\d+[\.\)]\s)', "\n" + text)
    item_blocks = [b.strip() for b in item_blocks if b.strip()]

    has_labels = bool(item_blocks and re.match(r'\d+[\.\)]\s', item_blocks[0]))

    if not has_labels:
        # Single item (may or may not have bullets)
        normalized = _extract_structured_query_from_ae_block(text)
        return [(None, normalized)]

    results = []
    for block in item_blocks:
        m = re.match(r'^(\d+)[\.\)]\s*', block)
        label = f"Item {m.group(1)}" if m else None
        block_body = re.sub(r'^\d+[\.\)]\s*', '', block).strip()
        normalized = _extract_structured_query_from_ae_block(block_body)
        results.append((label, normalized))
    return results


def extract_query_constraints(query):
    query = _collapse_bullet_text(query)
    text = clean_text(query).lower()
    constraints = {
        "product_line": "",
        "pro_generation": "",
        "laptop_generation": "",
        "platform": "",
        "cpu_tier": "",
        "os": "",
        "screen_size": "",
        "ram_gb": "",
        "storage_gb": "",
        "color": "",
        "connectivity": "",
    }

    compact_specs = _extract_compact_cpu_ram_storage(text)
    if compact_specs:
        constraints["cpu_tier"] = compact_specs.get("cpu_tier", "")
        constraints["platform"] = compact_specs.get("platform", "")
        constraints["ram_gb"] = compact_specs.get("ram_gb", "")
        constraints["storage_gb"] = compact_specs.get("storage_gb", "")

    if "platinum" in text or "plat" in text:
        constraints["color"] = "Platinum"
    elif "black" in text or "blk" in text or "graphite" in text:
        constraints["color"] = "Black"

    # Keyboard attachment implies Surface Pro (detachable) — never a laptop
    if re.search(r"\bkeyboard\s*(?:attach(?:ment)?|cover|folio|type\s*cover)?\b", text) or \
            re.search(r"\battach(?:able|ment)\s*keyboard\b", text) or \
            "type cover" in text or "keyboard attachment" in text:
        constraints["product_line"] = "Surface Pro"
    elif re.search(r"\bsurface\s+pro\b", text):
        constraints["product_line"] = "Surface Pro"
    elif re.search(r"\bsurface\s+laptop\b", text) or re.search(r"\blaptop\b", text) or re.search(r"\blp\s*(7|8|13)\b", text):
        constraints["product_line"] = "Surface Laptop"

    # Surface Pro 13-inch SKU family could be Pro 11 or Pro 10 5G - don't force generation yet.
    # Just ensure product_line is set if not already; let screen_size constraint handle filtering.
    if re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11)\b", text):
        constraints["pro_generation"] = "11"
    elif re.search(r"\b(surface\s+pro\s*13|pro\s*13|pro13)\b", text):
        constraints["pro_generation"] = "13"
    elif re.search(r"\b(surface\s+pro\s*12|pro\s*12|pro12|pro12in|pro12inch|12th|12[-\s]*inch|12in)\b", text):
        constraints["pro_generation"] = "12"

    if re.search(r"\b(laptop\s*7|lp\s*7|7th\s*edition|7th\s*gen)\b", text):
        constraints["laptop_generation"] = "7"
    elif re.search(r"\b(laptop\s*8|lp\s*8|8th\s*edition|8th\s*gen)\b", text):
        constraints["laptop_generation"] = "8"
    elif re.search(r"\b(laptop\s*13|lp\s*13|13\s*inch\s*laptop)\b", text):
        constraints["laptop_generation"] = "13"

    if re.search(r"\b(?:windows\s*11|win11|w11)\s*pro\b", text):
        constraints["os"] = "Windows 11 Pro"
    elif re.search(r"\b(?:windows\s*11|win11|w11)\s*home\b", text):
        constraints["os"] = "Windows 11 Home"

    if re.search(r"\b(5g|lte)\b", text):
        constraints["connectivity"] = "5G"

    screen_match = re.search(r"\b(13\.8|15|13|12)\s*(?:in|inch|inches|\")?\b", text)
    if screen_match:
        constraints["screen_size"] = screen_match.group(1)

    if re.search(r"\b(x\s*elite|xelite)\b", text):
        constraints["cpu_tier"] = "X Elite"
        constraints["platform"] = "ARM"
    elif re.search(r"\b(x\s*plus|xplus)\b", text):
        constraints["cpu_tier"] = "X Plus"
        constraints["platform"] = "ARM"
    elif "ultra 5" in text or re.search(r"\b(u5|cu5)\b", text):
        constraints["cpu_tier"] = "Ultra 5"
        constraints["platform"] = "Intel"
    elif "ultra 7" in text or re.search(r"\b(u7|cu7|i7)\b", text):
        constraints["cpu_tier"] = "Ultra 7"
        constraints["platform"] = "Intel"
    elif re.search(r"\b(ultra\s*x7|ux7)\b", text):
        constraints["cpu_tier"] = "Ultra X7"
        constraints["platform"] = "Intel"

    if "intel" in text:
        constraints["platform"] = "Intel"
    elif "snapdragon" in text or "snap" in text or "arm" in text:
        constraints["platform"] = "ARM"

    ram_match = re.search(r"\b(8|16|24|32|64)\s*(?:gb|g|ram)\b", text)
    if not ram_match:
        # Compact forms like /16/512 should still be allowed, but avoid picking the
        # trailing '8' from screen sizes like 13.8 as if it were 8GB RAM.
        ram_match = re.search(r"(?<![\d.])(8|16|24|32|64)(?!\.\d)\b", text)
    if ram_match and not constraints["ram_gb"]:
        constraints["ram_gb"] = ram_match.group(1)

    storage_match = re.search(r"\b(256|512|1024|1tb|1t)\s*(?:gb|g|ssd|storage)?\b", text)
    if storage_match and not constraints["storage_gb"]:
        storage = storage_match.group(1)
        constraints["storage_gb"] = "1024" if storage in {"1tb", "1t"} else storage

    return constraints


def extract_named_accessory_terms(query):
    text = clean_text(query).lower()
    named = set()
    if re.search(r"\bflex\s*keyboard\b", text):
        named.add("flex_keyboard")
    if re.search(r"\bkeyboard\b|\btype\s*cover\b|\bfolio\b", text):
        named.add("keyboard")
    if re.search(r"\bpen\b|\bstylus\b", text):
        named.add("pen")
    if re.search(r"\bdock\b", text):
        named.add("dock")
    if re.search(r"\bmouse\b", text):
        named.add("mouse")
    if re.search(r"\bcharger\b|\bpower\s*supply\b|\bpsu\b", text):
        named.add("charger")
    if re.search(r"\bhub\b", text):
        named.add("hub")
    return named


def is_accessory_text(text):
    desc = clean_text(text).lower()
    return bool(re.search(r"\b(keyboard|kb|pen|dock|mouse|charger|hub|accessor|type\s*cover|folio)\b", desc))


def matches_requested_pro_generation(item, requested_pro_gen):
    """Return True when item matches requested Surface Pro generation intent."""
    requested = clean_text(requested_pro_gen)
    if not requested:
        return True

    hay = " ".join([
        clean_text((item or {}).get("raw_description", "")),
        clean_text((item or {}).get("description", "")),
        clean_text((item or {}).get("sheet_name", "")),
        clean_text((item or {}).get("product_line", "")),
    ]).lower()

    has_pro11 = bool(re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11)\b", hay))
    has_pro12 = bool(re.search(r"\b(surface\s+pro\s*12|pro\s*12|pro12|pro12in|pro12inch|12th|12[-\s]*inch|12in)\b", hay))
    has_pro13 = bool(re.search(r"\b(surface\s+pro\s*13|pro\s*13|pro13)\b", hay))

    if requested == "11":
        # Keep explicit Pro 11 rows and modern Copilot+/Snapdragon Surface Pro rows,
        # but never include explicit Pro 12/13 rows.
        if has_pro12 or has_pro13:
            return False
        modern_pro_signal = bool(re.search(r"\b(snapdragon|x\s*elite|x\s*plus|copilot)\b", hay))
        return has_pro11 or (("surface pro" in hay) and modern_pro_signal)

    if requested == "12":
        return has_pro12

    if requested == "13":
        return has_pro13

    return True


def calculate_sku_match_score(constraints, product):
    """Calculate a weighted percentage match score between query constraints and a product.

    Priority weights (higher = more important):
      CPU tier    = 5  (most important)
      RAM         = 4
      SSD/Storage = 3
      Screen size = 2
      Color       = 1  (least important)

    Score = (sum of matched field weights / sum of specified field weights) * 100
    Only fields that are specified in the query contribute to the denominator,
    so the score represents "how well does this SKU match what was asked for".

    Returns:
        (score_pct: int, matched_fields: list[str])
    """
    WEIGHTS = {
        "cpu_tier":   5,
        "ram_gb":     4,
        "storage_gb": 3,
        "screen_size": 2,
        "color":      1,
        "connectivity": 1,
    }

    matched_weight = 0
    total_weight = 0
    matched_fields = []

    # ── CPU tier ──────────────────────────────────────────────────────────────
    req_cpu = clean_text(constraints.get("cpu_tier", "")).lower()
    prod_cpu = clean_text(product.get("cpu_tier", "")).lower()
    if req_cpu:
        total_weight += WEIGHTS["cpu_tier"]
        if prod_cpu and req_cpu == prod_cpu:
            matched_weight += WEIGHTS["cpu_tier"]
            matched_fields.append("CPU")

    # ── RAM ───────────────────────────────────────────────────────────────────
    req_ram = constraints.get("ram_gb", "")
    prod_ram = product.get("ram_gb", "")
    if req_ram:
        total_weight += WEIGHTS["ram_gb"]
        req_ram_int = to_int_or_zero(req_ram)
        prod_ram_int = to_int_or_zero(prod_ram)
        if req_ram_int and prod_ram_int and req_ram_int == prod_ram_int:
            matched_weight += WEIGHTS["ram_gb"]
            matched_fields.append("RAM")

    # ── Storage / SSD ─────────────────────────────────────────────────────────
    req_storage = constraints.get("storage_gb", "")
    prod_storage = product.get("storage_gb", "")
    if req_storage:
        total_weight += WEIGHTS["storage_gb"]
        req_storage_int = to_int_or_zero(req_storage)
        prod_storage_int = to_int_or_zero(prod_storage)
        if req_storage_int and prod_storage_int and req_storage_int == prod_storage_int:
            matched_weight += WEIGHTS["storage_gb"]
            matched_fields.append("SSD")

    # ── Screen size ───────────────────────────────────────────────────────────
    req_screen = clean_text(constraints.get("screen_size", ""))
    prod_screen = clean_text(product.get("screen_size", ""))
    if req_screen:
        total_weight += WEIGHTS["screen_size"]
        # Prefix match handles "13" matching "13\"" / "13.8" etc.
        if prod_screen and prod_screen.startswith(req_screen):
            matched_weight += WEIGHTS["screen_size"]
            matched_fields.append("Screen")

    # ── Color ─────────────────────────────────────────────────────────────────
    req_color = clean_text(constraints.get("color", "")).lower()
    prod_color = clean_text(product.get("color", "")).lower()
    if req_color:
        total_weight += WEIGHTS["color"]
        if prod_color and req_color == prod_color:
            matched_weight += WEIGHTS["color"]
            matched_fields.append("Color")

    # ── Connectivity ──────────────────────────────────────────────────────────
    req_connectivity = clean_text(constraints.get("connectivity", "")).lower()
    prod_connectivity = clean_text(product.get("connectivity", "")).lower()
    if req_connectivity:
        total_weight += WEIGHTS["connectivity"]
        connectivity_hay = f"{prod_connectivity} {clean_text(product.get('raw_description', ''))}".lower()
        if "5g" in connectivity_hay or "lte" in connectivity_hay:
            matched_weight += WEIGHTS["connectivity"]
            matched_fields.append("Connectivity")

    if total_weight == 0:
        return 0, []

    score_pct = round((matched_weight / total_weight) * 100)
    return score_pct, matched_fields


def search_products_by_description(query, part_list_lookup, inventory_by_sku, limit=15):
    query = _collapse_bullet_text(query)
    tokens = tokenize_query(query)
    if not tokens:
        return []

    constraints = extract_query_constraints(query)
    compact_specs = _extract_compact_cpu_ram_storage(clean_text(query).lower())
    exact_bundle_mode = bool(compact_specs)
    require_exact_cpu = bool(clean_text(constraints.get("cpu_tier", "")))
    require_exact_ram = bool(clean_text(constraints.get("ram_gb", "")))
    require_exact_storage = bool(clean_text(constraints.get("storage_gb", "")))
    require_exact_color = bool(clean_text(constraints.get("color", "")))
    require_exact_screen = bool(clean_text(constraints.get("screen_size", "")))
    allow_demo = explicitly_requests_term(query, "demo")
    allow_taa = explicitly_requests_term(query, "taa")
    requested_pro_gen = clean_text(constraints.get("pro_generation", ""))
    requested_screen_size = clean_text(constraints.get("screen_size", ""))
    pro_13_is_pro11_alias = requested_pro_gen == "11" and requested_screen_size == "13"
    # 13" can match Pro 10 5G or Pro 11; allow both when 13" is requested without explicit Pro generation
    pro_13_can_match_pro10_5g = not requested_pro_gen and requested_screen_size == "13"

    requested_ram_values = {
        int(v) for v in re.findall(r"\b(8|16|24|32|64)\s*(?:gb|g|ram)?\b", clean_text(query).lower())
    }
    requested_storage_values = set()
    for s in re.findall(r"\b(256|512|1024|1tb|1t)\s*(?:gb|g|ssd|storage)?\b", clean_text(query).lower()):
        requested_storage_values.add(1024 if s in {"1tb", "1t"} else int(s))

    strict_mode = sum(bool(v) for v in [
        constraints.get("product_line"),
        constraints.get("platform"),
        constraints.get("cpu_tier"),
        constraints.get("os"),
        constraints.get("screen_size"),
        constraints.get("ram_gb"),
        constraints.get("storage_gb"),
        constraints.get("color"),
        constraints.get("connectivity"),
    ]) >= 6

    results = []

    def _description_text(item):
        return clean_text(item.get("description") or item.get("raw_description", "")).lower()

    def _is_warranty_result(item):
        desc = _description_text(item)
        return bool(re.search(r"\b(warranty|service\s*plan|extended\s*service|microsoft\s*complete|complete\s*for\s*business|accidental\s*damage|support\s*plan|care\s*plan|\badp\b)\b", desc))

    def _is_non_device_part_result(item):
        desc = _description_text(item)
        return bool(re.search(r"\b(field\s*replacement|replacement\s*part|spare\s*part|rssd|ssd\s*field\s*replacement)\b", desc))

    def _is_allowed_device_lineup(item):
        desc = _description_text(item)
        line = clean_text(item.get("product_line", "")).lower()

        laptop_ok = bool(re.search(r"\b(surface\s+laptop\s*(5g|7|8|13)|laptop\s*(5g|7|8|13)|laptop(5g|7|8|13))\b", desc))
        if laptop_ok:
            return True

        # Keep the Pro lineup constrained to Pro 11 / Pro 12th family naming.
        pro_ok = bool(re.search(
            r"\b(surface\s+pro\s*(11|12|12th)|pro\s*(11|12|12th|13)|pro(11|12|13)|pro12in|pro12inch|pro\s*12[-\s]*inch|pro\s*12in|12th\s*edition)\b",
            desc,
        ))
        if pro_ok:
            return True

        # Fallback: if the profile line explicitly indicates Surface Laptop/Pro,
        # accept it even when description text omits generation numerals.
        if "surface laptop" in line:
            return True
        if "surface pro" in line:
            return True

        return False
    for product in part_list_lookup.values():
        if not product.get("sku"):
            continue
        if is_eol_profile(product):
            continue
        if not is_supported_generation_device_profile(product):
            continue

        if _is_warranty_result(product):
            continue
        if _is_non_device_part_result(product):
            continue

        description_lower = clean_text(product.get("raw_description", "")).lower()
        if not allow_demo and (str(product.get("is_demo", "")).lower() == "true" or re.search(r"\bdemo\b", description_lower)):
            continue
        if not allow_taa and re.search(r"\btaa\b", description_lower):
            continue

        product_cpu = clean_text(product.get("cpu_tier", ""))
        product_screen = clean_text(product.get("screen_size", ""))
        product_line = clean_text(product.get("product_line", ""))
        product_os = clean_text(product.get("os", ""))

        if constraints["product_line"] and product_line:
            req_line = clean_text(constraints["product_line"]).lower()
            prod_line = clean_text(product_line).lower()
            if req_line in {"surface pro", "surface laptop"}:
                if req_line not in prod_line:
                    continue
            elif prod_line != req_line:
                continue

        if constraints["os"]:
            if strict_mode and not product_os:
                continue
            if product_os and product_os != constraints["os"]:
                continue

        if constraints["platform"]:
            req_platform = clean_text(constraints["platform"]).lower()
            prod_platform = clean_text(product.get("platform", "")).lower()
            if req_platform == "arm":
                cpu_hay = f"{product_cpu} {description_lower}".lower()
                if prod_platform not in {"arm", "snapdragon"} and not any(sig in cpu_hay for sig in ["snapdragon", "x elite", "x plus"]):
                    continue
            elif req_platform == "intel":
                if prod_platform and prod_platform != "intel":
                    continue
                if strict_mode and not prod_platform and not any(sig in description_lower for sig in ["intel", "ultra", "core"]):
                    continue
            elif prod_platform != req_platform:
                continue

        if constraints["cpu_tier"]:
            req_cpu = clean_text(constraints["cpu_tier"]).lower()
            cpu_hay = f"{product_cpu} {description_lower}".lower()
            if req_cpu == "x elite":
                if "x elite" not in cpu_hay:
                    continue
            elif req_cpu == "x plus":
                if "x plus" not in cpu_hay:
                    continue
            elif req_cpu in {"ultra 5", "ultra 7", "ultra x7"}:
                if req_cpu not in cpu_hay:
                    continue
            elif product_cpu and clean_text(product_cpu).lower() != req_cpu:
                continue
            elif strict_mode and not product_cpu:
                continue

        if constraints["screen_size"] and product_screen:
            req_screen = clean_text(constraints["screen_size"])
            prod_screen = clean_text(product_screen)
            if req_screen and prod_screen and not prod_screen.startswith(req_screen):
                # Allow Pro 11 / Pro 10 5G when 13" is requested
                is_pro10_5g = bool(re.search(r"\bpro\s*10.*5g\b", description_lower))
                if not (pro_13_is_pro11_alias and matches_requested_pro_generation(product, "11")) and \
                   not (pro_13_can_match_pro10_5g and is_pro10_5g):
                    continue
        elif constraints["screen_size"] and strict_mode:
            if constraints["screen_size"] not in description_lower:
                # Allow Pro 11 / Pro 10 5G when 13" is requested
                is_pro10_5g = bool(re.search(r"\bpro\s*10.*5g\b", description_lower))
                if not (pro_13_is_pro11_alias and matches_requested_pro_generation(product, "11")) and \
                   not (pro_13_can_match_pro10_5g and is_pro10_5g):
                    continue

        requested_ram_gb = to_int_or_zero(constraints.get("ram_gb"))
        product_ram_gb = to_int_or_zero(product.get("ram_gb"))

        product_storage_gb = to_int_or_zero(product.get("storage_gb"))

        if exact_bundle_mode or require_exact_ram or require_exact_storage or require_exact_color or require_exact_screen:
            requested_storage_gb = to_int_or_zero(constraints.get("storage_gb"))
            if require_exact_ram and requested_ram_gb and product_ram_gb != requested_ram_gb:
                continue
            if require_exact_storage and requested_storage_gb and product_storage_gb != requested_storage_gb:
                continue

            req_color = clean_text(constraints.get("color", "")).lower()
            prod_color = clean_text(product.get("color", "")).lower()
            if require_exact_color and req_color and prod_color != req_color:
                continue

            req_screen = clean_text(constraints.get("screen_size", ""))
            prod_screen = clean_text(product.get("screen_size", ""))
            if require_exact_screen and req_screen and (not prod_screen or not prod_screen.startswith(req_screen)):
                # Allow Pro 11 / Pro 10 5G when 13" is requested
                is_pro10_5g = bool(re.search(r"\bpro\s*10.*5g\b", description_lower))
                if not (pro_13_is_pro11_alias and matches_requested_pro_generation(product, "11")) and \
                   not (pro_13_can_match_pro10_5g and is_pro10_5g):
                    continue

        if requested_pro_gen and not matches_requested_pro_generation(product, requested_pro_gen):
            continue

        requested_laptop_gen = constraints.get("laptop_generation", "")
        if requested_laptop_gen:
            laptop_hay = f"{clean_text(product.get('product_line', ''))} {description_lower}".lower()
            if not re.search(rf"\b(laptop\s*{requested_laptop_gen}|{requested_laptop_gen}th\s*edition|{requested_laptop_gen}th\s*gen)\b", laptop_hay):
                continue

        if constraints["platform"] == "Intel" and ("snapdragon" in description_lower or " snap" in f" {description_lower}"):
            continue

        if constraints["connectivity"] == "5G":
            connectivity_hay = f"{clean_text(product.get('connectivity', ''))} {description_lower}"
            if not ("5g" in connectivity_hay or "lte" in connectivity_hay):
                continue

        match_pct, matched_fields = calculate_sku_match_score(constraints, product)

        stock = summarize_stock(inventory_by_sku.get(product["sku"], []))

        # Require at least one specified field to match (or no fields specified).
        if match_pct == 0 and any(constraints.get(f) for f in ("cpu_tier", "ram_gb", "storage_gb", "screen_size", "color", "connectivity")):
            continue

        results.append({
            "sku": product["sku"],
            "description": product["raw_description"],
            "generation": product["generation"],
            "product_line": product["product_line"],
            "os": product_os,
            "screen_size": product["screen_size"],
            "platform": product["platform"],
            "cpu_tier": product["cpu_tier"],
            "cpu_family": product.get("cpu_family", ""),
            "ram_gb": product["ram_gb"],
            "storage_gb": product["storage_gb"],
            "color": product["color"],
            "connectivity": product["connectivity"],
            "score": match_pct,
            "match_pct": match_pct,
            "matched_tokens": ", ".join(matched_fields) if matched_fields else "product line",
            "total_on_hand": stock["total_on_hand"],
            "total_on_order": stock["total_on_order"],
        })

    # Fallback for long/mixed email queries: if strict constraints produce no hits,
    # run a relaxed pass so we still return best available device matches.
    if not results and not strict_mode and not exact_bundle_mode:
        query_lower = clean_text(query).lower()
        for product in part_list_lookup.values():
            if not product.get("sku"):
                continue
            if is_eol_profile(product):
                continue
            if not is_supported_generation_device_profile(product):
                continue

            if _is_warranty_result(product):
                continue
            if _is_non_device_part_result(product):
                continue

            description_lower = clean_text(product.get("raw_description", "")).lower()
            if not allow_demo and (str(product.get("is_demo", "")).lower() == "true" or re.search(r"\bdemo\b", description_lower)):
                continue
            if not allow_taa and re.search(r"\btaa\b", description_lower):
                continue

            requested_pro_gen = constraints.get("pro_generation", "")
            if requested_pro_gen and not matches_requested_pro_generation(product, requested_pro_gen):
                continue

            if constraints["platform"] == "ARM":
                if not (is_snapdragon_profile(product) or "x elite" in description_lower or "x plus" in description_lower):
                    continue

            match_pct, matched_fields = calculate_sku_match_score(constraints, product)

            stock = summarize_stock(inventory_by_sku.get(product["sku"], []))

            if match_pct > 0 or any(not constraints.get(f) for f in ("cpu_tier", "ram_gb", "storage_gb", "screen_size", "color")):
                results.append({
                    "sku": product["sku"],
                    "description": product["raw_description"],
                    "generation": product["generation"],
                    "product_line": product["product_line"],
                    "os": clean_text(product.get("os", "")),
                    "screen_size": product["screen_size"],
                    "platform": product["platform"],
                    "cpu_tier": product["cpu_tier"],
                    "cpu_family": product.get("cpu_family", ""),
                    "ram_gb": product["ram_gb"],
                    "storage_gb": product["storage_gb"],
                    "color": product["color"],
                    "connectivity": product["connectivity"],
                    "score": match_pct,
                    "match_pct": match_pct,
                    "matched_tokens": ", ".join(matched_fields) if matched_fields else "product line",
                    "total_on_hand": stock["total_on_hand"],
                    "total_on_order": stock["total_on_order"],
                })

    results.sort(key=lambda x: (-x["score"], -x["total_on_hand"], -x["total_on_order"], x["sku"]))

    # Device-only behavior for Description Search tab.
    results = [
        item
        for item in results
        if (not is_accessory_text(item.get("description") or item.get("raw_description", ""))) and _is_allowed_device_lineup(item)
    ]

    # SKU-only searches can produce broad zero-score matches; treat those as
    # no exact match so the in-stock alternatives fallback can render instead.
    if extract_skus(query) and results and max(int(item.get("score", 0)) for item in results) <= 0:
        return []

    return results[:limit]


def search_accessories_by_description(query, part_list_lookup, inventory_by_sku, limit=20):
    tokens = tokenize_query(query)
    allow_demo = explicitly_requests_term(query, "demo")
    allow_taa = explicitly_requests_term(query, "taa")
    constraints = extract_query_constraints(query)
    named_accessories = extract_named_accessory_terms(query)
    query_lower = clean_text(query).lower()

    if not tokens and not named_accessories:
        return []

    def _accessory_category(desc_lower):
        if re.search(r"\b(keyboard|kb|type\s*cover|folio|flex)\b", desc_lower):
            return "keyboard"
        if re.search(r"\bdock\b|\busb4\b", desc_lower):
            return "dock"
        if re.search(r"\bmouse\b|\barc\b", desc_lower):
            return "mouse"
        if re.search(r"\bhub\b", desc_lower):
            return "hub"
        if re.search(r"\bpen\b|\bstylus\b", desc_lower):
            return "pen"
        if re.search(r"\bcharger\b|\bpower\s*supply\b|\bpsu\b", desc_lower):
            return "charger"
        return "other"

    def _keyboard_generation_compatible(desc_lower, requested_pro_gen):
        if not requested_pro_gen:
            return True

        has_pro11 = bool(re.search(r"\b(surface\s+pro\s*11|pro\s*11|pro11)\b", desc_lower))
        has_pro12 = bool(re.search(r"\b(surface\s+pro\s*12|pro\s*12|pro12|12th|12[-\s]*inch|12in)\b", desc_lower))
        has_pro13 = bool(re.search(r"\b(surface\s+pro\s*13|pro\s*13|pro13|13[-\s]*inch|13in)\b", desc_lower))
        is_flex = bool(re.search(r"\bflex\b", desc_lower))

        # Business rule: Pro 11 requests should not return explicit Pro 12-inch keyboards,
        # but should allow compatible Flex Keyboard marketed as 13-inch.
        if requested_pro_gen == "11":
            if has_pro12:
                return False
            if has_pro11:
                return True
            if has_pro13 and is_flex:
                return True
            return not (has_pro12 or has_pro13)

        if requested_pro_gen == "12":
            return has_pro12
        if requested_pro_gen == "13":
            return has_pro13
        return True

    results = []
    for product in part_list_lookup.values():
        sku = clean_text(product.get("sku", ""))
        if not sku:
            continue
        if sku in EOL_ACCESSORY_SKUS:
            continue

        description = clean_text(product.get("raw_description", ""))
        if not is_accessory_text(description):
            continue

        description_lower = description.lower()
        category = _accessory_category(description_lower)
        if not allow_demo and (str(product.get("is_demo", "")).lower() == "true" or re.search(r"\bdemo\b", description_lower)):
            continue
        if not allow_taa and re.search(r"\btaa\b", description_lower):
            continue

        requested_pro_gen = constraints.get("pro_generation", "")
        if category == "keyboard" and not _keyboard_generation_compatible(description_lower, requested_pro_gen):
            continue

        if named_accessories:
            # Keep only requested accessory categories when request names them.
            wants_keyboard = "keyboard" in named_accessories or "flex_keyboard" in named_accessories
            requested_categories = set(named_accessories)
            requested_categories.discard("flex_keyboard")
            if wants_keyboard:
                requested_categories.add("keyboard")
            # "Flex Keyboard with Slim Pen" should prefer keyboard bundles, not standalone pens.
            if "flex_keyboard" in named_accessories and "keyboard" in requested_categories and "pen" in requested_categories:
                requested_categories.discard("pen")

            if category not in requested_categories:
                continue

            if "flex_keyboard" in named_accessories and category == "keyboard":
                if not re.search(r"\bflex\b", description_lower):
                    continue

        haystack = " ".join([
            sku,
            clean_text(product.get("product_line", "")),
            description,
            clean_text(product.get("color", "")),
            clean_text(product.get("connectivity", "")),
        ]).lower()

        score = 0
        matched = []
        for token in tokens:
            if token in haystack:
                score += 12
                matched.append(token)

        if named_accessories and category in {"keyboard", "dock", "mouse", "hub", "pen", "charger"}:
            score += 25
            matched.append(f"{category} requested")

        # Phrase-level boosts for common requested accessory names.
        if "usb4 dock" in query_lower and re.search(r"\busb4\b|\bdock\b", description_lower):
            score += 30
            matched.append("usb4 dock")
        if "arc mouse" in query_lower and re.search(r"\barc\b.*\bmouse\b|\bmouse\b.*\barc\b", description_lower):
            score += 30
            matched.append("arc mouse")
        if "flex keyboard" in query_lower and category == "keyboard" and "flex" in description_lower:
            score += 30
            matched.append("flex keyboard")

        stock = summarize_stock(inventory_by_sku.get(sku, []))
        if stock["total_on_hand"] > 0:
            score += 5

        if score > 0:
            results.append({
                "sku": sku,
                "description": description,
                "score": score,
                "matched_tokens": ", ".join(matched),
                "total_on_hand": stock["total_on_hand"],
                "total_on_order": stock["total_on_order"],
            })

    # Fallback: if nothing scored (common with long emails), include best category matches.
    if not results and named_accessories:
        for product in part_list_lookup.values():
            sku = clean_text(product.get("sku", ""))
            if not sku:
                continue
            if sku in EOL_ACCESSORY_SKUS:
                continue
            description = clean_text(product.get("raw_description", ""))
            if not is_accessory_text(description):
                continue
            description_lower = description.lower()
            category = _accessory_category(description_lower)

            wants_keyboard = "keyboard" in named_accessories or "flex_keyboard" in named_accessories
            requested_categories = set(named_accessories)
            requested_categories.discard("flex_keyboard")
            if wants_keyboard:
                requested_categories.add("keyboard")
            if "flex_keyboard" in named_accessories and "keyboard" in requested_categories and "pen" in requested_categories:
                requested_categories.discard("pen")
            if category not in requested_categories:
                continue

            if category == "keyboard" and not _keyboard_generation_compatible(description_lower, constraints.get("pro_generation", "")):
                continue
            if "flex_keyboard" in named_accessories and category == "keyboard" and "flex" not in description_lower:
                continue

            stock = summarize_stock(inventory_by_sku.get(sku, []))
            results.append({
                "sku": sku,
                "description": description,
                "score": 1 + (5 if stock["total_on_hand"] > 0 else 0),
                "matched_tokens": "fallback category match",
                "total_on_hand": stock["total_on_hand"],
                "total_on_order": stock["total_on_order"],
            })

    results.sort(key=lambda x: (-x["score"], -x["total_on_hand"], -x["total_on_order"], x["sku"]))
    return results[:limit]


@st.cache_data
def load_accessory_matcher_rules(matcher_file_path=None, cache_key=None):
    """Load accessory matcher reference rows from Accessory Matcher.xlsx.

    Expected schema:
    - Reference table rows contain SKU in Column A
    - Column B: Product name/description
    - Column D: comma-separated keywords
    - Column E: base weight
    - Optional keyword priority table with columns: Keyword, Priority Level, Multiplier
    """
    # cache_key is intentionally unused in function body; it exists so callers can
    # invalidate Streamlit cache when the workbook timestamp changes.
    path = Path(matcher_file_path) if matcher_file_path else resolve_data_file("Accessory Matcher.xlsx")
    if not path.exists():
        return [], f"Accessory matcher file not found: {path}"

    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
    except PermissionError:
        return [], f"Accessory matcher file is locked: {path}"
    except Exception as exc:
        return [], f"Unable to read accessory matcher file: {exc}"

    rules = []
    for row_idx in range(1, ws.max_row + 1):
        raw_sku = clean_text(ws.cell(row=row_idx, column=1).value)
        sku_candidates = extract_normalized_skus(raw_sku)
        sku = sku_candidates[0] if sku_candidates else normalize_sku_text(raw_sku)
        if not re.fullmatch(r"[A-Z0-9]{3}-[0-9]{5}", sku):
            continue

        product_name = clean_text(ws.cell(row=row_idx, column=2).value)
        keywords_raw = clean_text(ws.cell(row=row_idx, column=4).value)
        base_weight = clean_int(ws.cell(row=row_idx, column=5).value)

        keywords = [
            clean_text(k).lower()
            for k in re.split(r"[,;\n|]", keywords_raw)
            if clean_text(k)
        ]
        if not keywords:
            continue

        rules.append({
            "sku": sku,
            "product_name": product_name or f"SKU {sku}",
            "keywords": keywords,
            "base_weight": max(1, base_weight),
        })

    keyword_multipliers = {}
    header_row = None
    for row_idx in range(1, ws.max_row + 1):
        c1 = clean_text(ws.cell(row=row_idx, column=1).value).lower()
        c3 = clean_text(ws.cell(row=row_idx, column=3).value).lower()
        if c1 == "keyword" and c3 == "multiplier":
            header_row = row_idx
            break

    if header_row:
        for row_idx in range(header_row + 1, ws.max_row + 1):
            keyword = clean_text(ws.cell(row=row_idx, column=1).value).lower()
            if not keyword:
                break
            mult_raw = clean_text(ws.cell(row=row_idx, column=3).value).lower().replace("×", "x")
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", mult_raw)
            multiplier = float(m.group(1)) if m else 1.0
            keyword_multipliers[keyword] = max(0.0, multiplier)

    return rules, keyword_multipliers, f"Accessory matcher loaded: {len(rules)} rule(s) from {path.name}"


def _keyword_priority_multiplier(keyword, keyword_multipliers=None):
    k = clean_text(keyword).lower()
    if keyword_multipliers and k in keyword_multipliers:
        return keyword_multipliers[k]

    highest_priority = {
        "thunderbolt",
    }
    high_priority = {
        "flex", "slim", "bold", "pro 11", "dock",
    }

    if k in highest_priority:
        return 3
    if k in high_priority:
        return 2
    return 1


def _keyword_in_query(query_lower, keyword):
    k = clean_text(keyword).lower()
    if not k:
        return False

    # Use word/phrase boundaries for robust, case-insensitive matching.
    phrase = r"\s+".join(re.escape(part) for part in re.split(r"\s+", k) if part)
    return bool(re.search(rf"\b{phrase}\b", query_lower))


def search_accessories_by_matcher(query, part_list_lookup, inventory_by_sku, limit=9):
    """Accessory lookup algorithm driven by Accessory Matcher.xlsx rules.

    Score = sum(keyword_match * base_weight * keyword_multiplier)
    Match % uses a fixed 25-point confidence scale.
    """
    query_lower = clean_text(query).lower()
    if not query_lower:
        return [], "Please enter accessory keywords to search."

    matcher_path = resolve_data_file("Accessory Matcher.xlsx")
    cache_key = None
    try:
        cache_key = matcher_path.stat().st_mtime_ns
    except Exception:
        cache_key = None

    rules, keyword_multipliers, status = load_accessory_matcher_rules(
        matcher_file_path=matcher_path,
        cache_key=cache_key,
    )
    if not rules:
        # Graceful fallback to legacy behavior when matcher workbook is unavailable.
        fallback = search_accessories_by_description(query, part_list_lookup, inventory_by_sku, limit=max(9, limit))
        normalized_fallback = []
        for item in fallback:
            item["match_pct"] = item.get("match_pct", round((min(int(item.get("score", 0)), 25) / 25) * 100))
            if not clean_text(item.get("distributor_stock", "")):
                item["distributor_stock"] = stock_distributor_text(inventory_by_sku.get(clean_text(item.get("sku", "")), []))
            if int(item.get("match_pct", 0)) > 0:
                normalized_fallback.append(item)

        normalized_fallback.sort(key=lambda x: (-x.get("score", 0), -x.get("total_on_hand", 0), x.get("sku", "")))
        trimmed = normalized_fallback[: max(1, min(limit, 9))]
        for idx, item in enumerate(trimmed, start=1):
            item["unique_rank"] = idx
        return trimmed, status

    ranked = []
    for rule in rules:
        sku = rule["sku"]
        if sku in EOL_ACCESSORY_SKUS:
            continue

        score = 0
        matched_terms = []
        for keyword in rule["keywords"]:
            if _keyword_in_query(query_lower, keyword):
                points = rule["base_weight"] * _keyword_priority_multiplier(keyword, keyword_multipliers)
                score += points
                matched_terms.append(keyword)

        if score <= 0:
            continue

        stock_rows = inventory_by_sku.get(sku, [])
        stock = summarize_stock(stock_rows)
        profile_desc = clean_text((part_list_lookup or {}).get(sku, {}).get("raw_description", ""))
        description = profile_desc or rule.get("product_name", "")

        # Fixed confidence scale from algorithm spec.
        match_pct = round((min(score, 25) / 25) * 100)

        ranked.append({
            "sku": sku,
            "description": description,
            "score": score,
            "match_pct": match_pct,
            "matched_tokens": ", ".join(matched_terms),
            "base_weight": rule["base_weight"],
            "total_on_hand": stock["total_on_hand"],
            "total_on_order": stock["total_on_order"],
            "distributor_stock": stock_distributor_text(stock_rows),
        })

    ranked.sort(key=lambda x: (-x["score"], -x["total_on_hand"], x["sku"]))
    qualified = [r for r in ranked if int(r.get("match_pct", 0)) > 0]
    top = qualified[: max(1, min(limit, 9))]
    for idx, item in enumerate(top, start=1):
        item["unique_rank"] = idx
    return top, status


def build_description_search_html_table(results):
    headers = [
        "SKU",
        "Description",
        "Device Family",
        "OS",
        "Screen Size",
        "Platform",
        "CPU",
        "RAM",
        "Storage",
        "On Hand",
        "Match",
    ]

    header_html = "".join([
        f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{html.escape(h)}</th>"
        for h in headers
    ])

    row_html = []
    for item in results:
        row = [
            item.get("sku", ""),
            item.get("description", ""),
            item.get("product_line", ""),
            item.get("os", ""),
            item.get("screen_size", ""),
            item.get("platform", ""),
            item.get("cpu_tier", ""),
            gb_text(item.get("ram_gb", "")),
            gb_text(item.get("storage_gb", "")),
            str(item.get("total_on_hand", 0)),
            f"{item.get('match_pct', item.get('score', 0))}% ({item.get('matched_tokens', '')})",
        ]
        cells = "".join([
            f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
            for cell in row
        ])
        row_html.append(f"<tr>{cells}</tr>")

    return (
        "<div style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#1f2937;\">"
        "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:0;\">"
        "Description Search Results"
        "</div>"
        "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(row_html)}</tbody>"
        "</table>"
        "</div>"
    )


def build_accessory_search_email_html(query, results):
    headers = ["Rank", "SKU", "Product Description", "On Hand", "On Order", "Disti(s)", "Score", "Match"]

    header_html = "".join([
        f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{html.escape(h)}</th>"
        for h in headers
    ])

    def _category_for_sku(sku):
        normalized = normalize_sku_text(sku)
        if normalized in ACCESSORY_DOCK_SKUS:
            return "Dock"
        if normalized in ACCESSORY_KEYBOARD_SKUS:
            return "Keyboard"
        if normalized in ACCESSORY_PEN_SKUS:
            return "Pen and Pen Accessories"
        if normalized in ACCESSORY_MISC_SKUS:
            return "Misc"
        return "Misc"

    category_order = [
        "Dock",
        "Keyboard",
        "Pen and Pen Accessories",
        "Misc",
    ]
    categorized = {cat: [] for cat in category_order}
    for item in results:
        categorized[_category_for_sku(item.get("sku", ""))].append(item)

    category_sections = []
    for category in category_order:
        items = categorized.get(category, [])
        if not items:
            continue

        row_html = []
        for item in items:
            row = [
                str(item.get("unique_rank", "")),
                item.get("sku", ""),
                item.get("description", ""),
                str(item.get("total_on_hand", 0)),
                str(item.get("total_on_order", 0)),
                item.get("distributor_stock", "N/A"),
                str(item.get("score", 0)),
                f"{item.get('match_pct', item.get('score', 0))}% ({item.get('matched_tokens', '')})",
            ]
            cells = "".join([
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
                for cell in row
            ])
            row_html.append(f"<tr>{cells}</tr>")

        category_sections.append(
            "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:12px 0 0 0;\">"
            + html.escape(category)
            + "</div>"
            + "<table style=\"border-collapse:collapse;width:100%;font-size:13px;\">"
            + f"<thead><tr>{header_html}</tr></thead>"
            + f"<tbody>{''.join(row_html)}</tbody>"
            + "</table>"
        )

    return (
        "<div style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#1f2937;line-height:1.35;\">"
        "<p style=\"margin:0 0 10px 0;\">Hey,</p>"
        f"<p style=\"margin:0 0 10px 0;\">I searched for accessories related to '{html.escape(clean_text(query))}' and pulled the best matches below.</p>"
        f"{''.join(category_sections)}"
        "<p style=\"margin:10px 0 0 0;\">Regards,</p>"
        "</div>"
    )


def find_in_stock_alternatives(query, constraints, part_list_lookup, inventory_by_sku, exclude_skus=None, limit=3):
    """Find in-stock alternatives when primary results are out of stock.
    
    Uses looser criteria than primary search to find comparable products that are in stock.
    Ranks by spec similarity within the same product line and platform.
    """
    if exclude_skus is None:
        exclude_skus = set()

    allow_demo = explicitly_requests_term(query, "demo")
    allow_taa = explicitly_requests_term(query, "taa")

    # If the query contains a SKU, seed missing constraints from the matched
    # source profile so direct SKU lookups can still produce useful alternatives.
    source_skus = extract_skus(query)
    if source_skus:
        source = (part_list_lookup or {}).get(source_skus[0])
        if source:
            source_query = clean_text(source.get("raw_description", ""))
            source_constraints = extract_query_constraints(source_query or "")
            for key in ("product_line", "platform", "cpu_tier", "screen_size", "ram_gb", "storage_gb", "color", "os"):
                if not constraints.get(key):
                    constraints[key] = clean_text(source.get(key, "")) or source_constraints.get(key, "")
    
    # Collect alternative candidates with stock
    candidates = []
    for product in part_list_lookup.values():
        if not product.get("sku"):
            continue
        if is_eol_profile(product):
            continue
        if not is_supported_generation_device_profile(product):
            continue
        
        sku = product["sku"]
        if sku in exclude_skus:
            continue

        desc_lower = clean_text(product.get("raw_description", "")).lower()
        if not allow_demo and (str(product.get("is_demo", "")).lower() == "true" or re.search(r"\bdemo\b", desc_lower)):
            continue
        if not allow_taa and re.search(r"\btaa\b", desc_lower):
            continue
        
        # Check if in stock
        stock_rows = inventory_by_sku.get(sku, [])
        stock = summarize_stock(stock_rows)
        if stock["total_on_hand"] <= 0:
            continue

        in_stock_distributors = sorted({
            clean_text(row.get("distributor", ""))
            for row in stock_rows
            if int(row.get("qty_on_hand", 0)) > 0 and clean_text(row.get("distributor", ""))
        })
        distributor_stock = ", ".join(in_stock_distributors) if in_stock_distributors else "N/A"
        
        # Must match product line and platform (keep in same family)
        product_line = clean_text(product.get("product_line", ""))
        platform = product.get("platform", "")
        
        if constraints.get("product_line") and product_line and product_line != constraints["product_line"]:
            continue
        
        if constraints.get("platform") and platform and platform != constraints["platform"]:
            continue

        # Honour laptop generation constraint so alternatives don't cross device generations
        requested_laptop_gen = constraints.get("laptop_generation", "")
        if requested_laptop_gen:
            laptop_hay = f"{product_line} {desc_lower}".lower()
            if not re.search(rf"\b(laptop\s*{requested_laptop_gen}|{requested_laptop_gen}th\s*edition|{requested_laptop_gen}th\s*gen)\b", laptop_hay):
                continue
        
        # Score based on weighted match algorithm (same weights as primary search)
        match_pct, matched_fields = calculate_sku_match_score(constraints, product)

        # For alternatives we also give partial credit for "at least as good" specs
        bonus = 0
        if constraints.get("ram_gb"):
            req_ram_int = to_int_or_zero(constraints["ram_gb"])
            prod_ram_int = to_int_or_zero(product.get("ram_gb", ""))
            if prod_ram_int > req_ram_int > 0:
                bonus += 5  # Higher RAM than requested is a plus
        if constraints.get("storage_gb"):
            req_storage_int = to_int_or_zero(constraints["storage_gb"])
            prod_storage_int = to_int_or_zero(product.get("storage_gb", ""))
            if prod_storage_int > req_storage_int > 0:
                bonus += 3  # Higher storage than requested is a plus

        # Prefer products with more stock
        stock_bonus = min(stock["total_on_hand"], 10)

        combined_score = match_pct + bonus + stock_bonus

        if combined_score > 0:
            candidates.append({
                "sku": sku,
                "description": clean_text(product.get("raw_description", "")),
                "product_line": product_line,
                "os": clean_text(product.get("os", "")),
                "screen_size": product.get("screen_size", ""),
                "platform": platform,
                "cpu_tier": product.get("cpu_tier", ""),
                "cpu_family": product.get("cpu_family", ""),
                "ram_gb": product.get("ram_gb", ""),
                "storage_gb": product.get("storage_gb", ""),
                "color": product.get("color", ""),
                "connectivity": product.get("connectivity", ""),
                "score": match_pct,
                "match_pct": match_pct,
                "matched_tokens": ", ".join(matched_fields) if matched_fields else "product line",
                "_sort_score": combined_score,
                "total_on_hand": stock["total_on_hand"],
                "total_on_order": stock["total_on_order"],
                "distributor_stock": distributor_stock,
            })
    
    # Sort: when RAM is specified, prioritise closest RAM ≤ requested (highest below first),
    # then lowest RAM above requested.  Within each RAM tier, rank by match score then stock.
    req_ram_for_sort = to_int_or_zero(constraints.get("ram_gb", ""))

    def _ram_sort_key(prod_ram_raw):
        prod_ram = to_int_or_zero(prod_ram_raw)
        if req_ram_for_sort == 0 or prod_ram == 0:
            return (0, 0)
        if prod_ram <= req_ram_for_sort:
            # Prefer highest RAM that doesn't exceed request (sort descending → negate)
            return (0, -prod_ram)
        else:
            # Above requested: deprioritized, prefer lowest overshoot
            return (1, prod_ram)

    if req_ram_for_sort > 0:
        # RAM proximity is the primary tiebreaker when RAM was specified
        candidates.sort(key=lambda x: (
            _ram_sort_key(x.get("ram_gb", "")),
            -x["_sort_score"],
            -x["total_on_hand"],
        ))
    else:
        candidates.sort(key=lambda x: (
            -x["_sort_score"],
            -x["total_on_hand"],
        ))
    return candidates[:limit]


def build_description_search_email_notes(query, results, constraints, part_list_lookup=None, inventory_by_sku=None):
    notes = []
    missing_specs = []
    query_lower = clean_text(query).lower()

    cpu_specified = bool(constraints.get("cpu_tier")) or bool(re.search(
        r"\b(snapdragon|x\s*elite|x\s*plus|xelite|xplus|intel|core\s*ultra|ultra\s*[57]|ultra\s*x7|\bu5\b|\bu7\b|\bcu5\b|\bcu7\b|\bux7\b)\b",
        query_lower,
    ))

    if not cpu_specified:
        missing_specs.append("CPU")
    if not constraints.get("ram_gb"):
        missing_specs.append("RAM")
    if not constraints.get("storage_gb"):
        missing_specs.append("SSD")
    if not constraints.get("screen_size"):
        missing_specs.append("screen size")
    if not constraints.get("color"):
        missing_specs.append("color")

    if missing_specs:
        notes.append("Assumption: Because " + ", ".join(missing_specs) + " were not specified, I included all matching options for those attributes.")

    if any((item.get("platform", "").lower() == "arm") or ("snapdragon" in item.get("cpu_family", "").lower()) or ("snapdragon" in item.get("cpu_tier", "").lower()) for item in results):
        notes.append("Snapdragon note: Some results include Snapdragon-based devices. Please test them with your applications and enterprise workflows if you have not already.")

    requested_ram_values = {int(v) for v in re.findall(r"\b(8|16|24|32|64)\s*(?:gb|g|ram)?\b", query_lower)}
    available_ram_values = sorted({to_int_or_zero(item.get("ram_gb")) for item in results if to_int_or_zero(item.get("ram_gb")) > 0})
    for req_ram in sorted(requested_ram_values):
        if req_ram <= 0:
            continue
        if req_ram not in available_ram_values and available_ram_values:
            lower_options = [v for v in available_ram_values if v < req_ram]
            higher_options = [v for v in available_ram_values if v > req_ram]
            if lower_options:
                best_alt = max(lower_options)
                notes.append(f"Availability note: {req_ram}GB configuration is not available in the current matching catalog. Best comparable option is {best_alt}GB RAM.")
            elif higher_options:
                best_alt = min(higher_options)
                notes.append(f"Availability note: {req_ram}GB configuration is not available in the current matching catalog. Closest available option is {best_alt}GB RAM.")

    # Check if we have in-stock alternatives
    has_no_stock = all(r.get("total_on_hand", 0) == 0 for r in results)
    if has_no_stock and part_list_lookup and inventory_by_sku:
        exclude_skus = set(r["sku"] for r in results)
        alternatives = find_in_stock_alternatives(query, constraints, part_list_lookup, inventory_by_sku, exclude_skus)
        if alternatives:
            notes.append("Stock note: The matching options are not currently in stock, so I've included comparable in-stock alternatives below.")

    if not notes:
        notes.append("No additional assumptions were needed for this search.")

    return notes


def normalize_cpu_class(value):
    """Normalize CPU tokens so U7/UX7 classes can be compared consistently."""
    token = clean_text(value).lower()
    compact = re.sub(r"[^a-z0-9]+", "", token)

    if re.search(r"\b(ultra\s*x7|ux\s*-?\s*7)\b", token) or compact in {"ultrax7", "ux7", "coreultrax7"}:
        return "UX7"
    if re.search(r"\b(ultra\s*7|u\s*-?\s*7|i7)\b", token) or compact in {"ultra7", "u7", "cu7", "i7", "coreultra7"}:
        return "U7"
    if compact in {"ultra5", "u5", "cu5", "coreultra5"}:
        return "U5"
    if compact in {"xelite", "snapdragonxelite"}:
        return "X Elite"
    if compact in {"xplus", "snapdragonxplus"}:
        return "X Plus"
    return ""


def query_requests_u7_class(query):
    text = clean_text(query).lower()
    return bool(re.search(r"\b(?:ultra\s*7|u\s*-?\s*7)\b", text))


def find_u7_upgrade_ux7_candidates(source_item, part_list_lookup, inventory_by_sku):
    """Find UX7 upgrade candidates for U7-class requests.

    Screen size is not a hard filter for UX7 upgrade recommendations.
    Relax order: color -> SSD -> RAM. Screen mismatch is flagged as a note.
    """
    source_line = clean_text(source_item.get("product_line", "")).lower()
    source_screen = clean_text(source_item.get("screen_size", ""))
    source_ram = to_int_or_zero(source_item.get("ram_gb", ""))
    source_storage = to_int_or_zero(source_item.get("storage_gb", ""))
    source_color = clean_text(source_item.get("color", "")).lower()

    candidates = []
    for sku, profile in (part_list_lookup or {}).items():
        if not is_next_gen_device_profile(profile):
            continue

        candidate_line = clean_text(profile.get("product_line", "")).lower()
        if source_line and candidate_line and source_line != candidate_line:
            continue

        cpu_class = normalize_cpu_class(profile.get("cpu_tier", "") or profile.get("raw_description", ""))
        if cpu_class != "UX7":
            continue

        candidate_screen = clean_text(profile.get("screen_size", ""))
        candidate_ram = to_int_or_zero(profile.get("ram_gb", ""))
        candidate_storage = to_int_or_zero(profile.get("storage_gb", ""))
        candidate_color = clean_text(profile.get("color", "")).lower()

        color_mismatch = 1 if source_color and candidate_color and source_color != candidate_color else 0
        ssd_mismatch = 1 if source_storage and candidate_storage and source_storage != candidate_storage else 0
        ram_mismatch = 1 if source_ram and candidate_ram and source_ram != candidate_ram else 0
        screen_mismatch = 1 if source_screen and candidate_screen and not candidate_screen.startswith(source_screen) else 0

        note = ""
        if screen_mismatch:
            size_label = f"{candidate_screen}in" if candidate_screen else "a different size"
            note = f"Upgrade option - UX7 available only in {size_label} at launch"

        stock = summarize_stock((inventory_by_sku or {}).get(sku, []))
        candidates.append({
            "replacement_sku": sku,
            "replacement_description": clean_text(profile.get("raw_description", "")) or f"Mapped replacement SKU {sku}",
            "total_on_hand": stock.get("total_on_hand", 0),
            "cpu_class": "UX7",
            "note": note,
            "_rank": (color_mismatch, ssd_mismatch, ram_mismatch, screen_mismatch, -int(stock.get("total_on_hand", 0)), sku),
        })

    candidates.sort(key=lambda x: x.get("_rank", (1, 1, 1, 1, 0, "")))
    return candidates


def build_description_next_gen_rows(query, requested_rows, part_list_lookup=None, inventory_by_sku=None, part_list_map=None):
    """Build UX7 up-sell rows for the description search tab.

    For each requested item that is not already a UX7, find the closest UX7
    equivalent and surface a brief note on why the upgrade is worth it.
    Items already specced at UX7 are skipped (no up-sell needed).
    """
    # Up-sell notes sourced from Surface product knowledge (May 2026)
    _NOTE_U7_TO_UX7 = (
        "UX7 vs U7: Same 16-core CPU, but 3\u00d7 the GPU (12 Xe vs 4 Xe graphics). "
        "Delivers way better graphics and AI performance — best for heavy "
        "multitasking, creative work, and AI-powered workflows. "
        "35%+ more graphics performance than MacBook Air M5."
    )
    _NOTE_U5_TO_UX7 = (
        "UX7 vs U5: Doubles the CPU cores (8\u219216) and triples the GPU (4\u219212 Xe). "
        "This is the biggest performance jump Surface has ever offered — "
        "a completely different class of performance for AI workloads, "
        "heavy multitasking, and creative tasks. Future-proof investment."
    )

    rows = []

    for item in requested_rows:
        requested_sku = clean_text(item.get("sku", ""))
        if not requested_sku:
            continue

        # Determine current CPU class of the requested item
        source_cpu_class = normalize_cpu_class(
            item.get("cpu_tier", "") or item.get("description", "")
        )

        # If already UX7, no up-sell needed
        if source_cpu_class == "UX7":
            continue

        # Find UX7 upgrade candidates using the existing helper
        ux7_candidates = find_u7_upgrade_ux7_candidates(item, part_list_lookup, inventory_by_sku)
        ux7_pick = None
        for candidate in ux7_candidates:
            repl_sku = clean_text(candidate.get("replacement_sku", ""))
            if repl_sku:
                ux7_pick = candidate
                break

        if ux7_pick:
            repl_sku = clean_text(ux7_pick.get("replacement_sku", ""))
            repl_desc = clean_text(ux7_pick.get("replacement_description", ""))
            on_hand = str(int(ux7_pick.get("total_on_hand", 0)))

            # Choose note based on how big the step-up is
            upsell_note = _NOTE_U7_TO_UX7 if source_cpu_class == "U7" else _NOTE_U5_TO_UX7

            # Append any mismatch flag from the candidate finder (e.g. screen size note)
            candidate_note = clean_text(ux7_pick.get("note", ""))
            if candidate_note:
                upsell_note = f"{upsell_note} | {candidate_note}"

            rows.append([
                requested_sku,
                repl_sku,
                repl_desc,
                on_hand,
                NEXT_GEN_AVAILABLE_DATE,
                upsell_note,
            ])
        else:
            rows.append([
                requested_sku,
                "Not Available",
                "No UX7 upgrade option found in current inventory",
                "0",
                "N/A",
                "",
            ])

    return rows


def build_description_search_email(query, results, constraints, part_list_lookup=None, inventory_by_sku=None, part_list_map=None, include_warranties=False):
    def is_accessory_result(item):
        desc = clean_text(item.get("description", "")).lower()
        return bool(re.search(r"\b(keyboard|kb|pen|dock|mouse|charger|hub|accessor|type\s*cover|folio)\b", desc))

    def warranty_device_candidates(items):
        candidates = []
        for item in items:
            if is_accessory_result(item):
                continue
            sku = clean_text(item.get("sku", ""))
            if sku:
                candidates.append(sku)
        return [s for s in dict.fromkeys(candidates)][:3]

    def is_next_gen_result(item):
        return is_next_gen_device_profile(item)

    # When the user explicitly requests a next-gen device family (e.g. Laptop 8, Pro 12),
    # include those results in Requested Options rather than filtering them out.
    query_explicitly_requests_next_gen = (
        constraints.get("laptop_generation") in {"8"} or
        constraints.get("pro_generation") in {"12"}
    )
    requested_rows = [
        r for r in results
        if not is_next_gen_result(r) or query_explicitly_requests_next_gen
    ]
    show_disti_column = any(int(r.get("total_on_hand", 0)) > 0 for r in requested_rows)

    lines = []
    lines.append("Hey,")
    lines.append("")
    lines.append(f"I searched for '{clean_text(query)}' and pulled the closest matches below.")
    lines.append("")

    notes = build_description_search_email_notes(query, results, constraints, part_list_lookup, inventory_by_sku)
    lines.append("Quick heads-up on what I found:")
    for note in notes:
        lines.append(f"- {note}")
    lines.append("")

    # Requested Options table (includes devices and accessories with inventory)
    if requested_rows:
        lines.append("Requested Options (Current Match Set)")
        requested_headers = ["SKU", "Product Description", "On Hand"]
        if show_disti_column:
            requested_headers.append("Disti(s)")
        requested_headers.append("Match")
        requested_rows_table = []
        for item in requested_rows:
            row = [
                item.get("sku", ""),
                item.get("description", ""),
                str(item.get("total_on_hand", 0)),
            ]
            if show_disti_column:
                row.append(stock_distributor_text((inventory_by_sku or {}).get(item.get("sku", ""), [])))
            row.append(f"{item.get('match_pct', item.get('score', 0))}% ({item.get('matched_tokens', '')})")
            requested_rows_table.append(row)
        lines.append(render_text_table(requested_headers, requested_rows_table))
        lines.append("")

    device_rows = [r for r in requested_rows if not is_accessory_result(r)]
    stock_basis = device_rows if device_rows else results
    has_no_stock = bool(stock_basis) and all(r.get("total_on_hand", 0) == 0 for r in stock_basis)
    alternatives = []
    if has_no_stock and part_list_lookup and inventory_by_sku:
        exclude_skus = set(r["sku"] for r in results)
        alternatives = find_in_stock_alternatives(query, constraints, part_list_lookup, inventory_by_sku, exclude_skus)

    if has_no_stock:
        lines.append("The requested options are currently out of stock. The table below shows 1-3 in-stock comparable alternatives available now.")
        lines.append("")

    if alternatives:
        alternatives_headers = ["SKU", "Device Family", "OS", "Screen", "CPU", "RAM", "Storage", "Color", "On Hand", "Match Score", "Distributor"]
        lines.append("In-Stock Comparable Alternatives")
        lines.append(render_text_table(alternatives_headers, [
            [
                alt.get("sku", ""),
                alt.get("product_line", ""),
                alt.get("os", ""),
                screen_text(alt.get("screen_size", "")),
                alt.get("cpu_tier", ""),
                gb_text(alt.get("ram_gb", "")),
                gb_text(alt.get("storage_gb", "")),
                alt.get("color", ""),
                alt.get("total_on_hand", 0),
                match_score_percent_text(alt.get("score", 0)),
                alt.get("distributor_stock", "N/A"),
            ]
            for alt in alternatives
        ]))
        lines.append("")

    if ENABLE_NEXT_GEN_SUGGESTIONS:
        next_gen_rows = build_description_next_gen_rows(
            query,
            requested_rows,
            part_list_lookup,
            inventory_by_sku,
            part_list_map,
        )

        if next_gen_rows:
            lines.append("Upgrade Opportunity: UX7 Edition")
            lines.append(render_text_table(["Requested SKU", "UX7 Upgrade SKU", "Description", "On Hand", "In Stock As Of", "Why Upgrade"], next_gen_rows))
            lines.append("")

    if include_warranties:
        current_device_skus = warranty_device_candidates(requested_rows)
        if alternatives:
            current_device_skus.extend(warranty_device_candidates(alternatives))
        current_device_skus = [s for s in dict.fromkeys(current_device_skus) if clean_text(s)][:3]

        if current_device_skus:
            lines.append("Compatible warranty/service-plan options (current requested/comparable devices):")
            lines.append(build_warranty_section_text(current_device_skus, part_list_map, part_list_lookup))
            lines.append("")

    lines.append("Best,")
    lines.append("SKU Agent")
    return "\n".join(lines)


def build_description_search_email_html(query, results, constraints, part_list_lookup=None, inventory_by_sku=None, part_list_map=None, include_warranties=False):
    notes = build_description_search_email_notes(query, results, constraints, part_list_lookup, inventory_by_sku)
    include_snapdragon_disclaimer = any(is_snapdragon_profile(item) for item in results)

    def is_accessory_result(item):
        desc = clean_text(item.get("description", "")).lower()
        return bool(re.search(r"\b(keyboard|kb|pen|dock|mouse|charger|hub|accessor|type\s*cover|folio)\b", desc))

    def warranty_device_candidates(items):
        candidates = []
        for item in items:
            if is_accessory_result(item):
                continue
            sku = clean_text(item.get("sku", ""))
            if sku:
                candidates.append(sku)
        # Keep order, remove duplicates, cap to top 3 to prevent overlong warranty sections.
        return [s for s in dict.fromkeys(candidates)][:3]

    def is_next_gen_result(item):
        return is_next_gen_device_profile(item)

    # When the user explicitly requests a next-gen device family (e.g. Laptop 8, Pro 12),
    # include those results in Requested Options rather than filtering them out.
    query_explicitly_requests_next_gen = (
        constraints.get("laptop_generation") in {"8"} or
        constraints.get("pro_generation") in {"12"}
    )
    requested_rows = [
        r for r in results
        if not is_next_gen_result(r) or query_explicitly_requests_next_gen
    ]
    show_disti_column = any(int(r.get("total_on_hand", 0)) > 0 for r in requested_rows)

    header_html = "".join([
        f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{html.escape(h)}</th>"
        for h in (["SKU", "Product Description", "On Hand"] + (["Disti(s)"] if show_disti_column else []) + ["Match"])
    ])

    row_html = []
    for item in requested_rows:
        # Include all items (devices and accessories) with their inventory
        cells = [
            item.get("sku", ""),
            item.get("description", ""),
            item.get("total_on_hand", 0),
        ]
        if show_disti_column:
            cells.append(stock_distributor_text((inventory_by_sku or {}).get(item.get("sku", ""), [])))
        cells.append(
            f"{item.get('match_pct', item.get('score', 0))}% ({item.get('matched_tokens', '')})",
        )
        row_html.append("<tr>" + "".join([
            f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
            for cell in cells
        ]) + "</tr>")

    notes_html = "".join([
        f"<li style=\"margin:0 0 8px 0;\">{html.escape(clean_text(note))}</li>"
        for note in notes
    ])

    # Build alternatives section and next-gen equivalent section
    alternatives_html = ""
    alternatives = []
    device_rows = [r for r in requested_rows if not is_accessory_result(r)]
    stock_basis = device_rows if device_rows else results
    has_no_stock = bool(stock_basis) and all(r.get("total_on_hand", 0) == 0 for r in stock_basis)
    if has_no_stock and part_list_lookup and inventory_by_sku:
        exclude_skus = set(r["sku"] for r in results)
        alternatives = find_in_stock_alternatives(query, constraints, part_list_lookup, inventory_by_sku, exclude_skus)
        if alternatives:
            alt_header_html = "".join([
                f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{html.escape(h)}</th>"
                for h in ["SKU", "Description", "On Hand", "Match Score", "Distributor"]
            ])
            alt_row_html = []
            for alt in alternatives:
                alt_sku = alt.get("sku", "")
                raw_desc = part_list_lookup.get(alt_sku, {}).get("raw_description", "") or alt.get("product_line", "")
                cells = [
                    alt_sku,
                    raw_desc,
                    alt.get("total_on_hand", 0),
                    match_score_percent_text(alt.get("score", 0)),
                    alt.get("distributor_stock", "N/A"),
                ]
                alt_row_html.append("<tr>" + "".join([
                    f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
                    for cell in cells
                ]) + "</tr>")

                if is_snapdragon_profile(alt):
                    include_snapdragon_disclaimer = True

            alternatives_html = (
                "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:20px 0 0 0;\">"
                "In-Stock Comparable Alternatives"
                "</div>"
                "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
                f"<thead><tr>{alt_header_html}</tr></thead>"
                f"<tbody>{''.join(alt_row_html)}</tbody>"
                "</table>"
            )

    next_gen_html = ""
    if ENABLE_NEXT_GEN_SUGGESTIONS:
        next_gen_rows = build_description_next_gen_rows(
            query,
            requested_rows,
            part_list_lookup,
            inventory_by_sku,
            part_list_map,
        )

        next_gen_rows_html = []
        for cells in next_gen_rows:
            replacement_sku = clean_text(cells[1] if len(cells) > 1 else "")
            replacement_desc = clean_text(cells[2] if len(cells) > 2 else "")
            if replacement_sku and sku_is_snapdragon(replacement_sku, part_list_lookup):
                include_snapdragon_disclaimer = True
            if "snapdragon" in replacement_desc.lower():
                include_snapdragon_disclaimer = True
            next_gen_rows_html.append("<tr>" + "".join([
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
                for cell in cells
            ]) + "</tr>")

        if next_gen_rows_html:
            next_gen_html = (
                "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:20px 0 0 0;\">"
                "Upgrade Opportunity: UX7 Edition"
                "</div>"
            )
            next_gen_header_html = "".join([
                f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{h}</th>"
                for h in ["Requested SKU", "UX7 Upgrade SKU", "Description", "On Hand", "In Stock As Of", "Why Upgrade"]
            ])
            next_gen_html += (
                "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
                f"<thead><tr>{next_gen_header_html}</tr></thead>"
                f"<tbody>{''.join(next_gen_rows_html)}</tbody>"
                "</table>"
            )

    warranties_html = ""
    if include_warranties:
        current_device_skus = warranty_device_candidates(requested_rows)
        if alternatives:
            current_device_skus.extend(warranty_device_candidates(alternatives))
        current_device_skus = [s for s in dict.fromkeys(current_device_skus) if clean_text(s)][:3]
        if current_device_skus:
            warranties_html = (
                "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:20px 0 0 0;\">"
                "Compatible Warranty / Service-Plan Options (Current Requested/Comparable Devices)"
                "</div>"
                + build_warranty_section_html(current_device_skus, part_list_map, part_list_lookup)
            )

    snapdragon_disclaimer_html = ""
    if include_snapdragon_disclaimer:
        snapdragon_disclaimer_html = (
            "<div style=\"background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin:0 0 14px 0;font-size:13px;line-height:1.5;\">"
            "<strong>Snapdragon disclaimer:</strong> One or more searched or suggested options include Snapdragon-based devices. "
            "Please validate application, security tooling, and peripheral compatibility with your enterprise environment before final selection."
            "</div>"
        )

    return (
        "<div style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#1f2937;line-height:1.4;\">"
        "<p style=\"margin:0 0 12px 0;\">Hey,</p>"
        f"<p style=\"margin:0 0 14px 0;\">I searched for '<strong>{html.escape(clean_text(query))}</strong>' and pulled the closest matches below.</p>"
        "<div style=\"background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin:0 0 14px 0;font-size:13px;line-height:1.5;\">"
        "<strong>Quick heads-up on what I found:</strong>"
        f"<ul style=\"margin:8px 0 0 18px;padding:0;\">{notes_html}</ul>"
        "</div>"
        "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:14px 0 0 0;\">"
        "Requested Options (Current Match Set)"
        "</div>"
        "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(row_html)}</tbody>"
        "</table>"
        + ("<p style=\"margin:0 0 12px 0;\">The requested options are currently out of stock. The table below shows 1-3 in-stock comparable alternatives available now.</p>" if has_no_stock else "")
        + f"{alternatives_html}"
        + f"{next_gen_html}"
        + f"{snapdragon_disclaimer_html}"
        + f"{warranties_html}"
        + "<p style=\"margin:0;\">Let me know if you need anything else or want to dive deeper on any of these.<br>—&nbsp;SKU Agent</p>"
        + "</div>"
    )


def format_qty_need(qty):
    return "Not specified" if qty is None else str(qty)


def detect_greeting_name(email_text):
    if not email_text:
        return "Sales Team"

    m = re.search(r"(?im)^\s*(?:hi|hello)\s+([A-Za-z][A-Za-z\-']{1,30})\b", email_text)
    if m:
        return m.group(1)
    return "Sales Team"


def explicitly_requests_term(text, term):
    lower = clean_text(text).lower()
    return bool(re.search(rf"\b{re.escape(term.lower())}\b", lower))


def gb_text(value):
    text = clean_text(value)
    if not text:
        return "N/A"
    if text.lower().endswith("gb"):
        return text.upper()
    return f"{text}GB"


def match_score_percent_text(score):
    try:
        value = int(score)
    except (TypeError, ValueError):
        value = 0
    value = max(0, min(100, value))
    return f"{value}%"


def screen_text(value):
    text = clean_text(value)
    if not text:
        return "N/A"
    if text.endswith('"'):
        return text
    return f'{text}"'


def render_text_table(headers, rows):
    """Render an ASCII table for copy/paste-friendly plain text emails."""
    normalized_headers = [clean_text(h) for h in headers]
    normalized_rows = [[clean_text(c) for c in row] for row in rows]

    widths = [len(h) for h in normalized_headers]
    for row in normalized_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def fmt_row(values):
        padded = []
        for i, value in enumerate(values):
            width = widths[i] if i < len(widths) else len(value)
            padded.append(value.ljust(width))
        return "| " + " | ".join(padded) + " |"

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [border, fmt_row(normalized_headers), border]
    for row in normalized_rows:
        aligned = row + [""] * (len(widths) - len(row))
        out.append(fmt_row(aligned[: len(widths)]))
    out.append(border)
    return "\n".join(out)


def to_int_or_zero(value):
    try:
        return int(clean_text(value) or "0")
    except ValueError:
        return 0


def short_device_name(product):
    return f"{product['product_line']} {product['generation']} - {product['raw_description']}"


def get_platform_concerns(sku_info):
    """Return platform-specific warnings or concerns."""
    concerns = []
    if sku_info.get("cpu_family") and "snapdragon" in sku_info.get("cpu_family", "").lower():
        concerns.append("Snapdragon processor - test for enterprise compatibility before deployment")
    return concerns


def build_replacement_note(source, repl):
    source_device = f"{source['product_line']} {source['generation']}"
    repl_device = f"{repl['product_line']} {repl['generation']}"

    details = []
    source_ram = gb_text(source.get("ram_gb"))
    repl_ram = gb_text(repl.get("ram_gb"))
    details.append(f"RAM increased from {source_ram} to {repl_ram}")

    if source.get("storage_gb") and repl.get("storage_gb"):
        if source.get("storage_gb") == repl.get("storage_gb"):
            details.append(f"same storage ({gb_text(source.get('storage_gb'))})")
        elif to_int_or_zero(repl.get("storage_gb")) < to_int_or_zero(source.get("storage_gb")):
            details.append(f"storage decreased from {gb_text(source.get('storage_gb'))} to {gb_text(repl.get('storage_gb'))} ⚠️")
        else:
            details.append(f"storage increased from {gb_text(source.get('storage_gb'))} to {gb_text(repl.get('storage_gb'))}")
    if source.get("screen_size") and repl.get("screen_size") and source.get("screen_size") == repl.get("screen_size"):
        details.append(f"same {source.get('screen_size')}\" screen")
    if source.get("color") and repl.get("color") and source.get("color") == repl.get("color"):
        details.append(f"same {source.get('color')} color")

    platform_concerns = get_platform_concerns(repl)
    summary = ", ".join(details)
    note = f"{source['sku']}: Upgraded from {source_device} to {repl_device}, {summary}."
    if platform_concerns:
        note += " " + " | ".join(platform_concerns)
    return note


def build_consultative_analysis(requested_skus, replacements_by_sku, part_list_lookup):
    """Generate strategic analysis and recommendations."""
    analysis_lines = []

    # Identify any platform concerns across all suggestions
    snapdragon_suggestions = []
    intel_suggestions = []

    for requested_sku, replacements in replacements_by_sku.items():
        for repl in replacements:
            repl_sku = repl.get("replacement_sku")
            repl_info = (part_list_lookup or {}).get(repl_sku, {})
            platform = repl_info.get("platform", "").lower()

            if "snapdragon" in repl_info.get("cpu_family", "").lower():
                snapdragon_suggestions.append((requested_sku, repl_sku))
            elif platform == "intel":
                intel_suggestions.append((requested_sku, repl_sku))

    # Build analysis
    if snapdragon_suggestions and intel_suggestions:
        analysis_lines.append("Platform mix detected:")
        analysis_lines.append(f"- {len(snapdragon_suggestions)} option(s) include Snapdragon processors.")
        analysis_lines.append("- Validate application and driver compatibility with IT before broad deployment.")
        analysis_lines.append(f"- {len(intel_suggestions)} Intel option(s) are available when strict compatibility is required.")
    elif snapdragon_suggestions:
        analysis_lines.append("Snapdragon-only replacements detected:")
        analysis_lines.append("- Recommend a pilot validation for enterprise app and peripheral compatibility.")
        analysis_lines.append("- If needed, request Intel alternatives for strict legacy compatibility requirements.")

    return "\n".join(analysis_lines) if analysis_lines else ""


def email_requests_more_ram(email_text):
    text = clean_text(email_text).lower()
    if not text:
        return False
    triggers = [
        "more ram",
        "higher ram",
        "upgrade ram",
        "increased ram",
        "more memory",
        "higher memory",
        "upgrade memory",
    ]
    return any(t in text for t in triggers)


def email_explicitly_requests_next_gen(email_text):
    text = clean_text(email_text).lower()
    if not text:
        return False

    conditional_phrases = [
        "if unavailable",
        "if not available",
        "if out of stock",
        "when unavailable",
        "in case unavailable",
    ]
    if any(p in text for p in conditional_phrases):
        return False

    explicit_triggers = [
        "next gen",
        "next-gen",
        "next generation",
        "current generation equivalent",
        "equivalent sku",
        "replacement sku",
        "substitute sku",
    ]
    return any(t in text for t in explicit_triggers)


def build_replacement_guide_data(requested_skus, sku_lookup, inventory_by_sku, part_list_map=None, part_list_lookup=None, prefer_higher_ram=False):
    table_rows = []
    notes = []
    replacements_by_sku = {}

    for requested_sku in requested_skus:
        product = (part_list_lookup or {}).get(requested_sku)
        if not product:
            # Keep going — explicit workbook mapping can still provide valid replacements.
            product = {
                "sku": requested_sku,
                "product_line": "Surface Device",
                "generation": "",
                "raw_description": "SKU not found in inventory profile",
                "ram_gb": "",
            }

        replacements = find_replacements_for_sku(
            requested_sku, sku_lookup, inventory_by_sku, part_list_map, part_list_lookup, prefer_higher_ram
        )
        replacements_by_sku[requested_sku] = replacements

        source_ram = to_int_or_zero(product.get("ram_gb"))
        if replacements:
            if prefer_higher_ram:
                ranked = sorted(
                    replacements,
                    key=lambda r: (
                        -(to_int_or_zero(((part_list_lookup or {}).get(r["replacement_sku"]) or {}).get("ram_gb") or r.get("replacement_ram_gb", ""))),
                        -r["match_score"],
                        -r["total_on_hand"],
                        -r["total_on_order"],
                    ),
                )
            else:
                ranked = sorted(
                    replacements,
                    key=lambda r: (
                        abs(
                            to_int_or_zero(((part_list_lookup or {}).get(r["replacement_sku"]) or {}).get("ram_gb") or r.get("replacement_ram_gb", ""))
                            - source_ram
                        ),
                        -r["match_score"],
                        -r["total_on_hand"],
                        -r["total_on_order"],
                    ),
                )

            preferred = None
            for candidate in ranked:
                candidate_product = (part_list_lookup or {}).get(candidate["replacement_sku"])
                candidate_ram = to_int_or_zero(
                    (candidate_product or {}).get("ram_gb") or candidate.get("replacement_ram_gb", "")
                )
                if (not prefer_higher_ram) or candidate_ram >= source_ram:
                    preferred = candidate
                    break

            if not preferred:
                preferred = ranked[0]

            replacement_product = (part_list_lookup or {}).get(preferred["replacement_sku"])
            if replacement_product:
                table_rows.append([
                    product["sku"],
                    short_device_name(product),
                    gb_text(product.get("ram_gb")),
                    replacement_product["sku"],
                    short_device_name(replacement_product),
                    gb_text(replacement_product.get("ram_gb")),
                ])
                notes.append(build_replacement_note(product, replacement_product))
            else:
                ram_text = "N/A"
                if preferred.get("replacement_ram_gb"):
                    ram_text = gb_text(preferred.get("replacement_ram_gb"))
                table_rows.append([
                    product["sku"],
                    short_device_name(product),
                    gb_text(product.get("ram_gb")),
                    preferred["replacement_sku"],
                    preferred["replacement_description"],
                    ram_text,
                ])
                notes.append(f"{product['sku']}: Replacement selected based on uploaded part list mapping.")
        else:
            table_rows.append([
                product["sku"],
                short_device_name(product),
                gb_text(product.get("ram_gb")),
                "N/A",
                "No equivalent replacement found",
                "N/A",
            ])
            notes.append(f"{product['sku']}: No equivalent replacement was found in the current candidate set.")

    # Generate consultative analysis
    consultative = build_consultative_analysis(requested_skus, replacements_by_sku, part_list_lookup)
    
    return table_rows, notes, consultative, replacements_by_sku


def build_sales_email(email_text, requested_skus, requested_qtys, sku_lookup, inventory_by_sku, part_list_map=None, part_list_lookup=None, include_warranties=False):
    greeting_name = detect_greeting_name(email_text)
    prefer_higher_ram = email_requests_more_ram(email_text)
    allow_demo = explicitly_requests_term(email_text, "demo")
    allow_taa = explicitly_requests_term(email_text, "taa")

    lines = []
    lines.append(f"Hi {greeting_name},")
    lines.append("")
    if prefer_higher_ram:
        table_headers = [
            "Original SKU",
            "Original Device",
            "Original RAM",
            "New SKU",
            "New Device",
            "New RAM",
            "Available to Purchase",
        ]
    else:
        table_headers = [
            "Original SKU",
            "Original Device",
            "New SKU",
            "New Device",
            "Available to Purchase",
        ]
    table_rows, notes, consultative, replacements_by_sku = build_replacement_guide_data(
        requested_skus, sku_lookup, inventory_by_sku, part_list_map, part_list_lookup, prefer_higher_ram
    )
    mapped_rows = [r for r in table_rows if clean_text(r[3]) and clean_text(r[3]) != "N/A"]

    missing_profile_skus = [
        sku for sku in requested_skus
        if not (part_list_lookup or {}).get(sku)
    ]

    low_stock_requested = [
        sku for sku in requested_skus
        if summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0) < LOW_STOCK_COMPARABLE_THRESHOLD
    ]
    comparable_trigger_skus = list(dict.fromkeys(low_stock_requested + missing_profile_skus))
    mapped_count = sum(1 for r in table_rows if clean_text(r[3]) and clean_text(r[3]) != "N/A")
    requested_in_stock_count = sum(
        1 for sku in requested_skus
        if summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0) > 0
    )
    requested_total_on_hand = sum(
        summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0)
        for sku in requested_skus
    )
    include_next_gen = ENABLE_NEXT_GEN_SUGGESTIONS

    if include_next_gen:
        lines.append(
            f"I pulled {len(requested_skus)} SKU(s) and found {mapped_count} solid swap(s) from the part list."
        )
    else:
        lines.append(
            f"Stock check on {len(requested_skus)} SKU(s): looks like we've got inventory on these."
        )
    if prefer_higher_ram:
        lines.append("Also, I prioritized higher RAM configs since you mentioned needing the upgrade.")
    else:
        lines.append("I prioritized closest-match specs and pulled the latest stock visibility from our distributors.")
    lines.append(
        f"Inventory update: {requested_in_stock_count} of {len(requested_skus)} SKU(s) showing on-hand stock ({requested_total_on_hand} total)."
    )
    if comparable_trigger_skus:
        lines.append(
            f"These SKU(s) are low stock (<{LOW_STOCK_COMPARABLE_THRESHOLD} on hand) or out of stock, so I've pulled 1-3 in-stock alternatives below."
        )
    if missing_profile_skus:
        lines.append(
            "Some requested SKU descriptions were not found in the current part list and were treated as low-stock/out-of-stock for alternative matching: "
            + ", ".join(missing_profile_skus)
        )
    lines.append("")

    inventory_table_rows = []
    for sku in requested_skus:
        product_desc = (part_list_lookup or {}).get(sku, {}).get("raw_description", "")
        stock_rows = inventory_by_sku.get(sku, [])
        stock_summary = summarize_stock(stock_rows)
        fallback_desc = clean_text(stock_rows[0].get("description", "")) if stock_rows else ""
        resolved_desc = product_desc or fallback_desc or "Description not found in part list"
        inventory_table_rows.append([
            sku,
            resolved_desc,
            stock_distributor_text(stock_rows),
            str(stock_summary.get("total_on_hand", 0)),
        ])

    if inventory_table_rows:
        lines.append("Requested SKU Inventory Results")
        lines.append("")
        lines.append("| Device SKU | Product Description | Disti(s) | Amount of Stock on-hand |")
        lines.append("| --- | --- | --- | --- |")
        for row in inventory_table_rows:
            lines.append("| " + " | ".join([clean_text(c) for c in row]) + " |")
        lines.append("")

    in_stock_rows = []
    for sku in comparable_trigger_skus:
        comparables = find_in_stock_comparables_for_sku(
            sku,
            part_list_lookup,
            inventory_by_sku,
            sku_lookup,
            limit=3,
            allow_demo=allow_demo,
            allow_taa=allow_taa,
        )
        for comp in comparables:
            in_stock_rows.append([
                sku,
                comp.get("sku", ""),
                comp.get("product_line", ""),
                comp.get("cpu_tier", ""),
                gb_text(comp.get("ram_gb", "")),
                gb_text(comp.get("storage_gb", "")),
                str(comp.get("total_on_hand", 0)),
                match_score_percent_text(comp.get("score", 0)),
                comp.get("distributor_stock", "N/A"),
            ])

    if in_stock_rows:
        lines.append("In-Stock Comparable Alternatives")
        lines.append("")
        lines.append("| Requested SKU | In-Stock SKU | Device Family | CPU | RAM | Storage | On Hand | Match Score | Disti(s) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in in_stock_rows:
            lines.append("| " + " | ".join([clean_text(c) for c in row]) + " |")
        lines.append("")

    if include_next_gen:
        lines.append("Next-Gen Equivalent Options")
        lines.append("")

        if consultative:
            lines.append(consultative)
            lines.append("")

        lines.append("| " + " | ".join(table_headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(table_headers)) + " |")
        for row in table_rows:
            mapped = bool(clean_text(row[3]) and clean_text(row[3]) != "N/A")
            if prefer_higher_ram:
                render_row = row
            else:
                # Drop RAM columns when RAM is not part of the request.
                render_row = [row[0], row[1], row[3], row[4]]
            render_row = render_row + [NEXT_GEN_AVAILABLE_DATE if mapped else "N/A"]
            lines.append("| " + " | ".join([clean_text(col) for col in render_row]) + " |")

    if include_warranties:
        current_device_skus = []
        for row in table_rows:
            requested_sku = clean_text(row[0])
            mapped_sku = clean_text(row[3])
            if requested_sku and (part_list_lookup or {}).get(requested_sku):
                current_device_skus.append(requested_sku)
            elif mapped_sku and mapped_sku != "N/A":
                current_device_skus.append(mapped_sku)
            elif requested_sku:
                current_device_skus.append(requested_sku)
        current_device_skus = [s for s in dict.fromkeys(current_device_skus) if s]
        lines.append("")
        lines.append("Compatible warranty/service-plan options (requested/current devices):")
        lines.append(build_warranty_section_text(current_device_skus, part_list_map, part_list_lookup))

    lines.append("")
    lines.append("Best,")
    lines.append("SKU Agent")

    return "\n".join(lines)


def build_sales_email_html(email_text, requested_skus, requested_qtys, sku_lookup, inventory_by_sku, part_list_map=None, part_list_lookup=None, include_warranties=False):
    greeting_name = html.escape(detect_greeting_name(email_text))
    prefer_higher_ram = email_requests_more_ram(email_text)
    allow_demo = explicitly_requests_term(email_text, "demo")
    allow_taa = explicitly_requests_term(email_text, "taa")
    include_snapdragon_disclaimer = any(sku_is_snapdragon(sku, part_list_lookup, sku_lookup) for sku in requested_skus)

    if prefer_higher_ram:
        headers = [
            "Original SKU",
            "Original Device",
            "Original RAM",
            "New SKU",
            "New Device",
            "New RAM",
            "Available to Purchase",
        ]
    else:
        headers = [
            "Original SKU",
            "Original Device",
            "New SKU",
            "New Device",
            "Available to Purchase",
        ]
    table_rows, notes, consultative, replacements_by_sku = build_replacement_guide_data(
        requested_skus, sku_lookup, inventory_by_sku, part_list_map, part_list_lookup, prefer_higher_ram
    )
    mapped_rows = [r for r in table_rows if clean_text(r[3]) and clean_text(r[3]) != "N/A"]

    missing_profile_skus = [
        sku for sku in requested_skus
        if not (part_list_lookup or {}).get(sku)
    ]

    low_stock_requested = [
        sku for sku in requested_skus
        if summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0) < LOW_STOCK_COMPARABLE_THRESHOLD
    ]
    comparable_trigger_skus = list(dict.fromkeys(low_stock_requested + missing_profile_skus))
    mapped_count = sum(1 for r in table_rows if clean_text(r[3]) and clean_text(r[3]) != "N/A")
    requested_in_stock_count = sum(
        1 for sku in requested_skus
        if summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0) > 0
    )
    requested_total_on_hand = sum(
        summarize_stock(inventory_by_sku.get(sku, [])).get("total_on_hand", 0)
        for sku in requested_skus
    )
    include_next_gen = ENABLE_NEXT_GEN_SUGGESTIONS

    intro_first_line = (
        f"<p style=\"margin:0 0 10px 0;\">I pulled {len(requested_skus)} SKU(s) and found {mapped_count} solid swap(s) from the part list.</p>"
        if include_next_gen
        else f"<p style=\"margin:0 0 10px 0;\">Stock check on {len(requested_skus)} SKU(s): looks like we've got inventory on these.</p>"
    )
    intro_html = (
        intro_first_line
        + ("<p style=\"margin:0 0 10px 0;\">Also, I prioritized higher RAM configs since you mentioned needing the upgrade.</p>" if prefer_higher_ram else "<p style=\"margin:0 0 10px 0;\">I prioritized closest-match specs and pulled the latest stock visibility from our distributors.</p>")
        + f"<p style=\"margin:0 0 14px 0;\">Inventory update: {requested_in_stock_count} of {len(requested_skus)} SKU(s) showing on-hand stock ({requested_total_on_hand} total).</p>"
        + (f"<p style=\"margin:0 0 14px 0;\">These SKU(s) are low stock (&lt;{LOW_STOCK_COMPARABLE_THRESHOLD} on hand) or out of stock, so I've pulled 1-3 in-stock alternatives below.</p>" if comparable_trigger_skus else "")
        + (f"<p style=\"margin:0 0 14px 0;\">Some requested SKU descriptions were not found in the current part list and were treated as low-stock/out-of-stock for alternative matching: {html.escape(', '.join(missing_profile_skus))}</p>" if missing_profile_skus else "")
    )

    in_stock_table_html = ""
    inventory_results_html = ""
    inventory_table_rows = []
    for sku in requested_skus:
        product_desc = (part_list_lookup or {}).get(sku, {}).get("raw_description", "")
        stock_rows = inventory_by_sku.get(sku, [])
        stock_summary = summarize_stock(stock_rows)
        fallback_desc = clean_text(stock_rows[0].get("description", "")) if stock_rows else ""
        resolved_desc = product_desc or fallback_desc or "Description not found in part list"
        inventory_table_rows.append([
            sku,
            resolved_desc,
            stock_distributor_text(stock_rows),
            str(stock_summary.get("total_on_hand", 0)),
        ])

    if inventory_table_rows:
        inventory_header_html = "".join([
            f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{h}</th>"
            for h in ["Device SKU", "Product Description", "Disti(s)", "Amount of Stock on-hand"]
        ])
        inventory_row_html = []
        for row in inventory_table_rows:
            inventory_row_html.append("<tr>" + "".join([
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
                for cell in row
            ]) + "</tr>")
        inventory_results_html = (
            "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:14px 0 0 0;\">"
            "Requested SKU Inventory Results"
            "</div>"
            "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
            f"<thead><tr>{inventory_header_html}</tr></thead>"
            f"<tbody>{''.join(inventory_row_html)}</tbody>"
            "</table>"
        )

    in_stock_rows = []
    for sku in comparable_trigger_skus:
        comparables = find_in_stock_comparables_for_sku(
            sku,
            part_list_lookup,
            inventory_by_sku,
            sku_lookup,
            limit=3,
            allow_demo=allow_demo,
            allow_taa=allow_taa,
        )
        for comp in comparables:
            if is_snapdragon_profile(comp):
                include_snapdragon_disclaimer = True
            comp_sku = comp.get("sku", "")
            raw_desc = part_list_lookup.get(comp_sku, {}).get("raw_description", "") or comp.get("product_line", "")
            in_stock_rows.append([
                sku,
                comp_sku,
                raw_desc,
                str(comp.get("total_on_hand", 0)),
                match_score_percent_text(comp.get("score", 0)),
                comp.get("distributor_stock", "N/A"),
            ])

    if in_stock_rows:
        in_stock_header_html = "".join([
            f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{h}</th>"
            for h in ["Requested SKU", "In-Stock SKU", "Description", "On Hand", "Match Score", "Disti(s)"]
        ])
        in_stock_row_html = []
        for row in in_stock_rows:
            in_stock_row_html.append("<tr>" + "".join([
                f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
                for cell in row
            ]) + "</tr>")
        in_stock_table_html = (
            "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:14px 0 0 0;\">"
            "In-Stock Comparable Alternatives"
            "</div>"
            "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
            f"<thead><tr>{in_stock_header_html}</tr></thead>"
            f"<tbody>{''.join(in_stock_row_html)}</tbody>"
            "</table>"
        )

    row_html = []
    for row in table_rows:
        mapped_sku = clean_text(row[3])
        if mapped_sku and mapped_sku != "N/A" and sku_is_snapdragon(mapped_sku, part_list_lookup, sku_lookup):
            include_snapdragon_disclaimer = True
        if "snapdragon" in clean_text(row[4]).lower():
            include_snapdragon_disclaimer = True
        mapped = bool(mapped_sku and mapped_sku != "N/A")
        if prefer_higher_ram:
            render_row = row
        else:
            render_row = [row[0], row[1], row[3], row[4]]
        render_row = render_row + [NEXT_GEN_AVAILABLE_DATE if mapped else "N/A"]
        cells = "".join([
            f"<td style=\"padding:8px 10px;border:1px solid #d1d9e6;vertical-align:top;\">{html.escape(clean_text(cell))}</td>"
            for cell in render_row
        ])
        row_html.append(f"<tr>{cells}</tr>")

    warranties_html = ""
    if include_warranties:
        current_device_skus = []
        for row in table_rows:
            requested_sku = clean_text(row[0])
            mapped_sku = clean_text(row[3])
            if requested_sku and (part_list_lookup or {}).get(requested_sku):
                current_device_skus.append(requested_sku)
            elif mapped_sku and mapped_sku != "N/A":
                current_device_skus.append(mapped_sku)
            elif requested_sku:
                current_device_skus.append(requested_sku)
        current_device_skus = [s for s in dict.fromkeys(current_device_skus) if s]
        warranties_html = (
            "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:20px 0 0 0;\">"
            "Compatible Warranty / Service-Plan Options (Requested/Current Devices)"
            "</div>"
            + build_warranty_section_html(current_device_skus, part_list_map, part_list_lookup)
        )

    snapdragon_disclaimer_html = ""
    if include_snapdragon_disclaimer:
        snapdragon_disclaimer_html = (
            "<div style=\"background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin:0 0 14px 0;font-size:13px;line-height:1.5;\">"
            "<strong>Snapdragon disclaimer:</strong> One or more searched or suggested options include Snapdragon-based devices. "
            "Please validate application, security tooling, and peripheral compatibility with your enterprise environment before final selection."
            "</div>"
        )

    header_html = "".join([
        f"<th style=\"padding:8px 10px;border:1px solid #c5d0e0;background:#eef3fa;color:#0f172a;text-align:left;\">{html.escape(h)}</th>"
        for h in headers
    ])

    notes_html = "".join([
        f"<li style=\"margin:0 0 8px 0;\">{html.escape(clean_text(note))}</li>"
        for note in notes
    ])
    if not notes_html:
        notes_html = "<li style=\"margin:0 0 8px 0;\">No replacement notes available.</li>"

    consultative_html = ""
    if consultative:
        consultative_lines = consultative.split("\n")
        consultative_html = (
            "<div style=\"background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin:0 0 14px 0;font-size:13px;line-height:1.5;\">"
            + "".join([f"<div style=\"margin:0 0 6px 0;\">{html.escape(line)}</div>" for line in consultative_lines])
            + "</div>"
        )

    next_gen_section_html = ""
    if include_next_gen:
        next_gen_section_html = (
            "<div style=\"background:#2f62b6;color:#ffffff;font-weight:700;padding:8px 10px;margin:14px 0 0 0;\">"
            + ("Next-Gen Equivalent Options (Quote-Ready) - Equivalent Configs with More RAM" if prefer_higher_ram else "Next-Gen Equivalent Options (Quote-Ready)")
            + "</div>"
            + consultative_html
            + "<table style=\"border-collapse:collapse;width:100%;margin:0 0 14px 0;font-size:14px;\">"
            + f"<thead><tr>{header_html}</tr></thead>"
            + f"<tbody>{''.join(row_html)}</tbody>"
            + "</table>"
        )

    return (
        "<div style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#1f2937;line-height:1.4;\">"
        f"<p style=\"margin:0 0 12px 0;\">Hi {greeting_name},</p>"
        + intro_html
        + inventory_results_html
        + snapdragon_disclaimer_html
        + in_stock_table_html
        + next_gen_section_html
        + f"{warranties_html}"
        + "<p style=\"margin:0;\">Best,<br>SKU Agent</p>"
        + "</div>"
    )


# ---------------------------------------------------------------------------
# Warranty Lookup — mybusinessservice.surface.com
# ---------------------------------------------------------------------------

_WARRANTY_BASE = "https://mybusinessservice.surface.com"
_WARRANTY_POST = _WARRANTY_BASE + "/en-US/CheckWarranty/CheckWarranty"
_WARRANTY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_WARRANTY_PROGRESS_LOCK = threading.Lock()
_WARRANTY_PROGRESS = {}
_WARRANTY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _dotnet_ticks_from_now(seconds_ago: int = 5) -> str:
    """Return a .NET DateTime.Ticks string representing (now - seconds_ago)."""
    # .NET ticks: 100-ns intervals since 0001-01-01
    # Unix epoch in .NET ticks = 621355968000000000
    dt = datetime.datetime.utcnow() - datetime.timedelta(seconds=seconds_ago)
    unix_ts = dt.timestamp()
    ticks = int(unix_ts * 10_000_000) + 621_355_968_000_000_000
    return str(ticks)


def _parse_warranty_response(serial: str, html_text: str) -> dict:
    """Parse the warranty result page and return a result dict."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return {"serial": serial, "device": "", "warranty_type": "", "start_date": "", "end_date": "", "status": "parse error", "note": "beautifulsoup4 not installed"}

    result = {"serial": serial, "device": "", "warranty_type": "", "start_date": "", "end_date": "", "status": "", "note": ""}

    # Error message from form
    err_div = soup.find("div", class_="error-message")
    if err_div:
        msg = err_div.get_text(strip=True)
        if msg:
            if "invalid captcha" in msg.lower():
                result["status"] = "captcha"
            else:
                result["status"] = "Not Found"
            result["note"] = msg
            return result

    if "request is blocked" in html_text.lower():
        result["status"] = "blocked"
        result["note"] = "Request blocked by site protection"
        return result

    # Look for warranty result rows outside the form
    main = soup.find("main")
    if not main:
        result["status"] = "parse error"
        result["note"] = "No main element found"
        return result

    # Remove the main form to isolate result content
    form = main.find("form")
    if form:
        # Grab rows that appear AFTER the serial number input section
        # The result divs are siblings appended after the form closing tag
        pass  # We work with full main text below

    full_text = main.get_text(separator="\n", strip=True)

    # Extract date patterns (MM/DD/YYYY or YYYY-MM-DD or Month DD, YYYY)
    date_pattern = re.compile(
        r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4})\b",
        re.IGNORECASE,
    )
    dates = date_pattern.findall(full_text)

    # Try to find device name — look for "Surface" in text blocks
    device_match = re.search(r"(Surface [A-Za-z0-9 ]+?(?:Pro|Laptop|Go|Book|Studio|Hub)[A-Za-z0-9 ]*)", full_text)
    if device_match:
        result["device"] = device_match.group(1).strip()

    # Try to find warranty type keywords
    warranty_keywords = ["Extended", "Standard", "Accidental Damage", "Complete", "Limited Warranty", "Microsoft Complete"]
    for kw in warranty_keywords:
        if kw.lower() in full_text.lower():
            if result["warranty_type"]:
                result["warranty_type"] += f", {kw}"
            else:
                result["warranty_type"] = kw

    if len(dates) >= 2:
        result["start_date"] = dates[0]
        result["end_date"] = dates[-1]
    elif len(dates) == 1:
        result["end_date"] = dates[0]

    # Look for table rows which many warranty pages use
    rows = main.find_all("tr")
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) >= 2:
            label = cells[0].lower()
            value = cells[1]
            if "device" in label or "model" in label or "product" in label:
                result["device"] = value
            elif "warrant" in label:
                result["warranty_type"] = value
            elif "start" in label or "purchase" in label:
                result["start_date"] = value
            elif "end" in label or "expir" in label:
                result["end_date"] = value

    # Look for definition-list style rows (dt/dd pairs)
    dts = main.find_all("dt")
    dds = main.find_all("dd")
    for dt, dd in zip(dts, dds):
        label = dt.get_text(strip=True).lower()
        value = dd.get_text(strip=True)
        if "device" in label or "model" in label or "product" in label:
            result["device"] = value
        elif "warrant" in label:
            result["warranty_type"] = value
        elif "start" in label:
            result["start_date"] = value
        elif "end" in label or "expir" in label:
            result["end_date"] = value

    # Fallback: collect all key-value looking lines (Label: Value)
    if not result["device"] and not result["warranty_type"]:
        for line in full_text.split("\n"):
            line = line.strip()
            if ":" in line:
                parts = line.split(":", 1)
                label = parts[0].strip().lower()
                value = parts[1].strip()
                if "device" in label or "model" in label or "product" in label:
                    result["device"] = value
                elif "warrant" in label:
                    result["warranty_type"] = value
                elif "start" in label:
                    result["start_date"] = value
                elif "end" in label or "expir" in label:
                    result["end_date"] = value

    if result["device"] or result["warranty_type"] or result["end_date"]:
        result["status"] = "Found"
    else:
        # Capture the raw result text for debugging
        result["status"] = "Found (raw)"
        # Get text outside the form
        form_tag = main.find("form")
        if form_tag:
            after_form = "".join(str(s) for s in form_tag.next_siblings)
            from bs4 import BeautifulSoup as BS2
            result["note"] = BS2(after_form, "html.parser").get_text(separator=" ", strip=True)[:300]
        else:
            result["note"] = full_text[-300:]

    return result


def _extract_warranty_form_state(html_text: str) -> dict:
    """Extract form state required by the warranty POST endpoint."""
    token_match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', html_text)
    loaded_match = re.search(r'id="hdnFormLoadedAt"[^>]*value="([^"]+)"', html_text)
    lang_match = re.search(r'id="hdnCurrentLanguage"[^>]*name="CurrentLanguage"[^>]*value="([^"]+)"', html_text)

    return {
        "token": token_match.group(1) if token_match else "",
        "form_loaded_at": loaded_match.group(1) if loaded_match else "",
        "current_language": lang_match.group(1) if lang_match else "en-US",
    }


def run_warranty_lookups(serials: list, country_code: str = "USA", progress_cb=None) -> list:
    """
    Look up warranty info for a list of serial numbers.
    Returns a list of result dicts.
    """
    try:
        import requests as _req
        from bs4 import BeautifulSoup  # noqa: F401  (ensure it's available)
    except ImportError as e:
        return [{"serial": s, "device": "", "warranty_type": "", "start_date": "", "end_date": "", "status": "error", "note": str(e)} for s in serials]

    session = _req.Session()
    session.headers.update(_WARRANTY_HEADERS)

    results = []
    total = len(serials)

    for i, serial in enumerate(serials):
        serial = serial.strip()
        if not serial:
            continue

        if progress_cb:
            progress_cb(i, total, serial)

        serial_result = None
        for attempt in range(3):
            try:
                form_resp = session.get(_WARRANTY_BASE + "/", timeout=20)
                form_resp.raise_for_status()
                form_state = _extract_warranty_form_state(form_resp.text)
                if not form_state["token"] or not form_state["form_loaded_at"]:
                    serial_result = {
                        "serial": serial,
                        "device": "",
                        "warranty_type": "",
                        "start_date": "",
                        "end_date": "",
                        "status": "error",
                        "note": "Could not read warranty form token/state",
                    }
                    break

                # This site appears to validate real elapsed time between the
                # form GET and POST, so use a real dwell instead of a synthetic timestamp.
                base_delay = 2.8 + attempt
                time.sleep(random.uniform(base_delay, base_delay + 0.8))

                post_data = {
                    "CurrentLanguage": form_state["current_language"],
                    "SelectedCountry": country_code,
                    "InputSerialNumber": serial,
                    "WebsiteUrl": "",  # honeypot — must be empty
                    "FormLoadedAt": form_state["form_loaded_at"],
                    "__RequestVerificationToken": form_state["token"],
                }

                resp = session.post(
                    _WARRANTY_POST,
                    data=post_data,
                    headers={
                        "Referer": _WARRANTY_BASE + "/",
                        "Origin": _WARRANTY_BASE,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=30,
                    allow_redirects=True,
                )
                parsed = _parse_warranty_response(serial, resp.text)

                if parsed.get("status") in {"captcha", "blocked"} and attempt < 2:
                    time.sleep(random.uniform(1.0 + attempt, 1.8 + attempt))
                    continue

                serial_result = parsed
                break
            except Exception as e:
                serial_result = {
                    "serial": serial,
                    "device": "",
                    "warranty_type": "",
                    "start_date": "",
                    "end_date": "",
                    "status": "error",
                    "note": str(e),
                }

        if not serial_result:
            serial_result = {
                "serial": serial,
                "device": "",
                "warranty_type": "",
                "start_date": "",
                "end_date": "",
                "status": "captcha",
                "note": "Invalid Captcha Request after retries",
            }
        results.append(serial_result)

        # Keep a moderate gap between serials to avoid tripping site protection.
        if i < total - 1:
            time.sleep(random.uniform(0.7, 1.1))

    if progress_cb:
        progress_cb(total, total, "")

    return results


def _update_warranty_progress(job_id: str, done: int, total: int, current_serial: str = ""):
    with _WARRANTY_PROGRESS_LOCK:
        state = _WARRANTY_PROGRESS.setdefault(job_id, {})
        state.update({
            "done": done,
            "total": total,
            "current_serial": current_serial,
        })


def _get_warranty_progress(job_id: str) -> dict:
    with _WARRANTY_PROGRESS_LOCK:
        return dict(_WARRANTY_PROGRESS.get(job_id, {}))


def _clear_warranty_progress(job_id: str):
    with _WARRANTY_PROGRESS_LOCK:
        _WARRANTY_PROGRESS.pop(job_id, None)


def run_warranty_lookup_job(job_id: str, serials: list, country_code: str = "USA") -> list:
    _update_warranty_progress(job_id, 0, len(serials), "")

    def _progress(done, total, current_serial):
        _update_warranty_progress(job_id, done, total, current_serial)

    try:
        return run_warranty_lookups(serials, country_code=country_code, progress_cb=_progress)
    finally:
        _update_warranty_progress(job_id, len(serials), len(serials), "")


def collect_warranty_serials(manual_serials_text: str, uploaded_serials_file) -> tuple[list, str]:
    serial_list = []
    upload_error = ""

    if manual_serials_text.strip():
        for line in manual_serials_text.strip().splitlines():
            serial = line.strip().strip(",").strip()
            if serial:
                serial_list.append(serial)

    if uploaded_serials_file:
        try:
            uploaded_serials_file.seek(0)
            content = uploaded_serials_file.read().decode("utf-8", errors="ignore")
            for line in content.splitlines():
                first_col = line.split(",")[0].strip().strip('"').strip()
                if first_col and first_col.lower() not in ("serial", "serial number", "sn", "serialnumber"):
                    serial_list.append(first_col)
        except Exception as exc:
            upload_error = str(exc)

    seen = set()
    deduped = []
    for serial in serial_list:
        key = serial.upper()
        if key not in seen:
            seen.add(key)
            deduped.append(serial)

    return deduped, upload_error


def sync_warranty_lookup_state():
    future = st.session_state.get("warranty_lookup_future")
    if not future or not future.done():
        return

    job_id = st.session_state.get("warranty_lookup_job_id", "")

    try:
        results = future.result()
        error = ""
    except Exception as exc:
        results = []
        error = str(exc)

    st.session_state["warranty_lookup_results"] = results
    st.session_state["warranty_lookup_error"] = error
    st.session_state["warranty_lookup_running"] = False
    st.session_state["warranty_lookup_future"] = None
    st.session_state["warranty_lookup_job_id"] = ""
    st.session_state["warranty_lookup_completed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if job_id:
        _clear_warranty_progress(job_id)


def render_warranty_lookup_results(results):
    if not results:
        st.warning("Warranty lookup completed, but no results were returned.")
        return

    import pandas as pd

    df = pd.DataFrame(results, columns=["serial", "device", "warranty_type", "start_date", "end_date", "status", "note"])
    df.columns = ["Serial", "Device", "Warranty Type", "Start Date", "End Date", "Status", "Note"]

    found_df = df[df["Status"].isin(["Found", "Found (raw)"])]
    notfound_df = df[~df["Status"].isin(["Found", "Found (raw)"])]

    if not found_df.empty:
        st.success(f"Found warranty info for {len(found_df)} serial(s)")
        st.dataframe(
            found_df[["Serial", "Device", "Warranty Type", "Start Date", "End Date", "Note"]],
            use_container_width=True,
            hide_index=True,
        )
        csv_bytes = found_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download results as CSV",
            data=csv_bytes,
            file_name="warranty_results.csv",
            mime="text/csv",
        )

    if not notfound_df.empty:
        with st.expander(f"{len(notfound_df)} serial(s) not found or errored", expanded=len(found_df) == 0):
            st.dataframe(
                notfound_df[["Serial", "Status", "Note"]],
                use_container_width=True,
                hide_index=True,
            )


def render_email_preview_with_copy(html_content, preview_title, copy_key):
        """Render HTML preview and a one-click copy button for the full HTML content."""
        with st.expander(preview_title, expanded=True):
                st.markdown(html_content, unsafe_allow_html=True)

        escaped = (
                html_content
                .replace("\\", "\\\\")
                .replace("`", "\\`")
                .replace("${", "\\${")
        )


def render_text_copy_button(copy_text, copy_key, button_label="Copy Results"):
    """Render a one-click copy button for plain text/tabular output."""
    escaped = (
        clean_text(copy_text)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )

    components.html(
        f"""
        <div style=\"margin:8px 0 6px 0;\">
            <button id=\"copy-text-btn-{copy_key}\" style=\"background:#0067b8;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-weight:600;cursor:pointer;\">{button_label}</button>
            <span id=\"copy-text-status-{copy_key}\" style=\"margin-left:10px;color:#0f5132;font-weight:600;\"></span>
        </div>
        <script>
        (function() {{
            const btn = document.getElementById('copy-text-btn-{copy_key}');
            const status = document.getElementById('copy-text-status-{copy_key}');
            const payload = `{escaped}`;
            btn.addEventListener('click', async () => {{
                try {{
                    await navigator.clipboard.writeText(payload);
                    status.textContent = 'Copied';
                    setTimeout(() => status.textContent = '', 1500);
                }} catch (err) {{
                    status.textContent = 'Clipboard blocked by browser';
                }}
            }});
        }})();
        </script>
        """,
        height=48,
    )


def current_to_next_gen_rows_to_tsv(rows):
    """Convert current-to-next-gen table rows to tab-delimited text."""
    headers = [
        "Input SKU",
        "Product Description",
        "Next Gen SKU",
        "Next Gen Product Description",
    ]
    lines = ["\t".join(headers)]

    for row in rows:
        values = [
            clean_text(row.get("Input SKU", "")),
            clean_text(row.get("Product Description", "")),
            clean_text(row.get("Next Gen SKU", "")),
            clean_text(row.get("Next Gen Product Description", "")),
        ]
        lines.append("\t".join(values))

    return "\n".join(lines)


@st.cache_data
def load_ask_rj_knowledge_text(knowledge_file_path=None):
    """Load Ask RJ knowledge context from local workbook/text file."""
    path = Path(knowledge_file_path) if knowledge_file_path else resolve_data_file("Ask RJ.xlsx")
    if not path.exists():
        return "", f"Ask RJ knowledge file not found: {path}"

    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        try:
            return path.read_text(encoding="utf-8", errors="ignore"), f"Ask RJ knowledge loaded from {path.name}"
        except Exception as exc:
            return "", f"Unable to read Ask RJ knowledge file: {exc}"

    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
    except PermissionError:
        return "", f"Ask RJ knowledge file is locked: {path}"
    except Exception as exc:
        return "", f"Unable to read Ask RJ knowledge file: {exc}"

    lines = []
    for sheet in wb.worksheets:
        lines.append(f"[{sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            values = [clean_text(v) for v in row if clean_text(v)]
            if values:
                lines.append(" | ".join(values))
        lines.append("")

    knowledge_text = "\n".join(lines).strip()
    return knowledge_text, f"Ask RJ knowledge loaded from {path.name}"


def ask_rj_with_openai(user_question, rj_knowledge, api_key=None):
    """Query OpenAI with RJ-style system instructions and local knowledge context."""
    resolved_api_key = clean_text(api_key)
    if not resolved_api_key:
        resolved_api_key = clean_text(os.getenv("OPENAI_API_KEY"))
    if not resolved_api_key:
        try:
            resolved_api_key = clean_text(st.secrets.get("OPENAI_API_KEY", ""))
        except Exception:
            resolved_api_key = ""

    api_key = resolved_api_key
    if not api_key:
        return "", "Set OPENAI_API_KEY in environment or Streamlit secrets to use Ask RJ."

    try:
        from openai import OpenAI
    except Exception:
        return "", "OpenAI package not installed. Run: py -m pip install openai"

    system_prompt = (
        "You are \"Ask RJ\" - a Microsoft Surface technical sales expert.\n\n"
        "You must combine:\n"
        "1. Technical expertise (RJ knowledge)\n"
        "2. SKU recommendation logic from the application\n"
        "3. Forward-looking product strategy (next-gen awareness)\n\n"
        "CRITICAL LOGIC (MANDATORY):\n\n"
        "If a user requirement EXCEEDS current-generation Surface capabilities:\n\n"
        "- You MUST recognize the gap\n"
        "- You MUST explicitly state that current SKUs do NOT meet the requirement\n"
        "- You MUST recommend NEXT GENERATION SKUs (if available in provided data)\n"
        "- You MUST position the next-gen devices as the correct path forward\n\n"
        "Examples of this include:\n"
        "- Requests for 64GB RAM\n"
        "- Unsupported configurations\n"
        "- Hardware limitations due to SOC or architecture\n\n"
        "If next-generation SKUs are provided in the data:\n"
        "- Prioritize those over current-gen SKUs when they better meet the requirement\n\n"
        "SKU HANDLING RULES:\n\n"
        "- Use ONLY SKUs provided to you in the context\n"
        "- NEVER hallucinate SKUs\n"
        "- If both current-gen and next-gen SKUs are present:\n"
        "  - Compare them and recommend the best-fit option\n"
        "- If requirement cannot be met by any SKU:\n"
        "  - Say so clearly and recommend waiting or alternative strategy\n\n"
        "RESPONSE STRUCTURE (ALWAYS FOLLOW):\n\n"
        "Answer:\n"
        "Direct answer to the question\n\n"
        "Constraints / Risks:\n"
        "Explain why current generation devices may not meet the requirement\n\n"
        "Recommended SKUs:\n"
        "- Prioritize NEXT GEN SKUs if they solve the requirement\n"
        "- Include reasoning for each SKU\n\n"
        "Recommendation:\n"
        "Clear next step (wait, position new devices, shift requirements, etc.)\n\n"
        "What RJ would ask back:\n"
        "1-2 questions to move the deal forward\n\n"
        "STYLE:\n\n"
        "- Direct and confident\n"
        "- No fluff\n"
        "- Sales-aware and solution-oriented\n"
        "- Forward-looking (think roadmap, not just current inventory)\n\n"
        "GOAL:\n\n"
        "Help sales reps:\n"
        "- Avoid recommending incorrect SKUs\n"
        "- Position future products strategically\n"
        "- Move deals forward intelligently"
    )

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": clean_text(rj_knowledge)},
                {"role": "user", "content": clean_text(user_question)},
            ],
            temperature=0.2,
        )
        content = clean_text(response.choices[0].message.content if response.choices else "")
        if not content:
            return "", "Ask RJ returned an empty response."
        return content, ""
    except Exception as exc:
        return "", f"Ask RJ request failed: {exc}"


LOCAL_LLM_DEFAULT_MODEL = "phi3:mini"


def _extract_json_object(text):
    raw = clean_text(text)
    if not raw:
        return {}

    start = raw.find("{")
    if start == -1:
        return {}

    depth = 0
    end = -1
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break

    if end == -1:
        return {}

    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def local_ollama_chat(messages, model=LOCAL_LLM_DEFAULT_MODEL, temperature=0.1, timeout_seconds=35):
    """Call local Ollama chat API and return (content, error)."""
    host = clean_text(os.getenv("OLLAMA_HOST")) or "http://127.0.0.1:11434"
    url = host.rstrip("/") + "/api/chat"
    payload = {
        "model": clean_text(model) or LOCAL_LLM_DEFAULT_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8", errors="ignore"))
        content = clean_text(((data or {}).get("message") or {}).get("content", ""))
        if not content:
            return "", "Local LLM returned an empty response."
        return content, ""
    except urllib.error.HTTPError as exc:
        details = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                details = clean_text(parsed.get("error", ""))
            if not details:
                details = clean_text(body)
        except Exception:
            details = ""

        if exc.code == 404 and details:
            return "", f"Local Ollama returned 404: {details}"
        if details:
            return "", f"Local Ollama request failed ({exc.code}): {details}"
        return "", f"Local Ollama request failed ({exc.code}): {exc.reason}"
    except urllib.error.URLError as exc:
        return "", f"Could not connect to local Ollama at {host}: {exc}"
    except Exception as exc:
        return "", f"Local Ollama request failed: {exc}"


def heuristic_route_query(user_query):
    text = clean_text(user_query)
    lower = text.lower()
    extracted_skus = parse_bulk_sku_input(text)

    accessory_signals = [
        "keyboard", "dock", "mouse", "hub", "pen", "stylus", "charger", "adapter", "cover", "folio",
    ]
    mapping_signals = ["next gen", "next-gen", "equivalent", "replacement", "map", "upgrade path"]

    if extracted_skus and any(signal in lower for signal in mapping_signals):
        return {
            "intent": "next_gen_mapping",
            "skus": extracted_skus,
            "query": text,
            "reason": "Detected SKU(s) with next-gen/replacement intent.",
        }

    if any(signal in lower for signal in accessory_signals):
        return {
            "intent": "accessory_lookup",
            "skus": extracted_skus,
            "query": text,
            "reason": "Detected accessory keywords.",
        }

    if extracted_skus:
        return {
            "intent": "sku_lookup",
            "skus": extracted_skus,
            "query": text,
            "reason": "Detected SKU lookup pattern.",
        }

    return {
        "intent": "description_search",
        "skus": [],
        "query": text,
        "reason": "Defaulted to description search.",
    }


def llm_route_query(user_query, model_name):
    fallback = heuristic_route_query(user_query)
    system_prompt = (
        "You route a Surface sales query to one intent. Return ONLY valid JSON.\\n"
        "Allowed intents: sku_lookup, description_search, accessory_lookup, next_gen_mapping.\\n"
        "Required JSON shape: "
        "{\"intent\":\"...\",\"skus\":[\"EP2-12345\"],\"query\":\"...\",\"reason\":\"...\"}.\\n"
        "Rules:\\n"
        "- Use next_gen_mapping when user asks replacement/equivalent/next gen for SKU(s).\\n"
        "- Use accessory_lookup for accessories/peripherals.\\n"
        "- Use sku_lookup when explicit SKU(s) are primary request.\\n"
        "- Otherwise use description_search."
    )

    content, err = local_ollama_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_text(user_query)},
        ],
        model=model_name,
        temperature=0.0,
    )
    if err:
        return fallback, err

    parsed = _extract_json_object(content)
    intent = clean_text(parsed.get("intent", "")).lower()
    if intent not in {"sku_lookup", "description_search", "accessory_lookup", "next_gen_mapping"}:
        return fallback, "Local LLM routing output was invalid. Used heuristic routing instead."

    skus = parsed.get("skus", [])
    if not isinstance(skus, list):
        skus = []
    normalized_skus = parse_bulk_sku_input("\n".join(clean_text(s) for s in skus))

    routed = {
        "intent": intent,
        "skus": normalized_skus,
        "query": clean_text(parsed.get("query", "")) or clean_text(user_query),
        "reason": clean_text(parsed.get("reason", "")) or "Routed by local LLM.",
    }
    return routed, ""


def render_natural_language_answer(user_query, intent, evidence_lines, model_name, use_local_llm=True):
    context = "\n".join(evidence_lines)
    fallback = (
        f"Intent selected: {intent}\n\n"
        f"Answer:\n{context}"
    )

    if not use_local_llm:
        return fallback, ""

    prompt = (
        "You are a Surface SKU assistant. Produce a concise natural-language response from evidence only.\\n"
        "Do not invent SKUs.\\n"
        "Always include a 'Next Gen Equivalent' section, even if result is Not Found.\\n"
        "If no mapped equivalent exists, explicitly say Not Found.\\n"
        "Use this structure:\\n"
        "Answer:\\n"
        "Inventory/Match Summary:\\n"
        "Next Gen Equivalent:\\n"
        "Recommended Next Step:\\n"
    )

    content, err = local_ollama_chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"User query: {clean_text(user_query)}\\nIntent: {intent}\\nEvidence:\\n{context}"},
        ],
        model=model_name,
        temperature=0.2,
    )
    if err:
        return fallback, err
    return content, ""


st.set_page_config(page_title="SKU Agent", layout="wide")
apply_microsoft_like_theme()

inventory_upload_manifest = load_inventory_upload_manifest()
latest_inventory_upload_label = get_latest_inventory_upload_label(inventory_upload_manifest)
st.markdown(
    f"""
    <div class=\"inventory-upload-rail\">
        <div class=\"inventory-upload-icon\">📎</div>
        <div>
            <div class=\"inventory-upload-title\">Upload Inventory reports here</div>
            <div class=\"inventory-upload-meta\">Most recent inventory reports loaded: {html.escape(latest_inventory_upload_label)}</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)
uploaded_inventory_files = st.file_uploader(
    "Inventory reports",
    type=["csv", "xlsx"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

st.title("SKU Agent")
st.caption("SKU lookup, daily inventory upload, part-list driven replacement mapping, and fuzzy inventory search.")

with st.sidebar:
    st.markdown("### Part List")
    st.caption("Default part list is auto-loaded. Upload below to override.")
    uploaded_part_list_xlsx = st.file_uploader("Part List (optional update: XLSX or CSV)", type=["xlsx", "csv"])
    st.markdown("### Local LLM")
    use_local_llm = st.toggle("Use local LLM routing + response", value=True, key="use_local_llm_toggle")
    local_llm_model = st.text_input("Local model name", value=LOCAL_LLM_DEFAULT_MODEL, key="local_llm_model_name")
    st.caption("Requires Ollama running locally. Example: ollama pull phi4-mini")

part_list_map = {}
part_list_profiles = {}
part_list_lookup = {}
part_list_status = ""

preferred_part_list_files = [
    DEFAULT_PART_LIST_FILE,
    resolve_data_file("Pro 10 5G SKUs.xlsx"),
    resolve_data_file("Pro 12 SKU List.xlsx"),
]
part_list_sources = []
seen_part_list_sources = set()
for source_file in preferred_part_list_files:
    resolved_source = Path(source_file)
    if not resolved_source.exists():
        continue
    source_key = resolved_source.resolve()
    if source_key in seen_part_list_sources:
        continue
    seen_part_list_sources.add(source_key)
    part_list_sources.append(resolved_source)

loaded_part_list_map, loaded_part_list_profiles, loaded_part_list_status = load_part_list_sources(part_list_sources)
if loaded_part_list_profiles or loaded_part_list_map:
    part_list_map = loaded_part_list_map
    part_list_profiles = loaded_part_list_profiles
    part_list_status = f"Default part list sources loaded: {loaded_part_list_status}"
elif loaded_part_list_status:
    part_list_status = loaded_part_list_status

if uploaded_part_list_xlsx:
    uploaded_name = clean_text(uploaded_part_list_xlsx.name).lower()
    if uploaded_name.endswith(".csv"):
        uploaded_text = uploaded_part_list_xlsx.getvalue().decode("utf-8-sig")
        part_list_map, part_list_profiles, part_list_error = parse_part_list_csv_text(uploaded_text, uploaded_part_list_xlsx.name)
    else:
        part_list_map, part_list_profiles, part_list_error = parse_part_list_xlsx_bytes(uploaded_part_list_xlsx.getvalue())
    if part_list_error:
        part_list_status = part_list_error
    else:
        part_list_status = f"Updated part list loaded: {len(part_list_map)} source SKU(s)"

inventory_rows = []

inventory_source_parts = []
resolved_inventory_sources = {}

if uploaded_inventory_files:
    for uploaded_file in uploaded_inventory_files:
        source_name = clean_text(getattr(uploaded_file, "name", "")) or "uploaded inventory"
        distributor = infer_inventory_distributor_from_name(source_name)
        if not distributor:
            parsed_rows = []
            if source_name.lower().endswith(".xlsx"):
                try:
                    from openpyxl import load_workbook

                    workbook = load_workbook(io.BytesIO(uploaded_file.getvalue()), data_only=True, read_only=True)
                    for sheet in workbook.worksheets:
                        for row in sheet.iter_rows(values_only=True):
                            parsed_rows.append([clean_text(cell) for cell in row])
                            if len(parsed_rows) >= 50:
                                break
                        if parsed_rows:
                            break
                except Exception:
                    parsed_rows = []
            else:
                try:
                    parsed_rows = list(csv.reader(io.StringIO(uploaded_file.getvalue().decode("utf-8-sig"))))
                except Exception:
                    parsed_rows = []
            distributor = detect_inventory_distributor_from_rows(parsed_rows)

        if not distributor:
            st.warning(f"Skipping {source_name}: unable to determine distributor.")
            continue

        save_inventory_upload_cache(uploaded_file, distributor)
        resolved_inventory_sources[distributor] = {
            "kind": "uploaded",
            "source": uploaded_file,
            "source_name": source_name,
        }

cached_manifest = load_inventory_upload_manifest()
for distributor in INVENTORY_DISTI_NAMES:
    if distributor in resolved_inventory_sources:
        continue

    cached_meta = cached_manifest.get(distributor, {}) if isinstance(cached_manifest, dict) else {}
    cached_name = clean_text(cached_meta.get("file_name", ""))
    cached_path = INVENTORY_UPLOAD_CACHE_DIR / cached_name if cached_name else None
    if cached_path and cached_path.exists():
        resolved_inventory_sources[distributor] = {
            "kind": "cached",
            "source": cached_path,
            "source_name": clean_text(cached_meta.get("source_name", "")) or cached_path.name,
        }
        continue

    default_candidates = [
        name for name, mapped_distributor in DEFAULT_INVENTORY_FILES.items()
        if mapped_distributor == distributor
    ]
    for candidate_name in default_candidates:
        candidate_path = resolve_data_file(candidate_name)
        if candidate_path.exists():
            resolved_inventory_sources[distributor] = {
                "kind": "default",
                "source": candidate_path,
                "source_name": candidate_path.name,
            }
            break

for distributor in INVENTORY_DISTI_NAMES:
    source_info = resolved_inventory_sources.get(distributor)
    if not source_info:
        continue

    source = source_info["source"]
    source_name = source_info["source_name"]
    if source_info["kind"] == "uploaded":
        inventory_rows.extend(load_inventory_rows_from_uploaded_file(source, distributor))
    else:
        inventory_rows.extend(load_inventory_rows_from_path(source, distributor, source_name))
    inventory_source_parts.append(f"{distributor}: {source_name} ({source_info['kind']})")

# Keep accessories available even when distributor CSV uploads are used.
accessories_file = resolve_data_file("Surface Accessories.csv")
if accessories_file.exists():
    accessories_records = read_inventory_file(accessories_file, "DandH")
    inventory_rows.extend(parse_inventory_records(accessories_records, "DandH", accessories_file.name))
    inventory_source_parts.append(f"Accessories: {accessories_file.name} (default)")

if not inventory_rows:
    inventory_rows = load_default_inventory_files()
    inventory_source_label = f"Using default inventory files from {DATA_DIR}"
else:
    inventory_source_label = "Using " + "; ".join(inventory_source_parts)

inventory_by_sku = aggregate_inventory(inventory_rows)
sku_lookup = build_inventory_sku_lookup(inventory_rows)
part_list_lookup = build_part_list_sku_lookup(part_list_profiles)

# Merge part-list profiles into runtime lookup to enable description-based matching.
for sku, profile in part_list_lookup.items():
    if sku not in sku_lookup:
        sku_lookup[sku] = profile
    elif len(clean_text(profile.get("raw_description"))) > len(clean_text(sku_lookup[sku].get("raw_description"))):
        merged = dict(sku_lookup[sku])
        merged.update({k: v for k, v in profile.items() if clean_text(v)})
        sku_lookup[sku] = merged

# Include accessories (keyboard, dock, mouse, pen, etc.) from inventory in the search lookup.
# This ensures accessories are searchable via description search.
ACCESSORY_KEYWORDS = r"\b(keyboard|kb|flex|pen|stylus|dock|mouse|charger|hub|adapter|cover|type\s*cover|folio)\b"
for sku, inventory_item in sku_lookup.items():
    desc = clean_text(inventory_item.get("raw_description", "")).lower()
    if re.search(ACCESSORY_KEYWORDS, desc) and sku not in part_list_lookup:
        # Add this accessory to the part_list_lookup so it's searchable
        part_list_lookup[sku] = inventory_item

tab_smart, tab_warranty, tab_config = st.tabs([
    "Smart Assistant",
    "Warranty Lookup",
    "SKU Config Assistant",
])

with tab_smart:
    st.caption("One natural-language prompt. The app auto-selects SKU lookup, description search, accessory search, or next-gen mapping.")

    smart_query = st.text_area(
        "Ask anything",
        height=210,
        placeholder=(
            "Examples:\n"
            "- Need 25 units of EP2-33242 and alternatives\n"
            "- Find a Surface Laptop 15-inch Ultra 7 32GB 1TB\n"
            "- Recommend keyboard + dock + mouse for Surface Pro\n"
            "- Map EP2-37086, EP2-33245 to next gen"
        ),
        key="smart_query_input",
    )
    include_warranties_smart = st.toggle(
        "Include all compatible warranties in generated email outputs",
        value=True,
        key="include_warranties_smart",
    )

    if st.button("Run smart search", type="primary", key="smart_search_btn"):
        user_query = clean_text(smart_query)
        if not user_query:
            st.warning("Please enter a question or request.")
        elif not part_list_lookup:
            st.warning("No part list is loaded. Upload/update the part list and try again.")
        else:
            if use_local_llm:
                plan, route_err = llm_route_query(user_query, local_llm_model)
            else:
                plan, route_err = heuristic_route_query(user_query), ""

            if route_err:
                st.caption(route_err)

            intent = clean_text(plan.get("intent", "description_search")).lower()
            routed_query = clean_text(plan.get("query", "")) or user_query
            routed_skus = plan.get("skus", []) if isinstance(plan.get("skus", []), list) else []

            st.caption(f"Selected workflow: {intent} ({clean_text(plan.get('reason', '')) or 'auto-routed'})")

            evidence = []

            if intent == "sku_lookup":
                requested_skus = routed_skus or extract_skus(routed_query)
                if not requested_skus:
                    st.warning("No valid SKU detected. Try a SKU like EP2-33242.")
                else:
                    requested_qtys = extract_quantities(routed_query, requested_skus)
                    rows = []
                    for sku in requested_skus:
                        stock = summarize_stock(inventory_by_sku.get(sku, []))
                        replacements = find_replacements_for_sku(
                            sku,
                            sku_lookup,
                            inventory_by_sku,
                            part_list_map,
                            part_list_lookup,
                            email_requests_more_ram(routed_query),
                        )
                        top_replacement = replacements[0]["replacement_sku"] if replacements else "Not Found"
                        top_replacement_desc = replacements[0]["replacement_description"] if replacements else "No mapped equivalent"
                        rows.append({
                            "SKU": sku,
                            "Requested Qty": format_qty_need(requested_qtys.get(sku)),
                            "On Hand": stock.get("total_on_hand", 0),
                            "On Order": stock.get("total_on_order", 0),
                            "Next Gen Equivalent": top_replacement,
                            "Next Gen Description": top_replacement_desc,
                        })
                        evidence.append(
                            f"SKU {sku}: on hand {stock.get('total_on_hand', 0)}, on order {stock.get('total_on_order', 0)}, "
                            f"next gen equivalent {top_replacement} ({top_replacement_desc})."
                        )

                    st.dataframe(rows, use_container_width=True, hide_index=True)

                    draft = build_sales_email_html(
                        routed_query,
                        requested_skus,
                        requested_qtys,
                        sku_lookup,
                        inventory_by_sku,
                        part_list_map,
                        part_list_lookup,
                        include_warranties_smart,
                    )
                    render_email_preview_with_copy(draft, "Preview HTML email", "smart_tab_sku")

            elif intent == "accessory_lookup":
                results, matcher_status = search_accessories_by_matcher(
                    routed_query,
                    part_list_lookup,
                    inventory_by_sku,
                    limit=9,
                )
                if matcher_status:
                    st.caption(matcher_status)

                if results:
                    st.dataframe(results, use_container_width=True, hide_index=True)
                    for item in results[:5]:
                        evidence.append(
                            f"Accessory {item.get('sku', '')}: {clean_text(item.get('description', ''))}, "
                            f"on hand {item.get('total_on_hand', 0)}, on order {item.get('total_on_order', 0)}."
                        )
                    evidence.append("Next Gen Equivalent: Not Found for accessory requests.")

                    draft = build_accessory_search_email_html(routed_query, results)
                    render_email_preview_with_copy(draft, "Preview HTML email", "smart_tab_accessory")
                else:
                    evidence.append("No accessory matches found.")
                    evidence.append("Next Gen Equivalent: Not Found.")
                    st.info("No accessory matches found. Try keyboard, dock, mouse, hub, pen, or charger.")

            elif intent == "next_gen_mapping":
                mapping_skus = routed_skus or parse_bulk_sku_input(routed_query)
                if not mapping_skus:
                    st.warning("Please include one or more SKUs to map.")
                else:
                    rows = build_current_to_next_gen_rows(
                        mapping_skus,
                        sku_lookup,
                        inventory_by_sku,
                        part_list_map,
                        part_list_lookup,
                    )
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                    for row in rows:
                        evidence.append(
                            f"Input {row.get('Input SKU', '')}: next gen {row.get('Next Gen SKU', 'Not Found')} "
                            f"({row.get('Next Gen Product Description', 'Not Found')})."
                        )

                    copy_payload = current_to_next_gen_rows_to_tsv(rows)
                    render_text_copy_button(copy_payload, "smart_tab_next_gen", "Copy Mapping Results")

            else:
                items = normalize_ae_query(routed_query)
                all_results = []
                for _, normalized_q in items:
                    results = search_products_by_description(normalized_q, part_list_lookup, inventory_by_sku)
                    if not results:
                        constraints = extract_query_constraints(normalized_q)
                        results = find_in_stock_alternatives(normalized_q, constraints, part_list_lookup, inventory_by_sku, limit=5)
                    all_results.extend(results)

                if all_results:
                    st.dataframe(all_results[:12], use_container_width=True, hide_index=True)
                    for item in all_results[:5]:
                        next_gen_note = "Not Found"
                        replacements = find_replacements_for_sku(
                            clean_text(item.get("sku", "")),
                            sku_lookup,
                            inventory_by_sku,
                            part_list_map,
                            part_list_lookup,
                        )
                        if replacements:
                            next_gen_note = replacements[0].get("replacement_sku", "Not Found")
                        evidence.append(
                            f"Match {item.get('sku', '')}: {clean_text(item.get('description', ''))}, "
                            f"score {item.get('score', 0)}, on hand {item.get('total_on_hand', 0)}, next gen {next_gen_note}."
                        )

                    constraints = extract_query_constraints(routed_query)
                    draft = build_description_search_email_html(
                        routed_query,
                        all_results[:8],
                        constraints,
                        part_list_lookup,
                        inventory_by_sku,
                        part_list_map,
                        include_warranties_smart,
                    )
                    render_email_preview_with_copy(draft, "Preview HTML email", "smart_tab_desc")
                else:
                    evidence.append("No matching products found.")
                    evidence.append("Next Gen Equivalent: Not Found.")
                    st.info("No matching products found for that request.")

            if evidence:
                answer_text, answer_err = render_natural_language_answer(
                    user_query,
                    intent,
                    evidence,
                    local_llm_model,
                    use_local_llm=use_local_llm,
                )
                if answer_err:
                    st.caption(answer_err)
                st.markdown("### Natural Language Response")
                st.markdown(answer_text)


with tab_warranty:
    sync_warranty_lookup_state()
    st.session_state.setdefault("warranty_lookup_running", False)
    st.session_state.setdefault("warranty_lookup_future", None)
    st.session_state.setdefault("warranty_lookup_job_id", "")
    st.session_state.setdefault("warranty_lookup_results", None)
    st.session_state.setdefault("warranty_lookup_error", "")
    st.session_state.setdefault("warranty_lookup_started_at", "")
    st.session_state.setdefault("warranty_lookup_completed_at", "")
    st.session_state.setdefault("warranty_lookup_serial_count", 0)
    st.session_state.setdefault("warranty_lookup_country_code", "USA")

    st.subheader("Surface Warranty Lookup")
    st.caption(
        "Enter one or more device serial numbers to check warranty status via "
        "[mybusinessservice.surface.com](https://mybusinessservice.surface.com). "
        "Each lookup takes about 1 second. Requests run in the background so you can keep using the app."
    )

    col_manual, col_upload = st.columns([1, 1], gap="large")

    with col_manual:
        st.markdown("**Enter serials manually** (one per line)")
        manual_serials_text = st.text_area(
            "Serial numbers",
            placeholder="ABC123456789\nDEF987654321\nGHI111222333",
            height=180,
            label_visibility="collapsed",
        )

    with col_upload:
        st.markdown("**Or upload a file** (CSV or TXT, one serial per row)")
        uploaded_serials_file = st.file_uploader(
            "Upload serial list",
            type=["csv", "txt"],
            label_visibility="collapsed",
            key="warranty_serial_upload",
        )

    # Country selector
    warranty_country = st.selectbox(
        "Country / Region",
        options=["USA", "CAN", "GBR", "AUS", "DEU", "FRA", "JPN", "MEX", "SGP", "ARE"],
        index=0,
        key="warranty_country",
    )

    if st.session_state.get("warranty_lookup_running"):
        progress_state = _get_warranty_progress(st.session_state.get("warranty_lookup_job_id", ""))
        done = int(progress_state.get("done", 0))
        total = max(1, int(progress_state.get("total", st.session_state.get("warranty_lookup_serial_count", 0) or 1)))
        current_serial = clean_text(progress_state.get("current_serial", ""))
        st.progress(min(done / total, 1.0))
        st.caption(
            f"Processed {done} of {total} serial(s)"
            + (f". Currently checking: {current_serial}" if current_serial else ".")
        )
        st.info(
            f"Warranty lookup running in background for {st.session_state.get('warranty_lookup_serial_count', 0)} "
            f"serial(s) in {st.session_state.get('warranty_lookup_country_code', warranty_country)}. "
            "Keep using the other tabs and come back here to refresh or view results."
        )
        st.caption(f"Started at {st.session_state.get('warranty_lookup_started_at', '')}")
        refresh_col, clear_col = st.columns([1, 1])
        with refresh_col:
            st.button("Refresh status", key="warranty_refresh_status_btn")
        with clear_col:
            st.button("Clear completed results", key="warranty_clear_results_btn", disabled=True)
    else:
        refresh_col, clear_col = st.columns([1, 1])
        with refresh_col:
            st.button("Refresh status", key="warranty_refresh_status_btn_idle")
        with clear_col:
            if st.button("Clear completed results", key="warranty_clear_results_btn_idle"):
                st.session_state["warranty_lookup_results"] = None
                st.session_state["warranty_lookup_error"] = ""
                st.session_state["warranty_lookup_completed_at"] = ""

    if st.button(
        "Look up warranties",
        type="primary",
        key="warranty_lookup_btn",
        disabled=st.session_state.get("warranty_lookup_running", False),
    ):
        serial_list, upload_error = collect_warranty_serials(manual_serials_text, uploaded_serials_file)

        if upload_error:
            st.error(f"Could not read uploaded file: {upload_error}")
        elif not serial_list:
            st.warning("Please enter at least one serial number or upload a file.")
        else:
            job_id = uuid.uuid4().hex
            st.session_state["warranty_lookup_running"] = True
            st.session_state["warranty_lookup_error"] = ""
            st.session_state["warranty_lookup_results"] = None
            st.session_state["warranty_lookup_job_id"] = job_id
            st.session_state["warranty_lookup_started_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state["warranty_lookup_completed_at"] = ""
            st.session_state["warranty_lookup_serial_count"] = len(serial_list)
            st.session_state["warranty_lookup_country_code"] = warranty_country
            _update_warranty_progress(job_id, 0, len(serial_list), "")
            st.session_state["warranty_lookup_future"] = _WARRANTY_EXECUTOR.submit(
                run_warranty_lookup_job,
                job_id,
                serial_list,
                warranty_country,
            )
            st.success(
                f"Started background warranty lookup for {len(serial_list)} serial(s). "
                "You can keep using Email / SKU Lookup and Description Search while it runs."
            )

    if st.session_state.get("warranty_lookup_error"):
        st.error(f"Warranty lookup failed: {st.session_state['warranty_lookup_error']}")

    completed_at = st.session_state.get("warranty_lookup_completed_at")
    if completed_at and not st.session_state.get("warranty_lookup_running"):
        st.caption(f"Last completed at {completed_at}")

    if st.session_state.get("warranty_lookup_results") is not None and not st.session_state.get("warranty_lookup_running"):
        render_warranty_lookup_results(st.session_state.get("warranty_lookup_results"))


def parse_sku_config_request(user_input, part_list_lookup, sku_lookup=None, inventory_by_sku=None, part_list_map=None):
    """Interpret a natural-language SKU configuration request and find prioritized matching SKUs.

    Returns a dict with keys:
        base_sku, product_family, intent_type,
        requested_changes: {RAM, Storage},
        matched_skus_current_gen: list of current-gen SKUs matching exact specs
        matched_skus_next_gen: list of next-gen SKUs (same specs or closest higher RAM)
        recommended_sku: single best match (current-gen preferred, else next-gen)
        all_results: [{sku, generation, description, ram, storage, stock, score, reason}]
    """
    text = clean_text(user_input)
    text_lower = text.lower()

    # ── Extract any explicit SKU mentioned ────────────────────────────────────
    extracted = extract_normalized_skus(text)
    base_sku = extracted[0] if extracted else ""

    # ── Derive product family ─────────────────────────────────────────────────
    product_family = ""
    if base_sku and part_list_lookup and base_sku in part_list_lookup:
        profile = part_list_lookup[base_sku]
        product_family = clean_text(profile.get("product_line", "")) or clean_text(profile.get("raw_description", ""))
    if not product_family:
        if re.search(r"surface\s+pro", text_lower):
            product_family = "Surface Pro"
        elif re.search(r"surface\s+laptop", text_lower) or re.search(r"\blaptop\b", text_lower):
            product_family = "Surface Laptop"

    # ── Requested RAM ─────────────────────────────────────────────────────────
    ram = ""
    ram_match = re.search(
        r"(?:change|upgrade|with|set|make it|to)\s+(?:the\s+)?(?:ram|memory)\s+(?:to\s+)?(\d+)\s*gb"
        r"|\b(\d+)\s*gb\s+(?:ram|memory)\b"
        r"|\bram\s*[=:]?\s*(\d+)\s*gb\b",
        text_lower,
    )
    if ram_match:
        raw = next(g for g in ram_match.groups() if g)
        if raw in {"8", "16", "24", "32", "64"}:
            ram = f"{raw}GB"

    # ── Requested Storage ─────────────────────────────────────────────────────
    storage = ""
    storage_match = re.search(
        r"(?:change|upgrade|with|set|make it|to)\s+(?:the\s+)?(?:storage|ssd|drive)\s+(?:to\s+)?(\d+)\s*(?:gb|tb)"
        r"|\b(\d+)\s*(?:gb|tb)\s+(?:ssd|storage|drive)\b"
        r"|\bstorage\s*[=:]?\s*(\d+)\s*(?:gb|tb)\b",
        text_lower,
    )
    if storage_match:
        raw_val = next(g for g in storage_match.groups() if g)
        # normalise 1TB -> 1024
        tb_match = re.search(r"(\d+)\s*tb", text_lower[storage_match.start():storage_match.end()])
        if tb_match:
            storage = f"{int(tb_match.group(1)) * 1024}GB"
        else:
            storage = f"{raw_val}GB"

    # ── Intent type ───────────────────────────────────────────────────────────
    if base_sku and (ram or storage):
        intent_type = "modify_existing"
    elif base_sku:
        intent_type = "exact_match"
    else:
        intent_type = "new_request"

    # ── Find matching SKUs in part list ───────────────────────────────────────
    matched_skus_current_gen = []
    matched_skus_next_gen = []
    all_results = []
    recommended_sku = ""

    if part_list_lookup:
        seed_profile = part_list_lookup.get(base_sku, {}) if base_sku else {}
        target_ram = ram.replace("GB", "") if ram else clean_text(seed_profile.get("ram_gb", ""))
        target_storage = storage.replace("GB", "") if storage else clean_text(seed_profile.get("storage_gb", ""))
        target_line = clean_text(seed_profile.get("product_line", "")).lower() if seed_profile else clean_text(product_family).lower()
        target_platform = clean_text(seed_profile.get("platform", ""))
        target_cpu_tier = clean_text(seed_profile.get("cpu_tier", ""))
        target_color = clean_text(seed_profile.get("color", ""))
        target_conn = clean_text(seed_profile.get("connectivity", ""))
        base_generation = to_int_or_zero(seed_profile.get("generation", 0)) if seed_profile else 0

        # ── Scan all SKUs for matches ────────────────────────────────────────
        exact_matches = []  # Exact RAM+Storage match
        close_matches = []  # Same product line, platform, CPU, closest higher RAM
        
        for sku, profile in part_list_lookup.items():
            if is_eol_profile(profile):
                continue
            if not is_supported_generation_device_profile(profile):
                continue
            if is_accessory_text(clean_text(profile.get("raw_description", ""))):
                continue

            prof_line = clean_text(profile.get("product_line", "")).lower()
            if target_line and prof_line and target_line not in prof_line and prof_line not in target_line:
                continue
            if target_platform and clean_text(profile.get("platform", "")) and target_platform != clean_text(profile.get("platform", "")):
                continue
            if target_cpu_tier and clean_text(profile.get("cpu_tier", "")) and target_cpu_tier != clean_text(profile.get("cpu_tier", "")):
                continue
            if target_color and clean_text(profile.get("color", "")) and target_color.lower() != clean_text(profile.get("color", "")).lower():
                continue
            if target_conn and clean_text(profile.get("connectivity", "")) and target_conn != clean_text(profile.get("connectivity", "")):
                continue

            prof_ram = clean_text(profile.get("ram_gb", ""))
            prof_storage = clean_text(profile.get("storage_gb", ""))
            prof_gen = to_int_or_zero(profile.get("generation", 0))
            is_next_gen = is_next_gen_device_profile(profile)

            # Exact match on RAM and Storage
            if target_ram and prof_ram and target_ram == prof_ram and target_storage and prof_storage and target_storage == prof_storage:
                stock = summarize_stock(inventory_by_sku.get(sku, []) if inventory_by_sku else [])
                exact_matches.append({
                    "sku": sku,
                    "generation": prof_gen,
                    "is_next_gen": is_next_gen,
                    "ram": prof_ram,
                    "storage": prof_storage,
                    "description": clean_text(profile.get("raw_description", "")),
                    "stock_on_hand": stock.get("total_on_hand", 0) if inventory_by_sku else 0,
                    "stock_on_order": stock.get("total_on_order", 0) if inventory_by_sku else 0,
                    "score": 100,
                    "reason": "exact match (RAM + storage)",
                })
            # Close match: same product line, platform, CPU; accept closest higher RAM or same
            elif target_ram and prof_ram and target_storage and prof_storage:
                prof_ram_int = to_int_or_zero(prof_ram)
                target_ram_int = to_int_or_zero(target_ram)
                prof_storage_int = to_int_or_zero(prof_storage)
                target_storage_int = to_int_or_zero(target_storage)
                
                if prof_storage_int == target_storage_int and prof_ram_int >= target_ram_int:
                    stock = summarize_stock(inventory_by_sku.get(sku, []) if inventory_by_sku else [])
                    score = 90 - (prof_ram_int - target_ram_int) * 5  # Penalize higher RAM
                    close_matches.append({
                        "sku": sku,
                        "generation": prof_gen,
                        "is_next_gen": is_next_gen,
                        "ram": prof_ram,
                        "storage": prof_storage,
                        "description": clean_text(profile.get("raw_description", "")),
                        "stock_on_hand": stock.get("total_on_hand", 0) if inventory_by_sku else 0,
                        "stock_on_order": stock.get("total_on_order", 0) if inventory_by_sku else 0,
                        "score": score,
                        "reason": f"closest match (RAM {prof_ram}GB, storage {prof_storage}GB)",
                    })

        # Separate by generation
        for match in exact_matches:
            if match["is_next_gen"]:
                matched_skus_next_gen.append(match["sku"])
                all_results.append({**match, "category": "Exact (Next-Gen)"})
            else:
                matched_skus_current_gen.append(match["sku"])
                all_results.append({**match, "category": "Exact (Current-Gen)"})

        for match in close_matches:
            if match["is_next_gen"]:
                matched_skus_next_gen.append(match["sku"])
                all_results.append({**match, "category": "Close (Next-Gen)"})
            else:
                matched_skus_current_gen.append(match["sku"])
                all_results.append({**match, "category": "Close (Current-Gen)"})

        # Prioritize: exact current-gen > close current-gen > exact next-gen > close next-gen
        all_results.sort(
            key=lambda x: (
                0 if x["category"].startswith("Exact (Current") else (1 if x["category"].startswith("Close (Current") else (2 if x["category"].startswith("Exact (Next") else 3)),
                -x["score"],
                -x["stock_on_hand"],
                x["sku"],
            )
        )

        if all_results:
            recommended_sku = all_results[0]["sku"]

        # If modifying and find next-gen equivalent, check if it has requested RAM
        if intent_type == "modify_existing" and base_sku and part_list_map and (sku_lookup or inventory_by_sku):
            next_gen_equivs = find_replacements_for_sku(
                base_sku,
                sku_lookup or {},
                inventory_by_sku or {},
                part_list_map=part_list_map,
                part_list_lookup=part_list_lookup,
                prefer_higher_ram=True,
            )
            if next_gen_equivs and not matched_skus_next_gen:
                # No exact next-gen match found; add next-gen equivalents as alternatives
                for equiv in next_gen_equivs:
                    equiv_sku = equiv.get("replacement_sku", "")
                    equiv_profile = part_list_lookup.get(equiv_sku, {})
                    if equiv_profile:
                        all_results.append({
                            "sku": equiv_sku,
                            "generation": equiv_profile.get("generation", ""),
                            "is_next_gen": True,
                            "ram": equiv_profile.get("ram_gb", ""),
                            "storage": equiv_profile.get("storage_gb", ""),
                            "description": equiv.get("replacement_description", ""),
                            "stock_on_hand": equiv.get("total_on_hand", 0),
                            "stock_on_order": equiv.get("total_on_order", 0),
                            "score": 50,
                            "reason": equiv.get("reason", "next-gen equivalent"),
                            "category": "Alternative (Next-Gen Equiv)",
                        })
                        if equiv_sku not in matched_skus_next_gen:
                            matched_skus_next_gen.append(equiv_sku)

    return {
        "base_sku": base_sku,
        "product_family": product_family,
        "intent_type": intent_type,
        "requested_changes": {
            "RAM": ram,
            "Storage": storage,
        },
        "matched_skus_current_gen": matched_skus_current_gen,
        "matched_skus_next_gen": matched_skus_next_gen,
        "recommended_sku": recommended_sku,
        "all_results": all_results,
    }


with tab_config:
    st.caption("Interpret a SKU request and find matching configurations from the part list.")

    sku_config_input = st.text_area(
        "Describe what you need",
        height=120,
        placeholder=(
            "Examples:\n"
            "  EP2-00001\n"
            "  Give me EP2-00001 but with 32GB RAM\n"
            "  Surface Laptop 7 U7 with 512GB storage and 32GB RAM\n"
            "  Surface Pro 11 Snapdragon X Elite 16GB 512GB Black"
        ),
        key="sku_config_input",
    )

    if st.button("Parse & Find SKUs", type="primary", key="sku_config_btn"):
        raw_input = clean_text(sku_config_input)
        if not raw_input:
            st.warning("Please describe what configuration you need.")
        elif not part_list_lookup:
            st.warning("No part list loaded. The app should load one automatically.")
        else:
            result = parse_sku_config_request(
                raw_input,
                part_list_lookup,
                sku_lookup=sku_lookup,
                inventory_by_sku=inventory_by_sku,
                part_list_map=part_list_map,
            )

            import json as _json

            st.subheader("Parsed Request")
            parsed_display = {
                "base_sku": result["base_sku"] or "(none detected)",
                "product_family": result["product_family"] or "(none detected)",
                "intent_type": result["intent_type"],
                "requested_changes": {
                    "RAM": result["requested_changes"]["RAM"] or "(unchanged)",
                    "Storage": result["requested_changes"]["Storage"] or "(unchanged)",
                },
            }
            st.code(_json.dumps(parsed_display, indent=2), language="json")

            st.subheader("Search Results")
            all_results = result.get("all_results", [])
            recommended = result.get("recommended_sku", "")

            if not all_results:
                st.info("No matching SKUs found for those constraints in the current part list.")
            else:
                # Display recommended SKU prominently
                if recommended:
                    rec_profile = part_list_lookup.get(recommended, {})
                    rec_stock = summarize_stock(inventory_by_sku.get(recommended, []) if inventory_by_sku else [])
                    st.success(
                        f"**Recommended:** `{recommended}` – {clean_text(rec_profile.get('raw_description', '')[:80])}"
                    )
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("RAM", gb_text(rec_profile.get("ram_gb", "")))
                    with col2:
                        st.metric("Storage", gb_text(rec_profile.get("storage_gb", "")))
                    with col3:
                        st.metric("On Hand", rec_stock.get("total_on_hand", 0))
                    with col4:
                        st.metric("On Order", rec_stock.get("total_on_order", 0))
                    st.divider()

                # Explanation of priority order
                st.caption(
                    "**Priority Order:** Exact Current-Gen > Close Current-Gen > Exact Next-Gen > Close Next-Gen > Next-Gen Alternative"
                )

                # Build results table, grouped by category
                rows = []
                current_category = None
                for result_item in all_results:
                    category = result_item.get("category", "Other")
                    if category != current_category:
                        current_category = category
                        rows.append({
                            "SKU": f"**{category}**",
                            "Description": "",
                            "RAM": "",
                            "Storage": "",
                            "On Hand": "",
                            "On Order": "",
                            "Reason": "",
                        })

                    rows.append({
                        "SKU": result_item.get("sku", ""),
                        "Description": result_item.get("description", "")[:60],
                        "RAM": result_item.get("ram", ""),
                        "Storage": result_item.get("storage", ""),
                        "On Hand": result_item.get("stock_on_hand", 0),
                        "On Order": result_item.get("stock_on_order", 0),
                        "Reason": result_item.get("reason", ""),
                    })

                st.dataframe(rows, use_container_width=True, hide_index=True)

                # Copy button
                copy_payload = "\t".join(["SKU", "Description", "RAM", "Storage", "On Hand", "On Order", "Reason"])
                copy_payload += "\n" + "\n".join(
                    "\t".join([
                        r["SKU"], r["Description"][:30], r["RAM"] or "", r["Storage"] or "",
                        str(r["On Hand"] or ""), str(r["On Order"] or ""), r["Reason"] or "",
                    ])
                    for r in rows
                    if r["SKU"] and not r["SKU"].startswith("**")
                )
                render_text_copy_button(copy_payload, "tab7_sku_config_results", "Copy Results")
