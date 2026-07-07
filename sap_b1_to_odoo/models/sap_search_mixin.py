#
#    Bemade Inc.
#
#    Copyright (C) 2023-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
#    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#    DEALINGS IN THE SOFTWARE.
#
from odoo import api, models
from odoo.fields import Domain

_SAP_POSITIVE_OPERATORS = ("=", "ilike", "=ilike", "like", "=like")


class SapSearchMixin(models.AbstractModel):
    """Reusable mixin that extends ``name_search`` to also match on a
    model's SAP-imported identifier field(s), additively.

    Models with no custom core ``name_search`` (e.g. ``sale.order``,
    ``purchase.order``, ``account.move``, ``mrp.production``,
    ``res.partner``) only need to add this mixin to ``_inherit`` and set
    ``_SAP_SEARCH_FIELDS``: the mixin's own ``name_search`` below is the
    outermost seam and wins.

    Models whose concrete class already defines a custom ``name_search``
    (e.g. ``product.template``, ``product.product``) must keep a thin
    def-level stub on that concrete class that calls
    ``super().name_search(...)`` and then delegates to
    ``self._sap_augment_name_search(...)`` — see
    ``models/product_product.py``.
    """

    _name = "sap.search.mixin"
    _description = "SAP Identifier Search Mixin"

    # Models set this to the list of SAP identifier field names to search on.
    _SAP_SEARCH_FIELDS = []

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        result = super().name_search(name, domain, operator, limit)
        return self._sap_augment_name_search(result, name, domain, operator, limit)

    def _sap_augment_name_search(self, result, name, domain, operator, limit):
        """Append records matching any of ``_SAP_SEARCH_FIELDS`` to ``result``.

        Additive and deduped: existing ``result`` entries are preserved as-is
        and never removed; SAP matches not already present are appended,
        labeled with a ``— SAP #<value>`` suffix so they're distinguishable
        from name matches. Integer fields only match on an all-digit search
        term (using ``=``, since ``ilike`` on an integer column is invalid);
        Char fields use the caller's ``operator``.
        """
        if not name or operator not in _SAP_POSITIVE_OPERATORS:
            return result
        existing_ids = {r[0] for r in result}
        base_domain = Domain(domain) if domain else Domain.TRUE
        for fname in self._SAP_SEARCH_FIELDS:
            field = self._fields.get(fname)
            if field is None:
                continue
            if field.type == "integer":
                stripped = name.strip()
                if not stripped.isdigit():
                    continue
                sap_domain = base_domain & Domain(fname, "=", int(stripped))
            else:
                sap_domain = base_domain & Domain(fname, operator, name)
            records = self.search_fetch(sap_domain, ["display_name", fname], limit=limit)
            for rec in records:
                if rec.id in existing_ids:
                    continue
                result.append((rec.id, f"{rec.display_name} — SAP #{rec[fname]}"))
                existing_ids.add(rec.id)
        if limit:
            result = result[:limit]
        return result
