"""Non-semantic failures must never be sent to a paid test repair loop."""

class InfrastructureUnavailableError(RuntimeError):
    """Pause the instance; retry execution after repairing its environment."""


def check_execution_infrastructure(execution):
    # Narrow runner diagnostics only. ImportError, fixture errors and test
    # timeouts may be actual test/issue behavior and must not be blocked here.
    if execution.returncode == 0:
        return
    text = str(execution.stderr or "") + "\n" + str(execution.stdout or "")
    markers = (
        "fatal: Unable to create '/testbed/.git/index.lock': File exists",
        "Cannot connect to the Docker daemon",
        "Error response from daemon: No such container",
        "Error response from daemon: container",
        "CondaEnvironmentNotFoundError:",
        "EnvironmentNameNotFound:",
    )
    if any(marker in text for marker in markers):
        raise InfrastructureUnavailableError(text[-4000:])


def infrastructure_result(value):
    """Reject old execution checkpoints, without scanning generated source code."""
    if not isinstance(value, dict):
        return False
    if value.get('status') in {'PAUSED_INFRA', 'INFRA_ERROR'}:
        return True
    if 'returncode' in value:
        from types import SimpleNamespace
        try:
            check_execution_infrastructure(SimpleNamespace(
                returncode=value['returncode'], stdout=value.get('stdout', ''), stderr=value.get('stderr', '')))
        except InfrastructureUnavailableError:
            return True
    return any(infrastructure_result(value.get(key)) for key in
               ('seed_execution', 'buggy_execution', 'execution', 'dual_version_result'))
