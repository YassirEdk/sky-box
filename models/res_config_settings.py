# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    skybox_picker_reassign_delay = fields.Integer(
        string="Delai de redistribution (minutes)",
        config_parameter='skybox.picker_reassign_delay', default=60,
        help="Quand un picker se deconnecte (ou reste inactif), ses ordres "
             "en cours lui sont gardes pendant ce delai. Passe ce delai, ils "
             "sont redistribues aux autres pickers connectes. 0 = immediat.")
