# SPDX-License-Identifier: GPL-2.0

import json
import os
import unittest
from datetime import timedelta
from io import BytesIO
from typing import Final
from typing import Optional

from tests.integration.test_utils import EnsureHealthcheckRun
from tests.integration.test_utils import ExecutionTime
from tests.integration.test_utils import RunSubprocessMixin
from tests.integration.test_utils import podman_compose_path
from tests.integration.test_utils import test_path

EXECUTION_TIMEOUT: Final[float] = 30


def compose_yaml_path() -> str:
    return os.path.join(test_path(), "wait", "docker-compose.yml")


def compose_fail_yaml_path() -> str:
    return os.path.join(test_path(), "wait", "docker-compose-fail.yml")


class TestComposeWait(unittest.TestCase, RunSubprocessMixin):
    def setUp(self) -> None:
        # build the test image before starting the tests
        # this is not possible in setUpClass method, because the run_subprocess_* methods are not
        # available as classmethods
        self.run_subprocess_assert_returncode([
            podman_compose_path(),
            "-f",
            compose_yaml_path(),
            "build",
        ])

    def _get_health_status(self, container_name: str) -> str:
        output, _ = self.run_subprocess_assert_returncode([
            "podman",
            "inspect",
            container_name,
        ])
        inspect = json.load(BytesIO(output))

        self.assertEqual(len(inspect), 1)
        self.assertIn("State", inspect[0])
        self.assertIn("Health", inspect[0]["State"])
        self.assertIn("Status", inspect[0]["State"]["Health"])

        return inspect[0]["State"]["Health"]["Status"]

    def _is_running(self, container_name: str) -> bool:
        output, _ = self.run_subprocess_assert_returncode([
            "podman",
            "inspect",
            container_name,
        ])
        inspect = json.load(BytesIO(output))

        self.assertEqual(len(inspect), 1)
        self.assertIn("State", inspect[0])
        self.assertIn("Running", inspect[0]["State"])
        return inspect[0]["State"]["Running"]

    def _get_state(self, container_name: str) -> str:
        output, _ = self.run_subprocess_assert_returncode(["podman", "inspect", container_name])
        inspect = json.load(BytesIO(output))
        return inspect[0]["State"]["Status"]

    def _compose_down(self, compose_path: Optional[str] = None) -> None:
        self.run_subprocess_assert_returncode([
            podman_compose_path(),
            "-f",
            compose_path or compose_yaml_path(),
            "down",
            "-t",
            "0",
        ])

    def test_without_wait(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_app_health_1"
            ):
                # the execution time of this command must be not more then 10 seconds
                # otherwise this test case makes no sense
                with ExecutionTime(max_execution_time=timedelta(seconds=10)):
                    self.run_subprocess_assert_returncode([
                        podman_compose_path(),
                        "-f",
                        compose_yaml_path(),
                        "up",
                        "-d",
                    ])

                output, _ = self.run_subprocess_assert_returncode([
                    podman_compose_path(),
                    "-f",
                    compose_yaml_path(),
                    "ps",
                ])
                self.assertIn(b"wait_app_health_1", output)
                self.assertIn(b"wait_app_1", output)

                self.assertTrue(self._is_running("wait_app_1"))

                health = self._get_health_status("wait_app_health_1")
                self.assertEqual(health, "starting")
        finally:
            self._compose_down()

    def test_wait(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_app_health_1"
            ):
                # the execution time of this command must be at least 10 seconds,
                # because of the sleep command in entrypoint.sh
                with ExecutionTime(min_execution_time=timedelta(seconds=10)):
                    self.run_subprocess_assert_returncode(
                        [
                            podman_compose_path(),
                            "-f",
                            compose_yaml_path(),
                            "up",
                            "--wait",
                        ],
                        timeout=EXECUTION_TIMEOUT,
                    )

                output, _ = self.run_subprocess_assert_returncode([
                    podman_compose_path(),
                    "-f",
                    compose_yaml_path(),
                    "ps",
                ])
                self.assertIn(b"wait_app_health_1", output)
                self.assertIn(b"wait_app_1", output)

                self.assertTrue(self._is_running("wait_app_1"))

                health = self._get_health_status("wait_app_health_1")
                self.assertEqual(health, "healthy")
        finally:
            self._compose_down()

    def test_wait_with_timeout(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_app_health_1"
            ):
                # the execution time of this command must be between 5 and 10 seconds
                with ExecutionTime(
                    min_execution_time=timedelta(seconds=5),
                    max_execution_time=timedelta(seconds=10),
                ):
                    _, err, returncode = self.run_subprocess(
                        [
                            podman_compose_path(),
                            "-f",
                            compose_yaml_path(),
                            "up",
                            "-d",
                            "--wait",
                            "--wait-timeout",
                            "5",
                        ],
                        timeout=EXECUTION_TIMEOUT,
                    )
                    self.assertNotEqual(returncode, 0)
                    self.assertIn(b"timeout waiting for services", err)
                    self.assertNotIn(b"Traceback", err)

                output, _ = self.run_subprocess_assert_returncode([
                    podman_compose_path(),
                    "-f",
                    compose_yaml_path(),
                    "ps",
                ])
                self.assertIn(b"wait_app_health_1", output)
                self.assertIn(b"wait_app_1", output)

                self.assertTrue(self._is_running("wait_app_1"))

                health = self._get_health_status("wait_app_health_1")
                self.assertEqual(health, "starting")
        finally:
            self._compose_down()

    def test_wait_with_start(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_app_health_1"
            ):
                # the execution time of this command must be not more then 10 seconds
                # otherwise this test case makes no sense
                with ExecutionTime(max_execution_time=timedelta(seconds=10)):
                    # podman-compose create does not exist
                    # therefore bring the containers up and kill them immediately again
                    self.run_subprocess_assert_returncode([
                        podman_compose_path(),
                        "-f",
                        compose_yaml_path(),
                        "up",
                        "-d",
                    ])
                    self.run_subprocess_assert_returncode([
                        podman_compose_path(),
                        "-f",
                        compose_yaml_path(),
                        "kill",
                        "--all",
                    ])

                output, _ = self.run_subprocess_assert_returncode([
                    podman_compose_path(),
                    "-f",
                    compose_yaml_path(),
                    "ps",
                ])
                self.assertIn(b"wait_app_health_1", output)
                self.assertIn(b"wait_app_1", output)

                self.assertFalse(self._is_running("wait_app_health_1"))
                self.assertFalse(self._is_running("wait_app_1"))

                # the execution time of this command must be at least 10 seconds,
                # because of the sleep command in entrypoint.sh
                with ExecutionTime(min_execution_time=timedelta(seconds=10)):
                    self.run_subprocess_assert_returncode(
                        [
                            podman_compose_path(),
                            "-f",
                            compose_yaml_path(),
                            "start",
                            "--wait",
                        ],
                        timeout=EXECUTION_TIMEOUT,
                    )

                self.assertTrue(self._is_running("wait_app_1"))

                health = self._get_health_status("wait_app_health_1")
                self.assertEqual(health, "healthy")
        finally:
            self._compose_down()

    def test_wait_fails_when_service_exits(self) -> None:
        try:
            _, err, returncode = self.run_subprocess(
                [
                    podman_compose_path(),
                    "-f",
                    compose_fail_yaml_path(),
                    "up",
                    "--wait",
                    "exit_zero",
                ],
                timeout=EXECUTION_TIMEOUT,
            )

            self.assertNotEqual(returncode, 0)
            self.assertIn(b"exited with code 0", err)
            self.assertNotIn(b"Traceback", err)
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_wait_fails_when_service_is_unhealthy(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_unhealthy_1"
            ):
                _, err, returncode = self.run_subprocess(
                    [
                        podman_compose_path(),
                        "-f",
                        compose_fail_yaml_path(),
                        "up",
                        "--wait",
                        "--wait-timeout",
                        "10",
                        "unhealthy",
                    ],
                    timeout=EXECUTION_TIMEOUT,
                )

            self.assertNotEqual(returncode, 0)
            self.assertIn(b"is unhealthy", err)
            self.assertNotIn(b"Traceback", err)
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_unhealthy_dependency_prevents_dependent_start(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_unhealthy_1"
            ):
                _, err, returncode = self.run_subprocess(
                    [
                        podman_compose_path(),
                        "-f",
                        compose_fail_yaml_path(),
                        "up",
                        "--wait",
                        "--wait-timeout",
                        "10",
                        "dependent",
                    ],
                    timeout=EXECUTION_TIMEOUT,
                )

            self.assertNotEqual(returncode, 0)
            self.assertIn(b"is unhealthy", err)
            self.assertEqual(self._get_state("wait_dependent_1"), "created")
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_attached_unhealthy_dependency_returns_error(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_unhealthy_1"
            ):
                _, err, returncode = self.run_subprocess(
                    [
                        podman_compose_path(),
                        "-f",
                        compose_fail_yaml_path(),
                        "up",
                        "dependent",
                    ],
                    timeout=EXECUTION_TIMEOUT,
                )

            self.assertNotEqual(returncode, 0)
            self.assertIn(b"is unhealthy", err)
            self.assertNotIn(b"Task exception was never retrieved", err)
            self.assertNotIn(b"Traceback", err)
            self.assertEqual(self._get_state("wait_dependent_1"), "created")
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_wait_timeout_includes_dependency_wait(self) -> None:
        try:
            _, err, returncode = self.run_subprocess(
                [
                    podman_compose_path(),
                    "-f",
                    compose_fail_yaml_path(),
                    "up",
                    "--wait",
                    "--wait-timeout",
                    "1",
                    "slow_dependent",
                ],
                timeout=EXECUTION_TIMEOUT,
            )

            self.assertNotEqual(returncode, 0)
            self.assertIn(b"timeout waiting for dependencies", err)
            self.assertEqual(self._get_state("wait_slow_dependent_1"), "created")
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_healthy_dependency_starts_dependent(self) -> None:
        try:
            with EnsureHealthcheckRun(
                runner=self, test_case=self, container_name="wait_slow_health_1"
            ):
                self.run_subprocess_assert_returncode(
                    [
                        podman_compose_path(),
                        "-f",
                        compose_fail_yaml_path(),
                        "up",
                        "--wait",
                        "--wait-timeout",
                        "20",
                        "slow_dependent",
                    ],
                    timeout=EXECUTION_TIMEOUT,
                )

            self.assertTrue(self._is_running("wait_slow_health_1"))
            self.assertTrue(self._is_running("wait_slow_dependent_1"))
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_completed_dependency_satisfies_final_wait(self) -> None:
        try:
            self.run_subprocess_assert_returncode(
                [
                    podman_compose_path(),
                    "-f",
                    compose_fail_yaml_path(),
                    "up",
                    "--wait",
                    "--wait-timeout",
                    "5",
                    "after_migration",
                ],
                timeout=EXECUTION_TIMEOUT,
            )

            self.assertEqual(self._get_state("wait_migration_1"), "exited")
            self.assertTrue(self._is_running("wait_after_migration_1"))
        finally:
            self._compose_down(compose_fail_yaml_path())

    def test_targeted_up_wait_ignores_unselected_service(self) -> None:
        try:
            with ExecutionTime(max_execution_time=timedelta(seconds=5)):
                self.run_subprocess_assert_returncode(
                    [podman_compose_path(), "-f", compose_yaml_path(), "up", "--wait", "app"],
                    timeout=EXECUTION_TIMEOUT,
                )

            _, _, exists_returncode = self.run_subprocess([
                "podman",
                "container",
                "exists",
                "wait_app_health_1",
            ])
            self.assertNotEqual(exists_returncode, 0)
        finally:
            self._compose_down()

    def test_targeted_start_wait_ignores_unselected_service(self) -> None:
        try:
            self.run_subprocess_assert_returncode([
                podman_compose_path(),
                "-f",
                compose_yaml_path(),
                "up",
                "--detach",
            ])
            self.run_subprocess_assert_returncode([
                "podman",
                "stop",
                "--time",
                "0",
                "wait_app_health_1",
            ])

            with ExecutionTime(max_execution_time=timedelta(seconds=5)):
                self.run_subprocess_assert_returncode(
                    [
                        podman_compose_path(),
                        "-f",
                        compose_yaml_path(),
                        "start",
                        "--wait",
                        "app",
                    ],
                    timeout=EXECUTION_TIMEOUT,
                )

            self.assertEqual(self._get_state("wait_app_health_1"), "exited")
        finally:
            self._compose_down()
