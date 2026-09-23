// Isolated runtime check: no production credentials or external network required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { gzipSync } = require("node:zlib");

const UUID = "00000000-0000-0000-0000-000000000000";
const plain = "dmxlc3M6Ly9leGFtcGxl\n";
const fixture = `proxy-groups:
  - name: ads
    type: fallback
    x-out: {randomize: true, mode: per-refresh}
    proxies: [pair-A, pair-B, pair-C]
  - name: pair-A
    type: fallback
    proxies: [DIRECT, REJECT]
rules: ["MATCH,ads"]
`;

function serveMockBackend() {
  const translations = Object.fromEntries(
    [
      "installationGuideHeader",
      "connectionKeysHeader",
      "linkCopied",
      "linkCopiedToClipboard",
      "getLink",
      "scanQrCode",
      "scanQrCodeDescription",
      "copyLink",
      "name",
      "status",
      "active",
      "inactive",
      "expires",
      "bandwidth",
      "scanToImport",
      "expiresIn",
      "expired",
      "unknown",
      "indefinitely",
    ].map((key) => [key, { en: "Test" }]),
  );
  const config = {
    version: "1",
    locales: ["en"],
    brandingSettings: { title: "Test", logoUrl: "", supportUrl: "https://example.org/support" },
    uiConfig: { subscriptionInfoBlockType: "hidden", installationGuidesBlockType: "cards" },
    baseTranslations: translations,
    svgLibrary: {},
    platforms: {},
  };
  http
    .createServer((req, res) => {
      let data;
      if (req.url === "/api/system/metadata") {
        const git = { commitSha: "test", branch: "test", commitUrl: "https://example.org" };
        data = {
          version: "3.4.3",
          build: { time: "test", number: "test" },
          git: { backend: git, frontend: git },
        };
      } else if (req.url === "/api/subscription-page-configs/") {
        data = { total: 1, configs: [{ uuid: UUID, name: "Test", viewPosition: 0, config }] };
      } else if (req.url === "/api/subscription-page-configs/" + UUID) {
        data = { uuid: UUID, name: "Test", viewPosition: 0, config };
      } else if (req.url === "/api/sub/marked" || req.url === "/api/sub/plain") {
        const marked = req.url.endsWith("/marked");
        const body = marked ? gzipSync(fixture) : Buffer.from(plain);
        res.setHeader("Content-Type", marked ? "application/x-yaml" : "text/plain");
        res.setHeader("subscription-userinfo", "upload=0; download=0; total=123");
        res.setHeader("profile-title", "smoke-test");
        res.setHeader("Cache-Control", "public,max-age=600");
        if (marked) res.setHeader("Content-Encoding", "gzip");
        res.setHeader("Content-Length", body.length);
        res.end(body);
        return;
      } else {
        res.statusCode = 404;
        res.end();
        return;
      }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ response: data }));
    })
    .listen(39876, "127.0.0.1");
}

async function probe() {
  const { parse } = require("/opt/app/dist/node_modules/yaml");
  const headers = {
    "User-Agent": "mihomo/1.19",
    "X-Forwarded-For": "127.0.0.1",
    "X-Forwarded-Proto": "https",
  };
  const orders = new Set();
  for (let index = 0; index < 24; index++) {
    const res = await fetch("http://127.0.0.1:3010/sub/marked", { headers });
    assert.equal(res.status, 200);
    const body = Buffer.from(await res.arrayBuffer());
    assert.equal(res.headers.get("cache-control"), "private, no-store");
    assert.equal(res.headers.get("content-encoding"), null);
    assert.equal(Number(res.headers.get("content-length")), body.length);
    assert.equal(res.headers.get("profile-title"), "smoke-test");
    assert.equal(res.headers.get("subscription-userinfo"), "upload=0; download=0; total=123");
    const yaml = parse(body.toString());
    assert.equal(Object.hasOwn(yaml["proxy-groups"][0], "x-out"), false);
    assert.deepEqual([...yaml["proxy-groups"][0].proxies].sort(), ["pair-A", "pair-B", "pair-C"]);
    assert.deepEqual(yaml["proxy-groups"][1].proxies, ["DIRECT", "REJECT"]);
    orders.add(JSON.stringify(yaml["proxy-groups"][0].proxies));
  }
  assert.ok(orders.size > 1, "expected different orders across independent refreshes");
  const res = await fetch("http://127.0.0.1:3010/sub/plain", { headers });
  assert.equal(await res.text(), plain);
  console.log(
    JSON.stringify({
      requests: 24,
      distinctOrders: orders.size,
      gzip: "ok",
      headers: "ok",
      unmarked: "unchanged",
    }),
  );
}

async function main() {
  const role = process.argv[2];
  if (role === "backend") return serveMockBackend();
  if (role === "probe") return probe();
  const image = role || "out/subscription-page:8.0.0-x-out.1";
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "x-out-smoke-"));
  const name = path.basename(directory);
  fs.copyFileSync(__filename, path.join(directory, "smoke.cjs"));
  const docker = (args) =>
    execFileSync("docker", args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  let started = false;
  try {
    docker([
      "run",
      "-d",
      "--name",
      name,
      "--network",
      "none",
      "--mount",
      `type=bind,source=${directory},target=/smoke,readonly`,
      "-e",
      "REMNAWAVE_PANEL_URL=http://127.0.0.1:39876",
      "-e",
      "REMNAWAVE_API_TOKEN=synthetic-test-only",
      "-e",
      "CUSTOM_SUB_PREFIX=sub",
      "-e",
      "NODE_ENV=production",
      "--entrypoint",
      "/bin/sh",
      image,
      "-c",
      "node /smoke/smoke.cjs backend & exec /bin/sh docker-entrypoint.sh node dist/main.js",
    ]);
    started = true;
    let healthy = false;
    for (let index = 0; index < 30; index++) {
      try {
        docker(["exec", name, "curl", "-fsS", "http://127.0.0.1:3010/internal/health"]);
        healthy = true;
        break;
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    }
    assert.ok(healthy, "container did not become healthy");
    process.stdout.write(docker(["exec", name, "node", "/smoke/smoke.cjs", "probe"]));
  } catch (error) {
    if (started) {
      try {
        process.stderr.write(docker(["logs", "--tail", "30", name]));
      } catch {
        /* already exited */
      }
    }
    throw error;
  } finally {
    if (started) {
      try {
        docker(["rm", "-f", name]);
      } catch {
        /* already exited */
      }
    }
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
