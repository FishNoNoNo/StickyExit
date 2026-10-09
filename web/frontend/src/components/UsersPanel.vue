<script setup lang="ts">
import { computed, h, ref } from "vue";
import { Bottom, Connection, CopyDocument, Delete, Edit, Plus, Top } from "@element-plus/icons-vue";
import { copyText } from "../clipboard";
import {
  activateUser,
  createUser,
  deleteUser,
  loadNodeOptions,
  loadUsers,
  nodeOptions as allNodeOptions,
  resetPassword,
  revokeUser,
  setPool,
  status,
  userPage,
  userPageSize,
  users,
  usersTotal,
} from "../store";
import type { PoolNode } from "../types";

const newUser = ref("");
const creating = ref(false);

const poolDialog = ref(false);
const poolUser = ref("");
const poolTags = ref<string[]>([]);
const poolAddTag = ref("");
const savingPool = ref(false);

/** 节点池编辑器下拉用全量轻量节点列表：用户列表分页后不能只依赖当前页。 */
const options = computed(() =>
  allNodeOptions.value.map((n) => ({
    label: n.name + "（" + n.tag + "）",
    value: n.tag,
  })),
);

/** 池里已有成员不再出现在「添加节点」下拉里。 */
const addOptions = computed(() =>
  options.value.filter((o) => !poolTags.value.includes(o.value)),
);

const nameByTag = computed(() => {
  const m = new Map<string, string>();
  for (const n of allNodeOptions.value) m.set(n.tag, n.name);
  return m;
});

function nodeName(tag: string): string {
  return nameByTag.value.get(tag) ?? tag;
}

/** 节点列预览：最多展示前 3 个，其余折叠成「+N」。 */
const POOL_PREVIEW = 3;
function poolShown(nodes: PoolNode[]): PoolNode[] {
  return nodes.slice(0, POOL_PREVIEW);
}
function poolMore(nodes: PoolNode[]): number {
  return Math.max(0, nodes.length - POOL_PREVIEW);
}

/** 悬停显示整个池：按优先级列出，标注异常 / 在用。 */
function poolDetail(nodes: PoolNode[], active?: string): string {
  if (!nodes.length) return "未配置";
  return nodes
    .map((n, i) => {
      let line = i + 1 + ". " + (n.name || n.tag);
      if (!n.healthy) line += "（异常）";
      if (active && n.tag === active) line += "（在用）";
      return line;
    })
    .join("\n");
}

/** 池成员名摘要：最多列 3 个，多出的写成「等 N 个」。 */
function poolNamesText(nodes: PoolNode[]): string {
  const names = nodes.map((n) => n.name || n.tag);
  if (names.length === 0) return "（空池）";
  const head = names.slice(0, 3).join("、");
  return names.length > 3 ? head + " 等 " + names.length + " 个" : head;
}

/** 多行提示用 pre-line，避免 \n 被折叠成空格。 */
function multiline(text: string) {
  return h("div", { style: "white-space: pre-line" }, text);
}

/** 复制代理地址。内网 http 页面没有 clipboard API，走 clipboard.ts 的兜底。 */
async function onCopyProxy(text: string) {
  const ok = await copyText(text);
  if (ok) ElMessage.success("已复制代理地址");
  else ElMessage.error("复制失败，请手动选中复制");
}

async function onCreate() {
  const username = newUser.value.trim();
  if (!username) {
    ElMessage.warning("请输入用户名");
    return;
  }
  creating.value = true;
  try {
    const u = await createUser(username);
    ElMessage.success({
      message: multiline(
        (u.nodes.length === 0
          ? "已创建 " + u.username + "，节点池为空\n请用“节点池”按钮配置节点"
          : "已创建 " + u.username + "，节点池 " + u.nodes.length + " 个：" + poolNamesText(u.nodes)) +
        "\n代理: " + u.proxy +
        "\n注意：配置需手动点\"重载\"后才能生效",
      ),
      duration: 8000,
    });
    newUser.value = "";
  } catch (e) {
    ElMessage.error("建号失败: " + (e as Error).message);
  } finally {
    creating.value = false;
  }
}

