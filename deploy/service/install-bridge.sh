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
LISTEN_PORT="8443"
CADDY_DOMAIN="mazapay.sami-ahmed.net"
HOST_PUB_IP="194.163.161.162"   # samihost public v4 — the DNS gate's expected A record
LEGACY_PORT="18443"
PROC_DIR="/proc"
LOCK_DIR="/run/lock"

if [ -n "${SANDBOX_ROOT:-}" ]; then
    OPT_DIR="${SANDBOX_OPT_DIR:?SANDBOX_OPT_DIR required}"
    UNIT_DST="${SANDBOX_UNIT_DST:?SANDBOX_UNIT_DST required}"
    ENV_DST="${SANDBOX_ENV_DST:?SANDBOX_ENV_DST required}"
    CADDYFILE="${SANDBOX_CADDYFILE:?SANDBOX_CADDYFILE required}"
    RPC_CONF="${SANDBOX_RPC_CONF:?SANDBOX_RPC_CONF required}"
    LOCK_DIR="${SANDBOX_ROOT}/lock"
    PROC_DIR="${SANDBOX_PROC_DIR:?SANDBOX_PROC_DIR required}"
    LEGACY_PORT="${SANDBOX_LEGACY_PORT:-18999}"
    CADDY_DOMAIN="${SANDBOX_CADDY_DOMAIN:-sandbox.mazapay.invalid}"
    RUN_USER="${SANDBOX_RUN_USER:-mazapay-sbx}"
    TREE_SRC="${SANDBOX_TREE_SRC:?SANDBOX_TREE_SRC required}"
    UNIT_SRC="${SANDBOX_UNIT_SRC:?SANDBOX_UNIT_SRC required}"
    mkdir -p "$LOCK_DIR"
fi

if [ -z "${SANDBOX_ROOT:-}" ]; then
    UNIT_SRC="${SCRIPT_DIR}/../mazapay.service"
    [ -f "${SCRIPT_DIR}/mazapay.service" ] && UNIT_SRC="${SCRIPT_DIR}/mazapay.service"
    TREE_SRC="${SCRIPT_DIR}/../../mazapay"          # repo package dir
    [ -d "${SCRIPT_DIR}/mazapay" ] && TREE_SRC="${SCRIPT_DIR}/mazapay"
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
    find xargs grep sort diff awk sed cut wc useradd kill

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
[ -f "$TREE_SRC/api.py" ] && [ -d "$TREE_SRC/templates" ] \
    || fail 3 "source tree incomplete: $TREE_SRC (need api.py + templates/)"

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

# Port 8443 must be free — or held by OUR OWN unit on a rerun.
port_line="$(ss -tln 2>/dev/null | grep -E "127\.0\.0\.1:${LISTEN_PORT}\b" || true)"
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
if grep -q "BEGIN mazapay-bridge managed block" "$CADDYFILE" 2>/dev/null; then
    CADDY_BLOCK_OWNER=ours
elif [ -f "$CADDYFILE" ]; then
    CADDY_BLOCK_OWNER=absent
else
    fail 3 "caddyfile missing: $CADDYFILE"
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
CANDIDATE="${SCRATCH}/Caddyfile.candidate"
cp "$CADDYFILE" "$CANDIDATE" || fail 2 "caddyfile copy failed"
if [ "$CADDY_BLOCK_OWNER" = "ours" ]; then
    python3 - "$CANDIDATE" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(
    r"# BEGIN mazapay-bridge managed block.*?# END mazapay-bridge managed block\n?",
    "", text, flags=re.S)
open(path, "w").write(text)
PY
    log "caddy: replaced existing managed block"
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
	reverse_proxy ${LISTEN_HOST}:${LISTEN_PORT}
}
# END mazapay-bridge managed block
EOF
if ! caddy validate --adapter caddyfile --config "$CANDIDATE" >/dev/null 2>&1; then
    fail 2 "candidate Caddyfile failed validation — live file untouched"
fi
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
