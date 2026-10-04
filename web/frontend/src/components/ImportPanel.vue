<script setup lang="ts">
import { ref } from "vue";
import { Promotion, Refresh } from "@element-plus/icons-vue";
import { importNodes, reloadSingBox } from "../store";

const kind = ref<"url" | "text">("url");
const url = ref("");
const text = ref("");
const importing = ref(false);
const reloading = ref(false);

async function onImport() {
  const source = kind.value === "url" ? url.value.trim() : text.value.trim();
  if (!source) {
    ElMessage.warning("请填写订阅链接或节点文本");
    return;
  }
  importing.value = true;
  try {
    // 导入是增量合并：已有 tag 更新、新 tag 追加，不会清空原有节点。
    const d = await importNodes(kind.value, source);
    ElMessage.success(
      "解析 " + d.count + " 个节点：新增 " + d.added + "，更新 " + d.updated,
    );
  } catch (e) {
    ElMessage.error("导入失败: " + (e as Error).message);
  } finally {
    importing.value = false;
  }
}

async function onReload() {
  reloading.value = true;
  try {
    await reloadSingBox();
    ElMessage.success("已重载 sing-box");
  } catch (e) {
    ElMessage.error("重载失败: " + (e as Error).message);
  } finally {
    reloading.value = false;
  }
}
</script>

<template>
  <section class="mb-5 rounded-[10px] border border-line bg-card p-4">
    <h2 class="m-0 mb-3 text-[15px] font-semibold">导入节点</h2>

    <el-radio-group v-model="kind" class="mb-2.5">
      <el-radio value="url">订阅链接</el-radio>
      <el-radio value="text">粘贴节点文本</el-radio>
    </el-radio-group>

    <div class="flex flex-col gap-2.5">
      <el-input
        v-if="kind === 'url'"
        v-model="url"
        placeholder="https://机场订阅链接 或 http://... 节点链接"
        clearable
      />
      <el-input
        v-else
        v-model="text"
        type="textarea"
        :rows="5"
        placeholder="粘贴 Clash proxies YAML、或每行一个 vmess:// vless:// ss:// trojan:// hy2:// 链接"
      />

      <div class="flex flex-wrap gap-2.5">
        <el-button type="primary" :icon="Promotion" :loading="importing" @click="onImport">
          导入
        </el-button>
        <el-button :icon="Refresh" :loading="reloading" @click="onReload">
          重载 sing-box
        </el-button>
      </div>
    </div>
  </section>
</template>
