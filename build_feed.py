#!/usr/bin/env python3
"""HOME PRIME: stock-only sync for manually approved existing Prom products.

Only POSTs {"id": PROM_ID, "in_stock": bool} to Prom.
No product creation and no other product fields are sent.
"""
import csv, os, re, time
from collections import defaultdict
from urllib.request import Request, urlopen

import requests
from lxml import etree

FEEDS = {
    "top_drop": "https://www.top-drop.com.ua/export/feed/BfisHXpOQwXN2SSPKFDJLmSX.xml",
    "aveon": "https://aveon.net.ua/products_feed.xml?hash_tag=7b71fadcc4a12f03cf26a304da032fba&sales_notes=&product_ids=&label_ids=&exclude_fields=&html_description=0&yandex_cpa=&process_presence_sure=&languages=uk%2Cru&extra_fields=keywords&group_ids=",
    "opt_drop": "https://opt-drop.com/storage/xml/opt-drop-32.xml",
}
ID_COLUMNS = {"top_drop": "ID_TopDrop", "aveon": "ID_Aveon", "opt_drop": "ID_OptDrop"}
BASE = os.path.dirname(os.path.abspath(__file__))
WHITELIST = os.path.join(BASE, "whitelist_prom.csv")
REPORT = os.path.join(BASE, "sync-report.csv")
API_BASE = "https://my.prom.ua/api/v1"

def norm(v):
    return re.sub(r"[^A-ZА-ЯІЇЄҐ0-9]", "", (v or "").strip().upper())

def parse_status(raw):
    raw = (raw or "").strip().lower()
    if raw in ("true", "1", "+", "yes", "склад", "in_stock", "є", "наявний", "в наявності"):
        return True
    if raw in ("false", "0", "-", "no", "out_of_stock", "немає", "відсутній", "немає в наявності"):
        return False
    return None

def fetch(url, path):
    req = Request(url, headers={"User-Agent": "HOME-PRIME-stock-sync/2.0"})
    with urlopen(req, timeout=180) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

def parse_feed(path):
    found = defaultdict(list)
    context = etree.iterparse(path, events=("end",), tag=("offer", "item"),
                              recover=True, huge_tree=True, no_network=True)
    for _, elem in context:
        data = {}
        for child in elem:
            key = etree.QName(child).localname
            if key not in data:
                data[key] = (child.text or "").strip()
        code = norm(data.get("vendorCode") or data.get("vendor_code") or
                    data.get("article") or data.get("barcode") or data.get("sku"))
        supplier_id = (elem.get("id") or data.get("id") or "").strip()
        raw = (elem.get("available") or data.get("available") or
               data.get("presence") or data.get("stock_status") or "").strip()
        status = parse_status(raw)
        if code:
            found[("code", code)].append(status)
        if supplier_id:
            found[("id", supplier_id)].append(status)
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    if not found:
        raise RuntimeError(f"No product offers parsed from {path}; source format may have changed")
    return found

