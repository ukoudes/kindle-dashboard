"""Keep installed state outside the signed application bundle; preserve source checkout defaults."""
import os
from pathlib import Path


def default_data_dir(root=None):
    configured = os.environ.get('KINDLE_DASHBOARD_DATA_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(root) if root is not None else Path(__file__).resolve().parent.parent)/'data'
