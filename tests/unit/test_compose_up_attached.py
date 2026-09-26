# SPDX-License-Identifier: GPL-2.0

from __future__ import annotations

import argparse
import asyncio
import json
import unittest
from typing import Any
from unittest import mock

from podman_compose import PodmanCompose
from podman_compose import PodmanComposeError
from podman_compose import ServiceDependency
from podman_compose import compose_up


class TestComposeUpAttached(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name, result in (
            ("prepare_images", 0),
            ("create_secrets_from_environment", None),
            ("create_pods", None),
            ("container_to_args", []),
        ):
            patcher = mock.patch(f"podman_compose.{name}", new=mock.AsyncMock(return_value=result))
            patcher.start()
            self.addCleanup(patcher.stop)
        interval_patcher = mock.patch("podman_compose.WAIT_POLL_INTERVAL", 0.001)
        interval_patcher.start()
        self.addCleanup(interval_patcher.stop)
        loop = asyncio.get_running_loop()
        signal_patcher = mock.patch.object(loop, "add_signal_handler")
        signal_patcher.start()
        self.addCleanup(signal_patcher.stop)

    async def run_attached(
        self,
        condition: str,
        status: str,
        health: str,
        *,
        abort_on_exit: bool = False,
        abort_on_failure: bool = False,
        no_attach: bool = False,
        start_fails: bool = False,
    ) -> tuple[int | None, bool]:
        compose = PodmanCompose()
        compose.project_name = "test"
        compose.services = {"db": {}, "app": {}}
        compose.container_names_by_service = {"db": ["db_1"], "app": ["app_1"]}
        compose.containers = [
            {"name": "db_1", "_service": "db", "_deps": set()},
            {
                "name": "app_1",
                "_service": "app",
                "_deps": {ServiceDependency("db", condition)},
            },
        ]
        compose.podman = mock.Mock()
        compose.podman.existing_containers = mock.AsyncMock(return_value={})
        args = argparse.Namespace(
            services=[],
            no_attach=["app"] if no_attach else [],
            no_deps=False,
            dry_run=False,
            no_start=False,
            detach=False,
            abort_on_container_exit=abort_on_exit,
            abort_on_container_failure=abort_on_failure,
        )
        start_requested = asyncio.Event()
        old_exit_inspected = asyncio.Event()
        app_started = asyncio.Event()
        new_run = False
        attachment_cancelled = False

        async def run(_podman_args: Any, command: str, args: list[str], **_kwargs: Any) -> int:
            nonlocal new_run, attachment_cancelled
            if command == "create":
                return 0
            if args[-1] == "app_1":
                self.assertTrue(new_run)
                app_started.set()
                return 0
            start_requested.set()
            await old_exit_inspected.wait()
            if start_fails:
                return 125
            new_run = True
            try:
                await app_started.wait()
            except asyncio.CancelledError:
                attachment_cancelled = True
                raise
            return 0

        async def output(_podman_args: Any, command: str, _args: list[str]) -> bytes:
            if command == "wait":
                self.assertTrue(new_run, "completion wait registered against the previous run")
                return b"0\n"
            if start_requested.is_set() and not new_run:
                old_exit_inspected.set()
            return json.dumps([
                {
                    "Name": "db_1",
                    "Config": {"Healthcheck": {"Test": ["CMD", "true"]}},
                    "State": {
                        "Status": status if new_run else "exited",
                        "StartedAt": "new-start" if new_run else "old-start",
                        "ExitCode": 1 if new_run else 17,
                        "Health": {"Status": health if new_run else "unhealthy"},
                    },
                }
            ]).encode()

        compose.podman.run = mock.AsyncMock(side_effect=run)
        compose.podman.output = mock.AsyncMock(side_effect=output)
        try:
            result = await asyncio.wait_for(compose_up(compose, args), timeout=1)
        except PodmanComposeError:
            self.assertFalse(app_started.is_set())
            if not start_fails:
                self.assertTrue(attachment_cancelled)
            raise
        self.assertTrue(old_exit_inspected.is_set())
        return result, app_started.is_set()

    async def test_stopped_stack_waits_for_new_healthy_run(self) -> None:
        result, started = await self.run_attached("service_healthy", "running", "healthy")
        self.assertEqual(result, 0)
        self.assertTrue(started)

    async def test_completed_dependency_ignores_previous_exit(self) -> None:
        result, started = await self.run_attached(
            "service_completed_successfully", "exited", "starting"
        )
        self.assertEqual(result, 0)
        self.assertTrue(started)

    async def test_new_run_exit_fails_and_cleans_up_attachments(self) -> None:
        with self.assertRaisesRegex(PodmanComposeError, "db_1 exited with code 1"):
            await self.run_attached("service_healthy", "exited", "starting")

    async def test_failed_start_does_not_leave_dependency_waiting(self) -> None:
        with self.assertRaisesRegex(PodmanComposeError, "db_1 failed to start: exit code 125"):
            await self.run_attached("service_healthy", "running", "healthy", start_fails=True)

    async def test_unhealthy_dependency_propagates_with_all_abort_modes(self) -> None:
        for abort_on_exit, abort_on_failure in ((False, False), (True, False), (False, True)):
            for no_attach in (False, True):
                with self.subTest(
                    abort_on_exit=abort_on_exit,
                    abort_on_failure=abort_on_failure,
                    no_attach=no_attach,
                ):
                    with self.assertRaisesRegex(PodmanComposeError, "db_1 is unhealthy"):
                        await self.run_attached(
                            "service_healthy",
                            "running",
                            "unhealthy",
                            abort_on_exit=abort_on_exit,
                            abort_on_failure=abort_on_failure,
                            no_attach=no_attach,
                        )
