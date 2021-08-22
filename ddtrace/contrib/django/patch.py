"""
The Django patching works as follows:

Django internals are instrumented via normal `patch()`.

`django.apps.registry.Apps.populate` is patched to add instrumentation for any
specific Django apps like Django Rest Framework (DRF).
"""
from inspect import getmro
from inspect import isclass
from inspect import isfunction
import sys

from ddtrace import Pin
from ddtrace import config
from ddtrace.constants import SPAN_MEASURED_KEY
from ddtrace.contrib import dbapi
from ddtrace.contrib import func_name


try:
    from psycopg2._psycopg import cursor as psycopg_cursor_cls

    from ddtrace.contrib.psycopg.patch import Psycopg2TracedCursor
except ImportError:
    psycopg_cursor_cls = None
    Psycopg2TracedCursor = None

from ddtrace.ext import SpanTypes
from ddtrace.ext import http
from ddtrace.ext import sql as sqlx
from ddtrace.internal.compat import maybe_stringify
from ddtrace.internal.logger import get_logger
from ddtrace.utils.formats import asbool
from ddtrace.utils.formats import get_env
from ddtrace.vendor import wrapt

from . import utils
from .. import trace_utils


log = get_logger(__name__)

config._add(
    "django",
    dict(
        _default_service="django",
        cache_service_name=get_env("django", "cache_service_name") or "django",
        database_service_name_prefix=get_env("django", "database_service_name_prefix", default=""),
        database_service_name=get_env("django", "database_service_name", default=""),
        trace_fetch_methods=asbool(get_env("django", "trace_fetch_methods", default=False)),
        distributed_tracing_enabled=True,
        instrument_middleware=asbool(get_env("django", "instrument_middleware", default=True)),
        instrument_databases=True,
        instrument_caches=True,
        analytics_enabled=None,  # None allows the value to be overridden by the global config
        analytics_sample_rate=None,
        trace_query_string=None,  # Default to global config
        include_user_name=True,
        use_handler_resource_format=asbool(get_env("django", "use_handler_resource_format", default=False)),
        use_legacy_resource_format=asbool(get_env("django", "use_legacy_resource_format", default=False)),
        use_legacy_db_wrapping=asbool(get_env("django", "use_legacy_db_wrapping", default=False)),
    ),
)


def patch_conn(django, conn):
    def cursor(django, pin, func, instance, args, kwargs):
        alias = getattr(conn, "alias", "default")

        if config.django.database_service_name:
            service = config.django.database_service_name
        else:
            database_prefix = config.django.database_service_name_prefix
            service = "{}{}{}".format(database_prefix, alias, "db")

        vendor = getattr(conn, "vendor", "db")
        prefix = sqlx.normalize_vendor(vendor)
        tags = {
            "django.db.vendor": vendor,
            "django.db.alias": alias,
        }
        pin = Pin(service, tags=tags, tracer=pin.tracer, app=prefix)
        cursor = func(*args, **kwargs)  # type: django.db.backends.utils.CursorWrapper

        # if legacy behavior is desired
        #   wrap the cursor, potentially double wrapping db spans
        # elif the cursor.cursor is already wrapper
        #   update the pin metadata to add django specific tags
        #   do we need to update the service name?
        # else
        #   wrap the unwrapped cursor.cursor

        if config.django.use_legacy_db_wrapping:
            # https://github.com/DataDog/dd-trace-py/commit/af326b6a289600d9ba43d6979dc4de713d85aebe#diff-8f30c25b0c036c175363c0913e45dfa2a17cc2fd41f65669202423501b1e60fb
            traced_cursor_cls = dbapi.TracedCursor
            if (
                Psycopg2TracedCursor is not None
                and hasattr(cursor, "cursor")
                and isinstance(cursor.cursor, psycopg_cursor_cls)
            ):
                traced_cursor_cls = Psycopg2TracedCursor
            return traced_cursor_cls(cursor, pin, config.django)
        elif isinstance(cursor.cursor, dbapi.TracedCursor):
            # Update the pin used on the cursor to have django metadata
            existing_pin = Pin.get_from(cursor.cursor)
            if not existing_pin:
                pin.onto(cursor.cursor)
            else:
                if existing_pin.tags:
                    existing_pin.tags.update(pin.tags)
                else:
                    existing_pin.tags = pin.tags.copy()
                # TODO: Do we update the service?
        else:
            # Wrap the cursor
            traced_cursor_cls = dbapi.TracedCursor
            if Psycopg2TracedCursor is not None and isinstance(cursor.cursor, psycopg_cursor_cls):
                traced_cursor_cls = Psycopg2TracedCursor
            setattr(cursor, "cursor", traced_cursor_cls(cursor.cursor, pin, config.django))
        return cursor

    if not isinstance(conn.cursor, wrapt.ObjectProxy):
        conn.cursor = wrapt.FunctionWrapper(conn.cursor, trace_utils.with_traced_module(cursor)(django))


