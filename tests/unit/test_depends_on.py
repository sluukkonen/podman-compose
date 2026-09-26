import asyncio
import json
import subprocess
import unittest
from typing import Any
from typing import Optional
from unittest import mock

from parameterized import parameterized

from podman_compose import ContainerWaitCondition
from podman_compose import PodmanComposeError
from podman_compose import ServiceDependency
from podman_compose import _container_condition_status
from podman_compose import check_dep_conditions
from podman_compose import flat_deps
from podman_compose import wait_for_container_conditions
from podman_compose import wait_for_container_running_healthy


class TestDependsOn(unittest.TestCase):
    @parameterized.expand([
        (
            {
                "service_a": {},
                "service_b": {"depends_on": {"service_a": {"condition": "healthy"}}},
                "service_c": {"depends_on": {"service_b": {"condition": "healthy"}}},
            },
            # dependencies
            {
                "service_a": set(),
                "service_b": set(["service_a"]),
                "service_c": set(["service_a", "service_b"]),
            },
            # dependents
            {
                "service_a": set(["service_b", "service_c"]),
                "service_b": set(["service_c"]),
                "service_c": set(),
            },
        ),
    ])
    def test_flat_deps(
        self,
        services: dict[str, Any],
        deps: dict[str, set[str]],
        dependents: dict[str, set[str]],
    ) -> None:
        flat_deps(services)
        self.assertEqual(
            {
                name: set([x.name for x in value.get("_deps", set())])
                for name, value in services.items()
            },
            deps,
            msg="Dependencies do not match",
        )
        self.assertEqual(
            {
                name: set([x.name for x in value.get("_dependents", set())])
                for name, value in services.items()
            },
            dependents,
            msg="Dependents do not match",
        )


