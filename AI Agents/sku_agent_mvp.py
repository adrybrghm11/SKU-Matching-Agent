import csv
import re
from pathlib import Path
from collections import defaultdict
from datetime import date

TODAY = "2026-05-11"
NEXT_GEN_AVAILABLE_DATE = "2026-05-19"

DATA_DIR = Path("data")

PRODUCT_FILES = [
    "Laptop 7 SKU List.csv",
    "Laptop 8 SKU List.csv",
    "Pro 11 SKU List.csv",
]

INVENTORY_FILES = {
    "DandH5.11.2026.csv": "DandH",
    "Ingram5.11.26.csv": "Ingram",
    "Synnex5.11.26.csv": "Synnex",
}


def clean_text(value):
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def clean_int(value):
    value = clean_text(value).replace(",", "").replace("$", "")
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def normalize_bool(value):
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        return [row for row in reader]


def write_csv(path, fieldnames, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_first_sku(text):
    if not text:
        return None
    match = re.search(r"\b[A-Z0-9]{3}-[0-9]{5}\b", text.upper())
    return match.group(0) if match else None


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

    if "surface laptop" in d or "laptop" in d or "lpt" in d or "lptp" in d or "lp7" in d:
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
    elif "intel" in d or "ultra" in d or "u5/" in d or "u7/" in d or "ux7/" in d:
        result["platform"] = "Intel"
        result["cpu_family"] = "Core Ultra"
        if "ux7" in d:
            result["cpu_tier"] = "Ultra X7"
        elif "u7" in d or "ultra 7" in d:
            result["cpu_tier"] = "Ultra 7"
        elif "u5" in d or "ultra 5" in d:
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


def build_family_key(product_line, screen_size, platform):
    return f"{product_line.lower().replace(' ', '_')}|{screen_size}|{platform.lower()}"


def build_spec_key(product_line, screen_size, platform, cpu_tier, ram_gb, storage_gb, color):
    return "|".join([
        product_line.lower().replace(" ", "_"),
        str(screen_size),
        platform.lower(),
        cpu_tier.lower().replace(" ", "_"),
        str(ram_gb),
        str(storage_gb),
        color.lower()
    ])


def parse_product_files():
    rows = []

    for filename in PRODUCT_FILES:
        file_path = DATA_DIR / filename
        product_line, generation = infer_generation_and_line_from_file(filename)
        for row in read_csv_rows(file_path):
            joined = " | ".join([clean_text(c) for c in row if clean_text(c)])
            sku = extract_first_sku(joined)
            if not sku:
                continue

            desc = ""
            for cell in row:
                cell = clean_text(cell)
                if sku in cell and len(cell) > len(sku):
                    desc = cell
                    break

            if not desc:
                non_empty = [clean_text(c) for c in row if clean_text(c)]
                idx = None
                for i, val in enumerate(non_empty):
                    if val == sku:
                        idx = i
                        break
                if idx is not None and idx + 1 < len(non_empty):
                    desc = non_empty[idx + 1]

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
                "family_key": build_family_key(product_line, specs["screen_size"], specs["platform"]),
                "spec_key": build_spec_key(
                    product_line,
                    specs["screen_size"],
                    specs["platform"],
                    specs["cpu_tier"],
                    specs["ram_gb"],
                    specs["storage_gb"],
                    specs["color"],
                )
            }
            rows.append(item)

    deduped = {}
    for row in rows:
        if row["sku"] not in deduped or len(row["raw_description"]) > len(deduped[row["sku"]]["raw_description"]):
            deduped[row["sku"]] = row
    return list(deduped.values())


def parse_inventory_files():
    rows = []

    for filename, distributor in INVENTORY_FILES.items():
        file_path = DATA_DIR / filename

        if distributor == "DandH":
            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = clean_text(row.get("Vendor Item No"))
                    if not sku or sku == "Vendor Item No":
                        continue
                    rows.append({
                        "snapshot_date": TODAY,
                        "distributor": distributor,
                        "sku": sku,
                        "description": clean_text(row.get("Product Name")),
                        "qty_on_hand": clean_int(row.get("Stock on Hand")),
                        "qty_on_order": clean_int(row.get("Qty On Order")),
                        "source_file": filename,
                    })

        elif distributor == "Ingram":
            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = clean_text(row.get("MFR #"))
                    if not sku:
                        continue
                    rows.append({
                        "snapshot_date": TODAY,
                        "distributor": distributor,
                        "sku": sku,
                        "description": clean_text(row.get("Product Description")),
                        "qty_on_hand": clean_int(row.get("Ingram On-Hand")),
                        "qty_on_order": clean_int(row.get("Ingram On-Order")),
                        "source_file": filename,
                    })

        elif distributor == "Synnex":
            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sku = clean_text(row.get("MFGPart#"))
                    if not sku:
                        continue
                    rows.append({
                        "snapshot_date": TODAY,
                        "distributor": distributor,
                        "sku": sku,
                        "description": clean_text(row.get("Short Desc")),
                        "qty_on_hand": clean_int(row.get("Avail Qty")),
                        "qty_on_order": 0,
                        "source_file": filename,
                    })

    return rows