def instrument_dbs(django):
    def get_connection(wrapped, instance, args, kwargs):
        conn = wrapped(*args, **kwargs)
        try:
            patch_conn(django, conn)
        except Exception:
            log.debug("Error instrumenting database connection %r", conn, exc_info=True)
        return conn

    if not isinstance(django.db.utils.ConnectionHandler.__getitem__, wrapt.ObjectProxy):
        django.db.utils.ConnectionHandler.__getitem__ = wrapt.FunctionWrapper(
            django.db.utils.ConnectionHandler.__getitem__, get_connection
        )


@trace_utils.with_traced_module
def traced_cache(django, pin, func, instance, args, kwargs):
    if not config.django.instrument_caches:
        return func(*args, **kwargs)

    # get the original function method
    with pin.tracer.trace("django.cache", span_type=SpanTypes.CACHE, service=config.django.cache_service_name) as span:
        # update the resource name and tag the cache backend
        span.resource = utils.resource_from_cache_prefix(func_name(func), instance)
        cache_backend = "{}.{}".format(instance.__module__, instance.__class__.__name__)
        span._set_str_tag("django.cache.backend", cache_backend)

        if args:
            keys = utils.quantize_key_values(args[0])
            span._set_str_tag("django.cache.key", str(keys))

        return func(*args, **kwargs)


def instrument_caches(django):
    cache_backends = set([cache["BACKEND"] for cache in django.conf.settings.CACHES.values()])
    for cache_path in cache_backends:
        split = cache_path.split(".")
        cache_module = ".".join(split[:-1])
        cache_cls = split[-1]
        for method in ["get", "set", "add", "delete", "incr", "decr", "get_many", "set_many", "delete_many"]:
            try:
                cls = django.utils.module_loading.import_string(cache_path)
                # DEV: this can be removed when we add an idempotent `wrap`
                if not trace_utils.iswrapped(cls, method):
                    trace_utils.wrap(cache_module, "{0}.{1}".format(cache_cls, method), traced_cache(django))
            except Exception:
                log.debug("Error instrumenting cache %r", cache_path, exc_info=True)


@trace_utils.with_traced_module
def traced_populate(django, pin, func, instance, args, kwargs):
    """django.apps.registry.Apps.populate is the method used to populate all the apps.

    It is used as a hook to install instrumentation for 3rd party apps (like DRF).

    `populate()` works in 3 phases:

        - Phase 1: Initializes the app configs and imports the app modules.
        - Phase 2: Imports models modules for each app.
        - Phase 3: runs ready() of each app config.

    If all 3 phases successfully run then `instance.ready` will be `True`.
    """

    # populate() can be called multiple times, we don't want to instrument more than once
    if instance.ready:
        log.debug("Django instrumentation already installed, skipping.")
        return func(*args, **kwargs)

    ret = func(*args, **kwargs)

    if not instance.ready:
        log.debug("populate() failed skipping instrumentation.")
        return ret

    settings = django.conf.settings

    # Instrument databases
    if config.django.instrument_databases:
        try:
            instrument_dbs(django)
        except Exception:
            log.debug("Error instrumenting Django database connections", exc_info=True)

    # Instrument caches
    if config.django.instrument_caches:
        try:
            instrument_caches(django)
        except Exception:
            log.debug("Error instrumenting Django caches", exc_info=True)

    # Instrument Django Rest Framework if it's installed
    INSTALLED_APPS = getattr(settings, "INSTALLED_APPS", [])

    if "rest_framework" in INSTALLED_APPS:
        try:
            from .restframework import patch_restframework

            patch_restframework(django)
        except Exception:
            log.debug("Error patching rest_framework", exc_info=True)

    return ret


