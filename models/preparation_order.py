# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from .skybox_event import parse_skybox_datetime, to_float, to_int

_logger = logging.getLogger(__name__)

# Etapes de preparation, dans l'ordre (guide v2, section 11). On peut sauter
# des etapes, jamais revenir en arriere.
STEPS = [
    ('new', "Nouveau", None),
    ('picking', "Picking", 'PICKING'),
    ('picked', "Picked", 'PICKED'),
    ('packed', "Packed", 'PACKED'),
    ('ready', "Pret a expedier", 'READY_FOR_DISPATCH'),
    ('dispatched', "Expedie", 'DISPATCHED'),
]
STEP_RANK = {key: rank for rank, (key, _label, _code) in enumerate(STEPS)}
STEP_CODE = {key: code for key, _label, code in STEPS if code}

STATUS_PATH = "/v1/integrations/wms/preparation-orders/%s/status"

# Dernier jeton servi dans le tourniquet des pickers.
PICKER_CURSOR = 'skybox.picker_cursor'
# Un picker sans activite depuis ce delai sort du cycle.
PICKER_IDLE_MINUTES = 5
# Delai avant redistribution des ordres d'un picker sorti du cycle.
REASSIGN_DELAY_PARAM = 'skybox.picker_reassign_delay'
DEFAULT_REASSIGN_DELAY = 60
# Ordres encore "a faire", redistribues quand leur picker sort du cycle.
REASSIGN_STATES = ('new', 'picking', 'picked', 'packed', 'ready')
# Ordres qu'on peut deplacer pour equilibrer la charge (colis pas encore
# tous preleves). Les Nouveau partent en premier, puis les Picking.
BALANCE_STATES = ('new', 'picking')


