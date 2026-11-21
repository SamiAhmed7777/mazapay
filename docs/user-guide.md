# MazaPay User Guide

A plain-English guide to running MazaPay: checking your MAZA wallet,
taking payments, and getting the results into MIP Fund Accounting.

You do **not** need an AI agent, a programming background, or any
special tool. If you can open a terminal window and type (or paste) a
command, you can do everything in this guide.

---

## What MazaPay is

MazaPay is a small program that lets your organization accept payments
in **MAZA** (MazaCoin) while keeping your books in **US dollars**.

It sits between three things:

1. **The MAZA wallet** ("the wallet") — the software that holds and
   moves your MAZA on the MazaCoin network.
2. **The bridge** — MazaPay itself. It watches the wallet, locks in
   exchange rates, and keeps records of every payment.
3. **MIP Fund Accounting** — the accounting system your organization
   already uses. MazaPay produces import files that MIP reads.

For every payment, MazaPay locks the MAZA/USD exchange rate at the
moment of the quote. The USD amount your member agreed to pay is the
USD amount that lands on your books — which is what accounting rules
(IRS Notice 2014-21, FASB ASU 2023-08) expect for cryptocurrency.

**Plain-language glossary** (words you'll see below):

- **Daemon** — a program that runs quietly in the background. The MAZA
  wallet's daemon is called `mazad`.
- **RPC** — simply the "question-and-answer" channel the bridge uses to
  talk to the wallet. It's protected by a username and password that
  live in the wallet's own settings file.
- **Confirmation** — one block added to the MAZA blockchain *on top of*
  the block containing a payment. The more confirmations, the more
  final the payment. MazaPay waits for **6** confirmations by default.

---

## Before you start

You need:

| What | Why | How to check |
|---|---|---|
| Python 3 | Runs the bridge and the self-test | `python3 --version` |
| The MazaPay software (the `mazapay` folder) | The bridge itself | ask whoever manages your systems |
| The canonical MAZA wallet (MazaCoin/maza v0.16.3) installed | Holds and moves MAZA | wallet starts and shows a balance |
| RPC turned on in the wallet's settings | Lets the bridge talk to the wallet | `rpcuser`, `rpcpassword`, `rpcport`, and `server=1` set in `maza.conf` |
| Internet connection | Live MAZA/USD prices (no account or API key needed) | you're reading this online |

The wallet's settings file (`maza.conf`) lives in the wallet's data
folder — on Linux typically `~/.maza/maza.conf`, on Windows inside the
`AppData\Maza` folder. If your wallet was set up for you, the RPC
settings are probably already there. The self-test in Step 1 will tell
you if they're missing.

> Tip: back up your wallet before going live (the wallet program has a
> backup option for its `wallet.dat` file). MazaPay never needs your
> password and never displays it.

---

## Step 1 — Run the self-test on your wallet

Before connecting anything, run MazaPay's built-in checker. It asks
your wallet the same seven questions the bridge will ask later, so any
problem shows up now — with a plain-English fix — instead of in the
middle of a real payment.

Open a terminal, go to the `mazapay` folder, and run:

```bash
cd mazapay
python3 -m mazapay.check_daemon
```

The checker automatically finds your `maza.conf`. If it can't find one,
it will simply ask you a few questions (the wallet computer's address,
port, username, and password) and continue.

**This self-test never sends money.** Even its "can this wallet pay
vendors?" question is asked in a deliberately invalid way that the
wallet must refuse — so nothing can move. It was verified 7-for-7
against a live MAZA node.

### The seven checks

| # | Check | What it means in plain English |
|---|---|---|
| 1 | daemon reachable & credentials | Is the wallet running, and does its username/password work? |
| 2 | wallet loaded | Can the wallet create a fresh address? (Uses one harmless spare address.) |
| 3 | address validation | Are the addresses this wallet produces valid for payments? |
| 4 | receive tracking | Can the bridge see money arriving? |
| 5 | transaction history | Can the bridge look up recent transactions? |
| 6 | send capability | Could this wallet pay vendors? (Probed safely — nothing is sent.) |
| 7 | balance | A warning-only note. Zero balance is fine for *receiving*; paying vendors needs MAZA in the wallet. |

### Reading the result

Each check prints `[PASS]` or `[FAIL]`. Every failure comes with a
`fix:` line telling you exactly what to do — for example *"Is mazad
running?"* or *"re-check rpcuser and rpcpassword in maza.conf."*

- Exit code **0** and a line reading **`READY — this daemon can serve
  the MazaPay bridge.`** → you're set. Move to Step 2.
- Exit code **1** and **`NOT READY`** → fix the `[FAIL]` items it
  lists, then run it again. Repeat until READY.

When it's ready, the checker prints three lines to paste into the next
step. It shows your password as `<type your rpcpassword here>` — it
**never** prints the actual password, and it masks it as `***`
everywhere else.

---

## Step 2 — Connect the bridge to your wallet

Paste the settings lines the checker printed (fill in your wallet's
RPC password where indicated):

```bash
export MAZAPAY_MAZAD_RPC=http://127.0.0.1:<rpcport-from-maza.conf>
export MAZAPAY_MAZAD_RPC_USER=<your rpcuser>
export MAZAPAY_MAZAD_RPC_PASSWORD=<your rpcpassword>
```

Then create a password **for the bridge itself** (this protects the
payment-spending features; a real wallet refuses to run without it):

```bash
export MAZAPAY_API_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Write down the token it prints — you'll need it whenever you ask the
bridge to do something administrative (like exporting).

Now start the bridge:

```bash
python3 -m mazapay.api
```

It listens quietly on your own machine at `127.0.0.1:8443` (that
address means "this computer only" — not the open internet). To check
it's alive, open a second terminal:

```bash
curl -s localhost:8443/health
```

You should see `"chain_mode": "rpc"` — that means it's talking to your
real wallet. (If you ever see `"mock"`, the bridge is in practice mode
with a demo banner and no real money can move — the wallet settings
above didn't reach it.)

Leave this terminal window open while you take payments.

---

## Step 3 — Take a payment

This is the part members see. Open a web browser on the same machine
and go to the bridge's checkout page:

```
http://127.0.0.1:8443/
```

### The flow, step by step

1. **Enter the USD amount** the member owes (say, `25.00`) and click
   **Lock quote**. The page is branded with the MazaCoin wordmark and
   coin; if the bridge is running on the mock chain a small **Demo mode**
   chip shows at the top of the card.
2. MazaPay looks up the live MAZA price (from a public price service —
   no account needed) and **locks the rate** for this order. The page
   shows the exact MAZA amount to send, the locked rate, and a
   countdown — the quote is good for **15 minutes** by default.
3. The page shows a **QR code** in a framed tile under **"Scan with
   your MazaCoin wallet"** — scanning pre-fills the address *and* the
   amount in the MazaCoin desktop wallet (the QR carries the official
   `maza:` payment link). The address itself is printed below, with a
   **Copy address** button as the always-usable fallback for apps that
   only read bare addresses. Send *exactly* the quoted MAZA amount to
   this address. (A little *more* is fine and still credits; *less* will
   not register.)
4. A four-step progress line tracks the payment: **Quote locked →
   Payment seen → Confirmed → Receipt**. Under it the page reports
   **Watching the chain for your payment…** and checks automatically
   every few seconds.
5. Once the payment arrives and reaches **6 confirmations**, the progress
   completes and a **receipt panel** appears: amount in USD, MAZA
   received, the locked rate and its source, confirmations, the
   transaction id, the local time, and the order id — with a reminder
   that the panel can be screenshotted for the member's records. The
   order is now `paid` in the bridge's records, with the transaction ID
   saved for the audit trail. (Expired quotes get a panel offering a
   fresh quote; a late payment is flagged for review rather than lost.)

### What the statuses mean

| Status you might see | Meaning |
|---|---|
| `awaiting` | Quote issued; waiting for the payment to arrive. |
| `paid` | Payment received and confirmed (6 confirmations). Ready to export to MIP. |
| `expired` | The 15-minute quote ran out with no payment. Nothing happened — start a new quote. |
| `late_paid` | The quote expired but the money arrived anyway. **Not lost** — staff will accept it at the original locked price or refund it. |
| `refunding` / `refunded` | A late payment is being sent back / was sent back, at the original MAZA amount. |

### How long should it take?

Two clocks are involved:

- **MazaPay's watcher** re-checks the wallet every ~10 seconds, so
  detection is nearly instant once the chain confirms.
- **Confirmations** are the real wait. Each confirmation is a new
  block on the MAZA network stacked on top of your payment's block;
  MazaPay waits for 6. On a normally running network that's usually a
  matter of minutes, though it can vary — confirmations depend on the
  network, not on MazaPay.

If it's taking much longer than usual, see
[Troubleshooting](#troubleshooting) — the most common cause is the
wallet still syncing, not a lost payment.

---

## Step 4 — Export to MIP Fund Accounting

Once payments are `paid`, export them so MIP can record them. MazaPay
writes ordinary CSV files (spreadsheets) that MIP's **Transaction
Import** module reads.

Two kinds of export:

| Kind | URL ending | MIP session | What's in it |
|---|---|---|---|
| Money **in** (member payments) | `kind=in` | Cash Receipts | collected payments |
| Money **out** (vendor payments) | `kind=out` | Cash Disbursements | payments the bridge sent |

To export money received so far (in the bridge's terminal or a second
one):

```bash
curl -s -H "Authorization: Bearer <your-API-token>" "localhost:8443/export/mip?kind=in"
```

The reply tells you the **path** of the CSV file it wrote, for example:

```
<your export folder>/mip_cash_receipts_<session>.csv
```

Where the files land: the export folder is set when the bridge starts
(`MAZAPAY_MIP_EXPORT_DIR`). The default is a temporary folder
(`/tmp/mazapay-mip-out`) that gets cleared on reboot — for real use,
ask your operator to point it at a permanent folder.

### Importing into MIP

1. Open MIP's **Transaction Import** module.
2. Start a new session for **Cash Receipts** (or Cash Disbursements)
   and select the CSV file MazaPay wrote.
3. Your MIP consultant maps the columns once per organization. The
   default column layout is `SESSION_ID, TRX_DATE, BANK_CODE,
   PAYOR (or VENDOR), REFERENCE, GL_ACCOUNT, AMOUNT, CURRENCY,
   FX_RATE, DESCRIPTION` — if your MIP import definition expects a
   different order or names, the bridge's column layout can be
   reconfigured to match (`MAZAPAY_MIP_RECEIPT_COLUMNS` /
   `MAZAPAY_MIP_DISBURSEMENT_COLUMNS`).
4. Import, review, and post as with any MIP import session.

### What's in each row

Every row carries its full audit trail in the DESCRIPTION:
the MAZA quantity, the locked USD rate, where the rate came from, and
the blockchain transaction ID (`txid`). The AMOUNT is the **USD**
amount at the locked rate, so your books show dollars with the
crypto detail preserved — the accounting treatment for cryptocurrency
under IRS Notice 2014-21 / FASB ASU 2023-08.

### Safe to re-run

Exporting twice does **not** create duplicate MIP entries: payments
already exported are skipped on the next run. If an export is
interrupted (say, the power blinks), the next run resumes the *same*
session file rather than leaving you two half-finished files.

---

## Step 5 — See all your orders in one page

When you want an overview — what's awaiting payment, what's paid, what
was exported — open the bridge's dashboard page in a browser:

```
http://localhost:8443/admin
```

The page asks for a login: leave the username blank (or any word) and
enter the bridge's token as the **password** — the same token from
Step 2. (If you'd rather script it: `curl -H "Authorization: Bearer
***" localhost:8443/admin` shows the same data as HTML, and adding
`?format=json` gives a machine-readable version.)

