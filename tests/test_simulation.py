#!/usr/bin/env python3
"""Offline simulation of go-live sync (no real Paddle API calls).

Run: python3 tests/test_simulation.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sync_catalog as sc  # noqa: E402


SANDBOX_PRODUCT = {
    "id": "pro_01sandboxproduct000000001",
    "name": "Pro Plan",
    "tax_category": "standard",
    "type": "standard",
    "description": "Pro subscription",
    "image_url": "https://example.com/pro.png",
    "custom_data": {
        "suggested_addons": ["pro_01sandboxaddon00000000002"],
    },
    "status": "active",
    "import_meta": None,
    "prices": [
        {
            "id": "pri_01sandboxprice00000000001",
            "product_id": "pro_01sandboxproduct000000001",
            "description": "Monthly",
            "name": "Monthly",
            "type": "standard",
            "status": "active",
            "billing_cycle": {"interval": "month", "frequency": 1},
            "trial_period": {
                "interval": "day",
                "frequency": 14,
                "requires_payment_method": True,
            },
            "tax_mode": "account_setting",
            "unit_price": {"amount": "1500", "currency_code": "USD"},
            "unit_price_overrides": [
                {
                    "country_codes": ["GB"],
                    "unit_price": {"amount": "1200", "currency_code": "GBP"},
                }
            ],
            "quantity": {"minimum": 1, "maximum": 100},
            "custom_data": None,
            "import_meta": None,
        }
    ],
}

SANDBOX_ADDON = {
    "id": "pro_01sandboxaddon00000000002",
    "name": "Analytics Addon",
    "tax_category": "standard",
    "type": "standard",
    "description": "Addon",
    "image_url": None,
    "custom_data": None,
    "status": "active",
    "import_meta": None,
    "prices": [
        {
            "id": "pri_01sandboxaddonprice0000001",
            "product_id": "pro_01sandboxaddon00000000002",
            "description": "Addon monthly",
            "name": "Monthly",
            "type": "standard",
            "status": "active",
            "billing_cycle": {"interval": "month", "frequency": 1},
            "trial_period": None,
            "tax_mode": "account_setting",
            "unit_price": {"amount": "500", "currency_code": "USD"},
            "unit_price_overrides": [],
            "quantity": {"minimum": 1, "maximum": 10},
            "custom_data": None,
            "import_meta": None,
        }
    ],
}

SANDBOX_DISCOUNT = {
    "id": "dsc_01sandboxdiscount00000001",
    "status": "active",
    "description": "Welcome 20%",
    "enabled_for_checkout": True,
    "code": "WELCOME20",
    "type": "percentage",
    "mode": "standard",
    "amount": "20",
    "currency_code": None,
    "recur": False,
    "maximum_recurring_intervals": None,
    "usage_limit": None,
    "restrict_to": [
        "pro_01sandboxproduct000000001",
        "pri_01sandboxprice00000000001",
    ],
    "expires_at": None,
    "custom_data": None,
    "import_meta": None,
}

SANDBOX_WEBHOOK = {
    "id": "ntfset_01sandboxwebhook000001",
    "description": "Provisioning",
    "type": "url",
    "destination": "https://abc123.ngrok-free.app/webhooks/paddle",
    "active": True,
    "api_version": 1,
    "include_sensitive_fields": False,
    "traffic_source": "platform",
    "subscribed_events": [
        {"name": "subscription.created"},
        {"name": "subscription.updated"},
        "transaction.completed",
    ],
}

SANDBOX_LOCALHOST_WEBHOOK = {
    "id": "ntfset_01sandboxlocalhost00001",
    "description": "Local only",
    "type": "url",
    "destination": "http://localhost:3000/webhooks",
    "active": True,
    "api_version": 1,
    "include_sensitive_fields": False,
    "traffic_source": "simulation",
    "subscribed_events": ["customer.created"],
}


class FakeClient:
    """Minimal stand-in for PaddleClient."""

    def __init__(self, label: str, catalog: list[dict[str, Any]] | None = None):
        self.label = label
        self.catalog = catalog or []
        self.discounts: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self._create_counter = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path))
        # Fail loudly if settings GETs reappear
        if method == "GET" and path.startswith("/settings/"):
            raise AssertionError(f"Unexpected settings GET: {path}")

        if method == "GET" and path == "/products":
            return {"data": list(self.catalog), "meta": {"pagination": {"has_more": False}}}
        if method == "GET" and path == "/discounts":
            if params and params.get("code"):
                code = params["code"]
                matches = [d for d in self.discounts if d.get("code") == code]
                return {"data": matches, "meta": {"pagination": {"has_more": False}}}
            return {
                "data": list(self.discounts),
                "meta": {"pagination": {"has_more": False}},
            }
        if method == "GET" and path == "/notification-settings":
            return {
                "data": list(self.notifications),
                "meta": {"pagination": {"has_more": False}},
            }

        if method == "POST" and path == "/products":
            self._create_counter += 1
            new_id = f"pro_01live{self._create_counter:016d}"
            created = {"id": new_id, "status": "active", "type": "standard", **(body or {})}
            self.catalog.append({**created, "prices": []})
            return {"data": created}
        if method == "POST" and path == "/prices":
            self._create_counter += 1
            new_id = f"pri_01live{self._create_counter:016d}"
            created = {
                "id": new_id,
                "status": "active",
                "type": "standard",
                **(body or {}),
            }
            product_id = (body or {}).get("product_id")
            for product in self.catalog:
                if product.get("id") == product_id:
                    product.setdefault("prices", []).append(created)
                    break
            return {"data": created}
        if method == "POST" and path == "/discounts":
            self._create_counter += 1
            new_id = f"dsc_01live{self._create_counter:016d}"
            created = {"id": new_id, "status": "active", "mode": "standard", **(body or {})}
            self.discounts.append(created)
            return {"data": created}
        if method == "POST" and path == "/notification-settings":
            self._create_counter += 1
            new_id = f"ntfset_01live{self._create_counter:014d}"
            created = {
                "id": new_id,
                "active": True,
                **(body or {}),
                "endpoint_secret_key": f"pdl_ntfset_secret_{self._create_counter}",
            }
            self.notifications.append(created)
            return {"data": created}
        if method == "PATCH" and path.startswith("/products/"):
            return {"data": {"id": path.rsplit("/", 1)[-1], **(body or {})}}
        if method == "PATCH" and path.startswith("/settings/"):
            raise sc.PaddleAPIError(
                403,
                {"error": {"detail": "not authorized"}},
                method,
                path,
            )

        raise AssertionError(f"Unhandled fake request: {method} {path} params={params}")

    def list_all(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return list(self.request("GET", path, params=params).get("data") or [])

    def find_by_external_id(self, resource: str, external_id: str) -> dict[str, Any] | None:
        return None


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_payload_builders() -> None:
    product = sc.build_product_payload(
        SANDBOX_PRODUCT, preserve_import_meta=False, use_import_meta=False
    )
    _assert("import_meta" not in product, "seller mode should omit import_meta")
    _assert(
        product["custom_data"][sc.SANDBOX_ID_KEY] == SANDBOX_PRODUCT["id"],
        "product custom_data should track sandbox id",
    )

    price = sc.build_price_payload(
        SANDBOX_PRODUCT["prices"][0],
        "pro_01livexxxxxxxxxxxxxxxxxx",
        preserve_import_meta=False,
        use_import_meta=False,
    )
    _assert(price["product_id"] == "pro_01livexxxxxxxxxxxxxxxxxx", "price product_id remap")
    _assert("unit_price" in price["trial_period"] or "interval" in price["trial_period"], "trial")
    _assert(
        price["unit_price_overrides"][0]["country_codes"] == ["GB"],
        "overrides sanitized",
    )

    discount = sc.build_discount_payload(
        SANDBOX_DISCOUNT,
        product_id_map={"pro_01sandboxproduct000000001": "pro_01liveaaaaaaaaaaaaaaaa"},
        price_id_map={"pri_01sandboxprice00000000001": "pri_01livebbbbbbbbbbbbbbbb"},
        preserve_import_meta=False,
        use_import_meta=False,
    )
    _assert(
        discount["restrict_to"]
        == ["pro_01liveaaaaaaaaaaaaaaaa", "pri_01livebbbbbbbbbbbbbbbb"],
        f"restrict_to remapped incorrectly: {discount['restrict_to']}",
    )

    notif = sc.build_notification_payload(
        SANDBOX_WEBHOOK, destination="https://api.example.com/webhooks/paddle"
    )
    _assert(
        notif["subscribed_events"]
        == ["subscription.created", "subscription.updated", "transaction.completed"],
        f"events extracted badly: {notif['subscribed_events']}",
    )

    localhost = sc._remap_webhook_url(
        "http://localhost:3000/hooks",
        url_map={},
        host_replace=None,
    )
    _assert(localhost is None, "localhost webhook should be skipped")

    remapped = sc._remap_webhook_url(
        "https://abc123.ngrok-free.app/webhooks/paddle",
        url_map={},
        host_replace=("ngrok-free.app", "api.example.com"),
    )
    _assert(
        remapped == "https://abc123.api.example.com/webhooks/paddle"
        or "api.example.com" in (remapped or ""),
        f"host replace unexpected: {remapped}",
    )


def test_dry_run_simulation() -> None:
    sandbox = FakeClient(
        "sandbox",
        catalog=[json.loads(json.dumps(SANDBOX_PRODUCT)), json.loads(json.dumps(SANDBOX_ADDON))],
    )
    sandbox.discounts = [json.loads(json.dumps(SANDBOX_DISCOUNT))]
    sandbox.notifications = [
        json.loads(json.dumps(SANDBOX_WEBHOOK)),
        json.loads(json.dumps(SANDBOX_LOCALHOST_WEBHOOK)),
    ]
    live = FakeClient("live")

    def fake_ctor(base_url: str, api_key: str, *, partner_id: str | None = None):
        return sandbox if "sandbox" in base_url else live

    with patch.object(sc, "PaddleClient", side_effect=fake_ctor):
        report = sc.go_live_sync(
            sandbox_key="pdl_sdbx_apikey_test",
            live_key="",
            partner_id=None,
            dry_run=True,
            include_archived=False,
            preserve_import_meta=False,
            delay_seconds=0,
            output_path=None,
            mapping_path=None,
            live_checkout_url=None,
            webhook_url_map={},
            webhook_host_replace=("ngrok-free.app", "api.example.com"),
            skip_catalog=False,
            skip_discounts=False,
            skip_webhooks=False,
            skip_settings=False,
            default_tax_mode=None,
            statement_descriptor=None,
        )

    _assert(len(report["products"]) == 2, f"expected 2 products, got {len(report['products'])}")
    _assert(len(report["prices"]) == 2, f"expected 2 prices, got {len(report['prices'])}")
    _assert(len(report["discounts"]) == 1, "expected 1 discount")
    _assert(len(report["notification_settings"]) == 1, "localhost webhook should be skipped")
    _assert(len(report["errors"]) == 0, f"unexpected errors: {report['errors']}")
    _assert(
        any("settings" in w.lower() or "dashboard" in w.lower() for w in report["warnings"]),
        f"expected settings warning, got {report['warnings']}",
    )
    _assert(
        not any(method == "GET" and path.startswith("/settings/") for method, path in live.calls),
        f"live must not GET settings: {live.calls}",
    )
    _assert(
        not any(method == "POST" for method, _ in live.calls),
        f"dry-run must not POST to live: {live.calls}",
    )


def test_live_create_simulation() -> None:
    sandbox = FakeClient(
        "sandbox",
        catalog=[json.loads(json.dumps(SANDBOX_PRODUCT)), json.loads(json.dumps(SANDBOX_ADDON))],
    )
    sandbox.discounts = [json.loads(json.dumps(SANDBOX_DISCOUNT))]
    sandbox.notifications = [json.loads(json.dumps(SANDBOX_WEBHOOK))]
    live = FakeClient("live")

    def fake_ctor(base_url: str, api_key: str, *, partner_id: str | None = None):
        return sandbox if "sandbox" in base_url else live

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        with patch.object(sc, "PaddleClient", side_effect=fake_ctor):
            report = sc.go_live_sync(
                sandbox_key="pdl_sdbx_apikey_test",
                live_key="pdl_live_apikey_test",
                partner_id=None,
                dry_run=False,
                include_archived=False,
                preserve_import_meta=False,
                delay_seconds=0,
                output_path=str(out),
                mapping_path=None,
                live_checkout_url=None,
                webhook_url_map={
                    "https://abc123.ngrok-free.app/webhooks/paddle": (
                        "https://api.example.com/webhooks/paddle"
                    )
                },
                webhook_host_replace=None,
                skip_catalog=False,
                skip_discounts=False,
                skip_webhooks=False,
                skip_settings=True,
                default_tax_mode=None,
                statement_descriptor=None,
            )
        saved = json.loads(out.read_text())

    _assert(len(report["errors"]) == 0, f"errors: {report['errors']}")
    _assert(len(report["products"]) == 2, "two products created")
    _assert(len(report["prices"]) == 2, "two prices created")
    _assert(len(report["discounts"]) == 1, "one discount created")
    _assert(len(report["notification_settings"]) == 1, "one webhook created")
    _assert(report["notification_secrets"], "webhook secret should be captured")
    _assert(saved["product_id_map"], "report should include product map")

    # Idempotent re-run should skip everything
    with patch.object(sc, "PaddleClient", side_effect=fake_ctor):
        second = sc.go_live_sync(
            sandbox_key="pdl_sdbx_apikey_test",
            live_key="pdl_live_apikey_test",
            partner_id=None,
            dry_run=False,
            include_archived=False,
            preserve_import_meta=False,
            delay_seconds=0,
            output_path=None,
            mapping_path=None,
            live_checkout_url=None,
            webhook_url_map={
                "https://abc123.ngrok-free.app/webhooks/paddle": (
                    "https://api.example.com/webhooks/paddle"
                )
            },
            webhook_host_replace=None,
            skip_catalog=False,
            skip_discounts=False,
            skip_webhooks=False,
            skip_settings=True,
            default_tax_mode=None,
            statement_descriptor=None,
        )
    _assert(len(second["products"]) == 0, "second run should create no products")
    _assert(len(second["prices"]) == 0, "second run should create no prices")
    _assert(len(second["skipped_products"]) == 2, "products should be skipped")
    _assert(len(second["skipped_prices"]) == 2, "prices should be skipped")
    _assert(len(second["errors"]) == 0, f"second run errors: {second['errors']}")


def main() -> None:
    tests = [
        ("payload builders", test_payload_builders),
        ("dry-run simulation", test_dry_run_simulation),
        ("live create + idempotent re-run", test_live_create_simulation),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - show all simulation failures
            failures += 1
            print(f"FAIL  {name}: {exc}")
    if failures:
        print(f"\n{failures} simulation(s) failed")
        sys.exit(1)
    print("\nAll simulations passed.")


if __name__ == "__main__":
    main()
