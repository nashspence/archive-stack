from __future__ import annotations

import pytest
from pytest_bdd import scenarios

pytestmark = pytest.mark.integration

scenarios("../acceptance/features/api.collections.feature")
scenarios("../acceptance/features/api.fetches.feature")
scenarios("../acceptance/features/api.pins.feature")
scenarios("../acceptance/features/api.plan_and_images.feature")
scenarios("../acceptance/features/api.search.feature")
scenarios("../acceptance/features/cli.arc.feature")
scenarios("../acceptance/features/cli.arc_disc.feature")
scenarios("../acceptance/features/cli.arc_disc_api.feature")
