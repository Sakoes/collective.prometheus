### What was happening
`/@@metrics` is published at the **Zope app root**, not inside the Plone site. So `self.context` in the view is the root `Application` object, which has no `portal_catalog` / `acl_users`. `getToolByName(root, 'portal_catalog')` raises `AttributeError: portal_catalog`, and the same thing would happen for `_collect_users`.

The Zope-level metrics (process, ZODB, connections) work because they don't need a Plone site.

### Fix applied
Patched `_PloneCollector.__init__` in `devel/collective.prometheus/src/collective/prometheus/browser.py` to resolve a usable site context: if the given context has no `portal_catalog`, it scans child items at the app root and picks the first one that does (your `at_plone`). Falls back to original context otherwise.

```python
def __init__(self, request, context):
    self._request = request
    self._context = self._resolve_site(context)

@staticmethod
def _resolve_site(context):
    if getToolByName(context, 'portal_catalog', None) is not None:
        return context
    try:
        for _id, obj in context.objectItems():
            if getToolByName(obj, 'portal_catalog', None) is not None:
                return obj
    except Exception:
        pass
    return context
```

### Notes / alternatives to consider
- **Multi-site case:** if you ever host more than one Plone site in the same Zope, this picks "the first one." A cleaner long-term fix is to either (a) iterate all sites and label metrics with a `site_id`, or (b) require scraping `/<site_id>/@@metrics` and skip Plone collectors at the root.
- **Permissions:** at the app root the view runs without a Plone site's `unrestrictedTraverse` semantics — make sure whoever scrapes `/` has rights, or move the endpoint to `/at_plone/@@metrics` and just delete the root-resolution hack.
- The file was copied straight into the running container; on next `compose up --build` it'll be baked in via `COPY . .` since you edited the source on the host.
