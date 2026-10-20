#!/usr/bin/env bash
# Configuration Odoo : durcissement + acces reseau local + Inventaire.
# Usage : ./setup_inventory.sh
# Necessite sudo pour : durcissement conf, ufw.
set -euo pipefail

# ============ PARAMETRES ============
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ODOO_BIN="/opt/odoo/odoo-server/odoo-bin"
VENV_PY="/opt/odoo/venv/bin/python3"
CONF="/opt/odoo/odoo.conf"
DB="Skybox"
SCRIPT="${SCRIPT_DIR}/setup_inventory.py"
# ====================================

# --- Helper : definit ou remplace une cle dans odoo.conf (idempotent) ---
set_conf() {
    local key="$1" val="$2"
    if grep -qE "^\s*${key}\s*=" "$CONF"; then
        sudo sed -i -E "s|^\s*${key}\s*=.*|${key} = ${val}|" "$CONF"
    else
        sudo sed -i -E "0,/^\[options\]/s//[options]\n${key} = ${val}/" "$CONF"
    fi
    echo "   ${key} = ${val}"
}

# Caddy inutilisable derriere CGNAT : on le desactive s'il traine.
sudo systemctl disable --now caddy >/dev/null 2>&1 || true

echo ">> [1/3] Durcissement d'Odoo (${CONF})..."
sudo sed -i -E "s|^\s*xmlrpc_port\s*=.*|http_port = 8069|" "$CONF" || true
if grep -qE "^\s*admin_passwd\s*=\s*admin\s*$" "$CONF"; then
    NEW_MASTER="$($VENV_PY -c 'import secrets;print(secrets.token_urlsafe(24))')"
    set_conf admin_passwd "$NEW_MASTER"
    echo "   !!! NOUVEAU MOT DE PASSE MAITRE : ${NEW_MASTER}"
    echo "   !!! Notez-le, il ne sera plus affiche."
fi
# Pas de proxy devant Odoo : proxy_mode laisserait un client falsifier son IP
# (X-Forwarded-For) et contourner le blocage anti-brute-force.
set_conf proxy_mode "False"
# Ecoute sur le reseau local (acces direct http://<ip>:8069).
set_conf http_interface "0.0.0.0"
set_conf http_port "8069"
set_conf gevent_port "8072"
set_conf list_db "False"
set_conf dbfilter "^${DB}\$"

echo ">> [2/3] Pare-feu (ufw)..."
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 22/tcp   >/dev/null 2>&1 || true
    sudo ufw delete deny 8069/tcp >/dev/null 2>&1 || true
    sudo ufw delete deny 8072/tcp >/dev/null 2>&1 || true
    sudo ufw allow 8069/tcp >/dev/null 2>&1 || true
    sudo ufw allow 8072/tcp >/dev/null 2>&1 || true
    sudo ufw --force enable >/dev/null 2>&1 || true
    echo "   SSH et Odoo (8069/8072) ouverts."
else
    echo "   ufw non installe, etape ignoree."
fi

echo ">> [3/3] Application de la config Inventaire sur '${DB}'..."
"${ODOO_BIN}" shell -c "${CONF}" -d "${DB}" --no-http < "${SCRIPT}"

# Ancien tunnel Cloudflare (retire) : on l'arrete s'il tourne encore.
pkill -f "cloudflared tunnel" >/dev/null 2>&1 || true

IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "==================================================================="
echo ">> TOUT EST BON."
echo ">> Odoo sur le reseau local : http://${IP}:8069"
echo ">> Endpoint login           : http://${IP}:8069/skybox/api/login"
echo ">> IMPORTANT : redemarrez Odoo pour appliquer la configuration :"
echo "     ${ODOO_BIN} -c ${CONF} -d ${DB} -u skybox"
echo "==================================================================="
