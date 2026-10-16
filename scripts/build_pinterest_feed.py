import os
import base64
import csv
import re
import xml.etree.ElementTree as ET
import httpx

NS = "{urn:ebay:apis:eBLBaseComponents}"

def get_oauth_token() -> str:
    app_id = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")
    ref_token = os.environ.get("EBAY_REFRESH_TOKEN")
    
    if not (app_id and cert_id and ref_token):
        raise RuntimeError("Missing eBay credentials in environment")

    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    res = httpx.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={"grant_type": "refresh_token", "refresh_token": ref_token},
        timeout=20.0
    )
    res.raise_for_status()
    token = res.json().get("access_token")
    if not token:
        raise RuntimeError("Failed to obtain OAuth token")
    return token

def fetch_all_active_listings(token: str) -> list[dict]:
    app_id = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")
    dev_id = os.environ.get("EBAY_DEV_ID", "")
    
    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1199",
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "X-EBAY-API-SITEID": "100",
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml; charset=utf-8"
    }

    all_items = []
    page = 1
    while True:
        req_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>200</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
  <DetailLevel>ReturnAll</DetailLevel>
</GetMyeBaySellingRequest>"""

        r = httpx.post("https://api.ebay.com/ws/api.dll", headers=headers, content=req_xml.encode("utf-8"), timeout=35.0)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        items = root.findall(f".//{NS}ActiveList/{NS}ItemArray/{NS}Item")
        if not items:
            break
        
        for it in items:
            item_id = it.findtext(f"{NS}ItemID")
            title = it.findtext(f"{NS}Title", "").strip()
            price = it.findtext(f"{NS}BuyItNowPrice") or it.findtext(f"{NS}SellingStatus/{NS}CurrentPrice", "0.0")
            sku = it.findtext(f"{NS}SKU", "")
            gallery = it.findtext(f"{NS}PictureDetails/{NS}GalleryURL", "")
            picture_urls = [p.text for p in it.findall(f".//{NS}PictureDetails/{NS}PictureURL") if p.text]
            
            # Use high-res image
            image_url = ""
            if picture_urls:
                image_url = picture_urls[0]
            elif gallery:
                # Upgrade gallery thumbnail to high-res s-l1600.jpg
                image_url = re.sub(r's-l\d+\.jpg$', 's-l1600.jpg', gallery)
                if image_url == gallery and '/s-l' not in gallery:
                    image_url = gallery.rstrip('/') + '/s-l1600.jpg'
            
            all_items.append({
                "item_id": item_id,
                "title": title,
                "sku": sku,
                "price": float(price),
                "image_url": image_url
            })
        
        total_pages = int(root.findtext(f".//{NS}ActiveList/{NS}PaginationResult/{NS}TotalNumberOfPages", "1"))
        if page >= total_pages:
            break
        page += 1

    return all_items

def clean_brand(title: str) -> str:
    known_brands = [
        "Dorman Help", "Dorman", "KD Lamp Co.", "KD Lamp", "Grote", "TRW", 
        "Motorcraft", "ACDelco", "AC Delco", "Tomco", "Permatex", "Champ Service",
        "Perfect Parts", "Motormite", "Dietz", "Bendix", "Standard Motor Products",
        "Standard", "Raybestos", "Moog", "Wagner", "Carter", "Holley", "Standard Plus"
    ]
    for b in known_brands:
        if b.lower() in title.lower():
            return b
    return "WNCPARTSLINGERS"

def generate_tsv(items: list[dict], output_path: str):
    fieldnames = [
        "id",
        "title",
        "description",
        "link",
        "image_link",
        "price",
        "availability",
        "condition",
        "brand",
        "google_product_category"
    ]
    
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(fieldnames)
        
        for it in items:
            item_id = it["item_id"]
            title = it["title"]
            price_str = f"{it['price']:.2f} USD"
            # Canonical link on claimed domain pointing to part or redirect
            link = f"https://wncpartslingers.com/parts/{item_id}"
            image_link = it["image_url"] or "https://wncpartslingers.com/favicon.svg"
            brand = clean_brand(title)
            desc = f"Authentic New Old Stock (NOS) / OEM component: {title}. Dispatched within 24 hours with automatic combined shipping from WNCPARTSLINGERS in Hendersonville, NC."
            
            writer.writerow([
                item_id,
                title[:150],
                desc[:950],
                link,
                image_link,
                price_str,
                "in stock",
                "new",
                brand,
                "Vehicles & Parts > Vehicle Parts & Accessories > Motor Vehicle Parts"
            ])

def main():
    print("Authenticating with eBay API via Doppler session...")
    token = get_oauth_token()
    print("Fetching active listings...")
    items = fetch_all_active_listings(token)
    print(f"Retrieved {len(items)} active items from eBay Motors.")
    
    os.makedirs(r"D:\partslingers-web\public", exist_ok=True)
    tsv_path = r"D:\partslingers-web\public\catalog.tsv"
    generate_tsv(items, tsv_path)
    print(f"Generated Pinterest Product Feed at {tsv_path} with {len(items)} items.")
    
    json_path = r"D:\partslingers-web\src\data\active_ebay_items.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    print(f"Saved active items JSON at {json_path}")

if __name__ == "__main__":
    import json
    main()
