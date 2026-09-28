# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SkyboxTracking(models.Model):
    _name = 'skybox.tracking'
    _description = "Tracking"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string="Reference", required=True, default="New",
        copy=False, readonly=True, index=True,
    )

    _sql_constraints = [
        ('name_uniq', 'unique(name)', "La reference du tracking doit etre unique."),
    ]
    tracking_number = fields.Char(
        string="Numero de tracking", tracking=True,
    )
    suite_number = fields.Char(
        string="Suite du client", tracking=True,
    )
    courier_id = fields.Many2one(
        comodel_name='skybox.courier', string="Transporteur", tracking=True,
    )
    state = fields.Selection(
        selection=[
            ('to_send', "A envoyer"),
            ('sent', "Envoye"),
            ('error', "Erreur"),
        ],
        string="Statut", default='to_send', required=True, tracking=True,
        copy=False,
    )
    sent_date = fields.Datetime(string="Date d'envoi", readonly=True, copy=False)
    notes = fields.Text(string="Notes")

    # --- Client (repris de la prealerte liee) ---
    prealerte_ids = fields.One2many(
        comodel_name='skybox.prealerte', inverse_name='tracking_id',
        string="Prealertes",
    )
    client_id = fields.Many2one(
        comodel_name='res.partner', string="Client",
        compute='_compute_client', store=True,
    )
    client_ref = fields.Char(
        string="Reference client", compute='_compute_client', store=True,
    )

    @api.depends('prealerte_ids', 'prealerte_ids.partner_id',
                 'prealerte_ids.partner_id.ref')
    def _compute_client(self):
        for rec in self:
            prealerte = rec.prealerte_ids[:1]
            partner = prealerte.partner_id if prealerte else False
            rec.client_id = partner.id if partner else False
            rec.client_ref = partner.ref if partner else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == "New":
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'skybox.tracking') or "New"
        return super().create(vals_list)

    def _check_and_create(self):
        """Interroge l'API management pour les trackings de self et cree les
        prealertes trouvees. Retourne la liste des details par tracking.

        - PREALERT_FOUND -> prealerte creee + tracking Envoye.
        - PREALERT_NOT_FOUND -> laisse a reessayer.
        - Echec du check (401/500/reseau) -> tracking en Erreur.
        """
        numbers = list(dict.fromkeys(
            n for n in self.mapped('tracking_number') if n
        ))
        if not numbers:
            return []

        keys = self.env['skybox.api.key'].search([
            ('active', '=', True), ('url', '!=', False),
            ('api_type', '=', 'api_externe'),
        ])
        if not keys:
            _logger.warning(
                "Skybox: aucune API externe active configuree.")
            self.write({'state': 'error'})
            return []

        tracking_by_number = {}
        for track in self:
            tracking_by_number.setdefault(
                (track.tracking_number or '').lower(), track)

        prealerte_model = self.env['skybox.prealerte']
        now = fields.Datetime.now()
        check_ok = False
        details = []
        for key in keys:
            result = key._check_receptions(numbers)
            if not result:
                continue
            check_ok = True
            for item in (result.get('results') or []):
                if not isinstance(item, dict):
                    continue
                prealerte = prealerte_model._create_from_check_result(item)
                status = item.get('status')
                # Trouvee ou deja traitee -> tracking termine (Envoye).
                if prealerte or status == 'ALREADY_PROCESSED':
                    track = tracking_by_number.get(
                        (item.get('trackingNumber') or '').lower())
                    if track:
                        track.write({'state': 'sent', 'sent_date': now})
                pre_alert = item.get('preAlert') or {}
                details.append({
                    'tracking_number': item.get('trackingNumber'),
                    'status': item.get('status'),
                    'message': item.get('message'),
                    'skybill': item.get('skybillNumber'),
                    'courier': pre_alert.get('courier'),
                    'declared_value': pre_alert.get('declaredValue') or 0.0,
                    'currency': pre_alert.get('currency'),
                    'prealerte_id': prealerte.id if prealerte else False,
                })

        if not check_ok:
            _logger.warning("Skybox: check echoue, trackings en erreur.")
            self.write({'state': 'error'})
        return details

    @api.model
    def _cron_check_receptions(self):
        """Cron horaire : check des trackings a envoyer ou en erreur."""
        trackings = self.search([
            ('tracking_number', '!=', False),
            ('state', 'in', ('to_send', 'error')),
        ])
        if not trackings:
            _logger.info("Skybox: aucun tracking a verifier.")
            return
        trackings._check_and_create()
