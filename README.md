# StickyExit

English | [简体中文](README.zh-CN.md)

Turn a single proxy subscription into multiple long-lived proxies, with an ordered node pool per user that fails over by priority.

## Background

Some workloads need several long-lived exit IPs. Commercial long-lived proxies are billed per IP, which makes them expensive for this use case.

Every node in a proxy subscription is its own server with its own exit IP. StickyExit treats those nodes as an exit pool, runs one authenticated proxy port on your machine, and gives each user an ordered pool of nodes. Traffic uses the highest-priority node by default; after the configured number of consecutive failures it falls back to the next available member, and switches back automatically after the original node recovers.

```
user alice (pool: A → B) ──┐
                           ├─ mixed:1080 (auth) ─ routing ─> A / B / C (their exit IPs)
user bob   (pool: B → C) ──┘
```

Each user only needs `username:password@server-ip:1080`. HTTP and SOCKS5 share that port; sing-box routes each user through their own selector group using `auth_user` rules, and the order of nodes in the pool is the priority order.

## Features

- One sing-box process and one entry port; low memory footprint
- One ordered node pool per user; pools may overlap while authenticated routes stay isolated
- Node import from subscription URL, Clash YAML, base64, or plain node links (one per line: `socks5://`, `http://`, `vmess://`, `vless://`, `ss://`, `trojan://`, `hy2://`, `tuic://`, ...). Import appends to the pool instead of replacing it
- SQLite persistence for nodes, users, and node pools; configuration survives restarts
- Node health checks with runtime failover and recovery: prefer Clash API delay probes, fall back to TCP; switch to the next available pool member after the failure threshold and switch back after recovery
- Every change is rendered to a pending config file in the same DB transaction; `sing-box check` validates it before each manual reload
- Web UI: searchable and paginated node list, paginated user list, node-pool editor, create a user directly from a node row, delete a node, one-click copy of proxy addresses
- Docker deployment with data and the sing-box binary mounted as volumes

## Limitations

- Exit IPs come from the subscription nodes. When the active node fails, StickyExit switches to another member of that user's pool, so the exit IP changes.
- A user with an empty pool is omitted from the proxy configuration. If no active user has a usable pool, the proxy enters closed mode and rejects all traffic.
- Stability depends on the proxy service you buy.

## Quick Start

### Docker

```bash
git clone https://github.com/FishNoNoNo/StickyExit.git sticky-exit
cd sticky-exit

# optional: adjust ports, admin token, advertised IP
cp .env.example .env

docker compose up -d
```

After startup:

- Web UI: `http://SERVER_IP:5003`
- Proxy port: `1080` (HTTP and SOCKS5)

If you set an admin token, open `http://SERVER_IP:5003/?token=YOUR_TOKEN`.

> **The frontend build must be mounted.** `web/static` is listed in `.dockerignore` and is not baked into
> the image. At runtime it is mounted into the container by `- ./web/static:/app/web/static:ro` in
> `docker-compose.yml`. Without that mount the page reports "frontend not built" and
> `/app/web/static/index.html` does not exist inside the container. After changing `volumes` you must
> recreate the container; `docker compose restart` is not enough:
>
> ```bash
> docker compose up -d --force-recreate
> ```
>
> On older Compose, use `docker-compose` instead.

The sing-box binaries in `lib/` are mounted into the container the same way. The repository ships a Linux amd64 build and a Windows build. For other architectures (for example arm64), replace `lib/sing-box-linux/sing-box` with the matching build.

### Local Run

```bash
pip install -r requirements.txt
python main.py
```

Python 3.10 or newer. The Web UI listens on `5003` and the proxy port on `1080`.

If the node table is empty, only the Web UI starts. Import nodes and click "Reload sing-box" to launch the proxy.

### Frontend Development

```bash
cd web/frontend
npm install
npm run dev     # dev server, proxies /api to 127.0.0.1:5003
npm run build   # outputs to web/static, served by the backend
```

## Usage