class TestCheckDepConditions(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def inspect_state(
        status: str,
        *,
        name: str = "cnt_a",
        exit_code: int = 0,
        health_status: Optional[str] = None,
        healthcheck: Optional[list[str]] = None,
    ) -> bytes:
        state: dict[str, Any] = {"Status": status, "ExitCode": exit_code}
        if health_status is not None:
            state["Health"] = {"Status": health_status}
        config: dict[str, Any] = {}
        if healthcheck is not None:
            config["Healthcheck"] = {"Test": healthcheck}
        return json.dumps([{"Name": name, "State": state, "Config": config}]).encode()

    async def test_empty_deps_does_nothing(self) -> None:
        """check_dep_conditions with empty deps should return without any waits"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock()

        await check_dep_conditions(compose, set())

        compose.podman.output.assert_not_called()

    async def test_per_container_wait_single_condition(self) -> None:
        """Each container gets its own podman wait call, not batched together"""

        async def container_output(_podman_args: Any, command: str, names: list[str]) -> bytes:
            if command == "inspect":
                return self.inspect_state("running", name=names[-1])
            return b"0\n"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=container_output)
        compose.podman_version = None
        compose.container_names_by_service = {
            "srva": ["cnt_a1", "cnt_a2"],
        }
        deps = {
            ServiceDependency("srva", "service_completed_successfully"),
        }

        await check_dep_conditions(compose, deps)

        wait_calls = [c.args for c in compose.podman.output.call_args_list if c.args[1] == "wait"]
        self.assertEqual(
            wait_calls,
            [
                ([], "wait", ["--condition=stopped", "cnt_a1"]),
                ([], "wait", ["--condition=stopped", "cnt_a2"]),
            ],
        )

    async def test_per_container_wait_multiple_conditions(self) -> None:
        """Each condition's containers get individual wait calls"""

        async def container_output(_podman_args: Any, command: str, names: list[str]) -> bytes:
            if command == "inspect":
                return self.inspect_state("running", name=names[-1])
            return b"0\n"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=container_output)
        compose.podman_version = None
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
            "srvb": ["cnt_b"],
        }
        deps = {
            ServiceDependency("srva", "service_completed_successfully"),
            ServiceDependency("srvb", "service_started"),
        }

        await check_dep_conditions(compose, deps)

        wait_calls = [c.args for c in compose.podman.output.call_args_list if c.args[1] == "wait"]
        self.assertEqual(
            wait_calls,
            [
                ([], "wait", ["--condition=running", "cnt_b"]),
                ([], "wait", ["--condition=stopped", "cnt_a"]),
            ],
        )

    async def test_retry_on_error(self) -> None:
        """podman wait failure is retried (not fatal)"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock()
        compose.podman.output.side_effect = [
            self.inspect_state("running"),  # startup guard
            subprocess.CalledProcessError(1, "podman wait", b"", b"error"),
            b"0\n",  # retry: wait succeeds
        ]
        compose.podman_version = None
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
        }
        deps = {
            ServiceDependency("srva", "service_completed_successfully"),
        }

        with mock.patch("podman_compose.asyncio.sleep", new=mock.AsyncMock()):
            await check_dep_conditions(compose, deps)

        self.assertEqual(compose.podman.output.await_count, 3)

    async def test_checks_healthy_below_4_6_0(self) -> None:
        """Healthcheck condition is checked through inspect on podman < 4.6.0"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state("running", health_status="healthy")
        )
        compose.podman_version = "4.5.0"
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
        }
        deps = {
            ServiceDependency("srva", "service_healthy"),
        }

        await check_dep_conditions(compose, deps)

        compose.podman.output.assert_awaited_once_with([], "inspect", ["cnt_a"])

    async def test_checks_unhealthy_below_4_6_0(self) -> None:
        """UNHEALTHY condition is also checked on podman < 4.6.0"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state("running", health_status="unhealthy")
        )
        compose.podman_version = "4.5.0"
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
        }
        deps = {
            ServiceDependency("srva", "unhealthy"),
        }

        await check_dep_conditions(compose, deps)

        compose.podman.output.assert_awaited_once_with([], "inspect", ["cnt_a"])

    async def test_healthcheck_on_4_6_0_or_newer(self) -> None:
        """HEALTHY waits normally on podman >= 4.6.0 (not skipped)"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state("running", health_status="healthy")
        )
        compose.podman_version = "4.6.0"
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
        }
        deps = {
            ServiceDependency("srva", "service_healthy"),
        }

        await check_dep_conditions(compose, deps)

        calls = [c.args for c in compose.podman.output.call_args_list]
        self.assertEqual(
            calls,
            [
                ([], "inspect", ["cnt_a"]),
            ],
        )

    async def test_healthcheck_when_podman_version_none(self) -> None:
        """Healthcheck is checked even when the Podman version is unknown"""
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state("running", health_status="healthy")
        )
        compose.podman_version = None
        compose.container_names_by_service = {
            "srva": ["cnt_a"],
        }
        deps = {
            ServiceDependency("srva", "service_healthy"),
        }

        await check_dep_conditions(compose, deps)

        calls = [c.args for c in compose.podman.output.call_args_list]
        self.assertEqual(
            calls,
            [
                ([], "inspect", ["cnt_a"]),
            ],
        )

    async def test_lifecycle_dependency_conditions_use_event_waits(self) -> None:
        conditions = (
            "configured",
            "created",
            "exited",
            "initialized",
            "paused",
            "removing",
            "service_started",
            "stopped",
            "stopping",
        )

        for dependency_condition in conditions:
            with self.subTest(condition=dependency_condition):
                compose = mock.Mock()
                compose.podman.output = mock.AsyncMock(return_value=b"0")
                compose.container_names_by_service = {"srva": ["cnt_a"]}
                deps = {ServiceDependency("srva", dependency_condition)}

                await check_dep_conditions(compose, deps)

                podman_condition = (
                    "running" if dependency_condition == "service_started" else dependency_condition
                )
                compose.podman.output.assert_awaited_once_with(
                    [], "wait", [f"--condition={podman_condition}", "cnt_a"]
                )

    async def test_transition_wait_honors_deadline(self) -> None:
        async def slow_wait(*_args: Any) -> bytes:
            await asyncio.sleep(10)
            return b"0"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=slow_wait)
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "exited")}
        deadline = asyncio.get_running_loop().time() + 0.01

        with self.assertRaisesRegex(PodmanComposeError, "timeout waiting for dependencies"):
            await check_dep_conditions(compose, deps, deadline=deadline)

    async def test_matches_batched_inspect_results_by_name(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=b'[{"Name":"cnt_a2","State":{"Status":"exited"}},'
            b'{"Name":"cnt_a1","State":{"Status":"running"}}]'
        )

        await wait_for_container_conditions(
            compose,
            {
                "cnt_a1": ContainerWaitCondition.RUNNING,
                "cnt_a2": ContainerWaitCondition.EXITED,
            },
        )

    async def test_rechecks_ready_containers_until_all_are_ready(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            side_effect=[
                b'[{"Name":"fast","State":{"Status":"running"}},'
                b'{"Name":"slow","State":{"Status":"created"}}]',
                b'[{"Name":"fast","State":{"Status":"exited","ExitCode":1}},'
                b'{"Name":"slow","State":{"Status":"running"}}]',
            ]
        )

        with mock.patch("podman_compose.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaisesRegex(PodmanComposeError, "fast exited with code 1"):
                await wait_for_container_conditions(
                    compose,
                    {
                        "fast": ContainerWaitCondition.RUNNING,
                        "slow": ContainerWaitCondition.RUNNING,
                    },
                )

        self.assertEqual(compose.podman.output.await_count, 2)

    async def test_unhealthy_dependency_fails_immediately(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state(
                "running", health_status="unhealthy", healthcheck=["CMD", "false"]
            )
        )
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_healthy")}

        with self.assertRaisesRegex(PodmanComposeError, "cnt_a is unhealthy"):
            await check_dep_conditions(compose, deps)

        compose.podman.output.assert_awaited_once()

    async def test_healthy_dependency_requires_healthcheck(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(return_value=self.inspect_state("running"))
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_healthy")}

        with self.assertRaisesRegex(PodmanComposeError, "no healthcheck configured"):
            await check_dep_conditions(compose, deps)

    async def test_completed_dependency_latches_successful_exit(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=[self.inspect_state("running"), b"0\n"])
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}

        await check_dep_conditions(compose, deps)
        await check_dep_conditions(compose, deps)

        compose.podman.output.assert_has_awaits([
            mock.call([], "inspect", ["cnt_a"]),
            mock.call([], "wait", ["--condition=stopped", "cnt_a"]),
        ])
        self.assertEqual(compose.podman.output.await_count, 2)
        self.assertEqual(compose.completed_dependencies, {"cnt_a"})

    async def test_completed_dependency_waits_until_container_has_started(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            side_effect=[
                self.inspect_state("created"),
                self.inspect_state("running"),
                b"0\n",
            ]
        )
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}

        with mock.patch("podman_compose.asyncio.sleep", new=mock.AsyncMock()):
            await check_dep_conditions(compose, deps)

        compose.podman.output.assert_has_awaits([
            mock.call([], "inspect", ["cnt_a"]),
            mock.call([], "inspect", ["cnt_a"]),
            mock.call([], "wait", ["--condition=stopped", "cnt_a"]),
        ])

    async def test_completed_dependency_propagates_failure(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            side_effect=[self.inspect_state("exited", exit_code=17), b"17\n"]
        )
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}

        with self.assertRaisesRegex(
            PodmanComposeError, "didn't complete successfully: exit code 17"
        ):
            await check_dep_conditions(compose, deps)

    async def test_completed_dependency_failure_cancels_other_replica_waits(self) -> None:
        slow_wait_cancelled = asyncio.Event()

        async def wait_for_exit(_podman_args: object, command: str, args: list[str]) -> bytes:
            if command == "inspect":
                return self.inspect_state("running", name=args[-1])
            if args[-1] == "cnt_failed":
                return b"17\n"
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                slow_wait_cancelled.set()
                raise
            return b"0\n"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=wait_for_exit)
        compose.container_names_by_service = {"srva": ["cnt_failed", "cnt_slow"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}

        with self.assertRaisesRegex(
            PodmanComposeError, "didn't complete successfully: exit code 17"
        ):
            await check_dep_conditions(compose, deps)

        self.assertTrue(slow_wait_cancelled.is_set())

    async def test_completed_dependency_rejects_invalid_wait_output(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            side_effect=[self.inspect_state("running"), b"not-an-exit-code\n"]
        )
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}

        with self.assertRaisesRegex(PodmanComposeError, "failed to determine exit code"):
            await check_dep_conditions(compose, deps)

    async def test_completion_wait_does_not_wait_for_other_containers_to_start(self) -> None:
        fast_subscribed = asyncio.Event()
        release_slow_start = asyncio.Event()
        compose = mock.Mock()
        compose.completed_dependencies = set()
        compose.container_names_by_service = {"jobs": ["fast", "slow"]}

        async def output(_podman_args: Any, command: str, names: list[str]) -> bytes:
            name = names[-1]
            if command == "inspect":
                if name == "slow":
                    await fast_subscribed.wait()
                    # The first successful exit must already be recorded while
                    # the slow container is still starting.
                    self.assertIn("fast", compose.completed_dependencies)
                    await release_slow_start.wait()
                return self.inspect_state("running", name=name)
            if name == "fast":
                fast_subscribed.set()
            return b"0\n"

        compose.podman.output = mock.AsyncMock(side_effect=output)
        deps = {ServiceDependency("jobs", "service_completed_successfully")}
        task = asyncio.create_task(check_dep_conditions(compose, deps))
        try:
            await asyncio.wait_for(fast_subscribed.wait(), 1)
            self.assertFalse(task.done())
            release_slow_start.set()
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(compose.completed_dependencies, {"fast", "slow"})

    async def test_completion_failure_cancels_other_containers_startup_guards(self) -> None:
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()
        compose = mock.Mock()
        compose.container_names_by_service = {"jobs": ["fast", "slow"]}

        async def output(_podman_args: Any, command: str, names: list[str]) -> bytes:
            if command == "wait":
                await slow_started.wait()
                return b"17\n"
            if names[-1] == "slow":
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    slow_cancelled.set()
            return self.inspect_state("running", name=names[-1])

        compose.podman.output = mock.AsyncMock(side_effect=output)
        deps = {ServiceDependency("jobs", "service_completed_successfully")}
        with self.assertRaisesRegex(PodmanComposeError, "exit code 17"):
            await asyncio.wait_for(check_dep_conditions(compose, deps), 1)
        self.assertTrue(slow_cancelled.is_set())

    async def test_completed_dependency_startup_guard_honors_deadline(self) -> None:
        async def slow_inspect(*_args: Any) -> bytes:
            await asyncio.sleep(10)
            return b"[]"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=slow_inspect)
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_completed_successfully")}
        deadline = asyncio.get_running_loop().time() + 0.01

        with self.assertRaisesRegex(PodmanComposeError, "timeout waiting for dependencies"):
            await check_dep_conditions(compose, deps, deadline=deadline)

        compose.podman.output.assert_awaited_once_with([], "inspect", ["cnt_a"])

    async def test_final_wait_skips_latched_completed_dependencies(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            return_value=self.inspect_state("running", name="app")
        )

        await wait_for_container_running_healthy(
            compose,
            ["migration", "app"],
            completed_container_names={"migration"},
        )

        compose.podman.output.assert_awaited_once_with([], "inspect", ["app"])

    async def test_inspect_failure_is_not_retried_forever(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(
            side_effect=subprocess.CalledProcessError(1, "podman inspect")
        )
        compose.container_names_by_service = {"srva": ["cnt_a"]}
        deps = {ServiceDependency("srva", "service_healthy")}

        with self.assertRaisesRegex(PodmanComposeError, "failed to inspect containers"):
            await check_dep_conditions(compose, deps)

        compose.podman.output.assert_awaited_once()

    async def test_expired_deadline_does_not_inspect(self) -> None:
        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock()

        with self.assertRaisesRegex(PodmanComposeError, "deadline expired"):
            await wait_for_container_conditions(
                compose,
                {"cnt_a": ContainerWaitCondition.RUNNING},
                deadline=0,
                timeout_message="deadline expired",
            )

        compose.podman.output.assert_not_awaited()

    async def test_deadline_cancels_slow_inspect(self) -> None:
        async def slow_inspect(*_args: Any) -> bytes:
            await asyncio.sleep(10)
            return b"[]"

        compose = mock.Mock()
        compose.podman.output = mock.AsyncMock(side_effect=slow_inspect)
        deadline = asyncio.get_running_loop().time() + 0.01

        with self.assertRaisesRegex(PodmanComposeError, "deadline expired"):
            await wait_for_container_conditions(
                compose,
                {"cnt_a": ContainerWaitCondition.RUNNING},
                deadline=deadline,
                timeout_message="deadline expired",
            )


class TestContainerConditionStatus(unittest.TestCase):
    def test_empty_health_metadata_does_not_require_healthcheck(self) -> None:
        healthcheck: Optional[dict[str, Any]]
        for healthcheck in (None, {}, {"Test": []}, {"Test": ["NONE"]}):
            with self.subTest(healthcheck=healthcheck):
                info = {
                    "Config": {"Healthcheck": healthcheck},
                    "State": {
                        "Status": "running",
                        "Health": {"Status": "", "FailingStreak": 0, "Log": None},
                    },
                }
                self.assertEqual(
                    _container_condition_status(
                        "app", info, ContainerWaitCondition.RUNNING_OR_HEALTHY
                    ),
                    (True, None),
                )
                self.assertEqual(
                    _container_condition_status("app", info, ContainerWaitCondition.HEALTHY),
                    (False, "container app has no healthcheck configured"),
                )

    @parameterized.expand([
        ("configured_empty_status", {"Test": ["CMD", "true"]}, "", False),
        ("inherited_health_status", None, "starting", False),
        ("image_healthcheck", {"Test": ["CMD", "true"]}, "starting", False),
        ("disabled_healthcheck", {"Test": ["NONE"]}, None, True),
    ])
    def test_running_or_healthy(
        self,
        _name: str,
        healthcheck: Optional[dict[str, Any]],
        health_status: Optional[str],
        ready: bool,
    ) -> None:
        info: dict[str, Any] = {"State": {"Status": "running"}}
        if healthcheck is not None:
            info["Config"] = {"Healthcheck": healthcheck}
        if health_status is not None:
            info["State"]["Health"] = {"Status": health_status}
        self.assertEqual(
            _container_condition_status("app", info, ContainerWaitCondition.RUNNING_OR_HEALTHY),
            (ready, None),
        )

    def test_successful_early_exit_is_not_running(self) -> None:
        info = {"Config": {}, "State": {"Status": "exited", "ExitCode": 0}}

        ready, error = _container_condition_status(
            "app", info, ContainerWaitCondition.RUNNING_OR_HEALTHY
        )

        self.assertFalse(ready)
        self.assertIn("exited with code 0", error or "")
