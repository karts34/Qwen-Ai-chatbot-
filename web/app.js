"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    modelList: $("model-list"), history: $("conversation-list"), input: $("message-input"),
    form: $("chat-form"), send: $("send-button"), stop: $("stop-button"),
    messages: $("messages"), welcome: $("welcome"), scroll: $("chat-scroll"),
    reasoning: $("reasoning-toggle"), sidebar: $("sidebar"), overlay: $("sidebar-overlay"),
  };
  const STORAGE_KEY = "local-chat.history.v1";
  const SETTINGS_KEY = "local-chat.settings.v1";
  const MAX_HISTORY = 40;
  const icons = {
    check: '<svg class="model-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
    chat: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z"/></svg>',
    assistant: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 10v4m5-8v12m5-15v18m5-11v4"/></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/></svg>',
  };
  let models = [];
  let serverReady = false;
  let checking = false;
  let selectedModel = "";
  let history = [];
  let active = null;
  let generation = null;
  let storageNoticeShown = false;
  let settings = {};
  let preferredReasoning = false;
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) {
      history = saved.filter(validConversation).slice(0, MAX_HISTORY);
      for (const conversation of history) {
        conversation.messages = conversation.messages.filter(validMessage).map((message) => ({
          role: message.role, content: message.content.slice(0, 100000),
          thinking: typeof message.thinking === "string" ? message.thinking.slice(0, 100000) : "",
          error: typeof message.error === "string" ? message.error.slice(0, 2000) : "",
          stopped: !!message.stopped || !!message.pending,
        }));
      }
    }
    settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    if (!settings || typeof settings !== "object" || Array.isArray(settings)) settings = {};
    selectedModel = typeof settings.model === "string" ? settings.model : "";
    preferredReasoning = settings.reasoning === true;
    ui.reasoning.checked = preferredReasoning;
  } catch { history = []; settings = {}; }

  function validMessage(value) {
    return value && (value.role === "user" || value.role === "assistant") && typeof value.content === "string";
  }
  function validConversation(value) {
    return value && typeof value.id === "string" && typeof value.model === "string" &&
      typeof value.title === "string" && Array.isArray(value.messages) && value.messages.length <= 200;
  }
  function createElement(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  }
  function makeId() {
    return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
  }
  function readableName(name) {
    return name.replace(/:latest$/, "");
  }
  function sizeLabel(bytes) {
    return Number.isFinite(bytes) && bytes > 0 ? (bytes / 1e9).toFixed(bytes < 1e9 ? 2 : 1) + " GB" : "Local model";
  }
  function modelFamily(name) {
    const family = name.split(/[/:]/).find(Boolean) || name;
    return family.charAt(0).toUpperCase();
  }
  function reasoningMode(name) {
    const model = models.find((item) => item.name === name);
    const mode = model && model.reasoning && model.reasoning.mode;
    if (["toggle", "required", "default", "none"].includes(mode)) return mode;
    if (/(^|\/)qwen3[^/]*thinking/i.test(name)) return "required";
    return /(^|\/)qwen3:/i.test(name) ? "toggle" : "none";
  }
  function reasoningSupported(name) { return reasoningMode(name) === "toggle"; }
  function showNotice(message) {
    $("notice-text").textContent = message;
    $("notice").hidden = false;
  }
  function announce(message) { $("screen-reader-status").textContent = message; }
  function saveSettings() {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ model: selectedModel, reasoning: preferredReasoning })); }
    catch { storageWarning(); }
  }
  function storageWarning() {
    if (storageNoticeShown) return;
    storageNoticeShown = true;
    showNotice("This browser cannot save more chat history. Your current chat will remain available until you close this page.");
  }
  function saveHistory() {
    if (active && active.messages.length) {
      active.updated = Date.now();
      const index = history.findIndex((conversation) => conversation.id === active.id);
      if (index >= 0) history.splice(index, 1);
      history.unshift(active);
      history = history.slice(0, MAX_HISTORY);
    }
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); }
    catch { storageWarning(); }
    renderHistory();
  }
  function newChat(model = selectedModel) {
    stopGeneration();
    active = { id: makeId(), model, title: "New chat", messages: [], created: Date.now(), updated: Date.now() };
    selectedModel = model;
    ui.input.value = "";
    $("notice").hidden = true;
    resizeInput();
    renderModels();
    renderHistory();
    renderConversation();
    updateControls();
    saveSettings();
    closeSidebar();
    if (!ui.input.disabled) ui.input.focus();
  }
  function renderModels() {
    ui.modelList.replaceChildren();
    if (!models.length) {
      ui.modelList.append(createElement("p", "sidebar-empty", checking ? "Looking for your models…" : serverReady ? "No local models are installed yet." : "Local models will appear when the server is ready."));
      return;
    }
    for (const model of models) {
      const button = createElement("button", "model-button" + (model.name === selectedModel ? " selected" : ""));
      button.type = "button";
      button.setAttribute("aria-pressed", String(model.name === selectedModel));
      button.title = model.name;
      const symbol = createElement("span", "model-symbol", modelFamily(model.name));
      const info = createElement("span", "model-info");
      info.append(createElement("span", "model-name", readableName(model.name)));
      const detail = createElement("span", "model-detail");
      detail.append(createElement("span", "", sizeLabel(model.size)));
      if (model.details && typeof model.details.parameter_size === "string") detail.append(createElement("span", "", model.details.parameter_size));
      info.append(detail);
      button.append(symbol, info);
      if (model.name === selectedModel) button.insertAdjacentHTML("beforeend", icons.check);
      button.addEventListener("click", () => {
        if (model.name !== selectedModel) newChat(model.name);
        else closeSidebar();
      });
      ui.modelList.append(button);
    }
  }
  function renderHistory() {
    ui.history.replaceChildren();
    $("clear-history").disabled = history.length === 0;
    if (!history.length) {
      ui.history.append(createElement("p", "sidebar-empty", "Your conversations will appear here."));
      return;
    }
    for (const conversation of history) {
      const button = createElement("button", "conversation-button" + (active && active.id === conversation.id ? " selected" : ""));
      button.type = "button";
      button.title = conversation.title;
      if (active && active.id === conversation.id) button.setAttribute("aria-current", "true");
      button.innerHTML = icons.chat;
      const label = createElement("span", "conversation-label");
      label.append(createElement("span", "conversation-title", conversation.title));
      label.append(createElement("span", "conversation-meta", readableName(conversation.model)));
      button.append(label);
      button.addEventListener("click", () => {
        stopGeneration();
        active = conversation;
        selectedModel = conversation.model;
        ui.input.value = "";
        resizeInput();
        renderModels();
        renderHistory();
        renderConversation();
        updateControls();
        saveSettings();
        closeSidebar();
      });
      ui.history.append(button);
    }
  }
  function renderConversation() {
    ui.messages.replaceChildren();
    const hasMessages = active && active.messages.length > 0;
    ui.welcome.hidden = hasMessages;
    ui.messages.hidden = !hasMessages;
    if (hasMessages) for (const message of active.messages) ui.messages.append(buildMessage(message, active.model).root);
    scrollToBottom(true);
  }
  function buildMessage(message, model) {
    const root = createElement("article", "message " + message.role);
    const avatar = createElement("div", "message-avatar");
    avatar.setAttribute("aria-hidden", "true");
    if (message.role === "assistant") avatar.innerHTML = icons.assistant;
    else avatar.textContent = "You";
    const main = createElement("div", "message-main");
    const heading = createElement("div", "message-heading");
    heading.append(createElement("span", "message-author", message.role === "user" ? "You" : "Local Chat"));
    if (message.role === "assistant") heading.append(createElement("span", "message-model", readableName(model)));
    const content = createElement("div", "message-content", message.content);
    const thinking = createElement("details", "thinking");
    const summary = createElement("summary", "", "Reasoning");
    const thoughtContent = createElement("pre", "", message.thinking || "");
    thinking.append(summary, thoughtContent);
    thinking.hidden = !message.thinking;
    const typing = createElement("div", "typing");
    typing.innerHTML = '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span><span>Working on it…</span>';
    typing.hidden = !message.pending || !!message.content;
    const error = createElement("div", "message-error", message.error || "");
    error.hidden = !message.error;
    error.setAttribute("role", "alert");
    const actions = createElement("div", "message-actions");
    actions.hidden = message.role !== "assistant" || !!message.pending;
    const copy = createElement("button", "icon-button copy-message");
    copy.type = "button";
    copy.title = "Copy response";
    copy.setAttribute("aria-label", "Copy response");
    copy.innerHTML = icons.copy;
    copy.hidden = !message.content;
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(message.content);
        copy.title = "Copied";
        copy.setAttribute("aria-label", "Copied");
        announce("Response copied.");
        setTimeout(() => { copy.title = "Copy response"; copy.setAttribute("aria-label", "Copy response"); }, 2000);
      } catch { showNotice("Copy is unavailable in this browser. Select the response text to copy it."); }
    });
    const stopped = createElement("span", "", "Generation stopped");
    stopped.hidden = !message.stopped;
    actions.append(copy, stopped);
    main.append(heading, thinking, content, typing, error, actions);
    root.append(avatar, main);
    return { root, content, thinking, summary, thoughtContent, typing, error, actions, copy, stopped };
  }
  function updateMessage(elements, message) {
    elements.content.textContent = message.content;
    elements.thoughtContent.textContent = message.thinking || "";
    elements.thinking.hidden = !message.thinking;
    elements.summary.textContent = message.pending && !message.content ? "Reasoning…" : "Reasoning";
    elements.typing.hidden = !message.pending || !!message.content;
    elements.error.textContent = message.error || "";
    elements.error.hidden = !message.error;
    elements.actions.hidden = !!message.pending;
    elements.copy.hidden = !message.content;
    elements.stopped.hidden = !message.stopped;
  }
  function updateControls() {
    const installed = models.some((model) => model.name === selectedModel);
    const canChat = serverReady && installed;
    const mode = reasoningMode(selectedModel);
    const alwaysReasons = mode === "required" || mode === "default";
    const supportsReasoning = reasoningSupported(selectedModel);
    ui.input.disabled = !canChat;
    ui.input.placeholder = canChat ? "Message " + readableName(selectedModel) + "…" : serverReady ? "Choose an installed model to start…" : "Waiting for the local server…";
    ui.send.hidden = !!generation;
    ui.stop.hidden = !generation;
    ui.send.disabled = !canChat || !ui.input.value.trim() || !!generation;
    ui.reasoning.checked = alwaysReasons || (supportsReasoning && preferredReasoning);
    ui.reasoning.disabled = !!generation || !canChat || !supportsReasoning;
    ui.reasoning.nextElementSibling.textContent = mode === "default" ? "Model reasoning" : alwaysReasons ? "Always reasons" : supportsReasoning ? "Reasoning" : "Direct answers";
    ui.reasoning.parentElement.title = mode === "default" ? "This model uses its default reasoning behavior." : alwaysReasons ? "This model always uses reasoning before answering." : supportsReasoning ? "Enable reasoning before answering." : "This model responds without separate reasoning.";
    $("active-model-name").textContent = selectedModel ? readableName(selectedModel) : "Choose a local model";
    $("active-model-description").textContent = selectedModel && !installed ? "This conversation’s model is no longer installed." : "A little more privacy. A lot of possibility.";
    $("composer-model").textContent = selectedModel ? readableName(selectedModel) : "No model selected";
    $("composer-model").title = selectedModel;
  }
  function updateConnection(error = "") {
    const ready = serverReady && models.length > 0;
    const badge = $("local-badge");
    badge.classList.toggle("ready", ready);
    badge.classList.toggle("error", !serverReady && !checking);
    $("local-badge-label").textContent = ready ? "Offline ready" : checking ? "Connecting" : serverReady ? "Local server ready" : "Unavailable";
    $("sidebar-status-dot").className = "status-dot" + (serverReady ? " ready" : checking ? "" : " error");
    $("sidebar-status").textContent = ready ? models.length + " local model" + (models.length === 1 ? "" : "s") + " available" : checking ? "Connecting to local server" : serverReady ? "Local server connected" : "Local server unavailable";
    const connectionMessage = $("connection-message");
    connectionMessage.hidden = !error && (models.length > 0 || checking);
    connectionMessage.textContent = error || "Install a local model, then refresh this list.";
    updateControls();
  }
  async function getJSON(path) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(path, { signal: controller.signal, cache: "no-store" });
      let data;
      try { data = await response.json(); }
      catch { throw new Error("The local server returned an unreadable response."); }
      if (!response.ok) throw new Error(data.error || "The local server is unavailable.");
      return data;
    } finally { clearTimeout(timeout); }
  }
  async function refreshModels() {
    if (checking) return;
    checking = true;
    $("refresh-models").classList.add("spinning");
    updateConnection();
    const results = await Promise.allSettled([getJSON("/api/status"), getJSON("/api/models")]);
    const statusResult = results[0];
    const modelResult = results[1];
    serverReady = statusResult.status === "fulfilled" && statusResult.value.ok === true;
    let error = "";
    if (modelResult.status === "fulfilled" && Array.isArray(modelResult.value.models)) {
      models = modelResult.value.models.filter((model) => model && typeof model.name === "string");
    } else {
      models = [];
      error = modelResult.status === "rejected" ? modelResult.reason.message : "The model list is unavailable.";
    }
    if (!serverReady) {
      error = statusResult.status === "rejected" ? statusResult.reason.message : statusResult.value.error || "Start the local model server, then refresh.";
    }
    checking = false;
    $("refresh-models").classList.remove("spinning");
    if (models.length && (!selectedModel || (!active.messages.length && !models.some((model) => model.name === selectedModel)))) {
      selectedModel = (models.find((model) => model.name === "qwen3:1.7b") || models[0]).name;
      active.model = selectedModel;
      saveSettings();
    }
    renderModels();
    updateConnection(error);
  }
  function resizeInput() {
    ui.input.style.height = "auto";
    ui.input.style.height = Math.min(ui.input.scrollHeight, 190) + "px";
  }
  function scrollToBottom(force = false) {
    const nearBottom = ui.scroll.scrollHeight - ui.scroll.scrollTop - ui.scroll.clientHeight < 180;
    if (force || nearBottom) ui.scroll.scrollTop = ui.scroll.scrollHeight;
  }
  function stopGeneration() {
    if (!generation) return;
    const current = generation;
    current.stopped = true;
    current.controller.abort();
    current.message.pending = false;
    current.message.stopped = true;
    updateMessage(current.elements, current.message);
    generation = null;
    saveHistory();
    updateControls();
    announce("Generation stopped.");
  }
  async function sendMessage(event) {
    if (event) event.preventDefault();
    const prompt = ui.input.value.trim();
    if (!prompt || generation || ui.input.disabled) return;
    $("notice").hidden = true;
    if (!active || active.model !== selectedModel) newChat(selectedModel);
    if (active.messages.length >= 98) { showNotice("This conversation is full. Start a new chat to continue."); return; }
    const conversation = active;
    const existingLength = conversation.messages.reduce((total, message) => total + message.content.length, 0);
    if (existingLength + prompt.length > 64000) { showNotice("This conversation has reached its text limit. Start a new chat to continue."); return; }
    const userMessage = { role: "user", content: prompt };
    conversation.messages.push(userMessage);
    if (conversation.messages.length === 1) conversation.title = prompt.replace(/\s+/g, " ").slice(0, 70);
    const requestMessages = conversation.messages.filter((message) => message.role === "user" || message.content).map(({ role, content }) => ({ role, content }));
    const reply = { role: "assistant", content: "", thinking: "", pending: true };
    conversation.messages.push(reply);
    ui.input.value = "";
    resizeInput();
    ui.welcome.hidden = true;
    ui.messages.hidden = false;
    ui.messages.append(buildMessage(userMessage, selectedModel).root);
    const elements = buildMessage(reply, selectedModel);
    ui.messages.append(elements.root);
    const current = { controller: new AbortController(), message: reply, elements, conversation, stopped: false };
    generation = current;
    saveHistory();
    updateControls();
    scrollToBottom(true);
    announce("Generating a response.");
    let finished = false;
    function processEvent(line) {
      if (!line.trim()) return;
      let eventData;
      try { eventData = JSON.parse(line); }
      catch { throw new Error("The response stream could not be read. Please try again."); }
      if (!eventData || typeof eventData !== "object" || typeof eventData.type !== "string") throw new Error("The response stream could not be read. Please try again.");
      if (eventData.type === "token" && typeof eventData.content === "string") reply.content += eventData.content;
      else if (eventData.type === "thinking" && typeof eventData.content === "string") reply.thinking += eventData.content;
      else if (eventData.type === "error") throw new Error(eventData.error || "The model could not generate a response.");
      else if (eventData.type === "done") {
        finished = true;
        if (eventData.done_reason === "length") reply.error = reply.content ? "This reply reached the model’s output limit. Ask it to continue for more detail." : "The model reached its output limit while reasoning. Open Reasoning to see its progress, or try a shorter prompt.";
      }
      updateMessage(elements, reply);
      scrollToBottom();
    }
    try {
      const requestBody = { model: conversation.model, messages: requestMessages };
      if (reasoningSupported(conversation.model)) requestBody.think = preferredReasoning;
      const response = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
        signal: current.controller.signal,
      });
      if (!response.ok) {
        let errorData = {};
        try { errorData = await response.json(); } catch { /* The HTTP status still gives a useful failure. */ }
        throw new Error(errorData.error || "The local server could not start this response (" + response.status + ").");
      }
      if (!response.body) throw new Error("This browser could not read the response stream.");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (current.stopped) break;
        if (done) { buffer += decoder.decode(); break; }
        buffer += decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          processEvent(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
        }
      }
      if (!current.stopped && buffer.trim()) processEvent(buffer);
      if (!current.stopped && !finished) throw new Error("The response was interrupted. Please try again.");
      if (!current.stopped && !reply.content && !reply.thinking) reply.error = "The model returned an empty response. Try a different prompt.";
      else if (!current.stopped && !reply.content && reply.thinking && !reply.error) reply.error = "The model returned reasoning without a final answer. Open Reasoning to read it, or try a shorter prompt.";
    } catch (error) {
      if (!current.stopped) {
        reply.error = error.name === "AbortError" ? "The response was interrupted." : error.message || "Could not reach the local model. Check the server and try again.";
        announce("The response could not be completed.");
      }
    } finally {
      reply.pending = false;
      if (generation === current) generation = null;
      updateMessage(elements, reply);
      if (active === conversation) saveHistory();
      else {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); } catch { storageWarning(); }
      }
      updateControls();
      if (!current.stopped && !reply.error) announce("Response complete.");
      scrollToBottom();
      if (active === conversation && !ui.input.disabled) ui.input.focus();
    }
  }
  function closeSidebar() {
    ui.sidebar.classList.remove("open");
    ui.overlay.hidden = true;
    $("menu-button").setAttribute("aria-expanded", "false");
  }
  $("menu-button").addEventListener("click", () => {
    const opened = ui.sidebar.classList.toggle("open");
    ui.overlay.hidden = !opened;
    $("menu-button").setAttribute("aria-expanded", String(opened));
  });
  ui.overlay.addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
  ui.form.addEventListener("submit", sendMessage);
  ui.input.addEventListener("input", () => { resizeInput(); updateControls(); });
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); }
  });
  ui.stop.addEventListener("click", stopGeneration);
  $("new-chat").addEventListener("click", () => newChat());
  $("refresh-models").addEventListener("click", refreshModels);
  ui.reasoning.addEventListener("change", () => { preferredReasoning = ui.reasoning.checked; saveSettings(); });
  $("dismiss-notice").addEventListener("click", () => { $("notice").hidden = true; });
  document.querySelectorAll("[data-prompt]").forEach((card) => {
    card.addEventListener("click", () => {
      if (ui.input.disabled) { showNotice(serverReady ? "Choose an installed model in the sidebar to start chatting." : "The local model server is unavailable. Start it, then refresh your models."); return; }
      ui.input.value = card.dataset.prompt;
      resizeInput();
      updateControls();
      ui.input.focus();
    });
  });
  $("clear-history").addEventListener("click", () => {
    const dialog = $("clear-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else if (window.confirm("Clear all chat history saved in this browser?")) clearHistory();
  });
  $("clear-dialog").addEventListener("close", () => { if ($("clear-dialog").returnValue === "clear") clearHistory(); });
  function clearHistory() {
    stopGeneration();
    history = [];
    try { localStorage.removeItem(STORAGE_KEY); } catch { storageWarning(); }
    newChat();
    announce("Chat history cleared.");
  }
  window.addEventListener("beforeunload", () => {
    if (generation) { generation.message.stopped = true; generation.message.pending = false; saveHistory(); }
  });
  newChat();
  refreshModels();
  setInterval(() => { if (!generation && !document.hidden) refreshModels(); }, 30000);
})();
