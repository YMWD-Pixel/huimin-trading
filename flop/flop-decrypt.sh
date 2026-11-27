#!/bin/sh
# Decrypt the Flop key bundle on the VPS. Run from the directory containing
# flop-keys.enc and flop-pass.enc. Requires ~/.deploy_key.pem (private key,
# only exists on the VPS). The plaintext passphrase is never stored.
set -e
mkdir -p ~/flop
openssl pkeyutl -decrypt -inkey ~/.deploy_key.pem -in flop-pass.enc -out /tmp/flop-deploy-pass.txt
openssl enc -d -aes-256-cbc -salt -pbkdf2 -in flop-keys.enc -out /tmp/flop-keys.tar.gz -pass file:/tmp/flop-deploy-pass.txt
tar xzf /tmp/flop-keys.tar.gz -C ~/flop
chmod 600 ~/flop/keypair.json ~/flop/keystore.json ~/flop/keystore.pw
rm -f /tmp/flop-deploy-pass.txt /tmp/flop-keys.tar.gz
echo "flop keys decrypted to ~/flop/"
ls -l ~/flop/
