"""Run the existing isolated Node probes using only Python and Docker on the host."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time


def smoke(image: str, source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="x-out-smoke-") as directory:
        name = Path(directory).name
        fixture = Path(directory, "smoke.cjs")
        fixture.write_bytes((source / "scripts/smoke-x-out.cjs").read_bytes())
        # Docker's root daemon must be able to read the synthetic fixture.
        fixture.chmod(0o644)
        started = False
        try:
            subprocess.run([
                "docker", "run", "-d", "--name", name, "--network", "none",
                "--mount", f"type=bind,source={directory},target=/smoke,readonly",
                "-e", "REMNAWAVE_PANEL_URL=http://127.0.0.1:39876",
                "-e", "REMNAWAVE_API_TOKEN=synthetic-test-only",
                "-e", "CUSTOM_SUB_PREFIX=sub", "-e", "NODE_ENV=production",
                "--entrypoint", "/bin/sh", image, "-c",
                "node /smoke/smoke.cjs backend & exec /bin/sh docker-entrypoint.sh node dist/main.js",
            ], check=True, capture_output=True, timeout=60)
            started = True
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                health = subprocess.run([
                    "docker", "exec", name, "curl", "-fsS",
                    "http://127.0.0.1:3010/internal/health",
                ], capture_output=True, timeout=10)
                if health.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Isolated container did not become healthy")
            subprocess.run(["docker", "exec", name, "node", "/smoke/smoke.cjs", "probe"],
                           check=True, timeout=120)
        finally:
            if started:
                subprocess.run(["docker", "rm", "-f", name], check=True,
                               capture_output=True, timeout=30)


if __name__ == "__main__":
    smoke(sys.argv[1], Path(__file__).resolve().parents[1])
