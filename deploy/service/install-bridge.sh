#!/usr/bin/env bash
# MazaPay bridge — staged cutover installer for samihost.
#
# Installs/updates the bridge as a supervised systemd service behind Caddy:
#   - release dirs under /opt/mazapay/releases/<epoch> + `current` symlink
#   - hardened unit from deploy/mazapay.service (WorkingDirectory=current/)
#   - secrets assembled in-process into /etc/mazapay.env (0600): the mazad
#     RPC password is parsed from maza.conf and the API token generated
#     WITHOUT either value ever reaching stdout, a log line, or this repo.
#     Reruns PRESERVE an existing env file (sha256 proven, token reused).
#   - marker-delimited managed Caddy block: TLS reverse proxy, admin routes
#     (audit 4.3) and /health posture detail (audit L-4) off the public vhost.
#     Candidate validated (`caddy validate`) BEFORE the live file is touched;
#     restore path validates the restored file before reloading.
#   - retires the ad-hoc nohup :18443 instance ONLY if its cmdline matches
#     mazapay.api (anything else on that port is left alone with a warning).
#
# Exit codes: 0 deployed | 1 rolled back cleanly | 2 ROLLBACK FAILED
#             3 aborted before any mutation
#
# Sandboxability: every production path is overridable, but overrides are
# honored ONLY when SANDBOX_ROOT is set (fail-closed against fat-fingering
# production paths from a shell history).
set -uo pipefail
# NOTE: -e is intentionally NOT set: every gate checks rc explicitly and the
# EXIT trap drives rollback; errexit + explicit-rc mix silently skips traps.

# --- identity (never trust $0 after a cd) ------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- production constants (overridable ONLY under SANDBOX_ROOT) --------------
OPT_DIR="/opt/mazapay"
UNIT_DST="/etc/systemd/system/mazapay.service"
ENV_DST="/etc/mazapay.env"
CADDYFILE="/etc/caddy/Caddyfile"
CADDY_BACKUP_SUFFIX=".pre-mazapay"
RPC_CONF="/var/lib/mazad/maza.conf"
RUN_USER="mazapay"
LISTEN_HOST="127.0.0.1"
# 18500: loopback behind Caddy. 8443 is UNUSABLE on samihost — SamiPanel binds
# *:8443 (wildcard covers loopback; the unit's bind crash-loops Errno 98 and
# the health probe reads SamiPanel's 404). Found live 2026-11-15.
LISTEN_PORT="18500"
CADDY_DOMAIN="mazapay.sami-ahmed.net"
LOG_DIR="/var/log/caddy"   # sandbox: overridden below; path derived after the SANDBOX block
HOST_PUB_IP="194.163.161.162"   # samihost public v4 — the DNS gate's expected A record
LEGACY_PORT="18443"
# caddy-guard state files (Samihost reconciler): SamiPanel's Caddyfile generator
# rewrites the WHOLE file on panel events and the guard restores ONLY blocks
# registered in these files — an unregistered block is silently dropped on the
# next regen (found live 2026-11-15: the deployed mazapay vhost vanished hours
# after install, external TLS died with "tlsv1 alert internal error"). The
# guard rebuilds its merged file from a static array + the .pre-guard file on
# every event pass, so .pre-guard is the durable registration; both are written.
GUARD_BLOCKS="/var/lib/caddy-guard/protected-blocks.txt"
GUARD_PREGUARD="${GUARD_BLOCKS}.pre-guard"
PROC_DIR="/proc"
LOCK_DIR="/run/lock"

