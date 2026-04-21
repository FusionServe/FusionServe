"""Basic smoke tests for the fusionserve package."""

import fusionserve

__author__ = "Marco Frassinelli"
__copyright__ = "Marco Frassinelli"
__license__ = "MIT"


def test_package_has_version():
    """The package exposes a ``__version__`` string."""
    assert isinstance(fusionserve.__version__, str)
    assert fusionserve.__version__ != ""
