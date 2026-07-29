# Deployment (GitHub Actions → VPS)

Pushing to `main` deploys project-pilot to the VPS. GitHub builds the container
image, the server pulls it — the server never builds and never needs the source
tree.

```
push to main
  └─ gate     ruff · ruff format · mypy · pytest (with a Postgres service)
      └─ build  docker build → ghcr.io/nikita-petrich/project-pilot:sha-<commit>
          └─ deploy  ssh → pull the image → docker compose up -d → wait for healthy
```

Workflows: [`ci.yml`](../.github/workflows/ci.yml) (gate on branches and PRs),
[`deploy.yml`](../.github/workflows/deploy.yml) (gate + build + deploy on `main`,
also runnable by hand via **Actions → Deploy to VPS → Run workflow**), and
[`quality-gate.yml`](../.github/workflows/quality-gate.yml), the shared gate both
call.

## What lives where

Everything the app needs is versioned and rides inside the image. The server owns
exactly one file, because secrets are the one thing that cannot go in a public repo.

| File | Owner | Notes |
|---|---|---|
| the image | GHCR | built per commit, tagged `sha-<short>` plus `latest` |
| `compose.yaml` | repo (`compose.prod.yaml`) | overwritten on every deploy |
| `compose.override.yaml` | generated | pins the exact image tag; do not edit |
| `profile/profile.md` | repo | matching profile and signature block |
| `profile/constraints.yaml` | repo | deterministic hard rules |
| `cv/*.pdf` | repo | attached to application e-mails |
| `.env` | **server only** | secrets; never touched by a deploy |
| database | `pgdata` volume | survives deploys |

## Updating the profile or a CV

Replace the file and push — there is no second mechanism and nothing to do on the
server:

```sh
cp ~/new-cv.pdf cv/lebenslauf-de.pdf
git commit -am "chore: update the German CV" && git push
```

`CV_DE_PATH` and `CV_EN_PATH` default to `cv/lebenslauf-de.pdf` and `cv/cv-en.pdf`,
so keeping the filenames means never touching config. A path that does not exist
just means no attachment, which is why `cv/cv-en.pdf` being absent is harmless until
you add it.

**Keep CVs small.** They are e-mail attachments, and base64 encoding adds about a
third on the wire, so a 20 MB PDF arrives as ~28 MB and is refused by most mail
servers (Gmail caps at 25 MB). Browser-printed CVs are the usual culprit: they embed
photos at full camera resolution. A few MB is fine; if a PDF is much larger, downscale
its images before committing.

## One-time server setup

Docker with Compose v2, and a user that can reach the Docker socket.

```sh
sudo mkdir -p /opt/stack/project-pilot
sudo chown deploy:deploy /opt/stack/project-pilot
sudo usermod -aG docker deploy       # log out and back in for this to take effect
```

Then the one server-owned file:

```sh
cd /opt/stack/project-pilot
# Copy .env.example from the repo and fill in the real values. Leave DATABASE_URL
# out — compose sets it to reach the postgres service — and set a real
# POSTGRES_PASSWORD. The CV paths can stay commented out.
nano .env
```

## GitHub secrets

**Settings → Secrets and variables → Actions.**

| Secret | Required | Value |
|---|---|---|
| `VPS_HOST` | yes | hostname or IP of the VPS |
| `VPS_USER` | yes | SSH user, e.g. `deploy` |
| `VPS_SSH_KEY` | yes | the **private** deploy key, including the `BEGIN`/`END` lines |
| `VPS_SSH_KNOWN_HOSTS` | recommended | output of `ssh-keyscan`, pins the host key |
| `VPS_PORT` | no | SSH port if not 22 |

Optional repository **variable** `VPS_PATH` overrides the target directory
(default `/opt/stack/project-pilot`).

Generating the key pair and the host pin:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/project-pilot-deploy -C "github-actions" -N ""
ssh-copy-id -i ~/.ssh/project-pilot-deploy.pub deploy@<host>

cat ~/.ssh/project-pilot-deploy     # → VPS_SSH_KEY
ssh-keyscan -p 22 <host>            # → VPS_SSH_KNOWN_HOSTS
```

Without `VPS_SSH_KNOWN_HOSTS` the workflow still runs but trusts the host key on
first sight and logs a warning — fine for a first try, worth fixing after.

No registry credentials are needed on the server: the deploy job forwards its own
short-lived `GITHUB_TOKEN` over stdin for the `docker login`, and it expires with
the job. To pull by hand later, log in once with a personal access token that has
`read:packages`:

```sh
docker login ghcr.io -u <github-user>
```

## What a deploy does

[`deploy/remote-deploy.sh`](../deploy/remote-deploy.sh) runs on the server and:

1. installs the shipped `compose.yaml`;
2. refuses to continue if `.env` is missing;
3. writes `compose.override.yaml` pinning the image built for this commit;
4. `docker compose pull` and `up -d --remove-orphans`;
5. prunes dangling images (tagged `sha-*` images are kept, so rollback stays possible);
6. waits up to five minutes for the app container to report **healthy**, and fails
   the job with the last 80 log lines if it goes unhealthy or exits.

Migrations are not a separate step: the container entrypoint runs
`project-pilot init-db` (Alembic `upgrade head`) before starting the daemon.

Step 6 is a real gate. The healthcheck only passes once a scan has actually
succeeded, so a deploy that goes green means the worker is really working. It also
means a deploy can fail while the source is in a 403 cooldown — check the logs
before assuming the release is at fault.

## Rollback

Every commit keeps its own immutable tag, so rolling back is picking an older one:

```sh
cd /opt/stack/project-pilot
docker image ls ghcr.io/nikita-petrich/project-pilot     # or the Packages tab on GitHub
nano compose.override.yaml                               # set an earlier sha-* tag
docker compose up -d
```

Re-running the deploy workflow on an older commit does the same thing from GitHub.
Note that rollback covers the image only — a migration applied by the newer version
stays applied.

## Operating the stack

```sh
cd /opt/stack/project-pilot
docker compose logs -f app                        # follow the worker
docker compose ps                                 # health status
docker compose restart app                        # after editing .env or profile/
docker compose exec app project-pilot stats       # reporting summary
docker compose exec app project-pilot run-once    # one scan now
docker compose exec app project-pilot healthcheck # liveness/freshness probe
```

Day-to-day operations, troubleshooting, and threshold tuning are in
[`operations.md`](operations.md).

## Building on the server instead

The repo still carries `compose.yaml`, which builds the image locally from a
checkout. That path needs no registry, but it costs server CPU on every change. It is
unrelated to this pipeline — do not run both in the same directory, since a deploy
overwrites `compose.yaml`.