def traced_func(django, name, resource=None, ignored_excs=None):
    """Returns a function to trace Django functions."""

    def wrapped(django, pin, func, instance, args, kwargs):
        with pin.tracer.trace(name, resource=resource) as s:
            if ignored_excs:
                for exc in ignored_excs:
                    s._ignore_exception(exc)
            return func(*args, **kwargs)

    return trace_utils.with_traced_module(wrapped)(django)


def traced_process_exception(django, name, resource=None):
    def wrapped(django, pin, func, instance, args, kwargs):
        with pin.tracer.trace(name, resource=resource) as span:
            resp = func(*args, **kwargs)

            # If the response code is erroneous then grab the traceback
            # and set an error.
            if hasattr(resp, "status_code") and 500 <= resp.status_code < 600:
                span.set_traceback()
            return resp

    return trace_utils.with_traced_module(wrapped)(django)


@trace_utils.with_traced_module
def traced_load_middleware(django, pin, func, instance, args, kwargs):
    """Patches django.core.handlers.base.BaseHandler.load_middleware to instrument all middlewares."""
    settings_middleware = []
    # Gather all the middleware
    if getattr(django.conf.settings, "MIDDLEWARE", None):
        settings_middleware += django.conf.settings.MIDDLEWARE
    if getattr(django.conf.settings, "MIDDLEWARE_CLASSES", None):
        settings_middleware += django.conf.settings.MIDDLEWARE_CLASSES

    # Iterate over each middleware provided in settings.py
    # Each middleware can either be a function or a class
    for mw_path in settings_middleware:
        mw = django.utils.module_loading.import_string(mw_path)

        # Instrument function-based middleware
        if isfunction(mw) and not trace_utils.iswrapped(mw):
            split = mw_path.split(".")
            if len(split) < 2:
                continue
            base = ".".join(split[:-1])
            attr = split[-1]

            # Function-based middleware is a factory which returns a handler function for requests.
            # So instead of tracing the factory, we want to trace its returned value.
            # We wrap the factory to return a traced version of the handler function.
            def wrapped_factory(func, instance, args, kwargs):
                # r is the middleware handler function returned from the factory
                r = func(*args, **kwargs)
                if r:
                    return wrapt.FunctionWrapper(r, traced_func(django, "django.middleware", resource=mw_path))
                # If r is an empty middleware function (i.e. returns None), don't wrap since NoneType cannot be called
                else:
                    return r

            trace_utils.wrap(base, attr, wrapped_factory)

        # Instrument class-based middleware
        elif isclass(mw):
            for hook in [
                "process_request",
                "process_response",
                "process_view",
                "process_template_response",
                "__call__",
            ]:
                if hasattr(mw, hook) and not trace_utils.iswrapped(mw, hook):
                    trace_utils.wrap(
                        mw, hook, traced_func(django, "django.middleware", resource=mw_path + ".{0}".format(hook))
                    )
            # Do a little extra for `process_exception`
            if hasattr(mw, "process_exception") and not trace_utils.iswrapped(mw, "process_exception"):
                res = mw_path + ".{0}".format("process_exception")
                trace_utils.wrap(
                    mw, "process_exception", traced_process_exception(django, "django.middleware", resource=res)
                )

    return func(*args, **kwargs)


