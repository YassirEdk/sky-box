# -*- coding: utf-8 -*-
"""Callback Skybox -> Odoo : reception, validation, idempotence, traitement
et acquittement (ACK) des evenements envoyes par Skybox sur /skybox/events.

Contrat : "Integration Skybox <-> Odoo - Guide technique complet" (contrat v2).

Regles d'ACK (section 4 du guide) :
  - traite                         -> 200 ACCEPTED
  - eventId deja traite            -> 200 DUPLICATE
  - revision <= revision stockee   -> 200 ACCEPTED (mise a jour forcee)
  - refus metier definitif         -> 200 REJECTED + reasonCode (jamais renvoye)
  - annulation a decider           -> 200 PENDING (reponse plus tard sur /cancellation)
  - enveloppe invalide             -> 400 REJECTED + reasonCode (jamais renvoye)
  - secret absent / faux           -> 401 (renvoye par Skybox)
  - erreur passagere Odoo          -> 500, jamais REJECTED (renvoye par Skybox)
"""
import json
import logging
import re
from datetime import datetime, timedelta

import pytz

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

SUPPORTED_CONTRACT_VERSIONS = (2,)

EVENT_HANDLERS = {
    'PARCEL_PROCESSED': '_handle_parcel',
    'PARCEL_UPDATED': '_handle_parcel',
    'BAG_COMPLETED': '_handle_bag_completed',
    'PREALERT_COMPLETED': '_handle_prealert_completed',
    'PREPARATION_ORDER': '_handle_preparation_order',
    'PREPARATION_ORDER_CANCELLED': '_handle_preparation_order_cancelled',
}
ORDER_EVENTS = ('PREPARATION_ORDER', 'PREPARATION_ORDER_CANCELLED')

# Codes de refus renvoyes par Odoo (reasonCode), a communiquer a Skybox.
REASON_CODES = {
    'INVALID_JSON': "Corps JSON illisible",
    'MISSING_FIELD': "Champ obligatoire absent",
    'UNSUPPORTED_CONTRACT_VERSION': "contractVersion inconnue",
    'UNKNOWN_EVENT_TYPE': "eventType inconnu",
    'HEADER_MISMATCH': "Idempotency-Key / X-Skybox-Event different du corps",
    'PARTNER_CODE_MISMATCH': "partnerCode inattendu",
    'UNSUPPORTED_MEDIA_TYPE': "Content-Type different de application/json",
    'UNKNOWN_WAREHOUSE': "Entrepot inconnu d'Odoo",
    'UNKNOWN_PARCEL': "Colis inconnu d'Odoo",
    'ALREADY_DISPATCHED': "Ordre deja expedie, annulation impossible",
    'PARCEL_ALREADY_IN_ORDER': "Colis deja dans un ordre de preparation en cours",
    'INTERNAL_ERROR': "Erreur interne Odoo (renvoyer plus tard)",
}

MAX_REFERENCE = 150
MAX_MESSAGE = 500

# Decalage horaire en fin de date ISO : +08:00, -0500, +01.
_TZ_OFFSET = re.compile(r'([+-])(\d{2}):?(\d{2})?$')


