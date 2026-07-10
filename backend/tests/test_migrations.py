"""BUILD_PLAN Phase 1 done-gate: upgrade head -> downgrade base -> upgrade head
runs clean against a real Postgres."""

from alembic.config import Config

from alembic import command


def test_upgrade_downgrade_upgrade_cycle(test_database: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_database)
    # test_database fixture already upgraded to head
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
