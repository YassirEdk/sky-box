# -*- coding: utf-8 -*-
{
    'name': "Skybox",
    'summary': "Affecter chaque utilisateur a un entrepot specifique",
    'description': """
Ajoute un champ Entrepot (warehouse_id) sur le modele res.users et permet
de selectionner l'entrepot associe a chaque utilisateur depuis la liste des
entrepots disponibles dans le module Inventory.
""",
    'author': "Yassir",
    'category': "Inventory",
    'version': "18.0.1.0.0",
    'depends': ['stock', 'hr', 'mail'],
    'data': [
        'security/skybox_groups.xml',
        'security/ir.model.access.csv',
        'data/prealerte_sequence.xml',
        'data/tracking_cron.xml',
        'data/demo_locations_bags.xml',
        'data/location_barcode_init.xml',
        'views/res_users_views.xml',
        'views/hr_employee_views.xml',
        'views/courier_views.xml',
        'views/api_key_views.xml',
        'views/warehouse_views.xml',
        'views/stock_location_views.xml',
        'views/bag_views.xml',
        'views/tracking_views.xml',
        'views/check_tn_wizard_views.xml',
        'views/prealerte_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'skybox/static/src/css/skybox.css',
            'skybox/static/src/js/password_eye_field.js',
            'skybox/static/src/xml/password_eye_field.xml',
        ],
    },
    'installable': True,
    'application': True,
}
