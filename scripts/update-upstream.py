"""Open one reviewable PR per stable upstream release; never merge or deploy it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

UPSTREAM = "remnawave/subscription-page"
CONFLICT_REPORT = Path(".github/x-out-upstream-conflict.md")
IMAGE_PATTERN = r"(?m)^ARG UPSTREAM_IMAGE=remnawave/subscription-page:([^@\s]+)@sha256:[a-f0-9]{64}$"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def output(*args: str) -> str:
    return run(*args).stdout.strip()


def version(tag: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable semantic version tag")
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def current_version(dockerfile: str) -> str:
    match = re.search(IMAGE_PATTERN, dockerfile)
    if not match:
        raise ValueError("Cannot find the pinned upstream image in Dockerfile.x-out")
    return match[1]


def latest_stable_tag(refs: str) -> str:
    tags = [line.split("refs/tags/", 1)[1] for line in refs.splitlines()
            if "refs/tags/" in line]
    stable = [tag for tag in tags if re.fullmatch(r"v?\d+\.\d+\.\d+", tag)]
    if not stable:
        raise ValueError("Upstream has no stable version tags")
    return max(stable, key=version)


def pin_image(dockerfile: str, tag: str, digest: str) -> str:
    version(tag)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Invalid upstream image digest")
    current_version(dockerfile)
    updated = re.sub(
        IMAGE_PATTERN,
        f"ARG UPSTREAM_IMAGE={UPSTREAM}:{tag}@{digest}",
        dockerfile,
    )
    updated, count = re.subn(
        r'io\.out\.upstream-version="[^"]+"',
        f'io.out.upstream-version="{tag}"',
        updated,
    )
    if count != 1:
        raise ValueError("Cannot find the upstream version label")
    return updated


def prepare_branch(tag: str, digest: str, branch: str) -> list[str]:
    """Merge in a clean checkout; a conflict becomes a draft PR with a report."""
    version(tag)
    if output("git", "status", "--porcelain"):
        raise ValueError("Updater requires a clean checkout")
    run("git", "switch", "-c", branch)
    merged = run("git", "merge", "--no-ff", "--no-commit", "refs/x-out/upstream-release", check=False)
    if merged.returncode:
        conflicts = output("git", "diff", "--name-only", "--diff-filter=U").splitlines()
        run("git", "merge", "--abort", check=False)
        if not conflicts:
            raise RuntimeError("Upstream merge failed without file conflicts")
        CONFLICT_REPORT.parent.mkdir(parents=True, exist_ok=True)
        CONFLICT_REPORT.write_text(
            f"# Remnawave {tag}: требуется ручное слияние\n\n"
            "Автоматическое слияние отменено. Код upstream ещё не добавлен.\n"
            "Этот draft PR нельзя принимать как готовое обновление.\n\n"
            + "\n".join(f"- `{name}`" for name in conflicts)
            + f"\n\nВ этой ветке выполните `git fetch https://github.com/{UPSTREAM}.git "
            f"refs/tags/{tag}` и `git merge FETCH_HEAD`, разрешите конфликты.\n"
            f"Обновите Dockerfile.x-out до `{UPSTREAM}:{tag}@{digest}` и его label.\n"
            "Удалите этот файл, выполните проверки и переведите PR из draft в ready.\n",
            encoding="utf-8",
        )
        run("git", "add", str(CONFLICT_REPORT))
        run("git", "commit", "-m", f"Зафиксировать конфликты обновления Remnawave до {tag}")
        return conflicts
    dockerfile = Path("Dockerfile.x-out")
    dockerfile.write_text(pin_image(dockerfile.read_text(), tag, digest), encoding="utf-8")
    run("git", "add", "Dockerfile.x-out")
    run("git", "commit", "-m", f"Обновить Remnawave subscription-page до {tag}")
    return []


def main() -> None:
    repository = os.environ["UPDATE_REPOSITORY"]
    base = os.environ["UPDATE_BASE_BRANCH"]
    if repository != "ckeiituk/subscription-page":
        raise ValueError("Updater is restricted to the configured fork")
    # Upstream publishes Docker images from tags, without GitHub Releases.
    tag = latest_stable_tag(output(
        "git", "ls-remote", "--tags", "--refs", f"https://github.com/{UPSTREAM}.git",
    ))
    current = current_version(Path("Dockerfile.x-out").read_text())
    if version(tag) <= version(current):
        print(f"No newer stable release: installed source {current}, latest {tag}.")
        return
    branch = f"out/upstream-{tag}"
    prs = json.loads(output(
        "gh", "pr", "list", "--repo", repository, "--head", branch,
        "--base", base, "--state", "all", "--json", "number,state,url",
    ))
    if prs:
        pr = prs[0]
        print(f"Release PR already exists ({pr['state']}): {pr['url']}")
        if pr["state"] == "OPEN":
            ensure_checks(repository, branch)
        return
    remote_ref = output("git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote_ref:
        # Resume after a failed PR API call; never overwrite somebody's fixes.
        run("git", "fetch", "origin", f"refs/heads/{branch}")
        run("git", "switch", "--detach", "FETCH_HEAD")
    else:
        inspected = output("docker", "buildx", "imagetools", "inspect", f"{UPSTREAM}:{tag}")
        match = re.search(r"(?m)^Digest:\s+(sha256:[a-f0-9]{64})\s*$", inspected)
        if not match:
            raise ValueError("Released Docker image has no valid digest; retry after publication")
        run("git", "fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
            f"+refs/tags/{tag}:refs/x-out/upstream-release")
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        prepare_branch(tag, match[1], branch)
        run("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    blocked = CONFLICT_REPORT.exists()
    body = (
        f"Обновление upstream [{tag}](https://github.com/{UPSTREAM}/tree/{tag}).\n\n"
        + ("Слияние остановлено из-за конфликтов. В PR добавлен только отчёт "
           f"`{CONFLICT_REPORT}`; исправления upstream ещё не влиты. Нужна ручная доработка.\n\n"
           if blocked else
           "Влиты изменения релиза и обновлены версия и digest официального образа "
           "в Dockerfile.x-out. Расширение x-out сохранено.\n\n")
        + "Workflow `Check x-out` запускается явно: тесты, TypeScript, сборка и проверка "
        "контейнера. Перед принятием проверьте его результат и изменения upstream.\n\n"
        "Автоматического merge, публикации образа и установки на сервер нет."
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8") as file:
        file.write(body)
        file.flush()
        args = ["gh", "pr", "create", "--repo", repository, "--base", base,
                "--head", branch, "--title", f"Обновить Remnawave до {tag}", "--body-file", file.name]
        if blocked:
            args.append("--draft")
        print(output(*args))
    ensure_checks(repository, branch)


def ensure_checks(repository: str, branch: str) -> None:
    # Token-created push/PR events do not guarantee an automatic CI run.
    runs = json.loads(output(
        "gh", "run", "list", "--repo", repository, "--workflow", "x-out-check.yml",
        "--branch", branch, "--event", "workflow_dispatch", "--limit", "1", "--json", "databaseId",
    ))
    if not runs:
        run("gh", "workflow", "run", "x-out-check.yml", "--repo", repository, "--ref", branch)
        print(f"Started Check x-out for {branch}.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # No environment dump or command output that might include credentials.
        raise SystemExit(f"{error.cmd[0]} failed (exit {error.returncode}); no deployment performed") from None
