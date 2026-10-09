# 前端节点池改造交接（已完成）

> 本文是节点池改造时的工作记录，代码已经落地。当前契约以 README 和 `web/server.py` 为准。

本前端是 Vue 3 + TypeScript + Vite + Tailwind CSS + Element Plus 的管理前端（`D:\code\my_project\proxy-distributor\web\frontend`）。改造时后端 API 已经全部改完并验证过，前端完成改造后通过 `npm run build` 生成到 `web/static`。

## 背景：后端已经改成什么样子（这些事实是权威，别猜）

产品从「一用户绑死一个节点」改成了「一用户一个节点池」。池的**数组顺序就是优先级**（越靠前越优先）；运行时优先走池里第一个可用节点，它连续失败 N 次才自动降到下一个，恢复后自动回到优先级最高的。池与池**允许重叠**；池可以为空；池大小无上限。用户的代理地址（`http://user:pass@host:1080`）**一个字节都没变**，客户端零感知。

后端 API 的实际契约如下：

### `GET /api/users`（每页一批用户）
```json
{
  "users": [
    {
      "username": "c1",
      "status": "active",
      "nodes": [                      // 节点池，按优先级排列，可为空数组
        {"tag": "node-6cf9a848", "name": "香港01", "healthy": true},
        {"tag": "node-4e721288", "name": "日本02", "healthy": false}
      ],
      "active_node": "node-6cf9a848", // 当前实际选中的成员（Clash API 取），空串=未知
      "created_at": "…", "revoked_at": null,
      "proxy": "http://c1:xxx@1.2.3.4:1080"
    }
  ],
  "total": 15, "page": 1, "page_size": 10
}
```
### `POST /api/users`（建号）→ 返回 `{ "user": { "username", "password", "nodes": [...], "proxy" } }`
> 入参可以带 `prefer: "<tag>"`，含义=「把这个节点放进池首」（节点行上的「加用户」按钮走它）。`nodes` 是数组。

### `POST /api/users/{username}/pool`
- 入参 `{ "tags": ["node-a", "node-b"] }` —— **数组顺序就是优先级**。
- 允许 `tags: []`（空池=该用户暂时不进代理；后端接受），长度无上限。
- 后端会自己去重、校验 tag 存在；返回 `{ ok, tags, pending_reload }`。

### `POST /api/nodes/{tag}/delete`
- 返回 **`{ ok, tag, removed_from: string[], pending_reload }`**（`removed_from` = 哪些用户的池里含这个节点）。
- 语义：节点删除只是把这个 tag 从所有用户池里摘掉，**没有自动迁移、没有 doomed**。被摘空的池就空着。

### 其它没变的
- `GET /api/nodes`：`users` 字段含义变成「多少个 active 用户的池包含这个节点」（可能大于用户总数，因为池可重叠）。
- `GET /api/nodes/options`：`{ nodes: [{ tag, name, healthy }] }`，没变，复用给池编辑器。
- `/api/status`、导入、停用/恢复/改密/删除用户：没变。

## 改造时的文件清单（已完成）

