# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


class SkyboxLoginAttempt(models.Model):
    _name = 'skybox.login.attempt'
    _description = "Tentative de connexion API (anti-brute-force)"
    _order = 'id desc'

    # Blocage apres MAX_FAILED echecs, pendant LOCK_MINUTES minutes.
    MAX_FAILED = 5
    LOCK_MINUTES = 15

    ip = fields.Char(string="Adresse IP", index=True)
    login = fields.Char(string="Login", index=True)
    failed_count = fields.Integer(string="Echecs", default=0)
    last_attempt = fields.Datetime(string="Derniere tentative")
    locked_until = fields.Datetime(string="Bloque jusqu'a")

    @api.model
    def _get_or_create(self, ip, login):
        rec = self.sudo().search(
            [('ip', '=', ip), ('login', '=', login)], limit=1)
        if not rec:
            rec = self.sudo().create({'ip': ip, 'login': login})
        return rec

    @api.model
    def _locked_seconds(self, ip, login):
        """Retourne le nombre de secondes de blocage restant, ou 0."""
        rec = self.sudo().search(
            [('ip', '=', ip), ('login', '=', login)], limit=1)
        now = fields.Datetime.now()
        if rec and rec.locked_until and rec.locked_until > now:
            return int((rec.locked_until - now).total_seconds())
        return 0

    @api.model
    def _register_failure(self, ip, login):
        rec = self._get_or_create(ip, login)
        now = fields.Datetime.now()
        count = rec.failed_count + 1
        vals = {'failed_count': count, 'last_attempt': now}
        if count >= self.MAX_FAILED:
            vals['locked_until'] = now + timedelta(minutes=self.LOCK_MINUTES)
            vals['failed_count'] = 0
        rec.write(vals)

    @api.model
    def _register_success(self, ip, login):
        rec = self.sudo().search(
            [('ip', '=', ip), ('login', '=', login)], limit=1)
        if rec:
            rec.write({
                'failed_count': 0,
                'locked_until': False,
                'last_attempt': fields.Datetime.now(),
            })
