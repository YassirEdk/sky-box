# -*- coding: utf-8 -*-
from odoo import fields, models


class SkyboxCheckTnWizard(models.TransientModel):
    _name = 'skybox.check.tn.wizard'
    _description = "Resultat du Check TN"

    summary = fields.Char(string="Resume", readonly=True)
    line_ids = fields.One2many(
        comodel_name='skybox.check.tn.line',
        inverse_name='wizard_id', string="Resultats",
    )


class SkyboxCheckTnLine(models.TransientModel):
    _name = 'skybox.check.tn.line'
    _description = "Ligne de resultat Check TN"

    wizard_id = fields.Many2one(
        comodel_name='skybox.check.tn.wizard', ondelete='cascade',
    )
    tracking_number = fields.Char(string="Numero de tracking", readonly=True)
    status = fields.Char(string="Statut", readonly=True)
    message = fields.Char(string="Message", readonly=True)
    skybill = fields.Char(string="Skybill", readonly=True)
    courier = fields.Char(string="Transporteur", readonly=True)
    declared_value = fields.Float(string="Valeur declaree", readonly=True)
    currency = fields.Char(string="Devise", readonly=True)
    prealerte_id = fields.Many2one(
        comodel_name='skybox.prealerte', string="Prealerte", readonly=True,
    )
