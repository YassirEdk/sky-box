# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SkyboxPickerLog(models.Model):
    """Historique des connexions des pickers et des ordres redistribues."""
    _name = 'skybox.picker.log'
    _description = "Historique picker"
    _order = 'date desc, id desc'

    user_id = fields.Many2one(
        comodel_name='res.users', string="Picker",
        required=True, ondelete='cascade', index=True)
    date = fields.Datetime(string="Date", required=True, default=fields.Datetime.now)
    event = fields.Selection(
        selection=[
            ('login', "Connexion"),
            ('logout', "Deconnexion"),
            ('inactive', "Inactif / sorti du cycle"),
            ('reassigned', "Ordres redistribues"),
        ],
        string="Evenement", required=True)
    order_ids = fields.Many2many(
        comodel_name='skybox.preparation.order', string="Ordres actifs")
    order_count = fields.Integer(string="Nb ordres actifs")
    warning = fields.Boolean(
        string="Alerte", help="Sorti du cycle avec des ordres actifs affectes.")
    note = fields.Text(string="Detail")

    @api.model
    def _log(self, user, event, orders=None, note=False):
        orders = orders or self.env['skybox.preparation.order']
        return self.create({
            'user_id': user.id,
            'event': event,
            'order_ids': [(6, 0, orders.ids)],
            'order_count': len(orders),
            'warning': event != 'login' and bool(orders),
            'note': note,
        })
