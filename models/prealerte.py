# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SkyboxPrealerte(models.Model):
    _name = 'skybox.prealerte'
    _description = "Prealerte"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string="Reference", required=True, default="New",
        copy=False, readonly=True, index=True,
    )

    # --- En-tete ---
    tracking_number = fields.Char(
        string="Numero de tracking transporteur", tracking=True,
    )
    suite_number = fields.Char(
        string="Suite du client", tracking=True,
    )
    courier_id = fields.Many2one(
        comodel_name='skybox.courier', string="Transporteur", tracking=True,
    )
    tracking_id = fields.Many2one(
        comodel_name='skybox.tracking', string="Tracking sequence",
        tracking=True, ondelete='set null',
        help="Tracking Odoo a l'origine de cette prealerte.",
    )
    skybill = fields.Char(string="Skybill", tracking=True)
    partner_id = fields.Many2one(
        comodel_name='res.partner', string="Client", tracking=True,
        help="Client rattache a la prealerte (cree/mis a jour depuis le "
             "backoffice).",
    )
    partner_ref = fields.Char(
        related='partner_id.ref', string="Reference client", readonly=True,
    )
    partner_email = fields.Char(
        related='partner_id.email', string="Email client", readonly=True,
    )
    partner_phone = fields.Char(
        related='partner_id.phone', string="Telephone client", readonly=True,
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency', string="Devise",
        default=lambda self: self.env.company.currency_id,
    )
    declared_value = fields.Monetary(
        string="Valeur declaree", currency_field='currency_id', tracking=True,
    )
    line_total = fields.Monetary(
        string="Total des articles", currency_field='currency_id',
        compute='_compute_line_total', store=True,
    )
    delivery_date = fields.Date(string="Date estimee", tracking=True)
    notes = fields.Text(string="Notes")

    # --- Articles declares ---
    line_ids = fields.One2many(
        comodel_name='skybox.prealerte.line',
        inverse_name='prealerte_id', string="Articles declares",
    )

    # --- Facture ---
    invoice_file = fields.Binary(string="Facture", attachment=True)
    invoice_filename = fields.Char(string="Nom du fichier facture")

    # --- Statut ---
    state = fields.Selection(
        selection=[
            ('pending', "Pending"),
            ('received', "Received"),
        ],
        string="Statut de la Prealerte", default='pending',
        required=True, tracking=True,
        group_expand='_expand_states',
    )

    @api.model
    def _expand_states(self, states, domain):
        return [key for key, _ in self._fields['state'].selection]

    # --- Matching avec le colis recu ---
    matched_package_ref = fields.Char(string="Reference du colis recu")
    matched_weight = fields.Float(string="Poids constate (kg)")
    matched_date = fields.Date(string="Date de reception")
    match_notes = fields.Text(string="Notes de rapprochement")

    @api.model
    def _create_from_backoffice(self, data):
        """Cree une prealerte a partir du JSON renvoye par le backoffice.

        Ignore les doublons (meme numero de tracking deja present).
        """
        tracking = data.get('trackingNumber')
        if not tracking:
            return self.browse()

        suite = data.get('suiteNumber')

        # Tracking Odoo correspondant.
        track_rec = self.env['skybox.tracking'].search(
            [('tracking_number', '=ilike', tracking)], limit=1,
        )
        # Renseigne la suite sur le tracking s'il est vide.
        if suite and track_rec and not track_rec.suite_number:
            track_rec.suite_number = suite

        # Anti-doublon.
        existing = self.search(
            [('tracking_number', '=ilike', tracking)], limit=1,
        )
        if existing:
            if track_rec and not existing.tracking_id:
                existing.tracking_id = track_rec
            return existing

        # Devise (a partir du premier article, sinon devise societe).
        currency = self.env.company.currency_id
        items = data.get('items') or []
        if items and items[0].get('currency'):
            found = self.env['res.currency'].with_context(
                active_test=False,
            ).search([('name', '=', items[0]['currency'])], limit=1)
            if found:
                currency = found

        line_vals = [(0, 0, {
            'description': item.get('description') or '/',
            'quantity': item.get('quantity') or 0.0,
            'unit_value': item.get('unitValue') or 0.0,
            'hs_code': item.get('hsCode'),
            'fragile': bool(item.get('fragile')),
        }) for item in items]

        # Valeur declaree = somme des articles.
        declared_value = sum(
            (item.get('quantity') or 0.0) * (item.get('unitValue') or 0.0)
            for item in items
        )

        # Infos complementaires du colis regroupees dans les notes.
        note_lines = []
        for label, field in [
            ("Entrepot", 'warehouseCode'),
            ("Reference session", 'sessionReference'),
            ("Poids (kg)", 'weightKg'),
            ("Longueur (in)", 'lengthInches'),
            ("Largeur (in)", 'widthInches'),
            ("Hauteur (in)", 'heightInches'),
            ("Etat du colis", 'packageCondition'),
            ("Emplacement temporaire", 'temporaryLocation'),
        ]:
            value = data.get(field)
            if value not in (None, ''):
                note_lines.append("%s: %s" % (label, value))

        return self.create({
            'tracking_number': tracking,
            'suite_number': suite,
            'tracking_id': track_rec.id if track_rec else False,
            'matched_package_ref': data.get('warehouseCode'),
            'matched_weight': data.get('weightKg') or 0.0,
            'declared_value': declared_value,
            'currency_id': currency.id,
            'notes': "\n".join(note_lines),
            'line_ids': line_vals,
        })

    @api.model
    def _find_or_create_partner(self, customer):
        """Trouve le client (par customerId puis email) et met a jour ses infos,
        sinon le cree. Retourne le res.partner (ou vide)."""
        if not customer:
            return self.env['res.partner']
        Partner = self.env['res.partner']
        customer_id = customer.get('customerId')
        email = customer.get('email')
        name = ("%s %s" % (customer.get('firstName') or '',
                           customer.get('lastName') or '')).strip()

        partner = Partner.browse()
        if customer_id:
            partner = Partner.search([('ref', '=', customer_id)], limit=1)
        if not partner and email:
            partner = Partner.search([('email', '=', email)], limit=1)

        vals = {
            'name': name or email or customer_id,
            'email': email,
            'phone': customer.get('phone'),
            'ref': customer_id,
        }
        # N'ecrase pas les champs avec des valeurs vides.
        vals = {k: v for k, v in vals.items() if v}

        if partner:
            partner.write(vals)
        elif vals.get('name'):
            partner = Partner.create(vals)
        return partner

    @api.model
    def _create_from_check_result(self, result):
        """Cree une prealerte a partir d'un element 'results' de l'endpoint
        de check du backoffice (structure imbriquee preAlert/suite/customer).

        Ne cree rien si aucune preAlert n'est presente ou si le tracking
        existe deja.
        """
        tracking = result.get('trackingNumber')
        pre_alert = result.get('preAlert') or {}
        if not tracking or not pre_alert:
            return self.browse()

        suite = (result.get('suite') or {}).get('suiteNumber')

        # Tracking Odoo correspondant + remplissage de la suite si vide.
        track_rec = self.env['skybox.tracking'].search(
            [('tracking_number', '=ilike', tracking)], limit=1,
        )
        if suite and track_rec and not track_rec.suite_number:
            track_rec.suite_number = suite

        # Client : trouve/cree/mets a jour le partenaire.
        partner = self._find_or_create_partner(result.get('customer') or {})

        # Anti-doublon.
        existing = self.search(
            [('tracking_number', '=ilike', tracking)], limit=1,
        )
        if existing:
            if track_rec and not existing.tracking_id:
                existing.tracking_id = track_rec
            if partner and not existing.partner_id:
                existing.partner_id = partner
            return existing

        # Devise.
        currency = self.env.company.currency_id
        if pre_alert.get('currency'):
            found = self.env['res.currency'].with_context(
                active_test=False,
            ).search([('name', '=', pre_alert['currency'])], limit=1)
            if found:
                currency = found

        # Transporteur (cree si absent).
        courier_id = False
        if pre_alert.get('courier'):
            Courier = self.env['skybox.courier']
            courier = Courier.search(
                [('name', '=ilike', pre_alert['courier'])], limit=1,
            )
            if not courier:
                courier = Courier.create({'name': pre_alert['courier']})
            courier_id = courier.id

        # Articles.
        line_vals = [(0, 0, {
            'description': item.get('description') or '/',
            'quantity': item.get('quantity') or 0.0,
            'unit_value': item.get('unitValue') or 0.0,
            'hs_code': item.get('hsCode'),
            'fragile': bool(item.get('fragile')),
        }) for item in (pre_alert.get('items') or [])]

        # Notes : skybill, statut, client.
        customer = result.get('customer') or {}
        note_lines = []
        if result.get('skybillNumber'):
            note_lines.append("Skybill: %s" % result['skybillNumber'])
        if result.get('status'):
            note_lines.append("Statut backoffice: %s" % result['status'])
        if result.get('message'):
            note_lines.append("Message: %s" % result['message'])
        if customer:
            full_name = ("%s %s" % (customer.get('firstName') or '',
                                    customer.get('lastName') or '')).strip()
            if full_name:
                note_lines.append("Client: %s" % full_name)
            if customer.get('email'):
                note_lines.append("Email: %s" % customer['email'])
            if customer.get('phone'):
                note_lines.append("Tel: %s" % customer['phone'])

        # Date de reception = date de creation du tracking dans Odoo.
        matched_date = False
        if track_rec and track_rec.create_date:
            matched_date = track_rec.create_date.date()

        return self.create({
            'tracking_number': tracking,
            'suite_number': suite,
            'tracking_id': track_rec.id if track_rec else False,
            'partner_id': partner.id if partner else False,
            'courier_id': courier_id,
            'skybill': result.get('skybillNumber'),
            'matched_package_ref': result.get('skybillNumber'),
            'matched_date': matched_date,
            'declared_value': pre_alert.get('declaredValue') or 0.0,
            'currency_id': currency.id,
            'notes': "\n".join(note_lines),
            'line_ids': line_vals,
        })

    @api.depends('line_ids.subtotal')
    def _compute_line_total(self):
        for rec in self:
            rec.line_total = sum(rec.line_ids.mapped('subtotal'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == "New":
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'skybox.prealerte') or "New"
        return super().create(vals_list)

    def action_receive(self):
        self.write({'state': 'received'})

    def action_reset_pending(self):
        self.write({'state': 'pending'})


class SkyboxPrealerteLine(models.Model):
    _name = 'skybox.prealerte.line'
    _description = "Article declare"

    prealerte_id = fields.Many2one(
        comodel_name='skybox.prealerte', string="Prealerte",
        required=True, ondelete='cascade',
    )
    currency_id = fields.Many2one(
        related='prealerte_id.currency_id', string="Devise", store=True,
    )
    description = fields.Char(string="Description", required=True)
    quantity = fields.Float(string="Quantite", default=1.0)
    unit_value = fields.Monetary(
        string="Valeur unitaire", currency_field='currency_id',
    )
    subtotal = fields.Monetary(
        string="Sous-total", currency_field='currency_id',
        compute='_compute_subtotal', store=True,
    )
    hs_code = fields.Char(string="Code HS")
    category = fields.Selection(
        selection=[
            ('electronics', "Electronique"),
            ('clothing', "Vetements"),
            ('documents', "Documents"),
            ('cosmetics', "Cosmetiques"),
            ('food', "Alimentaire"),
            ('other', "Autre"),
        ],
        string="Categorie",
    )
    fragile = fields.Boolean(string="Indicateur Fragile")
    notes = fields.Char(string="Notes")

    @api.depends('quantity', 'unit_value')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.unit_value
