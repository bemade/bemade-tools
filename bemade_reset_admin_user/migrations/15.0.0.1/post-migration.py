def migrate(cr, version):
    if not version:
        return

    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Check if the eq_merge_duplicate_data module is installed
    if not env['ir.module.module'].search([('name', '=', 'eq_merge_duplicate_data'), ('state', '=', 'installed')]):
        return

    user_model = env['res.users']

    partner_model = env['res.partner']

    # Create a new user with the name "New Denis" and email "newdenis@example.com"
    new_partner = partner_model.create({
        'name': 'New Denis',
        'email': 'newdenis@example.com'
    })

    new_user = user_model.create({
        "name": "newdenis",
        "login": "newdenis",
        "email": "newdenis",
        "notification_type": "inbox",
    })

    # Use the wizard from the eq_merge_duplicate_data module
    merge_wizard = env['wizard.merge.data'].create({
        'duplicate_rec_id': 'res.users,2',
        'original_rec_id': f'res.users,{new_user.id}',
        'take_action': 'none'  # Or 'archived'
    })

    # Perform the merge
    merge_wizard.action_merge_duplicate_data()

    # Update Email and Email Signature of User ID 21
    user_id_2 = user_model.browse(2)

    new_user.write({
        'email': user_id_2.email,
        'signature': user_id_2.signature,
        'partner_id': user_id_2.partner_id.id,
        'password': user_id_2.password
    })

    # Change Partner ID of User ID 2 to 5021064
    user_id_2.write({'partner_id': 5021064})

    # Change Name and Email of User ID 2
    user_id_2.write({
        'name': 'Administrator',
        'email': 'admin',
        'login': 'admin'
    })

    # Update Partner Information for User ID 2
    partner_admin = partner_model.browse(5021064)

    partner_admin.write({
        'name': 'Administrator',
        'email': 'admin'
    })

    new_user.write({
        'name': 'Denis Durepos',
        'email': 'ddurepos@durpro.com',
        'login': 'ddurepos@durpro.com'
    })

    new_partner.unlink()