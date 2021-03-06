# Copyright (C) Least Authority TFA GmbH

from __future__ import (
    absolute_import,
    division,
    print_function,
)

# see https://github.com/LeastAuthority/magic-folder/issues/305
import warnings
warnings.filterwarnings("ignore", message=".*Python 2 is no longer supported by the Python core team.*")

__all__ = [
    "__version__",
]

from ._version import (
    __version__,
)
