<script setup lang="ts">
import { h, ref, watch } from "vue";
import { Delete, Search, User } from "@element-plus/icons-vue";
import {
  createUser,
  deleteNode,
  loadNodes,
  nodePage,
  nodePageSize,
  nodeSearch,
  nodes,
  nodesTotal,
} from "../store";

/** 快捷加用户：目标节点 + 输入的用户名。 */
const dialogVisible = ref(false);
const targetTag = ref("");
const targetName = ref("");
const username = ref("");
const creating = ref(false);
const deleting = ref("");

// 搜索框：输入时防抖 300ms 再查，回车立即查。过滤在后端做，只拉当前页。
const keyword = ref(nodeSearch.value);
let searchTimer: number | undefined;

watch(keyword, (v) => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    void loadNodes(1, nodePageSize.value, v);
  }, 300);
});

async function onSearchNow() {
  window.clearTimeout(searchTimer);
  await loadNodes(1, nodePageSize.value, keyword.value);
}

/** 多行提示用 pre-line，避免 \n 被折叠成空格。 */
function multiline(text: string) {
  return h("div", { style: "white-space: pre-line" }, text);
}

function openAddUser(tag: string, name: string) {
  targetTag.value = tag;
  targetName.value = name;
  username.value = "";
  dialogVisible.value = true;
}

async function onConfirmAddUser() {
  const name = username.value.trim();
  if (!name) {
    ElMessage.warning("请输入用户名");
    return;
  }
  if (!targetTag.value) return;
  creating.value = true;
  try {
    const u = await createUser(name, targetTag.value);
    ElMessage.success({
      message: multiline(
        "已为用户 " + u.username + " 指定节点 " + u.node_tag + "\n代理: " + u.proxy,
      ),
      duration: 6000,
    });
    dialogVisible.value = false;
  } catch (e) {
    ElMessage.error("建号失败: " + (e as Error).message);
  } finally {
    creating.value = false;
  }
}

async function onDeleteNode(tag: string, name: string, users: number) {
  const bound = users || 0;
  const tip =
    "删除节点 " + name + "（" + tag + "）？此操作不可恢复。" +
    (bound > 0
      ? "\n该节点上有 " + bound + " 个启用用户，删除后会自动迁移到其他节点。" +
        "\n若没有可用的剩余节点，这些用户会失去节点。"
      : "");
  try {
    await ElMessageBox.confirm(multiline(tip), "删除确认", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  deleting.value = tag;
  try {
    const d = await deleteNode(tag);
    let msg = "已删除节点 " + tag;
    if (d.migrated && d.migrated.length) msg += "，迁移用户 " + d.migrated.length + " 个";
    ElMessage.success(msg);
    if (d.doomed && d.doomed.length) {
      ElMessage.warning("以下用户没有可用节点：" + d.doomed.join("、"));
    }
  } catch (e) {
    ElMessage.error("删除失败: " + (e as Error).message);
  } finally {
    deleting.value = "";
  }
}

async function onPageChange(page: number) {
  await loadNodes(page, nodePageSize.value);
}

async function onSizeChange(size: number) {
  await loadNodes(1, size);
}
</script>

<template>
  <section class="mb-5 rounded-[10px] border border-line bg-card p-4">
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2.5">
      <h2 class="m-0 text-[15px] font-semibold">节点列表</h2>
      <el-input
        v-model="keyword"
        placeholder="搜索名称 / tag / 服务器"
        clearable
        class="w-[240px]"
        :prefix-icon="Search"
        @keyup.enter="onSearchNow"
      />
    </div>
    <el-table
      :data="nodes"
      size="small"
      :empty-text="nodeSearch ? '没有匹配的节点' : '暂无节点'"
    >
      <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
      <el-table-column label="tag" min-width="130">
        <template #default="{ row }">
          <code>{{ row.tag }}</code>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.type" size="small" type="info">{{ row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="server" label="服务器" min-width="150" show-overflow-tooltip />
      <el-table-column prop="port" label="端口" width="90" />
      <el-table-column label="用户数" width="80">
        <template #default="{ row }">{{ row.users ?? 0 }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.healthy ? 'success' : 'danger'" size="small">
            {{ row.healthy ? "健康" : "异常" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200">
        <template #default="{ row }">
          <div class="ops flex flex-wrap gap-1.5">
            <el-button
              type="primary"
              plain
              size="small"
              :icon="User"
              @click="openAddUser(row.tag, row.name)"
            >
              加用户
            </el-button>
            <el-button
              type="danger"
              plain
              size="small"
              :icon="Delete"
              :loading="deleting === row.tag"
              @click="onDeleteNode(row.tag, row.name, row.users)"
            >
              删除
            </el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <div class="mt-3 flex justify-end">
      <el-pagination
        :current-page="nodePage"
        :page-size="nodePageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="nodesTotal"
        size="small"
        background
        layout="total, sizes, prev, pager, next"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>

    <el-dialog v-model="dialogVisible" title="为节点添加用户" width="420px">
      <p class="m-0 mb-3 text-sm text-muted">
        节点：{{ targetName }}
        <code class="ml-1">{{ targetTag }}</code>
      </p>
      <el-input
        v-model="username"
        placeholder="请输入用户名"
        clearable
        @keyup.enter="onConfirmAddUser"
      />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onConfirmAddUser">确定</el-button>
      </template>
    </el-dialog>
  </section>
</template>
