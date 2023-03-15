# Associations

Associations are managed by the associations API which gives unidirectional associations between various CRM objects.
For our purposes, we are interested in associations involving tickets, contacts, companies, and engagements. The list
of associations and definition IDs is here: 
https://legacydocs.hubspot.com/docs/methods/crm-associations/crm-associations-overview

# Getting properties lists

curl --request GET --url 'https://api.hubapi.com/crm/v3/properties/<object-type>' \
--header 'authorization: Bearer <HS PRIVATE APP KEY HERE>' > <filepath.json>

Then, reformat the json file with Pycharm to make it readable with `cmd+option+L`: