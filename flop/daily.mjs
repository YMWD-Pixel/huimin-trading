// Daily Flop Testnet flow for agent 慧敏: claim once per JST day, then immediately
// spend the grant on Testnet inference (direct session) and verify it settled.
// Official sources of truth, re-read every run:
//   https://flop.network/llms.txt  (claim path, inference status, refusal actions)
//   https://faucet.flop.network/chain  +  /limits
//   Testnet RPC wss://testnet.flop.network / https://testnet.flop.network
//
// Identity: the EXISTING sr25519 wallet, address
// 5DXevwgi12ao1szL2Y9WHvSbDogZS4AcYzVrhKCZ6wCPUmdR (keypair.json, mode 600).
// NEVER create a new wallet, never --new-wallet, never switch identity on a 503.
// The claim itself is still the manual sign+POST from claim.mjs (same message
// format, same nonce rule); the CLI has no verified testnet claim path in use
// here. Spending uses the official CLI (`flop session run`) with the encrypted
// keystore of the SAME key (keystore.json + keystore.pw, both mode 600).
//
// Iron rules (from llms.txt + YM):
// - Testnet only. Never devnet, never mainnet, never localhost/Devnet gateway.
// - Never retry a refusal in a loop. retry_later -> stop, next 2h run retries.
//   agent_spend_missing -> spend first (if inference open), then ONE re-claim.
//   report_to_operator -> stop and flag for the human.
// - A timed-out claim POST is reconciled by repeating the SAME nonce (the faucet
//   returns the same tx and never pays twice); never re-request with a new nonce.
// - One claim per JST day, tracked in state.json (lastClaimJstDate).
// - Never print seeds, mnemonics, passwords or keystore contents.
//
// Run: node daily.mjs   (prints one JSON summary to stdout; appends JSON lines
// to claims.log; updates state.json)
import { cryptoWaitReady, sr25519PairFromSeed, sr25519Sign } from '@polkadot/util-crypto';
import { u8aToHex, stringToU8a, hexToU8a } from '@polkadot/util';
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const KEYPAIR_FILE = path.join(dir, 'keypair.json');
const KEYSTORE_FILE = path.join(dir, 'keystore.json');
const KEYSTORE_PW_FILE = path.join(dir, 'keystore.pw');
const STATE_FILE = path.join(dir, 'state.json');
const LOG_FILE = path.join(dir, 'claims.log');
const FAUCET = 'https://faucet.flop.network';
const RPC_WSS = 'wss://testnet.flop.network';
const RPC_HTTP = 'https://testnet.flop.network';
const LLMS_TXT = 'https://flop.network/llms.txt';
const EXPLORER = 'https://testnet.flopscan.io';
const EXPECTED_GENESIS = '0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800';
const GRANT_FLOP = 10n;
const PLANCK = 10n ** 18n;
const SPEND_MAX_FLOP = 1; // minimal direct session: prompt "hi", settle the channel
const JST_OFFSET_MS = 9 * 3600 * 1000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jstDate = (t = Date.now()) => new Date(t + JST_OFFSET_MS).toISOString().slice(0, 10);
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex').slice(0, 16);

function httpsJson(method, url, body, timeoutMs = 20000) {
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
        timeout: timeoutMs,
      },
      (res) => {
        let raw = '';
        res.on('data', (c) => (raw += c));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, headers: res.headers, json: raw ? JSON.parse(raw) : null });
          } catch {
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

function httpsText(url, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: timeoutMs }, (res) => {
      let raw = '';
      res.on('data', (c) => (raw += c));
      res.on('end', () => resolve({ status: res.statusCode, text: raw }));
    });
    req.on('timeout', () => req.destroy(new Error(`timeout GET ${url}`)));
    req.on('error', reject);
  });
}

async function rpcCall(method, params) {
  const res = await httpsJson('POST', RPC_HTTP, { jsonrpc: '2.0', id: 1, method, params });
  if (res.json?.error) throw new Error(`RPC ${method}: ${JSON.stringify(res.json.error)}`);
  return res.json?.result;
}

