# -*- coding: utf-8 -*-
from odoo import api, fields, models


class StockQuantPackage(models.Model):
    _inherit = 'stock.quant.package'

    # Commandes encore dans le bag : receptions pas encore stockees
    # (statut Skybox 'receipt', donc toujours en entree / WH/Input).
    skybox_order_ids = fields.One2many(
        comodel_name='stock.picking', inverse_name='bag',
        string="Commandes dans le bag",
        domain=[('skybox_status', '=', 'receipt')],
    )
    skybox_order_count = fields.Integer(
        string="Articles", compute='_compute_skybox_order_count',
    )

    @api.depends('skybox_order_ids')
    def _compute_skybox_order_count(self):
        for bag in self:
            bag.skybox_order_count = len(bag.skybox_order_ids)
