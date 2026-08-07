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

The server owns nothing. Everything the app needs is either versioned in the repo or
stored in the `prod` environment on GitHub, and a deploy puts it in place. A fresh
VPS needs Docker and an empty directory — nothing is hand-placed and nothing has to
be restored after a rebuild.

| File | Owner | Notes |
|---|---|---|
| the image | GHCR | built per commit, tagged `sha-<short>` plus `latest` |
| `compose.yaml` | repo (`compose.prod.yaml`) | overwritten on every deploy |
| `compose.override.yaml` | generated | pins the exact image tag; do not edit |
| `profile/profile.md` | repo | matching profile and signature block |
| `profile/constraints.yaml` | repo | deterministic hard rules |
| `cv/*.pdf` | repo | attached to application e-mails |
| `.env` | GitHub `prod` environment | rendered from its secrets and written on every deploy |
| database | `pgdata` volume | survives deploys |

Because the deploy writes `.env`, editing it on the server is pointless: the next
deploy replaces it. Change the secret instead and re-run the workflow.

## Updating the profile or a CV

Replace the file and push — there is no second mechanism and nothing to do on the
server:

```sh
cp ~/new-cv.pdf cv/CV-German.pdf
git commit -am "chore: update the German CV" && git push
```

`CV_DE_PATH` and `CV_EN_PATH` default to `cv/CV-German.pdf` and
`cv/CV-English.pdf` — the two PDFs versioned in the repo — so keeping the filenames
means never touching config, and the name on disk is the name the recipient sees.
The Word slots (`CV_DE_DOCX_PATH`, `CV_EN_DOCX_PATH`) are unset by default; set them
only if you add a `.docx`. Every configured CV is attached to every application; a
path that does not exist is skipped (and named in the draft's `📎 Attachments` line),
which is why a file you have not added yet is harmless.

**Keep CVs small.** They are e-mail attachments, and base64 encoding adds about a
third on the wire, so a 20 MB PDF arrives as ~28 MB and is refused by most mail
servers (Gmail caps at 25 MB). Browser-printed CVs are the usual culprit: they embed
photos at full camera resolution. A few MB is fine; if a PDF is much larger, downscale
its images before committing.

## One-time server setup

Docker with Compose v2, an empty directory, and a user that can reach the Docker
socket. That is the whole server side.

```sh
sudo mkdir -p /opt/stacks/project-pilot
sudo chown "$USER:$USER" /opt/stacks/project-pilot
sudo usermod -aG docker "$USER"     # log out and back in for this to take effect

docker compose version              # must work without sudo
```

### Networks

The `app` container joins the shared external network `edge` (alias
`project-pilot`) so it sits alongside the rest of the stack behind the proxy. It
serves nothing over it: there is no HTTP server, and Slack runs over an outbound
Socket Mode connection, so no proxy route or domain is involved.

`postgres` stays on the stack's private `default` network. Nothing outside this
stack should reach the database, and a second stack publishing its own `postgres`
service on `edge` would collide on that network's DNS.

If `edge` does not exist yet, the deploy creates it rather than failing.

## GitHub secrets

**Settings → Environments → New environment → `prod`**, then add these to it.
Repository-level secrets work identically if environment secrets are not available
on your plan for a private repo — the workflow just reads `secrets.*`.

| Secret | Required | Value |
|---|---|---|
| `VPS_HOST` | yes | hostname or IP of the VPS |
| `VPS_USER` | yes | the SSH user the setup commands ran as |
| `VPS_SSH_KEY` | yes | the **private** deploy key, including the `BEGIN`/`END` lines |
| `VPS_SSH_KNOWN_HOSTS` | recommended | output of `ssh-keyscan`, pins the host key |
| `VPS_PORT` | no | SSH port if not 22 |

Optional repository **variable** `VPS_PATH` overrides the target directory
(default `/opt/stacks/project-pilot`).

### App settings

Every other secret or variable in the `prod` environment becomes one line of the
app's `.env`, rendered by [`deploy/render-env.py`](../deploy/render-env.py) and
written to the server at the start of each deploy. Nothing is hardcoded in the
workflow, so adding a setting later is a new secret, not a code change.

Use `.env.example` as the checklist. Sensitive values go in as **secrets**; tunables
you want to read back and edit in the UI (`MATCH_THRESHOLD`, `LOG_LEVEL`,
`SEARCH_URLS`, …) work equally well as **variables**. A key defined as both wins as
the secret.

Required — the container dies at boot without them:

| Key | Value |
|---|---|
| `OPENAI_API_KEY` | OpenAI key for the match and application LLM |
| `LLM_MODEL` | model name, e.g. a small, cheap one |
| `SEARCH_URLS` | comma-separated board search URLs, sorted "newest first" |

Strongly recommended — it starts without them, but not usefully:

| Key | Value |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_CHANNEL` | without these there are no alerts at all |
| `CONTACT_MAIL` | goes into the scraper's user agent; a compliance promise, so use a real address |
| `POSTGRES_PASSWORD` | otherwise the default `pilot`. Set a real one **before the first deploy**: it is baked into the `pgdata` volume on creation, and changing it later means recreating the volume |

Everything else from `.env.example` is optional (`SMTP_*` for sending applications,
`ENRICHMENT_*`, thresholds, `LOG_LEVEL`).

Two things the renderer refuses, rather than writing a file a dotenv reader would
silently misparse: a value spanning several lines, and a value containing `' #'`.
`DATABASE_URL` is ignored even if set — compose puts it in the service's
`environment:`, which takes precedence over `env_file`.

Generating the key pair and the host pin:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/project-pilot-deploy -C "github-actions" -N ""
ssh-copy-id -i ~/.ssh/project-pilot-deploy.pub <user>@<host>

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

First the workflow ships `compose.prod.yaml` and the deploy script itself over SSH,
then renders the `prod` environment's secrets into `<VPS_PATH>/.env` with mode 600 —
fed over stdin, so the values never reach the job log or the server's process list.

Then [`deploy/remote-deploy.sh`](../deploy/remote-deploy.sh) runs on the server and:

1. installs the shipped `compose.yaml`;
2. refuses to continue if `.env` is missing (a hand-run safety net; the workflow
   always writes it first);
3. writes `compose.override.yaml` pinning the image built for this commit;
4. `docker compose pull` and `up -d --remove-orphans`;
5. prunes dangling images (tagged `sha-*` images are kept, so rollback stays possible);
6. waits up to five minutes for the app container to report **healthy**, and fails
   the job with the last 80 log lines if it goes unhealthy, exits, or restarts.

Migrations are not a separate step: the container entrypoint runs
`project-pilot init-db` (Alembic `upgrade head`) before starting the daemon.

Step 6 fails on evidence of breakage, not on slowness. The healthcheck only passes
once a scan has actually succeeded, so a green deploy means the worker is really
working — but a container still inside its healthcheck start period is reported and
the deploy still passes. That matters for the very first deploy: on an empty database
the seed run fetches every listing's detail page with a 2–5 second delay and can
easily outlast the wait, and failing that would be a false alarm. A crash loop or an
`unhealthy` verdict still fails the job.

A deploy can also fail while the source is in a 403 cooldown — check the logs before
assuming the release is at fault.

## Rollback

Every commit keeps its own immutable tag, so rolling back is picking an older one:

```sh
cd /opt/stacks/project-pilot
docker image ls ghcr.io/nikita-petrich/project-pilot     # or the Packages tab on GitHub
nano compose.override.yaml                               # set an earlier sha-* tag
docker compose up -d
```

Re-running the deploy workflow on an older commit does the same thing from GitHub.
Note that rollback covers the image only — a migration applied by the newer version
stays applied.

## Operating the stack

```sh
cd /opt/stacks/project-pilot
docker compose logs -f app                        # follow the worker
docker compose ps                                 # health status
docker compose restart app                        # after editing .env
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
