# -*- coding: utf-8 -*-
from odoo import api, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    def _skybox_barcode_candidate(self):
        """Code-barres = nom de l'emplacement ; nom complet si deja pris."""
        self.ensure_one()
        name = (self.name or '').strip()
        if not name:
            return False
        taken = self.with_context(active_test=False).search_count([
            ('id', '!=', self.id),
            ('barcode', '=', name),
            ('company_id', '=', self.company_id.id),
        ])
        return self.complete_name if taken else name

    def _skybox_fill_barcode(self, only_empty=True):
        for loc in self.filtered(lambda l: l.usage == 'internal'):
            if only_empty and loc.barcode:
                continue
            barcode = loc._skybox_barcode_candidate()
            if barcode and barcode != loc.barcode:
                super(StockLocation, loc).write({'barcode': barcode})

    @api.model_create_multi
    def create(self, vals_list):
        locations = super().create(vals_list)
        locations._skybox_fill_barcode()
        return locations

    def write(self, vals):
        if 'name' not in vals or 'barcode' in vals:
            return super().write(vals)
        # Barcode encore "automatique" (vide ou = ancien nom) -> suit le nouveau nom
        auto = self.filtered(
            lambda l: not l.barcode or l.barcode in (l.name, l.complete_name))
        res = super().write(vals)
        auto._skybox_fill_barcode(only_empty=False)
        return res

    @api.model
    def _skybox_init_barcodes(self):
        """Remplit le code-barres des emplacements internes existants."""
        self.with_context(active_test=False).search([
            ('usage', '=', 'internal'), ('barcode', '=', False),
        ])._skybox_fill_barcode()
