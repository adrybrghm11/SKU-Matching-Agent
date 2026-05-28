import warnings
import logging

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

import app


def _inventory_row(on_hand):
    return {
        "qty_on_hand": int(on_hand),
        "qty_on_order": 0,
        "distributor": "TestDisti",
        "description": "test",
    }


def _base_source_profile():
    return {
        "sku": "EP2-00001",
        "raw_description": "Surface Laptop 7 - 13.8in - Ultra 7 268V - U7/32/512 - Black",
        "sheet_name": "Surface Laptop 7 (ARM & Intel)",
        "product_line": "Surface Laptop",
        "generation": "7",
        "os": "Windows 11 Pro",
        "screen_size": "13.8",
        "platform": "Intel",
        "cpu_family": "Core Ultra",
        "cpu_tier": "Ultra 7",
        "ram_gb": "32",
        "storage_gb": "512",
        "color": "Black",
        "connectivity": "WiFi",
        "form_factor": "Clamshell",
        "is_demo": False,
    }


def _next_gen_u7_profile():
    return {
        "sku": "EP2-00002",
        "raw_description": "Surface Laptop 8 - 13.8in - Ultra 7 268V - U7/32/512 - Black",
        "sheet_name": "Surface Laptop 8",
        "product_line": "Surface Laptop",
        "generation": "8",
        "os": "Windows 11 Pro",
        "screen_size": "13.8",
        "platform": "Intel",
        "cpu_family": "Core Ultra",
        "cpu_tier": "Ultra 7",
        "ram_gb": "32",
        "storage_gb": "512",
        "color": "Black",
        "connectivity": "WiFi",
        "form_factor": "Clamshell",
        "is_demo": False,
    }


def _next_gen_ux7_profile():
    return {
        "sku": "EP2-00003",
        "raw_description": "Surface Laptop 8 - 15in - Ultra X7 278V - UX7/32/512 - Black",
        "sheet_name": "Surface Laptop 8",
        "product_line": "Surface Laptop",
        "generation": "8",
        "os": "Windows 11 Pro",
        "screen_size": "15",
        "platform": "Intel",
        "cpu_family": "Core Ultra",
        "cpu_tier": "Ultra X7",
        "ram_gb": "32",
        "storage_gb": "512",
        "color": "Black",
        "connectivity": "WiFi",
        "form_factor": "Clamshell",
        "is_demo": False,
    }


def _requested_rows():
    return [
        {
            "sku": "EP2-00001",
            "description": "Surface Laptop 7 - 13.8in - Ultra 7 268V - U7/32/512 - Black",
            "product_line": "Surface Laptop",
            "screen_size": "13.8",
            "cpu_tier": "Ultra 7",
            "ram_gb": "32",
            "storage_gb": "512",
            "color": "Black",
            "total_on_hand": 0,
        }
    ]


def test_u7_query_returns_u7_and_ux7_when_both_exist():
    part_list_lookup = {
        "EP2-00001": _base_source_profile(),
        "EP2-00002": _next_gen_u7_profile(),
        "EP2-00003": _next_gen_ux7_profile(),
    }
    part_list_map = {"EP2-00001": ["EP2-00002"]}
    inventory_by_sku = {
        "EP2-00002": [_inventory_row(8)],
        "EP2-00003": [_inventory_row(4)],
    }

    rows = app.build_description_next_gen_rows(
        "Surface Laptop 13.8 Ultra 7 U7/32/512 Black",
        _requested_rows(),
        part_list_lookup,
        inventory_by_sku,
        part_list_map,
    )

    skus = [r[1] for r in rows]
    assert skus == ["EP2-00002", "EP2-00003"], f"Expected U7 then UX7, got: {skus}"
    ux7_note = rows[1][5] if len(rows) > 1 and len(rows[1]) > 5 else ""
    assert "UX7 available only" in ux7_note and "15in" in ux7_note, f"Expected UX7 size note, got: {ux7_note}"


def test_when_only_u7_exists_returns_only_u7():
    part_list_lookup = {
        "EP2-00001": _base_source_profile(),
        "EP2-00002": _next_gen_u7_profile(),
    }
    part_list_map = {"EP2-00001": ["EP2-00002"]}
    inventory_by_sku = {
        "EP2-00002": [_inventory_row(8)],
    }

    rows = app.build_description_next_gen_rows(
        "Surface Laptop 13.8 U-7 32GB 512GB Black",
        _requested_rows(),
        part_list_lookup,
        inventory_by_sku,
        part_list_map,
    )

    skus = [r[1] for r in rows]
    assert skus == ["EP2-00002"], f"Expected only U7, got: {skus}"


def test_non_u7_input_does_not_add_ux7_upgrade():
    part_list_lookup = {
        "EP2-00001": _base_source_profile(),
        "EP2-00002": _next_gen_u7_profile(),
        "EP2-00003": _next_gen_ux7_profile(),
    }
    part_list_map = {"EP2-00001": ["EP2-00002"]}
    inventory_by_sku = {
        "EP2-00002": [_inventory_row(8)],
        "EP2-00003": [_inventory_row(5)],
    }

    non_u7_rows = [dict(_requested_rows()[0])]
    non_u7_rows[0]["cpu_tier"] = "Ultra 5"
    non_u7_rows[0]["description"] = non_u7_rows[0]["description"].replace("Ultra 7", "Ultra 5")

    rows = app.build_description_next_gen_rows(
        "Surface Laptop 13.8 Ultra 5 U5/32/512 Black",
        non_u7_rows,
        part_list_lookup,
        inventory_by_sku,
        part_list_map,
    )

    skus = [r[1] for r in rows]
    assert "EP2-00003" not in skus, f"Did not expect UX7 upgrade for non-U7 input, got: {skus}"


if __name__ == "__main__":
    test_u7_query_returns_u7_and_ux7_when_both_exist()
    print("PASS: U7 query returns both U7 and UX7 when both exist")

    test_when_only_u7_exists_returns_only_u7()
    print("PASS: Only U7 returned when UX7 not available")

    test_non_u7_input_does_not_add_ux7_upgrade()
    print("PASS: Non-U7 input does not include UX7 upgrade suggestion")

    print("All description next-gen mapping harness checks passed.")
