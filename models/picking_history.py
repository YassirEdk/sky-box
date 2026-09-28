# -*- coding: utf-8 -*-
from odoo import fields, models


class SkyboxPickingHistory(models.Model):
    _name = 'skybox.picking.history'
    _description = "Historique de la reception"
    _order = 'date desc, id desc'

    picking_id = fields.Many2one(
        comodel_name='stock.picking', string="Reception",
        required=True, ondelete='cascade', index=True,
    )
    action = fields.Selection(
        selection=[
            ('received', "Received"),
            ('stored', "Stored"),
            ('moved', "Transferred"),
            ('canceled', "Canceled"),
        ],
        string="Action", required=True,
    )
    user_id = fields.Many2one(
        comodel_name='res.users', string="Agent",
        default=lambda self: self.env.user,
    )
    date = fields.Datetime(
        string="Date", default=fields.Datetime.now,
    )
    location_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement",
    )
    bag_id = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag",
    )
    note = fields.Char(string="Note")
