#!/usr/bin/env python3
"""HOME PRIME: availability-only sync for manually approved existing Prom products."""
import csv, os, re, time
from collections import defaultdict
from urllib.request import Request, urlopen
from lxml import etree

FEEDS = {
    "top_drop": "https://www.top-drop.com.ua/export/feed/BfisHXpOQwXN2SSPKFDJLmSX.xml",
    "aveon": "https://aveon.net.ua/products_feed.xml?hash_tag=7b71fadcc4a12f03cf26a304da032fba&sales_notes=&product_ids=&label_ids=&exclude_fields=&html_description=0&yandex_cpa=&process_presence_sure=&languages=uk%2Cru&extra_fields=keywords&group_ids=",
    "opt_drop": "https://opt-drop.com/storage/xml/opt-drop-32.xml",
}
ID_COLUMNS = {"top_drop": "ID_TopDrop", "aveon": "ID_Aveon", "opt_drop": "ID_OptDrop"}
BASE = os.path.dirname(os.path.abspath(__file__))
WHITELIST = os.path.join(BASE, "whitelist_prom.csv")
PUBLIC = os.path.join(BASE, "public")
os.makedirs(PUBLIC, exist_ok=True)

def norm(v):
    return re.sub(r"[^A-ZА-ЯІЇЄҐ0-9]", "", (v or "").strip().upper())

def fetch(url, path):
    req = Request(url, headers={"User-Agent": "HOME-PRIME-stock-sync/1.1"})
    with urlopen(req, timeout=180) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

def parse_feed(path):
    # Index each supplier offer by both its own ID and its article/vendorCode.
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
                    data.get("article") or data.get("barcode"))
        supplier_id = (elem.get("id") or data.get("id") or "").strip()
        if code or supplier_id:
            raw = (elem.get("available") or data.get("available") or "").strip().lower()
            if raw in ("true", "1", "+", "yes", "склад", "in_stock"):
                status = True
            elif raw in ("false", "0", "-", "no", "out_of_stock"):
                status = False
            else:
                status = None
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

def main():
    whitelist = []
    seen_prom_ids = set()
    with open(WHITELIST, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Статус_підключення") != "ДОЗВОЛЕНО":
                continue
            code = norm(row.get("Код_товару"))
            prom_id = (row.get("Ідентифікатор_товару") or "").strip()
            # Prom ID must come from the Prom export. Supplier IDs are never used as Prom IDs.
            if not (code and prom_id.isdigit()):
                continue
            if prom_id in seen_prom_ids:
                raise RuntimeError(f"Duplicate Prom ID in whitelist: {prom_id}; refusing unsafe feed")
            seen_prom_ids.add(prom_id)
            whitelist.append({
                "code": code,
                "prom_id": prom_id,
                "supplier_ids": {source: (row.get(column) or "").strip()
                                 for source, column in ID_COLUMNS.items()},
            })
    if not whitelist:
        raise RuntimeError("No approved existing Prom products with numeric Prom IDs")

    temp_files, feeds = [], {}
    try:
        for source, url in FEEDS.items():
            path = os.path.join(BASE, source + ".xml")
            fetch(url, path)
            temp_files.append(path)
            feeds[source] = parse_feed(path)

        offers, report = [], []
        for product in whitelist:
            observations, ambiguous = [], []
            for source, index in feeds.items():
                supplier_id = product["supplier_ids"].get(source, "")
                values = index.get(("id", supplier_id), []) if supplier_id else []
                # If the supplier ID is absent/not found, use exact article/vendorCode.
                if not values:
                    values = index.get(("code", product["code"]), [])
                if len(values) > 1:
                    ambiguous.append(source)
                elif len(values) == 1 and values[0] is not None:
                    observations.append((source, values[0]))
            if ambiguous:
                report.append([product["prom_id"], product["code"], "SKIP_AMBIGUOUS", ",".join(ambiguous)])
                continue
            if not observations:
                report.append([product["prom_id"], product["code"], "SKIP_NOT_FOUND_OR_UNKNOWN", ""])
                continue
            # Available at any supplier => in stock. Out of stock only when all
            # suppliers with a definite matching status report out of stock.
            available = any(value for _, value in observations)
            # Critical: send ONLY availability. No names, IDs from suppliers,
            # categories, prices, descriptions, photos or other product fields.
            offers.append(etree.Element("offer", id=product["prom_id"],
                                        available="true" if available else "false"))
            report.append([product["prom_id"], product["code"],
                           "IN_STOCK" if available else "OUT_OF_STOCK",
                           ",".join(f"{source}:{'+' if value else '-'}"
                                    for source, value in observations)])

        if not offers:
            raise RuntimeError("No safe availability updates produced; refusing to publish empty feed")
        root = etree.Element("yml_catalog", date=time.strftime("%Y-%m-%d %H:%M"))
        shop = etree.SubElement(root, "shop")
        etree.SubElement(shop, "name").text = "HOME PRIME — availability only"
        currencies = etree.SubElement(shop, "currencies")
        etree.SubElement(currencies, "currency", id="UAH", rate="1")
        offers_node = etree.SubElement(shop, "offers")
        for offer in offers:
            offers_node.append(offer)
        etree.ElementTree(root).write(os.path.join(PUBLIC, "prom-feed.xml"),
                                      encoding="UTF-8", xml_declaration=True, pretty_print=True)
        with open(os.path.join(PUBLIC, "sync-report.csv"), "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела"])
            writer.writerows(report)
        print(f"OK: approved={len(whitelist)}, availability_updates={len(offers)}, skipped={len(report)-len(offers)}")
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass

if __name__ == "__main__":
    main()
