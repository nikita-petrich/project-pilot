# Proxy site config

project-pilot's `mcp` container publishes no host port. It sits on the proxy's
Docker network as `project-pilot-mcp:8765`, and the reverse proxy already running
on the VPS ([nikita-petrich/reverse-proxy](https://github.com/nikita-petrich/reverse-proxy),
`valian/docker-nginx-auto-ssl`) owns the public hostname and the certificate.

One subdomain per MCP server, and nothing more: that image orders a certificate
per hostname over HTTP-01 the first time it is requested, so a new MCP server
needs a DNS record and a route — no wildcard certificate, no DNS-01 challenge,
no shared cert to re-issue.

## Naming

`mcp-<service>.sequenz.io`. This one is `mcp-pilot.sequenz.io`; a Notion server
would be `mcp-notion.sequenz.io`, an n8n bridge `mcp-n8n.sequenz.io`.

The shared prefix is the point: every MCP server sorts together in Strato's
record list and in the Claude connector list, and the suffix says which service
it is. A bare `mcp.sequenz.io` becomes ambiguous the moment there are two, and
renaming a hostname later means a new certificate and a new connector URL in
every client.

## Wiring it up

**1. DNS.** At Strato, an `A` record for `mcp-pilot` under `sequenz.io` pointing
at the VPS (plus `AAAA` if it has IPv6). Verify before touching the proxy — a failed
certificate order counts against Let's Encrypt's rate limit:

```sh
dig +short mcp-pilot.sequenz.io
```

**2. `ALLOWED_DOMAINS`.** The regex in the proxy's `compose.yml` decides which
hostnames may get a certificate. It must match `mcp-pilot.sequenz.io`; a pattern that
already covers one level of subdomains (`^([a-z0-9-]+\.)?sequenz\.io$`) does.

**3. Mount this file.** In the proxy's `compose.yml`, as a **single file**, not a
directory — the entrypoint writes the generated `SITES` blocks into that same
directory, and mounting over it read-only would break every other site:

```yaml
    volumes:
      - ssl-data:/etc/resty-auto-ssl
      - ./mcp-pilot.sequenz.io.conf:/etc/nginx/conf.d/mcp-pilot.sequenz.io.conf:ro
```

Leave `mcp-pilot.sequenz.io` **out** of `SITES`. This file replaces the entry.

**4. The shared network.** project-pilot's `app` and `mcp` join the proxy's
network by the name in `PROXY_NETWORK` (see the repo's `.env.example`), which
must be the network the proxy container is on — `docker network ls` names it.
Then:

```sh
docker compose up -d
docker compose logs -f nginx
```

## Adding the next MCP server

Four edits, none of them in the proxy's own config:

1. An `A` record for `mcp-<service>` at Strato.
2. Copy this file to `mcp-<service>.sequenz.io.conf`; change `server_name` and
   `set $mcp_upstream` to that server's container name and port.
3. Mount the new file alongside this one.
4. `docker compose up -d` — the certificate is ordered on the first request.

`ALLOWED_DOMAINS` already covers it if the regex spans one subdomain level.

## Why not just a `SITES` entry

The image renders each `SITES` entry from one template with nginx's defaults:
`proxy_buffering on`, `proxy_read_timeout 60s`, gzip enabled. MCP responses are
Server-Sent Events, so buffering delays every tool call until the response ends
and the 60-second timeout severs an idle notification stream. This file is that
template plus `proxy_buffering off`, `gzip off`, and long read/send timeouts.

The upstream is also resolved per request through Docker's DNS (`127.0.0.11`)
rather than once at startup, because every project-pilot deploy replaces the
`mcp` container with a new IP — a statically resolved upstream would serve 502s
until the proxy itself was restarted.
