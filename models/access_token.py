# -*- coding: utf-8 -*-
import secrets
from datetime import timedelta

from odoo import api, fields, models


class SkyboxAccessToken(models.Model):
    _name = 'skybox.access.token'
    _description = "Jeton d'acces (app mobile)"
    _order = 'id desc'

    # Duree de vie glissante du jeton (prolongee a chaque utilisation).
    TTL_MINUTES = 5

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
            'expiry': now + timedelta(minutes=self.TTL_MINUTES),
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
        rec.write({'last_used': now, 'expiry': now + timedelta(minutes=self.TTL_MINUTES)})
        return rec

    @api.model
    def _gc_expired(self):
        """Desactive les jetons expires (cron) et journalise la deconnexion
        automatique dans l'historique de connexion."""
        expired = self.sudo().search([('expiry', '<', fields.Datetime.now())])
        Log = self.env['skybox.login.log'].sudo()
        for token in expired:
            Log._log(token.user_id, 'logout', 'mobile',
                     note="Session expiree (inactif depuis plus de %s minutes)"
                          % self.TTL_MINUTES, date=token.expiry)
        expired.write({'active': False})
