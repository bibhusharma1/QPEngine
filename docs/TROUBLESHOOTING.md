# Troubleshooting

- CORS: ensure origin matches allowlist or set `QUES_CORS_ORIGINS`
- TOTP: secret persists permanently; ON/OFF toggles verification only
- NVIDIA keys: check rotation/fallback, auth errors are not retried endlessly
- Docker GPU: requires NVIDIA runtime for full profile
