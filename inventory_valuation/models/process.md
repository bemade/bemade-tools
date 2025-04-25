1. Find the broken stock valuation layers and their predecessors and set the broken
   SVL's value to the value of the predecessor. Update its account move to match.
2. For all valuation layers, recompute the value for layers after the 8th of april 2025.
    
    2.1 Order the layers by id (same as ordering by create date), grouped by product.
    2.2 With the layers in a queue, move through them in chronological order, updating
        the value of any outgoing layers according to FIFO principles. Pop exhausted
        layers from the queue.
    2.3 For any layer that has its value changed, update the account move linked to it.

3. Update the cost field for products based on the inventory valuation (check how Odoo 
   does it when in FIFO mode).
4. For all customer invoices posted after 2025-04-08, set them to draft and re-post them
   to recalculate CoGS. Make sure to re-reconcile payments.
5. Fix the accounts set on the different journal items:

    5.1 Journal entries tied to an outgoing SVL should credit stock (1300) and debit 
        delivered but not billed(1028).
    5.2 Journal entries tied to an incoming SVL should debit stock (1300) and credit 
        received but not billed (2028). Exception for the broken SVL items: they should
        debit stock (1300) but credit equity (3400).
    5.3 Journal items tied to an invoice or vendor bill (credits and refunds too) 
        dated before 2025-04-09 and affecting account 1028 or 2028 should affect account 
        1300 (stock) directly instead.