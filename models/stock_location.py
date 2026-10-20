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

    def _skybox_position(self):
        """Position Skybox d'un emplacement, d'apres l'arborescence sous le
        stock de l'entrepot : WH/Stock/<zone>/<allee>/<rack>/<bin>.
        Le dernier niveau est toujours le locationCode (code-barres, sinon
        nom) ; les niveaux au-dessus donnent zoneCode, aisleCode, rackCode.
        Ex. WH/Stock/ZONE-A/A01/R03/BIN-04 -> ZONE-A / A01 / R03 / BIN-04 ;
        WH/Stock/Zone A1 -> locationCode "Zone A1"."""
        self.ensure_one()
        stock = self.warehouse_id.lot_stock_id
        chain, loc = [], self
        while loc and loc != stock and loc.usage == 'internal':
            chain.insert(0, loc)
            loc = loc.location_id
        if not chain:
            chain = [self]
        leaf, parents = chain[-1], chain[:-1]
        position = {'locationCode': leaf.barcode or leaf.name}
        for key, parent in zip(('zoneCode', 'aisleCode', 'rackCode'), parents[-3:]):
            position[key] = parent.name
        return position

    @api.model
    def _skybox_init_barcodes(self):
        """Remplit le code-barres des emplacements internes existants."""
        self.with_context(active_test=False).search([
            ('usage', '=', 'internal'), ('barcode', '=', False),
        ])._skybox_fill_barcode()
