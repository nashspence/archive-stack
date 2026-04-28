from __future__ import annotations

from pytest_bdd import scenarios

from tests.fixtures.bdd_steps import *  # noqa: F403
from tests.fixtures.production import acceptance_system  # noqa: F401

scenarios("../acceptance/features/api.collections.feature")
scenarios("../acceptance/features/api.fetches.feature")
scenarios("../acceptance/features/api.files.feature")
scenarios("../acceptance/features/api.pins.feature")
scenarios("../acceptance/features/api.plan_and_images.feature")
scenarios("../acceptance/features/api.recovery_sessions.feature")
scenarios("../acceptance/features/api.search.feature")
scenarios("../acceptance/features/api.storage.feature")
scenarios("../acceptance/features/cli.arc.feature")
scenarios("../acceptance/features/cli.arc_disc.feature")
scenarios("../acceptance/features/cli.arc_disc_burn.feature")
scenarios("../acceptance/features/cli.arc_disc_recover.feature")
