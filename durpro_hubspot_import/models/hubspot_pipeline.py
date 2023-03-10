from odoo import models, fields, api, _


class HubSpotPipeline(models.Model):
    _name = "durpro_hubspot_import.hubspot_pipeline"
    _inherit = "durpro_hubspot_import.hubspot_model"
    _description = 'Carries information imported from Hubspot Pipelines'

    label = fields.Char(string="Label", )
    display_order = fields.Integer(string="Display Order", )
    hs_archived = fields.Boolean(string="HS archived", )
    stages = fields.One2many("durpro_hubspot_import.hubspot_pipeline_stage", "hs_pipeline_id", string="Stages")
    hs_pipeline_id = fields.Char(string="HS Pipeline Stage", )

    helpdesk_team_id = fields.Many2one("helpdesk.team", string="Associated Helpdesk Team")

    @api.model
    def import_all(self):
        """
        Overwrite the base model here since there is no pipeline object available from crm.objects.get_all(..).
        We also don't have a properties endpoint for pipelines.

        We are only doing ticket pipelines, not deals.

        Results structure at https://developers.hubspot.com/docs/api/crm/pipelines under "Retrieve all pipelines"
        """
        objects_fetched = self._api_client().crm.pipelines.pipelines_api.get_all(object_type="tickets").results
        for obj in objects_fetched:
            obj = obj.to_dict()
            pipeline_vars = {
                'label': obj['label'],
                'display_order': obj['display_order'],
                'hs_archived': obj['archived'],
                'hs_pipeline_id': obj['id'],
            }
            pipeline = self.env[self._name].create(pipeline_vars)
            if 'stages' in obj:
                stages = []
                for stage in obj['stages']:
                    stages.append({
                        'label': stage['label'],
                        'hs_stage_id': stage['id'],
                        'hs_pipeline_id': pipeline.id,
                        'display_order': stage['display_order'],
                        'hs_archived': stage['archived'],
                        'ticket_state': stage['metadata']['ticketState'],
                    })
                recs = self.env['durpro_hubspot_import.hubspot_pipeline_stage'].create(stages)
                pipeline.stages = recs

    def _extract_hs_fields(self):
        """ In this case we do it directly in import_all due to the different structure"""
        pass

    def name_get(self):
        result = []
        for rec in self:
            result.append((rec.id, rec.label))
        return result


class HubSpotPipelineStage(models.Model):
    _name = "durpro_hubspot_import.hubspot_pipeline_stage"
    _description = 'Carries information imported from Hubspot Pipeline Stages'

    label = fields.Char("HS Stage Label", )
    hs_stage_id = fields.Char("HS Stage ID", )
    hs_pipeline_id = fields.Many2one("durpro_hubspot_import.hubspot_pipeline", string="Hubspot Pipeline", )
    display_order = fields.Integer(string="HS Display Order", )
    hs_archived = fields.Boolean(string="HS is archived", default=False)
    ticket_state = fields.Char(string="HS Ticket State")

    helpdesk_stage = fields.Many2one("helpdesk.stage", string="Odoo Helpdesk Stage")

    @api.depends("label")
    def name_get(self):
        result = []
        for rec in self:
            result.append((rec.id, rec.label))
        return result