function execCli(args, envExtra = {}, timeoutMs = 120000) {
  return new Promise((resolve) => {
    execFile('flop', args, { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024, env: { ...process.env, ...envExtra } }, (err, stdout, stderr) => {
      resolve({ code: err ? (err.code ?? 1) : 0, stdout: stdout ?? '', stderr: stderr ?? '', timedOut: !!err && err.killed });
    });
  });
}

// CLI prints @polkadot duplicate-version warnings to stderr (suppressed by
// ignoring stderr) but be defensive: slice stdout from the first { or [.
function parseCliJson(stdout) {
  const i = Math.min(
    ...['{', '['].map((c) => {
      const j = stdout.indexOf(c);
      return j === -1 ? Infinity : j;
    }),
  );
  if (!Number.isFinite(i)) throw new Error(`no JSON in CLI output: ${stdout.slice(0, 200)}`);
  return JSON.parse(stdout.slice(i));
}

function logLine(obj) {
  fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...obj }) + '\n');
}

function defaultState() {
  return {
    lastNonce: 0,
    totalClaimedFlop: 0,
    claims: [],
    spends: [],
    lastClaimJstDate: null,
    lastGrant: null,
    pendingNonce: null,
    refusalDays: [],
    refusalStreakNotified: false,
    lastNotifiedClaimJstDate: null,
    inferenceOpen: false,
    scoringAnnounced: false,
    lastFingerprint: null,
    seenErrors: [],
    cliVersion: null,
    sdkVersion: null,
  };
}

function loadState() {
  try {
    return { ...defaultState(), ...JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')) };
  } catch {
    return defaultState();
  }
}

function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2) + '\n');
}

function recordRefusalDay(state, date) {
  if (!state.refusalDays.includes(date)) {
    state.refusalDays.push(date);
    state.refusalDays = state.refusalDays.slice(-10);
  }
}

function clearRefusalStreak(state) {
  state.refusalDays = [];
  state.refusalStreakNotified = false;
}

// Find a key anywhere in a nested object (session JSON shape may vary).
function findKey(obj, wanted, depth = 0) {
  if (!obj || typeof obj !== 'object' || depth > 6) return undefined;
  for (const [k, v] of Object.entries(obj)) {
    if (wanted.includes(k.toLowerCase())) return v;
    const inner = findKey(v, wanted, depth + 1);
    if (inner !== undefined) return inner;
  }
  return undefined;
}

// ---- chain helpers (lazy @polkadot/api, only when a balance check is needed) ----
let apiPromise = null;
async function getApi() {
  if (!apiPromise) {
    const { ApiPromise, WsProvider } = await import('@polkadot/api');
    apiPromise = await ApiPromise.create({ provider: new WsProvider(RPC_WSS) });
  }
  return apiPromise;
}
async function closeApi() {
  if (apiPromise) {
    try {
      await apiPromise.disconnect();
    } catch {}
    apiPromise = null;
  }
}
async function freeBalanceAtFinalized(api, address) {
  const finalized = await api.rpc.chain.getFinalizedHead();
  const acct = await api.query.system.account.at(finalized, address);
  return acct.data.free.toBigInt();
}

// ---- claim: one signed POST; timeout is reconciled with the SAME nonce ----
async function postClaim(address, nonce, signature) {
  return httpsJson('POST', `${FAUCET}/v1/testnet/agent-claims`, { address, nonce, signature }, 25000);
}

