"""Residential electrification assistance analysis.

The package is standard-library only.  It ingests anonymous household
applications, electricity accounts and bills, housing/equipment facts,
installer quotes and availability windows, then reports:

* data completeness and the supportable scope of each application,
* band-level priority information (never a precise income ranking),
* comparisons between different slot/budget arrangements,
* the three independent states of grant approval, installation and
  measured bill change,
* the impact of bill revisions or installation delays on decisions that
  were already published, while preserving the original decision basis.
"""

from .analysis import Analysis, analyze
from .loader import DatasetError, load_dataset
from .planning import Scenario
from .state import load_state, save_state

__all__ = [
    "Analysis",
    "analyze",
    "load_dataset",
    "load_state",
    "save_state",
    "DatasetError",
    "Scenario",
]
