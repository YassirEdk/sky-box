# -*- coding: utf-8 -*-
import secrets
from datetime import timedelta

from odoo import api, fields, models


class SkyboxAccessToken(models.Model):
    _name = 'skybox.access.token'
    _description = "Jeton d'acces (app mobile)"
    _order = 'id desc'

    # Duree de vie glissante du jeton (prolongee a chaque utilisation).
    TTL_DAYS = 1

    token = fields.Char(
        string="Jeton", required=True, index=True, copy=False, readonly=True,
    )
    user_id = fields.Many2one(
        comodel_name='res.users', string="Utilisateur",
        required=True, ondelete='cascade', index=True,
    )
    api_key_id = fields.Many2one(
        comodel_name='skybox.api.key', string="Cle Login",
        ondelete='set null',
    )
    expiry = fields.Datetime(string="Expiration", required=True)
    last_used = fields.Datetime(string="Derniere utilisation")
    active = fields.Boolean(string="Actif", default=True)

    _sql_constraints = [
        ('token_uniq', 'unique(token)', "Le jeton doit etre unique."),
    ]

    @api.model
    def _issue(self, user, api_key=None):
        """Cree un nouveau jeton d'acces pour l'utilisateur."""
        now = fields.Datetime.now()
        return self.sudo().create({
            'token': secrets.token_urlsafe(48),
            'user_id': user.id,
            'api_key_id': api_key.id if api_key else False,
            'expiry': now + timedelta(days=self.TTL_DAYS),
            'last_used': now,
        })

    @api.model
    def _validate(self, token):
        """Retourne le jeton valide (non expire) et prolonge sa duree de vie,
        ou un recordset vide."""
        if not token:
            return self.browse()
        rec = self.sudo().search([
            ('token', '=', token), ('active', '=', True),
        ], limit=1)
        now = fields.Datetime.now()
        if not rec or (rec.expiry and rec.expiry < now):
            return self.browse()
        # Expiration glissante.
        rec.write({'last_used': now, 'expiry': now + timedelta(days=self.TTL_DAYS)})
        return rec

    @api.model
    def _gc_expired(self):
        """Nettoyage optionnel des jetons expires (cron)."""
        expired = self.sudo().search([('expiry', '<', fields.Datetime.now())])
        expired.write({'active': False})
