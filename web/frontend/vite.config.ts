import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [
    vue(),
    tailwindcss(),
    // Element Plus 按需引入：只打包模板里真正用到的组件，
    // 以及脚本里用到的 ElMessage / ElMessageBox（样式一并自动注入）。
    AutoImport({ resolvers: [ElementPlusResolver()], dts: "src/auto-imports.d.ts" }),
    Components({ resolvers: [ElementPlusResolver()], dts: "src/components.d.ts" }),
  ],
  // 生产构建产物由后端 FastAPI 托管：index.html 由 / 返回，静态资源挂在 /static 下，
  // 所以构建时资源前缀必须是 /static/。开发模式仍用根路径，方便本地调试。
  base: command === "build" ? "/static/" : "/",
  build: {
    // 直接输出到后端托管的目录 web/static
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    // 本地开发时把 /api 转发给 FastAPI（python main.py 默认监听 5003）
    proxy: {
      "/api": {
        target: "http://127.0.0.1:5003",
        changeOrigin: true,
      },
    },
  },
}));
