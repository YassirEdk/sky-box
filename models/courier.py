# -*- coding: utf-8 -*-
from odoo import fields, models


class SkyboxCourier(models.Model):
    _name = 'skybox.courier'
    _description = "Transporteur"
    _order = 'name'

    name = fields.Char(string="Nom", required=True)
    code = fields.Char(string="Code")
    active = fields.Boolean(string="Actif", default=True)
