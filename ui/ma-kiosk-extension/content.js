(() => {
  if (window.top !== window || document.getElementById("xiaorui-return-home")) return;
  const button = document.createElement("button");
  button.id = "xiaorui-return-home";
  button.type = "button";
  button.textContent = "返回小瑞";
  button.setAttribute("aria-label", "关闭音乐界面并返回小瑞");
  Object.assign(button.style, {
    position: "fixed",
    top: "max(12px, env(safe-area-inset-top))",
    right: "14px",
    zIndex: "2147483647",
    border: "1px solid rgba(143,227,195,.8)",
    borderRadius: "16px",
    padding: "12px 18px",
    color: "#e8fff5",
    background: "rgba(18,42,43,.94)",
    font: "600 16px sans-serif",
    boxShadow: "0 2px 12px rgba(0,0,0,.28)",
    touchAction: "manipulation",
  });
  button.addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "xiaorui-return" });
  });
  document.documentElement.appendChild(button);
})();
