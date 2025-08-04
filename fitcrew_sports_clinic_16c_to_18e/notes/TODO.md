Fix duplicates after migration:
2025-08-03 21:15:07,391 21753 WARNING migration_test odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners: 🔄 MERGED/DEDUPLICATED partner ID 1074: 'Sam Pinard' (email: None) - merged with existing partner 
2025-08-03 21:15:08,298 21753 WARNING migration_test odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners: 🔄 MERGED/DEDUPLICATED partner ID 1194: 'Guillaume Pleau' (email: None) - merged with existing partner
2025-08-01-medsportsuroit-prod=> select id,partner_id,last_name,first_name from sports_patient where partner_id in (select id from res_partner where name ilike 'Sam Pinard');
 id  | partner_id | last_name | first_name
-----+------------+-----------+------------
 631 |        725 | Pinard    | Sam
 692 |       1074 | Pinard    | Sam
(2 rows)

2025-08-01-medsportsuroit-prod=> select id,partner_id,last_name,first_name from sports_patient where partner_id in (select id from res_partner where name ilike 'Guillaume Pleau');
 id  | partner_id | last_name | first_name
-----+------------+-----------+------------
 794 |       1194 | Pleau     | Guillaume
 361 |        436 | Pleau     | Guillaume
(2 rows)

