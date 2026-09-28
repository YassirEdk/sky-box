# -*- coding: utf-8 -*-
# Script de configuration Inventaire (Odoo 18)
# Lancé via odoo shell -> voir setup_inventory.sh
# Idempotent : relançable sans effet de bord.

# ======= PARAMETRES A ADAPTER =======
WAREHOUSE_NAME = "Entrepot Principal"
WAREHOUSE_CODE = "WH"          # 5 caracteres max
RECEPTION_STEPS = "two_steps"  # one_step | two_steps | three_steps
DELIVERY_STEPS = "pick_ship"   # ship_only | pick_ship | pick_pack_ship
# ====================================

def run(env):
    # 1) Activer les options generales (routes multi-etapes + emplacements)
    #    On rend ces groupes impliques par le groupe "Utilisateur interne"
    #    -> actif pour tous les users actuels ET futurs. (4, id) = idempotent.
    group_user = env.ref('base.group_user')
    for xmlid in ('stock.group_stock_multi_locations',
                  'stock.group_adv_location'):
        grp = env.ref(xmlid, raise_if_not_found=False)
        if grp:
            group_user.sudo().write({'implied_ids': [(4, grp.id)]})
            # propage aux utilisateurs internes existants
            grp.sudo().write({'users': [(4, uid) for uid in group_user.users.ids]})
    print(">> Options Emplacements + Routes multi-etapes activees")

    # 2) Creer / recuperer l'entrepot
    Warehouse = env['stock.warehouse']
    company = env.company
    wh = Warehouse.search([('name', '=', WAREHOUSE_NAME),
                           ('company_id', '=', company.id)], limit=1)
    if not wh:
        wh = Warehouse.search([('code', '=', WAREHOUSE_CODE),
                               ('company_id', '=', company.id)], limit=1)
    if wh:
        wh.write({
            'name': WAREHOUSE_NAME,
            'code': WAREHOUSE_CODE,
            'reception_steps': RECEPTION_STEPS,
            'delivery_steps': DELIVERY_STEPS,
        })
        print(">> Entrepot existant mis a jour : %s (%s)" % (wh.name, wh.code))
    else:
        wh = Warehouse.create({
            'name': WAREHOUSE_NAME,
            'code': WAREHOUSE_CODE,
            'reception_steps': RECEPTION_STEPS,
            'delivery_steps': DELIVERY_STEPS,
            'company_id': company.id,
        })
        print(">> Entrepot cree : %s (%s)" % (wh.name, wh.code))

    # 3) Recap des types d'operations generes automatiquement
    print(">> Reception : %s | Livraison : %s" %
          (wh.reception_steps, wh.delivery_steps))
    print(">> Types d'operations disponibles :")
    for pt in env['stock.picking.type'].search([('warehouse_id', '=', wh.id)]):
        print("   - %s (%s)" % (pt.name, pt.code))

    env.cr.commit()
    print(">> Configuration terminee et validee (commit).")

run(env)  # 'env' est fourni par odoo shell
