# -*- coding: utf-8 -*-
"""Track order : recherche dans tout le systeme (ordres de preparation,
colis traites, receptions, prealertes). Chaque colis / ordre n'apparait
qu'une fois, a son etape actuelle :

Phases : Prealerte (20 %) -> Recu (40 %) -> Traite (60 %) -> En
preparation (80 %) -> Expedie (Past orders).

1. dans un ordre de preparation en cours -> l'ordre (la commande groupee
   pour une commande groupee) ;
2. sinon traite par Skybox                -> Colis traite ;
3. sinon recu a l'entrepot                -> Reception (Receipt / Stored) ;
4. sinon seulement prealerte              -> Prealerte.

Les ordres expedies et annules n'y sont pas ; la liste reste vide tant
qu'on n'a rien cherche.
"""
from odoo import api, fields, models, tools

OPEN_STATES = "('new', 'picking', 'picked', 'packed', 'ready')"


class SkyboxTrack(models.Model):
    _name = 'skybox.track'
    _description = "Track order"
    _auto = False
    _order = 'date desc, id desc'
    _rec_name = 'name'

    kind = fields.Selection(
        selection=[
            ('prealerte', "1. Prealerte"),
            ('reception', "2. Recu"),
            ('parcel', "3. Traite"),
            ('order', "4. En preparation"),
        ],
        string="Phase", readonly=True,
        help="Prealerte (pas encore recu) -> Recu a l'entrepot -> Traite par "
             "Skybox -> En preparation -> (Expedie, dans Past orders).")
    progress = fields.Integer(string="Avancement", readonly=True)
    name = fields.Char(string="Reference", readonly=True)
    tracking_numbers = fields.Char(string="Tracking", readonly=True)
    partner_id = fields.Many2one('res.partner', string="Client", readonly=True)
    suite_number = fields.Char(string="Suite", readonly=True)
    skybill = fields.Char(string="Skybill", readonly=True)
    status = fields.Char(string="Statut", readonly=True)
    state = fields.Char(string="Code statut", readonly=True)
    location = fields.Char(string="Emplacement / bag", readonly=True)
    parcel_count = fields.Integer(string="Colis", readonly=True)
    date = fields.Datetime(string="Date", readonly=True)
    search_text = fields.Char(string="Recherche", readonly=True)
    order_id = fields.Many2one('skybox.preparation.order', string="Ordre", readonly=True)
    parcel_id = fields.Many2one('skybox.parcel', string="Colis traite", readonly=True)
    picking_id = fields.Many2one('stock.picking', string="Reception", readonly=True)
    prealerte_id = fields.Many2one('skybox.prealerte', string="Prealerte", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %(table)s AS (
            WITH active_tn AS (
                SELECT DISTINCT upper(l.tracking_number) AS tn
                  FROM skybox_preparation_order_line l
                  JOIN skybox_preparation_order o ON o.id = l.order_id
                 WHERE NOT coalesce(o.is_group, false)
                   AND o.state IN %(open)s
                   AND l.tracking_number IS NOT NULL
            ),
            parcel_tn AS (
                SELECT DISTINCT upper(tracking_number) AS tn
                  FROM skybox_parcel WHERE tracking_number IS NOT NULL
            ),
            reception AS (
                SELECT sp.*,
                       CASE WHEN sp.skybox_status = 'stored' THEN sl.complete_name
                            ELSE bag.name END AS place
                  FROM stock_picking sp
                  JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
                       AND spt.code = 'incoming'
                  LEFT JOIN stock_location sl ON sl.id = sp.location_dest_id
                  LEFT JOIN stock_quant_package bag ON bag.id = sp.bag
                 WHERE sp.state != 'cancel'
                   AND coalesce(sp.skybox_status, '') != 'canceled'
            )

            -- 1. Ordres de preparation en cours (commande groupee seule)
            SELECT o.id * 10 + 1 AS id,
                   'order' AS kind,
                   80 AS progress,
                   o.name AS name,
                   (SELECT string_agg(l.tracking_number, ', ' ORDER BY l.id)
                      FROM skybox_preparation_order_line l
                     WHERE l.order_id = o.id) AS tracking_numbers,
                   o.partner_id AS partner_id,
                   o.suite_number AS suite_number,
                   o.skybill_number AS skybill,
                   CASE o.state
                        WHEN 'new' THEN 'Nouveau'
                        WHEN 'picking' THEN 'Picking'
                        WHEN 'picked' THEN 'Picked'
                        WHEN 'packed' THEN 'Packed'
                        WHEN 'ready' THEN 'Pret a expedier'
                   END
                   || CASE WHEN o.cancel_state = 'requested'
                           THEN ' - annulation demandee' ELSE '' END AS status,
                   o.state AS state,
                   NULL::varchar AS location,
                   (SELECT count(*) FROM skybox_preparation_order_line l
                     WHERE l.order_id = o.id) AS parcel_count,
                   coalesce(o.requested_at, o.create_date) AS date,
                   concat_ws(' ', o.name, o.reference, o.suite_number,
                             o.skybill_number, o.ship_to_name, rp.name,
                             (SELECT string_agg(concat_ws(' ', c.name, c.skybill_number), ' ')
                                FROM skybox_preparation_order c WHERE c.parent_id = o.id),
                             (SELECT string_agg(l.tracking_number, ' ')
                                FROM skybox_preparation_order_line l
                               WHERE l.order_id = o.id)) AS search_text,
                   o.id AS order_id,
                   NULL::integer AS parcel_id,
                   NULL::integer AS picking_id,
                   NULL::integer AS prealerte_id
              FROM skybox_preparation_order o
              LEFT JOIN res_partner rp ON rp.id = o.partner_id
             WHERE o.parent_id IS NULL
               AND o.state IN %(open)s

            UNION ALL

            -- 2. Colis traites (pas dans un ordre en cours, pas expedies)
            SELECT p.id * 10 + 2, 'parcel', 60, p.tracking_number, p.tracking_number,
                   p.partner_id, p.suite_number, p.skybill_number,
                   concat_ws(' - ', 'Colis traite', nullif(p.result, '')),
                   'parcel',
                   r.place,
                   1,
                   coalesce(p.processed_at, p.create_date),
                   concat_ws(' ', p.tracking_number, p.suite_number, p.skybill_number,
                             p.prealert_ref, rp.name, r.name),
                   NULL, p.id, r.id, p.prealerte_id
              FROM skybox_parcel p
              LEFT JOIN res_partner rp ON rp.id = p.partner_id
              LEFT JOIN reception r ON r.id = p.picking_id
             WHERE NOT coalesce(p.skybox_past, false)
               AND upper(p.tracking_number) NOT IN (SELECT tn FROM active_tn)

            UNION ALL

            -- 3. Receptions (pas encore traitees par Skybox)
            SELECT r.id * 10 + 3, 'reception', 40, r.name, r.tracking_number,
                   r.client_id, r.suite_number, NULL,
                   CASE r.skybox_status
                        WHEN 'receipt' THEN 'Reception - Receipt'
                        WHEN 'stored' THEN 'Reception - Stored'
                        WHEN 'processed' THEN 'Reception - Processed'
                        ELSE 'Reception' END,
                   'reception',
                   r.place,
                   1,
                   r.create_date,
                   concat_ws(' ', r.tracking_number, r.name, r.suite_number, rp.name),
                   NULL, NULL, r.id, NULL
              FROM reception r
              LEFT JOIN res_partner rp ON rp.id = r.client_id
             WHERE NOT coalesce(r.skybox_past, false)
               AND upper(r.tracking_number) NOT IN (SELECT tn FROM active_tn)
               AND upper(r.tracking_number) NOT IN (SELECT tn FROM parcel_tn)

            UNION ALL

            -- 4. Prealertes seules (colis pas encore recu)
            SELECT pr.id * 10 + 4, 'prealerte', 20, pr.name, pr.tracking_number,
                   pr.partner_id, pr.suite_number, pr.skybill,
                   'Prealerte - colis pas encore recu',
                   'prealerte',
                   NULL,
                   1,
                   pr.create_date,
                   concat_ws(' ', pr.tracking_number, pr.name, pr.suite_number,
                             pr.skybill, rp.name),
                   NULL, NULL, NULL, pr.id
              FROM skybox_prealerte pr
              LEFT JOIN res_partner rp ON rp.id = pr.partner_id
             WHERE NOT coalesce(pr.skybox_past, false)
               AND upper(pr.tracking_number) NOT IN (SELECT tn FROM active_tn)
               AND upper(pr.tracking_number) NOT IN (SELECT tn FROM parcel_tn)
               AND NOT EXISTS (SELECT 1 FROM reception r
                                WHERE upper(r.tracking_number) = upper(pr.tracking_number))
            )
        """ % {'table': self._table, 'open': OPEN_STATES})

    # --- Liste vide tant qu'on n'a rien cherche ---
    @api.model
    def web_search_read(self, domain, specification, offset=0, limit=None,
                        order=None, count_limit=None):
        if not domain:
            return {'length': 0, 'records': []}
        return super().web_search_read(
            domain, specification, offset=offset, limit=limit, order=order,
            count_limit=count_limit)

    @api.model
    def web_read_group(self, domain, fields, groupby, limit=None, offset=0,
                       orderby=False, lazy=True):
        if not domain:
            return {'groups': [], 'length': 0}
        return super().web_read_group(
            domain, fields, groupby, limit=limit, offset=offset,
            orderby=orderby, lazy=lazy)

    def action_open(self):
        """Clic sur une ligne : ouvre la fiche (ordre, colis, reception ou
        prealerte)."""
        self.ensure_one()
        record = (self.order_id or self.parcel_id or self.picking_id
                  or self.prealerte_id)
        return {
            'type': 'ir.actions.act_window',
            'res_model': record._name,
            'res_id': record.id,
            'view_mode': 'form',
            'target': 'current',
        }