def build_replacements(master_skus):
    by_sku = {row["sku"]: row for row in master_skus}
    rows = []

    # Direct known Laptop 7 -> Laptop 8 mappings
    direct_map = {
        "EP2-33224": "EP2-47589",
        "EP2-33242": "EP2-47614",
        "EP2-33228": "EP2-47640",
        "EP2-33246": "EP2-47665",
        "EP2-33233": "EP2-50779",
        "EP2-33251": "EP2-50804",
        "EP2-33229": "EP2-47990",
        "EP2-33247": "EP2-48015",
        "EP2-33234": "EP2-51079",
        "EP2-33252": "EP2-51104",
    }

    for source_sku, replacement_sku in direct_map.items():
        if source_sku in by_sku and replacement_sku in by_sku:
            source = by_sku[source_sku]
            repl = by_sku[replacement_sku]
            confidence = "high"
            if source["cpu_tier"] != repl["cpu_tier"]:
                confidence = "medium"
            rows.append({
                "source_sku": source_sku,
                "replacement_sku": replacement_sku,
                "replacement_type": "current_gen_comparable",
                "match_confidence": confidence,
                "reason": f'{source["raw_description"]} -> {repl["raw_description"]}',
                "replacement_generation_note": "Quoted as next generation device",
                "available_to_purchase_date": NEXT_GEN_AVAILABLE_DATE,
            })

    return rows


def aggregate_inventory(inventory_rows):
    by_sku = defaultdict(list)
    for row in inventory_rows:
        by_sku[row["sku"]].append(row)
    return by_sku


def find_replacements_for_sku(sku, replacement_rows):
    return [r for r in replacement_rows if r["source_sku"] == sku]


def summarize_stock(rows):
    if not rows:
        return {"total_on_hand": 0, "total_on_order": 0}
    return {
        "total_on_hand": sum(r["qty_on_hand"] for r in rows),
        "total_on_order": sum(r["qty_on_order"] for r in rows),
    }


def draft_sales_email(requested_sku, master_skus, inventory_by_sku, replacement_rows):
    by_sku = {row["sku"]: row for row in master_skus}
    product = by_sku.get(requested_sku)

    if not product:
        return f"Could not find SKU {requested_sku} in the master SKU list."

    requested_stock_rows = inventory_by_sku.get(requested_sku, [])
    requested_summary = summarize_stock(requested_stock_rows)
    replacements = find_replacements_for_sku(requested_sku, replacement_rows)

    lines = []
    lines.append("Hi Sales,")
    lines.append("")
    lines.append(f"For SKU {requested_sku} ({product['raw_description']}):")
    lines.append("")

    if requested_stock_rows:
        lines.append("Current distributor stock:")
        for row in requested_stock_rows:
            lines.append(
                f"- {row['distributor']}: {row['qty_on_hand']} on hand, {row['qty_on_order']} on order"
            )
        lines.append(
            f"- Total: {requested_summary['total_on_hand']} on hand, {requested_summary['total_on_order']} on order"
        )
    else:
        lines.append("Current distributor stock:")
        lines.append("- No stock found in the latest distributor files")

    if replacements:
        lines.append("")
        lines.append("Comparable current-generation option(s):")
        for repl in replacements:
            repl_sku = repl["replacement_sku"]
            repl_product = by_sku.get(repl_sku)
            repl_stock_rows = inventory_by_sku.get(repl_sku, [])
            repl_summary = summarize_stock(repl_stock_rows)

            lines.append(
                f"- {repl_sku} ({repl_product['raw_description']})"
            )
            lines.append(f"  Match confidence: {repl['match_confidence']}")
            lines.append(f"  Availability: {repl_summary['total_on_hand']} on hand, {repl_summary['total_on_order']} on order")
            lines.append(
                f"  Note: This newer-generation device can be quoted now but will not be available for purchase until {repl['available_to_purchase_date']}."
            )

    lines.append("")
    lines.append("Please let me know if you would like me to confirm the best quoted replacement based on required specs.")
    lines.append("")
    lines.append("Best,")
    lines.append("SKU Agent")

    return "\n".join(lines)


def main():
    master_skus = parse_product_files()
    inventory_rows = parse_inventory_files()
    replacement_rows = build_replacements(master_skus)

    write_csv(
        DATA_DIR / "master_skus.csv",
        [
            "sku", "product_line", "device_type", "generation", "screen_size", "platform",
            "cpu_family", "cpu_tier", "ram_gb", "storage_gb", "color", "connectivity",
            "os", "form_factor", "is_demo", "is_taa", "status", "raw_description",
            "family_key", "spec_key"
        ],
        master_skus
    )

    write_csv(
        DATA_DIR / "inventory_snapshot.csv",
        ["snapshot_date", "distributor", "sku", "description", "qty_on_hand", "qty_on_order", "source_file"],
        inventory_rows
    )

    write_csv(
        DATA_DIR / "sku_replacements.csv",
        [
            "source_sku", "replacement_sku", "replacement_type", "match_confidence",
            "reason", "replacement_generation_note", "available_to_purchase_date"
        ],
        replacement_rows
    )

    inventory_by_sku = aggregate_inventory(inventory_rows)

    # Example
    sample_sku = "EP2-33242"
    print("=" * 80)
    print(draft_sales_email(sample_sku, master_skus, inventory_by_sku, replacement_rows))
    print("=" * 80)


if __name__ == "__main__":
    main()