"use strict";

const state = {
  personas: [],
  personaId: null,
  persona: null,
  chats: [],
  chatId: null,
  sending: false,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

/* ------------------------------------------------------------- запуск */

async function boot() {
  const data = await api("/api/bootstrap");
  state.personas = data.personas;
  state.personaId = data.current_persona_id;

  if (!state.personaId) {
    $("setup").classList.remove("hidden");
    $("setup-name").focus();
    return;
  }
  $("app").classList.remove("hidden");
  await loadPersona();
  await loadChats();
}

async function loadPersona() {
  state.persona = await api(`/api/personas/${state.personaId}`);
  $("persona-name").textContent = state.persona.name;
  $("persona-avatar").textContent = [...state.persona.name][0]?.toUpperCase() || "?";
}

/* -------------------------------------------------------------- чаты */

function groupOf(isoDate) {
  const days = (Date.now() - new Date(isoDate).getTime()) / 86400000;
  if (days < 1) return "Сегодня";
  if (days < 7) return "7 дней";
  if (days < 30) return "30 дней";
  return "Давно";
}

async function loadChats() {
  state.chats = await api(`/api/personas/${state.personaId}/chats`);
  renderChats();
  if (state.chats.length) {
    const stillThere = state.chats.some((c) => c.id === state.chatId);
    await openChat(stillThere ? state.chatId : state.chats[0].id);
  } else {
    state.chatId = null;
    $("messages").replaceChildren();
    $("chat-title").textContent = "";
    $("chat-meta").textContent = "";
    $("system-block").classList.add("hidden");
  }
}

function renderChats() {
  const list = $("chat-list");
  list.replaceChildren();
  let lastGroup = null;
  const pinned = state.chats.filter((c) => c.pinned);
  const rest = state.chats.filter((c) => !c.pinned);

  const addItem = (chat) => {
    const button = document.createElement("button");
    button.className = "chat-item" + (chat.id === state.chatId ? " active" : "");
    button.textContent = chat.title;
    button.onclick = () => openChat(chat.id);
    list.appendChild(button);
  };

  if (pinned.length) {
    const header = document.createElement("div");
    header.className = "chat-group";
    header.textContent = "Закреплённые";
    list.appendChild(header);
    pinned.forEach(addItem);
    lastGroup = "__pinned__";
  }
  for (const chat of rest) {
    const group = groupOf(chat.created_at);
    if (group !== lastGroup) {
      const header = document.createElement("div");
      header.className = "chat-group";
      header.textContent = group;
      list.appendChild(header);
      lastGroup = group;
    }
    addItem(chat);
  }
}

async function openChat(id) {
  const data = await api(`/api/chats/${id}`);
  state.chatId = id;
  renderChats();

  $("chat-title").textContent = data.chat.title;
  updateMeta(data.spent_millicents, data.context_tokens, data.context_limit);

  const prompt = (data.chat.system_prompt || "").trim();
  $("system-block").classList.toggle("hidden", !prompt);
  $("system-text").textContent = prompt;

  const container = $("messages");
  container.replaceChildren();
  for (const message of data.messages) {
    container.appendChild(messageElement(message));
  }
  scrollDown(true);
}

function updateMeta(spent, tokens, limit) {
  const parts = [];
  if (tokens) {
    parts.push(`контекст ${tokens.toLocaleString("ru")} / ${(limit / 1e6).toFixed(0)}M`);
  }
  parts.push(`потрачено ${formatCost(spent)}`);
  $("chat-meta").textContent = parts.join(" · ");
}

function formatCost(millicents) {
  if (!millicents) return "0";
  if (millicents < 10) return `${millicents.toFixed(2)} мц`;
  if (millicents < 1000) return `${Math.round(millicents)} мц`;
  if (millicents < 100000) return `${(millicents / 1000).toFixed(2)} ¢`;
  return `$${(millicents / 100000).toFixed(2)}`;
}

/* --------------------------------------------------------- сообщения */

function renderBody(element, text) {
  element.replaceChildren();
  const parts = text.split("```");
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const pre = document.createElement("pre");
      const newline = part.indexOf("\n");
      pre.textContent = newline >= 0 ? part.slice(newline + 1) : part;
      element.appendChild(pre);
    } else if (part) {
      element.appendChild(document.createTextNode(part));
    }
  });
}

function messageElement(message) {
  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-" + message.role;
  wrapper.dataset.id = message.id;

  if (message.reasoning_content) {
    wrapper.appendChild(reasoningElement(message.reasoning_content));
  }

  const body = document.createElement("div");
  body.className = "body";
  renderBody(body, message.content);
  wrapper.appendChild(body);

  if (message.cost_millicents) {
    const cost = document.createElement("div");
    cost.className = "cost";
    cost.textContent = formatCost(message.cost_millicents);
    wrapper.appendChild(cost);
  }
  return wrapper;
}

function reasoningElement(text) {
  const details = document.createElement("details");
  details.className = "reasoning";
  const summary = document.createElement("summary");
  summary.textContent = "Размышление";
  const content = document.createElement("div");
  content.textContent = text;
  details.append(summary, content);
  return details;
}

