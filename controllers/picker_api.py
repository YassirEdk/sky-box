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
                'weight': line.weight,
                'fragile': line.fragile,
                'pick_state': line.pick_state or None,
            } for line in order.line_ids],
        })
        return data

    def _step_allowed(self, order, step):
        if order.state in ('dispatched', 'cancelled'):
            return False
        ranks = list(STEP_ACTIONS)
        current = ranks.index(order.state) if order.state in ranks else -1
        return ranks.index(step) > current

    @http.route('/skybox/api/orders', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_orders(self, **kwargs):
        """Ordres de preparation en cours du picker connecte.

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
            'orders': [self._order_summary(o) for o in orders],
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
        tracking = (kwargs.get('tracking_number') or '').strip()
        if not tracking:
            return {'success': False, 'error': 'missing_tracking_number'}
        pick_state = (kwargs.get('pick_state') or 'PICKED').upper()
        if pick_state not in PICK_STATES:
            return {'success': False, 'error': 'invalid_pick_state'}
        if order.state not in ('new', 'picking'):
            return {'success': False, 'error': 'order_not_in_picking',
                    'state': order.state}
        line = order.line_ids.filtered(
            lambda l: (l.tracking_number or '').lower() == tracking.lower())[:1]
        if not line:
            return {'success': False, 'error': 'parcel_not_in_order'}
        try:
            if order.state == 'new':
                order.action_picking()
            line.pick_state = pick_state
        except UserError as e:
            return {'success': False, 'error': 'step_refused', 'message': str(e)}
        return dict(self._order_summary(order), success=True,
                    line_id=line.id, tracking_number=line.tracking_number,
                    pick_state=line.pick_state)

    @http.route('/skybox/api/order/step', type='json', auth='public',
                methods=['POST'], csrf=False)
    def api_order_step(self, **kwargs):
        """Passe l'ordre a l'etape suivante et l'envoie a Skybox.

        Corps JSON: {"order_id": 12,
                     "step": "picking" | "picked" | "packed" | "ready",
                     "package_code": "..." (optionnel, a l'etape packed)}
        L'expedition (transporteur + suivi) se fait dans Odoo.
        """
        user, error = self._agent(kwargs)
        if error:
            return error
        order, error = self._order_for(user, kwargs)
        if error:
            return error
        step = (kwargs.get('step') or '').strip().lower()
        if step not in STEP_ACTIONS:
            return {'success': False, 'error': 'invalid_step',
                    'allowed': list(STEP_ACTIONS)}
        if not self._step_allowed(order, step):
            return {'success': False, 'error': 'step_not_allowed',
                    'state': order.state}
        try:
            if step == 'packed' and kwargs.get('package_code'):
                order.package_code = kwargs['package_code']
            getattr(order, STEP_ACTIONS[step])()
        except UserError as e:
            return {'success': False, 'error': 'step_refused', 'message': str(e)}
        return dict(self._order_summary(order), success=True,
                    sync_message=order.sync_message or None)
