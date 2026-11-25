#!/bin/sh
# Decrypt the Binance credentials bundle on the VPS. Run from the directory
# containing creds.enc and pass.enc. Requires ~/.deploy_key.pem (private).
set -e
mkdir -p ~/.binance
openssl pkeyutl -decrypt -inkey ~/.deploy_key.pem -in pass.enc -out /tmp/deploy-pass.txt
openssl enc -d -aes-256-cbc -salt -pbkdf2 -in creds.enc -out ~/.binance/credentials -pass file:/tmp/deploy-pass.txt
chmod 600 ~/.binance/credentials
rm -f /tmp/deploy-pass.txt
echo "credentials decrypted to ~/.binance/credentials"
