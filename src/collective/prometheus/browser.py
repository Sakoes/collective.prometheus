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


class Prometheus(BrowserView):

    def __call__(self, *args, **kwargs):
        registry = CollectorRegistry(auto_describe=True)
        registry.register(PROCESS_COLLECTOR)
        registry.register(PLATFORM_COLLECTOR)
        registry.register(_ZopeCollector(self.request, self.context))

        encoder, content_type = choose_encoder(
            self.request.getHeader('Accept', ''))
        self.request.response.setHeader('Content-Type', content_type)
        self.request.response.setHeader('Cache-Control', 'no-cache')
        return encoder(registry)
