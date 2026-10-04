<script setup lang="ts">
import { computed, h, ref } from "vue";
import { CopyDocument, Delete, Edit, Plus, Switch } from "@element-plus/icons-vue";
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
  status,
  switchNode,
  userPage,
  userPageSize,
  users,
  usersTotal,
} from "../store";

const newUser = ref("");
const creating = ref(false);

const switchDialog = ref(false);
const switchUser = ref("");
const switchTarget = ref("");
const switching = ref(false);

/** 换节点下拉用全量轻量节点列表：用户列表分页后不能只依赖当前页。 */
const options = computed(() =>
  allNodeOptions.value.map((n) => ({
    label: n.name + "（" + n.tag + "）",
    value: n.tag,
  })),
);

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
        "已创建 " + u.username + "，节点 " + u.node_tag + "\n代理: " + u.proxy,
      ),
      duration: 6000,
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
    ElMessage.success("已停用 " + name + "，其凭据已作废");
  } catch (e) {
    ElMessage.error("停用失败: " + (e as Error).message);
  }
}

async function onActivate(name: string) {
  try {
    await ElMessageBox.confirm(
      "恢复用户 " + name + "？将分配一个新节点并发放全新口令（旧口令不再有效）。",
      "恢复确认",
      { type: "warning", confirmButtonText: "恢复", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    const u = await activateUser(name);
    ElMessage.success({
      message: multiline("已恢复 " + name + "\n节点 " + u.node_tag + "\n" + u.proxy),
      duration: 6000,
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
      message: multiline(name + " 新口令: " + d.password + "\n" + d.proxy),
      duration: 8000,
    });
  } catch (e) {
    ElMessage.error("改密失败: " + (e as Error).message);
  }
}

function openSwitch(name: string, current: string) {
  switchUser.value = name;
  switchTarget.value = current;
  switchDialog.value = true;
  void loadNodeOptions();
}

async function onConfirmSwitch() {
  if (!switchTarget.value) {
    ElMessage.warning("请选择目标节点");
    return;
  }
  switching.value = true;
  try {
    await switchNode(switchUser.value, switchTarget.value);
    ElMessage.success(switchUser.value + " 已切换到 " + switchTarget.value);
    switchDialog.value = false;
  } catch (e) {
    ElMessage.error("切换失败: " + (e as Error).message);
  } finally {
    switching.value = false;
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
        建用户（自动分配节点）
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
      <el-table-column label="节点" min-width="160">
        <template #default="{ row }">
          {{ row.node_name || "-" }}
          <div v-if="row.node_tag">
            <code>{{ row.node_tag }}</code>
          </div>
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
      <el-table-column label="操作" width="250">
        <template #default="{ row }">
          <div class="ops flex flex-wrap gap-1.5">
            <template v-if="row.status === 'active'">
              <el-button type="danger" plain size="small" @click="onRevoke(row.username)">
                停用
              </el-button>
              <el-button size="small" :icon="Switch" @click="openSwitch(row.username, row.node_tag)">
                换节点
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

    <el-dialog v-model="switchDialog" title="切换节点" width="420px">
      <el-select v-model="switchTarget" placeholder="选择目标节点" class="w-full" filterable>
        <el-option
          v-for="o in options"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>
      <template #footer>
        <el-button @click="switchDialog = false">取消</el-button>
        <el-button type="primary" :loading="switching" @click="onConfirmSwitch">确定</el-button>
      </template>
    </el-dialog>
  </section>
</template>
