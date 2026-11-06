#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${EPHEMIO_SSH_HOST:-ubuntu@137.74.12.96}"
dry_run=false
case "${1:-}" in
    --dry-run) dry_run=true ;;
    --help|-h) echo 'Usage: ./deploy.sh [--dry-run] (existing VPS host service; EPHEMIO_SSH_HOST overrides SSH destination)'; exit 0 ;;
    '') ;;
    *) echo 'Usage: ./deploy.sh [--dry-run]' >&2; exit 2 ;;
esac
[[ $# -le 1 && "$REMOTE" =~ ^[a-zA-Z0-9_.@-]+$ && "$REMOTE" != -* ]] || exit 2
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "$REMOTE")
for command in ssh rsync python3; do command -v "$command" >/dev/null; done
python3 - "$ROOT" <<'PY'
import ast, pathlib, sys
for p in (pathlib.Path(sys.argv[1]) / 'runtime/host').rglob('*.py'):
    ast.parse(p.read_text(), filename=str(p))
PY
"${SSH[@]}" 'sudo -n test -f /etc/ephemio/config.json && test -x /opt/ephemio-venv/bin/python && sudo -n systemctl is-active ephemio-worker ephemio-gateway ephemio-sync ephemio-sweep.timer'
if $dry_run; then
    rsync -azn --itemize-changes --rsync-path='sudo -n rsync' -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' \
        --include=/service.py --include=/tests/ --include='/tests/*.py' --exclude='*' "$ROOT/runtime/host/" "$REMOTE:/opt/ephemio/"
    echo 'Would test staged code, back up /opt/ephemio, update dependencies and restart Ephemio services. Config, images, firewall and Nginx stay under separate management.'
    exit 0
fi
stage="$("${SSH[@]}" 'mktemp -d /tmp/ephemio-deploy.XXXXXXXX')"
[[ "$stage" =~ ^/tmp/ephemio-deploy\.[a-zA-Z0-9]+$ ]] || exit 1
cleanup() {
    local result=$?
    trap - EXIT
    if ! "${SSH[@]}" "sudo -n rm -rf -- $stage"; then
        echo "Temporary deployment cleanup failed: $REMOTE:$stage" >&2
        if [[ $result -eq 0 ]]; then result=1; fi
    fi
    exit "$result"
}
trap cleanup EXIT
rsync -az --exclude=__pycache__/ --exclude='*.pyc' -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' "$ROOT/runtime/host/" "$REMOTE:$stage/"
"${SSH[@]}" "sudo -n bash -s -- $stage" <<'REMOTE'
set -Eeuo pipefail
stage=$1
# Test with isolated dependencies before modifying the running installation.
python3 -m venv "$stage/venv"
"$stage/venv/bin/pip" install --disable-pip-version-check -r "$stage/requirements.txt"
"$stage/venv/bin/python" "$stage/tests/test_security.py"
backup=$(mktemp -d /opt/ephemio-backup.XXXXXXXX)
cp -a /opt/ephemio "$backup/host"
cp -a /opt/ephemio-venv "$backup/venv"
restart() {
    systemctl restart ephemio-worker
    systemctl start ephemio-gateway ephemio-sync ephemio-sweep.timer
    systemctl is-active --quiet ephemio-worker ephemio-gateway ephemio-sync ephemio-sweep.timer
}
rollback() {
    trap - ERR
    echo "Deployment failed; restoring $backup" >&2
    cp -a "$backup/host/." /opt/ephemio/
    cp -a "$backup/venv/." /opt/ephemio-venv/
    restart || true
    exit 1
}
trap rollback ERR
/opt/ephemio-venv/bin/pip install --disable-pip-version-check -r "$stage/requirements.txt"
install -m 644 "$stage/service.py" /opt/ephemio/service.py
install -d /opt/ephemio/tests
install -m 644 "$stage/tests/"*.py /opt/ephemio/tests/
/opt/ephemio-venv/bin/python /opt/ephemio/tests/test_security.py
restart
sleep 3
systemctl is-active --quiet ephemio-worker ephemio-gateway ephemio-sync ephemio-sweep.timer
trap - ERR
echo "Host service deployed. Backup: $backup"
REMOTE