What you'll see:

- **Status chips** at the top — how many orders are awaiting, paid,
  late, refunded, and so on.
- **The order table**, newest first: when it was created, its order ID
  (member payments link to their public checkout page), kind (in =
  money received, out = money sent), status, amounts, the locked
  exchange rate and where it came from, the transaction ID, and the
  reference text.
- **Export links** at the bottom — the same two MIP CSV exports as
  Step 4, one click each.

Handy filters (add them to the URL): `?kind=in` or `?kind=out` for
received vs. sent, `?status=paid` for one status,
`?limit=20` for a shorter page (up to 500).

The dashboard is read-only — nothing you do on the page can move
money or change an order. One security note: the browser login sends
the token as base64 (not encrypted by itself), so open the dashboard
only over the bridge's HTTPS address or on the machine it runs on.

---

## Payment links

A **payment link** is a shareable URL for a fixed USD amount. Create
it once, send it to the person who owes you (text, email, invoice
PDF — anything that carries a link), and they get the same checkout
flow as the checkout page — pre-filled, with nothing to type.

Create a link (requires the API token):

```
curl -X POST -H "Authorization: Bearer ***" \
     -d '{"usd_amount": "25.00", "memo": "Club dues, fall term"}' \
     localhost:8443/links
```

```json
{
  "link_id": "…",
  "pay_url": "http://localhost:8443/pay/…",
  "usd_amount": "25.00",
  "memo": "Club dues, fall term",
  "status": "active",
  "expires_at": "2026-12-20T…"
}
```