async function claimOnce(state, address, pair, agentLane) {
  const nonce = state.pendingNonce ?? String(Math.max(Date.now(), (state.lastNonce || 0) + 1));
  const message = `flop-faucet-agent/v1|${EXPECTED_GENESIS}|${address}|${nonce}`;
  const signature = u8aToHex(sr25519Sign(stringToU8a(message), pair));
  let res;
  try {
    res = await postClaim(address, nonce, signature);
  } catch (e) {
    if (/^timeout/.test(e.message)) {
      // Official idempotency: repeat the SAME nonce; it returns the same tx
      // and never pays twice. Never mint a new nonce after a timeout.
      logLine({ address, nonce, outcome: 'claim_timeout_reconciling' });
      try {
        res = await postClaim(address, nonce, signature);
      } catch (e2) {
        state.pendingNonce = nonce;
        return { outcome: 'unknown', nonce, error: `claim POST timed out twice: ${e2.message}` };
      }
    } else {
      throw e;
    }
  }
  const body = res.json ?? {};
  if (res.status >= 400 || body.error || body.reason) {
    return {
      outcome: 'refused',
      nonce,
      httpStatus: res.status,
      error: body.error ?? null,
      reason: body.reason ?? null,
      action: body.action ?? null,
      retryAfterSeconds: body.retryAfterSeconds ?? null,
    };
  }
  const txHash = body.txHash ?? body.tx_hash ?? null;
  // Verify the grant lands in a FINALIZED block (balance check at finalized head).
  let verified = false;
  let balanceAfter = null;
  try {
    const api = await getApi();
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
  } catch (e) {
    logLine({ address, nonce, outcome: 'balance_verify_unavailable', error: e.message });
  }
  const claim = { nonce, txHash, verifiedFinalized: verified, at: new Date().toISOString(), jstDate: jstDate() };
  state.pendingNonce = null;
  state.lastNonce = Number(nonce);
  state.claims.push(claim);
  if (verified) {
    state.totalClaimedFlop = Number(state.totalClaimedFlop || 0) + Number(GRANT_FLOP);
    state.lastClaimJstDate = claim.jstDate;
    state.lastGrant = { ...claim, amountFlop: Number(GRANT_FLOP), spent: false, spendTxHash: null, settled: false };
    clearRefusalStreak(state);
  }
  return { outcome: verified ? 'claimed' : 'submitted_unverified', nonce, txHash, verifiedFinalized: verified, balanceAfterPlanck: balanceAfter };
}

// ---- spend: direct session on testnet, then verify it settled ----
async function flopscanTxCheck(txHash) {
  // Best effort: flopscan is a Next.js app; the tx detail path is not
  // documented. Try the likely patterns; an inconclusive result never blocks
  // settlement (session JSON + on-chain channel list are the authority).
  for (const p of [`/transactions/${txHash}`, `/extrinsic/${txHash}`]) {
    try {
      const r = await httpsText(`${EXPLORER}${p}`, 15000);
      if (r.status === 200 && r.text.includes(txHash)) return { flopscan: 'found', path: p };
    } catch {}
  }
  return { flopscan: 'inconclusive' };
}

