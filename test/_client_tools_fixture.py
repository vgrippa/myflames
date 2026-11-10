"""Client fixture directories with permissions independent of the caller's umask."""
from pathlib import Path


def private_client_dir(directory):
    """Create each missing ancestor privately; preserve existing trust-test modes."""
    directory = Path(directory)
    if not directory.exists():
        private_client_dir(directory.parent)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    return directory
