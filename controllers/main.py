# -*- coding: utf-8 -*-
from odoo import fields, http
from odoo.exceptions import AccessDenied
from odoo.http import request


class SkyboxLoginController(http.Controller):

    def _user_role(self, user):
        """Retourne le role Skybox de l'utilisateur : admin, manager, agent."""
        env = request.env
        for xmlid, role in (
            ('skybox.group_skybox_admin', 'admin'),
            ('skybox.group_skybox_manager', 'manager'),
            ('skybox.group_skybox_agent', 'agent'),
        ):
            grp = env.ref(xmlid, raise_if_not_found=False)
            if grp and user in grp.sudo().users:
                return role
        return None

    def _bearer_token(self, kwargs):
        """Recupere le jeton depuis l'en-tete Authorization ou le corps."""
        auth = request.httprequest.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            return auth[7:].strip()
        return kwargs.get('access_token')

    @http.route('/skybox/api/login', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_login(self, **kwargs):
        """Odoo comme authentificateur.

        Un programme externe envoie sa cle API (type Login) + les identifiants
        d'un utilisateur Odoo. Odoo verifie et renvoie le resultat.

        Corps JSON attendu:
            {"api_key": "...", "email": "...", "password": "..."}
        (l'email peut aussi etre passe sous la cle "login")
        La cle API peut aussi etre fournie dans l'en-tete X-API-KEY.
        """
        api_key = (kwargs.get('api_key')
                   or request.httprequest.headers.get('X-API-KEY'))
        login = kwargs.get('email') or kwargs.get('login')
        password = kwargs.get('password')

        # 1) Autoriser le programme appelant via une cle de type "login".
        key_rec = request.env['skybox.api.key'].sudo().search([
            ('api_type', '=', 'login'),
            ('active', '=', True),
            ('key', '=', api_key),
        ], limit=1)
        if not key_rec:
            return {'success': False, 'error': 'invalid_api_key'}

        key_rec.last_used = fields.Datetime.now()

        if not login or not password:
            return {'success': False, 'error': 'missing_credentials'}

        # 1bis) Anti-brute-force : blocage temporaire apres trop d'echecs.
        ip = request.httprequest.remote_addr or '0.0.0.0'
        attempt = request.env['skybox.login.attempt']
        retry_after = attempt._locked_seconds(ip, login)
        if retry_after:
            return {'success': False, 'error': 'too_many_attempts',
                    'retry_after': retry_after}

        # 2) Verifier les identifiants de l'utilisateur Odoo.
        db = request.env.cr.dbname
        Users = request.env['res.users']
        try:
            try:
                # Odoo 17.3+/18 : credential est un dict, retour = auth_info.
                result = Users.authenticate(
                    db,
                    {'type': 'password', 'login': login, 'password': password},
                    {'interactive': False},
                )
            except TypeError:
                # Ancienne signature (Odoo <= 17.2) : (db, login, password, env).
                result = Users.authenticate(
                    db, login, password, {'interactive': False})
            uid = result.get('uid') if isinstance(result, dict) else result
        except AccessDenied:
            uid = False
        except Exception:  # noqa: BLE001
            uid = False

        if not uid:
            attempt._register_failure(ip, login)
            return {'success': False, 'error': 'invalid_credentials'}

        attempt._register_success(ip, login)
        user = request.env['res.users'].sudo().browse(uid)
        token = request.env['skybox.access.token']._issue(user, key_rec)
        return {
            'success': True,
            'access_token': token.token,
            'expiry': fields.Datetime.to_string(token.expiry),
            'uid': user.id,
            'name': user.name,
            'login': user.login,
            'email': user.email or None,
            'role': self._user_role(user),
        }

    @http.route('/skybox/api/me', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_me(self, **kwargs):
        """Renvoie l'utilisateur correspondant au jeton d'acces.

        En-tete: Authorization: Bearer <access_token>
        (ou corps JSON: {"access_token": "..."})
        """
        token = request.env['skybox.access.token']._validate(
            self._bearer_token(kwargs))
        if not token:
            return {'success': False, 'error': 'invalid_token'}
        user = token.user_id
        return {
            'success': True,
            'uid': user.id,
            'name': user.name,
            'login': user.login,
            'email': user.email or None,
            'role': self._user_role(user),
            'expiry': fields.Datetime.to_string(token.expiry),
        }

    @http.route('/skybox/api/logout', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_logout(self, **kwargs):
        """Revoque le jeton d'acces (deconnexion)."""
        raw = self._bearer_token(kwargs)
        rec = request.env['skybox.access.token'].sudo().search(
            [('token', '=', raw)], limit=1)
        if rec:
            rec.active = False
        return {'success': True}
