# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class SkyboxTransferWizard(models.TransientModel):
    _name = 'skybox.transfer.wizard'
    _description = "Transferer la reception vers un nouvel emplacement"

    picking_id = fields.Many2one(
        comodel_name='stock.picking', string="Reception", required=True,
    )
    current_location_id = fields.Many2one(
        comodel_name='stock.location', string="Emplacement actuel",
        readonly=True,
    )
    new_location_id = fields.Many2one(
        comodel_name='stock.location', string="Nouvel emplacement",
        required=True,
    )

    def action_confirm(self):
        """Deplace la reception vers le nouvel emplacement et journalise."""
        self.ensure_one()
        old_location = self.picking_id.location_dest_id
        if self.new_location_id == old_location:
            raise UserError("Le nouvel emplacement est identique a l'actuel.")
        self.picking_id.location_dest_id = self.new_location_id
        self.picking_id._log_history(
            'moved', location=self.new_location_id,
            note="De %s vers %s" % (
                old_location.display_name or '/',
                self.new_location_id.display_name),
        )
        return {'type': 'ir.actions.act_window_close'}
