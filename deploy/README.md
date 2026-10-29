# Production deployment

One Hetzner host, Docker Compose, no Kubernetes. GitHub Actions builds three images per commit
(`ghcr.io/nazar652/pisniar-{bot,worker,webapp}:<sha>`), and the Deploy workflow runs `deploy/deploy.sh`
on the host over SSH. The server builds nothing and holds no registry credential. The repository is public:
its Actions logs are public, so no script prints a value.

```
push to master -> CI (make -k check) -> Deploy: target -> build-push -> deploy (SSH)
host: git fetch + ancestor check -> git reset --hard <sha> -> write-env.sh -> deploy.sh <sha>
deploy.sh: pull -> stop bot webapp -> stop workers (7 m / 12 m grace) -> rm -> migrate + alembic check -> up --wait
```

Docker-based suites (`make test-int`, `test-fonts`, `test-media`) run on your machine through the
`.githooks/pre-push` hook (`make hooks`, once per clone). `git push --no-verify` skips it, and CI cannot tell;
CI runs `make -k check` and Deploy waits for it.

## 1. Check the server

```
uname -m          # x86_64; on aarch64 add `platforms: linux/arm64` to the build-push steps
lsb_release -a
free -h
df -h
swapon --show     # the 2G swap from the yachan setup stays
timedatectl       # "System clock synchronized: yes"
```

## 2. Remove yachan and k3s

Manual and irreversible; yachan keeps its data outside the server (Neon, R2).

1. `kubectl get all -A`: make sure nothing there is still needed.
2. Disable the Deploy workflow in the yachan-v2 repository, or its next push to master will SSH into this host again.
3. `sudo /usr/local/bin/k3s-uninstall.sh`
4. `sudo rm -f /usr/local/bin/kustomize`
5. `rm -rf ~/yachan-v2`
6. Remove `KUBECONFIG` from the shell profile.
7. Remove the yachan deploy key from `~/.ssh/authorized_keys`.
8. `sudo ufw status numbered`, then `sudo ufw delete allow 80` and `sudo ufw delete allow 443` (by their numbers; keep SSH).
9. Remove or repoint the yachan rules in the Hetzner Cloud Firewall and the yachan A record in Cloudflare.
10. `sudo reboot`

Afterwards nothing may be left:

```
command -v k3s kubectl kustomize          # prints nothing
ls /var/lib/rancher /etc/rancher          # No such file or directory
ip link | grep -E 'cni0|flannel'          # prints nothing
sudo iptables-save | grep -Ei 'kube|cni|flannel'   # prints nothing
ss -tlnp | grep -E ':(6443|10250|80|443)\b'        # prints nothing
```

## 3. Docker

Install Docker Engine and the compose plugin from Docker's official apt repository (the deploy needs
`up --wait`, `--wait-timeout` and `--no-build`). Add the deploy user to the `docker` group (that is root
for practical purposes). Then:

```
echo '{"log-driver": "local"}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

## 4. SSH and firewall

1. Create a new ed25519 key pair for GitHub on your machine. Append the public key to the deploy user's
   `~/.ssh/authorized_keys`; the private key becomes the secret `SSH_KEY`.
2. On the server: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. The `SHA256:...` value is the secret
   `SSH_FINGERPRINT`; the workflow refuses a host that presents another key.
   The deploy fails when the secret is empty or malformed. On a mismatch fix the value; never delete the secret.
3. Secrets `SSH_HOST`, `SSH_USER`, and `SSH_PORT` (optional, default 22).
4. Key-only login (`PasswordAuthentication no`). Inbound traffic: SSH only, in ufw and in the Hetzner Cloud
   Firewall. Docker bypasses ufw for published ports; production publishes none (the Funnel is outbound).

## 5. Clone

```
git clone https://github.com/Nazar652/pisniar.git ~/pisniar
```

## 6. The Tailscale state volume (once)

```
docker volume create pisniar-tailscale-state
```

`deploy.sh` refuses to run without it. It is external, so `down -v` never removes the node identity.

## 7. Tailscale

1. Delete the dev node `pisniar` in the Tailscale admin console. The production node then registers under the
   same name, and `https://pisniar.<tailnet>.ts.net` and every button already sent keep working. The local
   Funnel stops with it; a dev Funnel later is a separate task.
2. Generate a one-time auth key (tag `tag:pisniar`, pre-approved, not reusable, not ephemeral) and store it as the
   secret `TS_AUTHKEY`.
3. After the first deploy check the node in Machines, disable key expiry, and delete the secret `TS_AUTHKEY`.
   Later deploys write an empty `.env.tailscale` and the node keeps its identity from the volume.

## 8. GitHub secrets and variables

Settings -> Secrets and variables -> Actions. Every deploy rewrites `.env` and `.env.tailscale` on the host
from these; editing `.env` by hand is overwritten by the next deploy.

| Secrets | Notes |
| --- | --- |
| `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_FINGERPRINT`, `SSH_PORT` | section 4 |
| `BOT_TOKEN` | the production bot; the local `.env` keeps a separate dev bot and a test channel |
| `ADMIN_IDS`, `ALERT_CHAT_ID`, `CHANNEL_ID` | identifiers are secrets here: Variables are not masked in public logs |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | |
| `REDIS_PASSWORD`, `POSTGRES_PASSWORD` | `openssl rand -hex 24` each (hex only: the database URL embeds it) |
| `WEBAPP_PUBLIC_URL`, `MUSICBRAINZ_CONTACT` | `https://pisniar.<tailnet>.ts.net`, a contact address |
| `TS_AUTHKEY` | first registration only (section 7) |

