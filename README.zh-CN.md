# StickyExit

[English](README.md) | 简体中文

把一份机场订阅分发成多个长效代理，每个用户固定一个出口 IP。

## 背景

业务中需要多个长期可用的出口 IP。商用长效代理按 IP 计费，成本较高。

机场订阅中的每个节点都是一台独立服务器，对应一个出口 IP。本项目把这些节点作为出口池，在本机开启一个带认证的代理端口，每个用户绑定其中一个节点，从而得到多个出口 IP。

```
用户 alice ──┐
             ├─ mixed:1080（认证） ─ routing ─┬─> 节点 A（出口 IP 1）
用户 bob   ──┘                                └─> 节点 B（出口 IP 2）
```

用户侧只有一个 `用户名:密码@服务器IP:1080`，HTTP 与 SOCKS5 共用这个端口，由 sing-box 的 mixed 入站加 `auth_user` 路由规则分发到各自节点。

## 功能

- 单个 sing-box 进程、单个入口端口，内存占用小
- 用户与节点一对一绑定，互不串线
- 节点导入支持订阅链接、Clash YAML、base64、节点链接（每行一条），导入为追加而非覆盖
- SQLite 持久化节点与用户，重启后保留原有分配
- 节点健康检查（TCP 拨测）与故障迁移
- 配置先经 `sing-box check` 校验，再热重载
- Web 管理页：节点分页与搜索、用户分页、节点上快捷建号、删除节点、一键复制代理地址
- Docker 部署，数据与 sing-box 二进制通过 volume 挂载

## 限制

- 出口 IP 来自订阅节点，节点失效或被更换时，出口 IP 会随之变化。
- 稳定性取决于所购买的机场服务。
- 节点失效时，程序会把上面对应的用户迁移到其它健康节点。

## 快速开始

### Docker 部署

```bash
git clone <你的仓库地址> sticky-exit
cd sticky-exit

# 可选：按需修改端口、管理口令、对外 IP
cp .env.example .env

docker compose up -d
```

启动后：

- Web 管理页：`http://服务器IP:5003`
- 代理端口：`1080`（HTTP 与 SOCKS5 共用）

带管理口令时，打开 `http://服务器IP:5003/?token=你的口令`。

> **前端产物必须挂载。** `web/static` 在 `.dockerignore` 中，不会打进镜像，运行时由 `docker-compose.yml` 里的
> `- ./web/static:/app/web/static:ro` 挂载进容器。如果缺少这行挂载，页面会报
> 「前端未构建」，容器内 `/app/web/static/index.html` 也不存在。
> 修改了 `volumes` 后必须重建容器，`docker compose restart` 不会生效：
>
> ```bash
> docker compose up -d --force-recreate
> ```
>
> 老版本 compose 把命令换成 `docker-compose` 即可。

`lib/` 目录中的 sing-box 二进制同样以 volume 挂载进容器。仓库自带 Linux amd64 与 Windows 版本，其它架构（如 arm64）请自行替换 `lib/sing-box-linux/sing-box`。

### 本地运行

```bash
pip install -r requirements.txt
python main.py
```

要求 Python 3.10+。启动后 Web 管理页在 `5003`，代理端口在 `1080`。

节点池为空时只启动 Web 页面，导入节点后会自动拉起 sing-box。

### 前端开发

```bash
cd web/frontend
npm install
npm run dev     # 开发服务器，/api 自动代理到 127.0.0.1:5003
npm run build   # 构建产物输出到 web/static，由后端托管
```

## 使用流程

1. 打开 Web 管理页，导入节点（订阅链接 / Clash YAML / 节点链接）。
2. 新建用户，程序自动分配一个与其它用户不同的节点。
3. 复制页面上的代理地址，发给对应用户。

### 节点与用户的增删

- 导入节点是**追加**操作，不会覆盖已有节点，也不影响已有用户的分配。
- 删除节点时，绑定在该节点上的用户会自动迁移到其它健康节点。
- 停用用户会立刻作废其口令；恢复用户时发放全新口令，旧口令不会恢复。

## 交付给用户的内容

建好用户后，页面直接显示：

```
http://alice:xxxxx@服务器IP:1080
IP=服务器IP  端口=1080  用户名=alice  密码=xxxxx
```

用户在浏览器、系统代理或 Proxifier 中填入即可，不需要 Clash，也看不到原始订阅。

## 配置项

