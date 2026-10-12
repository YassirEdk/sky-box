# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, http
from odoo.http import request

from odoo.addons.web.controllers.session import Session


class SkyboxSession(Session):

    # readonly=False : on ecrit l'historique avant la deconnexion.
    @http.route('/web/session/logout', type='http', auth='none', readonly=False)
    def logout(self, redirect='/odoo'):
        uid = request.session.uid
        if uid and request.db:
            env = request.env(user=SUPERUSER_ID)
            user = env['res.users'].browse(uid).exists()
            if user:
                env['skybox.login.log']._log(user, 'logout', 'web')
        return super().logout(redirect=redirect)
