# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.http import request


class ResUsers(models.Model):
    _inherit = 'res.users'

    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string="Entrepot",
        help="Entrepot auquel cet utilisateur est affecte.",
    )

    is_skybox_picker = fields.Boolean(
        string="Picker Skybox", compute='_compute_skybox_picker')
    skybox_connected = fields.Boolean(
        string="Dans le cycle", compute='_compute_skybox_picker')
    skybox_open_order_count = fields.Integer(
        string="Ordres actifs", compute='_compute_skybox_picker')
    skybox_left_cycle_at = fields.Datetime(
        string="Sorti du cycle le", readonly=True, copy=False,
        help="Deconnexion / inactivite du picker. Ses ordres sont redistribues "
             "apres le delai configure, sauf s'il se reconnecte avant.")
    skybox_reassign_at = fields.Datetime(
        string="Redistribution prevue", compute='_compute_skybox_picker')
    skybox_picker_log_ids = fields.One2many(
        comodel_name='skybox.picker.log', inverse_name='user_id',
        string="Historique picker")

    skybox_login_log_count = fields.Integer(
        string="Historique de connexion", compute='_compute_skybox_login_log_count')

    def _compute_skybox_login_log_count(self):
        counts = dict(self.env['skybox.login.log'].sudo()._read_group(
            [('user_id', 'in', self._origin.ids)], ['user_id'], ['__count']))
        for user in self:
            user.skybox_login_log_count = counts.get(user._origin, 0)

    def action_skybox_login_log(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'skybox.action_skybox_login_log')
        action['domain'] = [('user_id', '=', self.id)]
        action['context'] = {'default_user_id': self.id}
        return action

    def _update_last_login(self):
        # Connexion a Odoo (web). Les connexions de l'app mobile passent aussi
        # par ici : elles sont journalisees par /skybox/api/login.
        res = super()._update_last_login()
        path = request.httprequest.path if request else ''
        if not path.startswith('/skybox/api/'):
            source = 'rpc' if path.startswith(('/xmlrpc', '/jsonrpc')) else 'web'
            self.env['skybox.login.log']._log(self.env.user, 'login', source)
        return res

    @api.depends('groups_id')
    def _compute_skybox_picker(self):
        picker = self.env.ref('skybox.group_skybox_picker', raise_if_not_found=False)
        agent = self.env.ref('skybox.group_skybox_agent', raise_if_not_found=False)
        Order = self.env['skybox.preparation.order'].sudo()
        connected = {t.user_id.id for t in Order._picker_cycle()}
        reassign_at = Order._picker_reassign_at(self._origin)
        for user in self:
            user.skybox_reassign_at = reassign_at.get(user._origin.id, False)
            groups = user.groups_id
            user.is_skybox_picker = bool(
                picker and picker in groups and not (agent and agent in groups))
            user.skybox_connected = user._origin.id in connected
            user.skybox_open_order_count = Order.search_count(
                Order._open_orders_domain() + [('agent_id', '=', user._origin.id)]
            ) if user._origin.id else 0

    def action_skybox_open_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'skybox.action_skybox_preparation_order')
        action['domain'] = [('agent_id', '=', self.id)]
        action['context'] = {'search_default_open': 1}
        return action

    # Rendre le champ lisible/modifiable par l'utilisateur sur son propre profil
    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ['warehouse_id']

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['warehouse_id']
