#!/usr/bin/env bash
# Configuration Odoo : durcissement + tunnel HTTPS (Cloudflare) + Inventaire.
# Usage : ./setup_inventory.sh
# Necessite sudo pour : durcissement conf, ufw, installation de cloudflared.
set -euo pipefail

# ============ PARAMETRES ============
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ODOO_BIN="/opt/odoo/odoo-server/odoo-bin"
VENV_PY="/opt/odoo/venv/bin/python3"
CONF="/opt/odoo/odoo.conf"
DB="Skybox"
SCRIPT="${SCRIPT_DIR}/setup_inventory.py"
TUNNEL_LOG="${SCRIPT_DIR}/cloudflared.log"
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

echo ">> [1/4] Durcissement d'Odoo (${CONF})..."
sudo sed -i -E "s|^\s*xmlrpc_port\s*=.*|http_port = 8069|" "$CONF" || true
if grep -qE "^\s*admin_passwd\s*=\s*admin\s*$" "$CONF"; then
    NEW_MASTER="$($VENV_PY -c 'import secrets;print(secrets.token_urlsafe(24))')"
    set_conf admin_passwd "$NEW_MASTER"
    echo "   !!! NOUVEAU MOT DE PASSE MAITRE : ${NEW_MASTER}"
    echo "   !!! Notez-le, il ne sera plus affiche."
fi
set_conf proxy_mode "True"
set_conf http_interface "127.0.0.1"
set_conf http_port "8069"
set_conf gevent_port "8072"
set_conf list_db "False"
set_conf dbfilter "^${DB}\$"

echo ">> [2/4] Pare-feu (ufw)..."
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 22/tcp   >/dev/null 2>&1 || true
    sudo ufw deny 8069/tcp  >/dev/null 2>&1 || true
    sudo ufw deny 8072/tcp  >/dev/null 2>&1 || true
    sudo ufw --force enable >/dev/null 2>&1 || true
    echo "   SSH ouvert, 8069/8072 fermes (le tunnel sort en sortant)."
else
    echo "   ufw non installe, etape ignoree."
fi

echo ">> [3/4] Application de la config Inventaire sur '${DB}'..."
"${ODOO_BIN}" shell -c "${CONF}" -d "${DB}" --no-http < "${SCRIPT}"

echo ">> [4/4] Tunnel HTTPS Cloudflare..."
# Installer cloudflared si absent
if ! command -v cloudflared >/dev/null 2>&1; then
    echo "   Telechargement de cloudflared..."
    curl -L -s \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
        -o /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
    sudo chmod +x /usr/local/bin/cloudflared
else
    echo "   cloudflared deja installe."
fi

# Arreter un ancien tunnel eventuel puis lancer en arriere-plan
pkill -f "cloudflared tunnel --url" >/dev/null 2>&1 || true
sleep 1
: > "$TUNNEL_LOG"
nohup cloudflared tunnel --url http://localhost:8069 > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

echo "   Demarrage du tunnel (patientez)..."
URL=""
for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 1
done

echo ""
echo "==================================================================="
if [ -n "$URL" ]; then
    echo ">> TOUT EST BON."
    echo ">> URL HTTPS publique : ${URL}"
    echo ">> Endpoint login     : ${URL}/skybox/api/login"
    echo ">> Tunnel en arriere-plan (PID ${TUNNEL_PID}) - log : ${TUNNEL_LOG}"
    echo ">> ATTENTION : URL temporaire (change si le tunnel redemarre)."
    echo ">>             Pour une URL fixe, utiliser un domaine + named tunnel."

    # --- Genere api_login.txt avec le lien reel du tunnel ---
    DOC="${SCRIPT_DIR}/api_login.txt"
    cat > "$DOC" <<EOF
===========================================================================
 SKYBOX - API D'AUTHENTIFICATION (mobile / programme externe)
===========================================================================
Genere le : $(date '+%Y-%m-%d %H:%M:%S')

URL HTTPS (tunnel Cloudflare, TEMPORAIRE - change si le tunnel redemarre) :
  ${URL}

Cle API "Login" : Odoo > Configuration > API management (type "Login").
Routes JSON-RPC : corps sous "params", reponse sous "result".

---------------------------------------------------------------------------
1) LOGIN  ->  POST /skybox/api/login
---------------------------------------------------------------------------
REQUETE :
curl -X POST ${URL}/skybox/api/login \\
  -H "Content-Type: application/json" \\
  -d '{"params":{"api_key":"VOTRE_CLE_LOGIN","email":"admin","password":"MOT_DE_PASSE"}}'

REPONSE (succes) :
{
  "result": {
    "success": true,
    "access_token": "abc123...",
    "expiry": "2026-09-25 11:00:00",
    "uid": 2,
    "name": "Mitchell Admin",
    "login": "admin",
    "email": "admin@example.com",
    "role": "admin"
  }
}
  role = "admin" | "manager" | "agent" | null

REPONSES (echecs) :
  {"result":{"success":false,"error":"invalid_api_key"}}
  {"result":{"success":false,"error":"missing_credentials"}}
  {"result":{"success":false,"error":"invalid_credentials"}}
  {"result":{"success":false,"error":"too_many_attempts","retry_after":900}}

---------------------------------------------------------------------------
2) UTILISATEUR COURANT  ->  POST /skybox/api/me
---------------------------------------------------------------------------
Prolonge la validite du jeton (glissante, 1 jour).

REQUETE :
curl -X POST ${URL}/skybox/api/me \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer VOTRE_ACCESS_TOKEN" \\
  -d '{"params":{}}'

REPONSE (succes) :
{
  "result": {
    "success": true,
    "uid": 2, "name": "Mitchell Admin",
    "login": "admin", "email": "admin@example.com",
    "role": "admin", "expiry": "2026-09-25 12:30:00"
  }
}
REPONSE (echec) : {"result":{"success":false,"error":"invalid_token"}}

---------------------------------------------------------------------------
3) LOGOUT  ->  POST /skybox/api/logout
---------------------------------------------------------------------------
curl -X POST ${URL}/skybox/api/logout \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer VOTRE_ACCESS_TOKEN" \\
  -d '{"params":{}}'

REPONSE : {"result":{"success":true}}

---------------------------------------------------------------------------
NOTES
---------------------------------------------------------------------------
- Jeton a duree de vie glissante de 1 jour (prolongee a chaque /me).
- "email" accepte l'email OU le login de l'utilisateur Odoo.
- Seuls les utilisateurs crees dans Odoo peuvent se connecter.
- Blocage anti-brute-force : 5 echecs -> 15 min (par IP+login).
- Stockez le jeton dans le stockage securise du mobile (Keychain/Keystore).
- URL temporaire : pour la production, domaine + named tunnel Cloudflare.
===========================================================================
EOF
    echo ">> Documentation generee : ${DOC}"
else
    echo ">> ECHEC : le tunnel n'a pas fourni d'URL. Voir : ${TUNNEL_LOG}"
fi
echo ">> Arreter le tunnel : pkill -f 'cloudflared tunnel'"
echo ">> IMPORTANT : redemarrez Odoo pour appliquer le durcissement :"
echo "     ${ODOO_BIN} -c ${CONF} -d ${DB} -u skybox"
echo "==================================================================="