1. Open the Web UI and import nodes (subscription URL / Clash YAML / node links).
2. Create a user and configure its node pool. Plain "create user" starts with an empty pool; use the node-pool editor to choose and order nodes. "Add user" on a node row creates a single-member pool containing that node.
3. Copy the proxy address shown on the page and hand it to that user.
4. Every change is committed to the database together with a freshly rendered pending config; click "Reload sing-box" to validate and apply it.

### Adding and Removing

- Importing nodes **appends** to the node set. It does not overwrite existing nodes or change existing node pools.
- Deleting a node removes it from every user pool, without substituting another node; a pool can become empty.
- Revoking a user invalidates the password immediately. Activating the user keeps the existing node pool (an empty pool stays empty) and issues a brand new password; the old one is never restored.

### Supported Node Links

Plain links (one per line in the "paste node text" box) are turned into sing-box
outbounds by `parser/links.py`:

| Link scheme | sing-box outbound |
|---|---|
| `socks5://` `socks://` `socks4://` `socks4a://` | `socks` (version 5 / 4 / 4a) |
| `http://` `https://` | `http` (`https` adds TLS) |
| `ss://` | `shadowsocks` |
| `vmess://` `vless://` `trojan://` | same name |
| `hy2://` `hysteria2://` | `hysteria2` |
| `tuic://` | `tuic` |

Anything else is skipped with a log line instead of failing the whole import —
including SSR, which sing-box removed in 1.6.

Display names come from the link's `#remark` (for vmess, the `ps` field inside
the base64 payload), falling back to `type host:port`. Names are fully decoupled
from node tags — a tag is derived from the connection config only, so renaming
or re-importing a subscription never duplicates nodes. Use the row's "Edit"
action to rename a node; it takes effect immediately and needs no reload.

## What Users Receive

After creating a user, the page shows:

```
http://alice:xxxxx@SERVER_IP:1080
IP=SERVER_IP  PORT=1080  USER=alice  PASSWORD=xxxxx
```

They can use it in a browser, the system proxy settings, or Proxifier. No Clash needed, and the original subscription is never exposed.

## Configuration

Configuration comes from `.env` and can also be overridden by environment variables. Every key is optional and falls back to a built-in default.

| Variable | Default | Description |
|---|---|---|
| `LISTEN_IP` | `0.0.0.0` | Proxy listen address |
| `LISTEN_PORT` | `1080` | Proxy port (HTTP + SOCKS5) |
| `WEB_HOST` | `0.0.0.0` | Web UI listen address |
| `WEB_PORT` | `5003` | Web UI port |
| `WEB_ADMIN_TOKEN` | empty | Admin token for the Web UI. Empty = no auth (use behind a reverse proxy or on a trusted network). When set, open the page with `?token=...`; every `/api/*` call must carry `?token=` or an `X-Admin-Token` header |
| `ADVERTISE_IP` | empty | Host shown in the proxy address handed to users. When empty it is auto-detected: explicit `LISTEN_IP` > public IP > LAN IP. On a server with a public IP, set it explicitly |
| `VERIFY_SSL` | `0` | Whether to verify TLS when fetching subscriptions. Proxy panels commonly use self-signed certificates, so verification is off by default |
| `NODE_CHECK_TIMEOUT` | `5.0` | TCP probe timeout for manual node checks, in seconds |
| `HEALTH_INTERVAL` | `30` | Health probe interval for nodes in user pools, in seconds |
| `FAIL_THRESHOLD` | `3` | Consecutive failures before falling back to the next available member |
| `RECOVER_THRESHOLD` | `2` | Consecutive successes before switching back to a higher-priority node |
| `HEALTH_PROBE_URL` | `http://www.gstatic.com/generate_204` | Health probe URL; must be reachable from the node side |
| `HEALTH_PROBE_TIMEOUT` | `5.0` | Timeout for one health probe, in seconds |
| `CLASH_API_HOST` | `127.0.0.1` | sing-box Clash API address; keep it bound to loopback |
| `CLASH_API_PORT` | `9090` | sing-box Clash API port |
| `CLASH_API_SECRET` | empty | sing-box Clash API secret |

