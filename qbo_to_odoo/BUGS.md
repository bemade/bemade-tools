# QBO to Odoo — Known Bugs & Audit Findings

Identified via full ETL pipeline audit on 2026-02-21.
Severity: **CRITICAL** → **HIGH** → **MEDIUM** → **LOW**

---

## CRITICAL

### BUG-1: Payment JEs always posted to general journal, never bank journal

**File:** `models/pipelines/payment_etl.py:342–350`

`_get_bank_account_from_payment` correctly resolves the *account* (bank, credit
card, or Undeposited Funds) but always books the journal entry to the first
`type='general'` journal found, regardless of which account was resolved.

```python
journal = ctx.env["account.journal"].search(
    [("type", "=", "general"), ("company_id", "=", company.id)],
    limit=1,
)
```

**Impact:** Bank reconciliation is entirely broken. Odoo matches bank statement
lines against entries in the *same* journal. Payments posted to the general journal
will never appear as candidates for reconciliation against bank statements.

**Fix direction:** Look up the journal whose `default_account_id` (or
`payment_debit_account_id` / `payment_credit_account_id`) matches the resolved
bank account, falling back to the general journal only for non-bank accounts such
as Undeposited Funds.

- [x] Implemented (untested) — `payment_etl.py`: search by `default_account_id`
  first, fall back to general journal.

---

### BUG-2: Multi-currency transactions land on the wrong AR/AP account

**Files:**
- `models/qbo_connection.py:711–746` (`_find_most_used_account`)
- `models/pipelines/invoice_etl.py:310–318` (AR override in load)
- `models/pipelines/bill_etl.py:338–346` (AP override in load)

> **Confirmed against the live DB on 2026-02-21.** `IrDefault.set()` works
> correctly in Odoo 19 — the `ir_default` table has the expected AR/AP records and
> partners (all with NULL property columns) correctly inherit them. The real bug is
> multi-currency account assignment, described below.

#### What the DB shows

The company has two AR accounts and two AP accounts in QBO, one per currency:

| Account | Type | Currency | QBO ID |
|---|---|---|---|
| Accounts Receivable CAD | AR | CAD | 118 |
| Accounts Receivable - USD | AR | USD | 138 |
| Accounts Payable - CAD | AP | CAD | 109 |
| Accounts Payable - USD | AP | USD | 137 |

After import, the actual counterpart accounts on posted moves are:

| Move type | Currency | Account used | Correct? |
|---|---|---|---|
| out\_invoice | USD (1126 moves) | AR USD (qbo\_id=138) | ✓ |
| out\_invoice | CAD (52 moves) | AR USD (qbo\_id=138) | ✗ should be AR CAD |
| in\_invoice | CAD (3617 moves) | AP CAD (qbo\_id=109) | ✓ |
| in\_invoice | USD (1181 moves) | AP CAD (qbo\_id=109) | ✗ should be AP USD |

#### Root cause — two bugs in sequence

**Bug 2a — `_find_most_used_account` picks a single global default based on
transaction count.**

```python
# qbo_connection.py:726
entities = client.query(entity_type, max_results=1000)
account_counts = Counter()
for entity in entities:
    account_ref = entity.get(account_ref_field, {})
    if account_ref and account_ref.get("value"):
        account_counts[int(account_ref["value"])] += 1
most_common = account_counts.most_common(1)[0]
```

USD invoices outnumber CAD invoices 1126 to 52, so AR-USD wins and becomes the
global `ir_default`. CAD invoices should use AR-CAD instead, but the global
default overrides that. The same asymmetry applies to bills in the opposite
direction (CAD bills dominate so AP-CAD is the global default, wrongly applied to
USD bills).

**Bug 2b — the per-transaction `ARAccountRef`/`APAccountRef` override fires before
`action_post()` and is immediately undone.**

The load step for both invoices and bills follows this sequence:

```python
move = ctx.env["account.move"].create(vals)   # counterpart uses ir_default account
recv_line = move.line_ids.filtered(...)
recv_line.account_id = ar_account_id          # set correct per-invoice account
move.action_post()                            # _recompute_payment_terms_lines()
                                              # resets account back to ir_default ← BUG
```

`action_post()` in Odoo 19 calls `_sync_dynamic_lines()` →
`_recompute_payment_terms_lines()`, which regenerates the receivable/payable
counterpart from `partner.property_account_receivable_id`. Because all partners
have a NULL property column and inherit the global `ir_default` (AR-USD), every
invoice's counterpart is reset to AR-USD after posting regardless of the
pre-post override. This is why the previous fix attempt "had no impact."

