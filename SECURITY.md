# Security

## API keys and webhook secrets

- Never commit `PADDLE_SANDBOX_API_KEY`, `PADDLE_LIVE_API_KEY`, or output report files.
- Report JSON may contain `endpoint_secret_key` values. Treat them like passwords.
- Use environment variables or a secrets manager — not committed `.env` files.

## Live environment

This tool writes to your **production** Paddle account when run without `--dry-run`.
Always run `--dry-run` first and review the output before syncing live.

## Reporting issues

If you discover a security issue, please open a private report with the repository
maintainer rather than filing a public issue with sensitive details.