async function spendOnce(state, address) {
  const keystorePw = fs.readFileSync(KEYSTORE_PW_FILE, 'utf8').trim();
  const baseArgs = ['--network', 'testnet', '--keystore', KEYSTORE_FILE, '--password-env', 'FLOP_KEYSTORE_PW'];
  const env = { FLOP_KEYSTORE_PW: keystorePw };
  // 1. Models registered on-chain?
  const models = await execCli([...baseArgs, 'model', 'list', '--json'], env);
  if (models.code !== 0) return { outcome: 'spend_error', error: `model list failed (code ${models.code}): ${models.stderr.slice(-300)}` };
  let modelList;
  try {
    modelList = parseCliJson(models.stdout);
  } catch (e) {
    return { outcome: 'spend_error', error: `model list unparseable: ${e.message}` };
  }
  const modelsArr = modelList.models ?? [];
  if (!modelsArr.length) return { outcome: 'inference_not_open', detail: 'no registered testnet models' };
  // Smallest model first (cheapest minimal session).
  modelsArr.sort((a, b) => (a.parameters_billion ?? 99) - (b.parameters_billion ?? 99));
  const model = modelsArr[0];
  // 2. Any miner SKUs serving it?
  const skus = await execCli([...baseArgs, 'miner', 'skus', '--json'], env);
  if (skus.code !== 0) return { outcome: 'spend_error', error: `miner skus failed (code ${skus.code}): ${skus.stderr.slice(-300)}` };
  let skuList;
  try {
    skuList = parseCliJson(skus.stdout);
  } catch (e) {
    return { outcome: 'spend_error', error: `miner skus unparseable: ${e.message}` };
  }
  const skuArr = skuList.skus ?? [];
  if (!skuArr.length) return { outcome: 'inference_not_open', detail: 'models registered but zero miner SKUs' };
  const sku = skuArr.find((s) => (s.model === model.hash || s.model === model.name || s.modelHash === model.hash)) ?? skuArr[0];
  const miner = sku.miner ?? sku.address ?? sku.account ?? sku.minerAddress;
  if (!miner) return { outcome: 'spend_error', error: `miner SKU shape unknown: ${JSON.stringify(sku).slice(0, 300)}` };
  // 3. Direct session, minimal spend, settle the channel.
  const run = await execCli(
    [...baseArgs, 'session', 'run', '--miner', miner, '--model', model.hash, '--prompt', 'hi', '--max-flop', String(SPEND_MAX_FLOP), '--via', 'auto', '--json'],
    env,
    300000,
  );
  if (run.code !== 0) return { outcome: 'spend_error', error: `session run failed (code ${run.code}): ${run.stderr.slice(-500)}` };
  let sessionJson;
  try {
    sessionJson = parseCliJson(run.stdout);
  } catch (e) {
    return { outcome: 'spend_error', error: `session run output unparseable: ${e.message}` };
  }
  const minerPaid = findKey(sessionJson, ['minerpaid', 'miner_paid']);
  const settleTx = findKey(sessionJson, ['settlementtx', 'settlement_tx', 'settletxhash', 'settle_tx_hash', 'txhash', 'tx_hash']);
  // 4. Cross-check: on-chain channel list (closed) for this agent.
  let channelPaid = null;
  let channelClosed = false;
  const ch = await execCli([...baseArgs, 'channel', 'list', '--agent', address, '--include-closed', '--json'], env);
  if (ch.code === 0) {
    try {
      const channels = parseCliJson(ch.stdout).channels ?? [];
      const mine = channels.filter((c) => (c.miner ?? c.minerAddress) === miner);
      channelClosed = mine.length > 0;
      channelPaid = mine.map((c) => c.paid ?? c.minerPaid ?? c.settled ?? null).find((v) => v != null) ?? null;
    } catch {}
  }
  const settled = minerPaid != null && Number(minerPaid) > 0 && (channelClosed || settleTx != null);
  let scan = { flopscan: 'skipped' };
  if (settleTx) scan = await flopscanTxCheck(String(settleTx));
  const spend = {
    at: new Date().toISOString(),
    jstDate: jstDate(),
    miner,
    model: model.name,
    modelHash: model.hash,
    maxFlop: SPEND_MAX_FLOP,
    minerPaid: minerPaid ?? null,
    settleTxHash: settleTx ?? null,
    channelClosed,
    channelPaid,
    ...scan,
    settled,
    session: sessionJson,
  };
  state.spends.push(spend);
  if (settled && state.lastGrant) {
    state.lastGrant.spent = true;
    state.lastGrant.spendTxHash = settleTx ?? null;
    state.lastGrant.settled = true;
  }
  return { outcome: settled ? 'spent_settled' : 'spend_unconfirmed', spend };
}

