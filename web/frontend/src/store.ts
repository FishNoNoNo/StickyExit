import { ref } from "vue";
import { api, post, postJSON } from "./api";
import type {
  CreatedUser,
  ImportResult,
  NodeOption,
  NodeRow,
  Status,
  UserRow,
} from "./types";

export const status = ref<Status | null>(null);

// 列表都是分页拉取：只保存当前页 + 总数，不再全量拉到浏览器。
export const nodes = ref<NodeRow[]>([]);
export const nodesTotal = ref(0);
export const nodePage = ref(1);
export const nodePageSize = ref(10);
/** 节点列表搜索关键词（按名称 / tag / 服务器过滤，后端执行） */
export const nodeSearch = ref("");

export const users = ref<UserRow[]>([]);
export const usersTotal = ref(0);
export const userPage = ref(1);
export const userPageSize = ref(10);

/** 「换节点」下拉要看到全部节点，所以单独走轻量接口。 */
export const nodeOptions = ref<NodeOption[]>([]);

/** 状态轮询失败保持静默，避免网络抖动时弹一堆错误。 */
export async function loadStatus(): Promise<void> {
  try {
    status.value = await api<Status>("/api/status");
  } catch {
    /* ignore */
  }
}

type Paged<T> = T & {
  total: number;
  page: number;
  page_size: number;
};

export async function loadNodes(
  page = nodePage.value,
  pageSize = nodePageSize.value,
  search = nodeSearch.value,
): Promise<void> {
  nodePage.value = page;
  nodePageSize.value = pageSize;
  nodeSearch.value = search;
  const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  const term = search.trim();
  if (term) qs.set("search", term);
  const d = await api<Paged<{ nodes: NodeRow[] }>>(`/api/nodes?${qs.toString()}`);
  nodes.value = d.nodes;
  nodesTotal.value = d.total;
}

export async function loadUsers(
  page = userPage.value,
  pageSize = userPageSize.value,
): Promise<void> {
  userPage.value = page;
  userPageSize.value = pageSize;
  const d = await api<Paged<{ users: UserRow[] }>>(
    `/api/users?page=${page}&page_size=${pageSize}`,
  );
  users.value = d.users;
  usersTotal.value = d.total;
}

export async function loadNodeOptions(): Promise<void> {
  const d = await api<{ nodes: NodeOption[] }>("/api/nodes/options");
  nodeOptions.value = d.nodes;
}

/** 刷新用户列表；当前页被删空时自动退到最后一页。 */
async function refreshUsers(): Promise<void> {
  await loadUsers();
  const lastPage = Math.max(1, Math.ceil(usersTotal.value / userPageSize.value));
  if (users.value.length === 0 && usersTotal.value > 0 && userPage.value > lastPage) {
    await loadUsers(lastPage);
  }
}

/** 刷新节点列表；当前页被删空时自动退到最后一页。 */
async function refreshNodes(): Promise<void> {
  await loadNodes();
  const lastPage = Math.max(1, Math.ceil(nodesTotal.value / nodePageSize.value));
  if (nodes.value.length === 0 && nodesTotal.value > 0 && nodePage.value > lastPage) {
    await loadNodes(lastPage);
  }
}

/** 新建用户后跳到最后一页，让新用户立刻可见（列表按 id 升序）。 */
async function showUser(username: string): Promise<void> {
  await refreshUsers();
  if (users.value.some((u) => u.username === username)) return;
  const lastPage = Math.max(1, Math.ceil(usersTotal.value / userPageSize.value));
  if (lastPage !== userPage.value) await loadUsers(lastPage);
}

export async function importNodes(
  sourceType: "url" | "text",
  source: string,
): Promise<ImportResult> {
  const d = await postJSON<ImportResult>("/api/nodes/import", {
    source_type: sourceType,
    source,
  });
  await Promise.all([loadStatus(), refreshNodes(), refreshUsers(), loadNodeOptions()]);
  return d;
}

export async function reloadSingBox(): Promise<void> {
  await post("/api/reload");
  await loadStatus();
}

export async function createUser(username: string, prefer?: string): Promise<CreatedUser> {
  const body: { username: string; prefer?: string } = { username };
  if (prefer) body.prefer = prefer;
  const d = await postJSON<{ user: CreatedUser }>("/api/users", body);
  // 新用户会占用某个节点，节点列表里的「用户数」随之变化，必须一起刷新。
  await Promise.all([
    loadStatus(), showUser(d.user.username), loadNodeOptions(), refreshNodes(),
  ]);
  return d.user;
}

/** 删除节点：绑在该节点上的用户由后端自动迁移。 */
export async function deleteNode(
  tag: string,
): Promise<{ migrated: string[]; doomed: string[] }> {
  const d = await post<{ migrated: string[]; doomed: string[] }>(
    "/api/nodes/" + encodeURIComponent(tag) + "/delete",
  );
  await Promise.all([loadStatus(), refreshNodes(), refreshUsers(), loadNodeOptions()]);
  return d;
}

export async function revokeUser(username: string): Promise<void> {
  await post("/api/users/" + encodeURIComponent(username) + "/revoke");
  await Promise.all([loadStatus(), refreshUsers(), refreshNodes()]);
}

export async function activateUser(username: string): Promise<CreatedUser> {
  const d = await post<{ user: CreatedUser }>(
    "/api/users/" + encodeURIComponent(username) + "/activate",
  );
  await Promise.all([loadStatus(), refreshUsers(), loadNodeOptions(), refreshNodes()]);
  return d.user;
}

export async function deleteUser(username: string): Promise<void> {
  await post("/api/users/" + encodeURIComponent(username) + "/delete");
  await Promise.all([loadStatus(), refreshUsers(), refreshNodes()]);
}

export async function resetPassword(
  username: string,
  password: string,
): Promise<{ password: string; proxy: string }> {
  return postJSON<{ password: string; proxy: string }>(
    "/api/users/" + encodeURIComponent(username) + "/password",
    { password },
  );
}

export async function switchNode(username: string, tag: string): Promise<void> {
  await postJSON("/api/users/" + encodeURIComponent(username) + "/switch", { tag });
  await Promise.all([loadStatus(), refreshUsers(), refreshNodes()]);
}
