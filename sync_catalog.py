#!/usr/bin/env python3
"""
Copy Paddle Billing configuration from sandbox to production (go-live sync).

Paddle sandbox and live are separate accounts with different entity IDs. This
script reads sandbox data via the API, transforms entities into create/update
request bodies, and recreates them in production.

Syncs catalog (products, prices, discounts), notification destinations, and
account settings (checkout defaults, payment methods, statement descriptor).

Usage:
    export PADDLE_SANDBOX_API_KEY="pdl_sdbx_apikey_..."
    export PADDLE_LIVE_API_KEY="pdl_live_apikey_..."
    python sync_catalog.py --dry-run
    python sync_catalog.py --live-checkout-url https://example.com/checkout -o report.json

See README.md for full documentation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

PADDLE_VERSION = "1"
SANDBOX_BASE_URL = "https://sandbox-api.paddle.com"
LIVE_BASE_URL = "https://api.paddle.com"
IMPORTED_FROM = "paddle-catalog-sync"
SANDBOX_ID_KEY = "_paddle_catalog_sync_sandbox_id"
VERSION = "1.0.0"

PRODUCT_CREATE_FIELDS = (
    "name",
    "description",
    "type",
    "tax_category",
    "image_url",
    "custom_data",
    "import_meta",
)

PRICE_CREATE_FIELDS = (
    "description",
    "type",
    "name",
    "product_id",
    "billing_cycle",
    "trial_period",
    "tax_mode",
    "unit_price",
    "unit_price_overrides",
    "quantity",
    "custom_data",
    "import_meta",
)

DISCOUNT_CREATE_FIELDS = (
    "description",
    "type",
    "amount",
    "currency_code",
    "enabled_for_checkout",
    "code",
    "recur",
    "maximum_recurring_intervals",
    "usage_limit",
    "restrict_to",
    "expires_at",
    "custom_data",
    "import_meta",
)

NOTIFICATION_CREATE_FIELDS = (
    "description",
    "type",
    "destination",
    "api_version",
    "include_sensitive_fields",
    "traffic_source",
    "subscribed_events",
)

ACCOUNT_SETTING_FIELDS = (
    "default_tax_mode",
    "primary_checkout_color",
    "saved_payment_methods_enabled",
)


class PaddleAPIError(Exception):
    def __init__(self, status: int, body: dict[str, Any] | str, method: str, path: str):
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        message = f"{method} {path} failed with HTTP {status}"
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            detail = err.get("detail") or err.get("message") or err
            message = f"{message}: {detail}"
        super().__init__(message)


class PaddleClient:
    def __init__(self, base_url: str, api_key: str, *, partner_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.partner_id = partner_id

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Paddle-Version": PADDLE_VERSION,
        }
        if self.partner_id:
            headers["Paddle-PartnerID"] = self.partner_id
        req = Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            raise PaddleAPIError(exc.code, parsed, method, path) from exc

    def list_all(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after: str | None = None
        base_params = dict(params or {})
        base_params.setdefault("per_page", 200)

        while True:
            page_params = dict(base_params)
            if after:
                page_params["after"] = after
            response = self.request("GET", path, params=page_params)
            items.extend(response.get("data", []))
            pagination = response.get("meta", {}).get("pagination", {})
            if not pagination.get("has_more"):
                break
            after = _extract_after_cursor(pagination.get("next"))
            if not after:
                break
        return items

    def find_by_external_id(self, resource: str, external_id: str) -> dict[str, Any] | None:
        response = self.request(
            "GET",
            f"/{resource}",
            params={"external_id": external_id, "per_page": 1},
        )
        data = response.get("data") or []
        return data[0] if data else None


def _extract_after_cursor(next_url: str | None) -> str | None:
    if not next_url:
        return None
    marker = "after="
    idx = next_url.find(marker)
    if idx == -1:
        return None
    return next_url[idx + len(marker) :].split("&", 1)[0]


def _sanitize_trial_period(trial_period: dict[str, Any] | None) -> dict[str, Any] | None:
    if not trial_period:
        return None
    sanitized: dict[str, Any] = {
        "interval": trial_period["interval"],
        "frequency": trial_period["frequency"],
    }
    if "requires_payment_method" in trial_period:
        sanitized["requires_payment_method"] = trial_period["requires_payment_method"]
    if trial_period.get("unit_price"):
        sanitized["unit_price"] = trial_period["unit_price"]
    return sanitized


def _sanitize_unit_price_overrides(
    overrides: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not overrides:
        return None
    return [
        {
            "country_codes": override["country_codes"],
            "unit_price": override["unit_price"],
        }
        for override in overrides
    ]


def _with_sandbox_tracking(
    custom_data: Any,
    sandbox_id: str,
) -> dict[str, Any]:
    data = dict(custom_data) if isinstance(custom_data, dict) else {}
    data[SANDBOX_ID_KEY] = sandbox_id
    return data


def _sandbox_id_from_entity(entity: dict[str, Any]) -> str | None:
    custom_data = entity.get("custom_data")
    if isinstance(custom_data, dict):
        value = custom_data.get(SANDBOX_ID_KEY)
        if isinstance(value, str) and value:
            return value
    import_meta = entity.get("import_meta") or {}
    external_id = import_meta.get("external_id")
    if isinstance(external_id, str) and external_id:
        return external_id
    return None


def _pick_fields(entity: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in fields:
        if field not in entity:
            continue
        value = entity[field]
        if value is None:
            continue
        if field == "image_url" and value == "":
            continue
        payload[field] = value
    return payload


def _remap_custom_data(
    custom_data: Any,
    product_id_map: dict[str, str],
    price_id_map: dict[str, str],
) -> Any:
    if custom_data is None:
        return None
    if isinstance(custom_data, str):
        return custom_data
    if isinstance(custom_data, list):
        return [
            _remap_custom_data(item, product_id_map, price_id_map) for item in custom_data
        ]
    if not isinstance(custom_data, dict):
        return custom_data

    remapped: dict[str, Any] = {}
    for key, value in custom_data.items():
        if key in {"suggested_addons", "addon_ids", "related_products"} and isinstance(
            value, list
        ):
            remapped[key] = [
                product_id_map.get(item, item) if isinstance(item, str) else item
                for item in value
            ]
            continue
        if isinstance(value, str):
            if value.startswith("pro_") and value in product_id_map:
                remapped[key] = product_id_map[value]
                continue
            if value.startswith("pri_") and value in price_id_map:
                remapped[key] = price_id_map[value]
                continue
        remapped[key] = _remap_custom_data(value, product_id_map, price_id_map)
    return remapped


def _remap_entity_id(
    entity_id: str,
    product_id_map: dict[str, str],
    price_id_map: dict[str, str],
) -> str:
    if entity_id.startswith("pro_"):
        return product_id_map.get(entity_id, entity_id)
    if entity_id.startswith("pri_"):
        return price_id_map.get(entity_id, entity_id)
    return entity_id


def _remap_restrict_to(
    restrict_to: list[str] | None,
    product_id_map: dict[str, str],
    price_id_map: dict[str, str],
) -> list[str] | None:
    if not restrict_to:
        return None
    return [
        _remap_entity_id(item, product_id_map, price_id_map)
        for item in restrict_to
    ]


def _extract_event_names(events: list[Any] | None) -> list[str]:
    names: list[str] = []
    for event in events or []:
        if isinstance(event, str):
            names.append(event)
        elif isinstance(event, dict) and event.get("name"):
            names.append(event["name"])
    return names


def _remap_webhook_url(
    destination: str,
    *,
    url_map: dict[str, str],
    host_replace: tuple[str, str] | None,
) -> str | None:
    if destination in url_map:
        return url_map[destination]
    if host_replace:
        old_host, new_host = host_replace
        parsed = urlparse(destination)
        host = (parsed.hostname or "").lower()
        if host == old_host.lower() or host.endswith(f".{old_host.lower()}"):
            new_netloc = parsed.netloc.replace(old_host, new_host, 1)
            return urlunparse(parsed._replace(netloc=new_netloc))
    if destination.startswith("http://") or destination.startswith("https://"):
        lowered = destination.lower()
        if "localhost" in lowered or "127.0.0.1" in lowered:
            return None
    return destination


def _new_report() -> dict[str, Any]:
    return {
        "products": [],
        "prices": [],
        "discounts": [],
        "notification_settings": [],
        "account_settings": None,
        "payment_methods": None,
        "statement_descriptor": None,
        "skipped_products": [],
        "skipped_prices": [],
        "skipped_discounts": [],
        "skipped_notification_settings": [],
        "warnings": [],
        "errors": [],
        "product_id_map": {},
        "price_id_map": {},
        "discount_id_map": {},
        "notification_id_map": {},
        "notification_secrets": {},
    }


def _append_error(report: dict[str, Any], entry: dict[str, Any]) -> None:
    report["errors"].append(entry)


def _append_warning(report: dict[str, Any], message: str) -> None:
    report["warnings"].append(message)
    print(f"  [warning] {message}", file=sys.stderr)


def build_product_payload(
    sandbox_product: dict[str, Any],
    *,
    preserve_import_meta: bool,
    use_import_meta: bool,
) -> dict[str, Any]:
    payload = _pick_fields(sandbox_product, PRODUCT_CREATE_FIELDS)
    payload.pop("custom_data", None)
    payload["custom_data"] = _with_sandbox_tracking(
        sandbox_product.get("custom_data"),
        sandbox_product["id"],
    )
    if use_import_meta:
        if preserve_import_meta and sandbox_product.get("import_meta"):
            payload["import_meta"] = sandbox_product["import_meta"]
        else:
            payload["import_meta"] = {
                "imported_from": IMPORTED_FROM,
                "external_id": sandbox_product["id"],
            }
    return payload


def build_price_payload(
    sandbox_price: dict[str, Any],
    live_product_id: str,
    *,
    preserve_import_meta: bool,
    use_import_meta: bool,
) -> dict[str, Any]:
    payload = _pick_fields(sandbox_price, PRICE_CREATE_FIELDS)
    payload["product_id"] = live_product_id
    payload.pop("custom_data", None)
    payload["custom_data"] = _with_sandbox_tracking(
        sandbox_price.get("custom_data"),
        sandbox_price["id"],
    )
    if payload.get("trial_period"):
        payload["trial_period"] = _sanitize_trial_period(payload["trial_period"])
    if payload.get("unit_price_overrides"):
        payload["unit_price_overrides"] = _sanitize_unit_price_overrides(
            payload["unit_price_overrides"]
        )
    if use_import_meta:
        if preserve_import_meta and sandbox_price.get("import_meta"):
            payload["import_meta"] = sandbox_price["import_meta"]
        else:
            payload["import_meta"] = {
                "imported_from": IMPORTED_FROM,
                "external_id": sandbox_price["id"],
            }
    return payload


def build_discount_payload(
    sandbox_discount: dict[str, Any],
    *,
    product_id_map: dict[str, str],
    price_id_map: dict[str, str],
    preserve_import_meta: bool,
    use_import_meta: bool,
) -> dict[str, Any]:
    payload = _pick_fields(sandbox_discount, DISCOUNT_CREATE_FIELDS)
    payload.pop("custom_data", None)
    payload["custom_data"] = _with_sandbox_tracking(
        sandbox_discount.get("custom_data"),
        sandbox_discount["id"],
    )
    if payload.get("restrict_to"):
        payload["restrict_to"] = _remap_restrict_to(
            payload["restrict_to"],
            product_id_map,
            price_id_map,
        )
    if use_import_meta:
        if preserve_import_meta and sandbox_discount.get("import_meta"):
            payload["import_meta"] = sandbox_discount["import_meta"]
        else:
            payload["import_meta"] = {
                "imported_from": IMPORTED_FROM,
                "external_id": sandbox_discount["id"],
            }
    return payload


def build_notification_payload(
    sandbox_notification: dict[str, Any],
    *,
    destination: str,
) -> dict[str, Any]:
    payload = _pick_fields(sandbox_notification, NOTIFICATION_CREATE_FIELDS)
    payload["destination"] = destination
    payload["subscribed_events"] = _extract_event_names(
        sandbox_notification.get("subscribed_events")
    )
    if payload.get("traffic_source") == "simulation":
        payload["traffic_source"] = "platform"
    return payload


def build_account_settings_payload(
    sandbox_settings: dict[str, Any],
    *,
    live_checkout_url: str | None,
) -> dict[str, Any]:
    payload = _pick_fields(sandbox_settings, ACCOUNT_SETTING_FIELDS)
    if live_checkout_url:
        payload["default_checkout_url"] = live_checkout_url
    return payload


def build_payment_methods_payload(
    sandbox_payment_methods: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for method, settings in sandbox_payment_methods.items():
        if not isinstance(settings, dict):
            continue
        if "enabled_for_checkout" in settings:
            payload[method] = {
                "enabled_for_checkout": settings["enabled_for_checkout"],
            }
    return payload


def fetch_sandbox_catalog(sandbox: PaddleClient, include_archived: bool) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "include": "prices",
        "status": ["active", "archived"] if include_archived else ["active"],
        "type": "standard",
    }
    products = sandbox.list_all("/products", params=params)
    for product in products:
        product["prices"] = [
            price
            for price in (product.get("prices") or [])
            if price.get("type") == "standard"
            and (include_archived or price.get("status") == "active")
        ]
    return products


def load_mapping_file(
    path: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    product_map = dict(data.get("product_id_map") or {})
    price_map = dict(data.get("price_id_map") or {})
    discount_map = dict(data.get("discount_id_map") or {})
    notification_map = dict(data.get("notification_id_map") or {})
    for entry in data.get("products") or []:
        product_map.setdefault(entry["sandbox_id"], entry["live_id"])
    for entry in data.get("prices") or []:
        price_map.setdefault(entry["sandbox_id"], entry["live_id"])
    for entry in data.get("discounts") or []:
        discount_map.setdefault(entry["sandbox_id"], entry["live_id"])
    for entry in data.get("notification_settings") or []:
        notification_map.setdefault(entry["sandbox_id"], entry["live_id"])
    return product_map, price_map, discount_map, notification_map


def build_live_catalog_index(
    live: PaddleClient,
    include_archived: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    params: dict[str, Any] = {
        "include": "prices",
        "status": ["active", "archived"] if include_archived else ["active"],
        "type": "standard",
    }
    products = live.list_all("/products", params=params)
    products_by_sandbox_id: dict[str, dict[str, Any]] = {}
    prices_by_sandbox_id: dict[str, dict[str, Any]] = {}

    for product in products:
        sandbox_id = _sandbox_id_from_entity(product)
        if sandbox_id:
            products_by_sandbox_id[sandbox_id] = product
        for price in product.get("prices") or []:
            if price.get("type") != "standard":
                continue
            if not include_archived and price.get("status") != "active":
                continue
            price_sandbox_id = _sandbox_id_from_entity(price)
            if price_sandbox_id:
                prices_by_sandbox_id[price_sandbox_id] = price

    return products_by_sandbox_id, prices_by_sandbox_id


def build_live_discount_index(
    live: PaddleClient,
    include_archived: bool,
) -> dict[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "status": ["active", "archived"] if include_archived else ["active"],
        "mode": "standard",
    }
    discounts = live.list_all("/discounts", params=params)
    by_sandbox_id: dict[str, dict[str, Any]] = {}
    for discount in discounts:
        sandbox_id = _sandbox_id_from_entity(discount)
        if sandbox_id:
            by_sandbox_id[sandbox_id] = discount
    return by_sandbox_id


def build_live_notification_index(
    live: PaddleClient,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    notifications = live.list_all("/notification-settings")
    by_sandbox_id: dict[str, dict[str, Any]] = {}
    by_description: dict[str, dict[str, Any]] = {}
    for notification in notifications:
        sandbox_id = _sandbox_id_from_entity(notification)
        if sandbox_id:
            by_sandbox_id[sandbox_id] = notification
        description = (notification.get("description") or "").strip().lower()
        if description:
            by_description[description] = notification
    return by_sandbox_id, by_description


def find_existing_product(
    live: PaddleClient,
    sandbox_product: dict[str, Any],
    *,
    use_import_meta: bool,
    product_id_map: dict[str, str],
    live_products_by_sandbox_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sandbox_product_id = sandbox_product["id"]
    if sandbox_product_id in product_id_map:
        return {"id": product_id_map[sandbox_product_id]}

    if sandbox_product_id in live_products_by_sandbox_id:
        return live_products_by_sandbox_id[sandbox_product_id]

    if use_import_meta:
        external_id = (
            sandbox_product.get("import_meta", {}) or {}
        ).get("external_id", sandbox_product_id)
        existing = live.find_by_external_id("products", external_id)
        if existing:
            return existing

    return None


def find_existing_price(
    live: PaddleClient,
    sandbox_price: dict[str, Any],
    *,
    use_import_meta: bool,
    price_id_map: dict[str, str],
    live_prices_by_sandbox_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sandbox_price_id = sandbox_price["id"]
    if sandbox_price_id in price_id_map:
        return {"id": price_id_map[sandbox_price_id]}

    if sandbox_price_id in live_prices_by_sandbox_id:
        return live_prices_by_sandbox_id[sandbox_price_id]

    if use_import_meta:
        price_external_id = (
            sandbox_price.get("import_meta", {}) or {}
        ).get("external_id", sandbox_price_id)
        existing = live.find_by_external_id("prices", price_external_id)
        if existing:
            return existing

    return None


def find_existing_discount(
    sandbox_discount: dict[str, Any],
    *,
    use_import_meta: bool,
    live: PaddleClient,
    discount_id_map: dict[str, str],
    live_discounts_by_sandbox_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sandbox_discount_id = sandbox_discount["id"]
    if sandbox_discount_id in discount_id_map:
        return {"id": discount_id_map[sandbox_discount_id]}
    if sandbox_discount_id in live_discounts_by_sandbox_id:
        return live_discounts_by_sandbox_id[sandbox_discount_id]
    if use_import_meta:
        external_id = (
            sandbox_discount.get("import_meta", {}) or {}
        ).get("external_id", sandbox_discount_id)
        existing = live.find_by_external_id("discounts", external_id)
        if existing:
            return existing
    code = sandbox_discount.get("code")
    if code:
        response = live.request(
            "GET",
            "/discounts",
            params={"code": code, "per_page": 1},
        )
        data = response.get("data") or []
        if data:
            return data[0]
    return None


def find_existing_notification(
    sandbox_notification: dict[str, Any],
    *,
    notification_id_map: dict[str, str],
    live_notifications_by_sandbox_id: dict[str, dict[str, Any]],
    live_notifications_by_description: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sandbox_notification_id = sandbox_notification["id"]
    if sandbox_notification_id in notification_id_map:
        return {"id": notification_id_map[sandbox_notification_id]}
    if sandbox_notification_id in live_notifications_by_sandbox_id:
        return live_notifications_by_sandbox_id[sandbox_notification_id]
    description = (sandbox_notification.get("description") or "").strip().lower()
    if description and description in live_notifications_by_description:
        return live_notifications_by_description[description]
    return None


def sync_catalog(
    *,
    sandbox: PaddleClient,
    live: PaddleClient,
    report: dict[str, Any],
    dry_run: bool,
    include_archived: bool,
    preserve_import_meta: bool,
    use_import_meta: bool,
    delay_seconds: float,
    mapping_path: str | None,
) -> None:
    print("Fetching sandbox catalog...")
    sandbox_products = fetch_sandbox_catalog(sandbox, include_archived)
    print(
        f"Found {len(sandbox_products)} standard product(s) "
        f"with {sum(len(p.get('prices') or []) for p in sandbox_products)} price(s)."
    )

    product_id_map: dict[str, str] = dict(report.get("product_id_map") or {})
    price_id_map: dict[str, str] = dict(report.get("price_id_map") or {})
    if mapping_path:
        loaded_products, loaded_prices, _, _ = load_mapping_file(mapping_path)
        product_id_map.update(loaded_products)
        price_id_map.update(loaded_prices)
        print(
            f"Loaded {len(loaded_products)} product mapping(s) and "
            f"{len(loaded_prices)} price mapping(s) from {mapping_path}."
        )

    live_products_by_sandbox_id: dict[str, dict[str, Any]] = {}
    live_prices_by_sandbox_id: dict[str, dict[str, Any]] = {}
    if not dry_run:
        print("Indexing production catalog for idempotent re-runs...")
        live_products_by_sandbox_id, live_prices_by_sandbox_id = build_live_catalog_index(
            live,
            include_archived,
        )
        print(
            f"Found {len(live_products_by_sandbox_id)} previously synced product(s) "
            f"and {len(live_prices_by_sandbox_id)} price(s) in production."
        )

    for sandbox_product in sandbox_products:
        sandbox_product_id = sandbox_product["id"]

        existing = find_existing_product(
            live,
            sandbox_product,
            use_import_meta=use_import_meta,
            product_id_map=product_id_map,
            live_products_by_sandbox_id=live_products_by_sandbox_id,
        )
        if existing:
            live_product_id = existing["id"]
            product_id_map[sandbox_product_id] = live_product_id
            report["skipped_products"].append(
                {
                    "sandbox_id": sandbox_product_id,
                    "live_id": live_product_id,
                    "name": sandbox_product.get("name"),
                    "reason": "already_exists",
                }
            )
            print(f"  [skip product] {sandbox_product['name']} -> {live_product_id}")
        else:
            payload = build_product_payload(
                sandbox_product,
                preserve_import_meta=preserve_import_meta,
                use_import_meta=use_import_meta,
            )
            if dry_run:
                live_product_id = f"dry_run_pro_{sandbox_product_id[-8:]}"
                print(f"  [dry-run product] {sandbox_product['name']}")
            else:
                try:
                    response = live.request("POST", "/products", body=payload)
                    live_product_id = response["data"]["id"]
                    print(
                        f"  [created product] {sandbox_product['name']} "
                        f"{sandbox_product_id} -> {live_product_id}"
                    )
                    if delay_seconds:
                        time.sleep(delay_seconds)
                except PaddleAPIError as exc:
                    _append_error(
                        report,
                        {
                            "entity": "product",
                            "sandbox_id": sandbox_product_id,
                            "name": sandbox_product.get("name"),
                            "error": str(exc),
                        },
                    )
                    print(f"  [error product] {sandbox_product['name']}: {exc}", file=sys.stderr)
                    continue

            product_id_map[sandbox_product_id] = live_product_id
            report["products"].append(
                {
                    "sandbox_id": sandbox_product_id,
                    "live_id": live_product_id,
                    "name": sandbox_product.get("name"),
                }
            )

        for sandbox_price in sandbox_product.get("prices") or []:
            sandbox_price_id = sandbox_price["id"]

            existing_price = find_existing_price(
                live,
                sandbox_price,
                use_import_meta=use_import_meta,
                price_id_map=price_id_map,
                live_prices_by_sandbox_id=live_prices_by_sandbox_id,
            )
            if existing_price:
                price_id_map[sandbox_price_id] = existing_price["id"]
                report["skipped_prices"].append(
                    {
                        "sandbox_id": sandbox_price_id,
                        "live_id": existing_price["id"],
                        "description": sandbox_price.get("description"),
                        "reason": "already_exists",
                    }
                )
                print(
                    f"    [skip price] {sandbox_price.get('description')} "
                    f"-> {existing_price['id']}"
                )
                continue

            price_payload = build_price_payload(
                sandbox_price,
                live_product_id,
                preserve_import_meta=preserve_import_meta,
                use_import_meta=use_import_meta,
            )
            price_payload["custom_data"] = _remap_custom_data(
                price_payload.get("custom_data"),
                product_id_map,
                price_id_map,
            )

            if dry_run:
                live_price_id = f"dry_run_pri_{sandbox_price_id[-8:]}"
                print(f"    [dry-run price] {sandbox_price.get('description')}")
            else:
                try:
                    response = live.request("POST", "/prices", body=price_payload)
                    live_price_id = response["data"]["id"]
                    print(
                        f"    [created price] {sandbox_price.get('description')} "
                        f"{sandbox_price_id} -> {live_price_id}"
                    )
                    if delay_seconds:
                        time.sleep(delay_seconds)
                except PaddleAPIError as exc:
                    _append_error(
                        report,
                        {
                            "entity": "price",
                            "sandbox_id": sandbox_price_id,
                            "product_sandbox_id": sandbox_product_id,
                            "description": sandbox_price.get("description"),
                            "error": str(exc),
                        },
                    )
                    print(
                        f"    [error price] {sandbox_price.get('description')}: {exc}",
                        file=sys.stderr,
                    )
                    continue

            price_id_map[sandbox_price_id] = live_price_id
            report["prices"].append(
                {
                    "sandbox_id": sandbox_price_id,
                    "live_id": live_price_id,
                    "product_sandbox_id": sandbox_product_id,
                    "product_live_id": live_product_id,
                    "description": sandbox_price.get("description"),
                    "name": sandbox_price.get("name"),
                }
            )

    # Second pass: remap product custom_data now that all product IDs are known.
    for entry in report["products"]:
        if dry_run:
            continue
        sandbox_product = next(
            p for p in sandbox_products if p["id"] == entry["sandbox_id"]
        )
        custom_data = _remap_custom_data(
            sandbox_product.get("custom_data"),
            product_id_map,
            price_id_map,
        )
        custom_data = _with_sandbox_tracking(custom_data, entry["sandbox_id"])
        existing_custom_data = sandbox_product.get("custom_data") or {}
        existing_custom_data = dict(existing_custom_data)
        existing_custom_data[SANDBOX_ID_KEY] = entry["sandbox_id"]
        if custom_data != existing_custom_data:
            try:
                live.request(
                    "PATCH",
                    f"/products/{entry['live_id']}",
                    body={"custom_data": custom_data},
                )
                print(f"  [updated custom_data] product {entry['live_id']}")
            except PaddleAPIError as exc:
                _append_error(
                    report,
                    {
                        "entity": "product_custom_data",
                        "live_id": entry["live_id"],
                        "error": str(exc),
                    },
                )

    report["product_id_map"] = product_id_map
    report["price_id_map"] = price_id_map


def sync_discounts(
    *,
    sandbox: PaddleClient,
    live: PaddleClient,
    report: dict[str, Any],
    dry_run: bool,
    include_archived: bool,
    preserve_import_meta: bool,
    use_import_meta: bool,
    delay_seconds: float,
    mapping_path: str | None,
) -> None:
    product_id_map = dict(report.get("product_id_map") or {})
    price_id_map = dict(report.get("price_id_map") or {})
    discount_id_map: dict[str, str] = dict(report.get("discount_id_map") or {})
    if mapping_path:
        _, _, loaded_discounts, _ = load_mapping_file(mapping_path)
        discount_id_map.update(loaded_discounts)

    params: dict[str, Any] = {
        "status": ["active", "archived"] if include_archived else ["active"],
        "mode": "standard",
    }
    print("Fetching sandbox discounts...")
    sandbox_discounts = sandbox.list_all("/discounts", params=params)
    print(f"Found {len(sandbox_discounts)} standard discount(s).")

    live_discounts_by_sandbox_id: dict[str, dict[str, Any]] = {}
    if not dry_run:
        live_discounts_by_sandbox_id = build_live_discount_index(live, include_archived)
        print(
            f"Found {len(live_discounts_by_sandbox_id)} previously synced discount(s) "
            "in production."
        )

    for sandbox_discount in sandbox_discounts:
        sandbox_discount_id = sandbox_discount["id"]
        existing = find_existing_discount(
            sandbox_discount,
            use_import_meta=use_import_meta,
            live=live,
            discount_id_map=discount_id_map,
            live_discounts_by_sandbox_id=live_discounts_by_sandbox_id,
        )
        if existing:
            live_discount_id = existing["id"]
            discount_id_map[sandbox_discount_id] = live_discount_id
            report["skipped_discounts"].append(
                {
                    "sandbox_id": sandbox_discount_id,
                    "live_id": live_discount_id,
                    "description": sandbox_discount.get("description"),
                    "reason": "already_exists",
                }
            )
            print(
                f"  [skip discount] {sandbox_discount.get('description')} "
                f"-> {live_discount_id}"
            )
            continue

        payload = build_discount_payload(
            sandbox_discount,
            product_id_map=product_id_map,
            price_id_map=price_id_map,
            preserve_import_meta=preserve_import_meta,
            use_import_meta=use_import_meta,
        )
        if dry_run:
            live_discount_id = f"dry_run_dsc_{sandbox_discount_id[-8:]}"
            print(f"  [dry-run discount] {sandbox_discount.get('description')}")
        else:
            try:
                response = live.request("POST", "/discounts", body=payload)
                live_discount_id = response["data"]["id"]
                print(
                    f"  [created discount] {sandbox_discount.get('description')} "
                    f"{sandbox_discount_id} -> {live_discount_id}"
                )
                if delay_seconds:
                    time.sleep(delay_seconds)
            except PaddleAPIError as exc:
                _append_error(
                    report,
                    {
                        "entity": "discount",
                        "sandbox_id": sandbox_discount_id,
                        "description": sandbox_discount.get("description"),
                        "error": str(exc),
                    },
                )
                print(
                    f"  [error discount] {sandbox_discount.get('description')}: {exc}",
                    file=sys.stderr,
                )
                continue

        discount_id_map[sandbox_discount_id] = live_discount_id
        report["discounts"].append(
            {
                "sandbox_id": sandbox_discount_id,
                "live_id": live_discount_id,
                "description": sandbox_discount.get("description"),
                "code": sandbox_discount.get("code"),
            }
        )

    report["discount_id_map"] = discount_id_map


def sync_notification_settings(
    *,
    sandbox: PaddleClient,
    live: PaddleClient,
    report: dict[str, Any],
    dry_run: bool,
    delay_seconds: float,
    mapping_path: str | None,
    webhook_url_map: dict[str, str],
    webhook_host_replace: tuple[str, str] | None,
) -> None:
    notification_id_map: dict[str, str] = dict(report.get("notification_id_map") or {})
    if mapping_path:
        _, _, _, loaded_notifications = load_mapping_file(mapping_path)
        notification_id_map.update(loaded_notifications)

    print("Fetching sandbox notification destinations...")
    sandbox_notifications = sandbox.list_all("/notification-settings")
    print(f"Found {len(sandbox_notifications)} notification destination(s).")

    live_by_sandbox_id: dict[str, dict[str, Any]] = {}
    live_by_description: dict[str, dict[str, Any]] = {}
    if not dry_run:
        live_by_sandbox_id, live_by_description = build_live_notification_index(live)
        print(
            f"Found {len(live_by_sandbox_id)} previously synced notification destination(s) "
            "in production."
        )

    for sandbox_notification in sandbox_notifications:
        sandbox_notification_id = sandbox_notification["id"]
        existing = find_existing_notification(
            sandbox_notification,
            notification_id_map=notification_id_map,
            live_notifications_by_sandbox_id=live_by_sandbox_id,
            live_notifications_by_description=live_by_description,
        )
        if existing:
            live_notification_id = existing["id"]
            notification_id_map[sandbox_notification_id] = live_notification_id
            report["skipped_notification_settings"].append(
                {
                    "sandbox_id": sandbox_notification_id,
                    "live_id": live_notification_id,
                    "description": sandbox_notification.get("description"),
                    "reason": "already_exists",
                }
            )
            print(
                f"  [skip notification] {sandbox_notification.get('description')} "
                f"-> {live_notification_id}"
            )
            continue

        destination = sandbox_notification.get("destination") or ""
        if sandbox_notification.get("type") == "url":
            mapped_destination = _remap_webhook_url(
                destination,
                url_map=webhook_url_map,
                host_replace=webhook_host_replace,
            )
            if not mapped_destination:
                message = (
                    f"Skipped notification '{sandbox_notification.get('description')}' "
                    f"because sandbox URL '{destination}' cannot be used in production. "
                    "Provide --webhook-url-map or --webhook-host-replace."
                )
                _append_warning(report, message)
                continue
            destination = mapped_destination

        payload = build_notification_payload(
            sandbox_notification,
            destination=destination,
        )
        if dry_run:
            live_notification_id = f"dry_run_ntfset_{sandbox_notification_id[-8:]}"
            print(
                f"  [dry-run notification] {sandbox_notification.get('description')} "
                f"-> {destination}"
            )
        else:
            try:
                response = live.request("POST", "/notification-settings", body=payload)
                created = response["data"]
                live_notification_id = created["id"]
                secret = created.get("endpoint_secret_key")
                print(
                    f"  [created notification] {sandbox_notification.get('description')} "
                    f"{sandbox_notification_id} -> {live_notification_id}"
                )
                if secret:
                    report["notification_secrets"][sandbox_notification_id] = {
                        "live_id": live_notification_id,
                        "endpoint_secret_key": secret,
                        "description": sandbox_notification.get("description"),
                    }
                    print(
                        "    Save endpoint_secret_key from the report output — "
                        "it cannot be retrieved again."
                    )
                if delay_seconds:
                    time.sleep(delay_seconds)
            except PaddleAPIError as exc:
                _append_error(
                    report,
                    {
                        "entity": "notification_setting",
                        "sandbox_id": sandbox_notification_id,
                        "description": sandbox_notification.get("description"),
                        "error": str(exc),
                    },
                )
                print(
                    f"  [error notification] {sandbox_notification.get('description')}: {exc}",
                    file=sys.stderr,
                )
                continue

        notification_id_map[sandbox_notification_id] = live_notification_id
        report["notification_settings"].append(
            {
                "sandbox_id": sandbox_notification_id,
                "live_id": live_notification_id,
                "description": sandbox_notification.get("description"),
                "destination": destination,
                "type": sandbox_notification.get("type"),
            }
        )

    report["notification_id_map"] = notification_id_map


def sync_account_settings(
    *,
    sandbox: PaddleClient,
    live: PaddleClient,
    report: dict[str, Any],
    dry_run: bool,
    delay_seconds: float,
    live_checkout_url: str | None,
) -> None:
    print("Fetching sandbox account settings...")
    sandbox_settings = sandbox.request("GET", "/settings/account").get("data") or {}
    account_payload = build_account_settings_payload(
        sandbox_settings,
        live_checkout_url=live_checkout_url,
    )
    if not live_checkout_url and sandbox_settings.get("default_checkout_url"):
        sandbox_checkout = sandbox_settings["default_checkout_url"]
        if "localhost" in sandbox_checkout.lower() or "127.0.0.1" in sandbox_checkout:
            _append_warning(
                report,
                "Skipped default_checkout_url (sandbox uses localhost). "
                "Pass --live-checkout-url to set the production checkout page.",
            )
        else:
            _append_warning(
                report,
                "Skipped default_checkout_url. Pass --live-checkout-url with your "
                "verified production checkout URL.",
            )

    if dry_run:
        print(f"  [dry-run account settings] {json.dumps(account_payload)}")
    elif account_payload:
        try:
            response = live.request("PATCH", "/settings/account", body=account_payload)
            report["account_settings"] = response.get("data")
            print("  [updated account settings]")
            if delay_seconds:
                time.sleep(delay_seconds)
        except PaddleAPIError as exc:
            _append_error(
                report,
                {"entity": "account_settings", "error": str(exc)},
            )
            print(f"  [error account settings] {exc}", file=sys.stderr)

    try:
        sandbox_descriptor = (
            sandbox.request("GET", "/settings/statement-descriptor").get("data") or {}
        )
        descriptor_name = sandbox_descriptor.get("name")
        if descriptor_name:
            if dry_run:
                print(f"  [dry-run statement descriptor] {descriptor_name}")
            else:
                response = live.request(
                    "PATCH",
                    "/settings/statement-descriptor",
                    body={"name": descriptor_name},
                )
                report["statement_descriptor"] = response.get("data")
                print(f"  [updated statement descriptor] {descriptor_name}")
                if delay_seconds:
                    time.sleep(delay_seconds)
    except PaddleAPIError as exc:
        _append_error(
            report,
            {"entity": "statement_descriptor", "error": str(exc)},
        )
        print(f"  [error statement descriptor] {exc}", file=sys.stderr)

    try:
        sandbox_payment_methods = (
            sandbox.request("GET", "/settings/payment-methods").get("data") or {}
        )
        payment_methods_payload = build_payment_methods_payload(sandbox_payment_methods)
        if payment_methods_payload:
            if dry_run:
                enabled = [
                    method
                    for method, settings in payment_methods_payload.items()
                    if settings.get("enabled_for_checkout")
                ]
                print(f"  [dry-run payment methods] enable: {', '.join(enabled)}")
            else:
                response = live.request(
                    "PATCH",
                    "/settings/payment-methods",
                    body=payment_methods_payload,
                )
                report["payment_methods"] = response.get("data")
                print("  [updated payment methods]")
        else:
            _append_warning(report, "No payment method settings found in sandbox.")
    except PaddleAPIError as exc:
        _append_error(
            report,
            {"entity": "payment_methods", "error": str(exc)},
        )
        print(f"  [error payment methods] {exc}", file=sys.stderr)


def go_live_sync(
    *,
    sandbox_key: str,
    live_key: str,
    partner_id: str | None,
    dry_run: bool,
    include_archived: bool,
    preserve_import_meta: bool,
    delay_seconds: float,
    output_path: str | None,
    mapping_path: str | None,
    live_checkout_url: str | None,
    webhook_url_map: dict[str, str],
    webhook_host_replace: tuple[str, str] | None,
    skip_catalog: bool,
    skip_discounts: bool,
    skip_webhooks: bool,
    skip_settings: bool,
) -> dict[str, Any]:
    use_import_meta = bool(partner_id)
    sandbox = PaddleClient(SANDBOX_BASE_URL, sandbox_key, partner_id=partner_id)
    live = PaddleClient(LIVE_BASE_URL, live_key, partner_id=partner_id)
    report = _new_report()

    if not skip_catalog:
        print("\n=== Catalog: products and prices ===")
        sync_catalog(
            sandbox=sandbox,
            live=live,
            report=report,
            dry_run=dry_run,
            include_archived=include_archived,
            preserve_import_meta=preserve_import_meta,
            use_import_meta=use_import_meta,
            delay_seconds=delay_seconds,
            mapping_path=mapping_path,
        )

    if not skip_discounts:
        print("\n=== Catalog: discounts ===")
        sync_discounts(
            sandbox=sandbox,
            live=live,
            report=report,
            dry_run=dry_run,
            include_archived=include_archived,
            preserve_import_meta=preserve_import_meta,
            use_import_meta=use_import_meta,
            delay_seconds=delay_seconds,
            mapping_path=mapping_path,
        )

    if not skip_webhooks:
        print("\n=== Notification destinations ===")
        sync_notification_settings(
            sandbox=sandbox,
            live=live,
            report=report,
            dry_run=dry_run,
            delay_seconds=delay_seconds,
            mapping_path=mapping_path,
            webhook_url_map=webhook_url_map,
            webhook_host_replace=webhook_host_replace,
        )

    if not skip_settings:
        print("\n=== Account settings ===")
        sync_account_settings(
            sandbox=sandbox,
            live=live,
            report=report,
            dry_run=dry_run,
            delay_seconds=delay_seconds,
            live_checkout_url=live_checkout_url,
        )

    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
        print(f"\nReport written to {output_path}")

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy Paddle Billing sandbox configuration to production "
            "(catalog, discounts, webhooks, account settings)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  PADDLE_SANDBOX_API_KEY   Sandbox API key (must contain _sdbx)
  PADDLE_LIVE_API_KEY      Live/production API key
  PADDLE_PARTNER_ID        Optional partner ID for import_meta writes

Sandbox keys need read permissions for entities being copied. Live keys need
matching write permissions. Create keys at Paddle > Developer tools > Authentication.

Examples:
  python sync_catalog.py --dry-run
  python sync_catalog.py --output go-live-report.json
  python sync_catalog.py --live-checkout-url https://example.com/checkout \\
      --webhook-host-replace ngrok-free.app api.example.com \\
      --output go-live-report.json
  python sync_catalog.py --skip-settings --skip-webhooks
        """,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch sandbox data and print actions without creating anything in production.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived products and prices from sandbox.",
    )
    parser.add_argument(
        "--preserve-import-meta",
        action="store_true",
        help="Keep existing import_meta from sandbox instead of tagging with sandbox Paddle IDs.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to wait between write requests (default: 0.2).",
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        help="Write sandbox -> live ID mapping JSON to this file.",
    )
    parser.add_argument(
        "--mapping",
        metavar="FILE",
        help="Load a previous sandbox -> live ID mapping to skip already-synced entities.",
    )
    parser.add_argument(
        "--partner-id",
        default=os.environ.get("PADDLE_PARTNER_ID"),
        help=(
            "Paddle partner ID for import_meta support "
            "(default: PADDLE_PARTNER_ID env var)."
        ),
    )
    parser.add_argument(
        "--live-checkout-url",
        help=(
            "Production default checkout URL (verified domain). "
            "Required to mirror sandbox default_checkout_url in live."
        ),
    )
    parser.add_argument(
        "--webhook-url-map",
        metavar="FILE",
        help="JSON map of sandbox webhook URLs to production URLs.",
    )
    parser.add_argument(
        "--webhook-host-replace",
        nargs=2,
        metavar=("OLD_HOST", "NEW_HOST"),
        help="Replace webhook hostname when copying URL destinations.",
    )
    parser.add_argument(
        "--skip-catalog",
        action="store_true",
        help="Skip products and prices.",
    )
    parser.add_argument(
        "--skip-discounts",
        action="store_true",
        help="Skip discounts.",
    )
    parser.add_argument(
        "--skip-webhooks",
        action="store_true",
        help="Skip notification destinations.",
    )
    parser.add_argument(
        "--skip-settings",
        action="store_true",
        help="Skip account settings (checkout, payment methods, descriptor).",
    )
    parser.add_argument(
        "--sandbox-key",
        default=os.environ.get("PADDLE_SANDBOX_API_KEY"),
        help="Sandbox API key (default: PADDLE_SANDBOX_API_KEY env var).",
    )
    parser.add_argument(
        "--live-key",
        default=os.environ.get("PADDLE_LIVE_API_KEY"),
        help="Live API key (default: PADDLE_LIVE_API_KEY env var).",
    )
    return parser.parse_args()


