# -*- coding: utf-8 -*-
"""Mouvements d'entrepot Odoo -> Skybox.

Guide "Mouvements d'entrepot - Guide complet du partenaire WMS" :
chaque deplacement physique d'un colis est envoye a Skybox sur
POST {URL Skybox}/v1/integrations/wms/warehouse-movements/batch.

- File d'attente : un enregistrement par mouvement, envoye juste apres le
  commit puis par le cron (toutes les minutes) tant qu'il n'est pas livre.
- movementId = identifiant Odoo definitif (ODOO-MV-<id>) : un renvoi apres
  timeout / 5xx ne cree jamais de doublon chez Skybox.
- sequenceNumber croissant par colis.
- 'to' contient toujours la position complete du colis apres le mouvement.
"""
import json
import logging

import requests

from odoo import SUPERUSER_ID, api, fields, models

from .skybox_event import format_skybox_datetime

_logger = logging.getLogger(__name__)

BATCH_PATH = "/v1/integrations/wms/warehouse-movements/batch"
BATCH_MAX = 500

MOVEMENT_TYPES = [
    ('STORED', "Rangement en stock"),
    ('BAG_TRANSFER', "Changement de sac"),
    ('LOCATION_TRANSFER', "Changement d'emplacement"),
    ('PICKING', "Picking"),
    ('PACKING', "Emballage"),
    ('STAGING', "Attente au quai"),
    ('DISPATCH', "Sortie de l'entrepot"),
    ('RETURN_TO_STORAGE', "Retour en stock"),
    ('EXCEPTION', "Exception"),
]
POSITION_KEYS = ('bagCode', 'zoneCode', 'aisleCode', 'rackCode', 'locationCode')

# Zones fixes des etapes de preparation (Parametres > Skybox).
ZONE_PARAMS = {
    'PICKING': ('skybox.zone_picking', 'PICK'),
    'PACKING': ('skybox.zone_packing', 'PACK'),
    'STAGING': ('skybox.zone_staging', 'QUAI'),
    'DISPATCH': ('skybox.zone_dispatch', 'SORTIE'),
    'EXCEPTION': ('skybox.zone_exception', 'ISOLEMENT'),
}


def _clean_position(position):
    return {k: str(v) for k, v in (position or {}).items()
            if k in POSITION_KEYS and v}


