# -*- coding: utf-8 -*-
import logging
import secrets

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SkyboxApiKey(models.Model):
    _name = 'skybox.api.key'
    _description = "Cle API"
    _order = 'id desc'

    name = fields.Char(string="Nom", required=True)
    api_type = fields.Selection(
        selection=[
            ('my_api', "My API"),
            ('api_externe', "API externe"),
            ('login', "Login"),
        ],
        string="Type d'API", default='api_externe', required=True,
        help="My API : API exposee par Odoo vers un autre programme.\n"
             "API externe : API tierce dont Odoo recupere les donnees.\n"
             "Login : cle permettant a un programme externe d'authentifier "
             "les utilisateurs Odoo (email + mot de passe).",
    )
    key = fields.Char(string="Cle", copy=False)
    token = fields.Char(string="Token", copy=False)
    last_used = fields.Datetime(string="Derniere utilisation", readonly=True)
    warehouse_code = fields.Char(
        string="Code entrepot",
        help="Envoye dans le champ warehouseCode du check (ex: WH-CASA-01).",
    )
    operator_code = fields.Char(
        string="Code operateur",
        help="Envoye dans le champ operator du check (ex: ODOO-AGENT-01).",
    )
    url = fields.Char(
        string="URL de l'API",
        help="Point d'acces a tester (ex: https://api.exemple.com/ping).",
    )
    auth_type = fields.Selection(
        selection=[
            ('bearer', "Bearer (Authorization: Bearer <cle>)"),
            ('header', "En-tete personnalise"),
            ('query', "Parametre d'URL"),
            ('none', "Aucune"),
        ],
        string="Authentification", default='bearer', required=True,
    )
    header_name = fields.Char(
        string="Nom de l'en-tete", default='X-API-Key',
        help="Utilise quand l'authentification est 'En-tete personnalise'.",
    )
    query_param = fields.Char(
        string="Nom du parametre", default='api_key',
        help="Utilise quand l'authentification est 'Parametre d'URL'.",
    )
    active = fields.Boolean(string="Actif", default=True)
    create_date = fields.Datetime(string="Date de creation", readonly=True)
    state = fields.Selection(
        selection=[
            ('draft', "Non teste"),
            ('success', "Connecte"),
            ('failed', "Echec"),
        ],
        string="Statut", default='draft', readonly=True, copy=False,
    )
    last_test_message = fields.Char(
        string="Dernier resultat", readonly=True, copy=False,
    )

    def write(self, vals):
        # Reinitialiser le statut uniquement quand la cle ou l'URL change.
        if 'state' not in vals and ('key' in vals or 'url' in vals):
            for rec in self:
                if (('key' in vals and rec.key != vals['key'])
                        or ('url' in vals and rec.url != vals['url'])):
                    vals = dict(vals, state='draft')
                    break
        return super().write(vals)

    @staticmethod
    def _new_token():
        return secrets.token_urlsafe(32)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # My API / Login : la cle est generee automatiquement par Odoo.
            if vals.get('api_type') in ('my_api', 'login') and not vals.get('key'):
                vals['key'] = self._new_token()
        return super().create(vals_list)

    @api.constrains('api_type', 'key')
    def _check_key(self):
        for rec in self:
            if rec.api_type == 'api_externe' and not rec.key:
                raise ValidationError(
                    "Une API externe doit avoir une cle.")

    def action_generate_key(self):
        """My API : (re)genere la cle a communiquer au programme externe."""
        for rec in self:
            rec.key = self._new_token()
        return True

    def action_save(self):
        # Le clic sur un bouton enregistre l'enregistrement au prealable.
        return True

    def _build_request(self):
        """Construit (headers, params) selon le mode d'authentification."""
        self.ensure_one()
        headers, params = {}, {}
        if self.auth_type == 'bearer':
            headers['Authorization'] = 'Bearer %s' % (self.key or '')
        elif self.auth_type == 'header':
            headers[self.header_name or 'X-API-Key'] = self.key or ''
        elif self.auth_type == 'query':
            params[self.query_param or 'api_key'] = self.key or ''
        return headers, params

    def _do_request(self, headers, params):
        """Retourne (status_code, reason) ou leve une exception reseau."""
        response = requests.get(
            self.url, headers=headers, params=params, timeout=10,
        )
        return response.status_code, response.reason

    def _check_receptions(self, numbers):
        """Envoie le lot de numeros de tracking a l'endpoint de check du
        backoffice (POST) et retourne le JSON de reponse, ou None.

        Requete envoyee:
            POST <url>
            Content-Type: application/json
            X-API-KEY: <cle>   (selon l'authentification configuree)
            {
              "batchId": "...",
              "warehouseCode": "...",
              "packages": [{"trackingNumber", "operator", "receivedAt"}, ...]
            }
        """
        self.ensure_one()
        if not self.url or not numbers:
            return None

        auth_headers, params = self._build_request()
        headers = {'Content-Type': 'application/json'}
        headers.update(auth_headers)

        now = fields.Datetime.now()
        user = self.env.user
        received_at = now.strftime('%Y-%m-%dT%H:%M:%S')
        warehouse_code = self.warehouse_code or (
            user.warehouse_id.code if user.warehouse_id else '')
        operator = self.operator_code or user.login
        payload = {
            'batchId': "ODOO-%s" % now.strftime('%Y%m%d-%H%M%S'),
            'warehouseCode': warehouse_code,
            'packages': [{
                'trackingNumber': number,
                'operator': operator,
                'receivedAt': received_at,
            } for number in numbers],
        }

        try:
            response = requests.post(
                self.url, json=payload, headers=headers, params=params,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            _logger.exception("Skybox: echec du check des receptions")
            return None
        if not (200 <= response.status_code < 300):
            _logger.warning("Skybox: backoffice a repondu %s au check",
                            response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            _logger.warning("Skybox: reponse non-JSON au check des receptions")
            return None

    def action_test_connectivity(self):
        self.ensure_one()
        if not self.url:
            message = "Veuillez renseigner l'URL de l'API avant de tester."
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Test impossible", message, 'danger')

        headers, params = self._build_request()
        try:
            # Appel AVEC la cle.
            auth_code, auth_reason = self._do_request(headers, params)
            # Appel SANS la cle (pour verifier que la cle est vraiment exigee).
            if self.auth_type != 'none':
                anon_code, _ = self._do_request({}, {})
            else:
                anon_code = None
        except requests.exceptions.Timeout:
            message = "Delai d'attente depasse (timeout)."
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Echec de la connexion", message, 'danger')
        except requests.exceptions.ConnectionError:
            message = "Impossible de se connecter a l'URL."
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Echec de la connexion", message, 'danger')
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Echec du test de connectivite API")
            message = "Erreur: %s" % exc
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Echec de la connexion", message, 'danger')

        with_key = "HTTP %s %s" % (auth_code, auth_reason)

        # 1) La cle est refusee -> echec.
        if auth_code in (401, 403):
            message = "Cle refusee par l'API (%s)." % with_key
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Cle invalide", message, 'danger')

        # 2) La cle n'est pas 2xx -> echec (erreur cote API).
        if not (200 <= auth_code < 300):
            message = "Reponse inattendue: %s" % with_key
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Echec de la connexion", message, 'danger')

        # 3) Meme reponse sans la cle -> l'endpoint ne valide pas la cle.
        if anon_code is not None and anon_code == auth_code:
            message = ("L'URL repond (%s) mais renvoie la meme reponse SANS la "
                       "cle: cet endpoint ne valide pas la cle. Utilisez un "
                       "endpoint authentifie de l'API." % with_key)
            self.write({'state': 'failed', 'last_test_message': message})
            return self._notify("Cle non validee", message, 'warning')

        # 4) Sans cle refuse (401/403) et avec cle 2xx -> cle valide.
        message = "Cle valide, l'API repond correctement (%s)." % with_key
        self.write({'state': 'success', 'last_test_message': message})
        return self._notify("Connexion reussie", message, 'success')

    def _notify(self, title, message, notif_type):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': notif_type,  # success / warning / danger / info
                'sticky': True,
            },
        }
