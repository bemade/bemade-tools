def post_init_hook(env):
    """Post-install hook. No-op.

    SAP import is triggered manually via odoo-bin shell after all modules
    are installed, ensuring _inherit overrides from dependent modules are
    loaded before the import runs.
    """
    pass
