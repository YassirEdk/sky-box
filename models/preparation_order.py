# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from .skybox_event import (
    SkyboxEventRejected, format_skybox_datetime, parse_skybox_datetime,
    to_float, to_int)

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
CANCEL_PATH = "/v1/integrations/wms/preparation-orders/%s/cancellation"

# Zone ou se trouvent les colis preleves, selon l'etape atteinte (retour en
# stock quand l'ordre est annule apres le picking).
STEP_ZONE = {
    'picking': 'PICKING',
    'picked': 'PICKING',
    'packed': 'PACKING',
    'ready': 'STAGING',
}
# Code de refus par defaut envoye a Skybox, selon l'etape atteinte.
REFUSE_CODE = {
    'picking': 'PICKING_IN_PROGRESS',
    'picked': 'ALREADY_PICKED',
    'packed': 'ALREADY_PACKED',
    'ready': 'ALREADY_PACKED',
}

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
    # Consolidation : la commande Skybox (parent, "commande groupee") est
    # decoupee en un ordre par colis (enfants) ; seul le parent dialogue
    # avec Skybox.
    is_group = fields.Boolean(
        string="Commande groupee", readonly=True, index=True,
        help="Ordre Skybox de consolidation, decoupe en un ordre par colis.")
    parent_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Commande groupee parente",
        readonly=True, index=True, ondelete='cascade')
    child_ids = fields.One2many(
        comodel_name='skybox.preparation.order', inverse_name='parent_id',
        string="Ordres du regroupement")
    child_count = fields.Integer(compute='_compute_child_count', string="Ordres")
    parent_skybill_number = fields.Char(
        related='parent_id.skybill_number', string="Skybill parent")
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

    # --- Demande d'annulation Skybox (PREPARATION_ORDER_CANCELLED) ---
    cancel_state = fields.Selection(
        selection=[
            ('requested', "Annulation demandee"),
            ('refused', "Annulation refusee"),
        ],
        string="Demande d'annulation", readonly=True, tracking=True, index=True,
        help="Annulation demandee : les etapes sont bloquees jusqu'a la "
             "decision de l'entrepot (Confirmer l'annulation / Refuser).")
    cancel_requested_by = fields.Char(string="Annulation demandee par", readonly=True)
    cancel_requested_at = fields.Datetime(string="Annulation demandee le", readonly=True)
    cancel_reached_step = fields.Char(
        string="Etape connue de Skybox", readonly=True,
        help="Derniere etape signalee a Skybox au moment de la demande.")
    cancel_decision = fields.Selection(
        selection=[('CANCELLED', "Annulation confirmee"), ('REFUSED', "Annulation refusee")],
        string="Decision a envoyer", readonly=True,
        help="Decision pas encore acceptee par Skybox : nouvel essai automatique.")
    cancel_refuse_message = fields.Char(
        string="Motif du refus",
        help="Explication envoyee a Skybox si l'annulation est refusee.")
    cancel_sync_message = fields.Char(string="Reponse Skybox (annulation)", readonly=True)
    cancel_blocked = fields.Boolean(
        string="Annulation en attente", compute='_compute_cancel_blocked',
        help="Cet ordre ou sa commande groupee attend une decision d'annulation.")
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

    # --- Affichage kanban ---
    picked_count = fields.Integer(string="Colis preleves", compute='_compute_kanban_info')
    pick_progress = fields.Integer(string="Avancement picking (%)", compute='_compute_kanban_info')
    has_fragile = fields.Boolean(string="Contient du fragile", compute='_compute_kanban_info')
    location_summary = fields.Char(string="Emplacements", compute='_compute_kanban_info')

    @api.depends('line_ids.pick_state', 'line_ids.fragile', 'line_ids.location_code')
    def _compute_kanban_info(self):
        for order in self:
            lines = order.line_ids
            total = len(lines) or order.total_parcels
            picked = len(lines.filtered('pick_state'))
            order.picked_count = picked
            order.pick_progress = int(picked * 100 / total) if total else 0
            order.has_fragile = any(lines.mapped('fragile'))
            # Bin Odoo du colis s'il est range, sinon la position Skybox.
            places = []
            for line in lines:
                place = line.wh_location_id.name or line.location_code or line.current_bag_code
                if place and place not in places:
                    places.append(place)
            order.location_summary = (", ".join(places) if len(places) <= 2
                                      else "%s emplacements" % len(places)) or False

    @api.depends('child_ids')
    def _compute_child_count(self):
        for order in self:
            order.child_count = len(order.child_ids)

    def action_open_group(self):
        """Ouvre la commande groupee (parent) ou ses ordres (enfants)."""
        self.ensure_one()
        if self.parent_id:
            return {'type': 'ir.actions.act_window', 'res_model': self._name,
                    'res_id': self.parent_id.id, 'view_mode': 'form'}
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'name': "Ordres de %s" % self.name, 'view_mode': 'list,form',
                'domain': [('parent_id', '=', self.id)]}

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
            if (not vals.get('agent_id') and not vals.get('is_group')
                    and vals.get('state', 'new') != 'cancelled'):
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
        # Les commandes groupees ne sont jamais affectees a un picker.
        return [('state', 'in', REASSIGN_STATES), ('is_group', '=', False)]

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
        if vals.get('state') == 'cancelled':
            # Commande groupee annulee par Skybox : ses ordres aussi.
            kids = self.filtered('is_group').child_ids.filtered(
                lambda c: c.state not in ('dispatched', 'cancelled'))
            if kids:
                kids.write({'state': 'cancelled',
                            'cancel_reason': vals.get('cancel_reason') or False,
                            'cancelled_at': vals.get('cancelled_at') or fields.Datetime.now()})
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

    def action_order_history(self):
        """Bouton Historique de la fiche ordre : tout l'ordre (tous ses
        colis, ses ordres groupes et les messages Skybox) dans une pop-up."""
        return self._history_popup(False)

    def _history_popup(self, tracking_number):
        """Pop-up : historique d'UN colis de l'ordre (ou de tout l'ordre si
        `tracking_number` est vide)."""
        self.ensure_one()
        wizard = self.env['skybox.colis.history'].create({
            'order_id': self.id,
            'tracking_number': tracking_number,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': ("Historique du colis %s" % tracking_number if tracking_number
                     else "Historique de l'ordre %s" % self.name),
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
        # Ordres precedents des memes colis (ex. ordre annule puis relance par
        # Skybox sous un nouveau numero), puis l'ordre lui-meme.
        top = self.parent_id or self
        Line = self.env['skybox.preparation.order.line'].sudo()
        tns = [tn for tn in self._tracking_numbers() if tn]
        previous = self.browse()
        if tns:
            domain = ['|'] * (len(tns) - 1) + [
                ('tracking_number', '=ilike', tn) for tn in tns]
            if not self.is_group:
                # Ordre d'un seul colis : l'ancien ordre de CE colis, pas
                # toute l'ancienne commande groupee.
                domain = [('order_id.is_group', '=', False)] + domain
            for line in Line.search(domain):
                prev = (line.order_id if not self.is_group
                        else line.order_id.parent_id or line.order_id)
                if prev.id < top.id:
                    previous |= prev
        for prev in previous.sorted('id'):
            self._history_order_rows(add, prev, previous=True)
            nxt = (previous.sorted('id').filtered(lambda o, p=prev: o.id > p.id)[:1] or self)
            if prev.state == 'cancelled':
                add(prev.cancelled_at, False, "Ordre annule",
                    "Ordre %s annule%s - relance par Skybox : %s" % (
                        prev.name,
                        " (motif : %s)" % prev.cancel_reason if prev.cancel_reason else "",
                        nxt.name),
                    '', 'event')
        last = previous.sorted('id')[-1:]
        self._history_order_rows(
            add, self, relaunch_of=last if last.state == 'cancelled' else None)
        if self.state == 'dispatched' and self.carrier_tracking_number:
            add(self.write_date, False, "Expedie",
                "%s - suivi %s" % (self.carrier or '', self.carrier_tracking_number),
                '', 'done')
        if tracking_number:
            rows = [r for r in rows if r['tracking_number'] in (False, tracking_number)]
        return sorted(rows, key=lambda r: r['date'])

    def _history_order_rows(self, add, order, previous=False, relaunch_of=None):
        """Lignes de l'historique pour un ordre : reception, changements de
        statut / picker / annulation (aussi ceux de sa commande groupee et de
        ses ordres groupes) et messages Skybox."""
        received = "Ordre %s recu (%s colis)" % (order.name, order.total_parcels)
        if relaunch_of:
            received += " - relance de %s" % relaunch_of.name
        add(order.requested_at or order.create_date, False,
            "Ordre precedent" if previous else "Ordre de preparation",
            received, order.requested_by)
        family = order | order.parent_id | order.child_ids
        if order.is_group and self.env.context.get('skybox_history_own'):
            # Bloc "commande groupee" : ses ordres ont chacun leur bloc.
            family = order
        names = {o.id: o.name for o in family}
        messages = self.env['mail.message'].sudo().search([
            ('model', '=', self._name), ('res_id', 'in', family.ids)], order='date, id')
        for msg in messages:
            prefix = ("%s : " % names.get(msg.res_id, '')
                      if previous or msg.res_id != order.id else "")
            from_group = order.parent_id and msg.res_id == order.parent_id.id
            for tv in msg.sudo().tracking_value_ids:
                if previous and order.state == 'cancelled' and (
                        tv.field_id.name == 'cancel_state'
                        or (tv.field_id.name == 'state' and tv.new_value_char == "Annule")):
                    # Ancien ordre annule : une seule ligne "Ordre annule".
                    continue
                if from_group and not (
                        tv.field_id.name == 'cancel_state'
                        or (tv.field_id.name == 'state'
                            and tv.new_value_char in ("Expedie", "Annule"))):
                    # Commande groupee : ses autres etapes viennent des autres
                    # ordres du regroupement, pas de celui-ci.
                    continue
                if tv.field_id.name == 'state':
                    add(msg.date, False, "Statut : %s" % (tv.new_value_char or ''),
                        "%s%s -> %s" % (prefix, tv.old_value_char or '', tv.new_value_char or ''),
                        msg.author_id.name)
                elif tv.field_id.name == 'agent_id':
                    add(msg.date, False, "Picker",
                        "%s%s -> %s" % (prefix, tv.old_value_char or '-', tv.new_value_char or '-'),
                        msg.author_id.name)
                elif tv.field_id.name == 'cancel_state':
                    add(msg.date, False, "Annulation : %s" % (tv.new_value_char or 'terminee'),
                        "%s%s" % (prefix, order.cancel_reason or ''), msg.author_id.name)
        # Messages Skybox (annulation, ...) ; la reception de l'ordre est
        # deja la ligne ci-dessus.
        for event in family.event_ids.filtered(lambda e: e.event_type != 'PREPARATION_ORDER'):
            add(event.occurred_at or event.received_at, False,
                "Skybox : %s" % event.event_type,
                " / ".join(filter(None, [event.ack_status, event.message])),
                '', 'event')

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

        vals = {
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
        }
        if (data.get('orderType') or '').upper() != 'CONSOLIDATION' or len(lines) < 2:
            return self.create(vals)
        # Consolidation : commande groupee (parent, dialogue avec Skybox) +
        # un ordre par colis (enfants, affectes aux pickers).
        group = self.create(dict(vals, is_group=True))
        for index, (_cmd, _id, line) in enumerate(lines, start=1):
            self.create(dict(
                vals,
                name="%s/%s" % (data['orderId'], index),
                parent_id=group.id,
                skybill_number=line.get('skybill_number') or vals['skybill_number'],
                total_parcels=1,
                total_weight=line.get('weight') or 0.0,
                line_ids=[(0, 0, dict(line))],
            ))
        return group

    LINE_COPY_FIELDS = (
        'tracking_number', 'parcel_id', 'skybill_number', 'current_bag_code',
        'bag_id', 'zone_code', 'aisle_code', 'rack_code', 'location_code',
        'location_id', 'weight', 'length_cm', 'width_cm', 'height_cm', 'fragile')

    @api.model
    def _init_cancel_refused(self):
        """Mise a jour du module : ordres dont un envoi a ete refuse
        definitivement par Skybox -> Annule."""
        for order in self.search([('sync_state', '=', 'error'),
                                  ('state', 'not in', ('cancelled', 'dispatched'))]):
            order.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancel_reason': "Envoi refuse par Skybox : %s" % (order.sync_message or ''),
            })

    @api.model
    def _init_split_consolidations(self):
        """Mise a jour du module : ordres de consolidation (2 colis ou plus)
        pas encore commences -> commande groupee + un ordre par colis."""
        orders = self.search([
            ('order_type', '=ilike', 'CONSOLIDATION'), ('state', '=', 'new'),
            ('is_group', '=', False), ('parent_id', '=', False),
        ])
        for group in orders.filtered(lambda o: len(o.line_ids) > 1):
            group.write({'is_group': True, 'agent_id': False})
            for index, line in enumerate(group.line_ids, start=1):
                line_vals = {f: line[f].id if hasattr(line[f], 'id') and line._fields[f].type == 'many2one'
                             else line[f] for f in self.LINE_COPY_FIELDS}
                group.copy({
                    'name': "%s/%s" % (group.name, index),
                    'reference': False,
                    'is_group': False,
                    'parent_id': group.id,
                    'agent_id': False,
                    'skybill_number': line.skybill_number or group.skybill_number,
                    'total_parcels': 1,
                    'total_weight': line.weight,
                    'line_ids': [(0, 0, line_vals)],
                })

    @api.model
    def _receive_cancel(self, data, event):
        """PREPARATION_ORDER_CANCELLED. Retourne (ordre, statut ACK, note) ou
        leve SkyboxEventRejected.

        - ordre pas commence (Nouveau) ou deja annule -> ACCEPTED (annule) ;
        - preparation en cours (Picking -> Pret)     -> PENDING : l'entrepot
          confirme ou refuse, la decision part sur /cancellation ;
        - deja expedie                               -> REJECTED."""
        order_id = data['orderId']
        reason = data.get('reason') or False
        order = self.search([('name', '=', order_id)], limit=1)
        if not order:
            # Annulation recue avant l'ordre : on garde une trace annulee.
            order = self.create({
                'name': order_id, 'state': 'cancelled', 'cancel_reason': reason,
                'cancelled_at': fields.Datetime.now(),
                'last_event_id': event.event_id,
            })
            return order, 'ACCEPTED', "Ordre %s inconnu : trace annulee creee." % order_id
        if order.state == 'cancelled':
            return order, 'ACCEPTED', "Ordre %s deja annule." % order_id
        if order.state == 'dispatched':
            raise SkyboxEventRejected(
                'ALREADY_DISPATCHED', "Ordre %s deja expedie." % order_id)
        if order.state == 'new':
            order.write({'last_event_id': event.event_id})
            order._do_cancel(reason)
            return order, 'ACCEPTED', "Ordre %s annule." % order_id

        # Preparation en cours : demande d'arret, a decider par l'entrepot.
        # Une nouvelle demande apres un refus est une nouvelle demande.
        order.write({
            'cancel_state': 'requested',
            'cancel_reason': reason,
            'cancel_requested_by': data.get('requestedBy') or False,
            'cancel_requested_at': fields.Datetime.now(),
            'cancel_reached_step': data.get('reachedStep') or False,
            'cancel_decision': False,
            'cancel_refuse_message': False,
            'cancel_sync_message': False,
            'last_event_id': event.event_id,
        })
        order.message_post(body=(
            "Skybox demande l'arret de l'ordre (%s) : %s. Etapes bloquees : "
            "confirmez l'annulation (colis remis en stock) ou refusez-la."
            % (data.get('requestedBy') or 'Skybox', reason or 'sans motif')))
        return order, 'PENDING', "Ordre %s : annulation demandee, decision de l'entrepot a venir." % order_id

    def _cancel_target(self):
        """Ordre connu de Skybox : la commande groupee pour un de ses ordres."""
        self.ensure_one()
        return self.parent_id or self

    @api.depends('cancel_state', 'parent_id.cancel_state')
    def _compute_cancel_blocked(self):
        for order in self:
            order.cancel_blocked = order._cancel_blocked()

    def _cancel_blocked(self):
        self.ensure_one()
        return self._cancel_target().cancel_state == 'requested'

    def _do_cancel(self, reason=False):
        """Annule l'ordre (et les ordres d'une commande groupee) ; les colis
        deja preleves sont remis a leur emplacement (RETURN_TO_STORAGE)."""
        now = fields.Datetime.now()
        for order in self:
            orders = order | order.child_ids.filtered(
                lambda c: c.state not in ('cancelled', 'dispatched'))
            for rec in orders.filtered(lambda o: not o.is_group):
                rec._return_to_stock()
            vals = {
                'state': 'cancelled',
                'cancelled_at': now,
                'cancel_state': False,
                'cancel_decision': False,
                'sync_state': 'none',
                'pending_status': False,
            }
            if reason:
                vals['cancel_reason'] = reason
            orders.write(vals)

    def _return_to_stock(self):
        """Colis preleves d'un ordre annule : retour a leur emplacement."""
        self.ensure_one()
        zone = STEP_ZONE.get(self.state)
        if not zone:
            return
        Movement = self.env['skybox.movement']
        for line in self.line_ids.filtered(lambda l: l.pick_state == 'PICKED'):
            Movement._record(
                'RETURN_TO_STORAGE', line.tracking_number,
                line._current_position(),
                {'zoneCode': Movement._zone(zone)},
                warehouse_code=self.warehouse_code,
                preparation_order_id=self.id,
                partner_reference=self.reference or self.name,
                notes="Ordre annule : colis remis en stock")
        self.message_post(body="Ordre annule : colis preleves remis a leur emplacement.")

    def action_cancel_confirm(self):
        """L'entrepot accepte la demande d'annulation de Skybox."""
        return self._decide_cancel('CANCELLED')

    def action_cancel_refuse(self):
        """L'entrepot refuse la demande : la preparation continue."""
        return self._decide_cancel('REFUSED')

    def _decide_cancel(self, decision):
        for order in self:
            target = order._cancel_target()
            if target.cancel_state != 'requested':
                raise UserError("Aucune demande d'annulation en attente sur %s." % target.name)
            target.write({'cancel_decision': decision})
            target._send_cancellation()
        return self.mapped(lambda o: o._cancel_target())._sync_notification(cancel=True)

    def _cancel_payload(self):
        self.ensure_one()
        payload = {
            'decision': self.cancel_decision,
            'operator': self.env.user.name,
            'occurredAt': format_skybox_datetime(self.env),
        }
        if self.cancel_decision == 'REFUSED':
            payload['reasonCode'] = REFUSE_CODE.get(self.state, 'PREPARATION_UNDER_WAY')
            payload['message'] = self.cancel_refuse_message or "Preparation deja %s." % (
                dict(self._fields['state'].selection).get(self.state, self.state))
        return payload

    def _send_cancellation(self):
        """POST {URL Skybox}/v1/integrations/wms/preparation-orders/{orderId}/cancellation
        Erreur passagere (reseau, 401, 5xx) : decision gardee, le cron renvoie."""
        self.ensure_one()
        decision = self.cancel_decision
        if not decision:
            return
        key, base = self.env['skybox.api.key']._skybox_wms_base()
        if not key:
            self.cancel_sync_message = ("Aucune API externe Skybox configuree "
                                        "(Configuration > API management).")
            return
        auth_headers, params = key._build_request()
        headers = {'Content-Type': 'application/json'}
        headers.update(auth_headers)
        payload = self._cancel_payload()
        try:
            response = requests.post(base + CANCEL_PATH % self.name, json=payload,
                                     headers=headers, params=params, timeout=15)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Skybox cancellation %s %s : %s", self.name, decision, exc)
            self.cancel_sync_message = "Skybox injoignable, nouvel essai automatique: %s" % exc
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = response.status_code
        err = body.get('errorCode') or body.get('code') or ''
        message = body.get('message') or ''
        current = body.get('status') or body.get('currentStatus') or ''

        if 200 <= code < 300 or (code == 409 and err == 'NO_CANCELLATION'):
            if current == 'CANCELLED':
                reason = self.cancel_reason or "Annulation demandee par Skybox"
                self.cancel_sync_message = "Annulation confirmee par Skybox."
                self._do_cancel(reason)
                self.message_post(body="Annulation confirmee a Skybox (%s)." % self.env.user.name)
                return
            if code == 409:
                note = ("Skybox n'a plus d'annulation en attente (etat Skybox: %s)."
                        % (current or '-'))
                state = False
            else:
                note = "Refus enregistre par Skybox : la preparation continue (%s)." % (
                    current or '-')
                state = 'refused'
            self.write({'cancel_state': state, 'cancel_decision': False,
                        'cancel_sync_message': note})
            self.message_post(body=note)
            if self.pending_status:
                # Etape bloquee pendant la demande : on la renvoie.
                self.sync_state = 'pending'
                self._send_status()
            return
        if code in (400, 404, 409):
            reason = "Decision %s refusee par Skybox - HTTP %s %s : %s" % (
                decision, code, err, message)
            self.write({'cancel_decision': False, 'cancel_sync_message': reason})
            self.message_post(body=reason)
            return
        self.cancel_sync_message = "HTTP %s %s, nouvel essai automatique." % (
            code, message or response.reason)

    # ------------------------------------------------------------------
    # Avancement Odoo -> Skybox (guide v2, section 11)
    # ------------------------------------------------------------------
    def _check_open(self):
        for order in self:
            if order._cancel_blocked():
                raise UserError(
                    "Skybox demande l'annulation de l'ordre %s : confirmez ou "
                    "refusez l'annulation avant de continuer."
                    % order._cancel_target().name)
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
            if order.parent_id:
                # Ordre d'une commande groupee : Skybox ne le connait pas, c'est
                # la commande groupee qui envoie l'etape quand tous ses ordres
                # y sont.
                if step == 'dispatched':
                    raise UserError(
                        "L'expedition se fait sur la commande groupee %s "
                        "(un seul carton)." % order.parent_id.name)
                order.write({'state': step, 'has_exception': False})
                order._step_movements(step)
                continue
            order.write({
                'state': step,
                'has_exception': False,
                'pending_status': STEP_CODE[step],
                'sync_state': 'pending',
            })
            if order.is_group:
                if step == 'dispatched':
                    kids = order.child_ids.filtered(
                        lambda c: c.state not in ('dispatched', 'cancelled'))
                    kids.write({'carrier': order.carrier,
                                'carrier_tracking_number': order.carrier_tracking_number,
                                'package_code': order.package_code})
                    for kid in kids:
                        kid.write({'state': 'dispatched', 'has_exception': False})
                        kid._step_movements('dispatched')
            else:
                order._step_movements(step)
            order._send_status()
        self.mapped('parent_id')._sync_from_children()
        return self.filtered(lambda o: not o.parent_id)._sync_notification()

    def _sync_from_children(self):
        """Commande groupee : etape envoyee a Skybox d'apres ses ordres.
        PICKING des qu'un ordre commence (Skybox ne peut plus annuler) ;
        PICKED / PACKED / PRET quand tous les ordres y sont. L'expedition
        se fait sur la commande groupee."""
        for group in self.filtered('is_group'):
            if group.state in ('dispatched', 'cancelled'):
                continue
            kids = group.child_ids.filtered(lambda c: c.state != 'cancelled')
            if not kids:
                continue
            ranks = [STEP_RANK[k.state] for k in kids]
            target = min(ranks)
            if max(ranks) >= STEP_RANK['picking']:
                target = max(target, STEP_RANK['picking'])
            target = min(target, STEP_RANK['ready'])
            step = STEPS[target][0]
            if STEP_RANK[step] > STEP_RANK[group.state]:
                if step in ('packed', 'ready') and not group.package_code:
                    group.package_code = group.reference
                group._go_to(step)

    # Mouvements d'entrepot envoyes a chaque etape (colis preleves).
    STEP_MOVEMENTS = {
        'packed': ('PACKING', 'PICKING'),
        'ready': ('STAGING', 'PACKING'),
        'dispatched': ('DISPATCH', 'STAGING'),
    }

    def _step_movements(self, step):
        """Packed / Pret / Expedie : un mouvement par colis preleve, de la
        zone de l'etape precedente vers celle de l'etape."""
        self.ensure_one()
        if step not in self.STEP_MOVEMENTS:
            return
        Movement = self.env['skybox.movement']
        movement_type, previous = self.STEP_MOVEMENTS[step]
        notes = False
        if step == 'dispatched':
            notes = "Remis a %s - suivi %s" % (
                self.carrier or '', self.carrier_tracking_number or '')
        for line in self.line_ids.filtered(lambda l: l.pick_state != 'MISSING'):
            Movement._record(
                movement_type, line.tracking_number,
                {'zoneCode': Movement._zone(movement_type)},
                {'zoneCode': Movement._zone(previous)},
                warehouse_code=self.warehouse_code,
                preparation_order_id=self.id,
                partner_reference=self.reference or self.name,
                notes=notes)

    def action_picking(self):
        return self._go_to('picking')

    def action_picked(self):
        for order in self:
            order.line_ids.filtered(lambda l: not l.pick_state).write(
                {'pick_state': 'PICKED'})
        return self._go_to('picked')

    def _fill_picked(self):
        """Etape Packed / Pret / Expedie sans passer par Picked : les colis
        pas encore scannes sont preleves."""
        for order in self.filtered(lambda o: not o.is_group):
            order.line_ids.filtered(lambda l: not l.pick_state).write(
                {'pick_state': 'PICKED'})

    def action_packed(self):
        self._check_open()
        self._fill_picked()
        for order in self:
            if not order.package_code:
                order.package_code = order.reference
        return self._go_to('packed')

    def action_ready(self):
        self._check_open()
        self._fill_picked()
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
            if order.parent_id:
                # Remontee a Skybox via la commande groupee.
                order.has_exception = True
                group = order.parent_id
                group.write({
                    'exception_code': order.exception_code,
                    'exception_message': "%s (%s)" % (order.exception_message, order.name),
                })
                group.action_exception()
                continue
            order.write({
                'has_exception': True,
                'pending_status': 'EXCEPTION',
                'sync_state': 'pending',
            })
            order._send_status()
        return self._sync_notification()

    def action_reset_new(self):
        """Ordre annule -> de nouveau 'Nouveau' (picking a refaire)."""
        Line = self.env['skybox.preparation.order.line']
        for order in self.filtered(lambda o: o.state == 'cancelled'):
            if order.parent_id and order.parent_id.state == 'cancelled':
                raise UserError(
                    "Cet ordre fait partie de la commande groupee annulee %s : "
                    "remettez la commande groupee en Nouveau." % order.parent_id.name)
            orders = order | order.child_ids.filtered(lambda c: c.state == 'cancelled')
            # Un colis ne peut etre que dans un seul ordre en cours.
            for line in orders.filtered(lambda o: not o.is_group).line_ids:
                other = Line.search([
                    ('id', '!=', line.id),
                    ('tracking_number', '=ilike', line.tracking_number or ''),
                    ('order_id.is_group', '=', False),
                    ('order_id.state', 'in', ('new', 'picking', 'picked', 'packed', 'ready')),
                ], limit=1)
                if line.tracking_number and other:
                    raise UserError(
                        "Le colis %s est deja dans l'ordre en cours %s."
                        % (line.tracking_number, other.order_id.name))
            for rec in orders:
                rec.line_ids.with_context(skybox_group_sync=True).write({'pick_state': False})
                rec.write({
                    'state': 'new',
                    'cancel_reason': False,
                    'cancelled_at': False,
                    'has_exception': False,
                    'sync_state': 'none',
                    'pending_status': False,
                    'sync_message': False,
                    'cancel_state': False,
                    'cancel_decision': False,
                    'agent_id': False if rec.is_group else self._next_picker().id,
                })
                rec.message_post(body="Ordre remis en Nouveau par %s." % self.env.user.name)
        return True

    def action_resend(self):
        for order in self.filtered('pending_status'):
            order._send_status()
        return self._sync_notification()

    def _sync_notification(self, cancel=False):
        """Resultat de l'envoi en notification discrete (bus), sans action
        client : le bouton renvoie True, le formulaire se met a jour sur
        place. Le detail reste visible sur la fiche (Envoi Skybox)."""
        for order in self:
            if cancel:
                ok = not order.cancel_decision
                message = order.cancel_sync_message
            else:
                ok = order.sync_state == 'sent'
                message = order.sync_message
            self.env.user._bus_send('simple_notification', {
                'title': "Skybox" if ok else "Envoi Skybox",
                'message': "%s : %s" % (order.name, message or ''),
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
            'occurredAt': format_skybox_datetime(self.env),
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
        err = (body.get('errorCode') or body.get('code')) if isinstance(body, dict) else None
        if code == 409 and err == 'ORDER_CANCELLED':
            # Ordre annule ou en cours d'annulation chez Skybox : l'etape
            # reste a envoyer (reprise si l'annulation est refusee) et
            # l'entrepot decide (Confirmer l'annulation / Refuser).
            vals = {'sync_date': now,
                    'sync_message': "Etape %s bloquee : ordre annule ou en cours "
                                    "d'annulation chez Skybox. %s" % (status, message)}
            if self.cancel_state != 'requested':
                vals.update({
                    'cancel_state': 'requested',
                    'cancel_requested_at': now,
                    'cancel_reached_step': self.last_sent_status or False,
                    'cancel_decision': False,
                    'cancel_reason': self.cancel_reason or (
                        "Skybox a refuse l'etape %s (ORDER_CANCELLED)." % status),
                })
            self.write(vals)
            self.message_post(body=vals['sync_message'])
            return
        if code in (400, 404, 409):
            # Refus definitif de Skybox : l'ordre passe en Annule.
            extra = (" (etat Skybox: %s)" % body.get('current')
                     if isinstance(body, dict) and body.get('current') else '')
            reason = "Envoi %s refuse par Skybox - HTTP %s %s : %s%s" % (
                status, code, err or '', message, extra)
            self.write({
                'state': 'cancelled', 'cancelled_at': now, 'cancel_reason': reason,
                'sync_state': 'error', 'pending_status': False, 'sync_date': now,
                'sync_message': reason,
            })
            self.message_post(body=reason)
            return
        # 401 / 5xx : passager, on garde l'etape a envoyer.
        self.write({
            'sync_date': now,
            'sync_message': "HTTP %s %s, nouvel essai automatique." % (
                code, message or response.reason),
        })

    @api.model
    def _cron_resend_status(self):
        """Renvoie les decisions d'annulation et les etapes pas encore
        acceptees par Skybox (etapes bloquees pendant une demande
        d'annulation)."""
        for order in self.search([('cancel_decision', '!=', False)]):
            order._send_cancellation()
        for order in self.search([('sync_state', '=', 'pending'),
                                  ('pending_status', '!=', False),
                                  ('cancel_state', '!=', 'requested')]):
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

    parcel_state = fields.Selection(
        selection=lambda self: self.env['skybox.preparation.order']._fields['state'].selection,
        string="Statut", compute='_compute_parcel_state',
        help="Ou en est le colis : statut de son ordre (pour une commande "
             "groupee, l'ordre du regroupement qui contient le colis).")
    parcel_order_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Ordre du colis",
        compute='_compute_parcel_state')

    @api.depends('order_id.state', 'order_id.child_ids.state', 'tracking_number')
    def _compute_parcel_state(self):
        for line in self:
            order = line.order_id
            if order.is_group:
                tn = (line.tracking_number or '').lower()
                order = order.child_ids.filtered(lambda c: any(
                    (l.tracking_number or '').lower() == tn for l in c.line_ids))[:1] or order
            line.parcel_order_id = order
            line.parcel_state = order.state

    wh_location_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement dans le WH",
        compute='_compute_wh_location',
        help="Bin ou le colis est stocke dans Odoo (reception au statut Stored).")
    wh_bag_id = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag dans le WH",
        compute='_compute_wh_location',
        help="Bag ou se trouve le colis s'il n'est pas encore range.")

    current_place = fields.Char(
        string="Emplacement actuel", compute='_compute_wh_location',
        help="Bin ou le colis est range ; s'il n'est pas encore range, le bag "
             "dans lequel il se trouve.")

    def _compute_wh_location(self):
        Picking = self.env['stock.picking'].sudo()
        for line in self:
            tracking = line.tracking_number or line.parcel_id.tracking_number
            picking = line.parcel_id.picking_id or (Picking.search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', tracking),
                ('state', '!=', 'cancel'),
            ], limit=1) if tracking else Picking)
            stored = picking.skybox_status == 'stored'
            line.wh_location_id = picking.location_dest_id if stored else False
            line.wh_bag_id = picking.bag if picking and not stored else False
            if line.wh_location_id:
                line.current_place = line.wh_location_id.complete_name
            else:
                bag = line.wh_bag_id.name or line.current_bag_code
                line.current_place = ("Bag %s" % bag) if bag else False

    def _current_position(self):
        """Position du colis donnee par Skybox dans l'ordre."""
        self.ensure_one()
        return {
            'bagCode': self.current_bag_code,
            'zoneCode': self.zone_code,
            'aisleCode': self.aisle_code,
            'rackCode': self.rack_code,
            'locationCode': self.location_code,
        }

    def write(self, vals):
        old = {l.id: l.pick_state for l in self} if 'pick_state' in vals else {}
        res = super().write(vals)
        if 'pick_state' in vals and not self.env.context.get('skybox_group_sync'):
            # Ordre d'une commande groupee : meme etat sur la ligne du parent
            # (envoye a Skybox avec les etapes de la commande groupee).
            for line in self.filtered('order_id.parent_id'):
                line.order_id.parent_id.line_ids.filtered(
                    lambda g: g.tracking_number == line.tracking_number
                ).with_context(skybox_group_sync=True).write({'pick_state': line.pick_state})
        if (self.env.context.get('skybox_group_sync')
                or not self.filtered(lambda l: not l.order_id.is_group)):
            return res
        if 'pick_state' in vals and vals['pick_state'] in ('PICKED', 'DAMAGED'):
            Movement = self.env['skybox.movement']
            for line in self.filtered(lambda l: old.get(l.id) != l.pick_state):
                if line.pick_state == 'PICKED':
                    Movement._record(
                        'PICKING', line.tracking_number,
                        {'zoneCode': Movement._zone('PICKING')},
                        line._current_position(),
                        warehouse_code=line.order_id.warehouse_code,
                        preparation_order_id=line.order_id.id,
                        partner_reference=line.order_id.reference or line.order_id.name)
                else:
                    Movement._record(
                        'EXCEPTION', line.tracking_number,
                        {'zoneCode': Movement._zone('EXCEPTION')},
                        line._current_position(),
                        warehouse_code=line.order_id.warehouse_code,
                        preparation_order_id=line.order_id.id,
                        partner_reference=line.order_id.reference or line.order_id.name,
                        reason_code='DAMAGED',
                        notes="Colis abime constate au picking")
        return res

    def action_history(self):
        self.ensure_one()
        return self.order_id._history_popup(
            self.tracking_number or self.parcel_id.tracking_number)
