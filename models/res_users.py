# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string="Entrepot",
        help="Entrepot auquel cet utilisateur est affecte.",
    )

    # Rendre le champ lisible/modifiable par l'utilisateur sur son propre profil
    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ['warehouse_id']

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['warehouse_id']
