# MazaPay bridge — deployment on any VPS

The serving endpoint is **config, not code** (infrastructure end-state directive,
2026-11-18): a deploy on a new host overrides two environment variables and runs
the same installer — zero code edits. The built-in defaults point at the current
bench host; per the directive they are acceptable ONLY as interim bench/testing —
before any public launch the serving endpoint moves to Maza-owned hosting via
exactly this config swap (values pinned by the battery's S0 checks).

## Prerequisites

- Debian/Ubuntu-class Linux, systemd, python3 ≥ 3.9, Caddy installed
- Commands the installer itself requires (its `need_bin` gate checks every
  one before any mutation): `python3 sha256sum install cp mv ln rm ss dig
  caddy systemctl getent date mktemp curl seq tr find xargs grep sort diff
  awk sed cut wc useradd userdel kill mkdir`
- `rsync` is needed on the machine you stage FROM (step 3 syncs the repo to
  the VPS); the installer itself does not use it. `openssl` is NOT required.
- The MazaCoin daemon (`mazad`) reachable on the same host — install with
  `deploy/node/install-mazad.sh` first if this is a fresh box
- DNS control over the domain you will serve

Sandbox battery layout (for the standalone reproduction of
`deploy/service/test-install-bridge.sh`): the battery expects
`install-bridge.sh` and `test-install-bridge.sh` side by side in
`deploy/service/` and the unit file one directory above at
`deploy/mazapay.service` (its `REAL_UNIT` reference) — exactly the repo
layout.

## Steps

The installer writes `/opt`, `/etc/systemd/system`, `/var/lib` and
`/etc/caddy` — it must run as **root**. The two endpoint values must be set
**on the installer's own command line** (step 4): `sudo`-style privilege
escalation scrubs exported environment variables, so an `export` alone would
silently fall back to the built-in defaults.

1. **Pick the endpoint** for this VPS — a public domain you control and the
   VPS's public IPv4 (the examples below use `checkout.your-domain.example`
   and `203.0.113.7`; substitute yours everywhere).

2. **Point DNS**: an A record for your domain → your VPS IPv4. The installer
   gates on an exact match (it aborts until the record resolves) because
   Caddy's TLS issuance would fail otherwise.

3. **Stage the repo ROOT on the VPS** (the unit runs `python3 -m mazapay.api`
   from the repo root — staging the package dir alone crash-loops):

   ```bash
   rsync -a --delete --exclude .git --exclude __pycache__ \
       --exclude '*.sqlite3' /path/to/mazapay/ root@YOUR_VPS:/opt/mazapay-src/
   ```

4. **Run the installer on the VPS as root, passing the endpoint explicitly**:

   ```bash
   ssh root@YOUR_VPS
   cd /opt/mazapay-src
   MAZAPAY_CADDY_DOMAIN="checkout.your-domain.example" \
   MAZAPAY_HOST_PUB_IP="203.0.113.7" \
       bash deploy/service/install-bridge.sh
   ```

   It stages the release under `/opt/mazapay/releases/<stamp>`, installs the
   hardened systemd unit, assembles secrets in-process from
   `/var/lib/mazad/maza.conf` into `/etc/mazapay.env` (0600 — never displayed,
   never stored in the repo), renders the Caddy vhost, registers it with
   caddy-guard when that host runs one (skipped gracefully otherwise), and
   health-polls the restarted unit. Exit codes: 0 deployed, 1 rolled back
   cleanly, 2 rollback itself failed, 3 aborted before any mutation.

5. **Self-test the daemon wiring** (the regular-user path, on the VPS):

   ```bash
   python3 -m mazapay.check_daemon --conf /var/lib/mazad/maza.conf
   ```

   Exit 0 = READY (reachability, wallet, receive tracking, history, send
   surface).

6. **Verify — including from OFF the VPS** (external TLS can be broken by
   upstream regens while the unit and loopback look healthy):

   ```bash
   # on the VPS:
   systemctl is-active mazapay
   curl -s http://127.0.0.1:18500/health        # loopback; 403 on the public vhost is by design
   # from a DIFFERENT machine (same domain value as step 4):
   curl -D - https://checkout.your-domain.example/
   ```

## Portability release gate

```bash
bash deploy/service/test-install-bridge.sh
```

Runs the full hermetic battery (S0–S16): every scenario uses PATH shims and
sandbox paths, asserting exit codes and resulting state — including S15 (an
override deploy under a different domain ships with zero code edits) and S16
(the portability gate catches a planted personal endpoint and fails on
operational errors). The gate sweeps the WHOLE repo surface for personal
endpoints; the only sanctioned holders are the installer's two pinned
defaults, this battery's own assertion/fixture literals (it must quote them to
pin and to catch them), and the sanitized node-install evidence files. All-
green output is a release requirement.