## Web UI

Open `http://SERVER_IP:5003`. From there you can:

- import node sources (subscription URL / pasted Clash YAML / node links)
- search and page through nodes (type, server, health, number of user pools containing the node)
- create a user directly from a node row
- delete a node
- page through users and copy a proxy address with one click
- create, revoke, reactivate, delete, reset a password, or edit a user's node pool
- reload sing-box with one click

## Layout

```
main.py           entry point: sing-box proxy + FastAPI Web app
config.py         global config (.env loading, advertised IP detection)
storage.py        SQLite data layer (nodes / users / node pools)

parser/           parse subscription URL / Clash YAML / base64 / node links -> node list
converter/        build and supervise sing-box config from nodes + user pools, with runtime failover/recovery
auth/             user creation, passwords, and node-pool management
web/server.py     FastAPI routes + JSON API + static hosting for the frontend
web/frontend/     Vue 3 + Tailwind CSS + Element Plus source
web/static/       frontend build output (served by the backend at runtime)
lib/              sing-box binaries (Linux / Windows)
data/             users.db / sing-box.json / sing-box.pending.json / sing-box.log
```

Data flow: `parser` produces the node list, `auth` manages users and node pools, `converter` emits and supervises the sing-box config, runtime health probes handle failover and recovery, and `web` ties it together.

## Tech Stack

- Backend: Python 3 + FastAPI + uvicorn + httpx + SQLite
- Frontend: Vue 3 + TypeScript + Vite + Tailwind CSS + Element Plus
- Proxy core: sing-box (mixed inbound + `auth_user` routing)

## FAQ

**What happens when the active node fails?**
After the configured `FAIL_THRESHOLD` consecutive failures, runtime health checks use the Clash API to switch that user to the next available pool member. After the original node reaches `RECOVER_THRESHOLD` consecutive successes, StickyExit switches back automatically. This does not rewrite or reload the configuration file.

**Why is a newly created user's proxy not working?**
A plain new user starts with an empty pool. Add at least one node in the node-pool editor, then click "Reload sing-box".

**Can each user get a dedicated port or rate limit?**
Edit `converter/configgen.py`: give each user a mixed inbound with its own port. It is still a single process.

**Why are there no SSR nodes?**
sing-box does not support SSR natively. Those nodes are filtered out and logged.

**The page says "frontend not built".**
The backend could not find `web/static/index.html`. Locally, run `npm install && npm run build` inside `web/frontend` first. For Docker, confirm the compose file mounts `./web/static:/app/web/static:ro`, and after changing `volumes` recreate the container with `docker compose up -d --force-recreate` rather than `restart`.

**How are subscriptions fetched?**
Fetches go out directly over httpx (async). Proxy panels commonly use self-signed certificates, so `VERIFY_SSL=0` by default. If the domain is blocked by SNI filtering on your host, solve it at the network layer (Clash TUN, system proxy, a different DNS, and so on). Panels also inject fake nodes such as "traffic remaining" or "plan expiring"; StickyExit filters them by the keywords used by common panels (流量 / 重置 / 到期 / 套餐 / 剩余 / 时间).

## Security

1. Never hand the raw subscription to users. They only get username, password, IP, and port.
2. Authentication is enforced whenever at least one active user has a usable pool. With no routable users, the proxy enters closed mode and rejects all traffic.
3. Open only `1080` in the firewall. Do not expose `5003` publicly; if you must, set `WEB_ADMIN_TOKEN` and put it behind a reverse proxy with TLS.
4. Watch the logs for authentication failures to catch password abuse.

## Usage and Compliance

This project redistributes a subscription you have already purchased across your own network. Check that your provider's terms allow it before use, and do not resell access or use it for anything unlawful. Subscription contents are never sent to users; they only receive `username:password@ip:port`.

## License

[MIT](LICENSE)
