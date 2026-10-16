"""
scripts/export_products.py
Exports active inventory items from inventory_reference_sheet.csv, enriched
with PostgreSQL wnc_parts_master records, into canonical schema JSON files for Astro.
"""

import os
import re
import csv
import json
import psycopg2
from datetime import datetime, timezone

CSV_PATH = r"D:\eBay-inventory\inventory_reference_sheet.csv"
OUTPUT_DIR = r"D:\partslingers-web\src\data\products"
OUTPUT_ALL = r"D:\partslingers-web\src\data\products.json"

def clean_alphanumeric(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", str(text or "")).upper()

def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", str(text or "").lower()).strip()
    return re.sub(r"[-\s]+", "-", cleaned)

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 1. Read CSV
    csv_items = []
    with open(CSV_PATH, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            csv_items.append(row)
            
    print(f"Loaded {len(csv_items)} inventory items from CSV.")
    
    # 2. Connect to PostgreSQL
    conn = psycopg2.connect(
        dbname="wnc_parts_master",
        user="postgres",
        password="postgres",
        host="localhost",
        port=5432
    )
    cur = conn.cursor()
    
    # Preload all MPNs in CSV
    mpns = [row.get("PartNumber", "").strip() for row in csv_items if row.get("PartNumber")]
    
    # Query parts_master
    cur.execute(
        "SELECT mpn, brand, title, description, category_breadcrumb, price, pic_urls, item_specifics, epids "
        "FROM parts_master WHERE mpn = ANY(%s);",
        (mpns,)
    )
    pg_parts = {r[0]: r for r in cur.fetchall()}
    print(f"Matched {len(pg_parts)} items in parts_master.")
    
    # Query catalog_fitment_reference
    cur.execute(
        "SELECT mpn, make, model, years, position_description "
        "FROM catalog_fitment_reference WHERE mpn = ANY(%s);",
        (mpns,)
    )
    fitment_map = {}
    for r in cur.fetchall():
        part_no = r[0]
        if part_no not in fitment_map:
            fitment_map[part_no] = []
        
        # parse years like '1974-81' or '1970'
        raw_years = str(r[3] or "").strip()
        y_start, y_end = 1970, 1985
        if "-" in raw_years:
            parts = raw_years.split("-")
            try:
                y_start = int(parts[0])
                end_part = parts[1]
                if len(end_part) == 2:
                    y_end = int(str(y_start)[:2] + end_part)
                else:
                    y_end = int(end_part)
            except Exception:
                pass
        elif raw_years.isdigit():
            y_start = int(raw_years)
            y_end = y_start

        fitment_map[part_no].append({
            "make": r[1] or "Chevrolet",
            "model": r[2] or "Truck",
            "year_start": y_start,
            "year_end": y_end,
            "engine": None,
            "notes": r[4] or None
        })
        
    # Query dorman_motormite_interchange_reference
    cur.execute(
        "SELECT dorman_mpn, motormite_mpn, direction "
        "FROM dorman_motormite_interchange_reference WHERE dorman_mpn = ANY(%s);",
        (mpns,)
    )
    interchange_map = {}
    for r in cur.fetchall():
        p_no = r[0]
        if p_no not in interchange_map:
            interchange_map[p_no] = []
        interchange_map[p_no].append({
            "brand": "Motormite / Dorman",
            "part_number": r[1] or "",
            "clean_part_number": clean_alphanumeric(r[1]),
            "interchange_type": "Direct Interchange"
        })
        
    conn.close()
    
    canonical_products = []
    
    for row in csv_items:
        mpn = row.get("PartNumber", "").strip()
        if not mpn:
            continue
            
        brand = row.get("Brand", "").strip() or "Dorman / AutoGrade"
        group_name = row.get("Group", "").strip()
        sku = row.get("SKU", "").strip() or f"WNC-{clean_alphanumeric(mpn)}"
        item_id = row.get("ItemID", "").strip()
        title = row.get("Title", "").strip() or f"{brand} {mpn}"
        
        try:
            price = float(row.get("CurrentPrice", 0.0) or 0.0)
        except (ValueError, TypeError):
            price = 0.0
            
        clean_mpn = clean_alphanumeric(mpn)
        slug = slugify(f"{brand}-{mpn}")
        
        # Merge Postgres data if available
        description = ""
        category_breadcrumb = group_name or "Automotive Parts & Accessories"
        media = []
        item_specifics = {}
        epid = None
        
        if mpn in pg_parts:
            pg = pg_parts[mpn]
            if pg[3]:  # description
                description = pg[3]
            if pg[4]:  # category_breadcrumb
                category_breadcrumb = pg[4]
            if pg[5] and price <= 0:  # price
                price = float(pg[5])
            if pg[6]:  # pic_urls
                for idx, url in enumerate(pg[6]):
                    media.append({
                        "url": url,
                        "is_primary": idx == 0,
                        "order": idx,
                        "media_source": "cloudinary" if "cloudinary" in url else "user_upload",
                        "alt_text": f"{brand} {mpn} {title}"
                    })
            if pg[7] and isinstance(pg[7], dict):
                item_specifics = pg[7]
            if pg[8] and len(pg[8]) > 0:
                epid = str(pg[8][0])
                
        # Fallback placeholder image if no images exist
        if not media:
            media.append({
                "url": "https://images.unsplash.com/photo-1763848843613-f8c2ca3a31a1?auto=format&fit=crop&w=900&q=85",
                "is_primary": True,
                "order": 0,
                "media_source": "placeholder",
                "alt_text": f"{brand} {mpn}"
            })
            
        fitments = fitment_map.get(mpn, [])
        interchanges = interchange_map.get(mpn, [])
        
        product_item = {
            "brand": brand,
            "mpn": mpn,
            "clean_mpn": clean_mpn,
            "title": title[:200],
            "slug": slug,
            "upc": None,
            "epid": epid,
            "description_markdown": description or f"Authentic {brand} part {mpn}. New Old Stock factory hardware, precision inspected and ready to ship.",
            "description_html": None,
            "category_breadcrumb": category_breadcrumb,
            "store_category": group_name or "Vintage Hardware",
            "department": group_name or "General Fasteners",
            "condition_badge": "NOS (New Old Stock)",
            "price": round(price, 2) if price > 0 else 18.95,
            "quantity": 3,
            "is_in_stock": True,
            "sku": sku,
            "ebay_item_id": item_id if item_id and "E+" not in item_id else None,
            "interchanges": interchanges,
            "fitments": fitments,
            "media": media,
            "specifications": item_specifics,
            "shipping": {
                "weight_major_lbs": 0,
                "weight_minor_oz": 6,
                "total_weight_oz": 6,
                "package_type": "Package/Thick Envelope"
            },
            "is_published": True,
            "data_sources": ["inventory_reference_sheet", "wnc_parts_master"],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        
        canonical_products.append(product_item)
        
        # Write individual product file
        out_file = os.path.join(OUTPUT_DIR, f"{clean_mpn}.json")
        with open(out_file, "w", encoding="utf-8") as pf:
            json.dump(product_item, pf, indent=2)
            
    # Write consolidated all-products file
    with open(OUTPUT_ALL, "w", encoding="utf-8") as af:
        json.dump(canonical_products, af, indent=2)
        
    print(f"Successfully exported {len(canonical_products)} products to {OUTPUT_DIR} and {OUTPUT_ALL}")

if __name__ == "__main__":
    main()
