#!/usr/bin/env python3
"""Build a stock-only YML feed for manually approved existing Prom products.

Prom IDs are resolved against the live Prom catalog (GET only). The generated
feed contains only the existing offer ID, availability and the unchanged
vendorCode matching key. This export mode sends no product-write API requests.
"""
import csv, json, os, re, time
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
    with urlopen(req, timeout=180) as r:
        body = r.read()

    # Some Top-Drop responses prepend PHP warnings before the actual YML.
    # Keep only a recognizable XML/YML document; never publish an error page.
    xml_start = body.find(b"<?xml")
    if xml_start < 0:
        xml_start = body.find(b"<yml_catalog")
    if xml_start < 0:
        xml_start = body.find(b"<shop")
    if xml_start > 0:
        prefix = body[:xml_start].lstrip()
        if not (prefix.startswith(b"Warning:") or prefix.startswith(b"Notice:") or
                prefix.startswith(b"Deprecated:") or prefix.startswith(b"\xef\xbb\xbfWarning:")):
            raise RuntimeError(
                f"Unexpected data before XML document from {url}: {prefix[:200]!r}"
            )
        print(f"SUPPLIER_XML stripped PHP warning preamble: {len(body[:xml_start])} bytes")
        body = body[xml_start:]
    if xml_start < 0:
        preview = body[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"Supplier URL did not return XML/YML: {url}; response={preview!r}")
    with open(path, "wb") as f:
        f.write(body)

def parse_feed(path):
    """Parse supplier YML offers with or without XML namespaces.

    Some supplier endpoints serve a namespaced YML document while uploaded
    snapshots may be namespace-free. Iterating all end events and comparing
    local element names handles both formats without weakening status checks.
    """
    found = defaultdict(list)
    offer_count = 0
    try:
        context = etree.iterparse(
            path, events=("end",), recover=True, huge_tree=True,
            no_network=True, resolve_entities=False
        )
        for _, elem in context:
            local_tag = etree.QName(elem).localname
            if local_tag not in ("offer", "item"):
                continue
            offer_count += 1
            data = {}
            for child in elem:
                key = etree.QName(child).localname
                if key not in data:
                    data[key] = (child.text or "").strip()

            code = norm(data.get("vendorCode") or data.get("vendor_code") or
                        data.get("article") or data.get("barcode") or data.get("sku"))
            supplier_id = (elem.get("id") or data.get("id") or "").strip()
            raw = (elem.get("available") or data.get("available") or
                   elem.get("in_stock") or data.get("in_stock") or
                   data.get("presence") or data.get("stock_status") or "").strip()
            status = parse_status(raw)
            if code:
                found[("code", code)].append(status)
            if supplier_id:
                found[("id", supplier_id)].append(status)

            elem.clear()
            parent = elem.getparent()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]
    except (OSError, etree.XMLSyntaxError) as exc:
        raise RuntimeError(f"Cannot parse supplier XML {path}: {exc}") from exc

    if offer_count == 0:
        try:
            with open(path, "rb") as source_file:
                preview = source_file.read(300).decode("utf-8", errors="replace")
        except OSError:
            preview = "(file could not be read)"
        raise RuntimeError(
            f"No <offer>/<item> elements found in {path}; "
            f"downloaded response may not be a YML feed. First bytes: {preview!r}"
        )
    if not found:
        raise RuntimeError(
            f"Found {offer_count} offers in {path}, but none had a usable supplier code or ID"
        )
    if not any(value is not None for values in found.values() for value in values):
        raise RuntimeError(
            f"Found {offer_count} offers in {path}, but no recognized availability values; "
            "refusing to publish a partial feed"
        )
    print(f"SUPPLIER_XML parsed: file={os.path.basename(path)} offers={offer_count} keys={len(found)}")
    return found

