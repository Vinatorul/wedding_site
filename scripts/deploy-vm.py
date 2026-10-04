#!/usr/bin/env python3
"""Publish the invitation and SQLite RSVP behind an existing Caddy proxy."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

IMAGE = "wedding-site-app"
APP_PORT = 8000
DATA_UID = 10001
MOUNT_ROOT = "/srv/wedding-site"
OWNER_LABEL = "me.wedding-site.managed"


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="Domain without scheme/path")
    parser.add_argument(
        "--network", required=True, help="Existing proxy Docker network"
    )
    parser.add_argument(
        "--proxy-container", required=True, help="Existing Caddy container"
    )
    parser.add_argument(
        "--caddyfile", required=True, type=Path, help="Caddyfile on the VM"
    )
    parser.add_argument("--deploy-root", type=Path, default=Path("/srv/wedding-site"))
    parser.add_argument("--container", default="wedding-site-web")
    parser.add_argument("--rover-upstream", help="Game Docker container:port")
    parser.add_argument("--rover-network", help="Existing game Docker network")
    return parser.parse_args()


def docker(*args, capture=False):
    return subprocess.run(
        ["docker", *args],
        check=True,
        text=True,
        timeout=180,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def validate_inputs(args, repo):
    validate_rover_settings(args.rover_upstream, args.rover_network)
    args.domain = args.domain.lower()
    pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    if len(args.domain) > 253 or not re.fullmatch(pattern, args.domain):
        raise ValueError("Передай домен без https://, порта и пути.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.container):
        raise ValueError("Некорректное имя контейнера сайта.")
    if args.container == args.proxy_container:
        raise ValueError("Контейнер сайта и общий прокси должны иметь разные имена.")
    args.deploy_root = args.deploy_root.resolve()
    args.caddyfile = args.caddyfile.resolve()
    if args.deploy_root == Path("/") or args.deploy_root.is_relative_to(repo):
        raise ValueError("Выбери отдельный каталог деплоя вне репозитория.")
    if not (repo / "dist/index.html").is_file() or not args.caddyfile.is_file():
        raise ValueError("Не найден dist/index.html или указанный Caddyfile.")
    current = args.deploy_root / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError(
            "current уже является обычным файлом/каталогом: выбери другой --deploy-root."
        )


def validate_rover_settings(upstream, network):
    if (upstream is None) != (network is None):
        raise ValueError("Передай --rover-upstream и --rover-network вместе.")
    if upstream is None and network is None:
        return
    match = re.fullmatch(r"([a-zA-Z0-9][a-zA-Z0-9_.-]*):([0-9]+)", upstream)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ValueError("Передай --rover-upstream как имя-контейнера:порт (1–65535).")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", network):
        raise ValueError("Некорректное имя Docker-сети игры.")


def rover_settings(args, managed):
    if "/rover" not in managed and "# rover-network:" not in managed:
        return args.rover_upstream, args.rover_network
    networks = re.findall(r"^\s*# rover-network: (\S+)[ \t]*$", managed, re.M)
    upstreams = re.findall(
        r"^\s*handle_path /rover/\* \{\s*\n"
        r"[ \t]*reverse_proxy (\S+)[ \t]*\n[ \t]*\}",
        managed,
        re.M,
    )
    if len(networks) != 1 or len(upstreams) != 1:
        raise ValueError(
            "Не удалось прочитать прежний маршрут /rover/ в блоке сайта. Проверь Caddyfile."
        )
    validate_rover_settings(upstreams[0], networks[0])
    if args.rover_upstream is not None:
        return args.rover_upstream, args.rover_network
    return upstreams[0], networks[0]


def site_routes(args):
    if args.rover_upstream is None:
        return f"    reverse_proxy {args.container}:{APP_PORT}\n"
    return (
        f"    # rover-network: {args.rover_network}\n"
        "    redir /rover /rover/?{query} 308\n"
        "    handle_path /rover/* {\n"
        f"        reverse_proxy {args.rover_upstream}\n"
        "    }\n"
        "    handle {\n"
        f"        reverse_proxy {args.container}:{APP_PORT}\n"
        "    }\n"
    )


def check_rover_network(args, proxy):
    if (
        args.rover_network
        and args.rover_network not in proxy["NetworkSettings"]["Networks"]
    ):
        raise ValueError(
            "Общий Caddy не подключён к Docker-сети игры. Подключи сеть до деплоя."
        )


def inspect_container(name):
    result = subprocess.run(
        ["docker", "container", "inspect", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise ValueError(result.stderr.strip())
    return json.loads(result.stdout)[0]


def proxy_config_path(args, proxy):
    if not proxy or not proxy["State"]["Running"]:
        raise ValueError("Указанный общий Caddy-контейнер не запущен.")
    if args.network not in proxy["NetworkSettings"]["Networks"]:
        raise ValueError("Общий Caddy не подключён к указанной Docker-сети.")
    for mount in proxy.get("Mounts", []):
        if mount["Type"] != "bind":
            continue
        source = Path(mount["Source"]).resolve()
        if args.caddyfile.is_relative_to(source):
            relative = args.caddyfile.relative_to(source)
            return str(PurePosixPath(mount["Destination"]) / relative)
    raise ValueError("Caddyfile не смонтирован в этот прокси; проверь --caddyfile.")


def check_site_container(args, container):
    if container is None:
        return
    if (container["Config"].get("Labels") or {}).get(OWNER_LABEL) != "true":
        raise ValueError(
            "Это имя уже занято другим контейнером. Укажи другой --container."
        )
    mounts = container.get("Mounts", [])
    expected = any(
        mount["Type"] == "bind"
        and mount["Source"] == str(args.deploy_root)
        and mount["Destination"] == MOUNT_ROOT
        and not mount["RW"]
        for mount in mounts
    )
    if not expected or args.network not in container["NetworkSettings"]["Networks"]:
        raise ValueError(
            "Каталог/сеть существующего контейнера отличаются. Используй прежние параметры."
        )
    if container["HostConfig"].get("PortBindings"):
        raise ValueError("У контейнера сайта обнаружены опубликованные порты.")
    check_data_mount(args, mounts)


def check_data_mount(args, mounts):
    for mount in mounts:
        if mount["Destination"] != "/data":
            continue
        if (
            mount["Type"] != "bind"
            or mount["Source"] != str(args.deploy_root / "data")
            or not mount["RW"]
        ):
            raise ValueError(
                "Каталог базы существующего контейнера отличается. Используй прежние параметры."
            )


def new_proxy_config(args, original):
    begin = f"# BEGIN {args.container}"
    end = f"# END {args.container}"
    if original.count(begin) != original.count(end) or original.count(begin) > 1:
        raise ValueError("Повреждены метки блока сайта в Caddyfile.")
    pattern = re.compile(rf"^{re.escape(begin)}\n.*?^{re.escape(end)}\n?", re.M | re.S)
    managed = pattern.search(original)
    args.rover_upstream, args.rover_network = rover_settings(
        args, managed[0] if managed else ""
    )
    base = pattern.sub("", original)
    if begin in base or end in base:
        raise ValueError("Не удалось найти границы блока сайта в Caddyfile.")
    if re.search(rf"(?<![\w.-]){re.escape(args.domain)}(?![\w.-])", base, re.I):
        raise ValueError(
            "Домен уже задан вне управляемого блока. Проверь существующую настройку."
        )
    block = f"{begin}\n{args.domain} {{\n{site_routes(args)}}}\n{end}\n"
    return base.rstrip() + "\n\n" + block


def create_release(repo, root):
    releases = root / "releases"
    new_root = not root.exists()
    new_releases = not releases.exists()
    releases.mkdir(parents=True, exist_ok=True)
    if new_root:
        root.chmod(0o755)
    if new_releases:
        releases.chmod(0o755)
    release = Path(
        tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=releases)
    )
    shutil.copytree(repo / "dist", release, dirs_exist_ok=True)
    release.chmod(0o755)
    for entry in release.rglob("*"):
        entry.chmod(0o755 if entry.is_dir() else 0o644)
    return release


def switch_release(root, target):
    temporary = root / f".current-{os.getpid()}"
    temporary.symlink_to(target)
    try:
        os.replace(temporary, root / "current")
    finally:
        temporary.unlink(missing_ok=True)


def build_application(repo, release):
    image = f"{IMAGE}:{release.name}"
    docker("build", "--tag", image, str(repo / "backend"))
    return image


def prepare_data(root):
    data = root / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    data.chmod(0o700)
    os.chown(data, DATA_UID, DATA_UID)


def run_application(args, image):
    docker(
        "run",
        "-d",
        "--name",
        args.container,
        "--restart",
        "unless-stopped",
        "--network",
        args.network,
        "--label",
        f"{OWNER_LABEL}=true",
        "--mount",
        f"type=bind,src={args.deploy_root},dst={MOUNT_ROOT},readonly",
        "--mount",
        f"type=bind,src={args.deploy_root / 'data'},dst=/data",
        image,
    )


def replace_application(args, existing, image, backup):
    if existing:
        if existing["State"]["Running"]:
            docker("stop", args.container)
        docker("rename", args.container, backup)
    run_application(args, image)


def restore_saved_container(args, existing, backup):
    saved = inspect_container(backup)
    if saved is not None:
        check_site_container(args, saved)
        docker("rename", backup, args.container)
    if existing and existing["State"]["Running"]:
        current = inspect_container(args.container)
        check_site_container(args, current)
        if current and not current["State"]["Running"]:
            docker("start", args.container)


def restore_application(args, existing, image, backup):
    try:
        current = inspect_container(args.container)
        if current and current["Config"].get("Image") == image:
            check_site_container(args, current)
            docker("rm", "--force", args.container)
        if existing:
            restore_saved_container(args, existing, backup)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(
            f"Не удалось восстановить контейнер приглашения: {error}", file=sys.stderr
        )


def remove_previous_container(args, backup):
    try:
        previous = inspect_container(backup)
        if previous is not None:
            check_site_container(args, previous)
            docker("rm", backup)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(
            f"Прежний контейнер остался для ручной проверки: {error}", file=sys.stderr
        )


def check_http(args):
    for _ in range(10):
        result = subprocess.run(
            [
                "docker",
                "exec",
                args.container,
                "python",
                "-c",
                "import urllib.request; "
                f"urllib.request.urlopen('http://127.0.0.1:{APP_PORT}/api/health', timeout=3)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise ValueError("Контейнер не отвечает. Проверь docker logs для контейнера сайта.")


def write_in_place(path, text):
    # Preserve the inode: the shared proxy may bind-mount this individual file.
    with path.open("w", encoding="utf-8") as config:
        config.write(text)


def run_caddy(args, operation, inside_path):
    docker(
        "exec",
        args.proxy_container,
        "caddy",
        operation,
        "--config",
        inside_path,
        "--adapter",
        "caddyfile",
    )


def restore_proxy_config(args, inside_path, original):
    try:
        write_in_place(args.caddyfile, original)
        run_caddy(args, "reload", inside_path)
    except (OSError, subprocess.SubprocessError) as error:
        print(
            f"Не удалось восстановить Caddy: {error}. Проверь сохранённую копию конфига.",
            file=sys.stderr,
        )


def apply_proxy_config(args, inside_path, original, updated, release):
    if original == updated:
        return
    backup_dir = args.deploy_root / "config-backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup = backup_dir / f"{release.name}.caddy"
    backup.write_text(original, encoding="utf-8")
    backup.chmod(0o600)
    try:
        write_in_place(args.caddyfile, updated)
        run_caddy(args, "validate", inside_path)
        run_caddy(args, "reload", inside_path)
    except (OSError, subprocess.SubprocessError):
        restore_proxy_config(args, inside_path, original)
        raise
    print(f"Сохранена копия прежнего Caddyfile: {backup}")


def deploy(args, repo):
    validate_inputs(args, repo)
    docker("info", capture=True)
    docker("network", "inspect", args.network, capture=True)
    proxy = inspect_container(args.proxy_container)
    inside_path = proxy_config_path(args, proxy)
    existing = inspect_container(args.container)
    check_site_container(args, existing)
    original = args.caddyfile.read_text(encoding="utf-8")
    updated = new_proxy_config(args, original)
    check_rover_network(args, proxy)
    release = create_release(repo, args.deploy_root)
    image = build_application(repo, release)
    prepare_data(args.deploy_root)
    deploy_release(args, existing, inside_path, original, updated, release, image)
    print(f"Файлы сайта и настройка Caddy обновлены: https://{args.domain}")
    print(f"Релиз: {release}; контейнер: {args.container}")


def restore_release(root, previous):
    if previous is None:
        (root / "current").unlink(missing_ok=True)
    else:
        switch_release(root, previous)


def deploy_release(args, existing, inside_path, original, updated, release, image):
    current = args.deploy_root / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    backup = f"{args.container}-previous-{release.name}"
    if inspect_container(backup) is not None:
        raise ValueError("Имя резервного контейнера уже занято; запусти деплой снова.")
    switch_release(args.deploy_root, f"releases/{release.name}")
    try:
        replace_application(args, existing, image, backup)
        check_http(args)
        apply_proxy_config(args, inside_path, original, updated, release)
    except (ValueError, OSError, subprocess.SubprocessError):
        restore_release(args.deploy_root, previous)
        restore_application(args, existing, image, backup)
        raise
    if existing:
        remove_previous_container(args, backup)


if __name__ == "__main__":
    try:
        deploy(arguments(), Path(__file__).resolve().parent.parent)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Ошибка деплоя: {error}", file=sys.stderr)
        sys.exit(1)