async function onRevoke(name: string) {
  try {
    await ElMessageBox.confirm(
      "停用用户 " + name + "？该用户的代理凭据会立即作废，旧口令无法再使用。",
      "停用确认",
      { type: "warning", confirmButtonText: "停用", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await revokeUser(name);
    ElMessage.success("已停用 " + name + "，其凭据已作废；重载后配置正式生效");
  } catch (e) {
    ElMessage.error("停用失败: " + (e as Error).message);
  }
}

async function onActivate(name: string) {
  try {
    await ElMessageBox.confirm(
      "恢复用户 " + name + "？将保留原节点池（为空则保持空池），并发放全新口令（旧口令不再有效）。",
      "恢复确认",
      { type: "warning", confirmButtonText: "恢复", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    const u = await activateUser(name);
    ElMessage.success({
      message: multiline(
        "已恢复 " + name + "\n节点池 " + u.nodes.length + " 个：" + poolNamesText(u.nodes) +
        "\n" + u.proxy +
        "\n注意：配置需手动点\"重载\"后才能生效",
      ),
      duration: 8000,
    });
  } catch (e) {
    ElMessage.error("恢复失败: " + (e as Error).message);
  }
}

async function onDelete(name: string) {
  try {
    await ElMessageBox.confirm(
      "彻底删除用户 " + name + "？记录与口令都会被清除，无法恢复。",
      "删除确认",
      { type: "error", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteUser(name);
    ElMessage.success("已删除 " + name);
  } catch (e) {
    ElMessage.error("删除失败: " + (e as Error).message);
  }
}

async function onResetPassword(name: string) {
  let pw = "";
  try {
    const r = await ElMessageBox.prompt(
      "为 " + name + " 设置新口令（留空则自动生成）：",
      "重置口令",
      {
        inputPlaceholder: "留空自动生成",
        confirmButtonText: "确定",
        cancelButtonText: "取消",
      },
    );
    pw = r.value || "";
  } catch {
    return;
  }
  try {
    const d = await resetPassword(name, pw);
    ElMessage.success({
      message: multiline(name + " 新口令: " + d.password + "\n" + d.proxy +
        "\n注意：需手动点\"重载\"后新口令才在服务端生效"),
      duration: 10000,
    });
  } catch (e) {
    ElMessage.error("改密失败: " + (e as Error).message);
  }
}

/** 打开节点池编辑器，把当前池的 tag 顺序灌进编辑态。 */
function openPool(name: string, nodes: PoolNode[]) {
  poolUser.value = name;
  poolTags.value = nodes.map((n) => n.tag);
  poolAddTag.value = "";
  poolDialog.value = true;
  void loadNodeOptions();
}

function movePool(i: number, dir: -1 | 1) {
  const j = i + dir;
  const arr = poolTags.value;
  if (j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j], arr[i]];
}

function removePool(i: number) {
  poolTags.value.splice(i, 1);
}

function onAddMember() {
  const tag = poolAddTag.value;
  if (!tag) return;
  if (!poolTags.value.includes(tag)) poolTags.value.push(tag);
  poolAddTag.value = "";
}

async function onConfirmPool() {
  savingPool.value = true;
  try {
    await setPool(poolUser.value, poolTags.value.slice());
    ElMessage.success("节点池已更新；重载后生效");
    poolDialog.value = false;
  } catch (e) {
    ElMessage.error("更新失败: " + (e as Error).message);
  } finally {
    savingPool.value = false;
  }
}

async function onPageChange(page: number) {
  await loadUsers(page, userPageSize.value);
}

async function onSizeChange(size: number) {
  await loadUsers(1, size);
}

/** 停用的用户整行变淡；类名会被 Tailwind 扫描到并生成。 */
function rowClass({ row }: { row: { status: string } }) {
  return row.status === "active" ? "" : "opacity-55";
}
</script>

<template>
  <section class="mb-5 rounded-[10px] border border-line bg-card p-4">
    <h2 class="m-0 mb-3 text-[15px] font-semibold">用户</h2>

    <div class="mb-3 flex flex-wrap gap-2.5">
      <el-input
        v-model="newUser"
        placeholder="用户名"
        class="w-[200px]"
        @keyup.enter="onCreate"
      />
      <el-button type="primary" :icon="Plus" :loading="creating" @click="onCreate">
        建用户（空池起步）
      </el-button>
    </div>

    <el-table :data="users" size="small" :row-class-name="rowClass" empty-text="暂无用户">
      <el-table-column prop="username" label="用户名" min-width="120" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small">
            {{ row.status === "active" ? "启用" : "停用" }}
          </el-tag>
          <div v-if="row.status !== 'active'" class="text-xs text-muted">
            {{ row.revoked_at || "" }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="节点池" min-width="220">
        <template #default="{ row }">
          <span v-if="!row.nodes || row.nodes.length === 0" class="text-xs text-muted">未配置</span>
          <el-tooltip v-else placement="top" effect="dark">
            <template #content>
              <div style="white-space: pre-line">{{ poolDetail(row.nodes, row.active_node) }}</div>
            </template>
            <span class="inline-flex flex-wrap items-center gap-1">
              <el-tag
                v-for="n in poolShown(row.nodes)"
                :key="n.tag"
                size="small"
                :type="n.tag === row.active_node ? 'success' : 'info'"
                :effect="n.tag === row.active_node ? 'dark' : 'plain'"
              >
                {{ n.name || n.tag }}<span v-if="n.tag === row.active_node"> ·在用</span>
              </el-tag>
              <el-tag v-if="poolMore(row.nodes) > 0" size="small" type="info" effect="plain">
                +{{ poolMore(row.nodes) }}
              </el-tag>
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="代理地址" min-width="260">
        <template #default="{ row }">
          <div v-if="row.status === 'active' && row.proxy" class="flex items-start gap-1.5">
            <code class="text-[11px] break-all">{{ row.proxy }}</code>
            <el-button
              class="shrink-0"
              size="small"
              text
              :icon="CopyDocument"
              title="复制代理地址"
              @click="onCopyProxy(row.proxy)"
            />
          </div>
          <span v-else class="text-xs text-muted">凭据已作废</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260">
        <template #default="{ row }">
          <div class="ops flex flex-wrap gap-1.5">
            <template v-if="row.status === 'active'">
              <el-button type="danger" plain size="small" @click="onRevoke(row.username)">
                停用
              </el-button>
              <el-button size="small" :icon="Connection" @click="openPool(row.username, row.nodes)">
                节点池
              </el-button>
              <el-button size="small" :icon="Edit" @click="onResetPassword(row.username)">
                改密
              </el-button>
            </template>
            <template v-else>
              <el-button type="success" size="small" @click="onActivate(row.username)">
                恢复
              </el-button>
              <el-button size="small" :icon="Edit" @click="onResetPassword(row.username)">
                改密
              </el-button>
              <el-button
                type="danger"
                plain
                size="small"
                :icon="Delete"
                @click="onDelete(row.username)"
              >
                删除
              </el-button>
            </template>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <div class="mt-3 flex justify-end">
      <el-pagination
        :current-page="userPage"
        :page-size="userPageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="usersTotal"
        size="small"
        background
        layout="total, sizes, prev, pager, next"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>

    <p v-if="status?.locked" class="m-0 mt-2.5 text-xs text-muted">
      当前没有任何启用用户：代理端口处于封闭模式，所有流量都会被拒绝。
    </p>

    <el-dialog v-model="poolDialog" :title="'节点池 · ' + poolUser" width="560px">
      <p class="m-0 mb-2.5 text-xs text-muted">
        顺序即优先级：越靠前越优先。池可以为空；改动需手动点"重载"后生效。
      </p>

      <div v-if="poolTags.length === 0" class="mb-2.5 text-xs text-muted">
        池为空：该用户暂时不进入代理。
      </div>
      <ul v-else class="m-0 mb-2.5 list-none space-y-1.5 p-0">
        <li
          v-for="(tag, i) in poolTags"
          :key="tag"
          class="flex items-center gap-2 rounded-md border border-line bg-card px-2.5 py-1.5"
        >
          <span class="w-5 shrink-0 text-xs text-muted">{{ i + 1 }}</span>
          <span class="min-w-0 flex-1 truncate">{{ nodeName(tag) }}</span>
          <code class="shrink-0">{{ tag }}</code>
          <el-button
            size="small"
            text
            :icon="Top"
            :disabled="i === 0"
            title="上移"
            @click="movePool(i, -1)"
          />
          <el-button
            size="small"
            text
            :icon="Bottom"
            :disabled="i === poolTags.length - 1"
            title="下移"
            @click="movePool(i, 1)"
          />
          <el-button
            size="small"
            text
            type="danger"
            :icon="Delete"
            title="移除"
            @click="removePool(i)"
          />
        </li>
      </ul>

      <el-select
        v-model="poolAddTag"
        class="w-full"
        filterable
        clearable
        placeholder="添加节点到池尾"
        @change="onAddMember"
      >
        <el-option
          v-for="o in addOptions"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>

      <template #footer>
        <el-button @click="poolDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingPool" @click="onConfirmPool">确定</el-button>
      </template>
    </el-dialog>
  </section>
</template>