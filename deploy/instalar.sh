#!/usr/bin/env bash
# Instala o actualiza pricelabs-sftp en el server. Correr con sudo; se puede repetir.
# NO prende el timer: eso se hace a mano, después de apagar el de la laptop.
set -euo pipefail

REPO=https://github.com/laferre26/ProcesarPrecios.git
BASE=/opt/pricelabs-sftp
USUARIO=pricelabs

[ "$(id -u)" -eq 0 ] || { echo "Correr con sudo" >&2; exit 1; }

# Sin preguntas de needrestart y sin reiniciar servicios del server
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
apt-get update -qq
apt-get install -y -qq git python3-venv

id "$USUARIO" >/dev/null 2>&1 ||
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin "$USUARIO"
install -d -o "$USUARIO" -g "$USUARIO" -m 750 "$BASE"
install -d -o "$USUARIO" -g "$USUARIO" -m 700 "$BASE/.ssh" "$BASE/.config" "$BASE/.config/pricelabs-sftp"

como() { runuser -u "$USUARIO" -- "$@"; }

if [ -d "$BASE/app/.git" ]; then
    como git -C "$BASE/app" pull --ff-only
else
    como git clone -q "$REPO" "$BASE/app"
fi
[ -d "$BASE/.venv" ] || como python3 -m venv "$BASE/.venv"
como "$BASE/.venv/bin/pip" install -q -r "$BASE/app/requirements.txt"

install -m 644 "$BASE/app/deploy/pricelabs-sftp.service" "$BASE/app/deploy/pricelabs-sftp.timer" /etc/systemd/system/
systemctl daemon-reload

CONFIG="$BASE/.config/pricelabs-sftp/config.ini"
HOSTS="$BASE/.ssh/known_hosts"
[ -f "$CONFIG" ] || echo "FALTA: $CONFIG (dueño $USUARIO, permisos 600)"
[ -f "$HOSTS" ]  || echo "FALTA: $HOSTS (dueño $USUARIO, con la llave del SFTP)"
echo "Instalado en $BASE. El timer sigue apagado."