| Variables (tuning, optional; empty means the default) | |
| --- | --- |
| `LOG_LEVEL`, `ITUNES_COUNTRY`, `SOURCE_FETCH_TTL_SECONDS`, `MEDIA_MIN_FREE_BYTES` | |
| `DOWNLOAD_TASK_SOFT_TIME_LIMIT_SECONDS`, `DOWNLOAD_TASK_TIME_LIMIT_SECONDS` | |
| `RENDER_TASK_SOFT_TIME_LIMIT_SECONDS`, `RENDER_TASK_TIME_LIMIT_SECONDS` | from the bench (section 11) |
| `PLAIN_LYRICS_TASK_SOFT_TIME_LIMIT_SECONDS`, `PLAIN_LYRICS_TASK_TIME_LIMIT_SECONDS` | |

A new key in `Settings` goes into `deploy/write-env.sh`, both lists in `deploy.yml` and `tests/unit/test_deploy.py`.

## 9. Repository settings and history before going public

1. Search the history for secrets before the repository becomes public:
   `git log --all -p | grep -E 'tskey-|[0-9]{8,10}:[A-Za-z0-9_-]{35}'` and review further patterns for your own
   tokens. Reissue anything found: a secret in history stays there after a later commit removes it.
2. Settings -> Actions -> General: "Require approval for all outside collaborators" for pull-request workflows.
3. Settings -> Code security: enable secret scanning and push protection.
4. Keep "Workflow permissions" at read-only; the workflows ask for what they need.

## 10. First deploy

1. Push to master (or run CI once) so that the images exist. Deploy then runs by itself after a green CI.
2. GHCR packages are private by default: open `pisniar-bot`, `pisniar-worker` and `pisniar-webapp` ->
   Package settings -> Change visibility -> Public. A first deploy that finds them private fails at `pull`
   before anything is stopped; after the change use "Re-run failed jobs".
3. Or start it by hand: Actions -> Deploy -> Run workflow on master with an empty `sha`.

## 11. Bench on the host

After the first deploy, once:

```
cd ~/pisniar
export COMPOSE_FILE=compose.yaml:compose.prod.yml PISNIAR_IMAGE_TAG=$(git rev-parse HEAD)
docker compose run --rm bench
```

Put the printed `RENDER_TASK_*` values into the Variables and run Deploy again. If the hard limit goes above
about 690 s, raise `stop_grace_period` of `worker-render` in `compose.prod.yml` as well: a deploy that stops a
render later than the grace period kills it (redelivered after an hour, or `/retry`).

## 12. Operating by hand

```
cd ~/pisniar
export COMPOSE_FILE=compose.yaml:compose.prod.yml COMPOSE_PROFILES=funnel PISNIAR_IMAGE_TAG=$(git rev-parse HEAD)
```

Allowed: `make ps`, `make logs`, `make psql`, `make shell`, `make peaks`, `make dev-token`.
Not on the server: `make build`, `up`, `clean`, `verify`, `bench-render`, `migrate`, `revision`, `downgrade`,
`test-*`, `docker compose down -v`, and editing `.env`.

## 13. Rollback

- Code only: Actions -> Deploy -> Run workflow on master with `sha` set to an earlier master commit. It builds
  nothing, uses that commit's image, and writes `.env` from the current secrets.
- With a migration: stop the writers, downgrade with the current image, then dispatch the old sha:
  `docker compose stop bot webapp worker worker-render`, then
  `docker compose run --rm migrate alembic downgrade <old head>`.
- By hand on the server: `sh deploy/deploy.sh <sha>` (images are public, no login).

## 14. Rotating passwords

`REDIS_PASSWORD`: change the secret and deploy. `POSTGRES_PASSWORD`: Postgres applies it only when the volume is
created, so first `docker compose exec postgres psql -U pisniar -d pisniar -c "ALTER ROLE pisniar PASSWORD '<new>'"`,
then change the secret and deploy; the other order leaves the app without a database.

## 15. What is not here

No backups: a lost disk or `down -v` destroys the posts, manual edits and lyric timings. A failed migration leaves
the app stopped and the Deploy job red; no Telegram alert is sent. Free disk by hand with `docker image prune`;
audio is never removed.

## 16. Acceptance on the real host

- MANUAL-R1: push, CI green, Deploy green; `docker compose ps` all healthy; the bot answers; the Mini App opens;
  a link goes through draft, video and publication to the channel.
- MANUAL-R2: `TS_AUTHKEY` registers the node `pisniar` with the former URL; after the secret is deleted the next
  deploy keeps the Funnel.
- MANUAL-R3: bench values in Variables, Deploy, then `docker compose exec worker-render printenv
  RENDER_TASK_TIME_LIMIT_SECONDS` (never print the whole environment).
- MANUAL-R4: deploy during a render, the preview still arrives; a rollback dispatch with `sha`; a pull request
  starts no Deploy.
- MANUAL-R5: `sudo reboot`, everything comes back; `stat -c %a .env .env.tailscale` prints 600 twice;
  `~/.docker/config.json` has no `ghcr.io` entry.
- MANUAL-H1: `make hooks`, then push to master: Docker off -> refused; stack down -> refused with `make up`;
  dirty tree -> refused; clean tree and stack up -> the three suites run, then the push; the same sha again ->
  skipped by the stamp; `git push --no-verify` -> no hook.
