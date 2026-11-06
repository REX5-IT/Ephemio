#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${EPHEMIO_SSH_HOST:-ubuntu@137.74.12.96}"
[[ "$REMOTE" =~ ^[a-zA-Z0-9_.@-]+$ && "$REMOTE" != -* ]] || exit 2
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$REMOTE")
case "${1:-}" in
    --dry-run) echo "Would build and validate Ubuntu KDE + Wine on $REMOTE, back up config, activate its immutable ID, and restart Ephemio services."; exit 0 ;;
    '') ;;
    *) echo 'Usage: ./deploy-image.sh [--dry-run]' >&2; exit 2 ;;
esac
stage=$("${SSH[@]}" 'mktemp -d /tmp/ephemio-image.XXXXXXXX')
[[ "$stage" =~ ^/tmp/ephemio-image\.[a-zA-Z0-9]+$ ]] || exit 1
trap '"${SSH[@]}" "sudo -n rm -rf -- $stage"' EXIT
rsync -az -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' "$ROOT/runtime/images/ubuntu-kde/" "$REMOTE:$stage/base/"
rsync -az -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' "$ROOT/runtime/images/common/" "$REMOTE:$stage/common/"
"${SSH[@]}" "sudo -n bash -s -- $stage" <<'REMOTE'
set -Eeuo pipefail
docker build -t ephemio/ubuntu-kde:wine-base "$1/base"
docker build --build-arg BASE_IMAGE=ephemio/ubuntu-kde:wine-base -t ephemio/ubuntu-kde:wine "$1/common"
image=$(docker image inspect ephemio/ubuntu-kde:wine --format '{{.Id}}')
docker run --rm --network none --entrypoint /bin/sh "$image" -ec '
    /usr/bin/python3 -c "from PyQt5 import QtWidgets"
    test -f /etc/xdg/autostart/ephemio-status.desktop
    wine --version
    command -v winecfg
    desktop-file-validate /usr/share/applications/ephemio-wine.desktop
    desktop-file-validate /usr/share/applications/ephemio-wine-settings.desktop
    test -x /usr/local/bin/ephemio-wine
'
backup=$(mktemp /etc/ephemio/config.before-wine.XXXXXXXX)
cp -p /etc/ephemio/config.json "$backup"
restart() {
    systemctl restart ephemio-worker
    systemctl start ephemio-gateway ephemio-sync
    sleep 3
    systemctl is-active --quiet ephemio-worker ephemio-gateway ephemio-sync
}
rollback() {
    trap - ERR
    cp -p "$backup" /etc/ephemio/config.json
    restart || true
    exit 1
}
trap rollback ERR
python3 - "$image" <<'PY'
import json, os, sys
p='/etc/ephemio/config.json'
with open(p) as f: c=json.load(f)
profiles=c.setdefault('profiles', {'ubuntu-kde': {'image': c.get('image', ''), 'process': 'plasmashell'}})
profiles['ubuntu-kde']['image']=sys.argv[1]
with open(p, 'w') as f:
    json.dump(c, f, indent=2)
    f.write('\n')
    f.flush()
    os.fsync(f.fileno())
PY
restart
trap - ERR
echo "Wine image activated: $image; config backup: $backup. Start a NEW KDE session."
REMOTE
