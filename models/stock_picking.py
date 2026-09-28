# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    skybox_status = fields.Selection(
        selection=[
            ('receipt', "Receipt"),
            ('stored', "Stored"),
            ('canceled', "Canceled"),
        ],
        string="Statut", default='receipt', required=True, copy=False,
    )
    prealerte_status = fields.Selection(
        selection=[
            ('ready_to_match', "Ready to match"),
            ('matched', "Matched"),
            ('not_found', "Not found"),
        ],
        string="Prealertes", default='ready_to_match', required=True, copy=False,
    )
    tracking_number = fields.Char(string="Numero de tracking")
    suite_number = fields.Char(string="Suite du client")
    courier_id = fields.Many2one(
        comodel_name='skybox.courier', string="Transporteur",
    )
    client_id = fields.Many2one(
        comodel_name='res.partner', string="Client",
    )
    client_ref = fields.Char(string="Reference client")
    bag = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag",
    )
    notes = fields.Text(string="Notes")

    history_ids = fields.One2many(
        comodel_name='skybox.picking.history', inverse_name='picking_id',
        string="Historique",
    )
    history_count = fields.Integer(
        string="Historique", compute='_compute_history_count',
    )

    @api.depends('history_ids')
    def _compute_history_count(self):
        for picking in self:
            picking.history_count = len(picking.history_ids)

    def _log_history(self, action, location=False, bag=False, note=False):
        """Ajoute une ligne d'historique (qui, quand, ou)."""
        self.env['skybox.picking.history'].create([{
            'picking_id': picking.id,
            'action': action,
            'user_id': self.env.user.id,
            'date': fields.Datetime.now(),
            'location_id': location.id if location else False,
            'bag_id': bag.id if bag else False,
            'note': note,
        } for picking in self])

    def action_view_history(self):
        """Bouton intelligent : ouvre l'historique de la reception."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Historique",
            'res_model': 'skybox.picking.history',
            'view_mode': 'list,form',
            'domain': [('picking_id', '=', self.id)],
            'context': {'default_picking_id': self.id},
        }

    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        note = self.env.context.get('skybox_history_note')
        for picking in pickings.filtered(
                lambda p: p.picking_type_code == 'incoming'):
            picking._log_history(
                'received', location=picking.location_dest_id,
                bag=picking.bag, note=note)
        return pickings

    def action_open_store(self):
        """Ouvre la fenetre 'Stored' (bag + emplacement de destination)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Stored",
            'res_model': 'skybox.store.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
                'default_bag': self.bag.id,
                'default_location_dest_id': self.location_dest_id.id,
            },
        }

    def action_open_transfer(self):
        """Ouvre la fenetre de transfert vers un nouvel emplacement."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Transfert",
            'res_model': 'skybox.transfer.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
                'default_current_location_id': self.location_dest_id.id,
            },
        }

    def action_cancel(self):
        """Annulation : passe aussi le statut Skybox en 'canceled'."""
        res = super().action_cancel()
        incoming = self.filtered(lambda p: p.picking_type_code == 'incoming')
        incoming.write({'skybox_status': 'canceled'})
        incoming._log_history('canceled')
        return res

    def _get_courier(self, name):
        """Retourne (cree si absent) le transporteur par nom."""
        Courier = self.env['skybox.courier']
        courier = Courier.search([('name', '=ilike', name)], limit=1)
        if not courier:
            courier = Courier.create({'name': name})
        return courier

    def _apply_prealerte(self, prealerte):
        """Reprend toutes les infos de la prealerte sur la reception :
        client, suite, transporteur, notes et articles (lignes de mouvement)."""
        self.ensure_one()
        if not prealerte:
            return
        vals = {}
        if prealerte.partner_id:
            vals['client_id'] = prealerte.partner_id.id
            vals['client_ref'] = prealerte.partner_id.ref
            if not self.partner_id:
                vals['partner_id'] = prealerte.partner_id.id
        if prealerte.suite_number:
            vals['suite_number'] = prealerte.suite_number
        if prealerte.courier_id:
            vals['courier_id'] = prealerte.courier_id.id
        if prealerte.notes and not self.notes:
            vals['notes'] = prealerte.notes
        if vals:
            self.write(vals)

        # Articles declares -> lignes de mouvement de la reception.
        Product = self.env['product.product']
        existing_products = self.move_ids_without_package.mapped('product_id')
        move_vals = []
        for line in prealerte.line_ids:
            if not line.description:
                continue
            product = Product.search(
                [('name', '=ilike', line.description)], limit=1)
            if not product:
                product = Product.create({
                    'name': line.description,
                    'type': 'consu',
                    'is_storable': True,
                })
            if product in existing_products:
                continue
            move_vals.append((0, 0, {
                'name': line.description,
                'product_id': product.id,
                'product_uom_qty': line.quantity or 1.0,
                'product_uom': product.uom_id.id,
                'location_id': self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
            }))
        if move_vals:
            self.move_ids_without_package = move_vals

    def _match_prealertes(self):
        """Interroge l'API externe pour les receptions de self, cree les
        prealertes trouvees et met a jour le statut Prealertes
        (matched / not_found). Retourne la liste des details par tracking."""
        api_key_model = self.env['skybox.api.key']
        pickings = self.filtered(lambda p: p.tracking_number)
        numbers = list(dict.fromkeys(pickings.mapped('tracking_number')))
        if not numbers:
            return []

        picking_by_number = {}
        for picking in pickings:
            picking_by_number.setdefault(
                (picking.tracking_number or '').lower(), picking)

        keys = api_key_model.search([
            ('active', '=', True), ('url', '!=', False),
            ('api_type', '=', 'api_externe'),
        ])
        prealerte_model = self.env['skybox.prealerte']
        details = []
        for key in keys:
            result = key._check_receptions(numbers)
            if not result:
                continue
            for item in (result.get('results') or []):
                if not isinstance(item, dict):
                    continue
                prealerte = prealerte_model._create_from_check_result(item)
                status = item.get('status')
                pre_alert = item.get('preAlert') or {}
                picking = picking_by_number.get(
                    (item.get('trackingNumber') or '').lower())
                if picking:
                    if prealerte:
                        picking.prealerte_status = 'matched'
                        picking._apply_prealerte(prealerte)
                    elif status == 'ALREADY_PROCESSED':
                        picking.prealerte_status = 'matched'
                        suite = (item.get('suite') or {}).get('suiteNumber')
                        if suite:
                            picking.suite_number = suite
                        if pre_alert.get('courier'):
                            picking.courier_id = self._get_courier(
                                pre_alert['courier']).id
                    else:
                        picking.prealerte_status = 'not_found'
                details.append({
                    'tracking_number': item.get('trackingNumber'),
                    'status': status,
                    'message': item.get('message'),
                    'skybill': item.get('skybillNumber'),
                    'courier': pre_alert.get('courier'),
                    'declared_value': pre_alert.get('declaredValue') or 0.0,
                    'currency': pre_alert.get('currency'),
                    'prealerte_id': prealerte.id if prealerte else False,
                })
        return details

    def action_check_tn(self):
        """Bouton 'Match Prealertes' : matche les receptions selectionnees et
        ouvre le resume."""
        if not self.env['skybox.api.key'].search_count([
            ('active', '=', True), ('url', '!=', False),
            ('api_type', '=', 'api_externe'),
        ]):
            raise UserError(
                "Aucune API externe active n'est configuree "
                "(menu Configuration > API management).")

        pickings = self.filtered(lambda p: p.tracking_number)
        if not pickings:
            raise UserError("Aucun numero de tracking a verifier.")

        details = pickings._match_prealertes()
        created = sum(1 for d in details if d['prealerte_id'])
        wizard = self.env['skybox.check.tn.wizard'].create({
            'summary': "%s tracking(s) verifie(s), %s prealerte(s)." % (
                len(details), created),
            'line_ids': [(0, 0, {
                'tracking_number': d['tracking_number'],
                'status': d['status'],
                'message': d['message'],
                'skybill': d['skybill'],
                'courier': d['courier'],
                'declared_value': d['declared_value'],
                'currency': d['currency'],
                'prealerte_id': d['prealerte_id'],
            }) for d in details],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': "Resultat Check TN",
            'res_model': 'skybox.check.tn.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    @api.model
    def _cron_match_prealertes(self):
        """Cron : matche les receptions dont le statut Prealertes est
        'ready_to_match' ou 'not_found'."""
        pickings = self.search([
            ('picking_type_code', '=', 'incoming'),
            ('tracking_number', '!=', False),
            ('prealerte_status', 'in', ('ready_to_match', 'not_found')),
        ])
        if not pickings:
            _logger.info("Skybox: aucune reception a matcher.")
            return
        pickings._match_prealertes()