### 1. `src/types.ts`
- `UserRow`（现 [:32-40](web/frontend/src/types.ts#L32-L40)）：删除 `node_tag: string` 和 `node_name: string`，改成：
```ts
export interface PoolNode {
  tag: string;
  name: string;
  healthy: boolean;
}
export interface UserRow {
  username: string;
  status: string;
  nodes: PoolNode[];
  /** 当前实际走的是池里哪个节点（由后端从 Clash API 取，空串=未知） */
  active_node?: string;
  created_at: string;
  revoked_at?: string | null;
  proxy: string;
}
```
- `CreatedUser`（现 [:42-47](web/frontend/src/types.ts#L42-L47)）：删 `node_tag`，改成 `nodes: PoolNode[]`。

### 2. `src/store.ts`
- 把 `switchNode(username, tag)`（现 [:186-189](web/frontend/src/store.ts#L186-L189)）整体替换成：
```ts
export async function setPool(username: string, tags: string[]): Promise<void> {
  await postJSON("/api/users/" + encodeURIComponent(username) + "/pool", { tags });
  await Promise.all([loadStatus(), refreshUsers(), refreshNodes()]);
}
```
- `deleteNode`（现 [:147-156](web/frontend/src/store.ts#L147-L156)）的返回类型从 `{ migrated: string[]; doomed: string[] }` 改成 `{ removed_from: string[] }`，注释同步更新。
- 老注释一律清掉：`nodeOptions` 顶部那行「换节点下拉…」（[:27](web/frontend/src/store.ts#L27)）、`createUser` 里「新用户会占用某个节点…」（[:140](web/frontend/src/store.ts#L140)）——现在新用户占用的是一池节点。

### 3. `src/components/UsersPanel.vue`（工作量最大，仔细看）
**节点列**（现 [:227-234](web/frontend/src/components/UsersPanel.vue#L227-L234)）：现在渲染 `row.node_name` + `<code>{{ row.node_tag }}</code>`，改成渲染**有序池**：
- 用 `row.nodes`（`PoolNode[]`）按顺序（=优先级，不用再排）渲染小 `el-tag`，每个标签显示节点名；`row.active_node` 对应的那个成员额外加一个高亮（比如 type="success" 或加个「在用」角标）。
- 空池显示「未配置」。
- 池太长会挤爆列宽——列宽 `min-width` 放宽到 220 左右，最多展示前 3 个成员，超出用「+N」折叠（用 `el-tooltip` 显示全量）。

**操作列的「换节点」按钮**（现 [:258](web/frontend/src/components/UsersPanel.vue#L258)）→ 改成「节点池」。它打开的不再是单选的 `el-select`，而是一个**池编辑器对话框**（顺序=优先级，用户要能调顺序）：

- 对话框内容：当前已选的节点列表，每行 = 节点名 + tag + 上移/下移/移除三个小按钮 + 一个「添加节点」的下拉（从 `nodeOptions` 多选或单选添加）。
- 开关对话框时把 `row.nodes` 里每个成员的 tag 装进 `ref<string[]>` 作为初值；确定时按这个顺序提交 `POST /api/users/{username}/pool`。
- 已有对话框在 [:305-318](web/frontend/src/components/UsersPanel.vue#L305-L318)，整个替换；`import { Switch } from "@element-plus/icons-vue"` 里的 `Switch` 图标可以换掉或留着按需（此时可能用 `Connection` / `Top` / `Bottom` / `Delete` 之类的图标）。
- 状态 ref 从 `switchTarget: ref("")` 改成 `poolTags: ref([])`；按钮 loading、确认、取消流程沿用现有模式。
- 换的交互文案：成功提示里把旧的「已切换到 x」改成「节点池已更新；重载后生效」。

**建号成功 toast**（现 [:58-65](web/frontend/src/components/UsersPanel.vue#L58-L65)）：`u.node_tag` 已不存在，改成展示池成员数 + 成员名，例如：
```
已创建 alice，节点池 3 个：香港01、日本02、新加坡03
代理: http://alice:xxx@1.2.3.4:1080
注意：配置需手动点"重载"后才能生效
```
（成员名取 `u.nodes[i].name`，最多列 3 个，多出的写「等 N 个」。）

**恢复成功 toast**（现 [:103-108](web/frontend/src/components/UsersPanel.vue#L103-L108)）：同样把 `u.node_tag` 换成池信息。

**「节点池」按钮的初值**（现 `openSwitch(row.username, row.node_tag)`）：改成 `openPool(row.username, row.nodes)`（把整组 tag 传进去当初值）。

> 加排序的 UI 不用做得很花哨：一条已选列表 + 上移/下移/移除 + 添加控件就够。顺序是产品的语义，一定要能改。

### 4. `src/components/NodesTable.vue`
- 「加用户」成功 toast（现 [:63-68](web/frontend/src/components/NodesTable.vue#L63-L68)）：`"已为用户 " + u.username + " 指定节点 " + u.node_tag` 改成 `u.username + " 已建号，节点 " + targetName.value + " 放入其节点池首位"`（或等效表述；`prefer` 使该节点位于池首）。`targetName` 这个 ref 已存在（[:19](web/frontend/src/components/NodesTable.vue#L19)）。
- 删节点确认文案（现 [:77-92](web/frontend/src/components/NodesTable.vue#L77-L92)）：`d.migrated` / `d.doomed` 已不存在。改成新语义：**节点会从所有包含它的用户池里移除，池可能因此变空**。成功回调里用 `d.removed_from` 提示「影响 N 个用户的节点池」（为 0 就不提示）。
- `用户数` 列（[:177-179](web/frontend/src/components/NodesTable.vue#L177-L179)）文案注释不用改字段，但含义已变成「多少用户的池包含它」——如果同步更新的列 header 或 tooltip 有说明文字，顺带改；否则数字照常显示即可。

### 5. 改造时的收尾
- `cd web\frontend && npm install && npm run build`，确认产物写进 `web/static/`（`.dockerignore` 排除了它，运行时由后端挂载/托管，注意 `web/static/assets` 里的旧 bundle 文件名会被新 hash 覆盖替换，`index.html` 引用也跟着变——让 build 自己处理）。
- `npm run dev` 可连 `127.0.0.1:5003` 的后端联调（vite proxy 已配好）。
- 用 `vue-tsc` / `tsc` 过一遍类型，别留 `node_tag` 引用。

## 约束
- **全程中文 UI 文案**，与现有页面一致。
- 成功提示统一带「需手动点重载后才生效」（后端现在是手动重载模型）。
- Element Plus 组件保持 auto-import 风格，不用显式 import；新的 `el-tooltip` / 图标若未被 `components.d.ts` / `auto-imports.d.ts` 收录，直接按本仓库惯例使用，build 时会自动补充。
- 不要修改任何后端文件；后端契约以本提示上方为准。
- 后端已在 `data/users.db` 完成数据迁移（15 用户已各自有池），可以直接对着真实数据联调。