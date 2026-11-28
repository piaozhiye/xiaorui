chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "xiaorui-return" && sender.tab?.id !== undefined) {
    chrome.tabs.remove(sender.tab.id);
  }
});
