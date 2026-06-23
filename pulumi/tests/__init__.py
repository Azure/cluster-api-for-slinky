"""Tests for the umbrella Pulumi stack at ``pulumi/__main__.py``.

Two tiers, separated by directory:

* ``unit/`` \u2014 fast pure-Python tests of the dynamic-Resource provider
  lifecycle methods (``Create`` / ``Read`` / ``Update`` / ``Delete`` /
  ``Diff`` / ``Check``). No Pulumi runtime, no Docker, no network.
  Mock ``subprocess.run`` for CLI wrappers and ``responses`` for HTTP.

* ``integration/`` \u2014 end-to-end tests that drive a real ``pulumi up`` /
  ``destroy`` against Docker via the Pulumi Automation API. Gated behind
  the ``integration`` pytest marker (skipped by default).

Run the fast tier with::

    cd pulumi && pytest

Run the slow tier (requires Docker + a few minutes)::

    cd pulumi && pytest -m integration
"""
