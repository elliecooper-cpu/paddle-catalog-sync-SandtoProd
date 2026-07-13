# Paddle Go-Live Sync

Copy your [Paddle Billing](https://www.paddle.com/) sandbox configuration to production using the Paddle API.

This is a **community tool**, not an official Paddle product. It implements the workflow described in Paddle's [go-live checklist](https://developer.paddle.com/build/go-live-checklist): recreate catalog entities in live and set up webhooks. Account settings are configured in the dashboard (they are not readable via the public seller API).

## Features

- **Catalog** — products, prices, and discounts (with ID remapping)
- **Webhooks** — notification destinations with URL mapping for production
- **Settings guidance** — dashboard checklist; optional write flags for partner APIs
- **Idempotent** — safe to re-run; skips entities already synced
- **Zero dependencies** — Python 3.10+ standard library only

## Requirements

- Python 3.10 or newer
- Paddle Billing API keys for sandbox and production

### API permissions

| Environment | Typical permissions |
|-------------|---------------------|
| Sandbox | `product.read`, `price.read`, `discount.read`, `notification_setting.read` |
| Production | Above plus `.write` variants for each entity type |

Create keys at **Paddle → Developer tools → Authentication**.

## Installation

**Option A — clone and run**

```bash
git clone https://github.com/YOUR_USERNAME/paddle-go-live-sync.git
cd paddle-go-live-sync
python3 sync_catalog.py --help
```

**Option B — install as a CLI (optional)**

```bash
pip install git+https://github.com/YOUR_USERNAME/paddle-go-live-sync.git
paddle-go-live-sync --help
```

No `pip install` is required if you run `python3 sync_catalog.py` directly.

## Quick start

```bash
export PADDLE_SANDBOX_API_KEY="pdl_sdbx_apikey_..."
export PADDLE_LIVE_API_KEY="pdl_live_apikey_..."

# 1. Preview (recommended)
python3 sync_catalog.py --dry-run

# 2. Sync to production
python3 sync_catalog.py \
  --webhook-host-replace your-ngrok-host.ngrok-free.app api.yourdomain.com \
  --output go-live-report.json
```

Configure live checkout URL, tax mode, payment methods, and statement descriptor in the **Paddle live dashboard** (Checkout settings). Those settings are not available to read from sandbox via the public seller API.

Copy `.env.example` to `.env` for local reference — **do not commit** `.env` files.

## What gets synced

| Phase | Entities | Notes |
|-------|----------|-------|
| Catalog | Products, prices, discounts | Remaps `restrict_to` and cross-references to live IDs |
| Webhooks | Notification destinations | Remaps URLs; saves new `endpoint_secret_key` in the report |
| Settings | Dashboard checklist | No public GET for sandbox settings; optional partner write flags |

## What does not get synced

- Customers, subscriptions, transactions, or payouts
- API keys, client-side tokens, or checkout domain approval
- Balance currency, Retain/dunning, or payout bank details

## CLI reference

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview without writing to production |
| `--output`, `-o` | Save report JSON (IDs, webhook secrets, warnings) |
| `--mapping` | Load a previous report to skip already-synced entities |
| `--live-checkout-url` | Optional partner API write: set live default checkout URL |
| `--default-tax-mode` | Optional partner API write: `location`, `external`, `internal`, or `account_setting` |
| `--statement-descriptor` | Optional partner API write: live statement descriptor name |
| `--webhook-url-map` | JSON file mapping sandbox → live webhook URLs |
| `--webhook-host-replace OLD NEW` | Replace hostname in webhook URLs |
| `--skip-catalog` | Skip products and prices |
| `--skip-discounts` | Skip discounts |
| `--skip-webhooks` | Skip notification destinations |
| `--skip-settings` | Skip settings checklist / optional writes |
| `--include-archived` | Include archived catalog items |
| `--partner-id` | Enable `import_meta` writes (Paddle partners) |

See `examples/webhook-map.example.json` for webhook URL mapping format.

## Webhook secrets

When new notification destinations are created, Paddle returns an `endpoint_secret_key` **once**. The script writes these to your report file. Store them securely and update your server's webhook verification config.

## After syncing

1. Replace sandbox `pro_*`, `pri_*`, and `dsc_*` IDs in your application using the report
2. Configure live webhook secrets from the report
3. Confirm tax categories are approved on your live account
4. Swap client-side tokens and remove `Paddle.Environment.set('sandbox')` from your frontend
5. Complete dashboard-only steps: balance currency, payouts, Retain, domain verification

## Offline simulation

No Paddle credentials needed:

```bash
python3 tests/test_simulation.py
```

This mocks sandbox/live API responses and checks payload building, dry-run behavior,
webhook URL remapping, create flow, and idempotent re-runs.

To share on GitHub:

1. Create a new public repository
2. Update `YOUR_USERNAME` in this README and in `pyproject.toml` `[project.urls]`
3. Push:

```bash
git init
git add .
git commit -m "Initial release: Paddle sandbox to production sync tool"
git remote add origin git@github.com:YOUR_USERNAME/paddle-go-live-sync.git
git push -u origin main
```

## Troubleshooting

| Issue | Likely cause |
|-------|--------------|
| `forbidden` | API key doesn't match the environment (sandbox vs live) |
| Tax category error | Category not approved on live account |
| Webhook skipped | Sandbox URL is localhost — use `--webhook-url-map` or `--webhook-host-replace` |
| Account settings 403 | Expected for seller keys — configure settings in the live dashboard |
| Duplicate discount code | A discount with the same code already exists in live |

## License

MIT — see [LICENSE](LICENSE).

## References

- [Paddle go-live checklist](https://developer.paddle.com/build/go-live-checklist)
- [Paddle API reference](https://developer.paddle.com/api-reference/about)
- [Notification destinations](https://developer.paddle.com/webhooks/about/notification-destinations)
- [Default payment link](https://developer.paddle.com/build/transactions/default-payment-link)