if [ -n "${SANDBOX_ROOT:-}" ]; then
    OPT_DIR="${SANDBOX_OPT_DIR:?SANDBOX_OPT_DIR required}"
    UNIT_DST="${SANDBOX_UNIT_DST:?SANDBOX_UNIT_DST required}"
    ENV_DST="${SANDBOX_ENV_DST:?SANDBOX_ENV_DST required}"
    CADDYFILE="${SANDBOX_CADDYFILE:?SANDBOX_CADDYFILE required}"
    RPC_CONF="${SANDBOX_RPC_CONF:?SANDBOX_RPC_CONF required}"
    GUARD_BLOCKS="${SANDBOX_GUARD_BLOCKS:-}"
    GUARD_PREGUARD="${SANDBOX_GUARD_PREGUARD:-}"
    LOCK_DIR="${SANDBOX_ROOT}/lock"
    PROC_DIR="${SANDBOX_PROC_DIR:?SANDBOX_PROC_DIR required}"
    LEGACY_PORT="${SANDBOX_LEGACY_PORT:-18999}"
    CADDY_DOMAIN="${SANDBOX_CADDY_DOMAIN:-sandbox.mazapay.invalid}"
    RUN_USER="${SANDBOX_RUN_USER:-mazapay-sbx}"
    TREE_SRC="${SANDBOX_TREE_SRC:?SANDBOX_TREE_SRC required}"
    UNIT_SRC="${SANDBOX_UNIT_SRC:?SANDBOX_UNIT_SRC required}"
    LOG_DIR="${SANDBOX_ROOT}/var/log/caddy"
    mkdir -p "$LOCK_DIR"
fi
LOG_PATH="${LOG_DIR}/${CADDY_DOMAIN}.access.log"

if [ -z "${SANDBOX_ROOT:-}" ]; then
    UNIT_SRC="${SCRIPT_DIR}/../mazapay.service"
    [ -f "${SCRIPT_DIR}/mazapay.service" ] && UNIT_SRC="${SCRIPT_DIR}/mazapay.service"
    # TREE_SRC is the REPO ROOT (parent of the mazapay/ package): the unit runs
    # `python3 -m mazapay.api` with WorkingDirectory=/opt/mazapay/current, and
    # `-m` resolves packages from cwd — so a release must CONTAIN mazapay/,
    # not BE the package dir. (The 132870e run crashed-looped exactly here.)
    TREE_SRC="${SCRIPT_DIR}/../.."
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"   # pid suffix: same-second reruns can't collide
REL_DIR=""
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/mazapay-install.XXXXXXXX")"
chmod 0700 "$SCRATCH"
LOCK_FILE="${LOCK_DIR}/mazapay-install.lock"

# --- state flags -------------------------------------------------------------
MUTATED=0        # armed before the first live mutation
CADDY_CHANGED=0
UNIT_CHANGED=0
SYMLINK_CHANGED=0
LEGACY_KILLED=0
SUCCESS=0
RC=0

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }
fail() { # fail <code> <message> — rollback decided by caller context
    local code="$1"; shift
    printf '[install] FAIL: %s\n' "$*" >&2
    exit "$code"
}

