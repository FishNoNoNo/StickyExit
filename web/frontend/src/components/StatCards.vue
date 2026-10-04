<script setup lang="ts">
import { computed } from "vue";
import { status } from "../store";

const cards = computed(() => {
  const s = status.value;
  return [
    { key: "nodes", label: "节点", value: s ? s.nodes : "-", tone: "" },
    { key: "healthy", label: "健康节点", value: s ? s.healthy_nodes : "-", tone: "" },
    { key: "users", label: "启用用户", value: s ? s.users : "-", tone: "" },
    { key: "revoked", label: "已停用", value: s ? (s.revoked_users ?? 0) : "-", tone: "" },
    {
      key: "alive",
      label: "sing-box",
      value: s ? (s.sing_box_alive ? "运行中" : "未运行") : "-",
      tone: s ? (s.sing_box_alive ? "text-ok" : "text-bad") : "",
    },
    { key: "listen", label: "监听", value: s ? s.listen : "-", tone: "", small: true },
  ];
});
</script>

<template>
  <div class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
    <div
      v-for="c in cards"
      :key="c.key"
      class="rounded-[10px] border border-line bg-card px-4 py-3.5"
    >
      <div
        class="font-semibold leading-[1.35]"
        :class="[c.small ? 'text-sm font-medium break-all' : 'text-[26px]', c.tone]"
      >
        {{ c.value }}
      </div>
      <div class="text-xs text-muted">{{ c.label }}</div>
    </div>
  </div>
</template>
