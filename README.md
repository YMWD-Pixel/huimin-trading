# huimin-trading

Trading bot and tooling for the $315/month survival challenge (Binance USDT futures, Hong Kong VPS).

- `check.py` — read-only Binance futures account check (stdlib only).
- `decrypt.sh` — decrypts the credentials bundle on the VPS.
- `creds.enc` / `pass.enc` — Binance API credentials, AES-256 encrypted; the
  passphrase is RSA-encrypted with a key that only exists on the VPS
  (`~/.deploy_key.pem`). Useless without that private key; the plaintext
  passphrase is never stored here.
