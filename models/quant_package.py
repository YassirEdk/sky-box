# -*- coding: utf-8 -*-
from odoo import api, fields, models


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
