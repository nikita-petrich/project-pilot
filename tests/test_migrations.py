"""Guards the Alembic revision tree. Needs no database — it only reads the scripts.

The container applies ``upgrade head`` on start, so a forked revision tree is not a
tidiness issue: it crash-loops the deploy. Two parallel feature branches merging
without a merge revision is the normal way that happens, and nothing else in the
suite would notice.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

CONFIG_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(CONFIG_PATH)))


def test_exactly_one_head() -> None:
    """More than one head makes ``alembic upgrade head`` ambiguous, and it aborts.

    Fix a failure with ``uv run alembic merge -m "<why>" heads``, which writes a
    revision joining them — not by editing an existing migration's down_revision.
    """
    heads = _scripts().get_heads()
    assert len(heads) == 1, (
        f"expected a single head, found {len(heads)}: {', '.join(sorted(heads))}. "
        "Run: uv run alembic merge -m '<why>' heads"
    )


def test_every_revision_is_reachable_from_the_head() -> None:
    """A revision outside the head's ancestry would silently never be applied."""
    scripts = _scripts()
    (head,) = scripts.get_heads()
    reachable = {script.revision for script in scripts.walk_revisions("base", head)}
    everything = {script.revision for script in scripts.walk_revisions()}
    assert everything == reachable, f"unreachable revisions: {everything - reachable}"
