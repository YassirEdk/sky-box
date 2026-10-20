# -*- coding: utf-8 -*-
from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from .main import SkyboxLoginController
from ..models.preparation_order import REASSIGN_STATES

# Etapes que le picker peut faire depuis l'app (Expedie reste dans Odoo).
STEP_ACTIONS = {
    'picking': 'action_picking',
    'picked': 'action_picked',
    'packed': 'action_packed',
    'ready': 'action_ready',
}
PICK_STATES = ('PICKED', 'MISSING', 'DAMAGED')


def _dt(value):
    return fields.Datetime.to_string(value) if value else None


class SkyboxPickerApi(SkyboxLoginController):
    """Ordres de preparation pour l'app mobile (pickers)."""

    def _order_for(self, user, kwargs):
        """Ordre par id ("order_id") ou nom ("order"), affecte au picker.
        Managers/Admins peuvent ouvrir tous les ordres.
        Retourne (ordre, None) ou (None, erreur)."""
        env = self._agent_env(user)
        Order = env['skybox.preparation.order']
        order_id = kwargs.get('order_id')
        name = (kwargs.get('order') or '').strip()
        if not order_id and not name:
            return None, {'success': False, 'error': 'missing_order'}
        if order_id:
            order = Order.browse(int(order_id)).exists()
        else:
            order = Order.search(['|', ('name', '=', name),
                                  ('reference', '=', name)], limit=1)
        if not order:
            return None, {'success': False, 'error': 'order_not_found'}
        if (order.agent_id != user
                and self._user_role(user) not in ('admin', 'manager')):
            return None, {'success': False, 'error': 'not_your_order'}
        return order, None

    def _order_summary(self, order):
        lines = order.line_ids
        return {
            'order_id': order.id,
            'order': order.name,
            'reference': order.reference,
            'state': order.state,
            'state_label': dict(order._fields['state'].selection)[order.state],
            'priority': order.priority or None,
            'type': order.order_type or None,
            'client': order.partner_id.name or order.ship_to_name or None,
            'suite_number': order.suite_number or None,
            'skybill_number': order.skybill_number or None,
            'total_parcels': order.total_parcels or len(lines),
            'picked_parcels': len(lines.filtered('pick_state')),
            'total_weight': order.total_weight,
            'requested_at': _dt(order.requested_at),
            'has_exception': order.has_exception,
            'sync_state': order.sync_state,
            # Skybox demande l'annulation : etapes et scans bloques.
            'cancel_requested': order._cancel_blocked(),
        }

    def _order_detail(self, order):
        data = self._order_summary(order)
        data.update({
            'instructions': order.instructions or None,
            'warehouse': order.warehouse_id.name or order.warehouse_code or None,
            'package_code': order.package_code or None,
            'ship_to': {
                'name': order.ship_to_name or None,
                'phone': order.ship_to_phone or None,
                'address': order.ship_to_address or None,
                'city': order.ship_to_city or None,
                'country': order.ship_to_country_id.name or None,
            },
            'next_steps': [step for step in STEP_ACTIONS
                           if self._step_allowed(order, step)],
            'lines': [{
                'line_id': line.id,
                'tracking_number': line.tracking_number,
                'skybill_number': line.skybill_number or None,
                'bag': line.bag_id.name or line.current_bag_code or None,
                'zone': line.zone_code or None,
                'aisle': line.aisle_code or None,
                'rack': line.rack_code or None,
                'location': line.location_id.complete_name or line.location_code or None,
                'wh_location': line.wh_location_id.complete_name or None,
                'wh_location_id': line.wh_location_id.id or None,
                'wh_bag': line.wh_bag_id.name or None,
                'weight': line.weight,
                'fragile': line.fragile,
                'pick_state': line.pick_state or None,
            } for line in order.line_ids],
        })
        return data

    def _step_allowed(self, order, step):
        if order.state in ('dispatched', 'cancelled') or order._cancel_blocked():
            return False
        ranks = list(STEP_ACTIONS)
        current = ranks.index(order.state) if order.state in ranks else -1
        return ranks.index(step) > current

    @http.route('/skybox/api/orders', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_orders(self, **kwargs):
        """Ordres de preparation en cours du picker connecte, avec leurs
        colis ("lines") et les etapes possibles ("next_steps").
        Les statuts se renvoient ensuite avec POST /skybox/api/order/step.

        En-tete: Authorization: Bearer <access_token>
        Corps JSON (optionnel): {"state": "new"}
        Sert aussi de "heartbeat" : l'appeler chaque minute garde le picker
        dans le cycle d'affectation.
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        env = self._agent_env(user)
        domain = [('agent_id', '=', user.id), ('state', 'in', REASSIGN_STATES)]
        if kwargs.get('state'):
            domain.append(('state', '=', kwargs['state']))
        orders = env['skybox.preparation.order'].search(domain, order='id')
        return {
            'success': True,
            'count': len(orders),
            'orders': [self._order_detail(o) for o in orders],
        }

    @http.route('/skybox/api/order', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_order(self, **kwargs):
        """Detail d'un ordre et de ses colis a prelever.

        Corps JSON: {"order_id": 12} ou {"order": "PO-0001"}
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        order, error = self._order_for(user, kwargs)
        if error:
            return error
        return dict(self._order_detail(order), success=True)

    @http.route('/skybox/api/order/scan', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_order_scan(self, **kwargs):
        """Scan d'un colis de l'ordre pendant le picking.

        Corps JSON: {"order_id": 12, "tracking_number": "...",
                     "pick_state": "PICKED" | "MISSING" | "DAMAGED"}
        Un ordre "Nouveau" passe automatiquement en "Picking".
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        order, error = self._order_for(user, kwargs)
        if error:
            return error
        line, error = self._scan_line(order, kwargs)
        if error:
            return error
        return dict(self._order_summary(order), success=True,
                    line_id=line.id, tracking_number=line.tracking_number,
                    pick_state=line.pick_state)

    @http.route('/skybox/api/order/step', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_order_step(self, **kwargs):
        """Passe un ou plusieurs ordres a l'etape suivante et l'envoie a
        Skybox.

        En-tete: Authorization: Bearer <access_token>  (-> picker de l'action)
        Corps JSON attendu:
            {"orders": [
                {"order_id": 12, "step": "picked",
                 "parcels": [{"tracking_number": "...",
                              "pick_state": "MISSING"}]},
                {"order": "PO-0001", "step": "packed",
                 "package_code": "..."}]}
        (un seul ordre : {"order_id", "step", "package_code"} accepte)
        step : "picking" | "picked" | "packed" | "ready".
        parcels (optionnel, ordre Nouveau ou Picking) : etat de prelevement
        des colis ("PICKED" | "MISSING" | "DAMAGED"), applique avant l'etape ;
        a "picked", les colis non scannes passent en PICKED.
        Chaque ordre est traite seul : en cas d'erreur, rien n'est modifie
        sur cet ordre et les autres continuent.
        L'expedition (transporteur + suivi) se fait dans Odoo.
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        items = kwargs.get('orders')
        if items is None:
            order, error = self._order_for(user, kwargs)
            if error:
                return error
            return self._step_order(order, kwargs)
        if not isinstance(items, list) or not items:
            return {'success': False, 'error': 'missing_orders'}

        results = []
        for item in items:
            item = item if isinstance(item, dict) else {}
            order, error = self._order_for(user, item)
            result = error or self._step_order(order, item)
            result.setdefault('order_id', item.get('order_id'))
            result.setdefault('order', item.get('order'))
            results.append(result)
        done = [r for r in results if r['success']]
        return {
            'success': len(done) == len(results),
            'agent': user.name,
            'agent_id': user.id,
            'date': _dt(fields.Datetime.now()),
            'count': len(results),
            'done': len(done),
            'failed': len(results) - len(done),
            'orders': results,
        }

    def _scan_line(self, order, item):
        """Etat de prelevement d'un colis de l'ordre (passe un ordre Nouveau
        en Picking). Retourne (ligne, None) ou (None, erreur)."""
        tracking = (item.get('tracking_number') or '').strip()
        if not tracking:
            return None, {'success': False, 'error': 'missing_tracking_number'}
        pick_state = (item.get('pick_state') or 'PICKED').upper()
        if pick_state not in PICK_STATES:
            return None, {'success': False, 'error': 'invalid_pick_state',
                          'tracking_number': tracking}
        if order.state not in ('new', 'picking'):
            return None, {'success': False, 'error': 'order_not_in_picking',
                          'state': order.state}
        if order._cancel_blocked():
            return None, {'success': False, 'error': 'cancel_requested',
                          'message': "Skybox demande l'annulation de cet "
                                     "ordre : attendez la decision du "
                                     "responsable."}
        line = order.line_ids.filtered(
            lambda l: (l.tracking_number or '').lower() == tracking.lower())[:1]
        if not line:
            return None, {'success': False, 'error': 'parcel_not_in_order',
                          'tracking_number': tracking}
        try:
            if order.state == 'new':
                order.action_picking()
            line.pick_state = pick_state
        except UserError as e:
            return None, {'success': False, 'error': 'step_refused',
                          'message': str(e)}
        return line, None

    def _step_order(self, order, item):
        """Colis (optionnel) puis etape d'un ordre, en une seule fois : en cas
        d'erreur, l'ordre est remis dans son etat initial."""
        step = (item.get('step') or '').strip().lower()
        if step not in STEP_ACTIONS:
            return {'success': False, 'error': 'invalid_step',
                    'allowed': list(STEP_ACTIONS)}
        parcels = item.get('parcels') or []
        if not isinstance(parcels, list):
            return {'success': False, 'error': 'invalid_parcels'}
        try:
            with request.env.cr.savepoint():
                for parcel in parcels:
                    _line, error = self._scan_line(
                        order, parcel if isinstance(parcel, dict) else {})
                    if error:
                        raise _StepAborted(error)
                if not self._step_allowed(order, step):
                    raise _StepAborted({'success': False,
                                        'error': 'step_not_allowed',
                                        'state': order.state})
                if step == 'packed' and item.get('package_code'):
                    order.package_code = item['package_code']
                getattr(order, STEP_ACTIONS[step])()
        except _StepAborted as e:
            order.invalidate_recordset()
            order.line_ids.invalidate_recordset()
            return e.error
        except UserError as e:
            order.invalidate_recordset()
            order.line_ids.invalidate_recordset()
            return {'success': False, 'error': 'step_refused',
                    'message': str(e)}
        return dict(self._order_detail(order), success=True,
                    sync_message=order.sync_message or None)


class _StepAborted(Exception):
    """Annule les modifications d'un ordre (savepoint) avec cette erreur."""

    def __init__(self, error):
        super().__init__(error.get('error'))
        self.error = error
