# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockQuantPackage(models.Model):
    _inherit = 'stock.quant.package'

    # Commandes encore dans le bag : receptions pas encore stockees
    # (Receipt, ou Processed par Skybox ; toujours en entree / WH/Input).
    skybox_order_ids = fields.One2many(
        comodel_name='stock.picking', inverse_name='bag',
        string="Commandes dans le bag",
        domain=[('skybox_status', 'in', ('receipt', 'processed'))],
    )
    skybox_prealerte_ids = fields.One2many(
        comodel_name='skybox.prealerte', inverse_name='bag_id',
        string="Prealertes dans le bag",
    )
    skybox_order_count = fields.Integer(
        string="Articles", compute='_compute_skybox_order_count',
    )

    @api.depends('skybox_order_ids')
    def _compute_skybox_order_count(self):
        for bag in self:
            bag.skybox_order_count = len(bag.skybox_order_ids)

    # --- Ouverture / fermeture ---
    # Un bag est ferme par defaut. Cycle :
    #   reception (app mobile) : l'agent l'ouvre (open), y recoit les colis,
    #     puis le ferme ;
    #   traitement (backoffice Skybox) : Skybox scanne le bag ferme
    #     (processing), recupere ses colis, les traite, puis le referme.
    # Skybox ne peut pas traiter un bag ouvert en reception.
    skybox_open_state = fields.Selection(
        selection=[('closed', "Ferme"),
                   ('open', "Ouvert (reception)"),
                   ('processing', "Ouvert (traitement)")],
        string="Etat du bag", default='closed', required=True, index=True,
    )
    skybox_opened_at = fields.Datetime(string="Ouvert le", readonly=True)
    skybox_opened_by = fields.Many2one(
        comodel_name='res.users', string="Ouvert par", readonly=True)
    skybox_closed_at = fields.Datetime(string="Ferme le", readonly=True)
    skybox_closed_by = fields.Many2one(
        comodel_name='res.users', string="Ferme par", readonly=True)
    # Affichage : agent mobile, sinon operateur du backoffice Skybox.
    skybox_opened_by_label = fields.Char(
        string="Ouvert par", compute='_compute_skybox_open_labels')
    skybox_closed_by_label = fields.Char(
        string="Ferme par", compute='_compute_skybox_open_labels')

    @api.depends('skybox_opened_by', 'skybox_closed_by', 'skybox_opened_at',
                 'skybox_closed_at', 'skybox_started_by', 'skybox_completed_by')
    def _compute_skybox_open_labels(self):
        for bag in self:
            if bag.skybox_opened_by:
                bag.skybox_opened_by_label = bag.skybox_opened_by.name
            elif bag.skybox_opened_at:
                bag.skybox_opened_by_label = "Skybox : %s" % (
                    bag.skybox_started_by or '-')
            else:
                bag.skybox_opened_by_label = False
            if bag.skybox_closed_by:
                bag.skybox_closed_by_label = bag.skybox_closed_by.name
            elif bag.skybox_closed_at:
                bag.skybox_closed_by_label = "Skybox : %s" % (
                    bag.skybox_completed_by or '-')
            else:
                bag.skybox_closed_by_label = False

    def _skybox_open(self):
        """Ouvre le bag ; l'utilisateur courant (env.user) est l'agent."""
        self.write({
            'skybox_open_state': 'open',
            'skybox_opened_at': fields.Datetime.now(),
            'skybox_opened_by': self.env.user.id,
        })

    def _skybox_close(self):
        """Ferme le bag ; l'utilisateur courant (env.user) est l'agent."""
        self.write({
            'skybox_open_state': 'closed',
            'skybox_closed_at': fields.Datetime.now(),
            'skybox_closed_by': self.env.user.id,
        })

    def _skybox_open_processing(self, operator=None, session_id=None):
        """Ouvre le bag pour le traitement Skybox (scan backoffice)."""
        now = fields.Datetime.now()
        vals = {
            'skybox_open_state': 'processing',
            'skybox_opened_at': now,
            'skybox_opened_by': False,
            'skybox_started_at': now,
            'skybox_started_by': operator or False,
        }
        if session_id:
            vals['skybox_session_id'] = session_id
        self.write(vals)

    def _skybox_close_processing(self, operator=None):
        """Referme le bag a la fin du traitement Skybox."""
        now = fields.Datetime.now()
        self.write({
            'skybox_open_state': 'closed',
            'skybox_closed_at': now,
            'skybox_closed_by': False,
            'skybox_completed_at': now,
            'skybox_completed_by': operator or False,
        })

    def _skybox_backoffice_dict(self):
        """Etat du bag et colis a traiter, renvoyes au backoffice Skybox."""
        self.ensure_one()

        def iso(value):
            return value.strftime('%Y-%m-%dT%H:%M:%SZ') if value else None

        parcels = [{
            'trackingNumber': p['tracking_number'],
            'partnerReference': p['reference'],
            'status': p['status'],
            'preAlert': p['prealerte'],
            'suiteNumber': p['suite_number'],
            'customer': p['client'],
            'courier': p['courier'],
            'weightKg': p['weight_kg'],
            'lengthCm': p['length_cm'],
            'widthCm': p['width_cm'],
            'heightCm': p['height_cm'],
        } for p in self._skybox_bag_contents()]
        return {
            'bagCode': self.name,
            'state': self.skybox_open_state,
            'openedAt': iso(self.skybox_opened_at),
            'openedBy': self.skybox_started_by or None,
            'closedAt': iso(self.skybox_closed_at),
            'sessionId': self.skybox_session_id or None,
            'count': len(parcels),
            'parcels': parcels,
        }

    def _skybox_state_dict(self):
        """Etat du bag renvoye a l'app mobile."""
        self.ensure_one()
        return {
            'bag': self.name,
            'bag_id': self.id,
            'state': self.skybox_open_state,
            'opened_at': fields.Datetime.to_string(self.skybox_opened_at) or None,
            'opened_by': self.skybox_opened_by_label or None,
            'opened_by_id': self.skybox_opened_by.id or None,
            'closed_at': fields.Datetime.to_string(self.skybox_closed_at) or None,
            'closed_by': self.skybox_closed_by_label or None,
            'closed_by_id': self.skybox_closed_by.id or None,
            'count': self.skybox_order_count,
        }

    def _skybox_other_open_bag(self, user):
        """Autre bag deja ouvert en reception par cet agent (un seul a la fois)."""
        return self.search([
            ('skybox_open_state', '=', 'open'),
            ('skybox_opened_by', '=', user.id),
            ('id', 'not in', self.ids),
        ], limit=1)

    def action_skybox_open(self):
        bags = self.filtered(lambda b: b.skybox_open_state == 'closed')
        other = bags._skybox_other_open_bag(self.env.user)
        if other or len(bags) > 1:
            raise UserError(_(
                "Un seul bag ouvert a la fois : fermez d'abord le bag %s.",
                other.name or bags[0].name))
        bags._skybox_open()

    def action_skybox_close(self):
        self.filtered(lambda b: b.skybox_open_state != 'closed')._skybox_close()

    # --- Suivi Skybox (BAG_COMPLETED / bagProgress) ---
    skybox_bag_status = fields.Char(string="Statut Skybox")
    skybox_session_id = fields.Char(string="Session Skybox")
    skybox_warehouse_code = fields.Char(string="Code entrepot Skybox")
    skybox_detail_level = fields.Char(string="Niveau de detail")
    skybox_started_at = fields.Datetime(string="Debut du traitement")
    skybox_started_by = fields.Char(string="Commence par")
    skybox_completed_at = fields.Datetime(string="Fin du traitement")
    skybox_completed_by = fields.Char(string="Termine par")
    skybox_expected = fields.Integer(string="Attendus")
    skybox_processed = fields.Integer(string="Traites")
    skybox_set_aside = fields.Integer(string="Mis de cote")
    skybox_missing = fields.Integer(string="Manquants")
    skybox_unexpected_kept = fields.Integer(string="Inattendus gardes")
    skybox_unexpected_set_aside = fields.Integer(string="Inattendus mis de cote")
    skybox_pending_identification = fields.Integer(string="En attente d'identification")
    skybox_remaining = fields.Integer(string="Restants")
    skybox_remaining_tracking_numbers = fields.Text(string="Trackings restants")
    skybox_parcels_json = fields.Text(string="Colis (JSON Skybox)")
    skybox_last_event_id = fields.Char(string="Dernier eventId")

    @api.model
    def _skybox_find_or_create(self, code, create=True):
        """Bag par son nom (code Skybox) ; cree s'il n'existe pas."""
        code = (code or '').strip()
        if not code:
            return self.browse()
        bag = self.search([('name', '=ilike', code)], limit=1)
        if not bag and create:
            bag = self.create({'name': code})
        return bag

    def _skybox_bag_contents(self):
        """Colis presents dans le bag (pour le scan de l'app mobile) :
        prealertes affectees au bag + receptions encore dans le bag (Receipt
        ou Processed). Un colis stocke ou annule (sorti du bag) n'est pas
        renvoye."""
        self.ensure_one()
        Picking = self.env['stock.picking']
        receipts = Picking.search([
            ('picking_type_code', '=', 'incoming'),
            ('bag', '=', self.id),
            ('skybox_status', 'in', ('receipt', 'processed')),
            ('state', '!=', 'cancel'),
        ])
        by_tracking = {(p.tracking_number or '').lower(): p for p in receipts}
        result, seen = [], set()

        for pre in self.skybox_prealerte_ids:
            key = (pre.tracking_number or '').lower()
            picking = by_tracking.get(key) or Picking.search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', pre.tracking_number or ''),
                ('state', '!=', 'cancel'),
            ], limit=1)
            if picking and picking.skybox_status in ('stored', 'canceled'):
                continue  # stocke / annule : plus dans le bag
            seen.add(key)
            result.append(self._skybox_colis_dict(pre, picking))

        for key, picking in by_tracking.items():
            if key not in seen:
                result.append(self._skybox_colis_dict(False, picking))
        return result

    @api.model
    def _skybox_bags_to_store(self, bag=None):
        """Bags a ranger (app mobile) : bags dont des colis ont ete traites
        par Skybox (Processed) et ne sont pas encore stockes, avec ces colis.
        bag : limiter a ce bag."""
        domain = [
            ('picking_type_code', '=', 'incoming'),
            ('skybox_status', '=', 'processed'),
            ('bag', '!=', False),
            ('state', '!=', 'cancel'),
        ]
        if bag:
            domain.append(('bag', '=', bag.id))
        pickings = self.env['stock.picking'].search(domain, order='bag, id')
        result = []
        for bag in pickings.mapped('bag'):
            prealertes = {(pre.tracking_number or '').lower(): pre
                          for pre in bag.skybox_prealerte_ids}
            parcels = [bag._skybox_colis_dict(
                prealertes.get((p.tracking_number or '').lower(), False), p)
                for p in pickings.filtered(lambda p, b=bag: p.bag == b)]
            result.append({
                'bag': bag.name,
                'bag_id': bag.id,
                'skybox_status': bag.skybox_bag_status or None,
                'state': bag.skybox_open_state,
                'count': len(parcels),
                'parcels': parcels,
            })
        return result

    @api.model
    def _skybox_colis_dict(self, prealerte, picking):
        partner = (prealerte.partner_id if prealerte else False) or (
            picking.client_id if picking else False)
        return {
            'tracking_number': (prealerte.tracking_number if prealerte
                                else picking.tracking_number),
            'prealerte': prealerte.name if prealerte else None,
            'prealerte_id': prealerte.id if prealerte else None,
            'processing_state': prealerte.processing_state if prealerte else None,
            'skybill': (prealerte.skybill if prealerte else None) or None,
            'suite_number': ((prealerte.suite_number if prealerte else None)
                             or (picking.suite_number if picking else None)
                             or None),
            'client': partner.name if partner else None,
            'courier': ((prealerte.courier_id.name if prealerte else None)
                        or (picking.courier_id.name if picking else None)
                        or None),
            'weight_kg': prealerte.matched_weight if prealerte else None,
            'length_cm': prealerte.length_cm if prealerte else None,
            'width_cm': prealerte.width_cm if prealerte else None,
            'height_cm': prealerte.height_cm if prealerte else None,
            'picking_id': picking.id if picking else None,
            'reference': picking.name if picking else None,
            'status': picking.skybox_status if picking else None,
        }