on_exit() {
    local rc=$?
    rm -rf "$LOCK_FILE" 2>/dev/null || true
    if [ "$SUCCESS" -eq 1 ]; then
        rm -rf "$SCRATCH"
        exit 0
    fi
    if [ "$MUTATED" -eq 0 ]; then
        rm -rf "$SCRATCH"
        log "aborted before any mutation — nothing to roll back"
        exit "$rc"
    fi
    # NOTE: scratch survives until rollback finishes — it carries the
    # prior-release pointer and the caddy restore working copies.
    rollback
    exit $?
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# --- rollback (fail-closed; proves restoration) ------------------------------
rollback() {
    trap - EXIT   # disarm ourselves; we manage the exit path here
    log "ROLLBACK: restoring pre-deploy state"
    local ok=1

    # 1. Caddy: restore backup, validate the RESTORED file, only then reload.
    if [ "$CADDY_CHANGED" -eq 1 ]; then
        local bak="${CADDYFILE}${CADDY_BACKUP_SUFFIX}"
        if [ -f "$bak" ]; then
            if cp "$bak" "${SCRATCH}/caddy.restored" \
               && caddy validate --adapter caddyfile --config "${SCRATCH}/caddy.restored" >/dev/null 2>&1 \
               && cp "${SCRATCH}/caddy.restored" "$CADDYFILE"; then
                if caddy reload --config "$CADDYFILE" >/dev/null 2>&1; then
                    log "ROLLBACK: caddy restored + validated + reloaded"
                else
                    warn "ROLLBACK: caddy file restored but RELOAD failed"
                    ok=0
                fi
            else
                warn "ROLLBACK: caddy restore failed validation — live file NOT touched"
                ok=0
            fi
        else
            warn "ROLLBACK: no caddy backup found"
            ok=0
        fi
    fi

    # 2. Unit: restore backup (or remove fresh-install unit), reload daemon.
    if [ "$UNIT_CHANGED" -eq 1 ]; then
        local ubak="${UNIT_DST}.pre-mazapay"
        if [ -f "$ubak" ]; then
            cp "$ubak" "$UNIT_DST" || { warn "ROLLBACK: unit restore failed"; ok=0; }
        else
            rm -f "$UNIT_DST" || warn "ROLLBACK: unit remove failed"
        fi
        systemctl daemon-reload >/dev/null 2>&1 || warn "ROLLBACK: daemon-reload failed"
    fi

    # 3. Release symlink: repoint to prior release (fresh install: remove).
    if [ "$SYMLINK_CHANGED" -eq 1 ]; then
        local prev
        prev="$(cat "${SCRATCH}/prev_release" 2>/dev/null || true)"
        if [ -n "$prev" ] && [ -d "$prev" ]; then
            ln -sfn "$prev" "${OPT_DIR}/current" && log "ROLLBACK: current -> $prev" \
                || { warn "ROLLBACK: symlink restore failed"; ok=0; }
        else
            rm -f "${OPT_DIR}/current" && log "ROLLBACK: fresh install, symlink removed" \
                || { warn "ROLLBACK: symlink remove failed"; ok=0; }
        fi
    fi
    # The staged release was never activated — discard it on every rollback path.
    if [ -n "$REL_DIR" ] && [ -d "$REL_DIR" ]; then
        rm -rf "${REL_DIR:?}" && log "ROLLBACK: staged release discarded"
    fi

    # 4. Service: prior release running again, or fresh install stays down.
    if [ -n "$(cat "${SCRATCH}/prev_release" 2>/dev/null || true)" ]; then
        systemctl restart mazapay.service >/dev/null 2>&1 || { warn "ROLLBACK: service restart failed"; ok=0; }
    else
        systemctl stop mazapay.service >/dev/null 2>&1 || true
        systemctl disable mazapay.service >/dev/null 2>&1 || true
    fi

    # 5. Legacy nohup: we killed OUR ad-hoc instance and cannot un-kill it;
    #    the systemd service (running or freshly restarted above) is the
    #    replacement, so this is not restorable state. Record, don't fail.
    if [ "$LEGACY_KILLED" -eq 1 ]; then
        log "ROLLBACK: legacy :${LEGACY_PORT} instance stays retired (superseded by unit)"
    fi

    if [ "$ok" -eq 1 ]; then
        log "ROLLBACK-DONE"
        exit 1
    fi
    printf '[install] FATAL: ROLLBACK FAILED — MANUAL ATTENTION REQUIRED\n' >&2
    exit 2
}

# --- prerequisites ------------------------------------------------------------
need_bin() {
    local b
    for b in "$@"; do
        command -v "$b" >/dev/null 2>&1 || fail 3 "prerequisite binary missing: $b"
    done
}
need_bin python3 sha256sum install cp mv ln rm ss dig caddy systemctl getent date mktemp curl seq tr \
    find xargs grep sort diff awk sed cut wc useradd kill mkdir

[ "$(id -u)" -eq 0 ] || fail 3 "must run as root"

if [ -z "${SANDBOX_ROOT:-}" ]; then
    # Single-instance lock so two ticks can't interleave.
    mkdir -p "$LOCK_DIR"
    if ! mkdir "$LOCK_FILE" 2>/dev/null; then
        fail 3 "another install appears in progress (lock: $LOCK_FILE)"
    fi
fi

# One-shot health readback: body -> file, status -> var, parsed as JSON.
health_probe() { # $1 = url ; echoes "STATUS" ; writes body to $2
    local url="$1" body="$2" code
    code="$(curl -m 5 -s -o "$body" -w '%{http_code}' "$url" 2>/dev/null)" || code="000"
    [ -n "$code" ] || code="000"
    printf '%s' "$code"
}
health_ok() { # $1 = url ; 0 iff HTTP 200 and body JSON has ok==true
    local code body="${SCRATCH}/health.json"
    code="$(health_probe "$1" "$body")"
    [ "$code" = "200" ] || return 1
    python3 - "$body" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("ok") is True else 1)
PY
}

