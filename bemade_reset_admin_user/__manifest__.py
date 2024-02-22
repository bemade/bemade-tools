{
    'name': 'Reset Admin User',
    'version': '15.0.0.2',
    'summary': 'Module for Custom Migration for resetting admin using eq_merge_duplicate_data',
    'sequence': 10,
    'description': """
    This module is for custom migration for resetting admin using eq_merge_duplicate_data
    
    it will create new user and then merge it with user id 2 (Denis Durepos)
    Then set new user email and email signature to user id 2
    Change partner id of user id 2 to 5021064 (Administrator)
    Change name of user id 2 to Administrator
    Change email and login of user id 2 to admin
    
    End result is user id 2 will be the admin user and user id 21 will be Denis Durepos
    """,
    'category': 'Custom',
    'author': 'Bemade',
    'website': 'https://www.bemade.org',
    'depends': [
        'base',
        'mail',
        'eq_merge_duplicate_data',  # Dependency to your existing module
    ],
    'data': [
        # Your module data files
    ],
    'demo': [],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'OPL-1'
}
