import csv
import re
from pathlib import Path

DATA_DIR = Path("data")
PRODUCT_FILES = [
    "Laptop 7 SKU List.csv",
    "Laptop 8 SKU List.csv",
    "Pro 11 SKU List.csv",
]

OUTPUT_MASTER = DATA_DIR / "clean_product_master.csv"
OUTPUT_ISSUES = DATA_DIR / "product_master_issues.csv"


def clean_text(value):
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def infer_generation_and_line_from_file(filename):
    lower = filename.lower()
    if "laptop 7" in lower:
        return "Surface Laptop", 7
    if "laptop 8" in lower:
        return "Surface Laptop", 8
    if "pro 11" in lower:
        return "Surface Pro", 11
    return "", ""


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
        "is_demo": infer_demo(desc),
        "is_taa": infer_taa(desc),
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

    if "snapdragon" in d or "snap" in d or "x plus" in d or "x elite" in d:
        result["platform"] = "ARM"
        result["cpu_family"] = "Snapdragon"
        if "x elite" in d or "elite" in d:
            result["cpu_tier"] = "X Elite"
        elif "x plus" in d or "plus" in d:
            result["cpu_tier"] = "X Plus"
    elif "intel" in d or "ultra" in d or "u5/" in d or "u7/" in d or "ux7/" in d or "cu5" in d or "cu7" in d:
        result["platform"] = "Intel"
        result["cpu_family"] = "Core Ultra"
        if "ux7" in d or "ultra x7" in d or "u7x" in d or "cux7" in d:
            result["cpu_tier"] = "Ultra X7"
        elif "u7" in d or "ultra 7" in d or "cu7" in d:
            result["cpu_tier"] = "Ultra 7"
        elif "u5" in d or "ultra 5" in d or "cu5" in d:
            result["cpu_tier"] = "Ultra 5"

    spec_match = re.search(r'[/ ](\d{1,2})/(\d{3,4}|1tb|512|256)', d)
    if spec_match:
        result["ram_gb"] = spec_match.group(1)
        storage_raw = spec_match.group(2).upper()
        result["storage_gb"] = "1024" if storage_raw == "1TB" else storage_raw

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


def main():
    master_rows = []
    issue_rows = []

    for filename in PRODUCT_FILES:
        product_line, generation = infer_generation_and_line_from_file(filename)
        rows = read_csv_rows(DATA_DIR / filename)

        for row in rows:
            cleaned = [clean_text(c) for c in row if clean_text(c)]
            if not cleaned:
                continue

            joined = " | ".join(cleaned)
            sku_match = re.search(r"\b[A-Z0-9]{3}-[0-9]{5}\b", joined.upper())
            if not sku_match:
                continue

            sku = sku_match.group(0)

            desc = ""
            for i, val in enumerate(cleaned):
                if val == sku and i + 1 < len(cleaned):
                    desc = cleaned[i + 1]
                    break

            if not desc:
                for cell in cleaned:
                    if sku in cell and len(cell) > len(sku):
                        desc = cell
                        break

            if not desc:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_description",
                    "details": "Could not determine product description from source row",
                    "raw_description": joined,
                })
                continue

            specs = parse_specs(desc)

            row_out = {
                "sku": sku,
                "product_line": product_line,
                "generation": generation,
                "screen_size": specs["screen_size"],
                "platform": specs["platform"],
                "cpu_family": specs["cpu_family"],
                "cpu_tier": specs["cpu_tier"],
                "ram_gb": specs["ram_gb"],
                "storage_gb": specs["storage_gb"],
                "color": specs["color"],
                "connectivity": specs["connectivity"],
                "is_demo": str(specs["is_demo"]).lower(),
                "is_taa": str(specs["is_taa"]).lower(),
                "raw_description": desc,
                "status": "active",
                "source_file": filename,
            }
            master_rows.append(row_out)

            if not specs["screen_size"]:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_screen_size",
                    "details": "Could not parse screen size",
                    "raw_description": desc,
                })
            if not specs["platform"]:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_platform",
                    "details": "Could not parse platform",
                    "raw_description": desc,
                })
            if not specs["cpu_tier"]:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_cpu_tier",
                    "details": "Could not parse CPU tier",
                    "raw_description": desc,
                })
            if not specs["ram_gb"]:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_ram",
                    "details": "Could not parse RAM",
                    "raw_description": desc,
                })
            if not specs["storage_gb"]:
                issue_rows.append({
                    "sku": sku,
                    "issue_type": "missing_storage",
                    "details": "Could not parse storage",
                    "raw_description": desc,
                })

    deduped = {}
    for row in master_rows:
        if row["sku"] not in deduped or len(row["raw_description"]) > len(deduped[row["sku"]]["raw_description"]):
            deduped[row["sku"]] = row

    write_csv(
        OUTPUT_MASTER,
        [
            "sku", "product_line", "generation", "screen_size", "platform",
            "cpu_family", "cpu_tier", "ram_gb", "storage_gb", "color",
            "connectivity", "is_demo", "is_taa", "raw_description", "status", "source_file"
        ],
        list(deduped.values())
    )

    write_csv(
        OUTPUT_ISSUES,
        ["sku", "issue_type", "details", "raw_description"],
        issue_rows
    )

    print(f"Wrote {OUTPUT_MASTER}")
    print(f"Wrote {OUTPUT_ISSUES}")
    print(f"Master rows: {len(deduped)}")
    print(f"Issues: {len(issue_rows)}")


if __name__ == "__main__":
    main()