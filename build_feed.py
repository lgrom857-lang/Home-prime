#!/usr/bin/env python3
"""
HOME PRIME supplier availability filter.
Builds a Prom YML feed containing ONLY manually approved existing Prom items.
It uses Prom product IDs from the supplied Prom export, never supplier IDs.
"""
import csv, os, re, sys, time
from collections import defaultdict
from urllib.request import Request, urlopen
from lxml import etree

FEEDS = {
    "top_drop": "https://www.top-drop.com.ua/export/feed/BfisHXpOQwXN2SSPKFDJLmSX.xml",
    "aveon": "https://aveon.net.ua/products_feed.xml?hash_tag=7b71fadcc4a12f03cf26a304da032fba&sales_notes=&product_ids=&label_ids=&exclude_fields=&html_description=0&yandex_cpa=&process_presence_sure=&languages=uk%2Cru&extra_fields=keywords&group_ids=",
    "opt_drop": "https://opt-drop.com/storage/xml/opt-drop-32.xml",
}
BASE = os.path.dirname(os.path.abspath(__file__))
WHITELIST = os.path.join(BASE, "whitelist_prom.csv")
PUBLIC = os.path.join(BASE, "public")
os.makedirs(PUBLIC, exist_ok=True)

def norm(v):
    return re.sub(r"[^A-ZА-ЯІЇЄҐ0-9]", "", (v or "").strip().upper())

def fetch(url, path):
    req = Request(url, headers={"User-Agent": "HOME-PRIME-stock-sync/1.0"})
    with urlopen(req, timeout=180) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)

def parse_feed(path):
    # Each code maps to all occurrences; duplicates are handled as ambiguous.
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
        if code:
            available = (elem.get("available") or data.get("available") or "").strip().lower()
            if available in ("true", "1", "+", "yes", "склад", "in_stock"):
                status = True
            elif available in ("false", "0", "-", "no", ""):
                status = False
            else:
                status = None
            found[code].append(status)
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    if not found:
        raise RuntimeError(f"No product offers parsed from {path}; source format may have changed")
    return found

def main():
    whitelist = []
    with open(WHITELIST, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Статус_підключення") != "ДОЗВОЛЕНО":
                continue
            code = norm(row.get("Код_товару"))
            prom_id = (row.get("Ідентифікатор_товару") or "").strip()
            name = (row.get("Назва_позиції_укр") or "").strip()
            category = (row.get("Ідентифікатор_підрозділу") or "").strip()
            if code and prom_id.isdigit() and name and category:
                whitelist.append({"code": code, "id": prom_id, "name": name, "category": category})

    tmp = []
    feeds = {}
    try:
        for key, url in FEEDS.items():
            path = os.path.join(BASE, key + ".xml")
            fetch(url, path)
            tmp.append(path)
            feeds[key] = parse_feed(path)

        offers = []
        report = []
        for p in whitelist:
            observations, ambiguous = [], []
            for source, index in feeds.items():
                vals = index.get(p["code"], [])
                if len(vals) > 1:
                    ambiguous.append(source)
                elif len(vals) == 1 and vals[0] is not None:
                    observations.append((source, vals[0]))
            if ambiguous:
                report.append([p["id"], p["code"], "SKIP_AMBIGUOUS", ",".join(ambiguous)])
                continue
            if not observations:
                report.append([p["id"], p["code"], "SKIP_NOT_FOUND", ""])
                continue
            # Available from any supplier wins. Out of stock only if all matched suppliers say false.
            available = any(v for _, v in observations)
            offer = etree.Element("offer", id=p["id"], available="true" if available else "false")
            etree.SubElement(offer, "name").text = p["name"]
            etree.SubElement(offer, "categoryId").text = p["category"]
            etree.SubElement(offer, "vendorCode").text = p["code"]
            offers.append(offer)
            report.append([p["id"], p["code"], "IN_FEED_AVAILABLE" if available else "IN_FEED_OUT_OF_STOCK",
                           ",".join(f"{s}:{'+' if v else '-'}" for s, v in observations)])

        root = etree.Element("yml_catalog", date=time.strftime("%Y-%m-%d %H:%M"))
        shop = etree.SubElement(root, "shop")
        etree.SubElement(shop, "name").text = "HOME PRIME — approved existing products"
        currencies = etree.SubElement(shop, "currencies")
        etree.SubElement(currencies, "currency", id="UAH", rate="1")
        catalog = etree.SubElement(shop, "categories")
        for category_id in sorted({p["category"] for p in whitelist}):
            etree.SubElement(catalog, "category", id=category_id).text = "Категорія Prom " + category_id
        offers_el = etree.SubElement(shop, "offers")
        for offer in offers:
            # categoryId in offer must refer to the existing Prom category; category catalogue omitted
            offers_el.append(offer)
        etree.ElementTree(root).write(os.path.join(PUBLIC, "prom-feed.xml"),
                                      encoding="UTF-8", xml_declaration=True, pretty_print=True)
        with open(os.path.join(PUBLIC, "sync-report.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Prom ID", "Код", "Результат", "Джерела"])
            w.writerows(report)
        print(f"OK: whitelist={len(whitelist)}, feed_offers={len(offers)}, report={len(report)}")
    finally:
        for path in tmp:
            try: os.remove(path)
            except OSError: pass

if __name__ == "__main__":
    main()
