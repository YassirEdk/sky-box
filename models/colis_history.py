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
    parcel_id = fields.Many2one(
        comodel_name='skybox.parcel', string="Colis traite", ondelete='cascade')
    partner_id = fields.Many2one(related='order_id.partner_id', string="Client")
    parcel_partner_id = fields.Many2one(
        related='parcel_id.partner_id', string="Client du colis")
    carrier = fields.Char(related='order_id.carrier', string="Transporteur")
    carrier_tracking_number = fields.Char(
        related='order_id.carrier_tracking_number', string="Suivi transporteur")
    step_count = fields.Integer(string="Etapes", compute='_compute_history')
    history_html = fields.Html(
        string="Historique", compute='_compute_history', sanitize=False)

    @api.depends('order_id', 'parcel_id', 'tracking_number')
    def _compute_history(self):
        for wiz in self:
            order = wiz.order_id
            kids = order.child_ids.sorted('id') if order.is_group else order.browse()
            if kids and not wiz.tracking_number:
                # Commande groupee : un bloc pour la commande, puis un bloc
                # par ordre du regroupement (son propre historique).
                own = [r for r in order.with_context(skybox_history_own=True)._history_rows()
                       if r['kind'] in ('order', 'event', 'done')]
                sections = [("Commande groupee %s" % order.name, own)]
                for kid in kids:
                    tns = ", ".join(sorted(t for t in kid._tracking_numbers() if t))
                    sections.append((" · ".join(filter(None, [kid.name, tns])),
                                     kid._history_rows()))
                wiz.step_count = sum(len(rows) for _title, rows in sections)
                wiz.history_html = Markup('').join(
                    Markup('<h5 class="o_skybox_tl_section mt-4 mb-2">'
                           '<i class="fa fa-cube me-2" title="Ordre"></i>%s</h5>%s')
                    % (title, wiz._render(rows)) for title, rows in sections)
                continue
            if order:
                rows = order._history_rows(wiz.tracking_number)
            else:
                # Colis traite pas encore dans un ordre de preparation.
                rows = wiz.parcel_id._history_rows() if wiz.parcel_id else []
            wiz.step_count = len(rows)
            wiz.history_html = wiz._render(rows)

    def _render(self, rows):
        if not rows:
            return Markup('<p class="text-muted">Aucun historique pour ce colis.</p>')
        items = []
        for row in rows:
            date = fields.Datetime.context_timestamp(self, row['date'])
            step = row['step']
            if not self.tracking_number and row['tracking_number']:
                # Historique de tout l'ordre : on precise le colis.
                step = "%s - %s" % (step, row['tracking_number'])
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
                escape(step),
                date.strftime('%d/%m/%Y %H:%M:%S'),
                Markup('<div class="o_skybox_tl_detail">%s</div>') % row['detail']
                if row['detail'] else '',
                Markup('<div class="o_skybox_tl_user"><i class="fa fa-user me-1" title="Par"></i>%s</div>')
                % row['user'] if row['user'] else '',
            ))
        return Markup('<ul class="o_skybox_timeline">%s</ul>') % Markup('').join(items)
