# StickyExit

English | [简体中文](README.zh-CN.md)

Turn a single proxy subscription into multiple long-lived proxies, with one fixed exit IP per user.

## Background

Some workloads need several long-lived exit IPs. Commercial long-lived proxies are billed per IP, which makes them expensive for this use case.

Every node in a proxy subscription is its own server with its own exit IP. StickyExit treats those nodes as an exit pool, runs one authenticated proxy port on your machine, and binds each user to one node. The result is multiple exit IPs served through a single port.

```
user alice ──┐
             ├─ mixed:1080 (auth) ─ routing ─┬─> node A (exit IP 1)
user bob   ──┘                               └─> node B (exit IP 2)
```

Each user only needs `username:password@server-ip:1080`. HTTP and SOCKS5 share that port, and sing-box dispatches each user to their own node through a mixed inbound plus `auth_user` routing rules.

## Features

- One sing-box process and one entry port; low memory footprint
- One-to-one user-to-node binding, so users never share an exit
- Node import from subscription URL, Clash YAML, base64, or plain node links (one per line). Import appends to the pool instead of replacing it
- SQLite persistence for nodes and users; existing assignments survive restarts
- Node health checks (TCP probe) and automatic failover
- Config validated with `sing-box check` before every hot reload
- Web UI: searchable and paginated node list, paginated user list, create a user directly from a node row, delete a node, one-click copy of proxy addresses
- Docker deployment with data and the sing-box binary mounted as volumes

## Limitations

- Exit IPs come from the subscription nodes. When a node dies or is replaced, the corresponding exit IP changes.
- Stability depends on the proxy service you buy.
- When a node fails, StickyExit migrates the users bound to it onto other healthy nodes.

## Quick Start

### Docker

```bash
git clone <your-repo-url> sticky-exit
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

If the node pool is empty, only the Web UI starts. sing-box is launched automatically after you import nodes.

### Frontend Development

```bash
cd web/frontend
npm install
npm run dev     # dev server, proxies /api to 127.0.0.1:5003
npm run build   # outputs to web/static, served by the backend
```

## Usage

1. Open the Web UI and import nodes (subscription URL / Clash YAML / node links).
2. Create a user; StickyExit assigns a node that no other user currently holds.
3. Copy the proxy address shown on the page and hand it to that user.

### Adding and Removing

- Importing nodes **appends** to the pool. It does not overwrite existing nodes or change existing assignments.
- Deleting a node automatically migrates the users bound to it to other healthy nodes.
- Revoking a user invalidates the password immediately. Activating the user issues a brand new password; the old one is never restored.

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
| `NODE_CHECK_TIMEOUT` | `5.0` | TCP probe timeout for node health checks, in seconds |

## Web UI

Open `http://SERVER_IP:5003`. From there you can:

- import node sources (subscription URL / pasted Clash YAML / node links)
- search and page through nodes (type, server, health, bound user count)
- create a user directly from a node row
- delete a node (affected users are migrated)
- page through users and copy a proxy address with one click
- create, revoke, reactivate, delete, re-password, or re-assign users
- reload sing-box with one click

## Layout

```
main.py           entry point: sing-box proxy + FastAPI Web app
config.py         global config (.env loading, advertised IP detection)
storage.py        SQLite data layer (nodes / users)

parser/           parse subscription URL / Clash YAML / base64 / node links -> node list
converter/        build sing-box config from nodes + users, validate, supervise the process
auth/             user creation, passwords, user-to-node assignment and failover
web/server.py     FastAPI routes + JSON API + static hosting for the frontend
web/frontend/     Vue 3 + Tailwind CSS + Element Plus source
web/static/       frontend build output (served by the backend at runtime)
lib/              sing-box binaries (Linux / Windows)
data/             users.db / sing-box.json / sing-box.log
```

Data flow: `parser` produces the node list, `auth` creates users and assigns nodes, `converter` emits the sing-box config, and `web` ties it together.

## Tech Stack

- Backend: Python 3 + FastAPI + uvicorn + httpx + SQLite
- Frontend: Vue 3 + TypeScript + Vite + Tailwind CSS + Element Plus
- Proxy core: sing-box (mixed inbound + `auth_user` routing)

## FAQ

**What happens when there are more users than nodes?**
The allocator reuses nodes. If you need strictly one node per user, cap the user count or import several subscriptions into the same pool.

**What happens when a node goes down?**
When you reload or import nodes from the Web UI, users on the dead node are migrated to other healthy nodes.

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
2. Once any user exists, authentication is enforced; unauthenticated requests get 407.
3. Open only `1080` in the firewall. Do not expose `5003` publicly; if you must, set `WEB_ADMIN_TOKEN` and put it behind a reverse proxy with TLS.
4. Watch the logs for authentication failures to catch password abuse.

## Usage and Compliance

This project redistributes a subscription you have already purchased across your own network. Check that your provider's terms allow it before use, and do not resell access or use it for anything unlawful. Subscription contents are never sent to users; they only receive `username:password@ip:port`.

## License

[MIT](LICENSE)
