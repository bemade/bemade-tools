# Sage 50 Mapping

The Sage 50 source identifiers, carried on the Odoo records a take-on creates.
Nothing else: no models of its own, no executable logic, no external
dependencies.

## Why it is a separate module

`sage50_to_odoo` needs a MySQL driver and a live connection to the client's
Sage company file. Neither has any business on a production server once the
migration is done, so the pipeline module is installed on the dev box that
runs the migration and **uninstalled before the database is promoted**.

The identifiers, though, have to survive. They are what answers "where did
this account come from?" three years later, when the Sage file is a backup
nobody can open — and what makes a re-run of the importer update in place
rather than duplicate. So they live here, and this module stays installed.

> **Uninstall, don't just delete.** A module flagged installed in the database
> whose code is missing from the addons path breaks registry loading. Uninstall
> `sage50_to_odoo` and the client layer *before* promoting the database.
> Their pipelines are `AbstractModel`s with no tables; the only real table is
> `sage.database`, which holds connection settings you want gone anyway.
>
> Keep `etl_framework` installed. It has no external dependencies and it owns
> `etl.import.report` — the take-on's execution history, which is exactly what
> you want to read the day a balance looks wrong.

## What it adds

| Model | Field | Sage source |
|---|---|---|
| `account.account` | `sage_account_id` | `taccount.lId` |
| `res.partner` | `sage_customer_id` | `tcustomr.lId` |
| `res.partner` | `sage_vendor_id` | `tvendor.lId` |
| `product.template` | `sage_product_id` | `tinvent.lId` |
| `product.template` | `sage_unit` | `tinvent.sStockUnit` |
| `product.category` | `sage_income_account` | `taccount.lId` of the item's revenue account |
| `account.move` | `sage_doc_id` | `tcustr.lId` / `tventr.lId` |

Partners get two id fields rather than one because Sage keeps customers and
vendors in unrelated tables with independent id sequences, and a company that
is both has an id in each.

It also exposes `sage_doc_id` on `account.invoice.report` and ships a saved
search, **Invoiced + Sage history**, whose domain is
`state not in (draft, cancel) OR sage_doc_id is set`. Historical invoices are
imported and cancelled so they carry no accounting weight; that filter puts
their product and quantity lines back into sales analysis without also
surfacing invoices somebody cancelled by mistake.