# --- preflight (READ-ONLY; journal before any mutation) -----------------------
log "preflight: source tree + environment checks"
[ -f "$TREE_SRC/mazapay/api.py" ] && [ -d "$TREE_SRC/mazapay/templates" ] \
    || fail 3 "source tree incomplete: $TREE_SRC (need mazapay/api.py + mazapay/templates/ at the repo ROOT)"

# Syntax gate on every .py we are about to ship.
if ! find "$TREE_SRC" -name '*.py' -print0 | xargs -0 -n1 python3 -m py_compile \
     >"${SCRATCH}/pycompile.log" 2>&1; then
    sed -n '1,20p' "${SCRATCH}/pycompile.log" >&2
    fail 3 "py_compile gate failed on source tree"
fi

# If this vhost is public, DNS must already point here or TLS issuance fails.
# Exact-match on the A record (never tail an unfiltered +short output — an
# AAAA line would satisfy a tail-based check).
if [ -z "${SANDBOX_ROOT:-}" ]; then
    if ! dig +short A "$CADDY_DOMAIN" @1.1.1.1 2>/dev/null | grep -qx "$HOST_PUB_IP"; then
        fail 3 "DNS ${CADDY_DOMAIN} has no A record resolving to ${HOST_PUB_IP} — add the fleet-dns record first"
    fi
fi

# The listen port must be free — or held by OUR OWN unit on a rerun.
# Match ANY local address ending in :<port> (127.0.0.1:*, *:*, [::]:*,
# 0.0.0.0:*): a wildcard holder binds loopback too, so a loopback-only grep
# would sail past it and the unit would crash-loop on bind (found live:
# SamiPanel holds *:8443 on samihost).
port_line="$(ss -tln 2>/dev/null | awk -v p=":${LISTEN_PORT}\$" '$4 ~ p {print}' || true)"
if [ -n "$port_line" ]; then
    if ! systemctl cat mazapay.service >/dev/null 2>&1; then
        fail 3 "port ${LISTEN_PORT} already held and mazapay.service is not installed: $port_line"
    fi
    log "port ${LISTEN_PORT} held by installed mazapay.service (upgrade path — will restart)"
fi

# Journal pre-state (abort paths for conflicts, per the pre-gate discipline).
PRE_UNIT=0; [ -f "$UNIT_DST" ] && PRE_UNIT=1
PRE_ENV=0;  [ -f "$ENV_DST" ]  && PRE_ENV=1
PRE_CURRENT=""
[ -L "${OPT_DIR}/current" ] && PRE_CURRENT="$(readlink "${OPT_DIR}/current")"
[ -f "$CADDYFILE" ] || fail 3 "caddyfile missing: $CADDYFILE"
# THREE-state ownership: the marker-delimited block we install, AND the bare
# guard-restored copy — the caddy-guard re-appends its registered block
# verbatim WITHOUT markers, so a post-guard-event rerun must recognize the
# bare form too (found live 2026-11-15: marked-form-only detection appended a
# duplicate site block and failed validation -> clean rollback, exit 1).
if grep -q "BEGIN mazapay-bridge managed block" "$CADDYFILE" 2>/dev/null; then
    CADDY_BLOCK_OWNER=marked
elif grep -qE "^${CADDY_DOMAIN} \{" "$CADDYFILE" 2>/dev/null; then
    CADDY_BLOCK_OWNER=bare
else
    CADDY_BLOCK_OWNER=absent
fi
if [ -d "${OPT_DIR}/releases" ] && [ "$PRE_CURRENT" = "" ] && [ -n "$(ls -A "${OPT_DIR}/releases" 2>/dev/null)" ]; then
    fail 3 "${OPT_DIR}/releases exists with no current symlink — refusing to guess (adopt manually)"
fi
log "prestate: unit=$PRE_UNIT env=$PRE_ENV current='${PRE_CURRENT:-none}' caddy_block=$CADDY_BLOCK_OWNER"

[ -f "$RPC_CONF" ] || fail 3 "rpc conf missing: $RPC_CONF"
getent passwd "$RUN_USER" >/dev/null 2>&1 && USER_EXISTS=1 || USER_EXISTS=0