class SkyboxEventRejected(Exception):
    """Refus metier definitif (ACK REJECTED + reasonCode)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_skybox_datetime(value):
    """Date ISO-8601 -> datetime naive UTC (ou False).

    '2026-10-08T10:28:28.259973500Z' et '2026-10-16T17:00:00+08:00' sont
    convertis en UTC (le decalage est respecte). Sans fuseau, la date est lue
    en UTC (contrat v2)."""
    if not value or not isinstance(value, str):
        return False
    value = value.strip()
    offset = timedelta(0)
    if value[-1:] in ('Z', 'z'):
        value = value[:-1]
    else:
        match = _TZ_OFFSET.search(value)
        if match and 'T' in value[:match.start()]:
            sign = -1 if match.group(1) == '-' else 1
            offset = sign * timedelta(hours=int(match.group(2)),
                                      minutes=int(match.group(3) or 0))
            value = value[:match.start()]
    if '.' in value:
        main, frac = value.split('.', 1)
        value = "%s.%s" % (main, frac[:6])
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt) - offset
        except ValueError:
            continue
    return False


TIMEZONE_PARAM = 'skybox.timezone'
DEFAULT_TIMEZONE = 'Africa/Casablanca'


def format_skybox_datetime(env, value=None):
    """datetime naive UTC (defaut : maintenant) -> date ISO-8601 envoyee a
    Skybox, a l'heure du fuseau choisi dans Parametres > Skybox, avec son
    decalage (ex. 2026-10-20T11:00:00+01:00). Meme instant que la date UTC."""
    value = value or fields.Datetime.now()
    name = env['ir.config_parameter'].sudo().get_param(TIMEZONE_PARAM) or DEFAULT_TIMEZONE
    try:
        tz = pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        tz = pytz.utc
    local = pytz.utc.localize(value.replace(microsecond=0)).astimezone(tz)
    return local.isoformat()


def to_float(value):
    try:
        return float(value) if value not in (None, '') else 0.0
    except (TypeError, ValueError):
        return 0.0


def to_int(value):
    try:
        return int(value) if value not in (None, '') else 0
    except (TypeError, ValueError):
        return 0


class SkyboxEvent(models.Model):
    _name = 'skybox.event'
    _description = "Evenement Skybox"
    _order = 'id desc'
    _rec_name = 'event_id'

    event_id = fields.Char(string="Event ID", index=True, readonly=True)
    event_type = fields.Char(string="Type d'evenement", index=True, readonly=True)
    contract_version = fields.Integer(string="Version du contrat", readonly=True)
    partner_code = fields.Char(string="Partner code", readonly=True)
    occurred_at = fields.Datetime(
        string="Produit le", readonly=True,
        help="occurredAt : moment du fait metier chez Skybox (identique a "
             "chaque renvoi).")
    enqueued_at = fields.Datetime(
        string="Mis en file le", readonly=True,
        help="enqueuedAt : moment ou Skybox a mis le message en file d'envoi.")
    sent_at = fields.Datetime(string="Envoye le", readonly=True)
    attempt = fields.Integer(string="Tentative", readonly=True)
    tracking_number = fields.Char(string="Numero de tracking", index=True, readonly=True)
    warehouse_code = fields.Char(string="Code entrepot", readonly=True)
    order_ref = fields.Char(string="Ordre de preparation", readonly=True)
    bag_code = fields.Char(string="Bag", readonly=True)
    revision = fields.Integer(string="Revision", readonly=True)

    received_at = fields.Datetime(
        string="Recu le", default=fields.Datetime.now, readonly=True)
    processed_at = fields.Datetime(string="Traite le", readonly=True)
    state = fields.Selection(
        selection=[
            ('received', "Recu"),
            ('accepted', "Accepte"),
            ('rejected', "Rejete"),
            ('error', "Erreur"),
        ],
        string="Resultat", default='received', required=True, readonly=True,
        index=True,
    )
    ack_status = fields.Char(
        string="ACK renvoye", readonly=True,
        help="ACCEPTED, DUPLICATE, PARTIAL, PENDING, REJECTED ou ERROR (HTTP 500).")
    reason_code = fields.Char(string="reasonCode", readonly=True)
    ack_parcels = fields.Text(
        string="Detail par colis (PARTIAL)", readonly=True)
    duplicate_count = fields.Integer(string="Doublons recus", readonly=True)
    last_duplicate_at = fields.Datetime(string="Dernier doublon", readonly=True)
    message = fields.Text(string="Message", readonly=True)
    partner_reference = fields.Char(string="Partner reference", readonly=True)
    payload = fields.Text(string="JSON recu", readonly=True)
    api_key_id = fields.Many2one(
        comodel_name='skybox.api.key', string="Cle API", readonly=True,
        ondelete='set null',
    )
    parcel_id = fields.Many2one(
        comodel_name='skybox.parcel', string="Colis", readonly=True,
        ondelete='set null',
    )
    preparation_order_id = fields.Many2one(
        comodel_name='skybox.preparation.order', string="Ordre de preparation",
        readonly=True, ondelete='set null',
    )
    bag_id = fields.Many2one(
        comodel_name='stock.quant.package', string="Bag (Odoo)", readonly=True,
        ondelete='set null',
    )
    prealerte_id = fields.Many2one(
        comodel_name='skybox.prealerte', string="Prealerte", readonly=True,
        ondelete='set null',
    )

    _sql_constraints = [
        ('event_id_uniq', 'unique(event_id)',
         "Un evenement Skybox avec ce eventId existe deja."),
    ]

    # ------------------------------------------------------------------
    # Reception
    # ------------------------------------------------------------------
    @api.model
    def _envelope_vals(self, envelope):
        data = envelope.get('data') if isinstance(envelope.get('data'), dict) else {}
        parcel = data.get('parcel') if isinstance(data.get('parcel'), dict) else {}
        bag = data.get('bag') if isinstance(data.get('bag'), dict) else {}
        return {
            'event_id': str(envelope.get('eventId') or '').strip() or False,
            'event_type': str(envelope.get('eventType') or '').strip() or False,
            'contract_version': to_int(envelope.get('contractVersion')),
            'partner_code': envelope.get('partnerCode') or False,
            'occurred_at': parse_skybox_datetime(envelope.get('occurredAt')),
            'enqueued_at': parse_skybox_datetime(envelope.get('enqueuedAt')),
            'sent_at': parse_skybox_datetime(envelope.get('sentAt')),
            'attempt': to_int(envelope.get('attempt')),
            'tracking_number': (parcel.get('trackingNumber')
                                or data.get('trackingNumber') or False),
            'revision': to_int(parcel.get('revision')),
            'warehouse_code': data.get('warehouseCode') or False,
            'order_ref': data.get('orderId') or False,
            'bag_code': bag.get('sourceBagCode') or False,
            'payload': json.dumps(envelope, indent=2, ensure_ascii=False),
        }

    @api.model
    def _guess_reference(self, vals):
        if vals.get('tracking_number'):
            return "skybox.parcel/%s" % vals['tracking_number']
        if vals.get('order_ref'):
            return vals['order_ref']
        if vals.get('bag_code'):
            return vals['bag_code']
        return vals.get('event_id') or None

    @api.model
    def _validate_envelope(self, envelope, vals, headers, api_key):
        """Retourne (reasonCode, message), ou None si l'enveloppe est valide."""
        if not vals['event_id']:
            return 'MISSING_FIELD', "eventId manquant."
        if not vals['event_type']:
            return 'MISSING_FIELD', "eventType manquant."
        if vals['event_type'] not in EVENT_HANDLERS:
            return 'UNKNOWN_EVENT_TYPE', "eventType non supporte: %s" % vals['event_type']
        if vals['contract_version'] not in SUPPORTED_CONTRACT_VERSIONS:
            return 'UNSUPPORTED_CONTRACT_VERSION', "contractVersion non supportee: %s" % (
                envelope.get('contractVersion'),)
        if 'data' in envelope and not isinstance(envelope['data'], dict):
            return 'INVALID_JSON', "data doit etre un objet JSON."
        idem = (headers.get('Idempotency-Key') or '').strip()
        if idem and idem != vals['event_id']:
            return 'HEADER_MISMATCH', "Idempotency-Key (%s) different de eventId (%s)." % (
                idem, vals['event_id'])
        header_type = (headers.get('X-Skybox-Event') or '').strip()
        if header_type and header_type != vals['event_type']:
            return 'HEADER_MISMATCH', "X-Skybox-Event (%s) different de eventType (%s)." % (
                header_type, vals['event_type'])
        if (api_key and api_key.partner_code
                and vals['partner_code'] != api_key.partner_code):
            return 'PARTNER_CODE_MISMATCH', "partnerCode inattendu: %s" % vals['partner_code']
        return None

    @api.model
    def _receive(self, envelope, headers, api_key=None):
        """Point d'entree du callback. Retourne (code HTTP, ACK JSON)."""
        if not isinstance(envelope, dict):
            return 400, {'status': 'REJECTED', 'reasonCode': 'INVALID_JSON',
                         'message': "Le corps doit etre un objet JSON."}

        vals = self._envelope_vals(envelope)
        vals['api_key_id'] = api_key.id if api_key else False
        reference = self._guess_reference(vals)
        now = fields.Datetime.now()

        # Idempotence (message) : un eventId deja traite n'est jamais refait.
        existing = self.search([('event_id', '=', vals['event_id'])], limit=1) \
            if vals['event_id'] else self.browse()
        if existing and existing.state == 'accepted':
            existing.write({
                'duplicate_count': existing.duplicate_count + 1,
                'last_duplicate_at': now,
            })
            _logger.info("Skybox event %s (%s) : doublon (attempt %s)",
                         vals['event_id'], vals['event_type'], vals['attempt'])
            if existing.event_type in ORDER_EVENTS:
                # Ordres : un renvoi du meme message garde la reponse du
                # premier envoi (ACCEPTED, DUPLICATE, PENDING...).
                return 200, existing._ack()
            return 200, existing._ack(status='DUPLICATE')

        # Renvoi d'un evenement en erreur (ou rejete) : on le retraite.
        if existing:
            vals.update(state='received', message=False, received_at=now,
                        ack_status=False, reason_code=False, ack_parcels=False)
            existing.write(vals)
            event = existing
        else:
            event = self.create(vals)

        error = self._validate_envelope(envelope, vals, headers, api_key)
        if error:
            event._set_result('rejected', reference, error[1],
                              ack_status='REJECTED', reason_code=error[0])
            return 400, event._ack()

        return event._process(envelope.get('data') or {})

    # ------------------------------------------------------------------
    # Traitement
    # ------------------------------------------------------------------
    def _ack(self, status=None):
        self.ensure_one()
        status = status or self.ack_status or 'REJECTED'
        reference = (self.partner_reference or '')[:MAX_REFERENCE] or None
        ack = {'status': status, 'partnerReference': reference}
        if self.event_type in ORDER_EVENTS:
            ack['partnerOrderRef'] = reference
        if self.event_type == 'PREPARATION_ORDER_CANCELLED' and status in (
                'ACCEPTED', 'REJECTED'):
            ack['cancelled'] = status == 'ACCEPTED'
        if self.reason_code and status in ('REJECTED', 'ERROR'):
            ack['reasonCode'] = self.reason_code
        if self.message and status != 'DUPLICATE':
            ack['message'] = self.message[:MAX_MESSAGE]
        if self.ack_parcels and status == 'PARTIAL':
            try:
                ack['parcels'] = json.loads(self.ack_parcels)
            except ValueError:
                pass
        return ack

    def _set_result(self, state, reference, message=False, ack_status=False,
                    reason_code=False, parcels=None):
        self.ensure_one()
        self.write({
            'state': state,
            'ack_status': ack_status,
            'reason_code': reason_code,
            'partner_reference': reference,
            'message': message,
            'ack_parcels': json.dumps(parcels, ensure_ascii=False) if parcels else False,
            'processed_at': fields.Datetime.now(),
        })
        _logger.info(
            "Skybox event %s (%s) attempt=%s tracking=%s -> %s %s %s",
            self.event_id, self.event_type, self.attempt,
            self.tracking_number or '-', ack_status, reason_code or '',
            message or '')

    def _process(self, data):
        self.ensure_one()
        reference = self._guess_reference({
            'tracking_number': self.tracking_number,
            'order_ref': self.order_ref,
            'bag_code': self.bag_code,
            'event_id': self.event_id,
        })
        handler = getattr(self, EVENT_HANDLERS[self.event_type])
        try:
            with self.env.cr.savepoint():
                result = handler(data)
        except SkyboxEventRejected as exc:
            self.invalidate_recordset()
            self._set_result('rejected', reference, exc.message,
                             ack_status='REJECTED', reason_code=exc.code)
            return 200, self._ack()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Skybox event %s : erreur de traitement",
                              self.event_id)
            self.invalidate_recordset()
            self._set_result('error', reference,
                             "Erreur interne Odoo: %s" % exc,
                             ack_status='ERROR', reason_code='INTERNAL_ERROR')
            # 5xx (jamais REJECTED) : Skybox renverra le message.
            return 500, self._ack()
        self._set_result(
            'accepted', result.get('reference') or reference,
            result.get('note'), ack_status=result.get('status', 'ACCEPTED'),
            parcels=result.get('parcels'))
        return 200, self._ack()

    def action_reprocess(self):
        """Retraite manuellement un evenement rejete / en erreur, ou ignore
        (DUPLICATE : revision deja recue)."""
        for event in self.filtered(lambda e: e.state in ('rejected', 'error', 'received')
                                   or e.ack_status == 'DUPLICATE'):
            try:
                envelope = json.loads(event.payload or '{}')
            except ValueError:
                continue
            if not isinstance(envelope, dict):
                continue
            vals = event._envelope_vals(envelope)
            error = event._validate_envelope(envelope, vals, {}, event.api_key_id)
            if error:
                event._set_result('rejected', event.partner_reference, error[1],
                                  ack_status='REJECTED', reason_code=error[0])
                continue
            event._process(envelope.get('data') or {})
        return True

    # ------------------------------------------------------------------
    # Controles communs
    # ------------------------------------------------------------------
    def _check_warehouse(self, code):
        """Entrepot inconnu -> REJECTED UNKNOWN_WAREHOUSE.
        Controle actif seulement si au moins un entrepot a un Code Skybox."""
        Warehouse = self.env['stock.warehouse']
        if not Warehouse.search_count([('skybox_code', '!=', False)]):
            return
        if not code or not Warehouse.search_count([('skybox_code', '=ilike', code)]):
            raise SkyboxEventRejected(
                'UNKNOWN_WAREHOUSE', "Entrepot inconnu d'Odoo: %s" % (code or '-'))

    def _known_parcel(self, tracking):
        """Colis connu d'Odoo : colis Skybox deja recu ou reception Odoo."""
        if not tracking:
            return False
        return bool(
            self.env['skybox.parcel'].search_count(
                [('tracking_number', '=ilike', tracking)])
            or self.env['stock.picking'].search_count([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', tracking),
                ('state', '!=', 'cancel'),
            ]))

    # ------------------------------------------------------------------
    # Handlers metier : retournent {'reference', 'note', 'status', 'parcels'}
    # ------------------------------------------------------------------
    def _apply_parcel_record(self, warehouse_code, parcel_data, bag_progress=None):
        """Applique l'etat complet d'un colis (regle de revision).
        Retourne (parcel, applique: bool, note)."""
        tracking = (parcel_data.get('trackingNumber') or '').strip()
        revision = to_int(parcel_data.get('revision'))
        Parcel = self.env['skybox.parcel']
        parcel = Parcel.search([('tracking_number', '=ilike', tracking)], limit=1)
        # Toujours appliquer, meme une revision deja recue ou plus ancienne :
        # le dernier evenement recu fait foi (mise a jour forcee).
        previous = parcel.revision if parcel else None
        data = {'warehouseCode': warehouse_code}
        if bag_progress:
            data['bagProgress'] = bag_progress
        parcel = Parcel._apply_event(parcel, data, parcel_data, self)
        if previous is not None and revision <= previous:
            return parcel, True, (
                "Revision %s appliquee (revision enregistree %s : mise a jour forcee)."
                % (revision, previous))
        return parcel, True, "Revision %s appliquee." % revision

    def _handle_parcel(self, data):
        """PARCEL_PROCESSED / PARCEL_UPDATED : etat complet du colis."""
        parcel_data = data.get('parcel')
        if not isinstance(parcel_data, dict):
            raise SkyboxEventRejected('MISSING_FIELD', "data.parcel manquant.")
        tracking = (parcel_data.get('trackingNumber') or '').strip()
        if not tracking:
            raise SkyboxEventRejected(
                'MISSING_FIELD', "data.parcel.trackingNumber manquant.")
        self._check_warehouse(data.get('warehouseCode'))

        parcel, applied, note = self._apply_parcel_record(
            data.get('warehouseCode'), parcel_data, data.get('bagProgress'))
        self.write({
            'parcel_id': parcel.id,
            'prealerte_id': parcel.prealerte_id.id,
            'bag_id': parcel.destination_bag_id.id,
        })
        return {
            'reference': "skybox.parcel/%s" % tracking,
            'note': note,
            # Revision ancienne : DUPLICATE, jamais REJECTED.
            'status': 'ACCEPTED' if applied else 'DUPLICATE',
        }

    def _handle_bag_completed(self, data):
        bag_data = data.get('bag')
        if not isinstance(bag_data, dict) or not bag_data.get('sourceBagCode'):
            raise SkyboxEventRejected(
                'MISSING_FIELD', "data.bag.sourceBagCode manquant.")
        self._check_warehouse(data.get('warehouseCode'))
        code = bag_data['sourceBagCode']
        bag = self.env['stock.quant.package']._skybox_find_or_create(code)
        counts = data.get('counts') or {}
        detail = data.get('detailLevel') or 'SUMMARY'
        bag.write({
            'skybox_bag_status': bag_data.get('status') or False,
            'skybox_session_id': bag_data.get('sessionId') or False,
            'skybox_started_at': parse_skybox_datetime(bag_data.get('startedAt')),
            'skybox_started_by': bag_data.get('startedBy') or False,
            'skybox_completed_at': parse_skybox_datetime(bag_data.get('completedAt')),
            'skybox_completed_by': bag_data.get('completedBy') or False,
            'skybox_detail_level': detail,
            'skybox_warehouse_code': data.get('warehouseCode') or False,
            'skybox_expected': to_int(counts.get('expected')),
            'skybox_processed': to_int(counts.get('processed')),
            'skybox_set_aside': to_int(counts.get('setAside')),
            'skybox_missing': to_int(counts.get('missing')),
            'skybox_unexpected_kept': to_int(counts.get('unexpectedKept')),
            'skybox_unexpected_set_aside': to_int(counts.get('unexpectedSetAside')),
            'skybox_pending_identification': to_int(
                counts.get('pendingIdentification')),
            'skybox_remaining': 0,  # sac termine
            'skybox_remaining_tracking_numbers': False,
            'skybox_parcels_json': json.dumps(
                data.get('parcels') or [], indent=2, ensure_ascii=False),
            'skybox_last_event_id': self.event_id,
        })
        self.bag_id = bag

        # Colis du sac : FULL = etat complet a appliquer, SUMMARY = resume.
        lines, rejected = [], 0
        for item in data.get('parcels') or []:
            if not isinstance(item, dict):
                continue
            tracking = (item.get('trackingNumber') or '').strip()
            if not tracking:
                rejected += 1
                lines.append({'trackingNumber': '', 'status': 'REJECTED',
                              'reference': '',
                              'message': "trackingNumber manquant"})
                continue
            if not self._known_parcel(tracking):
                rejected += 1
                lines.append({'trackingNumber': tracking, 'status': 'REJECTED',
                              'reference': '', 'message': "Colis inconnu d'Odoo"})
                continue
            if detail == 'FULL':
                try:
                    with self.env.cr.savepoint():
                        self._apply_parcel_record(data.get('warehouseCode'), item)
                except Exception as exc:  # noqa: BLE001
                    _logger.exception("Skybox BAG_COMPLETED %s : colis %s",
                                      code, tracking)
                    rejected += 1
                    lines.append({'trackingNumber': tracking, 'status': 'REJECTED',
                                  'reference': '', 'message': str(exc)[:200]})
                    continue
            lines.append({'trackingNumber': tracking, 'status': 'ACCEPTED',
                          'reference': "skybox.parcel/%s" % tracking})

        if rejected:
            return {
                'reference': code, 'status': 'PARTIAL', 'parcels': lines,
                'note': "%s colis refuse(s) sur %s." % (rejected, len(lines)),
            }
        return {'reference': code, 'status': 'ACCEPTED',
                'note': "Bag %s : %s (%s colis)." % (
                    code, bag_data.get('status') or 'EMPTIED', len(lines))}

    def _handle_prealert_completed(self, data):
        tracking = (data.get('trackingNumber') or '').strip()
        if not tracking:
            raise SkyboxEventRejected('MISSING_FIELD', "data.trackingNumber manquant.")
        self._check_warehouse(data.get('warehouseCode'))
        if not self._known_parcel(tracking):
            raise SkyboxEventRejected(
                'UNKNOWN_PARCEL', "Colis inconnu d'Odoo: %s" % tracking)

        pre_alert = data.get('preAlert') or {}
        confirmed = bool(data.get('ownershipConfirmed'))
        Prealerte = self.env['skybox.prealerte']
        prealerte = Prealerte._create_from_check_result({
            'trackingNumber': tracking,
            'preAlert': pre_alert,
            'suite': {'suiteNumber': data.get('declaredSuiteNumber')},
            'skybillNumber': data.get('skybillNumber'),
            'status': data.get('shipmentStatus'),
            # Donnees personnelles seulement si la propriete est confirmee.
            'customer': (data.get('customer') or {}) if confirmed else {},
        })
        if prealerte:
            vals = {}
            if data.get('skybillNumber') and not prealerte.skybill:
                vals['skybill'] = data['skybillNumber']
            if data.get('declaredSuiteNumber') and not prealerte.suite_number:
                vals['suite_number'] = data['declaredSuiteNumber']
            if vals:
                prealerte.write(vals)
            self.prealerte_id = prealerte

            # Complete la fiche de reception Odoo du meme tracking.
            picking = self.env['stock.picking'].search([
                ('picking_type_code', '=', 'incoming'),
                ('tracking_number', '=ilike', tracking),
                ('state', '!=', 'cancel'),
            ], limit=1)
            if picking:
                picking._apply_prealerte(prealerte)
                if picking.prealerte_status != 'matched':
                    picking.prealerte_status = 'matched'

        parcel = self.env['skybox.parcel'].search(
            [('tracking_number', '=ilike', tracking)], limit=1)
        if parcel:
            parcel._apply_prealert(pre_alert, prealerte)
            if data.get('skybillNumber') and not parcel.skybill_number:
                parcel.skybill_number = data['skybillNumber']
            self.parcel_id = parcel

        return {
            'reference': "skybox.prealerte/%s" % tracking,
            'note': "Prealerte %s." % (prealerte.name if prealerte else "non creee"),
        }

    def _handle_preparation_order(self, data):
        order_id = (data.get('orderId') or '').strip()
        if not order_id:
            raise SkyboxEventRejected('MISSING_FIELD', "data.orderId manquant.")
        Order = self.env['skybox.preparation.order']
        # orderId complet (suffixe compris) : un identifiant deja connu au
        # premier envoi -> DUPLICATE ; Skybox relance alors l'ordre sous un
        # nouveau numero (contrat du 16/10/2026, section 2.2).
        order = Order.search([('name', '=', order_id)], limit=1)
        if order:
            self.preparation_order_id = order
            return {'reference': order.reference, 'status': 'DUPLICATE',
                    'note': "Ordre %s deja recu." % order_id}

        self._check_warehouse(data.get('warehouseCode'))
        parcels = [p for p in (data.get('parcels') or []) if isinstance(p, dict)]
        if not parcels:
            raise SkyboxEventRejected('MISSING_FIELD', "data.parcels vide.")
        unknown = [p.get('trackingNumber') or '-' for p in parcels
                   if not self._known_parcel(p.get('trackingNumber'))]
        if unknown:
            raise SkyboxEventRejected(
                'UNKNOWN_PARCEL', "Colis inconnu(s) d'Odoo: %s" % ", ".join(unknown))
        # Un colis ne peut etre que dans un seul ordre en cours.
        busy = []
        Line = self.env['skybox.preparation.order.line']
        for p in parcels:
            tracking = (p.get('trackingNumber') or '').strip()
            line = Line.search([
                ('tracking_number', '=ilike', tracking),
                ('order_id.is_group', '=', False),
                ('order_id.state', 'in', ('new', 'picking', 'picked', 'packed', 'ready')),
            ], limit=1) if tracking else Line
            if line:
                busy.append("%s (%s)" % (tracking, line.order_id.name))
        if busy:
            raise SkyboxEventRejected(
                'PARCEL_ALREADY_IN_ORDER',
                "Colis deja dans un ordre de preparation en cours: %s" % ", ".join(busy))

        order = Order._apply_event(data, self)
        self.preparation_order_id = order
        return {'reference': order.reference,
                'note': "Ordre %s (%s colis)." % (order_id, len(order.line_ids))}

    def _handle_preparation_order_cancelled(self, data):
        """Demande d'annulation (ou d'arret) d'un ordre : ACCEPTED (annule
        tout de suite), PENDING (decision de l'entrepot, envoyee plus tard sur
        /cancellation) ou REJECTED (annulation impossible)."""
        order_id = (data.get('orderId') or '').strip()
        if not order_id:
            raise SkyboxEventRejected('MISSING_FIELD', "data.orderId manquant.")
        order, status, note = self.env['skybox.preparation.order']._receive_cancel(
            data, self)
        self.preparation_order_id = order
        return {'reference': order.reference or order.name, 'status': status,
                'note': note}
