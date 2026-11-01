"""Deploy a tested default-branch commit; restore the prior image on a failed probe.

Installed as a fixed, root-owned helper. No GitHub token or inbound SSH access is
needed: source and CI metadata are public. Production configuration stays local.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = "ckeiituk/subscription-page"
BRANCH = "out/x-out"
SERVICE = "remnawave-subscription-page"
BUILDER = "x-out-subscription-updates"


def run(*args: str, cwd: Path | None = None, stdin: str | None = None,
        timeout: int = 120) -> str:
    result = subprocess.run(args, cwd=cwd, input=stdin, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # docker inspect/compose output can contain production secrets.
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout.strip()


def latest_check(runs: list[dict], sha: str) -> dict | None:
    matching = [r for r in runs if r.get("head_sha") == sha
                and r.get("head_branch") == BRANCH
                and r.get("head_repository", {}).get("full_name") == REPOSITORY
                and r.get("path") == ".github/workflows/x-out-check.yml"
                and r.get("event") in ("push", "workflow_dispatch")]
    return max(matching, key=lambda r: r["id"], default=None)


def ci_passed(sha: str) -> bool:
    query = urllib.parse.urlencode({"branch": BRANCH, "head_sha": sha, "per_page": 100})
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/x-out-check.yml/runs?{query}",
        headers={"User-Agent": "x-out-server-updater", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        selected = latest_check(json.load(response)["workflow_runs"], sha)
    return bool(selected and selected["status"] == "completed"
                and selected["conclusion"] == "success")


def atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        name = Path(temporary.name)
    try:
        name.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(name, path)
    finally:
        name.unlink(missing_ok=True)


def replace_image(content: bytes, image: str) -> bytes:
    text = content.decode()
    match = re.search(r"(?m)^( +)" + re.escape(SERVICE) + r":\s*$", text)
    if not match:
        raise ValueError("Cannot locate subscription service in compose")
    start = match.end()
    end = re.search(r"(?m)^\S|^ {1," + str(len(match[1])) + r"}\S", text[start:])
    stop = start + end.start() if end else len(text)
    block, count = re.subn(r"(?m)^( +image:)\s*[^\n]+$", r"\g<1> " + image,
                          text[start:stop])
    if count != 1:
        raise ValueError("Expected exactly one subscription image in compose")
    return (text[:start] + block + text[stop:]).encode()


LIVE_PROBE = r"""
const fs = require('fs');
const {parse} = require('/opt/app/dist/node_modules/yaml');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const response = await fetch('http://127.0.0.1:3010' + input.path, {
    headers: {'User-Agent':'mihomo/1.19', 'X-Forwarded-For':'127.0.0.1',
      'X-Forwarded-Proto':'https'}, signal: AbortSignal.timeout(30000)
  });
  if(response.status !== 200) throw Error('HTTP ' + response.status);
  const body = await response.text();
  const config = parse(body, {maxAliasCount:100});
  if(!config || !Array.isArray(config['proxy-groups']) || !config.rules?.length)
    throw Error('Invalid subscription');
  if(config['proxy-groups'].some(g => Object.hasOwn(g, 'x-out')))
    throw Error('Unprocessed x-out marker');
  console.log(JSON.stringify({status:response.status, groups:config['proxy-groups'].length,
    xOutApplied:response.headers.get('cache-control') === 'private, no-store'}));
})().catch(() => {console.error('Live subscription probe failed');process.exit(1)});
"""


def inspect(name: str) -> dict:
    return json.loads(run("docker", "inspect", name))[0]


def live_probe() -> dict:
    checker = inspect("checker")
    environment = dict(item.split("=", 1) for item in checker["Config"]["Env"] if "=" in item)
    parts = urllib.parse.urlsplit(environment["SUBSCRIPTION_URL"]).path.rstrip("/").split("/")
    if parts[-1] in ("mihomo", "clash", "xray", "singbox", "v2ray-json"):
        parts.pop()
    identifier = parts[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Cannot derive test subscription identifier")
    environment = dict(item.split("=", 1) for item in inspect(SERVICE)["Config"]["Env"] if "=" in item)
    prefix = environment.get("CUSTOM_SUB_PREFIX", "").strip("/")
    route = "/" + "/".join(part for part in (prefix, identifier, "mihomo") if part)
    # Identifier is passed over stdin and never appears in argv, output or logs.
    return json.loads(run("docker", "exec", "-i", SERVICE, "node", "-e", LIVE_PROBE,
                          stdin=json.dumps({"path": route}), timeout=60))


def compose_up(directory: Path, override: Path | None = None) -> None:
    command = ["docker", "compose", "-f", "docker-compose.yml"]
    if override:
        command += ["-f", str(override)]
    run(*command, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
        "--wait", "--wait-timeout", "120", SERVICE, cwd=directory, timeout=180)


def activate(directory: Path, state: Path, image: str) -> str:
    compose = directory / "docker-compose.yml"
    original = compose.read_bytes()
    updated = replace_image(original, image)
    previous = inspect(SERVICE)["Image"]
    before = live_probe()
    backup = state / ("rollback-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(mode=0o700)
    atomic_write(backup / "docker-compose.yml", original)
    rollback = backup / "rollback.yml"
    atomic_write(rollback, json.dumps({"services": {SERVICE: {
        "image": previous, "pull_policy": "never"}}}).encode())
    if compose.read_bytes() != original:
        raise RuntimeError("Compose changed concurrently; deployment cancelled")
    atomic_write(compose, updated)
    try:
        run("docker", "compose", "-f", "docker-compose.yml", "config", "--quiet", cwd=directory)
        compose_up(directory)
        after = live_probe()
        if before["xOutApplied"] and not after["xOutApplied"]:
            raise RuntimeError("Live subscription lost x-out processing")
        print("Live subscription verified:", json.dumps(after), flush=True)
    except BaseException:
        if compose.read_bytes() != updated:
            raise RuntimeError("Compose changed concurrently; automatic rollback refused") from None
        atomic_write(compose, original)
        compose_up(directory, rollback)
        if inspect(SERVICE)["Image"] != previous:
            raise RuntimeError("Rollback image verification failed") from None
        live_probe()
        print("Previous image restored and live subscription verified.", flush=True)
        raise
    return previous


def clean_images(previous: str | None) -> None:
    keep = {inspect(SERVICE)["Image"], previous}
    for tag in run("docker", "image", "ls", "--filter", "reference=out/subscription-page:git-*",
                   "--format", "{{.Repository}}:{{.Tag}}").splitlines():
        image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", tag)
        if image_id not in keep:
            try:
                # No --force: Docker also protects images used by other containers.
                run("docker", "image", "rm", "--no-prune", tag)
            except RuntimeError:
                print("An old updater image is still in use; keeping it.", flush=True)


def prepare_builder() -> None:
    try:
        run("docker", "buildx", "inspect", BUILDER)
    except RuntimeError:
        run("docker", "buildx", "create", "--name", BUILDER, "--driver", "docker-container")
    # docker-container builders have no daemon until explicitly bootstrapped;
    # pruning an unstarted builder fails before the first image can be built.
    run("docker", "buildx", "inspect", "--bootstrap", BUILDER, timeout=180)


def clean_build_cache() -> None:
    # This builder belongs only to the updater. Never prune the host's default
    # builder, volumes, unrelated images, or the active/rollback runtime images.
    run("docker", "buildx", "prune", "--builder", BUILDER, "--force", "--max-used-space", "1gb",
        timeout=180)


def deploy(source: Path, compose: Path, state_dir: Path, retry: bool = False) -> None:
    state_path = state_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if run("git", "status", "--porcelain", cwd=source):
        raise RuntimeError("Source checkout contains local changes")
    if run("git", "remote", "get-url", "origin", cwd=source) != f"https://github.com/{REPOSITORY}.git":
        raise RuntimeError("Unexpected source repository")
    run("git", "fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
        cwd=source)
    sha = run("git", "rev-parse", f"refs/remotes/origin/{BRANCH}", cwd=source)
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid commit identifier")
    if sha == state.get("deployed"):
        print(f"Already deployed {sha[:12]}.", flush=True)
        return
    if sha == state.get("failed") and not retry:
        raise RuntimeError(f"Commit {sha[:12]} failed previously; repair or use --retry")
    if not ci_passed(sha):
        print(f"Waiting for successful Check x-out on {sha[:12]}.", flush=True)
        return
    clean_images(state.get("previous_image"))
    prepare_builder()
    clean_build_cache()
    if shutil.disk_usage(source).free < 3 * 1024**3:
        raise RuntimeError("Less than 3 GiB free; current service left untouched")
    run("git", "checkout", "--detach", sha, cwd=source)
    image = f"out/subscription-page:git-{sha}"
    print(f"Building verified commit {sha}.", flush=True)
    try:
        # Keep detailed build output private; no production environment is passed.
        with (state_dir / "build.log").open("w") as log:
            result = subprocess.run(["docker", "buildx", "build", "--builder", BUILDER, "--load",
                                     "-f", "Dockerfile.x-out", "-t", image, "."],
                                    cwd=source, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        if result.returncode:
            raise RuntimeError("Image build failed; see private build.log")
        run("python3", "-B", "scripts/smoke-x-out.py", image, cwd=source, timeout=360)
        previous = activate(compose, state_dir, image)
    except BaseException:
        state["failed"] = sha
        atomic_write(state_path, json.dumps(state).encode())
        raise
    atomic_write(state_path, json.dumps({"deployed": sha, "image": image,
                                        "previous_image": previous}).encode())
    clean_images(previous)
    clean_build_cache()
    print(f"Deployed {sha[:12]} successfully.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/remnawave/subscription-source"))
    parser.add_argument("--compose-dir", type=Path, default=Path("/opt/remnawave/subscription"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/remnawave-subscription-update"))
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / "update.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another update is running.")
            return
        deploy(args.source, args.compose_dir, args.state_dir, args.retry)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No raw CalledProcessError/HTTP response, subscription URL or environment.
        print(f"Update stopped: {type(error).__name__}: {error}", flush=True)
        raise SystemExit(1) from None
