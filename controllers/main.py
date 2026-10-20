# -*- coding: utf-8 -*-
from odoo import fields, http
from odoo.exceptions import AccessDenied
from odoo.http import request


class SkyboxLoginController(http.Controller):

    def _user_role(self, user):
        """Retourne le role Skybox de l'utilisateur : admin, manager, agent, picker."""
        env = request.env
        for xmlid, role in (
            ('skybox.group_skybox_admin', 'admin'),
            ('skybox.group_skybox_manager', 'manager'),
            ('skybox.group_skybox_agent', 'agent'),
            ('skybox.group_skybox_picker', 'picker'),
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
        request.env['skybox.login.log']._log(
            user, 'login', 'mobile', note="Connexion depuis l'app")
        role = self._user_role(user)
        if role == 'picker':
            request.env['skybox.preparation.order'].sudo()._picker_login(user)
        return {
            'success': True,
            'access_token': token.token,
            'expiry': fields.Datetime.to_string(token.expiry),
            'uid': user.id,
            'name': user.name,
            'login': user.login,
            'email': user.email or None,
            'role': role,
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
        if rec and rec.active:
            rec.write({'active': False, 'last_used': fields.Datetime.now()})
            request.env['skybox.login.log']._log(
                rec.user_id, 'logout', 'mobile', note="Deconnexion depuis l'app")
            if self._user_role(rec.user_id) == 'picker':
                request.env['skybox.preparation.order'].sudo()._picker_logout(
                    rec.user_id)
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

    @http.route('/skybox/api/bags', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_bags(self, **kwargs):
        """Bags a ranger (app mobile) : bags dont des colis ont ete traites
        par Skybox (Processed) et ne sont pas encore stockes, avec leurs colis.
        L'app l'appelle a l'ouverture de l'ecran et toutes les X secondes ;
        chaque colis se range ensuite avec POST /skybox/api/store.

        En-tete: Authorization: Bearer <access_token>
        Corps JSON (optionnel): {"bag": "BAG001"} ou {"bag_id": 1}
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        env = self._agent_env(user)
        bag = None
        if kwargs.get('bag') or kwargs.get('bag_id'):
            try:
                bag, error = self._find_bag(env, kwargs)
            except ValueError:
                error = {'success': False, 'error': 'bag_not_found'}
            if error:
                return error
        bags = env['stock.quant.package']._skybox_bags_to_store(bag)
        return {
            'success': True,
            'count': len(bags),
            'parcels': sum(b['count'] for b in bags),
            'bags': bags,
        }

    @http.route('/skybox/api/store', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_store(self, **kwargs):
        """Range les colis d'un bag dans leurs emplacements (app mobile).

        L'app recupere les bags a ranger via POST /skybox/api/bags, puis
        renvoie ici chaque colis avec son emplacement.
        En-tete: Authorization: Bearer <access_token>  (-> agent de l'action)
        Corps JSON attendu:
            {"bag": "BAG001",
             "parcels": [{"tracking_number": "...", "location": "Zone A1"},
                         {"tracking_number": "...", "location_id": 18}]}
        (un seul colis : {"bag", "tracking_number", "location"} accepte)
        "location" = code-barres, nom complet ou nom de l'emplacement.
        Le colis (encore dans le bag, statut Receipt ou Processed par Skybox)
        passe en Stored ; enregistre dans l'historique avec l'agent.
        Seule cette route fait passer un colis en Stored.
        """
        user, error = self._agent(kwargs)
        if error:
            return error

        env = self._agent_env(user)
        bag, error = self._find_bag(env, kwargs)
        if error:
            return error

        parcels = kwargs.get('parcels')
        if parcels is None and kwargs.get('tracking_number'):
            parcels = [kwargs]
        if not isinstance(parcels, list) or not parcels:
            return {'success': False, 'error': 'missing_parcels'}

        results = []
        for item in parcels:
            item = item if isinstance(item, dict) else {}
            result = self._store_parcel(env, bag, item)
            result['tracking_number'] = (
                result.get('tracking_number')
                or (item.get('tracking_number') or '').strip() or None)
            results.append(result)

        # Recalcul de l'emplacement des colis traites (suivi dans le chatter)
        # au nom de l'agent, pas de l'utilisateur public de la requete.
        env.flush_all()
        stored = [r for r in results if r['success']]
        return {
            'success': len(stored) == len(results),
            'bag': bag.name,
            'bag_id': bag.id,
            'agent': user.name,
            'agent_id': user.id,
            'date': fields.Datetime.to_string(fields.Datetime.now()),
            'count': len(results),
            'stored': len(stored),
            'failed': len(results) - len(stored),
            'parcels': results,
        }

    def _store_parcel(self, env, bag, item):
        """Range un colis du bag ; retourne son resultat (dict)."""
        tracking_number = (item.get('tracking_number') or '').strip()
        if not tracking_number:
            return {'success': False, 'error': 'missing_tracking_number'}
        try:
            location, error = self._find_location(env, item)
        except ValueError:
            location, error = None, {'success': False,
                                     'error': 'location_not_found'}
        if error:
            return error

        pickings = env['stock.picking'].search([
            ('picking_type_code', '=', 'incoming'),
            ('bag', '=', bag.id),
            ('skybox_status', 'in', ('receipt', 'processed')),
            ('tracking_number', '=ilike', tracking_number),
        ])
        if not pickings:
            return {'success': False, 'error': 'order_not_in_bag'}

        pickings.write({
            'location_dest_id': location.id,
            'skybox_status': 'stored',
        })
        note = "Stocke via l'app mobile"
        pickings._log_history('stored', location=location, bag=bag, note=note)
        pickings._skybox_store_movement(location, bag=bag, note=note)
        return {
            'success': True,
            'tracking_number': pickings[0].tracking_number,
            'location': location.complete_name,
            'location_id': location.id,
            'orders': [{
                'picking_id': p.id,
                'reference': p.name,
                'client': p.client_id.name or None,
            } for p in pickings],
        }

    @http.route('/skybox/api/transfer', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_transfer(self, **kwargs):
        """Deplace des colis stockes d'un emplacement vers un autre (app
        mobile), comme le bouton Transferer de la reception.

        En-tete: Authorization: Bearer <access_token>  (-> agent de l'action)
        Corps JSON attendu:
            {"parcels": [
                {"tracking_number": "...", "location": "Zone B1",
                 "from_location": "Zone A2"},
                {"tracking_number": "...", "location_id": 19}]}
        (un seul colis : {"tracking_number", "location"} accepte)
        "location" / "location_id" = nouvel emplacement ;
        "from_location" / "from_location_id" (optionnel) = emplacement scanne
        par l'agent, verifie contre l'emplacement actuel du colis.
        Seuls les colis Stored se transferent ; mouvement LOCATION_TRANSFER
        envoye a Skybox et ligne d'historique avec l'agent.
        """
        user, error = self._agent(kwargs)
        if error:
            return error

        parcels = kwargs.get('parcels')
        if parcels is None and kwargs.get('tracking_number'):
            parcels = [kwargs]
        if not isinstance(parcels, list) or not parcels:
            return {'success': False, 'error': 'missing_parcels'}

        env = self._agent_env(user)
        results = []
        for item in parcels:
            item = item if isinstance(item, dict) else {}
            result = self._transfer_parcel(env, item)
            result['tracking_number'] = (
                result.get('tracking_number')
                or (item.get('tracking_number') or '').strip() or None)
            results.append(result)

        # Recalcul de l'emplacement des colis traites (suivi dans le chatter :
        # ancien -> nouvel emplacement) au nom de l'agent.
        env.flush_all()
        done = [r for r in results if r['success']]
        return {
            'success': len(done) == len(results),
            'agent': user.name,
            'agent_id': user.id,
            'date': fields.Datetime.to_string(fields.Datetime.now()),
            'count': len(results),
            'transferred': len(done),
            'failed': len(results) - len(done),
            'parcels': results,
        }

    def _transfer_parcel(self, env, item):
        """Transfere un colis stocke ; retourne son resultat (dict)."""
        tracking_number = (item.get('tracking_number') or '').strip()
        if not tracking_number:
            return {'success': False, 'error': 'missing_tracking_number'}
        try:
            location, error = self._find_location(env, item)
        except ValueError:
            location, error = None, {'success': False,
                                     'error': 'location_not_found'}
        if error:
            return error

        picking = env['stock.picking'].search([
            ('picking_type_code', '=', 'incoming'),
            ('tracking_number', '=ilike', tracking_number),
            ('state', '!=', 'cancel'),
        ], order='id desc', limit=1)
        if not picking:
            return {'success': False, 'error': 'parcel_not_found'}
        if picking.skybox_status != 'stored':
            return {'success': False, 'error': 'parcel_not_stored',
                    'status': picking.skybox_status or None}

        old_location = picking.location_dest_id
        if item.get('from_location') or item.get('from_location_id'):
            try:
                from_location, error = self._find_location(env, {
                    'location': item.get('from_location'),
                    'location_id': item.get('from_location_id'),
                })
            except ValueError:
                from_location, error = None, {'success': False,
                                              'error': 'location_not_found'}
            if error:
                return dict(error, error='from_location_not_found')
            if from_location != old_location:
                return {'success': False, 'error': 'wrong_from_location',
                        'current_location': old_location.complete_name or None,
                        'current_location_id': old_location.id or None}
        if location == old_location:
            return {'success': False, 'error': 'same_location',
                    'current_location': old_location.complete_name}

        picking.location_dest_id = location
        picking._skybox_movement(
            'LOCATION_TRANSFER', location._skybox_position(),
            old_location._skybox_position() if old_location else None)
        picking._log_history(
            'moved', location=location,
            note="De %s vers %s (app mobile)" % (
                old_location.display_name or '/', location.display_name))
        return {
            'success': True,
            'tracking_number': picking.tracking_number,
            'from_location': old_location.complete_name or None,
            'from_location_id': old_location.id or None,
            'location': location.complete_name,
            'location_id': location.id,
            'picking_id': picking.id,
            'reference': picking.name,
            'client': picking.client_id.name or None,
        }
