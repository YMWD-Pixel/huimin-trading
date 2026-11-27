// Daily Flop Testnet faucet claim for agent 慧敏.
// Official flow (https://flop.network/llms.txt, "Get Testnet FLOP"):
//   1. GET /chain must report network=testnet + the expected genesis hash.
//   2. GET /limits must show faucet.status=open + agent.claimPath=/v1/testnet/agent-claims.
//   3. Sign UTF-8 `flop-faucet-agent/v1|<genesis>|<address>|<nonce>` with the wallet's
//      own sr25519 key; nonce = canonical decimal (ms clock), above the previous one.
//   4. POST /v1/testnet/agent-claims {address, nonce, signature: "0x<hex>"}.
//   5. Keep txHash; a grant counts as spendable only once in a FINALIZED block.
// Never retry a refusal in a loop. `agent_spend_missing` means the next grant needs
// a finalized spend of the last one (rolloverRequiresSpend=true in /limits) — that is
// a blocked state, not an error to hammer. The faucet never asks for a seed/deposit.
// Run: node claim.mjs   (logs one JSON line per attempt to claims.log, updates state.json)
import { cryptoWaitReady, sr25519PairFromSeed, sr25519Sign } from '@polkadot/util-crypto';
import { u8aToHex, stringToU8a, hexToU8a } from '@polkadot/util';
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const KEYPAIR_FILE = path.join(dir, 'keypair.json');
const STATE_FILE = path.join(dir, 'state.json');
const LOG_FILE = path.join(dir, 'claims.log');
const FAUCET = 'https://faucet.flop.network';
const RPC_WSS = 'wss://testnet.flop.network';
const EXPECTED_GENESIS = '0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800';
const GRANT_FLOP = 10n;
const PLANCK = 10n ** 18n;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function httpsJson(method, url, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const data = body ? JSON.stringify(body) : null;
    const req = https.request(
      {
        hostname: u.hostname,
        path: u.pathname + u.search,
        method,
        headers: data
          ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) }
          : { accept: 'application/json' },
        timeout: 20000,
      },
      (res) => {
        let raw = '';
        res.on('data', (c) => (raw += c));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, headers: res.headers, json: raw ? JSON.parse(raw) : null });
          } catch (e) {
            reject(new Error(`bad JSON from ${method} ${url}: ${raw.slice(0, 200)}`));
          }
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error(`timeout ${method} ${url}`)));
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

function logLine(obj) {
  fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...obj }) + '\n');
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return { lastNonce: 0, totalClaimedFlop: 0, claims: [] };
  }
}

async function freeBalanceAtFinalized(api, address) {
  const finalized = await api.rpc.chain.getFinalizedHead();
  const acct = await api.query.system.account.at(finalized, address);
  return acct.data.free.toBigInt();
}

async function main() {
  await cryptoWaitReady();
  const kp = JSON.parse(fs.readFileSync(KEYPAIR_FILE, 'utf8'));
  if (kp.network !== 'flop-testnet' || kp.scheme !== 'sr25519') throw new Error('unexpected keypair file');
  const pair = sr25519PairFromSeed(hexToU8a(kp.seedHex));
  const address = kp.address;

  // 1. Verify the faucet serves Testnet with the expected genesis.
  const chain = await httpsJson('GET', `${FAUCET}/chain`);
  if (chain.json?.network !== 'testnet' || chain.json?.genesisHash !== EXPECTED_GENESIS) {
    throw new Error(`faucet chain mismatch: ${JSON.stringify(chain.json)}`);
  }

  // 2. Verify grants are being served.
  const limits = await httpsJson('GET', `${FAUCET}/limits`);
  const f = limits.json?.faucet;
  if (f?.status !== 'open' || f?.agent?.claimPath !== '/v1/testnet/agent-claims') {
    const rec = { address, outcome: 'faucet_not_open', limits: limits.json?.faucet ?? null };
    logLine(rec);
    console.log(JSON.stringify(rec, null, 2));
    return;
  }
  const agentLane = f.agent;
  console.log(`faucet open; agent grant=${agentLane.grantFlop} FLOP, rolloverRequiresSpend=${agentLane.rolloverRequiresSpend}, faucet balance=${f.balanceFlop} FLOP`);

  // 3. Nonce: ms clock, strictly above the previous one.
  const state = loadState();
  const nonce = String(Math.max(Date.now(), (state.lastNonce || 0) + 1));

  // 4. Sign the claim message with the wallet's own key.
  const message = `flop-faucet-agent/v1|${EXPECTED_GENESIS}|${address}|${nonce}`;
  const signature = u8aToHex(sr25519Sign(stringToU8a(message), pair));

  // 5. POST the signed claim. (node:https sends no Sec-Fetch-* headers.)
  const res = await httpsJson('POST', `${FAUCET}/v1/testnet/agent-claims`, { address, nonce, signature });
  const body = res.json ?? {};
  if (res.status >= 400 || body.error || body.reason) {
    const rec = {
      address, nonce,
      outcome: 'refused',
      httpStatus: res.status,
      error: body.error ?? null,
      reason: body.reason ?? null,
      action: body.action ?? null,
      retryAfterSeconds: body.retryAfterSeconds ?? null,
      rolloverRequiresSpend: agentLane.rolloverRequiresSpend,
    };
    logLine(rec);
    console.log(JSON.stringify(rec, null, 2));
    if (body.reason === 'agent_spend_missing') {
      console.log('BLOCKED: next grant needs a finalized spend of the last one. Spend requires Testnet inference, which is not open yet. Do NOT loop-retry.');
    }
    return; // known refusal: log and stop, exit 0
  }

  const txHash = body.txHash ?? body.tx_hash ?? null;
  const rec = { address, nonce, outcome: 'submitted', httpStatus: res.status, txHash, response: body };
  console.log(`claim submitted; txHash=${txHash}`);

  // 6. Verify the grant lands in a FINALIZED block (balance check at finalized head).
  let verified = false;
  let balanceAfter = null;
  try {
    const { ApiPromise, WsProvider } = await import('@polkadot/api');
    const api = await ApiPromise.create({ provider: new WsProvider(RPC_WSS) });
    try {
      const before = await freeBalanceAtFinalized(api, address);
      const deadline = Date.now() + 180000;
      while (Date.now() < deadline) {
        const after = await freeBalanceAtFinalized(api, address);
        if (after >= before + GRANT_FLOP * PLANCK) {
          verified = true;
          balanceAfter = after.toString();
          break;
        }
        await sleep(5000);
      }
      if (!verified) balanceAfter = (await freeBalanceAtFinalized(api, address)).toString();
    } finally {
      await api.disconnect();
    }
  } catch (e) {
    console.log(`balance verification unavailable: ${e.message}`);
  }
  rec.verifiedFinalized = verified;
  rec.balanceAfterPlanck = balanceAfter;
  state.lastNonce = Number(nonce);
  state.totalClaimedFlop = Number(state.totalClaimedFlop || 0) + (verified ? 10 : 0);
  state.lastClaim = { nonce, txHash, verifiedFinalized: verified, at: new Date().toISOString() };
  state.claims.push(state.lastClaim);
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2) + '\n');
  logLine(rec);
  console.log(JSON.stringify(rec, null, 2));
  if (!verified) console.log('NOTE: grant submitted but NOT confirmed in a finalized block yet — reconcile with the txHash before re-claiming; do not spend it yet.');
}

main().catch((e) => {
  console.error('ERROR:', e.message);
  try { logLine({ outcome: 'error', error: e.message }); } catch {}
  process.exit(1);
});
