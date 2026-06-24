"""Unit tests: fast, hermetic, no Docker / no network.

Conventions for tests in this directory:

* Import the module-under-test at module scope so collection failures
  surface as ImportError rather than as silent skips.
* Mock external boundaries (``subprocess.run``, ``requests`` / ``urllib``,
  filesystem). Prefer ``pytest-mock``'s ``mocker`` fixture for stdlib /
  attribute patching and ``responses`` for HTTP.
* One assertion concept per test. Use parametrize for combinatorial
  variants instead of cramming multiple cases into one function.
* Test the provider lifecycle methods (``create``, ``diff``, ``update``,
  ``delete``, ``read``, ``check``) directly -- they're plain methods that
  take dicts and return dicts; they don't need Pulumi's runtime.
"""
