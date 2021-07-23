import datetime
import logging
import os
import platform
import re
import sys
from typing import Any
from typing import Dict
from typing import TYPE_CHECKING
from typing import Union

import pkg_resources

import ddtrace
from ddtrace.internal.writer import AgentWriter
from ddtrace.internal.writer import LogWriter

from .logger import get_logger


if TYPE_CHECKING:
    from ddtrace import Tracer


logger = get_logger(__name__)


def in_venv():
    # type: () -> bool
    # Works with both venv and virtualenv
    # https://stackoverflow.com/a/42580137
    return (
        "VIRTUAL_ENV" in os.environ
        or hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def tags_to_str(tags):
    # type: (Dict[str, Any]) -> str
    # Turn a dict of tags to a string "k1:v1,k2:v2,..."
    return ",".join(["%s:%s" % (k, v) for k, v in tags.items()])


def collect(tracer):
    # type: (Tracer) -> Dict[str, Any]
    """Collect system and library information into a serializable dict."""

    if isinstance(tracer.writer, LogWriter):
        agent_url = "AGENTLESS"
        agent_error = None
    elif isinstance(tracer.writer, AgentWriter):
        writer = tracer.writer
        agent_url = writer.agent_url
        try:
            writer.write([])
            writer.flush_queue(raise_exc=True)
        except Exception as e:
            agent_error = "Agent not reachable at %s. Exception raised: %s" % (agent_url, str(e))
        else:
            agent_error = None
    else:
        agent_url = "CUSTOM"
        agent_error = None

    is_venv = in_venv()

    packages_available = {p.project_name: p.version for p in pkg_resources.working_set}
    integration_configs = {}  # type: Dict[str, Union[Dict[str, Any], str]]
    for module, enabled in ddtrace.monkey.PATCH_MODULES.items():
        # TODO: this check doesn't work in all cases... we need a mapping
        #       between the module and the library name.
        module_available = module in packages_available
        module_instrumented = module in ddtrace.monkey._PATCHED_MODULES
        module_imported = module in sys.modules

        if enabled:
            # Note that integration configs aren't added until the integration
            # module is imported. This typically occurs as a side-effect of
            # patch().
            # This also doesn't load work in all cases since we don't always
            # name the configuration entry the same as the integration module
            # name :/
            config = ddtrace.config._config.get(module, "N/A")
        else:
            config = None

        if module_available:
            integration_configs[module] = dict(
                enabled=enabled,
                instrumented=module_instrumented,
                module_available=module_available,
                module_version=packages_available[module],
                module_imported=module_imported,
                config=config,
            )
        else:
            # Use N/A here to avoid the additional clutter of an entire
            # config dictionary for a module that isn't available.
            integration_configs[module] = "N/A"

    pip_version = packages_available.get("pip", "N/A")

    return dict(
        # Timestamp UTC ISO 8601
        date=datetime.datetime.utcnow().isoformat(),
        # eg. "Linux", "Darwin"
        os_name=platform.system(),
        # eg. 12.5.0
        os_version=platform.release(),
        is_64_bit=sys.maxsize > 2 ** 32,
        architecture=platform.architecture()[0],
        vm=platform.python_implementation(),
        version=ddtrace.__version__,
        lang="python",
        lang_version=platform.python_version(),
        pip_version=pip_version,
        in_virtual_env=is_venv,
        agent_url=agent_url,
        agent_error=agent_error,
        env=ddtrace.config.env or "",
        is_global_tracer=tracer == ddtrace.tracer,
        enabled_env_setting=os.getenv("DATADOG_TRACE_ENABLED"),
        tracer_enabled=tracer.enabled,
        sampler_type=type(tracer.sampler).__name__ if tracer.sampler else "N/A",
        priority_sampler_type=type(tracer.priority_sampler).__name__ if tracer.priority_sampler else "N/A",
        service=ddtrace.config.service or "",
        debug=ddtrace.tracer.log.isEnabledFor(logging.DEBUG),
        enabled_cli="ddtrace" in os.getenv("PYTHONPATH", ""),
        analytics_enabled=ddtrace.config.analytics_enabled,
        log_injection_enabled=ddtrace.config.logs_injection,
        health_metrics_enabled=ddtrace.config.health_metrics_enabled,
        dd_version=ddtrace.config.version or "",
        priority_sampling_enabled=tracer.priority_sampler is not None,
        global_tags=os.getenv("DD_TAGS", ""),
        tracer_tags=tags_to_str(tracer.tags),
        integrations=integration_configs,
        partial_flush_enabled=tracer._partial_flush_enabled,
        partial_flush_min_spans=tracer._partial_flush_min_spans,
    )


def pretty_collect(tracer, color=True):
    class bcolors:
        HEADER = "\033[95m"
        OKBLUE = "\033[94m"
        OKCYAN = "\033[96m"
        OKGREEN = "\033[92m"
        WARNING = "\033[93m"
        FAIL = "\033[91m"
        ENDC = "\033[0m"
        BOLD = "\033[1m"

    info = collect(tracer)

    info_pretty = """{0}{1}Tracer Configurations:{2}
    Tracer enabled: {3}
    Debug logging: {4}
    Writing traces to: {5}
    Agent error: {6}
    App Analytics enabled(deprecated): {7}
    Log injection enabled: {8}
    Health metrics enabled: {9}
    Priority sampling enabled: {10}
    Partial flushing enabled: {11}
    Partial flush minimum number of spans: {12}
    {13}{14}Tagging:{15}
    DD Service: {16}
    DD Env: {17}
    DD Version: {18}
    Global Tags: {19}
    Tracer Tags: {20}""".format(
        bcolors.OKBLUE,
        bcolors.BOLD,
        bcolors.ENDC,
        info.get("tracer_enabled"),
        info.get("debug"),
        info.get("agent_url") or "Not writing at the moment, is your tracer running?",
        info.get("agent_error") or "None",
        info.get("analytics_enabled"),
        info.get("log_injection_enabled"),
        info.get("health_metrics_enabled"),
        info.get("priority_sampling_enabled"),
        info.get("partial_flush_enabled"),
        info.get("partial_flush_min_spans") or "Not set",
        bcolors.OKGREEN,
        bcolors.BOLD,
        bcolors.ENDC,
        info.get("service") or "None",
        info.get("env") or "None",
        info.get("dd_version") or "None",
        info.get("global_tags") or "None",
        info.get("tracer_tags") or "None",
    )

    summary = "{0}{1}Summary{2}".format(bcolors.OKCYAN, bcolors.BOLD, bcolors.ENDC)

    if info.get("agent_error"):
        summary += "\n\n{0}ERROR: It looks like you have an agent error: '{1}'\n If you're experiencing a connection error, please make sure you've followed the setup for your particular environment so that the tracer and Datadog agent are configured properly to connect, and that the Datadog agent is running: https://ddtrace.readthedocs.io/en/stable/troubleshooting.html#failed-to-send-traces-connectionrefusederror\nIf your issue is not a connection error then please reach out to support for further assistance: https://docs.datadoghq.com/help/{2}".format(
            bcolors.FAIL, info.get("agent_error"), bcolors.ENDC
        )

    if not info.get("service"):
        summary += "\n\n{0}WARNING SERVICE NOT SET: It is recommended that a service tag be set for all traced applications. For more information please see https://ddtrace.readthedocs.io/en/stable/troubleshooting.html".format(
            bcolors.WARNING, bcolors.ENDC
        )

    if not info.get("env"):
        summary += "\n\n{0}WARNING ENV NOT SET: It is recommended that an env tag be set for all traced applications. For more information please see https://ddtrace.readthedocs.io/en/stable/troubleshooting.html{1}".format(
            bcolors.WARNING, bcolors.ENDC
        )

    if not info.get("dd_version"):
        summary += "\n\n{0}WARNING VERSION NOT SET: It is recommended that a version tag be set for all traced applications. For more information please see https://ddtrace.readthedocs.io/en/stable/troubleshooting.html{1}".format(
            bcolors.WARNING, bcolors.ENDC
        )

    info_pretty += "\n\n" + summary

    if color == False:
        return escape_ansi(info_pretty)

    return info_pretty


def escape_ansi(line):
    ansi_escape = re.compile(r"(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", line)
