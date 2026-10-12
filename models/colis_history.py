# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import api, fields, models

KIND_ICONS = {
    'prealerte': 'fa-bell',
    'reception': 'fa-inbox',
    'parcel': 'fa-cube',
    'event': 'fa-exchange',
    'order': 'fa-clipboard',
    'done': 'fa-truck',
}


class SkyboxColisHistory(models.TransientModel):
    """Pop-up : historique d'un colis (de la prealerte a l'expedition)."""
    _name = 'skybox.colis.history'
    _description = "Historique colis"

    order_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Ordre", ondelete='cascade')
    tracking_number = fields.Char(string="Colis")
    partner_id = fields.Many2one(related='order_id.partner_id', string="Client")
    carrier = fields.Char(related='order_id.carrier', string="Transporteur")
    carrier_tracking_number = fields.Char(
        related='order_id.carrier_tracking_number', string="Suivi transporteur")
    step_count = fields.Integer(string="Etapes", compute='_compute_history')
    history_html = fields.Html(
        string="Historique", compute='_compute_history', sanitize=False)

    @api.depends('order_id', 'tracking_number')
    def _compute_history(self):
        for wiz in self:
            rows = wiz.order_id._history_rows(wiz.tracking_number) if wiz.order_id else []
            wiz.step_count = len(rows)
            wiz.history_html = wiz._render(rows)

    def _render(self, rows):
        if not rows:
            return Markup('<p class="text-muted">Aucun historique pour ce colis.</p>')
        items = []
        for row in rows:
            date = fields.Datetime.context_timestamp(self, row['date'])
            items.append(Markup(
                '<li class="o_skybox_tl_item o_skybox_tl_item--%s">'
                '<span class="o_skybox_tl_dot"><i class="fa %s" title="%s"></i></span>'
                '<div class="o_skybox_tl_body">'
                '<div class="o_skybox_tl_head">'
                '<strong>%s</strong>'
                '<span class="o_skybox_tl_date">%s</span>'
                '</div>%s%s</div></li>'
            ) % (
                row['kind'], KIND_ICONS.get(row['kind'], 'fa-circle'), escape(row['step']),
                escape(row['step']),
                date.strftime('%d/%m/%Y %H:%M:%S'),
                Markup('<div class="o_skybox_tl_detail">%s</div>') % row['detail']
                if row['detail'] else '',
                Markup('<div class="o_skybox_tl_user"><i class="fa fa-user me-1" title="Par"></i>%s</div>')
                % row['user'] if row['user'] else '',
            ))
        return Markup('<ul class="o_skybox_timeline">%s</ul>') % Markup('').join(items)