# --- stage release (service may keep running; live tree untouched) ------------
REL_DIR="${OPT_DIR}/releases/${STAMP}"
log "staging release -> $REL_DIR"
mkdir -p "$OPT_DIR/releases"
cp -R "$TREE_SRC" "${REL_DIR}"
find "$REL_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
# Manifest equality: every staged file must byte-match its source counterpart.
( cd "$TREE_SRC" && find . -type f ! -path '*__pycache__*' -exec sha256sum {} + ) \
    | sort >"${SCRATCH}/manifest.src"
( cd "$REL_DIR" && find . -type f ! -path '*__pycache__*' -exec sha256sum {} + ) \
    | sort >"${SCRATCH}/manifest.rel"
if ! diff -u "${SCRATCH}/manifest.src" "${SCRATCH}/manifest.rel" \
        >"${SCRATCH}/manifest.diff" 2>&1; then
    sed -n '1,20p' "${SCRATCH}/manifest.diff" >&2
    rm -rf "$REL_DIR"
    fail 3 "staged tree manifest mismatch — release discarded, nothing mutated"
fi
log "staged tree verified: $(wc -l <"${SCRATCH}/manifest.rel") files byte-identical"

# Module-resolution gate: the unit runs `python3 -m mazapay.api` with
# WorkingDirectory=the release dir — prove the interpreter resolves the
# package from the staged ROOT before anything is activated (-B: no
# bytecode writes into the release). Catches release-layout bugs (a release
# that IS the package dir crash-loops the unit) pre-mutation.
if ! ( cd "$REL_DIR" && python3 -B -c 'import mazapay.api' ) >/dev/null 2>&1; then
    rm -rf "$REL_DIR"
    fail 3 "staged tree does not resolve 'mazapay.api' from its root — python3 -m mazapay.api would crash-loop; release discarded, nothing mutated"
fi

# --- secrets env (in-process; values never printed) ---------------------------
if [ "$PRE_ENV" -eq 1 ]; then
    ENV_SHA_BEFORE="$(sha256sum "$ENV_DST" | awk '{print $1}')"
    cp "$ENV_DST" "${SCRATCH}/env.before"   # for the secret-preservation proof
else
    ENV_SHA_BEFORE=""
fi
log "assembling env file (secrets stay in-process)"
python3 - "$RPC_CONF" "$ENV_DST" "$LISTEN_HOST" "$LISTEN_PORT" \
    >"${SCRATCH}/env-meta.log" <<'PY'
import hashlib, os, secrets, sys

conf_path, env_path, host, port = sys.argv[1:5]

existing = {}
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()

rpc = {}
for line in open(conf_path):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        rpc[k.strip()] = v.strip()

missing = [f for f in ("rpcuser", "rpcpassword") if not rpc.get(f)]
if missing:
    print(f"FATAL rpc conf missing: {missing}", file=sys.stderr)
    sys.exit(4)

rpcport = rpc.get("rpcport") or "12832"
token = existing.get("MAZAPAY_API_TOKEN") or secrets.token_hex(32)

body = (
    f"MAZAPAY_HOST={host}\n"
    f"MAZAPAY_PORT={port}\n"
    f"MAZAPAY_API_TOKEN={token}\n"
    f"MAZAPAY_MAZAD_RPC=http://127.0.0.1:{rpcport}\n"
    f"MAZAPAY_MAZAD_RPC_USER={rpc['rpcuser']}\n"
    f"MAZAPAY_MAZAD_RPC_PASSWORD={rpc['rpcpassword']}\n"
)
fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, body.encode())
os.close(fd)
os.chmod(env_path, 0o600)

sha = hashlib.sha256(open(env_path, "rb").read()).hexdigest()
print(f"env_present_before={bool(existing)}")
print(f"token_generated={'no' if existing.get('MAZAPAY_API_TOKEN') else 'yes'}")
print(f"token_len={len(token)}")
print(f"rpc_password_len={len(rpc['rpcpassword'])}")
print(f"rpc_port={rpcport}")
print(f"env_sha256={sha}")
PY
rc=$?
if [ $rc -ne 0 ]; then
    cat "${SCRATCH}/env-meta.log" >&2   # metadata only — never secret values
    rm -rf "${REL_DIR:?}"
    fail 3 "env assembly failed (rc=$rc) — no secrets were printed"
