# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.http import request


class SkyboxLoginLog(models.Model):
    """Historique de connexion : chaque connexion / deconnexion (web et app)."""
    _name = 'skybox.login.log'
    _description = "Historique de connexion"
    _order = 'date desc, id desc'
    _rec_name = 'user_id'

    user_id = fields.Many2one(
        comodel_name='res.users', string="Utilisateur",
        required=True, ondelete='cascade', index=True)
    date = fields.Datetime(string="Date et heure", required=True, default=fields.Datetime.now)
    event = fields.Selection(
        selection=[('login', "Connexion"), ('logout', "Deconnexion")],
        string="Evenement", required=True)
    source = fields.Selection(
        selection=[
            ('web', "Odoo"),
            ('mobile', "API (app mobile)"),
            ('rpc', "API externe (XML/JSON-RPC)"),
        ],
        string="Type de connexion", required=True)
    ip = fields.Char(string="Adresse IP")
    note = fields.Char(string="Detail")

    @api.model
    def _log(self, user, event, source, note=False, date=False):
        ip = False
        try:
            ip = request.httprequest.remote_addr if request else False
        except RuntimeError:
            pass
        return self.sudo().create({
            'user_id': user.id,
            'event': event,
            'source': source,
            'ip': ip,
            'note': note,
            'date': date or fields.Datetime.now(),
        })