Send the `pay_url`. Anyone who opens it sees the amount and your
memo, clicks **Pay**, and gets the usual quote → address/QR → receipt
flow; the watcher credits it exactly like any other payment, and the
receipt shows up in the MIP export with the memo as its reference
(memos that aren't safe reference text are recorded under a neutral
`paylink` tag instead). The public page shows only the link itself —
no merchant details, no tokens, no dashboard data.

Rules worth knowing:

- **Amounts are exact.** `usd_amount` is a decimal string with at
  most two places (`"25.00"`, not `25`); NaN, negatives, zero and
  sub-cent amounts are refused. The buyer cannot change the amount —
  the order always carries the link's own amount and memo.
- **Memos are optional**, stored verbatim up to **200 characters**
  (longer memos are refused, not truncated), and always rendered
  escaped on the page.
- **A link lives 30 days** by default (`MAZAPAY_LINK_TTL_DAYS` sets
  it). An expired or deactivated link shows "no longer available" and
  refuses new orders.
- **A link pays once, first-come-first-served.** The first checkout
  binds an order to the link; when that order settles, the link flips
  to **paid** and the page shows an "already paid" banner.
- **Idempotency:** send an `idempotency_key` when creating links —
  retrying the same key returns the same link instead of making a
  duplicate.
- **Resend the same link freely:** re-opening a paid or expired link
  never creates or changes an order.

