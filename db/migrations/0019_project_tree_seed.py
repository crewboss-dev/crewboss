"""Project tree seed: a no-op in the public release.

This migration used to seed one installation's own project tree (accounts, repos and environments). That was
instance-specific data, applied once on that installation and tracked by filename, so it is gone from the public
code. The file stays so the migration sequence is unbroken; a fresh install gets an empty schema and builds its tree
through the app (Projects > New project).
"""


def migrate(conn) -> None:
    return None
