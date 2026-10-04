/**
 * 复制文本到剪贴板。
 *
 * 管理页常常是 http://内网IP:5003 打开的，不属于安全上下文，
 * navigator.clipboard 在这种页面上是 undefined，所以必须留一条
 * textarea + execCommand 的老路兜底，否则复制按钮会静默失效。
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;

  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 没有权限就往下走兜底方案 */
    }
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
