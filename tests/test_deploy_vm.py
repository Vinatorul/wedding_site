import argparse
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-vm.py"
SPEC = importlib.util.spec_from_file_location("deploy_vm", SCRIPT)
deploy_vm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_vm)
OTHER_SITE = 'other.example.com {\n    respond "another site"\n}\n'


def site_container(args):
    return {
        "Config": {"Labels": {deploy_vm.OWNER_LABEL: "true"}},
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {args.network: {}}},
        "HostConfig": {"PortBindings": {}},
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(args.deploy_root),
                "Destination": deploy_vm.MOUNT_ROOT,
                "RW": False,
            }
        ],
    }


class DockerStub:
    def __init__(self, args):
        self.args = args
        self.commands = []
        self.caddy_calls = []
        self.failures = {}
        self.containers = {
            args.proxy_container: {
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {args.network: {}}},
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(args.caddyfile),
                        "Destination": "/etc/caddy/Caddyfile",
                        "RW": True,
                    }
                ],
            }
        }

    def __call__(self, command, **kwargs):
        if command[0] != "docker":
            raise AssertionError(f"Unexpected command: {command}")
        self.commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return self.inspect(command)
        if command[1] == "run":
            self.containers[self.args.container] = site_container(self.args)
        if command[1] == "start":
            self.containers[command[2]]["State"]["Running"] = True
        if command[1] == "exec" and command[3] == "caddy":
            return self.caddy(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    def inspect(self, command):
        container = self.containers.get(command[-1])
        if container is None:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="Error: No such container"
            )
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps([container]), stderr=""
        )

    def caddy(self, command):
        operation = command[4]
        current = self.args.deploy_root / "current"
        config = self.args.caddyfile.read_text(encoding="utf-8")
        self.caddy_calls.append((operation, config, os.readlink(current)))
        if self.failures.get(operation, 0):
            self.failures[operation] -= 1
            raise subprocess.CalledProcessError(
                1, command, stderr=f"{operation} failure {len(self.caddy_calls)}"
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.dist = self.repo / "dist"
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text("first site", encoding="utf-8")
        (self.dist / "assets").mkdir()
        (self.dist / "assets/photo.txt").write_text("photo", encoding="utf-8")
        caddyfile = self.base / "Caddyfile"
        caddyfile.write_text(OTHER_SITE, encoding="utf-8")
        self.args = argparse.Namespace(
            domain="wedding.example.com",
            network="public-proxy",
            proxy_container="shared-caddy",
            caddyfile=caddyfile,
            deploy_root=self.base / "deployment",
            container="wedding-site-web",
            rover_upstream=None,
            rover_network=None,
        )
        self.docker = DockerStub(self.args)
        self.mock_run = patch.object(deploy_vm.subprocess, "run", self.docker)
        self.mock_run.start()
        self.addCleanup(self.mock_run.stop)

    def run_deploy(self):
        with redirect_stdout(io.StringIO()):
            deploy_vm.deploy(self.args, self.repo)

    def seed_previous_release(self):
        release = self.args.deploy_root / "releases/previous"
        release.mkdir(parents=True)
        (release / "index.html").write_text("previous site", encoding="utf-8")
        (self.args.deploy_root / "current").symlink_to("releases/previous")
        self.docker.containers[self.args.container] = site_container(self.args)
        return "releases/previous"

    def filesystem_snapshot(self):
        snapshot = {}
        for path in self.base.rglob("*"):
            metadata = path.lstat()
            if path.is_symlink():
                contents = os.readlink(path)
            elif path.is_dir():
                contents = None
            else:
                contents = path.read_bytes()
            snapshot[str(path.relative_to(self.base))] = (
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                contents,
            )
        return snapshot

    def test_first_deploy_copies_dist_and_preserves_shared_config_inode(self):
        inode = self.args.caddyfile.stat().st_ino
        self.run_deploy()
        current = self.args.deploy_root / "current"
        target = os.readlink(current)
        self.assertFalse(Path(target).is_absolute())
        self.assertEqual(Path(target).parent, Path("releases"))
        self.assertEqual((current / "index.html").read_text(), "first site")
        self.assertEqual((current / "assets/photo.txt").read_text(), "photo")
        self.assertEqual(self.args.caddyfile.stat().st_ino, inode)
        config = self.args.caddyfile.read_text()
        self.assertTrue(config.startswith(OTHER_SITE))
        self.assertIn("reverse_proxy wedding-site-web:80", config)
        self.assertEqual(config.count("# BEGIN wedding-site-web"), 1)
        backups = list((self.args.deploy_root / "config-backups").glob("*.caddy"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), OTHER_SITE)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_repeat_deploy_keeps_previous_release_and_reuses_container(self):
        self.run_deploy()
        current = self.args.deploy_root / "current"
        first_target = os.readlink(current)
        first_config = self.args.caddyfile.read_text()
        inode = self.args.caddyfile.stat().st_ino
        (self.dist / "index.html").write_text("second site", encoding="utf-8")
        self.run_deploy()
        self.assertNotEqual(os.readlink(current), first_target)
        self.assertFalse(Path(os.readlink(current)).is_absolute())
        self.assertEqual((current / "index.html").read_text(), "second site")
        self.assertEqual(
            (self.args.deploy_root / first_target / "index.html").read_text(),
            "first site",
        )
        releases = list((self.args.deploy_root / "releases").iterdir())
        self.assertEqual(len(releases), 2)
        self.assertEqual(self.args.caddyfile.read_text(), first_config)
        self.assertEqual(self.args.caddyfile.stat().st_ino, inode)
        runs = sum(command[1] == "run" for command in self.docker.commands)
        self.assertEqual(runs, 1)
        operations = [call[0] for call in self.docker.caddy_calls]
        self.assertEqual(operations, ["validate", "reload"])

    def test_new_container_uses_public_image_readonly_mount_and_no_host_ports(self):
        self.run_deploy()
        command = next(
            command for command in self.docker.commands if command[1] == "run"
        )
        self.assertIn("docker.io/library/caddy:2-alpine", command)
        self.assertEqual(command[command.index("--network") + 1], self.args.network)
        self.assertEqual(
            command[command.index("--label") + 1], f"{deploy_vm.OWNER_LABEL}=true"
        )
        self.assertEqual(
            command[command.index("--mount") + 1],
            f"type=bind,src={self.args.deploy_root},dst=/srv/wedding-site,readonly",
        )
        self.assertEqual(
            command[command.index(deploy_vm.IMAGE) + 1 :],
            [
                "caddy",
                "file-server",
                "--root",
                "/srv/wedding-site/current",
                "--listen",
                ":80",
            ],
        )
        flags = ("-p", "-P", "--publish", "--publish-all")
        self.assertFalse(any(flag in command for flag in flags))

    def test_foreign_container_is_rejected_before_changes(self):
        self.seed_previous_release()
        self.docker.containers[self.args.container]["Config"]["Labels"] = {}
        before = self.filesystem_snapshot()
        with self.assertRaisesRegex(ValueError, "занято другим контейнером"):
            self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)
        operations = [command[1] for command in self.docker.commands]
        self.assertFalse(set(operations) & {"run", "start", "exec"})

    def test_domain_collision_is_rejected_before_changes(self):
        self.args.caddyfile.write_text(
            'wedding.example.com {\n    respond "owner"\n}\n'
        )
        before = self.filesystem_snapshot()
        with self.assertRaisesRegex(ValueError, "Домен уже задан"):
            self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)
        self.assertFalse(self.args.deploy_root.exists())
        operations = [command[1] for command in self.docker.commands]
        self.assertFalse(set(operations) & {"run", "start", "exec"})

    def assert_rollback(self, operation, previous=False):
        target = self.seed_previous_release() if previous else None
        original = self.args.caddyfile.read_text()
        inode = self.args.caddyfile.stat().st_ino
        self.docker.failures[operation] = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy()
        self.assertEqual(self.args.caddyfile.read_text(), original)
        self.assertEqual(self.args.caddyfile.stat().st_ino, inode)
        current = self.args.deploy_root / "current"
        if previous:
            self.assertEqual(os.readlink(current), target)
            self.assertEqual((current / "index.html").read_text(), "previous site")
        else:
            self.assertFalse(current.is_symlink())
            self.assertFalse(current.exists())
        self.assertEqual(self.docker.caddy_calls[-1][0:2], ("reload", original))

    def test_first_validate_failure_restores_config_and_removes_current(self):
        self.assert_rollback("validate")

    def test_repeat_validate_failure_restores_config_and_current(self):
        self.assert_rollback("validate", previous=True)

    def test_first_reload_failure_restores_config_and_removes_current(self):
        self.assert_rollback("reload")

    def test_repeat_reload_failure_restores_config_and_current(self):
        self.assert_rollback("reload", previous=True)

    def test_restore_reload_failure_preserves_original_error_and_current(self):
        previous = self.seed_previous_release()
        self.docker.failures["reload"] = 2
        errors = io.StringIO()
        with redirect_stderr(errors):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                self.run_deploy()
        self.assertEqual(raised.exception.stderr, "reload failure 2")
        self.assertIn("Не удалось восстановить Caddy", errors.getvalue())
        self.assertEqual(self.args.caddyfile.read_text(), OTHER_SITE)
        self.assertEqual(os.readlink(self.args.deploy_root / "current"), previous)
        self.assertEqual(self.docker.caddy_calls[-1][0:2], ("reload", OTHER_SITE))

    def test_proxy_validation_and_reload_use_mounted_caddyfile_adapter(self):
        self.run_deploy()
        commands = [
            command
            for command in self.docker.commands
            if command[1] == "exec" and command[3] == "caddy"
        ]
        for command in commands:
            self.assertEqual(command[2], self.args.proxy_container)
            self.assertEqual(
                command[-4:],
                [
                    "--config",
                    "/etc/caddy/Caddyfile",
                    "--adapter",
                    "caddyfile",
                ],
            )

    def test_existing_deploy_root_permissions_are_preserved(self):
        self.args.deploy_root.mkdir(mode=0o700)
        self.run_deploy()
        self.assertEqual(stat.S_IMODE(self.args.deploy_root.stat().st_mode), 0o700)

    def enable_rover(self):
        self.args.rover_upstream = "rover-rally-app:8787"
        self.args.rover_network = "rover-rally"
        proxy = self.docker.containers[self.args.proxy_container]
        proxy["NetworkSettings"]["Networks"][self.args.rover_network] = {}

    def test_rover_routes_and_main_site_use_separate_handlers(self):
        self.enable_rover()
        self.args.container = "custom-wedding"
        self.run_deploy()
        config = self.args.caddyfile.read_text()
        self.assertIn("redir /rover /rover/?{query} 308", config)
        self.assertIn(
            "handle_path /rover/* {\n        reverse_proxy rover-rally-app:8787", config
        )
        self.assertIn("handle {\n        reverse_proxy custom-wedding:80", config)
        self.assertTrue(config.startswith(OTHER_SITE))
        self.assertIn("# rover-network: rover-rally", config)
        mutations = [command[1:3] for command in self.docker.commands]
        self.assertNotIn(["network", "connect"], mutations)
        self.assertFalse(
            any(command[1] == "restart" for command in self.docker.commands)
        )

    def test_repeat_without_flags_preserves_rover_and_does_not_reload(self):
        self.enable_rover()
        self.run_deploy()
        original = self.args.caddyfile.read_text()
        self.args.rover_upstream = self.args.rover_network = None
        self.run_deploy()
        self.assertEqual(self.args.caddyfile.read_text(), original)
        self.assertEqual(self.args.rover_upstream, "rover-rally-app:8787")
        self.assertEqual(self.args.rover_network, "rover-rally")
        self.assertEqual(
            [call[0] for call in self.docker.caddy_calls], ["validate", "reload"]
        )

    def test_explicit_flags_update_existing_rover_route(self):
        self.enable_rover()
        self.run_deploy()
        self.args.rover_upstream = "another-game:9000"
        self.args.rover_network = "another-network"
        proxy = self.docker.containers[self.args.proxy_container]
        proxy["NetworkSettings"]["Networks"]["another-network"] = {}
        self.run_deploy()
        config = self.args.caddyfile.read_text()
        self.assertIn("reverse_proxy another-game:9000", config)
        self.assertIn("# rover-network: another-network", config)
        self.assertNotIn("rover-rally", config)

    def test_missing_game_network_is_rejected_before_changes(self):
        self.enable_rover()
        proxy = self.docker.containers[self.args.proxy_container]
        del proxy["NetworkSettings"]["Networks"][self.args.rover_network]
        before = self.filesystem_snapshot()
        with self.assertRaisesRegex(ValueError, "не подключён к Docker-сети игры"):
            self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)
        self.assertFalse(self.args.deploy_root.exists())

    def test_saved_network_is_checked_on_repeat_without_flags(self):
        self.enable_rover()
        self.run_deploy()
        proxy = self.docker.containers[self.args.proxy_container]
        del proxy["NetworkSettings"]["Networks"][self.args.rover_network]
        self.args.rover_upstream = self.args.rover_network = None
        before = self.filesystem_snapshot()
        with self.assertRaisesRegex(ValueError, "не подключён к Docker-сети игры"):
            self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_rover_flags_must_be_supplied_together(self):
        self.enable_rover()
        pairs = [("rover:8787", None), (None, "rover-rally"), (None, "")]
        for upstream, network in pairs:
            with self.subTest(upstream=upstream, network=network):
                self.args.rover_upstream, self.args.rover_network = upstream, network
                before = self.filesystem_snapshot()
                with self.assertRaisesRegex(ValueError, "вместе"):
                    self.run_deploy()
                self.assertEqual(self.filesystem_snapshot(), before)
        self.assertFalse(self.docker.commands)

    def test_invalid_rover_network_is_rejected_before_changes(self):
        self.enable_rover()
        before = self.filesystem_snapshot()
        for network in ("", "rover-rally extra", "rover-rally\n}"):
            with self.subTest(network=network):
                self.args.rover_network = network
                with self.assertRaisesRegex(ValueError, "имя Docker-сети игры"):
                    self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)
        self.assertFalse(self.docker.commands)

    def test_invalid_rover_upstreams_are_rejected_before_changes(self):
        self.enable_rover()
        invalid = (
            "https://rover:8787",
            "rover:0",
            "rover:65536",
            "rover:80 extra",
            "rover:80\n}",
        )
        before = self.filesystem_snapshot()
        for upstream in invalid:
            with self.subTest(upstream=upstream):
                self.args.rover_upstream = upstream
                with self.assertRaisesRegex(ValueError, "имя-контейнера:порт"):
                    self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)
        self.assertFalse(self.docker.commands)

    def test_unknown_saved_rover_route_is_preserved_and_rejected(self):
        self.enable_rover()
        self.run_deploy()
        config = self.args.caddyfile.read_text().replace(
            "    # rover-network: rover-rally\n", ""
        )
        self.args.caddyfile.write_text(config)
        self.args.rover_upstream = self.args.rover_network = None
        before = self.filesystem_snapshot()
        with self.assertRaisesRegex(ValueError, "Не удалось прочитать прежний маршрут"):
            self.run_deploy()
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_rover_route_from_another_site_is_not_adopted(self):
        self.enable_rover()
        self.run_deploy()
        config = self.args.caddyfile.read_text().replace(
            "wedding.example.com", "game.example.com"
        )
        self.args.caddyfile.write_text(
            config.replace("wedding-site-web", "other-container")
        )
        self.args.rover_upstream = self.args.rover_network = None
        self.run_deploy()
        config = self.args.caddyfile.read_text()
        own = config.split("# BEGIN wedding-site-web\n", 1)[1]
        self.assertNotIn("/rover", own)
        self.assertIn("reverse_proxy wedding-site-web:80", own)

    def test_failed_rover_change_restores_previous_route_and_release(self):
        self.enable_rover()
        self.run_deploy()
        original = self.args.caddyfile.read_text()
        current = self.args.deploy_root / "current"
        previous = os.readlink(current)
        inode = self.args.caddyfile.stat().st_ino
        self.args.rover_upstream = "replacement-game:9000"
        for operation in ("validate", "reload"):
            with self.subTest(operation=operation):
                self.docker.failures[operation] = 1
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_deploy()
                self.assertEqual(self.args.caddyfile.read_text(), original)
                self.assertEqual(self.args.caddyfile.stat().st_ino, inode)
                self.assertEqual(os.readlink(current), previous)


if __name__ == "__main__":
    unittest.main()