fi
if [ "$PRE_ENV" -eq 1 ]; then
    # Preservation invariant: every secret ALREADY in the env file must
    # survive verbatim (values compared in-process, never printed). A file
    # that lacked operational fields legitimately gains them — that is a
    # refresh, not a violation.
    env_ok=1
    while IFS= read -r key; do
        val="$(grep -E "^${key}=" "${SCRATCH}/env.before" | head -1 | cut -d= -f2-)"
        [ -n "$val" ] || continue
        grep -qF -- "${key}=${val}" "$ENV_DST" || env_ok=0
    done <<'KEYS'
MAZAPAY_API_TOKEN
MAZAPAY_MAZAD_RPC_PASSWORD
KEYS
    if [ "$env_ok" -ne 1 ]; then
        rm -rf "${REL_DIR:?}"
        fail 3 "existing env secrets were not preserved — aborting, no secrets printed"
    fi
    ENV_SHA_AFTER="$(sha256sum "$ENV_DST" | awk '{print $1}')"
    if [ "$ENV_SHA_BEFORE" = "$ENV_SHA_AFTER" ]; then
        log "env rerun: PRESERVED byte-identical (sha ${ENV_SHA_AFTER:0:12}…)"
    else
        log "env rerun: secrets PRESERVED, operational fields refreshed (sha ${ENV_SHA_AFTER:0:12}…)"
    fi
else
    log "env created fresh (0600); sha $(sha256sum "$ENV_DST" | awk '{print substr($1,1,12)}')…"
fi
grep -E '^(token_generated|rpc_port)=' "${SCRATCH}/env-meta.log" | while IFS='=' read -r k v; do
    log "env: $k=$v"
done

# --- quiesce: legacy nohup + running service ----------------------------------
# Retire the ad-hoc :18443 instance ONLY if it is really ours (cmdline match).
legacy_line="$(ss -tlnp 2>/dev/null | grep -E "127\.0\.0\.1:${LEGACY_PORT}\b" || true)"
if [ -n "$legacy_line" ]; then
    lpid="$(printf '%s' "$legacy_line" | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)"
    if [ -n "$lpid" ] && [ -r "${PROC_DIR}/${lpid}/cmdline" ]; then
        if tr '\0' ' ' <"${PROC_DIR}/${lpid}/cmdline" | grep -q 'mazapay\.api'; then
            log "retiring legacy nohup instance pid=$lpid on :${LEGACY_PORT}"
            MUTATED=1                       # first live mutation: arm rollback
            kill -TERM "$lpid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$lpid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$lpid" 2>/dev/null && { kill -KILL "$lpid" 2>/dev/null || true; sleep 1; }
            LEGACY_KILLED=1
            kill -0 "$lpid" 2>/dev/null && warn "legacy pid $lpid still alive after KILL"
        else
            warn ":${LEGACY_PORT} held by a NON-mazapay process (pid=$lpid) — leaving it alone"
        fi
    else
        warn ":${LEGACY_PORT} busy but pid unreadable — leaving it alone"
    fi
fi

systemctl stop mazapay.service >/dev/null 2>&1 || true   # upgrade path (fresh: no-op)

# --- mutations ----------------------------------------------------------------
MUTATED=1   # idempotent re-arm (already armed if legacy was killed)

# user
if [ "$USER_EXISTS" -eq 0 ]; then
    useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$RUN_USER" \
        || fail 2 "useradd $RUN_USER failed"   # post-mutation: rollback path
    log "system user created: $RUN_USER"
fi

# unit
if [ "$PRE_UNIT" -eq 1 ]; then
    cp "$UNIT_DST" "${UNIT_DST}.pre-mazapay" || fail 2 "unit backup failed"
fi
install -m 0644 "$UNIT_SRC" "$UNIT_DST" || fail 2 "unit install failed"
UNIT_CHANGED=1
systemctl daemon-reload >/dev/null 2>&1 || fail 2 "daemon-reload failed"

