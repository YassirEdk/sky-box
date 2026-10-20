# -*- coding: utf-8 -*-
import pytz

from odoo import fields, models

from .skybox_event import DEFAULT_TIMEZONE, TIMEZONE_PARAM


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        """Heure du serveur pour tout le monde : le client web affiche les
        dates dans le fuseau de Parametres > Skybox, pas dans celui de
        l'ordinateur (voir static/src/js/server_timezone.js)."""
        result = super().session_info()
        name = self.env['ir.config_parameter'].sudo().get_param(
            TIMEZONE_PARAM) or DEFAULT_TIMEZONE
        try:
            tz = pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
            name, tz = 'UTC', pytz.utc
        now = pytz.utc.localize(fields.Datetime.now())
        # Decalage calcule par le serveur (base de fuseaux a jour), utilise
        # si celle du navigateur est perimee (ex. Maroc encore a +1).
        offset = int(now.astimezone(tz).utcoffset().total_seconds() // 60)
        result['skybox_tz'] = {'name': name, 'offset': offset}
        if result.get('user_context'):
            # Regroupements par jour, filtres "aujourd'hui"... a la meme heure.
            result['user_context']['tz'] = name
        return result
