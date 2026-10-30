# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SkyboxEventController(http.Controller):
    """Callback Skybox -> Odoo : POST /skybox/events (JSON brut, pas JSON-RPC).

    Authentification : une cle API de type "My API" (Configuration > API
    management), envoyee selon son mode (Bearer / en-tete / parametre d'URL).
    Reponse (ACK, guide v2 section 4) :
        {"status": "ACCEPTED" | "DUPLICATE" | "PARTIAL" | "REJECTED",
         "partnerReference": "...", "reasonCode": "...", "message": "...",
         "parcels": [...]}
    Codes HTTP : 200 ACCEPTED / DUPLICATE / PARTIAL / REJECTED (refus metier
    definitif), 400 enveloppe invalide (definitif), 401 secret absent ou faux
    (Skybox renvoie), 500 erreur Odoo passagere (Skybox renvoie).
    """

    def _ack(self, http_status, status, reason_code=None, message=None):
        body = {'status': status}
        if reason_code:
            body['reasonCode'] = reason_code
        if message:
            body['message'] = message
        return request.make_json_response(body, status=http_status)

    @staticmethod
    def _auth_hint(httpreq):
        """Ce que la requete contenait comme authentification (noms d'en-tetes
        et longueur de la valeur, jamais le secret)."""
        found = []
        for name in ('X-Skybox-Secret', 'Authorization', 'X-API-Key'):
            value = httpreq.headers.get(name)
            if value is not None:
                found.append("%s (%s car.)" % (name, len(value)))
        for name in ('api_key',):
            if name in httpreq.args:
                found.append("?%s" % name)
        return ("en-tetes recus: %s" % ", ".join(found)) if found else \
            "aucun en-tete d'authentification recu"

    @http.route('/skybox/events', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def skybox_events(self, **kwargs):
        httpreq = request.httprequest
        headers = httpreq.headers
        event_hint = headers.get('Idempotency-Key') or '-'

        content_type = (httpreq.content_type or '').lower()
        if 'application/json' not in content_type:
            _logger.warning("Skybox callback %s : Content-Type refuse (%s)",
                            event_hint, content_type)
            return self._ack(400, 'REJECTED', 'UNSUPPORTED_MEDIA_TYPE',
                             "Content-Type must be application/json")

        api_key = request.env['skybox.api.key'].sudo()._match_incoming_request(
            headers, httpreq.args)
        if not api_key:
            _logger.warning(
                "Skybox callback %s : authentification refusee (ip %s) - %s",
                event_hint, httpreq.remote_addr, self._auth_hint(httpreq))
            # 401 = echec temporaire pour Skybox (renvoi apres correction).
            return self._ack(401, 'UNAUTHORIZED', message="Authentication failed")

        try:
            envelope = json.loads(httpreq.get_data(as_text=True) or 'null')
        except ValueError:
            _logger.warning("Skybox callback %s : JSON invalide", event_hint)
            return self._ack(400, 'REJECTED', 'INVALID_JSON', "Invalid JSON body")

        status, ack = request.env['skybox.event'].sudo()._receive(
            envelope, headers, api_key=api_key)
        return request.make_json_response(ack, status=status)

    # ------------------------------------------------------------------
    # Backoffice Skybox : traitement d'un bag (scan / fermeture)
    # ------------------------------------------------------------------
    def _backoffice_bag(self, route):
        """Authentifie (cle "My API") et lit {"bagCode": ...}.
        Retourne (bag, corps, None) ou (None, None, reponse d'erreur)."""
        httpreq = request.httprequest
        if 'application/json' not in (httpreq.content_type or '').lower():
            return None, None, self._ack(400, 'REJECTED', 'UNSUPPORTED_MEDIA_TYPE',
                                         "Content-Type must be application/json")
        api_key = request.env['skybox.api.key'].sudo()._match_incoming_request(
            httpreq.headers, httpreq.args)
        if not api_key:
            _logger.warning("Skybox %s : authentification refusee (ip %s) - %s",
                            route, httpreq.remote_addr, self._auth_hint(httpreq))
            return None, None, self._ack(401, 'UNAUTHORIZED',
                                         message="Authentication failed")
        try:
            body = json.loads(httpreq.get_data(as_text=True) or 'null')
        except ValueError:
            return None, None, self._ack(400, 'REJECTED', 'INVALID_JSON',
                                         "Invalid JSON body")
        if not isinstance(body, dict):
            return None, None, self._ack(400, 'REJECTED', 'INVALID_JSON',
                                         "Body must be a JSON object")
        code = str(body.get('bagCode') or '').strip()
        if not code:
            return None, None, self._ack(400, 'REJECTED', 'MISSING_FIELD',
                                         "bagCode manquant.")
        bag = request.env['stock.quant.package'].sudo().search(
            [('name', '=ilike', code)], limit=1)
        if not bag:
            return None, None, self._ack(200, 'REJECTED', 'UNKNOWN_BAG',
                                         "Bag inconnu d'Odoo: %s" % code)
        if bag.skybox_open_state == 'open':
            return None, None, self._ack(
                200, 'REJECTED', 'BAG_STILL_OPEN',
                "Bag %s encore ouvert dans Odoo (ouvert par %s) : "
                "le fermer avant le traitement." % (
                    bag.name, bag.skybox_opened_by.name or '-'))
        return bag, body, None

    @http.route('/skybox/bag/scan', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def skybox_bag_scan(self, **kwargs):
        """Backoffice : scan d'un bag a traiter. Ouvre le bag pour le
        traitement et renvoie ses colis.

        Corps JSON : {"bagCode": "BAG001", "operator": "...", "sessionId": "..."}
        Bag ouvert en reception (app mobile) -> REJECTED BAG_STILL_OPEN.
        Bag deja ouvert pour le traitement -> ACCEPTED, alreadyOpen: true.
        """
        bag, body, error = self._backoffice_bag('bag/scan')
        if error:
            return error
        already = bag.skybox_open_state == 'processing'
        if not already:
            bag._skybox_open_processing(body.get('operator'), body.get('sessionId'))
        _logger.info("Skybox bag/scan %s : ouvert pour traitement (deja: %s)",
                     bag.name, already)
        return request.make_json_response(dict(
            bag._skybox_backoffice_dict(), status='ACCEPTED', alreadyOpen=already))

    @http.route('/skybox/bag/close', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def skybox_bag_close(self, **kwargs):
        """Backoffice : fin du traitement d'un bag, le referme.

        Corps JSON : {"bagCode": "BAG001", "operator": "..."}
        Bag ouvert en reception (app mobile) -> REJECTED BAG_STILL_OPEN.
        Bag deja ferme -> ACCEPTED, alreadyClosed: true.
        """
        bag, body, error = self._backoffice_bag('bag/close')
        if error:
            return error
        already = bag.skybox_open_state == 'closed'
        if not already:
            bag._skybox_close_processing(body.get('operator'))
        _logger.info("Skybox bag/close %s : ferme (deja: %s)", bag.name, already)
        return request.make_json_response(dict(
            bag._skybox_backoffice_dict(), status='ACCEPTED', alreadyClosed=already))

    @http.route('/skybox/ping', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False, save_session=False)
    def skybox_ping(self, **kwargs):
        """Test de connectivite backoffice -> Odoo.

        Meme authentification que /skybox/events (cle "My API").
        200 {"status": "OK", ...}      : Odoo joignable, cle valide, base OK
        401 {"status": "UNAUTHORIZED"} : cle absente ou invalide
        500 {"status": "ERROR"}        : Odoo joignable mais base indisponible
        """
        httpreq = request.httprequest
        try:
            api_key = request.env['skybox.api.key'].sudo()._match_incoming_request(
                httpreq.headers, httpreq.args)
            if not api_key:
                _logger.warning("Skybox ping : authentification refusee (ip %s)",
                                httpreq.remote_addr)
                return request.make_json_response(
                    {'status': 'UNAUTHORIZED', 'message': "Authentication failed"},
                    status=401)
            request.env.cr.execute("SELECT 1")
            return request.make_json_response({
                'status': 'OK',
                'service': 'odoo',
                'partnerCode': api_key.partner_code or None,
                'serverTime': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
            }, status=200)
        except Exception:  # noqa: BLE001
            _logger.exception("Skybox ping : erreur")
            return request.make_json_response(
                {'status': 'ERROR', 'message': "Odoo internal error"}, status=500)
