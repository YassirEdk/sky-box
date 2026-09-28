# -*- coding: utf-8 -*-
from odoo import fields, models


class SkyboxStoreWizard(models.TransientModel):
    _name = 'skybox.store.wizard'
    _description = "Stocker la reception"

    picking_id = fields.Many2one(
        comodel_name='stock.picking', string="Reception", required=True,
    )
    bag = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag",
    )
    location_dest_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement de destination",
    )

    def action_confirm(self):
        """Enregistre le bag / l'emplacement et passe la reception en 'stored'."""
        self.ensure_one()
        self.picking_id.write({
            'bag': self.bag.id,
            'location_dest_id': self.location_dest_id.id,
            'skybox_status': 'stored',
        })
        self.picking_id._log_history(
            'stored', location=self.location_dest_id, bag=self.bag)
        return {'type': 'ir.actions.act_window_close'}
