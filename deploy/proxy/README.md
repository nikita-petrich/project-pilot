# Proxy stack

A minimal Caddy in front of the MCP servers on this host: TLS from Let's Encrypt,
one route per subdomain, no secrets. It exists because the Claude custom connector
and n8n need a public HTTPS URL, and the apps deliberately publish no host port of
their own.

Today it serves `mcp.sequenz.io` → project-pilot. Adding a second MCP server later
is an A record, a block in the `Caddyfile`, and a reload:

```sh
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

If the VPS already runs a reverse proxy, do not add this one — route the hostname
to `project-pilot-mcp:8765` on the `edge` network there instead. The equivalents
are one line each:

| Proxy | Route |
|---|---|
| Traefik | labels on the `mcp` service: `traefik.http.services.pp-mcp.loadbalancer.server.port=8765` plus a `Host(...)` router rule |
| Nginx Proxy Manager | new Proxy Host → forward to `project-pilot-mcp` port `8765`, scheme `http`, **Websockets Support on** |
| plain nginx | `proxy_pass http://project-pilot-mcp:8765;` plus `proxy_buffering off;` and `proxy_read_timeout 3600s;` |

Whatever the proxy, two things must hold: it must be on the `edge` network (else
the container name does not resolve), and it must not buffer responses — MCP
streams over Server-Sent Events, and a buffering proxy makes every call look like
a hang. Caddy handles both without configuration.

Setup is in [`../../docs/claude-setup.md`](../../docs/claude-setup.md).