#### Impact

52 CAD invoices have their AR on the USD receivable account. 1181 USD bills have
their AP on the CAD payable account. This means:
- The balance sheet AR/AP balances are split across the wrong accounts.
- Any payment against a CAD invoice will also use the global IR default (AR-USD)
  for its receivable line, so the accounts DO match between invoice and payment —
  reconciliation succeeds. But both are on the wrong account, making AR/AP
  sub-ledger reports wrong.

#### Fix direction

The pre-`action_post()` override approach is fundamentally broken. The fix must
apply the correct account **after** posting, when `_recompute_payment_terms_lines`
will not run again.

**Option A (recommended for ETL):** Temporarily set
`partner.property_account_receivable_id` (or payable) on the partner to the
per-invoice target account before creating + posting the move, then clear it.
Since `action_post()` re-reads the partner property to set the counterpart account,
it will naturally use the correct account. After posting, clear the partner
override back to `False` so future transactions inherit the `ir_default` again.

**Option B:** After `action_post()`, write the correct `account_id` directly
to the counterpart line via SQL, bypassing the ORM entirely. Acceptable for a
one-shot ETL on a fresh DB.

The `_find_most_used_account` heuristic is fine as a global fallback for the
majority case; the real fix is making the per-transaction override survive posting.

- [x] Implemented (untested) — new `qbo.partner.account.linker` pipeline sets
  `property_account_receivable_id` / `property_account_payable_id` per partner
  from QBO transaction history before invoices/bills are imported. Per-transaction
  override blocks removed from `invoice_etl.py` and `bill_etl.py`.

---

## HIGH

### BUG-3 (future idempotency): Account ETL archives all non-QBO accounts on every run

**File:** `models/pipelines/account_etl.py:105–143`

> **Current status: not a production issue.** The pipeline is only run on a fresh
> database before any user interaction, so there are no Odoo-created accounts to
> lose. Documented here for future reference if the pipeline ever needs to be
> idempotent (e.g. incremental syncs or re-runs after initial import).

`transform_accounts` unconditionally archives every `account.account` record that
does not have a `qbo_id`:

```python
odoo_default_accounts = ctx.env["account.account"].search(
    [("company_ids", "in", [company.id]), ("qbo_id", "=", False)]
)
odoo_default_accounts.write({"active": False})
```

This runs on every pipeline execution, including re-runs where `new_accounts` is
empty. Any account Odoo creates internally (tax settlement accounts, accounts added
by other modules, manually created accounts) would be archived on the next run.

**Fix direction:** Gate the archiving behind an explicit flag (e.g. a context key
`force_archive_defaults=True`) so it only fires during intentional initial import,
not on incremental re-runs.

- [ ] Fixed

---

### BUG-4: Invoice lines never carry an explicit `account_id`

**File:** `models/pipelines/invoice_etl.py:282–293`

`_transform_invoice_line` builds line values with `name`, `quantity`, `price_unit`,
and optionally `product_id` and `tax_ids`, but never sets `account_id`:

```python
line_vals = {
    "name": ...,
    "quantity": qty,
    "price_unit": unit_price,
}
if product_id:
    line_vals["product_id"] = product_id
```

Odoo resolves the account as: product income account → product category income
account → sale journal `default_account_id`. The journal default is set by the
account ETL to whichever QBO income account has the lowest code (see BUG-5). Any
invoice line where the product was not resolved, or where the product lacks
`property_account_income_id`, silently lands on that lowest-code account.

Note: item-based expense lines in `bill_etl.py` have the same gap. Account-based
bill lines *do* set `account_id` correctly via `AccountBasedExpenseLineDetail`.

**Fix direction:** For `SalesItemLineDetail` lines, resolve the income account from
`product.property_account_income_id` (or category fallback) explicitly in the
transform step and include it in `line_vals`. This mirrors what the
`account_etl.py` already does and removes reliance on Odoo's default account
cascade.

- [x] Implemented (untested) — `invoice_etl.py`: builds `product_income_map` from
  `product_template.property_account_income_id` joined via `product_tmpl_id`;
  passes to `_transform_invoice_line` and sets `account_id` when found.
  `bill_etl.py`: same for `property_account_expense_id` on item-based lines.

---

### BUG-5: Journal and product category account defaults set to lowest-code account

**File:** `models/pipelines/account_etl.py:331–374`