// ---- main daily flow ----
async function main() {
  await cryptoWaitReady();
  const summary = { runJstDate: jstDate(), notify: false, notifyReason: null };
  const state = loadState();
  const kp = JSON.parse(fs.readFileSync(KEYPAIR_FILE, 'utf8'));
  if (kp.network !== 'flop-testnet' || kp.scheme !== 'sr25519') throw new Error('unexpected keypair file');
  const pair = sr25519PairFromSeed(hexToU8a(kp.seedHex));
  const address = kp.address;
  summary.address = address;

  // CLI / SDK versions (llms.txt: CLI >= 0.8.3, SDK >= 0.11.0 pin testnet genesis).
  try {
    const v = await execCli(['--version'], {}, 30000);
    state.cliVersion = v.stdout.trim().split('\n').pop()?.trim() ?? null;
  } catch {}
  try {
    const sdkPkg = JSON.parse(fs.readFileSync('/usr/lib/node_modules/@flop-labs/sdk/package.json', 'utf8'));
    state.sdkVersion = sdkPkg.version;
  } catch {}
  summary.cliVersion = state.cliVersion;
  summary.sdkVersion = state.sdkVersion;
  if (state.cliVersion && state.cliVersion < '0.8.3') throw new Error(`CLI ${state.cliVersion} too old for testnet genesis pinning`);

  // 1. Verify the faucet serves Testnet with the expected genesis (faucet + RPC).
  const chain = await httpsJson('GET', `${FAUCET}/chain`);
  if (chain.json?.network !== 'testnet' || chain.json?.genesisHash !== EXPECTED_GENESIS) {
    throw new Error(`faucet chain mismatch: ${JSON.stringify(chain.json)}`);
  }
  const rpcGenesis = await rpcCall('chain_getBlockHash', [0]);
  if (rpcGenesis !== EXPECTED_GENESIS) throw new Error(`RPC genesis mismatch: ${rpcGenesis}`);
  summary.chainOk = true;
  summary.technocore = { room: chain.json?.technocoreBot?.room ?? null, botDid: chain.json?.technocoreBot?.did ?? null };

  // 2. Verify grants are being served.
  const limits = await httpsJson('GET', `${FAUCET}/limits`);
  const f = limits.json?.faucet;
  if (!f || f.status !== 'open' || f.agent?.claimPath !== '/v1/testnet/agent-claims') {
    const rec = { address, outcome: 'faucet_not_open', limits: f ?? null };
    logLine(rec);
    console.log(JSON.stringify({ ...summary, ...rec, notify: true, notifyReason: 'faucet_closed_or_changed' }, null, 2));
    saveState(state);
    return;
  }
  const agentLane = f.agent;
  const balanceFlop = Number(f.balanceFlop);
  const floorFlop = Number(f.minBalanceFlop);
  summary.faucet = { status: f.status, balanceFlop, floorFlop, grantFlop: agentLane.grantFlop, rolloverRequiresSpend: agentLane.rolloverRequiresSpend };

  // 3. Re-read llms.txt: inference open? scoring announced?
  let llms = '';
  try {
    const r = await httpsText(LLMS_TXT);
    llms = r.text;
  } catch (e) {
    logLine({ address, outcome: 'llms_unreadable', error: e.message });
  }
  const llmsSaysNoGateway = /no Testnet gateway/i.test(llms);
  const llmsSaysNoModels = /no registered Testnet model or miner/i.test(llms);
  const scoringAnnounced = /activation block/i.test(llms) && !/no block or date is announced yet/i.test(llms);
  const inferenceOpen = !llmsSaysNoGateway && !llmsSaysNoModels;
  if (scoringAnnounced && !state.scoringAnnounced) {
    state.scoringAnnounced = true;
    summary.notify = true;
    summary.notifyReason = 'scoring_phase_announced';
  }
  if (inferenceOpen && !state.inferenceOpen) {
    summary.notify = true;
    summary.notifyReason = summary.notifyReason ?? 'inference_open';
  }
  state.inferenceOpen = inferenceOpen;
  summary.inferenceOpen = inferenceOpen;
  summary.llmsSha = sha(llms);

  // 4. Rule/gateway fingerprint change (excluding routine balance drift).
  const fingerprint = JSON.stringify({
    status: f.status,
    floorFlop,
    grantFlop: agentLane.grantFlop,
    cooldownSeconds: agentLane.cooldownSeconds,
    rolloverRequiresSpend: agentLane.rolloverRequiresSpend,
    claimPath: agentLane.claimPath,
    inferenceOpen,
    room: chain.json?.technocoreBot?.room ?? null,
  });
  if (state.lastFingerprint && state.lastFingerprint !== fingerprint && !summary.notify) {
    summary.notify = true;
    summary.notifyReason = 'rules_changed';
  }
  state.lastFingerprint = fingerprint;

  const today = jstDate();
  const hasUnspentGrant = !!(state.lastGrant && state.lastGrant.verifiedFinalized && !state.lastGrant.spent);

  // 5. Spend first if an unspent grant is waiting (rollover requires a spend).
  let spendResult = null;
  if (hasUnspentGrant) {
    if (!inferenceOpen) {
      summary.spend = { outcome: 'blocked_spend_missing_inference_closed' };
      logLine({ address, outcome: 'blocked_spend_missing_inference_closed', grant: state.lastGrant });
    } else {
      spendResult = await spendOnce(state, address);
      summary.spend = spendResult.outcome === 'spent_settled' ? { outcome: spendResult.outcome, minerPaid: spendResult.spend.minerPaid, settleTxHash: spendResult.spend.settleTxHash } : spendResult;
      logLine({ address, ...summary.spend });
    }
  }

  // 6. Claim (at most once per JST day; skip if an unspent grant still waits).
  let claimResult = null;
  const claimedToday = state.lastClaimJstDate === today;
  const unspentStillWaiting = !!(state.lastGrant && state.lastGrant.verifiedFinalized && !state.lastGrant.spent);
  if (!claimedToday && !unspentStillWaiting) {
    if (balanceFlop - floorFlop < Number(agentLane.grantFlop)) {
      claimResult = { outcome: 'skipped_low_balance', balanceFlop, floorFlop };
      recordRefusalDay(state, today);
      logLine({ address, ...claimResult });
    } else {
      claimResult = await claimOnce(state, address, pair, agentLane);
      summary.claim = claimResult;
      logLine({ address, ...claimResult });
      if (claimResult.outcome === 'refused') {
        recordRefusalDay(state, today);
        if (claimResult.action === 'report_to_operator') {
          summary.notify = true;
          summary.notifyReason = 'report_to_operator';
        } else if (claimResult.reason === 'agent_spend_missing' && spendResult?.outcome === 'spent_settled') {
          // Spend just settled: one re-claim, then stop.
          claimResult = await claimOnce(state, address, pair, agentLane);
          summary.claim = claimResult;
          logLine({ address, ...claimResult, retriedAfterSpend: true });
          if (claimResult.outcome === 'refused') recordRefusalDay(state, today);
        }
      } else if (claimResult.outcome === 'claimed' && state.lastNotifiedClaimJstDate !== today) {
        state.lastNotifiedClaimJstDate = today;
        summary.notify = true;
        summary.notifyReason = summary.notifyReason ?? 'first_claim_success';
      } else if (claimResult.outcome === 'unknown') {
        summary.notify = true;
        summary.notifyReason = 'claim_unknown_state';
      }
    }
  } else {
    summary.claim = claimedToday ? { outcome: 'already_claimed_today' } : { outcome: 'skipped_unspent_grant_waiting' };
  }
  summary.claim = summary.claim ?? claimResult;

  // 7. Refusal streak: 3 distinct JST days with no success -> notify once.
  if (state.refusalDays.length >= 3 && !state.refusalStreakNotified) {
    state.refusalStreakNotified = true;
    if (!summary.notify) {
      summary.notify = true;
      summary.notifyReason = 'refused_3_days';
    }
  }
  summary.refusalDays = state.refusalDays;
  summary.totalClaimedFlop = state.totalClaimedFlop;
  summary.unspentGrant = state.lastGrant && !state.lastGrant.spent ? state.lastGrant : null;

  saveState(state);
  logLine({ address, outcome: 'run_summary', ...summary });
  console.log(JSON.stringify(summary, null, 2));
}

main()
  .catch((e) => {
    console.error('ERROR:', e.message);
    try {
      logLine({ outcome: 'error', error: e.message });
    } catch {}
    process.exit(1);
  })
  .finally(() => closeApi());
