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
            rows.append({"code": code, "prom_id": prom_id,
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
    token = os.environ.get("PROM_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing GitHub Actions secret PROM_API_TOKEN. Add it in Settings > Secrets and variables > Actions.")
    whitelist = load_whitelist()
    temp_files, feeds = [], {}
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "User-Agent": "HOME-PRIME-stock-sync/2.0"})
    report = []
    try:
        for source, url in FEEDS.items():
            path = os.path.join(BASE, source + ".xml")
            fetch(url, path)
            temp_files.append(path)
            feeds[source] = parse_feed(path)

        updates = 0
        for product in whitelist:
            available, decision, details = determine_status(product, feeds)
            if available is None:
                report.append([product["prom_id"], product["code"], decision, details])
                continue
            # Intentionally send ONLY the Prom ID and the in_stock flag.
            payload = {"id": int(product["prom_id"]), "in_stock": available}
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
                updates += 1
                report.append([product["prom_id"], product["code"],
                               "UPDATED_IN_STOCK" if available else "UPDATED_OUT_OF_STOCK",
                               f"HTTP {response.status_code}; {details}; response={str(data)[:250]}"])
                time.sleep(0.25)
            except (requests.RequestException, ValueError) as exc:
                report.append([product["prom_id"], product["code"], "API_ERROR", str(exc)[:400]])

        with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела / відповідь API"])
            writer.writerows(report)
        print(f"Finished. Approved products={len(whitelist)}; API updates accepted={updates}; skipped/errors={len(report)-updates}")
        if updates == 0:
            raise RuntimeError("No API updates were accepted. Review sync-report.csv; no feed was published.")
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass

if __name__ == "__main__":
    main()