function scrollDown(force) {
  const scroll = $("scroll");
  const nearBottom =
    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 150;
  if (force || nearBottom) scroll.scrollTop = scroll.scrollHeight;
}

/* -------------------------------------------------------- отправка */

async function send() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.sending || !state.chatId) return;

  state.sending = true;
  $("send").disabled = true;
  input.value = "";
  resizeInput();

  const container = $("messages");
  container.appendChild(messageElement({ id: 0, role: "user", content: text }));
  scrollDown(true);

  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-assistant";
  const reasoning = reasoningElement("");
  reasoning.classList.add("hidden");
  const body = document.createElement("div");
  body.className = "body";
  wrapper.append(reasoning, body);
  container.appendChild(wrapper);

  let contentText = "";
  let reasoningText = "";

  try {
    const response = await fetch(`/api/chats/${state.chatId}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text }),
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        if (!raw.startsWith("data: ")) continue;
        const event = JSON.parse(raw.slice(6));

        if (event.type === "reasoning") {
          reasoningText += event.text;
          reasoning.classList.remove("hidden");
          reasoning.querySelector("div").textContent = reasoningText;
          scrollDown(false);
        } else if (event.type === "content") {
          contentText += event.text;
          renderBody(body, contentText);
          scrollDown(false);
        } else if (event.type === "done") {
          wrapper.dataset.id = event.message_id;
          const cost = document.createElement("div");
          cost.className = "cost";
          cost.textContent = formatCost(event.cost_millicents);
          wrapper.appendChild(cost);
          updateMeta(
            event.chat_spent_millicents,
            event.context_tokens,
            1_000_000
          );
        } else if (event.type === "error") {
          throw new Error(event.message);
        }
      }
    }
  } catch (error) {
    wrapper.classList.add("msg-error");
    body.textContent = "Ошибка: " + error.message;
    // Сервер убрал неотправленное сообщение, перечитываем чат целиком.
    setTimeout(() => openChat(state.chatId), 1500);
  } finally {
    state.sending = false;
    $("send").disabled = false;
    input.focus();
    scrollDown(false);
  }
}

/* ------------------------------------------------------------- меню */

function closeMenu() {
  $("persona-menu").classList.add("hidden");
  $("overlay").classList.add("hidden");
}

function openPersonaMenu() {
  const menu = $("persona-menu");
  menu.replaceChildren();

  for (const persona of state.personas) {
    const button = document.createElement("button");
    button.textContent = persona.name;
    if (persona.id === state.personaId) button.classList.add("current");
    button.onclick = async () => {
      closeMenu();
      if (persona.id === state.personaId) return;
      await api("/api/current-persona", {
        method: "POST",
        body: JSON.stringify({ persona_id: persona.id }),
      });
      state.personaId = persona.id;
      state.chatId = null;
      await loadPersona();
      await loadChats();
    };
    menu.appendChild(button);
  }

  const separator = document.createElement("div");
  separator.className = "sep";
  menu.appendChild(separator);

  const create = document.createElement("button");
  create.textContent = "Создать личность";
  create.onclick = () => {
    closeMenu();
    $("app").classList.add("hidden");
    $("setup").classList.remove("hidden");
    $("setup-name").value = "";
    $("setup-prompt").value = "";
    $("setup-thinking").checked = false;
    $("setup-name").focus();
  };
  menu.appendChild(create);

  const anchor = $("persona-button").getBoundingClientRect();
  menu.classList.remove("hidden");
  menu.style.left = anchor.left + "px";
  menu.style.bottom = window.innerHeight - anchor.top + 8 + "px";
  $("overlay").classList.remove("hidden");
}

/* --------------------------------------------------------- мелочи UI */

function resizeInput() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 220) + "px";
}

$("setup-create").onclick = async () => {
  const name = $("setup-name").value.trim();
  const errorBox = $("setup-error");
  if (!name) {
    errorBox.textContent = "Впиши имя";
    errorBox.classList.remove("hidden");
    return;
  }
  errorBox.classList.add("hidden");
  try {
    const created = await api("/api/personas", {
      method: "POST",
      body: JSON.stringify({
        name,
        system_prompt: $("setup-prompt").value,
        thinking_enabled: $("setup-thinking").checked,
      }),
    });
    const data = await api("/api/bootstrap");
    state.personas = data.personas;
    state.personaId = created.id;
    state.chatId = null;
    $("setup").classList.add("hidden");
    $("app").classList.remove("hidden");
    await loadPersona();
    await loadChats();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  }
};

$("new-chat").onclick = async () => {
  const chat = await api(`/api/personas/${state.personaId}/chats`, { method: "POST" });
  state.chatId = chat.id;
  await loadChats();
  $("input").focus();
};

$("toggle-sidebar").onclick = () => $("app").classList.toggle("collapsed");
$("persona-button").onclick = openPersonaMenu;
$("overlay").onclick = closeMenu;
$("send").onclick = send;

$("input").addEventListener("input", resizeInput);
$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

boot().catch((error) => {
  document.body.textContent = "Не удалось запуститься: " + error.message;
});
