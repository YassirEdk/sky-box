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

    def _agent(self, kwargs):
        """Retourne (utilisateur du jeton, None) ou (None, erreur)."""
        token = request.env['skybox.access.token']._validate(
            self._bearer_token(kwargs))
        if not token:
            return None, {'success': False, 'error': 'invalid_token'}
        user = token.user_id
        if not self._user_role(user):
            return None, {'success': False, 'error': 'access_denied'}
        return user, None

    def _agent_env(self, user):
        """Environnement "agent" : sudo pour les droits, mais env.user = agent
        (createur des enregistrements + agent dans l'historique)."""
        return request.env(user=user.id, su=True)

    def _find_bag(self, env, kwargs):
        """Bag (obligatoire) par id ("bag_id") ou par nom ("bag").
        Retourne (bag, None) ou (None, erreur)."""
        Package = env['stock.quant.package']
        bag_id = kwargs.get('bag_id')
        bag_name = (kwargs.get('bag') or '').strip()
        if not bag_id and not bag_name:
            return None, {'success': False, 'error': 'missing_bag'}
        if bag_id:
            bag = Package.browse(int(bag_id)).exists()
        else:
            bag = Package.search([('name', '=ilike', bag_name)], limit=1)
        if not bag:
            return None, {'success': False, 'error': 'bag_not_found'}
        return bag, None

    def _find_location(self, env, kwargs):
        """Emplacement interne (obligatoire) par id ("location_id") ou par
        code-barres / nom complet / nom ("location").
        Retourne (emplacement, None) ou (None, erreur)."""
        Location = env['stock.location']
        loc_id = kwargs.get('location_id')
        loc_name = (kwargs.get('location') or '').strip()
        if not loc_id and not loc_name:
            return None, {'success': False, 'error': 'missing_location'}
        internal = [('usage', '=', 'internal')]
        if loc_id:
            location = Location.search(
                internal + [('id', '=', int(loc_id))], limit=1)
        else:
            location = Location.browse()
            for field in ('barcode', 'complete_name', 'name'):
                location = Location.search(
                    internal + [(field, '=ilike', loc_name)], limit=1)
                if location:
                    break
        if not location:
            return None, {'success': False, 'error': 'location_not_found'}
        return location, None

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
        user, error = self._agent(kwargs)
        if error:
            return error

        tracking_number = (kwargs.get('tracking_number') or '').strip()
        if not tracking_number:
            return {'success': False, 'error': 'missing_tracking_number'}

        env = self._agent_env(user)
        Picking = env['stock.picking']

        bag, error = self._find_bag(env, kwargs)
        if error:
            return error

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

    @http.route('/skybox/api/store', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_store(self, **kwargs):
        """Stocke les commandes d'un bag dans un emplacement (app mobile).

        En-tete: Authorization: Bearer <access_token>
        Corps JSON attendu:
            {"tracking_number": "...", "bag": "BAG001",
             "location": "WH/Stock/Zone A1"}
        La commande (encore dans le bag, statut Receipt) passe en Stored
        dans l'emplacement ; enregistree dans l'historique.
        """
        user, error = self._agent(kwargs)
        if error:
            return error

        tracking_number = (kwargs.get('tracking_number') or '').strip()
        if not tracking_number:
            return {'success': False, 'error': 'missing_tracking_number'}

        env = self._agent_env(user)
        bag, error = self._find_bag(env, kwargs)
        if error:
            return error
        location, error = self._find_location(env, kwargs)
        if error:
            return error

        pickings = env['stock.picking'].search([
            ('picking_type_code', '=', 'incoming'),
            ('bag', '=', bag.id),
            ('skybox_status', '=', 'receipt'),
            ('tracking_number', '=ilike', tracking_number),
        ])
        if not pickings:
            return {'success': False, 'error': 'order_not_in_bag'}

        pickings.write({
            'location_dest_id': location.id,
            'skybox_status': 'stored',
        })
        pickings._log_history(
            'stored', location=location, bag=bag,
            note="Stocke via l'app mobile")
        return {
            'success': True,
            'bag': bag.name,
            'bag_id': bag.id,
            'location': location.complete_name,
            'location_id': location.id,
            'agent': user.name,
            'agent_id': user.id,
            'date': fields.Datetime.to_string(fields.Datetime.now()),
            'count': len(pickings),
            'orders': [{
                'picking_id': p.id,
                'reference': p.name,
                'tracking_number': p.tracking_number,
                'client': p.client_id.name or None,
            } for p in pickings],
        }
