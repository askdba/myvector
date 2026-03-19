# Post-RC: pull published images and run smoke tests

Use this after CI is green **and** images exist in GitHub Container Registry (GHCR).

## 1) When images are available on GHCR

Per `.github/workflows/docker-publish.yml`, images are **pushed** only when:

- A **git tag** matching `v*` is pushed, or
- A **GitHub Release** is published.

**Pull requests** still build and smoke-test images inside CI, but they **do not** push to `ghcr.io`. If you only merged the RC PR without tagging, pull the images **after** you push the release tag (or publish the release) that triggers the publish workflow.

Tags published (multi-arch `linux/amd64`, `linux/arm64`):

| Matrix row   | Image tag |
| :----------- | :-------- |
| MySQL 8.0.45 | `ghcr.io/askdba/myvector:mysql8.0` |
| MySQL 8.4.8  | `ghcr.io/askdba/myvector:mysql8.4` |
| MySQL 9.6.0  | `ghcr.io/askdba/myvector:mysql9.6` |

MySQL 8.0 build also receives `ghcr.io/askdba/myvector:latest`.

## 2) Prerequisites

- Docker running (Desktop on macOS is fine).
- Log in to GHCR if the package is private:

  ```bash
  echo "$GITHUB_TOKEN" | docker login ghcr.io -u USERNAME --password-stdin
  ```

- On Apple Silicon, Docker usually pulls the correct arch; to force a platform:

  ```bash
  export DOCKER_DEFAULT_PLATFORM=linux/arm64   # or linux/amd64
  ```

## 3) Automated smoke (recommended)

From the repository root, after images are pushed:

```bash
./scripts/smoke-published-images.sh
```

Optional: heavier Stanford subset load per image:

```bash
MYVECTOR_SMOKE_STANFORD=1 ./scripts/smoke-published-images.sh
```

## 4) Manual smoke (same checks as CI “Smoke test README examples”)

For each tag you care about:

```bash
docker pull ghcr.io/askdba/myvector:mysql8.4
MYSQL_ROOT_PASSWORD=myvector MYSQL_DATABASE=vectordb \
  bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql8.4
```

Repeat for `mysql8.0` and `mysql9.6`.

## 5) Online index updates (optional, longer)

Documented in `docs/ONLINE_INDEX_UPDATES.md`. Default script targets 8.4:

```bash
./scripts/test-online-updates.sh ghcr.io/askdba/myvector:mysql8.4
```

Run against other tags only if you need binlog/online coverage on those MySQL lines.

## 6) Pass / fail criteria

- **Pass:** `smoke-readme.sh` completes for each pulled tag (plugin load, UDFs, sample queries).
- **Fail:** container exits early, UDF timeout, or SQL errors — capture `docker logs` for the container name printed by the script.

## 7) Record for RC sign-off

- Note commit or tag that produced the images.
- List tags smoke-tested and pass/fail.
- Attach or link CI run URL for the publish workflow.