# release symlink
ln -sfn "$REL_DIR" "${OPT_DIR}/current" || fail 2 "symlink swap failed"
SYMLINK_CHANGED=1
[ -n "$PRE_CURRENT" ] && printf '%s' "$PRE_CURRENT" >"${SCRATCH}/prev_release"
log "current -> $REL_DIR"

# caddy managed block
# Guard registration: the vhost block must ALSO exist in the caddy-guard state
# files (byte-exact bare block, no marker comments — the guard re-appends its
# canonical copy verbatim). Written BEFORE the Caddyfile changes: the guard
# reacts to Caddyfile events, and its .pre-guard file is the durable
# registration source. Runs only after the candidate validates, so an abort
# here leaves no registration debris for a block that was never installed.
register_guard_block() {
    [ -n "$GUARD_PREGUARD" ] || return 0
    if [ ! -d "$(dirname "$GUARD_PREGUARD")" ]; then
        log "caddy-guard not present on this host — skipping block registration"
        return 0
    fi
    python3 - "$GUARD_PREGUARD" "$GUARD_BLOCKS" "$CADDY_DOMAIN" "${LISTEN_HOST}:${LISTEN_PORT}" "$LOG_PATH" <<'PYGUARD' || fail 2 "caddy-guard registration failed — a panel Caddyfile regen would drop the vhost (guard copies kept; rerun the installer after fixing)"
import os, sys
preguard, merged, label, upstream, logpath = sys.argv[1:6]
block = (
    "%s {\n"
    "\t@healthcheck path /health\n"
    "\trespond @healthcheck 403\n"
    "\t@adminsurface path /vendors /vendors/* /payments /payments/* /watch /watch/* /export /export/*\n"
    "\trespond @adminsurface 404\n"
    "\trequest_body {\n"
    "\t\tmax_size 1MB\n"
    "\t}\n"
    "\tencode gzip\n"
    "\tlog {\n"
    "\t\toutput file %s {\n"
    "\t\t\troll_size 100MB\n"
    "\t\t\troll_keep 5\n"
    "\t\t}\n"
    "\t\tformat json\n"
    "\t}\n"
    "\treverse_proxy %s\n"
    "}" % (label, logpath, upstream)
)
def ensure(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    txt = ""
    if os.path.exists(path):
        with open(path) as f:
            txt = f.read()
    # guard merge semantics: chunks split on blank lines, label = first token
    # of the first line; replacing any same-label chunk keeps reruns idempotent
    # and refreshes stale copies after a port/domain change.
    chunks = [c.strip() for c in txt.split("\n\n") if c.strip()]
    keep = [c for c in chunks if c.splitlines()[0].split()[0] != label]
    keep.append(block)
    with open(path, "w") as f:
        f.write("\n\n".join(keep) + "\n")
ensure(preguard)
if merged:
    ensure(merged)
print("caddy-guard: block registered (label=%s)" % label)
PYGUARD
    log "caddy-guard: vhost registered in guard state files"
}
CANDIDATE="${SCRATCH}/Caddyfile.candidate"
cp "$CADDYFILE" "$CANDIDATE" || fail 2 "caddyfile copy failed"
# Replace BOTH prior-block forms: our marker-delimited block AND any bare
# top-level block for the domain (guard-restored copies carry no markers).
# Brace-counted removal handles the block's nested directives (request_body,
# log/output-file). Without this, a post-guard-event rerun appends a second
# site block for the same address and caddy validate refuses the candidate.
python3 - "$CANDIDATE" "$CADDY_DOMAIN" <<'PY'
import re, sys
path, domain = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r"# BEGIN mazapay-bridge managed block.*?# END mazapay-bridge managed block\n?",
              "", text, flags=re.S)
lines = text.split("\n")
out, i, removed = [], 0, 0
while i < len(lines):
    line = lines[i]
    if line.strip() == domain + " {":
        depth = line.count("{") - line.count("}")
        i += 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        removed += 1
        continue
    out.append(line)
    i += 1
open(path, "w").write("\n".join(out))
print("caddy: prior block removal done (unmarked removed: %d)" % removed)
PY
if [ "$CADDY_BLOCK_OWNER" != "absent" ]; then
    log "caddy: replaced existing ${CADDY_BLOCK_OWNER} block"
