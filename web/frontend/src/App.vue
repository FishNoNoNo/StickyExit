<script setup lang="ts">
import { onMounted, onUnmounted } from "vue";
import StatCards from "./components/StatCards.vue";
import ImportPanel from "./components/ImportPanel.vue";
import NodesTable from "./components/NodesTable.vue";
import UsersPanel from "./components/UsersPanel.vue";
import { loadNodeOptions, loadNodes, loadStatus, loadUsers } from "./store";

let timer: number | undefined;

onMounted(async () => {
  try {
    await Promise.all([
      loadStatus(),
      loadNodes(),
      loadUsers(),
      loadNodeOptions(),
    ]);
  } catch (e) {
    ElMessage.error("加载失败: " + (e as Error).message);
  }
  // 原页面每 5 秒刷新一次概览状态，这里保持一致。
  timer = window.setInterval(() => void loadStatus(), 5000);
});

onUnmounted(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <div class="mx-auto max-w-7xl p-6">
    <h1 class="m-0 mb-1 text-xl font-semibold">StickyExit</h1>
    <p class="m-0 mb-5 text-xs text-muted">
      节点解析 → 认证分配 → sing-box 输出代理 IP 端口
    </p>

    <StatCards />
    <ImportPanel />
    <NodesTable />
    <UsersPanel />
  </div>
</template>
