"""Prepare stable upstream PRs and merge only their exact, successfully tested heads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

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
    keep_schedule_alive(repository, base)
    # Upstream publishes Docker images from tags, without GitHub Releases.
    tag = latest_stable_tag(output(
        "git", "ls-remote", "--tags", "--refs", f"https://github.com/{UPSTREAM}.git",
    ))
    current = current_version(Path("Dockerfile.x-out").read_text())
    if version(tag) <= version(current):
        print(f"No newer stable release: installed source {current}, latest {tag}.")
        ensure_checks(repository, base)
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
            auto_merge_pr(repository, base, pr["number"], tag)
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
        "контейнера. Успешно проверенный head автоматически сливается, если не меняет "
        "наши проверки, расширение и механизм обновления. sber отдельно проверяет CI, "
        "собирает образ и устанавливает его с проверкой и откатом."
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8") as file:
        file.write(body)
        file.flush()
        args = ["gh", "pr", "create", "--repo", repository, "--base", base,
                "--head", branch, "--title", f"Обновить Remnawave до {tag}", "--body-file", file.name]
        if blocked:
            args.append("--draft")
        print(output(*args))
    # Even conflict drafts get a visible failing check; completion triggers the
    # separate trusted updater, which applies eligibility and exact-head gates.
    ensure_checks(repository, branch)


def check_runs(repository: str, branch: str) -> list[dict]:
    return json.loads(output(
        "gh", "run", "list", "--repo", repository, "--workflow", "x-out-check.yml",
        "--branch", branch, "--limit", "30", "--json",
        "databaseId,headSha,status,conclusion,event",
    ))


def keep_schedule_alive(repository: str, base: str) -> None:
    # GitHub disables scheduled workflows in public repos after 60 days without
    # repository activity. A monthly heartbeat keeps unattended updates alive.
    last_commit = int(output("git", "log", "-1", "--format=%ct"))
    if time.time() - last_commit < 30 * 86400:
        return
    if output("git", "status", "--porcelain"):
        raise ValueError("Heartbeat requires a clean checkout")
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    heartbeat = Path(".github/x-out-updater-heartbeat")
    heartbeat.write_text(time.strftime("%Y-%m-%d", time.gmtime()) + "\n", encoding="utf-8")
    run("git", "add", str(heartbeat))
    run("git", "commit", "-m", "Поддержать автоматическую проверку обновлений")
    run("git", "push", "origin", f"HEAD:refs/heads/{base}")
    ensure_checks(repository, base)


def ensure_checks(repository: str, branch: str, sha: str | None = None) -> dict | None:
    if sha is None:
        sha = output("gh", "api", f"repos/{repository}/git/ref/heads/{branch}", "--jq", ".object.sha")
    runs = [r for r in check_runs(repository, branch)
            if r["headSha"] == sha and r["event"] in ("push", "workflow_dispatch")]
    if runs:
        return runs[0]  # A newer failure must never be hidden by an older success.
    run("gh", "workflow", "run", "x-out-check.yml", "--repo", repository, "--ref", branch)
    print(f"Started Check x-out for {branch} ({sha[:12]}).")
    return None


def protected_changes(base: str, head: str) -> list[str]:
    paths = output("git", "diff", "--name-only", base, head, "--",
                   ".github", "scripts", "deploy", "backend/test", "backend/tsconfig.json",
                   "backend/tsconfig.test.json", "backend/src/modules/subscription/x-out.ts",
                   "Dockerfile.x-out.dockerignore").splitlines()
    # The updater may change only the pinned version/digest and version label.
    before = output("git", "show", f"{base}:Dockerfile.x-out")
    after = output("git", "show", f"{head}:Dockerfile.x-out")
    normalized = pin_image(before, current_version(after),
                           re.search(r"@(sha256:[a-f0-9]{64})", after)[1])
    if normalized != after:
        paths.append("Dockerfile.x-out")
    return paths


def auto_merge_pr(repository: str, base: str, number: int, tag: str) -> None:
    pr = json.loads(output("gh", "pr", "view", str(number), "--repo", repository, "--json",
                          "author,state,isDraft,isCrossRepository,headRefName,headRefOid,baseRefName,baseRefOid"))
    if (pr["state"] != "OPEN" or pr["isDraft"] or pr["isCrossRepository"]
            or pr["author"]["login"] not in ("app/github-actions", "github-actions[bot]")
            or pr["headRefName"] != f"out/upstream-{tag}" or pr["baseRefName"] != base):
        print(f"PR #{number}: manual review required; automatic merge not eligible.")
        return
    head = pr["headRefOid"]
    run("git", "fetch", "origin", f"refs/heads/{base}:refs/x-out/auto-base",
        f"refs/heads/{pr['headRefName']}:refs/x-out/auto-head")
    if output("git", "rev-parse", "refs/x-out/auto-base") != pr["baseRefOid"] or \
            output("git", "rev-parse", "refs/x-out/auto-head") != head:
        raise RuntimeError("PR changed during inspection; retry next run")
    if run("git", "merge-base", "--is-ancestor", pr["baseRefOid"], head, check=False).returncode:
        run("gh", "api", "--method", "PUT", f"repos/{repository}/pulls/{number}/update-branch",
            "-f", f"expected_head_sha={head}")
        print(f"PR #{number}: refreshing base; a fresh CI run is required.")
        return
    changed = protected_changes(pr["baseRefOid"], head)
    if changed:
        print(f"PR #{number}: manual review required; protected files changed: {', '.join(changed)}")
        return
    result = ensure_checks(repository, pr["headRefName"], head)
    if not result or result["status"] != "completed" or result["conclusion"] != "success":
        print(f"PR #{number}: waiting for successful checks on {head[:12]}.")
        return
    # GitHub atomically rejects a changed PR head. No admin bypass or force-push.
    run("gh", "pr", "merge", str(number), "--repo", repository, "--merge", "--match-head-commit", head)
    print(f"Merged PR #{number} after Check x-out #{result['databaseId']}.")
    # GITHUB_TOKEN merges do not trigger a push workflow automatically.
    ensure_checks(repository, base)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # No environment dump or command output that might include credentials.
        raise SystemExit(f"{error.cmd[0]} failed (exit {error.returncode}); no deployment performed") from None
