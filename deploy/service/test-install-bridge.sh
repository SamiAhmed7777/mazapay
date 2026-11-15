#!/usr/bin/env bash
# Sandbox battery for deploy/service/install-bridge.sh.
# Every external effect (systemctl, caddy, curl, ss, useradd, sleep) is a
# PATH shim; every production path derives from SANDBOX_ROOT. Scenarios
# assert EXIT CODES and resulting sandbox state — never shim internals alone.
#
# BATTERY_RC is printed exactly once: 0 = all green, N = N failed assertions.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="${HERE}/install-bridge.sh"
REAL_UNIT="${HERE}/../mazapay.service"

FAIL=0
PASS=0
ck() { # ck <description> <command...>
    local d="$1"; shift
    if "$@" >/dev/null 2>&1; then
        PASS=$((PASS+1))
    else
        echo "  ASSERT-FAIL: $d"
        FAIL=$((FAIL+1))
    fi
}
file_has()  { grep -qF -- "$2" "$1"; }
file_eq()   { cmp -s "$1" "$2"; }
mode_is()   { [ "$(stat -c %a "$1")" = "$2" ]; }
link_to()   { [ "$(readlink "$1" 2>/dev/null)" = "$2" ]; }
rc_is()     { [ "$1" -eq "$2" ]; }
not_has()   { ! grep -qF -- "$2" "$1"; }

RPC_SECRET="SBXRPCSECRET-DO-NOT-LEAK-7f3a"   # fixture value for leak assertions

new_sbx() { # $1 = scenario name
    local s="$1"
    local sbx="/tmp/mzp-sbx-${s}.$$"
    rm -rf "$sbx"; mkdir -p "$sbx"
    mkdir -p "$sbx"/{shims,logs,opt,proc,lock,etc/systemd/system,etc/caddy,etc}
    # fixture source tree — REPO-ROOT shaped (the installer stages the root;
    # the unit resolves `python3 -m mazapay.api` from WorkingDirectory=release).
    # api.py carries a relative import so the import-resolution assertions
    # exercise real package mechanics, not just file presence.
    mkdir -p "$sbx/tree/mazapay/templates"
    printf 'from . import config\n\n\ndef placeholder():\n    return "sandbox tree"\n' > "$sbx/tree/mazapay/api.py"
    printf 'LISTEN_PORT = 8443\n' > "$sbx/tree/mazapay/config.py"
    printf '<html>sbx checkout</html>\n' > "$sbx/tree/mazapay/templates/checkout.html"
    printf '# repo root marker\n' > "$sbx/tree/README.md"
    # fixture rpc conf (secret values are fixtures)
    printf 'server=1\nrpcbind=127.0.0.1\nrpcport=12832\nrpcuser=sbxuser\nrpcpassword=%s\n' \
        "$RPC_SECRET" > "$sbx/rpc.conf"
    # fixture caddyfile
    printf 'import /etc/caddy/snippets.d/*.caddy\n\nsbxsites.example {\n\treverse_proxy 127.0.0.1:7000\n}\n' \
        > "$sbx/etc/caddy/Caddyfile"
    # fixture caddy-guard state files (S11 exercises registration; other
    # scenarios carry the paths so the registration code path is uniform)
    mkdir -p "$sbx/var/lib/caddy-guard"
    printf 'sbxsites.example {\n\treverse_proxy 127.0.0.1:7000\n}\n' \
        > "$sbx/var/lib/caddy-guard/protected-blocks.txt"
    printf '' > "$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard"
    # shims
    cat > "$sbx/shims/sleep" <<'EOF'
#!/bin/sh
[ "${SLEEP_FAST:-0}" = "1" ] && exit 0
exec /bin/sleep "$@"
EOF
    cat > "$sbx/shims/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >> "$SANDBOX_ROOT/logs/systemctl.log"
case "$1" in
    cat)   [ -f "$SANDBOX_ROOT/unit_installed" ] && exit 0 || exit 1 ;;
    is-active) exit 0 ;;
    restart) [ "${ROLLBACK_RESTART_FAIL:-0}" = "1" ] && exit 1 || exit 0 ;;
    *) exit 0 ;;
esac
EOF
    cat > "$sbx/shims/caddy" <<'EOF'