配置来自 `.env`，也可直接用环境变量覆盖。所有项均可省略，省略时使用内置默认值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTEN_IP` | `0.0.0.0` | 代理监听地址 |
| `LISTEN_PORT` | `1080` | 代理端口（HTTP + SOCKS5 同端口） |
| `WEB_HOST` | `0.0.0.0` | Web 管理页监听地址 |
| `WEB_PORT` | `5003` | Web 管理页端口 |
| `WEB_ADMIN_TOKEN` | 空 | 管理页访问口令；留空=不认证（内网/反代后使用）。设置后打开页面需 `?token=密码`，所有 `/api/*` 接口需携带 `?token=` 或 `X-Admin-Token` 请求头 |
| `ADVERTISE_IP` | 空 | 展示给用户的对外 IP。留空自动探测：`LISTEN_IP` 具体值 > 公网 IP > 本机内网 IP；服务器有公网 IP 建议直接填 |
| `VERIFY_SSL` | `0` | 拉取订阅是否校验证书；机场面板多为自签名证书，默认不校验 |
| `NODE_CHECK_TIMEOUT` | `5.0` | 节点健康检查 TCP 拨测超时（秒） |

## Web 管理页

浏览器打开 `http://服务器IP:5003`，可以：

- 导入节点源（订阅链接 / 粘贴 Clash YAML / 节点链接）
- 分页、搜索节点列表（类型、服务器、健康状态、已绑用户数）
- 在节点行上直接给该节点建用户
- 删除节点（自动迁移受影响用户）
- 分页查看用户列表，一键复制代理地址
- 建用户、停用、恢复、删除、改密、换节点
- 一键重载 sing-box

## 目录结构

```
main.py           入口：启动 sing-box 代理 + FastAPI 管理页
config.py         全局配置（.env 读取、对外 IP 探测）
storage.py        SQLite 数据层（节点 / 用户）

parser/           解析：订阅链接 / Clash YAML / base64 / 节点链接 -> 节点列表
converter/        生成：节点 + 用户 -> sing-box 配置，校验与进程守护
auth/             认证：建号、口令、用户到节点的分配与故障迁移
web/server.py     FastAPI 路由 + JSON API + 前端静态托管
web/frontend/     Vue 3 + Tailwind CSS + Element Plus 源码
web/static/       前端构建产物（运行时由后端托管）
lib/              sing-box 二进制（Linux / Windows）
data/             users.db / sing-box.json / sing-box.log
```

数据流：`parser` 得到节点列表，`auth` 完成建号与分配，`converter` 输出 sing-box 配置，`web` 统一管理。

## 技术栈

- 后端：Python 3 + FastAPI + uvicorn + httpx + SQLite
- 前端：Vue 3 + TypeScript + Vite + Tailwind CSS + Element Plus
- 代理核心：sing-box（mixed 入站 + `auth_user` 路由）

## 常见问题

**Q: 用户数超过节点数怎么办？**
分配器会先复用（降级）。要严格一人一节点就限制用户数，或把多家订阅导入同一个节点池。

**Q: 节点挂了怎么办？**
在 Web 页重载或重新导入节点时，会把挂掉节点上的用户迁移到其它健康节点。

**Q: 想给每个用户独立端口 / 独立限速？**
改 `converter/configgen.py`：每个用户一个 mixed 入站加独立端口即可，进程仍然只有一个。

**Q: SSR 节点为什么没有？**
sing-box 原生不支持 SSR，这类节点会被自动过滤并写日志。

**Q: 页面提示「前端未构建」？**
后端没有找到 `web/static/index.html`。本地先在 `web/frontend` 执行 `npm install && npm run build`；Docker 部署确认 compose 里挂载了 `./web/static:/app/web/static:ro`，改完 volumes 用 `docker compose up -d --force-recreate` 重建容器，而不是 `restart`。

**Q: 拉取订阅的网络说明？**
订阅拉取为直连（httpx 异步）。机场面板常使用自签名证书，默认 `VERIFY_SSL=0` 不校验证书。若本地域名被 SNI 阻断，需要自行在网络层解决（Clash TUN / 系统代理 / 换 DNS 等）。面板会把「剩余流量 / 套餐到期」等伪装成节点，程序按关键词自动过滤（流量 / 重置 / 到期 / 套餐 / 剩余 / 时间）。

## 安全建议

1. 原始订阅不要发给用户，用户只拿到用户名、密码、IP、端口。
2. `users` 非空即强制认证，未认证一律返回 407。
3. 防火墙只放开 `1080`；`5003` 不要直接暴露公网，需要暴露时至少设置 `WEB_ADMIN_TOKEN`，并放在反向代理与 TLS 之后。
4. 定期查看日志中的认证失败记录，防止口令被刷。

## 使用与合规

本项目用于把已购买的订阅在自有网络中分发。使用前请确认服务商条款允许，不要用于对外售卖或其它违法用途。订阅内容不会下发给用户，用户只拿到 `用户名:密码@IP:端口`。

## License

[MIT](LICENSE)
