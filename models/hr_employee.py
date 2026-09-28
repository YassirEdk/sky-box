# -*- coding: utf-8 -*-
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string="Entrepot",
        help="Entrepot auquel cet employe est affecte.",
    )
