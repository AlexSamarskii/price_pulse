from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.script = None
        self.scripts = []
        self.links = []
        self.next_links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") in {"application/ld+json", "application/json"}:
            self.script = [attrs, ""]
        if tag in {"a", "link"} and attrs.get("href") and len(self.links) < 2000:
            self.links.append(attrs["href"])
            if "next" in attrs.get("rel", "").split():
                self.next_links.append(attrs["href"])

    def handle_data(self, text):
        if self.script is not None:
            self.script[1] += text

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append(self.script)
            self.script = None


def jsonld(document):
    items = []
    for attrs, body in document.scripts:
        if attrs.get("type") != "application/ld+json":
            continue
        value = json.loads(body)
        values = value if isinstance(value, list) else [value]
        for item in values[:100]:
            if isinstance(item, dict):
                items.extend(item.get("@graph", [item])[:100])
    return [item for item in items if isinstance(item, dict)]


def public_url(base, value, prefix):
    if not isinstance(value, str):
        return None
    absolute = urljoin(base, value)
    parts = urlsplit(absolute)
    if (parts.scheme != "https" or parts.netloc != "limestore.com" or parts.query
            or parts.fragment or not parts.path.startswith(prefix)):
        return None
    if any(part in {".", ".."} for part in parts.path.split("/")) or any(c in parts.path for c in "%\\;\x00"):
        return None
    return absolute


def catalog(document, context):
    links = []
    for item in jsonld(document):
        entity = item.get("mainEntity", item)
        if not isinstance(entity, dict):
            continue
        for row in entity.get("itemListElement", [])[:500]:
            if isinstance(row, dict):
                nested = row.get("item", {})
                links.append(row.get("url") or (nested.get("url") if isinstance(nested, dict) else None))
    links.extend(document.links)
    products = [public_url(context["url"], link, "/ru_ru/product/") for link in links]
    pages = [public_url(context["url"], link, "/ru_ru/catalog/") for link in document.next_links]
    return {"product_urls": list(dict.fromkeys(p for p in products if p))[:500],
            "next_urls": list(dict.fromkeys(p for p in pages if p))[:10]}


def resolve(values, index, depth=0, seen=()):
    if type(index) is not int or index >= len(values) or depth >= 10 or index in seen:
        raise ValueError("invalid or cyclic Nuxt reference")
    if index < 0:
        return None
    value = values[index]
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("Nuxt object exceeds supported bounds")
        return {k: resolve(values, v, depth + 1, seen + (index,)) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("Nuxt list exceeds supported bounds")
        return [resolve(values, v, depth + 1, seen + (index,)) for v in value]
    return value


def product(document, context):
    matches = [p for p in jsonld(document) if p.get("@type") == "Product" and p.get("url") == context["url"]]
    if len(matches) != 1:
        raise ValueError("selected product has no unique JSON-LD identity")
    item = matches[0]
    offers = item["offers"]
    if not isinstance(offers, dict) or offers.get("priceCurrency") != "RUB" or not item.get("sku"):
        raise ValueError("unsupported product offer or currency")
    arrays = [json.loads(body) for attrs, body in document.scripts if attrs.get("id") == "__NUXT_DATA__"]
    if len(arrays) != 1 or not isinstance(arrays[0], list) or len(arrays[0]) > 20000:
        raise ValueError("product Nuxt data is missing or exceeds supported bounds")
    values = arrays[0]
    code = context["url"].rsplit("/", 1)[1].partition("-")[2]
    matches = [(i, v) for i, v in enumerate(values) if isinstance(v, dict) and "skus" in v
               and "code" in v and resolve(values, v["code"]) == code]
    if len(matches) != 1:
        raise ValueError("selected product color is ambiguous")
    pointer, obj = matches[0]
    color = resolve(values, obj["color"])["name"]
    if color != item.get("color") or resolve(values, obj["name"]) != item["name"]:
        raise ValueError("product representations disagree")
    pointers = values[obj["skus"]]
    if not isinstance(pointers, list) or not 1 <= len(pointers) <= 50:
        raise ValueError("product has no bounded variant list")
    result = []
    for sku_pointer in pointers:
        sku = resolve(values, sku_pointer)
        stock = sku["stock"]["online"]
        if type(stock) is not int or stock < 0 or not sku.get("skuId"):
            raise ValueError("variant stock or identity is unknown")
        if str(sku["price"]) != str(offers["price"]):
            raise ValueError("variant price differs from the selected JSON-LD offer")
        size = sku["size"]
        if not isinstance(size.get("value"), str) or not size["value"] or not size.get("unit"):
            raise ValueError("variant size is unknown")
        evidence = {"source_url": context["url"], "capture_sha256": context["capture_sha256"],
                    "observed_at": context["observed_at"], "source_kind": "live_public_capture",
                    "fields": {"title": "JSON-LD Product.name", "product_id": "JSON-LD Product.sku",
                               "currency": "JSON-LD Product.offers.priceCurrency",
                               "price": f"__NUXT_DATA__[{sku_pointer}].price; checked against JSON-LD",
                               "sku": f"__NUXT_DATA__[{sku_pointer}].skuId",
                               "color": f"__NUXT_DATA__[{pointer}].color.name; checked against JSON-LD",
                               "size": f"__NUXT_DATA__[{sku_pointer}].size.value",
                               "availability": f"__NUXT_DATA__[{sku_pointer}].stock.online > 0"}}
        row = {"source": context["source_id"], "product_id": str(item["sku"]), "canonical_url": context["url"],
               "sku": str(sku["skuId"]), "title": item["name"], "price": str(sku["price"]), "currency": "RUB",
               "availability": "available" if stock > 0 else "unavailable", "observed_at": context["observed_at"],
               "attributes": {"color": color, "size": size["value"], "size_system": size["unit"]}, "evidence": evidence}
        if sku.get("old_price") is not None:
            row["list_price"] = str(sku["old_price"])
            evidence["fields"]["list_price"] = f"__NUXT_DATA__[{sku_pointer}].old_price"
        result.append(row)
    if len({r["sku"] for r in result}) != len(result):
        raise ValueError("duplicate variant identity")
    return result


def main():
    context = json.loads(Path("input.json").read_text(encoding="utf-8"))
    html = context["html"]
    if hashlib.sha256(html.encode()).hexdigest() != context["capture_sha256"]:
        raise ValueError("capture digest mismatch")
    document = Document()
    document.feed(html)
    output = catalog(document, context) if context["mode"] == "catalog" else product(document, context)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
