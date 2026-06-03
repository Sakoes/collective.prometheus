"""Prometheus exporter for Zope/Plone."""

import logging
import sys
import threading
import time

from prometheus_client import CollectorRegistry, PROCESS_COLLECTOR, PLATFORM_COLLECTOR
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.exposition import choose_encoder

from Products.Five.browser import BrowserView
from zExceptions import NotFound
from ZODB.ActivityMonitor import ActivityMonitor

try:
    import ZServer.PubCore
    Z_SERVER = True
except Exception:
    Z_SERVER = False

try:
    from Products.CMFCore.utils import getToolByName
    from Products.CMFPlone import __version__ as PLONE_VERSION
    PLONE_AVAILABLE = True
except ImportError:
    PLONE_AVAILABLE = False
    PLONE_VERSION = None

logger = logging.getLogger(__name__)


# Monotonic ZODB activity totals, keyed by db name. Survives across
# scrapes for the lifetime of the process; resets on restart (see
# HISTORY.txt 2.0.0 note). Guarded by `_ACTIVITY_LOCK` because Zope
# serves /@@metrics on multiple threads.
_ACTIVITY = {}            # dbname -> {'loads': int, 'stores': int, 'last_end': float}
_ACTIVITY_LOCK = threading.Lock()


def _gauge(name, help_text, labels=()):
    return GaugeMetricFamily(name, help_text, labels=list(labels))


def _counter(name, help_text, labels=()):
    return CounterMetricFamily(name, help_text, labels=list(labels))


class _ZopeCollector(object):
    """Per-scrape collector. One instance per /@@metrics request.

    Sub-collectors are isolated; one failure won't blank a scrape.
    """

    def __init__(self, request, context):
        self._request = request
        self._context = context

    def _safe(self, name, gen):
        try:
            yield from gen()
        except Exception:
            logger.exception("collector %s failed", name)

    def collect(self):
        sub_collectors = (
            ('zopethreads',     self._collect_threads,     Z_SERVER),
            ('zopecache',       self._collect_cache,       True),
            ('zodbactivity',    self._collect_activity,    True),
            ('zopeconnections', self._collect_connections, True),
        )
        for name, fn, enabled in sub_collectors:
            if enabled:
                yield from self._safe(name, fn)

    def _getdbs(self):
        fs = self._request.get('filestorage')
        db = self._context.unrestrictedTraverse('/Control_Panel/Database')
        if fs == '*':
            for n in db.getDatabaseNames():
                yield db[n], n
        elif fs:
            if fs not in db.getDatabaseNames():
                raise NotFound
            yield db[fs], fs
        else:
            yield db['main'], 'main'

    def _collect_threads(self):
        total = len(sys._current_frames())
        handle = getattr(ZServer.PubCore, '_handle', None)
        lists = ((), (), ())
        if handle is not None:
            lists = handle.__self__._lists
        # See ZRendezvous.__init__: (busy, request_queue, free)
        busy, queued, free = (len(l) for l in lists)
        for name, help_text, val in (
            ('zope_total_threads', 'Number of running Zope threads.', total),
            ('zope_free_threads', 'Idle Zope worker threads.', free),
            ('zope_busy_threads', 'Zope worker threads handling a request.', busy),
            ('zope_request_queue_length', 'Requests waiting for a worker.', queued),
        ):
            g = _gauge(name, help_text)
            g.add_metric([], val)
            yield g

    def _collect_cache(self):
        specs = [
            (_gauge('zope_total_objects', 'Number of objects in the ZODB.', ['db']),
             lambda db: db.database_size()),
            (_gauge('zope_cache_objects', 'Objects currently held in the ZODB cache.', ['db']),
             lambda db: db.cache_length()),
            (_gauge('zope_cache_size', 'Configured maximum objects per ZODB cache.', ['db']),
             lambda db: db.cache_size()),
        ]
        for db, name in self._getdbs():
            for fam, fn in specs:
                fam.add_metric([name], fn(db))
        for fam, _ in specs:
            yield fam

    def _collect_activity(self):
        conns = _gauge('zodb_connections', 'Open ZODB connections.', ['db'])
        loads = _counter('zodb_object_loads', 'Total ZODB object loads.', ['db'])
        stores = _counter('zodb_object_stores', 'Total ZODB object stores.', ['db'])

        for db, name in self._getdbs():
            zodb = db._getDB()
            if zodb.getActivityMonitor() is None:
                zodb.setActivityMonitor(ActivityMonitor())
            now = time.time()
            with _ACTIVITY_LOCK:
                # First scrape backfills 60s rather than asking "since epoch".
                state = _ACTIVITY.setdefault(
                    name, {'loads': 0, 'stores': 0, 'last_end': now - 60})
                data = zodb.getActivityMonitor().getActivityAnalysis(
                    start=state['last_end'], end=now, divisions=1)[0]
                state['loads'] += data['loads']
                state['stores'] += data['stores']
                state['last_end'] = now
                loads_total = state['loads']
                stores_total = state['stores']
            conns.add_metric([name], data['connections'])
            loads.add_metric([name], loads_total)
            stores.add_metric([name], stores_total)
        yield conns
        yield loads
        yield stores

    def _collect_connections(self):
        active = _gauge('zope_connection_active_objects',
                        'Active (non-ghost) objects in a ZODB connection cache.',
                        ['db', 'connection'])
        total = _gauge('zope_connection_total_objects',
                       'Total objects in a ZODB connection cache.',
                       ['db', 'connection'])
        for db, name in self._getdbs():
            details = sorted(db._p_jar.db().cacheDetailSize(),
                             key=lambda m: m['connection'])
            for i, d in enumerate(details):
                active.add_metric([name, str(i)], d.get('ngsize', 0))
                total.add_metric([name, str(i)], d.get('size', 0))
        yield active
        yield total


