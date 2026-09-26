# SPDX-License-Identifier: GPL-2.0

import argparse
import unittest
from unittest import mock

from podman_compose import compose_start


class TestComposeStart(unittest.IsolatedAsyncioTestCase):
    async def test_wait_dry_run_does_not_inspect_containers(self) -> None:
        compose = mock.Mock(unsafe=True)
        compose.container_names_by_service = {"app": ["app_1"]}
        compose.podman.run = mock.AsyncMock(return_value=None)
        compose.podman.output = mock.AsyncMock()
        args = argparse.Namespace(
            services=["app"],
            wait=True,
            wait_timeout=1,
            dry_run=True,
            timeout=None,
        )

        with mock.patch("podman_compose.get_wait_deadline") as get_deadline:
            result = await compose_start(compose, args)

        self.assertIsNone(result)
        get_deadline.assert_not_called()
        compose.podman.output.assert_not_awaited()

    async def test_wait_timeout_starts_after_containers_are_started(self) -> None:
        events = []

        async def start_container(*_args: object, **_kwargs: object) -> int:
            events.append("start")
            return 0

        compose = mock.Mock(unsafe=True)
        compose.container_names_by_service = {"app": ["app_1"]}
        compose.podman.run = mock.AsyncMock(side_effect=start_container)
        args = argparse.Namespace(
            services=["app"],
            wait=True,
            wait_timeout=1,
            dry_run=False,
            timeout=None,
        )

        def make_deadline(_timeout: int) -> float:
            events.append("deadline")
            return 123.0

        async def wait_until_ready(*_args: object, **_kwargs: object) -> None:
            events.append("wait")

        with mock.patch("podman_compose.get_wait_deadline", side_effect=make_deadline), mock.patch(
            "podman_compose.wait_for_container_running_healthy",
            new=mock.AsyncMock(side_effect=wait_until_ready),
        ) as wait_for_ready:
            result = await compose_start(compose, args)

        self.assertEqual(result, 0)
        self.assertEqual(events, ["start", "deadline", "wait"])
        wait_for_ready.assert_awaited_once_with(compose, ["app_1"], deadline=123.0)