#!/bin/sh
sub="$1"; shift
case "$sub" in
    validate)
        c="$SANDBOX_ROOT/validate_calls"
        n=$(cat "$c" 2>/dev/null || echo 0); echo $((n+1)) > "$c"
        [ "${CADDY_VALIDATE_FAIL:-0}" = "1" ] && exit 1
        # fail only the ROLLBACK-time validation (call >= 2): candidate
        # validation (call 1) must pass so the deploy proceeds to reload
        if [ "${CADDY_VALIDATE_FAIL_ROLLBACK:-0}" = "1" ] && [ $((n+1)) -ge 2 ]; then exit 1; fi
        exit 0 ;;
    reload)
        n=$(cat "$SANDBOX_ROOT/caddy_reload_fails" 2>/dev/null || echo 0)
        if [ "$n" -gt 0 ]; then echo $((n-1)) > "$SANDBOX_ROOT/caddy_reload_fails"; exit 1; fi
        exit 0 ;;
    *) exit 0 ;;
esac
EOF
    cat > "$sbx/shims/curl" <<'EOF'
#!/bin/sh
out=""; status="${CURL_STATUS:-200}"
body='{"ok": true}'
[ -n "${CURL_BODY:-}" ] && body="$CURL_BODY"
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -w) shift 2 ;;
        *) shift ;;
    esac
done
printf '%s' "$body" > "$out"
printf '%s' "$status"
exit 0
EOF
    cat > "$sbx/shims/useradd" <<'EOF'
#!/bin/sh
echo "useradd $*" >> "$SANDBOX_ROOT/logs/useradd.log"
: > "$SANDBOX_ROOT/user_added"
exit 0
EOF
    cat > "$sbx/shims/ss" <<'EOF'
#!/bin/sh
[ "${SS_HOLD_LISTEN:-0}" = "1" ] && echo 'LISTEN 0 5 127.0.0.1:18500 0.0.0.0:*'
[ "${SS_HOLD_WILDCARD:-0}" = "1" ] && echo 'LISTEN 0 5 *:18500 0.0.0.0:*'
if [ "${SS_LEGACY:-0}" = "1" ]; then
    echo "LISTEN 0 5 127.0.0.1:${SS_LEGACY_PORT:-18999} 0.0.0.0:* users:((\"python3\",pid=${SS_PID:-4242},fd=3))"