class _PloneCollector(object):
    """Per-scrape collector for Plone-domain metrics.

    Only registered when PLONE_AVAILABLE. Sub-collectors are isolated;
    one failure won't blank a scrape.
    """

    def __init__(self, request, context):
        self._request = request
        self._context = self._resolve_site(context)

    @staticmethod
    def _resolve_site(context):
        """Defensive: ensure context has Plone tools.

        With ZCML restricted to IPloneSiteRoot this short-circuits on
        the first check. Walk one level if a non-site context slips in
        (e.g. test harness or stray re-registration); fall back to the
        original context (sub-collectors fail-safe via `_safe`).
        """
        if getToolByName(context, 'portal_catalog', None) is not None:
            return context
        try:
            for _id, obj in context.objectItems():
                if getToolByName(obj, 'portal_catalog', None) is not None:
                    return obj
        except Exception:
            pass
        return context

    def _safe(self, name, gen):
        try:
            yield from gen()
        except Exception:
            logger.exception("collector %s failed", name)

    def collect(self):
        sub_collectors = (
            ('ploneversion', self._collect_version, True),
            ('plonecatalog', self._collect_catalog, True),
            ('plonecontent', self._collect_content, True),
            ('ploneusers',   self._collect_users,   True),
        )
        for name, fn, enabled in sub_collectors:
            if enabled:
                yield from self._safe(name, fn)

    def _collect_version(self):
        g = _gauge(
            'plone_version_info',
            'Plone version information (always 1).',
            ['plone_version', 'cmf_version'],
        )
        cmf = ''
        try:
            from Products.CMFCore import __version__ as cmf
        except Exception:
            cmf = ''
        g.add_metric([PLONE_VERSION or '', cmf or ''], 1)
        yield g

    def _collect_catalog(self):
        catalog = getToolByName(self._context, 'portal_catalog')
        size = _gauge('plone_catalog_size',
                      'Number of objects indexed in portal_catalog.')
        size.add_metric([], len(catalog))
        yield size

        idx_size = _gauge(
            'plone_catalog_index_size',
            'Number of indexed values per portal_catalog index.',
            ['index'],
        )
        for idx_name in catalog.indexes():
            idx = catalog.Indexes[idx_name]
            n = getattr(idx, 'numObjects', None)
            if callable(n):
                idx_size.add_metric([idx_name], n())
        yield idx_size

    def _collect_content(self):
        catalog = getToolByName(self._context, 'portal_catalog')
        g = _gauge(
            'plone_content_objects',
            'Number of catalogued content objects per portal_type.',
            ['portal_type'],
        )
        search = getattr(catalog, 'unrestrictedSearchResults',
                         catalog.searchResults)
        for ptype in catalog.uniqueValuesFor('portal_type'):
            n = len(search(portal_type=ptype))
            g.add_metric([ptype], n)
        yield g

    def _collect_users(self):
        acl = getToolByName(self._context, 'acl_users')
        users = _gauge('plone_users_total',
                       'Number of users known to acl_users.')
        groups = _gauge('plone_groups_total',
                        'Number of groups known to acl_users.')

        src = getattr(acl, 'source_users', None)
        if src is not None and hasattr(src, 'getUserIds'):
            n_users = len(src.getUserIds())
        else:
            n_users = len(acl.searchUsers())
        users.add_metric([], n_users)

        n_groups = 0
        grp = getattr(acl, 'source_groups', None)
        if grp is not None and hasattr(grp, 'getGroupIds'):
            n_groups = len(grp.getGroupIds())
        groups.add_metric([], n_groups)

        yield users
        yield groups


class Prometheus(BrowserView):
    """Zope-only metrics. Registered for=*; safe at app root."""

    _include_plone = False

    def __call__(self, *args, **kwargs):
        registry = CollectorRegistry(auto_describe=True)
        registry.register(PROCESS_COLLECTOR)
        registry.register(PLATFORM_COLLECTOR)
        registry.register(_ZopeCollector(self.request, self.context))
        if self._include_plone and PLONE_AVAILABLE:
            registry.register(_PloneCollector(self.request, self.context))

        encoder, content_type = choose_encoder(
            self.request.getHeader('Accept', ''))
        self.request.response.setHeader('Content-Type', content_type)
        self.request.response.setHeader('Cache-Control', 'no-cache')
        return encoder(registry)


class PrometheusPlone(Prometheus):
    """Zope + Plone metrics. Registered for=IPloneSiteRoot."""

    _include_plone = True
