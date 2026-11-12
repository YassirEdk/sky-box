# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SkyboxBagType(models.Model):
    _name = 'skybox.bag.type'
    _description = "Type de bag"
    _order = 'sequence, name'

    name = fields.Char(string="Nom", required=True)
    code = fields.Char(string="Code")
    description = fields.Text(string="Description")
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(string="Actif", default=True)
    bag_count = fields.Integer(string="Bags", compute='_compute_bag_count')

    _sql_constraints = [
        ('name_uniq', 'unique(name)', "Ce type de bag existe deja."),
    ]

    def _compute_bag_count(self):
        Package = self.env['stock.quant.package']
        data = Package._read_group(
            [('skybox_bag_type_id', 'in', self.ids)],
            groupby=['skybox_bag_type_id'], aggregates=['__count'])
        counts = {bag_type.id: count for bag_type, count in data}
        for bag_type in self:
            bag_type.bag_count = counts.get(bag_type.id, 0)

    def action_view_bags(self):
        """Ouvre la liste des bags de ce type."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Bags - %s" % self.name,
            'res_model': 'stock.quant.package',
            'view_mode': 'list,form',
            'domain': [('skybox_bag_type_id', '=', self.id)],
            'context': {'default_skybox_bag_type_id': self.id},
        }