Managing links (all require the token):

```
curl -H "Authorization: Bearer ***" localhost:8443/links          # newest first, with status + order id
curl -X POST -H "Authorization: Bearer ***" \
     localhost:8443/links/<link_id>/deactivate                    # retire a link
```

A paid link cannot be deactivated (its receipt history stays
truthful), and the public checkout on a link counts against the same
open-order limits as the checkout page, so a leaked link can't be
used to flood the bridge.

---

## Troubleshooting

### "The explorer shows my payment, but the order still says awaiting"

**This is almost always sync lag, not lost funds.**

The MAZA blockchain is about **4.48 million blocks** long. A newly
started wallet must download all of them before it can see recent
payments — on first launch this takes hours, and during that time the
wallet reports it is in **"initial block download."** While syncing:

- a **public block explorer** (which runs on a fully synced server)
  shows your payment, but
- your **own wallet** — the one MazaPay watches — can't see it yet.

So the money is on the chain and safe; your wallet just hasn't caught
up. **Leave the wallet running** until it finishes: its block count
keeps climbing toward ~4.48 million, and the "initial block download"
flag turns off when it's done. MazaPay then detects the payment
automatically and confirms the order — no refund, no re-send, no
action needed.

Quick way to check sync progress (optional):

```bash
maza-cli getblockchaininfo
```

If `blocks` is still far below the current chain height and
`initialblockdownload` is `true`, it's still syncing. Wait — and
check the explorer to reassure yourself the payment exists.

### The self-test says NOT READY

Read the `fix:` line — the checker explains each failure in plain
English. The usual ones:

| Fix message | What to do |
|---|---|
| "Is mazad running? … maza.conf has server=1" | Start the wallet daemon; make sure `server=1` is in `maza.conf`. |
| "RPC rejected the credentials — re-check rpcuser and rpcpassword" | The username/password in `maza.conf` don't match what you passed. |
| "The wallet is not loaded or is locked/encrypted" | Unlock the wallet, or create/load one and back it up. |
| "balance is 0" (warning only) | Fine for collecting payments; top up before paying vendors. |