class SkyboxPreparationOrder(models.Model):
    """Ordre de preparation envoye par Skybox (PREPARATION_ORDER)."""
    _name = 'skybox.preparation.order'
    _description = "Ordre de preparation Skybox"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string="Ordre Skybox", required=True, index=True, readonly=True,
        copy=False)
    reference = fields.Char(
        string="Reference Odoo", readonly=True, copy=False, index=True,
        help="partnerOrderRef renvoye a Skybox.")
    order_type = fields.Char(string="Type", readonly=True)
    priority = fields.Char(string="Priorite", readonly=True)
    state = fields.Selection(
        selection=[(key, label) for key, label, _code in STEPS]
        + [('cancelled', "Annule")],
        string="Statut", default='new', required=True, tracking=True,
        group_expand='_expand_states',
    )
    warehouse_code = fields.Char(string="Code entrepot", readonly=True)
    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse', string="Entrepot")

    skybill_number = fields.Char(string="Skybill", readonly=True)
    skybill_status = fields.Char(string="Statut Skybill", readonly=True, tracking=True)
    paid_at = fields.Datetime(string="Paye le", readonly=True)

    suite_number = fields.Char(string="Suite du client", readonly=True)
    partner_id = fields.Many2one(comodel_name='res.partner', string="Client")
    agent_id = fields.Many2one(
        comodel_name='res.users', string="Picker", tracking=True, index=True,
        help="Picker connecte a l'app mobile, affecte a tour de role.")

    ship_to_name = fields.Char(string="Destinataire")
    ship_to_phone = fields.Char(string="Telephone")
    ship_to_address = fields.Char(string="Adresse")
    ship_to_city = fields.Char(string="Ville")
    ship_to_country_id = fields.Many2one(
        comodel_name='res.country', string="Pays")

    total_parcels = fields.Integer(string="Nombre de colis", readonly=True)
    total_weight = fields.Float(string="Poids total (kg)", readonly=True, digits=(12, 3))
    instructions = fields.Text(string="Instructions", readonly=True)
    requested_by = fields.Char(string="Demande par", readonly=True)
    requested_at = fields.Datetime(string="Demande le", readonly=True)

    cancel_reason = fields.Text(string="Motif d'annulation", readonly=True, tracking=True)
    cancelled_at = fields.Datetime(string="Annule le", readonly=True)
    last_event_id = fields.Char(string="Dernier eventId", readonly=True)

    # --- Carton final (PACKED) ---
    package_code = fields.Char(string="Code carton")

    # --- Expedition (DISPATCHED) ---
    carrier = fields.Char(string="Transporteur", tracking=True)
    carrier_tracking_number = fields.Char(
        string="N° de suivi transporteur", tracking=True)

    # --- Exception (EXCEPTION) ---
    has_exception = fields.Boolean(string="Exception en cours", readonly=True)
    exception_code = fields.Char(string="Code exception")
    exception_message = fields.Char(string="Message exception")

    # --- Synchronisation avec Skybox (appel d'avancement) ---
    sync_state = fields.Selection(
        selection=[
            ('none', "Rien a envoyer"),
            ('pending', "A envoyer"),
            ('sent', "Envoye"),
            ('error', "Erreur"),
        ],
        string="Envoi Skybox", default='none', readonly=True, tracking=True,
    )
    pending_status = fields.Char(string="Etape a envoyer", readonly=True)
    last_sent_status = fields.Char(string="Derniere etape envoyee", readonly=True)
    sync_message = fields.Char(string="Reponse Skybox", readonly=True)
    sync_date = fields.Datetime(string="Dernier envoi", readonly=True)

    line_ids = fields.One2many(
        comodel_name='skybox.preparation.order.line', inverse_name='order_id',
        string="Colis a preparer",
    )
    event_ids = fields.One2many(
        comodel_name='skybox.event', inverse_name='preparation_order_id',
        string="Evenements",
    )

    _sql_constraints = [
        ('name_uniq', 'unique(name)', "Cet ordre de preparation existe deja."),
    ]

    @api.model
    def _expand_states(self, states, domain):
        # Les ordres expedies sont dans "Past orders", pas dans le kanban.
        return [key for key, _ in self._fields['state'].selection
                if key != 'dispatched']

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('reference'):
                vals['reference'] = self.env['ir.sequence'].next_by_code(
                    'skybox.preparation.order') or vals.get('name')
            if not vals.get('agent_id') and vals.get('state', 'new') != 'cancelled':
                vals['agent_id'] = self._next_picker().id
        return super().create(vals_list)

    @api.model
    def _picker_cycle(self):
        """Jetons du cycle des pickers, 1 par picker, dans l'ordre de connexion.

        Un picker deconnecte (jeton revoque/expire) ou inactif depuis
        PICKER_IDLE_MINUTES sort du cycle ; un nouveau connecte entre a la fin.
        Seuls les Pickers comptent (pas les Agents/Managers/Admins)."""
        picker = self.env.ref('skybox.group_skybox_picker', raise_if_not_found=False)
        agent = self.env.ref('skybox.group_skybox_agent', raise_if_not_found=False)
        if not picker:
            return []
        now = fields.Datetime.now()
        tokens = self.env['skybox.access.token'].sudo().search([
            ('active', '=', True),
            ('expiry', '>', now),
            ('last_used', '>', now - timedelta(minutes=PICKER_IDLE_MINUTES)),
            ('user_id.active', '=', True),
            ('user_id.groups_id', 'in', picker.id),
        ], order='id')
        cycle, seen = [], set()
        for token in tokens:
            user = token.user_id
            if user.id in seen or (agent and agent in user.groups_id):
                continue
            seen.add(user.id)
            cycle.append(token)
        return cycle

    @api.model
    def _next_picker(self, cycle=None):
        """Tourniquet : premier jeton apres le dernier servi, puis retour au
        debut. Retourne le picker (res.users), vide si personne n'est connecte."""
        if cycle is None:
            cycle = self._picker_cycle()
        if not cycle:
            return self.env['res.users']
        params = self.env['ir.config_parameter'].sudo()
        cursor = int(params.get_param(PICKER_CURSOR, 0) or 0)
        token = next((t for t in cycle if t.id > cursor), cycle[0])
        params.set_param(PICKER_CURSOR, token.id)
        return token.user_id

    @api.model
    def _open_orders_domain(self):
        return [('state', 'in', REASSIGN_STATES)]

    @api.model
    def _reassign_delay(self):
        """Delai (minutes) avant de redistribuer les ordres d'un picker sorti
        du cycle (Parametres > Skybox). 0 = immediatement."""
        value = self.env['ir.config_parameter'].sudo().get_param(
            REASSIGN_DELAY_PARAM, DEFAULT_REASSIGN_DELAY)
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return DEFAULT_REASSIGN_DELAY

    @api.model
    def _picker_reassign_at(self, users):
        """Date a partir de laquelle les ordres de chaque picker sorti du
        cycle seront redistribues : {user_id: datetime}. Le delai compte a
        partir du moment ou il a quitte le cycle (deconnexion / inactivite)."""
        delay = timedelta(minutes=self._reassign_delay())
        return {user.id: user.skybox_left_cycle_at + delay
                for user in users.sudo() if user.skybox_left_cycle_at}

    @api.model
    def _rebalance_pickers(self):
        """Redistribue a tour de role :
        - les ordres sans picker (tout de suite),
        - les ordres des pickers sortis du cycle depuis plus que le delai
          configure (Parametres > Skybox).
        Ne fait rien si aucun picker n'est connecte (les ordres restent)."""
        cycle = self._picker_cycle()
        if not cycle:
            return
        connected = [t.user_id.id for t in cycle]
        # Revenus dans le cycle (activite reprise sans nouvelle connexion) :
        # le delai en cours est annule.
        back = self.env['res.users'].sudo().browse(connected).filtered('skybox_left_cycle_at')
        if back:
            back.write({'skybox_left_cycle_at': False})
        orders = self.sudo().search(self._open_orders_domain() + [
            '|', ('agent_id', '=', False), ('agent_id', 'not in', connected),
        ], order='id')
        now = fields.Datetime.now()
        # Picker absent sans date de sortie (inactif, ou ordres affectes a la
        # main) : le delai demarre maintenant.
        absent = orders.agent_id.sudo().filtered(lambda u: not u.skybox_left_cycle_at)
        if absent:
            absent.write({'skybox_left_cycle_at': now})
        reassign_at = self._picker_reassign_at(orders.agent_id)
        moved = {}
        for order in orders:
            old = order.agent_id
            if old and reassign_at.get(old.id, now) > now:
                continue  # Delai pas encore ecoule : il peut encore revenir.
            new = self._next_picker(cycle)
            order.agent_id = new
            if old:
                moved.setdefault(old, []).append((order, new))
        Log = self.env['skybox.picker.log'].sudo()
        for old, items in moved.items():
            Log._log(old, 'reassigned', self.browse([o.id for o, _n in items]),
                     note=self._moved_note(items))
        self._even_out_pickers(cycle)

    @api.model
    def _even_out_pickers(self, cycle):
        """Equilibre la charge entre les pickers connectes : tant qu'un picker
        a au moins 2 ordres ouverts de plus qu'un autre, on lui retire un
        ordre Nouveau (sinon Picking), le plus recent, pour le donner au
        moins charge. Les ordres Picked / Packed / Pret ne bougent pas.
        Ex. : 5 ordres en attente, A se connecte -> A a 5 ; B se connecte ->
        A 3, B 2 ; C se connecte -> A 2, B 2, C 1."""
        if len(cycle) < 2:
            return
        users = [t.user_id for t in cycle]
        orders = self.sudo().search(self._open_orders_domain() + [
            ('agent_id', 'in', [u.id for u in users])], order='id')
        load = {u.id: orders.filtered(lambda o, u=u: o.agent_id == u) for u in users}
        moved = {}
        while True:
            # Ordre du cycle en cas d'egalite : le premier connecte recoit.
            low = min(users, key=lambda u: len(load[u.id]))
            high = max(users, key=lambda u: len(load[u.id]))
            if len(load[high.id]) - len(load[low.id]) < 2:
                break
            movable = (load[high.id].filtered(lambda o: o.state == 'new')
                       or load[high.id].filtered(lambda o: o.state in BALANCE_STATES))
            if not movable:
                # Ce picker n'a que des ordres avances : on ne les touche pas.
                users.remove(high)
                if len(users) < 2:
                    break
                continue
            order = movable[-1]
            order.agent_id = low
            load[high.id] -= order
            load[low.id] |= order
            moved.setdefault(high, []).append((order, low))
        Log = self.env['skybox.picker.log'].sudo()
        for old, items in moved.items():
            Log._log(old, 'balanced', self.browse([o.id for o, _n in items]),
                     note="Reequilibrage : " + ", ".join(
                         "%s -> %s" % (o.name, n.name) for o, n in items))

    @api.model
    def _moved_note(self, items):
        return "Redistribues : " + ", ".join(
            "%s -> %s" % (order.name, new.name) for order, new in items)

    @api.model
    def _picker_logout(self, user):
        """Deconnexion d'un picker : journal ; ses ordres sont redistribues
        apres le delai configure s'il ne revient pas."""
        Log = self.env['skybox.picker.log'].sudo()
        orders = self.sudo().search(
            self._open_orders_domain() + [('agent_id', '=', user.id)], order='id')
        if user.id in [t.user_id.id for t in self._picker_cycle()]:
            # Encore connecte sur un autre appareil : il garde ses ordres.
            Log._log(user, 'logout', orders,
                     note="Encore connecte sur un autre appareil, ordres conserves.")
            return
        user.sudo().skybox_left_cycle_at = fields.Datetime.now()
        if not orders:
            Log._log(user, 'logout', orders)
            return
        delay = self._reassign_delay()
        if delay:
            when = fields.Datetime.context_timestamp(
                self, fields.Datetime.now() + timedelta(minutes=delay))
            note = ("Deconnecte avec %s ordre(s) actif(s) : redistribues aux "
                    "autres pickers a partir de %s (delai %s min) s'il ne se "
                    "reconnecte pas." % (len(orders), when.strftime('%d/%m/%Y %H:%M'), delay))
        else:
            note = "Deconnecte avec %s ordre(s) actif(s) : redistribution immediate." % len(orders)
        Log._log(user, 'logout', orders, note=note)
        if not delay:
            self._rebalance_pickers()

    @api.model
    def _picker_login(self, user):
        """Connexion d'un picker : journal + reprise des ordres en attente."""
        # De retour dans le cycle : il garde ses ordres, le delai est annule.
        user.sudo().skybox_left_cycle_at = False
        self.env['skybox.picker.log'].sudo()._log(user, 'login')
        self._rebalance_pickers()

    @api.model
    def _cron_rebalance_pickers(self):
        self._rebalance_pickers()

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state') == 'dispatched':
            self._mark_past()
        return res

    # ------------------------------------------------------------------
    # Past orders : ordres expedies + leurs colis retires des autres menus
    # ------------------------------------------------------------------
    def _tracking_numbers(self):
        return set(filter(None, self.line_ids.mapped('tracking_number')
                          + self.line_ids.parcel_id.mapped('tracking_number')))

    def _related_parcels(self):
        tns = list(self._tracking_numbers())
        return self.line_ids.parcel_id | self.env['skybox.parcel'].sudo().search(
            [('tracking_number', 'in', tns)])

    def _related_pickings(self, parcels):
        tns = list(self._tracking_numbers())
        return parcels.picking_id | self.env['stock.picking'].sudo().search([
            ('picking_type_code', '=', 'incoming'),
            ('tracking_number', 'in', tns),
        ])

    def _related_prealertes(self, parcels):
        tns = list(self._tracking_numbers())
        return parcels.prealerte_id | self.env['skybox.prealerte'].sudo().search(
            [('tracking_number', 'in', tns)])

    def _mark_past(self):
        """Retire les colis de l'ordre expedie de tous les autres menus."""
        if not self:
            return
        parcels = self._related_parcels()
        parcels.write({'skybox_past': True})
        self._related_pickings(parcels).write({'skybox_past': True})
        self._related_prealertes(parcels).write({'skybox_past': True})

    @api.model
    def _init_past(self):
        """Mise a jour du module : ordres deja expedies -> Past orders."""
        self.sudo().search([('state', '=', 'dispatched')])._mark_past()

    def action_history(self):
        """Bouton Historique sur un ordre : pop-up du colis s'il n'y en a
        qu'un, sinon la liste de ses colis (chacun avec son Historique)."""
        self.ensure_one()
        tns = sorted(self._tracking_numbers())
        if len(tns) <= 1:
            return self._history_popup(tns[0] if tns else False)
        action = self.env['ir.actions.act_window']._for_xml_id(
            'skybox.action_skybox_past_order')
        action['domain'] = [('order_id', '=', self.id)]
        action['name'] = "Colis de %s" % self.name
        return action

    def _history_popup(self, tracking_number):
        """Pop-up : historique d'UN colis de l'ordre."""
        self.ensure_one()
        wizard = self.env['skybox.colis.history'].create({
            'order_id': self.id,
            'tracking_number': tracking_number,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': "Historique du colis %s" % (tracking_number or self.name),
            'res_model': 'skybox.colis.history',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _history_rows(self, tracking_number=None):
        """Lignes de l'historique, triees par date. Avec `tracking_number` :
        uniquement ce colis + les etapes de l'ordre (communes a ses colis)."""
        rows = []
        parcels = self._related_parcels()

        def add(date, tn, step, detail='', user='', kind='order'):
            if date:
                rows.append({'date': date, 'tracking_number': tn or False,
                             'step': step, 'detail': detail or '',
                             'user': user or '', 'kind': kind})

        # Prealertes
        for pre in self._related_prealertes(parcels):
            add(pre.create_date, pre.tracking_number, "Prealerte",
                "Prealerte %s creee" % pre.name, pre.create_uid.name, 'prealerte')
        # Reception / stockage / transferts (historique des receptions)
        pickings = self._related_pickings(parcels)
        actions = dict(self.env['skybox.picking.history']._fields['action'].selection)
        for h in self.env['skybox.picking.history'].sudo().search(
                [('picking_id', 'in', pickings.ids)]):
            # Lignes automatiques des evenements Skybox (deja "Colis traite").
            if (h.note or '').startswith('Skybox '):
                continue
            detail = " / ".join(filter(None, [
                h.picking_id.name,
                h.location_id.complete_name and "Emplacement : %s" % h.location_id.complete_name,
                h.bag_id.name and "Bag : %s" % h.bag_id.name,
                h.note]))
            add(h.date, h.picking_id.tracking_number,
                "Reception - %s" % actions.get(h.action, h.action), detail, h.user_id.name,
                'reception')
        # Traitement Skybox du colis
        for parcel in parcels:
            add(parcel.processed_at, parcel.tracking_number, "Colis traite",
                " / ".join(filter(None, [parcel.package_condition, parcel.result])),
                parcel.processed_by, 'parcel')
            add(parcel.skybill_date, parcel.tracking_number, "Skybill",
                "Skybill %s" % (parcel.skybill_number or ''), '', 'parcel')
            add(parcel.decided_at, parcel.tracking_number, "Decision",
                " / ".join(filter(None, [parcel.decision_kind, parcel.decision_note])),
                parcel.decided_by, 'parcel')
        # Ordre de preparation : reception + chaque changement de statut
        add(self.requested_at or self.create_date, False, "Ordre de preparation",
            "Ordre %s recu (%s colis)" % (self.name, self.total_parcels),
            self.requested_by)
        states = dict(self._fields['state'].selection)
        messages = self.env['mail.message'].sudo().search([
            ('model', '=', self._name), ('res_id', '=', self.id)])
        for msg in messages:
            for tv in msg.sudo().tracking_value_ids:
                if tv.field_id.name == 'state':
                    add(msg.date, False, "Statut : %s" % (tv.new_value_char or ''),
                        "%s -> %s" % (tv.old_value_char or '', tv.new_value_char or ''),
                        msg.author_id.name)
                elif tv.field_id.name == 'agent_id':
                    add(msg.date, False, "Picker",
                        "%s -> %s" % (tv.old_value_char or '-', tv.new_value_char or '-'),
                        msg.author_id.name)
        if self.state == 'dispatched' and self.carrier_tracking_number:
            add(self.write_date, False, "Expedie",
                "%s - suivi %s" % (self.carrier or '', self.carrier_tracking_number),
                '', 'done')
        if tracking_number:
            rows = [r for r in rows if r['tracking_number'] in (False, tracking_number)]
        return sorted(rows, key=lambda r: r['date'])

    @api.model
    def _init_references(self):
        for order in self.search([('reference', '=', False)], order='id'):
            order.reference = self.env['ir.sequence'].next_by_code(
                'skybox.preparation.order') or order.name

    # ------------------------------------------------------------------
    # Evenements Skybox -> Odoo
    # ------------------------------------------------------------------
    @api.model
    def _apply_event(self, data, event):
        Parcel = self.env['skybox.parcel']
        Package = self.env['stock.quant.package']
        Location = self.env['stock.location']
        ship_to = data.get('shipTo') or {}
        totals = data.get('totals') or {}

        partner = self.env['skybox.prealerte']._find_or_create_partner(
            data.get('customer') or {})
        country = self.env['res.country']
        if ship_to.get('countryCode'):
            country = country.search(
                [('code', '=ilike', ship_to['countryCode'])], limit=1)

        lines = []
        for item in data.get('parcels') or []:
            if not isinstance(item, dict):
                continue
            loc = item.get('currentLocation') or {}
            dims = item.get('dimensionsCm') or {}
            tracking = item.get('trackingNumber') or False
            location = Location.browse()
            if loc.get('locationCode'):
                location = Location.search([
                    ('usage', '=', 'internal'),
                    '|', ('barcode', '=ilike', loc['locationCode']),
                    ('name', '=ilike', loc['locationCode']),
                ], limit=1)
            lines.append((0, 0, {
                'tracking_number': tracking,
                'parcel_id': Parcel.search(
                    [('tracking_number', '=ilike', tracking)], limit=1).id
                if tracking else False,
                'skybill_number': item.get('skybillNumber') or False,
                'current_bag_code': item.get('currentBagCode') or False,
                'bag_id': Package._skybox_find_or_create(
                    item.get('currentBagCode'), create=False).id,
                'zone_code': loc.get('zoneCode') or False,
                'aisle_code': loc.get('aisleCode') or False,
                'rack_code': loc.get('rackCode') or False,
                'location_code': loc.get('locationCode') or False,
                'location_id': location.id,
                'weight': to_float(item.get('weightKg')),
                'length_cm': to_float(dims.get('length')),
                'width_cm': to_float(dims.get('width')),
                'height_cm': to_float(dims.get('height')),
                'fragile': bool(item.get('fragile')),
            }))

        return self.create({
            'name': data['orderId'],
            'order_type': data.get('orderType') or False,
            'priority': data.get('priority') or False,
            'warehouse_code': data.get('warehouseCode') or False,
            'warehouse_id': Parcel._warehouse(data.get('warehouseCode')).id,
            'skybill_number': data.get('skybillNumber') or False,
            'skybill_status': data.get('skybillStatus') or False,
            'paid_at': parse_skybox_datetime(data.get('paidAt')),
            'suite_number': data.get('suiteNumber') or False,
            'partner_id': partner.id if partner else False,
            'ship_to_name': ship_to.get('name') or False,
            'ship_to_phone': ship_to.get('phone') or False,
            'ship_to_address': ship_to.get('addressLine') or False,
            'ship_to_city': ship_to.get('city') or False,
            'ship_to_country_id': country.id,
            'total_parcels': to_int(totals.get('parcelCount')),
            'total_weight': to_float(totals.get('weightKg')),
            'instructions': data.get('instructions') or False,
            'requested_by': data.get('requestedBy') or False,
            'requested_at': parse_skybox_datetime(data.get('requestedAt')),
            'last_event_id': event.event_id,
            'line_ids': lines,
        })

    @api.model
    def _apply_cancel(self, data, event):
        order = self.search([('name', '=', data['orderId'])], limit=1)
        vals = {
            'state': 'cancelled',
            'cancel_reason': data.get('reason') or False,
            'cancelled_at': fields.Datetime.now(),
            'last_event_id': event.event_id,
            'sync_state': 'none',
            'pending_status': False,
        }
        if order:
            order.write(vals)
        else:
            # Annulation recue avant l'ordre : on garde une trace annulee.
            order = self.create(dict(vals, name=data['orderId']))
        return order

    # ------------------------------------------------------------------
    # Avancement Odoo -> Skybox (guide v2, section 11)
    # ------------------------------------------------------------------
    def _check_open(self):
        for order in self:
            if order.state == 'cancelled':
                raise UserError("L'ordre %s est annule." % order.name)
            if order.state == 'dispatched':
                raise UserError("L'ordre %s est deja expedie." % order.name)

    def _go_to(self, step):
        """Passe a l'etape `step` (jamais en arriere) et l'envoie a Skybox."""
        self._check_open()
        for order in self:
            if STEP_RANK[step] <= STEP_RANK[order.state]:
                raise UserError(
                    "Retour en arriere impossible : l'ordre %s est deja %s."
                    % (order.name, dict(order._fields['state'].selection)[order.state]))
            order.write({
                'state': step,
                'has_exception': False,
                'pending_status': STEP_CODE[step],
                'sync_state': 'pending',
            })
            order._send_status()
        return self._sync_notification()

    def action_picking(self):
        return self._go_to('picking')

    def action_picked(self):
        for order in self:
            order.line_ids.filtered(lambda l: not l.pick_state).write(
                {'pick_state': 'PICKED'})
        return self._go_to('picked')

    def action_packed(self):
        for order in self:
            if not order.package_code:
                order.package_code = order.reference
        return self._go_to('packed')

    def action_ready(self):
        return self._go_to('ready')

    def action_dispatched(self):
        for order in self:
            if order.state != 'ready':
                raise UserError(
                    "L'ordre %s doit etre Pret a expedier avant d'etre "
                    "expedie." % order.name)
            if not order.carrier or not order.carrier_tracking_number:
                raise UserError(
                    "Renseignez le transporteur et le numero de suivi "
                    "transporteur avant d'expedier : il devient le numero de "
                    "suivi du client.")
        return self._go_to('dispatched')

    def action_exception(self):
        self._check_open()
        for order in self:
            if not order.exception_code or not order.exception_message:
                raise UserError(
                    "Renseignez le code et le message de l'exception.")
            order.write({
                'has_exception': True,
                'pending_status': 'EXCEPTION',
                'sync_state': 'pending',
            })
            order._send_status()
        return self._sync_notification()

    def action_resend(self):
        for order in self.filtered('pending_status'):
            order._send_status()
        return self._sync_notification()

    def _sync_notification(self):
        """Resultat de l'envoi en notification discrete (bus), sans action
        client : le bouton renvoie True, le formulaire se met a jour sur
        place. Le detail reste visible sur la fiche (Envoi Skybox)."""
        for order in self:
            ok = order.sync_state == 'sent'
            self.env.user._bus_send('simple_notification', {
                'title': "Skybox" if ok else "Envoi Skybox",
                'message': "%s : %s" % (order.name, order.sync_message or ''),
                'type': 'success' if ok else 'warning',
                'sticky': False,
            })
        return True

    def _status_payload(self, status):
        self.ensure_one()
        payload = {
            'status': status,
            'partnerOrderRef': self.reference,
            'operator': self.env.user.name,
            'occurredAt': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        if status != 'PICKING' and any(self.line_ids.mapped('pick_state')):
            payload['parcels'] = [
                {'trackingNumber': line.tracking_number,
                 'state': line.pick_state or 'PICKED'}
                for line in self.line_ids if line.tracking_number]
        if status in ('PACKED', 'READY_FOR_DISPATCH', 'DISPATCHED') and self.package_code:
            payload['package'] = {'packageCode': self.package_code}
        if status == 'DISPATCHED':
            payload['dispatch'] = {
                'carrier': self.carrier,
                'carrierTrackingNumber': self.carrier_tracking_number,
            }
        if status == 'EXCEPTION':
            payload['exception'] = {'reasonCode': self.exception_code,
                                    'message': self.exception_message}
        return payload

    def _send_status(self):
        """POST {URL Skybox}/v1/integrations/wms/preparation-orders/{orderId}/status
        Erreur passagere (reseau, 401, 5xx) : reste 'A envoyer', le cron renvoie.
        Erreur definitive (400, 404, 409) : passe en Erreur."""
        self.ensure_one()
        status = self.pending_status
        if not status:
            return
        key, base = self.env['skybox.api.key']._skybox_wms_base()
        now = fields.Datetime.now()
        if not key:
            self.write({'sync_message': "Aucune API externe Skybox configuree "
                                        "(Configuration > API management).",
                        'sync_date': now})
            return
        auth_headers, params = key._build_request()
        headers = {'Content-Type': 'application/json'}
        headers.update(auth_headers)
        url = base + STATUS_PATH % self.name
        try:
            response = requests.post(url, json=self._status_payload(status),
                                     headers=headers, params=params, timeout=15)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Skybox status %s %s : %s", self.name, status, exc)
            self.write({'sync_message': "Skybox injoignable, nouvel essai "
                                        "automatique: %s" % exc,
                        'sync_date': now})
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = response.status_code
        message = (body.get('message') or '') if isinstance(body, dict) else ''
        if 200 <= code < 300:
            self.write({
                'sync_state': 'sent', 'pending_status': False,
                'last_sent_status': status, 'sync_date': now,
                'sync_message': "%s accepte par Skybox%s." % (
                    status, " (deja recu)" if body.get('duplicate') else ''),
            })
            return
        err = body.get('code') if isinstance(body, dict) else None
        if code == 409 and err == 'ORDER_CANCELLED':
            self.write({
                'state': 'cancelled', 'sync_state': 'error',
                'pending_status': False, 'sync_date': now,
                'cancelled_at': now,
                'sync_message': "Ordre annule cote Skybox : arreter la "
                                "preparation. %s" % message,
            })
            return
        if code in (400, 404, 409):
            extra = (" (etat Skybox: %s)" % body.get('current')
                     if isinstance(body, dict) and body.get('current') else '')
            self.write({
                'sync_state': 'error', 'pending_status': False, 'sync_date': now,
                'sync_message': "HTTP %s %s : %s%s" % (code, err or '', message, extra),
            })
            return
        # 401 / 5xx : passager, on garde l'etape a envoyer.
        self.write({
            'sync_date': now,
            'sync_message': "HTTP %s %s, nouvel essai automatique." % (
                code, message or response.reason),
        })

    @api.model
    def _cron_resend_status(self):
        """Renvoie les etapes pas encore acceptees par Skybox."""
        for order in self.search([('sync_state', '=', 'pending'),
                                  ('pending_status', '!=', False)]):
            order._send_status()


class SkyboxPreparationOrderLine(models.Model):
    _name = 'skybox.preparation.order.line'
    _description = "Colis de l'ordre de preparation"

    order_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Ordre",
        required=True, ondelete='cascade', index=True,
    )
    tracking_number = fields.Char(string="Numero de tracking")
    parcel_id = fields.Many2one(comodel_name='skybox.parcel', string="Colis")
    order_state = fields.Selection(related='order_id.state', string="Statut ordre")
    order_partner_id = fields.Many2one(related='order_id.partner_id', string="Client")
    order_agent_id = fields.Many2one(related='order_id.agent_id', string="Picker")
    order_carrier = fields.Char(related='order_id.carrier', string="Transporteur")
    order_carrier_tracking = fields.Char(
        related='order_id.carrier_tracking_number', string="Suivi transporteur")
    order_dispatched_at = fields.Datetime(
        related='order_id.write_date', string="Expedie le")
    skybill_number = fields.Char(string="Skybill")
    current_bag_code = fields.Char(string="Bag actuel")
    bag_id = fields.Many2one(comodel_name='stock.quant.package', string="Bag (Odoo)")
    zone_code = fields.Char(string="Zone")
    aisle_code = fields.Char(string="Allee")
    rack_code = fields.Char(string="Rack")
    location_code = fields.Char(string="Emplacement")
    location_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement (Odoo)")
    weight = fields.Float(string="Poids (kg)", digits=(12, 3))
    length_cm = fields.Float(string="Longueur (cm)")
    width_cm = fields.Float(string="Largeur (cm)")
    height_cm = fields.Float(string="Hauteur (cm)")
    fragile = fields.Boolean(string="Fragile")
    pick_state = fields.Selection(
        selection=[
            ('PICKED', "Preleve"),
            ('MISSING', "Manquant"),
            ('DAMAGED', "Abime"),
        ],
        string="Etat au picking",
    )

    def action_history(self):
        self.ensure_one()
        return self.order_id._history_popup(
            self.tracking_number or self.parcel_id.tracking_number)