@trace_utils.with_traced_module
def traced_get_response(django, pin, func, instance, args, kwargs):
    """Trace django.core.handlers.base.BaseHandler.get_response() (or other implementations).

    This is the main entry point for requests.

    Django requests are handled by a Handler.get_response method (inherited from base.BaseHandler).
    This method invokes the middleware chain and returns the response generated by the chain.
    """

    request = kwargs.get("request", args[0])
    if request is None:
        return func(*args, **kwargs)

    trace_utils.activate_distributed_headers(pin.tracer, int_config=config.django, request_headers=request.META)

    with pin.tracer.trace(
        "django.request",
        resource=request.method,
        service=trace_utils.int_service(pin, config.django),
        span_type=SpanTypes.WEB,
    ) as span:
        utils._before_request_tags(pin, span, request)
        span.metrics[SPAN_MEASURED_KEY] = 1

        response = None
        try:
            response = func(*args, **kwargs)
            return response
        finally:
            # DEV: Always set these tags, this is where `span.resource` is set
            utils._after_request_tags(pin, span, request, response)


@trace_utils.with_traced_module
def traced_template_render(django, pin, wrapped, instance, args, kwargs):
    """Instrument django.template.base.Template.render for tracing template rendering."""
    template_name = maybe_stringify(getattr(instance, "name", None))
    if template_name:
        resource = template_name
    else:
        resource = "{0}.{1}".format(func_name(instance), wrapped.__name__)

    with pin.tracer.trace("django.template.render", resource=resource, span_type=http.TEMPLATE) as span:
        if template_name:
            span._set_str_tag("django.template.name", template_name)
        engine = getattr(instance, "engine", None)
        if engine:
            span._set_str_tag("django.template.engine.class", func_name(engine))

        return wrapped(*args, **kwargs)


def instrument_view(django, view):
    """
    Helper to wrap Django views.

    We want to wrap all lifecycle/http method functions for every class in the MRO for this view
    """
    if hasattr(view, "__mro__"):
        for cls in reversed(getmro(view)):
            _instrument_view(django, cls)

    return _instrument_view(django, view)


def _instrument_view(django, view):
    """Helper to wrap Django views."""
    # All views should be callable, double check before doing anything
    if not callable(view):
        return view

    # Patch view HTTP methods and lifecycle methods
    http_method_names = getattr(view, "http_method_names", ("get", "delete", "post", "options", "head"))
    lifecycle_methods = ("setup", "dispatch", "http_method_not_allowed")
    for name in list(http_method_names) + list(lifecycle_methods):
        try:
            func = getattr(view, name, None)
            if not func or isinstance(func, wrapt.ObjectProxy):
                continue

            resource = "{0}.{1}".format(func_name(view), name)
            op_name = "django.view.{0}".format(name)
            trace_utils.wrap(view, name, traced_func(django, name=op_name, resource=resource))
        except Exception:
            log.debug("Failed to instrument Django view %r function %s", view, name, exc_info=True)

    # Patch response methods
    response_cls = getattr(view, "response_class", None)
    if response_cls:
        methods = ("render",)
        for name in methods:
            try:
                func = getattr(response_cls, name, None)
                # Do not wrap if the method does not exist or is already wrapped
                if not func or isinstance(func, wrapt.ObjectProxy):
                    continue

                resource = "{0}.{1}".format(func_name(response_cls), name)
                op_name = "django.response.{0}".format(name)
                trace_utils.wrap(response_cls, name, traced_func(django, name=op_name, resource=resource))
            except Exception:
                log.debug("Failed to instrument Django response %r function %s", response_cls, name, exc_info=True)

    # If the view itself is not wrapped, wrap it
    if not isinstance(view, wrapt.ObjectProxy):
        view = wrapt.FunctionWrapper(
            view, traced_func(django, "django.view", resource=func_name(view), ignored_excs=[django.http.Http404])
        )
    return view


@trace_utils.with_traced_module
def traced_urls_path(django, pin, wrapped, instance, args, kwargs):
    """Wrapper for url path helpers to ensure all views registered as urls are traced."""
    try:
        if "view" in kwargs:
            kwargs["view"] = instrument_view(django, kwargs["view"])
        elif len(args) >= 2:
            args = list(args)
            args[1] = instrument_view(django, args[1])
            args = tuple(args)
    except Exception:
        log.debug("Failed to instrument Django url path %r %r", args, kwargs, exc_info=True)
    return wrapped(*args, **kwargs)