def load_webhook_url_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        sys.exit(f"--webhook-url-map must contain a JSON object, got {type(data).__name__}")
    return {str(key): str(value) for key, value in data.items()}


def validate_keys(sandbox_key: str | None, live_key: str | None, dry_run: bool) -> None:
    if not sandbox_key:
        sys.exit("Missing sandbox API key. Set PADDLE_SANDBOX_API_KEY or pass --sandbox-key.")
    if "_sdbx" not in sandbox_key:
        print(
            "Warning: sandbox key does not contain '_sdbx'. "
            "Make sure you are using a sandbox API key.",
            file=sys.stderr,
        )
    if not dry_run and not live_key:
        sys.exit("Missing live API key. Set PADDLE_LIVE_API_KEY or pass --live-key.")
    if live_key and "_sdbx" in live_key:
        sys.exit("Live API key looks like a sandbox key (contains '_sdbx'). Aborting.")


def main() -> None:
    args = parse_args()
    validate_keys(args.sandbox_key, args.live_key, args.dry_run)
    webhook_url_map = load_webhook_url_map(args.webhook_url_map)
    webhook_host_replace = (
        tuple(args.webhook_host_replace) if args.webhook_host_replace else None
    )

    if args.dry_run:
        print("DRY RUN — no changes will be made in production.\n")
    else:
        print("This will update your LIVE Paddle account.\n")

    report = go_live_sync(
        sandbox_key=args.sandbox_key,
        live_key=args.live_key or "",
        partner_id=args.partner_id,
        dry_run=args.dry_run,
        include_archived=args.include_archived,
        preserve_import_meta=args.preserve_import_meta,
        delay_seconds=args.delay,
        output_path=args.output,
        mapping_path=args.mapping,
        live_checkout_url=args.live_checkout_url,
        webhook_url_map=webhook_url_map,
        webhook_host_replace=webhook_host_replace,
        skip_catalog=args.skip_catalog,
        skip_discounts=args.skip_discounts,
        skip_webhooks=args.skip_webhooks,
        skip_settings=args.skip_settings,
    )

    print(
        f"\nDone. Created {len(report['products'])} product(s), "
        f"{len(report['prices'])} price(s), {len(report['discounts'])} discount(s), "
        f"{len(report['notification_settings'])} notification destination(s); "
        f"{len(report['errors'])} error(s), {len(report['warnings'])} warning(s)."
    )
    if report.get("notification_secrets"):
        print(
            "\nNew webhook secrets were created. Save endpoint_secret_key values from "
            "the report — they cannot be retrieved again."
        )

    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