def load_whitelist():
    rows = []
    allowed_statuses = {"ДОЗВОЛЕНО", "РУЧНА ПЕРЕВІРКА — повторний артикул Prom"}
    with open(WHITELIST, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            status = (row.get("Статус_підключення") or "").strip()
            if status not in allowed_statuses:
                continue
            code = norm(row.get("Код_товару"))
            # This legacy column is mixed: some rows contain a Prom ID, others
            # contain the product external_id. Keep it only as an identity check;
            # never send it as the offer ID.
            candidate = (row.get("Ідентифікатор_товару") or "").strip()
            if not code or not candidate:
                continue
            supplier_ids = {src: (row.get(col) or "").strip()
                            for src, col in ID_COLUMNS.items()}
            manual_duplicate = status != "ДОЗВОЛЕНО"
            if manual_duplicate and not any(supplier_ids.values()):
                continue
            rows.append({
                "code": code,
                "article": (row.get("Код_товару") or "").strip(),
                "prom_id": "",
                "prom_id_candidate": candidate,
                "name": (row.get("Назва_позиції_укр") or "").strip(),
                "category_id": (row.get("Ідентифікатор_підрозділу") or "").strip(),
                "supplier_ids": supplier_ids,
                "manual_duplicate": manual_duplicate
            })
    # A supplier ID shared by different articles is not safe for exact matching.
    id_owners = defaultdict(set)
    for product in rows:
        for source, supplier_id in product["supplier_ids"].items():
            if supplier_id:
                id_owners[(source, supplier_id)].add(product["code"])
    for product in rows:
        product["supplier_id_conflicts"] = {
            source for source, supplier_id in product["supplier_ids"].items()
            if supplier_id and len(id_owners[(source, supplier_id)]) > 1
        }
    if not rows:
        raise RuntimeError("No manually approved products with an identity candidate in the whitelist")
    return rows

def load_duplicate_resolutions():
    """Load the manually reviewed 53 duplicate-article pairs."""
    path = os.path.join(BASE, "prom-duplicate-resolution.csv")
    resolutions, seen_ids = [], set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            article = (row.get("Артикул") or "").strip()
            keep_id = (row.get("Prom_ID_залишити") or "").strip()
            disable_id = (row.get("Prom_ID_немає_в_наявності") or "").strip()
            if not article or not keep_id.isdigit() or not disable_id.isdigit() or keep_id == disable_id:
                raise RuntimeError(f"Invalid duplicate resolution row: {row}")
            if keep_id in seen_ids or disable_id in seen_ids:
                raise RuntimeError(f"Duplicate Prom ID in duplicate resolution: {row}")
            seen_ids.update((keep_id, disable_id))
            resolutions.append((article, keep_id, disable_id))
    if len(resolutions) != 53:
        raise RuntimeError(f"Expected 53 reviewed duplicate pairs, found {len(resolutions)}")
    return resolutions

def exclude_duplicate_prom_cards(products, resolutions, report):
    """Never publish the reviewed duplicate Prom cards marked for disabling."""
    disabled_by_id = {disable_id: article for article, _keep_id, disable_id in resolutions}
    keep_by_article = {norm(article): keep_id for article, keep_id, _disable_id in resolutions}
    filtered = []
    for product in products:
        pid = str(product.get("prom_id", "")).strip()
        article = str(product.get("article", "")).strip()
        article_key = norm(article)
        if pid in disabled_by_id:
            report.append([
                pid, article, "SKIP_DUPLICATE_DISABLED",
                "Reviewed duplicate pair; keep Prom ID="
                + keep_by_article.get(article_key, "")
            ])
            continue
        expected_keep = keep_by_article.get(article_key)
        if expected_keep and pid != expected_keep:
            report.append([
                pid, article, "SKIP_DUPLICATE_NOT_APPROVED",
                "Reviewed duplicate pair; only Prom ID=" + expected_keep + " may be published"
            ])
            continue
        filtered.append(product)

    remaining_ids = {str(p.get("prom_id", "")).strip() for p in filtered}
    leaked = sorted(set(disabled_by_id) & remaining_ids)
    if leaked:
        raise RuntimeError(
            "Safety stop: disabled duplicate Prom IDs entered the feed: " + ", ".join(leaked)
        )
    print(
        f"DUPLICATE_FILTER: reviewed_pairs={len(resolutions)}; "
        f"disabled_cards_excluded={len(products)-len(filtered)}"
    )
    return filtered


def load_prom_catalog(session):
    """Read the current Prom catalog; only IDs returned by this GET scan may enter the feed."""
    by_sku, by_id = defaultdict(list), {}
    page_size = 100
    last_id = None
    max_pages = int(os.environ.get("PROM_CATALOG_MAX_PAGES", "200"))
    stop_reason = "MAX_PAGES_REACHED"

    for page_no in range(1, max_pages + 1):
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = session.get(f"{API_BASE}/products/list", params=params, timeout=60)
        print(f"PROM_CATALOG products/list page={page_no} params={params} HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        products = data.get("products", []) if isinstance(data, dict) else []
        if not isinstance(products, list):
            raise RuntimeError("Unexpected products/list response: 'products' is not a list")
        if not products:
            stop_reason = "EMPTY_PAGE"
            break

        new_count, numeric_ids = 0, []
        for product in products:
            pid = str(product.get("id", "")).strip()
            if not pid.isdigit():
                continue
            numeric_ids.append(int(pid))
            if pid not in by_id:
                by_id[pid] = product
                new_count += 1
            sku = norm(str(product.get("sku", "") or ""))
            if sku and pid not in by_sku[sku]:
                by_sku[sku].append(pid)

        print(f"PROM_CATALOG page={page_no} products={len(products)} unique_total={len(by_id)}")
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

    if stop_reason not in ("LAST_PAGE", "EMPTY_PAGE") or not by_id:
        raise RuntimeError(f"Prom catalog scan incomplete ({stop_reason}); refusing to publish a possibly incomplete feed")
    print(f"PROM_CATALOG complete: products={len(by_id)} stop={stop_reason}")
    return by_sku, by_id

def map_whitelist_to_current_prom(whitelist, by_sku, by_id, report):
    """Map each approved supplier-linked row to its own current Prom card.

    Prefer the whitelist identity candidate (Prom external_id or Prom ID).
    SKU is only a validation field, not a unique key: different suppliers can
    legitimately have separate Prom cards with the same article/SKU.
    """
    mapped, used_ids = [], set()
    for product in whitelist:
        candidate = str(product.get("prom_id_candidate", "")).strip()
        # First resolve by the exact stable identity from the approved whitelist.
        identity_matches = []
        if candidate in by_id:
            identity_matches.append(candidate)
        identity_matches.extend(
            pid for pid, current in by_id.items()
            if str(current.get("external_id", "") or "").strip() == candidate
            and pid not in identity_matches
        )

        if len(identity_matches) != 1:
            report.append(["", product["article"], "SKIP_IDENTITY_NOT_UNIQUE",
                           f"Whitelist identity={candidate}; current Prom identity matches={len(identity_matches)}"])
            continue

        pid = identity_matches[0]
        current = by_id[pid]
        current_sku = norm(str(current.get("sku", "") or ""))
        if current_sku != product["code"]:
            report.append([pid, product["article"], "SKIP_IDENTITY_SKU_MISMATCH",
                           f"Whitelist identity={candidate}; current Prom SKU={current.get('sku', '')}; expected={product['article']}"])
            continue

        if pid in used_ids:
            raise RuntimeError(f"Prom ID {pid} mapped more than once; refusing duplicate offer IDs")
        external_id = str(current.get("external_id", "") or "").strip()
        if not external_id:
            report.append([
                pid, product["article"], "SKIP_NO_EXTERNAL_ID",
                "Current Prom card has no external_id; refusing to publish an offer that could be created as new"
            ])
            continue

        product["prom_id"] = pid
        product["prom_external_id"] = external_id
        used_ids.add(pid)
        mapped.append(product)

    print(f"PROM_MAPPING: whitelist rows={len(whitelist)}; mapped current products={len(mapped)}; excluded={len(whitelist)-len(mapped)}")
    return mapped


def load_previous_feed_ids():
    """Freeze the feed to IDs already present in the last published feed.

    Prom's URL-based scheduled import does not persist the manual 'only update'
    option. Any unseen YML offer ID can therefore become a new product. This
    baseline lock makes the stock feed strictly update-only: newly approved or
    newly discovered IDs are excluded until deliberately onboarded outside this
    stock-only workflow.
    """
    path = os.path.join(BASE, "prom-feed.xml")
    if not os.path.isfile(path):
        raise RuntimeError(
            "Safety stop: previous prom-feed.xml baseline is missing; "
            "refusing to publish a feed that could create new Prom products."
        )
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        root = etree.parse(path, parser).getroot()
    except Exception as exc:
        raise RuntimeError(
            "Safety stop: previous prom-feed.xml baseline is invalid; "
            "refusing to publish a feed that could create new Prom products."
        ) from exc
    ids = {
        str(offer.get("id", "")).strip()
        for offer in root.xpath(".//offer")
        if str(offer.get("id", "")).strip()
    }
    if not ids:
        raise RuntimeError(
            "Safety stop: previous prom-feed.xml contains no offer IDs; "
            "refusing to publish a feed that could create new Prom products."
        )
    print(f"FEED_BASELINE: locked existing YML IDs={len(ids)}")
    return ids


def keep_only_previous_feed_ids(products, previous_ids, report):
    """Never introduce a YML ID that was not already in the published feed."""
    kept = []
    for product in products:
        offer_id = str(product.get("prom_external_id", "")).strip()
        if offer_id not in previous_ids:
            report.append([
                product.get("prom_id", ""),
                product.get("article", ""),
                "SKIP_NEW_ID_NOT_IN_PREVIOUS_FEED",
                "Stock-only safety lock: this YML ID was not in the previous published feed"
            ])
            continue
        kept.append(product)
    dropped = len(products) - len(kept)
    print(
        f"FEED_BASELINE_FILTER: kept={len(kept)}; "
        f"blocked_new_ids={dropped}; baseline_ids={len(previous_ids)}"
    )
    if not kept:
        raise RuntimeError(
            "Safety stop: no products remain after the previous-feed ID lock."
        )
    return kept

def determine_status(product, feeds):
    """Use only the supplier feed whose exact supplier ID identifies this card.

    Article/SKU fallback is intentionally forbidden: multiple supplier cards can
    share the same Prom SKU and must be updated independently.
    """
    matches, ambiguous = [], []
    for source in FEEDS:
        supplier_id = (product.get("supplier_ids") or {}).get(source, "").strip()
        if not supplier_id:
            continue
        if source in product.get("supplier_id_conflicts", set()):
            ambiguous.append(source + ":" + supplier_id + ":ID_SHARED_BY_ARTICLES")
            continue
        values = feeds.get(source, {}).get(("id", supplier_id), [])
        if len(values) > 1:
            ambiguous.append(source + ":" + supplier_id + ":DUPLICATE_FEED_ID")
        elif len(values) == 1:
            matches.append((source, supplier_id, values[0]))

    if ambiguous:
        return None, "SKIP_AMBIGUOUS_SUPPLIER_ID", "; ".join(ambiguous)
    if not matches:
        # Approved whitelist rows may have no supplier-specific ID recorded.
        # In that case, allow only one exact normalized article/vendorCode match
        # across all supplier feeds. Multiple rows or suppliers remain ambiguous.
        article_matches = []
        for fallback_source in FEEDS:
            values = feeds.get(fallback_source, {}).get(("code", product.get("code", "")), [])
            if values:
                article_matches.append((fallback_source, values))
        total_rows = sum(len(values) for _, values in article_matches)
        if total_rows != 1 or len(article_matches) != 1:
            return None, "SKIP_ARTICLE_MATCH_NOT_UNIQUE", (
                "Exact article/vendorCode matches=" + str(total_rows) +
                "; matched suppliers=" + str(len(article_matches)) +
                "; requires exactly one supplier row in exactly one feed"
            )
        source, values = article_matches[0]
        status = values[0]
        supplier_id = "exact-article:" + product.get("article", "")
        if status is None:
            return None, "SKIP_UNKNOWN_SUPPLIER_STATUS", (
                "Supplier=" + source + "; exact article match; status not recognized"
            )
        matches = [(source, supplier_id, status)]
    if len(matches) != 1:
        detail = "; ".join(source + ":" + supplier_id for source, supplier_id, _ in matches)
        return None, "SKIP_SUPPLIER_NOT_UNIQUE", (
            "Exact supplier-ID matches=" + str(len(matches)) +
            (("; " + detail) if detail else "")
        )
    source, supplier_id, status = matches[0]
    if status is None:
        return (
            None,
            "SKIP_UNKNOWN_SUPPLIER_STATUS",
            "Supplier=" + source + "; supplier ID=" + supplier_id + "; feed status not recognized",
        )
    if status:
        return True, "IN_STOCK", "Supplier=" + source + "; supplier ID=" + supplier_id
    return False, "OUT_OF_STOCK", "Supplier=" + source + "; supplier ID=" + supplier_id
def audit_missing_prom_ids(session):
    """Read-only lookup of whitelist rows missing Prom IDs against the live Prom catalog."""
    targets = []
    target_path = os.path.join(BASE, "prom-id-audit-targets.csv")
    with open(target_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            article = (row.get("Код_товару") or "").strip()
            if article:
                targets.append({"article": article, "article_norm": norm(article)})

    by_key = defaultdict(list)
    seen_ids = set()
    page_size = 100
    last_id = None
    max_pages = int(os.environ.get("PROM_READ_ONLY_MAX_PAGES", "200"))
    pages_read = 0
    stop_reason = "MAX_PAGES_REACHED"

    for page_no in range(1, max_pages + 1):
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = session.get(f"{API_BASE}/products/list", params=params, timeout=60)
        print(f"ID_AUDIT products/list page={page_no} HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        products = data.get("products", []) if isinstance(data, dict) else []
        if not isinstance(products, list):
            raise RuntimeError("Unexpected products/list response: 'products' is not a list")
        pages_read += 1
        if not products:
            stop_reason = "EMPTY_PAGE"
            break

        numeric_ids = []
        new_count = 0
        for p in products:
            pid = str(p.get("id", "")).strip()
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                new_count += 1
            if pid.isdigit():
                numeric_ids.append(int(pid))
            # Keep exact source fields for matching; do not infer Prom IDs from URL/unique_id.
            for field in ("sku", "external_id"):
                value = norm(str(p.get(field, "") or ""))
                if value:
                    by_key[("article", value)].append(p)
            name_key = norm(str(p.get("name", "") or ""))
            if name_key:
                by_key[("name", name_key)].append(p)

        print(f"ID_AUDIT page={page_no} products={len(products)} unique_total={len(seen_ids)}")
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

    output = []
    for target in targets:
        candidates = {}
        for p in by_key.get(("article", target["article_norm"]), []):
            pid = str(p.get("id", "")).strip()
            if pid:
                candidates[pid] = p
        match_type = "ARTICLE_EXACT" if len(candidates) == 1 else (
            "MULTIPLE_CANDIDATES" if len(candidates) > 1 else "NO_MATCH"
        )
        if candidates:
            for pid, p in candidates.items():
                output.append([
                    target["article"], match_type, pid, p.get("sku", ""),
                    p.get("external_id", ""), p.get("name", ""), p.get("presence", "")
                ])
        else:
            output.append([target["article"], match_type, "", "", "", "", ""])

    audit_path = os.path.join(BASE, "prom-id-audit.csv")
    with open(audit_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Артикул з Excel", "Тип збігу", "Prom ID з API",
                         "API sku", "API external_id", "Назва з API", "Наявність з API"])
        writer.writerows(output)
    print(f"ID_AUDIT complete: targets={len(targets)} pages={pages_read} catalog_unique={len(seen_ids)} stop={stop_reason} output_rows={len(output)}")
    print("ID_AUDIT is GET-only; no Prom product fields were changed.")
    if stop_reason not in ("LAST_PAGE", "EMPTY_PAGE"):
        raise RuntimeError(f"Prom catalog scan incomplete ({stop_reason}); audit file is partial and must not be used to assign IDs.")


def audit_duplicate_product_details(session):
    """GET-only detail audit for all Prom IDs in the ambiguous article report."""
    source_path = os.path.join(BASE, "prom-id-audit-ambiguous.csv")
    if not os.path.exists(source_path):
        print("DETAIL_AUDIT skipped: ambiguous candidate file is absent")
        return
    ids = []
    with open(source_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pid = (row.get("Prom ID з API") or "").strip()
            if pid.isdigit() and pid not in ids:
                ids.append(pid)

    detail_rows, raw_records = [], []
    for index, pid in enumerate(ids, 1):
        response = session.get(f"{API_BASE}/products/{pid}", timeout=60)
        print(f"DETAIL_AUDIT product={index}/{len(ids)} id={pid} HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        product = payload.get("product", payload) if isinstance(payload, dict) else {}
        if not isinstance(product, dict):
            product = {"unexpected_response": payload}
        raw_records.append({"requested_id": pid, "response": payload})
        images = product.get("images")
        if isinstance(images, list):
            image_count = len(images)
        elif product.get("main_image"):
            image_count = 1
        else:
            image_count = ""
        def val(*keys):
            for key in keys:
                value = product.get(key)
                if value is not None and value != "":
                    if isinstance(value, (dict, list)):
                        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    return str(value)
            return ""
        detail_rows.append([
            pid, val("id"), val("sku"), val("external_id"), val("name"),
            val("presence"), val("category", "category_id", "group_id"),
            val("url", "product_url"), val("date_created", "created_at"),
            val("date_modified", "updated_at", "modified_at"),
            image_count, val("main_image"), val("keywords"),
            len(str(val("description", "description_ua", "description_ru"))),
            ",".join(sorted(product.keys()))
        ])
        time.sleep(0.15)

    csv_path = os.path.join(BASE, "prom-id-audit-details.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Requested Prom ID", "Returned Prom ID", "SKU", "External ID",
            "Name", "Presence", "Category/group", "Product URL",
            "Created", "Modified", "Images count", "Main image",
            "Keywords", "Description length", "API fields"
        ])
        writer.writerows(detail_rows)
    json_path = os.path.join(BASE, "prom-id-audit-details.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(raw_records, f, ensure_ascii=False, indent=2)
    print(f"DETAIL_AUDIT complete: requested={len(ids)} saved={len(detail_rows)}; GET-only, no product changes.")

def main():
    yml_export_only = os.environ.get("PROM_YML_EXPORT_ONLY", "0").strip().lower() in ("1", "true", "yes")
    token = os.environ.get("PROM_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing GitHub Actions secret PROM_API_TOKEN. A live read-only Prom catalog lookup is required to prevent wrong IDs and deleted products from entering the feed.")
    whitelist = load_whitelist()
    temp_files, feeds = [], {}
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "User-Agent": "HOME-PRIME-stock-sync/2.0"})
    report = []
    try:
        if os.environ.get("PROM_ID_AUDIT", "0").strip().lower() in ("1", "true", "yes"):
            if not token:
                raise RuntimeError("PROM_ID_AUDIT requires PROM_API_TOKEN secret")
            audit_missing_prom_ids(session)
            audit_duplicate_product_details(session)

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

        if not yml_export_only:
            raise RuntimeError("Safety stop: this project is configured to publish a stock-only YML feed; direct Prom product writes are disabled.")

        # Map every approved whitelist row independently, including repeated SKUs
        # that belong to separate supplier cards.
        prom_by_sku, prom_by_id = load_prom_catalog(session)
        whitelist = map_whitelist_to_current_prom(
            whitelist, prom_by_sku, prom_by_id, report
        )

        # The reviewed duplicate-resolution file is a mandatory exclusion list.
        # A pair's secondary Prom card must never be published, even if it still
        # exists in the live catalog and its supplier reports any stock status.
        duplicate_resolutions = load_duplicate_resolutions()
        whitelist = exclude_duplicate_prom_cards(
            whitelist, duplicate_resolutions, report
        )

        # Safety is based on the live Prom catalog, not on the previous feed.
        # Every row has already been matched to exactly one current Prom card,
        # its SKU is checked, and its external_id is verified below to belong
        # to exactly one current card. This allows approved existing products
        # omitted from an older feed to be restored without creating new cards.

        for source, url in FEEDS.items():
            path = os.path.join(BASE, source + ".xml")
            fetch(url, path)
            temp_files.append(path)
            feeds[source] = parse_feed(path)

        # YML export mode: only currently existing Prom products are included.
        # Each offer contains the exact live Prom ID, availability and unchanged vendorCode.
        if yml_export_only:
            root = etree.Element("yml_catalog", date=time.strftime("%Y-%m-%d %H:%M"))
            shop = etree.SubElement(root, "shop")
            etree.SubElement(shop, "name").text = "HOME PRIME — stock-only sync"
            currencies = etree.SubElement(shop, "currencies")
            etree.SubElement(currencies, "currency", id="UAH", rate="1")
            offers = etree.SubElement(shop, "offers")
            included, skipped, supplier_confirmed = 0, 0, 0

            for product in whitelist:
                available, decision, details = determine_status(product, feeds)
                if available is None:
                    skipped += 1
                    report.append([product["prom_id"], product["article"], decision, details])
                    continue
                # YML offer id must remain the product's original Prom external_id.
                # Prom's internal numeric product ID is used only for live-catalog
                # validation and must never replace the import identity.
                offer = etree.SubElement(offers, "offer", id=product["prom_external_id"],
                                         available="true" if available else "false")
                etree.SubElement(offer, "name").text = product["name"]
                etree.SubElement(offer, "vendorCode").text = product["article"]
                included += 1
                supplier_confirmed += 1
                report.append([product["prom_id"], product["article"],
                               "YML_READY_IN_STOCK" if available else "YML_READY_OUT_OF_STOCK",
                               details])

            min_confirmed = int(os.environ.get("PROM_MIN_CONFIRMED_PRODUCTS", "400"))
            if supplier_confirmed < min_confirmed:
                with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела"])
                    writer.writerows(report)
                raise RuntimeError(
                    f"YML export stopped: only {supplier_confirmed} supplier-confirmed products; "
                    f"minimum safety threshold is {min_confirmed}. "
                    "No new feed was published."
                )
            if included == 0:
                raise RuntimeError("YML export stopped: no current existing Prom products have a conclusive status.")

            offer_ids = [offer.get("id") for offer in offers.findall("offer")]
            if len(offer_ids) != len(set(offer_ids)):
                raise RuntimeError("YML export stopped: duplicate original external_id values detected.")
            # Validate every YML ID against exactly one live Prom card. Never
            # substitute the internal Prom ID: that changes the import identity
            # and can make Prom create a duplicate instead of updating a card.
            external_id_owners = defaultdict(list)
            for current_pid, current_product in prom_by_id.items():
                external_id = str(current_product.get("external_id", "") or "").strip()
                if external_id:
                    external_id_owners[external_id].append(current_pid)
            invalid_external_ids = sorted(
                offer_id for offer_id in offer_ids
                if len(external_id_owners.get(offer_id, [])) != 1
            )
            if invalid_external_ids:
                raise RuntimeError(
                    "YML export stopped: external_id does not resolve to exactly one current Prom card: "
                    + ", ".join(invalid_external_ids[:10])
                )
            mismatched = [
                product["prom_external_id"] for product in whitelist
                if external_id_owners.get(product["prom_external_id"]) != [product["prom_id"]]
            ]
            if mismatched:
                raise RuntimeError(
                    "YML export stopped: external_id-to-Prom-ID validation failed: "
                    + ", ".join(mismatched[:10])
                )

            public_dir = os.path.join(BASE, "public")
            os.makedirs(public_dir, exist_ok=True)
            tree = etree.ElementTree(root)
            tree.write(os.path.join(BASE, "prom-feed.xml"), encoding="UTF-8",
                       xml_declaration=True,
                       doctype='<!DOCTYPE yml_catalog SYSTEM "shops.dtd">',
                       pretty_print=True)
            tree.write(os.path.join(public_dir, "prom-feed.xml"), encoding="UTF-8",
                       xml_declaration=True,
                       doctype='<!DOCTYPE yml_catalog SYSTEM "shops.dtd">',
                       pretty_print=True)
            with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Prom ID", "Артикул", "Результат", "Джерела"])
                writer.writerows(report)
            print(
                f"YML READY: current Prom catalog={len(prom_by_id)}; "
                f"supplier-confirmed={supplier_confirmed}; included={included}; skipped={skipped}"
            )
            print("Feed contains only existing Prom IDs, availability and unchanged vendorCode. No Prom API write requests were sent.")
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