def load_whitelist():
    rows, seen_ids = [], set()
    with open(WHITELIST, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Статус_підключення") != "ДОЗВОЛЕНО":
                continue
            code = norm(row.get("Код_товару"))
            prom_id = (row.get("Ідентифікатор_товару") or "").strip()
            # Only the Prom export ID is accepted. Supplier IDs are never Prom IDs.
            if not code or not prom_id.isdigit():
                continue
            if prom_id in seen_ids:
                raise RuntimeError(f"Duplicate Prom ID in whitelist: {prom_id}; refusing all updates")
            seen_ids.add(prom_id)
            rows.append({"code": code, "article": (row.get("Код_товару") or "").strip(), "prom_id": prom_id,
                         "name": (row.get("Назва_позиції_укр") or "").strip(),
                         "category_id": (row.get("Ідентифікатор_підрозділу") or "").strip(),
                         "supplier_ids": {src: (row.get(col) or "").strip()
                                          for src, col in ID_COLUMNS.items()}})
    if not rows:
        raise RuntimeError("No approved existing Prom products with numeric Prom IDs")
    return rows

def determine_status(product, feeds):
    observations, missing, ambiguous = [], [], []
    for source, index in feeds.items():
        supplier_id = product["supplier_ids"].get(source, "")
        values = index.get(("id", supplier_id), []) if supplier_id else []
        if not values:
            values = index.get(("code", product["code"]), [])
        if len(values) > 1:
            ambiguous.append(source)
        elif len(values) == 1 and values[0] is not None:
            observations.append((source, values[0]))
        else:
            missing.append(source)
    if ambiguous:
        return None, "SKIP_AMBIGUOUS", ",".join(ambiguous)
    if any(value for _, value in observations):
        return True, "IN_STOCK", ",".join(f"{s}:{'+' if v else '-'}" for s, v in observations)
    # Never mark unavailable unless ALL THREE supplier sources have one exact,
    # unambiguous, explicit out-of-stock match.
    if len(observations) == len(FEEDS) and not missing and all(not v for _, v in observations):
        return False, "OUT_OF_STOCK", ",".join(f"{s}:-" for s, _ in observations)
    return None, "SKIP_INCOMPLETE_OR_UNKNOWN", (
        "matched=" + ",".join(f"{s}:{'+' if v else '-'}" for s, v in observations) +
        "; missing_or_unknown=" + ",".join(missing)
    )

def main():
    yml_export_only = os.environ.get("PROM_YML_EXPORT_ONLY", "0").strip().lower() in ("1", "true", "yes")
    token = os.environ.get("PROM_API_TOKEN", "").strip()
    if not yml_export_only and not token:
        raise RuntimeError("Missing GitHub Actions secret PROM_API_TOKEN. Add it in Settings > Secrets and variables > Actions.")
    whitelist = load_whitelist()
    test_id = os.environ.get("PROM_TEST_ONLY_ID", "").strip()
    if test_id:
        whitelist = [p for p in whitelist if p["prom_id"] == test_id]
        if len(whitelist) != 1:
            raise RuntimeError(f"Safety stop: expected exactly one approved product with Prom ID {test_id}; found {len(whitelist)}")
        print(f"TEST MODE: only Prom product ID {test_id} can be updated")
    temp_files, feeds = [], {}
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "User-Agent": "HOME-PRIME-stock-sync/2.0"})
    report = []
    try:
        # Read-only diagnostic mode: page through the existing Prom catalog.
        # Prom returns at most 100 products per response; use last_id cursor.
        # This branch performs GET requests only and never changes product data.
        if os.environ.get("PROM_API_READ_ONLY", "1").strip().lower() in ("1", "true", "yes"):
            targets = {"151160", "151160-J834"}
            seen_ids = set()
            hits = []
            page_size = 100
            last_id = None
            max_pages = int(os.environ.get("PROM_READ_ONLY_MAX_PAGES", "100"))
            pages_read = 0
            stop_reason = "MAX_PAGES_REACHED"

            for page_no in range(1, max_pages + 1):
                params = {"limit": page_size}
                if last_id is not None:
                    params["last_id"] = last_id
                response = session.get(f"{API_BASE}/products/list", params=params, timeout=60)
                print(f"READ_ONLY products/list page={page_no} params={params} HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                products = data.get("products", []) if isinstance(data, dict) else []
                if not isinstance(products, list):
                    raise RuntimeError("Unexpected products/list response: 'products' is not a list")
                pages_read += 1
                print(f"READ_ONLY page={page_no} products={len(products)}")
                if not products:
                    stop_reason = "EMPTY_PAGE"
                    break

                new_count = 0
                numeric_ids = []
                for p in products:
                    pid = str(p.get("id", "")).strip()
                    if pid:
                        if pid not in seen_ids:
                            new_count += 1
                            seen_ids.add(pid)
                        if pid.isdigit():
                            numeric_ids.append(int(pid))
                    external_id = str(p.get("external_id", "")).strip()
                    sku = str(p.get("sku", "")).strip()
                    if external_id in targets or sku in targets:
                        hits.append({k: p.get(k) for k in ("id", "external_id", "sku", "name", "presence")})

                if len(products) < page_size:
                    stop_reason = "LAST_PAGE"
                    break
                if not numeric_ids:
                    stop_reason = "NO_NUMERIC_PRODUCT_IDS_FOR_CURSOR"
                    break

                next_last_id = str(min(numeric_ids))
                if new_count == 0 or next_last_id == last_id:
                    stop_reason = "CURSOR_DID_NOT_ADVANCE_OR_PAGE_REPEATED"
                    break
                last_id = next_last_id
            else:
                stop_reason = "MAX_PAGES_REACHED"

            print(f"READ_ONLY total_unique_products={len(seen_ids)} pages={pages_read} stop={stop_reason} target_hits={len(hits)}")
            for hit in hits:
                print("TARGET_PRODUCT:", hit)
            with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Mode", "Pages read", "Unique products scanned", "Stop reason", "Target hits", "Target product JSON"])
                writer.writerow(["READ_ONLY_PAGINATED", pages_read, len(seen_ids), stop_reason, len(hits), str(hits)])
            print("Read-only paginated API lookup complete. No product data was changed.")
            return

        for source, url in FEEDS.items():
            path = os.path.join(BASE, source + ".xml")
            fetch(url, path)
            temp_files.append(path)
            feeds[source] = parse_feed(path)

        # YML export mode: generate a single-product availability-only feed; no Prom API writes.
        if os.environ.get("PROM_YML_EXPORT_ONLY", "0").strip().lower() in ("1", "true", "yes"):
            if not test_id or len(whitelist) != 1:
                raise RuntimeError("Safety stop: YML test export requires exactly one PROM_TEST_ONLY_ID")
            product = whitelist[0]
            available, decision, details = determine_status(product, feeds)
            if available is None:
                raise RuntimeError(f"YML export stopped: supplier status is inconclusive ({decision}: {details})")
            if not product["name"] or not product["category_id"].isdigit():
                raise RuntimeError("YML export stopped: missing approved product name or category ID")
            root = etree.Element("yml_catalog", date=time.strftime("%Y-%m-%d %H:%M"))
            shop = etree.SubElement(root, "shop")
            etree.SubElement(shop, "name").text = "HOME PRIME — stock-only test"
            currencies = etree.SubElement(shop, "currencies")
            etree.SubElement(currencies, "currency", id="UAH", rate="1")
            categories = etree.SubElement(shop, "categories")
            etree.SubElement(categories, "category", id=product["category_id"]).text = product["name"]
            offers = etree.SubElement(shop, "offers")
            # Use the approved product article as the YML offer ID and explicit vendorCode.
            # Prom's internal product ID remains only in the whitelist/safety guard.
            offer = etree.SubElement(offers, "offer", id=product["article"],
                                     available="true" if available else "false")
            etree.SubElement(offer, "name").text = product["name"]
            etree.SubElement(offer, "categoryId").text = product["category_id"]
            etree.SubElement(offer, "vendorCode").text = product["article"]
            etree.ElementTree(root).write(os.path.join(BASE, "prom-feed.xml"),
                                          encoding="UTF-8", xml_declaration=True,
                                          doctype='<!DOCTYPE yml_catalog SYSTEM "shops.dtd">',
                                          pretty_print=True)
            with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела"])
                writer.writerow([product["prom_id"], product["article"],
                                 "YML_READY_IN_STOCK" if available else "YML_READY_OUT_OF_STOCK",
                                 details])
            print(f"YML TEST READY: Prom ID={product['prom_id']}; available={available}; {details}")
            print("No Prom API write requests were sent.")
            return

        updates = 0
        # First validation run is deliberately limited to one product.
        sync_limit = int(os.environ.get("PROM_SYNC_LIMIT", "1"))
        for product in whitelist[:sync_limit]:
            available, decision, details = determine_status(product, feeds)
            if available is None:
                report.append([product["prom_id"], product["code"], decision, details])
                continue
            # Send only existing Prom ID and availability; no card fields.
            payload = [{
                "id": int(product["prom_id"]),
                "presence": "available" if available else "not_available"
            }]
            if test_id and str(payload[0]["id"]) != test_id:
                raise RuntimeError("Safety stop: outgoing Prom ID differs from PROM_TEST_ONLY_ID")
            try:
                response = session.post(f"{API_BASE}/products/edit", json=payload, timeout=45)
                if response.status_code >= 400:
                    report.append([product["prom_id"], product["code"], "API_ERROR",
                                   f"HTTP {response.status_code}: {response.text[:400]}"])
                    continue
                data = response.json() if response.content else {}
                # Surface explicit API-level errors; never treat an empty/unknown response as success.
                if data.get("error") or data.get("errors"):
                    report.append([product["prom_id"], product["code"], "API_ERROR",
                                   str(data.get("error") or data.get("errors"))[:400]])
                    continue
                processed = data.get("processed_ids") if isinstance(data, dict) else None
                if processed is not None and int(product["prom_id"]) not in [int(x) for x in processed]:
                    report.append([product["prom_id"], product["code"], "NOT_CONFIRMED",
                                   f"HTTP {response.status_code}; Prom did not confirm this ID in processed_ids; response={str(data)[:400]}"])
                    continue
                updates += 1
                report.append([product["prom_id"], product["code"],
                               "UPDATED_IN_STOCK" if available else "UPDATED_OUT_OF_STOCK",
                               f"HTTP {response.status_code}; {details}; response={str(data)[:400]}"])
                time.sleep(0.25)
            except (requests.RequestException, ValueError) as exc:
                report.append([product["prom_id"], product["code"], "API_ERROR", str(exc)[:400]])

        with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела / відповідь API"])
            writer.writerows(report)
        print(f"Finished. Approved products={len(whitelist)}; validation limit={sync_limit}; API updates accepted={updates}; skipped/errors={len(report)-updates}")
        if updates == 0:
            raise RuntimeError("No API updates were accepted. Review sync-report.csv; no stock changes confirmed.")
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass

if __name__ == "__main__":
    main()
