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

    def _receipt_picking_type(self, user):
        """Type d'operation Reception de l'entrepot de l'agent."""
        env = request.env
        warehouse = user.warehouse_id
        if not warehouse:
            employee = env['hr.employee'].sudo().search(
                [('user_id', '=', user.id)], limit=1)
            warehouse = employee.warehouse_id
        if warehouse and warehouse.in_type_id:
            return warehouse.in_type_id
        return env['stock.picking.type'].sudo().search([
            ('code', '=', 'incoming'),
            ('company_id', 'in', user.company_ids.ids),
        ], limit=1)

    @http.route('/skybox/api/receive', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_receive(self, **kwargs):
        """Cree une reception depuis l'app mobile.

        En-tete: Authorization: Bearer <access_token>
        Corps JSON attendu:
            {"tracking_number": "...", "bag": "BAG001",
             "courier": "DHL" (optionnel), "notes": "..." (optionnel)}
        "bag" accepte le nom du bag ou son id ("bag_id").
        L'agent est l'utilisateur du jeton ; la reception et le bag sont
        enregistres dans l'historique.
        """
        token = request.env['skybox.access.token']._validate(
            self._bearer_token(kwargs))
        if not token:
            return {'success': False, 'error': 'invalid_token'}
        user = token.user_id
        if not self._user_role(user):
            return {'success': False, 'error': 'access_denied'}

        tracking_number = (kwargs.get('tracking_number') or '').strip()
        if not tracking_number:
            return {'success': False, 'error': 'missing_tracking_number'}

        # Environnement "agent" : sudo pour les droits, mais env.user = agent
        # (createur de la reception + agent dans l'historique).
        env = request.env(user=user.id, su=True)
        Picking = env['stock.picking']

        # Bag (obligatoire) : par id ou par nom.
        Package = env['stock.quant.package']
        bag = Package.browse()
        bag_id = kwargs.get('bag_id')
        bag_name = (kwargs.get('bag') or '').strip()
        if bag_id:
            bag = Package.browse(int(bag_id)).exists()
        elif bag_name:
            bag = Package.search([('name', '=ilike', bag_name)], limit=1)
        if not bag_id and not bag_name:
            return {'success': False, 'error': 'missing_bag'}
        if not bag:
            return {'success': False, 'error': 'bag_not_found'}

        # Doublon : tracking deja recu (non annule).
        existing = Picking.search([
            ('picking_type_code', '=', 'incoming'),
            ('tracking_number', '=ilike', tracking_number),
            ('state', '!=', 'cancel'),
        ], limit=1)
        if existing:
            return {'success': False, 'error': 'already_received',
                    'picking_id': existing.id, 'reference': existing.name}

        picking_type = self._receipt_picking_type(user)
        if not picking_type:
            return {'success': False, 'error': 'no_receipt_type'}

        vals = {
            'picking_type_id': picking_type.id,
            'location_id': (picking_type.default_location_src_id.id
                            or env.ref('stock.stock_location_suppliers').id),
            'location_dest_id': picking_type.default_location_dest_id.id,
            'tracking_number': tracking_number,
            'bag': bag.id,
            'notes': kwargs.get('notes') or False,
            'company_id': picking_type.company_id.id,
        }
        if kwargs.get('courier'):
            vals['courier_id'] = Picking._get_courier(kwargs['courier']).id

        picking = Picking.with_context(
            skybox_history_note="Recu via l'app mobile").create(vals)
        history = picking.history_ids[:1]
        return {
            'success': True,
            'picking_id': picking.id,
            'reference': picking.name,
            'tracking_number': picking.tracking_number,
            'bag': bag.name,
            'bag_id': bag.id,
            'agent': user.name,
            'agent_id': user.id,
            'warehouse': picking_type.warehouse_id.name or None,
            'date': fields.Datetime.to_string(history.date),
        }
