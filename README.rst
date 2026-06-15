============================
Prometheus Plone Integration
============================

This package publishes Plone/Zope statistics in a format that can be
consumed by Prometheus_. It is built on top of the official
``prometheus_client`` library.

It was largely based on ``munin.zope``. See https://pypi.org/project/munin.zope/

Metrics
-------

============================================== ======== =================
Name                                           Type     Labels
============================================== ======== =================
zope_total_threads                             gauge    —
zope_free_threads                              gauge    —
zope_busy_threads                              gauge    —
zope_request_queue_length                      gauge    —
zope_total_objects                             gauge    db
zope_cache_objects                             gauge    db
zope_cache_size                                gauge    db
zodb_connections                               gauge    db
zodb_object_loads_total                        counter  db
zodb_object_stores_total                       counter  db
zope_connection_active_objects                 gauge    db, connection
zope_connection_total_objects                  gauge    db, connection
plone_version_info                             gauge    plone_version, cmf_version
plone_catalog_size                             gauge    —
plone_catalog_index_size                       gauge    index
plone_content_objects                          gauge    portal_type
plone_users_total                              gauge    —
plone_groups_total                             gauge    —
============================================== ======== =================

The ``zope_*_threads`` and ``zope_request_queue_length`` gauges are only
emitted when ZServer is available (install with the ``zserver`` extra).
The ``plone_*`` metrics are emitted only when ``Products.CMFPlone`` is
importable (install with the ``plone`` extra, or rely on Plone already
being present in the instance).
Standard ``process_*`` and ``python_*`` metrics from ``prometheus_client``
are also exposed.

Endpoints
---------

* ``/@@metrics`` — Zope/process/ZODB metrics. Works at the Zope
  application root and inside any traversable object. Always safe to
  scrape, even on bare-Zope (non-Plone) deployments.
* ``/<site_id>/@@metrics`` — adds Plone metrics
  (``plone_catalog_size``, ``plone_catalog_index_size``,
  ``plone_content_objects``, ``plone_users_total``,
  ``plone_groups_total``, ``plone_version_info``). This is the
  canonical scrape target for a Plone site.

Installation (using Buildout)
-----------------------------

Add ``collective.prometheus`` to your instance eggs in ``buildout.cfg``.

On a bare-Zope install, only ``zope_*`` and ``zodb_*`` metrics are
exposed. Install with ``pip install collective.prometheus[plone]`` (or
add ``Products.CMFPlone`` separately) to enable the ``plone_*`` metrics.

Usage
-----

Assuming Plone listens on ``localhost:8000``, start your Plone instance
and visit http://localhost:8000/@@metrics to confirm output is being
served.

Add a job to ``scrape_configs`` in ``prometheus.yaml``:

.. code-block:: yaml

    - job_name: 'plone'
      metrics_path: '/@@metrics'
      static_configs:
      - targets: ['localhost:8000']

Pass ``?filestorage=*`` to scrape every configured filestorage; pass
``?filestorage=<name>`` for a single one. The default scrapes ``main``.

Example PromQL
--------------

.. code-block:: promql

    # Object load rate per database
    rate(zodb_object_loads_total[5m])

    # ZODB size by database
    sum by (db) (zope_total_objects)

    # Largest connection cache
    max by (connection) (zope_connection_active_objects)

    # Worker saturation
    zope_busy_threads / zope_total_threads

    # Top 10 most common content types
    topk(10, plone_content_objects)

    # Catalog growth rate (objects/sec) over the last hour
    deriv(plone_catalog_size[1h])

Security
--------

The ``metrics`` view ships with the default ``zope2.View`` permission so
that existing anonymous Prometheus scrape jobs keep working after upgrade.
For production deployments it is recommended to:

* Override the ``metrics`` view permission to ``zope2.ManageServer`` (or a
  custom permission granted only to a dedicated scrape user) via a ZCML
  override. Drop the following into your policy package's
  ``overrides.zcml``:

  .. code-block:: xml

      <configure xmlns:browser="http://namespaces.zope.org/browser">
        <browser:page
            for="*"
            name="metrics"
            class="collective.prometheus.browser.Prometheus"
            permission="zope2.ManageServer"
            />
      </configure>

* Restrict access to ``/@@metrics`` at the network/reverse-proxy layer.

Upgrading to 2.0
----------------

Version 2.0 is a breaking rewrite. Metric names, labels, and the wire
format have all changed. See ``docs/HISTORY.txt`` for the full list of
changes; downstream Grafana dashboards will need to be updated.

.. _Prometheus: https://prometheus.io/
