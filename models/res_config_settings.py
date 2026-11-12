# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.addons.base.models.res_partner import _tz_get

from .skybox_event import DEFAULT_TIMEZONE, TIMEZONE_PARAM


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    skybox_picker_reassign_delay = fields.Integer(
        string="Delai de redistribution (minutes)",
        config_parameter='skybox.picker_reassign_delay', default=60,
        help="Quand un picker se deconnecte (ou reste inactif), ses ordres "
             "en cours lui sont gardes pendant ce delai. Passe ce delai, ils "
             "sont redistribues aux autres pickers connectes. 0 = immediat.")

    skybox_batch_size = fields.Integer(
        string="Colis par batch",
        config_parameter='skybox.batch_size', default=10,
        help="Nombre de colis regroupes automatiquement dans un batch (vague "
             "de picking) des qu'assez de colis prets s'accumulent. "
             "0 = pas de creation automatique (batches crees a la main).")

    skybox_warehouse_code = fields.Char(
        string="Code entrepot",
        config_parameter='skybox.warehouse_code',
        help="Code entrepot envoye dans le champ warehouseCode des appels "
             "vers Skybox (ex: WH-2026-00002). Defini une seule fois ici pour "
             "toute l'integration.")

    skybox_timezone = fields.Selection(
        selection=_tz_get, string="Fuseau horaire",
        config_parameter=TIMEZONE_PARAM, default=DEFAULT_TIMEZONE,
        help="Heure de travail : toutes les dates d'Odoo s'affichent a cette "
             "heure, quel que soit le fuseau de l'ordinateur, et les dates "
             "envoyees a Skybox partent avec son decalage (Skybox les "
             "convertit en UTC).")

    # Zones envoyees a Skybox dans les mouvements d'entrepot.
    skybox_zone_picking = fields.Char(
        string="Zone de picking", config_parameter='skybox.zone_picking', default='PICK')
    skybox_zone_packing = fields.Char(
        string="Zone d'emballage", config_parameter='skybox.zone_packing', default='PACK')
    skybox_zone_staging = fields.Char(
        string="Zone de quai", config_parameter='skybox.zone_staging', default='QUAI')
    skybox_zone_dispatch = fields.Char(
        string="Zone de sortie", config_parameter='skybox.zone_dispatch', default='SORTIE')
    skybox_zone_exception = fields.Char(
        string="Zone d'isolement", config_parameter='skybox.zone_exception',
        default='ISOLEMENT')