class SkyboxMovement(models.Model):
    _name = 'skybox.movement'
    _description = "Mouvement d'entrepot (Odoo -> Skybox)"
    _order = 'id desc'
    _rec_name = 'movement_ref'

    movement_ref = fields.Char(string="movementId", readonly=True, index=True, copy=False)
    tracking_number = fields.Char(string="Colis", index=True)
    bag_code = fields.Char(string="Sac entier",
                           help="Deplacement d'un sac entier (sans colis).")
    movement_type = fields.Selection(MOVEMENT_TYPES, string="Type", required=True)
    warehouse_code = fields.Char(string="Code entrepot")
    from_json = fields.Text(string="Depart (from)")
    to_json = fields.Text(string="Arrivee (to)", required=True)
    from_display = fields.Char(string="Depart", compute='_compute_display')
    to_display = fields.Char(string="Arrivee", compute='_compute_display')
    occurred_at = fields.Datetime(string="Date du mouvement", required=True,
                                  default=fields.Datetime.now)
    sequence_number = fields.Integer(string="Sequence")
    user_id = fields.Many2one(comodel_name='res.users', string="Operateur",
                              default=lambda self: self.env.user)
    reason_code = fields.Char(string="Code raison")
    partner_reference = fields.Char(string="Reference Odoo")
    notes = fields.Char(string="Notes")
    picking_id = fields.Many2one(comodel_name='stock.picking', string="Reception",
                                 ondelete='set null')
    preparation_order_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Ordre de preparation",
        ondelete='set null')

    state = fields.Selection(
        selection=[
            ('pending', "A envoyer"),
            ('sent', "Envoye"),
            ('refused', "Refuse"),
        ],
        string="Statut", default='pending', required=True, index=True)
    duplicate = fields.Boolean(string="Deja recu par Skybox")
    applied_to_position = fields.Boolean(string="Position mise a jour")
    warnings = fields.Char(string="Avertissements")
    error_code = fields.Char(string="Code erreur")
    message = fields.Char(string="Reponse Skybox")
    attempts = fields.Integer(string="Tentatives")
    sent_at = fields.Datetime(string="Envoye le")

    @api.depends('from_json', 'to_json')
    def _compute_display(self):
        def show(raw):
            try:
                pos = json.loads(raw or '{}')
            except ValueError:
                return raw or ''
            return " / ".join(pos[k] for k in POSITION_KEYS if pos.get(k))
        for rec in self:
            rec.from_display = show(rec.from_json)
            rec.to_display = show(rec.to_json)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @api.model
    def _zone(self, movement_type):
        param, default = ZONE_PARAMS[movement_type]
        return self.env['ir.config_parameter'].sudo().get_param(param) or default

    @api.model
    def _next_sequence(self, tracking_number):
        last = self.sudo().search([('tracking_number', '=', tracking_number)],
                                  order='sequence_number desc', limit=1)
        return (last.sequence_number or 0) + 1

    @api.model
    def _record(self, movement_type, tracking_number, to, from_=None, **extra):
        """Cree un mouvement et programme son envoi. `to` = position complete
        apres le mouvement (dict bagCode/zoneCode/aisleCode/rackCode/
        locationCode). Ne fait rien si `to` est vide (Skybox le refuserait)."""
        to = _clean_position(to)
        tracking = (tracking_number or '').strip().upper()
        if not to or not tracking:
            return self.browse()
        vals = {
            'movement_type': movement_type,
            'tracking_number': tracking,
            'to_json': json.dumps(to, ensure_ascii=False),
            'from_json': json.dumps(_clean_position(from_), ensure_ascii=False)
            if _clean_position(from_) else False,
            'sequence_number': self._next_sequence(tracking),
            'user_id': self.env.user.id,
        }
        vals.update({k: v for k, v in extra.items() if v})
        movement = self.sudo().create(vals)
        self._schedule_send()
        return movement

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            # Identifiant Odoo definitif : cle d'idempotence chez Skybox.
            rec.movement_ref = "ODOO-MV-%06d" % rec.id
        return records

    # ------------------------------------------------------------------
    # Envoi
    # ------------------------------------------------------------------
    @api.model
    def _schedule_send(self):
        """Envoi juste apres le commit (sans bloquer l'utilisateur sur une
        erreur reseau) ; le cron rattrape ce qui n'est pas parti."""
        cr = self.env.cr
        if cr.postcommit.data.get('skybox_movement_send'):
            return
        cr.postcommit.data['skybox_movement_send'] = True
        registry = self.env.registry

        def send():
            try:
                with registry.cursor() as new_cr:
                    env = api.Environment(new_cr, SUPERUSER_ID, {})
                    env['skybox.movement']._send_pending()
            except Exception:  # noqa: BLE001
                _logger.exception("Skybox mouvements : envoi apres commit en echec")

        cr.postcommit.add(send)

    def _payload(self):
        self.ensure_one()
        data = {
            'movementId': self.movement_ref,
            'movementType': self.movement_type,
            'to': json.loads(self.to_json),
            'occurredAt': format_skybox_datetime(self.env, self.occurred_at),
        }
        if self.tracking_number:
            data['trackingNumber'] = self.tracking_number
            if self.sequence_number:
                data['sequenceNumber'] = self.sequence_number
        if self.bag_code:
            data['bagCode'] = self.bag_code
        if self.from_json:
            data['from'] = json.loads(self.from_json)
        if self.warehouse_code:
            data['warehouseCode'] = self.warehouse_code
        if self.user_id:
            data['operator'] = {'operatorId': str(self.user_id.login or self.user_id.id)[:100],
                                'displayName': (self.user_id.name or '')[:150]}
        if self.reason_code:
            data['reasonCode'] = self.reason_code[:60]
        if self.partner_reference:
            data['partnerReference'] = self.partner_reference[:120]
        if self.notes:
            data['notes'] = self.notes[:1000]
        return data

    @api.model
    def _send_pending(self):
        """Envoie les mouvements 'A envoyer' par lots de 500, dans l'ordre."""
        key, base = self.env['skybox.api.key']._skybox_wms_base()
        pending = self.search([('state', '=', 'pending')], order='id', limit=BATCH_MAX)
        if not pending:
            return
        now = fields.Datetime.now()
        if not key:
            pending.write({'message': "Aucune API externe Skybox configuree "
                                      "(Configuration > API management)."})
            return
        auth_headers, params = key._build_request()
        headers = {'Content-Type': 'application/json'}
        headers.update(auth_headers)
        for rec in pending:
            rec.attempts += 1
        try:
            response = requests.post(
                base + BATCH_PATH, json={'movements': [m._payload() for m in pending]},
                headers=headers, params=params, timeout=30)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Skybox mouvements : %s", exc)
            pending.write({'message': "Skybox injoignable, nouvel essai automatique: %s" % exc})
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = response.status_code
        if code != 200 or not isinstance(body, dict):
            err = (body.get('code') or body.get('error')) if isinstance(body, dict) else ''
            msg = (body.get('message') or '') if isinstance(body, dict) else ''
            # 401 / 5xx / reseau : passager, on garde 'A envoyer'.
            pending.write({'message': "HTTP %s %s %s, nouvel essai automatique."
                                      % (code, err or '', msg or response.reason)})
            return
        by_ref = {m.movement_ref: m for m in pending}
        for result in body.get('results') or []:
            rec = by_ref.get(result.get('movementId'))
            if not rec:
                continue
            rec._apply_result(result, now)

    def _apply_result(self, result, now):
        self.ensure_one()
        if result.get('accepted'):
            warnings = result.get('warnings') or []
            self.write({
                'state': 'sent',
                'sent_at': now,
                'duplicate': bool(result.get('duplicate')),
                'applied_to_position': bool(result.get('appliedToPosition'))
                if not result.get('duplicate') else self.applied_to_position,
                'warnings': ", ".join(warnings) or False,
                'error_code': False,
                'message': "Deja recu par Skybox" if result.get('duplicate')
                else ("Enregistre, position %s" % (
                    "mise a jour" if result.get('appliedToPosition')
                    else "inchangee (mouvement plus ancien)")),
            })
            return
        code = result.get('code') or ''
        message = result.get('message') or ''
        if code == 'INTERNAL_ERROR':
            self.write({'error_code': code,
                        'message': "Erreur Skybox, nouvel essai automatique: %s" % message})
            return
        self.write({'state': 'refused', 'error_code': code, 'message': message})

    def action_resend(self):
        """Renvoie un mouvement refuse (meme movementId) apres correction."""
        self.filtered(lambda m: m.state == 'refused').write(
            {'state': 'pending', 'error_code': False})
        self._send_pending()
        return True

    @api.model
    def _cron_send(self):
        self._send_pending()