@trace_utils.with_traced_module
def traced_as_view(django, pin, func, instance, args, kwargs):
    """
    Wrapper for django's View.as_view class method
    """
    try:
        instrument_view(django, instance)
    except Exception:
        log.debug("Failed to instrument Django view %r", instance, exc_info=True)
    view = func(*args, **kwargs)
    return wrapt.FunctionWrapper(view, traced_func(django, "django.view", resource=func_name(view)))


@trace_utils.with_traced_module
def traced_get_asgi_application(django, pin, func, instance, args, kwargs):
    from ddtrace.contrib.asgi import TraceMiddleware

    def django_asgi_modifier(span, scope):
        span.name = "django.request"

    return TraceMiddleware(func(*args, **kwargs), integration_config=config.django, span_modifier=django_asgi_modifier)


def _patch(django):
    Pin().onto(django)
    trace_utils.wrap(django, "apps.registry.Apps.populate", traced_populate(django))

    # DEV: this check will be replaced with import hooks in the future
    if "django.core.handlers.base" not in sys.modules:
        import django.core.handlers.base

    if config.django.instrument_middleware:
        trace_utils.wrap(django, "core.handlers.base.BaseHandler.load_middleware", traced_load_middleware(django))

    trace_utils.wrap(django, "core.handlers.base.BaseHandler.get_response", traced_get_response(django))
    if hasattr(django.core.handlers.base.BaseHandler, "get_response_async"):
        # Have to inline this import as the module contains syntax incompatible with Python 3.5 and below
        from ._asgi import traced_get_response_async

        trace_utils.wrap(django, "core.handlers.base.BaseHandler.get_response_async", traced_get_response_async(django))

        # Only wrap get_asgi_application if get_response_async exists. Otherwise we will effectively double-patch
        # because get_response and get_asgi_application will be used.
        if "django.core.asgi" not in sys.modules:
            try:
                import django.core.asgi
            except ImportError:
                pass
            else:
                trace_utils.wrap(django, "core.asgi.get_asgi_application", traced_get_asgi_application(django))

    # DEV: this check will be replaced with import hooks in the future
    if "django.template.base" not in sys.modules:
        import django.template.base
    trace_utils.wrap(django, "template.base.Template.render", traced_template_render(django))

    # DEV: this check will be replaced with import hooks in the future
    if "django.conf.urls.static" not in sys.modules:
        import django.conf.urls.static
    trace_utils.wrap(django, "conf.urls.url", traced_urls_path(django))
    if django.VERSION >= (2, 0, 0):
        trace_utils.wrap(django, "urls.path", traced_urls_path(django))
        trace_utils.wrap(django, "urls.re_path", traced_urls_path(django))

    # DEV: this check will be replaced with import hooks in the future
    if "django.views.generic.base" not in sys.modules:
        import django.views.generic.base
    trace_utils.wrap(django, "views.generic.base.View.as_view", traced_as_view(django))


def patch():
    # DEV: this import will eventually be replaced with the module given from an import hook
    import django

    if django.VERSION < (1, 10, 0):
        utils.Resolver404 = django.core.urlresolvers.Resolver404
    else:
        utils.Resolver404 = django.urls.exceptions.Resolver404

    utils.DJANGO22 = django.VERSION >= (2, 2, 0)

    if getattr(django, "_datadog_patch", False):
        return
    _patch(django)

    setattr(django, "_datadog_patch", True)


def _unpatch(django):
    trace_utils.unwrap(django.apps.registry.Apps, "populate")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "load_middleware")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "get_response")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "get_response_async")
    trace_utils.unwrap(django.template.base.Template, "render")
    trace_utils.unwrap(django.conf.urls.static, "static")
    trace_utils.unwrap(django.conf.urls, "url")
    if django.VERSION >= (2, 0, 0):
        trace_utils.unwrap(django.urls, "path")
        trace_utils.unwrap(django.urls, "re_path")
    trace_utils.unwrap(django.views.generic.base.View, "as_view")
    for conn in django.db.connections.all():
        trace_utils.unwrap(conn, "cursor")
    trace_utils.unwrap(django.db.utils.ConnectionHandler, "__getitem__")


def unpatch():
    import django

    if not getattr(django, "_datadog_patch", False):
        return

    _unpatch(django)

    setattr(django, "_datadog_patch", False)