fi
cat >>"$CANDIDATE" <<EOF

# BEGIN mazapay-bridge managed block — edits between the markers are overwritten
${CADDY_DOMAIN} {
	@healthcheck path /health
	respond @healthcheck 403
	@adminsurface path /vendors /vendors/* /payments /payments/* /watch /watch/* /export /export/*
	respond @adminsurface 404
	request_body {
		max_size 1MB
	}
	encode gzip
	log {
		output file ${LOG_PATH} {
			roll_size 100MB
			roll_keep 5
		}
		format json
	}
	reverse_proxy ${LISTEN_HOST}:${LISTEN_PORT}
}
# END mazapay-bridge managed block
EOF
# caddy validate INSTANTIATES log writers — as root it would create the access
# log root-owned 0600 and the later in-service reload (user caddy) then fails
# with permission denied (found live 2026-11-15). Pre-create it owned like the
# log dir so both validate and the service open the same caddy-owned file.
# Fresh hosts may lack the log dir entirely — create it and hand it to the
# caddy user (best effort: a host without the caddy user manages its own
# logging layout and gets a loud WARN instead of a silent root-owned file).
if [ ! -d "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR" || fail 2 "log dir creation failed: $LOG_DIR"
    if getent passwd caddy >/dev/null 2>&1; then
        chown caddy:caddy "$LOG_DIR" 2>/dev/null || chown caddy "$LOG_DIR" 2>/dev/null || true
        chmod 750 "$LOG_DIR" 2>/dev/null || true
        log "caddy: log dir created ($LOG_DIR, owner caddy)"
    else
        chmod 755 "$LOG_DIR" 2>/dev/null || true
        warn "log dir created but no caddy user on this host — file ownership will follow the dir (root)"
    fi
fi
if [ -d "$LOG_DIR" ]; then
    [ -e "$LOG_PATH" ] || touch "$LOG_PATH"
    chown --reference="$LOG_DIR" "$LOG_PATH" 2>/dev/null || true
    chmod 600 "$LOG_PATH"
    log "caddy: access log pre-created ($LOG_PATH)"
fi
if ! caddy validate --adapter caddyfile --config "$CANDIDATE" >/dev/null 2>&1; then
    fail 2 "candidate Caddyfile failed validation — live file untouched"
fi
register_guard_block
cp "$CADDYFILE" "${CADDYFILE}${CADDY_BACKUP_SUFFIX}" || fail 2 "caddy backup failed"
cp "$CANDIDATE" "$CADDYFILE" || fail 2 "caddy install failed"
CADDY_CHANGED=1
if ! caddy reload --config "$CADDYFILE" >/dev/null 2>&1; then
    fail 2 "caddy reload failed — rolling back to previous Caddyfile"
fi
log "caddy: block installed + reloaded"

# --- start + bounded readiness poll -------------------------------------------
systemctl enable mazapay.service >/dev/null 2>&1 || fail 2 "enable failed"
systemctl start mazapay.service >/dev/null 2>&1 || fail 2 "start failed"
log "service started; polling health (bounded)"
up=0
for i in $(seq 1 30); do
    if health_ok "http://${LISTEN_HOST}:${LISTEN_PORT}/health"; then
        up=1
        break
    fi
    sleep 1
done
if [ "$up" -ne 1 ]; then
    code="$(health_probe "http://${LISTEN_HOST}:${LISTEN_PORT}/health" "${SCRATCH}/health.last" || true)"
    fail 2 "health probe never passed after 30s (last status: ${code:-unknown})"
fi
log "health OK (200, ok=true) after ${i}s"

# active-path proof: journald-visible banner line from the unit itself
systemctl is-active --quiet mazapay.service || fail 2 "service not active after health OK"

SUCCESS=1
log "DEPLOYED: ${CADDY_DOMAIN} -> ${LISTEN_HOST}:${LISTEN_PORT} (release ${STAMP})"
[ "$LEGACY_KILLED" -eq 1 ] && log "legacy :${LEGACY_PORT} nohup instance retired"
log "admin routes + /health detail stay off the public vhost (loopback/tailscale for operators)"
exit 0
