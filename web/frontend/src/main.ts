import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "./style.css";
import App from "./App.vue";

// 管理页统一走深色主题，和原页面配色保持一致。
document.documentElement.classList.add("dark");

createApp(App).mount("#app");
