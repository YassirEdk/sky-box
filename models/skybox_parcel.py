# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError

from .skybox_event import parse_skybox_datetime, to_float, to_int


class SkyboxParcel(models.Model):
    """Colis traite par Skybox (PARCEL_PROCESSED / PARCEL_UPDATED)."""
    _name = 'skybox.parcel'
    _description = "Colis Skybox"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'write_date desc, id desc'
    _rec_name = 'tracking_number'

    tracking_number = fields.Char(
        string="Numero de tracking", required=True, index=True, tracking=True)
    revision = fields.Integer(string="Revision", readonly=True, tracking=True)
    last_event_id = fields.Char(string="Dernier eventId", readonly=True)
    last_event_type = fields.Char(string="Dernier evenement", readonly=True)
    storage_location_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement de stockage",
        compute='_compute_storage_location', inverse='_inverse_storage_location',
        store=True, readonly=False, tracking=True,
        domain=[('usage', '=', 'internal')],
        help="Bin ou le colis est stocke. Le choisir ici stocke la reception "
             "(statut Stored), comme le scan 'Store' de l'app mobile.")
    last_event_at = fields.Datetime(
        string="Derniere mise a jour", readonly=True,
        help="Date et heure de la derniere mise a jour envoyee par Skybox.")
    event_ids = fields.One2many(
        comodel_name='skybox.event', inverse_name='parcel_id',
        string="Evenements",
    )
    history_count = fields.Integer(
        string="Historique", compute='_compute_history_count')

    def _compute_history_count(self):
        for parcel in self:
            parcel.history_count = len(parcel._history_rows())

    def _history_order(self):
        """Dernier ordre de preparation (hors commande groupee) du colis."""
        self.ensure_one()
        return self.env['skybox.preparation.order.line'].sudo().search([
            ('order_id.is_group', '=', False),
            ('tracking_number', '=ilike', self.tracking_number),
        ], order='id desc', limit=1).order_id

    def _history_rows(self):
        """Lignes de l'historique du colis (meme format que celui des ordres
        de preparation) : dans un ordre, son historique complet ; sinon
        prealerte, reception / stockage / transferts et traitement Skybox."""
        self.ensure_one()
        order = self._history_order()
        if order:
            return order._history_rows(self.tracking_number)
        rows = []

        def add(date, step, detail='', user='', kind='reception'):
            if date:
                rows.append({'date': date, 'tracking_number': self.tracking_number,
                             'step': step, 'detail': detail or '',
                             'user': user or '', 'kind': kind})

        tn = self.tracking_number or ''
        prealertes = self.prealerte_id | self.env['skybox.prealerte'].sudo().search(
            [('tracking_number', '=ilike', tn)])
        for pre in prealertes:
            add(pre.create_date, "Prealerte", "Prealerte %s creee" % pre.name,
                pre.create_uid.name, 'prealerte')
        pickings = self.picking_id | self.env['stock.picking'].sudo().search([
            ('picking_type_code', '=', 'incoming'),
            ('tracking_number', '=ilike', tn),
        ])
        History = self.env['skybox.picking.history'].sudo()
        actions = dict(History._fields['action'].selection)
        for h in History.search([('picking_id', 'in', pickings.ids)]):
            # Lignes automatiques des evenements Skybox (deja "Colis traite").
            if (h.note or '').startswith('Skybox '):
                continue
            add(h.date, "Reception - %s" % actions.get(h.action, h.action),
                " / ".join(filter(None, [
                    h.picking_id.name,
                    h.location_id.complete_name
                    and "Emplacement : %s" % h.location_id.complete_name,
                    h.bag_id.name and "Bag : %s" % h.bag_id.name,
                    h.note])),
                h.user_id.name)
        add(self.processed_at, "Colis traite",
            " / ".join(filter(None, [self.package_condition, self.result])),
            self.processed_by, 'parcel')
        add(self.skybill_date, "Skybill",
            "Skybill %s" % (self.skybill_number or ''), '', 'parcel')
        add(self.decided_at, "Decision",
            " / ".join(filter(None, [self.decision_kind, self.decision_note])),
            self.decided_by, 'parcel')
        return sorted(rows, key=lambda r: r['date'])

    def action_history(self):
        """Bouton Historique : pop-up chronologique du colis."""
        self.ensure_one()
        wizard = self.env['skybox.colis.history'].create({
            'parcel_id': self.id,
            'order_id': self._history_order().id,
            'tracking_number': self.tracking_number,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': "Historique du colis %s" % self.tracking_number,
            'res_model': 'skybox.colis.history',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    skybox_past = fields.Boolean(
        string="Termine (Past orders)", index=True, copy=False, readonly=True,
        help="Expedie : l'ordre de preparation est termine, visible "
             "uniquement dans Past orders.")
    in_preparation = fields.Boolean(
        string="Dans un ordre de preparation", compute='_compute_in_preparation',
        search='_search_in_preparation',
        help="Colis dans un ordre de preparation en cours (Nouveau a Pret a "
             "expedier) : il est suivi dans Preparation orders, plus dans "
             "Colis traites.")

    def _preparation_parcel_ids(self):
        """Colis dans un ordre de preparation en cours."""
        lines = self.env['skybox.preparation.order.line'].sudo().search([
            ('order_id.is_group', '=', False),
            ('order_id.state', 'in', ('new', 'picking', 'picked', 'packed', 'ready')),
        ])
        tns = {(t or '').upper() for t in lines.mapped('tracking_number') if t}
        ids = set(lines.parcel_id.ids)
        if tns:
            ids |= set(self.sudo().search([('tracking_number', 'in', list(tns))]).ids)
        return ids

    def _compute_in_preparation(self):
        ids = self._preparation_parcel_ids()
        for parcel in self:
            parcel.in_preparation = parcel.id in ids

    def _search_in_preparation(self, operator, value):
        ids = list(self._preparation_parcel_ids())
        positive = (operator == '=') == bool(value)
        return [('id', 'in' if positive else 'not in', ids)]

    warehouse_code = fields.Char(string="Code entrepot")
    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse', string="Entrepot")
    result = fields.Char(string="Resultat", tracking=True)
    expected_in_bag = fields.Boolean(string="Attendu dans le bag")
    identification = fields.Char(string="Identification")
    shipment_status = fields.Char(string="Statut expedition", tracking=True)

    # --- Reception ---
    batch_id = fields.Char(string="Batch")
    source_bag_code = fields.Char(string="Bag source")
    source_bag_id = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag source (Odoo)")
    partner_received_at = fields.Datetime(string="Recu par le partenaire le")
    reception_outcome = fields.Char(string="Resultat reception")
    suite_number_at_reception = fields.Char(string="Suite envoyee a la reception")

    # --- Traitement ---
    session_id = fields.Char(string="Session")
    processed_at = fields.Datetime(string="Traite le")
    processed_by = fields.Char(string="Traite par")
    package_condition = fields.Char(string="Etat du colis")
    processing_notes = fields.Text(string="Notes de traitement")

    # --- Proprietaire ---
    ownership_confirmed = fields.Boolean(string="Proprietaire confirme")
    ownership_source = fields.Char(string="Source")
    suite_number = fields.Char(string="Suite du client", tracking=True)
    partner_id = fields.Many2one(
        comodel_name='res.partner', string="Client", tracking=True)

    # --- Mesures ---
    actual_weight = fields.Float(string="Poids reel (kg)", digits=(12, 3))
    length_cm = fields.Float(string="Longueur (cm)")
    width_cm = fields.Float(string="Largeur (cm)")
    height_cm = fields.Float(string="Hauteur (cm)")
    volumetric_weight = fields.Float(string="Poids volumetrique (kg)", digits=(12, 3))
    chargeable_weight = fields.Float(string="Poids taxable (kg)", digits=(12, 3))
    volumetric_divisor = fields.Integer(string="Diviseur volumetrique (cm3/kg)")

    # --- Contenu ---
    items_source = fields.Char(string="Source des articles")
    line_ids = fields.One2many(
        comodel_name='skybox.parcel.line', inverse_name='parcel_id',
        string="Articles",
    )

    # --- Prealerte ---
    prealerte_id = fields.Many2one(
        comodel_name='skybox.prealerte', string="Prealerte",
        ondelete='set null')
    prealert_ref = fields.Char(string="preAlertId")
    prealert_status = fields.Char(string="Statut prealerte")
    courier_id = fields.Many2one(
        comodel_name='skybox.courier', string="Transporteur")
    declared_value = fields.Float(string="Valeur declaree")
    declared_currency_id = fields.Many2one(
        comodel_name='res.currency', string="Devise declaree")

    # --- Skybill ---
    skybill_number = fields.Char(string="Skybill", index=True, tracking=True)
    skybill_status = fields.Char(string="Statut Skybill", tracking=True)
    skybill_date = fields.Datetime(string="Skybill cree le")
    skybill_currency_id = fields.Many2one(
        comodel_name='res.currency', string="Devise Skybill")
    skybill_total = fields.Monetary(
        string="Montant Skybill", currency_field='skybill_currency_id')

    # --- Decision d'un agent Skybox ---
    decision_kind = fields.Char(
        string="Decision", tracking=True,
        help="SET_ASIDE, MISSING, UNEXPECTED_SET_ASIDE ou UNEXPECTED_KEPT")
    decision_note = fields.Char(string="Raison de la decision")
    decided_by = fields.Char(string="Decide par")
    decided_at = fields.Datetime(string="Decide le")

    # --- Bag de destination ---
    destination_bag_code = fields.Char(string="Bag de destination")
    destination_bag_id = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag de destination (Odoo)")

    # --- Reception Odoo liee ---
    picking_id = fields.Many2one(
        comodel_name='stock.picking', string="Reception Odoo",
        ondelete='set null')

    @api.depends('picking_id.location_dest_id', 'picking_id.skybox_status')
    def _compute_storage_location(self):
        for parcel in self:
            picking = parcel.picking_id
            parcel.storage_location_id = (
                picking.location_dest_id if picking.skybox_status == 'stored' else False)

    def _inverse_storage_location(self):
        """Stocke la reception du colis dans l'emplacement choisi."""
        for parcel in self.filtered('storage_location_id'):
            picking = parcel.picking_id or self.env['stock.picking'].search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', parcel.tracking_number),
                ('state', '!=', 'cancel'),
            ], limit=1)
            if not picking:
                raise UserError(
                    "Le colis %s n'a pas de reception Odoo : recevez-le d'abord "
                    "(app mobile) avant de le stocker." % parcel.tracking_number)
            if picking.skybox_status == 'canceled':
                raise UserError("La reception %s est annulee." % picking.name)
            location = parcel.storage_location_id
            if picking.skybox_status == 'stored' and picking.location_dest_id == location:
                continue
            bag = picking.bag
            was_stored = picking.skybox_status == 'stored'
            old_location = picking.location_dest_id
            picking.write({
                'location_dest_id': location.id,
                'skybox_status': 'stored',
            })
            picking._log_history(
                'stored', location=location, bag=bag,
                note="Stocke depuis Colis traites")
            picking._skybox_store_movement(
                location, bag=bag, was_stored=was_stored,
                old_location=old_location, note="Stocke depuis Colis traites")
            if parcel.picking_id != picking:
                parcel.picking_id = picking

    def write(self, vals):
        # Sac change a la main dans Odoo (pas par un evenement Skybox, qui
        # envoie aussi destination_bag_code) : mouvement BAG_TRANSFER.
        manual_bag = 'destination_bag_id' in vals and 'destination_bag_code' not in vals
        old_bags = {p.id: p.destination_bag_id for p in self} if manual_bag else {}
        res = super().write(vals)
        if manual_bag:
            for parcel in self:
                old, new = old_bags.get(parcel.id), parcel.destination_bag_id
                if new and old != new:
                    self.env['skybox.movement']._record(
                        'BAG_TRANSFER', parcel.tracking_number,
                        {'bagCode': new.name},
                        {'bagCode': old.name} if old else None,
                        warehouse_code=parcel.warehouse_code,
                        picking_id=parcel.picking_id.id,
                        partner_reference=parcel.picking_id.name)
        if vals.get('destination_bag_id'):
            # Bag de destination saisi (Skybox ou a la main) : le colis quitte
            # son bag source.
            self._move_to_destination_bag()
        return res

    def _move_to_destination_bag(self):
        """Met la reception du colis dans son bag de destination (tant qu'elle
        n'est pas stockee / annulee) : le colis disparait du bag source dans
        la liste des bags."""
        Picking = self.env['stock.picking']
        for parcel in self.filtered('destination_bag_id'):
            picking = parcel.picking_id or Picking.search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', parcel.tracking_number),
                ('state', '!=', 'cancel'),
            ], limit=1)
            bag = parcel.destination_bag_id
            if (not picking or picking.bag == bag
                    or picking.skybox_status not in ('receipt', 'processed')):
                continue
            source = picking.bag
            picking.bag = bag
            picking._log_history(
                'moved', bag=bag,
                note="Bag %s -> bag de destination %s" % (source.name or '-', bag.name))

    @api.model
    def _init_restore_stored(self):
        """Mise a jour du module : receptions rangees (derniere action
        'stored' / 'moved' vers un emplacement) remises a tort en Processed
        par une mise a jour Skybox : on les remet en Stored."""
        History = self.env['skybox.picking.history']
        pickings = self.env['stock.picking'].search([
            ('picking_type_code', '=', 'incoming'),
            ('skybox_status', '=', 'processed'),
        ])
        for picking in pickings:
            last = History.search([
                ('picking_id', '=', picking.id),
                ('action', 'in', ('stored', 'received')),
            ], order='date desc, id desc', limit=1)
            if last.action == 'stored':
                picking.skybox_status = 'stored'

    @api.model
    def _init_destination_bags(self):
        """Mise a jour du module : colis deja affectes a un bag de destination
        mais encore comptes dans leur bag source."""
        self.search([('destination_bag_id', '!=', False)])._move_to_destination_bag()

    @api.model
    def _init_last_event_at(self):
        """Mise a jour du module : remplit la date de derniere mise a jour des
        colis existants avec leur dernier evenement Skybox traite."""
        for parcel in self.search([('last_event_at', '=', False)]):
            event = self.env['skybox.event'].search([
                ('parcel_id', '=', parcel.id), ('state', '=', 'accepted'),
            ], order='received_at desc, id desc', limit=1)
            if event.received_at:
                parcel.last_event_at = event.received_at

    _sql_constraints = [
        ('tracking_number_uniq', 'unique(tracking_number)',
         "Ce numero de tracking existe deja."),
    ]

    # ------------------------------------------------------------------
    @api.model
    def _currency(self, code):
        if not code:
            return self.env['res.currency']
        return self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', code)], limit=1)

    @api.model
    def _warehouse(self, code):
        if not code:
            return self.env['stock.warehouse']
        Warehouse = self.env['stock.warehouse']
        return (Warehouse.search([('skybox_code', '=ilike', code)], limit=1)
                or Warehouse.search([('code', '=ilike', code)], limit=1))

    @api.model
    def _courier(self, name):
        if not name:
            return self.env['skybox.courier']
        Courier = self.env['skybox.courier']
        return (Courier.search([('name', '=ilike', name)], limit=1)
                or Courier.create({'name': name}))

    @api.model
    def _apply_event(self, parcel, data, parcel_data, event):
        """Cree / met a jour le colis a partir de data (PARCEL_*)."""
        tracking = parcel_data['trackingNumber'].strip()
        reception = parcel_data.get('reception') or {}
        processing = parcel_data.get('processing') or {}
        ownership = parcel_data.get('ownership') or {}
        measures = parcel_data.get('measurements') or {}
        skybill = parcel_data.get('skybill') or {}
        dest_bag = parcel_data.get('destinationBag') or {}
        Package = self.env['stock.quant.package']

        vals = {
            'tracking_number': tracking,
            'revision': to_int(parcel_data.get('revision')),
            'last_event_id': event.event_id,
            'last_event_type': event.event_type,
            'last_event_at': fields.Datetime.now(),
            'warehouse_code': data.get('warehouseCode') or False,
            'warehouse_id': self._warehouse(data.get('warehouseCode')).id,
            'result': parcel_data.get('result') or False,
            'expected_in_bag': bool(parcel_data.get('expectedInBag')),
            'identification': parcel_data.get('identification') or False,
            'shipment_status': parcel_data.get('shipmentStatus') or False,
        }
        # Chaque message porte l'etat COMPLET du colis (guide v2, section 5) :
        # on remplace tout ; un bloc absent efface les valeurs precedentes.
        decision = parcel_data.get('decision') or {}
        partner = self.env['skybox.prealerte']._find_or_create_partner(
            ownership.get('customer') or {})
        vals.update({
            # Reception (ce qu'Odoo avait declare)
            'batch_id': reception.get('batchId') or False,
            'source_bag_code': reception.get('sourceBagCode') or False,
            'source_bag_id': Package._skybox_find_or_create(
                reception.get('sourceBagCode')).id,
            'partner_received_at': parse_skybox_datetime(
                reception.get('partnerReceivedAt')),
            'reception_outcome': reception.get('receptionOutcome') or False,
            'suite_number_at_reception': reception.get(
                'suiteNumberSentAtReception') or False,
            # Pretraitement
            'session_id': processing.get('sessionId') or False,
            'processed_at': parse_skybox_datetime(processing.get('processedAt')),
            'processed_by': processing.get('processedBy') or False,
            'package_condition': processing.get('packageCondition') or False,
            'processing_notes': processing.get('notes') or False,
            # Proprietaire (client seulement si confirme)
            'ownership_confirmed': bool(ownership.get('confirmed')),
            'ownership_source': ownership.get('source') or False,
            'suite_number': ownership.get('suiteNumber') or False,
            'partner_id': partner.id if partner else False,
            # Mesures
            'actual_weight': to_float(measures.get('actualWeightKg')),
            'length_cm': to_float(measures.get('lengthCm')),
            'width_cm': to_float(measures.get('widthCm')),
            'height_cm': to_float(measures.get('heightCm')),
            'volumetric_weight': to_float(measures.get('volumetricWeightKg')),
            'chargeable_weight': to_float(measures.get('chargeableWeightKg')),
            'volumetric_divisor': to_int(measures.get('volumetricDivisorCm3PerKg')),
            # Skybill
            'skybill_number': skybill.get('skybillNumber') or False,
            'skybill_status': skybill.get('status') or False,
            'skybill_date': parse_skybox_datetime(skybill.get('createdAt')),
            'skybill_total': to_float(skybill.get('total')),
            'skybill_currency_id': self._currency(skybill.get('currency')).id,
            # Sac de destination (saisi au pretraitement)
            'destination_bag_code': dest_bag.get('bagCode') or False,
            'destination_bag_id': Package._skybox_find_or_create(
                dest_bag.get('bagCode')).id,
            # Decision d'un agent (mis de cote, absent...)
            'decision_kind': decision.get('kind') or False,
            'decision_note': decision.get('note') or False,
            'decided_by': decision.get('decidedBy') or False,
            'decided_at': parse_skybox_datetime(decision.get('decidedAt')),
        })
        # Contenu verifie : toujours une liste ([] si non traite).
        vals['items_source'] = parcel_data.get('itemsSource') or False
        lines = [(5, 0, 0)]
        for item in parcel_data.get('items') or []:
            if not isinstance(item, dict):
                continue
            lines.append((0, 0, {
                'line_no': to_int(item.get('lineNo')),
                'description': item.get('description') or '/',
                'quantity': to_float(item.get('quantity')),
                'unit_value': to_float(item.get('unitValue')),
                'currency_id': self._currency(item.get('currency')).id,
                'hs_code': item.get('hsCode') or False,
                'category': item.get('category') or False,
                'fragile': bool(item.get('fragile')),
                'notes': item.get('notes') or False,
            }))
        vals['line_ids'] = lines

        if parcel:
            parcel.write(vals)
        else:
            parcel = self.create(vals)

        parcel._apply_prealert(parcel_data.get('preAlert') or {})
        parcel._sync_picking()
        parcel._apply_bag_progress(data.get('bagProgress') or {})
        return parcel

    def _apply_prealert(self, pre_alert, prealerte=None):
        """Lie / cree la prealerte Odoo et recopie les infos preAlert."""
        self.ensure_one()
        Prealerte = self.env['skybox.prealerte']
        if pre_alert:
            self.write({
                'prealert_ref': pre_alert.get('preAlertId') or False,
                'prealert_status': pre_alert.get('status') or False,
                'courier_id': self._courier(pre_alert.get('courier')).id,
                'declared_value': to_float(pre_alert.get('declaredValue')),
                'declared_currency_id': self._currency(pre_alert.get('currency')).id,
            })
        if not prealerte:
            prealerte = Prealerte.search(
                [('tracking_number', '=ilike', self.tracking_number)], limit=1)
        if not prealerte and pre_alert:
            prealerte = Prealerte._create_from_check_result({
                'trackingNumber': self.tracking_number,
                'preAlert': pre_alert,
                'suite': {'suiteNumber': self.suite_number},
                'skybillNumber': self.skybill_number,
            })
        if not prealerte:
            return
        vals = {}
        if self.skybill_number and prealerte.skybill != self.skybill_number:
            vals['skybill'] = self.skybill_number
        if self.suite_number and not prealerte.suite_number:
            vals['suite_number'] = self.suite_number
        if self.partner_id and not prealerte.partner_id:
            vals['partner_id'] = self.partner_id.id
        if self.actual_weight:
            vals['matched_weight'] = self.actual_weight
        if vals:
            prealerte.write(vals)
        prealerte._fill_reception_date()
        self.prealerte_id = prealerte

    def _sync_picking(self):
        """Complete la reception Odoo (app mobile) du meme tracking avec les
        infos saisies au traitement Skybox (client, suite, transporteur,
        articles), meme si aucune prealerte n'a ete matchee.
        Colis traite par Skybox (result PROCESSED) : la reception Receipt
        passe en Processed (jamais une reception deja Stored ou annulee).
        Tant qu'il n'est pas stocke, le colis suit le bag envoye par Skybox."""
        for parcel in self:
            picking = parcel.picking_id or self.env['stock.picking'].search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', parcel.tracking_number),
                ('state', '!=', 'cancel'),
            ], limit=1)
            if not picking:
                continue
            vals = {}
            # Infos du traitement Skybox = reference : elles remplacent
            # celles de la reception.
            if parcel.partner_id and picking.client_id != parcel.partner_id:
                vals['client_id'] = parcel.partner_id.id
                vals['client_ref'] = parcel.partner_id.ref or False
                if not picking.partner_id:
                    vals['partner_id'] = parcel.partner_id.id
            if parcel.suite_number and picking.suite_number != parcel.suite_number:
                vals['suite_number'] = parcel.suite_number
            if parcel.ownership_confirmed and picking.prealerte_status != 'matched':
                vals['prealerte_status'] = 'matched'
            if parcel.courier_id and not picking.courier_id:
                vals['courier_id'] = parcel.courier_id.id
            bag = parcel.destination_bag_id or parcel.source_bag_id
            moved = (bag and picking.skybox_status in ('receipt', 'processed')
                     and picking.bag != bag)
            if moved:
                vals['bag'] = bag.id
            # Jamais revenir en arriere : un colis deja range (Stored) reste
            # Stored meme si Skybox renvoie une mise a jour du colis.
            processed = (parcel.result == 'PROCESSED'
                         and picking.skybox_status in ('receipt',))
            if processed:
                vals['skybox_status'] = 'processed'
            if vals:
                picking.write(vals)
            if moved:
                picking._log_history(
                    'moved', bag=bag,
                    note="Mis dans le bag %s par Skybox" % bag.name)
            if parcel.picking_id != picking:
                parcel.picking_id = picking
            picking._add_products(parcel.line_ids)
            picking._log_history(
                'processed', bag=parcel.destination_bag_id,
                note="Skybox %s (revision %s)" % (
                    parcel.last_event_type or '', parcel.revision))

    def _apply_bag_progress(self, progress):
        if not progress or not progress.get('sourceBagCode'):
            return
        bag = self.env['stock.quant.package']._skybox_find_or_create(
            progress['sourceBagCode'])
        bag.write({
            'skybox_expected': to_int(progress.get('expected')),
            'skybox_processed': to_int(progress.get('processed')),
            'skybox_set_aside': to_int(progress.get('setAside')),
            'skybox_missing': to_int(progress.get('missing')),
            'skybox_pending_identification': to_int(
                progress.get('pendingIdentification')),
            'skybox_remaining': to_int(progress.get('remaining')),
            'skybox_remaining_tracking_numbers': "\n".join(
                progress.get('remainingTrackingNumbers') or []),
        })


class SkyboxParcelLine(models.Model):
    _name = 'skybox.parcel.line'
    _description = "Article du colis Skybox"
    _order = 'parcel_id, line_no, id'

    parcel_id = fields.Many2one(
        comodel_name='skybox.parcel', string="Colis",
        required=True, ondelete='cascade', index=True,
    )
    line_no = fields.Integer(string="N°")
    description = fields.Char(string="Description", required=True)
    quantity = fields.Float(string="Quantite", default=1.0)
    currency_id = fields.Many2one(comodel_name='res.currency', string="Devise")
    unit_value = fields.Monetary(
        string="Valeur unitaire", currency_field='currency_id')
    subtotal = fields.Monetary(
        string="Sous-total", currency_field='currency_id',
        compute='_compute_subtotal', store=True,
    )
    hs_code = fields.Char(string="Code HS")
    category = fields.Char(string="Categorie")
    fragile = fields.Boolean(string="Fragile")
    notes = fields.Char(string="Notes")

    @api.depends('quantity', 'unit_value')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.unit_value