Fix, re-run `python3 -m mazapay.check_daemon`, and repeat until READY.

### "price feed unavailable" when quoting

The bridge checks a primary public price feed first, then automatically
tries its backup feed — so one feed having a bad moment usually costs
nothing. Only if **both** live feeds fail does quoting stop. Nothing was
lost; just try the quote again in a minute. No order is ever priced from a
stale or made-up rate without your operator explicitly allowing it, so
until a feed answers, the bridge refuses to guess rather than misprice an
order.

### "Quote expired — start a new payment," but I already sent the MAZA

Not lost. The bridge notices money arriving even after a quote
expires and marks the order `late_paid`. Staff then either **accept**
it (recorded at the original locked price) or **refund** it (the
original MAZA amount sent back to an address you provide).

### The page never turns green and it's not sync lag

Check, in order:

1. The address was copied in full (they're long — use the copy button).
2. The amount sent matches the quote exactly (more is OK; less won't
   register).
3. Enough time has passed for 6 confirmations.
4. The bridge terminal is still open and shows no errors — if it
   stopped, restart it (`python3 -m mazapay.api`); its records are
   kept in a database file, not in memory, so nothing is lost by a
   restart.

### Vendor payment status jargon (for operators)

When the bridge *sends* MAZA (vendor payments or refunds), you may see
`pending_send` (about to go), `sent` (done, with txid), or
`send_uncertain` (the wallet's answer was cut off mid-send). Nothing is
ever silently dropped: `send_uncertain` has a dedicated reconcile step
that looks up the payment on the chain and settles it either way.

---

## Frequently asked questions

**Do I need an AI agent or coding skills?**
No. Everything in this guide is a web page, a terminal command, or a
MIP import — the same tools you already use.

**Where does the exchange rate come from?**
A public MAZA/USD price feed — CoinPaprika by default (no account or
API key required), with automatic failover to CoinLore if the primary
doesn't answer. Each order's locked rate and the source that actually
served it are recorded for the audit trail.

**Why does the rate "lock"?**
So the books match reality. The USD you quote the member is the USD
recorded when the payment confirms — even if the market price moved in
between. That's what the accounting rules for cryptocurrency expect.

**What exactly is a confirmation?**
A new block added on the MAZA network *after* the block containing
your payment. One confirmation means it's in the chain; six (the
default) is the standard level of certainty that it stays there.

**How long does a payment take end to end?**
Send time, plus 6 confirmations — typically minutes on a healthy
network. The bridge re-checks every ~10 seconds and updates the page
automatically. If it drags on, check sync status first (see
Troubleshooting).

**Does the self-test ever move money?**
No. Its send-capability question is asked with deliberately invalid
data the wallet must refuse, and its wallet-loaded question only
consumes one unused address. It was verified 7-for-7 against a live
node.

**Does MazaPay ever see or show my wallet password?**
The self-test masks it as `***` in every output and prints a
placeholder instead of the real value. You type it only into your own
`maza.conf` / environment settings — never into a web page or a chat.

**Where do the export files go?**
Into the export folder configured when the bridge starts
(`MAZAPAY_MIP_EXPORT_DIR`; the default `/tmp/...` is temporary — ask
your operator for your organization's real folder). The export reply
always tells you the exact file path.

**What if two people export at the same time, or an export crashes?**
Exports are designed to be safe under both: concurrent runs split the
work without overlap, and a crashed export is resumed as the *same*
session file — never two competing files for the same payments.

**Can we run this without MIP?**
Yes. The exports are ordinary CSV files — useful even if you only want
a record of payments. MIP's Transaction Import is just the step that
turns those files into posted accounting entries.

**Is there a practice mode?**
Yes. With no wallet configured, the bridge runs against a clearly
labeled mock chain (the checkout page shows a subdued "Demo mode" chip and
`/health` reports `chain_mode: mock`). Nothing real moves — handy for
training before Step 2.

---

*Questions or anything unclear in this guide? The README.md in the
`mazapay` folder covers the technical details, and your system
operator can adjust the settings mentioned above.*