fi
exit 0
EOF
    chmod +x "$sbx"/shims/*
    echo "$sbx"
}

run_install() { # $1 = sbx ; remaining args: extra VAR=val exported to the installer.
    # Runs in a SUBSHELL so scenario flags can't leak into the next scenario
    # (bash persists VAR=x fn prefixes after the call returns).
    local sbx="$1"; shift
    (
        export SANDBOX_ROOT="$sbx"
        export SANDBOX_OPT_DIR="$sbx/opt"
        export SANDBOX_UNIT_DST="$sbx/etc/systemd/system/mazapay.service"
        export SANDBOX_ENV_DST="$sbx/etc/mazapay.env"
        export SANDBOX_CADDYFILE="$sbx/etc/caddy/Caddyfile"
        export SANDBOX_GUARD_BLOCKS="$sbx/var/lib/caddy-guard/protected-blocks.txt"
        export SANDBOX_GUARD_PREGUARD="$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard"
        export SANDBOX_RPC_CONF="$sbx/rpc.conf"
        export SANDBOX_PROC_DIR="$sbx/proc"
        export SANDBOX_TREE_SRC="$sbx/tree"
        export SANDBOX_UNIT_SRC="$REAL_UNIT"
        export PATH="$sbx/shims:$PATH"
        export SLEEP_FAST=1
        [ "$#" -gt 0 ] && export "$@"
        bash "$INSTALLER"
    ) >"$sbx/logs/run.log" 2>&1
    echo $?
}

leak_check() { # $1 = sbx — the fixture rpc secret must appear NOWHERE in the run log
    local sbx="$1"
    ck "rpc secret never reaches the transcript" not_has "$sbx/logs/run.log" "$RPC_SECRET"
    if [ -f "$sbx/etc/mazapay.env" ]; then
        local tok
        tok="$(grep '^MAZAPAY_API_TOKEN=' "$sbx/etc/mazapay.env" | cut -d= -f2)"
        [ -n "$tok" ] && ck "generated token never reaches the transcript" not_has "$sbx/logs/run.log" "$tok"
    fi
}

echo "== S0: cross-file consistency — unit MAZAPAY_PORT == installer LISTEN_PORT"
uport="$(grep -oE '^Environment=MAZAPAY_PORT=[0-9]+' "$REAL_UNIT" | cut -d= -f3)"
iport="$(grep -oE '^LISTEN_PORT="[0-9]+"' "$INSTALLER" | tr -d '"' | cut -d= -f2)"
eq() { [ "$1" = "$2" ] && [ -n "$1" ]; }
ck "unit MAZAPAY_PORT ($uport) == installer LISTEN_PORT ($iport), both nonempty" eq "$uport" "$iport"

echo "== S1: fresh install, happy path"
sbx="$(new_sbx s1)"
rc="$(run_install "$sbx")"
ck "exit 0" rc_is "$rc" 0
ck "run log says DEPLOYED" file_has "$sbx/logs/run.log" "DEPLOYED"
ck "unit installed" file_has "$sbx/etc/systemd/system/mazapay.service" "ExecStart=/usr/bin/python3 -m mazapay.api"
ck "env is 0600" mode_is "$sbx/etc/mazapay.env" "600"
ck "env carries RPC URL" file_has "$sbx/etc/mazapay.env" "MAZAPAY_MAZAD_RPC=http://127.0.0.1:12832"
ck "token is 64 hex" grep -qE '^MAZAPAY_API_TOKEN=[0-9a-f]{64}$' "$sbx/etc/mazapay.env"
rel="$(readlink "$sbx/opt/current")"
ck "current symlink set" [ -n "$rel" ] && [ -d "$rel" ]
ck "release is repo-ROOT (mazapay package nested, not flattened)" [ -f "$rel/mazapay/api.py" ]
ck "release carries templates" [ -f "$rel/mazapay/templates/checkout.html" ]
modrc="$( cd "$rel" && python3 -B -c 'import mazapay.api' >/dev/null 2>&1; echo $?)"
ck "staged tree resolves 'import mazapay.api' from its root (unit ExecStart mechanics)" rc_is "$modrc" 0
ck "caddy block begins" file_has "$sbx/etc/caddy/Caddyfile" "# BEGIN mazapay-bridge managed block"
ck "caddy block ends" file_has "$sbx/etc/caddy/Caddyfile" "# END mazapay-bridge managed block"
ck "health blocked at edge" file_has "$sbx/etc/caddy/Caddyfile" "respond @healthcheck 403"
ck "admin routes off public vhost" file_has "$sbx/etc/caddy/Caddyfile" "@adminsurface path /vendors"
ck "reverse proxy target" file_has "$sbx/etc/caddy/Caddyfile" "reverse_proxy 127.0.0.1:18500"
ck "original caddy site kept" file_has "$sbx/etc/caddy/Caddyfile" "sbxsites.example"
ck "user shim ran" [ -f "$sbx/user_added" ]
ck "service started" grep -q "systemctl start mazapay.service" "$sbx/logs/systemctl.log"
ck "health returned 200+ok" file_has "$sbx/logs/run.log" "health OK"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S2: rerun preserves env file and token"
sbx="$(new_sbx s2)"
rc="$(run_install "$sbx")"; ck "first install exit 0" rc_is "$rc" 0
sha1="$(sha256sum "$sbx/etc/mazapay.env" | awk '{print $1}')"
tok1="$(grep '^MAZAPAY_API_TOKEN=' "$sbx/etc/mazapay.env")"
rc="$(run_install "$sbx")"
ck "rerun exit 0" rc_is "$rc" 0
ck "env byte-identical after rerun" [ "$(sha256sum "$sbx/etc/mazapay.env" | awk '{print $1}')" = "$sha1" ]
ck "token reused" [ "$(grep '^MAZAPAY_API_TOKEN=' "$sbx/etc/mazapay.env")" = "$tok1" ]
ck "rerun says PRESERVED" file_has "$sbx/logs/run.log" "PRESERVED"
ck "rerun says token_generated=no" file_has "$sbx/logs/run.log" "token_generated=no"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S3: upgrade, health fails, rollback restores prior release (exit 1)"
sbx="$(new_sbx s3)"
mkdir -p "$sbx/opt/releases/old"
printf 'OLD-UNIT-CONTENT\n' > "$sbx/etc/systemd/system/mazapay.service"
ln -s "$sbx/opt/releases/old" "$sbx/opt/current"
printf 'MAZAPAY_API_TOKEN=oldtoken\n' > "$sbx/etc/mazapay.env"
rc="$(run_install "$sbx" CURL_STATUS=503)"
ck "rollback exit code is 1" rc_is "$rc" 1
ck "current repointed to old release" link_to "$sbx/opt/current" "$sbx/opt/releases/old"
ck "unit restored byte-exact" file_has "$sbx/etc/systemd/system/mazapay.service" "OLD-UNIT-CONTENT"
ck "env preserved (oldtoken still there)" file_has "$sbx/etc/mazapay.env" "MAZAPAY_API_TOKEN=oldtoken"
ck "caddy restored without block" not_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge"
ck "rollback marker printed" file_has "$sbx/logs/run.log" "ROLLBACK-DONE"
ck "rollback restarted service" grep -q "systemctl restart mazapay.service" "$sbx/logs/systemctl.log"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S4: rollback itself fails (restart broken) -> exit 2 + FATAL"
sbx="$(new_sbx s4)"
mkdir -p "$sbx/opt/releases/old"; ln -s "$sbx/opt/releases/old" "$sbx/opt/current"
printf 'OLD-UNIT-CONTENT\n' > "$sbx/etc/systemd/system/mazapay.service"
printf 'MAZAPAY_API_TOKEN=oldtoken\n' > "$sbx/etc/mazapay.env"
rc="$(run_install "$sbx" CURL_STATUS=503 ROLLBACK_RESTART_FAIL=1)"
ck "exit code is 2" rc_is "$rc" 2
ck "FATAL line on transcript" file_has "$sbx/logs/run.log" "FATAL: ROLLBACK FAILED"
ck "current still restored (best effort)" link_to "$sbx/opt/current" "$sbx/opt/releases/old"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S5: corrupt source tree -> abort 3, nothing mutated"
sbx="$(new_sbx s5)"
printf 'this is ((( not python\n' >> "$sbx/tree/mazapay/api.py"
rc="$(run_install "$sbx")"
ck "exit code is 3" rc_is "$rc" 3
ck "no unit written" [ ! -e "$sbx/etc/systemd/system/mazapay.service" ]
ck "no env written" [ ! -e "$sbx/etc/mazapay.env" ]
ck "no caddy block" not_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge"
ck "no releases staged" [ -z "$(ls -A "$sbx/opt/releases" 2>/dev/null)" ]
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S6: caddy reload fails once -> file restored + reloaded (exit 1)"
sbx="$(new_sbx s6)"
printf '1' > "$sbx/caddy_reload_fails"
cp "$sbx/etc/caddy/Caddyfile" "$sbx/etc/caddy/Caddyfile.orig"
rc="$(run_install "$sbx")"
ck "rollback exit code is 1" rc_is "$rc" 1
ck "live caddyfile == original" file_eq "$sbx/etc/caddy/Caddyfile" "$sbx/etc/caddy/Caddyfile.orig"
ck "backup existed and was consumed" [ -f "$sbx/etc/caddy/Caddyfile.pre-mazapay" ]
ck "rollback marker printed" file_has "$sbx/logs/run.log" "ROLLBACK-DONE"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S6b: rollback-time caddy validation fails -> live file left untouched (exit 2)"
sbx="$(new_sbx s6b)"
printf '1' > "$sbx/caddy_reload_fails"
cp "$sbx/etc/caddy/Caddyfile" "$sbx/etc/caddy/Caddyfile.orig"
rc="$(run_install "$sbx" CADDY_VALIDATE_FAIL_ROLLBACK=1)"
ck "exit code is 2" rc_is "$rc" 2
ck "FATAL line on transcript" file_has "$sbx/logs/run.log" "FATAL: ROLLBACK FAILED"
ck "restore refused, live file untouched" file_has "$sbx/logs/run.log" "restore failed validation"
ck "live caddyfile still carries the failed block (not the unvalidated restore)" \
    file_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S7a: legacy nohup (ours) retired"
sbx="$(new_sbx s7a)"
mkdir -p "$sbx/proc/4242"
printf 'python3\0-c\0import logging; from mazapay.api import main\0' > "$sbx/proc/4242/cmdline"
rc="$(run_install "$sbx" SS_LEGACY=1 SS_PID=4242)"
ck "exit 0 with legacy retired" rc_is "$rc" 0
ck "retire line in log" file_has "$sbx/logs/run.log" "retiring legacy nohup instance pid=4242"
ck "deployed anyway" file_has "$sbx/logs/run.log" "DEPLOYED"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S7b: port held by FOREIGN process -> left alone, deploy proceeds"
sbx="$(new_sbx s7b)"
mkdir -p "$sbx/proc/5151"
printf 'nginx\0worker\0' > "$sbx/proc/5151/cmdline"
rc="$(run_install "$sbx" SS_LEGACY=1 SS_PID=5151)"
ck "exit 0" rc_is "$rc" 0
ck "warned, did not touch" file_has "$sbx/logs/run.log" "NON-mazapay process"
ck "no retire happened" not_has "$sbx/logs/run.log" "retiring legacy"
ck "deployed anyway" file_has "$sbx/logs/run.log" "DEPLOYED"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S8: candidate caddy validation fails -> clean rollback (exit 1)"
sbx="$(new_sbx s8)"
cp "$sbx/etc/caddy/Caddyfile" "$sbx/etc/caddy/Caddyfile.orig"
rc="$(run_install "$sbx" CADDY_VALIDATE_FAIL=1)"
ck "rollback exit code is 1" rc_is "$rc" 1
ck "caddyfile untouched" file_eq "$sbx/etc/caddy/Caddyfile" "$sbx/etc/caddy/Caddyfile.orig"
ck "no caddy backup created" [ ! -e "$sbx/etc/caddy/Caddyfile.pre-mazapay" ]
ck "fresh-install unit removed" [ ! -e "$sbx/etc/systemd/system/mazapay.service" ]
ck "staged release discarded" [ -z "$(ls -A "$sbx/opt/releases" 2>/dev/null)" ]
ck "no guard registration debris on abort" not_has "$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard" "reverse_proxy"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S9: foreign process holds the listen port (loopback) -> abort 3"
sbx="$(new_sbx s9)"
rc="$(run_install "$sbx" SS_HOLD_LISTEN=1)"
ck "exit code is 3" rc_is "$rc" 3
ck "no unit written" [ ! -e "$sbx/etc/systemd/system/mazapay.service" ]
ck "no caddy block" not_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S9b: WILDCARD holder on the listen port (binds loopback too) -> abort 3"
sbx="$(new_sbx s9b)"
rc="$(run_install "$sbx" SS_HOLD_WILDCARD=1)"
ck "exit code is 3" rc_is "$rc" 3
ck "no unit written" [ ! -e "$sbx/etc/systemd/system/mazapay.service" ]
ck "no caddy block" not_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S10: existing env + rpc conf mismatch -> env preserved on abort"
sbx="$(new_sbx s10)"
printf 'MAZAPAY_API_TOKEN=keepme\n' > "$sbx/etc/mazapay.env"
cp "$sbx/rpc.conf" "$sbx/rpc.conf.bak"
printf 'server=1\nrpcbind=127.0.0.1\nrpcuser=sbxuser\n' > "$sbx/rpc.conf"   # password missing
rc="$(run_install "$sbx")"
ck "exit code is 3" rc_is "$rc" 3
ck "existing env untouched" file_has "$sbx/etc/mazapay.env" "MAZAPAY_API_TOKEN=keepme"
ck "no releases staged" [ -z "$(ls -A "$sbx/opt/releases" 2>/dev/null)" ]
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S11: caddy-guard registration — block lands in BOTH state files, rerun idempotent"
sbx="$(new_sbx s11)"
rc="$(run_install "$sbx")"
ck "exit 0" rc_is "$rc" 0
ck "preguard carries the upstream line" file_has "$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard" "reverse_proxy 127.0.0.1:18500"
ck "merged carries the upstream line" file_has "$sbx/var/lib/caddy-guard/protected-blocks.txt" "reverse_proxy 127.0.0.1:18500"
ck "preguard label is the sandbox domain" file_has "$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard" "sandbox.mazapay.invalid {"
ck "registered BEFORE caddy swap (log order)" [ "$(grep -n 'vhost registered in guard state files' "$sbx/logs/run.log" | cut -d: -f1)" -lt "$(grep -n 'caddy: block installed' "$sbx/logs/run.log" | cut -d: -f1)" ]
rc2="$(run_install "$sbx")"
ck "rerun exit 0" rc_is "$rc2" 0
ck "preguard has exactly ONE block after rerun" [ "$(grep -c 'reverse_proxy 127.0.0.1:18500' "$sbx/var/lib/caddy-guard/protected-blocks.txt.pre-guard")" = "1" ]
ck "merged has exactly ONE block after rerun" [ "$(grep -c 'reverse_proxy 127.0.0.1:18500' "$sbx/var/lib/caddy-guard/protected-blocks.txt")" = "1" ]
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo "== S11b: no caddy-guard on host (dir absent) -> graceful skip, deploy proceeds"
sbx="$(new_sbx s11b)"
rm -rf "$sbx/var/lib/caddy-guard"
rc="$(run_install "$sbx")"
ck "exit 0 without guard" rc_is "$rc" 0
ck "skip line logged" file_has "$sbx/logs/run.log" "caddy-guard not present on this host"
ck "vhost block still installed" file_has "$sbx/etc/caddy/Caddyfile" "BEGIN mazapay-bridge managed block"
leak_check "$sbx"
[ "${KEEP_SBX:-0}" = "1" ] || rm -rf "$sbx"

echo
echo "assertions: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
    echo "BATTERY_RC=0"
    exit 0
else
    echo "BATTERY_RC=$FAIL (sandboxes kept under /tmp/mzp-sbx-* for inspection)"
    exit 1
fi
