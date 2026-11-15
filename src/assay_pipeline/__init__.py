"""Trusted Assay Pipeline: provenance-carrying assay data read by an audited, guarded agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("assay-pipeline")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
