# -*- coding: utf-8 -*-
from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    skybox_code = fields.Char(
        string="Code Skybox", index=True,
        help="warehouseCode envoye par Skybox (ex: WH-2026-00003).",
    )