`_set_account_defaults` picks default income/expense accounts by sorting all QBO
accounts of that type by code and taking the first one:

```python
income_accounts = qbo_accounts.filtered(
    lambda a: a.account_type == "income"
).sorted("code")
income_account = income_accounts[0]  # arbitrary lowest-code winner
sale_journals.write({"default_account_id": income_account.id})
IrDefault.set("product.category", "property_account_income_categ_id", income_account.id)
```

In a typical QBO chart of accounts with `4000 General Revenue`, `4100 Service
Revenue`, `4200 Product Sales`, etc., this picks `4000` as the journal default and
product category default — a blanket revenue bucket that is likely wrong for most
line items.

Compounding the problem: `_set_account_defaults` is called on every pipeline run
(see `load_accounts`), so any manual corrections to journal defaults are
overwritten on the next sync.

**Fix direction:** Use QBO's `AccountSubType` (e.g. `SalesOfProductIncome`) for
defaults rather than lowest-code sorting, or remove the journal default assignment
entirely and rely on per-product income accounts (which are imported correctly from
`IncomeAccountRef`).

- [ ] Fixed

---

## MEDIUM

### BUG-6: Bill and Invoice `move_type` inferred from amount sign instead of QBO entity type

**Files:** `models/pipelines/bill_etl.py:167–171`,
`models/pipelines/invoice_etl.py:171–172`

```python
# bill_etl.py
computed_total = sum(price_unit * quantity for ...)
move_type = "in_refund" if computed_total < 0 else "in_invoice"

# invoice_etl.py
total_amt = float(inv.get("TotalAmt", 0) or 0)
move_type = "out_refund" if total_amt < 0 else "out_invoice"
```

QBO has dedicated entities for refunds (`VendorCredit`, `CreditMemo`) with their
own pipelines. A Bill or Invoice should always be `in_invoice` / `out_invoice`
regardless of line amounts. A bill with a negative correction line (discount,
adjustment) gets misclassified as `in_refund`.

**Fix direction:** Remove the amount-based inference. Bills are always `in_invoice`;
Invoices are always `out_invoice`. If QBO sends a negative total on these entity
types, log a warning rather than flipping the move type.

- [ ] Fixed

---

### BUG-7: SalesReceipt Undeposited Funds fallback uses hardcoded account code prefix

**File:** `models/pipelines/sales_receipt_etl.py:121–127`

```python
undeposited_funds = ctx.env["account.account"].search(
    [
        ("code", "=like", "1408%"),
        ("company_ids", "in", [company.id]),
    ],
    limit=1,
)
```

This hardcodes the account code prefix `1408`. The `deposit_etl.py` and
`payment_etl.py` both find the same account by name (`("name", "ilike",
"Undeposited Funds")`). If the imported Undeposited Funds account has any other
code, this returns nothing and sales receipts without a `DepositToAccountRef` are
silently skipped rather than falling back correctly.

**Fix direction:** Use the name-based search consistent with the other pipelines.

- [ ] Fixed

---

### BUG-8: `company.env` used instead of `ctx.env` in deposit and sales receipt pipelines

**Files:** `models/pipelines/deposit_etl.py:155, 270–276`,
`models/pipelines/sales_receipt_etl.py:174, 307`

```python
currency = company.env["res.currency"].search(...)
uf_account = company.env["account.account"].search(...)
product = company.env["product.product"].browse(product_id)
```

All other pipelines consistently use `ctx.env`. `company.env` accesses the
environment attached to the `res.company` recordset, which may carry a different
user context or sudo level than the ETL session environment. This is inconsistent
and can produce unexpected permission or context behaviour.

**Fix direction:** Replace all `company.env[...]` calls in these two files with
`ctx.env[...]`.

- [ ] Fixed

---

## LOW

### BUG-9: QBO NonInventory items mapped to storable Odoo products

**File:** `models/pipelines/product_etl.py:98–105`

```python
if item_type == "Service":
    product_type = "service"
    is_storable = False
else:
    product_type = "consu"
    is_storable = True   # ← applies to NonInventory and Inventory alike
```

In Odoo 17+, `type='consu'` + `is_storable=True` creates a fully stock-tracked
product with valuation. QBO `Inventory` → storable is correct. QBO `NonInventory`
items (consumables with no stock tracking) should map to `type='consu'`,
`is_storable=False`.

**Fix direction:** Add an explicit branch for `item_type == "NonInventory"` that
sets `is_storable=False`.

- [ ] Fixed
